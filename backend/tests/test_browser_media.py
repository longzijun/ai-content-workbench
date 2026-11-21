"""Observed article DOM contracts in isolated headless Edge, with no platform I/O.

The browser has a fresh temporary profile and no CDP endpoint. Fixtures use only
inline HTML/data images; every network route is blocked. These selector checks
do not claim that an unobserved WeChat cover control has been verified.
"""
import base64
import html
import struct
import zlib

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from backend.app import browser_media as media, browser_toutiao as toutiao


REMOTE_ID = "7412345678901234567"


def image_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b"")


PIXEL = "data:image/png;base64," + base64.b64encode(image_bytes()).decode("ascii")


@pytest.fixture(scope="module")
def isolated_browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True,
            args=["--disable-extensions", "--disable-background-networking", "--no-first-run"])
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(isolated_browser):
    context = isolated_browser.new_context(viewport={"width": 1000, "height": 800})
    requests = []
    context.route("**/*", lambda route: (requests.append(route.request.url), route.abort())[1])
    page = context.new_page()
    try:
        yield page
        assert page.url == "about:blank" and requests == []
    finally:
        context.close()


class OfflineEditor:
    """Run production CSS/JS on real DOM; reload uses the same local fixture."""
    def __init__(self, page, markup, platform="toutiao"):
        self.page, self.markup = page, markup
        self.url = toutiao.ORIGIN + toutiao.PUBLISH_PATH + "?pgc_id=" + REMOTE_ID if platform == "toutiao" else "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=123"
        self.reloads, self.waits = 0, []
        page.set_content(markup, wait_until="load")

    def __getattr__(self, name):
        return getattr(self.page, name)

    def reload(self, **kwargs):
        self.reloads += 1
        self.page.set_content(self.markup, wait_until="load")

    def wait_for_function(self, expression, **kwargs):
        self.waits.append(expression)
        # Static HTML has no server/SPA hydration. A short test deadline keeps
        # deliberate mismatches bounded, without changing any production JS.
        kwargs["timeout"] = min(kwargs.get("timeout", 1000), 1000)
        return self.page.wait_for_function(expression, **kwargs)


def trusted_work(tmp_path, count, body="第一段。\n\n第二段。", platform="toutiao"):
    paths = []
    for index in range(count):
        path = tmp_path / f"fixture-{index}.png"
        path.write_bytes(image_bytes())
        paths.append({"id": f"fixture-{index}", "path": str(path), "role": "cover" if index == 0 else "body"})
    return {"platform": platform, "format": "article", "title": "审核图片与正文", "body": body,
            "image_prompts": ["离线测试示意图"] if count else [], "_image_assets": paths}


def wx_dom(count=1):
    real = "".join(f'<p><img class="rich_pages wxw-img js_insertlocalimg" src="{PIXEL}" width="20" height="20"></p>' for _ in range(count))
    return f'<div class="ProseMirror" contenteditable="true"><p>正文</p>{real}<img class="ProseMirror-separator" src="{PIXEL}"></div><div><img class="rich_pages wxw-img js_insertlocalimg" src="{PIXEL}"></div>'


def tt_widget():
    return f'<div class="pgc-img" style="display:none"><img src="{PIXEL}"></div><div class="pgc-img-wrapper ProseMirror-widget" contenteditable="false"><div class="img-loading-container"><img src="{PIXEL}" width="20" height="20"></div><div><span>编辑</span><br><span>搜图</span></div></div>'


def tt_dom(count=1, paragraphs="<p>第一段。</p><p>第二段。</p>", cover=True, title="审核图片与正文"):
    return f'<textarea placeholder="{toutiao.TITLE_PLACEHOLDER}">{html.escape(title)}</textarea><div class="ProseMirror" contenteditable="true">{paragraphs}{tt_widget() * count}</div>' + (f'<div class="article-cover-img-wrap"><img src="{PIXEL}" width="20" height="20"></div>' if cover else "")


@pytest.mark.parametrize("count", [1, 2])
def test_wechat_selector_counts_real_body_images_not_separator_or_external_preview(page, count):
    editor = OfflineEditor(page, wx_dom(count), platform="wechat")
    selected = editor.locator(media.article_image_selector("wechat"))
    assert selected.count() == count
    assert editor.locator('.ProseMirror img').count() == count + 1
    media._loaded(editor, media.article_image_selector("wechat"), count, timeout=1000)


def test_multiple_real_wechat_images_cannot_pass_single_expected_image_readback(page):
    editor = OfflineEditor(page, wx_dom(2), platform="wechat")
    with pytest.raises(PlaywrightTimeout):
        media._loaded(editor, media.article_image_selector("wechat"), 1, timeout=1000)


@pytest.mark.parametrize("count", [1, 2])
def test_toutiao_selector_counts_visual_image_once_and_ignores_hidden_backing_copy(page, count):
    editor = OfflineEditor(page, tt_dom(count))
    assert editor.locator('.ProseMirror img').count() == count * 2
    assert editor.locator(media.article_image_selector("toutiao")).count() == count
    media._loaded(editor, media.article_image_selector("toutiao"), count, timeout=1000)


@pytest.mark.parametrize("actual,expected,cover,status", [(1, 1, True, "verified"), (2, 2, True, "verified"), (2, 1, True, "unverified"), (1, 2, True, "unverified"), (1, 1, False, "unverified")])
def test_toutiao_media_requires_exact_real_image_count_and_cover(page, tmp_path, actual, expected, cover, status):
    editor = OfflineEditor(page, tt_dom(actual, cover=cover))
    result = media.verify_media(editor, trusted_work(tmp_path, expected))
    assert result["status"] == status and result["expected_count"] == expected
    if status == "verified":
        assert result["verified_count"] == expected and result["cover_verified"] is True


def test_real_image_nodes_without_decoded_pixels_cannot_verify_media(page, tmp_path):
    markup = tt_dom().replace(PIXEL, "data:image/png;base64,bm90LWEtcG5n")
    editor = OfflineEditor(page, markup)
    result = media.verify_media(editor, trusted_work(tmp_path, 1))
    assert result["status"] == "unverified" and result["verified_count"] == 0


def test_toutiao_body_clone_removes_only_widget_controls_and_keeps_paragraph_boundaries(page, tmp_path):
    editor = OfflineEditor(page, tt_dom())
    field = editor.locator(toutiao.BODY_SELECTOR)
    original = field.inner_html()
    assert "编辑" in field.inner_text() and "搜图" in field.inner_text()
    actual = toutiao._body_text(field, trusted_work(tmp_path, 1))
    assert toutiao._normalise(actual) == "第一段。\n第二段。"
    assert field.inner_html() == original


@pytest.mark.parametrize("paragraphs", ["<p>编辑</p><p>搜图</p>", "<p>编辑\n搜图</p>", "<p>编辑<br>搜图</p>"])
def test_user_written_edit_search_words_are_preserved_even_with_real_widget(page, tmp_path, paragraphs):
    editor = OfflineEditor(page, tt_dom(paragraphs=paragraphs))
    field = editor.locator(toutiao.BODY_SELECTOR)
    original = field.inner_html()
    actual = toutiao._body_text(field, trusted_work(tmp_path, 1, body="编辑\n搜图"))
    assert toutiao._normalise(actual) == "编辑\n搜图"
    assert field.inner_html() == original


def test_toutiao_production_readback_js_and_final_clone_agree_on_complete_multiline_body(page, tmp_path):
    editor = OfflineEditor(page, tt_dom())
    work = trusted_work(tmp_path, 1)
    assert toutiao._read_editor(editor, REMOTE_ID, work) is True
    assert editor.reloads == 1 and len(editor.waits) == 1
    assert page.locator('.pgc-img-wrapper').count() == 1


def test_toutiao_readback_rejects_extra_real_body_text_without_removing_same_words(page, tmp_path):
    editor = OfflineEditor(page, tt_dom(paragraphs="<p>第一段。</p><p>第二段。</p><p>编辑</p><p>搜图</p>"))
    assert toutiao._read_editor(editor, REMOTE_ID, trusted_work(tmp_path, 1)) is False
    assert page.locator('.ProseMirror p').count() == 4
