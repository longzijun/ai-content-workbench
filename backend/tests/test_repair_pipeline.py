import asyncio
from copy import deepcopy
import json
import socket

import httpx
import pytest
from fastapi import HTTPException

from backend.app import adapters, config, db, delivery, heartbeat, local_generation, research, service


def payload(platform='wechat'):
    return {'strategy': '依据资料写作', 'works': [{'platform': platform, 'format': 'note' if platform == 'xiaohongshu' else 'article',
        'title': '如何核对信息来源', 'body': '先阅读原文并核对日期，再判断是否适用于自己。',
        'tags': [], 'image_prompts': ['中性示意图'], 'source_urls': []}]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    monkeypatch.setattr(adapters, '_loaded_local_model_info', lambda _: {})
    delivery._stop.clear()
    db.init_db()
    return config.get_settings()


def frozen():
    topic = service.create_topic({'title': '核对资料', 'brief': '', 'platforms': ['wechat'], 'sources': [], 'content_line': 'A', 'stance_ids': []})
    service.decide_topic(topic['id'], {'decision': 'approved', 'expected_revision': 1})
    job = service.generate(topic['id'], 1)
    service.claim_next_job()
    version = service.complete_job(job['id'], payload(), 'local:test', defer_delivery=True)
    draft = next(d for d in service.state()['drafts'] if d['version_id'] == version['id'])
    return topic, job, version, draft


def test_retry_reuses_completed_platform_but_new_revision_does_not(world, monkeypatch):
    topic = {'id': 't', 'revision': 1, '_generation_run': 'run1', 'platforms': ['wechat', 'toutiao'], 'sources': []}
    calls = []
    fail = True

    async def stream(settings, request, *args):
        nonlocal fail
        platform = 'wechat' if '"platforms": ["wechat"]' in request['messages'][0]['content'] else 'toutiao'
        calls.append(platform)
        if platform == 'toutiao' and fail:
            raise TimeoutError('interrupted')
        return json.dumps(payload(platform))

    monkeypatch.setattr(local_generation, 'stream_payload', stream)
    with pytest.raises(TimeoutError):
        local_generation.generate_platforms(topic, world, 'test', lambda: False, lambda *a: None)
    fail = False
    result, _ = local_generation.generate_platforms(topic, world, 'test', lambda: False, lambda *a: None)
    assert calls == ['wechat', 'toutiao', 'toutiao']
    assert len(result['works']) == 2
    topic['revision'] = 2
    local_generation.generate_platforms(topic, world, 'test', lambda: False, lambda *a: None)
    assert calls[-2:] == ['wechat', 'toutiao']


def test_retry_job_keeps_run_key_new_generation_changes_it(world):
    topic = service.create_topic({'title': '来源', 'brief': '', 'platforms': ['wechat'], 'sources': [], 'content_line': 'A', 'stance_ids': []})
    service.decide_topic(topic['id'], {'decision': 'approved', 'expected_revision': 1})
    first = service.generate(topic['id'], 1)
    service.claim_next_job()
    service.fail_job(first['id'], 'timeout')
    retry = service.retry_job(first['id'])
    with db.connect() as c:
        records = [json.loads(r[0])['_generation_run'] for r in c.execute('SELECT input_snapshot FROM jobs ORDER BY created_at')]
    assert records == [first['id'], first['id']]
    service.cancel_job(retry['id'])
    new = service.generate(topic['id'], 1)
    with db.connect() as c:
        assert json.loads(c.execute('SELECT input_snapshot FROM jobs WHERE id=?', (new['id'],)).fetchone()[0])['_generation_run'] == new['id']


def test_title_only_repair_preserves_body_and_sources(world, monkeypatch):
    original = payload('xiaohongshu')
    original['works'][0]['title'] = '长' * 24
    calls = []

    async def stream(settings, request, *args):
        calls.append(request)
        return json.dumps(original if len(calls) == 1 else {'title': '核对来源再判断'})

    monkeypatch.setattr(local_generation, 'stream_payload', stream)
    result, _ = local_generation.generate_platforms({'id': 't', 'platforms': ['xiaohongshu'], 'sources': []}, world, 'test', lambda: False, lambda *a: None)
    assert len(calls) == 2
    assert result['works'][0]['title'] == '核对来源再判断'
    assert result['works'][0]['body'] == original['works'][0]['body']
    assert result['works'][0]['source_urls'] == original['works'][0]['source_urls']


def mock_stream(monkeypatch, events):
    original = httpx.AsyncClient
    data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)), **kw))


def test_truncated_response_is_not_accepted(world, monkeypatch):
    mock_stream(monkeypatch, [{'choices': [{'delta': {'content': json.dumps(payload())}, 'finish_reason': 'length'}]}])
    with pytest.raises(ValueError, match='截断'):
        asyncio.run(local_generation.stream_payload(world, {'model': 'test'}, lambda: False, lambda *a: None, world.runtime_dir / 'diag.json'))


def test_native_stream_reports_usage_without_storing_reasoning(world, monkeypatch):
    mock_stream(monkeypatch, [{'type': 'reasoning.delta', 'content': 'private reasoning'}, {'type': 'message.delta', 'content': '{}'},
                             {'type': 'chat.end', 'result': {'stats': {'total_output_tokens': 12}, 'output': []}}])
    path = world.runtime_dir / 'diag.json'
    result = asyncio.run(local_generation.stream_payload(world, {'model': 'test', 'input': 'test', 'max_output_tokens': 100}, lambda: False, lambda *a: None, path))
    assert result == '{}'
    assert 'private reasoning' not in path.read_text('utf-8')
    assert json.loads(path.read_text('utf-8'))['stats']['reasoning_chars'] == 17


def test_deadline_covers_waiting_for_headers(world, monkeypatch):
    original = httpx.AsyncClient
    closed = []

    async def wait(request):
        try:
            await asyncio.sleep(20)
        finally:
            closed.append(True)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(wait), **kw))
    world.task_timeout_seconds = 0.02
    with pytest.raises(TimeoutError):
        asyncio.run(local_generation.stream_payload(world, {'model': 'test'}, lambda: False, lambda *a: None, world.runtime_dir / 'diag.json'))
    assert closed == [True]


def test_cancel_before_request_does_not_call_model(world, monkeypatch):
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: pytest.fail('must not call model'))
    with pytest.raises(adapters.TaskCancelled):
        asyncio.run(local_generation.stream_payload(world, {'model': 'test'}, lambda: True, lambda *a: None, world.runtime_dir / 'diag.json'))


def test_native_tool_call_stops_task(world, monkeypatch):
    mock_stream(monkeypatch, [{'type': 'tool_call.start', 'tool': 'read_file'}])
    with pytest.raises(RuntimeError, match='禁止'):
        asyncio.run(local_generation.stream_payload(world, {'model': 'test', 'input': 'test', 'max_output_tokens': 100}, lambda: False, lambda *a: None, world.runtime_dir / 'diag.json'))


@pytest.mark.parametrize('platform,title,body', [('xiaohongshu', '长' * 21, '正文'), ('toutiao', '长' * 31, '正文'), ('xiaohongshu', '标题', '文' * 1001)])
def test_review_blocks_the_same_constraints_as_upload(world, platform, title, body):
    work = payload(platform)
    work['works'][0].update(title=title, body=body)
    review = service._rule_review({'platforms': json.dumps([platform]), 'sources': '[]'}, work, 'v')
    assert review['status'] == 'blocked'


def test_text_commit_durably_queues_without_platform_io(world, monkeypatch):
    monkeypatch.setattr(service, 'sync_draft', lambda *a, **kw: pytest.fail('synchronous upload'))
    _, job, _, draft = frozen()
    assert service.get_job(job['id'])['state'] == 'succeeded'
    assert draft['delivery']['state'] == 'queued'
    assert heartbeat._job_status() == 'busy'


def test_delivery_recovery_only_resumes_safe_preplatform_phase(world):
    _, _, _, draft = frozen()
    with db.transaction() as c:
        c.execute("UPDATE delivery_tasks SET state='running',stage='images'")
        c.execute("UPDATE drafts SET state='syncing'")
    service.recover_jobs()
    delivery.recover()
    result = service.get_draft(draft['id'])
    assert result['state'] == 'local' and result['delivery']['state'] == 'queued'
    with db.transaction() as c:
        c.execute("UPDATE delivery_tasks SET state='running',stage='platform'")
        c.execute("UPDATE drafts SET state='syncing'")
    service.recover_jobs()
    delivery.recover()
    result = service.get_draft(draft['id'])
    assert result['state'] == 'unknown' and result['delivery']['state'] == 'waiting'


def test_stale_queued_delivery_never_contacts_platform(world, monkeypatch):
    topic, _, _, draft = frozen()
    service.decide_topic(topic['id'], {'decision': 'parked', 'expected_revision': 1})
    monkeypatch.setattr(service, 'sync_draft', lambda *a, **kw: pytest.fail('stale upload'))
    delivery.run_one()
    assert service.get_draft(draft['id'])['delivery']['state'] == 'cancelled'


def test_platform_phase_cannot_be_cancelled_as_if_nothing_was_saved(world):
    _, _, _, draft = frozen()
    with db.transaction() as c:
        c.execute("UPDATE delivery_tasks SET state='running',stage='platform'")
    with pytest.raises(HTTPException):
        delivery.cancel(draft['id'])


def test_manual_planning_keeps_coordinator_busy(world):
    assert heartbeat._job_status() == 'idle'
    with heartbeat.busy_operation():
        assert heartbeat._job_status() == 'busy'
    assert heartbeat._job_status() == 'idle'


@pytest.mark.parametrize('ip', ['127.0.0.1', '192.168.1.1', '169.254.169.254', '::1'])
def test_research_rejects_private_dns_targets(monkeypatch, ip):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(0, 0, 0, '', (ip, 443))])
    with pytest.raises(ValueError, match='内网'):
        research.public_target('https://source.example/article')


def test_search_page_only_topic_stops_before_writing():
    with pytest.raises(ValueError, match='原始报道'):
        research.prepare_approved({'sources': [{'url': 'https://www.baidu.com/s?wd=topic'}]}, lambda *a: None)


def test_research_is_a_proposal_does_not_change_approval(world, monkeypatch):
    topic, _, _, _ = frozen()
    original = deepcopy(topic)
    monkeypatch.setattr(research, 'fetch_public', lambda *a: ('https://www.bing.com/search', '<rss><channel><item><link>https://news.example/a</link></item></channel></rss>'))
    monkeypatch.setattr(research, 'read_article', lambda *a: {'url': a[0], 'title': topic['title'], 'body': '原文内容' * 100, 'published_at': None, 'fetched_at': '2026-10-19', 'facts_verified': False})
    result = research.collect(topic)
    assert topic == original
    assert result['sources'] and not result['documents'][0]['facts_verified']
    assert service.get_topic(topic['id'])['status'] == 'approved'


def test_discovery_rejects_unrelated_article_that_only_mentions_seniors():
    title = '老人去见苦命“小海” 上车才知是AI：普通人需要了解什么'
    assert research.search_query(title) == '"小海" AI'
    assert not research.relevant(title, {'title': '让老年人有美满晚年', 'body': '老人养老生活幸福' * 50})
    assert research.relevant(title, {'title': '老人去见“小海”才知是AI', 'body': '原始报道具体内容'})


def test_parse_model_json_repairs_bare_quotes_and_control_chars():
    raw = '{"title":"老人千里寻“小海”","body":"视频标注了"AI生成"，但仅是标识。\\n第二段"}'
    parsed = local_generation.parse_model_json(raw)
    assert parsed['title'] == '老人千里寻“小海”'
    assert '"AI生成"' in parsed['body']
    assert '\n第二段' in parsed['body']
    # A bare quote that genuinely ends the string stays untouched.
    assert local_generation.parse_model_json('{"a":"好","b":2}') == {'a': '好', 'b': 2}
    # Ambiguous output still raises the original error.
    with pytest.raises(json.JSONDecodeError):
        local_generation.parse_model_json('{"a": "他说"好",然后走"')  # quote before ASCII comma: ambiguous


def test_parse_model_json_repairs_curly_quote_delimiters():
    # 真实失败样本：模型用中文弯引号 “ ” 当 JSON 字符串定界符。
    raw = '{"strategy":"x","works":[{"platform":"wechat","format":"article",'
    raw += '"title": “黑色星期五”导火索：Anthropic 真那么差？","body":"正文内容"}]}'
    parsed = local_generation.parse_model_json(raw)
    assert parsed['works'][0]['platform'] == 'wechat'
    assert '黑色星期五' in parsed['works'][0]['title']
    assert parsed['works'][0]['body'] == '正文内容'
    # 字符串内部的弯引号是正文，不能被误转义改坏。
    keep = '{"title":"老人千里寻“小海”","body":"他说“你好”"}'
    assert local_generation.parse_model_json(keep)['body'] == '他说“你好”'


def test_generation_survives_unescaped_quotes_without_model_repair(world, monkeypatch):
    good = json.dumps(payload(), ensure_ascii=False)
    broken = good.replace('如何核对信息来源', '视频标注了"AI生成"才核对', 1)
    assert '"视频标注了"AI生成"才核对"' in broken
    calls = []

    async def stream(settings, request, *args):
        calls.append(request)
        return broken

    monkeypatch.setattr(local_generation, 'stream_payload', stream)
    result, _ = local_generation.generate_platforms({'id': 't', 'platforms': ['wechat'], 'sources': []}, world, 'test', lambda: False, lambda *a: None)
    assert len(calls) == 1
    assert result['works'][0]['title'] == '视频标注了"AI生成"才核对'
