"""中文排版叠加：把标题、账号标识、页码由程序绘制到图片上，避免模型写中文出错。

边界：只对已通过校验的版本资产做派生成品图，绝不改动原图与 manifest；
成品图按内容哈希缓存，参数不变则复用同一文件。
"""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .image_assets import _asset_directory, get_asset_file

_FONT_CANDIDATES = ('msyhbd.ttc', 'msyh.ttc', 'simhei.ttf', 'simsun.ttc')
MAX_TITLE = 120
MAX_LABEL = 60
MAX_PAGE = 20


def _load_font(size):
    from sys import prefix as _ignored  # noqa: F401  (keep import local; font paths are Windows-specific)
    for name in _FONT_CANDIDATES:
        path = Path('C:/Windows/Fonts') / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    lines = []
    for paragraph in text.split('\n'):
        current = ''
        for char in paragraph:
            candidate = current + char
            if draw.textlength(candidate, font=font) > max_width and current:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines or ['']


#: 公开别名。排版审计（layout_audit）需要在不写像素的前提下测量文字，
#: 所以字体加载与折行逻辑必须可复用。原私有名保留给既有内部调用。
load_font = _load_font
wrap_lines = _wrap


def measure_band(size, title, *, account_label='', page_label='', fonts=None):
    """计算信息带的几何，不写任何像素。

    返回 title_lines / line_height / band_height / band_top / fonts /
    band_height_capped。typeset_image 与 probe_cover_geometry 都调用本函数 ——
    这就是「探测与渲染不会漂移」的实现方式：它们不是两份算术，是同一份。

    注意 band_top 用 min(band_height, height // 2) 封顶：标题过长时信息带
    不会吃掉整张图，代价是**标题被静默截断**。truncated 字段如实标出这一
    情况，供排版审计报 warning —— 渲染本身保持原样，不改变现有产出。
    """
    width, height = size
    picker = fonts or _load_font
    title_text = str(title or '').strip()[:MAX_TITLE]
    if not title_text:
        raise ValueError('排版标题不能为空')
    label = str(account_label or '').strip()[:MAX_LABEL]
    page = str(page_label or '').strip()[:MAX_PAGE]
    # 折行需要 draw.textlength；几何探测用一张一次性画布即可，不触碰原图。
    scratch = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    title_font = picker(max(18, width // 24))
    small_font = picker(max(12, width // 48))
    title_lines = _wrap(scratch, title_text, title_font, width - 48)
    line_height = title_font.size + 8
    band_height = (24 + line_height * len(title_lines)
                   + (small_font.size + 10 if (label or page) else 0) + 12)
    capped = min(band_height, height // 2)
    # 标题行能完整落在信息带里才算没被截断。
    visible_rows = (capped - 12) // line_height
    return {
        'title_lines': title_lines,
        'line_height': line_height,
        'band_height': band_height,
        'band_top': height - capped,
        'band_capped': capped < band_height,
        'title_truncated': len(title_lines) > max(0, visible_rows),
        'visible_rows': max(0, visible_rows),
        'fonts': (title_font, small_font),
    }


def probe_cover_geometry(size, title, *, account_label='', page_label='', fonts=None):
    """只算不画的信息带几何。供排版审计与预览使用。"""
    measured = measure_band(size, title, account_label=account_label,
                            page_label=page_label, fonts=fonts)
    measured.pop('fonts')
    measured['width'], measured['height'] = size
    return measured


def typeset_image(image_bytes, *, title, account_label='', page_label=''):
    """在图片底部叠加半透明信息带（标题 + 账号标识 + 页码），返回 PNG 字节。"""
    with Image.open(BytesIO(image_bytes)) as source:
        base = source.convert('RGBA')
    if base.width < 64 or base.height < 64:
        raise ValueError('图片过小，不适合排版')
    measured = measure_band(base.size, title,
                            account_label=account_label, page_label=page_label)
    title_font, small_font = measured['fonts']
    title_lines = measured['title_lines']
    line_height = measured['line_height']
    band_top = measured['band_top']
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([(0, band_top), (base.width, base.height)], fill=(12, 16, 24, 200))
    y = band_top + 12
    for line in title_lines:
        draw.text((24, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += line_height
    if account_label or page_label:
        label = str(account_label or '').strip()[:MAX_LABEL]
        page = str(page_label or '').strip()[:MAX_PAGE]
        footer = ' · '.join(part for part in (label, page) if part)
        draw.text((24, y + 2), footer, font=small_font, fill=(190, 200, 212, 255))
    composited = Image.alpha_composite(base, overlay).convert('RGB')
    output = BytesIO()
    composited.save(output, format='PNG')
    return output.getvalue()


def typeset_asset(settings, topic_id, version_id, platform, asset_id, *,
                  title, account_label='', page_label=''):
    """对指定资产生成（或复用缓存）排版成品图，返回成品文件路径。"""
    source_path = get_asset_file(settings, topic_id, version_id, platform, asset_id)
    key = sha256('\x1f'.join((asset_id, title, account_label, page_label)).encode('utf-8')).hexdigest()[:16]
    directory = _asset_directory(settings, topic_id, version_id, platform)
    derived = directory / 'derived'
    derived.mkdir(parents=True, exist_ok=True)
    target = derived / f'{asset_id}_typeset_{key}.png'
    if target.is_file():
        return target
    data = typeset_image(source_path.read_bytes(), title=title,
                         account_label=account_label, page_label=page_label)
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(data)
    temporary.replace(target)
    return target
