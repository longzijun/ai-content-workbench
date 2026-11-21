"""内容形式注册表：把「形式」拆成两条正交轴。

A 轴（渠道形态）= work.format，取值 article/note，由 platform_rules 唯一裁决；
本模块只做引用与展示，不复制数值、不参与判定。
B 轴（叙事形式）= work.content_form，取值见 CONTENT_FORM_IDS，是组织建议。

边界 —— 本模块不做的事：
  不校验生成输出，不读数据库，不联网，不改写 work；
  不决定平台硬约束，不参与 platform_rules.work_errors；
  B 轴只出 warning，绝不阻断草稿、不触发生成重写循环；
  不提供 render 字段：渲染方式由 richtext.render 只按 platform 分派，
  其签名里根本没有 format 参数（见 test_richtext_stays_single_axis）。

命名提醒：capabilities.py 里的局部变量 content_format 取值是
article/note/short_post，那是 A 轴，与本模块的 content_form 不是一回事。

正交性由依赖图保证：axis_a_* 只读 platform_rules，get_content_form 只读
_CONTENT_FORMS，两者之间没有交叉调用。
"""
from __future__ import annotations

from copy import deepcopy

from .platform_rules import NAMES, TITLE_LIMITS, expected_format
from . import platform_registry as _platform_registry

# --------------------------------------------------------------------------
# A 轴（渠道形态）：全私有，只经 getter 暴露
# --------------------------------------------------------------------------

_AXIS_A = {
    'article': {'label': '文章'},
    'note': {'label': '笔记'},
    'video_script': {'label': '短视频脚本'},
}


def axis_a_for(platform):
    """A 轴裁决。直接委托 platform_rules，本模块不重算。未知平台抛 ValueError。"""
    return expected_format(platform)


def axis_a_platforms(format_id):
    """该渠道形态覆盖的平台元组，顺序取自 TITLE_LIMITS。未知形态抛 ValueError。

    由权威程序化派生而非写死，否则 platform_rules 改了这里会静默脱钩。
    """
    if format_id not in _AXIS_A:
        raise ValueError('不支持的渠道形态')
    return tuple(p for p in _platform_registry.PLATFORM_IDS if expected_format(p) == format_id)


def axis_a_entry(format_id):
    """A 轴条目深拷贝，含 id / label / 平台名。未知形态抛 ValueError。"""
    if format_id not in _AXIS_A:
        raise ValueError('不支持的渠道形态')
    result = deepcopy(_AXIS_A[format_id])
    platforms = axis_a_platforms(format_id)
    result.update(id=format_id, platforms=platforms,
                  platform_names=[NAMES[p] for p in platforms])
    return result


# --------------------------------------------------------------------------
# B 轴（叙事形式）：全私有，只经 getter 暴露
# --------------------------------------------------------------------------

_CONTENT_FORMS = {
    'tutorial': {
        'label': '步骤教程',
        'summary': '按可执行步骤组织，先给结论再给操作顺序。',
        'complexity': 'high',
        'title_window': (10, 24),
        'sources_min': 1,
        'items_min': 3,
        'boost': ('wechat', 'toutiao'),
    },
    'opinion': {
        'label': '观点短评',
        'summary': '以判断和论据为主，不追求完整流程覆盖。',
        'complexity': 'low',
        'title_window': (8, 20),
        'sources_min': 1,
        'items_min': 0,
        'boost': ('toutiao', 'xiaohongshu'),
    },
    'checklist': {
        'label': '清单速查',
        'summary': '可逐条核对的并列项，条目即卖点。',
        'complexity': 'medium',
        'title_window': (8, 20),
        'sources_min': 1,
        'items_min': 5,
        'boost': ('xiaohongshu', 'toutiao'),
    },
    'case': {
        'label': '案例拆解',
        'summary': '以一个真实案例为轴，拆背景、动作与结果。',
        'complexity': 'high',
        'title_window': (10, 26),
        # 这一形式最容易诱导模型编造经历，来源门槛最高。
        'sources_min': 2,
        'items_min': 3,
        'boost': ('wechat', 'toutiao'),
    },
    'brief': {
        'label': '要点速报',
        'summary': '压缩成要点速报，牺牲深度换取时效。',
        'complexity': 'low',
        'title_window': (8, 18),
        'sources_min': 1,
        'items_min': 3,
        'boost': ('toutiao',),
    },
}

# sorted 而非字典插入序：V2-04 直接拿它当 LLM schema 的 enum，顺序会进
# prompt 文本与缓存键。插入序会让「在中间加一条」悄悄改变已缓存请求的形状，
# 而 sorted() 只依赖标识符本身，两个进程构造出的 schema 必然逐字节相同。
CONTENT_FORM_IDS = tuple(sorted(_CONTENT_FORMS))

_COMPLEXITY_VALUES = frozenset({'low', 'medium', 'high'})


def get_content_form(content_form_id):
    """B 轴条目深拷贝，含 id。未知 id 抛 ValueError。"""
    key = normalize_content_form(content_form_id)
    if key not in _CONTENT_FORMS:
        raise ValueError('不支持的叙事形式')
    result = deepcopy(_CONTENT_FORMS[key])
    result.update(id=key)
    return result


def normalize_content_form(value):
    """把任意输入映射为注册表 id 或 ''（未指定）。全函数，永不抛错。

    与 _platform 范式的刻意差异：平台错了是硬失败，值得抛错；B 轴错值是
    软漂移（native 生成路径不强制 JSON schema，模型可能幻觉出该键），
    此时应降级为「未指定 + 一条 warning」，而不是让整个生成任务失败。
    """
    if not isinstance(value, str):
        return ''
    key = value.strip().casefold()
    return key if key in _CONTENT_FORMS else ''


def content_form_warnings(work):
    """B 轴建议，只出 warning 字符串列表。永不抛错、永不阻断。

    刻意返回纯字符串列表而非 platform_source_policy 的 findings 结构：
    对齐 platform_rules.work_errors 的形状，便于后续卡以 severity='warning'
    接入 service._rule_review。

    三条约定：
      1. 输入非 dict 抛 ValueError（对齐 work_errors 用 .get() 的契约）
      2. 不校验 platform —— 平台裁决权完全留给 platform_rules
      3. 未知值检测用 raw value，不拿归一后的结果，否则丢了「填了但填错」的信号
    """
    if not isinstance(work, dict):
        raise ValueError('稿件必须是字典')
    if work.get('platform') not in TITLE_LIMITS:
        # 平台裁决权完全留给 platform_rules。未知平台时不做任何 B 轴判断，
        # 避免 formats 成为第二个平台权威。
        return []
    raw = work.get('content_form')
    key = normalize_content_form(raw)
    label = _CONTENT_FORMS[key]['label'] if key else ''
    warnings = []
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return warnings
    if not key:
        warnings.append(f'叙事形式「{str(raw)[:60]}」不在注册表内，已按未指定处理；'
                        f'可用形式：{"、".join(CONTENT_FORM_IDS)}')
        return warnings
    entry = _CONTENT_FORMS[key]
    sources = work.get('source_urls')
    source_count = len(sources) if isinstance(sources, list) else 0
    if source_count < entry['sources_min']:
        warnings.append(
            f'{label}建议至少 {entry["sources_min"]} 条已批准来源，'
            f'当前 {source_count} 条；缺来源时请核对原文，不要靠印象补写')
    minimum = entry['items_min']
    if minimum:
        body = work.get('body')
        bullet_count = body.count('\n- ') + body.count('\n* ') if isinstance(body, str) else 0
        if bullet_count < minimum:
            warnings.append(
                f'{label}建议至少 {minimum} 个并列要点，当前 {bullet_count} 个；'
                f'要点不足时篇幅通常撑不住')
    return warnings


def format_catalog():
    """两条轴的只读快照，供前端与文档消费。整体 JSON 可序列化。"""
    axis_a_items = [axis_a_entry(key) for key in sorted(_AXIS_A)]
    return {
        'axis_a': {
            'label': '渠道形态',
            'field': 'format',
            'ids': sorted(_AXIS_A),
            'items': axis_a_items,
        },
        'axis_b': {
            'label': '叙事形式',
            'field': 'content_form',
            'ids': list(CONTENT_FORM_IDS),
            'items': [get_content_form(key) for key in CONTENT_FORM_IDS],
            'complexity_values': sorted(_COMPLEXITY_VALUES),
        },
    }
