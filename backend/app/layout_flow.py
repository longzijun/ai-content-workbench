"""把排版审计接进真实流程。

`layout_audit.py` 是纯函数 + 落库，`layout_templates.py` 是模板解析，两者都已
写好但**没有任何调用方**：main / service / worker 里零引用，所以模板与审计
从来没有参与过任何一次真实审核。这个模块补的就是这一层编排。

调用点只有一处：`service.review_version()` 在写事务提交之后调用
`run_for_version()`。放在事务外是因为审计要读模板表、要写 layout_audits，
而 review 的写事务已经持锁 —— 在锁里再做一轮 IO 没有好处。

边界：
  审计结论**不参与**草稿状态计算（沿用「出图失败不阻断平台草稿交付」的既有
  决策，人工决定是否修订）；
  审计自身失败不得让审核失败 —— 审核是红线流程，排版检查是辅助信息。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import brand, db, format_plan, layout_audit, layout_templates

#: 单张图片读取上限，与 image_assets 的 MAX_IMAGE_BYTES 保持一致。
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def _platform_images(settings, topic_id: str, version_id: str, platform: str):
    """从版本资产目录读回图片字节，供审计检查尺寸。

    路径规则与 `image_assets._asset_directory` 一致：`runtime/assets/<topic>/<version>/<platform>/`。
    这里不复用该函数，因为它在目录不存在时会 mkdir —— 审计是读操作，不该
    凭空造出空目录。

    安全边界：
      - 目录必须落在 `runtime/assets` 之内；
      - manifest 里记录的绝对路径必须落在该平台目录之内，防止被改写成任意路径；
      - 超过 32 MB 或读不出来的直接跳过（`audit()` 会把 None 当「没有这张图」）。
    """
    if not topic_id or not version_id:
        return None, []
    assets_root = (Path(settings.runtime_dir) / "assets").resolve()
    root = (assets_root / topic_id / version_id / platform).resolve()
    if not root.is_relative_to(assets_root) or not root.is_dir():
        return None, []
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None, []
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None, []

    cover, body = None, []
    for asset in manifest.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        try:
            path = Path(str(asset.get("path") or ""))
            if not path.is_absolute() or not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            if resolved.stat().st_size > MAX_IMAGE_BYTES:
                continue
            payload = resolved.read_bytes()
        except (OSError, ValueError):
            continue
        if asset.get("role") == "cover" and cover is None:
            cover = payload
        else:
            body.append(payload)
    return cover, body


def _version(version_id: str) -> tuple:
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise ValueError('版本不存在')
        topic = connection.execute("SELECT * FROM topics WHERE id=?", (row["topic_id"],)).fetchone()
    return dict(row), dict(topic) if topic else {}


def run_for_version(version_id: str) -> dict:
    """对某版本的全部作品跑一遍排版审计并落库。

    `images_checked` 如实反映「这次到底有没有读到图片字节」：读到了才够格说
    图片检查跑过；一张都没读到就仍报 False，图片检查在 findings 里显示为 skipped。
    """
    from .config import get_settings

    version, topic = _version(version_id)
    settings = get_settings()
    try:
        payload = json.loads(version["payload"])
    except (TypeError, ValueError) as error:
        raise ValueError(f'版本内容无法解析：{str(error)[:120]}')
    works = payload.get("works") if isinstance(payload, dict) else None
    if not isinstance(works, list) or not works:
        return {'version_id': version_id, 'results': [], 'errors': [],
                'images_checked': False, 'image_platforms': [],
                'note': '该版本没有可审计的作品'}

    brand_id = brand.ensure_default()
    results, errors = [], []
    image_platforms = []
    for work in works:
        if not isinstance(work, dict):
            continue
        platform = str(work.get("platform") or "")
        format_id = str(work.get("format") or "article")
        content_form = ''
        try:
            content_form = format_plan.resolve(topic, platform, work)
            template = layout_templates.resolve(brand_id, platform, format_id, content_form)
        except (LookupError, ValueError) as error:
            # 模板缺失是配置问题，要看得见，但不该让整轮审计崩掉。
            errors.append({'platform': platform, 'format': format_id,
                           'content_form': content_form,
                           'message': str(error)[:300]})
            continue
        cover, body_images = _platform_images(settings, version["topic_id"], version_id, platform)
        if cover is not None or body_images:
            image_platforms.append(platform)
        result = layout_audit.audit(template, work, cover_bytes=cover, body_images=body_images)
        layout_audit.record(result, version_id)
        results.append(result)

    return {
        'version_id': version_id,
        'results': results,
        'errors': errors,
        'images_checked': bool(image_platforms),
        'image_platforms': image_platforms,
        'note': '标题与正文按编辑器可见文本测量；image_blur 判据是「宽于模板下限」，'
                '不是图像质量分析；cover_occlusion 没有人工标注就跳过。'
                '审计结论不参与草稿交付判定。',
    }


def results_for_version(version_id: str) -> dict:
    """读回已落库的审计结果。纯读。"""
    rows = layout_audit.history(version_id)
    return {
        'version_id': version_id,
        'results': rows,
        'worst': _worst(rows),
        'note': '数值取自排版模板，项目基线，非平台官方规格；不代表平台公开要求。',
    }


def _worst(rows: list) -> str:
    order = {'passed': 0, 'warning': 1, 'blocked': 2}
    worst = 'passed'
    for row in rows:
        if order.get(row.get('status'), 0) > order[worst]:
            worst = row.get('status')
    return worst
