"""本机模型操作锁与繁忙状态。"""
import threading
from contextlib import contextmanager

from . import db

_operations = 0
_operations_lock = threading.Lock()


@contextmanager
def busy_operation():
    global _operations
    from .execution_runtime import model_operation
    with model_operation(timeout=60):
        with _operations_lock:
            _operations += 1
        try:
            yield
        finally:
            with _operations_lock:
                _operations -= 1


def _job_status() -> str:
    """jobs 表存在进行中的任务则视为 busy。"""
    if _operations:
        return "busy"
    from .execution_runtime import model_busy
    if model_busy():
        return "busy"
    try:
        with db.connect() as connection:
            row = connection.execute(
                "SELECT (SELECT COUNT(*) FROM jobs WHERE state IN ('running','pending','queued')) + (SELECT COUNT(*) FROM delivery_tasks WHERE state IN ('queued','running')) + (SELECT COUNT(*) FROM drafts WHERE state='syncing') AS c"
            ).fetchone()
        return "busy" if row and row["c"] else "idle"
    except Exception:
        return "busy"
