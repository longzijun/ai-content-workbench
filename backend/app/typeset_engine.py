"""品牌感知的图片排版：复用 typeset 的几何，只把参数换成品牌基线。

为什么单独一层：
typeset.py 是既有的产出路径，改它风险高（34 个测试文件、已有真实产出图）。
本模块在其之上包一层，只做两件事：
  1. 把品牌基线（brand.DEFAULT_PROFILE）翻译成 typeset 的绘制参数
  2. 复用 typeset.probe_cover_geometry 做只算不画的测量，供排版审计用

不变式：**不传品牌参数时，compose_cover 的输出必须与 typeset.typeset_image
逐字节相同**。这不是「差不多」，是哈希相等 —— 有测试守着。品牌基线的数值
全部来自 typeset 现有的常量（brand.metrics 里逐条抄了并有测试双向比对），
所以品牌化不会引入新的排版行为，只会把可配的部分暴露出来。

边界 —— 本模块不做的事：
  不新增字体（只用系统已有中文字体，AI 生图错字问题的正解是程序化排版而非
  换一个会写错字的模型）；不做自动截断（截断会破坏内容与图片的绑定，
  由排版审计报 warning 让作者自己改标题）；不写原图与 manifest。
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

from . import brand, typeset


def _picker():
    """返回一个 size -> font 的取字函数，绑住本模块要用的字体候选表。

    品牌基线的 pil_candidates 与 typeset._FONT_CANDIDATES 由 test_brand 双向
    比对锁定；这里仍走 typeset 的加载逻辑，不自己实现字体查找。
    """
    candidates = brand.fonts()["pil_candidates"]
    original = typeset._FONT_CANDIDATES
    if candidates == list(original):
        return typeset.load_font

    def picker(size):
        from pathlib import Path
        from PIL import ImageFont
        for name in candidates:
            path = Path('C:/Windows/Fonts') / name
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
        return ImageFont.load_default()

    return picker


def compose_cover(image_bytes, *, title, account_label='', page_label='', profile=None):
    """按品牌基线合成封面信息带。profile 缺省用品牌默认；不改则等同 typeset。"""
    tokens = (profile or brand.DEFAULT_PROFILE)["tokens"]
    if _matches_typeset_defaults(tokens):
        # 与 typeset.typeset_image 的绘制参数完全一致，直接复用既有路径。
        # 这是「无品牌参数时逐字节相同」的物理保证，不靠测试去凑。
        return typeset.typeset_image(image_bytes, title=title,
                                     account_label=account_label, page_label=page_label)
    measured = typeset.measure_band(_probe_size(image_bytes), title,
                                    account_label=account_label, page_label=page_label,
                                    fonts=_picker())
    return _render_with_tokens(image_bytes, title, account_label, page_label,
                               measured, tokens)


def _matches_typeset_defaults(tokens) -> bool:
    """品牌基线是否与 typeset 的内联绘制参数一致。

    typeset.typeset_image 里写死了 fill=(12,16,24,200)、标题 (255,255,255)、
    页脚 (190,200,212)；brand 的 band_* 抄的就是这三个值。一致时走既有路径。
    """
    return (tokens['band_fill'] == '#0c1018'
            and tokens['band_alpha'] == 200
            and tokens['band_title'] == '#ffffff'
            and tokens['band_footer'] == '#bec8d4')


def _render_with_tokens(image_bytes, title, account_label, page_label, measured, tokens):
    with Image.open(BytesIO(image_bytes)) as source:
        base = source.convert('RGBA')
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    title_font, small_font = measured['fonts']
    fill = _rgba(tokens['band_fill']) + (int(tokens['band_alpha']),)
    draw.rectangle([(0, measured['band_top']), (base.width, base.height)], fill=fill)
    y = measured['band_top'] + 12
    for line in measured['title_lines']:
        draw.text((24, y), line, font=title_font, fill=_rgba(tokens['band_title']))
        y += measured['line_height']
    label = str(account_label or '').strip()[:typeset.MAX_LABEL]
    page = str(page_label or '').strip()[:typeset.MAX_PAGE]
    if label or page:
        footer = ' · '.join(part for part in (label, page) if part)
        draw.text((24, y + 2), footer, font=small_font, fill=_rgba(tokens['band_footer']))
    composited = Image.alpha_composite(base, overlay).convert('RGB')
    output = BytesIO()
    composited.save(output, format='PNG')
    return output.getvalue()


def probe(image_bytes, *, title, account_label='', page_label=''):
    """只算不画的信息带几何。品牌感知。"""
    return typeset.probe_cover_geometry(_probe_size(image_bytes), title,
                                        account_label=account_label, page_label=page_label)


def title_overflow(image_bytes, title, *, account_label='', page_label='', max_ratio=0.5):
    """标题是否会被信息带静默截断。返回 (是否溢出, 说明, 几何)。

    对应 typeset 里 min(band_height, height // 2) 的封顶：标题太长时信息带
    不会吃掉整张图，代价是标题被裁掉。渲染保持原样（本模块不改产出），
    这里只如实报出该情况，由排版审计转成 warning。
    """
    geometry = probe(image_bytes, title=title, account_label=account_label,
                     page_label=page_label)
    ratio = geometry['band_height'] / max(1, geometry['height'])
    overflow = geometry['title_truncated']
    if overflow:
        detail = (f"标题折成 {len(geometry['title_lines'])} 行，信息带最多容 "
                  f"{geometry['visible_rows']} 行，尾部会被裁掉；请缩短标题或改用更矮的画幅")
    elif ratio > max_ratio:
        detail = (f"信息带占画幅 {ratio:.0%}，超过 {max_ratio:.0%} 建议值；"
                  f"画面主体会被压缩，建议缩短标题")
    else:
        detail = ''
    return overflow, detail, geometry


def _probe_size(image_bytes):
    with Image.open(BytesIO(image_bytes)) as source:
        size = source.size
    if size[0] < 64 or size[1] < 64:
        raise ValueError('图片过小，不适合排版')
    return size


def _rgba(hex_value):
    text = str(hex_value).lstrip('#')
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))