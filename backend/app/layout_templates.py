"""排版模板库：这个平台、这个叙事形式，稿子该长什么样。

与 richtext.py 的职责边界（零重叠，可测）：

  richtext 回答「这段 Markdown 填进编辑器会变成什么」—— 适配层。
  本模块回答「这个平台这个叙事形式稿子该长什么样」—— 版式骨架。

本模块**不产生任何 HTML 或纯文本**。它调用 richtext.expected_visible_text
拿到编辑器里真正会出现的文字，再在这串文字上做测量（长度、留白、层级）。
这件事有一个测试用源码断言守着（见 test_formats 里同名断言）：本文件里
不得出现 to_html / to_text / strip_markup 的调用 —— 出现了就说明职责越界，
两处逻辑会各自演化。

模板里为什么允许出现平台数值（如小红书 1000 字）：
那不是编造的，是从 platform_rules 引用来的 —— 本模块 import 那个模块并
在种子生成时读取，不在此复述数值（数值与 platform_rules 的同步由测试双向
比对锁定）。凡是无法从既有模块推导的数字（例如封面比例这类平台未公布、
工作台自己定的运营参数），一律带 provenance 标注「项目基线，非平台官方规格」。

边界 —— 本模块不做的事：
  不渲染、不裁剪、不自动截断、不阻断交付；不读图片像素（那是 layout_audit）；
  不决定 A 轴渠道形态（A 轴权威在 platform_rules）。
  模板只是「长什么样」的约定，能不能做到由审计如实判定。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

from . import formats, richtext
from .brand import DEFAULT_BRAND_ID, ensure_default
from .platform_rules import TITLE_LIMITS, expected_format, work_errors

#: 无法从既有模块推导的运营参数，标注出处，避免日后被当成平台规则。
PROJECT_BASELINE = '项目基线，非平台官方规格'
PLATFORM_OFFICIAL = '平台官方限制，取自 platform_rules'


def render_mode_for(platform: str) -> str:
    """该平台的填入方式。必须与 richtext.render 的分派一致。

    刻意不从 richtext 反查 —— 它的签名里没有 format 参数，且反查需要构造
    一段 Markdown 再丢弃。两条判据的同步由 test_layout_templates 逐平台比对。
    """
    if platform == 'xiaohongshu':
        return 'text'
    return 'html'


def _seed_skeleton(content_form: str) -> list:
    """叙事形式对应的区块骨架。有序，审计按此顺序检查。"""
    shared = ['lead', 'evidence', 'action']
    return {
        'tutorial': ['lead', 'steps', 'pitfalls', 'action'],
        'opinion': ['claim', 'reasoning', 'counterpoint', 'action'],
        'checklist': ['lead', 'items', 'caveats', 'action'],
        'case': ['background', 'actions', 'result', 'lesson'],
        'brief': ['lead', 'points', 'source', 'action'],
        '': shared,
    }[content_form]


def _seed_title_style(content_form: str) -> dict:
    """标题样式约定。注意 title_window 来自 formats 注册表，不在此复述。"""
    return {
        'content_form': content_form,
        'emphasis': {
            'tutorial': '承诺可执行的结果',
            'opinion': '给出可被反驳的判断',
            'checklist': '承诺可逐条核对',
            'case': '点明案例主体',
            'brief': '交代事件与时间',
            '': '具体兑现价值',
        }[content_form],
        'window': list(formats.get_content_form(content_form)['title_window'])
        if formats.normalize_content_form(content_form) else None,
    }


def _seed_media_spec(platform: str, content_form: str) -> dict:
    """画幅与图片数量的项目基线。"""
    entry = formats.get_content_form(content_form) if formats.normalize_content_form(content_form) else None
    complexity = entry['complexity'] if entry else 'medium'
    return {
        'cover_ratio': {'wechat': '2.35:1', 'toutiao': '16:9', 'xiaohongshu': '3:4'}[platform],
        'max_images': {'low': 1, 'medium': 2, 'high': 3}[complexity],
        'min_width': 800,
        'body_ceiling': body_ceiling_for(platform),
        'provenance': PROJECT_BASELINE,
        'note': '画幅比例与图片数量是本工作台的运营参数，不是平台公开规格。',
    }


def body_ceiling_for(platform: str) -> int | None:
    """正文可承载上限。小红书有官方硬限制，其余平台没有公开正文上限。

    返回 None 表示「无平台硬上限，用目标档位」。数值不写死在这里 ——
    platform_rules 的 work_errors 才是正文硬约束的唯一权威，其行为由
    test_layout_templates 的双向比对锁定：给 platform_rules 换一个上限，
    本函数必须同步。
    """
    if platform == 'xiaohongshu':
        return _xiaohongshu_body_ceiling()
    return None


_CEILING_CACHE: dict = {}


def _xiaohongshu_body_ceiling() -> int:
    """探测 platform_rules 实际生效的小红书正文上限，不在此复述数值。

    二分找的是「最大的仍然合规的正文长度」。platform_rules 的判定是
    `len(body) > ceiling` 报错，所以返回值就是那个 ceiling 本身。
    这样 platform_rules 改了上限，本函数自动跟随，不会出现两套数字。
    结果缓存 —— 二分要构造十几个长正文，没必要每次种子都重算。
    """
    if 'xhs' in _CEILING_CACHE:
        return _CEILING_CACHE['xhs']

    def rejected(length: int) -> bool:
        probe = {'platform': 'xiaohongshu', 'format': 'note',
                 'title': '标题', 'body': '字' * length}
        return any('正文超过' in message for message in work_errors(probe))

    ceiling = 200000
    if rejected(ceiling):
        low, high = 1, ceiling
        while low + 1 < high:
            middle = (low + high) // 2
            if rejected(middle):
                high = middle
            else:
                low = middle
        ceiling = low
    _CEILING_CACHE['xhs'] = ceiling
    return ceiling


def _seed_checks(platform: str, content_form: str) -> dict:
    """该模板启用哪些自动检查。V2-09 的审计按此开关执行。"""
    common = {'title_overflow': True, 'body_overflow': True,
              'image_blur': True, 'cover_occlusion': False}
    if platform == 'xiaohongshu':
        # 短正文平台，溢出更容易发生，且封面遮挡值得看
        common['cover_occlusion'] = True
    if formats.normalize_content_form(content_form) == 'checklist':
        common['item_count'] = True
    return common


def recommended_content_form(platform: str) -> str:
    """该平台的推荐叙事形式。

    取注册表里 boost 含该平台、且 boost 下标最靠前的那一条。`boost` 已是
    sorted 的内容形式顺序里的一个元组，遍历顺序即注册表顺序，稳定可复现。
    没匹配到就返回空串（退化成通用模板），不抛错 —— 推荐是建议不是硬约束。
    """
    for form_id in formats.CONTENT_FORM_IDS:
        if platform in formats.get_content_form(form_id)['boost']:
            return form_id
    return ''


def build_seed(brand_id: str = None) -> list:
    """生成全量种子模板行。

    每个平台两条：一条通用（content_form=''，回退用），一条按该平台的推荐
    叙事形式。不为 5×3 全组合建行 —— 通用模板已经覆盖回退，逐组合建行会
    让库里出现大量「与通用模板同构」的噪声行，反而更难看出真实差异。
    """
    brand_id = brand_id or DEFAULT_BRAND_ID
    rows = []
    stamp = datetime.now(timezone.utc).isoformat()
    for platform in sorted(TITLE_LIMITS):
        for content_form in ('', recommended_content_form(platform)):
            normalized = formats.normalize_content_form(content_form)
            rows.append({
                'id': f'{brand_id}:{platform}:{expected_format(platform)}:{normalized or "general"}',
                'brand_id': brand_id,
                'platform': platform,
                'format_id': expected_format(platform),
                'content_form': normalized,
                'render': render_mode_for(platform),
                'skeleton': _seed_skeleton(normalized),
                'title_style': _seed_title_style(normalized),
                'media_spec': _seed_media_spec(platform, normalized),
                'checks': _seed_checks(platform, normalized),
                'provenance': (f'叙事形式与标题窗口取自 formats 注册表；'
                               f'正文上限{PLATFORM_OFFICIAL}；画幅与图片数量为{PROJECT_BASELINE}'),
                'enabled': 1,
                'created_at': stamp,
                'updated_at': stamp,
            })
    return rows


# --------------------------------------------------------------------------
# 读路径：三级回退，绝不返回 None 静默通过
# --------------------------------------------------------------------------

def resolve(brand_id: str, platform: str, format_id: str, content_form: str):
    """按精确命中 → 通用模板 → 抛错的顺序取模板。

    绝不返回 None：模板缺失是配置问题，应当在编排层显式失败，而不是让下游
    拿到 None 后走「没有模板就跳过检查」的分支 —— 那会让审计静默失效。
    """
    from . import db

    normalized = formats.normalize_content_form(content_form)
    with db.transaction() as connection:
        exact = _fetch(connection, brand_id, platform, format_id, normalized)
        if exact is None:
            exact = _fetch(connection, brand_id, platform, format_id, '')
    if exact is None:
        raise LookupError(
            f"排版模板缺失：{platform}/{format_id}/{normalized or '通用'}；"
            f"请先执行种子写入，不要跳过模板检查")
    return exact


def _fetch(connection, brand_id, platform, format_id, content_form):
    row = connection.execute(
        "SELECT id,brand_id,platform,format_id,content_form,render,skeleton,title_style,"
        "media_spec,checks,provenance,enabled,created_at,updated_at FROM layout_templates "
        "WHERE brand_id=? AND platform=? AND format_id=? AND content_form=?",
        (brand_id, platform, format_id, content_form)).fetchone()
    return _view(row) if row else None


def _view(row) -> dict:
    return {
        'id': row['id'],
        'brand_id': row['brand_id'],
        'platform': row['platform'],
        'format_id': row['format_id'],
        'content_form': row['content_form'],
        'render': row['render'],
        'skeleton': json.loads(row['skeleton']),
        'title_style': json.loads(row['title_style']),
        'media_spec': json.loads(row['media_spec']),
        'checks': json.loads(row['checks']),
        'provenance': row['provenance'],
        'enabled': bool(row['enabled']),
    }


# --------------------------------------------------------------------------
# 写路径：幂等种子
# --------------------------------------------------------------------------

def seed(connection=None) -> dict:
    """把种子模板写入库。幂等，可重复执行。返回统计。"""
    from . import db

    context = db.transaction() if connection is None else None
    if context is None:
        return _seed_with(connection)
    with context as owned:
        return _seed_with(owned)


def _seed_with(connection) -> dict:
    brand_id = ensure_default(connection)
    rows = build_seed(brand_id)
    written = 0
    for row in rows:
        values = (row['render'], json.dumps(row['skeleton'], ensure_ascii=False),
                  json.dumps(row['title_style'], ensure_ascii=False),
                  json.dumps(row['media_spec'], ensure_ascii=False),
                  json.dumps(row['checks'], ensure_ascii=False), row['provenance'])
        existing = connection.execute(
            "SELECT render,skeleton,title_style,media_spec,checks,provenance FROM layout_templates WHERE id=?", (row['id'],)).fetchone()
        if existing:
            if tuple(existing) != values:
                connection.execute(
                    "UPDATE layout_templates SET render=?,skeleton=?,title_style=?,media_spec=?,"
                    "checks=?,provenance=?,updated_at=? WHERE id=?",
                    (*values, row['updated_at'], row['id']))
        else:
            connection.execute(
                "INSERT INTO layout_templates(id,brand_id,platform,format_id,content_form,"
                "render,skeleton,title_style,media_spec,checks,provenance,enabled,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row['id'], row['brand_id'], row['platform'], row['format_id'],
                 row['content_form'], row['render'],
                 json.dumps(row['skeleton'], ensure_ascii=False),
                 json.dumps(row['title_style'], ensure_ascii=False),
                 json.dumps(row['media_spec'], ensure_ascii=False),
                 json.dumps(row['checks'], ensure_ascii=False),
                 row['provenance'], row['enabled'], row['created_at'], row['updated_at']))
        written += 1
    return {'brand_id': brand_id, 'written': written}


def catalog(brand_id: str = None) -> list:
    """读出全部启用模板，按平台 + 叙事形式排序。纯读。"""
    from . import db

    brand_id = brand_id or DEFAULT_BRAND_ID
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT id,brand_id,platform,format_id,content_form,render,skeleton,title_style,"
            "media_spec,checks,provenance,enabled,created_at,updated_at FROM layout_templates "
            "WHERE brand_id=? AND enabled=1 ORDER BY platform, content_form", (brand_id,)).fetchall()
    return [_view(row) for row in rows]


def measure_body(template: dict, markdown: str) -> dict:
    """在编辑器可见文本上测量长度，而不是 Markdown 原文。

    Markdown 原文与可见文本长度差异明显（`**粗体**` 记 4 字符，可见文本记
    2 个字）。拿原文去估长度会系统性偏大，导致小红书这类短正文平台误报溢出。
    """
    platform = template['platform']
    visible = richtext.expected_visible_text(markdown, platform)
    ceiling = template['media_spec'].get('body_ceiling')
    return {
        'visible_chars': len(visible),
        'markdown_chars': len(markdown),
        'ceiling': ceiling,
        'over': bool(ceiling) and len(visible) > ceiling,
    }
