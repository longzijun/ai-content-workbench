"""Workflow guarantees with isolated storage and no real providers or credentials."""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import adapters, artifacts, config, db, image_assets, main, service, worker


REAL_PLATFORM_SYNC = artifacts.sync_platform_draft
SOURCE_URL = "https://example.com/reference"


def payload(platforms=("wechat",), body="普通用户可以先确认任务范围，再审核生成内容。"):
    return {
        "strategy": "每个平台一份可审核作品。",
        "works": [
            {
                "platform": platform,
                "format": "article" if platform != "xiaohongshu" else "note",
                "title": "审核生成内容的三个步骤",
                "body": body,
                "tags": [],
                "image_prompts": ["中性背景与三个步骤的示意图"],
                "source_urls": [SOURCE_URL],
            }
            for platform in platforms
        ],
    }


def review_data(account="测试账号"):
    return {"account_label": account, "ai_disclosure": "AI辅助生成，已人工核实", "facts_confirmed": True}


def record_data(**updates):
    data = {
        "url": "https://example.com/published",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "confirmation": True,
    }
    data.update(updates)
    return data


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(worker, "start_worker", lambda: None)
    monkeypatch.setattr(worker, "stop_worker", lambda: None)
    monkeypatch.setattr(adapters, "get_health", lambda settings: {"cloud": {"enabled": False}})

    def forbidden(*args, **kwargs):
        pytest.fail("A workflow test attempted a real network/provider/credential operation")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(config, "get_wechat_credentials", forbidden)
    # Legacy business-chain tests do not verify rendering. Keep the newly added
    # image provider isolated; real file/media guarantees have dedicated tests.
    monkeypatch.setattr(image_assets, "generate_work_assets", lambda *args, **kwargs: {"status": "ready", "assets": [], "error": None})
    calls = []

    def disconnected(draft, version, topic, settings):
        calls.append(draft["id"])
        return {"state": "needs_connection", "error": "未存入平台后台，本机恢复副本已保留"}

    monkeypatch.setattr(artifacts, "sync_platform_draft", disconnected)
    with TestClient(main.app) as client:
        bootstrap = authenticated_bootstrap(client)
        assert bootstrap.status_code == 200
        client.headers["X-Workbench-Token"] = bootstrap.json()["token"]
        yield {"client": client, "runtime": runtime, "vault": vault, "calls": calls, "monkeypatch": monkeypatch}


def new_topic(platforms=("wechat",), approved=True):
    topic = service.create_topic({"title": "审核生成内容", "brief": "先核实后发布", "platforms": list(platforms), "sources": [{"url": SOURCE_URL, "title": "测试来源", "note": ""}], "content_line": "A", "stance_ids": []})
    if approved:
        topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    return topic


def completed(world, platforms=("wechat",), body=None):
    topic = new_topic(platforms)
    job = service.generate(topic["id"], topic["revision"])
    claimed = service.claim_next_job()
    assert claimed["id"] == job["id"]
    version = service.complete_job(job["id"], payload(platforms, body or "普通用户可以先确认任务范围，再审核生成内容。"), "test-provider")
    drafts = [item for item in service.state()["drafts"] if item["version_id"] == version["id"]]
    return service.get_topic(topic["id"]), version, drafts


def approve(client, version):
    data = {**review_data(), "platform": "wechat", "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    response = client.post(f"/api/versions/{version['id']}/approve", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_api_session_csrf_host_and_origin(world):
    client = world["client"]
    create = {"title": "测试", "platforms": ["wechat"]}
    with TestClient(main.app) as unauthenticated:
        assert unauthenticated.get("/api/state").status_code == 401
        assert unauthenticated.post("/api/topics", json=create).status_code == 401
    assert client.post("/api/topics", json=create, headers={"X-Workbench-Token": ""}).status_code == 403
    assert client.post("/api/topics", json=create, headers={"X-Workbench-Token": "incorrect"}).status_code == 403
    assert client.get("/api/bootstrap", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/api/bootstrap", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "http://testserver:8000"}).status_code == 403
    assert client.post("/api/topics", json=create, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/topics", json=create, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/topics", json=create, headers={"Origin": "http://testserver"}).status_code == 200


def test_generation_requires_current_approved_scope_and_one_active_job(world):
    client = world["client"]
    topic = new_topic(approved=False)
    route = f"/api/topics/{topic['id']}"
    assert client.post(route + "/generate", json={"expected_revision": 1}).status_code == 409
    assert service.state()["jobs"] == []
    assert client.post(route + "/decision", json={"decision": "approved", "expected_revision": 2}).status_code == 409
    assert client.post(route + "/decision", json={"decision": "approved", "expected_revision": 1}).status_code == 200
    assert client.post(route + "/generate", json={"expected_revision": 1}).status_code == 200
    assert client.post(route + "/generate", json={"expected_revision": 1}).status_code == 409


@pytest.mark.parametrize("invalid", ["missing", "extra", "duplicate"])
def test_payload_must_match_approved_platforms_exactly(world, invalid):
    topic, version, drafts = completed(world, ("wechat", "toutiao"))
    candidate = payload(("wechat", "toutiao"))
    if invalid == "missing":
        candidate["works"].pop()
    elif invalid == "extra":
        candidate["works"].extend(payload(("xiaohongshu",))["works"])
    else:
        candidate["works"].append(deepcopy(candidate["works"][0]))
    response = world["client"].post(f"/api/topics/{topic['id']}/versions", json={"payload": candidate, "base_version_id": version["id"]})
    assert response.status_code == 422
    assert service.get_topic(topic["id"])["current_version"]["id"] == version["id"]
    assert len(service.get_topic(topic["id"])["versions"]) == 1


def test_unconnected_platforms_keep_recovery_copy_without_claiming_delivery(world):
    topic, version, drafts = completed(world, ("wechat", "toutiao", "xiaohongshu"))
    assert len(drafts) == 3
    for draft in drafts:
        assert draft["state"] == "needs_connection"
        assert draft["destination"] == "platform"
        assert draft["remote_id"] is None
        assert draft["remote_url"] is None
        assert world["client"].post(f"/api/drafts/{draft['id']}/review", json=review_data()).status_code == 409
    state = service.state()
    assert state["overview"]["published"] == 0
    assert not any("已存入后台" in item["title"] for item in state["notifications"])
    content = world["client"].get(f"/api/drafts/{drafts[0]['id']}/content").json()
    assert content["version_id"] == version["id"]
    assert content["work"]["body"] == version["payload"]["works"][0]["body"]


def test_synced_without_remote_id_becomes_unknown_and_cannot_be_resubmitted(world):
    calls = []

    def dishonest(*args):
        calls.append(True)
        return {"state": "synced", "remote_url": "https://example.com/local"}

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", dishonest)
    topic, version, drafts = completed(world)
    draft = drafts[0]
    assert draft["state"] == "unknown"
    assert draft["remote_id"] is None
    assert world["client"].post(f"/api/drafts/{draft['id']}/sync", json={}).status_code == 409
    assert world["client"].post(f"/api/versions/{version['id']}/drafts", json={"platform": "wechat"}).status_code == 409
    assert len(calls) == 1


def test_platform_adapter_cannot_return_local_as_remote_delivery(world):
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda *args: {"state": "local"})
    _, _, drafts = completed(world)
    assert drafts[0]["state"] == "needs_connection"
    assert drafts[0]["remote_id"] is None


def test_approval_rejects_heatlist_only_topic(world):
    """批准入口就是闸门：热榜候选没有原文，批准时立刻拦，不放行到生产。

    2026-10-22 两次真实撞墙：用户批准热榜候选 → 点「开始生产」→ 才被 409。
    检查提前到批准入口后，批准失败状态不得变化，文案与 generate 一致。
    """
    topic = service.create_topic({"title": "热榜候选", "brief": "b",
                                  "platforms": ["wechat"],
                                  "sources": [{"url": "https://www.baidu.com/s?wd=x",
                                               "title": "百度搜索页", "note": ""}]})
    with pytest.raises(HTTPException) as exc_info:
        service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    assert "尚无原始报道" in str(exc_info.value.detail)
    assert service.get_topic(topic["id"])["status"] == "pending", "批准失败后状态不得变化"


def test_blocked_content_never_reaches_platform_adapter(world):
    with pytest.raises(HTTPException) as error:
        completed(world, body="TODO：待核实后填写。")
    assert error.value.status_code == 422
    assert "统一检查未通过" in error.value.detail
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert world["calls"] == []


def test_honest_uncertainty_section_is_not_a_placeholder(world):
    """「待核实问题」是诚实声明，不是没写完。

    2026-10-22 真实误伤：选题标题就叫「已知事实与待确认问题」，模型按
    brief 输出了「⚖️待核实问题：」小节，裸词匹配把整篇正文拦成 blocked。
    占位检测抓的应该是「这里要填东西」的标记形态（【待核实】、TODO），
    不是声明不确定性的正常内容。
    """
    body = ("⚠️PHP 曝出高危漏洞。\n\n⚖️待核实问题：\n受影响主机数量以官方最终统计为准。\n\n"
            "📌来源：澎湃新闻、财联社。")
    _, version, drafts = completed(world, body=body)
    blockers = [f for f in version["review"]["findings"] if f["severity"] == "blocker"]
    assert blockers == []
    assert version["review"]["status"] == "passed"


@pytest.mark.parametrize("snippet", ["联系方式【待核实】", "价格（待补充）", "此处为占位文本"])
def test_marked_placeholders_still_block(world, snippet):
    with pytest.raises(HTTPException) as error:
        completed(world, body=f"正文开始。{snippet}。正文结束。")
    assert error.value.status_code == 422
    assert "占位或待核实内容" in error.value.detail


def test_new_version_invalidates_drafts_and_approvals_and_preserves_frozen_payload(world):
    topic, old, drafts = completed(world)
    publication = approve(world["client"], old)
    candidate = payload(body="已经完成第二次修改，仍需审核。")
    response = world["client"].post(f"/api/topics/{topic['id']}/versions", json={"payload": candidate, "base_version_id": old["id"]})
    assert response.status_code == 200
    newer = response.json()
    assert newer["number"] == old["number"] + 1
    assert service.get_draft(drafts[0]["id"])["state"] == "stale"
    assert next(item for item in service.state()["publications"] if item["id"] == publication["id"])["state"] == "stale"
    assert service.get_version(old["id"])["payload"] == old["payload"]
    assert world["client"].post(f"/api/versions/{old['id']}/review", json={}).status_code == 409
    assert world["client"].post(f"/api/topics/{topic['id']}/versions", json={"payload": candidate, "base_version_id": old["id"]}).status_code == 409
    with db.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE versions SET payload=? WHERE id=?", ("{}", old["id"]))


def test_topic_edit_revokes_scope_and_discards_running_old_result(world):
    topic, version, drafts = completed(world)
    publication = approve(world["client"], version)
    old_job = service.generate(topic["id"], topic["revision"])
    assert service.claim_next_job()["id"] == old_job["id"]
    response = world["client"].patch(f"/api/topics/{topic['id']}", json={"brief": "改为讨论另一种任务范围", "expected_revision": topic["revision"]})
    assert response.status_code == 200
    revised = response.json()
    assert revised["status"] == "pending"
    assert revised["revision"] == topic["revision"] + 1
    assert service.job_cancelled(old_job["id"])
    assert service.complete_job(old_job["id"], payload(), "late-provider") is None
    assert service.get_job(old_job["id"])["state"] == "cancelled"
    assert service.get_job(old_job["id"])["output_version_id"] is None
    assert len(service.get_topic(topic["id"])["versions"]) == 1
    assert service.get_draft(drafts[0]["id"])["state"] == "stale"
    assert next(item for item in service.state()["publications"] if item["id"] == publication["id"])["state"] == "stale"
    assert world["client"].post(f"/api/jobs/{old_job['id']}/retry", json={}).status_code == 409


@pytest.mark.parametrize("running", [False, True])
def test_cancelled_jobs_cannot_deliver_late_results_and_retry_is_new_job(world, running):
    topic = new_topic()
    job = service.generate(topic["id"], topic["revision"])
    if running:
        assert service.claim_next_job()["id"] == job["id"]
    response = world["client"].post(f"/api/jobs/{job['id']}/cancel", json={})
    assert response.status_code == 200
    if running:
        assert service.complete_job(job["id"], payload(), "late-provider") is None
    assert service.get_job(job["id"])["state"] == "cancelled"
    assert service.get_topic(topic["id"])["current_version"] is None
    assert service.state()["drafts"] == []
    retried = world["client"].post(f"/api/jobs/{job['id']}/retry", json={})
    assert retried.status_code == 200
    assert retried.json()["id"] != job["id"]
    assert retried.json()["output_version_id"] is None


def test_restart_recovers_jobs_and_unknown_drafts_without_recreating(world):
    topic, _, drafts = completed(world)
    job = service.generate(topic["id"], topic["revision"])
    service.claim_next_job()
    with db.transaction() as connection:
        connection.execute("UPDATE drafts SET state='syncing' WHERE id=?", (drafts[0]["id"],))
    calls_before = len(world["calls"])
    service.recover_jobs()
    assert service.get_job(job["id"])["state"] == "failed"
    assert service.get_job(job["id"])["output_version_id"] is None
    assert service.get_draft(drafts[0]["id"])["state"] == "unknown"
    assert world["client"].post(f"/api/drafts/{drafts[0]['id']}/sync", json={}).status_code == 409
    assert len(world["calls"]) == calls_before


def test_real_remote_evidence_and_manual_review_required_before_published_record(world):
    calls = []

    def remote(draft, *args):
        calls.append(draft["id"])
        return {"state": "synced", "remote_id": "remote-draft-123", "remote_url": "https://example.com/private-draft", "account_label": "绑定的测试账号"}

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", remote)
    _, version, drafts = completed(world)
    draft = drafts[0]
    client = world["client"]
    assert draft["state"] == "synced"
    assert service.state()["overview"]["published"] == 0
    assert client.post(f"/api/drafts/{draft['id']}/record", json=record_data()).status_code in (409, 422)
    assert client.post(f"/api/drafts/{draft['id']}/review", json=review_data("另一个账号")).status_code == 409
    assert client.post(f"/api/drafts/{draft['id']}/review", json={**review_data("绑定的测试账号"), "facts_confirmed": False}).status_code == 422
    reviewed = client.post(f"/api/drafts/{draft['id']}/review", json=review_data("绑定的测试账号"))
    assert reviewed.status_code == 200
    assert reviewed.json()["state"] == "reviewed"
    assert client.post(f"/api/versions/{version['id']}/drafts", json={"platform": "wechat", "account_label": "另一个账号"}).json()["id"] == draft["id"]
    assert client.post(f"/api/drafts/{draft['id']}/sync", json={}).status_code == 200
    assert len(calls) == 1
    assert client.post(f"/api/drafts/{draft['id']}/record", json=record_data(confirmation=False)).status_code == 422
    assert client.post(f"/api/drafts/{draft['id']}/record", json=record_data(published_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())).status_code == 422
    assert client.post(f"/api/drafts/{draft['id']}/record", json=record_data(url="file:///private/draft")).status_code == 422
    published = client.post(f"/api/drafts/{draft['id']}/record", json=record_data())
    assert published.status_code == 200
    assert published.json()["state"] == "published"
    assert service.state()["overview"]["published"] == 1
    assert client.post(f"/api/drafts/{draft['id']}/record", json=record_data()).status_code == 409


def test_optional_export_is_not_publication_and_expired_approval_is_unusable(world):
    _, version, _ = completed(world)
    publication = approve(world["client"], version)
    exported = world["client"].get(f"/api/publications/{publication['id']}/export")
    assert exported.status_code == 200
    assert exported.content.startswith(b"PK")
    assert service.state()["overview"]["published"] == 0
    with db.transaction() as connection:
        connection.execute("UPDATE publications SET expires_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), publication["id"]))
    assert world["client"].post(f"/api/publications/{publication['id']}/record", json=record_data()).status_code == 409


def test_scope_change_during_remote_save_retains_id_without_claiming_current_delivery(world):
    def remote(draft, version, topic, settings):
        service.patch_topic(topic["id"], {"brief": "平台请求期间更新范围", "expected_revision": topic["revision"]})
        return {"state": "synced", "remote_id": "historical-remote-id"}

    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", remote)
    topic, version, drafts = completed(world)
    assert topic["status"] == "pending"
    assert drafts[0]["state"] == "stale"
    assert drafts[0]["remote_id"] == "historical-remote-id"
    assert not any("已存入后台" in item["title"] for item in service.state()["notifications"])
    assert world["client"].post(f"/api/drafts/{drafts[0]['id']}/review", json=review_data()).status_code == 409


def test_obsidian_external_changes_preserved_with_separate_conflict_candidate(world):
    topic, version, _ = completed(world)
    route = f"/api/topics/{topic['id']}/sync"
    first = world["client"].post(route, json={"version_id": version["id"]})
    assert first.status_code == 200
    path = Path(first.json()["path"])
    assert path.is_relative_to(world["vault"])
    assert first.json()["status"] == "written"
    external = path.read_text("utf-8") + "\n用户在 Obsidian 添加的原始记录\n"
    path.write_text(external, encoding="utf-8")
    next_version = world["client"].post(f"/api/topics/{topic['id']}/versions", json={"payload": payload(body="这是新的候选正文，需要保留外部记录。"), "base_version_id": version["id"]}).json()
    conflicted = world["client"].post(route, json={"version_id": next_version["id"]})
    assert conflicted.status_code == 200
    result = conflicted.json()
    assert result["status"] == "conflict"
    assert path.read_text("utf-8") == external
    candidate = Path(result["conflict_path"])
    assert candidate != path and candidate.is_relative_to(world["vault"])
    assert "新的候选正文" in candidate.read_text("utf-8")
    assert world["client"].post(f"/api/topics/{topic['id']}/import", json={"base_version_id": next_version["id"]}).status_code == 409


def adapter_inputs(world):
    settings = config.Settings(runtime_dir=world["runtime"], vault_path=str(world["vault"]), wechat_app_id="test-app", wechat_thumb_media_id="permanent-cover", wechat_capability_verified=True)
    return {"id": "test-draft", "topic_id": "test-topic", "version_id": "test-version", "platform": "wechat", "state": "local", "remote_id": None}, {"id": "test-version", "payload": payload()}, {"id": "test-topic"}, settings


def response(data):
    return httpx.Response(200, json=data, request=httpx.Request("POST", "https://api.weixin.qq.com/test"))


def test_wechat_adapter_requires_real_create_id_and_successful_readback(world):
    calls = []
    world["monkeypatch"].setattr(artifacts, "_wechat_token", lambda settings: "test-token")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/draft/add"):
            return response({"media_id": "actual-remote-id"})
        assert url.endswith("/draft/get")
        assert kwargs["json"] == {"media_id": "actual-remote-id"}
        return response({"news_item": [{"title": "审核生成内容的三个步骤", "content": "<p>普通用户可以先确认任务范围，再审核生成内容。</p>", "url": "https://example.com/temporary"}]})

    world["monkeypatch"].setattr(httpx, "post", post)
    result = REAL_PLATFORM_SYNC(*adapter_inputs(world))
    assert result["state"] == "synced" and result["remote_id"] == "actual-remote-id"
    assert len(calls) == 2
    article = calls[0][1]["json"]["articles"][0]
    assert article["article_type"] == "news" and article["thumb_media_id"] == "permanent-cover"
    assert all("freepublish" not in url and "/mass/" not in url for url, _ in calls)


def test_wechat_timeout_without_id_is_unknown_and_never_posted_twice(world):
    calls = []
    world["monkeypatch"].setattr(artifacts, "_wechat_token", lambda settings: "test-token")

    def timeout(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("response lost")

    world["monkeypatch"].setattr(httpx, "post", timeout)
    draft, version, topic, settings = adapter_inputs(world)
    result = REAL_PLATFORM_SYNC(draft, version, topic, settings)
    assert result["state"] == "unknown" and not result.get("remote_id")
    repeated = REAL_PLATFORM_SYNC({**draft, **result}, version, topic, settings)
    assert repeated["state"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("case,expected", [("denied", "failed"), ("missing_id", "unknown"), ("unconfirmed_readback", "unknown")])
def test_wechat_denial_or_incomplete_evidence_cannot_claim_synced(world, case, expected):
    calls = []
    world["monkeypatch"].setattr(artifacts, "_wechat_token", lambda settings: "test-token")

    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("/draft/add"):
            data = {"errcode": 48001} if case == "denied" else ({"media_id": "received-id"} if case == "unconfirmed_readback" else {"errcode": 0})
            return response(data)
        assert url.endswith("/draft/get")
        return response({"errcode": 40007})

    world["monkeypatch"].setattr(httpx, "post", post)
    result = REAL_PLATFORM_SYNC(*adapter_inputs(world))
    assert result["state"] == expected
    assert result.get("remote_id") == ("received-id" if case == "unconfirmed_readback" else None)
    assert len(calls) == (2 if case == "unconfirmed_readback" else 1)


@pytest.mark.parametrize("readback", [True, False])
def test_existing_wechat_remote_id_is_queried_without_creating_duplicate(world, readback):
    calls = []
    world["monkeypatch"].setattr(artifacts, "_wechat_token", lambda settings: "test-token")

    def post(url, **kwargs):
        calls.append(url)
        assert url.endswith("/draft/get")
        return response({"news_item": [{"title": "审核生成内容的三个步骤", "content": "<p>普通用户可以先确认任务范围，再审核生成内容。</p>"}]} if readback else {"errcode": 48001})

    world["monkeypatch"].setattr(httpx, "post", post)
    draft, version, topic, settings = adapter_inputs(world)
    result = REAL_PLATFORM_SYNC({**draft, "state": "unknown", "remote_id": "existing-id"}, version, topic, settings)
    assert result["state"] == ("synced" if readback else "unknown")
    assert result["remote_id"] == "existing-id"
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["missing_cover", "unverified", "title_too_long", "nonwechat"])
def test_wechat_adapter_checks_prerequisites_before_any_external_request(world, case):
    draft, version, topic, settings = adapter_inputs(world)
    if case == "missing_cover":
        settings.wechat_thumb_media_id = ""
    elif case == "unverified":
        settings.wechat_capability_verified = False
    elif case == "title_too_long":
        version["payload"]["works"][0]["title"] = "字" * 33
    else:
        draft["platform"] = "toutiao"
    result = REAL_PLATFORM_SYNC(draft, version, topic, settings)
    assert result["state"] in ("needs_connection", "blocked")
    assert not result.get("remote_id")
