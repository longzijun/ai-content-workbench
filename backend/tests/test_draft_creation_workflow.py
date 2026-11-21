"""Approval, deduplication and HTTP identity for the draft-first workflow."""
import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.app import config, db, draft_workflow, main, service
from backend.tests.auth_helpers import bootstrap

pytestmark = pytest.mark.local_content_only

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return service.create_topic({"title":"整理写作流程", "brief":"", "content_line":"A",
                                 "platforms":["wechat","toutiao"], "sources":[]})

def request(topic, request_id="isolated-request-1", **kw):
    return {"expected_revision":topic["revision"], "platforms":topic["platforms"], "request_id":request_id, **kw}

def test_empty_fact_base_approval_and_enqueue_are_atomic(world):
    result = draft_workflow.create(world["id"], request(world))
    assert result["topic"]["status"] == "approved"
    assert result["job"]["state"] == "queued"
    assert result["job"]["candidate_mode"] is True
    with db.connect() as connection:
        row = service._row(connection, "jobs", result["job"]["id"])
        assert json.loads(row["input_snapshot"])["_candidate_mode"] is True
        assert connection.execute("SELECT COUNT(*) FROM fact_bases").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0

def test_double_click_and_lost_response_reuse_persistent_job(world):
    first = draft_workflow.create(world["id"], request(world))
    assert draft_workflow.create(world["id"], request(world))["job"]["id"] == first["job"]["id"]
    assert draft_workflow.create(world["id"], request(world,"second-request"))["job"]["id"] == first["job"]["id"]
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (first["job"]["id"],))
    assert draft_workflow.create(world["id"], request(world))["job"]["id"] == first["job"]["id"]

def test_reused_key_with_changed_scope_is_rejected(world):
    draft_workflow.create(world["id"], request(world))
    with pytest.raises(HTTPException) as caught:
        draft_workflow.create(world["id"], request(world, platforms=["wechat"]))
    assert caught.value.status_code == 409

def test_stale_revision_never_approves_or_queues(world):
    with pytest.raises(HTTPException):
        draft_workflow.create(world["id"], request(world, expected_revision=2))
    assert service.get_topic(world["id"])["status"] == "pending"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

def test_missing_risk_confirmation_rolls_back_approval(world):
    topic = service.patch_topic(world["id"], {"expected_revision":1, "title":"股票投资建议与风险"})
    with pytest.raises(HTTPException):
        draft_workflow.create(topic["id"], request(topic))
    assert service.get_topic(topic["id"])["status"] == "pending"

def test_index_only_sources_do_not_require_manual_fact_form(world):
    topic = service.patch_topic(world["id"], {"expected_revision":1,
        "sources":[{"url":"https://www.toutiao.com/hot-event/hot-board/", "title":"热榜"}]})
    assert draft_workflow.create(topic["id"], request(topic))["job"]["state"] == "queued"

def test_candidate_retry_keeps_mode_and_parent_without_fact_gate(world):
    job = draft_workflow.create(world["id"], request(world))["job"]
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (job["id"],))
    retried = service.retry_job(job["id"])
    with db.connect() as connection:
        snap = json.loads(service._row(connection,"jobs",retried["id"])["input_snapshot"])
    assert snap["_candidate_mode"] is True
    assert snap["_candidate_parent_job"] == job["id"]
    assert snap["_generation_run"] == retried["id"]
    assert service.retry_job(job["id"])["id"] == retried["id"]

def test_http_requires_owner_session_and_token(world):
    with TestClient(main.app) as client:
        assert client.post(f"/api/topics/{world['id']}/create-drafts",json=request(world)).status_code == 401
        boot = bootstrap(client)
        token = boot.json()["token"]
        assert client.post(f"/api/topics/{world['id']}/create-drafts",json=request(world)).status_code == 403
        response = client.post(f"/api/topics/{world['id']}/create-drafts",json=request(world), headers={"x-workbench-token":token})
        assert response.status_code == 200, response.text
        assert response.json()["job"]["state"] == "queued"


def test_commit_rejects_formal_version_changed_since_preview(world):
    from backend.app import auto_materials, candidate_drafts
    job = draft_workflow.create(world["id"], request(world))["job"]
    claimed = service.claim_next_job()
    material = {"mode":"limited","author_text":"这篇内容说明工作稿的保存方式。", "evidence_urls":[],"facts":[]}
    snapshot = {**claimed["topic"], "_auto_materials": material}
    selected = {}
    for platform in world["platforms"]:
        selected[platform] = candidate_drafts.publish(snapshot, platform, {"strategy":"隔离测试", "works":[{
            "platform":platform,"format":"article","title":"保存写作中的工作稿", "body":"这篇内容说明工作稿的保存方式。",
            "tags":[],"source_urls":[],"image_prompts":[]}]})
    candidate_drafts.finish_job(job["id"])
    data = {"expected_version_id":None,"candidate_ids":{p:c["id"] for p,c in selected.items()},
            "expected_revisions":{p:c["revision"] for p,c in selected.items()}}
    first = candidate_drafts.commit(world["id"], data)["version"]
    with pytest.raises(HTTPException) as caught:
        candidate_drafts.commit(world["id"], data)
    assert caught.value.status_code == 409
    assert service.get_topic(world["id"])["current_version_id"] == first["id"]
    assert len(service.get_topic(world["id"])["versions"]) == 1
