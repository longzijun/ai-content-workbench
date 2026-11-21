"""Capability evidence must not authorize writes or promote historical accounts."""
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import capabilities, config, db, main, worker, trends


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    settings = config.get_settings()
    db.init_db()
    monkeypatch.setattr(capabilities.browser_bridge, 'get_status', lambda settings: {})
    def forbidden(*args, **kwargs):
        pytest.fail('Capability inventory attempted network, credentials, or a browser action')
    for name in ('get', 'post'):
        monkeypatch.setattr(httpx, name, forbidden)
    for name in ('open_login', 'check_connection', 'run_in_page'):
        monkeypatch.setattr(capabilities.browser_bridge, name, forbidden)
    monkeypatch.setattr(config, 'get_wechat_credentials', forbidden)
    return SimpleNamespace(settings=settings, monkeypatch=monkeypatch)


def record(world, key, platform='wechat', state='synced', remote_id='browser:wechat:123', destination='platform', delivery=None):
    with db.connect() as connection:
        connection.execute("INSERT OR IGNORE INTO topics(id,title,brief,platforms,sources,created_at,updated_at) VALUES('topic','t','','[]','[]','2026-10-21','2026-10-21')")
        connection.execute('INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           (key, 'topic', int(key), 1, '{}', key, 'test', '2026-10-21'))
        connection.execute('INSERT INTO drafts(id,topic_id,version_id,platform,title,state,destination,remote_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (key, 'topic', key, platform, 'test', state, destination, remote_id, '2026-10-21', '2026-10-21T12:00:00Z'))
        if delivery:
            connection.execute('INSERT INTO delivery_tasks(draft_id,state,stage,updated_at) VALUES(?,?,?,?)', (key, delivery, 'platform', '2026-10-21'))


def channel(world, identity):
    return next(item for item in capabilities.inventory(world.settings)['channels'] if item['id'] == identity)


def test_history_does_not_confirm_current_login_or_api_write(world):
    record(world, '1')
    row = channel(world, 'wechat:browser')
    assert row['auth_state'] == 'unknown'
    assert row['write_evidence'] == 'historical_records'
    assert row['history']['account_scope'] == 'all_recorded_accounts'
    assert channel(world, 'wechat:official_api')['write_evidence'] == 'none'


def test_read_probe_is_never_write_verification_and_no_secrets_returned(world):
    world.settings.wechat_app_id = 'sensitive-app-id'
    world.settings.wechat_thumb_media_id = 'sensitive-cover-id'
    world.settings.wechat_capability_verified = True
    (world.settings.runtime_dir / 'data/wechat.credentials').write_bytes(b'not decrypted')
    row = channel(world, 'wechat:official_api')
    assert row['configured'] is True
    assert row['read_evidence'] == 'settings_read_probe'
    assert row['write_evidence'] == 'none'
    result = json.dumps(capabilities.inventory(world.settings))
    assert 'sensitive-' not in result and 'not decrypted' not in result
    assert channel(world, 'wechat:browser')['read_evidence'] == 'none'


def test_stale_read_flag_without_credentials_is_not_verified(world):
    world.settings.wechat_app_id = 'appid'
    world.settings.wechat_capability_verified = True
    assert channel(world, 'wechat:official_api')['read_evidence'] == 'none'


def test_unknown_waiting_overlap_counts_distinct_drafts(world):
    record(world, '1', state='unknown', delivery='waiting')
    record(world, '2', state='stale', delivery='waiting')
    record(world, '3', state='unknown')
    record(world, '4', state='failed', delivery='failed')
    assert capabilities.inventory(world.settings)['attention'] == {
        'available': True, 'unknown_count': 2, 'waiting_count': 2,
        'overlap_count': 1, 'distinct_draft_count': 3,
    }


def test_browser_local_records_are_not_cloud_or_api_evidence(world):
    record(world, '1', 'xiaohongshu', 'browser_saved', 'browser:xiaohongshu:abc', 'browser')
    assert channel(world, 'xiaohongshu:browser')['history']['saved_record_count'] == 1
    assert channel(world, 'xiaohongshu:browser')['storage_scope'] == 'browser'
    assert channel(world, 'xiaohongshu:cloud')['history']['saved_record_count'] == 0
    assert channel(world, 'xiaohongshu:cloud')['implemented'] is False


@pytest.mark.parametrize('state,remote,destination', [
    ('unknown', 'browser:wechat:1', 'platform'), ('stale', 'browser:wechat:1', 'platform'),
    ('failed', 'browser:wechat:1', 'platform'), ('synced', None, 'platform'),
    ('synced', 'browser:toutiao:1', 'platform'), ('synced', 'browser:wechat:1', 'browser'),
])
def test_unconfirmed_or_misbound_rows_cannot_supply_success_evidence(world, state, remote, destination):
    record(world, '1', state=state, remote_id=remote, destination=destination)
    assert channel(world, 'wechat:browser')['history']['saved_record_count'] == 0
    assert channel(world, 'wechat:official_api')['history']['saved_record_count'] == 0


def test_cached_verified_session_is_labeled_observation_not_live_probe(world):
    world.monkeypatch.setattr(capabilities.browser_bridge, 'get_status', lambda settings: {
        'toutiao': {'state': 'connected', 'last_checked': '2026-10-21T12:00:00Z',
                    'account_label': 'test', 'draft_capability_verified': True}})
    row = channel(world, 'toutiao:browser')
    assert row['auth_state'] == 'connected_observed'
    assert row['write_evidence'] == row['read_evidence'] == 'session_save_readback'
    assert channel(world, 'toutiao:short_post')['write_evidence'] == 'none'


def test_missing_database_is_unknown_and_not_created(tmp_path):
    settings = config.Settings(runtime_dir=tmp_path / 'absent')
    result = capabilities.inventory(settings)
    assert result['attention']['available'] is False
    assert result['attention']['unknown_count'] is None
    assert all(item['history']['saved_record_count'] is None for item in result['channels'])
    assert not settings.runtime_dir.exists()


def test_inventory_preserves_database_and_files(world):
    record(world, '1', state='unknown', delivery='waiting')
    path = world.settings.runtime_dir / 'data/workbench.sqlite3'
    with sqlite3.connect(path) as connection:
        before = '\n'.join(connection.iterdump())
    before_files = {str(p): p.read_bytes() for p in world.settings.runtime_dir.rglob('*') if p.is_file() and p.suffix not in {'.sqlite3', '.sqlite3-shm', '.sqlite3-wal'}}
    capabilities.inventory(world.settings)
    with sqlite3.connect(path) as connection:
        assert '\n'.join(connection.iterdump()) == before
    assert {str(p): p.read_bytes() for p in world.settings.runtime_dir.rglob('*') if p.is_file() and p.suffix not in {'.sqlite3', '.sqlite3-shm', '.sqlite3-wal'}} == before_files


def test_endpoint_requires_session_and_rejects_foreign_origin(world):
    for module, start, stop in [(worker, 'start_worker', 'stop_worker'), (trends, 'start_monitor', 'stop_monitor')]:
        world.monkeypatch.setattr(module, start, lambda: None)
        world.monkeypatch.setattr(module, stop, lambda: None)
    with TestClient(main.app) as client:
        assert client.get('/api/capabilities').status_code == 401
        from backend.tests.auth_helpers import bootstrap
        assert bootstrap(client).status_code == 200
        assert client.get('/api/capabilities', headers={'Origin': 'https://foreign.invalid'}).status_code == 403
        result = client.get('/api/capabilities')
        assert result.status_code == 200
        assert result.json()['observation_only'] is True
