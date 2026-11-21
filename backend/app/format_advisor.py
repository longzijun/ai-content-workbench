"""AI 推荐器：在规则引擎给出的可行集合内排序，不扩大集合。

与 format_rules 的分工：
  format_rules 决定「哪些形式可行」——硬约束，不可协商，纯函数，可单测。
  本模块决定「可行的那几个里先写哪个」——软排序，可以用模型。

关键设计 —— enum 由可行集合生成：
strict JSON Schema 的 enum 直接取自 format_rules 输出的 feasible 列表，
因此模型在受约束解码下**物理上无法**推荐一个被硬约束排除的形式。这不是
「校验后拒绝」，而是「根本生成不出来」。降级路径（native 不强制 schema）
下仍会返回结构化校验，越界一律整体丢弃而不是静默纠正。

为什么不切 Codex 兜底：
  一次 200 token 的 JSON 排序不值得拉起 60 秒的沙箱子进程；而且
  adapters._run_codex 会写 codex-last-success.json，那会让「建议」与
  「生成」在审计记录上混同 —— 本模块的结论必须能独立追溯。

边界 —— 本模块不做的事：
  不扩大可行集合、不改硬约束、不写数据库、不产生草稿、不决定发布时机；
  不做形式本身该有什么内容（那是 formats 注册表与生成提示的事）。
  模型不可用时降级为规则排序，不静默失败、也不假装成功。
"""
from __future__ import annotations

import json

from . import formats
from .local_generation import parse_model_json

#: 排序任务要的是稳定与可复现，不是文采。低于正文生成的 0.4。
ADVISOR_TEMPERATURE = 0.2
#: 一次 JSON 决策的输出上限，足够放推荐 + 理由 + 备选。
ADVISOR_MAX_TOKENS = 700
#: 备选最多给几个。给多了用户不会用，反而稀释第一个的注意力。
MAX_ALTERNATIVES = 2
#: confidence 的取值域。schema 会限制，但代码里也要独立校验。
MIN_CONFIDENCE = 0
MAX_CONFIDENCE = 100
#: 低于此值视为「模型自己也没把握」，降级为规则排序。
LOW_CONFIDENCE = 40


class AdvisorError(Exception):
    """模型返回无法采信。调用方应降级而不是重试。"""


def _schema(feasible: list[str]) -> dict:
    """只允许模型在可行集合内选择。

    每个 object 层都关掉 additionalProperties 且要求全部字段，
    这样 strict 模式下受约束解码不会产出多余或缺失的键。
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommended": {"type": "string", "enum": list(feasible)},
            "confidence": {"type": "integer", "minimum": MIN_CONFIDENCE, "maximum": MAX_CONFIDENCE},
            "reason": {"type": "string"},
            "alternatives": {
                "type": "array",
                "items": {"type": "string", "enum": list(feasible)},
                "maxItems": MAX_ALTERNATIVES,
            },
            "questions_for_user": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
        },
        "required": ["recommended", "confidence", "reason", "alternatives", "questions_for_user"],
    }


def _instruction(feasible: list[str], labels: dict[str, str]) -> str:
    listing = "、".join(f"{item}({labels[item]})" for item in feasible)
    return (
        "你在为一个本地自媒体运营工作台挑选稿件的叙事形式。"
        "下面是已经过规则筛选的可行形式，只需要在其中排序，不要提出集合外的形式。\n"
        f"可选形式：{listing}\n"
        "选最合适的一个作为 recommended，给出 0 到 100 的整数 confidence 说明把握程度，"
        "用一句话写 reason 解释为什么它更适合这个选题。"
        "alternatives 最多给 2 个备选（必须不同于 recommended，且都在可选列表内）。"
        "若选题信息不足以判断，把需要作者确认的问题写进 questions_for_user，最多 3 条；"
        "没有疑问就给空数组。不要编造选题里没有的事实、经历、数据或平台规则。"
        "只返回 JSON，不要输出思考过程。\n"
        f"必须符合此 JSON Schema：{json.dumps(_schema(feasible), ensure_ascii=False)}"
    )


def build_request(feasible, labels, topic, *, model, native=False) -> dict:
    """构造请求体。双协议与 local_generation 同构。

    native（LM Studio 原生协议）只把 schema 拼进 prompt 文本、不强制约束解码，
    约束力弱于 strict。排序任务要的是确定性的 enum 约束，因此默认不用它；
    调用方明确需要时可传 native=True。协议选择刻意不做能力探测 —— 那需要发
    一次请求，会让「模型不可用」与「模型返回无效」两种降级混在一起。
    """
    instruction = _instruction(feasible, labels)
    body = (f"选题：{topic.get('title', '')}\n"
            f"说明：{topic.get('body', '')}\n")
    # 协议选择刻意不做能力探测：判断服务是否支持原生协议需要发一次请求，
    # 那会让「模型不可用」与「模型返回无效」两种降级混在一起，破坏
    # advise() 的四级降级契约。默认走 OpenAI 兼容路径（strict 约束解码，
    # 约束力最强）；调用方若明确需要原生协议，可传 native=True。
    if native:
        return {'model': model, 'input': body + instruction,
                'reasoning': 'off', 'store': False, 'integrations': [], 'stream': True,
                'temperature': ADVISOR_TEMPERATURE, 'max_output_tokens': ADVISOR_MAX_TOKENS}
    return {'model': model,
            'messages': [{'role': 'user', 'content': body + instruction}],
            'temperature': ADVISOR_TEMPERATURE, 'max_tokens': ADVISOR_MAX_TOKENS,
            'stream': True,
            'response_format': {'type': 'json_schema',
                                'json_schema': {'name': 'format_advice', 'strict': True,
                                                'schema': _schema(feasible)}}}


def validate(payload, feasible: list[str]) -> dict:
    """校验模型输出。四条约束，任何一条不满足就整体丢弃。

    1. recommended 必须落在可行集合内。越界**整体丢弃**而不是静默纠正 ——
       静默纠正会让 advisor_reason 描述的理由与实际结论对不上，结论就无法追溯。
    2. confidence 必须是真 int。用 type(x) is not int 而不是 isinstance，
       因为 Python 里 bool 是 int 的子类，True 会被当成 1。
    3. 边界在代码里独立校验，不依赖 schema —— 降级路径（native）根本不强制 schema。
    4. 备选裁到 MAX_ALTERNATIVES，且不得包含 recommended 自身。
    """
    if not isinstance(payload, dict):
        raise AdvisorError("推荐结果不是对象")
    recommended = payload.get('recommended')
    if not isinstance(recommended, str) or recommended not in feasible:
        raise AdvisorError(f"推荐形式 {recommended!r} 不在可行集合内")
    confidence = payload.get('confidence')
    if type(confidence) is not int:
        raise AdvisorError("信心值必须是整数")
    if not MIN_CONFIDENCE <= confidence <= MAX_CONFIDENCE:
        raise AdvisorError(f"信心值 {confidence} 超出 {MIN_CONFIDENCE} 到 {MAX_CONFIDENCE}")
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise AdvisorError("缺少推荐理由")
    raw_alternatives = payload.get('alternatives')
    if not isinstance(raw_alternatives, list):
        raise AdvisorError("备选必须是数组")
    alternatives = []
    for item in raw_alternatives:
        if isinstance(item, str) and item in feasible and item != recommended:
            if item not in alternatives:
                alternatives.append(item)
    alternatives = alternatives[:MAX_ALTERNATIVES]
    questions = payload.get('questions_for_user')
    if not isinstance(questions, list):
        questions = []
    questions = [str(item).strip()[:200] for item in questions if str(item).strip()][:3]
    return {'recommended': recommended, 'confidence': confidence,
            'reason': reason.strip()[:300], 'alternatives': alternatives,
            'questions_for_user': questions}


def parse(raw: str, feasible: list[str]) -> dict:
    """解析模型原始输出并校验。

    小模型的机械 JSON 故障先做一次修复（复用 local_generation，不另造一套）。
    解析彻底失败时统一转成 AdvisorError —— 否则 JSONDecodeError 会越过
    advise() 的降级捕获直接冒泡，把一次「模型输出不可用」变成流程崩溃。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise AdvisorError("本地模型没有返回内容")
    try:
        payload = parse_model_json(raw)
    except ValueError as error:
        raise AdvisorError(f'模型输出不是可解析的 JSON：{str(error)[:100]}') from error
    return validate(payload, feasible)


def rule_ranked(evaluation: dict) -> list[str]:
    """规则引擎的排序结果。这是降级目标，也是模型不可用时的唯一依据。"""
    return [row['content_form'] for row in evaluation.get('feasible', [])]


def fallback(evaluation: dict, status: str, detail: str = '') -> dict:
    """模型不可用时的确定性结论。用规则排序，不假装有模型参与。"""
    ranked = rule_ranked(evaluation)
    top = ranked[0] if ranked else ''
    return {
        'recommended': top,
        'confidence': 0,
        'reason': detail or ('本地模型不可用，按规则引擎排序取第一项' if top else '没有可行形式'),
        'alternatives': ranked[1:1 + MAX_ALTERNATIVES],
        'questions_for_user': [],
        'status': status,
        'rule_ranked': ranked,
    }


def advise(evaluation: dict, topic: dict, *, settings=None, ask_model) -> dict:
    """在规则引擎结论之上给出推荐。ask_model(request) 返回模型原始文本。

    ask_model 由调用方注入（通常是 local_generation.stream_payload_with_retry
    的同步包装）。本模块不自己发请求 —— 那样既难测，也会让「模型不可用」
    与「模型返回无效」两种降级混在一起。

    四级降级：
      local        模型给出可用结论
      unavailable  模型服务不可用，用规则排序
      failed       模型返回无法采信，用规则排序
      rule_only    可行集合为空，一个都不调
    """
    ranked = rule_ranked(evaluation)
    if not ranked:
        # 可行集合为空时一次模型请求都不发。空集合是硬约束结论，
        # 让模型「在没有选项的情况下选一个」只会诱导编造。
        return fallback(evaluation, 'rule_only', '没有可行形式，规则引擎已排除全部候选')

    if settings is None or ask_model is None:
        return fallback(evaluation, 'unavailable', '未提供本地模型连接，按规则引擎排序取第一项')

    try:
        request = build_request(ranked,
                                {item: formats.get_content_form(item)['label'] for item in ranked},
                                topic, model=_model_name(settings))
    except Exception as error:
        return fallback(evaluation, 'unavailable', f'未能构造推荐请求：{str(error)[:120]}')

    try:
        raw = ask_model(request)
    except Exception as error:
        return fallback(evaluation, 'unavailable', f'本地模型未响应：{str(error)[:120]}')

    try:
        result = parse(raw, ranked)
    except AdvisorError as error:
        return fallback(evaluation, 'failed', f'模型输出无法采信，已改用规则排序：{str(error)[:120]}')

    if result['confidence'] < LOW_CONFIDENCE:
        # 模型自己没把握时不该让它主导结论：保留它的理由，但推荐回到规则第一位。
        return {
            **result,
            'recommended': ranked[0],
            'alternatives': ranked[1:1 + MAX_ALTERNATIVES],
            'status': 'local',
            'rule_ranked': ranked,
            'note': f'模型信心值 {result["confidence"]} 低于 {LOW_CONFIDENCE}，'
                    f'已改用规则排序第一项；模型理由保留供参考',
        }
    return {**result, 'status': 'local', 'rule_ranked': ranked}


def _model_name(settings) -> str:
    """跟随 LM Studio 当前已加载的实例，不自行指定档位（与正文生成一致）。"""
    try:
        from .adapters import _loaded_local_model_info
        info = _loaded_local_model_info(settings)
    except Exception:
        info = {}
    name = str(info.get('id') or '').strip()
    if not name:
        raise AdvisorError('本地模型未加载，请在 LM Studio 中加载模型后重试')
    return name
