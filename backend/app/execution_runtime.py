"""Cross-process execution ownership for the local Web/API and Worker pair.

SQLite remains the task authority. OS locks serialize local processes; expiring
job leases fence late results after ownership has changed. No lock is held by a
database transaction while inference or network I/O is running.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import get_settings

SCHEMA_VERSION = 2
LEASE_SECONDS = max(15, min(300, int(os.environ.get("WORKBENCH_LEASE_SECONDS", "45"))))
HEARTBEAT_SECONDS = max(1, min(10, int(os.environ.get("WORKBENCH_HEARTBEAT_SECONDS", "5")), LEASE_SECONDS // 3))
_execution = contextvars.ContextVar("workbench_execution", default=None)
_local_model = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    """Called only by the controlled database migration, never a read endpoint."""
    with db.transaction() as connection:
        init_schema(connection)


def init_schema(connection) -> None:
    """Reuse the current migration transaction instead of opening a second one."""
    statements = """
            CREATE TABLE IF NOT EXISTS execution_components (
                name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, pid INTEGER NOT NULL,
                state TEXT NOT NULL, heartbeat_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS job_leases (
                job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                lease_expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS job_retry_backoff (
                job_id TEXT PRIMARY KEY, next_retry_at TEXT NOT NULL
            );
        """
    for statement in statements.split(';'):
        if statement.strip():
            connection.execute(statement)
    connection.execute("INSERT INTO execution_meta(key,value) VALUES('schema_version',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))


def check_schema() -> bool:
    try:
        with db.connect() as connection:
            row = connection.execute("SELECT value FROM execution_meta WHERE key='schema_version'").fetchone()
            tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return bool(row and int(row[0]) == SCHEMA_VERSION and
                        {"candidate_drafts", "auto_material_snapshots", "version_material_snapshots",
                         "draft_creation_requests"} <= tables)
    except Exception:
        return False


class InstanceBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class ProcessLock:
    """An OS-released lock, safe after a process crashes (PID files are not locks)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        if not self.acquire():
            raise InstanceBusy("本运行目录已有相同执行器，未启动第二个实例")
        return self

    def __exit__(self, *args):
        self.release()


@contextmanager
def worker_instance():
    owner = uuid.uuid4().hex
    with ProcessLock(get_settings().runtime_dir / "worker.lock"):
        yield owner


@contextmanager
def model_operation(timeout: float = 60, cancelled=None):
    """One inference at a time across API suggestions and background generation.

    Reentrant in the current thread so existing busy_operation call sites can
    share the same boundary without deadlocking. Waiting is bounded and observes
    cancellation. The OS releases the lock if a process exits unexpectedly.
    """
    if getattr(_local_model, "depth", 0):
        _local_model.depth += 1
        try:
            yield
        finally:
            _local_model.depth -= 1
        return
    lock = ProcessLock(get_settings().runtime_dir / "model.lock")
    deadline = time.monotonic() + max(0, timeout)
    while not lock.acquire():
        if cancelled and cancelled():
            raise LeaseLost("等待模型时任务已取消或执行权失效")
        if time.monotonic() >= deadline:
            raise InstanceBusy("本地模型正在处理其他任务，请稍后重试")
        time.sleep(0.15)
    _local_model.depth = 1
    try:
        yield
    finally:
        _local_model.depth = 0
        lock.release()


def model_busy() -> bool:
    if getattr(_local_model, "depth", 0):
        return True
    path = get_settings().runtime_dir / "model.lock"
    if not path.exists():
        return False
    probe = ProcessLock(path)
    if not probe.acquire():
        return True
    probe.release()
    return False


def acquire_job(connection, job_id: str, owner_id: str) -> dict:
    """Must share the transaction that moves queued -> running."""
    row = connection.execute("SELECT attempt FROM job_leases WHERE job_id=?", (job_id,)).fetchone()
    attempt = (int(row[0]) if row else 0) + 1
    expires = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    connection.execute("INSERT INTO job_leases VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                       "owner_id=excluded.owner_id,attempt=excluded.attempt,lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at",
                       (job_id, owner_id, attempt, expires, now()))
    return {"job_id": job_id, "owner_id": owner_id, "attempt": attempt}


@contextmanager
def job_execution(token: dict):
    previous = _execution.set(dict(token))
    try:
        yield
    finally:
        _execution.reset(previous)


def current_execution():
    return _execution.get()


def owns_job(connection, job_id: str, token=None) -> bool:
    token = token if token is not None else _execution.get()
    row = connection.execute("SELECT * FROM job_leases WHERE job_id=?", (job_id,)).fetchone()
    # Legacy manual helpers/tests without an active lease remain usable. A leased
    # running job can only be written by the holder, never an unscoped caller.
    if row is None:
        return token is None
    return bool(token and token.get("job_id") == job_id and row["owner_id"] == token.get("owner_id")
                and row["attempt"] == token.get("attempt") and row["lease_expires_at"] > now())


def require_job_owner(connection, job_id: str) -> None:
    if not owns_job(connection, job_id):
        raise LeaseLost("任务执行权已失效，已丢弃迟到结果")


def renew_job_leases(owner_id: str, job_id=None) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with db.transaction() as connection:
        connection.execute("UPDATE job_leases SET lease_expires_at=?,updated_at=? WHERE owner_id=? "
                           "AND lease_expires_at>? AND (? IS NULL OR job_id=?) "
                           "AND job_id IN (SELECT id FROM jobs WHERE state='running')",
                           (expires, now(), owner_id, now(), job_id, job_id))


def component_heartbeat(name: str, owner_id: str, state="running", detail=None) -> None:
    with db.transaction() as connection:
        connection.execute("INSERT INTO execution_components VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                           "owner_id=excluded.owner_id,pid=excluded.pid,state=excluded.state,heartbeat_at=excluded.heartbeat_at,detail=excluded.detail",
                           (name, owner_id, os.getpid(), state, now(), json.dumps(detail or {}, ensure_ascii=False)))


def component_event(name: str, *, success=False, error=None) -> None:
    """Persist useful tick health instead of swallowing loop exceptions."""
    try:
        with db.transaction() as connection:
            row = connection.execute("SELECT * FROM execution_components WHERE name=?", (name,)).fetchone()
            detail = json.loads(row["detail"]) if row else {}
            if success:
                detail["last_success_at"] = now()
                detail["last_error"] = None
            if error is not None:
                detail["last_error"] = str(error)[:800]
                detail["last_error_at"] = now()
            connection.execute("INSERT INTO execution_components VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                               "pid=excluded.pid,state=excluded.state,heartbeat_at=excluded.heartbeat_at,detail=excluded.detail",
                               (name, row["owner_id"] if row else "local", os.getpid(), "degraded" if error else "running", now(), json.dumps(detail, ensure_ascii=False)))
    except Exception:
        # Status reporting must never turn a completed user operation into a
        # failure when migration has not run or a disk is temporarily unavailable.
        pass


def runtime_status() -> dict:
    result = {"schema_ready": check_schema(), "worker_online": False, "components": {}, "queue": {},
              "model_busy": model_busy(), "startup_mode": "user_login", "requires_awake_host": True}
    if not result["schema_ready"]:
        return result
    with db.connect() as connection:
        for row in connection.execute("SELECT * FROM execution_components"):
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            item["online"] = item["state"] not in ("stopped", "failed") and item["heartbeat_at"] > (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)).isoformat()
            result["components"][item["name"]] = item
        result["queue"] = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) count FROM jobs GROUP BY state")}
    result["worker_online"] = result["components"].get("worker", {}).get("online", False)
    return result
