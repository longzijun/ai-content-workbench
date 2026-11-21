"""在平台富文本编辑器中注入正文。

三个平台的正文编辑器都是 ProseMirror 系（微信/头条的 .ProseMirror，小红书的
.tiptap）。Playwright 的 fill() 只能写入纯文本，Markdown 标记会原样暴露；正
确做法是投递一次携带 text/html 的粘贴事件，由编辑器按自身 schema 解析——
编辑器允许的节点会保留，不认识的会被丢弃，不需要针对每个平台做私有适配。

注意：调用方拿到 False 时应回退为纯文本写入，避免正文为空。
"""
from __future__ import annotations

# 返回 true 表示编辑器接受了这次粘贴（ProseMirror 处理时会 preventDefault）。
_PASTE = r"""(node, markup) => {
    if (!node || node.getAttribute('contenteditable') !== 'true') return false;
    node.focus();
    let data;
    try {
        data = new DataTransfer();
    } catch (error) {
        return false;
    }
    data.setData('text/html', markup);
    data.setData('text/plain', '');
    let event;
    try {
        event = new ClipboardEvent('paste', {
            clipboardData: data, bubbles: true, cancelable: true, composed: true,
        });
    } catch (error) {
        return false;
    }
    const notHandled = node.dispatchEvent(event);
    return notHandled === false;
}"""


def paste_html(field, markup: str) -> bool:
    """把语义 HTML 粘进编辑器。返回编辑器是否接受。"""
    if not markup:
        return False
    try:
        return bool(field.evaluate(_PASTE, arg=markup))
    except Exception:
        return False


def write_body(field, platform: str, markdown: str, rich_text) -> tuple[str, str]:
    """按平台能力写入正文，返回 (实际使用的方式, 写入内容)。

    富文本编辑器优先用粘贴注入；编辑器不接受时回退为去掉标记的纯文本，
    任何情况下都不再把 Markdown 标记原样写进平台。
    """
    kind, rendered = rich_text.render(markdown, platform)
    if kind == "html" and paste_html(field, rendered):
        return "html", rendered
    plain = rich_text.strip_markup(markdown) if kind == "html" else rendered
    field.fill(plain)
    return "text", plain
