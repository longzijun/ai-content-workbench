"""Format registry invariants; no database, network, or generation access."""
import inspect
import json

import pytest

from backend.app import formats, image_assets, platform_rules, richtext
from backend.app.platform_rules import NAMES, TITLE_LIMITS, expected_format, work_errors
from backend.app.schemas import Payload, Work


def work(platform="wechat", **extra):
    return {
        "platform": platform,
        "format": expected_format(platform),
        "title": "一个具体问题",
        "body": "先阅读原文并核对日期。",
        "tags": [],
        "image_prompts": [],
        "source_urls": [],
        **extra,
    }


def enumerable(platform, count, form):
    return work(platform=platform, content_form=form, source_urls=["https://example.com/a"] * count,
                body="\n".join(f"- 要点{i}" for i in range(count)))


# --------------------------------------------------------------------------
# D.1 / D.2  A 轴：与 platform_rules 双向一致 + 纯提取零副作用
# --------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["wechat", "toutiao", "xiaohongshu"])
def test_axis_a_is_derived_from_platform_rules_both_directions(platform):
    """整个 V2 的地基。双向断言：只测单向捕获不了两侧同时反转。"""
    assert formats.axis_a_for(platform) == expected_format(platform)
    # 正向：该平台属于这个形态
    assert platform in formats.axis_a_platforms(formats.axis_a_for(platform))
    # 反向：把 TITLE_LIMITS 里每个平台都过一遍，谁映射到这个形态就必被列出，
    # 少一个就是派生逻辑与权威脱钩了
    derived = {p for p in TITLE_LIMITS if expected_format(p) == expected_format(platform)}
    assert set(formats.axis_a_platforms(expected_format(platform))) == derived


def test_axis_a_platforms_partition_every_platform():
    """每个平台恰好属于一个渠道形态，无重叠无遗漏。"""
    seen = []
    for format_id in formats.format_catalog()["axis_a"]["ids"]:
        seen.extend(formats.axis_a_platforms(format_id))
    from backend.app.platform_registry import PLATFORM_IDS
    assert sorted(seen) == sorted(PLATFORM_IDS)
    assert len(seen) == len(set(seen)), "同一平台不能同时归属两个渠道形态"


@pytest.mark.parametrize("platform", ["wechat", "toutiao", "xiaohongshu"])
def test_expected_format_extraction_changed_no_behavior(platform):
    """纯提取 expected_format() 不得改变 work_errors 的返回值或文案。"""
    correct = expected_format(platform)
    other = "note" if correct == "article" else "article"
    assert work_errors(work(platform=platform, format=correct)) == []
    errors = work_errors(work(platform=platform, format=other))
    assert errors == [f'{NAMES[platform]}当前仅支持 {correct} 草稿']
    # 未设置 format 时不报错，与改动前一致
    payload = work(platform=platform)
    payload.pop("format")
    assert work_errors(payload) == []


def test_expected_format_rejects_unknown_platform():
    with pytest.raises(ValueError, match="不支持的发布平台"):
        expected_format("weibo")


def test_axis_a_for_rejects_unknown_platform():
    with pytest.raises(ValueError, match="不支持的发布平台"):
        formats.axis_a_for("weibo")


# --------------------------------------------------------------------------
# D.3 / D.4  正交性
# --------------------------------------------------------------------------

def test_axis_a_and_axis_b_are_independent():
    """B 轴取遍全部取值时，A 轴判定逐字节不变。"""
    baseline_a = {p: formats.axis_a_for(p) for p in TITLE_LIMITS}
    baseline_errors = {p: work_errors(work(platform=p)) for p in TITLE_LIMITS}
    for form in formats.CONTENT_FORM_IDS + ("micro", "", None):
        assert {p: formats.axis_a_for(p) for p in TITLE_LIMITS} == baseline_a
        assert {p: work_errors(work(platform=p, content_form=form))
                for p in TITLE_LIMITS} == baseline_errors


def test_axis_a_changes_do_not_touch_axis_b():
    """A 轴取遍各平台合法值时，B 轴条目逐字节不变。"""
    baseline = {form: formats.get_content_form(form) for form in formats.CONTENT_FORM_IDS}
    for platform in TITLE_LIMITS:
        formats.axis_a_for(platform)
        formats.axis_a_platforms(expected_format(platform))
    assert {form: formats.get_content_form(form) for form in formats.CONTENT_FORM_IDS} == baseline


def test_content_form_never_enters_platform_rule_errors():
    """决策 2 的最强表述：B 轴改值，work_errors 返回值必须完全相同。"""
    baseline = work_errors(work(content_form="tutorial"))
    for form in ("micro", "", "  ", None, "case", "123"):
        assert work_errors(work(content_form=form)) == baseline
    # 换个平台也一样
    for platform in TITLE_LIMITS:
        one = work_errors(work(platform=platform, content_form="tutorial"))
        for form in ("micro", "opinion", None):
            assert work_errors(work(platform=platform, content_form=form)) == one


def test_work_errors_error_text_is_unchanged_for_every_violation():
    """纯提取不得改动任何既有错误文案。"""
    assert work_errors({"platform": "weibo"}) == ["不支持的发布平台"]
    assert work_errors(work(platform="wechat", title="")) == ["公众号标题需为 1 到 32 字，请修改版本"]
    assert work_errors(work(platform="xiaohongshu", body="字" * 1001)) == [
        "小红书正文超过 1000 字限制，请修改版本；不会自动截断"
    ]


# --------------------------------------------------------------------------
# D.5  CONTENT_FORM_IDS 的 schema 就绪性
# --------------------------------------------------------------------------

def test_content_form_ids_are_sorted_unique_and_schema_ready():
    ids = formats.CONTENT_FORM_IDS
    assert list(ids) == sorted(ids)
    assert len(ids) == len(set(ids))
    assert all(isinstance(value, str) and value and value == value.lower() for value in ids)
    assert all(value.strip() == value and " " not in value for value in ids)
    # V2-04 直接把它当 enum 用
    schema = {"type": "string", "enum": list(ids)}
    assert json.loads(json.dumps(schema)) == schema


def test_content_form_ids_are_five_as_specified():
    assert formats.CONTENT_FORM_IDS == ("brief", "case", "checklist", "opinion", "tutorial")


# --------------------------------------------------------------------------
# D.6 / D.7  条目完整性与跨轴一致性
# --------------------------------------------------------------------------

@pytest.mark.parametrize("form", formats.CONTENT_FORM_IDS)
def test_every_content_form_entry_is_complete(form):
    entry = formats.get_content_form(form)
    assert entry["id"] == form
    assert entry["label"].strip() and entry["summary"].strip()
    assert entry["complexity"] in {"low", "medium", "high"}
    window = entry["title_window"]
    assert isinstance(window, tuple) and len(window) == 2
    assert all(type(v) is int for v in window)
    assert 0 < window[0] < window[1]
    assert type(entry["sources_min"]) is int and entry["sources_min"] >= 0
    assert type(entry["items_min"]) is int and entry["items_min"] >= 0
    assert entry["boost"] and set(entry["boost"]) <= set(TITLE_LIMITS)


@pytest.mark.parametrize("form", formats.CONTENT_FORM_IDS)
def test_title_window_never_contradicts_platform_title_limit(form):
    """B 轴建议不能比 A 轴硬上限更宽，否则两轴从正交变成打架。

    只对 boost 里的平台断言 —— B 轴窗口是「在该形式被推荐的平台上」的
    建议值。日后把 xiaohongshu 加进某个 boost 时，本测试会立刻变红，
    提示必须为该平台另设更窄的窗口。
    """
    entry = formats.get_content_form(form)
    high = entry["title_window"][1]
    for platform in entry["boost"]:
        assert high <= TITLE_LIMITS[platform][1], (
            f"{form} 建议标题上限 {high} 超过了其推荐平台 {platform} "
            f"的硬上限 {TITLE_LIMITS[platform][1]}"
        )


def test_form_recommended_to_xiaohongshu_fits_its_title_limit():
    """小红书 20 字是最紧的上限，任何推给它的形式窗口都必须够窄。"""
    for form in formats.CONTENT_FORM_IDS:
        if "xiaohongshu" in formats.get_content_form(form)["boost"]:
            assert formats.get_content_form(form)["title_window"][1] <= TITLE_LIMITS["xiaohongshu"][1]


def test_case_form_requires_more_sources_than_the_others():
    """case 最易诱导编造经历，来源门槛必须最高。"""
    thresholds = {form: formats.get_content_form(form)["sources_min"]
                  for form in formats.CONTENT_FORM_IDS}
    assert thresholds["case"] == max(thresholds.values())
    assert thresholds["case"] >= 2


def test_only_opinion_may_have_zero_items():
    """items_min == 0 表示「不适用」（散文体），不是「零要点也算合规」。"""
    zeros = [form for form in formats.CONTENT_FORM_IDS
             if formats.get_content_form(form)["items_min"] == 0]
    assert zeros == ["opinion"]


# --------------------------------------------------------------------------
# D.8 / D.9 / D.10  隔离性
# --------------------------------------------------------------------------

def test_get_content_form_returns_deepcopy_and_rejects_unknown():
    first = formats.get_content_form("tutorial")
    first["boost"] = ("mutated",)
    first["label"] = "改过"
    assert formats.get_content_form("tutorial")["boost"] == ("wechat", "toutiao")
    assert formats.get_content_form("tutorial")["label"] == "步骤教程"
    for bad in ("micro", "video", "", "  ", None, 123):
        with pytest.raises(ValueError, match="不支持的叙事形式"):
            formats.get_content_form(bad)


def test_axis_a_entry_returns_deepcopy_and_rejects_unknown():
    first = formats.axis_a_entry("article")
    first["platforms"] = ("mutated",)
    first["platform_names"].append("假的")
    assert formats.axis_a_entry("article")["platforms"] == ("wechat", "toutiao")
    assert "假的" not in formats.axis_a_entry("article")["platform_names"]
    with pytest.raises(ValueError, match="不支持的渠道形态"):
        formats.axis_a_entry("micro")


def test_registry_is_private_and_getters_cover_it():
    """全私有不变式：外部只能经 getter 读，不能拿到内部字典。"""
    source = inspect.getsource(formats)
    assert "_AXIS_A = {" in source and "_CONTENT_FORMS = {" in source, "两张注册表都应全私有"
    public = {name for name in dir(formats) if not name.startswith("_") and name != "annotations"}
    expected = {"NAMES", "TITLE_LIMITS", "CONTENT_FORM_IDS", "deepcopy", "expected_format",
                "axis_a_for", "axis_a_platforms", "axis_a_entry",
                "get_content_form", "normalize_content_form", "content_form_warnings",
                "format_catalog"}
    assert public == expected


def test_format_catalog_is_serializable_and_isolated():
    catalog = formats.format_catalog()
    assert json.dumps(catalog, ensure_ascii=False)
    # tuple 序列化后变 list，回读结果应只在容器类型上不同
    round_tripped = json.loads(json.dumps(catalog, ensure_ascii=False))
    assert round_tripped["axis_b"]["ids"] == list(formats.CONTENT_FORM_IDS)
    assert round_tripped["axis_a"]["ids"] == ["article", "note", "video_script"]
    catalog["axis_b"]["items"][0]["label"] = "改过"
    catalog["axis_a"]["items"][0]["platforms"] = ("mutated",)
    assert formats.format_catalog()["axis_b"]["items"][0]["label"] == "要点速报"
    assert formats.format_catalog()["axis_a"]["items"][0]["platforms"] == ("wechat", "toutiao")


def test_format_catalog_labels_are_locked_for_the_frontend():
    """V2-12 的消歧词汇在此固化：渠道形态 / 叙事形式。"""
    catalog = formats.format_catalog()
    assert catalog["axis_a"]["label"] == "渠道形态"
    assert catalog["axis_a"]["field"] == "format"
    assert catalog["axis_b"]["label"] == "叙事形式"
    assert catalog["axis_b"]["field"] == "content_form"
    assert catalog["axis_b"]["ids"] == list(formats.CONTENT_FORM_IDS)


def test_axis_a_entry_platform_names_come_from_platform_rules():
    entry = formats.axis_a_entry("note")
    assert entry["platforms"] == ("xiaohongshu",)
    assert entry["platform_names"] == [NAMES["xiaohongshu"]]
    assert entry["label"] == "笔记", "形态名不应复述 platform_rules 的平台名"


# --------------------------------------------------------------------------
# D.11 / D.12  normalize
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["tutorial", " Tutorial ", "TUTORIAL", "\ttutorial\n", "  Brief  "])
def test_normalize_content_form_is_case_and_whitespace_insensitive(raw):
    assert formats.normalize_content_form(raw) == raw.strip().casefold()


@pytest.mark.parametrize("raw", [None, "", "   ", "micro", "video", "article", "note",
                                 123, [], {}, True, "步骤教程"])
def test_normalize_content_form_maps_unset_and_unknown_to_empty(raw):
    assert formats.normalize_content_form(raw) == ""


def test_normalize_never_raises_on_any_input():
    for raw in (object(), b"tutorial", 3.5, ["tutorial"], {"content_form": "tutorial"}):
        assert formats.normalize_content_form(raw) == ""


# --------------------------------------------------------------------------
# D.13 / D.14 / D.15  warnings
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"content_form": "micro", "platform": "weibo"},
    {"content_form": "case", "platform": "wechat", "title": "", "body": ""},
    {"content_form": "checklist", "platform": "xiaohongshu", "body": "字" * 5000},
    {"content_form": None},
    {"content_form": ""},
])
def test_content_form_warnings_never_raises_and_returns_plain_strings(payload):
    result = formats.content_form_warnings(payload)
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    assert not any(isinstance(item, dict) for item in result), "只出纯字符串，不出 findings 结构"


def test_content_form_warnings_requires_a_dict():
    for bad in (None, "work", 123, []):
        with pytest.raises(ValueError, match="稿件必须是字典"):
            formats.content_form_warnings(bad)


@pytest.mark.parametrize("form", formats.CONTENT_FORM_IDS)
def test_compliant_work_gets_no_warnings(form):
    platform = formats.get_content_form(form)["boost"][0]
    assert formats.content_form_warnings(enumerable(platform, 6, form)) == []


def test_unknown_content_form_is_flagged_with_the_available_options():
    result = formats.content_form_warnings(work(content_form="micro"))
    assert len(result) == 1
    assert "micro" in result[0]
    for form in formats.CONTENT_FORM_IDS:
        assert form in result[0], "提示里应列出全部可用形式"
    assert "不在注册表内" in result[0]


def test_unknown_content_form_does_not_fall_through_to_other_warnings():
    """未知值就只报一条，不要叠加上来源/要点告警。"""
    assert len(formats.content_form_warnings(work(content_form="micro", source_urls=[]))) == 1


def test_blank_content_form_is_silent_not_flagged():
    """空串是「未指定」，不是非法值，不该出告警。"""
    assert formats.content_form_warnings(work(content_form="")) == []
    assert formats.content_form_warnings(work(content_form="   ")) == []
    assert formats.content_form_warnings(work()) == []
    assert formats.content_form_warnings({"content_form": None}) == []


def test_case_form_flags_insufficient_sources():
    result = formats.content_form_warnings(work(content_form="case", source_urls=[]))
    assert any("案例拆解" in item and "2 条" in item for item in result)
    # 补到 2 条来源后，来源告警消失
    satisfied = formats.content_form_warnings(
        work(content_form="case", source_urls=["https://example.com/a", "https://example.com/b"],
             body="- 一\n- 二\n- 三"))
    assert not any("案例拆解" in item and "来源" in item for item in satisfied)


def test_checklist_form_flags_too_few_bullets():
    result = formats.content_form_warnings(
        enumerable("xiaohongshu", 2, "checklist"))
    assert any("清单速查" in item and "5 个并列要点" in item for item in result)


def test_opinion_form_never_flags_bullets():
    """items_min == 0 表示不适用，不该要求并列要点。"""
    assert formats.content_form_warnings(
        work(platform="toutiao", content_form="opinion", source_urls=["https://example.com/a"],
             body="一段没有列表的连续文字。")) == []


def test_content_form_warnings_leave_platform_validation_alone():
    """未知平台返回空。formats 不做第二个平台权威。"""
    assert formats.content_form_warnings({"platform": "weibo", "content_form": "case"}) == []


def test_content_form_warnings_uses_raw_value_for_the_unknown_check():
    """用归一后的结果会丢掉「填了但填错」的信号。"""
    assert formats.content_form_warnings(work(content_form="  micro  "))[0].count("micro") == 1
    assert formats.normalize_content_form("  micro  ") == ""


def test_warning_text_truncates_a_long_bad_value():
    long_value = "x" * 500
    result = formats.content_form_warnings(work(content_form=long_value))
    assert len(result) == 1
    assert "x" * 500 not in result[0], "超长值必须截断，不能整段塞进提示"


# --------------------------------------------------------------------------
# D.16 / D.17 / D.18  schema 行为
# --------------------------------------------------------------------------

def test_work_schema_defaults_content_form_to_empty_string():
    legacy = {"platform": "wechat", "format": "article", "title": "标题", "body": "正文",
              "tags": [], "image_prompts": [], "source_urls": []}
    assert "content_form" not in legacy
    parsed = Work.model_validate(legacy)
    assert parsed.content_form == ""
    assert parsed.model_dump()["content_form"] == ""


def test_work_schema_still_forbids_unknown_fields():
    """extra='forbid' 未被削弱：content_form 被接受，别的未知键仍 422。"""
    assert Work.model_validate({**work(), "content_form": "tutorial"}).content_form == "tutorial"
    with pytest.raises(ValueError, match="Extra inputs are not permitted|extra_forbidden"):
        Work.model_validate({**work(), "nope": "x"})
    with pytest.raises(ValueError):
        Payload.model_validate({"strategy": "s", "works": [{**work(), "other": 1}]})


def test_work_schema_has_no_literal_constraint_on_content_form():
    """native 路径不强制 schema，模型可能幻觉出该键；schema 层不硬拦。"""
    for value in ("micro", "步骤教程", "x" * 100):
        assert Work.model_validate({**work(), "content_form": value}).content_form == value
    with pytest.raises(ValueError):
        Work.model_validate({**work(), "content_form": "x" * 101})


def test_work_schema_accepts_content_form_from_a_full_payload():
    payload = Payload.model_validate({"strategy": "依据资料写作", "works": [
        {**work(platform="wechat", content_form="tutorial"),
         "content_form": "tutorial"},
        {**work(platform="xiaohongshu"), "content_form": "checklist"},
    ]})
    assert [item.content_form for item in payload.works] == ["tutorial", "checklist"]


# --------------------------------------------------------------------------
# D.19  hash 语义
# --------------------------------------------------------------------------

def test_legacy_payload_keeps_its_frozen_hash():
    """service.py 的两处校验都从 DB 已存字符串重算，不重过 Payload。"""
    from backend.app.service import digest, encode

    legacy = {"strategy": "依据资料写作", "works": [
        {"platform": "wechat", "format": "article", "title": "标题", "body": "正文",
         "tags": [], "image_prompts": [], "source_urls": []}]}
    frozen = encode(legacy)
    assert "content_form" not in frozen
    assert digest(json.loads(frozen)) == digest(legacy)
    assert Payload.model_validate(legacy).works[0].content_form == ""
    # 新版本会多一个空串键，hash 因此不同 —— 这是预期，不是回归
    refreshed = Payload.model_validate(legacy).model_dump()
    assert refreshed["works"][0]["content_form"] == ""
    assert digest(refreshed) != digest(legacy)


# --------------------------------------------------------------------------
# D.20  图片资产哈希不受影响
# --------------------------------------------------------------------------

def test_content_form_does_not_enter_image_asset_hash():
    without = work(platform="wechat", image_prompts=["中性示意图"])
    with_form = {**without, "content_form": "tutorial"}
    assert image_assets.work_asset_hash(without) == image_assets.work_asset_hash(with_form)
    # 对照：image_tasks 进白名单，说明白名单本身是有效的
    assert image_assets.work_asset_hash(without) != image_assets.work_asset_hash(
        {**without, "image_tasks": [{"prompt": "示意图", "aspect": "16:9"}]})


# --------------------------------------------------------------------------
# D.21 / D.22  命名冲突与迁移一致性
# --------------------------------------------------------------------------

def test_richtext_stays_single_axis():
    """richtext.render 只按 platform 分派，不存在平台×形式二维判断。"""
    assert list(inspect.signature(richtext.render).parameters) == ["markdown", "platform"]
    assert list(inspect.signature(richtext.expected_visible_text).parameters) == ["markdown", "platform"]
    assert list(richtext.PLATFORMS) == list(TITLE_LIMITS)


def test_short_post_is_not_in_either_axis():
    """capabilities.py 的 short_post 是未实现通道占位，不属于任何一轴。"""
    catalog = formats.format_catalog()
    everywhere = catalog["axis_a"]["ids"] + catalog["axis_b"]["ids"]
    assert "short_post" not in everywhere
    assert set(formats.axis_a_for(p) for p in TITLE_LIMITS) == {"article", "note"}


def test_registry_vocabulary_matches_layout_templates_column():
    """防 V2-07 另造一套词表。"""
    from backend.app import migrations

    sql = "".join(sql for _, _, sql in migrations.MIGRATIONS if "layout_templates" in sql)
    assert "content_form" in sql
    assert "DEFAULT ''" in sql, "空串语义必须与注册表一致"
    for form in formats.CONTENT_FORM_IDS:
        assert form.replace("_", "").isalnum() and form.isascii() and form.islower()
