"""视觉生产引擎 V1.0 新增能力的隔离测试：结构化任务、候选管理、策划提案、中文排版。

背景（2026-10-15 审计）：旧代码 image_prompts 为扁平字符串、每平台硬编码单一尺寸、
最多 3 张、manifest ready 即冻结无单图重生成。本文件锁定修复后的行为。
"""
import json

import httpx
import pytest

from backend.app import config, image_assets, typeset, visual_planner
from backend.app.config import Settings

from backend.tests.test_image_assets import multi_comfy, png, setup, work  # noqa: F401


# ---------- 结构化图片任务 ----------

def structured_work(platform='wechat'):
    return {**work(), 'platform': platform,
            'image_tasks': [
                {'prompt': 'Cover art of local AI workstation', 'page_role': 'cover', 'aspect': '2.35:1'},
                {'prompt': 'Body art explaining deployment', 'page_role': 'body'},
            ]}


def test_structured_tasks_drive_per_image_sizes(setup, monkeypatch):  # noqa: F811
    _, submitted = multi_comfy(monkeypatch)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', structured_work())
    assert result['status'] == 'ready' and result['requested_count'] == 2
    assert [job['page_role'] for job in result['generation_jobs']] == ['cover', 'body']
    assert [graph['4']['inputs']['width'] for graph in submitted] == [1088, 768]
    assert [graph['4']['inputs']['height'] for graph in submitted] == [464, 432]
    assert [(task['page_role'], task['width'], task['height']) for task in result['tasks']] == [('cover', 1088, 464), ('body', 768, 432)]


def test_legacy_xiaohongshu_prompts_now_use_spec_aspect(setup, monkeypatch):  # noqa: F811
    """回归 2026-10-15 审计：小红书旧默认 512x768（2:3）与规范 3:4 不符。"""
    _, submitted = multi_comfy(monkeypatch)
    content = {**work(), 'platform': 'xiaohongshu', 'image_prompts': ['Cover concept']}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert result['status'] == 'ready'
    assert (submitted[0]['4']['inputs']['width'], submitted[0]['4']['inputs']['height']) == (768, 1024)
    assert result['tasks'][0]['aspect'] == '3:4'


@pytest.mark.parametrize('bad', [
    {'prompt': '   '},
    {'prompt': 'ok', 'page_role': 'hero'},
    {'prompt': 'ok', 'aspect': '21:9'},
    {'prompt': 'ok', 'width': 500, 'height': 500},
    'not-a-dict',
])
def test_invalid_structured_tasks_fail_closed_before_any_submission(setup, monkeypatch, bad):  # noqa: F811
    contacted = []

    def handler(request):
        contacted.append(str(request.url))
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs))
    content = structured_work()
    content['image_tasks'][1] = bad
    with pytest.raises(image_assets.ImageAssetError):
        image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert contacted == []


def test_structured_tasks_participate_in_version_binding(setup, monkeypatch):  # noqa: F811
    multi_comfy(monkeypatch)
    content = structured_work()
    image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    changed = {**content, 'image_tasks': [
        {'prompt': 'Different cover', 'page_role': 'cover', 'aspect': '2.35:1'},
        {'prompt': 'Body art explaining deployment', 'page_role': 'body'}]}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', changed)
    assert result['status'] == 'blocked' and '新版本' in result['error']


# ---------- 单图重生成与候选管理 ----------

def test_regenerate_replaces_selected_and_keeps_old_candidate(setup, monkeypatch):  # noqa: F811
    _, submitted = multi_comfy(monkeypatch)
    content = structured_work()
    first = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert first['status'] == 'ready' and len(submitted) == 2
    old_body = first['assets'][1]
    assert (submitted[1]['4']['inputs']['width'], submitted[1]['4']['inputs']['height']) == (768, 432)

    regen = image_assets.regenerate_asset(setup[0], 'topic-1', 'version-1', 'wechat', 1)
    assert regen['status'] == 'generating'
    assert regen['assets'][1]['id'] == old_body['id']  # 重生成期间旧图仍选定，不出现空位

    second = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert second['status'] == 'ready' and len(submitted) == 3
    assert second['assets'][0]['id'] == first['assets'][0]['id']  # 未重生成的位不受影响
    new_body = second['assets'][1]
    assert new_body['id'] != old_body['id'] and new_body['sha256'] != old_body['sha256']
    assert second['selected']['1'] == new_body['id']
    assert [item['id'] for item in second['candidates']['1']] == [old_body['id'], new_body['id']]

    # 回退到旧候选
    rolled = image_assets.select_asset(setup[0], 'topic-1', 'version-1', 'wechat', 1, old_body['id'])
    assert rolled['status'] == 'ready'
    assert rolled['assets'][1]['id'] == old_body['id'] and rolled['selected']['1'] == old_body['id']
    assert image_assets.get_asset_file(setup[0], 'topic-1', 'version-1', 'wechat', old_body['id']).is_file()

    # 越界与非法候选拒绝
    with pytest.raises(image_assets.ImageAssetError):
        image_assets.regenerate_asset(setup[0], 'topic-1', 'version-1', 'wechat', 5)
    with pytest.raises(image_assets.ImageAssetError, match='候选'):
        image_assets.select_asset(setup[0], 'topic-1', 'version-1', 'wechat', 0, old_body['id'])


# ---------- 设置项 ----------

def test_settings_expose_and_validate_visual_engine_fields(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    settings = Settings(runtime_dir=runtime)
    assert settings.comfy_base_url == 'http://127.0.0.1:8188'
    assert settings.image_checkpoint_name == '' and settings.image_max_assets == 5
    with pytest.raises(ValueError, match='回环'):
        config._require_loopback_url('https://example.com', 'ComfyUI 地址')
    with pytest.raises(ValueError, match='回环'):
        config._require_loopback_url('http://user:pass@127.0.0.1:8188', 'ComfyUI 地址')
    assert config._require_loopback_url('http://127.0.0.1:8188', 'ComfyUI 地址') is None


def test_preflight_uses_configured_checkpoint(setup, monkeypatch):  # noqa: F811
    multi_comfy(monkeypatch)
    settings = Settings(runtime_dir=setup[0].runtime_dir, image_checkpoint_name='missing-model.safetensors')
    result = image_assets.probe_comfy(settings)
    assert result['available'] and not result['compatible']  # 配置了不存在的模型必须报 incompatible


# ---------- 视觉策划引擎 ----------

def planner_comfy(monkeypatch, content, status=200):
    original = httpx.Client

    def handler(request):
        if request.url.path == '/api/v1/models':
            return httpx.Response(200, json={'models': [{'type': 'llm', 'key': 'test-model', 'loaded_instances': [{'id': 'test-model'}]}]})
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(status, json={'choices': [{'message': {'content': content}}]})

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def settings_with_model(tmp_path):
    return Settings(runtime_dir=tmp_path / 'runtime', local_base_url='http://127.0.0.1:1234/v1', local_model='test-model')


def test_planner_parses_model_json_with_fences_and_forces_cover(tmp_path, monkeypatch):
    content = "好的，方案如下：\n```json\n{\"tasks\":[{\"prompt\":\"cover art\",\"page_role\":\"cover\",\"aspect\":\"2.35:1\"},{\"prompt\":\"body art\",\"page_role\":\"body\"}]}\n```"
    planner_comfy(monkeypatch, content)
    proposal = visual_planner.plan_image_tasks(settings_with_model(tmp_path), 'wechat', '本地AI部署', '正文' * 100)
    assert proposal['source'] == 'local_model' and proposal['model'] == 'test-model'
    assert [task['page_role'] for task in proposal['tasks']] == ['cover', 'body']
    assert proposal['tasks'][0]['aspect'] == '2.35:1' and proposal['tasks'][1]['aspect'] == '16:9'
    assert all(task['prompt'] and task['prompt'] == task['prompt'].strip() for task in proposal['tasks'])


def test_planner_falls_back_to_heuristic_on_garbage(tmp_path, monkeypatch):
    planner_comfy(monkeypatch, '这不是JSON')
    proposal = visual_planner.plan_image_tasks(settings_with_model(tmp_path), 'wechat', '标题', '正文' * 100)
    assert proposal['source'] == 'heuristic'
    assert any('策划未完成' in warning for warning in proposal['warnings'])
    assert proposal['tasks'][0]['page_role'] == 'cover'


def test_planner_without_model_returns_heuristic_directly(tmp_path, monkeypatch):
    from backend.app import adapters
    monkeypatch.setattr(adapters, '_loaded_local_model', lambda _: '')
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('no completion without loaded model'))
    proposal = visual_planner.plan_image_tasks(Settings(runtime_dir=tmp_path / 'runtime'), 'xiaohongshu', '标题', '正文')
    assert proposal['source'] == 'heuristic'
    assert all(task['aspect'] == '3:4' for task in proposal['tasks'])


def test_heuristic_uses_wechat_cover_aspect():
    tasks = visual_planner.heuristic_tasks('wechat', '免费部署本地AI', '正文' * 800)
    assert tasks[0]['aspect'] == '2.35:1' and len(tasks) == 2
    assert visual_planner.heuristic_tasks('wechat', '短文', '短')[0]['aspect'] == '2.35:1'
    assert len(visual_planner.heuristic_tasks('wechat', '短文', '短')) == 1


# ---------- 中文排版 ----------

def test_typeset_overlays_band_and_preserves_size(tmp_path):
    from io import BytesIO
    from PIL import Image
    source = png(320, 180)
    output = typeset.typeset_image(source, title='免费部署本地 AI 的完整路径', account_label='示例工作室', page_label='1/3')
    with Image.open(BytesIO(output)) as image:
        assert image.size == (320, 180) and image.format == 'PNG'
    assert output != source
    with pytest.raises(ValueError):
        typeset.typeset_image(source, title='   ')


def test_typeset_asset_caches_by_content_hash(setup):  # noqa: F811
    from PIL import Image
    from io import BytesIO
    import hashlib
    setup[1].write_bytes(png(320, 180))  # 排版需要 ≥64px 的真实图
    manifest = image_assets.register_assets(setup[0], 'topic-1', 'version-1', 'wechat', [setup[1]], ['用途说明'])
    asset_id = manifest['assets'][0]['id']
    first = typeset.typeset_asset(setup[0], 'topic-1', 'version-1', 'wechat', asset_id, title='标题一')
    second = typeset.typeset_asset(setup[0], 'topic-1', 'version-1', 'wechat', asset_id, title='标题一')
    other = typeset.typeset_asset(setup[0], 'topic-1', 'version-1', 'wechat', asset_id, title='标题二')
    assert first == second and first != other
    assert first.is_file() and first.stat().st_size > 0
    with Image.open(BytesIO(first.read_bytes())) as image:
        assert image.format == 'PNG'
    # 不同参数生成不同缓存文件，原始资产未被改动
    assert hashlib.sha256(setup[1].read_bytes()).hexdigest() == manifest['assets'][0]['sha256']
