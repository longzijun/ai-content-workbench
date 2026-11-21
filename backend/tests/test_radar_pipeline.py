"""统一数据处理层：去重、合并、热度标准化、保留策略。

重点不是「能算出来」，而是**不许假装**——跨源热度值不归一、短标题不合并、
超出窗口不合并，这三类"看起来聪明其实编数据"的行为都要被钉住。
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import config, db, radar_pipeline, trends

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    monkeypatch.setattr(trends, "_now", lambda: NOW)
    return config.get_settings()


def _candidate(connection, title, url, seen=None, candidate_id="c1"):
    stamp = (seen or NOW).isoformat()
    connection.execute(
        "INSERT INTO trend_candidates(id,title,title_key,url,summary,published_at,first_seen_at,last_seen_at)"
        " VALUES(?,?,?,?,'',?,?,?)",
        (candidate_id, title, radar_pipeline.title_key(title), url,
         (NOW - timedelta(hours=2)).isoformat(), stamp, stamp))


# --------------------------------------------------------------------------
# URL 规范化
# --------------------------------------------------------------------------

def test_canonical_url_strips_tracking_and_host_prefixes():
    assert radar_pipeline.canonical_url("https://www.Example.com/a/b/?utm_source=rss&id=3") == "https://example.com/a/b?id=3"
    assert radar_pipeline.canonical_url("https://m.example.com/x/") == "https://example.com/x"
    assert radar_pipeline.canonical_url("http://openai.com/news/one#section") == "http://openai.com/news/one"


@pytest.mark.parametrize("value", ["ftp://example.com/a", "https://user:pw@example.com/a",
                                   "https://example.com:8080/a", "not a url", "", None,
                                   "javascript:alert(1)"])
def test_canonical_url_rejects_anything_it_cannot_normalise(value):
    assert radar_pipeline.canonical_url(value) is None


# --------------------------------------------------------------------------
# 相似度：不许把两件事并成一件
# --------------------------------------------------------------------------

def test_repeated_headlines_merge_and_unrelated_ones_do_not():
    left = "OpenAI 发布 GPT-5.1，推理能力提升"
    assert radar_pipeline.similarity(left, "OpenAI 发布 GPT-5.1，推理能力提升！") >= radar_pipeline.MERGE_THRESHOLD
    assert radar_pipeline.similarity(left, "西藏那曲巴青县发生7.0级地震") < radar_pipeline.MERGE_THRESHOLD


def test_a_paraphrase_is_deliberately_not_auto_merged():
    """同义改写不自动合并：合并错了会丢线索，不合并只是多显示一条。

    实测这组标题 Jaccard ≈ 0.54，低于 0.72 的阈值。保留这条测试是为了把
    "故意不合并"这个取舍钉住 —— 以后想放宽阈值时必须连这个判断一起改。
    """
    left = "OpenAI 发布 GPT-5.1，推理能力提升"
    right = "OpenAI 推出 GPT-5.1，推理能力增强"
    assert radar_pipeline.similarity(left, right) < radar_pipeline.MERGE_THRESHOLD


def test_short_titles_never_merge_on_similarity():
    """短标题 Token 太少，Jaccard 会失真 —— 直接判 0，不参与合并。"""
    assert radar_pipeline.similarity("地震", "地震") == 0.0
    assert radar_pipeline.similarity("AI", "AI 新闻") == 0.0


# --------------------------------------------------------------------------
# 热度标准化：只在批内归一，跨源不归一
# --------------------------------------------------------------------------

def test_heat_is_normalised_within_a_batch_only():
    assert radar_pipeline.normalize_heat(1, 30) == 1.0
    assert radar_pipeline.normalize_heat(30, 30) == pytest.approx(0.0333, abs=1e-4)
    assert radar_pipeline.normalize_heat(15, 30) == pytest.approx(0.5333, abs=1e-4)


@pytest.mark.parametrize("rank,max_rank", [(1, None), (1, 0), (31, 30), (0, 30), (None, 30), (-1, 30), (True, 30)])
def test_heat_is_not_normalised_when_the_batch_size_is_unknown_or_inconsistent(rank, max_rank):
    """没有可靠的批内基准就返回 None —— 不拿 30 去假装所有榜单都是 30 条。"""
    assert radar_pipeline.normalize_heat(rank, max_rank) is None


def test_batch_max_rank_uses_the_largest_rank_seen():
    items = [{"rank": 1}, {"rank": 22}, {"rank": None}, {"rank": -3}, {"title": "no rank"}]
    assert radar_pipeline.batch_max_rank(items) == 22
    assert radar_pipeline.batch_max_rank([]) is None


# --------------------------------------------------------------------------
# 合并：三级递进，窗口内生效
# --------------------------------------------------------------------------

def test_same_url_merges_across_sources(world):
    with db.transaction() as connection:
        _candidate(connection, "同一个事件的不同写法", "https://example.com/a/1")
        row, reason = radar_pipeline.find_merge_target(connection, "https://www.example.com/a/1?utm_source=x", "完全不同的标题", NOW)
    assert row is not None and reason == "same_url"


def test_same_title_key_merges_within_the_window(world):
    with db.transaction() as connection:
        _candidate(connection, "OpenAI 发布新模型", "https://example.com/a/1")
        row, reason = radar_pipeline.find_merge_target(connection, "https://example.com/b/2", "OpenAI 发布新模型", NOW)
    assert row is not None and reason == "same_title"


def test_a_similar_title_outside_the_window_does_not_merge(world):
    """窗口外的相似标题是"新进展"，合并会把旧事件和新事件搅成一条。"""
    with db.transaction() as connection:
        _candidate(connection, "OpenAI 发布 GPT-5.1，推理能力提升", "https://example.com/a/1",
                   seen=NOW - timedelta(days=radar_pipeline.MERGE_WINDOW_DAYS + 1))
        row, _ = radar_pipeline.find_merge_target(connection, "https://example.com/b/2",
                                                  "OpenAI 推出 GPT-5.1，推理能力增强", NOW)
    assert row is None


def test_unrelated_titles_do_not_merge(world):
    with db.transaction() as connection:
        _candidate(connection, "OpenAI 发布 GPT-5.1，推理能力提升", "https://example.com/a/1")
        row, _ = radar_pipeline.find_merge_target(connection, "https://example.com/b/2", "西藏那曲巴青县发生7.0级地震", NOW)
    assert row is None


# --------------------------------------------------------------------------
# 采集日志与保留策略
# --------------------------------------------------------------------------

def test_every_attempt_is_logged_including_failures(world):
    with db.transaction() as connection:
        radar_pipeline.record_source_run(connection, "run-1", "openai_news", "ok",
                                         item_count=30, kept=12, duplicates=17, dropped=1)
        radar_pipeline.record_source_run(connection, "run-1", "hackernews_front", "error",
                                         item_count=0, error="timeout")
    with db.connect() as connection:
        rows = connection.execute("SELECT source_id,status,kept_count,duplicate_count,dropped_count,error FROM radar_source_runs ORDER BY source_id").fetchall()
    assert [(row["source_id"], row["status"]) for row in rows] == [("hackernews_front", "error"), ("openai_news", "ok")]
    ok = rows[1]
    assert (ok["kept_count"], ok["duplicate_count"], ok["dropped_count"]) == (12, 17, 1)
    assert str(rows[0]["error"]) == "timeout", "失败也要留证据，否则分不清没到点还是挂了"


def test_retention_only_touches_collection_details(world):
    """保留策略删的是采集明细；候选与选题一条都不许动。"""
    with db.transaction() as connection:
        _candidate(connection, "一条候选", "https://example.com/a/1")
        connection.execute(
            "INSERT INTO radar_source_runs(id,run_id,source_id,status,started_at,finished_at) VALUES('old','r','openai_news','ok',?,?)",
            ((NOW - timedelta(days=60)).isoformat(), (NOW - timedelta(days=60)).isoformat()))
        connection.execute(
            "INSERT INTO radar_source_runs(id,run_id,source_id,status,started_at,finished_at) VALUES('new','r','openai_news','ok',?,?)",
            (NOW.isoformat(), NOW.isoformat()))
        radar_pipeline.retention(connection, now=NOW)
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_source_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trend_candidates").fetchone()[0] == 1


def test_recording_a_run_counts_merges_separately_from_drops(world):
    """新增 0 有三种原因，必须能分开看，否则排障只能猜。"""
    items = [
        {"title": "OpenAI 发布 GPT-5.1，推理能力提升", "url": "https://example.com/a/1", "rank": 1},
        {"title": "OpenAI 发布 GPT-5.1，推理能力提升！", "url": "https://example.com/a/1?utm_source=rss", "rank": 2},
    ]
    with db.transaction() as connection:
        connection.execute("INSERT INTO trend_runs(id,state,started_at) VALUES('r1','running',?)", (NOW.isoformat(),))
    source = {"id": "openai_news", "name": "OpenAI", "url": "https://openai.com/news/rss.xml", "kind": "rss", "directions": ["ai"]}
    trends._record_source("r1", source, {"status": "ok", "fetched_at": NOW.isoformat(), "items": items})
    with db.connect() as connection:
        runs = connection.execute("SELECT * FROM radar_source_runs WHERE source_id='openai_news'").fetchall()
        candidates = connection.execute("SELECT COUNT(*) FROM trend_candidates").fetchone()[0]
        observation = connection.execute("SELECT heat_normalized,merge_reason FROM trend_observations ORDER BY rowid DESC LIMIT 1").fetchone()
    assert candidates == 1, "同一 URL 去除跟踪参数后应合并成一条候选"
    assert len(runs) == 1
    assert runs[0]["item_count"] == 2 and runs[0]["kept_count"] == 1 and runs[0]["duplicate_count"] == 1
    assert observation["merge_reason"] == "same_url"
    assert observation["heat_normalized"] is not None, "批内名次已知，应当归一"
