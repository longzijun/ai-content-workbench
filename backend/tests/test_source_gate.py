"""A predictable failure must be refused at the entrance, not inside the worker.

Real bug this covers: with only search/hot-list sources, generate() used to
succeed, the worker failed a moment later, the UI showed "执行失败" with a
"继续未完成的生成" button, and clicking it produced another guaranteed-failing
job. Three such failures in a row from one topic. The check belongs at the
door.
"""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import config, db, main, research, service

HOT_LIST = [{"url": "https://top.baidu.com/board", "title": "热榜", "note": ""}]
SEARCH = [{"url": "https://www.bing.com/search?q=x", "title": "搜索", "note": ""}]
ORIGINAL = [{"url": "https://example.com/news/1", "title": "原文", "note": ""}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    from backend.app import adapters
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    db.init_db()
    return config.get_settings()


@pytest.fixture
def client(world):
    with TestClient(main.app) as test_client:
        yield test_client


def topic(topic_id="t1", sources=None, status="approved", revision=1):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,content_line,stance_ids,created_at,updated_at)"
            " VALUES(?,?,'b','[\"wechat\"]',?,?,?,'A','[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (topic_id, "选题 " + topic_id, json.dumps(sources if sources is not None else HOT_LIST),
             revision, status))
    return topic_id


def job_count():
    return db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------

def test_has_original_report_rejects_hot_list_only():
    assert research.has_original_report(HOT_LIST) is False


def test_has_original_report_rejects_search_only():
    assert research.has_original_report(SEARCH) is False


def test_has_original_report_accepts_one_original_among_indexes():
    """混着也算通过：只要有一条能读的原文，就不是「只有热榜」。"""
    assert research.has_original_report(HOT_LIST + ORIGINAL) is True


def test_has_original_report_allows_empty_sources():
    """没有来源不等于「只有热榜」—— 空来源走的是另一条路径（无资料写作）。"""
    assert research.has_original_report([]) is True
    assert research.has_original_report(None) is True


def test_has_original_report_tolerates_malformed_entries():
    assert research.has_original_report([{}, "不是字典"]) is True


# --------------------------------------------------------------------------
# generate 前置检查
# --------------------------------------------------------------------------

def test_generate_refuses_a_hot_list_only_topic(world):
    topic()
    with pytest.raises(HTTPException) as error:
        service.generate("t1", 1)
    assert error.value.status_code == 409
    assert "检索并读取原文" in error.value.detail


def test_generate_leaves_no_job_when_refused(world):
    """关键：拒绝发生在建任务之前，不留下无效任务记录。"""
    topic()
    with pytest.raises(HTTPException):
        service.generate("t1", 1)
    assert job_count() == 0


def test_generate_accepts_a_topic_with_an_original_report(world):
    topic(sources=ORIGINAL)
    assert service.generate("t1", 1)["state"] == "queued"
    assert job_count() == 1


def test_generate_accepts_a_topic_with_no_sources_at_all(world):
    topic(sources=[])
    assert service.generate("t1", 1)["state"] == "queued"


def test_generate_still_checks_approval_first(world):
    """顺序：先批准、再来源。未批准的选题不该因为来源问题被误导。"""
    topic(status="pending")
    with pytest.raises(HTTPException) as error:
        service.generate("t1", 1)
    assert "批准" in error.value.detail
    assert "热榜" not in error.value.detail


# --------------------------------------------------------------------------
# retry 前置检查 —— 截图里的「继续未完成的生成」就走这条
# --------------------------------------------------------------------------

def test_retry_refuses_when_sources_are_still_only_hot_list(world):
    """来源没补就重试，只会得到一模一样的失败 —— 应当在入口拒绝。"""
    topic(sources=ORIGINAL)
    job = service.generate("t1", 1)
    service.claim_next_job()
    service.fail_job(job["id"], "模拟失败")
    # 用户把来源改回只有热榜（或原本就是），现在重试
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET sources=? WHERE id='t1'", (json.dumps(HOT_LIST),))
    with pytest.raises(HTTPException) as error:
        service.retry_job(job["id"])
    assert error.value.status_code == 409
    assert "检索并读取原文" in error.value.detail
    assert job_count() == 1, "被拒绝的重试不得产生新任务"


def test_retry_works_once_sources_are_fixed(world):
    topic(sources=ORIGINAL)
    job = service.generate("t1", 1)
    service.claim_next_job()
    service.fail_job(job["id"], "模拟失败")
    retried = service.retry_job(job["id"])
    assert retried["state"] == "queued"
    assert job_count() == 2


def test_repeated_retry_cannot_pile_up_failed_jobs(world):
    """截图里的现象：连点三次「继续未完成的生成」，堆出三条注定失败的 job。"""
    topic()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO jobs(id,topic_id,kind,state,stage,input_revision,input_snapshot,"
            "events,created_at,updated_at) VALUES('j-old','t1','generate','failed','执行失败',1,"
            "'{}','[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    for _ in range(3):
        with pytest.raises(HTTPException):
            service.retry_job("j-old")
    assert job_count() == 1, "三条都被拒绝，只有原有的那一条"


# --------------------------------------------------------------------------
# worker 侧兜底仍在
# --------------------------------------------------------------------------

def test_prepare_approved_keeps_the_guard_for_queued_legacy_jobs(world):
    """队列里可能存着前置检查上线前建好的任务，worker 仍须兜底。"""
    with pytest.raises(ValueError, match="检索并读取原文"):
        research.prepare_approved({"sources": HOT_LIST}, lambda *a, **k: None)


def test_prepare_approved_passes_through_when_sources_are_readable(world):
    result = research.prepare_approved({"sources": [], "id": "t1", "revision": 1},
                                       lambda *a, **k: None)
    assert result["sources"] == []


def test_both_checks_share_one_message():
    """两处文案必须同源，否则用户会看到两种说法。

    验证方式是比对抛出的异常文本与常量本身是否相等 —— 而不是去源码里
    搜字符串（常量是多段隐式拼接的，搜不到完整文本，那种断言只会误报）。
    """
    with pytest.raises(ValueError) as prepare_error:
        research.prepare_approved({"sources": SEARCH}, lambda *a, **k: None)
    assert str(prepare_error.value) == research.SOURCE_ONLY_INDEX_MESSAGE

    with pytest.raises(ValueError) as require_error:
        research.require_original_sources({"sources": HOT_LIST})
    assert str(require_error.value) == research.SOURCE_ONLY_INDEX_MESSAGE

    assert prepare_error.value.args == require_error.value.args


# --------------------------------------------------------------------------
# 端点层
# --------------------------------------------------------------------------

def test_endpoint_returns_409_and_creates_nothing(client):
    topic()
    token = authenticated_bootstrap(client).json()["token"]
    response = client.post("/api/topics/t1/generate", json={"expected_revision": 1},
                           headers={"X-Workbench-Token": token})
    assert response.status_code == 409
    assert "检索并读取原文" in response.json()["detail"]
    assert job_count() == 0


def test_endpoint_tells_the_user_what_to_do_next(client):
    """拒绝时必须给出可执行的下一步，而不是只报「失败」。"""
    topic()
    token = authenticated_bootstrap(client).json()["token"]
    detail = client.post("/api/topics/t1/generate", json={"expected_revision": 1},
                         headers={"X-Workbench-Token": token}).json()["detail"]
    assert "选题详情" in detail and "核对并保存来源" in detail
    assert "不会把热榜摘要当作已核实事实写入正文" in detail
