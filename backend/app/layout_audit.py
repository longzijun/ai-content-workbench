"""排版审计：把模板约定与图片几何变成可执行、可追溯的检查结果。

四个检查，各自的强度是有理由的：

  title_overflow    warning  标题会被信息带裁掉。改标题即可，不必阻断。
  body_overflow     blocked   正文超出平台可承载上限。平台硬约束，platform_rules
                             已经在投递侧兜底，这里提前如实报出。
  image_blur        blocked   封面/正文图尺寸低于模板下限，画面本身不可用。
  cover_occlusion   passed    没有人工标注的主体框就明确「跳过」，不用图像
                             处理猜 —— 猜出来的结论没人敢信。

三条硬约束：

1. **纯函数**。除调用方传入的图片字节外不读文件、不写库、不联网。
   数据库写入由 record() 单独承担，audit() 自身不碰 db。
2. **不自动截断、不自动修复**。正文超限只报不裁 —— 截断会破坏
   BrowserDraftLedger 的 content_hash(work) 与版本冻结内容之间的绑定，
   草稿回读比对会因此失效。
3. **不接入投递判定**。audit_results 不参与 service.sync_draft 的状态计算：
   沿用「出图失败不阻断平台草稿交付」的既有决策，人工决定是否修订。

边界 —— 本模块不做的事：
  不改渲染、不重采样、不引入模糊度算法、不猜测画面主体、不阻断交付。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import layout_templates, richtext, typeset_engine
from .layout_templates import PROJECT_BASELINE

#: 证据里必须带出的口径声明。所有数值都不是平台公布的。
EVIDENCE_PROVENANCE = f'数值取自排版模板，{PROJECT_BASELINE}；不代表平台公开要求'

_PASSED = 'passed'
_WARNING = 'warning'
_BLOCKED = 'blocked'
_SEVERITY_ORDER = {_PASSED: 0, _WARNING: 1, _BLOCKED: 2}


def audit(template: dict, work: dict, *, cover_bytes=None, body_images=None):
    """对一个平台的一篇稿件跑全部启用的检查。纯函数，不落库。

    template 来自 layout_templates.resolve；work 需含 title / body。
    cover_bytes 与 body_images 是可选的图片字节（调用方已从版本资产读出），
    传入 None 表示该侧没有图片，此时对应检查跳过。
    """
    findings = []
    checks = template.get('checks') or {}

    if checks.get('title_overflow', True):
        findings.append(_check_title(template, work, cover_bytes))

    if checks.get('body_overflow', True):
        findings.append(_check_body(template, work))

    if checks.get('item_count', False):
        findings.append(_check_items(template, work))

    if checks.get('image_blur', True):
        findings.extend(_check_images(template, cover_bytes, body_images or []))

    if checks.get('cover_occlusion', False):
        findings.append(_check_cover_occlusion(cover_bytes))

    findings = [item for item in findings if item]
    status = _PASSED
    for item in findings:
        if _SEVERITY_ORDER[item['severity']] > _SEVERITY_ORDER[status]:
            status = item['severity']
    findings.sort(key=lambda item: (-_SEVERITY_ORDER[item['severity']], item['code']))
    return {
        'template_id': template['id'],
        'platform': template['platform'],
        'content_form': template['content_form'],
        'status': status,
        'findings': findings,
        'evidence': {
            'provenance': EVIDENCE_PROVENANCE,
            'title_window': (template.get('title_style') or {}).get('window'),
            'body_ceiling': (template.get('media_spec') or {}).get('body_ceiling'),
            'measured_on': 'editor_visible_text',
            'skeleton': list(template.get('skeleton') or []),
        },
    }


# --------------------------------------------------------------------------
# 四个检查
# --------------------------------------------------------------------------

def _check_title(template, work, cover_bytes):
    title = str(work.get('title') or '')
    if not title:
        return {'code': 'title_overflow', 'severity': _WARNING,
                'message': '标题为空，无法测量信息带排版'}
    if cover_bytes is None:
        return {'code': 'title_overflow', 'severity': _PASSED,
                'message': '没有封面图片，跳过标题溢出测量',
                'skipped': True}
    if _image_size(cover_bytes) is None:
        # 图片本身坏了，几何无从测量。这一条由 image_blur 报 blocked，
        # 这里不能崩 —— 一次坏图不该让整轮审计中断。
        return {'code': 'title_overflow', 'severity': _PASSED,
                'message': '封面图片无法读取尺寸，跳过标题溢出测量', 'skipped': True}
    try:
        overflow, detail, geometry = typeset_engine.title_overflow(
            cover_bytes, title, account_label='')
    except (ValueError, OSError) as error:
        # 尺寸够但排版算不了（例如过小画幅）。如实说跳过，不假装通过。
        return {'code': 'title_overflow', 'severity': _PASSED, 'skipped': True,
                'message': f'标题排版无法测量（{str(error)[:60]}），已跳过',
                'evidence': {'skipped_reason': str(error)[:120]}}
    return {
        'code': 'title_overflow',
        'severity': _WARNING if overflow else _PASSED,
        'message': detail or '标题可完整放进信息带',
        'evidence': {'title_rows': len(geometry['title_lines']),
                     'visible_rows': geometry['visible_rows'],
                     'band_ratio': round(geometry['band_height'] / max(1, geometry['height']), 3)},
    }


def _check_body(template, work):
    """在编辑器可见文本上量长度，不是 Markdown 原文。"""
    body = str(work.get('body') or '')
    measured = layout_templates.measure_body(template, body)
    if measured['ceiling'] is None:
        return {'code': 'body_overflow', 'severity': _PASSED,
                'message': '该平台没有公开正文上限，按目标档位判断',
                'evidence': {'visible_chars': measured['visible_chars']}}
    if measured['over']:
        return {
            'code': 'body_overflow', 'severity': _BLOCKED,
            'message': (f"编辑器可见正文 {measured['visible_chars']} 字，超过"
                        f"{template['platform']}可承载上限 {measured['ceiling']} 字；"
                        f"请手动精简，系统不会自动截断"),
            'evidence': {'visible_chars': measured['visible_chars'],
                         'markdown_chars': measured['markdown_chars'],
                         'ceiling': measured['ceiling']},
        }
    return {'code': 'body_overflow', 'severity': _PASSED,
            'message': f"可见正文 {measured['visible_chars']} 字，在上限内",
            'evidence': {'visible_chars': measured['visible_chars'],
                         'ceiling': measured['ceiling']}}


def _check_items(template, work):
    """清单速查的并列项数量。

    数的是**渲染后的可见段落**，不是 Markdown 的 `- ` 前缀 —— 前缀在画面上
    并不存在，数它等于在数标记而不是内容。
    """
    minimum = _checklist_minimum()
    if minimum == 0:
        return None
    visible = richtext.expected_visible_text(str(work.get('body') or ''), template['platform'])
    blocks = [line for line in visible.splitlines() if line.strip()]
    count = len(blocks)
    if count >= minimum:
        return {'code': 'item_count', 'severity': _PASSED,
                'message': f'可辨认的并列项 {count} 个，满足清单速查的 {minimum} 个'}
    return {'code': 'item_count', 'severity': _WARNING,
            'message': f'清单速查期望至少 {minimum} 个并列项，当前约 {count} 个；'
                       f'条目不足时读者难以逐条核对'}


def _checklist_minimum() -> int:
    from . import formats
    entry = formats.get_content_form('checklist')
    return entry['items_min']


def _check_images(template, cover_bytes, body_images):
    """只读图片元数据，不重采样、不引入模糊度算法。

    「模糊」在本工作台的可判定含义是「尺寸低于模板下限」—— 放大上去必然糊。
    这不是图像质量分析，是一个诚实的近似。
    """
    spec = template.get('media_spec') or {}
    floor = int(spec.get('min_width') or 800)
    findings = []
    for label, data in [('cover', cover_bytes)] + [(f'body[{i}]', item)
                                                  for i, item in enumerate(body_images)]:
        if data is None:
            continue
        size = _image_size(data)
        if size is None:
            findings.append({'code': 'image_blur', 'severity': _BLOCKED,
                             'message': f'{label} 图片无法读取尺寸，可能已损坏',
                             'evidence': {'label': label}})
            continue
        width, height = size
        if width < floor:
            findings.append({
                'code': 'image_blur', 'severity': _BLOCKED,
                'message': (f'{label} 图片宽 {width} 像素，低于模板下限 {floor}；'
                            f'平台侧放大后必然模糊，请换更高分辨率的原图'),
                'evidence': {'label': label, 'width': width, 'height': height,
                             'min_width': floor, 'basis': '尺寸下限，非图像质量分析',
                             'provenance': EVIDENCE_PROVENANCE}})
        else:
            findings.append({'code': 'image_blur', 'severity': _PASSED,
                             'message': f'{label} 图片 {width}×{height}，达到模板下限',
                             'evidence': {'label': label, 'width': width, 'height': height}})
    if not findings:
        return [{'code': 'image_blur', 'severity': _PASSED,
                 'message': '本版没有配图，跳过尺寸检查', 'skipped': True}]
    return findings


def _check_cover_occlusion(cover_bytes):
    """没有人工标注的主体框就明确跳过，不用图像处理猜。"""
    if cover_bytes is None:
        return {'code': 'cover_occlusion', 'severity': _PASSED,
                'message': '没有封面图片，跳过遮挡检查', 'skipped': True}
    return {
        'code': 'cover_occlusion', 'severity': _PASSED, 'skipped': True,
        'message': '工作台没有人工标注的画面主体框，无法判断遮挡；已跳过而非猜测',
        'evidence': {'skipped_reason': 'no_subject_annotation',
                     'would_need': '人工在预览页框出主体区域，或未来接入人脸/主体检测'},
    }


def _image_size(data):
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as image:
            return image.size
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None


# --------------------------------------------------------------------------
# 落库：与 audit() 分离，单独承担 IO
# --------------------------------------------------------------------------

def record(result: dict, version_id: str) -> dict:
    """把审计结果写入 layout_audits。这是本模块唯一做 IO 的函数。"""
    from . import db

    stamp = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO layout_audits(version_id,platform,template_id,status,findings,evidence,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(version_id,platform,template_id) DO UPDATE SET "
            "status=excluded.status,findings=excluded.findings,evidence=excluded.evidence,"
            "updated_at=excluded.updated_at",
            (version_id, result['platform'], result['template_id'], result['status'],
             json.dumps(result['findings'], ensure_ascii=False),
             json.dumps(result['evidence'], ensure_ascii=False), stamp, stamp))
    return result


def history(version_id: str) -> list:
    """读回某版本的全部审计结果，按平台排序。纯读。"""
    from . import db

    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT platform,template_id,status,findings,evidence,updated_at FROM layout_audits "
            "WHERE version_id=? ORDER BY platform", (version_id,)).fetchall()
    return [{'platform': row['platform'], 'template_id': row['template_id'],
             'status': row['status'], 'findings': json.loads(row['findings']),
             'evidence': json.loads(row['evidence']), 'updated_at': row['updated_at']}
            for row in rows]