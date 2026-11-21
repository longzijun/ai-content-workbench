"""Markdown → 平台正文 渲染层测试。

重点覆盖 2026-10-14 发现的问题：三平台草稿里 "## " 和 "**" 原样暴露。
"""
import pytest

from backend.app import richtext

SAMPLE = """## 先分清：展示、试乘，还是日常服务

同样写着"科技体验"，实际内容可能差别很大。

- **展示**主要让人观看或互动。
- **试乘或试驾**通常对应一次限定体验。
- **实际服务**则要看是否提供可使用的功能。

> 分清这三类，才知道自己买的是参观、体验，还是一段交通服务。

### 无人驾驶这个词，还需要进一步拆开

1. 先核对场景
2. 再核对资格
3. 最后确认流程
"""


@pytest.mark.parametrize("platform", ["wechat", "toutiao"])
def test_rendered_html_never_leaks_markdown_markers(platform):
    """核心回归：渲染结果里不能出现 "## " 或 "**"。"""
    rendered = richtext.to_html(SAMPLE, platform)
    assert "##" not in rendered
    assert "**" not in rendered
    assert "###" not in rendered


def test_xiaohongshu_text_never_leaks_markdown_markers():
    rendered = richtext.to_text(SAMPLE, "xiaohongshu")
    assert "#" not in rendered
    assert "**" not in rendered
    assert "*" not in rendered


def test_wechat_headings_become_bold_paragraphs():
    """微信公众号正文没有标题语义，标题降级为加粗段落。"""
    html = richtext.to_html("## 小标题", "wechat")
    assert html == "<p><strong>小标题</strong></p>"
    assert "<h2>" not in html


def test_toutiao_headings_keep_levels():
    assert richtext.to_html("# 一级", "toutiao") == "<h2>一级</h2>"
    assert richtext.to_html("## 二级", "toutiao") == "<h2>二级</h2>"
    assert richtext.to_html("#### 四级", "toutiao") == "<h3>四级</h3>"


def test_bold_code_and_italic_are_rendered():
    html = richtext.to_html("**加粗**与`代码`与*斜体*", "wechat")
    assert "<strong>加粗</strong>" in html
    assert "<code>代码</code>" in html
    assert "<em>斜体</em>" in html


def test_lists_and_quotes_render_as_semantic_tags():
    html = richtext.to_html("- 一\n- 二\n\n> 引用一句", "wechat")
    assert "<ul><li>一</li><li>二</li></ul>" in html
    assert "<blockquote><p>引用一句</p></blockquote>" in html


def test_ordered_list_renders_as_ol():
    html = richtext.to_html("1. 第一\n2. 第二", "toutiao")
    assert "<ol><li>第一</li><li>第二</li></ol>" in html


def test_fenced_code_block_is_escaped():
    html = richtext.to_html("```\n<script>alert(1)</script>\n```", "wechat")
    assert "<pre><code>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_inline_html_is_escaped_to_prevent_injection():
    html = richtext.to_html("正文里有 <img src=x onerror=alert(1)> 标签", "wechat")
    assert "<img" not in html
    assert "&lt;img" in html


def test_paragraphs_are_separated():
    html = richtext.to_html("第一段\n\n第二段", "wechat")
    assert html == "<p>第一段</p>\n<p>第二段</p>"


def test_soft_line_break_inside_paragraph_uses_br():
    html = richtext.to_html("第一行\n第二行", "wechat")
    assert html == "<p>第一行<br>第二行</p>"


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("## 标题", "标题"),
        ("**加粗**文字", "加粗文字"),
        ("- 项目", "项目"),
        ("1. 第一", "第一"),
        ("> 引用", "引用"),
        ("`代码`", "代码"),
    ],
)
def test_strip_markup_matches_what_editor_shows(markdown, expected):
    """strip_markup 必须与编辑器渲染后的可见文本一致（用于回读校验）。"""
    assert richtext.strip_markup(markdown) == expected


def test_strip_markup_drops_list_markers_like_editor_does():
    """<ul>/<ol> 的 innerText 不含项目符号和序号，strip_markup 也要去掉。"""
    text = richtext.strip_markup("- 一\n- 二\n1. 甲\n2. 乙")
    assert text == "一\n二\n甲\n乙"


def test_xiaohongshu_text_is_readable_and_not_markdown():
    rendered = richtext.to_text(SAMPLE, "xiaohongshu")
    assert "先分清：展示、试乘，还是日常服务" in rendered
    assert "· ***展示" not in rendered
    assert rendered.startswith("先分清")


def test_empty_and_whitespace_input_are_safe():
    for value in ("", "   ", "\n\n"):
        assert richtext.to_html(value, "wechat") == ""
        assert richtext.to_text(value, "xiaohongshu") == ""
        assert richtext.strip_markup(value) == ""


def test_unsupported_platform_is_rejected():
    with pytest.raises(ValueError):
        richtext.to_html("正文", "douyin")
    with pytest.raises(ValueError):
        richtext.to_html("正文", "xiaohongshu")


def test_render_dispatches_by_platform_capability():
    assert richtext.render("## 标题", "wechat")[0] == "html"
    assert richtext.render("## 标题", "toutiao")[0] == "html"
    assert richtext.render("## 标题", "xiaohongshu")[0] == "text"


def test_expected_visible_text_matches_editor_kind():
    """微信/头条用去标记文本比对，小红书用实际填入的纯文本比对。"""
    assert richtext.expected_visible_text("## 标题", "wechat") == "标题"
    assert richtext.expected_visible_text("## 标题", "toutiao") == "标题"
    assert richtext.expected_visible_text("## 标题", "xiaohongshu") == richtext.to_text("## 标题", "xiaohongshu")
