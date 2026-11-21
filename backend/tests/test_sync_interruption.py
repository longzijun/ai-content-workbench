"""An interrupted private-draft save must never become eligible for another add."""
from contextlib import contextmanager

import httpx
import pytest
from fastapi import HTTPException

from backend.app import adapters, artifacts, config, db, service


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")

    def forbidden(*args, **kwargs):
        pytest.fail("A draft interruption test attempted a real provider or platform call")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(config, "get_wechat_credentials", forbidden)
    db.init_db()
    return monkeypatch


def running_job():
    topic = service.create_topic({"title": "同步中断保护", "brief": "验证避免重复提交", "platforms": ["wechat"], "sources": [], "content_line": "A", "stance_ids": []})
    service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": 1})
    job = service.generate(topic["id"], 1)
    assert service.claim_next_job()["id"] == job["id"]
    payload = {"strategy": "草稿同步结果明确呈现", "works": [{"platform": "wechat", "format": "article", "title": "保存结果待核对", "body": "用户应在平台草稿箱确认保存结果。", "tags": [], "image_prompts": [], "source_urls": []}]}
    return job, payload


def only_draft(version):
    return next(draft for draft in service.state()["drafts"] if draft["version_id"] == version["id"])


def test_final_transaction_interruption_retains_unknown_and_blocks_second_add(isolated):
    job, payload = running_job()
    real_transaction = db.transaction
    create_calls = []
    fail_next_transaction = False

    def remote_saved(*args):
        nonlocal fail_next_transaction
        create_calls.append(True)
        fail_next_transaction = True
        return {"state": "synced", "remote_id": "platform-created-id"}

    @contextmanager
    def interrupted_transaction():
        nonlocal fail_next_transaction
        if fail_next_transaction:
            fail_next_transaction = False
            raise RuntimeError("simulated database interruption after platform save")
        with real_transaction() as connection:
            yield connection

    isolated.setattr(artifacts, "sync_platform_draft", remote_saved)
    isolated.setattr(db, "transaction", interrupted_transaction)
    version = service.complete_job(job["id"], payload, "test-provider")
    draft = only_draft(version)
    assert service.get_job(job["id"])["state"] == "succeeded"
    assert draft["state"] == "unknown"
    assert draft["remote_id"] is None  # The failed transaction never committed its remote result.
    assert len(create_calls) == 1
    with pytest.raises(HTTPException) as failure:
        service.sync_draft(draft["id"])
    assert failure.value.status_code == 409
    assert len(create_calls) == 1
    assert service.get_draft(draft["id"])["state"] == "unknown"


@pytest.mark.parametrize("state", ["syncing", "unknown"])
def test_known_remote_id_and_error_survive_completion_exception(isolated, state):
    job, payload = running_job()
    real_sync = service.sync_draft

    def interrupted(draft_id):
        with db.transaction() as connection:
            connection.execute("UPDATE drafts SET state=?,remote_id=?,remote_url=?,error=? WHERE id=?", (state, "known-platform-id", "https://example.com/private", "平台已返回ID，等待回读", draft_id))
        raise RuntimeError("simulated completion interruption")

    isolated.setattr(service, "sync_draft", interrupted)
    version = service.complete_job(job["id"], payload, "test-provider")
    draft = only_draft(version)
    assert draft["state"] == "unknown"
    assert draft["remote_id"] == "known-platform-id"
    assert draft["remote_url"] == "https://example.com/private"
    assert draft["error"] == "平台已返回ID，等待回读"

    calls = []

    def read_existing(draft, *args):
        calls.append(draft["remote_id"])
        assert draft["state"] == "unknown"
        return {"state": "synced", "remote_id": draft["remote_id"]}

    isolated.setattr(service, "sync_draft", real_sync)
    isolated.setattr(artifacts, "sync_platform_draft", read_existing)
    assert service.sync_draft(draft["id"])["state"] == "synced"
    assert calls == ["known-platform-id"]


@pytest.mark.parametrize("state", ["synced", "reviewed"])
def test_confirmed_or_reviewed_draft_is_not_downgraded_by_later_exception(isolated, state):
    job, payload = running_job()

    def interrupted_after_confirmation(draft_id):
        with db.transaction() as connection:
            connection.execute("UPDATE drafts SET state=?,remote_id=?,error=NULL WHERE id=?", (state, "confirmed-platform-id", draft_id))
        raise RuntimeError("simulated interruption after persisted confirmation")

    isolated.setattr(service, "sync_draft", interrupted_after_confirmation)
    version = service.complete_job(job["id"], payload, "test-provider")
    draft = only_draft(version)
    assert draft["state"] == state
    assert draft["remote_id"] == "confirmed-platform-id"
    assert draft["error"] is None
    assert service.get_job(job["id"])["state"] == "succeeded"


def test_preflight_failure_before_claim_remains_retryable(isolated):
    job, payload = running_job()

    def preflight_error(draft_id):
        raise RuntimeError("preflight check failed before any draft claim")

    isolated.setattr(service, "sync_draft", preflight_error)
    version = service.complete_job(job["id"], payload, "test-provider")
    draft = only_draft(version)
    assert draft["state"] == "failed"
    assert draft["remote_id"] is None
    assert "preflight" in draft["error"]
