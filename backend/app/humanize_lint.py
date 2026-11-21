"""Deterministic editorial style diagnostics.

Mechanical phrasing, empty abstractions and weak specificity are warnings, not a
vocabulary ban. Format incompatibilities remain hard errors. Compliance and
first-person evidence checks live in editorial_rules and are combined by callers.
"""
from __future__ import annotations

import re

_CONNECTIVES = (
    ("首先", "其次"),
    ("一方面", "另一方面"),
    ("不仅", "而且"),
    ("不仅", "还"),
    ("不是", "而是"),
    ("综上所述",),
    ("在当今",),
    ("众所周知",),
    ("随着", "不断"),
)
_ABSTRACT = re.compile(r"赋能|闭环|抓手|底层逻辑|重大意义|迈上新台阶|保驾护航|未来可期|拭目以待|彰显了|体现了|折射出了|标志着")
_EMOTION = re.compile(r"高效|优质|领先|极致|强大|便捷|完美|卓越|震撼|惊艳")
_SUMMARY = re.compile(r"(?:总之|综上|所以|这就是|可见|归根结底)[^。！？\n]*[。！？]?$")
_MARKDOWN = {
    "heading": re.compile(r"(?m)^#{1,6}\s+"),
    "bold": re.compile(r"\*\*[^*\n]+\*\*"),
    "link": re.compile(r"!?\[[^\]]*\]\([^)]*\)"),
    "table": re.compile(r"(?m)^\s*\|.*\|\s*$"),
    "html": re.compile(r"<[A-Za-z][^>]*>"),
}
_FULL_BOLD = re.compile(r"(?m)^\s*\*\*[^*\n]+\*\*\s*$")
_INTERNAL_TRACKING_CLAIM = re.compile(r"TrendRadar\s*数据显示[，,]?\s*相关记录出现过\s*\d+\s*次")
_FETCH_HEAT_CLAIM = re.compile(r"最近一次抓取时间显示热度仍在持续")
_PROMPT_LEAK = re.compile(
    r"^\s*(?:只根据下面选题|材料边界：|表达要求：|合规红线：|已批准选题数据：)",
    re.MULTILINE,
)
_CLICHE = re.compile(r"在当今[^。\n]{0,30}时代|值得注意的是|总而言之|让我们一起")
_QUOTED = re.compile(r"“[^”\n]*”|‘[^’\n]*’|《[^》\n]*》|\"[^\"\n]*\"")


def _cliche_scan_text(text: str) -> str:
    """Exclude actual quotations and source titles from the four hard cliches."""
    without_sources = re.sub(r"(?m)^\s*(?:>|来源：).*$", "", text)
    return _QUOTED.sub("", without_sources)


def missing_concrete_reasons(text: str) -> list[str]:
    reasons = []
    if not re.search(r"截图|原始输出|工具|按钮|菜单|设置|导入|导出|步骤|流程|用法|操作|输入|选择|点击|复制|新建|运行|配置|提示词", text):
        reasons.append("缺少具体工具、操作、截图或原始输出")
    if not re.search(r"\d|结果|前是|后是|之前|之后|实测|测试|输出|报错|成功|失败|耗时|分钟|次|个|条|份|页", text):
        reasons.append("缺少真实结果、数字或可对照的前后变化")
    return reasons


def check(text: str, fmt: str = "article") -> tuple[list[str], list[str]]:
    if not text:
        return [], []
    violations: list[str] = []
    advisories: list[str] = []
    if fmt == "note":
        for label, pattern in _MARKDOWN.items():
            if pattern.search(text):
                violations.append(f"小红书正文不使用{label}标记")
    else:
        if _MARKDOWN["link"].search(text):
            violations.append("出现链接或图片语法，渲染器不支持；出处写成来源说明")
        if _MARKDOWN["table"].search(text):
            violations.append("出现表格语法，渲染器不支持，改为列表或短句")
        if _MARKDOWN["html"].search(text):
            violations.append("出现行内 HTML 标签，会被字面转义成正文文字")
        if _FULL_BOLD.search(text):
            violations.append("存在整段加粗；加粗只用于句中关键短语")
    if _INTERNAL_TRACKING_CLAIM.search(text) or _FETCH_HEAT_CLAIM.search(text):
        violations.append("把内部采集记录或抓取时间当成正文热度证据；请改用原始事实与出处")
    prompt_leak = _PROMPT_LEAK.search(text)
    if prompt_leak:
        violations.append("正文复述了执行提示词或规则标签；删除这些指令，只保留读者要看的内容。命中片段：" + prompt_leak.group(0).strip())
    for cliche in _CLICHE.finditer(_cliche_scan_text(text)):
        violations.append(f"出现套话「{cliche.group(0)}」；改为具体事实、过程或直接判断")
    for words in _CONNECTIVES:
        if all(word in text for word in words):
            advisories.append("检查机械连接词或固定句式：" + "、".join(words) + "；有真实逻辑可保留")
            break
    if _ABSTRACT.search(text):
        advisories.append("检查空泛抽象词；若不能换成具体动作、对象或结果，建议删改")
    if len(_EMOTION.findall(text)) >= 3:
        advisories.append("空泛情绪词偏多；优先用事实、过程或明确判断替代")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    summaries = sum(1 for part in paragraphs if _SUMMARY.search(part))
    if len(paragraphs) >= 3 and summaries >= 2:
        advisories.append("多个段落以总结句收尾，检查是否形成机械模板")
    for reason in missing_concrete_reasons(text):
        advisories.append("具体内容不足：" + reason)
    if fmt == "note" and len(re.findall(r"[😀-🙏🌀-🫿✨🔥📌✅❗️💰]", text)) > 3:
        advisories.append("小红书 emoji 偏多；只保留确有信息作用的使用")
    return list(dict.fromkeys(violations)), list(dict.fromkeys(advisories))
