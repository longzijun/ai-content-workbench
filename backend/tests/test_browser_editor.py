"""正文写入策略测试：富文本粘贴优先，纯文本回退，任何路径都不残留 Markdown 标记。

对应 2026-10-14 的修复：三个平台原先用 fill() 写入 Markdown 原文，导致
"## " 与 "**" 出现在平台草稿里。
"""
from backend.app import browser_editor, richtext

MARKDOWN = "## 小标题\n\n**加粗**内容。\n\n- 一\n- 二"


class Field:
    """模拟 ProseMirror 编辑器：接受粘贴或拒绝。"""

    def __init__(self, accept_paste=True):
        self.accept_paste = accept_paste
        self.filled = []
        self.pasted = []

    def fill(self, value, **kwargs):
        self.filled.append(value)

    def evaluate(self, expression, **kwargs):
        assert "ClipboardEvent" in expression, "只有粘贴注入才应调用 evaluate"
        if not self.accept_paste:
            return False
        self.pasted.append(kwargs["arg"])
        return True


def test_wechat_pastes_rich_text_and_never_fills():
    field = Field()
    kind, value = browser_editor.write_body(field, "wechat", MARKDOWN, richtext)
    assert kind == "html"
    assert field.filled == []
    assert field.pasted == [value]
    assert "##" not in value and "**" not in value
    assert "<strong>" in value


def test_toutiao_pastes_rich_text_with_headings():
    field = Field()
    kind, value = browser_editor.write_body(field, "toutiao", MARKDOWN, richtext)
    assert kind == "html"
    assert field.filled == []
    assert "<h2>" in value and "##" not in value


def test_xiaohongshu_writes_plain_text_without_markers():
    field = Field()
    kind, value = browser_editor.write_body(field, "xiaohongshu", MARKDOWN, richtext)
    assert kind == "text"
    assert field.pasted == []
    assert field.filled == [value]
    assert "##" not in value and "**" not in value


def test_falls_back_to_plain_text_when_editor_rejects_paste():
    """编辑器不接受粘贴时回退为去标记的纯文本，绝不回退到 Markdown 原文。"""
    field = Field(accept_paste=False)
    kind, value = browser_editor.write_body(field, "wechat", MARKDOWN, richtext)
    assert kind == "text"
    assert field.pasted == []
    assert field.filled == [value]
    assert "##" not in value and "**" not in value
    assert value == richtext.strip_markup(MARKDOWN)


def test_evaluate_failure_is_treated_as_rejection_not_crash():
    class Broken(Field):
        def evaluate(self, expression, **kwargs):
            raise RuntimeError("page detach")

    field = Broken()
    kind, value = browser_editor.write_body(field, "wechat", MARKDOWN, richtext)
    assert kind == "text" and field.filled == [value]


def test_empty_markdown_never_pastes():
    field = Field()
    kind, value = browser_editor.write_body(field, "wechat", "   ", richtext)
    assert kind == "text" and field.pasted == []
