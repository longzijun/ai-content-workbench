"""Typeset geometry: probe and render must never drift apart.

The interesting invariant is that `probe_cover_geometry` predicts where the
information band actually lands in the produced PNG. Both call
`measure_band`, so they are the same arithmetic rather than two copies — this
module proves that by scanning real pixels.
"""
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from backend.app import brand, typeset, typeset_engine

# Byte-level baselines for the Windows renderer. The normal case uses a fictional account label.
FROZEN_OUTPUT = {
    "normal": ("2168f3c656276e2252c60334622a7f776171e5cb2c673fe0ac305ffea8f2ff1b",
               {"title": "免费部署本地 AI 的完整路径",
                "account_label": "示例工作室", "page_label": "1/3"}),
    "short": ("a422d1b08e01f49b591622f899429c28e3345c829d00758b5284eb60cdd613f1",
              {"title": "短标题"}),
    "long": ("bb4d4650fe5a075afe312353bce690a2b42458407ab07aaaa4de7cf3a2467fd0",
             {"title": "标" * 120, "account_label": "账号", "page_label": "9/9"}),
}

BASE_COLOR = (40, 60, 50)


def canvas(width=800, height=600, color=BASE_COLOR):
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def scan_first_band_row(image_bytes):
    """Scan the produced PNG for the first row that differs from the base colour."""
    with Image.open(BytesIO(image_bytes)) as opened:
        rgb = opened.convert("RGB")
    pixels = rgb.load()
    for y in range(rgb.height):
        if any(pixels[x, y] != BASE_COLOR for x in range(0, rgb.width, 4)):
            return y
    return None


# --------------------------------------------------------------------------
# 字节兼容：重构不得改变任何产出
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FROZEN_OUTPUT))
def test_typeset_output_is_byte_identical_to_the_windows_baseline(name):
    expected, kwargs = FROZEN_OUTPUT[name]
    produced = typeset.typeset_image(canvas(), **kwargs)
    assert hashlib.sha256(produced).hexdigest() == expected


def test_typeset_engine_default_profile_is_byte_identical_to_typeset():
    """无品牌参数时逐字节相同 —— 这是物理保证，不靠测试凑。"""
    source = canvas()
    kwargs = {"title": "免费部署本地 AI 的完整路径", "account_label": "示例工作室",
              "page_label": "1/3"}
    assert typeset_engine.compose_cover(source, **kwargs) == \
        typeset.typeset_image(source, **kwargs)


def test_typeset_module_gained_no_new_dependencies():
    source = open(typeset.__file__, encoding="utf-8").read()
    for forbidden in ("import requests", "import numpy", "from .brand",
                      "from . import brand"):
        assert forbidden not in source, f"typeset 不得依赖 {forbidden}"
    assert "from PIL import Image, ImageDraw, ImageFont" in source


# --------------------------------------------------------------------------
# 探测与渲染不得漂移
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "短标题",
    "免费部署本地 AI 的完整路径",
    "一个比较长但还能放下的标题示例文字",
])
def test_probe_band_top_matches_the_rendered_pixels(title):
    """probe 报的 band_top 必须等于产出图里信息带首行的 y 坐标。"""
    source = canvas()
    kwargs = {"title": title, "account_label": "账号", "page_label": "1/3"}
    produced = typeset.typeset_image(source, **kwargs)
    geometry = typeset.probe_cover_geometry((800, 600), **kwargs)
    assert abs(scan_first_band_row(produced) - geometry["band_top"]) <= 1


def test_probe_does_not_write_pixels():
    source = canvas()
    before = hashlib.sha256(source).hexdigest()
    typeset.probe_cover_geometry((800, 600), title="标题")
    assert hashlib.sha256(source).hexdigest() == before


def test_measure_band_is_shared_by_probe_and_render():
    """两者调用同一份算术，不是两份拷贝。"""
    import inspect

    probe_source = inspect.getsource(typeset.probe_cover_geometry)
    render_source = inspect.getsource(typeset.typeset_image)
    assert "measure_band" in probe_source
    assert "measure_band" in render_source


def test_engine_probe_agrees_with_typeset_probe():
    source = canvas()
    kwargs = {"title": "免费部署本地 AI 的完整路径", "account_label": "账号"}
    assert typeset_engine.probe(source, **kwargs) == \
        typeset.probe_cover_geometry((800, 600), **kwargs)


# --------------------------------------------------------------------------
# 截断检测：对应现有的静默封顶
# --------------------------------------------------------------------------

def test_tall_title_on_a_short_canvas_is_reported_as_truncated():
    """800x383 画幅配 120 字标题：信息带被 height//2 封顶，尾部被裁。"""
    geometry = typeset.probe_cover_geometry((800, 383), "标" * 120,
                                           account_label="账号", page_label="1/3")
    assert geometry["band_capped"] is True
    assert geometry["title_truncated"] is True
    assert geometry["band_top"] == 383 - 383 // 2


def test_same_title_on_a_tall_canvas_is_not_truncated():
    geometry = typeset.probe_cover_geometry((800, 600), "标" * 120,
                                           account_label="账号", page_label="1/3")
    assert geometry["title_truncated"] is False
    assert geometry["visible_rows"] >= len(geometry["title_lines"])


def test_engine_title_overflow_matches_the_geometry():
    source = canvas(800, 383)
    overflow, detail, geometry = typeset_engine.title_overflow(source, "标" * 120,
                                                              account_label="账号")
    assert overflow is True
    assert "裁掉" in detail
    assert geometry["title_truncated"] is True


def test_engine_title_overflow_is_quiet_for_a_normal_title():
    overflow, detail, _ = typeset_engine.title_overflow(canvas(), "短标题")
    assert overflow is False
    assert detail == ""


def test_engine_title_overflow_reports_a_dominant_band_even_without_truncation():
    """不截断但信息带吃掉大半个画幅时也要提醒。"""
    _, detail, _ = typeset_engine.title_overflow(canvas(800, 600), "标" * 120,
                                                account_label="账号")
    assert "建议值" in detail or "裁掉" in detail


def test_truncation_is_reported_not_silently_fixed():
    """排版层不改产出：截断只如实上报，由作者自己改标题。"""
    source = canvas(800, 383)
    kwargs = {"title": "标" * 120, "account_label": "账号"}
    before = hashlib.sha256(typeset.typeset_image(source, **kwargs)).hexdigest()
    typeset_engine.title_overflow(source, "标" * 120, account_label="账号")
    after = hashlib.sha256(typeset.typeset_image(source, **kwargs)).hexdigest()
    assert before == after


# --------------------------------------------------------------------------
# measure_band 的边界
# --------------------------------------------------------------------------

def test_empty_title_is_rejected_by_measurement_too():
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="排版标题不能为空"):
            typeset.probe_cover_geometry((800, 600), bad)


def test_title_is_capped_at_max_title():
    geometry = typeset.probe_cover_geometry((800, 600), "标" * 500)
    assert sum(len(line) for line in geometry["title_lines"]) <= typeset.MAX_TITLE


def test_geometry_is_json_serializable():
    import json

    geometry = typeset.probe_cover_geometry((800, 600), "标题", account_label="账号")
    assert json.loads(json.dumps(geometry, ensure_ascii=False)) == geometry


def test_public_aliases_exist_for_reuse():
    assert typeset.load_font is typeset._load_font
    assert typeset.wrap_lines is typeset._wrap


# --------------------------------------------------------------------------
# 品牌基线接线
# --------------------------------------------------------------------------

def test_engine_reads_band_colours_from_the_brand_profile():
    tokens = brand.tokens()
    assert typeset_engine._matches_typeset_defaults(tokens) is True
    off_brand = {**tokens, "band_fill": "#101010"}
    assert typeset_engine._matches_typeset_defaults(off_brand) is False


def test_off_brand_profile_renders_through_the_token_path():
    off_brand = {"tokens": {**brand.tokens(), "band_fill": "#202020",
                            "band_title": "#ffee00"}}
    source = canvas()
    output = typeset_engine.compose_cover(source, title="标题", profile=off_brand)
    assert output != typeset.typeset_image(source, title="标题")
    assert Image.open(BytesIO(output)).size == (800, 600)


def test_brand_metrics_still_match_the_typeset_arithmetic():
    """brand.metrics 与 typeset 的算术必须一致，否则探测会算错字号与折行宽度。"""
    from PIL import ImageDraw

    metrics = brand.metrics()
    width = 800
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    expected_font = typeset.load_font(max(18, width // metrics["title_divisor"]))
    lines = typeset.wrap_lines(scratch, "标" * 40, expected_font,
                               width - metrics["band_padding"])
    assert expected_font.size == max(18, width // 24)
    assert expected_font.size + metrics["line_gap"] > 0
    assert lines and all(line for line in lines)
    # 小字号同样是 width // 48，与 typeset 内联一致
    assert typeset.load_font(max(12, width // metrics["footer_divisor"])).size == \
        max(12, width // 48)


def test_requirements_lock_is_untouched():
    from backend.app import config

    content = (config.PROJECT_ROOT / "backend" / "requirements.lock.txt").read_text(encoding="utf-8")
    for forbidden in ("matplotlib", "weasyprint", "reportlab", "numpy"):
        assert forbidden not in content, f"V2-08 不得引入新依赖（{forbidden}）"


def test_engine_module_docstring_forbids_new_fonts_and_autotruncation():
    source = open(typeset_engine.__file__, encoding="utf-8").read()
    assert "不新增字体" in source
    assert "不做自动截断" in source


def test_engine_rejects_a_too_small_canvas():
    with pytest.raises(ValueError, match="图片过小"):
        typeset_engine.probe(canvas(32, 32), title="标题")
