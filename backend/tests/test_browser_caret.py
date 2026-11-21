"""光标定位与插图后校验：守住「图片不能覆盖正文」这条。

2026-10-22 的真实事故：头条写入 `fields-ok body_len=459 img=True`，看起来
全对；实际草稿里前三段被三张图覆盖，只剩后半段。原因是插图前用了
`body_field.click()`，Playwright 点元素中心，ProseMirror 把光标停在了第三段。

这里的测试不碰真实浏览器，只守两件事：
  1. 定位光标只用 DOM Range，不点击；
  2. 插入图片之后、确认保存之前，正文必须与预期逐字一致。
"""
import pytest

from backend.app import browser_media, browser_toutiao


class FakeBody:
    """最小替身：记录调用序列，按需返回正文。"""

    def __init__(self, text=""):
        self.text = text
        self.calls = []

    def focus(self, **kwargs):
        self.calls.append("focus")

    def evaluate(self, expression, **kwargs):
        if "selectNodeContents" in expression:
            self.calls.append("caret")
            assert "collapse(false)" in expression, "选区要折叠到末尾，不能选中内容"
            return None
        if "cloneNode(true)" in expression:
            self.calls.append("read_body")
            return self.text
        raise AssertionError(f"未预期的表达式：{expression[:80]}")

    def press(self, key, **kwargs):
        self.calls.append(f"press:{key}")

    def inner_text(self, **kwargs):
        self.calls.append("inner_text")
        return self.text


def test_caret_is_placed_with_a_range_not_a_click():
    body = FakeBody()
    browser_media._place_caret_at_end(body)
    assert body.calls == ["focus", "caret"]
    assert "click" not in "".join(body.calls), "点击会把光标停在元素中心，正是覆盖段落的成因"


def test_verify_skips_when_there_are_no_images():
    """没有配图就没有插入动作，不该因为「对不上」误报。"""
    body = FakeBody(text="完全不同的内容")
    browser_toutiao._verify_body_after_images(
        body, {"platform": "toutiao"}, "先确认任务范围。\n\n再核实正文与来源。")
    assert body.calls == [], "无图路径不得触发任何读取"


def test_verify_raises_when_the_body_lost_a_paragraph():
    work = {"platform": "toutiao", "_image_assets": [{"id": "a"}]}
    body = FakeBody(text="再核实正文与来源。")
    with pytest.raises(browser_toutiao.ToutiaoDraftUncertain, match="覆盖"):
        browser_toutiao._verify_body_after_images(
            body, work, "先确认任务范围。\n\n再核实正文与来源。")


def test_verify_passes_when_the_body_is_intact():
    work = {"platform": "toutiao", "_image_assets": [{"id": "a"}]}
    body = FakeBody(text="先确认任务范围。\n\n再核实正文与来源。")
    browser_toutiao._verify_body_after_images(
        body, work, "先确认任务范围。\n\n再核实正文与来源。")
    assert "read_body" in body.calls
