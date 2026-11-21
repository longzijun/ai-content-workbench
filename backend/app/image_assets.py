"""Version-bound real image assets from trusted registrations or local ComfyUI.

Model output cannot select local files or attest to material rights. Call
``register_assets`` only with paths obtained by a trusted tool or a user file
selection. It copies raster bytes into the runtime and verifies them on read.
``generate_work_assets`` never treats a prompt as an image, never installs or
starts a service, and never resubmits an uncertain generation request.

Comfy HTTP contracts: https://docs.comfy.org/development/comfyui-server/comms_routes
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from .config import PROJECT_ROOT
from . import platform_registry

PLATFORMS = platform_registry.PLATFORM_SET
COMFY_DEFAULT = 'http://127.0.0.1:8188'
DEFAULT_CHECKPOINT = 'juggernautXL_ragnarok.safetensors'
MAX_IMAGE_BYTES = 32 * 1024 * 1024
# 画幅白名单：尺寸必须取自本表，保证 SDXL 可稳定生成且与平台规范一致。
# '2.35:1' 公众号封面（1088x464≈2.35）、'3:4' 小红书、'16:9' 头条/内文。
ASPECT_SIZES = {
    '2.35:1': (1088, 464),
    '16:9': (768, 432),
    '3:4': (768, 1024),
    '9:16': (576, 1024),
    '1:1': (768, 768),
}
PAGE_ROLES = {'cover', 'body', 'card', 'concept', 'diagram'}
DEFAULT_CAP = 5
_lock = threading.RLock()


class ImageAssetError(ValueError):
    pass


class ImageCancelled(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _component(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value):
        raise ImageAssetError(f'{name} 不是有效的固定标识')
    return value


def _asset_directory(settings, topic_id, version_id, platform):
    _component(topic_id, 'topic_id')
    _component(version_id, 'version_id')
    if platform not in PLATFORMS:
        raise ImageAssetError('不支持的图片平台')
    runtime = Path(settings.runtime_dir).resolve()
    directory = runtime / 'assets' / topic_id / version_id / platform
    if not directory.resolve().is_relative_to(runtime):
        raise ImageAssetError('图片路径离开本项目 runtime')
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to((runtime / 'assets').resolve()):
        raise ImageAssetError('图片资产目录被重定向')
    return directory


def _atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(path)


def _image_metadata(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageAssetError('图片为空或超过 32 MB')
    raster_header = data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8') or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')
    if not raster_header:
        raise ImageAssetError('文件不是支持的 PNG、JPEG 或 WebP 图片')
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            format_name = image.format
            if format_name not in {'PNG', 'JPEG', 'WEBP'}:
                raise ImageAssetError('文件不是支持的 PNG、JPEG 或 WebP 图片')
            if not 1 <= width <= 16384 or not 1 <= height <= 16384 or width * height > 40_000_000:
                raise ImageAssetError('图片尺寸无效或过大')
            image.verify()
        # Header/CRC checks alone cannot prove a complete raster. Decode it too.
        with Image.open(BytesIO(data)) as image:
            image.load()
        mime, extension = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'), 'WEBP': ('image/webp', '.webp')}[format_name]
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ImageAssetError('图片解码或校验失败，未作为真实素材导入') from error
    return {'width': width, 'height': height, 'mime_type': mime, 'extension': extension,
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _allowed_source_roots(settings):
    roots = [PROJECT_ROOT.resolve(), Path(settings.runtime_dir).resolve(), Path(tempfile.gettempdir()).resolve()]
    profile = os.environ.get('USERPROFILE')
    if profile:
        roots.append((Path(profile) / '.codex' / 'visualizations').resolve())
    return roots


def _read_trusted_source(path, allowed_roots):
    original = Path(path)
    if not original.is_absolute():
        raise ImageAssetError('注册图片需要可信工具返回的绝对文件路径')
    resolved = original.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
        raise ImageAssetError('图片来源不在已授权的输出目录内')
    if not resolved.is_file() or resolved.stat().st_size > MAX_IMAGE_BYTES:
        raise ImageAssetError('图片文件不存在或超过 32 MB')
    data = resolved.read_bytes()
    return data, _image_metadata(data)


def _persist_bytes(directory, data, prompt, provider, source, index, asset_source=None):
    metadata = _image_metadata(data)
    image_id = metadata['sha256'][:24]
    path = directory / (image_id + metadata.pop('extension'))
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
        raise ImageAssetError('固定图片资产文件被改动')
    if not path.exists():
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
    generated = provider in {'codex_imagegen', 'comfyui'}
    # 逐图溯源：asset_source 覆盖批量级的 source。来源素材的图片往往来自
    # 不同文章，共享一份 source 会让每张图的来源链接都指错——这正是
    # 「图文对不上、来源按钮点不动」的根因。
    provenance = {**(source or {}), **(asset_source or {})}
    return {**metadata, 'id': image_id, 'path': str(path.resolve()), 'filename': path.name,
            'role': 'cover' if index == 0 else 'body', 'prompt': str(prompt)[:4000],
            'provider': provider, 'created_at': _now(), 'source': provenance,
            'is_ai_generated': generated, 'depicts_verified_event': False if generated else provenance.get('depicts_verified_event') is True,
            'caption': ('AI 生成示意图，非真实事件现场照片' if generated
                        else provenance.get('caption') or provenance.get('source_title') or ''),
            'license': provenance.get('license', 'provider_generation_terms' if generated else 'unknown'),
            'license_url': provenance.get('license_url', ''), 'rights_holder': provenance.get('rights_holder', ''),
            'rights_status': 'generated_terms_review' if generated else 'authorized', 'review_required': True,
            'credit': provenance.get('credit', ''), 'credit_required': provenance.get('credit_required') is True,
            'prompt_index': index}


def _base_manifest(topic_id, version_id, platform, provider, source):
    return {'schema_version': 2, 'topic_id': topic_id, 'version_id': version_id, 'platform': platform,
            'provider': provider, 'version_hash': source.get('version_hash'), 'request_hash': source.get('work_hash'), 'created_at': _now(),
            'updated_at': _now(), 'status': 'ready', 'assets': [], 'error': None, 'warnings': [],
            'candidates': {}, 'selected': {}}


def register_assets(settings, topic_id, version_id, platform, paths, prompts,
                    provider='codex_imagegen', source=None, *, allowed_source_roots=None,
                    asset_sources=None):
    """Internal trusted-file registration; never expose raw paths to a model API.

    Rights for existing material must be confirmed by a trusted user workflow.
    Unknown-rights files are not imported. Generated assets retain their AI
    disclosure and a user review requirement; they are not event photographs.

    asset_sources 与 paths 同序，每项是该图专属的溯源信息（原文链接、标题、
    权利人）。缺省则全部沿用批量级 source。来源素材通常横跨多篇文章，
    不给逐图溯源就会让每张图的来源链接都指错。
    """
    source = dict(source or {'kind': 'generated' if provider in {'codex_imagegen', 'comfyui'} else 'material'})
    generated = provider in {'codex_imagegen', 'comfyui'}
    if not generated and not (source.get('rights_confirmed') is True and isinstance(source.get('license'), str) and source['license'].strip() not in {'', 'unknown'} and isinstance(source.get('authorization_id'), str) and source['authorization_id'].strip()):
        raise ImageAssetError('素材许可或用户授权未确认，不能自动导入')
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 18:
        raise ImageAssetError('需要 1 到 18 个真实图片文件')
    if not isinstance(prompts, (list, tuple)) or len(prompts) != len(paths):
        raise ImageAssetError('每个实际图片文件必须绑定一个提示词或素材用途说明')
    if asset_sources is not None:
        if not isinstance(asset_sources, (list, tuple)) or len(asset_sources) != len(paths):
            raise ImageAssetError('逐图溯源信息必须与图片数量一一对应')
        asset_sources = [dict(item) if isinstance(item, dict) else {} for item in asset_sources]
    allowed = [Path(p) for p in allowed_source_roots] if allowed_source_roots is not None else _allowed_source_roots(settings)
    # Verify all inputs before copying any image or replacing a ready manifest.
    verified = [_read_trusted_source(path, allowed)[0] for path in paths]
    with _lock:
        directory = _asset_directory(settings, topic_id, version_id, platform)
        previous = get_asset_manifest(settings, topic_id, version_id, platform)
        if previous['status'] == 'ready':
            hashes = [hashlib.sha256(data).hexdigest() for data in verified]
            if hashes != [asset['sha256'] for asset in previous['assets']] or (source.get('version_hash') and previous.get('version_hash') != source['version_hash']):
                raise ImageAssetError('已有固定版本图片不能被替换，请建立新版本')
            return previous
        if previous['status'] in {'needs_review', 'blocked'}:
            raise ImageAssetError('该版本已有图片审核决定，请先处理审核或建立新版本')
        manifest = _base_manifest(topic_id, version_id, platform, provider, source)
        manifest['assets'] = [_persist_bytes(directory, data, prompts[index], provider, source, index,
                                              asset_sources[index] if asset_sources else None)
                              for index, data in enumerate(verified)]
        manifest.update(requested_count=len(verified), produced_count=len(verified), remaining_count=0,
                        total_prompt_count=len(prompts), unprocessed_prompt_count=0)
        manifest['warnings'] = ['AI 图像仅为示意，审核时需检查内容、文字与披露。'] if generated else ['请审核素材授权范围、署名与事件说明。']
        _atomic_json(directory / 'manifest.json', manifest)
        return get_asset_manifest(settings, topic_id, version_id, platform)


def get_asset_manifest(settings, topic_id, version_id, platform):
    directory = _asset_directory(settings, topic_id, version_id, platform)
    path = directory / 'manifest.json'
    if not path.exists():
        return {'schema_version': 1, 'topic_id': topic_id, 'version_id': version_id, 'platform': platform,
                'status': 'not_started', 'assets': [], 'error': None, 'warnings': []}
    try:
        manifest = json.loads(path.read_text('utf-8'))
        if not isinstance(manifest, dict) or not isinstance(manifest.get('assets', []), list):
            raise ImageAssetError('图片清单结构无效')
        if any(manifest.get(key) != value for key, value in [('topic_id', topic_id), ('version_id', version_id), ('platform', platform)]):
            raise ImageAssetError('图片清单与固定版本不匹配')
        for asset in manifest.get('assets', []):
            if not isinstance(asset, dict):
                raise ImageAssetError('图片清单记录无效')
            local = Path(asset['path']).resolve()
            if not local.is_relative_to(directory.resolve()) or not local.is_file():
                raise ImageAssetError('实际图片文件缺失或离开固定版本目录')
            if local.stat().st_size > MAX_IMAGE_BYTES:
                raise ImageAssetError('实际图片文件超过允许大小')
            data = local.read_bytes()
            metadata = _image_metadata(data)
            if metadata['sha256'] != asset.get('sha256'):
                raise ImageAssetError('实际图片与冻结校验值不一致')
        if manifest.get('status') == 'ready' and not manifest.get('assets'):
            raise ImageAssetError('图片清单没有实际图片')
        if manifest.get('status') == 'ready' and isinstance(manifest.get('requested_count'), int) and len(manifest['assets']) < manifest['requested_count']:
            raise ImageAssetError('图片清单尚未完成所请求数量，不能标为全部就绪')
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'schema_version': 1, 'topic_id': topic_id, 'version_id': version_id, 'platform': platform,
                'status': 'corrupt', 'assets': [], 'error': f'图片资产无法验证：{error}', 'warnings': []}


def _comfy_url(settings):
    value = str(getattr(settings, 'comfy_base_url', COMFY_DEFAULT)).rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ImageAssetError('ComfyUI 必须使用本机回环地址')
    return value


def _json_get(client, path):
    response = client.get(path)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ImageAssetError('本机 ComfyUI 返回了无效结构')
    return value


def _preflight(client, settings):
    stats = _json_get(client, '/system_stats')
    nodes = _json_get(client, '/object_info')
    needed = {'CheckpointLoaderSimple', 'CLIPTextEncode', 'EmptyLatentImage', 'KSampler', 'VAEDecode', 'SaveImage'}
    missing = needed - nodes.keys()
    model = str(getattr(settings, 'image_checkpoint_name', '') or DEFAULT_CHECKPOINT)
    models = nodes.get('CheckpointLoaderSimple', {}).get('input', {}).get('required', {}).get('ckpt_name', [[]])[0]
    sampler = nodes.get('KSampler', {}).get('input', {}).get('required', {})
    if missing or model not in models or 'euler' not in sampler.get('sampler_name', [[]])[0] or 'normal' not in sampler.get('scheduler', [[]])[0]:
        raise ImageAssetError('本机 ComfyUI 缺少已支持的节点、采样器或指定 checkpoint；服务在线不等于可以出图')
    return {'available': True, 'compatible': True, 'checkpoint': model,
            'version': stats.get('system', {}).get('comfyui_version', ''),
            'workflow': 'workbench_checkpoint_v1', 'generation_verified': False}


def probe_comfy(settings):
    """Read-only preflight; successful health is never reported as generated."""
    available = False
    try:
        with httpx.Client(base_url=_comfy_url(settings), timeout=8, trust_env=False, follow_redirects=False) as client:
            _json_get(client, '/system_stats')
            available = True
            return _preflight(client, settings)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {'available': available, 'compatible': False, 'generation_verified': False,
                'error': 'ComfyUI 不可达或缺少可执行节点/模型，尚未生成实际图片'}


def _prompt_graph(prompt, checkpoint, seed, prefix, width, height):
    return {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': checkpoint}},
        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt, 'clip': ['1', 1]}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'documentary event photo, fake news photo, text, logos, watermark, blurry', 'clip': ['1', 1]}},
        '4': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '5': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'seed': seed, 'steps': 20,
                                               'cfg': 7, 'sampler_name': 'euler', 'scheduler': 'normal',
                                               'positive': ['2', 0], 'negative': ['3', 0], 'latent_image': ['4', 0], 'denoise': 1}},
        '6': {'class_type': 'VAEDecode', 'inputs': {'samples': ['5', 0], 'vae': ['1', 2]}},
        '7': {'class_type': 'SaveImage', 'inputs': {'images': ['6', 0], 'filename_prefix': prefix}},
    }


def _download_output(client, descriptor):
    filename, subfolder, kind = descriptor.get('filename'), descriptor.get('subfolder', ''), descriptor.get('type')
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename or filename in {'.', '..'} or kind != 'output':
        raise ImageAssetError('ComfyUI 输出文件名或类型无效')
    # ComfyUI on Windows returns the SaveImage subfolder with backslashes
    # (filename_prefix "workbench/<version>/<platform>/<n>" comes back as
    # "workbench\\<version>\\<platform>"). Accept both separators, but still
    # reject absolute paths, drive letters and any ".." segment.
    if not isinstance(subfolder, str) or ':' in subfolder:
        raise ImageAssetError('ComfyUI 输出子目录无效')
    normalized = subfolder.replace('\\', '/')
    segments = [piece for piece in normalized.split('/') if piece not in ('', '.')]
    if normalized.startswith('/') or any(piece == '..' for piece in segments):
        raise ImageAssetError('ComfyUI 输出子目录无效')
    with client.stream('GET', '/view', params={'filename': filename, 'subfolder': subfolder, 'type': 'output'}) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ImageAssetError('ComfyUI 图片输出超过 32 MB')
            chunks.append(chunk)
    data = b''.join(chunks)
    _image_metadata(data)
    return data


def _read_prompt_output(client, prompt_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', prompt_id):
        raise ImageAssetError('ComfyUI prompt_id 无效')
    history = _json_get(client, '/history/' + prompt_id)
    entry = history.get(prompt_id)
    if not entry:
        return None
    status = entry.get('status', {})
    if status.get('status_str') == 'error':
        raise ImageAssetError('ComfyUI 执行失败或中断，未取得可用图片')
    # Only the SaveImage node we submitted may provide an output file.
    images = entry.get('outputs', {}).get('7', {}).get('images', [])
    if status.get('completed') is True:
        if not images:
            raise ImageAssetError('ComfyUI 已结束但没有实际图片输出')
        return _download_output(client, images[0])
    return None


def _task_size(task, platform):
    """画幅解析：显式 aspect → 白名单尺寸；显式宽高 → 必须等于白名单中的某个尺寸。"""
    aspect = task.get('aspect')
    if aspect is not None:
        if aspect not in ASPECT_SIZES:
            raise ImageAssetError(f'不支持的图片画幅比例：{aspect}')
        return ASPECT_SIZES[aspect]
    width, height = task.get('width'), task.get('height')
    if width is not None or height is not None:
        for value in (width, height):
            if type(value) is not int or value % 8 or not 256 <= value <= 1536:
                raise ImageAssetError('图片宽高需为 8 的倍数且在 256 到 1536 之间')
        if (width, height) not in ASPECT_SIZES.values():
            raise ImageAssetError('图片宽高必须使用受支持的画幅组合（2.35:1 / 16:9 / 3:4 / 9:16 / 1:1）')
        return width, height
    return ASPECT_SIZES['3:4'] if platform == 'xiaohongshu' else ASPECT_SIZES['16:9']


def resolve_image_tasks(work):
    """把版本里的图片请求解析成结构化任务列表。

    image_tasks（结构化，含用途/画幅）优先；旧版扁平 image_prompts 字符串
    按平台默认画幅包装（小红书 3:4，其余 16:9——修正旧代码 2:3/16:9 硬编码
    与规范不符的问题）。任务校验失败一律 fail-closed，不猜测。
    """
    raw = work.get('image_tasks')
    platform = str(work.get('platform') or '')
    if raw not in (None, []):
        if not isinstance(raw, list) or len(raw) > 50:
            raise ImageAssetError('图片任务列表无效')
        tasks = []
        for item in raw:
            if not isinstance(item, dict):
                raise ImageAssetError('每个图片任务需为对象')
            prompt = str(item.get('prompt') or '').strip()[:2000]
            if not prompt:
                raise ImageAssetError('每个图片任务需要非空提示词')
            role = item.get('page_role') or 'body'
            if role not in PAGE_ROLES:
                raise ImageAssetError(f'不支持的图片用途：{role}')
            width, height = _task_size(item, platform)
            tasks.append({'prompt': prompt, 'page_role': role,
                          'visual_type': str(item.get('visual_type') or '')[:60],
                          'style_id': str(item.get('style_id') or '')[:60],
                          'aspect': item.get('aspect'), 'width': width, 'height': height})
        return tasks
    prompts = [value.strip()[:2000] for value in work.get('image_prompts', [])
               if isinstance(value, str) and value.strip()] if isinstance(work.get('image_prompts'), list) else []
    default = '3:4' if platform == 'xiaohongshu' else '16:9'
    width, height = ASPECT_SIZES[default]
    return [{'prompt': prompt, 'page_role': 'body', 'visual_type': '', 'style_id': '',
             'aspect': default, 'width': width, 'height': height} for prompt in prompts]


def work_asset_hash(work):
    content = {key: work.get(key) for key in ('platform', 'title', 'body', 'image_prompts')}
    tasks = work.get('image_tasks')
    if tasks:
        # 结构化任务参与版本绑定；旧版 work 不携带该键，哈希保持不变。
        content['image_tasks'] = tasks
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def get_asset_file(settings, topic_id, version_id, platform, asset_id):
    """Return only an existing, verified asset from its exact version manifest."""
    if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{24}', asset_id):
        raise ImageAssetError('图片资产标识无效')
    manifest = get_asset_manifest(settings, topic_id, version_id, platform)
    if manifest['status'] not in {'ready', 'generating', 'unknown', 'failed', 'cancelled', 'needs_review'} or not manifest.get('assets'):
        raise ImageAssetError('该版本没有已验证的可用图片')
    matches = [asset for asset in manifest['assets'] if asset['id'] == asset_id]
    if not matches:
        raise ImageAssetError('该图片不属于当前固定版本与平台')
    return Path(matches[0]['path'])


def _discover_material_candidates(topic, work):
    try:
        from .material_sources import discover_material_candidates
        return discover_material_candidates(topic, work)
    except ImportError:
        return {'checked_at': _now(), 'candidates': [], 'error': '相关素材检索模块尚未就绪'}


def _download_authorized_material(candidate_id, settings, topic, work):
    from .material_sources import download_authorized_candidate
    return download_authorized_candidate(candidate_id, settings, topic, work)


def _save_generation_progress(directory, base):
    base['produced_count'] = len(base.get('assets', []))
    base['remaining_count'] = max(0, base['requested_count'] - base['produced_count'])
    base['updated_at'] = _now()
    _atomic_json(directory / 'manifest.json', base)


def _install_asset(base, index, asset):
    """安装一张图片到指定位：assets 恒为每 index 一张（当前选定），历史版本进 candidates。"""
    selected = base.setdefault('selected', {})
    candidates = base.setdefault('candidates', {})
    if not isinstance(selected, dict) or not isinstance(candidates, dict):
        raise ImageAssetError('候选版本记录无效')
    group = candidates.setdefault(str(index), [])
    if not any(candidate['id'] == asset['id'] for candidate in group):
        group.append(dict(asset))
    base['assets'] = [item for item in base['assets'] if item['prompt_index'] != index]
    base['assets'].append(asset)
    selected[str(index)] = asset['id']


def _known_hashes(base):
    hashes = {asset['sha256'] for asset in base['assets']}
    candidates = base.get('candidates') or {}
    if isinstance(candidates, dict):
        for group in candidates.values():
            if isinstance(group, list):
                hashes.update(candidate['sha256'] for candidate in group if isinstance(candidate, dict) and candidate.get('sha256'))
    return hashes


def _prepare_materials(settings, topic, work, directory, base, cancelled, progress):
    if 'material_search' not in base:
        progress('优先检索相关素材，确认许可与主题匹配后才使用')
        try:
            result = _discover_material_candidates(topic, work)
            if not isinstance(result, dict) or not isinstance(result.get('candidates'), list):
                raise ImageAssetError('素材检索结构无效')
            base['material_search'] = {**result, 'candidates': result['candidates'][:20]}
        except Exception:
            base['material_search'] = {'checked_at': _now(), 'candidates': [], 'error': '相关素材检索未完成，未用未知版权图片替代'}
        _save_generation_progress(directory, base)
    if base['material_search'].get('error'):
        warning = '相关素材检索未完成；候选与失败记录保留，缺口尝试生成 AI 示意图。'
        if warning not in base['warnings']:
            base['warnings'].append(warning)
    pending_candidates = [item for item in base['material_search'].get('candidates', []) if isinstance(item, dict)]
    for candidate in pending_candidates:
        if len(base['assets']) >= base['requested_count'] or cancelled():
            break
        relevance = candidate.get('relevance', {})
        if candidate.get('eligible') is not True or not isinstance(relevance, dict) or relevance.get('status') != 'exact_subject' or not relevance.get('matched_subjects'):
            continue
        if candidate.get('asset_id'):
            continue
        # A generation already submitted for a slot must be reconciled first.
        occupied = {asset['prompt_index'] for asset in base['assets']}
        occupied.update(job['index'] for job in base['generation_jobs']
                        if job.get('prompt_id') or job.get('status') in {'generating', 'unknown'} or job.get('regenerate'))
        index = next((value for value in range(base['requested_count']) if value not in occupied), None)
        if index is None:
            break
        try:
            download = _download_authorized_material(candidate['id'], settings, topic, work)
            source = dict(download['source'])
            if source.get('kind') != 'material' or source.get('rights_confirmed') is not True or not isinstance(source.get('license'), str) or source['license'].strip().casefold() in {'', 'unknown', 'unverified', 'pending'} or not isinstance(source.get('authorization_id'), str) or not source['authorization_id'].strip():
                raise ImageAssetError('下载素材缺少明确许可证明')
            data, metadata = _read_trusted_source(download['path'], [Path(settings.runtime_dir)])
            if any(asset['sha256'] == metadata['sha256'] for asset in base['assets']):
                candidate['download_error'] = '与已准备素材重复，未重复计入图片数量'
                continue
            source.setdefault('url', source.get('source_url', ''))
            asset = _persist_bytes(directory, data, '相关且许可明确的来源素材', 'authorized_material', source, index)
            asset['material_candidate_id'] = candidate['id']
            _install_asset(base, index, asset)
            candidate['asset_id'] = asset['id']
            base['generation_jobs'] = [job for job in base['generation_jobs'] if job['index'] != index]
            base['generation_jobs'].append({'index': index, 'status': 'ready', 'provider': 'authorized_material', 'asset_id': asset['id'], 'error': None})
            _save_generation_progress(directory, base)
            progress(f'已准备第 {len(base["assets"])} / {base["requested_count"]} 张授权来源图片')
        except Exception:
            candidate['download_error'] = '相关素材下载或许可校验未完成，未作为已授权图片导入'
            _save_generation_progress(directory, base)
    review = [item for item in pending_candidates if not item.get('asset_id') and (item.get('review_reasons') or item.get('eligible') is not True or item.get('download_error'))]
    base['material_review_required'] = bool(review)
    if review:
        warning = '检索候选中有相关性、许可或下载待核对事项；未自动使用这些候选，缺口使用 AI 示意图。'
        if warning not in base['warnings']:
            base['warnings'].append(warning)
    _save_generation_progress(directory, base)


def generate_work_assets(settings, topic, version_id, work, cancelled: Callable[[], bool] | None = None,
                         progress: Callable[[str], None] | None = None):
    """Material-first image preparation with durable per-prompt execution.

    Native Codex ImageGen is available through the interactive Codex tool, not
    an HTTP model API that this worker can fabricate. Tool outputs use trusted
    ``register_assets``. Unregistered gaps use only the configured local Comfy.
    At most ``image_max_assets`` prompts run per request; unexecuted excess
    prompts are explicit. Partial files and candidate review evidence survive
    failure/cancellation.
    """
    if platform_registry.is_video_platform(work.get('platform')):
        raise ImageAssetError('视频脚本只保存拍摄计划与已登记素材，不执行图片或视频生成')
    cancelled, progress = cancelled or (lambda: False), progress or (lambda message: None)
    topic_id, platform = topic['id'], work['platform']
    with _lock:
        directory = _asset_directory(settings, topic_id, version_id, platform)
        manifest = get_asset_manifest(settings, topic_id, version_id, platform)
        request_hash = work_asset_hash(work)
        if manifest.get('request_hash') and manifest['request_hash'] != request_hash:
            return {**manifest, 'status': 'blocked', 'error': '图片请求与冻结版本正文不一致，请建立新版本'}
        if manifest['status'] in {'ready', 'corrupt', 'needs_review', 'blocked'}:
            return manifest
        if cancelled():
            return {**manifest, 'status': 'cancelled', 'error': '图片任务已取消，未提交新请求'}
        tasks = resolve_image_tasks(work)
        if not tasks:
            tasks = [{'prompt': 'Concept illustration of ' + str(work.get('title') or topic.get('title') or 'technology and society')[:1000],
                      'page_role': 'body', 'visual_type': '', 'style_id': '',
                      'aspect': '3:4' if platform == 'xiaohongshu' else '16:9',
                      'width': 0, 'height': 0}]
            tasks[0]['width'], tasks[0]['height'] = ASPECT_SIZES[tasks[0]['aspect']]
        cap = max(1, min(10, int(getattr(settings, 'image_max_assets', DEFAULT_CAP) or DEFAULT_CAP)))
        base = _base_manifest(topic_id, version_id, platform, 'comfyui', {'kind': 'generated'})
        base.update(manifest)
        base.setdefault('warnings', [])
        for key, fallback in (('candidates', {}), ('selected', {})):
            value = base.get(key)
            if not isinstance(value, dict):
                if manifest.get('status') in {'ready', 'generating'}:
                    return {**manifest, 'status': 'blocked', 'error': f'{key} 记录无效，请人工核对后重建版本'}
                base[key] = fallback
        base.update(request_hash=request_hash, requested_count=min(cap, len(tasks)), total_prompt_count=len(tasks),
                    unprocessed_prompt_count=max(0, len(tasks) - cap), tasks=tasks[:min(cap, len(tasks))])
        if len(tasks) > cap:
            warning = f'当前单次最多准备 {cap} 张图片，另 {len(tasks) - cap} 个提示词未执行。'
            if warning not in base['warnings']:
                base['warnings'].append(warning)
        jobs = base.get('generation_jobs', [])
        if not isinstance(jobs, list) or any(not isinstance(job, dict) or type(job.get('index')) is not int or not 0 <= job['index'] < base['requested_count'] for job in jobs):
            return {**manifest, 'status': 'blocked', 'error': '逐图执行记录无法核对，未重新提交图片任务'}
        base['generation_jobs'] = jobs
        # Recover the earlier one-prompt manifest without starting another task.
        if not jobs and manifest.get('prompt_id'):
            jobs.append({'index': 0, 'status': 'generating', 'provider': 'comfyui', 'prompt_id': manifest['prompt_id'],
                         'prompt': manifest.get('prompt', tasks[0]['prompt']), 'workflow_hash': manifest.get('workflow_hash'),
                         'checkpoint': manifest.get('preflight', {}).get('checkpoint', 'unknown'), 'error': None})
        elif not jobs and manifest['status'] in {'generating', 'unknown'}:
            return {**manifest, 'status': 'unknown', 'error': '上次生图提交结果不确定，需核对 ComfyUI，未自动重新提交'}
        if any(job.get('status') in {'generating', 'unknown'} and not job.get('prompt_id') for job in jobs):
            return {**base, 'status': 'unknown', 'error': '有图片提交结果未确定且无可回读标识，未重新提交或替换该图片'}
        for index, asset in enumerate(base['assets']):
            asset.setdefault('prompt_index', index)
        base.update(status='generating', error=None)
        _save_generation_progress(directory, base)
        _prepare_materials(settings, topic, work, directory, base, cancelled, progress)
        jobs = base['generation_jobs']
        current_job, submitted = None, False
        try:
            # 缺口位或显式重生成位都需要访问 ComfyUI；单图重生成时 assets 数量可能已满。
            if len(base['assets']) < base['requested_count'] or any(job.get('regenerate') for job in jobs):
                with httpx.Client(base_url=_comfy_url(settings), timeout=15, trust_env=False, follow_redirects=False) as client:
                    for index in range(base['requested_count']):
                        current_job, submitted = None, False
                        has_asset = any(asset['prompt_index'] == index for asset in base['assets'])
                        if cancelled():
                            raise ImageCancelled()
                        current_job = next((job for job in jobs if job['index'] == index), None)
                        if has_asset and not (current_job and current_job.get('regenerate')):
                            continue
                        if current_job is None:
                            current_job = {'index': index, 'status': 'not_started', 'provider': 'comfyui', 'error': None}
                            jobs.append(current_job)
                        submitted = bool(current_job.get('prompt_id'))
                        task = tasks[index] if index < len(tasks) else tasks[-1]
                        prompt = task['prompt'] + '\nEditorial concept illustration. Not a photograph of a real event. No text, logos, or watermark.'
                        if not submitted:
                            preflight = _preflight(client, settings)
                            base['preflight'] = preflight
                            if cancelled():
                                raise ImageCancelled()
                            seed = secrets.randbits(63)
                            graph = _prompt_graph(prompt, preflight['checkpoint'], seed,
                                                  f'workbench/{version_id}/{platform}/{index + 1}',
                                                  task['width'], task['height'])
                            current_job.update(status='generating', prompt=prompt, seed=seed, checkpoint=preflight['checkpoint'],
                                               page_role=task['page_role'], width=task['width'], height=task['height'],
                                               workflow_hash=hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(), error=None)
                            base.update(prompt=prompt, seed=seed, workflow_hash=current_job['workflow_hash'])
                            base.pop('prompt_id', None)
                            _save_generation_progress(directory, base)
                            submitted = True
                            response = client.post('/prompt', json={'prompt': graph, 'client_id': secrets.token_hex(16)})
                            response.raise_for_status()
                            result = response.json()
                            if result.get('error') or result.get('node_errors'):
                                submitted = False
                                raise ImageAssetError('ComfyUI 拒绝了图片工作流，未生成图片')
                            prompt_id = result.get('prompt_id')
                            if not isinstance(prompt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', prompt_id):
                                raise ImageAssetError('ComfyUI 没有返回可核对的生图标识')
                            current_job['prompt_id'] = prompt_id
                            base['prompt_id'] = prompt_id
                            _save_generation_progress(directory, base)
                        prompt_id = current_job['prompt_id']
                        deadline = time.monotonic() + min(1800, max(30, int(getattr(settings, 'image_generation_timeout_seconds', settings.task_timeout_seconds))))
                        progress(f'本机正在生成第 {index + 1} / {base["requested_count"]} 张实际图片')
                        while time.monotonic() < deadline:
                            if cancelled():
                                raise ImageCancelled()
                            data = _read_prompt_output(client, prompt_id)
                            if data is not None:
                                metadata = _image_metadata(data)
                                if metadata['sha256'] in _known_hashes(base):
                                    current_job.update(status='needs_review', error='新输出与已有图片重复，未重复计入素材数量')
                                    base.update(status='needs_review', error='出现重复图片，已保留现有实际图片；剩余素材需要处理')
                                    _save_generation_progress(directory, base)
                                    return get_asset_manifest(settings, topic_id, version_id, platform)
                                source = {'kind': 'generated', 'comfy_prompt_id': prompt_id, 'checkpoint': current_job.get('checkpoint', 'unknown'),
                                          'workflow_hash': current_job.get('workflow_hash'), 'license': 'local_model_terms_unverified'}
                                asset = _persist_bytes(directory, data, current_job.get('prompt', prompt), 'comfyui', source, index)
                                _install_asset(base, index, asset)
                                current_job.update(status='ready', asset_id=asset['id'], error=None, regenerate=False)
                                base.setdefault('preflight', {'available': True, 'compatible': False, 'workflow': 'recorded_prior_task'})['generation_verified'] = True
                                _save_generation_progress(directory, base)
                                progress(f'第 {index + 1} / {base["requested_count"]} 张实际图片已保存并校验')
                                break
                            time.sleep(1)
                        else:
                            current_job.update(status='unknown', error='等待超时，保留原 prompt_id 供回读')
                            base.update(status='unknown', error='部分生图等待超时，已完成图片与原任务标识保留，未重复提交')
                            _save_generation_progress(directory, base)
                            return get_asset_manifest(settings, topic_id, version_id, platform)
            if cancelled():
                raise ImageCancelled()
            base['assets'].sort(key=lambda asset: asset['prompt_index'])
            providers = {asset['provider'] for asset in base['assets']}
            base.update(status='ready', error=None, provider=next(iter(providers)) if len(providers) == 1 else 'mixed')
            if any(asset['is_ai_generated'] for asset in base['assets']):
                warning = 'AI 生成示意图，非真实事件现场照片；本地模型使用条款需人工审阅。'
                if warning not in base['warnings']:
                    base['warnings'].append(warning)
            _save_generation_progress(directory, base)
            progress('实际图片已保存并通过文件与版本校验')
            return get_asset_manifest(settings, topic_id, version_id, platform)
        except ImageCancelled:
            if current_job:
                current_job.update(status='unknown' if submitted else 'cancelled', error='图片任务已取消')
            base.update(status='unknown' if submitted else 'cancelled', error='任务已取消；已完成图片保留，已提交任务仅按已有 prompt_id 核对，不全局中断 ComfyUI')
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
            if current_job:
                current_job.update(status='unknown' if submitted else 'failed', error='当前图片提交或结果未确认')
            base.update(status='unknown' if submitted else 'failed', error='生图提交或结果未确认，已完成图片与任务标识保留，未以提示词替代图片' if submitted else '本机生图服务或工作流不可用，缺口尚未生成实际图片；已完成图片保留')
        _save_generation_progress(directory, base)
        return get_asset_manifest(settings, topic_id, version_id, platform)


def regenerate_asset(settings, topic_id, version_id, platform, index):
    """把某个图片位重新排队生成新候选：已有图片保持选定，直到新图成功替换。

    与整体重建版本不同：请求哈希不变（正文未变），历史候选保留可回退。
    提交结果不确定时沿用全局 unknown 语义，绝不盲目重提。
    """
    if type(index) is not int or index < 0:
        raise ImageAssetError('图片序号无效')
    with _lock:
        directory = _asset_directory(settings, topic_id, version_id, platform)
        manifest = get_asset_manifest(settings, topic_id, version_id, platform)
        if manifest['status'] in {'corrupt', 'blocked'}:
            return manifest
        if manifest['status'] == 'generating':
            raise ImageAssetError('已有图片任务在执行，请等待完成后再重生成')
        requested = manifest.get('requested_count')
        if isinstance(requested, int) and index >= requested:
            raise ImageAssetError('图片序号超出本次请求范围')
        base = dict(manifest)
        for key in ('candidates', 'selected'):
            value = base.get(key)
            if not isinstance(value, dict):
                if manifest['status'] == 'ready':
                    raise ImageAssetError(f'{key} 记录无效，请人工核对后重建版本')
                base[key] = {}
        jobs = base.get('generation_jobs') if isinstance(base.get('generation_jobs'), list) else []
        jobs = [job for job in jobs if isinstance(job, dict) and job.get('index') != index]
        jobs.append({'index': index, 'status': 'not_started', 'provider': 'comfyui',
                     'error': None, 'regenerate': True})
        base['generation_jobs'] = jobs
        base.update(status='generating', error=None, updated_at=_now())
        _atomic_json(directory / 'manifest.json', base)
        return get_asset_manifest(settings, topic_id, version_id, platform)


def select_asset(settings, topic_id, version_id, platform, index, asset_id):
    """在某个图片位的候选版本之间切换当前选定；切换后立即走完整校验。"""
    if type(index) is not int or index < 0:
        raise ImageAssetError('图片序号无效')
    if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{24}', asset_id):
        raise ImageAssetError('图片资产标识无效')
    with _lock:
        directory = _asset_directory(settings, topic_id, version_id, platform)
        manifest = get_asset_manifest(settings, topic_id, version_id, platform)
        if manifest['status'] != 'ready':
            raise ImageAssetError('图片尚未全部就绪，不能切换候选版本')
        requested = manifest.get('requested_count')
        if isinstance(requested, int) and index >= requested:
            raise ImageAssetError('图片序号超出本次请求范围')
        candidates = manifest.get('candidates') if isinstance(manifest.get('candidates'), dict) else {}
        match = next((item for item in candidates.get(str(index), [])
                      if isinstance(item, dict) and item.get('id') == asset_id), None)
        if not match:
            raise ImageAssetError('候选版本不存在或不属于该图片位')
        base = dict(manifest)
        base['assets'] = [item for item in base['assets'] if item['prompt_index'] != index]
        base['assets'].append({**match, 'prompt_index': index})
        base.setdefault('selected', {})[str(index)] = asset_id
        base['updated_at'] = _now()
        _atomic_json(directory / 'manifest.json', base)
        # 落盘后立即整册复验（sha256、路径、数量），任何不一致都以 corrupt 呈现。
        return get_asset_manifest(settings, topic_id, version_id, platform)
