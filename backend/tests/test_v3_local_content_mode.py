"""V3 本地内容模式：运行模式闸门、旧任务隔离、生成链路零平台触达。

验收口径（执行提示词 9.1）：用「禁止调用的替身 + 请求记录」断言违规调用为 0，
不只检查配置值；隔离环境不连接真实平台账号。
"""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import json
import socket

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import adapters, artifacts, browser_bridge, browser_drafts, config, db, delivery, main, production, research, runtime_mode, service, v3_migration, worker

SOURCE = {"url": "https://example.invalid/source", "title": "测试来源", "note": ""}
PLATFORM_NAMES = {"wechat": "公众号", "toutiao": "今日头条", "xiaohongshu": "小红书"}

# 本模块验证的是真实的本地内容模式上锁行为，退出 conftest 的平台测试模式放行。
pytestmark = pytest.mark.local_content_only


def payload(platform="xiaohongshu", body="先核实事实，再审核内容，最后由用户发布。"):
    return {"strategy": "一平台一份作品", "works": [{"platform": platform, "format": "note" if platform == "xiaohongshu" else "article", "title": "审核内容的三个步骤", "body": body, "tags": [], "image_prompts": ["中性的三个步骤示意图"], "source_urls": [SOURCE["url"]]}]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append({"args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})
        pytest.fail("本地内容模式测试触达了平台/网络/模型")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(browser_bridge, "_load_playwright", forbidden)
    monkeypatch.setattr(browser_bridge, "run_in_page", forbidden)
    monkeypatch.setattr(browser_bridge, "_ensure_edge", forbidden)
    monkeypatch.setattr(browser_bridge, "open_login", forbidden)
    monkeypatch.setattr(browser_drafts, "sync_browser_draft", forbidden, raising=False)
    monkeypatch.setattr(browser_drafts, "open_browser_draft", forbidden, raising=False)
    monkeypatch.setattr(artifacts, "sync_platform_draft", forbidden)
    db.init_db()
    yield {"runtime": runtime, "vault": vault, "monkeypatch": monkeypatch, "attempts": attempts}


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


def approved_topic(platforms=("wechat", "toutiao", "xiaohongshu")):
    topic = service.create_topic({"title": "三平台并行测试", "brief": "一份底稿三份作品",
                                  "platforms": list(platforms),
                                  "sources": [SOURCE], "content_line": "A", "stance_ids": []})
    return service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})


def remigrate():
    """init_db 已自动执行过一次迁移（标记已在库）；清掉标记重跑，
    才能验证迁移逻辑本身与幂等性。必须走事务连接，否则不提交。"""
    with db.transaction() as connection:
        connection.execute("DELETE FROM runtime_state WHERE key='v3_quarantine_migration'")
        return v3_migration.migrate(connection)


def completed(client, platforms=("wechat", "toutiao", "xiaohongshu")):
    topic = approved_topic(platforms)
    job = service.generate(topic["id"], topic["revision"])
    assert service.claim_next_job()["id"] == job["id"]
    version = service.complete_job(job["id"], payload_all(platforms), "fake-provider")
    drafts = [item for item in service.state()["drafts"] if item["version_id"] == version["id"]]
    return topic, version, drafts


def payload_all(platforms):
    return {"strategy": "一平台一份作品", "works": [
        {"platform": p, "format": "note" if p == "xiaohongshu" else "article",
         "title": f"{PLATFORM_NAMES[p]}版本标题", "body": "先核实事实，再审核内容，最后由用户发布。",
         "tags": [], "image_prompts": [], "source_urls": [SOURCE["url"]]} for p in platforms]}


# ---------------------------------------------------------------- 运行模式

def test_new_install_defaults_to_local_content_only(world):
    assert runtime_mode.get_mode() == runtime_mode.MODE_LOCAL_CONTENT_ONLY
    view = runtime_mode.mode_view()
    assert view["platform_operations"] is False
    assert view["mode_label"] == "本地内容模式"


def test_recorded_platform_mode_stays_local_in_public_build(world):
    """公开版忽略磁盘上的旧平台许可。"""
    runtime_mode.set_mode(runtime_mode.MODE_PLATFORM_OPERATIONS)
    assert runtime_mode.get_mode() == runtime_mode.MODE_LOCAL_CONTENT_ONLY
    assert runtime_mode.platform_operations_allowed() is False
    # 白名单内平台未启用 → 仍不放行
    assert runtime_mode.platform_draft_supported("wechat") is False
    # 白名单外平台（视频号/快手/抖音）即使尝试启用也被拒
    with pytest.raises(HTTPException) as error:
        runtime_mode.set_platform_access("douyin", True, "账号")
    assert error.value.status_code == 409
    # 小红书永久锁
    with pytest.raises(HTTPException) as error:
        runtime_mode.set_platform_access("xiaohongshu", True, "账号")
    assert error.value.status_code == 409
    assert runtime_mode.platform_draft_supported("xiaohongshu") is False


def test_corrupted_or_missing_mode_fails_closed(world):
    with db.transaction() as connection:
        runtime_mode._ensure_table(connection)
        connection.execute("INSERT INTO runtime_state(key,value,updated_at) VALUES('execution_mode','platform_operations','2026-01-01T00:00:00+00:00') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    assert runtime_mode.platform_operations_allowed() is False
    with db.transaction() as connection:
        connection.execute("UPDATE runtime_state SET value='garbage' WHERE key='execution_mode'")
    assert runtime_mode.get_mode() == runtime_mode.MODE_LOCAL_CONTENT_ONLY
    with db.connect() as connection:
        connection.execute("DROP TABLE runtime_state")
    assert runtime_mode.get_mode() == runtime_mode.MODE_LOCAL_CONTENT_ONLY


def test_mode_view_endpoint(client):
    response = client.get("/api/runtime-mode")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "local_content_only"
    assert body["platform_operations"] is False


# ---------------------------------------------------------------- 端点闸门

def test_all_platform_endpoints_rejected(client, world):
    _, _, drafts = completed(client)
    draft = drafts[0]
    cases = [
        ("post", "/api/integrations/wechat/check", None),
        ("post", "/api/integrations/browser/wechat/login", None),
        ("post", "/api/integrations/browser/toutiao/check", None),
        ("post", "/api/integrations/browser/xiaohongshu/inspect", None),
        ("post", "/api/integrations/browser/xiaohongshu/probe", {"action": "inspect"}),
        ("post", "/api/integrations/browser/wechat/verify", None),
        ("post", "/api/drafts/queue", {"draft_ids": [d["id"] for d in drafts]}),
        ("post", f"/api/drafts/{draft['id']}/sync", None),
        ("post", f"/api/drafts/{draft['id']}/queue", None),
        ("post", f"/api/drafts/{draft['id']}/open", None),
    ]
    for method, path, body in cases:
        response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert response.status_code == 403, f"{path} 未被模式闸门拒绝"
        detail = response.json()["detail"]
        assert detail["code"] == "platform_operations_disabled", path
        assert "本地内容模式" in detail["message"]
    assert world["attempts"] == [], "存在真实平台触达调用"


def test_generation_creates_no_delivery_tasks(client, world):
    _, version, drafts = completed(client)
    with db.connect() as connection:
        tasks = connection.execute("SELECT * FROM delivery_tasks").fetchall()
    assert tasks == []
    assert len(drafts) == 3
    for draft in drafts:
        assert draft["state"] == "local"
        assert draft["destination"] == "workbench"
    assert world["attempts"] == []


def test_review_approve_and_export_do_not_touch_platform(client, world):
    topic, version, drafts = completed(client)
    for draft in drafts:
        reviewed = service.review_draft(draft["id"], {"account_label": "测试账号", "ai_disclosure": "AI 辅助创作", "facts_confirmed": True})
        assert reviewed["state"] == "reviewed"
    assert world["attempts"] == []


# ---------------------------------------------------------------- 交付队列

def test_delivery_worker_stays_down_in_local_mode(world):
    delivery.start()
    assert delivery._thread is None or not delivery._thread.is_alive()
    assert delivery.run_one() is False
    with db.transaction() as connection:
        with pytest.raises(HTTPException) as error:
            delivery.enqueue_in(connection, ["whatever"])
        assert error.value.status_code == 403
    assert world["attempts"] == []


def test_quarantine_never_requeued(world):
    topic = approved_topic(("xiaohongshu",))
    job = service.generate(topic["id"], topic["revision"])
    service.claim_next_job()
    version = service.complete_job(job["id"], payload("xiaohongshu"), "fake-provider")
    draft = next(item for item in service.state()["drafts"] if item["version_id"] == version["id"])
    # 手工造一条旧平台任务（模拟 V3 之前的库）
    with db.transaction() as connection:
        connection.execute("INSERT INTO delivery_tasks(draft_id,state,stage,message,attempts,updated_at) VALUES(?,'waiting','platform','正在保存或回读平台草稿',3,'2026-10-23T06:58:00+00:00')", (draft["id"],))
    report = remigrate()
    assert report["already_migrated"] is False
    assert report["quarantined"] == 1
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM delivery_tasks WHERE draft_id=?", (draft["id"],)).fetchone()
        assert row["state"] == "quarantined"
        reason = json.loads(row["quarantine_reason"])
        assert reason["from_state"] == "waiting"
        assert reason["attempts"] == 3
        assert row["cancel_requested"] == 1
    # 迁移第二次执行：不重复、不改写
    again = v3_migration.migrate(db.connect())
    assert again["already_migrated"] is True
    assert again["quarantined"] == 0
    # 隔离态无法被重新排队
    with db.transaction() as connection:
        with pytest.raises(HTTPException):
            delivery.enqueue_in(connection, [draft["id"]])
    # 隔离清单可读
    with db.connect() as connection:
        items = v3_migration.quarantined_tasks(connection)
    assert len(items) == 1 and items[0]["platform"] == "xiaohongshu"


def test_unknown_drafts_stay_for_manual_check(world):
    topic = approved_topic(("xiaohongshu",))
    job = service.generate(topic["id"], topic["revision"])
    service.claim_next_job()
    version = service.complete_job(job["id"], payload("xiaohongshu"), "fake-provider")
    draft = next(item for item in service.state()["drafts"] if item["version_id"] == version["id"])
    with db.transaction() as connection:
        connection.execute("UPDATE drafts SET state='unknown',remote_id='browser:xiaohongshu:abc',updated_at=? WHERE id=?", (service.now(), draft["id"]))
    report = remigrate()
    assert report["unknown_drafts_flagged"] == 1
    with db.connect() as connection:
        row = connection.execute("SELECT state FROM drafts WHERE id=?", (draft["id"],)).fetchone()
    assert row["state"] == "unknown", "结果未知的远端保存必须保持待人工核对"


# ---------------------------------------------------------------- 排期与任务暂停

def test_slots_and_jobs_paused_then_resumed(world):
    topic = approved_topic(("xiaohongshu",))
    slot = production.schedule_slot(topic["id"], "2026-10-26", note="旧排期")
    job = service.generate(topic["id"], topic["revision"])
    report = remigrate()
    assert report["slots_paused"] == 1
    assert report["jobs_paused"] == 1
    assert production.list_slots()[0]["paused"] == 1
    # 暂停的排队任务不会被 worker 领走
    assert service.claim_next_job() is None
    # 暂停的排期不进当日批次
    picked, waiting = production._claim_slots({"started_at": "2026-10-26T00:00:00+00:00"}, "2026-10-26", service._date("2026-10-26T01:00:00+00:00"))
    assert picked == [] and waiting == 0
    # 人工恢复：排期与任务各自恢复，且只生成本地内容
    resumed_job = service.resume_paused_job(job["id"])
    assert resumed_job["paused"] is False
    resumed_slot = production.resume_slot(slot["id"])
    assert resumed_slot["paused"] is False
    assert service.claim_next_job()["id"] == job["id"]


# ---------------------------------------------------------------- 研究抓取

@pytest.mark.parametrize("url", [
    "https://www.xiaohongshu.com/explore/abc",
    "https://xiaohongshu.com/discovery/item/1",
    "https://creator.xiaohongshu.com/publish",
    "https://xhs.link/abc123",
    "https://sns-webpic-qc.xhscdn.com/abc.jpg",
])
def test_research_blocks_xiaohongshu(world, url):
    with pytest.raises(ValueError) as error:
        research.public_target(url)
    assert "小红书" in str(error.value)


def test_research_still_allows_public_sources(world, monkeypatch):
    monkeypatch.setattr(research.socket, "getaddrinfo",
                        lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    parsed, port, address = research.public_target("https://example.com/news/1")
    assert parsed.hostname == "example.com" and port == 443
