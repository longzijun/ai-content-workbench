"""Article drafts through the observed official Toutiao creator web UI only.

The common adapter owns account checks and BrowserDraftLedger claims. Call
``prepare`` before claiming, then ``create(..., on_id=...)`` once. An exception
after typing means uncertainty, never permission to create another draft.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from . import browser_editor, richtext


ORIGIN = "https://mp.toutiao.com"
PUBLISH_PATH = "/profile_v4/graphic/publish"
DRAFTS_PATH = "/profile_v4/manage/draft"
TITLE_PLACEHOLDER = "请输入文章标题（2～30个字）"
BODY_SELECTOR = 'div.ProseMirror[contenteditable="true"]'
_REAL_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_SAVED = re.compile(r"^(?:草稿)?已(?:自动)?保存(?:[（(：:\s].*)?$")


class ToutiaoDraftUncertain(RuntimeError):
    """A UI write may have reached the platform; verify instead of creating again."""


def _validate_work(work):
    if not isinstance(work, dict) or work.get("platform") != "toutiao":
        raise ValueError("头条适配器需要对应平台的作品")
    from .platform_rules import validate_work
    validate_work(work)
    title, body = work['title'], work['body']
    return title.strip(), body


def _official_editor(page):
    try:
        parsed = urlparse(page.url)
        return (parsed.scheme == "https" and parsed.hostname == "mp.toutiao.com"
                and parsed.path == PUBLISH_PATH and not parsed.username and not parsed.password
                and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def _fields(page):
    if not _official_editor(page):
        raise RuntimeError("当前页面不是已观察的头条官方文章编辑器")
    title = page.get_by_placeholder(TITLE_PLACEHOLDER, exact=True).filter(visible=True)
    body = page.locator(BODY_SELECTOR).filter(visible=True)
    title.first.wait_for(state="visible", timeout=20000)
    body.first.wait_for(state="visible", timeout=20000)
    if title.count() != 1 or body.count() != 1:
        raise RuntimeError("头条标题或正文编辑区未唯一识别，尚未授权写入")
    return title, body


def _id_from_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "mp.toutiao.com" or parsed.path != PUBLISH_PATH or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        identifiers = parse_qs(parsed.query, keep_blank_values=True).get("pgc_id", [])
        if len(identifiers) == 1 and _REAL_ID.fullmatch(identifiers[0]):
            return identifiers[0]
    except (ValueError, TypeError):
        pass
    return None


def _normalise(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    # Rich text paragraphs can add blank lines; the text of every line must match.
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def _body_text(field, work):
    if not work.get('_image_assets'):
        return field.inner_text(timeout=10000)
    return field.evaluate('''node => {
      const clone=node.cloneNode(true);
      clone.querySelectorAll('.pgc-img-wrapper').forEach(e=>e.remove());
      clone.querySelectorAll('br').forEach(e=>e.replaceWith(document.createTextNode('\\n')));
      clone.querySelectorAll('p,section,div,h1,h2,h3,h4,li').forEach(e=>e.appendChild(document.createTextNode('\\n')));
      return clone.textContent||'';
    }''', timeout=10000)


def _verify_body_after_images(body_field, work, body):
    """图片插入后立刻确认正文没被覆盖。

    2026-10-22 实测到一次真实的坏写入：光标定位到了第三段，三张图插进去
    把那一段覆盖掉，平台草稿里留下了缺段正文，而写入阶段一切「成功」，
    直到换页回读才暴露。这里在保存确认之前拦住，并把原因指向图片插入，
    免得后续再去怀疑编辑器加载或网络。

    只做正文比对，不碰图片数量 —— 图片数量由 `_loaded()` 单独守。
    没有配图时直接跳过：没有插入动作，就不存在插入覆盖。
    """
    if not work.get('_image_assets'):
        return
    expected = _normalise(richtext.expected_visible_text(body, "toutiao"))
    actual = _normalise(_body_text(body_field, work))
    if actual != expected:
        raise ToutiaoDraftUncertain(
            "插入图片后正文与预期不一致（可能覆盖了段落）；已在保存确认前停止，"
            "请人工核对头条草稿箱中这一篇，不要直接重试")


def _blank(editor, fields=None):
    title, body = fields or _fields(editor)
    has_identifier = "pgc_id" in parse_qs(urlparse(editor.url).query, keep_blank_values=True)
    # Only the observed noneditable widget is excluded, on a detached clone.
    # A user-written paragraph saying the same words remains real content.
    actual_body = body.evaluate(r"""node => {
        const clone = node.cloneNode(true);
        clone.querySelectorAll('.syl-placeholder[contenteditable="false"].ProseMirror-widget').forEach(widget => widget.remove());
        return clone.textContent || '';
    }""", timeout=10000)
    if has_identifier or title.input_value(timeout=10000).strip() or _normalise(actual_body):
        raise RuntimeError("头条编辑器不是新的空白草稿，拒绝覆盖已有内容")


def _read_editor(editor, remote_id, work):
    title, body = _validate_work(work)
    url_ok = _id_from_url(editor.url) == remote_id
    _log_read_event("read-stage", work, remote_id, "pre-reload url_ok=%s url=%s" % (url_ok, editor.url))
    if not url_ok:
        return False
    editor.reload(wait_until="domcontentloaded", timeout=30000)
    url_ok = _id_from_url(editor.url) == remote_id
    _log_read_event("read-stage", work, remote_id, "post-reload url_ok=%s url=%s" % (url_ok, editor.url))
    if not url_ok:
        return False
    title_field, body_field = _fields(editor)
    # 富文本化后 innerText 不再等于 Markdown 原文，与渲染后的可见文本比较。
    expected_body = _normalise(richtext.expected_visible_text(body, "toutiao"))
    _log_read_event("read-stage", work, remote_id, "fields-ok body_len=%d img=%s" % (len(expected_body), bool(work.get('_image_assets'))))
    try:
        editor.wait_for_function(r"""expected => {
            const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
            const titles = Array.from(document.querySelectorAll('textarea')).filter(node => node.placeholder === expected.placeholder && visible(node));
            const bodies = Array.from(document.querySelectorAll(expected.selector)).filter(visible);
            const normalise = value => String(value || '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ').split('\n').map(line => line.trim()).filter(Boolean).join('\n');
            let actual=bodies[0]?.innerText;
            if(expected.ignore_image_ui&&bodies[0]) {
              const clone=bodies[0].cloneNode(true);
              clone.querySelectorAll('.pgc-img-wrapper').forEach(e=>e.remove());
              clone.querySelectorAll('br').forEach(e=>e.replaceWith(document.createTextNode('\n')));
              clone.querySelectorAll('p,section,div,h1,h2,h3,h4,li').forEach(e=>e.appendChild(document.createTextNode('\n')));
              actual=clone.textContent||'';
            }
            return titles.length === 1 && bodies.length === 1 && titles[0].value.trim() === expected.title && normalise(actual) === expected.body;
        }""", arg={"title": title, "body": expected_body, "placeholder": TITLE_PLACEHOLDER, "selector": BODY_SELECTOR,'ignore_image_ui':bool(work.get('_image_assets'))}, timeout=15000)
        _log_read_event("read-stage", work, remote_id, "wait_for_function OK")
    except Exception:
        _log_read_event("read-stage", work, remote_id, "wait_for_function FAILED")
        try:
            snap = editor.evaluate(r"""expected => {
                const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
                const titles = Array.from(document.querySelectorAll('textarea')).filter(node => node.placeholder === expected.placeholder && visible(node));
                const bodies = Array.from(document.querySelectorAll(expected.selector)).filter(visible);
                const normalise = value => String(value || '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ').split('\n').map(line => line.trim()).filter(Boolean).join('\n');
                let actual=bodies[0]?.innerText;
                if(expected.ignore_image_ui&&bodies[0]) {
                  const clone=bodies[0].cloneNode(true);
                  clone.querySelectorAll('.pgc-img-wrapper').forEach(e=>e.remove());
                  clone.querySelectorAll('br').forEach(e=>e.replaceWith(document.createTextNode('\n')));
                  clone.querySelectorAll('p,section,div,h1,h2,h3,h4,li').forEach(e=>e.appendChild(document.createTextNode('\n')));
                  actual=clone.textContent||'';
                }
                return {titleCount:titles.length, bodyCount:bodies.length, titleValue:(titles[0]?.value||'').slice(0,120), actual:(actual||'').slice(0,2000), expectedTitle:expected.title, expectedBody:expected.body};
            }""", arg={"title": title, "body": expected_body, "placeholder": TITLE_PLACEHOLDER, "selector": BODY_SELECTOR,'ignore_image_ui':bool(work.get('_image_assets'))})
            import json as _json
            _log_read_event("read-snapshot", work, remote_id, _json.dumps(snap, ensure_ascii=False)[:1800])
        except Exception as _snap_exc:
            _log_read_event("read-snapshot", work, remote_id, "snapshot capture FAILED: %r" % (_snap_exc,))
        for _sample in (1, 2, 3):
            import time as _time
            _time.sleep(8)
            try:
                probe = editor.evaluate(r"""expected => {
                    const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
                    const bodies = Array.from(document.querySelectorAll(expected.selector)).filter(visible);
                    const text = (bodies[0]?.textContent||'').slice(0,160);
                    return {hasBody: bodies.length>=1, text: text, htmlLen: (bodies[0]?.innerHTML||'').length, childCount: bodies[0]?.children.length};
                }""", arg={"selector": BODY_SELECTOR})
                import json as _json2
                _log_read_event("read-probe%d" % _sample, work, remote_id, _json2.dumps(probe, ensure_ascii=False)[:600])
            except Exception as _probe_exc:
                _log_read_event("read-probe%d" % _sample, work, remote_id, "probe FAILED: %r" % (_probe_exc,))
        return False
    url_ok = _id_from_url(editor.url) == remote_id
    _log_read_event("read-stage", work, remote_id, "post-wait url_ok=%s" % url_ok)
    if not url_ok:
        return False
    matched = title_field.input_value(timeout=10000).strip() == title and _normalise(_body_text(body_field,work)) == expected_body
    _log_read_event("read-stage", work, remote_id, "final-compare matched=%s" % matched)
    return matched


def prepare(page, work):
    """Open a blank independent editor; do not type or alter the account homepage."""
    _validate_work(work)
    if urlparse(page.url).hostname != "mp.toutiao.com":
        raise ValueError("请从已登录的头条官方后台准备文章")
    editor = page.context.new_page()
    editor.goto(ORIGIN + PUBLISH_PATH, wait_until="domcontentloaded", timeout=30000)
    fields = _fields(editor)
    _blank(editor, fields)
    try:
        editor.wait_for_function(r"""expected => {
            const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
            const titles = Array.from(document.querySelectorAll('textarea')).filter(node => node.placeholder === expected.placeholder && visible(node));
            const bodies = Array.from(document.querySelectorAll(expected.selector)).filter(visible);
            if (titles.length !== 1 || bodies.length !== 1 || titles[0].value.trim()) return false;
            const clone = bodies[0].cloneNode(true);
            clone.querySelectorAll('.syl-placeholder[contenteditable="false"].ProseMirror-widget').forEach(widget => widget.remove());
            return !(clone.textContent || '').trim();
        }""", arg={"title": "", "body": "", "placeholder": TITLE_PLACEHOLDER, "selector": BODY_SELECTOR}, timeout=15000)
    except Exception:
        raise RuntimeError("头条空白编辑器字段状态未确认，拒绝覆盖已有内容") from None
    _blank(editor, fields)
    return editor


def _edit_control(drafts_page, title):
    titles = drafts_page.get_by_text(title, exact=True).filter(visible=True)
    titles.first.wait_for(state="visible", timeout=20000)
    if titles.count() != 1:
        raise ToutiaoDraftUncertain("草稿箱中标题未唯一匹配，请人工核对；不会操作其他草稿")
    container = titles
    for _ in range(7):
        container = container.locator("xpath=..")
        controls = container.get_by_text("编辑", exact=True).filter(visible=True)
        count = controls.count()
        if count == 1:
            return controls
        if count > 1:
            break
    raise ToutiaoDraftUncertain("该标题的草稿编辑入口未唯一识别，请人工核对；未点击其他草稿")


def _open_existing(drafts_page, control):
    before = list(drafts_page.context.pages)
    popup = None
    try:
        with drafts_page.expect_popup(timeout=8000) as opened:
            control.click(timeout=10000)
        popup = opened.value
    except Exception:
        # Some layouts edit in the same tab. The click above is never repeated.
        candidates = [candidate for candidate in drafts_page.context.pages if candidate not in before and not candidate.is_closed() and _official_editor(candidate)]
        if candidates:
            popup = candidates[-1]
        elif _official_editor(drafts_page):
            popup = drafts_page
    if popup is None:
        raise ToutiaoDraftUncertain("已点击对应草稿，但编辑页面未确认；请在平台核对")
    popup.wait_for_load_state("domcontentloaded", timeout=15000)
    return popup


def create(editor, work, on_id=None):
    """Fill once, observe autosave, identify in the draft box and reload-read it.

    ``on_id`` is called as soon as a real public-UI pgc_id is observed so the
    common ledger can persist it before later navigation or verification fails.
    This function never clicks any publish, scheduling or preview control.
    """
    title, body = _validate_work(work)
    title_field, body_field = _fields(editor)
    _blank(editor, (title_field, body_field))
    known_id = None

    def remember(remote_id):
        nonlocal known_id
        if not remote_id:
            return
        if known_id and known_id != remote_id:
            raise ToutiaoDraftUncertain("编辑器与草稿箱显示的草稿标识不一致，请人工核对")
        if known_id is None:
            known_id = remote_id
            if on_id is not None:
                on_id(remote_id)

    # These locator fills are the only platform writes performed by this module.
    title_field.fill(title, timeout=10000)
    remember(_id_from_url(editor.url))
    # 头条文章支持小标题，richtext 按平台输出 h2/h3，交给 ProseMirror 渲染。
    browser_editor.write_body(body_field, "toutiao", body, richtext)
    remember(_id_from_url(editor.url))
    from .browser_media import upload_article
    upload_article(editor, work, body_field)
    _verify_body_after_images(body_field, work, body)
    try:
        editor.get_by_text(_SAVED).filter(visible=True).first.wait_for(state="visible", timeout=30000)
    except Exception:
        remember(_id_from_url(editor.url))
        raise ToutiaoDraftUncertain("正文已填写，尚未确认自动保存状态；请先核对头条草稿箱") from None
    remember(_id_from_url(editor.url))

    drafts_page = editor.context.new_page()
    drafts_page.goto(ORIGIN + DRAFTS_PATH, wait_until="domcontentloaded", timeout=30000)
    control = _edit_control(drafts_page, title)
    # A visible edit link is also real UI evidence; never persist its whole URL.
    href = control.get_attribute("href")
    if href:
        remember(_id_from_url(urljoin(ORIGIN, href)))
    saved_editor = _open_existing(drafts_page, control)
    remote_id = _id_from_url(saved_editor.url)
    if not remote_id:
        raise ToutiaoDraftUncertain("已找到对应草稿，但公开编辑页面没有可确认的 pgc_id")
    remember(remote_id)
    if not _read_editor(saved_editor, remote_id, work):
        raise ToutiaoDraftUncertain("平台草稿已有真实标识，但重新加载后的标题或正文不一致；请人工审核")
    editor._workbench_verified_editor = saved_editor
    return remote_id


def read(page, remote_id, work):
    """Open only this known draft in a new tab and verify after a server reload."""
    _validate_work(work)
    _log_read_event("read-enter", work, remote_id, "page=" + str(page.url))
    if not isinstance(remote_id, str) or not _REAL_ID.fullmatch(remote_id):
        _log_read_event("read-bad-id", work, remote_id, "")
        return False
    if urlparse(page.url).hostname != "mp.toutiao.com":
        _log_read_event("read-wrong-host", work, remote_id, "page=" + str(page.url))
        return False
    try:
        # The official publish view ignores an external ?pgc_id= query (it only
        # restores the most recently edited draft as a SPA convenience), so the
        # only reliable route back to a known draft is through the draft box's
        # unique "编辑" control, exactly as create() verifies after autosave.
        drafts_page = page.context.new_page()
        drafts_page.goto(ORIGIN + DRAFTS_PATH, wait_until="domcontentloaded", timeout=30000)
        control = _edit_control(drafts_page, work["title"])
        editor = _open_existing(drafts_page, control)
        if _id_from_url(editor.url) != remote_id:
            raise ToutiaoDraftUncertain("草稿箱入口打开的编辑页草稿标识与记录不一致")
        verified = _read_editor(editor, remote_id, work)
        if verified:
            page._workbench_verified_editor = editor
            editor.bring_to_front()
        return verified
    except Exception:
        _log_read_failure(work, remote_id)
        return False


def _log_read_failure(work, remote_id):
    """Persist the real readback failure so a swallowed False stays diagnosable."""
    try:
        from datetime import datetime
        from .config import get_settings
        import traceback as _tb
        log_dir = get_settings().runtime_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "toutiao-read-errors.log"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n=== %s ===\n" % datetime.now().isoformat(timespec="seconds"))
            handle.write("title=%r remote_id=%r\n" % (work.get("title"), remote_id))
            handle.write(_tb.format_exc())
    except Exception:
        pass


def _log_read_event(event, work, remote_id, detail=""):
    try:
        from datetime import datetime
        from .config import get_settings
        log_dir = get_settings().runtime_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "toutiao-read-events.log"
        with target.open("a", encoding="utf-8") as handle:
            handle.write("%s %s title=%r remote_id=%r %s\n" % (
                datetime.now().isoformat(timespec="seconds"), event,
                work.get("title"), remote_id, detail))
    except Exception:
        pass
