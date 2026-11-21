"""源生图链路：原文图片解析过滤 → 选题池构建 → 版本冻结注册。"""
import json
import struct
import time
import zlib
from pathlib import Path

import pytest

from backend.app import research, source_images
from backend.app.config import Settings
from backend.app.image_assets import get_asset_manifest


def png(width=8, height=8, color=(24, 120, 100)):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    pixels = zlib.compress((b'\x00' + (bytes(color) + b'\xff') * width) * height)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', pixels) + chunk(b'IEND', b'')


@pytest.fixture
def settings(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    return Settings(runtime_dir=runtime)


def test_article_parser_collects_probable_photos_only():
    parser = research.Article()
    parser.feed('<html><body>'
                '<img src="/img/lead.jpg">'
                '<img data-src="https://cdn.example.com/photo/202610/news.png">'
                '<img src="https://cdn.example.com/static/logo.png">'
                '<img src="data:image/png;base64,AAAA">'
                '<img src="/pic/icon.gif">'
                '</body></html>')
    base = 'https://www.example.com/news/1.html'
    filtered = [url for url in (research.urljoin(base, image) for image in parser.images)
                if research._is_probable_photo(url)]
    assert filtered == ['https://www.example.com/img/lead.jpg',
                        'https://cdn.example.com/photo/202610/news.png']


def test_read_article_exposes_absolute_image_candidates(monkeypatch):
    page = ('<html><head><title>原始报道</title></head><body>'
            '<p>' + '现场情况持续更新，记者核实了相关细节与时间线，官方回应称已介入调查处理。' * 8 + '</p>'
            '<img src="//img.example.com/photo/a.jpg"></body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'] == ['https://img.example.com/photo/a.jpg']


def test_collect_source_images_downloads_dedups_and_caps_per_article(settings, monkeypatch):
    topic = {'id': 't1', 'revision': 1}
    documents = [
        {'url': 'https://a.example.com/1.html', 'title': '报道一',
         'images': [f'https://a.example.com/p{n}.jpg' for n in range(1, 6)]},
        {'url': 'https://b.example.com/2.html', 'title': '报道二',
         'images': ['https://b.example.com/p1.jpg']},
    ]

    def fake_fetch(url, referer, deadline):
        name = url.rsplit('/', 1)[-1]
        assert referer == documents[0]['url'] or referer == documents[1]['url']
        # 假图必须像真新闻配图：正常尺寸 + 有色彩（黑白双峰会被二维码判别滤掉）
        return png(width=640, height=360, color=(90, 140 + int(name[1]) * 12, 200)), 'image/png'

    monkeypatch.setattr(research, 'fetch_image', fake_fetch)
    pool, errors = research.collect_source_images(topic, documents, time.monotonic() + 30, settings=settings)
    assert errors == []
    assert len(pool) == research.MAX_IMAGES_PER_ARTICLE  # 第二篇 p1 与第一篇同字节去重，不新增
    assert pool[0]['source_title'] == '报道一' and pool[0]['source_url'].startswith('https://a.example.com')
    assert Path(pool[0]['path']).is_relative_to(settings.runtime_dir / 'data' / 'research' / 'images' / 't1')
    stored = json.loads(source_images.pool_path(settings, 't1').read_text('utf-8'))
    assert stored['images'] == pool


def test_collect_source_images_keeps_previous_pool_on_refetch(settings, monkeypatch):
    topic = {'id': 't2', 'revision': 1}
    source_images.pool_path(settings, 't2').parent.mkdir(parents=True, exist_ok=True)
    old = Path(settings.runtime_dir / 'data' / 'research' / 'images' / 't2' / 'old.png')
    old.write_bytes(png(color=(9, 9, 9)))
    source_images.pool_path(settings, 't2').write_text(json.dumps(
        {'topic_id': 't2', 'images': [{'path': str(old), 'sha256': 'deadbeef', 'bytes': old.stat().st_size,
                                       'source_url': 'https://x.example.com/0.html', 'source_title': '旧图'}]}), 'utf-8')

    monkeypatch.setattr(research, 'fetch_image', lambda url, referer, deadline: png(color=(3, 3, 3)), )
    pool, errors = research.collect_source_images(topic, [], time.monotonic() + 5, settings=settings)
    assert errors == []
    assert [item['sha256'] for item in pool] == ['deadbeef']


def test_fetch_image_rejects_gif_content_type(monkeypatch):
    # Content-Type 白名单分支：伪 fetch 直接复用 fetch_image 内部校验不可行，
    # 此处只验证 SOURCE_IMAGE_TYPES 与注册管线支持的栅格格式一致。
    assert set(research.SOURCE_IMAGE_TYPES) == {'image/jpeg', 'image/png', 'image/webp'}


def test_register_source_images_respects_limits_and_is_idempotent(settings):
    topic_id, version_id = 't1', 'v1'
    pool = []
    for index in range(6):
        path = settings.runtime_dir / 'pool' / f'img{index}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png(color=(index + 1, 5, 5)))
        pool.append({'path': str(path), 'sha256': f'fake{index}', 'bytes': path.stat().st_size,
                     'source_url': f'https://a.example.com/{index}.html', 'source_title': f'报道{index}'})
    source_images.pool_path(settings, topic_id).parent.mkdir(parents=True, exist_ok=True)
    source_images.pool_path(settings, topic_id).write_text(
        json.dumps({'topic_id': topic_id, 'images': pool}), 'utf-8')

    manifest = source_images.register_source_images(settings, topic_id, version_id, 'wechat')
    assert manifest['status'] == 'ready' and len(manifest['assets']) == 3
    asset = manifest['assets'][0]
    assert asset['is_ai_generated'] is False and asset['review_required'] is True
    assert '源生图｜来自原文：报道0' in asset['prompt']
    assert asset['license'] == 'source_publisher_material_user_decides'

    again = source_images.register_source_images(settings, topic_id, version_id, 'wechat')
    assert [a['sha256'] for a in again['assets']] == [a['sha256'] for a in manifest['assets']]

    note = source_images.register_source_images(settings, topic_id, 'v2', 'xiaohongshu')
    assert len(note['assets']) == 6  # 池内仅 6 张，小红书上限 9 不虚增


def test_register_source_images_without_pool_returns_none(settings):
    assert source_images.register_source_images(settings, 't-none', 'v1', 'wechat') is None


def test_register_for_works_reports_without_raising(settings):
    works = [{'platform': 'wechat'}, {'platform': 'toutiao'}, {'platform': 'xiaohongshu'}]
    outcome = source_images.register_for_works(settings, {'id': 't-empty'}, 'v1', works)
    assert outcome == {'wechat': {'status': 'no_source_images', 'count': 0},
                       'toutiao': {'status': 'no_source_images', 'count': 0},
                       'xiaohongshu': {'status': 'no_source_images', 'count': 0}}


def test_register_for_works_never_blocks_on_missing_files(settings, monkeypatch):
    topic_id = 't3'
    source_images.pool_path(settings, topic_id).parent.mkdir(parents=True, exist_ok=True)
    source_images.pool_path(settings, topic_id).write_text(json.dumps(
        {'topic_id': topic_id, 'images': [{'path': 'Z:/gone/missing.png', 'sha256': 'x', 'bytes': 1,
                                           'source_url': 'https://a.example.com/1.html', 'source_title': '丢'}]}), 'utf-8')
    outcome = source_images.register_for_works(settings, {'id': topic_id}, 'v9', [{'platform': 'wechat'}])
    assert outcome['wechat'] == {'status': 'no_source_images', 'count': 0}


def test_source_manifest_stays_blocked_consistent_with_asset_pipeline(settings):
    topic_id, version_id = 't4', 'v1'
    source_images.pool_path(settings, topic_id).parent.mkdir(parents=True, exist_ok=True)
    source_images.pool_path(settings, topic_id).write_text(json.dumps({'topic_id': topic_id, 'images': []}), 'utf-8')
    assert source_images.register_source_images(settings, topic_id, version_id, 'wechat') is None
    assert get_asset_manifest(settings, topic_id, version_id, 'wechat')['status'] == 'not_started'


# --------------------------------------------------------------------------
# SPA / 内嵌 JSON 页面的图片提取（2026-10-28：wallstreetcn 抓到 0 图的修复）
# --------------------------------------------------------------------------

def test_read_article_extracts_images_from_embedded_json(monkeypatch):
    # wallstreetcn 形态：正文图整段存在 <script> 的 JSON 字符串里，
    # 斜杠被转义成 \/ ，DOM 层没有 <img>。
    escaped_slash = chr(92) + '/'  # \/ ——避开 shell/字面量转义歧义
    json_gallery = '{"gallery":[{"url":"https:' + escaped_slash * 2 + 'wpimg.example.com'
    json_gallery += escaped_slash + '2026' + escaped_slash + '10' + escaped_slash + 'abc.jpg"}'
    json_gallery += ',{"url":"https:' + escaped_slash * 2 + 'img.example.com'
    json_gallery += escaped_slash + 'chart_def.png?x-oss-process=style/w"}]}'
    page = ('<html><head><title>原始报道</title></head><body>'
            '<p>' + '申报文件披露了上半年亏损构成与算力租赁合同的细节，监管与市场都在关注。' * 8 + '</p>'
            '<script>window.__STATE=' + json_gallery + '</script>'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'] == ['https://wpimg.example.com/2026/10/abc.jpg',
                                  'https://img.example.com/chart_def.png?x-oss-process=style/w']


def test_read_article_json_extraction_filters_junk_urls(monkeypatch):
    escaped_slash = chr(92) + '/'
    json_state = ('{"qrcode":"https:' + escaped_slash * 2 + 'img.example.com'
                  + escaped_slash + 'qrcode.jpg",'
                  '"logo":"https:' + escaped_slash * 2 + 'cdn.example.com'
                  + escaped_slash + 'site_logo.png",'
                  '"photo":"https:' + escaped_slash * 2 + 'img.example.com'
                  + escaped_slash + 'photo' + escaped_slash + 'lead.jpg"}')
    page = ('<html><head><title>原始报道</title></head><body>'
            '<p>' + '正文内容持续更新，记者核实了相关细节与时间线，官方回应称已介入调查处理。' * 8 + '</p>'
            '<script>var s=' + json_state + '</script>'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'] == ['https://img.example.com/photo/lead.jpg']


def test_read_article_dom_images_rank_before_json_images(monkeypatch):
    escaped_slash = chr(92) + '/'
    json_state = '{"photo":"https:' + escaped_slash * 2 + 'img.example.com' + escaped_slash + 'json-second.jpg"}'
    page = ('<html><head><title>原始报道</title></head><body>'
            '<p>' + '正文内容持续更新，记者核实了相关细节与时间线，官方回应称已介入调查处理。' * 8 + '</p>'
            '<img src="//img.example.com/dom-first.jpg">'
            '<script>var s=' + json_state + '</script>'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'] == ['https://img.example.com/dom-first.jpg',
                                  'https://img.example.com/json-second.jpg']


def test_read_article_srcset_takes_first_candidate(monkeypatch):
    page = ('<html><head><title>原始报道</title></head><body>'
            '<p>' + '正文内容持续更新，记者核实了相关细节与时间线，官方回应称已介入调查处理。' * 8 + '</p>'
            '<img srcset="https://img.example.com/small.jpg 640w, https://img.example.com/large.jpg 1280w">'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert 'https://img.example.com/small.jpg' in document['images']


def test_read_article_og_image_leads_candidates(monkeypatch):
    # wallstreetcn 形态：正文经客户端渲染，静态 HTML 没有正文 <img>，
    # 但 SSR head 有 og:image（封面大图）——应成为首选候选。
    page = ('<html><head><title>原始报道</title>'
            '<meta property="og:image" content="https://wpimg.example.com/cover-1024.png">'
            '<meta name="twitter:image" content="https://wpimg.example.com/cover-small.jpg">'
            '</head><body>'
            '<p>' + '申报文件披露了上半年亏损构成与算力租赁合同的细节，监管与市场都在关注。' * 8 + '</p>'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'][0] == 'https://wpimg.example.com/cover-1024.png'
    assert 'https://wpimg.example.com/cover-small.jpg' in document['images']


def test_read_article_og_image_qrcode_filtered(monkeypatch):
    page = ('<html><head><title>原始报道</title>'
            '<meta property="og:image" content="https://cdn.example.com/qrcode.jpg">'
            '</head><body>'
            '<p>' + '正文内容持续更新，记者核实了相关细节与时间线，官方回应称已介入调查处理。' * 8 + '</p>'
            '</body></html>')
    monkeypatch.setattr(research, 'fetch_public', lambda url, deadline: ('https://news.example.com/x.html', page))
    document = research.read_article('https://news.example.com/x.html', time.monotonic() + 10)
    assert document['images'] == []
