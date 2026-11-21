"""Toutiao UI draft-only tests with a fully simulated visible editor and list."""
from copy import deepcopy
import html
import re
from urllib.parse import parse_qs, urlparse

import pytest

from backend.app import browser_toutiao as adapter


def _markup_text(markup):
    """模拟 ProseMirror 把粘贴的 HTML 渲染成可见文本。"""
    text = re.sub(r"<br\s*/?>", "\n", markup)
    text = re.sub(r"</(?:p|li|h[1-6]|blockquote|pre)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


WORK = {"platform": "toutiao", "format": "article", "title": "自动化工作台的审核步骤", "body": "先确认任务范围。\n\n再核实正文与来源。", "tags": [], "image_prompts": []}
REMOTE_ID = "7412345678901234567"


class Locator:
    def __init__(self, page, kind, level=0):
        self.page, self.kind, self.level = page, kind, level

    def filter(self, **kwargs):
        assert kwargs == {"visible": True}
        return self

    @property
    def first(self):
        return self

    def wait_for(self, **kwargs):
        if self.count() == 0:
            raise TimeoutError("simulated locator not visible")

    def count(self):
        if self.kind == "saved":
            return int(self.page.context.autosave)
        if self.kind == "matching_title":
            return 2 if self.page.context.duplicate_titles else 1
        if self.kind == "edit":
            return 1 if self.level >= 2 else 0
        return 1

    def fill(self, value, **kwargs):
        assert self.kind in ("title", "body")
        self.page.context.writes.append((self.kind, value))
        self.page.values[self.kind] = value
        if self.kind == "body":
            self.page.url = adapter.ORIGIN + adapter.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID
            self.page.context.saved[REMOTE_ID] = deepcopy(self.page.values)
            if self.page.context.corrupt_readback:
                self.page.context.saved[REMOTE_ID]["body"] = "不匹配的平台正文"

    def input_value(self, **kwargs):
        return self.page.values["title"]

    def inner_text(self, **kwargs):
        return self.page.values["body"]

    def evaluate(self, expression, **kwargs):
        assert self.kind == "body"
        if "ClipboardEvent" in expression:
            markup = kwargs.get("arg")
            assert isinstance(markup, str) and markup.strip().startswith("<"), "富文本正文必须以 HTML 投递"
            assert "##" not in markup and "**" not in markup, "正文不得残留 Markdown 标记"
            self.page.context.pastes.append(markup)
            self.page.values["body"] = _markup_text(markup)
            # 与 fill 保持相同的可观察副作用：写入正文即视为已自动保存
            self.page.url = adapter.ORIGIN + adapter.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID
            self.page.context.saved[REMOTE_ID] = deepcopy(self.page.values)
            if self.page.context.corrupt_readback:
                self.page.context.saved[REMOTE_ID]["body"] = "不匹配的平台正文"
            return True
        assert "cloneNode(true)" in expression and "clone.querySelectorAll" in expression
        return self.page.values["body"]

    def locator(self, selector):
        assert selector == "xpath=.."
        return Locator(self.page, "ancestor", self.level + 1)

    def get_by_text(self, text, **kwargs):
        assert text == "编辑" and kwargs == {"exact": True}
        return Locator(self.page, "edit", self.level)

    def get_attribute(self, name):
        assert name == "href"
        return adapter.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID + "&token=not-to-return"

    def click(self, **kwargs):
        assert self.kind == "edit"
        self.page.context.clicks.append("编辑")
        if self.page.context.same_tab:
            self.page.goto(adapter.ORIGIN + adapter.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID)
            return
        opened = self.page.context.new_page()
        opened.goto(adapter.ORIGIN + adapter.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID)
        self.page.popup.value = opened


class Popup:
    def __init__(self, page):
        self.page, self.value = page, None

    def __enter__(self):
        self.page.popup = self
        return self

    def __exit__(self, *args):
        if self.value is None:
            raise TimeoutError("same-tab editor, no popup")


class Page:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.values = {"title": "", "body": ""}
        self.popup = None

    def goto(self, url, **kwargs):
        self.url = url
        self.context.visits.append(url)
        pgc_id = parse_qs(urlparse(url).query).get("pgc_id", [None])[0]
        if pgc_id in self.context.saved:
            self.values = deepcopy(self.context.saved[pgc_id])
        elif url == adapter.ORIGIN + adapter.PUBLISH_PATH:
            self.values = deepcopy(self.context.restored_input)
            if self.context.new_editor_query is not None:
                self.url += "?" + self.context.new_editor_query

    def reload(self, **kwargs):
        self.context.reloads.append(self.url)
        self.goto(self.url)
        if self.context.delayed_hydration:
            self.values = {"title": "", "body": ""}

    def wait_for_function(self, expression, **kwargs):
        assert "querySelectorAll" in expression and kwargs["timeout"] == 15000
        expected = kwargs["arg"]
        self.context.hydration_waits += 1
        if self.context.delayed_hydration:
            remote_id = parse_qs(urlparse(self.url).query).get("pgc_id", [None])[0]
            self.values = deepcopy(self.context.saved.get(remote_id, {"title": "", "body": ""}))
        if self.values["title"].strip() != expected["title"] or adapter._normalise(self.values["body"]) != expected["body"]:
            raise TimeoutError("simulated full-content wait expired")

    def get_by_placeholder(self, placeholder, **kwargs):
        assert placeholder == adapter.TITLE_PLACEHOLDER and kwargs == {"exact": True}
        return Locator(self, "title")

    def locator(self, selector):
        assert selector == adapter.BODY_SELECTOR
        return Locator(self, "body")

    def get_by_text(self, value, **kwargs):
        if isinstance(value, str):
            assert value == WORK["title"] and kwargs == {"exact": True}
            return Locator(self, "matching_title")
        assert value.pattern == adapter._SAVED.pattern
        return Locator(self, "saved")

    def bring_to_front(self):
        self.context.focused = self

    def expect_popup(self, **kwargs):
        return Popup(self)

    def is_closed(self):
        return False

    def wait_for_load_state(self, state, **kwargs):
        assert state == "domcontentloaded"


class Context:
    def __init__(self):
        self.pages, self.saved, self.writes, self.clicks, self.visits, self.reloads = [], {}, [], [], [], []
        self.pastes = []
        self.autosave = True
        self.duplicate_titles = False
        self.corrupt_readback = False
        self.same_tab = False
        self.delayed_hydration = False
        self.hydration_waits = 0
        self.restored_input = {"title": "", "body": ""}
        self.new_editor_query = None

    def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page


@pytest.fixture
def home():
    context = Context()
    page = context.new_page()
    page.url = adapter.ORIGIN + "/profile_v4/index"
    return page


def test_prepare_preserves_account_home_and_does_not_write(home):
    editor = adapter.prepare(home, WORK)
    assert editor is not home
    assert home.url == adapter.ORIGIN + "/profile_v4/index"
    assert editor.url == adapter.ORIGIN + adapter.PUBLISH_PATH
    assert home.context.writes == [] and home.context.clicks == []


@pytest.mark.parametrize("same_tab", [False, True])
def test_create_uses_only_fields_and_exact_edit_control_then_reload_verifies(home, same_tab):
    home.context.same_tab = same_tab
    editor = adapter.prepare(home, WORK)
    identifiers = []
    result = adapter.create(editor, WORK, on_id=identifiers.append)
    assert result == REMOTE_ID
    assert identifiers == [REMOTE_ID]
    # 正文改为富文本粘贴投递，不再走 fill 纯文本写入
    assert home.context.writes == [("title", WORK["title"])]
    assert len(home.context.pastes) == 1
    assert home.context.clicks == ["编辑"]
    assert len(home.context.reloads) == 1
    assert all(urlparse(url).path in {adapter.PUBLISH_PATH, adapter.DRAFTS_PATH} for url in home.context.visits)
    assert "not-to-return" not in str(identifiers)


MARKDOWN_WORK = {**WORK, "body": "## 小标题层级\n\n**加粗**说明。\n\n- 一\n- 二"}


def test_markdown_body_keeps_toutiao_headings_and_drops_markers(home):
    """回归 2026-10-14：头条文章支持小标题，正文以 HTML 投递且不残留 Markdown 标记。"""
    editor = adapter.prepare(home, MARKDOWN_WORK)
    assert adapter.create(editor, MARKDOWN_WORK, on_id=lambda _: None) == REMOTE_ID
    assert len(home.context.pastes) == 1
    markup = home.context.pastes[0]
    assert "<h2>" in markup and "<strong>" in markup and "<ul>" in markup
    assert "##" not in markup and "**" not in markup


def test_read_known_id_is_read_only_and_compares_real_reloaded_fields(home):
    home.context.saved[REMOTE_ID] = {"title": WORK["title"], "body": WORK["body"]}
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.focused is home.context.pages[-1]
    # 2026-10-18：官方发布页不认外部 ?pgc_id= 参数，读取改为经草稿箱"编辑"控件
    # 进入（与 create() 校验同路）。只读语义 = 零写入 + 仅这一次入口点击。
    assert home.context.writes == [] and home.context.clicks == ["编辑"]
    home.context.saved[REMOTE_ID]["body"] = "另一篇正文"
    assert adapter.read(home, REMOTE_ID, WORK) is False
    assert home.context.writes == []


def test_read_waits_for_complete_spa_hydration_without_writing(home):
    home.context.delayed_hydration = True
    home.context.saved[REMOTE_ID] = {"title": WORK["title"], "body": WORK["body"]}
    assert adapter.read(home, REMOTE_ID, WORK) is True
    assert home.context.hydration_waits == 1
    assert home.context.writes == [] and home.context.clicks == ["编辑"]


@pytest.mark.parametrize("field", ["title", "body"])
def test_prepare_refuses_locally_restored_input_before_any_write(home, field):
    home.context.restored_input[field] = "平台恢复的原有内容"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.prepare(home, WORK)
    assert home.context.writes == [] and home.context.clicks == []


@pytest.mark.parametrize("query", ["pgc_id=", "pgc_id=0", "pgc_id=1&pgc_id=2", "pgc_id=1&pgc_id=", "pgc_id=invalid", "pgc_id=" + REMOTE_ID])
def test_prepare_refuses_any_existing_id_marker_even_with_empty_fields(home, query):
    home.context.new_editor_query = query
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.prepare(home, WORK)
    assert home.context.writes == [] and home.context.clicks == []


@pytest.mark.parametrize("field", ["title", "body"])
def test_create_rechecks_empty_controls_if_user_types_after_prepare(home, field):
    editor = adapter.prepare(home, WORK)
    editor.values[field] = "用户在准备后写入的正文"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.create(editor, WORK)
    assert home.context.writes == [] and home.context.clicks == []


@pytest.mark.parametrize("query", ["pgc_id=", "pgc_id=1&pgc_id=2", "pgc_id=invalid"])
def test_create_rechecks_existing_identifier_before_typing(home, query):
    editor = adapter.prepare(home, WORK)
    editor.url += "?" + query
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.create(editor, WORK)
    assert home.context.writes == [] and home.context.clicks == []


@pytest.mark.parametrize("updates", [{"title": "短"}, {"title": "字" * 31}, {"body": " "}, {"format": "micro"}, {"platform": "wechat"}])
def test_work_constraints_fail_before_opening_or_typing(home, updates):
    with pytest.raises(ValueError):
        adapter.prepare(home, {**WORK, **updates})
    assert len(home.context.pages) == 1 and home.context.writes == []


def test_autosave_uncertainty_retains_observed_id_and_never_clicks_publish(home):
    home.context.autosave = False
    editor = adapter.prepare(home, WORK)
    identifiers = []
    with pytest.raises(adapter.ToutiaoDraftUncertain, match="自动保存"):
        adapter.create(editor, WORK, on_id=identifiers.append)
    assert identifiers == [REMOTE_ID]
    assert home.context.clicks == []
    assert len(home.context.pages) == 2


def test_ambiguous_same_title_drafts_are_never_clicked(home):
    home.context.duplicate_titles = True
    editor = adapter.prepare(home, WORK)
    identifiers = []
    with pytest.raises(adapter.ToutiaoDraftUncertain, match="唯一"):
        adapter.create(editor, WORK, on_id=identifiers.append)
    assert identifiers == [REMOTE_ID]
    assert home.context.clicks == []


def test_readback_mismatch_remains_uncertain_with_id_already_recorded(home):
    home.context.corrupt_readback = True
    editor = adapter.prepare(home, WORK)
    identifiers = []
    with pytest.raises(adapter.ToutiaoDraftUncertain, match="正文不一致"):
        adapter.create(editor, WORK, on_id=identifiers.append)
    assert identifiers == [REMOTE_ID]
    assert home.context.clicks == ["编辑"]


@pytest.mark.parametrize("value", ["https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id=123&pgc_id=456", "https://evil.example/profile_v4/graphic/publish?pgc_id=123", "https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id=0", "https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id=made-up-id", "https://mp.toutiao.com/private/api?pgc_id=123"])
def test_only_actual_public_editor_pgc_id_is_accepted(value):
    assert adapter._id_from_url(value) is None


@pytest.mark.parametrize("remote_id", ["", "0", "https://mp.toutiao.com/?pgc_id=123", "pgc_id=123", "fake-id"])
def test_read_invalid_identifier_never_opens_or_writes(home, remote_id):
    assert adapter.read(home, remote_id, WORK) is False
    assert len(home.context.pages) == 1
    assert home.context.visits == [] and home.context.writes == []
