"""四大方向分类与六维可解释评分。

钉住的是三条红线：

1. **权重必须与任务书一致**（20/20/20/15/15/10），改权重必须同步改测试。
2. **缺数据不许伪造**：没有热度就给 0 分 + 低置信度 + 写明缺口，
   绝不给一个"看起来合理"的中间值。
3. **高风险不因高分放行**：`publishable` 是独立于总分的第二道闸。
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import radar_scoring
from backend.app.trend_sources import (DIRECTION_AI, DIRECTION_LIFE,
                                       DIRECTION_SOCIETY, DIRECTION_TECH)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _observation(source_id="s1", tier="official", publisher="openai", rank=None,
                 heat=None, stamp=None):
    return {"source_id": source_id, "tier": tier, "publisher_id": publisher,
            "kind": "ranking" if rank else "rss", "rank": rank,
            "heat_normalized": heat,
            "fetched_at": (stamp or NOW).isoformat()}


# --------------------------------------------------------------------------
# 权重与结构
# --------------------------------------------------------------------------

def test_weights_match_the_brief_and_sum_to_one():
    assert radar_scoring.WEIGHTS == {"heat": 0.20, "timeliness": 0.20, "audience": 0.20,
                                     "value": 0.15, "originality": 0.15, "credibility": 0.10}
    assert round(sum(radar_scoring.WEIGHTS.values()), 10) == 1.0
    assert set(radar_scoring.WEIGHTS) == set(radar_scoring.DIMENSION_LABELS)


def test_account_mix_weights_match_the_brief():
    assert radar_scoring.DIRECTION_WEIGHTS == {DIRECTION_AI: 0.50, DIRECTION_TECH: 0.25,
                                               DIRECTION_SOCIETY: 0.15, DIRECTION_LIFE: 0.10}


def test_every_dimension_is_returned_with_a_weight_a_score_and_a_basis():
    result = radar_scoring.evaluate(
        {"title": "OpenAI 发布新模型", "summary": "官方公告", "url": "https://openai.com/x",
         "published_at": (NOW - timedelta(hours=3)).isoformat(),
         "directions": [DIRECTION_AI]},
        [_observation()], now=NOW)
    assert len(result["dimensions"]) == 6
    for entry in result["dimensions"]:
        assert 0.0 <= entry["score"] <= 1.0
        assert 0.0 <= entry["confidence"] <= 1.0
        assert entry["basis"], f"{entry['key']} 必须给出依据"
        assert entry["label"]
    assert 0 <= result["total"] <= 100
    assert isinstance(result["total"], int)


# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------

def test_source_declared_direction_wins_over_keywords():
    """来源采集时已声明方向，就不要再用关键词猜一遍并把猜测当成结论。"""
    classified = radar_scoring.classify("医保新规落地", "事关就业与社保", [DIRECTION_TECH])
    assert classified["direction"] == DIRECTION_TECH
    assert classified["method"] == "rule"
    assert any("来源声明方向" in item for item in classified["basis"])


def test_without_a_declared_direction_keywords_decide_and_say_so():
    classified = radar_scoring.classify("arXiv 新论文提出新的 benchmark", "")
    assert classified["direction"] == DIRECTION_AI
    assert any("按关键词判定" in item for item in classified["basis"])


def test_unclassifiable_falls_back_to_ai_and_declares_the_guess():
    classified = radar_scoring.classify("今天天气不错", "")
    assert classified["direction"] == DIRECTION_AI
    assert any("待人工调整" in item for item in classified["basis"])


def test_a_major_direction_is_chosen_by_evidence_not_by_list_order():
    """候选方向是各来源声明的并集，顺序取决于哪个源先被采集。

    实测：地震条目被 tr_douyin（声明 life,society）采集，取第一项就归成了
    "生活观察"，推荐跟着变成"小红书 + 步骤教程"。方向必须按证据选。
    """
    classified = radar_scoring.classify("西藏那曲发生7级地震", "暂无人员伤亡报告",
                                        [DIRECTION_LIFE, DIRECTION_SOCIETY])
    assert classified["direction"] == DIRECTION_SOCIETY
    assert any("个方向" in item for item in classified["basis"])


def test_without_keyword_evidence_the_account_weight_decides():
    classified = radar_scoring.classify("一个没有任何方向特征的标题", "",
                                        [DIRECTION_LIFE, DIRECTION_AI])
    assert classified["direction"] == DIRECTION_AI
    assert any("账号占比" in item for item in classified["basis"])


def test_subtopics_are_detected_when_the_direction_matches():
    classified = radar_scoring.classify("Ollama 发布新版本，量化推理显存占用下降", "",
                                        [DIRECTION_AI])
    assert "基础设施" in classified["subtopics"]


# --------------------------------------------------------------------------
# 缺数据不许伪造
# --------------------------------------------------------------------------

def test_without_observations_heat_is_zero_and_the_gap_is_declared():
    result = radar_scoring.evaluate({"title": "某条线索", "url": "https://x.com/a"},
                                    [], now=NOW)
    heat = next(d for d in result["dimensions"] if d["key"] == "heat")
    assert heat["score"] == 0.0
    assert heat["confidence"] <= 0.2
    assert any("采集记录" in gap for gap in result["data_gaps"])
    assert result["estimated"] is True


def test_repeated_collection_rounds_do_not_inflate_heat():
    """同一个源每轮采集都留一条记录，轮次多不等于热度高。"""
    one_round = [_observation(source_id="a", tier="ranking", publisher="p1", rank=10, heat=0.5)]
    ten_rounds = [_observation(source_id="a", tier="ranking", publisher="p1", rank=10, heat=0.5)
                  for _ in range(10)]
    single = radar_scoring._heat_dimension(one_round)[0]
    repeated = radar_scoring._heat_dimension(ten_rounds)[0]
    assert repeated == single, "采集轮次不能抬高热度分"


def test_two_independent_sources_score_higher_than_one():
    one = [_observation(source_id="a", tier="ranking", publisher="p1", rank=10, heat=0.5)]
    two = [_observation(source_id="a", tier="ranking", publisher="p1", rank=10, heat=0.5),
           _observation(source_id="b", tier="ranking", publisher="p2", rank=12, heat=0.4)]
    assert radar_scoring._heat_dimension(two)[0] > radar_scoring._heat_dimension(one)[0]


def test_a_source_without_any_heat_value_does_not_invent_one():
    result = radar_scoring.evaluate(
        {"title": "某条线索", "url": "https://x.com/a", "directions": [DIRECTION_AI]},
        [_observation(tier="official", rank=None, heat=None)], now=NOW)
    heat = next(d for d in result["dimensions"] if d["key"] == "heat")
    assert heat["score"] == 0.0
    assert any("不提供名次或热度值" in item for item in heat["basis"])


def test_a_missing_publication_date_is_estimated_from_discovery_and_declared():
    result = radar_scoring.evaluate(
        {"title": "某条线索", "url": "https://x.com/a",
         "discovered_at": (NOW - timedelta(hours=5)).isoformat()},
        [_observation()], now=NOW)
    timing = next(d for d in result["dimensions"] if d["key"] == "timeliness")
    assert timing["confidence"] <= 0.4
    assert any("估算" in item for item in timing["basis"])
    assert any("未提供发布时间" in gap for gap in result["data_gaps"])
    assert result["estimated"] is True


def test_a_real_publication_date_gives_a_confident_timeliness():
    result = radar_scoring.evaluate(
        {"title": "某条线索", "url": "https://x.com/a",
         "published_at": (NOW - timedelta(hours=2)).isoformat()},
        [_observation()], now=NOW)
    timing = next(d for d in result["dimensions"] if d["key"] == "timeliness")
    assert timing["score"] == 1.0
    assert timing["confidence"] >= 0.8


def test_a_missing_url_caps_credibility():
    result = radar_scoring.evaluate({"title": "某条线索", "directions": [DIRECTION_AI]},
                                    [_observation(tier="official")], now=NOW)
    credibility = next(d for d in result["dimensions"] if d["key"] == "credibility")
    assert credibility["score"] <= 0.2
    assert any("不是可访问的原始报道页" in item for item in credibility["basis"])


def test_a_hot_board_aggregator_url_caps_credibility_too():
    result = radar_scoring.evaluate(
        {"title": "某热搜词条", "url": "https://newsnow.busiyi.world/api/s?id=toutiao&latest",
         "directions": [DIRECTION_SOCIETY]},
        [_observation(tier="signal", publisher="toutiao", rank=1, heat=1.0)], now=NOW)
    credibility = next(d for d in result["dimensions"] if d["key"] == "credibility")
    assert credibility["score"] <= 0.2
    assert any("聚合页" in flag for flag in result["risk_flags"])
    assert result["publishable"] is False
    assert "榜单或聚合页" in result["publishable_reason"]


def test_a_real_report_url_counts_as_an_original_source():
    assert radar_scoring.is_original_report("https://www.chinanews.com.cn/shipin/2026/10-23/news.html")
    assert not radar_scoring.is_original_report("https://top.baidu.com/board?tab=realtime")
    assert not radar_scoring.is_original_report("https://newsnow.busiyi.world/api/s?id=zhihu&latest")
    assert not radar_scoring.is_original_report("")


# --------------------------------------------------------------------------
# 受众匹配
# --------------------------------------------------------------------------

def test_audience_follows_the_account_mix():
    ai = radar_scoring._audience_dimension(DIRECTION_AI, [])
    tech = radar_scoring._audience_dimension(DIRECTION_TECH, [])
    life = radar_scoring._audience_dimension(DIRECTION_LIFE, [])
    assert ai[0] > tech[0] > life[0]
    assert "50%" in ai[2][0]


def test_audience_leaves_room_for_keywords():
    """最高占比方向不能一上来就满分，否则关键词命中与否在分数上没有区别。"""
    assert radar_scoring._audience_dimension(DIRECTION_AI, [])[0] < 1.0



def test_matching_custom_keywords_raise_audience_fit():
    base = radar_scoring._audience_dimension(DIRECTION_AI, [])
    hit = radar_scoring._audience_dimension(DIRECTION_AI, ["ComfyUI", "量化"])
    assert hit[0] > base[0]
    assert any("关键词" in item for item in hit[2])


# --------------------------------------------------------------------------
# 风险闸门（与总分解耦）
# --------------------------------------------------------------------------

def test_a_high_risk_hot_board_item_is_not_publishable_even_when_it_scores_well():
    """热榜出现伤亡类词条，分数再高也不自动推荐为可发布内容。"""
    result = radar_scoring.evaluate(
        {"title": "某地发生事故造成多人遇难", "summary": "现场已封锁，原因正在调查",
         "url": "https://x.com/a",
         "published_at": (NOW - timedelta(hours=1)).isoformat(),
         "directions": [DIRECTION_SOCIETY]},
        [_observation(tier="signal", publisher="toutiao", rank=1, heat=1.0)], now=NOW)
    assert result["total"] >= 40  # 分数不低，但照样不能被放行
    assert result["publishable"] is False
    assert result["publishable_reason"]
    assert any("高风险题材" in flag for flag in result["risk_flags"])
    assert any("没有可访问的原始报道" in flag for flag in result["risk_flags"])


def test_a_high_risk_item_with_official_cross_checked_sources_may_pass():
    result = radar_scoring.evaluate(
        {"title": "某地发生事故造成多人遇难", "summary": "官方通报已发布，调查进展同步更新",
         "url": "https://x.com/a",
         "published_at": (NOW - timedelta(hours=1)).isoformat(),
         "directions": [DIRECTION_SOCIETY]},
        [_observation(source_id="a", tier="official", publisher="chinanews", rank=None),
         _observation(source_id="b", tier="interpretation", publisher="bbc", rank=None),
         _observation(source_id="c", tier="interpretation", publisher="thepaper", rank=None)],
        now=NOW)
    assert result["publishable"] is True


def test_a_single_source_item_is_flagged_even_without_risk_terms():
    result = radar_scoring.evaluate(
        {"title": "某模型发布新版本", "url": "https://x.com/a",
         "published_at": (NOW - timedelta(hours=1)).isoformat(),
         "directions": [DIRECTION_AI]},
        [_observation(tier="official")], now=NOW)
    assert any("单一来源" in flag for flag in result["risk_flags"])


def test_a_hot_board_only_item_is_not_publishable_even_without_risk_terms():
    """热榜只说明有人在看，不说明确有此事——没有原始出处就不自动推荐。"""
    result = radar_scoring.evaluate(
        {"title": "某明星官宣新作品", "url": "https://x.com/a",
         "discovered_at": (NOW - timedelta(hours=2)).isoformat(),
         "directions": [DIRECTION_SOCIETY]},
        [_observation(source_id="a", tier="signal", publisher="toutiao", rank=2, heat=0.95),
         _observation(source_id="b", tier="signal", publisher="baidu", rank=5, heat=0.8)],
        now=NOW)
    assert result["total"] >= 40
    assert result["publishable"] is False
    assert "热榜线索" in result["publishable_reason"]
    assert any("不能当作已核实事实" in flag for flag in result["risk_flags"])


def test_an_official_source_lifts_the_unverified_block():
    result = radar_scoring.evaluate(
        {"title": "某明星官宣新作品", "url": "https://x.com/a",
         "published_at": (NOW - timedelta(hours=2)).isoformat(),
         "directions": [DIRECTION_SOCIETY]},
        [_observation(source_id="a", tier="signal", publisher="toutiao", rank=2, heat=0.95),
         _observation(source_id="b", tier="official", publisher="chinanews")],
        now=NOW)
    assert result["publishable"] is True


def test_a_clean_item_has_no_blocking_reason():
    result = radar_scoring.evaluate(
        {"title": "Ollama 发布新版本，支持更多量化格式",
         "summary": "官方博客公布了本次更新的完整清单，包含推理性能改进与显存优化。",
         "url": "https://ollama.com/blog/x",
         "published_at": (NOW - timedelta(hours=4)).isoformat(),
         "directions": [DIRECTION_AI]},
        [_observation(tier="official", publisher="ollama")], now=NOW)
    assert result["publishable"] is True
    assert result["publishable_reason"] == ""
    assert result["risk_flags"] == [] or all("高风险" not in f for f in result["risk_flags"])


# --------------------------------------------------------------------------
# 本地模型增强层：默认规则，失败回落，不假装模型参与过
# --------------------------------------------------------------------------

def test_the_model_may_only_choose_among_the_four_directions():
    request = radar_scoring.build_classify_request({"title": "x"}, "local-model")
    schema = request["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["direction"]["enum"] == list(radar_scoring._MODEL_DIRECTIONS)


def test_the_token_budget_leaves_room_for_reasoning_models():
    """推理型本地模型会把预算花在思考上，给太小会整段输出被判为截断。

    实测 qwen3.5-9b：600 三次里失败两次，2000 三次全通过。
    """
    assert radar_scoring.CLASSIFY_MAX_TOKENS >= 1500


def test_a_direction_outside_the_enum_is_rejected_not_corrected():
    with pytest.raises(ValueError):
        radar_scoring.validate_classification(
            {"direction": "财经", "subtopics": [], "angle": "x", "confidence": 50, "cautions": []})


def test_a_boolean_confidence_is_rejected_since_bool_is_an_int_subclass():
    with pytest.raises(ValueError):
        radar_scoring.validate_classification(
            {"direction": DIRECTION_AI, "subtopics": [], "angle": "x", "confidence": True, "cautions": []})


def test_a_missing_angle_is_rejected():
    with pytest.raises(ValueError):
        radar_scoring.validate_classification(
            {"direction": DIRECTION_AI, "subtopics": [], "angle": "  ", "confidence": 50, "cautions": []})


def test_unusable_model_output_falls_back_to_none_instead_of_raising():
    def broken(request):
        raise RuntimeError("LM Studio 未连接")

    assert radar_scoring.classify_with_model({"title": "x"}, ask_model=broken,
                                             model="m", parse_json=lambda raw: {}) is None


def test_model_output_that_cannot_be_parsed_falls_back_to_none():
    def ok(request):
        return "不是 JSON"

    def boom(raw):
        raise ValueError("无法解析")

    assert radar_scoring.classify_with_model({"title": "x"}, ask_model=ok,
                                             model="m", parse_json=boom) is None


def test_a_model_classification_overrides_the_direction_only():
    """模型只能改分类，不能改六个维度的分数——维度必须由可核对的事实推导。"""
    candidate = {"title": "某条线索", "summary": "摘要", "url": "https://x.com/a",
                 "published_at": (NOW - timedelta(hours=1)).isoformat(),
                 "directions": [DIRECTION_AI]}
    observations = [_observation(tier="official")]
    by_rule = radar_scoring.evaluate(candidate, observations, now=NOW)
    by_model = radar_scoring.evaluate(
        candidate, observations, now=NOW,
        model_classification={"direction": DIRECTION_LIFE, "subtopics": ["效率工具"],
                              "angle": "从普通人的时间成本切入", "confidence": 72,
                              "cautions": ["标题未给出具体数据"]})
    assert by_rule["direction"] == DIRECTION_AI
    assert by_model["direction"] == DIRECTION_LIFE
    assert by_model["classification_method"] == "model"
    assert by_model["model"]["angle"] == "从普通人的时间成本切入"
    # 事实类维度（热度/时效/价值/可信度）必须一模一样，模型改不动；
    # 受众匹配随方向变、原创空间随二级主题变，都是有依据的连锁，不是随手改分。
    fixed = {"heat", "timeliness", "value", "credibility"}
    for before, after in zip(by_rule["dimensions"], by_model["dimensions"]):
        if before["key"] in fixed:
            assert after["score"] == before["score"], f"{before['key']} 不该被模型改写"
        elif before["key"] == "audience":
            assert after["score"] < before["score"]
    assert by_model["subtopics"] == ["效率工具"]


def test_model_cautions_do_not_replace_fact_based_risk_flags():
    result = radar_scoring.evaluate(
        {"title": "某地发生事故", "url": "https://x.com/a", "directions": [DIRECTION_SOCIETY]},
        [_observation(tier="signal", publisher="toutiao")], now=NOW,
        model_classification={"direction": DIRECTION_SOCIETY, "subtopics": [], "angle": "x",
                              "confidence": 90, "cautions": ["模型提示"]})
    assert any("高风险题材" in flag for flag in result["risk_flags"])
    assert result["model"]["cautions"] == ["模型提示"]


# --------------------------------------------------------------------------
# 排序语义
# --------------------------------------------------------------------------

def test_a_fresh_official_item_outranks_a_stale_signal():
    fresh = radar_scoring.evaluate(
        {"title": "新模型发布", "summary": "官方发布公告", "url": "https://a.com/1",
         "published_at": (NOW - timedelta(hours=1)).isoformat(),
         "directions": [DIRECTION_AI]},
        [_observation(tier="official", heat=0.9)], now=NOW)
    stale = radar_scoring.evaluate(
        {"title": "旧热搜词条", "url": "https://b.com/2",
         "published_at": (NOW - timedelta(days=12)).isoformat(),
         "directions": [DIRECTION_LIFE]},
        [_observation(tier="signal", publisher="weibo_x", rank=40)], now=NOW)
    assert fresh["total"] > stale["total"]
