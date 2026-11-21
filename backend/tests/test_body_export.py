"""复制链路导出：正文富文本 HTML + 图片内嵌（body_export）。

背景（2026-10-28）：V3 本地内容模式下草稿靠人工复制进平台编辑器，
纯文本复制导致「一堵字墙 + 图片带不过去」。本模块把 body 渲染成
公众号/头条编辑器可粘贴的语义 HTML，并把已冻结的图片资产压成
base64 data URI 内嵌。
"""
import base64
import json
import struct
import zlib

import pytest

from backend.app import body_export
from backend.app.config import Settings
from backend.app.image_assets import register_assets


def jpg_bytes(width=600, height=400):
    """最小合法 JPEG：用 Pillow 生成，避免手拼字节。"""
    from io import BytesIO
    from PIL import Image
    image = Image.new('RGB', (width, height), (30, 90, 160))
    output = BytesIO()
    image.save(output, format='JPEG', quality=90)
    return output.getvalue()


@pytest.fixture
def settings(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    return Settings(runtime_dir=runtime)


WORK = {
    'platform': 'wechat',
    'title': '标题',
    'body': '## 关键判断\n\n这是一段正文，包含**重点数字 807 亿**。\n\n> 一句引用\n',
    'tags': ['t'],
}


def test_wechat_renders_html_with_markup(tmp_path, settings):
    result = body_export.build(WORK, 'wechat', settings, 'topic', 'ver')
    assert '<strong>重点数字 807 亿</strong>' in result['html']
    assert '<blockquote>' in result['html']
    # 无图片清单：不内嵌图片
    assert '<img' not in result['html']
    assert result['images'] == []
    # text 是去标记降级
    assert '##' not in result['text'] and '**' not in result['text']
    assert '重点数字 807 亿' in result['text']


def test_toutiao_heading_renders_h2(tmp_path, settings):
    result = body_export.build(WORK, 'toutiao', settings, 'topic', 'ver')
    assert '<h2>关键判断</h2>' in result['html']


def test_xiaohongshu_gets_text_only(tmp_path, settings):
    result = body_export.build(WORK, 'xiaohongshu', settings, 'topic', 'ver')
    assert result['html'] is None
    assert result['text'].startswith('关键判断')


def test_ready_manifest_images_embedded(tmp_path, settings):
    image_path = tmp_path / 'pool' / 'a.jpg'
    image_path.parent.mkdir()
    image_path.write_bytes(jpg_bytes(600, 400))
    manifest = register_assets(
        settings, 'topic', 'ver', 'wechat', [str(image_path)],
        ['源生图｜测试'], provider='source',
        source={'kind': 'source', 'rights_confirmed': True,
                'license': 'source_publisher_material_user_decides',
                'authorization_id': 'user-source-image-policy-2026-10-20'},
        asset_sources=[{'source_url': 'https://news.example.com/a',
                        'source_title': '报道标题', 'publisher': 'news.example.com'}])
    assert manifest['status'] == 'ready'
    result = body_export.build(WORK, 'wechat', settings, 'topic', 'ver')
    assert '<p><img src="data:image/jpeg;base64,' in result['html']
    assert len(result['images']) == 1
    embedded = result['images'][0]
    raw = base64.b64decode(embedded['data_uri'].split(',', 1)[1])
    assert raw.startswith(b'\xff\xd8')  # JPEG 魔数
    # 只缩小不放大：600px 原宽保持
    from PIL import Image
    from io import BytesIO
    assert Image.open(BytesIO(raw)).size == (600, 400)


def test_oversized_image_downscaled(tmp_path, settings):
    image_path = tmp_path / 'pool' / 'big.jpg'
    image_path.parent.mkdir()
    image_path.write_bytes(jpg_bytes(2400, 1600))
    register_assets(
        settings, 'topic', 'ver', 'wechat', [str(image_path)], ['测试'],
        provider='source',
        source={'kind': 'source', 'rights_confirmed': True,
                'license': 'l', 'authorization_id': 'x'})
    result = body_export.build(WORK, 'wechat', settings, 'topic', 'ver')
    from PIL import Image
    from io import BytesIO
    raw = base64.b64decode(result['images'][0]['data_uri'].split(',', 1)[1])
    assert Image.open(BytesIO(raw)).size == (1080, 720)


def test_missing_image_file_becomes_warning_not_error(tmp_path, settings):
    image_path = tmp_path / 'pool' / 'gone.jpg'
    image_path.parent.mkdir()
    image_path.write_bytes(jpg_bytes())
    register_assets(
        settings, 'topic', 'ver', 'wechat', [str(image_path)], ['测试'],
        provider='source',
        source={'kind': 'source', 'rights_confirmed': True,
                'license': 'l', 'authorization_id': 'x'})
    (tmp_path / 'runtime' / 'data' / 'assets').exists()  # 目录布局由模块内部决定
    # 直接把文件删掉，模拟资产文件丢失
    for candidate in (tmp_path / 'runtime').rglob('*.jpg'):
        candidate.unlink()
    result = body_export.build(WORK, 'wechat', settings, 'topic', 'ver')
    assert result['images'] == []
    assert result['warnings'], '文件丢失必须显式说明，不能静默'
    assert '<img' not in result['html']


def test_corrupt_manifest_degrades_to_no_images(tmp_path, settings):
    # 清单不存在：按无图处理，正文照常导出
    result = body_export.build(WORK, 'wechat', settings, 'topic', 'ver')
    assert result['html'].startswith('<p>')
    assert result['warnings'] == []
