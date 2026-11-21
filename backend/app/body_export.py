"""把草稿正文渲染成「复制到平台草稿箱」可直接粘贴的形态。

V3 本地内容模式下平台自动投递关闭，三平台草稿由人工从工作台复制进官方
编辑器。此前的「复制正文」复制的是 body 原文纯文本：没有 Markdown 标记时
粘贴出来是一堵字墙，有标记时 ``##``/``**`` 会原样暴露；图片更是完全带不过
去——图片只存在于素材面板，用户要在平台编辑器里手动逐张上传。

本模块产出三层：

- ``html`` —— 语义化富文本（richtext.to_html），并把已就绪的图片资产
  压缩后以 base64 data URI 内嵌为 ``<p><img></p>`` 附加在正文之后。
  公众号与头条的网页编辑器粘贴时会识别内嵌图片并转存为平台素材，
  本机图片因此不需要公网 URL。小红书正文是纯文本域，不给 html。
- ``text`` —— 纯文本降级（richtext.to_text），富文本粘贴异常时的兜底。
- ``images`` —— 逐图条目（data URI + 图注），供前端每图独立「复制图片」
  按钮使用；平台编辑器对整段富文本粘贴处理不稳时，单图 Ctrl+V 最可靠。

压缩口径：长边宽压到 1080px（公众号正文图标准宽度），只缩小不放大；
JPEG 质量从 85 逐级降低直到体积达标，控制剪贴板里的 HTML 总体积。

边界：只读已冻结的图片资产（manifest status='ready'），不生成、不修改
图片内容；单图读取或压缩失败只记 warning 并跳过，绝不阻断正文导出。
"""
from __future__ import annotations

import base64
import html as html_module
import logging
from io import BytesIO
from pathlib import Path

from .image_assets import get_asset_manifest
from .richtext import to_html, to_text

logger = logging.getLogger(__name__)

#: 内嵌图最大宽度。公众号正文渲染宽度就是 1080，更大只会撑大剪贴板。
MAX_EMBED_WIDTH = 1080
#: 单图压缩目标的粗略上限：base64 后约 1.2MB，3 张图整份 HTML 仍在剪贴板
#: 可靠范围内。质量逐级降级，直到达标或到最低档。
MAX_EMBED_BYTES = 900 * 1024
_QUALITY_LADDER = (85, 72, 58, 45)


def build(work: dict, platform: str, settings, topic_id: str, version_id: str) -> dict:
    """返回 {'html', 'text', 'images', 'warnings'}。纯读，不改任何状态。"""
    body = str((work or {}).get('body') or '')
    text = to_text(body, platform)
    warnings: list[str] = []
    if platform == 'xiaohongshu':
        # 小红书笔记正文是纯文本域，富文本 HTML 没有落脚点；图片走逐图复制。
        rich = None
        figures = ''
    else:
        rich = to_html(body, platform)
        figures = ''
    images, embed_warnings = _embedded_images(settings, topic_id, version_id, platform)
    warnings.extend(embed_warnings)
    if platform != 'xiaohongshu' and rich is not None:
        figures = ''.join(
            '<p><img src="%s" alt="%s"/></p>' % (image['data_uri'], html_module.escape(image['caption'] or '配图', quote=True))
            for image in images)
        if figures:
            rich = rich + '\n' + figures
    return {'html': rich, 'text': text, 'images': images, 'warnings': warnings}


def _embedded_images(settings, topic_id: str, version_id: str, platform: str) -> tuple[list, list[str]]:
    """读已冻结的平台图片资产，返回 (逐图条目, warnings)。失败不抛出。"""
    try:
        manifest = get_asset_manifest(settings, topic_id, version_id, platform)
    except Exception as error:  # 清单损坏等异常按无图处理，正文导出不能被图片卡死
        logger.warning('图片清单读取失败 %s/%s: %s', version_id, platform, error)
        return [], [f'图片清单读取失败，正文未内嵌图片：{str(error)[:120]}']
    if manifest.get('status') != 'ready' or not manifest.get('assets'):
        # 有清单但不可用（文件缺失、校验失败等）必须显式说明；从未产出过图
        # （not_started）是正常无图情况，不算 warning。
        error = str(manifest.get('error') or '').strip()
        if error:
            return [], ['图片清单不可用，正文未内嵌图片：' + error[:120]]
        return [], []
    images, warnings = [], []
    for asset in manifest.get('assets', []):
        if not isinstance(asset, dict) or not asset.get('path'):
            continue
        path = Path(asset['path'])
        try:
            data = path.read_bytes()
        except OSError as error:
            warnings.append(f'图片 {path.name} 读取失败，未内嵌：{str(error)[:120]}')
            continue
        encoded, reason = _compress(data)
        if encoded is None:
            warnings.append(f'图片 {path.name} 未能压缩内嵌：{reason}')
            continue
        images.append({
            'id': asset.get('id') or path.stem,
            'caption': str(asset.get('caption') or '')[:300],
            'data_uri': 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode('ascii'),
        })
    return images, warnings


def _compress(data: bytes) -> tuple[bytes | None, str]:
    """压到 1080px 内的 JPEG；只缩小不放大，质量逐级降低直到体积达标。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow 是既有依赖，正常不会缺
        return None, 'Pillow 不可用'
    try:
        with Image.open(BytesIO(data)) as image:
            converted = image.convert('RGB')
            width, height = converted.size
            if width > MAX_EMBED_WIDTH:
                converted = converted.resize(
                    (MAX_EMBED_WIDTH, max(1, round(height * MAX_EMBED_WIDTH / width))))
            for quality in _QUALITY_LADDER:
                output = BytesIO()
                converted.save(output, format='JPEG', quality=quality)
                if output.tell() <= MAX_EMBED_BYTES or quality == _QUALITY_LADDER[-1]:
                    return output.getvalue(), ''
    except Exception as error:
        return None, str(error)[:120]
    return None, '未知压缩失败'
