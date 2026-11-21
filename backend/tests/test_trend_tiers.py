"""来源分层与健康：谁该采、谁可以等、失败几次算降级。

这一层的价值不在「多一个字段」，而在两件事：
  1. 官方 RSS 不必陪着热榜每轮被拉，超时的源不再拖慢整批；
  2. 「本轮没采」和「这个源挂了」在界面上是两件事，不能混成一个红点。
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import config, db, trends

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    monkeypatch.setattr(trends, "_now", lambda: NOW)
    return config.get_settings()


# --------------------------------------------------------------------------
# 分层是数据，不是文案
# --------------------------------------------------------------------------

def test_every_source_declares_a_valid_tier(world):
    catalog = trends._catalog()
    assert catalog
    for source in catalog:
        assert source["tier"] in ("official", "interpretation", "signal"), source["id"]
        assert isinstance(source["min_interval_minutes"], int)
        assert source["min_interval_minutes"] >= 0


def test_official_sources_have_an_interval_and_hot_boards_are_collected_every_round(world):
    """实时热榜每轮都采；其余源（含榜单类但更新更慢的 HN 首页）可声明自己的间隔。

    原规则是「signal 层一律 0 分钟」，那时 signal 层只有百度/头条两个分钟级
    热榜。加入 Hacker News 首页后它同样是榜单（signal），但更新节奏是小时级，
    每轮都拉既无收益也占并发。因此规则按 kind 判定：只有实时热榜必须每轮采。
    """
    catalog = {source["id"]: source for source in trends._catalog()}
    for source_id, source in catalog.items():
        if source["tier"] == "official":
            assert source["min_interval_minutes"] > 0, f"{source_id} 官方源应当有最小间隔"
        if source["kind"] == "ranking":
            assert source["min_interval_minutes"] == 0, f"{source_id} 实时热榜应当每轮都采"


def test_the_interpretation_tier_only_holds_real_media_feeds(world):
    """中文解读层必须由真实媒体/作者的订阅源构成，不能拿热榜凑数。

    这一层在 P3 之前是空的，当时的断言是「不允许出现 interpretation」——
    那是为了防止硬塞。现在由 IT之家 / 爱范儿 / 少数派 / InfoQ 中国 / 阮一峰
    周刊填充，断言改为守住同一件事：它们必须是订阅源，不是榜单或聚合入口。
    """
    catalog = [source for source in trends._catalog() if source["tier"] == "interpretation"]
    assert catalog, "中文解读层已有真实来源，不应退回空层"
    for source in catalog:
        assert source["kind"] == "rss", f"{source['id']} 解读层不能用榜单冒充"
        assert source["min_interval_minutes"] > 0, f"{source['id']} 解读层不该陪热榜每轮被拉"


def test_every_direction_has_at_least_three_sources(world):
    """四大内容方向每类至少 3 个来源。任务书硬指标，钉死不许退化。

    生活方向靠 TrendRadar 的知乎/抖音/B站/贴吧补足 —— 只算内置 RSS 源时
    它只有 2 个，这也是为什么 TrendRadar 适配属于 P3 而不是可选项。
    """
    from backend.app import trend_sources
    counts = {direction: 0 for direction in trend_sources.DIRECTIONS}
    for source in trends._catalog():
        assert source["directions"], f"{source['id']} 没有归类到任何内容方向"
        for direction in source["directions"]:
            assert direction in counts, f"{source['id']} 出现未知方向 {direction}"
            counts[direction] += 1
    for direction, count in counts.items():
        assert count >= 3, f"{trend_sources.DIRECTION_LABELS[direction]} 只有 {count} 个来源"


def test_trendradar_platforms_are_part_of_the_catalog(world):
    """TrendRadar 的 10 个热榜平台必须在目录里，且带真实方向与发布平台归属。"""
    from backend.app import radar_trendradar
    ids = {source["id"] for source in trends._catalog()}
    for platform_id in radar_trendradar.PLATFORMS:
        assert radar_trendradar.PREFIX + platform_id in ids, f"{platform_id} 未进入来源目录"
    entry = next(item for item in trends._catalog() if item["id"] == "tr_toutiao")
    assert entry["platform"] == "toutiao", "头条热榜应归属头条平台，用于发布平台自身热点优先"
    assert entry["tier"] == "signal" and entry["kind"] == "trendradar"


# --------------------------------------------------------------------------
# 到期判断
# --------------------------------------------------------------------------

def test_first_round_collects_everything(world):
    ids = [source["id"] for source in trends._catalog()]
    due, skipped = trends._due_sources(ids, now=NOW)
    assert set(due) == set(ids)
    assert skipped == []


def test_official_source_is_not_due_again_immediately(world):
    trends._record_health("bbc_world", "ok")
    due, skipped = trends._due_sources(["bbc_world"], now=NOW + timedelta(minutes=30))
    assert due == []
    assert skipped[0]["status"] == "not_due"
    assert skipped[0]["next_at"], "要给出下次采集时间，否则界面只能显示「没数据」"


def test_signal_source_is_due_every_round(world):
    trends._record_health("baidu_realtime", "ok")
    due, _ = trends._due_sources(["baidu_realtime"], now=NOW + timedelta(minutes=1))
    assert due == ["baidu_realtime"]


def test_official_source_returns_after_its_interval(world):
    trends._record_health("openai_news", "ok")
    due, _ = trends._due_sources(["openai_news"], now=NOW + timedelta(minutes=179))
    assert due == []
    due, _ = trends._due_sources(["openai_news"], now=NOW + timedelta(minutes=181))
    assert due == ["openai_news"]


def test_repeated_failures_push_the_interval_out(world):
    for _ in range(trends.DEGRADED_AFTER):
        trends._record_health("bbc_world", "error", "timeout")
    # 官方源 180 分钟；连续失败 3 次后放大 DEGRADED_MULTIPLIER 倍 = 540。
    due, _ = trends._due_sources(["bbc_world"], now=NOW + timedelta(minutes=200))
    assert due == [], "已经挂掉的源不该按原间隔反复重试"
    due, _ = trends._due_sources(["bbc_world"], now=NOW + timedelta(minutes=541))
    assert due == ["bbc_world"], "降级不是永久拉黑，要能自动恢复"


def test_success_clears_the_failure_count(world):
    trends._record_health("bbc_world", "error", "timeout")
    trends._record_health("bbc_world", "error", "timeout")
    trends._record_health("bbc_world", "ok")
    record = trends._health_map()["bbc_world"]
    assert record["consecutive_failures"] == 0
    assert record["last_success_at"]
    assert record["last_error"] == ""


def test_degraded_flag_surfaces_in_state(world):
    for _ in range(trends.DEGRADED_AFTER):
        trends._record_health("google_research", "error", "timeout")
    state = trends.state()
    source = next(item for item in state["sources"] if item["id"] == "google_research")
    assert source["health"]["degraded"] is True
    assert source["health"]["consecutive_failures"] == trends.DEGRADED_AFTER
    assert source["tier"] == "official"


# --------------------------------------------------------------------------
# 本轮无源到期，不是失败
# --------------------------------------------------------------------------

def test_a_round_with_nothing_due_is_succeeded(world):
    trends._record_health("bbc_world", "ok")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO trend_runs(id,state,started_at,source_results) VALUES('r1','running',?,'[]')",
            (NOW.isoformat(),))
    trends._refresh("r1", ["bbc_world"])
    with db.connect() as connection:
        row = connection.execute("SELECT state,source_results FROM trend_runs WHERE id='r1'").fetchone()
    assert row["state"] == "succeeded", "没到点等于没活干，不等于失败"
    assert "not_due" in row["source_results"]
