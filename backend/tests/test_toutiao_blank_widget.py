"""Observed Toutiao placeholder widget is display text, not article content."""
import pytest

from backend.app import browser_toutiao as adapter


WORK = {"platform": "toutiao", "format": "article", "title": "审核正文内容", "body": "需要用户审核的真实正文。"}
SELECTOR = '.syl-placeholder[contenteditable="false"].ProseMirror-widget'


class Field:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    @property
    def first(self):
        return self

    def filter(self, **kwargs):
        assert kwargs == {"visible": True}
        return self

    def count(self):
        return 1

    def wait_for(self, **kwargs):
        pass

    def input_value(self, **kwargs):
        assert self.kind == "title"
        return self.page.title

    def inner_text(self, **kwargs):
        return self.page.body + ("请输入正文" if self.page.widget else "")

    def evaluate(self, expression, **kwargs):
        assert self.kind == "body"
        assert "cloneNode(true)" in expression and SELECTOR in expression
        assert "clone.querySelectorAll" in expression and "clone.textContent" in expression
        self.page.clone_reads += 1
        return self.page.body

    def fill(self, *args, **kwargs):
        pytest.fail("Blank validation must never fill an editor")


class Page:
    def __init__(self, context):
        self.context = context
        self.url = adapter.ORIGIN + adapter.PUBLISH_PATH
        self.title, self.body = "", ""
        self.widget = True
        self.clone_reads = self.waits = 0

    def goto(self, url, **kwargs):
        self.url = url

    def get_by_placeholder(self, placeholder, **kwargs):
        assert placeholder == adapter.TITLE_PLACEHOLDER
        return Field(self, "title")

    def locator(self, selector):
        assert selector == adapter.BODY_SELECTOR
        return Field(self, "body")

    def wait_for_function(self, expression, **kwargs):
        assert SELECTOR in expression and "cloneNode(true)" in expression
        assert kwargs["timeout"] == 15000
        self.waits += 1
        if self.context.user_writes_during_hydration:
            self.body = "平台加载或用户编辑后出现的真实文字"


class Context:
    def __init__(self):
        self.pages = []
        self.user_writes_during_hydration = False

    def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page


@pytest.fixture
def editor():
    return Context().new_page()


def test_observed_noneditable_placeholder_is_read_from_clone_without_removing_real_widget(editor):
    body = editor.locator(adapter.BODY_SELECTOR)
    assert body.inner_text() == "请输入正文"
    adapter._blank(editor)
    assert editor.widget is True and body.inner_text() == "请输入正文"
    assert editor.clone_reads == 1


@pytest.mark.parametrize("with_widget", [False, True])
def test_identical_user_written_sentence_is_real_content_and_never_blank(editor, with_widget):
    editor.widget = with_widget
    editor.body = "请输入正文"
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter._blank(editor)


@pytest.mark.parametrize("query", ["pgc_id=", "pgc_id=1", "pgc_id=0", "pgc_id=1&pgc_id=2", "pgc_id=invalid"])
def test_placeholder_cannot_make_existing_id_marker_safe_to_overwrite(editor, query):
    editor.url += "?" + query
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter._blank(editor)


def test_prepare_waits_for_consistent_fields_and_rechecks_blank_without_editing_source(editor):
    source_url = editor.url
    prepared = adapter.prepare(editor, WORK)
    assert prepared is not editor and editor.url == source_url
    assert prepared.waits == 1 and prepared.clone_reads == 2 and prepared.widget


def test_prepare_rechecks_content_that_appears_during_field_hydration(editor):
    editor.context.user_writes_during_hydration = True
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        adapter.prepare(editor, WORK)
