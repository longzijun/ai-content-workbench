"""Real API/SQLite/Edge checks in an isolated runtime with explicit fixture content.

This script never calls a model, a platform or a user profile. Fixture text is
published through the same candidate persistence API used by the local Worker.
It verifies the workflow, not real model writing quality or a real phone.
"""
from pathlib import Path
import json
import os
import shutil
import socket
import sys
import threading
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get('WORKBENCH_CANDIDATE_UI_DIR', str(PROJECT / 'runtime' / ('candidate-ui-' + uuid4().hex[:8]))))
RUN.mkdir(parents=True, exist_ok=False)
os.environ['WORKBENCH_RUNTIME_DIR'] = str(RUN / 'data')
os.environ['WORKBENCH_VAULT_PATH'] = str(RUN / 'vault')
sys.path.insert(0, str(PROJECT))

from backend.app import access, adapters, browser_bridge, candidate_drafts, config, db, draft_workflow, main, scheduler, service, trends, worker
from playwright.sync_api import sync_playwright, expect
import uvicorn


def verify():
    build = Path(os.environ.get('WORKBENCH_CANDIDATE_BUILD_DIR', str(PROJECT / 'frontend' / 'dist')))
    assert (build / 'index.html').is_file(), 'Build frontend first'
    preview = RUN / 'preview'
    shutil.copytree(build, preview / 'frontend' / 'dist')
    main.PROJECT_ROOT = preview
    config.discover_codex = lambda: ''
    adapters.get_health = lambda *a, **k: {'local': {'available': False, 'models': [], 'loaded_model': ''}, 'codex': {'available': False, 'authenticated': False}, 'cloud': {'enabled': False}, 'checked_at': service.now()}
    for module, start, stop in [(worker, 'start_worker', 'stop_worker'), (trends, 'start_monitor', 'stop_monitor'), (scheduler, 'start', 'stop')]:
        setattr(module, start, lambda: None)
        setattr(module, stop, lambda: None)
    browser_bridge.stop_browser = lambda: None
    db.init_db()
    keepalive = db.connect()
    keepalive.execute('SELECT 1 FROM topics LIMIT 1').fetchone()
    topic = service.create_topic({'title': '隔离验收｜草稿先保存再修改', 'brief': '这是明确标注的软件验收资料：工作稿用于继续编辑，正式版本用于审阅。', 'platforms': ['wechat', 'toutiao'], 'content_line': 'A', 'sources': []})
    access.setup_password('候选稿隔离验收123456')
    session = access.login('候选稿隔离验收123456', '隔离Edge', 'owner-login')['id']
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(128)
    base = f'http://127.0.0.1:{listener.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(main.app, log_level='warning', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.05)
    assert server.started
    screenshots = RUN / 'evidence'; screenshots.mkdir()
    evidence = {'runtime': str(RUN), 'fixture_model': True, 'fixture_note': 'Content is explicit software-test fixture; no inference or platform delivery is exercised.', 'checks': [], 'screenshots': [], 'console_errors': [], 'http_5xx': [], 'external_requests': []}
    def check(name): evidence['checks'].append(name)
    def material(): return {'mode': 'evidenced', 'author_text': topic['brief'], 'author_experience': '', 'facts': [], 'documents': [], 'evidence_urls': [], 'issues': [], 'audience': '软件验收作者', 'suggested_angle': '验证资料、工作稿与正式版本分别保存'}
    def publish(snapshot, platform, title, body, state='draft', issues=None):
        return candidate_drafts.publish({**snapshot, '_auto_materials': material()}, platform, {'strategy': '明确标注的软件测试替身内容，不是真实模型生成', 'works': [{'platform': platform, 'format': 'article', 'title': title, 'body': body, 'tags': [], 'source_urls': [], 'image_prompts': []}]}, raw='explicit-ui-fixture', issues=issues, state=state)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width': 1550, 'height': 1050}, accept_downloads=True)
            context.add_cookies([{'name': access.COOKIE, 'value': session, 'url': base}])
            page = context.new_page()
            page.on('pageerror', lambda error: evidence['console_errors'].append(str(error)))
            page.on('response', lambda response: evidence['http_5xx'].append({'url': response.url, 'status': response.status}) if response.status >= 500 else None)
            def route(request_route):
                if request_route.request.url.startswith(base + '/'): request_route.continue_()
                else: evidence['external_requests'].append(request_route.request.url); request_route.abort()
            page.route('**/*', route)
            page.goto(base + '/?page=topics&topic=' + topic['id'], wait_until='networkidle')
            page.get_by_role('textbox', name='选题标题', exact=True).fill('隔离验收｜当前修改随批准保存')
            page.get_by_role('button', name='批准并创作', exact=True).click()
            expect(page.locator('.candidate-heading')).to_be_visible(timeout=10000)
            with db.connect() as connection:
                assert not connection.execute('SELECT 1 FROM fact_bases WHERE topic_id=?', (topic['id'],)).fetchone()
                jobs = connection.execute('SELECT * FROM jobs WHERE topic_id=?', (topic['id'],)).fetchall()
                assert len(jobs) == 1
            assert service.get_topic(topic['id'])['title'] == '隔离验收｜当前修改随批准保存'
            check('无事实底稿、无来源：批准创作自动保存当前选择并入队进入工坊')
            job = service.claim_next_job()
            assert job['topic_id'] == topic['id']
            snapshot = job['topic']
            body = '这是明确标记的软件验收正文。\n\n工作稿先保存，作者可以继续编辑。\n\n正式版本冻结后，再进入人工审阅。'
            long_title = '这是一个用于验证标题超过限制时正文仍然完整保存并且可以继续编辑的软件验收标题'
            wc = publish(snapshot, 'wechat', long_title, body, 'needs_changes', ['公众号标题需为 1 到 32 字，请修改版本'])
            tc = publish(snapshot, 'toutiao', '头条待继续', '', 'failed', ['测试替身：头条连接暂时中断'])
            candidate_drafts.finish_job(job['id'], 'test:explicit-ui-fixture')
            title = page.get_by_role('textbox', name='工作稿标题', exact=True)
            editor = page.get_by_role('textbox', name='工作稿正文', exact=True)
            expect(editor).to_have_value(body, timeout=12000)
            expect(title).to_have_value(long_title)
            expect(page.get_by_role('button', name='公众号标题需为 1 到 32 字，请修改版本')).to_be_visible()
            assert service.get_topic(topic['id'])['current_version'] is None
            check('没有正式版本也能编辑：长标题和全部正文保留，问题可定位')
            page.get_by_role('textbox', name='预览标题', exact=True).fill('先保存工作稿，再集中检查')
            revised = body + '\n\n这段由作者直接在预览中补充。'
            page.get_by_role('textbox', name='预览正文', exact=True).fill(revised)
            expect(editor).to_have_value(revised)
            expect(title).to_have_value('先保存工作稿，再集中检查')
            expect(page.locator('.candidate-editor > header')).to_contain_text('已保存', timeout=10000)
            assert candidate_drafts.read(topic['id'], wc['id'])['body'] == revised
            check('预览直接编辑与左侧同步，自动保存到真实SQLite')
            page.reload(wait_until='networkidle')
            expect(editor).to_have_value(revised, timeout=10000)
            check('刷新恢复已保存的候选内容和标题')
            sources = page.get_by_role('textbox', name='工作稿来源', exact=True)
            sources.fill('  https://example.invalid/optional  ')
            page.get_by_role('button', name='保存工作稿', exact=True).click()
            expect(page.locator('.candidate-editor > header')).to_contain_text('已保存', timeout=10000)
            assert candidate_drafts.read(topic['id'], wc['id'])['source_urls'] == ['https://example.invalid/optional']
            sources.fill('')
            page.get_by_role('button', name='保存工作稿', exact=True).click()
            expect(page.locator('.candidate-editor > header')).to_contain_text('已保存', timeout=10000)
            assert candidate_drafts.read(topic['id'], wc['id'])['source_urls'] == []
            check('来源自动去空白，删除后真实保存为空数组，不产生无效空链接')
            page.get_by_role('tab', name='今日头条').click()
            expect(editor).to_have_value('')
            page.get_by_role('tab', name='公众号').click()
            expect(editor).to_have_value(revised)
            check('头条失败时公众号稿仍独立可编辑，切平台不串稿')
            bootstrap = context.request.get(base + '/api/bootstrap').json()
            token = bootstrap['token']
            before = candidate_drafts.read(topic['id'], wc['id'])
            # Race before autosave: a real API request represents another device.
            local_text = revised + '\n\n本窗口保留的修改。'
            editor.fill(local_text)
            response = context.request.put(base + '/api/candidate-drafts/' + wc['id'], headers={'X-Workbench-Token': token}, data={k: before[k] for k in ('title', 'body', 'tags', 'source_urls')} | {'body': revised + '\n\n另一窗口写入。', 'expected_revision': before['revision']})
            assert response.status == 200, response.text()
            expect(page.get_by_text('发现另一窗口的新修订', exact=False)).to_be_visible(timeout=12000)
            expect(editor).to_have_value(local_text)
            page.get_by_role('button', name='以当前输入合并保存', exact=True).click()
            expect(page.locator('.candidate-editor > header')).to_contain_text('已保存', timeout=10000)
            assert candidate_drafts.read(topic['id'], wc['id'])['body'] == local_text
            check('真实API并发CAS冲突保留本地输入，显式合并后保存成功')
            page.get_by_role('tab', name='今日头条').click()
            title.fill('头条工作稿完成后再定稿')
            editor.fill('这是用于软件验收的头条工作稿。\n\n公众号与头条分别保留可编辑内容。\n\n完成后统一检查并保存正式版本。')
            expect(page.locator('.candidate-editor > header')).to_contain_text('已保存', timeout=10000)
            page.get_by_role('button', name='检查并定稿', exact=True).click()
            expect(page.get_by_role('button', name='正式版本与审阅', exact=True)).to_be_visible(timeout=12000)
            expect(page.locator('.content-studio')).to_be_visible(timeout=12000)
            assert service.get_topic(topic['id'])['current_version'] is not None
            check('修正后检查并定稿，创建真实正式版本并进入既有审阅界面')
            page.get_by_role('button', name='开始新一轮创作', exact=True).click()
            expect(page.get_by_role('button', name='可编辑工作稿', exact=True)).to_have_class(re.compile('primary'))
            page.get_by_role('tab', name='公众号').click()
            expect(editor).to_be_visible(timeout=10000)
            current_text = editor.input_value()
            dirty = current_text + '\n\n生成同时保留作者输入。'
            editor.fill(dirty)
            next_job = service.claim_next_job(); assert next_job
            publish(next_job['topic'], 'wechat', '后台新候选不会覆盖作者稿', '这是另一份明确标注的软件验收候选内容。')
            candidate_drafts.finish_job(next_job['id'], 'test:explicit-ui-fixture')
            page.wait_for_timeout(3500)
            expect(editor).to_have_value(dirty)
            assert page.get_by_role('combobox', name='选择工作稿').locator('option').count() >= 2
            check('已有正式版本仍可创作，后台新候选不替换当前选中稿及人工输入')
            selected_id = page.get_by_role('combobox', name='选择工作稿').input_value()
            page.reload(wait_until='networkidle')
            page.get_by_role('button', name='可编辑工作稿', exact=True).click()
            expect(editor).to_have_value(dirty, timeout=10000)
            expect(page.get_by_role('combobox', name='选择工作稿')).to_have_value(selected_id)
            check('新候选到达后刷新仍恢复作者选中稿，不跳到另一份模型输出')
            page.screenshot(path=str(screenshots / '01-desktop-candidate.png'), full_page=True)
            evidence['screenshots'].append('01-desktop-candidate.png')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(screenshots / '02-mobile-layout.png'), full_page=True)
            evidence['screenshots'].append('02-mobile-layout.png')
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')
            check('390像素模拟手机布局无横向溢出（不代表真实手机验收）')
            assert not evidence['console_errors'], evidence['console_errors']
            assert not evidence['http_5xx'], evidence['http_5xx']
            assert not evidence['external_requests'], evidence['external_requests']
            check('页面无JavaScript异常、无服务端5xx、无外部平台请求')
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        keepalive.close()
        (RUN / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'runtime': str(RUN), 'checks': len(evidence['checks']), 'result': str(RUN / 'result.json')}, ensure_ascii=False))


if __name__ == '__main__':
    import re
    verify()
