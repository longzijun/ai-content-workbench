"""Offline signal-truthfulness tests; no platform, browser, model, or HTTP calls."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.app.trend_ranking import count_cross_sources, rank_event


NOW = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)


def observed(minutes=0, rank=20, heat=None, label="热搜指数", source="baidu-hot", publisher="baidu", kind="ranking", **changes):
    record = {"source_id": source, "publisher_id": publisher, "source_name": source, "source_url": "https://example.com/" + source, "kind": kind, "fetched_at": (NOW - timedelta(minutes=minutes)).isoformat(), "rank": rank, "heat_value": heat, "heat_label": label}
    record.update(changes)
    return record


def event(observations=(), title="一条普通来源新闻", published_hours=1, **changes):
    result = {"title": title, "url": "https://example.com/article", "summary": "来源摘要需要对照原文核查。", "published_at": (NOW - timedelta(hours=published_hours)).isoformat() if published_hours is not None else None, "observations": list(observations)}
    result.update(changes)
    return result


def ranking(result):
    return result["signal_evidence"]["ranking"]


def test_real_recent_top_rank_is_hot_but_one_fetch_has_no_speed_claim():
    result = rank_event(event([observed(rank=3)]), NOW)
    assert result["classification"] == "hot" and result["eligible"] is True
    assert ranking(result)["current"][0]["rank"] == 3
    assert ranking(result)["comparisons"] == []
    assert any("一次有效抓取" in item for item in result["uncertainties"])
    assert "推荐优先级" in result["priority_label"]
    assert "不是预测热度" in result["signal_evidence"]["priority_semantics"]


@pytest.mark.parametrize("kind", ["rss", "news", "article", "search"])
def test_rss_or_news_ordinal_and_heat_are_never_public_ranking(kind):
    result = rank_event(event([observed(rank=1, heat=999999, kind=kind)]), NOW)
    assert result["classification"] == "context"
    assert ranking(result)["current"] == []
    assert any("RSS" in item for item in result["uncertainties"])


@pytest.mark.parametrize("rank", [None, 0, -1, True, 2.5, "unknown"])
def test_invalid_rank_cannot_create_hot_label(rank):
    result = rank_event(event([observed(rank=rank)]), NOW)
    assert result["classification"] != "hot"


def test_same_board_rank_gain_requires_real_different_times_and_gain_of_five():
    result = rank_event(event([observed(5, rank=25), observed(0, rank=20)]), NOW)
    assert result["classification"] == "rising"
    comparison = ranking(result)["comparisons"][0]
    assert comparison["interval_minutes"] == 5 and comparison["rank_gain"] == 5
    assert comparison["qualifies_by"] == ["rank"]
    assert any("同榜" in reason for reason in result["reasons"])


@pytest.mark.parametrize("minutes,should_rise", [(4.9, False), (5, True), (1440, True), (1440.1, False)])
def test_comparison_window_is_five_minutes_through_twentyfour_hours(minutes, should_rise):
    result = rank_event(event([observed(minutes, rank=30), observed(rank=20)]), NOW)
    assert (result["classification"] == "rising") is should_rise


def test_nearest_baseline_is_used_instead_of_cherry_picking_older_positive_pair():
    result = rank_event(event([observed(60, rank=80), observed(10, rank=20), observed(rank=30)]), NOW)
    assert result["classification"] != "rising"
    comparison = ranking(result)["comparisons"][0]
    assert comparison["previous_rank"] == 20 and comparison["rank_gain"] == -10


def test_duplicate_fetch_time_is_not_a_second_observation():
    result = rank_event(event([observed(rank=20), observed(rank=20)]), NOW)
    assert result["classification"] != "rising"
    assert ranking(result)["comparisons"] == []


def test_conflicting_same_time_records_are_not_chosen_for_better_rank():
    result = rank_event(event([observed(rank=1), observed(rank=50)]), NOW)
    assert result["classification"] not in {"hot", "rising"}
    assert any("冲突" in item for item in result["uncertainties"])


def test_conflicting_latest_capture_does_not_fall_back_to_older_hot_rank():
    result = rank_event(event([observed(10, rank=2), observed(rank=1), observed(rank=50)]), NOW)
    assert result["classification"] not in {"hot", "rising"}
    assert ranking(result)["current"] == []
    assert ranking(result)["historical"][0]["rank"] == 2


@pytest.mark.parametrize("new_heat,should_rise", [(124, False), (125, True), (200, True)])
def test_same_metric_heat_growth_has_explicit_twentyfive_percent_threshold(new_heat, should_rise):
    result = rank_event(event([observed(10, heat=100), observed(heat=new_heat)]), NOW)
    assert (result["classification"] == "rising") is should_rise
    assert ranking(result)["comparisons"][0]["heat_growth_fraction"] == (new_heat - 100) / 100


@pytest.mark.parametrize("old_label,new_label", [("阅读量", "热搜指数"), ("", "热搜指数"), ("", "")])
def test_missing_or_changed_heat_metric_is_not_compared(old_label, new_label):
    result = rank_event(event([observed(10, heat=100, label=old_label), observed(heat=200, label=new_label)]), NOW)
    assert result["classification"] != "rising"
    assert ranking(result)["comparisons"][0]["heat_growth_fraction"] is None


def test_zero_heat_baseline_does_not_invent_infinite_growth():
    result = rank_event(event([observed(10, heat=0), observed(heat=100)]), NOW)
    assert result["classification"] != "rising"
    assert ranking(result)["comparisons"][0]["heat_growth_fraction"] is None
    assert any("基线为零" in item for item in result["uncertainties"])


def test_extreme_heat_ratio_stays_finite_json_and_is_not_a_rising_signal():
    result = rank_event(event([observed(10, heat=1e-300), observed(heat=1e300)]), NOW)
    assert result["classification"] != "rising"
    assert ranking(result)["comparisons"][0]["heat_growth_fraction"] is None
    json.dumps(result, allow_nan=False)


def test_heat_from_two_sources_is_not_compared_or_combined():
    result = rank_event(event([observed(10, rank=30, heat=100, source="one", publisher="one"), observed(rank=20, heat=1000000, source="two", publisher="two")]), NOW)
    assert result["classification"] == "watch"
    assert ranking(result)["comparisons"] == []
    assert not any("增加" in item for item in result["reasons"])
    assert "heat_value" not in result["signal_evidence"]


def test_different_boards_with_same_source_id_are_not_a_trend_pair():
    result = rank_event(event([observed(10, rank=30, board_id="daily"), observed(rank=20, board_id="weekly")]), NOW)
    assert result["classification"] != "rising"
    assert ranking(result)["comparisons"] == []


def test_stale_current_board_has_only_historical_evidence():
    result = rank_event(event([observed(180, rank=20), observed(121, rank=2)]), NOW)
    assert result["classification"] not in {"hot", "rising"}
    assert ranking(result)["current"] == [] and len(ranking(result)["historical"]) == 1
    assert any("超过2小时" in item for item in result["uncertainties"])


def test_observed_time_overrides_recent_fetch_of_stale_board():
    result = rank_event(event([observed(rank=1, observed_at=(NOW - timedelta(hours=4)).isoformat())]), NOW)
    assert result["classification"] != "hot"
    assert ranking(result)["historical"][0]["age_minutes"] == 240


@pytest.mark.parametrize("time_field", [None, "not-a-date", "2026-10-12T04:00:00", (NOW + timedelta(minutes=1)).isoformat()])
def test_unknown_naive_or_future_board_time_cannot_create_hot_or_rising(time_field):
    result = rank_event(event([observed(rank=1, fetched_at=time_field)]), NOW)
    assert result["classification"] == "context"
    assert ranking(result)["current"] == []


def test_future_publication_is_not_promoted_even_with_current_rank():
    result = rank_event(event([observed(rank=1)], published_at=(NOW + timedelta(hours=1)).isoformat()), NOW)
    assert result["classification"] == "context" and result["priority"] == 0
    assert result["signal_evidence"]["publication"]["status"] == "future"


def test_current_rank_with_unknown_article_date_does_not_claim_first_release():
    result = rank_event(event([observed(rank=2)], published_hours=None), NOW)
    assert result["classification"] == "hot"
    assert any("发布时间未知" in item for item in result["uncertainties"])
    assert not any("新近首发" in item for item in result["reasons"])


def test_old_article_can_only_be_current_hot_when_a_fresh_real_board_supports_it():
    fresh = rank_event(event([observed(rank=2)], published_hours=8 * 24), NOW)
    old_board = rank_event(event([observed(180, rank=2)], published_hours=8 * 24), NOW)
    assert fresh["classification"] == "hot" and old_board["classification"] == "context"
    assert any("超过7天" in item for item in fresh["uncertainties"])


def test_single_publisher_multiple_feeds_are_not_multi_source_confirmation():
    observations = [observed(kind="rss", rank=None, source="bbc-world", publisher="bbc", source_url="https://feeds.bbci.co.uk/news/world/rss.xml"), observed(kind="rss", rank=None, source="bbc-tech", publisher="bbc", source_url="https://feeds.bbci.co.uk/news/technology/rss.xml")]
    assert count_cross_sources(observations) == 1
    result = rank_event(event(observations), NOW)
    assert result["classification"] == "context"
    assert result["signal_evidence"]["sources"]["publisher_or_host_count"] == 1
    assert any("同一发布者" in item for item in result["uncertainties"])


def test_hostname_fallback_deduplicates_feeds_and_mixed_identity_metadata():
    observations = [observed(source="one", publisher="bbc", source_url="https://www.bbc.com/feed/world"), observed(source="two", publisher="", source_url="https://bbc.com/feed/technology")]
    assert count_cross_sources(observations) == 1
    assert count_cross_sources([{"source_id": "a"}, {"source_id": "b"}]) == 0


def test_two_publishers_are_only_multi_source_leads_not_verified_facts():
    result = rank_event(event([observed(kind="rss", rank=None, publisher="bbc", source="bbc"), observed(kind="rss", rank=None, publisher="guardian", source="guardian")]), NOW)
    assert result["classification"] == "watch"
    assert result["signal_evidence"]["sources"]["independence_verified"] is False
    assert any("需核查是否转载" in item for item in result["reasons"])


def test_recent_release_is_watch_without_promising_it_will_be_popular():
    result = rank_event(event([observed(kind="rss", rank=None)], title="开源办公工具发布新版本"), NOW)
    assert result["classification"] == "watch"
    assert any("不保证" in item for item in result["uncertainties"])
    assert "不虚构个人亲测" in result["angle"]


def test_configured_news_window_does_not_relabel_old_policy_as_new():
    candidate = event([observed(kind="rss", rank=None)], title="公开新规政策说明", published_hours=48)
    assert rank_event(candidate, NOW, window_days=7)["classification"] == "watch"
    assert rank_event(candidate, NOW, window_days=1)["classification"] == "context"


def test_broad_mode_keeps_social_news_even_with_ai_keyword_preferences():
    candidate = event([observed(kind="rss", rank=None)], title="公共交通政策发布", summary="社会与民生信息。")
    broad = rank_event(candidate, NOW, scope="broad", keywords=["AI"])
    ai_only = rank_event(candidate, NOW, scope="ai_only", keywords=["AI"])
    assert broad["eligible"] is True and broad["classification"] == "watch"
    assert ai_only["eligible"] is False and ai_only["priority"] == 0
    assert ai_only["recommended_platforms"] == []


def test_ai_filter_uses_word_boundaries_and_supports_known_ai_topics():
    assert rank_event(event(title="Spain air transport update"), NOW, scope="ai_only")["eligible"] is False
    assert rank_event(event(title="OpenAI发布模型更新"), NOW, scope="ai_only")["eligible"] is True
    assert rank_event(event(title="机器人开源工具发布"), NOW, scope="ai_only", keywords=["机器人"])["eligible"] is True


def test_sensitive_public_topics_require_sources_and_do_not_generate_panic_titles():
    result = rank_event(event(title="震惊！医疗健康新规发布"), NOW)
    assert "震惊" not in result["suggested_title"]
    assert any("核对官方或一手来源" in item for item in result["uncertainties"])
    assert "避免制造恐慌" in result["angle"]


def test_output_is_strict_json_and_priority_orders_evidenced_signals():
    hot = rank_event(event([observed(rank=3)]), NOW)
    rising = rank_event(event([observed(10, rank=40), observed(rank=20)]), NOW)
    watch = rank_event(event(title="开源工具发布"), NOW)
    context = rank_event(event([observed(kind="rss", rank=None, heat=float("nan"))]), NOW)
    assert hot["priority"] > rising["priority"] > watch["priority"] > context["priority"]
    for result in (hot, rising, watch, context):
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        assert 0 <= result["priority"] <= 100
        assert set(result["recommended_platforms"]) <= {"wechat", "toutiao", "xiaohongshu"}


@pytest.mark.parametrize("now,scope,keywords,window", [(NOW.replace(tzinfo=None), "broad", [], 7), (NOW, "typo", [], 7), (NOW, "broad", "AI", 7), (NOW, "broad", [], 0)])
def test_invalid_contract_parameters_are_rejected(now, scope, keywords, window):
    with pytest.raises(ValueError):
        rank_event(event(), now, scope=scope, keywords=keywords, window_days=window)
