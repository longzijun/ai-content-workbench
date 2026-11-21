"""Layout template registry. The point is the boundary with richtext, plus the
three-level fallback that never returns None.
"""
import inspect
import json

import pytest

from backend.app import brand, db, formats, layout_templates, richtext
from backend.app.layout_templates import PROJECT_BASELINE
from backend.app.platform_rules import TITLE_LIMITS, expected_format, work_errors


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(db, "connect", db.connect)
    db.init_db()
    from backend.app import config
    return config.get_settings()


# --------------------------------------------------------------------------
# 职责边界：本模块不产生任何 HTML 或纯文本
# --------------------------------------------------------------------------

def test_template_module_never_renders():
    """模板里出现 to_html/to_text/strip_markup 就是职责越界。"""
    source = inspect.getsource(layout_templates)
    for forbidden in ("to_html(", "to_text(", "strip_markup(", "richtext.render("):
        assert forbidden not in source, f"模板库不得调用 {forbidden}"
    # 但必须调用 expected_visible_text 来测量真实可见文本
    assert "expected_visible_text" in source


def test_docstring_states_the_boundary_with_richtext():
    doc = layout_templates.__doc__
    assert "richtext" in doc
    assert "适配层" in doc
    assert "版式骨架" in doc


def test_measure_body_uses_visible_text_not_markdown():
    """测 Markdown 原文会系统性偏大，导致短正文平台误报溢出。"""
    template = {"platform": "wechat", "media_spec": {"body_ceiling": None}}
    markdown = "**粗体** 和 `代码` 与 # 标题"
    result = layout_templates.measure_body(template, markdown)
    assert result["visible_chars"] == len(richtext.expected_visible_text(markdown, "wechat"))
    assert result["visible_chars"] < result["markdown_chars"], "加粗与代码标记占字符但不出现在画面上"


def test_measure_body_detects_real_overflow_on_xiaohongshu(world):
    template = next(t for t in layout_templates.catalog() if t["platform"] == "xiaohongshu")
    assert template["media_spec"]["body_ceiling"] == 1000
    assert layout_templates.measure_body(template, "字" * 1200)["over"] is True
    assert layout_templates.measure_body(template, "字" * 500)["over"] is False


# --------------------------------------------------------------------------
# 三级回退：精确 → 通用 → 抛错，绝不返回 None
# --------------------------------------------------------------------------

def test_resolve_hits_the_exact_template(world):
    template = layout_templates.resolve(brand.DEFAULT_BRAND_ID, "wechat", "article", "case")
    assert template["content_form"] == "case"
    assert template["platform"] == "wechat"


def test_resolve_falls_back_to_the_general_template(world):
    """该平台没有这个叙事形式的专属模板时，回退到通用模板。"""
    template = layout_templates.resolve(brand.DEFAULT_BRAND_ID, "wechat", "article", "brief")
    assert template["content_form"] == "", "brief 在公众号没有专属行，应回退到通用"
    assert template["platform"] == "wechat"


def test_resolve_raises_when_even_the_general_template_is_missing(world):
    with pytest.raises(LookupError, match="排版模板缺失"):
        layout_templates.resolve("brand-does-not-exist", "wechat", "article", "")


def test_resolve_never_returns_none(world):
    """模板缺失必须显式失败，不能让下游走「没有模板就跳过检查」。"""
    for platform in TITLE_LIMITS:
        result = layout_templates.resolve(brand.DEFAULT_BRAND_ID, platform,
                                          expected_format(platform), "")
        assert result is not None


def test_resolve_normalises_an_unknown_content_form(world):
    template = layout_templates.resolve(brand.DEFAULT_BRAND_ID, "wechat", "article", "micro")
    assert template["content_form"] == "", "未知形式归一后落到通用模板，不是报错"


# --------------------------------------------------------------------------
# 与 richtext / formats 的一致性
# --------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TITLE_LIMITS))
def test_template_render_matches_richtext_dispatch(platform):
    """模板的 render 字段必须与 richtext.render 的实际分派一致。"""
    assert layout_templates.render_mode_for(platform) == \
        richtext.render("测试正文", platform)[0]


@pytest.mark.parametrize("platform", sorted(TITLE_LIMITS))
def test_every_seed_row_render_matches_richtext(platform):
    for row in layout_templates.build_seed():
        if row["platform"] == platform:
            assert row["render"] == richtext.render("测试正文", platform)[0]


def test_seed_rows_match_the_axis_a_authority():
    for row in layout_templates.build_seed():
        assert row["format_id"] == expected_format(row["platform"])


def test_seed_covers_every_platform_with_a_general_and_a_specific_row():
    rows = layout_templates.build_seed()
    for platform in TITLE_LIMITS:
        mine = [row for row in rows if row["platform"] == platform]
        assert len(mine) == 2, f"{platform} 应有通用行与专属行各一条"
        assert {row["content_form"] for row in mine} == {"", layout_templates.recommended_content_form(platform)}


def test_recommended_content_form_is_stable_and_a_known_id():
    for platform in TITLE_LIMITS:
        recommended = layout_templates.recommended_content_form(platform)
        assert recommended in formats.CONTENT_FORM_IDS
        assert platform in formats.get_content_form(recommended)["boost"]


def test_title_window_is_copied_from_the_registry_not_repeated():
    """标题窗口必须等于 formats 注册表里的值，不在此另说一套。"""
    for row in layout_templates.build_seed():
        if not row["content_form"]:
            assert row["title_style"]["window"] is None
            continue
        assert row["title_style"]["window"] == \
            list(formats.get_content_form(row["content_form"])["title_window"])


# --------------------------------------------------------------------------
# 正文上限：从 platform_rules 探测而非复述
# --------------------------------------------------------------------------

def test_body_ceiling_is_probed_from_platform_rules(monkeypatch):
    """给 platform_rules 换上限，本模块必须自动跟随。"""
    monkeypatch.setattr(layout_templates, "_CEILING_CACHE", {})
    real = work_errors({"platform": "xiaohongshu", "format": "note",
                        "title": "标题", "body": "字" * 1001})
    assert any("正文超过" in item for item in real), "前置条件：platform_rules 确实在拦"
    assert layout_templates.body_ceiling_for("xiaohongshu") == 1000

    def shortened(work):
        if work.get("platform") == "xiaohongshu" and len(work.get("body", "")) > 50:
            return ["小红书正文超过 50 字限制，请修改版本；不会自动截断"]
        return work_errors(work)

    monkeypatch.setattr(layout_templates, "work_errors", shortened)
    monkeypatch.setattr(layout_templates, "_CEILING_CACHE", {})
    assert layout_templates.body_ceiling_for("xiaohongshu") == 50, \
        "上限改了本模块必须跟随，不能出现两套数字"


def test_platforms_without_a_public_body_ceiling_report_none():
    for platform in ("wechat", "toutiao"):
        assert layout_templates.body_ceiling_for(platform) is None


def test_ceiling_agrees_with_platform_rules_behaviour():
    """模板里的上限必须与 platform_rules 的实际拦截点一致。"""
    ceiling = layout_templates.body_ceiling_for("xiaohongshu")
    probe = lambda n: {'platform': 'xiaohongshu', 'format': 'note',
                       'title': '标题', 'body': '字' * n}
    assert work_errors(probe(ceiling)) == []
    assert any('正文超过' in item for item in work_errors(probe(ceiling + 1)))


# --------------------------------------------------------------------------
# provenance：项目基线不得被当成平台规格
# --------------------------------------------------------------------------

def test_media_spec_always_declares_itself_a_project_baseline():
    for row in layout_templates.build_seed():
        assert row["media_spec"]["provenance"] == PROJECT_BASELINE
        assert PROJECT_BASELINE in row["provenance"]


def test_seed_provenance_names_both_kinds_of_source():
    for row in layout_templates.build_seed():
        assert PROJECT_BASELINE in row["provenance"]
        assert "platform_rules" in row["provenance"]


def test_no_cover_ratio_is_claimed_as_platform_official():
    for row in layout_templates.build_seed():
        spec = row["media_spec"]
        assert spec["provenance"] == PROJECT_BASELINE, "画幅比例是项目定的，不是平台公布的"
        assert "不是平台公开规格" in spec["note"]


def test_cover_ratios_differ_per_platform():
    ratios = {row["platform"]: row["media_spec"]["cover_ratio"]
              for row in layout_templates.build_seed()}
    assert len(set(ratios.values())) == len(ratios), "三个平台的封面比例不应完全相同"


# --------------------------------------------------------------------------
# checks 开关
# --------------------------------------------------------------------------

def test_checks_enable_the_audit_items_and_declare_provenance_free():
    for row in layout_templates.build_seed():
        assert row["checks"]["title_overflow"] is True
        assert row["checks"]["body_overflow"] is True
        assert row["checks"]["image_blur"] is True


def test_cover_occlusion_is_off_by_default_because_there_is_no_annotator():
    for row in layout_templates.build_seed():
        if row["platform"] == "xiaohongshu":
            assert row["checks"]["cover_occlusion"] is True, "短正文平台封面值得看"
        else:
            assert row["checks"]["cover_occlusion"] is False


def test_item_count_check_only_for_checklist():
    for row in layout_templates.build_seed():
        expected = row["content_form"] == "checklist"
        assert row["checks"].get("item_count", False) is expected


# --------------------------------------------------------------------------
# 落库与幂等
# --------------------------------------------------------------------------

def test_seed_is_written_by_init_db(world):
    rows = layout_templates.catalog()
    assert rows, "init_db 之后应有模板种子"
    assert len(rows) == 6


def test_seed_is_idempotent(world):
    before = layout_templates.catalog()
    again = layout_templates.seed()
    assert again["written"] == 6
    assert layout_templates.catalog() == before


def test_seed_does_not_duplicate_rows(world):
    layout_templates.seed()
    layout_templates.seed()
    count = db.connect().execute("SELECT COUNT(*) FROM layout_templates").fetchone()[0]
    assert count == 6


def test_reseed_refreshes_content_without_duplicating(world):
    from backend.app import db as database
    with database.transaction() as connection:
        connection.execute("UPDATE layout_templates SET provenance='过期内容'")
    layout_templates.seed()
    rows = layout_templates.catalog()
    assert all(PROJECT_BASELINE in row["provenance"] for row in rows)
    assert len(rows) == 6


def test_catalog_is_sorted_and_serializable(world):
    rows = layout_templates.catalog()
    keys = [(row["platform"], row["content_form"]) for row in rows]
    assert keys == sorted(keys)
    assert json.loads(json.dumps(rows, ensure_ascii=False)) == rows


def test_catalog_returns_copies_not_shared_state(world):
    first = layout_templates.catalog()
    first[0]["skeleton"].append("篡改")
    first[0]["checks"]["title_overflow"] = False
    second = layout_templates.catalog()
    assert "篡改" not in second[0]["skeleton"]
    assert second[0]["checks"]["title_overflow"] is True


def test_lookups_are_independent_of_insertion_order(world):
    """同一查询重复执行返回相同内容，不受其它行影响。"""
    first = layout_templates.resolve(brand.DEFAULT_BRAND_ID, "xiaohongshu", "note", "checklist")
    layout_templates.catalog()
    second = layout_templates.resolve(brand.DEFAULT_BRAND_ID, "xiaohongshu", "note", "checklist")
    assert first == second


# --------------------------------------------------------------------------
# 骨架与红线
# --------------------------------------------------------------------------

def test_skeletons_are_non_empty_ordered_blocks():
    for row in layout_templates.build_seed():
        assert row["skeleton"], "每个模板都要有区块骨架"
        assert len(set(row["skeleton"])) == len(row["skeleton"]), "区块不应重复"
        assert row["skeleton"][0] in ("lead", "claim", "background"), "首块应是引入"


def test_generic_skeleton_is_shared_across_platforms():
    general = {row["platform"]: row["skeleton"] for row in layout_templates.build_seed()
               if row["content_form"] == ""}
    assert len(set(map(tuple, general.values()))) == 1, "通用骨架不该因平台而异"


def test_template_ids_are_deterministic():
    assert [row["id"] for row in layout_templates.build_seed()] == \
           [row["id"] for row in layout_templates.build_seed()]


def test_module_does_not_import_delivery_or_browser_layers():
    source = inspect.getsource(layout_templates)
    code = "\n".join(line for line in source.splitlines()
                      if not line.strip().startswith("#") and '"""' not in line)
    for forbidden in ("browser_", "delivery", "adapters", "artifacts", "httpx"):
        assert forbidden not in code


def test_module_docstring_forbids_autotruncation_and_blocking():
    doc = layout_templates.__doc__
    assert "不自动截断" in doc
    assert "不阻断交付" in doc