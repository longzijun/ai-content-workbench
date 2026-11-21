"""Candidate storage/lease/CAS tests use isolated SQLite, never production."""
import json
from copy import deepcopy

from fastapi import HTTPException
import pytest

from backend.app import candidate_drafts as candidates, config, db, execution_runtime as execution, service

pytestmark = pytest.mark.local_content_only


@pytest.fixture
def candidate_world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    db.init_db()
    execution.ensure_schema()
    candidates.init_schema()
    topic = service.create_topic({'title': '说明写作流程', 'brief': '用户提供的写作说明', 'platforms': ['wechat', 'toutiao'],
                                  'sources': [], 'content_line': 'A', 'stance_ids': []})
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET status='approved' WHERE id=?", (topic['id'],))
        row = service._row(connection, 'topics', topic['id'])
        job = service._create_job(connection, row)
        snap = json.loads(connection.execute('SELECT input_snapshot FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
        snap['_candidate_mode'] = True
        connection.execute('UPDATE jobs SET input_snapshot=? WHERE id=?', (service.encode(snap), job['id']))
    job = service.claim_next_job(owner_id='candidate-test')
    topic = job['topic']
    topic['_auto_materials'] = {'mode': 'evidenced', 'source_urls': [], 'issues': [], 'facts': [], 'input_fingerprint': 'test'}
    return {'topic': topic, 'job': job, 'token': job['execution'], 'settings': config.get_settings()}


def payload(platform='wechat', title='把写作过程讲清楚'):
    return {'strategy': '测试数据', 'works': [{'platform': platform, 'format': 'article', 'title': title,
             'body': '开头说明问题。\n\n中间说明具体过程。\n\n结尾给出方法。', 'tags': ['写作'], 'source_urls': [], 'image_prompts': []}]}


def publish(world, platform='wechat', **kwargs):
    with execution.job_execution(world['token']):
        return candidates.publish(world['topic'], platform, payload(platform), **kwargs)


def test_candidate_available_before_any_official_version(candidate_world):
    world = candidate_world
    saved = publish(world)
    assert saved['body'] and not saved['stale']
    assert candidates.read(world['topic']['id'], saved['id'])['body'] == saved['body']
    with db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM versions').fetchone()[0] == 0


def test_candidate_edits_while_job_running_and_cas_conflict(candidate_world):
    world = candidate_world
    initial = publish(world)
    edit = {'expected_revision': 0, 'title': '作者改的标题', 'body': '作者改的正文', 'tags': [], 'source_urls': []}
    saved = candidates.save(world['topic']['id'], initial['id'], edit)
    assert saved['revision'] == 1 and saved['body'] == '作者改的正文'
    with pytest.raises(HTTPException) as error:
        candidates.save(world['topic']['id'], initial['id'], edit)
    assert error.value.status_code == 409
    assert candidates.read(world['topic']['id'], initial['id'])['body'] == '作者改的正文'


def test_new_model_candidate_never_overwrites_author_candidate(candidate_world):
    world = candidate_world
    first = publish(world)
    candidates.save(world['topic']['id'], first['id'], {'expected_revision': 0, 'title': '人工标题', 'body': '人工保留', 'tags': [], 'source_urls': []})
    second = publish(world)
    assert first['id'] != second['id']
    assert candidates.read(world['topic']['id'], first['id'])['body'] == '人工保留'
    assert len(candidates.list_for_topic(world['topic']['id'])['candidates']) == 2


def test_lost_lease_and_changed_input_reject_late_write(candidate_world):
    world = candidate_world
    with db.transaction() as connection:
        new = execution.acquire_job(connection, world['job']['id'], 'replacement')
    with execution.job_execution(world['token']), pytest.raises(execution.LeaseLost):
        candidates.publish(world['topic'], 'wechat', payload())
    with db.transaction() as connection:
        connection.execute('UPDATE topics SET revision=revision+1 WHERE id=?', (world['topic']['id'],))
    with execution.job_execution(new), pytest.raises(HTTPException):
        candidates.publish(world['topic'], 'wechat', payload())


def test_read_old_candidate_but_cannot_edit_changed_scope(candidate_world):
    world = candidate_world
    saved = publish(world)
    with db.transaction() as connection:
        connection.execute('UPDATE topics SET revision=revision+1 WHERE id=?', (world['topic']['id'],))
    assert candidates.read(world['topic']['id'], saved['id'])['stale']
    with pytest.raises(HTTPException):
        candidates.save(world['topic']['id'], saved['id'], {'expected_revision': 0, 'title': '改稿', 'body': '正文', 'tags': [], 'source_urls': []})


def test_finish_job_preserves_partial_platform_without_formal_version(candidate_world):
    world = candidate_world
    publish(world)
    with execution.job_execution(world['token']):
        candidates.finish_job(world['job']['id'], 'local:test')
    job = service.get_job(world['job']['id'])
    assert job['state'] == 'succeeded' and '1/2' in job['stage'] and '待预览' in job['stage']
    assert not job['output_version_id']


def test_commit_requires_all_platforms_and_strict_title(candidate_world, monkeypatch):
    from backend.app import auto_materials
    world = candidate_world
    first = publish(world)
    second = publish(world, 'toutiao')
    with execution.job_execution(world['token']):
        candidates.finish_job(world['job']['id'])
    monkeypatch.setattr(auto_materials, 'freeze_gaps', lambda material, payload: [])
    ids = {'wechat': first['id'], 'toutiao': second['id']}
    revisions = {'wechat': 0, 'toutiao': 0}
    with pytest.raises(HTTPException):
        candidates.commit(world['topic']['id'], {'candidate_ids': {'wechat': first['id']}, 'expected_revisions': {'wechat': 0}})
    edited = candidates.save(world['topic']['id'], first['id'], {'expected_revision': 0, 'title': '超' * 40, 'body': first['body'], 'tags': [], 'source_urls': []})
    revisions['wechat'] = edited['revision']
    with pytest.raises(HTTPException):
        candidates.commit(world['topic']['id'], {'candidate_ids': ids, 'expected_revisions': revisions})
    with db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM versions').fetchone()[0] == 0


def test_empty_finish_does_not_claim_draft_success(candidate_world):
    world = candidate_world
    with execution.job_execution(world['token']):
        candidates.finish_job(world['job']['id'])
    job = service.get_job(world['job']['id'])
    assert job['state'] == 'failed' and '暂未生成正文' in job['stage']


def test_public_material_summary_omits_private_and_raw_text(candidate_world):
    world = candidate_world
    world['topic']['_auto_materials'].update({'author_text': '私有原文', 'private_info': '内部记录',
        'documents': [{'url': 'https://example.invalid/source', 'title': '资料标题', 'body': '完整原文'}],
        'issues': [{'message': '一条来源未读取', 'blocking': False}]})
    row = publish(world, issues=world['topic']['_auto_materials']['issues'])
    summary = candidates.list_for_topic(world['topic']['id'])['materials_summary']
    assert summary['documents'] == [{'url': 'https://example.invalid/source', 'title': '资料标题'}]
    assert summary['issues'] == ['一条来源未读取']
    assert row['issues'] == ['一条来源未读取']
    assert '私有原文' not in json.dumps(summary, ensure_ascii=False)
    assert '内部记录' not in json.dumps(row, ensure_ascii=False)


def test_model_candidate_bounds_video_fields_and_unsafe_types(candidate_world):
    world = candidate_world
    work = payload()['works'][0]
    work.update(title={'unsafe': 'object'}, tags=[{}, '正常'], shots=[{'spoken_text': '口播' * 5000,
        'visual_notes': {'unsafe': True}, 'source_refs': [None, 'https://example.invalid']} for _ in range(100)])
    with execution.job_execution(world['token']):
        saved = candidates.publish(world['topic'], 'wechat', {'strategy': '候选', 'works': [work]})
    assert saved['title'] == '' and saved['tags'] == ['正常']
    assert len(saved['shots']) == 40 and len(saved['shots'][0]['spoken_text']) == 6000
    assert saved['shots'][0]['visual_notes'] == ''
    assert saved['shots'][0]['source_refs'] == ['https://example.invalid']


def test_retry_reuses_needs_changes_and_does_not_touch_author_revision(candidate_world):
    world = candidate_world
    saved = publish(world, state='needs_changes', issues=['标题需调整'])
    edited = candidates.save(world['topic']['id'], saved['id'], {'expected_revision': 0, 'title': '长' * 40,
        'body': '作者保留的正文', 'tags': [], 'source_urls': []})
    child_topic = {**world['topic'], '_generation_run': 'new-job', '_candidate_parent_job': world['job']['id']}
    reused = candidates.reusable_parent(child_topic, 'wechat')
    assert reused['id'] == saved['id'] and reused['body'] == '作者保留的正文'
    assert reused['revision'] == edited['revision'] == 1


def test_freeze_candidate_binds_material_snapshot_without_confirming_facts(candidate_world, monkeypatch):
    from backend.app import auto_materials, fact_base
    world = candidate_world
    first = publish(world)
    second = publish(world, 'toutiao')
    with execution.job_execution(world['token']):
        candidates.finish_job(world['job']['id'])
    monkeypatch.setattr(auto_materials, 'freeze_gaps', lambda material, payload: [])
    frozen = candidates.commit(world['topic']['id'], {'candidate_ids': {'wechat': first['id'], 'toutiao': second['id']},
        'expected_revisions': {'wechat': 0, 'toutiao': 0}})['version']
    assert auto_materials.version_materials(frozen['id']) == world['topic']['_auto_materials']
    assert fact_base.get(world['topic']['id']) is None


def test_discovered_source_freezes_and_review_ignores_unused_fact_gaps(candidate_world):
    from backend.app import auto_materials, fact_base, review, publish_preparation
    world = candidate_world
    url = 'https://example.invalid/automatically-found-original'
    material = world['topic']['_auto_materials']
    material.update({'documents': [{'url': url, 'title': '自动找到的原文', 'body': payload()['works'][0]['body']}],
                     'source_urls': [url], 'evidence_urls': [url],
                     'facts': [{'claim': '未采用的待核条目', 'status': 'unverified'}]})
    fact_base.save(world['topic']['id'], {'facts': [{'claim': '未采用的待核条目', 'status': 'unverified'}]})
    rows = {}
    with execution.job_execution(world['token']):
        for platform in world['topic']['platforms']:
            value = payload(platform)
            value['works'][0]['source_urls'] = [url]
            rows[platform] = candidates.publish(world['topic'], platform, value)
        candidates.finish_job(world['job']['id'])
    frozen = candidates.commit(world['topic']['id'], {'candidate_ids': {platform: row['id'] for platform, row in rows.items()},
        'expected_revisions': {platform: 0 for platform in rows}})['version']
    assert auto_materials.version_materials(frozen['id'])['evidence_urls'] == [url]
    assert review.console(world['topic']['id'])['gaps'] == []
    for entry in review.console(world['topic']['id'])['platforms']:
        review.record(entry['draft']['id'], 'approved', '', '测试账号', 'AI辅助创作', True,
                      expected_version_id=frozen['id'], expected_fact_revision=1)
    data = review.console(world['topic']['id'])
    assert all(entry['review']['approved'] for entry in data['platforms'])
    assert not publish_preparation.get(world['topic']['id'])['blocking']
