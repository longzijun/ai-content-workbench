"""Real ownership/locking and backup round-trip tests, never production data."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest

from backend.app import artifacts, backup_scheduler, backup_tools, config, db, execution_runtime as runtime, service


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    db.init_db()
    runtime.ensure_schema()
    return config.get_settings()


def test_os_lock_refuses_second_owner_and_releases_after_process_exit(isolated):
    path = isolated.runtime_dir / 'test-process.lock'
    code = ('import sys; from pathlib import Path; from backend.app.execution_runtime import ProcessLock; '
            'lock=ProcessLock(Path(sys.argv[1])); assert lock.acquire(); print("ready",flush=True); input(); lock.release()')
    child = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        assert child.stdout.readline().strip() == 'ready'
        contender = runtime.ProcessLock(path)
        assert not contender.acquire()
        child.communicate('\n', timeout=10)
        assert child.returncode == 0
        assert contender.acquire()
        contender.release()
    finally:
        if child.poll() is None:
            child.communicate('\n', timeout=10)


def test_lease_fences_unscoped_and_old_attempt_writes(isolated):
    with db.transaction() as connection:
        old = runtime.acquire_job(connection, 'job-1', 'old-worker')
    with db.connect() as connection:
        assert not runtime.owns_job(connection, 'job-1')
        assert runtime.owns_job(connection, 'job-1', old)
    with db.transaction() as connection:
        new = runtime.acquire_job(connection, 'job-1', 'new-worker')
        assert new['attempt'] == old['attempt'] + 1
    with runtime.job_execution(old), db.transaction() as connection:
        with pytest.raises(runtime.LeaseLost):
            runtime.require_job_owner(connection, 'job-1')
    with runtime.job_execution(new), db.transaction() as connection:
        runtime.require_job_owner(connection, 'job-1')


def test_expired_lease_cannot_be_revived(isolated):
    with db.transaction() as connection:
        token = runtime.acquire_job(connection, 'job-2', 'worker')
        connection.execute("UPDATE job_leases SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    runtime.renew_job_leases('worker')
    with db.connect() as connection:
        assert not runtime.owns_job(connection, 'job-2', token)


def test_model_lock_is_cross_thread_bounded_and_reentrant(isolated):
    failures = []
    with runtime.model_operation(timeout=0):
        with runtime.model_operation(timeout=0):
            assert runtime.model_busy()
        def contender():
            try:
                with runtime.model_operation(timeout=0.05):
                    failures.append('incorrectly acquired')
            except runtime.InstanceBusy:
                failures.append('busy')
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=3)
        assert failures == ['busy']
        # Inference ownership does not keep a SQLite write transaction open.
        with db.transaction() as connection:
            connection.execute("SELECT 1")
    assert not runtime.model_busy()


def test_status_distinguishes_stopped_and_stale_worker(isolated):
    runtime.component_heartbeat('worker', 'worker')
    assert runtime.runtime_status()['worker_online']
    runtime.component_heartbeat('worker', 'worker', 'stopped')
    assert not runtime.runtime_status()['worker_online']
    runtime.component_heartbeat('worker', 'worker')
    with db.transaction() as connection:
        connection.execute("UPDATE execution_components SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    assert not runtime.runtime_status()['worker_online']


def test_backup_has_verified_integrity_and_restores_to_empty_directory(isolated, tmp_path):
    result = artifacts.create_backup(isolated)
    archive = Path(result['path'])
    assert result['verification']['verified']
    output = tmp_path / 'restored'
    restored = backup_tools.restore_archive(archive, output)
    assert restored['verified'] and not restored['started_services']
    assert (output / 'data' / 'workbench.sqlite3').is_file()
    with pytest.raises(ValueError, match='空目录'):
        backup_tools.restore_archive(archive, output)


def test_backup_rejects_mutated_entry_even_with_valid_zip_crc(isolated, tmp_path):
    result = artifacts.create_backup(isolated)
    damaged = tmp_path / 'damaged.zip'
    with zipfile.ZipFile(result['path']) as source, zipfile.ZipFile(damaged, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name == 'manifest.json':
                data += b' '
            target.writestr(name, data)
    with pytest.raises(ValueError, match='SHA-256'):
        backup_tools.verify_archive(damaged)


def test_backup_rejects_archive_traversal_before_extract(isolated, tmp_path):
    bad = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(bad, 'w') as archive:
        archive.writestr('../outside', 'unsafe')
    with pytest.raises(ValueError, match='不安全路径'):
        backup_tools.verify_archive(bad)
    assert not (tmp_path / 'outside').exists()


def test_automatic_backup_runs_once_per_beijing_day_and_reports_no_off_disk_copy(isolated):
    assert backup_scheduler.tick()
    assert not backup_scheduler.tick()
    status = backup_scheduler.status()
    assert status['verified_backups'] == 1
    assert not status['off_disk_copy']


def _queued_job():
    topic = service.create_topic({'title': '隔离任务边界检查', 'brief': '隔离测试，仅验证状态',
                                  'platforms': ['wechat'], 'content_line': 'A', 'stance_ids': [],
                                  'sources': [{'url': 'https://example.com/ref', 'title': '测试来源', 'note': ''}]})
    service.decide_topic(topic['id'], {'decision': 'approved', 'expected_revision': topic['revision']})
    return service.generate(topic['id'], topic['revision'])


def test_live_lease_is_not_recovered_and_expired_attempt_is_fenced(isolated):
    job = _queued_job()
    old = service.claim_next_job(owner_id='old')['execution']
    assert service.recover_jobs() == 0
    with db.transaction() as connection:
        connection.execute("UPDATE job_leases SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert service.recover_jobs() == 1
    new = service.claim_next_job(owner_id='new')['execution']
    assert new['attempt'] == 2
    with runtime.job_execution(old):
        with pytest.raises(runtime.LeaseLost):
            service.job_progress(job['id'], 'late', 'must not arrive')
        with pytest.raises(runtime.LeaseLost):
            service.fail_job(job['id'], 'late failure')
        with pytest.raises(runtime.LeaseLost):
            service.complete_job(job['id'], {}, 'late')
    with runtime.job_execution(new):
        service.job_progress(job['id'], 'owned', 'current owner')
        assert not service.job_cancelled(job['id'])
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 0
        assert connection.execute('SELECT stage FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'owned'


def test_transient_retry_is_bounded_and_paused_jobs_are_not_claimed(isolated):
    job = _queued_job()
    for attempt in range(1, 4):
        claimed = service.claim_next_job(owner_id='worker')
        assert claimed['execution']['attempt'] == attempt
        with runtime.job_execution(claimed['execution']):
            if attempt < 3:
                assert service.defer_transient_job(job['id'], 'connection temporarily unavailable')
            else:
                assert not service.defer_transient_job(job['id'], 'still unavailable')
                service.fail_job(job['id'], 'retry limit')
        if attempt < 3:
            assert service.claim_next_job(owner_id='worker') is None
            with db.transaction() as connection:
                connection.execute("UPDATE job_retry_backoff SET next_retry_at='2000-01-01T00:00:00+00:00'")
    another = _queued_job()
    with db.transaction() as connection:
        connection.execute('UPDATE jobs SET paused=1 WHERE id=?', (another['id'],))
    assert service.claim_next_job(owner_id='worker') is None


def test_api_lifespan_never_recovers_or_starts_background_jobs(isolated, monkeypatch):
    from backend.app import main, worker, scheduler, trends
    from fastapi.testclient import TestClient
    job = _queued_job()
    service.claim_next_job(owner_id='running-worker')
    def forbidden(*args, **kwargs):
        pytest.fail('API lifecycle attempted to own background work')
    monkeypatch.setattr(service, 'recover_jobs', forbidden)
    monkeypatch.setattr(worker, 'start_worker', forbidden)
    monkeypatch.setattr(scheduler, 'start', forbidden)
    monkeypatch.setattr(trends, 'start_monitor', forbidden)
    with TestClient(main.app) as client:
        assert client.get('/api/ready').status_code == 200
    with db.connect() as connection:
        assert connection.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'running'


def test_backup_excludes_live_session_credentials_without_logging_out_owner(isolated, tmp_path):
    with db.transaction() as connection:
        connection.execute("INSERT INTO auth_sessions VALUES('session','write-token','device','2026','2099')")
    result = artifacts.create_backup(isolated)
    backup_tools.restore_archive(Path(result['path']), tmp_path / 'restored')
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(tmp_path / 'restored' / 'data' / 'workbench.sqlite3')) as restored:
        assert restored.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 0
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 1


def test_manual_refresh_is_persistent_and_only_worker_launches_when_automatic_monitor_disabled(isolated, monkeypatch):
    from backend.app import trends
    launches = []
    monkeypatch.setattr(trends, '_launch_refresh', lambda run_id, source_ids: launches.append(run_id))
    assert not trends.settings()['enabled']
    requested = trends.request_refresh()
    assert requested['queued'] and launches == []
    assert trends.request_refresh()['queued'] and launches == []
    assert trends.monitor_tick()
    assert len(launches) == 1
    assert not trends.request_refresh()['queued']
    assert not trends.monitor_tick()
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM trend_runs').fetchone()[0] == 1
