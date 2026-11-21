"""Shared editorial gates for topics, generated text and version freezing."""
from __future__ import annotations

import json
import re
from typing import Iterable

from . import style_library

CONTENT_LINES = {"A", "B", "C"}
CONFIRMABLE_RISKS = {"finance", "current_affairs"}
RISK_LABELS = {"finance": "金融", "current_affairs": "时事", "crypto": "币圈/虚拟货币"}

#: 生成失败的稳定分类，前端与任务页据此给出定点修正动作，而不是把
#: 所有失败都叫作「格式问题」。是 ValueError 的子类，旧调用方的
#: `except ValueError` 与 `pytest.raises(ValueError)` 语义不受影响。
ERROR_FORMAT = "format"          # JSON 解析失败
ERROR_FIELD = "field"            # 字段结构缺失/不完整
ERROR_MATERIAL = "material"      # 材料缺口
ERROR_RISK = "risk"              # 事实/内容风险（荐股、医疗承诺、虚构经历等）
ERROR_STYLE = "style"            # 文风硬规则（加粗、禁用词等）
ERROR_CONNECTION = "connection"  # 模型连接/超时/服务未就绪
ERROR_CANCEL = "cancel"          # 用户取消
ERROR_CATEGORIES = (ERROR_FORMAT, ERROR_FIELD, ERROR_MATERIAL, ERROR_RISK, ERROR_STYLE, ERROR_CONNECTION, ERROR_CANCEL)


class GenerationError(ValueError):
    """带分类的生成失败。category 属于 ERROR_CATEGORIES。"""

    def __init__(self, category: str, message: str, *, platform: str | None = None):
        super().__init__(message)
        self.category = category
        self.platform = platform

_CRYPTO = re.compile(r"币圈|虚拟货币|加密货币|比特币|以太坊|狗狗币|稳定币|代币|NFT|Web3|炒币|区块链投资", re.I)
_FINANCE = re.compile(r"金融|荐股|股票|股市|A股|港股|美股|基金|理财|债券|汇率|央行|降息|牛市|熊市|目标价|满仓|空仓|抄底", re.I)
_CURRENT = re.compile(r"时事|新闻|热点|突发|政策|新规|官宣|发布会|近日|今天|昨天|本周|最新|地震|战争|选举|关税|监管|通报", re.I)
_MEDICAL_CONTEXT = re.compile(r"医疗|患者|病人|药品?|器械|治疗|疗效|临床|病症|疾病|鼻炎|糖尿病")
_MEDICAL_CLAIM = re.compile(r"治愈|根治|药到病除|保证疗效|百分百有效|100%有效|疗效显著|有效率|治愈率|替代医生|包好")
_INVESTMENT = re.compile(r"荐股|投资建议|推荐(?:买入|股票)|买入.{0,16}(?:股票|基金)|(?:股票|基金).{0,16}(?:必涨|稳赚|翻倍|保本)|具体买点|目标价|满仓|空仓|抄底")
_FIRST_PERSON = re.compile(r"我(?:亲测|实测|测了|试了|用了|用)|去年我|昨天我")
_COMPANY = re.compile(r"[\u4e00-\u9fff]{2,30}(?:股份有限公司|有限责任公司|有限公司|集团)")
_INTERNAL = re.compile(r"项目代号|内部代号|代号[：:]|内部资料|内部文件|保密|客户资料|未公开")

#: 否定/免责语境。命中这些词说明正文是在「不建议做什么」或「讨论某概念」，
#: 不是真的在荐股/承诺疗效/虚构亲测，不能当硬阻断（降级为提醒人工复核）。
#: 只向前看最后一个子句：否定词与命中词之间若有句读，否定的是别处，不算数。
_NEGATION_WORD = re.compile(r"(?:不|别|勿|莫|没|未|非|避免|切勿|杜绝|拒绝|规避|不宜|不得|不要|不应|不能|不可|不是|莫要|慎)")
_CLAUSE_BOUNDARY = "，。；！？\n、,;!?"
_QUOTE_OPEN = "'“‘「『\""
_QUOTE_CLOSE = "'”’」』\""


def _negated_or_quoted(text: str, start: int, end: int) -> bool:
    """判断一次规则命中是否处于否定或引用/讨论语境（不构成实际建议/承诺）。

    只做**局部**判断：否定词必须与命中词在同一子句内；引号必须正好包裹命中词。
    一篇正文里其他位置的真实建议不会被一句免责声明放过。
    """
    before = text[max(0, start - 16):start]
    last_boundary = max((i for i, ch in enumerate(before) if ch in _CLAUSE_BOUNDARY), default=-1)
    clause = before[last_boundary + 1:]
    if _NEGATION_WORD.search(clause):
        return True
    if start > 0 and text[start - 1] in _QUOTE_OPEN and end < len(text) and text[end] in _QUOTE_CLOSE:
        return True
    return False


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def topic_text(topic: dict) -> str:
    sources = _json_list(topic.get("sources"))
    source_text = "\n".join(
        f"{item.get('title', '')}\n{item.get('note', '')}" for item in sources if isinstance(item, dict)
    )
    return "\n".join(str(topic.get(key) or "") for key in ("title", "brief")) + "\n" + source_text


def topic_risks(topic: dict) -> list[str]:
    text = topic_text(topic)
    risks = []
    if _CRYPTO.search(text):
        risks.append("crypto")
    if _FINANCE.search(text):
        risks.append("finance")
    if _CURRENT.search(text):
        risks.append("current_affairs")
    return risks


def required_confirmations(topic: dict) -> list[str]:
    return [risk for risk in topic_risks(topic) if risk in CONFIRMABLE_RISKS]


def generation_errors(topic: dict) -> list[str]:
    errors = []
    if topic.get("content_line") not in CONTENT_LINES:
        errors.append("请先明确选择 A/B/C 内容线；旧选题不会自动猜测内容线")
    if "crypto" in topic_risks(topic):
        errors.append("币圈或虚拟货币类选题已阻断")
    required = set(required_confirmations(topic))
    confirmation = topic.get("risk_confirmation")
    if isinstance(confirmation, str):
        try:
            confirmation = json.loads(confirmation)
        except ValueError:
            confirmation = None
    confirmed = set(confirmation.get("risks") or []) if isinstance(confirmation, dict) else set()
    if required and (
        not isinstance(confirmation, dict)
        or confirmation.get("revision") != topic.get("revision")
        or confirmed != required
    ):
        labels = "、".join(RISK_LABELS[risk] for risk in required_confirmations(topic))
        errors.append(f"当前选题涉及{labels}，请对当前修订明确确认后再生成；修订后需重新确认")
    stance_ids = topic.get("stance_ids") or []
    if isinstance(stance_ids, str):
        stance_ids = _json_list(stance_ids)
    if not isinstance(stance_ids, list) or len(set(stance_ids)) != len(stance_ids):
        errors.append("观点 ID 列表无效或重复")
    else:
        confirmed_stances = style_library.confirmed_stances()
        unknown = [identifier for identifier in stance_ids if identifier not in confirmed_stances]
        if unknown:
            errors.append("存在未确认或无效的观点 ID：" + "、".join(str(item) for item in unknown))
    return errors


def _material_text(topic: dict) -> str:
    sources = _json_list(topic.get("sources"))
    documents = []
    research = topic.get("_research")
    if isinstance(research, dict):
        documents = research.get("documents") or []
    return json.dumps(
        {
            "title": topic.get("title"),
            "brief": topic.get("brief"),
            "sources": sources,
            "fact_base": topic.get("_fact_base"),
            "research": documents,
        },
        ensure_ascii=False,
    )


def text_findings(body: str, topic: dict) -> list[dict]:
    """Return deterministic blocker/warning findings for one body."""
    findings = []

    def add(severity: str, message: str):
        if not any(item["severity"] == severity and item["message"] == message for item in findings):
            findings.append({"severity": severity, "message": message})

    if _CRYPTO.search(body):
        add("blocker", "正文涉及币圈或虚拟货币内容。")
    for match in _INVESTMENT.finditer(body):
        if _negated_or_quoted(body, match.start(), match.end()):
            add("warning", "正文出现投资相关表述（否定或引用语境），请人工确认不是变相荐股或具体投资建议。")
        else:
            add("blocker", "正文涉及荐股或具体投资建议。")
    for match in _MEDICAL_CLAIM.finditer(body):
        if _MEDICAL_CONTEXT.search(body) and not _negated_or_quoted(body, match.start(), match.end()):
            add("blocker", "正文涉及医疗效果宣传或疗效承诺。")
    if _FIRST_PERSON.search(body) and not _FIRST_PERSON.search(_material_text(topic)):
        add("blocker", "正文出现材料未提供的第一人称经历或实测；请补充真实材料，或改为客观表达。")
    if _COMPANY.search(body):
        add("warning", "正文疑似出现公司名，请确认已按作者要求模糊处理。")
    if _INTERNAL.search(body):
        add("warning", "正文疑似出现项目代号、内部资料或客户信息，请确认可公开。")
    return findings


def risk_labels(risks: Iterable[str]) -> str:
    return "、".join(RISK_LABELS.get(risk, risk) for risk in risks)
