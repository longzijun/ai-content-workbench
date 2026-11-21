"""Worker-only durable automatic research; fixtures never fetch public pages."""
import json

import pytest
from fastapi import HTTPException

from backend.app import config, db, research, research_queue, service
from backend.app.execution_runtime import ProcessLock

pytestmark = pytest.mark.local_content_only


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    db.init_db()
    return config.get_settings()


def topic():
    return service.create_topic({'title': '自动原文队列隔离验证', 'brief': '', 'platforms': ['wechat'],
                                 'sources': [{'url': 'https://example.invalid/article', 'title': '材料', 'note': ''}]})


def completed(snapshot, **kwargs):
    assert kwargs == {'collect_images': False}
    return {'topic_id': snapshot['id'], 'revision': snapshot['revision'],
            'documents': [{'url': snapshot['sources'][0]['url'], 'title': '隔离正文'}],
            'errors': [], 'source_images': 0, 'message': '正文供人工核对'}


def test_enqueue_is_idempotent_and_never_collects_or_approves(world, monkeypatch):
    monkeypatch.setattr(research, 'collect', lambda *args, **kwargs: pytest.fail('API enqueue cannot fetch'))
    value = topic()
    first = research_queue.enqueue(value)
    second = research_queue.enqueue(value)
    assert first['id'] == second['id']
    assert first['state'] == 'queued' and first['attempt'] == 0
    assert 'snapshot' not in first
    assert service.get_topic(value['id'])['status'] == 'pending'
    assert service.get_topic(value['id'])['jobs'] == []


def test_stale_enqueue_cannot_replace_current_revision(world):
    value = topic()
    service.patch_topic(value['id'], {'expected_revision': value['revision'], 'title': '新修订'})
    with pytest.raises(HTTPException) as failure:
        research_queue.enqueue(value)
    assert failure.value.status_code == 409


def test_revised_queued_request_is_skipped_and_current_request_runs(world, monkeypatch):
    value = topic()
    old = research_queue.enqueue(value)
    current = service.patch_topic(value['id'], {'expected_revision': value['revision'], 'brief': '已修改范围'})
    fresh = research_queue.enqueue(current)
    monkeypatch.setattr(research, 'collect', completed)
    assert research_queue._run_once('owner') is True
    records = {row['id']: row for row in research_queue.status()['requests']}
    assert records[old['id']]['state'] == 'stale'
    assert records[fresh['id']]['state'] == 'succeeded'
    assert records[fresh['id']]['result_summary']['facts_verified'] is False


def test_revision_change_during_fetch_discards_result_without_mutating_topic(world, monkeypatch):
    value = topic()
    queued = research_queue.enqueue(value)

    def change_during_fetch(snapshot, **kwargs):
        service.patch_topic(value['id'], {'expected_revision': value['revision'], 'title': '用户的新标题'})
        return completed(snapshot, **kwargs)

    monkeypatch.setattr(research, 'collect', change_during_fetch)
    assert research_queue._run_once('owner') is True
    result = research_queue.status(value['id'])['requests'][0]
    assert result['state'] == 'stale'
    current = service.get_topic(value['id'])
    assert current['title'] == '用户的新标题' and current['status'] == 'pending'
    assert current['sources'] == value['sources']


def test_worker_restart_recovers_only_current_running_requests(world):
    value = topic()
    queued = research_queue.enqueue(value)
    running = research_queue._claim('old-owner')
    assert running['id'] == queued['id']
    with ProcessLock(world.runtime_dir / 'research-consumer.lock'):
        research_queue._recover_after_lock()
    again = research_queue._claim('new-owner')
    assert again['attempt'] == 2 and again['owner_id'] == 'new-owner'
    service.patch_topic(value['id'], {'expected_revision': value['revision'], 'brief': '当前范围已变'})
    with ProcessLock(world.runtime_dir / 'research-consumer.lock'):
        research_queue._recover_after_lock()
    assert research_queue.status()['requests'][0]['state'] == 'stale'


def test_singleton_lock_prevents_second_research_consumer(world):
    with ProcessLock(world.runtime_dir / 'research-consumer.lock'):
        from backend.app.execution_runtime import InstanceBusy
        with pytest.raises(InstanceBusy):
            research_queue.start('second-owner')


def test_network_error_is_durable_and_does_not_loop_forever(world, monkeypatch):
    value = topic()
    research_queue.enqueue(value)
    monkeypatch.setattr(research, 'collect', lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError('bounded failure')))
    assert research_queue._run_once('owner') is True
    assert research_queue._run_once('owner') is False
    row = research_queue.status()['requests'][0]
    assert row['state'] == 'failed' and 'bounded failure' in row['error']
    assert row['attempt'] == 1


def test_prefetch_never_updates_topic_image_pool_or_current_revision_cache(world, monkeypatch):
    value = topic()
    image = world.runtime_dir / 'data' / 'research' / 'images' / value['id'] / 'pool.json'
    image.parent.mkdir(parents=True)
    original = '{"images":[]}'
    image.write_text(original, 'utf-8')
    monkeypatch.setattr(research, 'read_article', lambda url, deadline: {
        'url': url, 'title': '隔离正文', 'body': '只提供正文，未经事实确认。',
        'fetched_at': '2026-11-01T00:00:00Z', 'published_at': None, 'images': ['https://example.invalid/image.png']})
    monkeypatch.setattr(research, 'collect_source_images', lambda *args, **kwargs: pytest.fail('automatic prefetch must not touch shared image pool'))
    research_queue.enqueue(value)
    assert research_queue._run_once('owner') is True
    assert image.read_text('utf-8') == original
    assert research.cached(value)['revision'] == value['revision']
    current = service.patch_topic(value['id'], {'expected_revision': value['revision'], 'brief': '新修订材料'})
    assert research.cached(current) is None


def test_queue_status_is_read_only(world):
    value = topic()
    research_queue.enqueue(value)
    with db.connect() as connection:
        before = '\n'.join(connection.iterdump())
    research_queue.status(value['id'])
    with db.connect() as connection:
        assert '\n'.join(connection.iterdump()) == before
