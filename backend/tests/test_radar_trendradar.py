"""TrendRadar 输出适配器：只读本地 SQLite，缺数据就说缺，不拿旧榜单顶替。"""
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import subprocess

import pytest

from backend.app import config, radar_trendradar

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

_SCHEMA = """
CREATE TABLE news_items (
    id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER, url TEXT,
    mobile_url TEXT, first_crawl_time TEXT, last_crawl_time TEXT, crawl_count INTEGER,
    created_at TEXT, updated_at TEXT);
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.delenv("WORKBENCH_TRENDRADAR_OUTPUT", raising=False)
    return tmp_path


def _make_db(directory, rows, name="2026-10-22.db", age_hours=0):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.executemany(
        "INSERT INTO news_items(title,platform_id,rank,url,mobile_url,first_crawl_time,last_crawl_time,crawl_count)"
        " VALUES(?,?,?,?,'',?,?,?)", rows)
    connection.commit()
    connection.close()
    if age_hours:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (stamp, stamp))
    return path


def test_catalog_lists_all_ten_platforms_with_directions():
    catalog = radar_trendradar.catalog()
    assert len(catalog) == 10
    for entry in catalog:
        assert entry["id"].startswith(radar_trendradar.PREFIX)
        assert entry["directions"], f"{entry['id']} 没有内容方向"
        assert entry["tier"] == "signal" and entry["kind"] == "trendradar"


def test_life_direction_is_covered_by_trendradar_platforms():
    """生活方向靠知乎/抖音/B站/贴吧补足 —— 内置订阅源在这里只有 2 个。"""
    from backend.app.trend_sources import DIRECTION_LIFE
    covered = [entry["id"] for entry in radar_trendradar.catalog() if DIRECTION_LIFE in entry["directions"]]
    assert len(covered) >= 3


def test_unconfigured_output_is_reported_not_faked(world):
    result = radar_trendradar.collect("tr_toutiao")
    assert result["status"] == "error" and result["items"] == []
    assert "未配置" in result["error"]
    assert radar_trendradar.state()["configured"] is False


def test_collect_reads_ranks_and_keeps_publication_time_unknown(world):
    _make_db(world / "news", [
        ("西藏那曲发生地震", "toutiao", 1, "https://www.toutiao.com/trending/1/", "01-51", "01-53", 2),
        ("第二条热点", "toutiao", 2, "https://www.toutiao.com/trending/2/", "01-51", "01-53", 1),
    ])
    monkeypatch_env = world
    os.environ["WORKBENCH_TRENDRADAR_OUTPUT"] = str(monkeypatch_env / "news")
    try:
        result = radar_trendradar.collect("tr_toutiao")
    finally:
        del os.environ["WORKBENCH_TRENDRADAR_OUTPUT"]
    assert result["status"] == "ok" and len(result["items"]) == 2
    first = result["items"][0]
    assert first["rank"] == 1 and first["rank_verified"] is True
    # 热榜不给事件发布时间：保持未知，不用抓取时间冒充发布时间。
    assert first["published_at"] is None
    assert first["summary"] == "今日头条热榜线索，未提供事件原文摘要；正文与发布时间需补原始出处。"
    assert first["internal_tracking"]["crawl_count"] == 2
    assert "出现 2 次" not in first["summary"]
    assert any("抓取时间不等于发布时间" in line for line in first["signal_uncertainties"])


def test_rows_with_unexpected_hosts_are_dropped(world):
    """上游出现异常域名时宁可丢弃，也不放进证据链。"""
    _make_db(world / "news", [
        ("正常条目", "toutiao", 1, "https://www.toutiao.com/trending/1/", "01-51", "01-53", 1),
        ("异常域名", "toutiao", 2, "https://evil.example.com/trending/2/", "01-51", "01-53", 1),
        ("明文协议", "toutiao", 3, "http://www.toutiao.com/trending/3/", "01-51", "01-53", 1),
    ])
    os.environ["WORKBENCH_TRENDRADAR_OUTPUT"] = str(world / "news")
    try:
        result = radar_trendradar.collect("tr_toutiao")
    finally:
        del os.environ["WORKBENCH_TRENDRADAR_OUTPUT"]
    assert result["status"] == "ok"
    assert [item["rank"] for item in result["items"]] == [1]


def test_stale_output_is_not_treated_as_live_hot_list(world):
    _make_db(world / "news", [("旧热点", "toutiao", 1, "https://www.toutiao.com/trending/1/", "01-51", "01-53", 1)],
             age_hours=radar_trendradar.MAX_OUTPUT_AGE_HOURS + 2)
    os.environ["WORKBENCH_TRENDRADAR_OUTPUT"] = str(world / "news")
    try:
        result = radar_trendradar.collect("tr_toutiao")
        state = radar_trendradar.state()
    finally:
        del os.environ["WORKBENCH_TRENDRADAR_OUTPUT"]
    assert result["status"] == "error" and "未更新" in result["error"]
    assert state["note"] and "未更新" in state["note"]


def test_configure_persists_the_output_directory(world):
    radar_trendradar.configure(str(world / "news"))
    assert radar_trendradar.state()["output_dir"] == str(world / "news")
    with pytest.raises(ValueError):
        radar_trendradar.configure("")


def _runner_layout(world, monkeypatch):
    root = world / "TrendRadar"
    output = root / "output" / "news"
    database = _make_db(output, [("新热点", "toutiao", 1, "https://www.toutiao.com/trending/1/", "01-51", "01-53", 1)])
    (root / "trendradar").mkdir(parents=True)
    (root / "trendradar" / "__main__.py").write_text("# test entry", "utf-8")
    python = world / "trendradar-python.exe"
    python.write_text("test", "utf-8")
    monkeypatch.setenv("WORKBENCH_TRENDRADAR_OUTPUT", str(output))
    monkeypatch.setenv("WORKBENCH_TRENDRADAR_PYTHON", str(python))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    return root, database, python


def test_refresh_output_runs_one_isolated_process_and_requires_new_db(world, monkeypatch):
    root, database, python = _runner_layout(world, monkeypatch)
    captured = {}

    def fake_run(command, **kwargs):
        captured.update({"command": command, **kwargs})
        stamp = database.stat().st_mtime + 10
        os.utime(database, (stamp, stamp))
        return subprocess.CompletedProcess(command, 0, "采集完成", "")

    monkeypatch.setattr(radar_trendradar.subprocess, "run", fake_run)
    result = radar_trendradar.refresh_output()
    assert result["status"] == "ok"
    assert captured["command"] == [str(python), "-m", "trendradar"]
    assert captured["cwd"] == str(root)
    assert captured["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "HTTP_PROXY" not in captured["env"]
    assert result["log"]


def test_refresh_output_failure_is_explicit(world, monkeypatch):
    _runner_layout(world, monkeypatch)
    monkeypatch.setattr(radar_trendradar.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "network down"))
    result = radar_trendradar.refresh_output()
    assert result["status"] == "error"
    assert "network down" in result["error"]


def test_refresh_output_rejects_success_without_a_new_snapshot(world, monkeypatch):
    _runner_layout(world, monkeypatch)
    monkeypatch.setattr(radar_trendradar.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "nothing happened", ""))
    result = radar_trendradar.refresh_output()
    assert result["status"] == "error"
    assert "没有更新输出数据库" in result["error"]
