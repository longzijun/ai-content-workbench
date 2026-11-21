"""Local generation faults preserve safe editable text and isolate platforms."""
import json
import pytest

from backend.app import adapters, candidate_drafts, db, execution_runtime as execution, local_generation
from backend.tests.test_candidate_drafts import candidate_world, payload

pytestmark = pytest.mark.local_content_only


def test_long_title_repair_failure_preserves_body_and_continues(candidate_world, monkeypatch):
    world = candidate_world
    monkeypatch.setattr(adapters, '_loaded_local_model_info', lambda settings: {'id': 'test'})
    monkeypatch.setattr(adapters, 'validate_generated_payload', lambda value, part: (value, []))
    def stream(settings, request, cancel, progress, diagnostic):
        if '.title.' in diagnostic.name:
            return '{"title":false}'
        platform = diagnostic.name.split('.')[0]
        return json.dumps(payload(platform, '长' * 40 if platform == 'wechat' else '可用的头条标题'), ensure_ascii=False)
    monkeypatch.setattr(local_generation, 'stream_payload_with_retry', stream)
    with execution.job_execution(world['token']):
        local_generation.generate_platforms(world['topic'], world['settings'], 'test', lambda: False, lambda *args: None)
        candidate_drafts.finish_job(world['job']['id'])
    rows = candidate_drafts.list_for_topic(world['topic']['id'])['candidates']
    wechat = next(row for row in rows if row['platform'] == 'wechat')
    toutiao = next(row for row in rows if row['platform'] == 'toutiao')
    assert wechat['body'] == payload()['works'][0]['body'] and wechat['state'] == 'needs_changes'
    assert toutiao['body'] and toutiao['state'] == 'draft'
    assert any('短标题' in issue for issue in wechat['issues'])


def test_truncated_json_recovers_body_as_incomplete(candidate_world, monkeypatch):
    world = candidate_world
    monkeypatch.setattr(adapters, '_loaded_local_model_info', lambda settings: {'id': 'test'})
    def stream(settings, request, cancel, progress, diagnostic):
        local_generation.atomic_json(diagnostic, {'partial_content': '{"works":[{"platform":"wechat","title":"原题","body":"已有正文\\n换一段'})
        raise ValueError('模型输出被截断')
    monkeypatch.setattr(local_generation, 'stream_payload_with_retry', stream)
    with execution.job_execution(world['token']):
        local_generation.generate_platforms(world['topic'], world['settings'], 'test', lambda: False, lambda *args: None)
    rows = candidate_drafts.list_for_topic(world['topic']['id'])['candidates']
    assert all(row['state'] == 'incomplete' for row in rows)
    assert all(row['body'] == '已有正文\n换一段' for row in rows)


def test_recovery_never_exposes_raw_json_as_prose():
    assert local_generation._recover_candidate_text('{broken json source tokens}', 'wechat') is None
    value = local_generation._recover_candidate_text('{"title":"A","body":"text \\"quote\\" trailing', 'wechat')
    assert value['works'][0]['body'].startswith('text')


def test_cancellation_stops_before_new_platform(candidate_world, monkeypatch):
    world = candidate_world
    monkeypatch.setattr(adapters, '_loaded_local_model_info', lambda settings: {'id': 'test'})
    with execution.job_execution(world['token']), pytest.raises(adapters.TaskCancelled):
        local_generation.generate_platforms(world['topic'], world['settings'], 'test', lambda: True, lambda *args: None)
    assert not candidate_drafts.list_for_topic(world['topic']['id'])['candidates']
