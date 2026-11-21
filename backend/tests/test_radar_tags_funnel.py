"""批次C：雷达来源标签与文章标签分离、失败分类、采集漏斗。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.app import config, db, radar_scoring, trends, trend_sources


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(trends, "start_monitor", lambda: None)
    monkeypatch.setattr(trends, "stop_monitor", lambda: None)
    monkeypatch.setattr(trends, "_launch_refresh", lambda *a: None)
    db.init_db()
    return tmp_path


def test_article_tags_detect_ai_in_tech_media():
    # 科技媒体的 AI 报道：标题命中 AI 术语，文章标签应含 ai，与来源标签无关。
    tags = radar_scoring.article_tags("用 Ollama 在本地推理部署大模型", "")
    assert "ai" in tags


def test_article_tags_multiple_and_ordered():
    tags = radar_scoring.article_tags("大模型开源与显卡部署", "")
    assert "ai" in tags
    # 多标签按命中数降序，首个是证据最强的方向。
    assert tags == sorted(set(tags), key=lambda d: tags.index(d))


def test_classify_error_categories():
    assert trends._classify_error("来源读取超时，请稍后重试") == "timeout"
    assert trends._classify_error("来源暂不可访问（HTTP 500）") == "http"
    assert trends._classify_error("来源 RSS/XML 格式无法可靠解析") == "parse"
    assert trends._classify_error("来源返回了空内容") == "empty"
    assert trends._classify_error("榜单缓存已超过一小时") == "stale"
    assert trends._classify_error("TLS 握手失败") == "connection"
    assert trends._classify_error("") is None


def test_candidate_view_exposes_source_and_article_tags(world):
    run = trends.refresh()
    catalog = {s["id"]: s for s in trends._catalog()}
    # bbc_technology 声明方向 tech；文章标题命中 AI 术语。
    item = {"title": "用 Ollama 在本地推理部署大模型", "url": "https://www.bbc.com/news/technology-ai",
            "summary": "本地推理部署", "rank": None, "heat_value": None, "heat_label": None, "published_at": None}
    trends._record_source(run["run_id"], catalog["bbc_technology"],
                          {"source_id": "bbc_technology", "status": "ok", "fetched_at": datetime.now(timezone.utc).isoformat(), "items": [item]})
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM trend_candidates WHERE title LIKE ?", ("%Ollama%",)).fetchone()
        view = trends._candidate_view(connection, row, trends.settings())
    assert view["source_tags"] == ["tech"]
    assert "ai" in view["article_tags"]


def test_funnel_accounts_kept_duplicates_and_dropped(world):
    from backend.app import radar_pipeline
    run_id = "run-1"
    with db.transaction() as connection:
        connection.execute("INSERT INTO trend_runs(id,state,started_at,source_results) VALUES(?,?,?,?)",
                           (run_id, "succeeded", datetime.now(timezone.utc).isoformat(), json.dumps([])))
        radar_pipeline.record_source_run(connection, run_id, "bbc_world", "ok", item_count=10, kept=3, duplicates=5, dropped=2)
        radar_pipeline.record_source_run(connection, run_id, "openai_news", "error", item_count=0, error="来源读取超时，请稍后重试")
    with db.connect() as connection:
        funnel = trends._funnel(connection)
    assert funnel["collected"] == 10
    assert funnel["kept"] == 3
    assert funnel["duplicates"] == 5
    assert funnel["dropped"] == 2
    assert funnel["sources_ok"] == 1 and funnel["sources_error"] == 1
    assert funnel["per_source"]["openai_news"]["error_category"] == "timeout"
