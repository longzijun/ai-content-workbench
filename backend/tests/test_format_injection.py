"""Generation-side injection and the production/queue endpoints.

Two load-bearing invariants:
  1. Without a format decision the generation request is byte-identical to
     before this card. The decision is a workbench-internal judgement, not a
     prompt instruction, and it must not leak into the model call by accident.
  2. The queue endpoint only enqueues. It never publishes, and it never
     retries a draft whose save result was unconfirmed.
"""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import ast
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import adapters, config, db, format_plan, formats, local_generation, main, style_library
from backend.app.platform_rules import expected_format

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)
TODAY = "2026-10-22"
DECISION = format_plan.DECISION_KEY


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    db.init_db()
    return config.get_settings()


@pytest.fixture
def client(world):
    with TestClient(main.app) as test_client:
        yield test_client


def token(client):
    return authenticated_bootstrap(client).json()["token"]


def approved(topic_id="t1", revision=1, platforms=("wechat",)):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,content_line,stance_ids,created_at,updated_at)"
            " VALUES(?,?,'b',?,'[]',?,'approved','A','[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (topic_id, "选题 " + topic_id, json.dumps(list(platforms)), revision))
    return topic_id


def decision(**per_platform):
    return {DECISION: {"platforms": per_platform}}


# --------------------------------------------------------------------------
# 提示注入：无决策时逐字节不变
# --------------------------------------------------------------------------

def test_prompt_is_byte_identical_without_a_decision():
    base = {"title": "选题", "brief": "", "platforms": ["wechat"], "sources": []}
    assert adapters._prompt(base) == adapters._prompt(dict(base))
    assert "_format_hint" not in adapters._prompt(base)


def test_absent_decision_key_does_not_add_a_null():
    """键必须「构造时不存在」而不是放 None，否则 prompt 文本会多出 null。"""
    base = {"title": "选题", "brief": "", "platforms": ["wechat"], "sources": []}
    assert '"_format_decision": null' not in adapters._prompt(base)
    assert '"_format_hint": null' not in adapters._prompt(base)


def test_hint_is_injected_when_a_form_is_adopted():
    topic = {"title": "选题", "brief": "", "platforms": ["wechat"], "sources": [],
             "_format_hint": format_plan.hint_for(
                 {"title": "选题", **decision(wechat={"adopted": "tutorial"})}, "wechat")}
    text = adapters._prompt(topic)
    assert "步骤教程" in text and "_format_hint" in text


def test_hint_for_returns_none_without_a_decision():
    assert format_plan.hint_for({"title": "x"}, "wechat") is None
    assert format_plan.hint_for({"title": "x", **decision(wechat={})}, "wechat") is None
    assert format_plan.hint_for({"title": "x", **decision(wechat={"adopted": "micro"})},
                                "wechat") is None


def test_hint_names_the_window_and_keeps_the_platform_limit_supreme():
    hint = format_plan.hint_for({"title": "x", **decision(wechat={"adopted": "tutorial"})}, "wechat")
    window = formats.get_content_form("tutorial")["title_window"]
    assert str(window[0]) in hint and str(window[1]) in hint
    assert "平台硬性上限" in hint, "B 轴的窗口不能盖过 A 轴的硬上限"


def test_case_hint_forbids_fabricated_experience():
    hint = format_plan.hint_for({"title": "x", **decision(wechat={"adopted": "case"})}, "wechat")
    assert "来源不支持的经历一律不写" in hint


def test_hint_is_scoped_to_its_platform():
    topic = {"title": "x", **decision(wechat={"adopted": "tutorial"})}
    assert format_plan.hint_for(topic, "wechat")
    assert format_plan.hint_for(topic, "toutiao") is None


def test_schema_enum_for_format_is_unchanged():
    """A 轴受约束解码的 enum 必须逐字符还是那两个值。"""
    schema = adapters._schema()
    assert schema["properties"]["works"]["items"]["properties"]["format"]["enum"] == \
        ["article", "note"]


def test_schema_does_not_declare_content_form():
    """模型不该自己填 B 轴：它由工作台在返回后写入。"""
    schema = adapters._schema()
    assert "content_form" not in schema["properties"]["works"]["items"]["properties"]


# --------------------------------------------------------------------------
# 生成请求体：有无决策都不受影响
# --------------------------------------------------------------------------

def _stub(monkeypatch, reply):
    calls = []

    def fake(settings, request, cancel, progress, diagnostic):
        calls.append(json.loads(json.dumps(request)))
        return reply

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", fake)
    return calls


def _reply(platform="wechat"):
    return json.dumps({"strategy": "依据资料写作",
                       "works": [{"platform": platform,
                                  "format": expected_format(platform),
                                  "title": "一个具体问题", "body": "先阅读原文并核对日期。",
                                  "tags": [], "image_prompts": [], "source_urls": []}]},
                      ensure_ascii=False)


def run(settings, monkeypatch, topic):
    _stub(monkeypatch, _reply(topic["platforms"][0]))
    return local_generation.generate_platforms(topic, settings, "local/test",
                                               lambda: False, lambda *a, **k: None)


def test_request_body_is_unchanged_without_a_decision(world, monkeypatch):
    base = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
            "sources": [], "platforms": ["wechat"]}
    first = run(world, monkeypatch, dict(base))[0]
    calls = _stub(monkeypatch, _reply())
    local_generation.generate_platforms(dict(base), world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    assert calls[0]["messages"][0]["content"] == \
        adapters._prompt({**base, "platforms": ["wechat"]}) + \
        adapters.LOCAL_GENERATION_INSTRUCTION


def test_request_body_carries_the_hint_when_a_form_is_adopted(world, monkeypatch):
    topic = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": ["wechat"],
             **decision(wechat={"adopted": "tutorial"})}
    calls = _stub(monkeypatch, _reply())
    local_generation.generate_platforms(topic, world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    text = calls[0]["messages"][0]["content"]
    assert "步骤教程" in text
    assert "article" in text, "A 轴的 format 约束仍在提示里"


def test_two_decisions_do_not_share_a_checkpoint(world, monkeypatch):
    """换个叙事形式就是另一件作品，不能复用上一篇的检查点。"""
    base = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
            "sources": [], "platforms": ["wechat"], "_generation_run": {"id": "run-1"}}
    _stub(monkeypatch, _reply())
    local_generation.generate_platforms(dict(base), world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    tutorial = {**base, **decision(wechat={"adopted": "tutorial"})}
    calls = _stub(monkeypatch, _reply())
    local_generation.generate_platforms(tutorial, world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    assert calls, "新决策必须重新生成，不能命中旧检查点"


def test_style_library_change_does_not_share_a_checkpoint(world, monkeypatch):
    """确认样本变化后，旧风格产物不能被续跑检查点静默恢复。"""
    base = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
            "sources": [], "platforms": ["wechat"], "_generation_run": {"id": "run-1"}}
    monkeypatch.setattr(style_library, "fingerprint", lambda: "style-v1")
    _stub(monkeypatch, _reply())
    local_generation.generate_platforms(dict(base), world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    monkeypatch.setattr(style_library, "fingerprint", lambda: "style-v2")
    calls = _stub(monkeypatch, _reply())
    local_generation.generate_platforms(dict(base), world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    assert calls, "样本库变化必须重新生成，不能命中旧文风检查点"


def test_payload_still_records_the_adopted_form(world, monkeypatch):
    topic = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": ["wechat"],
             **decision(wechat={"adopted": "brief"})}
    payload, _ = run(world, monkeypatch, topic)
    assert payload["works"][0]["content_form"] == "brief"


# --------------------------------------------------------------------------
# 排期端点
# --------------------------------------------------------------------------

def test_scheduling_requires_a_write_token(client):
    """无 session 时 401；有 session 但缺 token 时 403。这是项目既有语义。"""
    approved()
    assert client.post("/api/topics/t1/schedule",
                       json={"batch_date": TODAY}).status_code == 401
    authenticated_bootstrap(client)  # 建立会话
    assert client.post("/api/topics/t1/schedule",
                       json={"batch_date": TODAY}).status_code == 403


def test_scheduling_works_with_a_token(client):
    approved()
    response = client.post("/api/topics/t1/schedule", json={"batch_date": TODAY},
                           headers={"X-Workbench-Token": token(client)})
    assert response.status_code == 200
    assert response.json()["state"] == "scheduled"


def test_scheduling_an_unapproved_topic_is_refused(client):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at)"
            " VALUES('t-pending','未批准','','[\"wechat\"]','[]',1,'pending',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    response = client.post("/api/topics/t-pending/schedule", json={"batch_date": TODAY},
                           headers={"X-Workbench-Token": token(client)})
    assert response.status_code == 409
    assert "批准" in response.json()["detail"]


def test_production_overview_is_read_only(client):
    assert client.get("/api/production").status_code == 401
    response = client.get("/api/production", headers={"X-Workbench-Token": token(client)})
    body = response.json()
    assert set(body) == {"production", "scheduler", "slots"}
    assert "不补发" in body["production"]["note"]


def test_cancel_slot_endpoint(client):
    approved()
    headers = {"X-Workbench-Token": token(client)}
    slot = client.post("/api/topics/t1/schedule", json={"batch_date": TODAY},
                       headers=headers).json()
    assert client.delete(f"/api/production/slots/{slot['id']}",
                         headers=headers).status_code == 200
    assert client.get("/api/production", headers=headers).json()["slots"][0]["state"] == "cancelled"


def test_run_endpoint_uses_the_same_path_as_the_scheduler(client):
    approved()
    headers = {"X-Workbench-Token": token(client)}
    client.post("/api/topics/t1/schedule", json={"batch_date": TODAY}, headers=headers)
    body = client.post("/api/production/run", headers=headers).json()
    assert "batch_date" in body and "items" in body


# --------------------------------------------------------------------------
# 批量排队端点
# --------------------------------------------------------------------------

def make_draft(topic_id="t1", platform="wechat", state="local"):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO drafts(id,topic_id,version_id,platform,title,state,destination,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,'platform',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            ("d-" + platform + "-" + state, topic_id, "v1", platform, "标题", state))
    return "d-" + platform + "-" + state


@pytest.fixture
def draft_world(world):
    """一个可用的版本。payload 必须含 works —— service._version 会遍历它
    为每个平台补图片资产视图，空对象会直接 KeyError。"""
    approved()
    payload = json.dumps({"strategy": "s", "works": [
        {"platform": "wechat", "format": "article", "title": "标题",
         "body": "正文", "tags": [], "image_prompts": [], "source_urls": []},
        {"platform": "toutiao", "format": "article", "title": "标题",
         "body": "正文", "tags": [], "image_prompts": [], "source_urls": []},
    ]}, ensure_ascii=False)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at)"
            " VALUES('v1','t1',1,1,?,'h','local:test','2026-01-01T00:00:00+00:00')", (payload,))
        connection.execute("UPDATE topics SET current_version_id='v1' WHERE id='t1'")
    return world


def test_queue_requires_a_write_token(client, draft_world):
    draft = make_draft()
    assert client.post("/api/drafts/queue",
                       json={"draft_ids": [draft]}).status_code == 401
    authenticated_bootstrap(client)
    assert client.post("/api/drafts/queue",
                       json={"draft_ids": [draft]}).status_code == 403


def test_queue_enqueues_a_local_draft(client, draft_world):
    draft = make_draft()
    response = client.post("/api/drafts/queue", json={"draft_ids": [draft]},
                           headers={"X-Workbench-Token": token(client)})
    body = response.json()
    assert body["queued"] == [draft]
    assert body["skipped"] == []
    assert "不会代你发布" in body["note"]


def test_queue_skips_a_draft_whose_save_result_is_unknown(client, draft_world):
    """上次保存未确认的草稿不能自动重试 —— 那可能造成平台重复草稿。"""
    draft = make_draft(state="unknown")
    body = client.post("/api/drafts/queue", json={"draft_ids": [draft]},
                       headers={"X-Workbench-Token": token(client)}).json()
    assert body["queued"] == []
    assert body["skipped"][0]["state"] == "unknown"
    assert "核对" in body["skipped"][0]["reason"]


@pytest.mark.parametrize("state", ["blocked", "stale", "published", "reviewed"])
def test_queue_skips_states_that_cannot_be_queued(client, draft_world, state):
    draft = make_draft(state=state)
    body = client.post("/api/drafts/queue", json={"draft_ids": [draft]},
                       headers={"X-Workbench-Token": token(client)}).json()
    assert body["queued"] == []
    assert body["skipped"][0]["state"] == state


def test_queue_lists_every_skip_instead_of_failing_the_batch(client, draft_world):
    good = make_draft(platform="wechat", state="local")
    bad = make_draft(platform="toutiao", state="unknown")
    body = client.post("/api/drafts/queue", json={"draft_ids": [good, bad]},
                       headers={"X-Workbench-Token": token(client)}).json()
    assert body["queued"] == [good]
    assert [item["draft_id"] for item in body["skipped"]] == [bad]


def test_queue_is_idempotent_for_the_same_draft(client, draft_world):
    draft = make_draft()
    headers = {"X-Workbench-Token": token(client)}
    for _ in range(3):
        body = client.post("/api/drafts/queue", json={"draft_ids": [draft]},
                           headers=headers).json()
        assert body["queued"] == [draft]
    count = db.connect().execute(
        "SELECT COUNT(*) FROM delivery_tasks WHERE draft_id=?", (draft,)).fetchone()[0]
    assert count == 1


def test_queue_does_not_re_enqueue_a_waiting_draft(client, draft_world):
    draft = make_draft()
    headers = {"X-Workbench-Token": token(client)}
    client.post("/api/drafts/queue", json={"draft_ids": [draft]}, headers=headers)
    with db.transaction() as connection:
        connection.execute("UPDATE delivery_tasks SET state='running' WHERE draft_id=?", (draft,))
    client.post("/api/drafts/queue", json={"draft_ids": [draft]}, headers=headers)
    state = db.connect().execute(
        "SELECT state FROM delivery_tasks WHERE draft_id=?", (draft,)).fetchone()[0]
    assert state == "running", "已在交付中的草稿不能被重置为 queued"


def test_queue_never_calls_sync_draft():
    """排队只把草稿送进交付流程；投递由 delivery 线程按既有语义执行。"""
    source = open(main.__file__, encoding="utf-8").read()
    segment = source.split('def queue_drafts')[1].split('@app.')[0]
    assert "sync_draft" not in segment
    assert "enqueue_in" in segment
    # 'published' 是状态名，不是动作；只查真正的投递调用
    for forbidden in ("delivery.enqueue(", "publish_draft", "record_draft", "review_draft"):
        assert forbidden not in segment


def test_queue_notice_says_the_workbench_will_not_publish(client, draft_world):
    draft = make_draft()
    client.post("/api/drafts/queue", json={"draft_ids": [draft]},
                headers={"X-Workbench-Token": token(client)})
    notices = db.connect().execute(
        "SELECT title,body FROM notifications WHERE topic_id='t1'").fetchall()
    assert notices
    joined = " ".join(f"{row[0]} {row[1]}" for row in notices)
    assert "不会代你发布" in joined


# --------------------------------------------------------------------------
# 端点层的红线
# --------------------------------------------------------------------------

def test_no_production_endpoint_can_reach_published():
    """V2 新增端点不得有任何途径把草稿推进到 synced/reviewed/published。"""
    source = open(main.__file__, encoding="utf-8").read()
    for name in ("schedule_topic", "cancel_production_slot", "run_production_now", "queue_drafts"):
        segment = source.split(f"def {name}")[1].split("@app.")[0]
        for forbidden in ("sync_draft", "record_draft", "review_draft", "publish_draft"):
            assert forbidden not in segment, f"{name} 不得调用 {forbidden}"


def test_production_module_still_has_no_direct_job_insert():
    source = open(__import__("backend.app.production", fromlist=["production"]).__file__,
                  encoding="utf-8").read()
    assert "INSERT INTO jobs" not in source, "建任务只能经 service.generate()"


def test_scheduler_status_is_exposed(client):
    body = client.get("/api/production",
                      headers={"X-Workbench-Token": token(client)}).json()
    assert body["scheduler"]["production_hour_local"] == 6
    assert "服务不运行就没有定时" in body["scheduler"]["note"]
