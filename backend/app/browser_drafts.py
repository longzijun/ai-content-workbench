"""Draft-only operations on the user-authenticated official creator web UI."""
from pathlib import Path
from urllib.parse import urlparse
import re
import json

from . import browser_bridge
from .browser_draft_state import BrowserDraftLedger, content_hash


_CONTROLS = r"""() => {
  const visible = el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
  const nodes=[...document.querySelectorAll('input,textarea,button,a,[contenteditable],iframe,[role="textbox"],[role="button"],select')];
  return nodes.filter(visible).slice(0,100).map(el=>({tag:el.tagName.toLowerCase(),id:el.id,type:el.getAttribute('type'),role:el.getAttribute('role'),name:el.getAttribute('name'),class_name:typeof el.className==='string'?el.className:'',placeholder:el.getAttribute('placeholder'),editable:el.getAttribute('contenteditable'),accept:el.getAttribute('accept'),text:['INPUT','TEXTAREA'].includes(el.tagName)?'':String(el.innerText||'').trim().slice(0,100)}));
}"""


def probe_platform(platform, action="inspect", settings=None, capture_screenshot=False):
    if action not in {"inspect", "open_editor", "open_drafts", "open_text_image", "open_long_text"}:
        raise ValueError("不支持的后台观察动作")

    def observe(page, account):
        host = urlparse(browser_bridge.PLATFORM_URLS[platform]).hostname
        candidates = [p for p in page.context.pages if not p.is_closed() and urlparse(p.url).hostname == host]
        if candidates:
            page = candidates[-1]
        path = urlparse(page.url).path
        if action == "open_editor":
            if platform == "toutiao":
                # These UI routes were observed in the authenticated creator navigation.
                page = page.context.new_page()
                page.goto("https://mp.toutiao.com/profile_v4/graphic/publish", wait_until="domcontentloaded")
                page.get_by_placeholder("请输入文章标题（2～30个字）", exact=True).wait_for(timeout=20000)
            elif platform == "xiaohongshu" and path in {"/", "/new/home"}:
                page.get_by_text("发布图文笔记", exact=True).click(timeout=8000)
            elif platform == "wechat":
                candidates = page.get_by_text("文章", exact=True)
                if candidates.count() != 1:
                    return {"state": "needs_observation", "error": "需要根据当前公众号后台确认文章创作入口"}
                candidates.click(timeout=8000)
        elif action in {"open_text_image", "open_long_text"}:
            if platform != "xiaohongshu":
                return {"state": "needs_observation", "error": "此观察动作只适用于小红书"}
            page.get_by_text("文字配图" if action == "open_text_image" else "写长文", exact=True).filter(visible=True).last.click(timeout=8000)
        elif action == "open_drafts":
            if platform == "toutiao":
                page.goto("https://mp.toutiao.com/profile_v4/manage/draft", wait_until="domcontentloaded")
            else:
                candidates = page.get_by_text(re.compile(r"^草稿箱(?:\(\d+\))?$"))
                if candidates.count() != 1:
                    return {"state": "needs_observation", "error": "当前页面未显示唯一草稿箱入口"}
                candidates.click(timeout=8000)
        page.wait_for_timeout(1200)
        candidates = [p for p in page.context.pages if not p.is_closed() and urlparse(p.url).hostname == host]
        if candidates:
            page = candidates[-1]
        result = {"state": "observed", "platform": platform, "account_label": account, "url": browser_bridge._safe_url(page.url), "text": browser_bridge._safe_text(page.locator("body").inner_text()[:5000]), "controls": page.evaluate(_CONTROLS)}
        if capture_screenshot:
            directory = (settings or browser_bridge.get_settings()).runtime_dir / "browser-qa"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (platform + "-editor-observation.png")
            page.screenshot(path=str(target), full_page=False)
            result["screenshot_path"] = str(target)
        return result

    return browser_bridge.run_in_page(platform, observe, settings, timeout_seconds=35)


def sync_browser_draft(draft, version, topic, settings):
    return _draft_operation(draft, version, topic, settings, open_only=False)


def _module(platform):
    if platform == 'wechat':
        from . import browser_wechat
        return browser_wechat
    if platform == 'toutiao':
        from . import browser_toutiao
        return browser_toutiao
    if platform == 'xiaohongshu':
        from . import browser_xiaohongshu
        return browser_xiaohongshu
    raise ValueError('不支持的平台')


def _native_id(platform, value):
    prefix = 'browser:' + platform + ':'
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    bare = value[len(prefix):]
    if not bare or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}', bare):
        return None
    return bare


def _draft_operation(draft, version, topic, settings, open_only):
    platform = draft['platform']
    adapter = _module(platform)
    work = next((w for w in version['payload']['works'] if w['platform'] == platform), None)
    if not work:
        return {'state': 'blocked', 'error': '当前版本没有此平台稿件'}
    if topic['id'] != draft['topic_id'] or version['id'] != draft['version_id']:
        return {'state': 'blocked', 'error': '草稿与版本绑定不一致'}
    ledger = BrowserDraftLedger(settings.runtime_dir)
    prefix = 'browser:' + platform + ':'
    from .image_assets import get_asset_manifest
    manifest = get_asset_manifest(settings, topic['id'],version['id'],platform)
    # 2026-10-18：出图失败不阻断交付——只要存在已验证素材就随稿上传（允许部分交付）。
    injectable = bool(manifest.get('assets')) and manifest['status'] in ('ready', 'failed', 'unknown', 'cancelled', 'needs_review')
    delivery_work = {**work,'_image_assets':manifest['assets']} if injectable else work

    def operate(page, account):
        if draft.get('account_label') and draft['account_label'] != account:
            return {'state': 'unknown', 'remote_id': draft.get('remote_id'), 'error': '当前后台账号与原草稿账号不一致，请切回原账号'}
        evidence = ledger.load(topic['id'], version['id'], platform)
        if evidence and (evidence['account_label'] != account or evidence['content_hash'] != content_hash(work)):
            return {'state': 'blocked', 'remote_id': draft.get('remote_id'), 'error': '保存记录的账号或版本内容已改变，未执行平台写入'}
        remote_id = _native_id(platform, draft.get('remote_id'))
        if draft.get('remote_id') and not remote_id:
            return {'state': 'blocked', 'error': '此草稿不是该平台网页登录生成的草稿，未创建新记录'}
        if evidence and evidence.get('remote_id'):
            if remote_id and remote_id != evidence['remote_id']:
                return {'state': 'blocked', 'remote_id': draft.get('remote_id'), 'error': '工作台与保存记录的草稿标识不一致，请核对'}
            remote_id = evidence['remote_id']
        claim = None

        def verified(bare, checked_page=None):
            browser_bridge.mark_draft_verified(platform, account, settings)
            from .browser_media import verify_media
            media = verify_media(checked_page or page, delivery_work)
            if open_only:
                return {'state': 'opened', 'remote_id': prefix + bare, 'account_label': account, 'content_verified': True,
                        'storage_scope': 'browser' if platform == 'xiaohongshu' else 'platform', 'error': None}
            return {'state': 'browser_saved' if platform == 'xiaohongshu' else 'synced', 'remote_id': prefix + bare,
                    'destination': 'browser' if platform == 'xiaohongshu' else 'platform', 'account_label': account,
                    'remote_url': browser_bridge.PLATFORM_URLS[platform], 'error': None,'media':media}

        # A known ID permits only readback, even when the on-disk ledger is absent.
        if remote_id:
            try:
                if adapter.read(page, remote_id, delivery_work):
                    return verified(remote_id)
            except Exception:
                pass
            return {'state': 'unknown', 'remote_id': prefix + remote_id, 'error': '已有草稿标识，但后台标题或正文回读未确认；请核对，不会重复创建'}
        if open_only:
            return {'state': 'blocked', 'error': '没有已保存的真实草稿标识，无法打开审核'}
        if draft.get('state') == 'unknown' or evidence:
            return {'state': 'unknown', 'error': '上次保存结果待核对；没有已知草稿标识，不会重复创建'}
        try:
            editor = adapter.prepare(page, delivery_work)
        except ValueError as exc:
            return {'state': 'blocked', 'error': str(exc)}
        except Exception as error:
            # 不要用一句固定文案覆盖真实原因：那会让「入口没开」与「字段找不到」
            # 在界面上长得一模一样，每次只能靠猜。
            return {'state': 'failed',
                    'error': f'官方编辑器准备失败，尚未写入内容（{type(error).__name__}: {str(error)[:220]}），请检查页面'}
        try:
            claim = ledger.claim(topic['id'], version['id'], platform, work, account)
            if claim.action == 'blocked':
                return {'state': 'unknown', 'error': claim.reason}
            if claim.action == 'read':
                if adapter.read(page, claim.remote_id, delivery_work):
                    return verified(claim.remote_id)
                return {'state': 'unknown', 'remote_id': prefix + claim.remote_id, 'error': '已有草稿回读未确认，不会重复创建'}
            def remember(bare):
                ledger.record_saved(claim, bare)
            remote_id = adapter.create(editor, delivery_work, on_id=remember)
            ledger.record_saved(claim, remote_id)
            # Modules create only returns after independent persisted readback.
            return verified(remote_id,editor)
        except Exception as error:
            if claim is not None and claim.action in ('create', 'read'):
                try:
                    ledger.mark_unknown(claim)
                except Exception:
                    pass
            try:
                saved = ledger.load(topic['id'], version['id'], platform)
                bare = saved.get('remote_id') if saved else remote_id
            except Exception:
                bare = remote_id
            # 具体原因必须带出来：插入图后正文校验、保存确认超时、回读不一致，
            # 三者的处置完全不同，压成一句话会让人重复排查已经排除的方向。
            return {'state': 'unknown', 'remote_id': prefix + bare if bare else None,
                    'error': f'草稿保存或回读发生中断（{type(error).__name__}: {str(error)[:220]}）；'
                             '请先核对官方草稿箱，已记录此次尝试，不会重复创建'}

    result = browser_bridge.run_in_page(platform, operate, settings, timeout_seconds=180)
    # Driver timeouts cannot discard an ID already durably observed by callback.
    if result.get('state') == 'unknown' and not result.get('remote_id'):
        try:
            evidence = ledger.load(topic['id'], version['id'], platform)
            if evidence and evidence.get('remote_id') and evidence['content_hash'] == content_hash(work):
                result['remote_id'] = prefix + evidence['remote_id']
                result['account_label'] = evidence['account_label']
        except Exception:
            pass
    return result


def open_browser_draft(draft, version, topic, settings):
    """Open and verify an already saved draft in the dedicated Edge; no writes."""
    return _draft_operation(draft, version, topic, settings, open_only=True)


def verify_saved_connection_draft(platform, settings):
    """Recheck an already observed private integration draft, without creating."""
    adapter = _module(platform)
    path = settings.runtime_dir / 'data' / 'platform-verification.json'
    try:
        if path.stat().st_size > 65536:
            raise ValueError('proof size')
        proof = json.loads(path.read_text('utf-8')).get(platform)
        if not isinstance(proof, dict) or not proof.get('account_label') or not proof.get('remote_id') or not isinstance(proof.get('work'), dict):
            raise ValueError('missing proof')
    except Exception:
        return {'state': 'blocked', 'error': '没有已保存的连接测试稿；实际内容首次保存并回读成功后会验证通道，不会为检查再创建草稿'}
    def verify(page, account):
        if proof['account_label'] != account:
            return {'state': 'blocked', 'error': '后台账号与原连接测试稿不同，未打开其他账号草稿'}
        if not adapter.read(page, proof['remote_id'], proof['work']):
            return {'state': 'unknown', 'error': '原测试草稿已删除、被修改或回读未确认；未创建新草稿'}
        browser_bridge.mark_draft_verified(platform, account, settings)
        return {'state': 'verified', 'storage_scope': 'browser' if platform == 'xiaohongshu' else 'platform', 'content_verified': True}
    return browser_bridge.run_in_page(platform, verify, settings, timeout_seconds=90)
