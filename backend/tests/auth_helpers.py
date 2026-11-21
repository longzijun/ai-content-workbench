"""Explicit owner login for existing isolated business-chain HTTP tests."""
from backend.app import access


def bootstrap(client):
    access.init_schema()
    if not access.configured():
        access.setup_password("isolated-test-password-only")
    if access.current(client.cookies.get(access.COOKIE)) is None:
        response = client.post("/api/auth/login", json={"password": "isolated-test-password-only", "device_name": "isolated-tests"})
        assert response.status_code == 200, response.text
    return client.get("/api/bootstrap")
