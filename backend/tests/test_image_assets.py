"""Real raster fixtures in tmp directories; no external service or image I/O."""
import hashlib
import json
from pathlib import Path
from io import BytesIO
import struct
from types import SimpleNamespace
import zlib

import httpx
import pytest
from PIL import Image

from backend.app import image_assets
from backend.app.config import Settings


def png(width=8, height=8):
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    pixels = zlib.compress((b'\x00' + bytes([24, 120, 100, 255]) * width) * height)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', pixels) + chunk(b'IEND', b'')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    source = tmp_path / 'tool-output.png'
    source.write_bytes(png())
    monkeypatch.setattr(image_assets, '_discover_material_candidates', lambda *args: {'checked_at': 'isolated-fixture', 'candidates': [], 'error': None})
    return Settings(runtime_dir=runtime), source


def register(setup, **kwargs):
    settings, source = setup
    return image_assets.register_assets(settings, 'topic-1', 'version-1', 'wechat',
                                      [source], ['真实工具输出的用途说明'], allowed_source_roots=[source.parent], **kwargs)


def test_register_copies_real_bytes_and_binds_version_hash_and_ai_disclosure(setup):
    settings, source = setup
    result = register(setup, source={'kind': 'generated', 'version_hash': 'frozen-version-hash'})
    asset = result['assets'][0]
    assert result['status'] == 'ready' and result['version_hash'] == 'frozen-version-hash'
    assert asset['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(asset['path']).is_relative_to(settings.runtime_dir / 'assets' / 'topic-1' / 'version-1' / 'wechat')
    assert asset['width'] == asset['height'] == 8 and asset['mime_type'] == 'image/png'
    assert asset['is_ai_generated'] is True and asset['depicts_verified_event'] is False
    assert '非真实事件现场照片' in asset['caption'] and asset['review_required'] is True
    source.unlink()
    assert image_assets.get_asset_manifest(settings, 'topic-1', 'version-1', 'wechat')['status'] == 'ready'


def test_registered_licensed_material_is_preferred_without_comfy_network(setup, monkeypatch):
    manifest = register(setup, provider='user_material', source={'kind': 'material', 'rights_confirmed': True,
                        'license': 'CC0', 'license_url': 'https://example.invalid/license', 'authorization_id': 'trusted-ui-selection-1'})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('registered material must avoid Comfy'))
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', {'platform': 'wechat'})
    assert result == manifest and result['assets'][0]['is_ai_generated'] is False


@pytest.mark.parametrize('source', [{}, {'kind': 'material', 'license': 'unknown'},
                                  {'kind': 'generated'}, {'license': 'CC0', 'rights_confirmed': True},
                                  {'license': [], 'rights_confirmed': True, 'authorization_id': 'x'}])
def test_unknown_rights_and_arbitrary_provider_cannot_be_auto_imported(setup, source):
    with pytest.raises(image_assets.ImageAssetError, match='授权'):
        register(setup, provider='material', source=source)
    assert not (setup[0].runtime_dir / 'assets' / 'topic-1' / 'version-1' / 'wechat' / 'manifest.json').exists()


def test_registered_version_images_cannot_be_silently_replaced(setup):
    previous = register(setup)
    setup[1].write_bytes(png(16, 8))
    with pytest.raises(image_assets.ImageAssetError, match='新版本'):
        register(setup)
    assert image_assets.get_asset_manifest(setup[0], 'topic-1', 'version-1', 'wechat') == previous


def test_modified_asset_fails_hash_readback_and_cannot_be_served(setup):
    result = register(setup)
    asset = result['assets'][0]
    Path(asset['path']).write_bytes(png(16, 8))
    assert image_assets.get_asset_manifest(setup[0], 'topic-1', 'version-1', 'wechat')['status'] == 'corrupt'
    with pytest.raises(image_assets.ImageAssetError, match='已验证'):
        image_assets.get_asset_file(setup[0], 'topic-1', 'version-1', 'wechat', asset['id'])


def test_asset_lookup_requires_exact_version_platform_and_id(setup):
    result = register(setup)
    asset = result['assets'][0]
    assert image_assets.get_asset_file(setup[0], 'topic-1', 'version-1', 'wechat', asset['id']).is_file()
    for version, platform, id_ in [('version-2', 'wechat', asset['id']), ('version-1', 'toutiao', asset['id']),
                                  ('version-1', 'wechat', '../anything'), ('version-1', 'wechat', '0' * 24)]:
        with pytest.raises(image_assets.ImageAssetError):
            image_assets.get_asset_file(setup[0], 'topic-1', version, platform, id_)


@pytest.mark.parametrize('topic_id,version_id,platform', [('../escape', 'version-1', 'wechat'),
                                                       ('topic-1', '../../outside', 'wechat'),
                                                       ('topic-1', 'version-1', '../bad')])
def test_path_components_cannot_escape_runtime(setup, topic_id, version_id, platform):
    with pytest.raises(image_assets.ImageAssetError):
        image_assets.get_asset_manifest(setup[0], topic_id, version_id, platform)


def test_trusted_registration_rejects_outside_source_path_and_text_disguised_as_image(setup):
    settings, source = setup
    with pytest.raises(image_assets.ImageAssetError, match='授权'):
        image_assets.register_assets(settings, 'topic-1', 'version-1', 'wechat', [source], ['x'], allowed_source_roots=[settings.runtime_dir])
    source.write_text('This is only an image prompt, not an image.', 'utf-8')
    with pytest.raises(image_assets.ImageAssetError, match='不是支持'):
        register(setup)


def test_invalid_png_crc_is_not_a_real_asset(setup):
    data = bytearray(png())
    data[30] ^= 1
    setup[1].write_bytes(data)
    with pytest.raises(image_assets.ImageAssetError, match='校验'):
        register(setup)


def fake_comfy(monkeypatch, *, offline=False, no_models=False, submit_timeout=False,
               invalid_output=False, unsafe_output=False, history_error=False, complete=True):
    requests = []
    original_client = httpx.Client
    catalog = {name: {'input': {'required': {}}} for name in ['CheckpointLoaderSimple', 'CLIPTextEncode', 'EmptyLatentImage', 'KSampler', 'VAEDecode', 'SaveImage']}
    catalog['CheckpointLoaderSimple']['input']['required']['ckpt_name'] = [[] if no_models else [image_assets.DEFAULT_CHECKPOINT]]
    catalog['KSampler']['input']['required'] = {'sampler_name': [['euler']], 'scheduler': [['normal']]}

    def handler(request):
        requests.append(request)
        if offline:
            raise httpx.ConnectError('offline fixture', request=request)
        route = request.url.path
        if route == '/system_stats':
            return httpx.Response(200, json={'system': {'comfyui_version': 'isolated-fixture'}})
        if route == '/object_info':
            return httpx.Response(200, json=catalog)
        if route == '/prompt':
            if submit_timeout:
                raise httpx.ReadTimeout('uncertain fixture response', request=request)
            body = json.loads(request.content)
            assert body['prompt']['7']['class_type'] == 'SaveImage'
            assert set(body['prompt']) == {str(i) for i in range(1, 8)}
            return httpx.Response(200, json={'prompt_id': 'prompt-fixture-1', 'node_errors': {}})
        if route == '/history/prompt-fixture-1':
            return httpx.Response(200, json={'prompt-fixture-1': {
                'status': {'completed': complete, 'status_str': 'error' if history_error else 'success'},
                'outputs': {'7': {'images': [{'filename': '../escape.png' if unsafe_output else 'result.png', 'subfolder': '', 'type': 'output'}]}}}})
        if route == '/view':
            return httpx.Response(200, content=b'only a prompt' if invalid_output else png(32, 24), headers={'content-type': 'image/png'})
        raise AssertionError(route)

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def work():
    return {'platform': 'wechat', 'title': '科技与社会选题', 'body': '固定版本正文', 'image_prompts': ['Editorial illustration of technology and society']}


def test_comfy_health_is_preflight_only_not_generation_verified(setup, monkeypatch):
    requests = fake_comfy(monkeypatch)
    result = image_assets.probe_comfy(setup[0])
    assert result['available'] and result['compatible'] and not result['generation_verified']
    assert all(request.method == 'GET' for request in requests)


def test_online_server_with_missing_models_is_distinct_from_available_generation(setup, monkeypatch):
    fake_comfy(monkeypatch, no_models=True)
    result = image_assets.probe_comfy(setup[0])
    assert result['available'] and not result['compatible'] and not result['generation_verified']


def test_comfy_actual_fixture_output_is_downloaded_hashed_and_version_bound(setup, monkeypatch):
    requests = fake_comfy(monkeypatch)
    progress = []
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work(), progress=progress.append)
    assert result['status'] == 'ready' and len(result['assets']) == 1
    assert result['prompt_id'] == 'prompt-fixture-1' and result['preflight']['generation_verified']
    assert result['assets'][0]['width'] == 32 and result['assets'][0]['height'] == 24
    assert result['assets'][0]['provider'] == 'comfyui' and result['assets'][0]['is_ai_generated']
    assert Path(result['assets'][0]['path']).read_bytes() == png(32, 24)
    assert progress[-1] == '实际图片已保存并通过文件与版本校验'
    assert sum(request.method == 'POST' for request in requests) == 1
    assert image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work()) == result
    assert sum(request.method == 'POST' for request in requests) == 1


def test_changed_work_cannot_reuse_existing_generated_version_assets(setup, monkeypatch):
    requests = fake_comfy(monkeypatch)
    image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    changed = {**work(), 'body': '正文已改却没有建立新版本'}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', changed)
    assert result['status'] == 'blocked'
    assert sum(request.method == 'POST' for request in requests) == 1


def test_uncertain_submit_is_not_blindly_repeated(setup, monkeypatch):
    requests = fake_comfy(monkeypatch, submit_timeout=True)
    first = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    second = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert first['status'] == second['status'] == 'unknown' and not first.get('prompt_id')
    assert first['assets'] == second['assets'] == []
    assert sum(request.method == 'POST' for request in requests) == 1


@pytest.mark.parametrize('mode', ['invalid_output', 'unsafe_output', 'history_error'])
def test_failed_or_unsafe_output_never_claims_ready(setup, monkeypatch, mode):
    requests = fake_comfy(monkeypatch, **{mode: True})
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert result['status'] == 'unknown' and result['assets'] == []
    assert result['prompt_id'] == 'prompt-fixture-1'
    if mode in {'unsafe_output', 'history_error'}:
        assert not any(request.url.path == '/view' for request in requests)


def test_offline_failure_and_model_paths_do_not_create_fake_assets(setup, monkeypatch):
    requests = fake_comfy(monkeypatch, offline=True)
    model_work = {**work(), 'image_path': str(setup[1]), 'image_candidates': [{'path': str(setup[1]), 'license': 'CC0'}]}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', model_work)
    assert result['status'] == 'failed' and result['assets'] == []
    assert all(request.method == 'GET' for request in requests)
    assert '尚未生成实际图片' in result['error']


def test_cancel_before_submit_makes_no_network_requests(setup, monkeypatch):
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('cancelled work cannot contact Comfy'))
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work(), cancelled=lambda: True)
    assert result['status'] == 'cancelled' and result['assets'] == []


def test_pending_task_with_known_id_resumes_same_history_without_resubmit(setup, monkeypatch):
    directory = image_assets._asset_directory(setup[0], 'topic-1', 'version-1', 'wechat')
    image_assets._atomic_json(directory / 'manifest.json', {'schema_version': 1, 'topic_id': 'topic-1',
        'version_id': 'version-1', 'platform': 'wechat', 'status': 'unknown', 'assets': [],
        'request_hash': image_assets.work_asset_hash(work()), 'prompt_id': 'prompt-fixture-1',
        'workflow_hash': 'prior-fixed-workflow', 'prompt': 'prior-fixed-prompt'})
    requests = fake_comfy(monkeypatch)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert result['status'] == 'ready' and result['prompt_id'] == 'prompt-fixture-1'
    assert all(request.method == 'GET' for request in requests)


def test_review_decision_is_not_overwritten_by_automatic_generation(setup, monkeypatch):
    directory = image_assets._asset_directory(setup[0], 'topic-1', 'version-1', 'wechat')
    previous = {'schema_version': 1, 'topic_id': 'topic-1', 'version_id': 'version-1', 'platform': 'wechat',
                'status': 'needs_review', 'assets': [], 'error': '待用户确认素材授权'}
    image_assets._atomic_json(directory / 'manifest.json', previous)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('review decision must be preserved'))
    assert image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work()) == previous


@pytest.mark.parametrize('url', ['https://example.com', 'http://127.0.0.1:8188/private',
                                'http://user:secret@127.0.0.1:8188', 'http://127.0.0.1:8188?next=https://example.com'])
def test_comfy_endpoint_rejects_remote_credentials_and_redirect_routes(setup, url):
    settings = SimpleNamespace(runtime_dir=setup[0].runtime_dir, comfy_base_url=url, task_timeout_seconds=30)
    with pytest.raises(image_assets.ImageAssetError, match='回环'):
        image_assets._comfy_url(settings)


def test_full_decoder_rejects_jpeg_header_without_actual_pixels(setup):
    setup[1].write_bytes(b'\xff\xd8\xff\xc0\x00\x11\x08\x00\x18\x00\x20\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xd9')
    with pytest.raises(image_assets.ImageAssetError, match='校验'):
        register(setup)


def test_real_jpeg_fixture_can_be_verified_and_registered(setup):
    stream = BytesIO()
    Image.new('RGB', (12, 9), (24, 100, 70)).save(stream, format='JPEG')
    setup[1].write_bytes(stream.getvalue())
    result = register(setup)
    assert result['assets'][0]['mime_type'] == 'image/jpeg'
    assert (result['assets'][0]['width'], result['assets'][0]['height']) == (12, 9)


def multi_comfy(monkeypatch, *, fail_submit_index=None, duplicate=False):
    requests = []
    original_client = httpx.Client
    catalog = {name: {'input': {'required': {}}} for name in ['CheckpointLoaderSimple', 'CLIPTextEncode', 'EmptyLatentImage', 'KSampler', 'VAEDecode', 'SaveImage']}
    catalog['CheckpointLoaderSimple']['input']['required']['ckpt_name'] = [[image_assets.DEFAULT_CHECKPOINT]]
    catalog['KSampler']['input']['required'] = {'sampler_name': [['euler']], 'scheduler': [['normal']]}
    submitted = []

    def handler(request):
        requests.append(request)
        route = request.url.path
        if route == '/system_stats':
            return httpx.Response(200, json={'system': {'comfyui_version': 'multi-fixture'}})
        if route == '/object_info':
            return httpx.Response(200, json=catalog)
        if route == '/prompt':
            payload = json.loads(request.content)
            submitted.append(payload['prompt'])
            index = len(submitted)
            if index == fail_submit_index:
                raise httpx.ReadTimeout('uncertain multi fixture', request=request)
            return httpx.Response(200, json={'prompt_id': f'multi-prompt-{index}', 'node_errors': {}})
        if route.startswith('/history/multi-prompt-'):
            prompt_id = route.split('/')[-1]
            index = int(prompt_id.rsplit('-', 1)[1])
            return httpx.Response(200, json={prompt_id: {'status': {'completed': True, 'status_str': 'success'},
                'outputs': {'7': {'images': [{'filename': f'output-{index}.png', 'subfolder': '', 'type': 'output'}]}}}})
        if route == '/view':
            index = int(request.url.params['filename'].removesuffix('.png').rsplit('-', 1)[1])
            return httpx.Response(200, content=png(32 if duplicate else 32 + index, 24))
        raise AssertionError('Unexpected network fixture route: ' + str(request.url))

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    return requests, submitted


def test_each_of_three_prompts_executes_once_with_bound_prompt_ids(setup, monkeypatch):
    requests, submitted = multi_comfy(monkeypatch)
    content = {**work(), 'image_prompts': ['First cover concept', 'Second body concept', 'Third body concept']}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert result['status'] == 'ready' and result['requested_count'] == result['produced_count'] == 3
    assert result['remaining_count'] == 0 and result['unprocessed_prompt_count'] == 0
    assert len(submitted) == 3 and [job['prompt_id'] for job in result['generation_jobs']] == ['multi-prompt-1', 'multi-prompt-2', 'multi-prompt-3']
    assert [asset['prompt_index'] for asset in result['assets']] == [0, 1, 2]
    assert all(expected in graph['2']['inputs']['text'] for expected, graph in zip(content['image_prompts'], submitted))
    image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert sum(request.method == 'POST' for request in requests) == 3


def test_excess_prompts_are_explicitly_not_executed(setup, monkeypatch):
    _, submitted = multi_comfy(monkeypatch)
    content = {**work(), 'image_prompts': [f'Concept {i}' for i in range(7)]}
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert result['total_prompt_count'] == 7 and result['requested_count'] == 5
    assert result['produced_count'] == 5 and result['unprocessed_prompt_count'] == 2
    assert len(submitted) == 5 and any('2 个提示词未执行' in warning for warning in result['warnings'])


def test_second_uncertain_submit_preserves_first_asset_and_never_resubmits(setup, monkeypatch):
    requests, submitted = multi_comfy(monkeypatch, fail_submit_index=2)
    content = {**work(), 'image_prompts': ['cover', 'body', 'third']}
    first = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert first['status'] == 'unknown' and first['produced_count'] == 1 and first['remaining_count'] == 2
    assert first['generation_jobs'][0]['status'] == 'ready'
    assert first['generation_jobs'][1]['status'] == 'unknown' and not first['generation_jobs'][1].get('prompt_id')
    assert image_assets.get_asset_file(setup[0], 'topic-1', 'version-1', 'wechat', first['assets'][0]['id']).is_file()
    second = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert second['status'] == 'unknown' and second['assets'] == first['assets']
    assert len(submitted) == 2 and sum(request.method == 'POST' for request in requests) == 2


def test_cancellation_after_first_preserves_file_and_resumes_only_missing_slots(setup, monkeypatch):
    _, submitted = multi_comfy(monkeypatch)
    content = {**work(), 'image_prompts': ['cover', 'body']}
    cancelled = [False]

    def progress(message):
        if message.startswith('第 1 / 2 张实际图片已保存'):
            cancelled[0] = True

    first = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content,
                                            cancelled=lambda: cancelled[0], progress=progress)
    assert first['status'] == 'cancelled' and first['produced_count'] == 1 and first['remaining_count'] == 1
    assert first['generation_jobs'][0]['status'] == 'ready' and len(submitted) == 1
    second = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', content)
    assert second['status'] == 'ready' and len(submitted) == 2
    assert second['assets'][0]['id'] == first['assets'][0]['id']


def test_duplicate_output_does_not_pretend_to_produce_two_different_images(setup, monkeypatch):
    _, submitted = multi_comfy(monkeypatch, duplicate=True)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', {**work(), 'image_prompts': ['cover', 'body']})
    assert result['status'] == 'needs_review' and result['produced_count'] == 1 and result['remaining_count'] == 1
    assert len(result['assets']) == 1 and len(submitted) == 2


def authorized_source():
    return {'kind': 'material', 'rights_confirmed': True, 'authorization_id': 'official-source-proof',
            'license': 'CC-BY-4.0', 'license_url': 'https://commons.wikimedia.org/wiki/License',
            'rights_holder': 'Fixture Author', 'credit': 'Fixture Author / Commons / CC-BY-4.0',
            'source_url': 'https://commons.wikimedia.org/wiki/File:Fixture.png', 'depicts_verified_event': False}


def material_candidate(id_='commons-fixture-1', *, eligible=True, exact=True):
    return {'id': id_, 'eligible': eligible, 'relevance': {'status': 'exact_subject' if exact else 'needs_review',
            'matched_subjects': ['Fixture Subject'] if exact else [], 'basis': 'isolated exact subject'},
            'review_reasons': [] if eligible and exact else ['许可或相关性需要审核']}


def install_material_fixture(setup, monkeypatch, *, eligible=True, exact=True, source=None):
    settings, _ = setup
    material = settings.runtime_dir / 'material.png'
    material.write_bytes(png(12, 10))
    record = material_candidate(eligible=eligible, exact=exact)
    downloads = []
    monkeypatch.setattr(image_assets, '_discover_material_candidates', lambda *args: {'checked_at': 'isolated-fixture', 'candidates': [dict(record)], 'error': None})

    def download(candidate_id, *_):
        downloads.append(candidate_id)
        return {'path': str(material), 'metadata': {}, 'source': source or authorized_source()}

    monkeypatch.setattr(image_assets, '_download_authorized_material', download)
    return downloads


def test_related_licensed_material_is_imported_before_any_comfy_request(setup, monkeypatch):
    downloads = install_material_fixture(setup, monkeypatch)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('enough authorized material must avoid Comfy'))
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert downloads == ['commons-fixture-1'] and result['status'] == 'ready'
    assert result['provider'] == 'authorized_material' and not result['assets'][0]['is_ai_generated']
    assert result['assets'][0]['credit'].startswith('Fixture Author')
    assert result['assets'][0]['source']['url'].startswith('https://commons.wikimedia.org/')
    assert result['material_search']['candidates'][0]['asset_id'] == result['assets'][0]['id']


@pytest.mark.parametrize('eligible,exact', [(False, True), (True, False)])
def test_uncertain_rights_or_subject_match_stays_for_review_and_falls_back_to_illustration(setup, monkeypatch, eligible, exact):
    downloads = install_material_fixture(setup, monkeypatch, eligible=eligible, exact=exact)
    fake_comfy(monkeypatch)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert downloads == [] and result['status'] == 'ready' and result['assets'][0]['is_ai_generated']
    assert result['material_review_required'] and len(result['material_search']['candidates']) == 1
    assert result['material_search']['candidates'][0]['eligible'] is eligible


def test_material_download_must_prove_license_not_merely_index_match(setup, monkeypatch):
    downloads = install_material_fixture(setup, monkeypatch, source={'kind': 'material', 'rights_confirmed': True, 'license': 'unknown'})
    fake_comfy(monkeypatch)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', work())
    assert downloads and result['assets'][0]['is_ai_generated']
    assert 'download_error' in result['material_search']['candidates'][0]


def test_partial_licensed_material_survives_local_generation_offline(setup, monkeypatch):
    install_material_fixture(setup, monkeypatch)
    fake_comfy(monkeypatch, offline=True)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', {**work(), 'image_prompts': ['cover', 'body']})
    assert result['status'] == 'failed' and result['produced_count'] == 1 and result['remaining_count'] == 1
    assert not result['assets'][0]['is_ai_generated']
    assert image_assets.get_asset_file(setup[0], 'topic-1', 'version-1', 'wechat', result['assets'][0]['id']).is_file()


def test_material_plus_generated_gap_records_both_sources_and_job_indexes(setup, monkeypatch):
    install_material_fixture(setup, monkeypatch)
    fake_comfy(monkeypatch)
    result = image_assets.generate_work_assets(setup[0], {'id': 'topic-1'}, 'version-1', {**work(), 'image_prompts': ['cover', 'body']})
    assert result['status'] == 'ready' and result['produced_count'] == result['requested_count'] == 2
    assert result['provider'] == 'mixed'
    assert [asset['is_ai_generated'] for asset in result['assets']] == [False, True]
    assert sorted(job['index'] for job in result['generation_jobs']) == [0, 1]
    assert next(job for job in result['generation_jobs'] if job['index'] == 1)['prompt_id'] == 'prompt-fixture-1'


@pytest.mark.parametrize('subfolder', ['workbench/version-1/wechat', 'workbench\\version-1\\wechat'])
def test_windows_backslash_subfolder_from_comfy_is_accepted(subfolder):
    """回归 2026-10-14 实际出图验收：ComfyUI 在 Windows 上把 SaveImage 的
    filename_prefix（"workbench/<version>/<platform>/<n>"）回读成反斜杠子目录。

    此前模拟数据一律使用空 subfolder，这条真实路径从未被覆盖；实际图片已经生成，
    却被路径校验判为无效，任务被静默记成 unknown，生成从未被确认。
    """
    seen = []

    def handler(request):
        seen.append(request.url.params['subfolder'])
        return httpx.Response(200, content=png(32, 24), headers={'content-type': 'image/png'})

    client = httpx.Client(base_url='http://127.0.0.1:8188', transport=httpx.MockTransport(handler))
    data = image_assets._download_output(client, {'filename': '1_00001_.png', 'subfolder': subfolder, 'type': 'output'})
    assert data.startswith(b'\x89PNG') and seen == [subfolder]


@pytest.mark.parametrize('subfolder', ['../escape', 'workbench/../../escape', 'workbench\\..\\escape', 'C:\\absolute', '/rooted', '\\\\server\\share'])
def test_unsafe_subfolder_is_rejected_before_any_request(subfolder):
    """放宽反斜杠支持后，路径穿越、盘符和绝对路径仍必须在发起请求前被拒绝。"""
    contacted = []

    def handler(request):
        contacted.append(str(request.url))
        return httpx.Response(200, content=png(), headers={'content-type': 'image/png'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(image_assets.ImageAssetError):
        image_assets._download_output(client, {'filename': '1_00001_.png', 'subfolder': subfolder, 'type': 'output'})
    assert contacted == []
