"""把格式决策落到作品上。纯逻辑，不读库、不联网。

为什么由工作台写入而不是让模型自己填：
adapters._schema() 的 works 属性里没有 content_form，且该 schema 用
strict + additionalProperties=False 走受约束解码 —— 模型在物理上不可能
输出这个键。所以 B 轴只能由工作台在模型返回之后写进去。这也顺带避免了
「模型自己编一个叙事形式」的漂移。

为什么空串是正常值：
content_form 缺省即「未指定」，不是错误。老版本 payload 不含该键，读取时
归一为空串，两条路径在落库后长得一样。

本模块不做的事：
  不判断该不该选某个形式（那是 format_rules 的硬约束）；
  不排序推荐（那是 format_advisor）；不写数据库、不改版本 hash 之外的任何状态。
"""
from __future__ import annotations

from . import formats

#: 决策字段在选题 dict 里的键名。调用方（service / 未来的排期器）预置，
#: generate_platforms 只读不写这个键。
DECISION_KEY = '_format_decision'


def decision_for(topic: dict, platform: str) -> dict:
    """取出某平台预置的形式决策。没有就用空决策，不猜。"""
    decision = topic.get(DECISION_KEY)
    if not isinstance(decision, dict):
        return {}
    per_platform = decision.get('platforms')
    if isinstance(per_platform, dict) and isinstance(per_platform.get(platform), dict):
        return per_platform[platform]
    return {}


def resolve(topic: dict, platform: str, work: dict) -> str:
    """决定这篇 work 的 content_form。

    优先级：
      1. work 上已有非空 content_form（人工在编辑器里指定的，优先）
      2. 选题上为该平台预置的决策
      3. 决策里的形式不在注册表内 → 归一为空串（不静默改写成别的形式）
      4. 以上都没有 → 空串
    """
    existing = work.get('content_form')
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    adopted = decision_for(topic, platform).get('adopted')
    return formats.normalize_content_form(adopted)


def apply_content_forms(topic: dict, payload: dict, platform: str | None = None) -> list[str]:
    """把工作台叙事形式写回每件作品，返回被写入的平台。

    两条生成通道共用：本地通道在平台循环里调，人工交接在冻结版本前调。
    不共用的后果是 WorkBuddy 稿按指定结构写成、落库却显示“未指定叙事形式”，
    后续拿不到对应的形式提示，也无法按形式统计。

    `platform` 用于本地通道显式指定当前平台；缺省时取 work 自带的 platform。
    """
    touched = []
    for work in (payload or {}).get('works') or []:
        if not isinstance(work, dict):
            continue
        resolved = resolve(topic, platform or work.get('platform'), work)
        if work.get('content_form') != resolved:
            work['content_form'] = resolved
            touched.append(str(work.get('platform') or ''))
    return touched


def warnings_for(work: dict) -> list[str]:
    """B 轴证据门槛的告警。只在真的指定了形式时才可能非空。"""
    return formats.content_form_warnings(work)


def hint_for(topic: dict, platform: str) -> str | None:
    """给生成模型看的叙事形式提示。没有决策时返回 None —— 调用方据此
    决定「不注入」，而不是注入一个空字符串。

    提示文本只说「按什么结构组织」，不规定具体结论或事实：内容判断仍在模型，
    而形式的组织方式由决策引擎给定 —— 两者职责不能混。
    """
    decision = decision_for(topic, platform)
    if not decision:
        return None
    adopted = formats.normalize_content_form(decision.get('adopted'))
    if not adopted:
        return None
    entry = formats.get_content_form(adopted)
    window = entry['title_window']
    parts = [
        f'本次【{entry["label"]}】形式创作：{entry["summary"]}',
        f'标题建议 {window[0]} 到 {window[1]} 个汉字，仍须满足平台硬性上限。',
    ]
    if adopted == 'case':
        parts.append('只用来源里确有的案例；来源不支持的经历一律不写，宁可留空。')
    if entry['sources_min']:
        parts.append(f'至少引用 {entry["sources_min"]} 条已批准来源；缺来源的论断不要下。')
    return ''.join(parts)