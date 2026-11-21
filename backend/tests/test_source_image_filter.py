"""源图过滤：二维码、站点标识、推广图不能进正文配图。

2026-10-22 真实事故：澎湃文末的「早晚报」栏目图与两个公众号二维码被抓成
源图写进三平台草稿，读者看到的是「正文 + 澎湃二维码」—— 比没有图更糟。
新闻页面里的图大部分是站点物料，不是报道配图。
"""
from io import BytesIO

from PIL import Image

from backend.app.research import Article, _image_looks_like_junk, _unusable_image


def _parse(html: str) -> Article:
    parser = Article()
    parser.feed(html)
    parser.close()
    return parser


def qrcode_png(width=468, height=528):
    """按真实事故样本造一张二维码：黑白随机方块 + 底部说明文字。"""
    import random
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    random.seed(7)
    for y in range(0, int(height * 0.75), 16):
        for x in range(16, int(width * 0.9), 16):
            if random.random() < 0.5:
                draw.rectangle([x, y, x + 14, y + 14], fill='black')
    draw.text((width * 0.18, height * 0.88), '微信扫一扫 关注公众号', fill='black')
    buffer = BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def test_qrcode_image_is_rejected_by_pixel_signature():
    assert _unusable_image(qrcode_png()) is True


def test_black_and_white_photo_with_midtones_is_kept():
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (640, 400), (128, 128, 128))
    draw = ImageDraw.Draw(image)
    for x in range(0, 640, 8):
        draw.line([x, 0, x, 400], fill=(40 + (x % 200), 40 + (x % 200), 40 + (x % 200)))
    buffer = BytesIO()
    image.save(buffer, 'PNG')
    assert _unusable_image(buffer.getvalue()) is False


def test_download_time_size_floor():
    small = Image.new('RGB', (200, 50), 'white')
    buffer = BytesIO()
    small.save(buffer, 'PNG')
    assert _unusable_image(buffer.getvalue()) is True

    # 纯白图按「黑白双峰、无彩色」判据本来就该滤：空白占位不是配图。
    blank = Image.new('RGB', (640, 360), 'white')
    buffer = BytesIO()
    blank.save(buffer, 'PNG')
    assert _unusable_image(buffer.getvalue()) is True

    usable = Image.new('RGB', (640, 360), (150, 190, 230))
    buffer = BytesIO()
    usable.save(buffer, 'PNG')
    assert _unusable_image(buffer.getvalue()) is False


def test_qrcode_and_promo_images_are_skipped():
    html = """
    <img src="https://www.thepaper.cn/content.jpg">
    <img class="qrcode-img" src="https://www.thepaper.cn/qr.png">
    <img alt="微信扫一扫 关注公众号" src="https://www.thepaper.cn/follow.png">
    <img src="https://mmbiz.qpic.cn/mmbiz/abc/640">
    <img alt="澎湃早晚报" src="https://www.thepaper.cn/daily.png">
    """
    parser = _parse(html)
    assert parser.images == ['https://www.thepaper.cn/content.jpg']
    assert parser.junk_images == 4


def test_tiny_declared_size_is_skipped():
    parser = _parse('<img width="88" height="31" src="https://x.com/tracker.gif">')
    assert parser.images == []


def test_normal_photo_is_kept():
    html = '<img src="https://www.163.com/photo/php-server.jpg" alt="机房照片">'
    assert _parse(html).images == ['https://www.163.com/photo/php-server.jpg']


def test_junk_host_is_rejected_even_without_class_hints():
    attrs = {'src': 'https://mmbiz.qpic.cn/mmbiz/abc'}
    assert _image_looks_like_junk(attrs, attrs['src'])
