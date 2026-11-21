"""Business-state tests with temporary storage and mocked official browser UIs."""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
from copy import deepcopy

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import adapters, artifacts, browser_bridge, browser_drafts, browser_draft_state, config, db, image_assets, main, service, worker


ACCOUNT = "测试账号"
NATIVE_ID = "827e33f7-2fdb-4028-ad4b-2dc4460002d2"
LOCAL_ID = "browser:xiaohongshu:" + NATIVE_ID
SOURCE = "https://example.invalid/source"


def payload(platform="xiaohongshu", body="先核实事实，再审核内容，最后由用户发布。"):
    return {"strategy": "一平台一份作品", "works": [{"platform": platform, "format": "note" if platform == "xiaohongshu" else "article", "title": "审核内容的三个步骤", "body": body, "tags": [], "image_prompts": ["中性的三个步骤示意图"], "source_urls": [SOURCE]}]}


def saved_result(remote_id=LOCAL_ID, account=ACCOUNT, **changes):
    result = {"state": "browser_saved", "destination": "browser", "remote_id": remote_id, "account_label": account, "error": None}
    result.update(changes)
    return result


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")

    def forbidden(*args, **kwargs):
        pytest.fail("A browser draft service test attempted real login/network/provider access")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(config, "get_wechat_credentials", forbidden)
    monkeypatch.setattr(image_assets, "generate_work_assets", lambda *args, **kwargs: {"status": "ready", "assets": [], "error": None})
    monkeypatch.setattr(browser_bridge, "_load_playwright", forbidden)
    monkeypatch.setattr(browser_bridge, "run_in_page", forbidden)
    monkeypatch.setattr(browser_drafts, "open_browser_draft", forbidden, raising=False)
    calls = []

    def disconnected(draft, version, topic, settings):
        calls.append(draft["id"])
        return {"state": "needs_connection", "error": "测试模拟：账号尚未接入"}

    monkeypatch.setattr(artifacts, "sync_platform_draft", disconnected)
    db.init_db()
    yield {"runtime": runtime, "vault": vault, "monkeypatch": monkeypatch, "calls": calls}


@pytest.fixture
def client(world):
    monkeypatch = world["monkeypatch"]
    monkeypatch.setattr(worker, "start_worker", lambda: None)
    monkeypatch.setattr(worker, "stop_worker", lambda: None)
    monkeypatch.setattr(browser_bridge, "stop_browser", lambda: None)
    monkeypatch.setattr(adapters, "get_health", lambda settings: {"cloud": {"enabled": False}})
    with TestClient(main.app) as client:
        bootstrap = authenticated_bootstrap(client)
        assert bootstrap.status_code == 200
        client.headers["X-Workbench-Token"] = bootstrap.json()["token"]
        yield client


def completed(world, platform="xiaohongshu"):
    topic = service.create_topic({"title": "审核内容", "brief": "先核实后发布", "platforms": [platform], "sources": [{"url": SOURCE, "title": "测试来源", "note": ""}], "content_line": "A", "stance_ids": []})
    topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    job = service.generate(topic["id"], topic["revision"])
    assert service.claim_next_job()["id"] == job["id"]
    version = service.complete_job(job["id"], payload(platform), "fake-provider")
    draft = next(item for item in service.state()["drafts"] if item["version_id"] == version["id"])
    world["calls"].clear()
    return topic, version, draft


def set_draft(draft, **values):
    assert set(values) <= {"state", "destination", "remote_id", "account_label"}
    with db.transaction() as connection:
        assignments = ",".join(key + "=?" for key in values)
        connection.execute("UPDATE drafts SET " + assignments + " WHERE id=?", (*values.values(), draft["id"]))
    return service.get_draft(draft["id"])


def evidence(world, topic, version, platform="xiaohongshu", remote_id=NATIVE_ID, content=None, account=ACCOUNT, unknown=False):
    ledger = browser_draft_state.BrowserDraftLedger(world["runtime"])
    ticket = ledger.claim(topic["id"], version["id"], platform, content or service._work(version, platform), account)
    if remote_id:
        ledger.record_saved(ticket, remote_id)
    if unknown:
        ledger.mark_unknown(ticket)
    return ledger, ticket


@pytest.mark.parametrize("platform,native_id", [("wechat", "100000291"), ("toutiao", "7695197376592298531"), ("xiaohongshu", NATIVE_ID)])
def test_native_ledger_id_is_validated_then_prefixed_only_in_db(world, platform, native_id):
    topic, version, draft = completed(world, platform)
    ledger, _ = evidence(world, topic, version, platform=platform, remote_id=native_id)
    set_draft(draft, state="unknown", account_label=ACCOUNT)
    expected = "browser:" + platform + ":" + native_id

    def only_read(saved, *args):
        assert saved["remote_id"] == expected
        return {"state": "browser_saved" if platform == "xiaohongshu" else "synced", "destination": "browser" if platform == "xiaohongshu" else "platform", "remote_id": expected, "account_label": ACCOUNT}

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", only_read)
    world["monkeypatch"].setattr(browser_draft_state.BrowserDraftLedger, "claim", lambda *args: pytest.fail("Native ID recovery attempted a new claim"))
    assert service.sync_draft(draft["id"])["remote_id"] == expected
    assert ledger.load(topic["id"], version["id"], platform)["remote_id"] == native_id


@pytest.mark.parametrize("platform,native_id", [("wechat", "0"), ("wechat", "0001"), ("wechat", "1" * 33), ("toutiao", "local-id"), ("toutiao", "browser:toutiao:7695197376592298531"), ("xiaohongshu", "64abcdef1234567890abcdef"), ("xiaohongshu", "00000000-0000-0000-0000-000000000000"), ("xiaohongshu", "827e33f72fdb4028ad4b2dc4460002d2"), ("xiaohongshu", LOCAL_ID)])
def test_unconfirmed_native_id_shapes_cannot_recover_or_invoke_ui(world, platform, native_id):
    topic, version, draft = completed(world, platform)
    evidence(world, topic, version, platform=platform, remote_id=native_id)
    set_draft(draft, state="unknown", account_label=ACCOUNT)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: pytest.fail("Invalid native ID reached UI"))
    with pytest.raises(HTTPException) as rejected:
        service.sync_draft(draft["id"])
    assert rejected.value.status_code == 409
    assert service.get_draft(draft["id"])["remote_id"] is None


def review_data(account=ACCOUNT):
    return {"account_label": account, "ai_disclosure": "AI辅助生成，已经人工核实", "facts_confirmed": True}


def test_xiaohongshu_success_persists_browser_destination_and_truthful_notice(world):
    _, _, draft = completed(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: saved_result())
    result = service.sync_draft(draft["id"])
    assert result["state"] == "browser_saved" and result["destination"] == "browser"
    assert result["remote_id"] == LOCAL_ID
    overview = service.state()["overview"]
    assert overview["pending_review"] == 1 and overview["published"] == 0
    notices = service.state()["notifications"]
    assert any("专用 Edge" in item["title"] and "仅保存在本机" in item["body"] for item in notices)
    assert not any("小红书草稿已存入后台" in item["title"] for item in notices)
    reviewed = service.review_draft(draft["id"], review_data())
    assert reviewed["state"] == "reviewed" and reviewed["destination"] == "browser"
    assert service.state()["overview"]["pending_review"] == 0
    assert service.state()["overview"]["published"] == 0


@pytest.mark.parametrize("changes", [{"remote_id": None}, {"remote_id": "local-made-up-id"}, {"remote_id": "browser:toutiao:7517424010146334720"}, {"destination": "platform"}, {"account_label": ""}])
def test_incomplete_browser_saved_result_cannot_claim_success(world, changes):
    _, _, draft = completed(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: saved_result(**changes))
    assert service.sync_draft(draft["id"])["state"] == "unknown"
    assert not any("小红书草稿已保存" in item["title"] for item in service.state()["notifications"])


def test_xiaohongshu_cannot_be_mislabeled_as_cloud_synced(world):
    _, _, draft = completed(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: saved_result(state="synced", destination="platform"))
    result = service.sync_draft(draft["id"])
    assert result["state"] == "unknown"
    assert "仅保存在本机" in result["error"]
    assert not any("小红书草稿已存入后台" in item["title"] for item in service.state()["notifications"])


@pytest.mark.parametrize("platform", ["wechat", "toutiao"])
def test_wechat_and_toutiao_browser_ui_cloud_drafts_remain_synced_platform(world, platform):
    _, _, draft = completed(world, platform)
    remote_id = "browser:" + platform + ":7517424010146334720"
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: {"state": "synced", "destination": "platform", "remote_id": remote_id, "account_label": ACCOUNT})
    result = service.sync_draft(draft["id"])
    assert result["state"] == "synced" and result["destination"] == "platform"


@pytest.mark.parametrize("original_account", ["", ACCOUNT])
def test_unknown_db_id_recovers_bound_ledger_id_and_only_invokes_readback(world, original_account):
    topic, version, draft = completed(world)
    evidence(world, topic, version)
    set_draft(draft, state="unknown", account_label=original_account)
    calls = []

    def only_read(saved, frozen, parent, settings):
        calls.append(saved)
        assert saved["remote_id"] == LOCAL_ID
        assert saved["account_label"] == ACCOUNT
        assert saved["destination"] == "browser"
        assert frozen["id"] == version["id"]
        return saved_result()

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", only_read)
    world["monkeypatch"].setattr(browser_draft_state.BrowserDraftLedger, "claim", lambda *args: pytest.fail("Recovery must not claim a new write"))
    result = service.sync_draft(draft["id"])
    assert result["state"] == "browser_saved" and len(calls) == 1


@pytest.mark.parametrize("damage", ["wrong content", "wrong account", "no id", "unknown no id", "non-browser id"])
def test_unknown_recovery_never_uses_unbound_or_uncertain_evidence(world, damage):
    topic, version, draft = completed(world)
    options = {}
    if damage == "wrong content":
        options["content"] = {**service._work(version, "xiaohongshu"), "body": "不同的正文"}
    elif damage == "wrong account":
        options["account"] = "另一个账号"
    elif damage in ("no id", "unknown no id"):
        options["remote_id"] = None
        options["unknown"] = damage == "unknown no id"
    elif damage == "non-browser id":
        options["remote_id"] = "64abcdef1234567890abcdef"
    evidence(world, topic, version, **options)
    set_draft(draft, state="unknown", account_label=ACCOUNT)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: pytest.fail("Uncertain recovery attempted a UI operation"))
    with pytest.raises(HTTPException) as rejected:
        service.sync_draft(draft["id"])
    assert rejected.value.status_code == 409
    assert service.get_draft(draft["id"])["remote_id"] is None


def test_db_writeback_failure_recovers_saved_ledger_id_after_restart_without_recreation(world):
    topic, version, draft = completed(world)
    creates = []

    def first_save(saved, frozen, parent, settings):
        creates.append(True)
        evidence(world, topic, version)
        return saved_result()

    original_notice = service._notice

    def interrupted_notice(connection, topic_id, title, body):
        if "小红书草稿已保存" in title:
            raise RuntimeError("simulated interruption during DB result writeback")
        return original_notice(connection, topic_id, title, body)

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", first_save)
    world["monkeypatch"].setattr(service, "_notice", interrupted_notice)
    with pytest.raises(RuntimeError):
        service.sync_draft(draft["id"])
    interrupted = service.get_draft(draft["id"])
    assert interrupted["state"] == "syncing" and interrupted["remote_id"] is None
    world["monkeypatch"].setattr(service, "_notice", original_notice)
    service.recover_jobs()
    assert service.get_draft(draft["id"])["state"] == "unknown"

    def recovery_read(saved, *args):
        assert saved["remote_id"] == LOCAL_ID and saved["account_label"] == ACCOUNT
        return saved_result()

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", recovery_read)
    world["monkeypatch"].setattr(browser_draft_state.BrowserDraftLedger, "claim", lambda *args: pytest.fail("Restart recovery attempted another creation"))
    assert service.sync_draft(draft["id"])["state"] == "browser_saved"
    assert len(creates) == 1


@pytest.mark.parametrize("changes", [{"remote_id": "browser:xiaohongshu:64abcdef1234567890abcdee"}, {"account_label": "另一个账号"}])
def test_known_id_readback_cannot_replace_original_id_or_account(world, changes):
    _, _, draft = completed(world)
    set_draft(draft, state="unknown", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: saved_result(**changes))
    result = service.sync_draft(draft["id"])
    assert result["state"] == "unknown"
    assert result["remote_id"] == LOCAL_ID and result["account_label"] == ACCOUNT


@pytest.mark.parametrize("state_name,remote_id", [("local", LOCAL_ID), ("synced", LOCAL_ID), ("unknown", LOCAL_ID), ("browser_saved", None), ("browser_saved", "not-a-browser-id")])
def test_browser_review_requires_saved_state_and_true_browser_id(world, state_name, remote_id):
    _, _, draft = completed(world)
    set_draft(draft, state=state_name, destination="browser", remote_id=remote_id, account_label=ACCOUNT)
    with pytest.raises(HTTPException) as rejected:
        service.review_draft(draft["id"], review_data())
    assert rejected.value.status_code == 409


def test_scope_change_during_save_keeps_history_without_current_success_notice(world):
    topic, _, draft = completed(world)

    def changed_scope(*args):
        service.patch_topic(topic["id"], {"expected_revision": topic["revision"], "brief": "选题范围已改变"})
        return saved_result()

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", changed_scope)
    result = service.sync_draft(draft["id"])
    assert result["state"] == "stale"
    assert result["remote_id"] == LOCAL_ID and result["destination"] == "browser"
    assert not any("小红书草稿已保存" in item["title"] for item in service.state()["notifications"])


@pytest.mark.parametrize("state_name", ["browser_saved", "reviewed", "unknown", "stale"])
def test_open_saved_or_historical_draft_requires_readback_and_preserves_all_state(world, state_name):
    topic, version, draft = completed(world)
    set_draft(draft, state=state_name, destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    before = deepcopy(service.get_draft(draft["id"]))
    calls = []

    def opened(saved, frozen, parent, settings):
        calls.append(saved["id"])
        assert frozen["id"] == version["id"] and frozen["payload"] == version["payload"]
        assert parent["id"] == topic["id"]
        return {"state": "opened", "remote_id": LOCAL_ID, "account_label": ACCOUNT, "content_verified": True}

    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", opened)
    assert service.open_draft(draft["id"])["state"] == "opened"
    assert service.get_draft(draft["id"]) == before and calls == [draft["id"]]
    assert service.get_version(version["id"])["payload"] == version["payload"]


def test_open_stale_draft_uses_its_frozen_version_after_new_version_exists(world):
    topic, version, draft = completed(world)
    set_draft(draft, state="browser_saved", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    newer = service.create_version(topic["id"], payload(body="新版本与原草稿的正文不同。"), version["id"])
    assert newer["id"] != version["id"]
    assert service.get_draft(draft["id"])["state"] == "stale"

    def opened(saved, frozen, *args):
        assert frozen["id"] == version["id"]
        assert frozen["payload"]["works"][0]["body"] != newer["payload"]["works"][0]["body"]
        return {"state": "opened", "remote_id": LOCAL_ID, "account_label": ACCOUNT, "content_verified": True}

    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", opened)
    assert service.open_draft(draft["id"])["state"] == "opened"


@pytest.mark.parametrize("changes", [{"remote_id": "browser:xiaohongshu:another-id"}, {"account_label": "另一个账号"}, {"content_verified": False}, {"content_verified": None}, {"account_label": ""}])
def test_open_navigation_without_matching_content_and_live_account_is_not_success(world, changes):
    _, _, draft = completed(world)
    set_draft(draft, state="browser_saved", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    result = {"state": "opened", "remote_id": LOCAL_ID, "account_label": ACCOUNT, "content_verified": True, **changes}
    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", lambda *args: result)
    assert service.open_draft(draft["id"])["state"] == "unknown"
    assert service.get_draft(draft["id"])["state"] == "browser_saved"


@pytest.mark.parametrize("state_name,remote_id", [("needs_connection", LOCAL_ID), ("local", LOCAL_ID), ("unknown", None), ("synced", "api-media-id"), ("reviewed", "browser:toutiao:7517424010146334720")])
def test_open_rejects_unsaved_api_or_wrong_platform_ids_before_browser_operation(world, state_name, remote_id):
    _, _, draft = completed(world)
    set_draft(draft, state=state_name, remote_id=remote_id)
    with pytest.raises(HTTPException) as rejected:
        service.open_draft(draft["id"])
    assert rejected.value.status_code == 409


def test_open_failure_is_truthful_and_does_not_modify_content(world):
    _, version, draft = completed(world)
    set_draft(draft, state="browser_saved", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)

    def unavailable(*args):
        raise RuntimeError("simulated browser unavailable")

    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", unavailable)
    assert service.open_draft(draft["id"])["state"] == "unknown"
    assert service.get_version(version["id"])["payload"] == version["payload"]


@pytest.mark.parametrize("state_name", ["unknown", "needs_connection", "failed", "observed"])
def test_open_api_returns_409_for_every_unconfirmed_result(world, client, state_name):
    world["monkeypatch"].setattr(service, "open_draft", lambda draft_id: {"state": state_name, "error": "测试：账号或正文回读尚未确认"})
    response = client.post("/api/drafts/fake-draft/open")
    assert response.status_code == 409
    assert response.json()["detail"] == "测试：账号或正文回读尚未确认"


def test_open_api_success_requires_actual_business_readback_proof(world, client):
    _, _, draft = completed(world)
    set_draft(draft, state="browser_saved", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", lambda *args: {"state": "opened", "remote_id": LOCAL_ID, "account_label": ACCOUNT, "content_verified": True})
    response = client.post(f"/api/drafts/{draft['id']}/open")
    assert response.status_code == 200 and response.json()["state"] == "opened"
    assert service.get_draft(draft["id"])["state"] == "browser_saved"
    assert client.post(f"/api/drafts/{draft['id']}/open", headers={"X-Workbench-Token": ""}).status_code == 403


def test_open_api_does_not_report_success_for_content_mismatch(world, client):
    _, _, draft = completed(world)
    set_draft(draft, state="browser_saved", destination="browser", remote_id=LOCAL_ID, account_label=ACCOUNT)
    world["monkeypatch"].setattr(browser_drafts, "open_browser_draft", lambda *args: {"state": "opened", "remote_id": LOCAL_ID, "account_label": ACCOUNT, "content_verified": False})
    response = client.post(f"/api/drafts/{draft['id']}/open")
    assert response.status_code == 409
    assert "正文回读尚未确认" in response.json()["detail"]
