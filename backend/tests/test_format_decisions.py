"""形式决策：从推荐到生成快照的完整链路。

本条链路上最容易静默失败的一环是 `jobs.input_snapshot`：`local_generation`
从**快照**里读 `_format_decision`，而不是从 topics 表读。只要建任务时没把决策
写进快照，规则引擎与推荐器的结论就会在无人察觉的情况下被丢掉，生成出来的
仍然是没有任何形式约束的通用稿。这里的测试守的就是这一跳。
"""
import json
from datetime import datetime, timezone

import pytest

from backend.app import adapters, config, db, format_decisions, format_plan, service

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)
DECISION = format_plan.DECISION_KEY


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    db.init_db()
    return config.get_settings()


def approved(topic_id="t1", revision=1, platforms=("wechat",), sources=None):
    payload = json.dumps(sources if sources is not None else [], ensure_ascii=False)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,content_line,stance_ids,created_at,updated_at)"
            " VALUES(?,?,'b',?,?,?,'approved','A','[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (topic_id, "选题 " + topic_id, json.dumps(list(platforms)), payload, revision))
    return topic_id


#: 有原始报道的选题。所有叙事形式都要求至少一条来源，空来源时规则引擎会
#: 把全部候选排除 —— 那是正确结论，但不适合用来测「有推荐」。
SOURCES = [{"url": "https://example.com/a", "title": "原始报道", "note": ""},
           {"url": "https://example.com/b", "title": "跟进报道", "note": ""}]


def snapshot_of(job_id):
    with db.connect() as connection:
        row = connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row["input_snapshot"])


# --------------------------------------------------------------------------
# 快照：没有决策就不加键
# --------------------------------------------------------------------------

def test_snapshot_is_empty_when_nothing_was_adopted(world):
    approved()
    assert format_decisions.snapshot("t1") == {}


def test_job_snapshot_has_no_decision_key_without_a_decision(world):
    """无决策路径的请求体必须与 V2-11 之前逐字节相同。"""
    approved()
    job = service.generate("t1", 1)
    assert DECISION not in snapshot_of(job["id"])


def test_adopted_decision_reaches_the_job_snapshot(world):
    """这一步不通，推荐器算得再准也传不到模型。"""
    approved()
    format_decisions.save("t1", "wechat", "case")
    job = service.generate("t1", 1)
    snapshot = snapshot_of(job["id"])
    assert DECISION in snapshot, "建任务时必须把决策写进快照"
    assert snapshot[DECISION]["platforms"]["wechat"]["adopted"] == "case"


def test_decision_survives_multiple_platforms(world):
    approved(platforms=("wechat", "toutiao"))
    format_decisions.save("t1", "wechat", "case")
    format_decisions.save("t1", "toutiao", "brief")
    job = service.generate("t1", 1)
    platforms = snapshot_of(job["id"])[DECISION]["platforms"]
    assert platforms["wechat"]["adopted"] == "case"
    assert platforms["toutiao"]["adopted"] == "brief"


def test_empty_adoption_is_treated_as_no_decision(world):
    """adopted 空串等于撤销：不写键，而不是写一个空形式。"""
    approved()
    format_decisions.save("t1", "wechat", "")
    job = service.generate("t1", 1)
    assert DECISION not in snapshot_of(job["id"])


def test_clearing_a_decision_removes_it_from_the_next_snapshot(world):
    approved()
    format_decisions.save("t1", "wechat", "case")
    format_decisions.clear("t1", "wechat")
    job = service.generate("t1", 1)
    assert DECISION not in snapshot_of(job["id"])


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def test_unknown_content_form_is_rejected(world):
    approved()
    with pytest.raises(ValueError, match="不支持的叙事形式"):
        format_decisions.save("t1", "wechat", "video_essay")


def test_unknown_platform_is_rejected(world):
    approved()
    with pytest.raises(ValueError, match="不支持的发布平台"):
        format_decisions.save("t1", "weibo", "case")


def test_missing_topic_is_rejected(world):
    with pytest.raises(ValueError, match="选题不存在"):
        format_decisions.save("nope", "wechat", "case")


def test_decision_overwrites_instead_of_duplicating(world):
    approved()
    format_decisions.save("t1", "wechat", "case")
    format_decisions.save("t1", "wechat", "checklist")
    rows = format_decisions.load("t1")
    assert len(rows) == 1
    assert rows[0]["adopted"] == "checklist"


def test_manual_override_is_recorded_as_manual(world):
    approved()
    format_decisions.save("t1", "wechat", "opinion", by="manual",
                          recommendation={"recommended": "case", "confidence": 70,
                                          "reason": "来源足够", "status": "local",
                                          "rule_ranked": ["case", "opinion"]})
    row = format_decisions.load("t1")[0]
    assert row["decided_by"] == "manual"
    assert row["recommended"] == "case", "推荐结果要保留，才能看出人改了什么"


# --------------------------------------------------------------------------
# 推荐
# --------------------------------------------------------------------------

def test_recommend_returns_all_three_platforms(world):
    approved()
    result = format_decisions.recommend_all("t1", now=NOW)
    assert set(result["platforms"]) == {"wechat", "toutiao", "xiaohongshu"}
    for platform, item in result["platforms"].items():
        assert item["platform"] == platform
        assert isinstance(item["alternatives"], list)
        assert item["status"] in ("local", "unavailable", "failed", "rule_only")


def test_recommendation_without_a_model_says_it_used_rules_only(world):
    approved(sources=SOURCES)
    result = format_decisions.recommend("t1", "wechat", now=NOW)
    assert result["status"] == "unavailable"
    assert "规则" in result["reason"], "没调模型就必须说明用的是规则排序"
    assert result["recommended"], "有来源时规则引擎应该给出一个可行形式"


def test_no_sources_means_no_recommendation(world):
    """一条来源都没有时，规则引擎排除全部形式 —— 这是正确结论，不是故障。"""
    approved()
    result = format_decisions.recommend("t1", "wechat", now=NOW)
    assert result["status"] == "rule_only"
    assert result["recommended"] == ""
    assert result["blocked"], "要告诉用户为什么一个形式都不可行"


def test_recommendation_is_marked_stale_when_inputs_change(world):
    approved()
    first = format_decisions.recommend("t1", "wechat", now=NOW)
    format_decisions.save("t1", "wechat", first["recommended"] or "case",
                          by="recommendation", recommendation=first)
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET title=? WHERE id='t1'",
                           ("完全换了一个标题：本地模型显存不够怎么办",))
    again = format_decisions.recommend_all("t1", now=NOW)["platforms"]["wechat"]
    assert again["stale"] is True, "输入变了要提示决策已过期"


def test_recommend_for_missing_topic_raises(world):
    with pytest.raises(ValueError, match="选题不存在"):
        format_decisions.recommend("nope", "wechat", now=NOW)
