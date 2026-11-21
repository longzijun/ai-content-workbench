"""Official Xiaohongshu web drafts, stored ONLY in the dedicated browser.

Uses the observed text-to-image composer and its native Save and Leave button.
No publish controls or private platform APIs are called. A data-draft-id is a
browser-local UI identifier, never evidence of a cloud/App draft.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import browser_editor, richtext

HOST = 'creator.xiaohongshu.com'
ORIGIN = 'https://' + HOST
PUBLISH_PATH = '/publish/publish'
TITLE_PLACEHOLDER = '填写标题会有更多赞哦'
BODY_SELECTOR = '.tiptap[contenteditable="true"]'
CARD_SELECTOR = '.draft-item[data-draft-type="image"][data-draft-id]'
_REAL_ID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z', re.I)


class XiaohongshuDraftUncertain(RuntimeError):
    pass


def _validate_work(work):
    if not isinstance(work, dict) or work.get('platform') != 'xiaohongshu':
        raise ValueError('小红书适配器需要对应平台的笔记')
    from .platform_rules import validate_work
    validate_work(work)
    title, body = work['title'], work['body']
    return title.strip(), body


def _official(page):
    parsed = urlparse(page.url)
    return parsed.scheme == 'https' and parsed.hostname == HOST and parsed.path == PUBLISH_PATH


def _normalise(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _expected_body(body):
    """小红书正文按纯文本交付，期望文本等于实际填入编辑器的内容。"""
    return _normalise(richtext.expected_visible_text(body, 'xiaohongshu'))


def _wait_content(page, title, body):
    page.wait_for_function("""expected => {
      const t=document.querySelector('input[placeholder="填写标题会有更多赞哦"]');
      const b=document.querySelector('.tiptap[contenteditable="true"]');
      const norm=s=>String(s||'').replace(/\\s+/g,' ').trim();
      return t&&b&&t.value.trim()===expected.title&&norm(b.innerText)===norm(expected.body);
    }""", arg={'title': title, 'body': _expected_body(body)}, timeout=15000)


def _new_landing(page):
    if urlparse(page.url).hostname != HOST:
        raise ValueError('请从已登录的小红书官方后台打开草稿')
    landing = page.context.new_page()
    landing.goto(ORIGIN + PUBLISH_PATH, wait_until='domcontentloaded', timeout=30000)
    image_tab = landing.get_by_text('上传图文', exact=True).filter(visible=True)
    image_tab.last.wait_for(state='visible', timeout=20000)
    image_tab.last.click(timeout=10000)
    landing.get_by_text('文字配图', exact=True).filter(visible=True).first.wait_for(timeout=15000)
    return landing


def _open_box(landing):
    entry = landing.get_by_text(re.compile(r'^草稿箱(?:\(\d+\))?$')).filter(visible=True)
    entry.first.wait_for(state='visible', timeout=15000)
    if entry.count() != 1:
        raise RuntimeError('小红书网页草稿箱入口未唯一识别，未修改草稿')
    entry.click(timeout=10000)
    landing.get_by_text(re.compile(r'草稿存储于当前使用的浏览器本地')).filter(visible=True).first.wait_for(timeout=15000)
    image_drafts = landing.get_by_text(re.compile(r'^图文笔记\(\d+\)$')).filter(visible=True)
    if image_drafts.count() != 1:
        raise RuntimeError('网页草稿箱图文分类未唯一识别')
    image_drafts.click(timeout=10000)
    return landing.locator(CARD_SELECTOR).filter(visible=True)


def prepare(page, work):
    """Validate limits and available local capacity before any content write."""
    _validate_work(work)
    landing = _new_landing(page)
    box = landing.get_by_text(re.compile(r'^草稿箱\((\d+)\)$')).filter(visible=True)
    if box.count() == 1:
        match = re.search(r'\((\d+)\)', box.inner_text())
        if match and int(match.group(1)) >= 100:
            raise ValueError('小红书网页草稿箱已达 100 篇，请先手动整理；不会触发自动删除旧稿')
    if work.get('_image_assets'):
        upload = landing.locator('input[type="file"][accept=".jpg,.jpeg,.png,.webp"][multiple]')
        if upload.count() != 1 or landing.get_by_placeholder(TITLE_PLACEHOLDER,exact=True).count():
            raise RuntimeError('小红书上传页未确认为新的空白图文入口')
        return landing
    text_entry = landing.get_by_text('文字配图', exact=True).filter(visible=True)
    if text_entry.count() != 1:
        raise RuntimeError('未唯一识别小红书原生文字配图入口')
    text_entry.click(timeout=10000)
    field = landing.locator(BODY_SELECTOR).filter(visible=True)
    field.first.wait_for(timeout=15000)
    if field.count() != 1 or _normalise(field.inner_text()):
        raise RuntimeError('文字配图编辑器未确认为空白，拒绝覆盖已有内容')
    landing.get_by_text('生成图片', exact=True).filter(visible=True).first.wait_for(timeout=10000)
    return landing


def _fields(page):
    if not _official(page):
        raise RuntimeError('当前页面不是已观察的小红书官方编辑器')
    title = page.get_by_placeholder(TITLE_PLACEHOLDER, exact=True).filter(visible=True)
    body = page.locator(BODY_SELECTOR).filter(visible=True)
    title.first.wait_for(timeout=20000)
    body.first.wait_for(timeout=20000)
    if title.count() != 1 or body.count() != 1:
        raise RuntimeError('小红书笔记标题或正文编辑区未唯一识别')
    return title, body


def _save_leave(page):
    """Click the exact native accessible save button in a closed shadow root."""
    host = page.locator('xhs-publish-btn[is-save-draft="true"][save-text="暂存离开"]').filter(visible=True)
    page.wait_for_function('''() => {
      const nodes=[...document.querySelectorAll('xhs-publish-btn[is-save-draft="true"][save-text="暂存离开"]')];
      return nodes.length===1&&nodes[0].getAttribute('save-disabled')==='false';
    }''',timeout=30000)
    if host.count() != 1 or host.get_attribute('save-disabled') != 'false':
        raise XiaohongshuDraftUncertain('原生暂存按钮不可用，未点击发布；请在后台核对')
    session = page.context.new_cdp_session(page)
    try:
        nodes = session.send('Accessibility.getFullAXTree')['nodes']
        matches = [n for n in nodes if not n.get('ignored') and n.get('role', {}).get('value') == 'button'
                   and n.get('name', {}).get('value') == '暂存离开' and n.get('backendDOMNodeId')
                   and not any(p.get('name') == 'disabled' and p.get('value', {}).get('value') is True for p in n.get('properties', []))]
        if len(matches) != 1:
            raise XiaohongshuDraftUncertain('未唯一识别原生暂存按钮，不会点击其他控件')
        quad = session.send('DOM.getBoxModel', {'backendNodeId': matches[0]['backendDOMNodeId']})['model']['content']
        x, y = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
        bounds = host.bounding_box()
        if not bounds or not (bounds['x'] <= x <= bounds['x'] + bounds['width'] and bounds['y'] <= y <= bounds['y'] + bounds['height']):
            raise XiaohongshuDraftUncertain('暂存按钮位置已变化，未点击任何控件')
        page.mouse.click(x, y)
    finally:
        session.detach()


def create(editor, work, on_id=None):
    """Create native text cards and save ONE local web draft; never publish."""
    title, body = _validate_work(work)
    if work.get('_image_assets'):
        from .browser_media import upload_note
        upload_note(editor,work)
    else:
        source = editor.locator(BODY_SELECTOR).filter(visible=True)
        if source.count() != 1 or _normalise(source.inner_text()):
            raise XiaohongshuDraftUncertain('文字配图编辑器不为空，拒绝覆盖')
        source.fill(title + '\n\n' + richtext.to_text(body, 'xiaohongshu'), timeout=15000)
        editor.get_by_text('生成图片', exact=True).filter(visible=True).click(timeout=10000)
        next_step = editor.get_by_text('下一步', exact=True).filter(visible=True)
        next_step.first.wait_for(timeout=30000)
        if next_step.count() != 1:
            raise XiaohongshuDraftUncertain('文字配图生成后未唯一识别下一步，请检查后台')
        next_step.click(timeout=10000)
    guide = editor.get_by_role('button', name='我知道了', exact=True).filter(visible=True)
    if guide.count() == 1:
        guide.click(timeout=5000)
    title_field, body_field = _fields(editor)
    title_field.fill(title, timeout=10000)
    # 小红书笔记正文为纯文本，richtext 去掉全部 Markdown 标记并保留可读结构。
    browser_editor.write_body(body_field, 'xiaohongshu', body, richtext)
    _save_leave(editor)
    # Save and Leave normally opens the box; wait for the actual saved card.
    titles = editor.locator(CARD_SELECTOR).filter(has=editor.get_by_text(title, exact=True)).filter(visible=True)
    titles.first.wait_for(timeout=20000)
    if titles.count() != 1:
        raise XiaohongshuDraftUncertain('网页草稿箱未唯一匹配此标题，请手动核对；不会重复创建')
    remote_id = titles.get_attribute('data-draft-id')
    if not isinstance(remote_id, str) or not _REAL_ID.fullmatch(remote_id):
        raise XiaohongshuDraftUncertain('网页草稿已暂存但未取得真实本地草稿标识')
    if on_id is not None:
        on_id(remote_id)
    if not read(editor, remote_id, work):
        raise XiaohongshuDraftUncertain('本地网页草稿已有标识，但重新打开后的内容未通过核对')
    return remote_id


def read(page, remote_id, work):
    """Find the exact local UI ID in a fresh page and reopen only that draft."""
    title, body = _validate_work(work)
    if not isinstance(remote_id, str) or not _REAL_ID.fullmatch(remote_id):
        return False
    try:
        landing = _new_landing(page)
        _open_box(landing)
        card = landing.locator(CARD_SELECTOR + '[data-draft-id="' + remote_id + '"]').filter(visible=True)
        if card.count() != 1:
            return False
        if card.locator('.draft-title-text').inner_text(timeout=10000).strip() != title:
            return False
        edit = card.get_by_text('编辑', exact=True).filter(visible=True)
        if edit.count() != 1:
            return False
        edit.click(timeout=10000)
        title_field, body_field = _fields(landing)
        _wait_content(landing, title, body)
        matched = title_field.input_value(timeout=10000).strip() == title and _normalise(body_field.inner_text(timeout=10000)) == _expected_body(body)
        if matched:
            page._workbench_verified_editor = landing
            landing.bring_to_front()
        return matched
    except Exception:
        return False
