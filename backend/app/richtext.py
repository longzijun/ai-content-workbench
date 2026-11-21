"""把 Markdown 正文渲染成各平台编辑器实际能承载的格式。

背景（2026-10-14）
    生成器按提示词输出 Markdown 正文（adapters._prompt 明确要求 body 为可编辑
    Markdown），但三个平台原先都用 Playwright fill() 把原文当纯文本塞进编辑器，
    "## " 和 "**" 原样暴露在草稿里。本模块补齐"Markdown → 平台正文"的渲染环节。

三个平台的编辑器（2026-10-14 从实际页面确认）
    微信       .ProseMirror[contenteditable]      ProseMirror
    今日头条   div.ProseMirror[contenteditable]   ProseMirror
    小红书     .tiptap[contenteditable]           Tiptap（基于 ProseMirror）

    因为都是 ProseMirror 系，统一做法是把语义化 HTML 交给编辑器自身的 schema
    过滤：编辑器不认识或不允许的节点会被丢弃，不会报错。因此这里输出保守的
    语义标签，而不是平台私有的 class 或行内样式。

平台差异（决定渲染策略）
    微信公众号  正文没有标题语义，编辑器里的"标题"仍是段落，只靠加粗与留白
                体现层级 → 标题渲染为加粗段落。
    今日头条    文章支持小标题 → #/## 渲染为 h2，更深层级渲染为 h3。
    小红书      笔记正文本质是纯文本，不能承载富文本 → 降级为纯文本，用符号和
                空行保留可读结构，并确保不残留任何 Markdown 标记。

    上述平台行为依据当前代码中确认的编辑器类型与项目既有平台说明；真实平台的
    最终表现仍需在专用 Edge 中做一次端到端验收（见 README 的平台交付说明）。
"""
from __future__ import annotations

import html
import re

PLATFORMS = ("wechat", "toutiao", "xiaohongshu")

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET = re.compile(r"^[-*+]\s+(.+)$")
_ORDERED = re.compile(r"^(\d+)[.)]\s+(.+)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^([-*_])(?:\s*\1){2,}$")
_FENCE = re.compile(r"^\s*```")

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_CODE = re.compile(r"`([^`\n]+)`")


def _inline_html(text: str) -> str:
    """转义后渲染行内标记。顺序很重要：先转义，再生成标签。"""
    value = html.escape(text)
    value = _BOLD.sub(r"<strong>\1</strong>", value)
    value = _ITALIC.sub(r"<em>\1</em>", value)
    value = _CODE.sub(r"<code>\1</code>", value)
    return value


def _inline_plain(text: str) -> str:
    """去掉行内标记，保留文字本身。"""
    value = _BOLD.sub(r"\1", text)
    value = _ITALIC.sub(r"\1", value)
    value = _CODE.sub(r"\1", value)
    return value


def strip_markup(markdown: str) -> str:
    """去掉 Markdown 标记，得到编辑器应当显示的纯文本。

    用途是回读校验比对：富文本化之后编辑器的 innerText 不再等于 Markdown 原文，
    必须和这个结果比较。列表渲染成 <ul>/<ol> 后 innerText 不含项目符号或序号，
    因此这里也一并去掉，与编辑器实际可见文本对齐。
    """
    lines: list[str] = []
    in_code = False
    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            lines.append(line)
            continue
        if not line.strip():
            lines.append("")
            continue
        if _RULE.match(line.strip()):
            continue
        if match := _HEADING.match(line):
            lines.append(match.group(2).strip())
            continue
        if match := _BULLET.match(line):
            lines.append(match.group(1).strip())
            continue
        if match := _ORDERED.match(line):
            lines.append(match.group(2).strip())
            continue
        if match := _QUOTE.match(line):
            lines.append(match.group(1).strip())
            continue
        lines.append(line.strip())
    return _inline_plain("\n".join(lines)).strip()


def to_html(markdown: str, platform: str = "wechat") -> str:
    """渲染成 ProseMirror 可解析的语义 HTML（微信 / 今日头条）。"""
    if platform not in PLATFORMS:
        raise ValueError("不支持的平台")
    if platform == "xiaohongshu":
        raise ValueError("小红书正文为纯文本，请使用 to_text")

    blocks: list[str] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code = False
    list_kind: str | None = None
    list_items: list[str] = []
    quote_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_items:
            tag = list_kind or "ul"
            blocks.append("<" + tag + ">" + "".join("<li>" + item + "</li>" for item in list_items) + "</" + tag + ">")
            list_items.clear()
        list_kind = None

    def flush_quote() -> None:
        if quote_lines:
            inner = "".join("<p>" + line + "</p>" for line in quote_lines)
            blocks.append("<blockquote>" + inner + "</blockquote>")
            quote_lines.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()

        if _FENCE.match(line):
            flush_all()
            if in_code:
                blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines.clear()
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue

        if not line.strip():
            flush_all()
            continue

        if _RULE.match(line.strip()):
            flush_all()
            blocks.append("<hr>")
            continue

        if match := _HEADING.match(line):
            flush_all()
            text = _inline_html(match.group(2).strip())
            if platform == "toutiao":
                level = 2 if len(match.group(1)) <= 2 else 3
                blocks.append(f"<h{level}>{text}</h{level}>")
            else:
                # 微信公众号正文没有标题语义，用加粗段落表达层级
                blocks.append("<p><strong>" + text + "</strong></p>")
            continue

        if match := _BULLET.match(line):
            flush_paragraph()
            flush_quote()
            if list_kind not in (None, "ul"):
                flush_list()
            list_kind = "ul"
            list_items.append(_inline_html(match.group(1).strip()))
            continue

        if match := _ORDERED.match(line):
            flush_paragraph()
            flush_quote()
            if list_kind not in (None, "ol"):
                flush_list()
            list_kind = "ol"
            list_items.append(_inline_html(match.group(2).strip()))
            continue

        if match := _QUOTE.match(line):
            flush_paragraph()
            flush_list()
            quote_lines.append(_inline_html(match.group(1).strip()))
            continue

        flush_list()
        flush_quote()
        paragraph.append(_inline_html(line.strip()))

    flush_all()
    if code_lines:
        blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")

    return "\n".join(blocks)


def to_text(markdown: str, platform: str = "xiaohongshu") -> str:
    """降级为纯文本（小红书，或任何需要纯文本的场景）。

    小红书正文不能承载富文本，因此用空行分段的短行 + 符号化列表保留可读结构，
    同时确保不残留 Markdown 标记。标题折成独立短行并以顿号收尾，避免出现
    "#" 这类在笔记里显得像乱码的字符。
    """
    lines: list[str] = []
    in_code = False
    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            lines.append(line)
            continue
        if not line.strip():
            lines.append("")
            continue
        if _RULE.match(line.strip()):
            lines.append("————")
            continue
        if match := _HEADING.match(line):
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(_inline_plain(match.group(2).strip()))
            lines.append("")
            continue
        if match := _BULLET.match(line):
            lines.append("· " + _inline_plain(match.group(1).strip()))
            continue
        if match := _ORDERED.match(line):
            lines.append(f"{match.group(1)}. " + _inline_plain(match.group(2).strip()))
            continue
        if match := _QUOTE.match(line):
            lines.append("「" + _inline_plain(match.group(1).strip()) + "」")
            continue
        lines.append(_inline_plain(line.strip()))

    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed)


def render(markdown: str, platform: str) -> tuple[str, str]:
    """返回 (填入编辑器的方式, 渲染结果)。

    方式为 "html" 时结果交给 ProseMirror 粘贴渲染；为 "text" 时按纯文本填入。
    """
    if platform == "xiaohongshu":
        return "text", to_text(markdown, platform)
    return "html", to_html(markdown, platform)


def expected_visible_text(markdown: str, platform: str) -> str:
    """回读校验用的期望文本（编辑器可见文本）。

    小红书按纯文本填入，编辑器的 innerText 就等于 to_text 的结果；微信与头条
    渲染成富文本后，可见文本对应 strip_markup 的结果。
    """
    if platform == "xiaohongshu":
        return to_text(markdown, platform)
    return strip_markup(markdown)
