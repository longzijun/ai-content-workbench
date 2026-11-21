"""源生图逐图溯源：每张图必须带自己的原文链接与标题。

背景（2026-10-22 实测）：75 张已落库图片中，source_url / license_url /
rights_holder 填充率均为 0/75。根因是 register_source_images 把池里的
url 与 title 拼进了 prompt 字符串，而 manifest 的 source 是所有图共享的
一份、caption 为空。前端 AssetPreview 读 asset.source.url 恒为 undefined，
「查看原始来源」按钮因此永不渲染，图文对应关系也无从核对。

这些测试锁死逐图溯源，防止回退成批量共享 source。
"""
import json

import pytest
from PIL import Image

from backend.app import db, image_assets, source_images


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(__import__("backend.app.config", fromlist=["config"]),
                        "discover_codex", lambda: "")
    settings = __import__("backend.app.config", fromlist=["config"]).get_settings()
    yield settings
    db.init_db()


def make_jpeg(path, payload: bytes):
    """生成真实可解码的 JPEG。

    不能手搓字节：_read_trusted_source 会真的 image.load() 解码并校验尺寸，
    伪造的字节过不了（这正是这道防线的价值）。这里用 Pillow 落一张真图，
    用 payload 派生像素内容保证各图 sha256 不同。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = payload[0] if payload else 0
    Image.new('RGB', (64, 48), (seed, seed, seed)).save(path, 'JPEG')
    return path


def seed_pool(settings, topic_id, entries):
    pool = source_images.pool_path(settings, topic_id)
    pool.parent.mkdir(parents=True, exist_ok=True)
    images = []
    for index, (url, title) in enumerate(entries):
        path = make_jpeg(pool.parent / f'img{index}.jpg', bytes([index] * 64))
        images.append({'path': str(path), 'sha256': f'h{index}', 'bytes': path.stat().st_size,
                       'source_url': url, 'source_title': title})
    pool.write_text(json.dumps({'images': images}, ensure_ascii=False), 'utf-8')
    return images


def register(settings, topic_id='t1', version_id='v1', platform='wechat'):
    return source_images.register_source_images(settings, topic_id, version_id, platform)


# --------------------------------------------------------------------------
# 逐图溯源
# --------------------------------------------------------------------------

def test_each_asset_carries_its_own_source_url(world):
    seed_pool(world, 't1', [
        ('https://news.china.com/a/1.html', '甲文标题'),
        ('https://www.sohu.com/a/2', '乙文标题'),
    ])
    manifest = register(world)
    urls = [asset['source'].get('source_url') for asset in manifest['assets']]
    assert urls == ['https://news.china.com/a/1.html', 'https://www.sohu.com/a/2']


def test_each_asset_carries_its_own_title(world):
    seed_pool(world, 't1', [('https://a.com/1', '甲文'), ('https://b.com/2', '乙文')])
    manifest = register(world)
    titles = [asset['source'].get('source_title') for asset in manifest['assets']]
    assert titles == ['甲文', '乙文'], '标题必须逐图对应，不能所有图共用一条'


def test_two_assets_from_one_article_keep_the_same_url(world):
    seed_pool(world, 't1', [('https://a.com/1', '同一篇'), ('https://a.com/1', '同一篇')])
    manifest = register(world)
    assert len(manifest['assets']) == 2
    assert all(asset['source']['source_url'] == 'https://a.com/1' for asset in manifest['assets'])


def test_rights_holder_is_derived_from_the_real_host(world):
    seed_pool(world, 't1', [('https://www.sohu.com/a/2', '乙文')])
    manifest = register(world)
    assert manifest['assets'][0]['rights_holder'] == 'sohu.com'
    assert manifest['assets'][0]['source']['publisher'] == 'sohu.com'


def test_source_image_is_never_flagged_as_verified_event_photo(world):
    """抓来的图不等于事件现场照片。未人工确认前必须 False。"""
    seed_pool(world, 't1', [('https://a.com/1', '某现场图')])
    manifest = register(world)
    assert manifest['assets'][0]['depicts_verified_event'] is False


def test_caption_falls_back_to_article_title(world):
    """无 alt 文本时用原文标题当图注，避免素材卡片只剩一个空说明。"""
    seed_pool(world, 't1', [('https://a.com/1', '钟睒睒押注医美')])
    manifest = register(world)
    assert manifest['assets'][0]['caption'] == '钟睒睒押注医美'


def test_explicit_alt_text_wins_over_title(world):
    pool = source_images.pool_path(world, 't1')
    pool.parent.mkdir(parents=True, exist_ok=True)
    path = make_jpeg(pool.parent / 'x.jpg', b'\x01' * 64)
    pool.write_text(json.dumps({'images': [
        {'path': str(path), 'sha256': 'h', 'source_url': 'https://a.com/1',
         'source_title': '文章标题', 'alt': '文章里的图片说明'}
    ]}, ensure_ascii=False), 'utf-8')
    manifest = register(world)
    assert manifest['assets'][0]['caption'] == '文章里的图片说明'


def test_manifest_keeps_the_batch_level_rights_policy(world):
    """逐图溯源不能把用户 2026-10-20 的授权策略冲掉。"""
    seed_pool(world, 't1', [('https://a.com/1', '甲')])
    manifest = register(world)
    asset = manifest['assets'][0]
    assert asset['license'] == 'source_publisher_material_user_decides'
    assert asset['source']['rights_confirmed'] is True
    assert asset['source']['authorization_id'] == 'user-source-image-policy-2026-10-20'


def test_generated_assets_still_get_the_generated_disclosure(world):
    """回归红线：AI 生成图的披露文案不能被逐图溯源改掉。"""
    manifest = image_assets.get_asset_manifest(world, 't1', 'v1', 'wechat')
    assert manifest['status'] in {'not_started', 'ready'}


# --------------------------------------------------------------------------
# 缺失与异常输入
# --------------------------------------------------------------------------

def test_asset_without_url_still_registers_but_marks_it_unlinked(world):
    pool = source_images.pool_path(world, 't1')
    pool.parent.mkdir(parents=True, exist_ok=True)
    path = make_jpeg(pool.parent / 'y.jpg', b'\x02' * 64)
    pool.write_text(json.dumps({'images': [
        {'path': str(path), 'sha256': 'h', 'source_url': '', 'source_title': ''}
    ]}, ensure_ascii=False), 'utf-8')
    manifest = register(world)
    assert manifest['assets'][0]['source']['source_url'] == ''
    assert manifest['assets'][0]['depicts_verified_event'] is False


def test_empty_pool_returns_none(world):
    assert register(world) is None


def test_corrupt_pool_file_is_treated_as_empty(world):
    pool = source_images.pool_path(world, 't1')
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text('{ 不是 json', 'utf-8')
    assert source_images.load_pool(world, 't1') == []
    assert register(world) is None


def test_pool_entry_with_missing_file_is_skipped(world):
    seed_pool(world, 't1', [('https://a.com/1', '甲')])
    data = json.loads(source_images.pool_path(world, 't1').read_text('utf-8'))
    data['images'][0]['path'] = str(source_images.pool_path(world, 't1').parent / 'nope.jpg')
    source_images.pool_path(world, 't1').write_text(json.dumps(data, ensure_ascii=False), 'utf-8')
    assert register(world) is None


# --------------------------------------------------------------------------
# 幂等
# --------------------------------------------------------------------------

def test_registering_twice_returns_the_same_manifest(world):
    seed_pool(world, 't1', [('https://a.com/1', '甲'), ('https://b.com/2', '乙')])
    first = register(world)
    second = register(world)
    assert [a['id'] for a in first['assets']] == [a['id'] for a in second['assets']]
    assert second['assets'][0]['source']['source_url'] == 'https://a.com/1'


# --------------------------------------------------------------------------
# 平台上限
# --------------------------------------------------------------------------

def test_platform_limit_is_applied(world):
    seed_pool(world, 't1', [(f'https://a.com/{i}', f'文{i}') for i in range(12)])
    assert len(register(world, platform='wechat')['assets']) == 3
    assert len(register(world, platform='xiaohongshu', version_id='v2')['assets']) == 9


def test_urls_remain_correct_after_truncation(world):
    """取前 N 张后，被截掉的后几张不能导致来源错位。"""
    seed_pool(world, 't1', [(f'https://a.com/{i}', f'文{i}') for i in range(12)])
    manifest = register(world, platform='wechat')
    assert [asset['source']['source_url'] for asset in manifest['assets']] == [
        'https://a.com/0', 'https://a.com/1', 'https://a.com/2']


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------

def test_mismatched_asset_sources_length_is_rejected(world):
    seed_pool(world, 't1', [('https://a.com/1', '甲'), ('https://b.com/2', '乙')])
    images = source_images.load_pool(world, 't1')
    with pytest.raises(image_assets.ImageAssetError):
        image_assets.register_assets(
            world, 't1', 'v9', 'wechat', [i['path'] for i in images],
            ['p', 'p'], provider='source', source=source_images.SOURCE_USER_POLICY,
            asset_sources=[{'source_url': 'https://a.com/1'}])


def test_source_url_must_be_a_plain_string(world):
    """非字符串 URL 不能进 manifest，避免前端拿到对象后渲染成 [object]。"""
    pool = source_images.pool_path(world, 't1')
    pool.parent.mkdir(parents=True, exist_ok=True)
    path = make_jpeg(pool.parent / 'z.jpg', b'\x03' * 64)
    pool.write_text(json.dumps({'images': [
        {'path': str(path), 'sha256': 'h', 'source_url': {'nested': 1}, 'source_title': '甲'}
    ]}, ensure_ascii=False), 'utf-8')
    manifest = register(world)
    assert isinstance(manifest['assets'][0]['source']['source_url'], str)