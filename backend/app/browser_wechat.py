"""Private WeChat article drafts through the observed official creator UI.

The common adapter checks the account and owns its durable ledger. ``prepare``
only opens and validates a blank popup. The ledger must claim before ``create``
fills fields. No publishing controls or private API endpoints are used here;
only a bare appmsgid crosses this module's return/callback boundary.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from . import browser_editor, richtext


HOST = "mp.weixin.qq.com"
HOME_PATH = "/cgi-bin/home"
EDITOR_PATH = "/cgi-bin/appmsg"
FIELDS_SELECTOR = '.ProseMirror[contenteditable="true"]'
DRAFT_MENU_SELECTOR = "a#menu_10125"
DRAFT_TITLE_SELECTOR = "a.weui-desktop-publish__cover__title"
DRAFT_CARD_SELECTOR = "xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' weui-desktop-card__bd ')][1]"
EDIT_PATH_SELECTOR = 'svg path[d^="M13 4v1.5H4.5v14h14V11H20"]'
_REAL_ID = re.compile(r"[1-9][0-9]{0,31}\Z")


class WechatDraftUncertain(RuntimeError):
    """A draft write may have reached WeChat; read it before another attempt."""


def _validate_work(work):
    if not isinstance(work, dict) or work.get("platform") != "wechat":
        raise ValueError("公众号适配器需要对应平台的文章")
    title, body = work.get("title"), work.get("body")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 64:
        raise ValueError("公众号网页文章标题需为 1 到 64 字，请先修改版本")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("公众号文章正文不能为空")
    if work.get("format") not in (None, "article"):
        raise ValueError("此版本仅支持公众号文章草稿")
    return title.strip(), body


def _official(value, editor=False):
    try:
        parsed = urlparse(value)
        return (parsed.scheme == "https" and parsed.hostname == HOST
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (not editor or parsed.path == EDITOR_PATH))
    except (TypeError, ValueError):
        return False


def _id_from_url(value):
    if not _official(value, editor=True):
        return None
    try:
        values = parse_qs(urlparse(value).query, keep_blank_values=True).get("appmsgid", [])
        if len(values) == 1 and _REAL_ID.fullmatch(values[0]):
            return values[0]
    except (TypeError, ValueError):
        pass
    return None


def _normalise(value):
    # Compare all visible text, allowing paragraph/whitespace rendering changes.
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fields(editor):
    if not _official(editor.url, editor=True):
        raise RuntimeError("当前页面不是已观察的公众号官方文章编辑器")
    fields = editor.locator(FIELDS_SELECTOR).filter(visible=True)
    fields.first.wait_for(state="visible", timeout=20000)
    fields.nth(1).wait_for(state="visible", timeout=20000)
    if fields.count() != 2:
        raise RuntimeError("公众号标题与正文编辑区未唯一识别，未授权写入")
    return fields.first, fields.last


def _blank(editor, fields=None):
    title, body = fields or _fields(editor)
    # A malformed/duplicated identifier is also an existing-draft marker. Only
    # the fresh popup without any appmsgid query key may receive new content.
    has_identifier = "appmsgid" in parse_qs(urlparse(editor.url).query, keep_blank_values=True)
    actual_body = body.evaluate(r"""node => {
        const clone = node.cloneNode(true);
        clone.querySelectorAll('.editor_content_placeholder[contenteditable="false"].ProseMirror-widget').forEach(widget => widget.remove());
        return clone.textContent || '';
    }""", timeout=10000)
    if has_identifier or _normalise(title.inner_text(timeout=10000)) or _normalise(actual_body):
        raise RuntimeError("公众号编辑器不是新的空白草稿，拒绝覆盖已有内容")


def _save_control(editor):
    control = editor.get_by_role("button", name="保存为草稿", exact=True).filter(visible=True)
    control.first.wait_for(state="visible", timeout=10000)
    if control.count() != 1:
        raise RuntimeError("未唯一识别公众号保存草稿按钮，未授权写入")
    return control


def _official_pages(page):
    """Prefer the supplied tab, then other already open official tabs."""
    return [page] + [candidate for candidate in reversed(page.context.pages)
                     if candidate is not page and not candidate.is_closed() and _official(candidate.url)]


def _href(source, link, path=None):
    if link.count() != 1:
        return None
    value = link.get_attribute("href", timeout=10000)
    if not value:
        return None
    value = urljoin(source.url, value)
    if not _official(value) or (path is not None and urlparse(value).path != path):
        return None
    return value


def _home_page(page):
    # URLs from observed pages/visible links remain in memory; never synthesize
    # a session token, return the full URL, or navigate an existing editor.
    for source in _official_pages(page):
        if _official(source.url) and urlparse(source.url).path == HOME_PATH:
            return source
    for source in _official_pages(page):
        href = _href(source, source.get_by_role("link", name="首页", exact=True).filter(visible=True), HOME_PATH)
        if href:
            home = page.context.new_page()
            home.goto(href, wait_until="domcontentloaded", timeout=30000)
            if _official(home.url) and urlparse(home.url).path == HOME_PATH:
                return home
            break
    raise RuntimeError("未观察到公众号官方首页入口，尚未授权写入")


def prepare(page, work):
    """Use an observed homepage, then open and validate a blank article popup."""
    _validate_work(work)
    if not _official(page.url):
        raise ValueError("请从已登录的微信公众号官方后台准备文章")
    try:
        home = _home_page(page)
        entry = home.get_by_text("文章", exact=True).filter(visible=True)
        entry.first.wait_for(state="visible", timeout=15000)
        if entry.count() != 1:
            raise RuntimeError("公众号文章入口未唯一识别，尚未创建草稿")
        with home.expect_popup(timeout=15000) as opened:
            entry.click(timeout=10000)
        editor = opened.value
        if editor is home:
            raise RuntimeError("文章编辑器未作为独立页面打开")
        editor.wait_for_load_state("domcontentloaded", timeout=20000)
        _blank(editor, _fields(editor))
        _save_control(editor)
        return editor
    except Exception as error:
        # 必须把内层原因带出来。原来 `from None` 把「文章入口未唯一识别」
        # 「编辑器不是独立页面」「空白校验失败」三种完全不同的故障压成同一句话，
        # 10-21 与 10-22 两次复现都只能看到这个笼统提示，无法定位。
        raise RuntimeError(
            "公众号新文章页面未确认空白标题、正文和保存入口；尚未授权写入"
            f"（具体原因：{type(error).__name__}: {str(error)[:200]}）") from error


def _read_editor(editor, remote_id, work):
    title, body = _validate_work(work)
    if _id_from_url(editor.url) != remote_id:
        return False
    editor.reload(wait_until="domcontentloaded", timeout=30000)
    if _id_from_url(editor.url) != remote_id:
        return False
    title_field, body_field = _fields(editor)
    # 富文本化后 innerText 不再等于 Markdown 原文，与渲染后的可见文本比较。
    expected_body = _normalise(richtext.expected_visible_text(body, "wechat"))
    try:
        editor.wait_for_function(r"""expected => {
            const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
            const fields = Array.from(document.querySelectorAll(expected.selector)).filter(visible);
            const normalise = value => String(value || '').replace(/\s+/g, ' ').trim();
            return fields.length === 2 && normalise(fields[0].innerText) === expected.title && normalise(fields[1].innerText) === expected.body;
        }""", arg={"title": _normalise(title), "body": expected_body, "selector": FIELDS_SELECTOR}, timeout=15000)
    except Exception:
        return False
    if _id_from_url(editor.url) != remote_id:
        return False
    return (_normalise(title_field.inner_text(timeout=10000)) == _normalise(title)
            and _normalise(body_field.inner_text(timeout=10000)) == expected_body)


def create(editor, work, on_id=None):
    """Fill once, click only Save Draft, persist its real ID, then reload-read."""
    title, body = _validate_work(work)
    fields = _fields(editor)
    _blank(editor, fields)
    save = _save_control(editor)
    known_id = None

    def remember():
        nonlocal known_id
        remote_id = _id_from_url(editor.url)
        if not remote_id:
            return
        if known_id and known_id != remote_id:
            raise WechatDraftUncertain("公众号编辑器的草稿标识发生变化，请人工核对")
        if known_id is None:
            known_id = remote_id
            if on_id is not None:
                on_id(remote_id)

    try:
        fields[0].fill(title, timeout=10000)
        remember()
        # 正文按 ProseMirror 富文本粘贴写入：公众号正文没有标题语义，
        # richtext 会把 Markdown 标题渲染成加粗段落。
        browser_editor.write_body(fields[1], "wechat", body, richtext)
        remember()
        from .browser_media import upload_article
        upload_article(editor, work, fields[1])
        save.click(timeout=15000)
        remember()
        editor.wait_for_url(lambda value: _id_from_url(str(value)) is not None, wait_until="domcontentloaded", timeout=30000)
        remember()
        if known_id is None:
            raise WechatDraftUncertain("已点击保存草稿，但未取得真实 appmsgid，请核对草稿箱")
        if not _read_editor(editor, known_id, work):
            raise WechatDraftUncertain("公众号草稿已有真实标识，但重新加载后的标题或正文不一致，请人工核对")
        return known_id
    except WechatDraftUncertain:
        raise
    except Exception:
        # Capture any ID already visible even if waiting or reload failed. Never
        # repeat a save click or leak a token-bearing URL in an error message.
        try:
            remember()
        except Exception:
            pass
        raise WechatDraftUncertain("公众号草稿保存或回读尚未确认，请核对平台草稿箱；不会重复创建") from None


def read(page, remote_id, work):
    """Reload this ID, or recover it using the observed draft card edit popup."""
    _validate_work(work)
    if not isinstance(remote_id, str) or not _REAL_ID.fullmatch(remote_id) or not _official(page.url):
        return False
    candidates = [editor for editor in page.context.pages if not editor.is_closed() and _id_from_url(editor.url) == remote_id]
    if page in candidates:
        candidates.remove(page)
        candidates.insert(0, page)
    try:
        if candidates:
            editor = candidates[0]
        else:
            drafts = _drafts_page(page)
            editor = _open_draft(drafts, remote_id, work["title"].strip())
        verified = _read_editor(editor, remote_id, work)
        if verified:
            page._workbench_verified_editor = editor
            editor.bring_to_front()
        return verified
    except Exception:
        return False


def _drafts_page(page):
    for source in _official_pages(page):
        menu = source.locator(DRAFT_MENU_SELECTOR).filter(visible=True)
        if menu.count() == 0:
            content = source.get_by_text("内容管理", exact=True).filter(visible=True)
            if content.count() == 1:
                content.click(timeout=10000)
                menu.first.wait_for(state="visible", timeout=10000)
        href = _href(source, menu)
        if href:
            drafts = page.context.new_page()
            drafts.goto(href, wait_until="domcontentloaded", timeout=30000)
            if not _official(drafts.url):
                raise RuntimeError("草稿箱未保留在公众号官方后台")
            return drafts
    raise RuntimeError("未唯一观察到公众号草稿箱菜单，不猜测带登录凭据的地址")


def _open_draft(drafts, remote_id, title):
    titles = drafts.locator(DRAFT_TITLE_SELECTOR).filter(has_text=re.compile(r"^\s*" + re.escape(title) + r"\s*$")).filter(visible=True)
    titles.first.wait_for(state="visible", timeout=15000)
    if titles.count() != 1:
        raise RuntimeError("公众号草稿标题未唯一匹配，不操作其他草稿")
    card = titles.locator(DRAFT_CARD_SELECTOR)
    if card.count() != 1:
        raise RuntimeError("公众号草稿卡片未唯一识别")
    card.hover(timeout=10000)
    control = card.locator("a.weui-desktop-icon20").filter(has=drafts.locator(EDIT_PATH_SELECTOR)).filter(visible=True)
    control.first.wait_for(state="visible", timeout=10000)
    if control.count() != 1:
        raise RuntimeError("公众号草稿的已观察编辑图标未唯一识别")
    control.hover(timeout=10000)
    tooltip = drafts.get_by_text("编辑", exact=True).filter(visible=True)
    tooltip.first.wait_for(state="visible", timeout=10000)
    if tooltip.count() != 1:
        raise RuntimeError("公众号编辑提示未唯一确认")
    before = list(drafts.context.pages)
    try:
        with drafts.expect_popup(timeout=15000) as opened:
            control.click(timeout=10000)
        editor = opened.value
    except Exception:
        # A late popup is read-only recoverable; never click the control twice.
        editors = [candidate for candidate in drafts.context.pages
                   if candidate not in before and not candidate.is_closed()
                   and _id_from_url(candidate.url) == remote_id]
        if len(editors) != 1:
            raise RuntimeError("草稿编辑页面未确认，不重复点击") from None
        editor = editors[0]
    editor.wait_for_load_state("domcontentloaded", timeout=20000)
    if _id_from_url(editor.url) != remote_id:
        raise RuntimeError("草稿卡的实际 appmsgid 与已记录标识不同，停止回读")
    return editor
