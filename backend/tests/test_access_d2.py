"""Authentication migration and device boundaries, with no real credentials."""
import pytest
import json
from fastapi.testclient import TestClient

from backend.app import access, adapters, config, db, main

pytestmark = pytest.mark.local_content_only
PASSWORD = "isolated-owner-password"


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(adapters, "get_health", lambda *_: {})
    db.init_db()
    access.init_schema()
    with TestClient(main.app) as desktop, TestClient(main.app) as phone:
        yield desktop, phone


def sign_in(client, name="test-device"):
    response = client.post("/api/auth/login", json={"password": PASSWORD, "device_name": name})
    assert response.status_code == 200, response.text
    token = client.get("/api/bootstrap").json()["token"]
    client.headers["X-Workbench-Token"] = token
    return response


def test_anonymous_bootstrap_and_old_cookie_cannot_grant_access(clients):
    desktop, _ = clients
    with db.transaction() as connection:
        connection.execute("INSERT INTO sessions(id,token,expires_at) VALUES('legacy-cookie','legacy-token','2999-01-01T00:00:00+00:00')")
    desktop.cookies.set(access.COOKIE, "legacy-cookie")
    desktop.headers["X-Workbench-Token"] = "legacy-token"
    for path in ("/api/bootstrap", "/api/state", "/api/settings", "/api/platform-registry"):
        assert desktop.get(path).status_code == 401
    assert desktop.get("/api/auth/status").json() == {"configured": False, "authenticated": False}
    assert desktop.post("/api/auth/login", json={"password": PASSWORD}).status_code == 503
    assert desktop.post("/api/auth/setup", json={"password": PASSWORD}).status_code in (404, 405)


def test_devices_login_independently_and_revocation_invalidates_csrf(clients):
    desktop, phone = clients
    access.setup_password(PASSWORD)
    sign_in(desktop, "desktop")
    sign_in(phone, "phone")
    sessions = desktop.get("/api/auth/sessions").json()["sessions"]
    assert len(sessions) == 2
    phone_session = next(item for item in sessions if item["device_name"] == "phone")
    assert all("token" not in item for item in sessions)
    assert desktop.delete("/api/auth/sessions/" + phone_session["id"]).status_code == 200
    assert phone.get("/api/state").status_code == 401
    assert phone.post("/api/backup", json={}).status_code == 401
    assert desktop.get("/api/state").status_code == 200
    assert desktop.post("/api/auth/logout").status_code == 200
    assert desktop.get("/api/state").status_code == 401


def test_login_origin_machine_callback_and_wrong_password(clients, monkeypatch):
    desktop, _ = clients
    access.setup_password(PASSWORD)
    assert desktop.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert desktop.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    monkeypatch.delenv("WORKBENCH_MACHINE_TOKEN", raising=False)
    assert desktop.post("/api/lms/reload").status_code == 401
    sign_in(desktop)
    assert desktop.post("/api/lms/reload").status_code == 401
    assert desktop.get("/api/bootstrap", headers={"Host": "attacker.invalid"}).status_code == 400


def test_password_reset_revokes_devices_and_hash_is_not_plaintext(clients):
    desktop, _ = clients
    access.setup_password(PASSWORD)
    sign_in(desktop)
    with db.connect() as connection:
        saved = connection.execute("SELECT password_hash FROM auth_owner").fetchone()[0]
    assert PASSWORD not in saved and saved.startswith("scrypt$")
    access.setup_password("replacement-test-password", reset=True)
    assert desktop.get("/api/bootstrap").status_code == 401


def test_login_rate_limit_survives_client_changes(clients):
    desktop, phone = clients
    access.setup_password(PASSWORD)
    for index in range(10):
        client = desktop if index % 2 else phone
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert phone.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_https_login_cookie_is_secure(clients):
    access.setup_password(PASSWORD)
    with TestClient(main.app, base_url="https://localhost") as secure:
        response = sign_in(secure)
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_password_reset_during_verification_cannot_issue_old_password_session(clients, monkeypatch):
    desktop, _ = clients
    access.setup_password(PASSWORD)
    original = access._hash
    reset_done = False

    def resetting_hash(password, salt):
        nonlocal reset_done
        result = original(password, salt)
        if password == PASSWORD and not reset_done:
            reset_done = True
            access.setup_password("replacement-test-password", reset=True)
        return result

    monkeypatch.setattr(access, "_hash", resetting_hash)
    assert desktop.post("/api/auth/login", json={"password": PASSWORD}).status_code == 401
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


def test_private_network_identity_restricts_peer_but_does_not_replace_login(clients):
    policy = config.get_settings().runtime_dir / "private-network.json"
    policy.write_text(json.dumps({"origin": "https://workbench.private.ts.net", "owner_login": "owner@example.invalid"}), "utf-8")
    with TestClient(main.app, client=("100.100.1.2", 443)) as remote:
        assert remote.get("/api/auth/status").status_code == 403
        assert remote.get("/api/auth/status", headers={"tailscale-user-login": "other@example.invalid"}).status_code == 403
        headers = {"tailscale-user-login": "owner@example.invalid"}
        assert remote.get("/api/auth/status", headers=headers).status_code == 200
        assert remote.get("/api/bootstrap", headers=headers).status_code == 401
