"""Studio HTTP authorization, read semantics and atomic commit integration."""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.app import adapters, config, db, fact_base, main, material_package, service

pytestmark = pytest.mark.local_content_only


@pytest.fixture
def studio_api(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    monkeypatch.setattr(adapters, 'get_health', lambda *args, **kwargs: {})
    db.init_db()
    source = {'url': 'https://example.invalid/studio', 'title': '隔离测试', 'note': ''}
    topic = service.create_topic({'title': '隔离测试：内容工坊', 'brief': '验证持久保存',
                                  'platforms': ['wechat', 'toutiao', 'xiaohongshu'], 'sources': [source], 'content_line': 'A', 'stance_ids': []})
    topic = service.decide_topic(topic['id'], {'decision': 'approved', 'expected_revision': topic['revision']})
    payload = {'strategy': '隔离测试数据', 'works': [
        {'platform': p, 'format': 'note' if p == 'xiaohongshu' else 'article',
         'title': '测试标题', 'body': '这是一份隔离测试正文，用于验证工作稿保存。',
         'tags': [], 'source_urls': [source['url']], 'image_prompts': []}
        for p in topic['platforms']]}
    version = service.create_version(topic['id'], payload, _initial_version(topic['id'], payload), defer_delivery=True)
    client = TestClient(main.app)
    token = authenticated_bootstrap(client).json()['token']
    client.headers['X-Workbench-Token'] = token
    yield client, topic, version, source
    client.close()


def _initial_version(topic_id, payload):
    # No provider is invoked; this is explicitly an isolated persistence fixture.
    with db.transaction() as connection:
        topic = service._row(connection, 'topics', topic_id)
        version_id, _ = service._insert_version(connection, topic, payload, 'test:studio-api')
    return version_id


def path(topic, version, suffix=''):
    return f"/api/topics/{topic['id']}/studio-draft/wechat{suffix}?base_version_id={version['id']}"


def test_draft_get_is_read_only_and_write_requires_session_token(studio_api):
    client, topic, version, _ = studio_api
    data = client.get(path(topic, version)).json()
    assert data['revision'] == 0
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM studio_working_copies').fetchone()[0] == 0
    body = {key: data[key] for key in ('title', 'body', 'tags', 'source_urls')}
    body['expected_revision'] = 0
    token = client.headers.pop('X-Workbench-Token')
    assert client.put(path(topic, version), json=body).status_code == 403
    client.headers['X-Workbench-Token'] = token
    assert client.put(path(topic, version), json=body).status_code == 200
    assert client.put(path(topic, version), json=body).status_code == 409


def test_invalid_body_and_platform_return_422(studio_api):
    client, topic, version, _ = studio_api
    assert client.put(path(topic, version), json={'expected_revision': -1}).status_code == 422
    assert client.post(path(topic, version, '/suggestions'), json={'directive': '修改'}).status_code == 422
    assert client.get(path(topic, version).replace('/wechat?', '/douyin?')).status_code == 404
    assert client.post(f"/api/topics/{topic['id']}/studio-commit", json={}).status_code == 422


def test_atomic_http_commit_preserves_other_platforms_and_old_copy(studio_api):
    client, topic, version, _ = studio_api
    original = client.get(path(topic, version)).json()
    save = {key: original[key] for key in ('title', 'body', 'tags', 'source_urls')}
    save.update(expected_revision=0, body=original['body'] + '\n新增的隔离测试段落。')
    assert client.put(path(topic, version), json=save).status_code == 200
    revisions = {'wechat': 1, 'toutiao': 0, 'xiaohongshu': 0}
    result = client.post(f"/api/topics/{topic['id']}/studio-commit",
                         json={'base_version_id': version['id'], 'expected_revisions': revisions})
    assert result.status_code == 200, result.text
    new = result.json()['version']
    assert new['payload']['works'][0]['body'] == save['body']
    assert new['payload']['works'][1:] == version['payload']['works'][1:]
    old = client.get(path(topic, version)).json()
    assert old['stale'] is True and old['body'] == save['body']
    assert client.post(f"/api/topics/{topic['id']}/studio-commit",
                       json={'base_version_id': version['id'], 'expected_revisions': revisions}).status_code == 409
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM delivery_tasks').fetchone()[0] == 0


def test_fact_get_reports_gaps_without_rewriting_locked_fact(studio_api):
    client, topic, _, source = studio_api
    fact = fact_base.save(topic['id'], {'audience': '隔离测试读者', 'core_view': '保存与审阅分开',
         'sources': [source], 'facts': [{'claim': '测试稿保存在本机', 'status': 'confirmed'}]})
    fact_base.mark_ready(topic['id'])
    with db.transaction() as connection:
        fact_base.lock(connection, topic['id'])
    for _ in range(2):
        response = client.get(f"/api/topics/{topic['id']}/fact-base")
        assert response.json()['gaps'] == []
        assert response.json()['fact_base']['status'] == 'locked'
    with db.connect() as connection:
        rows = connection.execute('SELECT * FROM fact_bases WHERE topic_id=?', (topic['id'],)).fetchall()
    assert len(rows) == 1 and rows[0]['revision'] == fact['revision']


def test_review_rejects_wrong_preview_version_and_changed_facts(studio_api):
    client, topic, version, source = studio_api
    fact = fact_base.save(topic['id'], {'audience': '测试', 'core_view': '测试观点', 'sources': [source],
                                      'facts': [{'claim': '测试事实', 'status': 'confirmed'}]})
    draft = next(d for d in service.state()['drafts'] if d['version_id'] == version['id'])
    body = {'result': 'approved', 'comment': '', 'account_label': '测试账号',
            'ai_disclosure': 'AI辅助', 'facts_confirmed': True,
            'expected_version_id': 'wrong-version', 'expected_fact_revision': fact['revision']}
    url = f"/api/drafts/{draft['id']}/review-platform"
    assert client.post(url, json=body).status_code == 409
    body.update(expected_version_id=version['id'], expected_fact_revision=fact['revision'] + 1)
    assert client.post(url, json=body).status_code == 409
    body['expected_fact_revision'] = fact['revision']
    assert client.post(url, json=body).status_code == 200


def test_package_fingerprints_distinguish_reapproved_account_on_same_version(studio_api):
    client, topic, version, source = studio_api
    fact_base.save(topic['id'], {'audience': '测试读者', 'core_view': '先审阅后导出', 'sources': [source],
                                'facts': [{'claim': '测试稿保存在本机', 'status': 'confirmed'}]})
    fact_base.mark_ready(topic['id'])
    drafts = [d for d in service.state()['drafts'] if d['version_id'] == version['id']]
    body = {'result': 'approved', 'comment': '', 'account_label': '原测试账号',
            'ai_disclosure': 'AI辅助', 'facts_confirmed': True, 'expected_version_id': version['id']}
    for draft in drafts:
        assert client.post(f"/api/drafts/{draft['id']}/review-platform", json=body).status_code == 200
    url = f"/api/topics/{topic['id']}/material-packages"
    assert client.post(url, json={}).status_code == 410
    first = material_package.build(topic['id'])  # Historical archive semantics remain testable.
    chosen = drafts[0]
    fingerprint = first['manifest']['platforms'][chosen['platform']]['review']['fingerprint']
    assert fingerprint
    assert first['manifest']['platforms'][chosen['platform']]['ai_disclosure'] == body['ai_disclosure']
    first_bytes = Path(first['zip_path']).read_bytes()
    body['account_label'] = '新测试账号'
    assert client.post(f"/api/drafts/{chosen['id']}/review-platform", json=body).status_code == 200
    console = client.get(f"/api/topics/{topic['id']}/review").json()
    current = next(e['review']['fingerprint'] for e in console['platforms'] if e['platform'] == chosen['platform'])
    assert fingerprint != current
    second = material_package.build(topic['id'])
    assert second['manifest']['platforms'][chosen['platform']]['review']['fingerprint'] == current
    assert second['zip_path'] != first['zip_path']
    assert Path(first['zip_path']).read_bytes() == first_bytes
