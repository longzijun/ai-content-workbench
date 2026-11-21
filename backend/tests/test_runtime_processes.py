"""Real isolated API/Worker process checks with a test-only inference adapter.

Tests use temporary data and local fixtures.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from backend.app import config, db, execution_runtime, service

PROJECT = Path(__file__).resolve().parents[2]
WORKER_CODE = r'''
import asyncio,time
from pathlib import Path
from backend.app import adapters,research
from backend.app.config import get_settings
from backend.tests.test_workflow import payload
research.prepare_approved = lambda topic,progress: topic
def fake_inference(topic,settings,cancel,progress):
    progress('isolated-slow-task','隔离测试执行中，不访问真实模型','test-only')
    while not (settings.runtime_dir/'test-allow-finish').exists():
        if cancel():
            raise adapters.TaskCancelled()
        time.sleep(.1)
    return payload(tuple(topic['platforms'])),'test-only'
adapters.generate_payload=fake_inference
import runpy
runpy.run_path(str(Path.cwd()/'scripts'/'worker.py'),run_name='__main__')
'''


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(.1)
    raise AssertionError('isolated process did not reach the expected state before timeout')


def test_supervisor_survives_a_locked_status_snapshot(tmp_path, monkeypatch):
    from scripts.supervisor import Supervisor

    supervisor = Supervisor(tmp_path, 18765)
    supervisor.save()
    snapshot = tmp_path / 'supervisor.json'
    previous = snapshot.read_bytes()

    def locked(source, target):
        error = PermissionError('status file open in another process')
        error.winerror = 5
        raise error

    supervisor.states['web'] = {'state': 'running'}
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', locked)
        supervisor.save()
    assert snapshot.read_bytes() == previous
    supervisor.save()
    assert json.loads(snapshot.read_text('utf-8'))['children']['web']['state'] == 'running'


@pytest.fixture
def processes(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(runtime))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    db.init_db()
    children = []
    environment = {**os.environ, 'WORKBENCH_DISABLE_AUTOMATION': '1', 'WORKBENCH_SUPERVISED': '1',
                   'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}

    def spawn(role):
        arguments = [sys.executable, '-c', WORKER_CODE] if role == 'worker' else [sys.executable, str(PROJECT / 'scripts' / 'server.py'), '--port', str(port)]
        log = (tmp_path / f'{role}-{len(children)}.log').open('w', encoding='utf-8')
        child = subprocess.Popen(arguments, cwd=PROJECT, env={**environment, 'WORKBENCH_PROCESS_ROLE': role},
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        children.append((role, child, log))
        return child

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    yield {'runtime': runtime, 'spawn': spawn, 'url': f'http://127.0.0.1:{port}', 'children': children}
    (runtime / 'test-allow-finish').write_text('finish', 'utf-8')
    for role in ('web', 'worker'):
        (runtime / (role + '-stop-request')).write_text('stop', 'utf-8')
    for role, child, log in children:
        if child.poll() is None:
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                # Only this fixture's known child is terminated on test failure.
                child.terminate()
                child.wait(timeout=5)
        log.close()


def queue_job():
    topic = service.create_topic({'title': '隔离进程恢复检查', 'brief': '仅验证任务状态', 'platforms': ['wechat'],
                                  'content_line': 'A', 'stance_ids': [],
                                  'sources': [{'url': 'https://example.com/reference', 'title': '测试来源', 'note': ''}]})
    service.decide_topic(topic['id'], {'decision': 'approved', 'expected_revision': topic['revision']})
    return service.generate(topic['id'], topic['revision'])


def job_state(job_id):
    with db.connect() as connection:
        return dict(connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def test_real_api_restart_does_not_interrupt_worker_or_duplicate_result(processes):
    worker = processes['spawn']('worker')
    wait_until(lambda: execution_runtime.runtime_status()['worker_online'])
    web = processes['spawn']('web')
    wait_until(lambda: httpx.get(processes['url'] + '/api/ready', timeout=1).status_code == 200)
    job = queue_job()
    wait_until(lambda: job_state(job['id'])['stage'] == 'isolated-slow-task')
    (processes['runtime'] / 'web-stop-request').write_text('stop', 'utf-8')
    web.wait(timeout=8)
    assert worker.poll() is None and job_state(job['id'])['state'] == 'running'
    replacement = processes['spawn']('web')
    wait_until(lambda: httpx.get(processes['url'] + '/api/ready', timeout=1).status_code == 200)
    assert replacement.poll() is None and worker.poll() is None
    assert job_state(job['id'])['state'] == 'running'
    (processes['runtime'] / 'test-allow-finish').write_text('finish', 'utf-8')
    wait_until(lambda: job_state(job['id'])['state'] == 'succeeded')
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM versions WHERE topic_id=?', (job['topic_id'],)).fetchone()[0] == 1


def test_real_worker_crash_releases_singleton_and_expired_lease_is_reclaimed(processes):
    worker = processes['spawn']('worker')
    wait_until(lambda: execution_runtime.runtime_status()['worker_online'])
    job = queue_job()
    wait_until(lambda: job_state(job['id'])['stage'] == 'isolated-slow-task')
    duplicate = processes['spawn']('worker')
    duplicate.wait(timeout=10)
    assert duplicate.returncode != 0 and worker.poll() is None
    with db.connect() as connection:
        old_owner = connection.execute('SELECT owner_id FROM job_leases WHERE job_id=?', (job['id'],)).fetchone()[0]
    worker.terminate()
    worker.wait(timeout=5)
    # Fault-inject expiry rather than sleeping through the production lease.
    with db.transaction() as connection:
        connection.execute("UPDATE job_leases SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job['id'],))
    replacement = processes['spawn']('worker')
    def reclaimed():
        with db.connect() as connection:
            lease = connection.execute('SELECT * FROM job_leases WHERE job_id=?', (job['id'],)).fetchone()
            return lease['attempt'] == 2 and lease['owner_id'] != old_owner
    wait_until(reclaimed)
    assert replacement.poll() is None
    (processes['runtime'] / 'test-allow-finish').write_text('finish', 'utf-8')
    wait_until(lambda: job_state(job['id'])['state'] == 'succeeded')
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM versions WHERE topic_id=?', (job['topic_id'],)).fetchone()[0] == 1
