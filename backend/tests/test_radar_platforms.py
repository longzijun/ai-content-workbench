"""三平台匹配与推荐。

钉住的红线：

1. **推荐不等于批准** —— `confirmed_required` 恒 True，模块不写库、不建选题。
2. **推荐的形式必须是生成端真实存在的** —— 推荐落地不了等于没推荐。
3. **题材与形式不能违和** —— 新闻题材在公众号上不能推荐成步骤教程。
"""
import pytest

from backend.app import formats, platform_rules, radar_platforms
from backend.app.trend_sources import (DIRECTION_AI, DIRECTION_LIFE,
                                       DIRECTION_SOCIETY, DIRECTION_TECH)


def _recommend(title, summary, url, direction, subtopics=(), tiers=("official",)):
    observations = [{"tier": tier, "source_id": f"s{index}", "publisher_id": f"p{index}"}
                    for index, tier in enumerate(tiers)]
    return radar_platforms.recommend(
        {"title": title, "summary": summary, "url": url},
        {"direction": direction, "subtopics": list(subtopics)}, observations)


# --------------------------------------------------------------------------
# 结构与契约
# --------------------------------------------------------------------------

def test_all_three_platforms_are_returned_sorted_by_fit():
    result = _recommend("某模型发布", "官方公布更新清单", "https://openai.com/x", DIRECTION_AI)
    assert {item["id"] for item in result["platforms"]} == set(radar_platforms.PLATFORM_IDS)
    fits = [item["fit"] for item in result["platforms"]]
    assert fits == sorted(fits, reverse=True)
    assert result["primary"] == result["platforms"][0]["id"]


def test_materially_different_platforms_are_not_all_the_same_fit():
    """三平台匹配度必须真的有差异，否则"差异化推荐"就是一句空话。"""
    result = _recommend("某地发生地震", "当地已启动应急响应。",
                        "https://www.chinanews.com.cn/1.html", DIRECTION_SOCIETY, ["公共事件"])
    fits = [item["fit"] for item in result["platforms"]]
    assert max(fits) - min(fits) >= 0.15


def test_every_platform_carries_a_basis_and_a_material_list():
    result = _recommend("某模型发布", "官方公布更新清单", "https://openai.com/x", DIRECTION_AI)
    for item in result["platforms"]:
        assert item["basis"], f"{item['name']} 必须给出匹配依据"
        assert 0.0 <= item["fit"] <= 1.0
        assert item["materials"], f"{item['name']} 必须说明要准备什么"
        assert item["title_direction"], f"{item['name']} 必须给标题方向"


def test_the_suggested_form_exists_in_the_generation_catalog():
    """推荐的形式必须在生成端真实存在，否则推荐落不了地。"""
    ids = set(formats.CONTENT_FORM_IDS)
    for direction in (DIRECTION_AI, DIRECTION_TECH, DIRECTION_SOCIETY, DIRECTION_LIFE):
        result = _recommend("某条线索", "摘要内容摘要内容", "https://x.com/a", direction)
        for item in result["platforms"]:
            assert item["form"] in ids


def test_a_recommendation_is_never_an_approval():
    result = _recommend("某条线索", "摘要内容", "https://x.com/a", DIRECTION_AI)
    assert result["confirmed_required"] is True


def test_title_limits_match_the_platform_rules():
    result = _recommend("某条线索", "摘要内容", "https://x.com/a", DIRECTION_AI)
    for item in result["platforms"]:
        assert item["title_limit"] == platform_rules.TITLE_LIMITS[item["id"]]


# --------------------------------------------------------------------------
# 方向 → 平台
# --------------------------------------------------------------------------

def test_an_official_ai_release_goes_to_wechat():
    result = _recommend("Ollama 发布新版本，支持更多量化格式",
                        "官方博客公布更新清单，包含推理性能改进与显存优化。",
                        "https://ollama.com/blog/x", DIRECTION_AI, ["模型发布"])
    assert result["primary"] == "wechat"
    assert result["confidence"] in ("medium", "high")


def test_a_society_hot_board_item_goes_to_toutiao():
    result = _recommend("某地发生地震，暂无人员伤亡报告", "当地已启动应急响应。",
                        "https://newsnow.busiyi.world/api/s?id=toutiao&latest",
                        DIRECTION_SOCIETY, ["公共事件"], tiers=("signal",))
    assert result["primary"] == "toutiao"


def test_a_life_tool_item_goes_to_xiaohongshu():
    result = _recommend("这 5 个免费工具让我的效率翻倍",
                        "亲测好用的效率工具清单，附安装配置步骤。",
                        "https://sspai.com/post/1", DIRECTION_LIFE, ["效率工具"],
                        tiers=("interpretation",))
    assert result["primary"] == "xiaohongshu"


# --------------------------------------------------------------------------
# 形式不能与题材违和
# --------------------------------------------------------------------------

def test_a_news_item_is_not_pushed_into_a_tutorial_on_wechat():
    """新闻题材在公众号上被推荐成步骤教程，是推荐与生成端判断脱节的典型症状。"""
    result = _recommend("Ollama 发布新版本，官方公布更新清单",
                        "本次更新包含推理性能改进。", "https://ollama.com/blog/x",
                        DIRECTION_AI, ["模型发布"])
    wechat = next(item for item in result["platforms"] if item["id"] == "wechat")
    assert wechat["form"] != "tutorial" or wechat["fit"] < 0.5


def test_a_public_event_on_a_depth_platform_becomes_an_opinion_piece():
    result = _recommend("某地发生地震", "当地已启动应急响应。",
                        "https://www.chinanews.com.cn/1.html",
                        DIRECTION_SOCIETY, ["公共事件"])
    wechat = next(item for item in result["platforms"] if item["id"] == "wechat")
    assert wechat["form"] == "opinion"


def test_a_troubleshooting_item_becomes_a_tutorial():
    result = _recommend("安装 ComfyUI 报错怎么解决", "给出排查步骤。",
                        "https://github.com/x", DIRECTION_AI, ["基础设施"])
    wechat = next(item for item in result["platforms"] if item["id"] == "wechat")
    assert wechat["form"] == "tutorial"


def test_form_reasons_are_always_explained():
    for title, summary in (("某模型发布", "官方公布"), ("如何安装配置某工具", "步骤说明"),
                           ("两个模型对比评测", "实测对比"), ("一个没有任何关键词的标题", "无")):
        result = _recommend(title, summary, "https://x.com/a", DIRECTION_AI)
        for item in result["platforms"]:
            assert item["form_reason"], f"{item['name']} 必须说明为什么选这个形式"


# --------------------------------------------------------------------------
# 素材与难度
# --------------------------------------------------------------------------

def test_a_missing_original_source_is_listed_as_a_material_gap():
    result = _recommend("某热搜词条", "摘要", "https://newsnow.busiyi.world/api/s?id=baidu&latest",
                        DIRECTION_SOCIETY, tiers=("signal",))
    all_materials = " ".join(item["materials"][0] for item in result["platforms"])
    assert "原始报道" in all_materials


def test_xiaohongshu_states_its_image_and_length_requirements():
    result = _recommend("某工具实测", "亲测好用的工具。", "https://sspai.com/1",
                        DIRECTION_LIFE, tiers=("interpretation",))
    notes = next(item for item in result["platforms"] if item["id"] == "xiaohongshu")
    joined = " ".join(notes["materials"])
    assert "配图" in joined and "1000 字" in joined


def test_difficulty_is_explained_and_within_range():
    result = _recommend("某条线索", "摘要内容摘要内容", "https://x.com/a", DIRECTION_AI)
    for item in result["platforms"]:
        assert item["difficulty"]["level"] in ("low", "medium", "high")
        assert 0.0 <= item["difficulty"]["value"] <= 1.0
        assert item["difficulty"]["basis"]


def test_thin_material_raises_difficulty_for_the_same_form():
    """同一形式下，材料越薄越难写。

    注意跨形式不能这样比：材料薄时推荐会主动换成更省力的形式，整体难度
    反而更低——那是推荐该有的行为，不是缺陷。
    """
    thin = radar_platforms._difficulty("wechat", "tutorial", 0.35, 20)
    rich = radar_platforms._difficulty("wechat", "tutorial", 1.0, 200)
    assert thin["value"] > rich["value"]
    assert thin["basis"] != rich["basis"]


def test_thin_material_pushes_the_recommendation_to_a_lighter_form():
    """材料不足时不该硬推深度教程。"""
    result = _recommend("某条线索", "短", "https://newsnow.busiyi.world/api/s?id=zhihu&latest",
                        DIRECTION_AI, tiers=("signal",))
    wechat = next(item for item in result["platforms"] if item["id"] == "wechat")
    assert wechat["form"] == "opinion"


# --------------------------------------------------------------------------
# 题材歧义
# --------------------------------------------------------------------------

def test_ambiguous_kinds_accumulate_weights_instead_of_falling_back():
    """「两个模型对比评测」同时命中 news 与 review，累加后应偏向观点短评。"""
    kinds = radar_platforms._infer_kinds("两个模型对比评测", "实测对比结果")
    assert len(kinds) >= 2
    result = _recommend("两个模型对比评测", "实测对比结果。", "https://ifanr.com/1", DIRECTION_AI)
    wechat = next(item for item in result["platforms"] if item["id"] == "wechat")
    assert wechat["form"] == "opinion"


def test_unknown_kind_still_yields_a_valid_form():
    result = _recommend("一个完全不含任何关键词的标题", "同样平淡的摘要",
                        "https://x.com/a", DIRECTION_LIFE)
    for item in result["platforms"]:
        assert item["form"] in formats.CONTENT_FORM_IDS


@pytest.mark.parametrize("direction", [DIRECTION_AI, DIRECTION_TECH, DIRECTION_SOCIETY, DIRECTION_LIFE])
def test_every_direction_produces_a_complete_recommendation(direction):
    result = _recommend("某条线索", "摘要内容摘要内容摘要内容", "https://x.com/a", direction)
    assert result["primary"] in radar_platforms.PLATFORM_IDS
    assert result["reason"]
    assert len(result["platforms"]) == 3
