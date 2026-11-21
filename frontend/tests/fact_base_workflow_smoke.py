"""Real API + Edge checks for topic approval and fact-base production readiness.

Uses a fresh runtime/vault below --output and the isolated source's built dist.
No Worker, model inference, external source request, or platform operation runs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18874)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (PROJECT / 'frontend' / 'dist' / 'index.html').is_file():
        raise RuntimeError('先在隔离 source/frontend 构建 dist，再运行专项检查')
    work = Path(tempfile.mkdtemp(prefix='fact-base-workflow-', dir=output))
    os.environ['WORKBENCH_RUNTIME_DIR'] = str(work / 'runtime')
    os.environ['WORKBENCH_VAULT_PATH'] = str(work / 'vault')
    os.environ['WORKBENCH_PROCESS_ROLE'] = 'web'
    # A fresh runtime must not inherit the deployed ingress's host configuration.
    os.environ.pop('WORKBENCH_EXTERNAL_ORIGIN', None)
    from backend.app import access, adapters, config, db, service
    assert config.PROJECT_ROOT.resolve() == PROJECT
    config.discover_codex = lambda: ''
    db.init_db()
    settings = config.get_settings()
    assert settings.runtime_dir.resolve().is_relative_to(work)
    assert Path(settings.vault_path).resolve().is_relative_to(work)
    password = secrets.token_urlsafe(24)
    access.setup_password(password)
    adapters.get_health = lambda settings: dict(
        codex=dict(available=False, authenticated=False),
        local=dict(available=False, models=[], error='隔离验证不连接模型'),
        cloud=dict(enabled=False), checked_at=service.now())
    from backend.app.main import app
    import httpx
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    listener = socket.socket()
    listener.bind(('127.0.0.1', args.port))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    passed, page_errors, failed_requests, ui_mutations, blocked_external = [], [], [], [], []
    injected_failures = []
    base = f'http://127.0.0.1:{args.port}'
    thread.start()
    result = dict(kind='real_auth_api_sqlite_edge_desktop_and_mobile_viewport',
                  runtime=str(work / 'runtime'), vault=str(work / 'vault'),
                  source=str(PROJECT), port=args.port, model_inference=False,
                  worker_started=False, physical_iphone=False)
    try:
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError('隔离服务启动失败')
            time.sleep(.05)
        assert server.started
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
            assert client.get('/api/bootstrap').status_code == 401
            login = client.post('/api/auth/login', json=dict(password=password, device_name='隔离底稿接口检查'))
            assert login.status_code == 200
            token = client.get('/api/bootstrap').json()['token']
            headers = {'X-Workbench-Token': token}

            def get(path):
                response = client.get('/api' + path)
                assert response.status_code == 200, (path, response.status_code, response.text)
                return response.json()

            def post(path, body):
                response = client.post('/api' + path, headers=headers, json=body)
                assert response.status_code == 200, (path, response.status_code, response.text)
                return response.json()

            title = '隔离底稿工作流验证（非运营内容）'
            value = post('/topics', dict(title=title, brief='仅检查批准、保存与生产门禁。',
                                        platforms=['wechat', 'toutiao'], sources=[], content_line='B'))
            original = post('/topics', dict(title='隔离原创字段保留验证（非运营内容）',
                                           brief='本地隔离字段保留检查。', platforms=['wechat'],
                                           sources=[], content_line='B'))
            post(f'/topics/{original["id"]}/decision', dict(decision='approved', expected_revision=original['revision']))
            original_raw = '隔离测试原始输入。保存编辑时必须保留这段原文。\n第二行保持不变。'
            original_fact = dict(material_type='original', raw_text=original_raw,
                                 audience='隔离字段验证者', core_view='检查原创材料类型持久保存',
                                 sources=[], facts=[dict(claim='这是作者提供的隔离测试输入。', status='confirmed')])
            post(f'/topics/{original["id"]}/fact-base', original_fact)

            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport=dict(width=1800, height=1000), service_workers='block')

                def local_only(route):
                    parsed = urlsplit(route.request.url)
                    if parsed.scheme in ('http', 'https') and parsed.hostname != '127.0.0.1':
                        blocked_external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', local_only)
                # Reproduce a persisted hidden desktop materials column once.
                context.add_init_script("""if (!sessionStorage.getItem('fact-smoke-layout')) {
                  localStorage.setItem('workbench.studio.layout', JSON.stringify({left:250,right:350,hideLeft:true,hideRight:false}));
                  sessionStorage.setItem('fact-smoke-layout', '1');
                }""")
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on('pageerror', lambda error: page_errors.append(error.stack or str(error)))
                page.on('request', lambda request: ui_mutations.append(dict(method=request.method, path=urlsplit(request.url).path))
                        if request.method != 'GET' and '/api/' in request.url else None)
                page.on('response', lambda response: failed_requests.append(dict(status=response.status, path=urlsplit(response.url).path))
                        if response.status >= 400 and '/api/' in response.url and '/bootstrap' not in response.url else None)
                page.goto(f'{base}/?page=topics&topic={value["id"]}')
                page.get_by_role('heading', name='登录运营工作台').wait_for()
                page.locator('input[type="password"]').fill(password)
                page.get_by_role('button', name='登录', exact=True).click()
                page.get_by_role('dialog').wait_for(state='hidden')

                # Approval changes only the topic decision and opens the studio.
                approve = page.get_by_role('button', name='批准并进入内容工坊', exact=True)
                expect(approve).to_be_enabled()
                approve.click()
                page.locator('.content-studio').wait_for()
                expect(page.locator('.studio-heading h2')).to_have_text(title)
                assert get(f'/topics/{value["id"]}')['status'] == 'approved'
                assert get(f'/topics/{value["id"]}')['jobs'] == []
                assert not any(item['path'].endswith('/generate') or item['path'].endswith('/produce') for item in ui_mutations)
                passed.append('approval_opens_studio_without_creating_job_or_legacy_generate')

                start = page.get_by_role('button', name='开始生产', exact=True)
                expect(start).to_be_disabled()
                supplement = page.get_by_role('button', name='补齐并确认资料', exact=True)
                expect(supplement).to_be_visible()
                expect(page.locator('.studio-no-work')).to_contain_text('尚未建立共享事实底稿')
                expect(page.locator('.studio-desk')).to_have_class(re.compile(r'\bhide-left\b'))
                supplement.click()
                expect(page.locator('.studio-desk')).not_to_have_class(re.compile(r'\bhide-left\b'))
                expect(page.locator('.studio-materials')).to_be_visible()
                editor_box = page.locator('.studio-editor-column').bounding_box()
                preview_box = page.locator('.studio-preview-column').bounding_box()
                assert editor_box and preview_box
                assert editor_box['width'] >= 340, editor_box
                assert preview_box['width'] >= 300, preview_box
                assert abs(editor_box['y'] - preview_box['y']) <= 2, (editor_box, preview_box)
                assert preview_box['x'] >= editor_box['x'] + editor_box['width'], (editor_box, preview_box)
                passed.append('desktop_1800_editor_and_preview_have_readable_width_and_share_row')
                page.screenshot(path=str(output / 'fact-base-desktop-gaps.png'), full_page=True)

                page.set_viewport_size(dict(width=1440, height=1000))
                page.get_by_role('button', name='收起资料', exact=True).click()
                expect(page.locator('.studio-materials')).to_be_hidden()
                editor_box = page.locator('.studio-editor-column').bounding_box()
                preview_box = page.locator('.studio-preview-column').bounding_box()
                assert editor_box and preview_box
                assert editor_box['width'] >= 310, editor_box
                assert preview_box['width'] >= 300, preview_box
                assert abs(editor_box['y'] - preview_box['y']) <= 2, (editor_box, preview_box)
                assert preview_box['x'] >= editor_box['x'] + editor_box['width'], (editor_box, preview_box)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'fact-base-desktop-1440-gaps.png'), full_page=True)
                passed.append('desktop_1440_drawer_closed_editor_and_preview_share_readable_row')

                page.set_viewport_size(dict(width=390, height=844))
                supplement.click()
                expect(page.locator('.studio-materials')).to_be_visible()
                page.get_by_role('button', name='收起资料', exact=True).click()
                expect(page.locator('.studio-materials')).to_be_hidden()
                supplement.click()
                expect(page.locator('.studio-materials')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'fact-base-mobile-materials.png'), full_page=True)
                passed.extend(['gaps_block_production_and_reveal_hidden_desktop_materials',
                               'mobile_viewport_materials_reveal_and_no_overflow'])
                page.set_viewport_size(dict(width=1800, height=1000))

                fact_panel = page.locator('.fact-editor')
                fact_panel.get_by_role('textbox', name='写给谁，解决什么问题', exact=True).fill('只验证本地工作台流程的测试人员')
                fact_panel.get_by_role('textbox', name='我的判断与核心观点', exact=True).fill('先确认资料，再生成平台作品。')
                fact_panel.get_by_role('button', name='添加事实', exact=True).click()
                fact_panel.get_by_role('textbox', name='事实内容', exact=True).fill('隔离测试提供了这条可核对的输入。')
                source_url = 'https://example.invalid/fact-base-smoke-source'
                fact_panel.get_by_role('textbox', name='来源网址', exact=True).fill(source_url)
                expect(fact_panel.get_by_role('combobox', name='确认状态', exact=True)).to_have_value('unverified')
                fact_path = f'/api/topics/{value["id"]}/fact-base'
                fact_route = f'**{fact_path}'

                def reject_save(route):
                    if route.request.method == 'POST':
                        injected_failures.append(dict(status=503, path=fact_path))
                        route.fulfill(status=503, json=dict(detail='隔离注入：资料保存暂时失败'))
                    else:
                        route.continue_()

                page.route(fact_route, reject_save)
                fact_panel.get_by_role('button', name='保存资料', exact=True).click()
                expect(fact_panel).to_contain_text('隔离注入：资料保存暂时失败')
                expect(fact_panel.get_by_role('textbox', name='事实内容', exact=True)).to_have_value('隔离测试提供了这条可核对的输入。')
                expect(fact_panel.get_by_role('textbox', name='来源网址', exact=True)).to_have_value(source_url)
                expect(fact_panel.get_by_role('textbox', name='写给谁，解决什么问题', exact=True)).to_have_value('只验证本地工作台流程的测试人员')
                expect(start).to_be_disabled()
                assert get(f'/topics/{value["id"]}/fact-base')['fact_base'] is None
                page.unroute(fact_route, reject_save)
                passed.append('failed_save_preserves_unsaved_form_and_keeps_production_blocked')
                with page.expect_response(lambda r: urlsplit(r.url).path == f'/api/topics/{value["id"]}/fact-base' and r.request.method == 'POST'):
                    fact_panel.get_by_role('button', name='保存资料', exact=True).click()
                saved = get(f'/topics/{value["id"]}/fact-base')
                assert saved['fact_base']['facts'][0]['status'] == 'unverified'
                assert any(item['url'] == source_url for item in saved['fact_base']['sources'])
                assert saved['gaps'] and saved['fact_base']['status'] == 'draft'
                expect(start).to_be_disabled()
                expect(fact_panel.get_by_role('button', name='确认底稿', exact=True)).to_be_disabled()
                passed.append('fact_source_url_is_saved_as_source_without_automatic_fact_confirmation')

                # Only the user's explicit confirmation removes this gap.
                fact_panel.get_by_role('combobox', name='确认状态', exact=True).select_option('confirmed')
                with page.expect_response(lambda r: urlsplit(r.url).path == f'/api/topics/{value["id"]}/fact-base' and r.request.method == 'POST'):
                    fact_panel.get_by_role('button', name='保存资料', exact=True).click()
                saved = get(f'/topics/{value["id"]}/fact-base')
                assert saved['gaps'] == [] and saved['fact_base']['status'] == 'draft'
                expect(start).to_be_disabled()
                ready = fact_panel.get_by_role('button', name='确认底稿', exact=True)
                expect(ready).to_be_enabled()

                def reject_preflight_read(route):
                    if route.request.method == 'GET':
                        injected_failures.append(dict(status=503, path=fact_path))
                        route.fulfill(status=503, json=dict(detail='隔离注入：底稿读取暂时失败'))
                    else:
                        route.continue_()

                page.route(fact_route, reject_preflight_read)
                ready.click()
                expect(page.locator('.studio-no-work')).to_contain_text('资料检查失败：隔离注入：底稿读取暂时失败')
                expect(start).to_be_disabled()
                assert get(f'/topics/{value["id"]}/fact-base')['fact_base']['status'] == 'ready'
                page.unroute(fact_route, reject_preflight_read)
                page.get_by_role('button', name='重新检查', exact=True).click()
                expect(start).to_be_enabled()
                passed.append('preflight_read_failure_blocks_ready_base_and_retry_recovers')
                passed.append('saved_complete_draft_still_blocks_until_explicit_ready_confirmation')

                # No Worker is started: the single task remains queued for inspection.
                start.click()
                expect(page.locator('.studio-job')).to_be_visible()
                expect(page.locator('.studio-job')).to_contain_text('正在生产内容')
                expect(start).to_be_disabled()
                queued = get(f'/topics/{value["id"]}')['jobs']
                assert len(queued) == 1 and queued[0]['state'] == 'queued'
                with db.connect() as connection:
                    row = connection.execute('SELECT input_snapshot FROM jobs WHERE id=?', (queued[0]['id'],)).fetchone()
                    snapshot = json.loads(row['input_snapshot'])
                    assert snapshot['_fact_base'] and '先确认资料，再生成平台作品。' in snapshot['_fact_base']
                    assert snapshot['platforms'] == ['wechat', 'toutiao']
                    assert connection.execute('SELECT COUNT(*) FROM delivery_tasks').fetchone()[0] == 0
                assert get(f'/topics/{value["id"]}/fact-base')['fact_base']['status'] == 'locked'
                assert sum(item['path'] == f'/api/topics/{value["id"]}/produce' for item in ui_mutations) == 1
                page.screenshot(path=str(output / 'fact-base-queued-progress.png'), full_page=True)
                passed.append('single_queued_job_contains_locked_fact_snapshot_and_visible_progress')

                # An original quick-draft record must survive ordinary studio saves.
                page.goto(f'{base}/?page=projects&topic={original["id"]}')
                fact_panel = page.locator('.fact-editor')
                material = fact_panel.get_by_role('combobox', name='材料类型', exact=True)
                expect(material).to_have_value('original')
                fact_panel.get_by_role('textbox', name='我的判断与核心观点', exact=True).fill('编辑后仍保留原创类型与原始输入。')
                with page.expect_response(lambda r: urlsplit(r.url).path == f'/api/topics/{original["id"]}/fact-base' and r.request.method == 'POST'):
                    fact_panel.get_by_role('button', name='保存资料', exact=True).click()
                saved_original = get(f'/topics/{original["id"]}/fact-base')
                assert saved_original['fact_base']['material_type'] == 'original'
                assert saved_original['fact_base']['raw_text'] == original_raw
                assert saved_original['gaps'] == []
                page.reload()
                expect(page.locator('.fact-editor').get_by_role('combobox', name='材料类型', exact=True)).to_have_value('original')
                assert get(f'/topics/{original["id"]}/fact-base')['fact_base']['raw_text'] == original_raw
                expect(page.get_by_role('button', name='开始生产', exact=True)).to_be_disabled()
                passed.append('original_material_type_and_raw_text_survive_studio_save_and_refresh')
                assert not page_errors, page_errors
                assert failed_requests == injected_failures, (failed_requests, injected_failures)
                assert not blocked_external, blocked_external
                context.close()
                browser.close()
        result['status'] = 'passed'
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        result.update(passed=passed, page_errors=page_errors,
                      failed_api_requests=failed_requests, blocked_external_requests=blocked_external,
                      intentionally_injected_failures=injected_failures, ui_mutations=ui_mutations)
        (output / 'fact-base-workflow-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        # No credentials, cookies or write tokens are included in this output.
        print(json.dumps(result, ensure_ascii=False))
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        if thread.is_alive():
            raise RuntimeError('隔离服务未按时退出')


if __name__ == '__main__':
    main()
