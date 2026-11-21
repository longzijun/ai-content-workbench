"""Isolated WeChat UI simulations: no real browser, credentials or platform I/O."""
from copy import deepcopy
import html
import re
from urllib.parse import parse_qs, urlparse

import pytest

from backend.app import browser_wechat as adapter


def _markup_text(markup):
    """模拟 ProseMirror 把粘贴的 HTML 渲染成可见文本。"""
    text = re.sub(r"<br\s*/?>", "\n", markup)
    text = re.sub(r"</(?:p|li|h[1-6]|blockquote|pre)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


WORK = {"platform": "wechat", "format": "article", "title": "公众号草稿审核流程", "body": "先确认选题。\n\n再核实正文与来源。", "tags": [], "image_prompts": []}
REMOTE_ID = "789012345"
HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=not-to-persist"
NEW_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&isNew=1&token=not-to-persist"
SAVED_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&appmsgid=" + REMOTE_ID + "&token=not-to-persist"
DRAFTS_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_list&action=list&token=not-to-persist"


class Locator:
    def __init__(self, page, kind, pattern=None):
        self.page, self.kind, self.pattern = page, kind, pattern

    def filter(self, **kwargs):
        if "has_text" in kwargs:
            assert self.kind == "draft_titles"
            return Locator(self.page, self.kind, kwargs["has_text"])
        if "has" in kwargs:
            assert self.kind == "edit" and kwargs["has"].kind == "svg"
            return self
        assert kwargs == {"visible": True}
        return self

    @property
    def first(self):
        return self.nth(0) if self.kind == "fields" else self

    @property
    def last(self):
        return self.nth(self.count() - 1) if self.kind == "fields" else self

    def nth(self, index):
        assert self.kind == "fields"
        return Locator(self.page, "title" if index == 0 else "body")

    def count(self):
        context = self.page.context
        if self.kind == "fields":
            return context.field_count
        if self.kind == "article":
            return context.entry_count if urlparse(self.page.url).path == adapter.HOME_PATH else 0
        if self.kind == "save":
            return context.save_count
        if self.kind == "body":
            return int(context.field_count >= 2)
        if self.kind == "home_link":
            return context.home_link_count
        if self.kind == "draft_menu":
            return context.menu_count if not context.menu_collapsed else 0
        if self.kind == "content_menu":
            return int(context.menu_collapsed)
        if self.kind in ("draft_titles", "card"):
            return sum(bool(self.pattern.search(title)) for title in context.draft_titles) if self.pattern else len(context.draft_titles)
        if self.kind == "edit":
            return context.edit_count
        if self.kind == "tooltip":
            return context.tooltip_count if context.edit_hovered else 0
        if self.kind == "svg":
            return 1
        return int(context.field_count >= 1)

    def wait_for(self, **kwargs):
        assert kwargs["state"] == "visible"
        if not self.count():
            raise TimeoutError("simulated field not ready")

    def inner_text(self, **kwargs):
        assert self.kind in ("title", "body")
        if self.kind == "body" and self.page.context.placeholder_widget and not self.page.values["body"]:
            return "从这里开始写正文"
        return self.page.values[self.kind]

    def evaluate(self, expression, **kwargs):
        assert self.kind == "body"
        if "ClipboardEvent" in expression:
            markup = kwargs.get("arg")
            assert isinstance(markup, str) and markup.strip().startswith("<"), "富文本正文必须以 HTML 投递"
            assert "##" not in markup and "**" not in markup, "正文不得残留 Markdown 标记"
            self.page.context.pastes.append(markup)
            self.page.values["body"] = _markup_text(markup)
            # True = 编辑器接受了这次粘贴（对应 ProseMirror preventDefault）
            return True
        assert "cloneNode(true)" in expression
        assert '.editor_content_placeholder[contenteditable="false"].ProseMirror-widget' in expression
        assert "clone.querySelectorAll" in expression and "textContent" in expression
        self.page.context.clone_reads += 1
        return self.page.values["body"]

    def fill(self, value, **kwargs):
        assert self.kind in ("title", "body")
        self.page.context.writes.append((self.kind, value))
        self.page.values[self.kind] = value

    def get_attribute(self, name, **kwargs):
        assert name == "href"
        return self.page.context.home_href if self.kind == "home_link" else self.page.context.draft_href

    def locator(self, selector):
        if self.kind == "draft_titles":
            assert selector == adapter.DRAFT_CARD_SELECTOR
            return Locator(self.page, "card", self.pattern)
        assert self.kind == "card" and selector == "a.weui-desktop-icon20"
        return Locator(self.page, "edit")

    def hover(self, **kwargs):
        assert self.kind in ("card", "edit")
        self.page.context.hovers.append(self.kind)
        if self.kind == "edit":
            self.page.context.edit_hovered = True

    def click(self, **kwargs):
        context = self.page.context
        assert self.kind in ("article", "save", "edit", "content_menu"), "must never click publishing controls"
        context.clicks.append({"article": "文章", "save": "保存为草稿", "edit": "编辑", "content_menu": "内容管理"}[self.kind])
        if self.kind == "content_menu":
            context.menu_collapsed = False
            return
        if self.kind == "edit":
            if context.no_popup:
                return
            editor = context.new_page()
            editor.url = SAVED_URL.replace(REMOTE_ID, context.edit_remote_id)
            self.page.popup.value = editor
            return
        if self.kind == "article":
            if context.no_popup:
                return
            editor = context.new_page()
            editor.url = NEW_URL
            if context.nonblank_popup:
                editor.values["body"] = "其他文章正文，不允许覆盖"
            if context.existing_popup:
                editor.url = SAVED_URL
            self.page.popup.value = editor
            return
        if context.missing_id:
            return
        self.page.url = SAVED_URL
        context.saved[REMOTE_ID] = deepcopy(self.page.values)
        if context.corrupt_readback:
            context.saved[REMOTE_ID]["body"] = "平台保存了不匹配的完整正文"
        if context.save_failure:
            raise TimeoutError("save acknowledged then interrupted")


class Popup:
    def __init__(self, page):
        self.page, self.value = page, None

    def __enter__(self):
        self.page.popup = self
        return self

    def __exit__(self, *args):
        if self.value is None:
            raise TimeoutError("popup unavailable")


class Page:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.values = {"title": "", "body": ""}
        self.popup = None
        self.closed = False

    def locator(self, selector):
        kinds = {adapter.FIELDS_SELECTOR: "fields", adapter.DRAFT_MENU_SELECTOR: "draft_menu", adapter.DRAFT_TITLE_SELECTOR: "draft_titles", adapter.EDIT_PATH_SELECTOR: "svg"}
        assert selector in kinds
        return Locator(self, kinds[selector])

    def get_by_text(self, text, **kwargs):
        assert text in ("文章", "内容管理", "编辑") and kwargs == {"exact": True}
        return Locator(self, {"文章": "article", "内容管理": "content_menu", "编辑": "tooltip"}[text])

    def get_by_role(self, role, **kwargs):
        if role == "link":
            assert kwargs == {"name": "首页", "exact": True}
            return Locator(self, "home_link")
        assert role == "button" and kwargs == {"name": "保存为草稿", "exact": True}
        return Locator(self, "save")

    def expect_popup(self, **kwargs):
        return Popup(self)

    def wait_for_load_state(self, state, **kwargs):
        assert state == "domcontentloaded"

    def wait_for_url(self, predicate, **kwargs):
        assert callable(predicate)
        if not predicate(self.url):
            raise TimeoutError("no appmsgid observed")

    def goto(self, value, **kwargs):
        self.url = value
        self.context.visits.append(value)

    def bring_to_front(self):
        self.context.focused = self

    def reload(self, **kwargs):
        self.context.reloads.append(self.url)
        if self.context.reload_failure:
            raise TimeoutError("simulated reload failure")
        if self.context.redirect_read:
            self.url = "https://example.invalid/cgi-bin/appmsg?appmsgid=" + REMOTE_ID
            return
        remote_id = parse_qs(urlparse(self.url).query).get("appmsgid", [None])[0]
        self.values = deepcopy(self.context.saved.get(remote_id, {"title": "", "body": ""}))
        if self.context.delayed_hydration:
            self.values = {"title": "", "body": ""}

    def wait_for_function(self, expression, **kwargs):
        assert "querySelectorAll" in expression and kwargs["timeout"] == 15000
        self.context.hydration_waits += 1
        if self.context.delayed_hydration:
            remote_id = parse_qs(urlparse(self.url).query).get("appmsgid", [None])[0]
            self.values = deepcopy(self.context.saved.get(remote_id, {"title": "", "body": ""}))
        expected = kwargs["arg"]
        if adapter._normalise(self.values["title"]) != expected["title"] or adapter._normalise(self.values["body"]) != expected["body"]:
            raise TimeoutError("simulated complete-content wait expired")

    def is_closed(self):
        return self.closed


class Context:
    def __init__(self):
        self.pages, self.saved, self.writes, self.clicks, self.reloads = [], {}, [], [], []
        self.field_count, self.entry_count, self.save_count = 2, 1, 1
        self.no_popup = self.nonblank_popup = self.existing_popup = False
        self.missing_id = self.corrupt_readback = self.save_failure = False
        self.reload_failure = self.redirect_read = False
        self.home_href, self.draft_href = HOME_URL, DRAFTS_URL
        self.home_link_count = self.menu_count = 0
        self.menu_collapsed = self.delayed_hydration = False
        self.hydration_waits = 0
        self.draft_titles = []
        self.edit_count = self.tooltip_count = 1
        self.edit_remote_id = REMOTE_ID
        self.edit_hovered = False
        self.hovers, self.visits = [], []
        self.focused = None
        self.placeholder_widget = False
        self.clone_reads = 0
        self.pastes = []

    def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page


@pytest.fixture
def home():
    context = Context()
    page = context.new_page()
    page.url = HOME_URL
    return page


def test_prepare_opens_blank_popup_only_and_preserves_home(home):
    editor = adapter.prepare(home, WORK)
    assert editor is not home and home.url == HOME_URL
    assert editor.url == NEW_URL
    assert home.context.clicks == ["文章"] and home.context.writes == []


def test_prepare_ignores_only_observed_noneditable_widget_in_a_detached_clone(home):
    home.context.placeholder_widget = True
    editor = adapter.prepare(home, WORK)
    assert editor.locator(adapter.FIELDS_SELECTOR).last.inner_text() == "从这里开始写正文"
    assert home.context.clone_reads == 1 and home.context.writes == []
    assert editor.values["body"] == "" and home.context.placeholder_widget is True


def test_user_body_identical_to_placeholder_sentence_is_never_treated_as_blank(home):
    home.context.placeholder_widget = True
    editor = adapter.prepare(home, WORK)
    editor.values["body"] = "从这里开始写正文"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.create(editor, WORK)
    assert home.context.writes == [] and home.context.clicks == ["文章"]


@pytest.mark.parametrize("flag", ["no_popup", "nonblank_popup", "existing_popup"])
def test_prepare_refuses_unconfirmed_or_nonblank_popup_before_writes(home, flag):
    setattr(home.context, flag, True)
    with pytest.raises(RuntimeError, match="尚未授权写入"):
        adapter.prepare(home, WORK)
    assert home.context.writes == []
    assert "保存为草稿" not in home.context.clicks


@pytest.mark.parametrize("count", [0, 1, 3])
def test_prepare_requires_exactly_two_ready_editable_controls(home, count):
    home.context.field_count = count
    with pytest.raises(RuntimeError):
        adapter.prepare(home, WORK)
    assert home.context.writes == []


def test_create_saves_only_private_draft_and_records_bare_id_before_reload(home):
    editor = adapter.prepare(home, WORK)
    identifiers = []

    def record(remote_id):
        assert home.context.reloads == [], "ID must persist before verification can fail"
        identifiers.append(remote_id)

    assert adapter.create(editor, WORK, on_id=record) == REMOTE_ID
    assert identifiers == [REMOTE_ID]
    # 正文改为富文本粘贴投递，不再走 fill 纯文本写入
    assert home.context.writes == [("title", WORK["title"])]
    assert len(home.context.pastes) == 1
    assert home.context.clicks == ["文章", "保存为草稿"]
    assert len(home.context.reloads) == 1
    assert "token" not in str(identifiers)
    assert "发表" not in home.context.clicks


MARKDOWN_WORK = {**WORK, "body": "## 先分清三类\n\n**展示**主要让人观看或互动。\n\n- 展示\n- 试乘\n- 实际服务"}


def test_markdown_body_is_rendered_as_rich_text_not_written_verbatim(home):
    """回归 2026-10-14：正文必须以渲染后的 HTML 投递，
    "## " 与 "**" 不得原样出现在平台草稿里。"""
    editor = adapter.prepare(home, MARKDOWN_WORK)
    identifiers = []
    assert adapter.create(editor, MARKDOWN_WORK, on_id=identifiers.append) == REMOTE_ID
    assert len(home.context.pastes) == 1
    markup = home.context.pastes[0]
    assert "<p>" in markup and "<strong>" in markup and "<ul>" in markup
    assert "##" not in markup and "**" not in markup
    # 微信公众号正文没有标题语义，标题必须降级为加粗段落
    assert "<h1>" not in markup and "<h2>" not in markup


def test_read_existing_editor_is_only_reload_and_compares_all_text(home):
    editor = home.context.new_page()
    editor.url = SAVED_URL
    home.context.saved[REMOTE_ID] = {"title": WORK["title"], "body": " 先确认选题。\n\n\n再核实正文与来源。\u00a0 "}
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.focused is editor
    assert home.context.writes == [] and home.context.clicks == []
    home.context.saved[REMOTE_ID]["body"] += "另加一段未经授权的文字"
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.writes == [] and home.context.clicks == []


def test_read_with_no_existing_editor_never_guesses_url_or_writes(home):
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert len(home.context.pages) == 1 and home.context.writes == [] and home.context.clicks == []


def test_prepare_from_drafts_uses_observed_home_link_without_altering_draft_page(home):
    home.url = DRAFTS_URL
    home.context.home_link_count = 1
    editor = adapter.prepare(home, WORK)
    assert editor.url == NEW_URL and home.url == DRAFTS_URL
    assert home.context.visits == [HOME_URL]
    assert home.context.writes == [] and home.context.clicks == ["文章"]


def test_prepare_from_editor_reuses_existing_home_before_blank_popup(home):
    existing = home.context.new_page()
    existing.url = SAVED_URL
    assert adapter.prepare(existing, WORK).url == NEW_URL
    assert existing.url == SAVED_URL and home.url == HOME_URL
    assert home.context.writes == []


@pytest.mark.parametrize("href", ["https://evil.invalid/cgi-bin/home?token=secret", "javascript:alert(1)", "https://user@mp.weixin.qq.com/cgi-bin/home", "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=1"])
def test_prepare_rejects_unobserved_or_nonhome_link_without_navigation(home, href):
    home.url = DRAFTS_URL
    home.context.home_link_count = 1
    home.context.home_href = href
    with pytest.raises(RuntimeError) as error:
        adapter.prepare(home, WORK)
    assert home.context.visits == [] and home.context.writes == [] and home.context.clicks == []
    assert "secret" not in str(error.value) and "not-to-persist" not in str(error.value)


def setup_recoverable_draft(home):
    home.context.menu_count = 1
    home.context.draft_titles = [WORK["title"]]
    home.context.saved[REMOTE_ID] = {"title": WORK["title"], "body": WORK["body"]}


def test_read_recovers_exact_card_through_unique_observed_svg_and_focuses_editor(home):
    setup_recoverable_draft(home)
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.visits == [DRAFTS_URL] and home.url == HOME_URL
    assert home.context.hovers == ["card", "edit"]
    assert home.context.clicks == ["编辑"] and home.context.writes == []
    assert home.context.focused.url == SAVED_URL
    assert home.context.reloads == [SAVED_URL]


def test_read_expands_actual_content_menu_once_before_recovery(home):
    setup_recoverable_draft(home)
    home.context.menu_collapsed = True
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.clicks == ["内容管理", "编辑"]
    assert home.context.writes == []


@pytest.mark.parametrize("titles", [[], [WORK["title"], WORK["title"]], ["另一篇草稿"]])
def test_read_never_edits_missing_or_ambiguous_title_cards(home, titles):
    setup_recoverable_draft(home)
    home.context.draft_titles = titles
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.clicks == [] and home.context.writes == [] and home.context.reloads == []


@pytest.mark.parametrize("edit_count,tooltip_count", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_read_requires_unique_observed_icon_and_edit_tooltip(home, edit_count, tooltip_count):
    setup_recoverable_draft(home)
    home.context.edit_count, home.context.tooltip_count = edit_count, tooltip_count
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.clicks == [] and home.context.writes == []


def test_read_recovered_popup_with_different_actual_id_is_never_reloaded_or_written(home):
    setup_recoverable_draft(home)
    home.context.edit_remote_id = "999999999"
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.clicks == ["编辑"] and home.context.writes == []
    assert home.context.reloads == [] and home.context.focused is None


def test_read_missing_popup_does_not_repeat_edit_click(home):
    setup_recoverable_draft(home)
    home.context.no_popup = True
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.clicks == ["编辑"] and home.context.writes == []


def test_read_waits_for_complete_spa_hydration_without_writing(home):
    setup_recoverable_draft(home)
    home.context.delayed_hydration = True
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.hydration_waits == 1
    assert home.context.writes == [] and home.context.clicks == ["编辑"]


@pytest.mark.parametrize("flag", ["corrupt_readback", "reload_failure", "save_failure", "redirect_read"])
def test_uncertain_save_retains_real_id_and_never_repeats_write(home, flag):
    setattr(home.context, flag, True)
    editor = adapter.prepare(home, WORK)
    identifiers = []
    with pytest.raises(adapter.WechatDraftUncertain) as error:
        adapter.create(editor, WORK, on_id=identifiers.append)
    assert identifiers == [REMOTE_ID]
    assert home.context.clicks == ["文章", "保存为草稿"]
    # 标题 fill 一次、正文粘贴一次；保存结果不确定时也不重复写入
    assert len(home.context.writes) == 1 and len(home.context.pastes) == 1
    assert "not-to-persist" not in str(error.value)


def test_no_url_identifier_stays_uncertain_and_no_callback_is_fabricated(home):
    home.context.missing_id = True
    editor = adapter.prepare(home, WORK)
    identifiers = []
    with pytest.raises(adapter.WechatDraftUncertain):
        adapter.create(editor, WORK, on_id=identifiers.append)
    assert identifiers == []
    assert home.context.clicks == ["文章", "保存为草稿"]


def test_create_rechecks_blank_editor_and_never_overwrites_existing_text(home):
    editor = adapter.prepare(home, WORK)
    editor.values["body"] = "用户在等待账本时写入的内容"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.create(editor, WORK)
    assert home.context.writes == [] and home.context.clicks == ["文章"]


@pytest.mark.parametrize("query", ["appmsgid=", "appmsgid=0", "appmsgid=1&appmsgid=2", "appmsgid=invalid"])
def test_create_rejects_any_existing_identifier_marker_even_with_blank_fields(home, query):
    editor = adapter.prepare(home, WORK)
    editor.url = "https://mp.weixin.qq.com/cgi-bin/appmsg?" + query
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.create(editor, WORK)
    assert home.context.writes == [] and home.context.clicks == ["文章"]


@pytest.mark.parametrize("count", [0, 2])
def test_create_requires_unique_save_button_before_typing(home, count):
    editor = adapter.prepare(home, WORK)
    home.context.save_count = count
    with pytest.raises((RuntimeError, TimeoutError)):
        adapter.create(editor, WORK)
    assert home.context.writes == []


def test_64_character_web_title_is_accepted_and_65_is_rejected(home):
    adapter.prepare(home, {**WORK, "title": "字" * 64})
    with pytest.raises(ValueError):
        adapter.prepare(home, {**WORK, "title": "字" * 65})
    assert home.context.writes == []


@pytest.mark.parametrize("changes", [{"platform": "toutiao"}, {"title": " "}, {"body": " "}, {"format": "video"}])
def test_invalid_work_fails_before_any_platform_action(home, changes):
    with pytest.raises(ValueError):
        adapter.prepare(home, {**WORK, **changes})
    assert len(home.context.pages) == 1 and home.context.clicks == [] and home.context.writes == []


@pytest.mark.parametrize("url", [
    "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=1&appmsgid=2",
    "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=1&appmsgid=",
    "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=0",
    "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=made-up-id",
    "https://mp.weixin.qq.com/cgi-bin/private-api?appmsgid=1",
    "https://mp.weixin.qq.com.evil.invalid/cgi-bin/appmsg?appmsgid=1",
    "https://user@mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=1",
    "http://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=1",
])
def test_identifier_is_only_bare_real_id_from_observed_official_editor(url):
    assert adapter._id_from_url(url) is None


@pytest.mark.parametrize("remote_id", ["", "0", "appmsgid=1", "https://mp.weixin.qq.com/?appmsgid=1", "invented"])
def test_invalid_read_id_never_opens_or_writes(home, remote_id):
    assert adapter.read(home, remote_id, WORK) is False
    assert len(home.context.pages) == 1 and home.context.reloads == [] and home.context.writes == []
