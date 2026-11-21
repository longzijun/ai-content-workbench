"""User-login supervisor for two independent processes, not a Windows service.

Launch through the existing .cmd entry. Restart only exited children; dependency
or model health never causes a healthy process to be killed.
"""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class Supervisor:
    def __init__(self, runtime: Path, port: int):
        self.runtime, self.port = runtime.resolve(), port
        self.children, self.history, self.retry_at, self.states = {}, {}, {}, {}
        self.stopping = False
        self.started_at = _stamp()

    def _log_reader(self, role, pipe):
        logger = logging.getLogger('workbench.' + role)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(self.runtime / 'logs' / (role + '.log'), maxBytes=5 * 1024 * 1024,
                                      backupCount=4, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        try:
            for line in iter(pipe.readline, ''):
                logger.info(line.rstrip())
        finally:
            pipe.close()
            logger.removeHandler(handler)
            handler.close()

    def spawn(self, role):
        script = 'server.py' if role == 'web' else 'worker.py'
        command = [sys.executable, str(PROJECT / 'scripts' / script)]
        if role == 'web':
            command += ['--port', str(self.port)]
        environment = dict(os.environ)
        environment.update(WORKBENCH_RUNTIME_DIR=str(self.runtime), WORKBENCH_SUPERVISED='1',
                           PYTHONUTF8='1', PYTHONUNBUFFERED='1', WORKBENCH_PROCESS_ROLE=role)
        process = subprocess.Popen(command, cwd=PROJECT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding='utf-8', errors='replace',
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.children[role] = process
        self.states[role] = {'pid': process.pid, 'state': 'running', 'started_at': _stamp()}
        threading.Thread(target=self._log_reader, args=(role, process.stdout), daemon=True).start()

    def save(self):
        record = {'pid': os.getpid(), 'executable': sys.executable, 'project': str(PROJECT),
                  'runtime': str(self.runtime), 'port': self.port, 'started_at': self.started_at,
                  'startup_mode': 'user_login', 'children': self.states, 'updated_at': _stamp()}
        temporary = self.runtime / 'supervisor.tmp'
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
        # Windows readers can briefly hold the old status file without delete sharing.
        for attempt in range(3):
            try:
                temporary.replace(self.runtime / 'supervisor.json')
                return
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33):
                    raise
                if attempt < 2:
                    time.sleep(0.05)
        logging.warning('Supervisor status file is busy; keeping the previous snapshot.')

    def run(self):
        from backend.app.execution_runtime import ProcessLock
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / 'logs').mkdir(exist_ok=True)
        with ProcessLock(self.runtime / 'supervisor.lock'):
            for marker in ('stop-request', 'web-stop-request', 'worker-stop-request'):
                (self.runtime / marker).unlink(missing_ok=True)
            environment = {**os.environ, 'WORKBENCH_RUNTIME_DIR': str(self.runtime), 'PYTHONUTF8': '1'}
            subprocess.run([sys.executable, str(PROJECT / 'scripts' / 'migrate_runtime.py')],
                           cwd=PROJECT, env=environment, check=True)
            for role in ('worker', 'web'):
                self.spawn(role)
            try:
                while True:
                    if (self.runtime / 'stop-request').exists():
                        break
                    for role in ('web', 'worker'):
                        process = self.children[role]
                        if process.poll() is None:
                            continue
                        clock = time.monotonic()
                        if self.states[role]['state'] == 'running':
                            recent = self.history.setdefault(role, deque())
                            while recent and recent[0] < clock - 600:
                                recent.popleft()
                            recent.append(clock)
                            self.states[role].update(state='retry_wait' if len(recent) <= 5 else 'failed',
                                                     exit_code=process.returncode, exited_at=_stamp())
                            self.retry_at[role] = clock + min(60, 2 ** len(recent))
                        if self.states[role]['state'] == 'retry_wait' and clock >= self.retry_at[role]:
                            self.spawn(role)
                    self.save()
                    time.sleep(0.5)
            finally:
                self.stopping = True
                for role in ('web', 'worker'):
                    (self.runtime / (role + '-stop-request')).write_text('stop', 'utf-8')
                # Existing inference has bounded cancellation checks. Wait rather
                # than killing a PID which might later belong to another app.
                for role, process in self.children.items():
                    try:
                        process.wait(timeout=45)
                        self.states[role]['state'] = 'stopped'
                    except subprocess.TimeoutExpired:
                        self.states[role]['state'] = 'shutdown_pending'
                self.save()
                (self.runtime / 'stop-request').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    from backend.app.config import get_settings
    runtime = args.runtime or get_settings().runtime_dir
    os.environ['WORKBENCH_RUNTIME_DIR'] = str(runtime.resolve())
    Supervisor(runtime, args.port).run()


if __name__ == '__main__':
    main()
