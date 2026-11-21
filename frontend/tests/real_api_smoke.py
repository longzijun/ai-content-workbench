"""Real auth/HTTP/SQLite frontend smoke in a new isolated runtime; no Worker or model inference."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18770)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='real-api-', dir=args.output))
    os.environ['WORKBENCH_RUNTIME_DIR'] = str(work / 'runtime')
    os.environ['WORKBENCH_VAULT_PATH'] = str(work / 'vault')
    os.environ['WORKBENCH_PROCESS_ROLE'] = 'web'
    from backend.app import access, adapters, config, content_studio, db, fact_base, platform_registry, service
    config.discover_codex = lambda: ''
    db.init_db()
    password = secrets.token_urlsafe(24)
    access.setup_password(password)
    from backend.tests.test_video_c1 import SOURCE, freeze, script, text_work, topic
    value = topic(platform_registry.PLATFORM_IDS)
    # Explicitly label this fixture. All content stays inside the new isolated runtime.
    with db.transaction() as connection:
        connection.execute('UPDATE topics SET title=? WHERE id=?', ('隔离双端功能验证（非运营内容）', value['id']))
    fact_base.save(value['id'], dict(audience='隔离界面验证', core_view='检查保存和恢复行为', sources=[SOURCE], facts=[dict(claim=SOURCE['note'], status='confirmed', source_url=SOURCE['url'])]))
    fact_base.mark_ready(value['id'])
    version = freeze(value, dict(strategy='隔离功能测试，不是创作案例', works=[script(p) if platform_registry.is_video_platform(p) else text_work(p) for p in platform_registry.PLATFORM_IDS]))
    # Health is unrelated to this smoke and must not contact an actual model service.
    adapters.get_health = lambda settings: dict(codex=dict(available=False, authenticated=False), local=dict(available=False, models=[], error='隔离验证未连接模型'), cloud=dict(enabled=False), checked_at=service.now())
    from backend.app.main import app
    import httpx
    import uvicorn
    from playwright.sync_api import sync_playwright
    listener = socket.socket()
    listener.bind(('127.0.0.1', args.port))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{args.port}'
    passed, errors, failed_requests = [], [], []
    try:
        for _ in range(100):
            if server.started: break
            if not thread.is_alive(): raise RuntimeError('隔离服务启动失败')
            time.sleep(.05)
        assert server.started
        with httpx.Client(base_url=base) as client:
            assert client.get('/api/bootstrap').status_code == 401
            assert client.post('/api/auth/login', json=dict(password=password, device_name='隔离接口检查')).status_code == 200
            prep = client.get(f'/api/topics/{value["id"]}/publish-preparation')
            assert prep.status_code == 200, prep.text
            assert not prep.json()['ready'] and prep.json()['blocking']
            assert 'json' in client.get('/manifest.webmanifest').headers['content-type']
            assert 'javascript' in client.get('/service-worker.js').headers['content-type']
            assert client.get('/app-icon-180.png').headers['content-type'].startswith('image/png')
            passed.extend(['anonymous_bootstrap_denied', 'unreviewed_preparation_blocked', 'pwa_static_mime_types'])
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport=dict(width=1440, height=1000), service_workers='block')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(error.stack or str(error)))
            page.on('response', lambda response: failed_requests.append(dict(status=response.status, path=response.url.split('/api')[-1])) if response.status >= 400 and '/api/' in response.url and '/bootstrap' not in response.url else None)
            url = f'{base}/?page=projects&topic={value["id"]}&version={version["id"]}&platform=weixin_channels'
            page.goto(url)
            page.get_by_role('heading', name='登录运营工作台').wait_for()
            page.get_by_label('工作台密码').fill(password)
            page.get_by_role('button', name='登录', exact=True).click()
            page.get_by_role('dialog').wait_for(state='hidden')
            page.locator('.video-shot-card').first.wait_for()
            assert page.locator('.studio-platforms [role="tab"]').count() == 6
            first = page.locator('.video-shot-card').first.locator('textarea').first
            changed = '隔离保存恢复验证文字。'
            first.fill(changed)
            page.wait_for_function("document.body.innerText.includes('工作稿已保存')")
            saved = content_studio.read_copy(value['id'], version['id'], 'weixin_channels')
            assert saved['shots'][0]['spoken_text'] == changed
            assert saved['body'].startswith(changed)
            page.reload()
            page.locator('.video-shot-card').first.wait_for()
            assert page.locator('.video-shot-card').first.locator('textarea').first.input_value() == changed
            passed.extend(['real_login_six_platforms', 'real_structured_save_and_refresh_restore'])
            page.screenshot(path=str(args.output / 'real-desktop-script.png'), full_page=True)
            page.set_viewport_size(dict(width=390, height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='预览与审阅', exact=True).click()
            page.locator('.studio-platform-stage .video-script-preview').wait_for(state='visible')
            page.screenshot(path=str(args.output / 'real-iphone-viewport-script.png'), full_page=True)
            passed.append('real_api_iphone_viewport_no_overflow')
            assert not errors, errors
            assert not failed_requests, failed_requests
            context.close(); browser.close()
        result = dict(kind='real_backend_auth_sqlite_edge_viewport_not_physical_iphone', passed=passed, page_errors=errors, failed_api_requests=failed_requests, runtime=str(work), model_inference=False, worker_started=False)
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        if thread.is_alive(): raise RuntimeError('隔离服务未按时退出')


if __name__ == '__main__': main()
