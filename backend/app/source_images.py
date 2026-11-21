"""把研究阶段抓取的源生图注册为冻结版本资产。

2026-10-20 用户决策：热点抓取时顺带抓原文源生图，抓到即自动随版本进入
各平台草稿箱；是否替换、删除或修改由用户在平台端自行决定。版权口径：
manifest 如实记录来源（原文标题与 URL），发布前由用户人工过目，不冒充原创。

边界：本模块只做"候选池 → 版本资产"的注册，不生成、不修改图片内容；
失败绝不阻断正文交付主链路（无图交付，媒体校验按 not_required 走）。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import json
import logging

from .image_assets import register_assets, get_asset_manifest

logger = logging.getLogger(__name__)

# 各平台随稿交付的源生图数量上限：公众号/头条正文图宜少而精，
# 小红书图文笔记本身就是多图形态。
PLATFORM_LIMITS = {'wechat': 3, 'toutiao': 3, 'xiaohongshu': 9}

# register_assets 的素材授权防线要求显式来源声明。这里不是伪造授权，
# 而是记录用户 2026-10-20 的明确策略决定：源生图由用户自行判断使用，
# 工具职责是保留来源与 credit，发布前人工过目（草稿箱模式）。
SOURCE_USER_POLICY = {
    'kind': 'source',
    'rights_confirmed': True,
    'license': 'source_publisher_material_user_decides',
    'authorization_id': 'user-source-image-policy-2026-10-20',
    'credit_required': False,
}


def pool_path(settings, topic_id):
    return settings.runtime_dir / 'data' / 'research' / 'images' / str(topic_id) / 'pool.json'


def load_pool(settings, topic_id):
    """读取选题级源生图池；损坏或缺失一律按空池处理，不抛异常。"""
    path = pool_path(settings, topic_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text('utf-8'))
        images = data.get('images')
        if not isinstance(images, list):
            return []
        return [item for item in images
                if isinstance(item, dict) and item.get('path') and item.get('sha256')]
    except (OSError, ValueError):
        return []


def _publisher_of(url: str) -> str:
    """从原文 URL 取站点名，用于右侧「权利人」栏。只取主机名，不做主观判断。"""
    try:
        host = urlparse(url).hostname or ''
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def _asset_provenance(item) -> dict:
    """把源生图池里的一条记录翻成该图专属的溯源信息。

    2026-10-22 修正：此前这些字段只被拼进 prompt 字符串，manifest 的
    source 对象是所有图共享的一份且 caption 为空，导致前端「查看原始来源」
    永远拿不到链接而不渲染按钮，图文对应关系也无从核对。溯源必须逐图落库。
    """
    url = item.get('source_url')
    url = str(url).strip() if isinstance(url, str) else ''
    title = item.get('source_title')
    title = str(title).strip() if isinstance(title, str) else ''
    publisher = _publisher_of(url)
    alt = item.get('alt') or item.get('caption')
    alt = str(alt).strip() if isinstance(alt, str) else ''
    caption = alt or title
    provenance = {
        'source_url': url,
        'source_title': title,
        'publisher': publisher,
        'rights_holder': publisher,
        # 抓取自原文的图不等于该事件现场照片。未人工确认前一律 False。
        'depicts_verified_event': False,
    }
    if caption:
        provenance['caption'] = caption[:300]
    return provenance


def register_source_images(settings, topic_id, version_id, platform):
    """注册源生图到固定版本的资产清单。

    返回 manifest（已就绪）、幂等返回既有 ready 清单；无图返回 None。
    清单状态不允许注册时抛 ImageAssetError，由调用方决定如何兜底。
    """
    manifest = get_asset_manifest(settings, topic_id, version_id, platform)
    if manifest.get('status') == 'ready' and manifest.get('assets'):
        return manifest
    pool = [item for item in load_pool(settings, topic_id) if Path(str(item['path'])).is_file()]
    if not pool:
        return None
    picked = pool[:PLATFORM_LIMITS.get(platform, 3)]
    paths = [item['path'] for item in picked]
    prompts = ['源生图｜来自原文：' + str(item.get('source_title') or '未知')[:80]
               + '｜' + str(item.get('source_url') or '')[:200] for item in picked]
    credit = '；'.join(dict.fromkeys(str(item.get('source_title') or '')[:60] for item in picked))[:400]
    source = {**SOURCE_USER_POLICY, 'caption': '', 'credit': credit}
    return register_assets(settings, topic_id, version_id, platform, paths, prompts,
                           provider='source', source=source,
                           asset_sources=[_asset_provenance(item) for item in picked])


def register_for_works(settings, topic, version_id, works):
    """版本冻结后逐平台注册源生图；返回结果索引，失败只记录不抛出。"""
    outcome = {}
    for work in works:
        platform = work.get('platform') if isinstance(work, dict) else None
        if not platform:
            continue
        try:
            manifest = register_source_images(settings, topic['id'], version_id, platform)
            if manifest:
                outcome[platform] = {'status': manifest.get('status'),
                                     'count': len(manifest.get('assets', []))}
            else:
                outcome[platform] = {'status': 'no_source_images', 'count': 0}
        except Exception as error:  # 源生图失败不阻断正文交付
            logger.warning('源生图注册未完成 %s/%s: %s', version_id, platform, error)
            outcome[platform] = {'status': 'failed', 'error': str(error)[:200], 'count': 0}
    return outcome
