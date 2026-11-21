"""Mock API frontend behavior check. Screenshots are viewport emulation, not physical iPhone evidence."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ['wechat', 'toutiao', 'xiaohongshu', 'weixin_channels', 'kuaishou', 'douyin']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18769)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    works = []
    for platform in PLATFORMS:
        work = dict(platform=platform, format='note' if platform == 'xiaohongshu' else 'article', title='隔离功能验证', body='隔离正文，仅用于界面行为测试。', tags=[], source_urls=[], image_prompts=[])
        if platform in PLATFORMS[3:]:
            shots = [dict(id=role, order=i + 1, role=role, spoken_text='隔离测试口播。', visual_type='screen_recording', visual_notes='待录制', subtitle_points=[], source_refs=[]) for i, role in enumerate(['opening', 'closing'])]
            work.update(format='video_script', content_type='video_script', cover_text='隔离文字', shots=shots, target_duration_seconds=60, body='\n\n'.join(shot['spoken_text'] for shot in shots))
        works.append(work)
    version = dict(id='vtest', topic_id='ttest', number=1, payload=dict(strategy='隔离功能测试', works=works), hash='test', created_at='2026-11-01T00:00:00Z', provider='local:test', review=None, sync=None)
    topic = dict(id='ttest', title='隔离界面功能验证', brief='仅验证界面，不是创作案例。', platforms=PLATFORMS, sources=[], revision=1, status='approved', content_line='A', stance_ids=[], risk_confirmation=None, required_risk_confirmations=[], created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:00:00Z', current_version=version, versions=[version], jobs=[])
    copies = {work['platform']: {key: deepcopy(work[key]) for key in ('title', 'body', 'tags', 'source_urls', 'cover_text', 'shots', 'target_duration_seconds') if key in work} for work in works}
    for copy in copies.values():
        copy.update(revision=0, updated_at=None, stale=False, body_fingerprint=hashlib.sha256(copy['body'].encode()).hexdigest())
    flags = dict(authenticated=False, conflict=False)
    calls, errors, passed = [], [], []

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw): super().__init__(*a, directory=str(ROOT / 'dist'), **kw)
        def log_message(self, *_): pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def mock(route):
        request = route.request
        path = urlparse(request.url).path.removeprefix('/api')
        calls.append((request.method, path))
        data, code = {}, 200
        entries = [dict(platform=w['platform'], work=w, assets=[], draft=dict(id='d-' + w['platform'], state='reviewed'), review=dict(approved=True, fingerprint='fp-' + w['platform'], fingerprint_valid=True, blocking=[], latest=dict(account_label='隔离账号', ai_disclosure='AI 辅助创作'))) for w in works]
        if path == '/auth/status': data = dict(configured=True, authenticated=flags['authenticated'])
        elif path == '/auth/login': flags['authenticated'] = True; data = dict(authenticated=True)
        elif path == '/bootstrap': data = dict(token='test-only-token', settings=dict(codex_path='', local_base_url='http://127.0.0.1:1234', local_model='test', task_timeout_seconds=300, vault_path='isolated'), health=dict(codex=dict(available=False, authenticated=False), local=dict(available=False, models=[]), cloud=dict(enabled=False), checked_at='2026-11-01'), overview={})
        elif path == '/state': data = dict(topics=[topic], jobs=[], publications=[], drafts=[], notifications=[], activity=[], overview={})
        elif path == '/runtime-mode': data = dict(mode='local_content_only', mode_label='本地内容模式', platform_operations=False, note='隔离验证')
        elif path == '/trash': data = dict(total=0)
        elif path.endswith('/layout-audit'): data = dict(results=[], worst='none')
        elif path.endswith('/formats'): data = dict(platforms={})
        elif path.endswith('/review'): data = dict(version_id='vtest', fact_base=dict(revision=1), gaps=[], platforms=entries)
        elif path.endswith('/publish-preparation'): data = dict(version_id='vtest', fact_base_revision=1, ready=True, blocking=[], platforms=entries)
        elif path.endswith('/material-packages'): data = dict(items=[])
        elif '/studio-draft/' in path:
            platform = path.split('/studio-draft/')[1].split('/')[0]
            if request.method == 'PUT':
                if flags['conflict']: code, data = 409, dict(detail='隔离冲突：修订已经变化')
                else:
                    revision = copies[platform]['revision'] + 1
                    copies[platform].update({k: deepcopy(v) for k, v in request.post_data_json.items() if k != 'expected_revision'})
                    copies[platform].update(revision=revision, updated_at='2026-11-01T01:00:00Z')
                    data = copies[platform]
            else: data = copies[platform]
        elif path.endswith('/fact-base'): data = dict(fact_base=dict(revision=1, status='locked', audience='隔离', core_view='验证', sources=[], facts=[], open_questions=[], author_experience='', private_info='', asset_list=[], platform_angles={}), gaps=[])
        elif path == '/auth/sessions': data = dict(sessions=[])
        route.fulfill(status=code, content_type='application/json', body=json.dumps(data, ensure_ascii=False))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport=dict(width=1440, height=1000), service_workers='block')
            context.route('**/api/**', mock)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(error.stack or str(error)))
            page.goto(f'http://127.0.0.1:{args.port}/?page=projects&topic=ttest&version=vtest&platform=weixin_channels')
            page.get_by_role('heading', name='登录运营工作台').wait_for()
            assert not any(path == '/bootstrap' for _, path in calls)
            page.get_by_label('工作台密码').fill('isolated-test-password')
            page.get_by_role('button', name='登录', exact=True).click()
            page.get_by_role('dialog').wait_for(state='hidden')
            try:
                page.locator('.video-shot-card').first.wait_for(timeout=10000)
            except Exception:
                print(json.dumps(dict(errors=errors, text=page.locator('body').inner_text(), calls=calls), ensure_ascii=False), flush=True)
                page.screenshot(path=str(args.output / 'failure.png'), full_page=True)
                raise
            assert page.locator('.studio-platforms [role="tab"]').count() == 6, json.dumps(dict(errors=errors, text=page.locator('body').inner_text()), ensure_ascii=False)
            assert page.get_by_role('button', name='构建材料包', exact=True).count() == 0
            assert page.get_by_text('WorkBuddy 人工交接生成', exact=True).count() == 0
            passed.append('desktop_login_six_platforms_and_retired_actions')
            first = page.locator('.video-shot-card').first.locator('textarea').first
            first.fill('隔离修改口播，不作为创作内容。')
            page.wait_for_function("document.body.innerText.includes('工作稿已保存')")
            assert copies['weixin_channels']['shots'][0]['spoken_text'] == '隔离修改口播，不作为创作内容。'
            assert copies['weixin_channels']['body'].startswith('隔离修改口播')
            passed.append('structured_autosave_shots_and_derived_body')
            page.get_by_role('button', name='添加内容镜头', exact=True).click()
            assert page.locator('.video-shot-card').count() == 3
            page.get_by_role('button', name='镜头 2 上移', exact=True).click()
            assert page.locator('.video-shot-card').first.inner_text().startswith('镜头 1 · 开场钩子')
            page.wait_for_timeout(1600)
            flags['conflict'] = True
            first = page.locator('.video-shot-card').first.locator('textarea').first
            first.fill('隔离冲突保留文本')
            page.get_by_role('alert').filter(has_text='隔离冲突').wait_for()
            assert first.input_value() == '隔离冲突保留文本'
            passed.append('shot_reorder_and_conflict_preserves_input')
            page.screenshot(path=str(args.output / 'desktop-video-editor.png'), full_page=True)
            page.set_viewport_size(dict(width=390, height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.get_by_role('button', name='预览与审阅', exact=True).click()
            page.locator('.studio-platform-stage .video-script-preview').wait_for(state='visible')
            page.screenshot(path=str(args.output / 'iphone-viewport-video-preview.png'), full_page=True)
            passed.append('iphone_viewport_no_overflow_and_script_preview')
            page.evaluate("window.dispatchEvent(new Event('workbench:auth-required'))")
            page.get_by_role('dialog').wait_for(state='visible')
            page.get_by_label('工作台密码').fill('isolated-test-password')
            page.get_by_role('button', name='登录', exact=True).click()
            page.get_by_role('dialog').wait_for(state='hidden')
            page.get_by_role('button', name='正文编辑', exact=True).click()
            assert page.locator('.video-shot-card').first.locator('textarea').first.input_value() == '隔离冲突保留文本'
            passed.append('relogin_preserves_unsaved_editor')
            assert not errors, errors
            context.close(); browser.close()
        result = dict(kind='mocked_frontend_not_backend_or_physical_iphone', passed=passed, page_errors=errors, api_calls=len(calls))
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        server.shutdown(); server.server_close()


if __name__ == '__main__': main()
