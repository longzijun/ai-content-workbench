from __future__ import annotations

import threading
import uuid
import httpx
from .config import get_settings
from . import execution_runtime as execution

_stop = threading.Event()
_thread: threading.Thread | None = None
_lease_thread: threading.Thread | None = None
_owner_id: str | None = None
_instance_lock = None
_active_job_id = None


def _loop() -> None:
    global _active_job_id
    from . import service
    from .adapters import generate_payload, TaskCancelled
    while not _stop.is_set():
        try:
            job = service.claim_next_job(owner_id=_owner_id)
            if not job:
                _stop.wait(0.7)
                continue
            job_id = job["id"]
            _active_job_id = job_id
            with execution.job_execution(job["execution"]):
                try:
                    topic = job.get("topic") or service.get_topic(job["topic_id"])
                    cancelled = lambda: _stop.is_set() or service.job_cancelled(job_id)
                    progress = lambda stage, message, provider=None: service.job_progress(job_id, stage, message, provider)
                    if topic.get('_candidate_mode'):
                        from . import auto_materials
                        topic = auto_materials.prepare(topic, cancelled, progress)
                    else:
                        from .research import prepare_approved
                        topic = prepare_approved(topic, progress)
                    with execution.model_operation(timeout=60, cancelled=cancelled):
                        payload, provider = generate_payload(
                            topic, get_settings(), cancelled,
                            lambda stage, message, provider=None: service.job_progress(job_id, stage, message, provider),
                        )
                    if _stop.is_set():
                        service.fail_job(job_id, "工作台退出，任务已停止；可以重试")
                    else:
                        if topic.get('_candidate_mode'):
                            from . import candidate_drafts
                            candidate_drafts.finish_job(job_id, provider)
                        else:
                            service.complete_job(job_id, payload, provider, defer_delivery=True)
                    execution.component_event("generation", success=True)
                except execution.LeaseLost:
                    # A reclaimed execution cannot update even its failure state.
                    from . import db
                    with db.connect() as connection:
                        still_owned = execution.owns_job(connection, job_id)
                    if still_owned:
                        service.fail_job(job_id, "任务已取消或工作台退出")
                except TaskCancelled:
                    service.fail_job(job_id, "任务已取消")
                except (httpx.ConnectError, httpx.ReadTimeout, TimeoutError) as exc:
                    if not service.defer_transient_job(job_id, exc):
                        service.fail_job(job_id, str(exc)[:1200])
                except Exception as exc:
                    service.fail_job(job_id, str(exc)[:1200])
                    execution.component_event("generation", error=exc)
        except Exception as exc:
            execution.component_event("generation", error=exc)
            _stop.wait(2)
        finally:
            _active_job_id = None


def _renew_loop():
    while not _stop.wait(execution.HEARTBEAT_SECONDS):
        try:
            if _active_job_id is not None:
                execution.renew_job_leases(_owner_id, _active_job_id)
            from . import service
            service.recover_jobs(include_drafts=False)
            execution.component_heartbeat("worker", _owner_id)
        except Exception as exc:
            execution.component_event("worker", error=exc)


def start_worker(owner_id=None) -> None:
    global _thread, _lease_thread, _owner_id, _instance_lock
    if _thread and _thread.is_alive():
        return
    if not execution.check_schema():
        raise RuntimeError("后台执行数据库尚未迁移，请先运行 scripts/migrate_runtime.py")
    if owner_id is None:
        _instance_lock = execution.ProcessLock(get_settings().runtime_dir / "worker.lock")
        if not _instance_lock.acquire():
            raise execution.InstanceBusy("本运行目录已有后台执行器")
    _owner_id = owner_id or uuid.uuid4().hex
    from . import service
    service.recover_jobs()
    from . import delivery
    delivery.start()
    _stop.clear()
    execution.component_heartbeat("worker", _owner_id)
    _lease_thread = threading.Thread(target=_renew_loop, name="workbench-worker-lease", daemon=True)
    _lease_thread.start()
    _thread = threading.Thread(target=_loop, name="workbench-worker", daemon=True)
    _thread.start()


def stop_worker() -> None:
    global _instance_lock
    _stop.set()
    from . import delivery
    delivery.stop()
    if _thread:
        _thread.join(timeout=8)
    if _lease_thread:
        _lease_thread.join(timeout=2)
    if _owner_id:
        execution.component_heartbeat("worker", _owner_id, "stopped")
    if _instance_lock and not (_thread and _thread.is_alive()):
        _instance_lock.release()
        _instance_lock = None
