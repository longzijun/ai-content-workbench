"""失败任务重试必须带着共享事实底稿。

底稿不是装饰：它进模型快照、进检查点身份。重试时静默丢掉它，等于换了一套
材料重写一遍，已完成平台也续不上；而静默采用新底稿，又会让「这次失败重跑」
变成一次没人确认过的生产。这里把两条都钉死：沿用冻结底稿，底稿已修订则明确
要求从选题重新发起生产。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from backend.app import config, db, fact_base, service

SOURCE = {"url": "https://example.com/report/1", "title": "原始报道", "note": ""}


def base_data(**changes):
    data = {
        "audience": "想用 AI 提效的普通职场人",
        "core_view": "先把事实核清楚，再让 AI 参与生产",
        "sources": [SOURCE],
        "facts": [{"claim": "工作台在本地模式下不保存平台草稿", "source_url": SOURCE["url"],
                   "source_date": "2026-10-25", "status": "confirmed"}],
        "open_questions": [],
        "author_experience": "",
        "private_info": "",
        "asset_list": [],
        "platform_angles": {},
    }
    data.update(changes)
    return data


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return tmp_path


def _topic(topic_id="t1", platforms=("wechat",)):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,content_line,stance_ids,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,'approved','A','[]','2026-01-01','2026-01-01')",
            (topic_id, "一个具体题目", "基于原文说明具体变化。", json.dumps(list(platforms)),
             json.dumps([SOURCE], ensure_ascii=False)))


def _snapshot_of(job_id):
    with db.connect() as connection:
        return json.loads(connection.execute(
            "SELECT input_snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()[0])


def _fail(job_id):
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))


def test_retry_keeps_the_frozen_fact_base(world):
    _topic()
    fact_base.save("t1", base_data())
    fact_base.mark_ready("t1")
    job = service.produce_topic("t1", ["wechat"])
    frozen = _snapshot_of(job["id"])["_fact_base"]
    _fail(job["id"])
    retried = service.retry_job(job["id"])
    assert _snapshot_of(retried["id"])["_fact_base"] == frozen


def test_retry_refuses_when_fact_base_was_revised(world):
    """底稿已修订时不能静默换材料，也不能退回无底稿。"""
    _topic()
    fact_base.save("t1", base_data())
    fact_base.mark_ready("t1")
    job = service.produce_topic("t1", ["wechat"])
    _fail(job["id"])
    fact_base.save("t1", base_data(core_view="修订后的核心观点"))
    with pytest.raises(HTTPException) as error:
        service.retry_job(job["id"])
    assert error.value.status_code == 409
    assert "重新发起生产" in error.value.detail


def test_retry_without_fact_base_stays_without(world):
    """旧通道建的任务本来就没有底稿，重试不得凭空补一个。"""
    _topic()
    job = service.generate("t1", 1)
    assert "_fact_base" not in _snapshot_of(job["id"])
    _fail(job["id"])
    retried = service.retry_job(job["id"])
    assert "_fact_base" not in _snapshot_of(retried["id"])
