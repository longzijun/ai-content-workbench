"""Durable per-draft delivery. Unknown platform writes never become blind retries.

V3：本地内容模式下本模块整体不运行——不启动线程、不恢复旧任务、不入队。
隔离态（quarantined）永远不能被重新排队。
"""
import logging
import threading

from . import db

_stop = threading.Event()
_thread = None


class DeliveryCancelled(Exception):
    pass


def enqueue_in(connection, draft_ids):
    from . import runtime_mode, service
    runtime_mode.require_platform_operations("草稿交付入队")
    for draft_id in draft_ids:
        draft = service._row(connection, 'drafts', draft_id)
        if draft['state'] in ('blocked', 'stale', 'published'):
            continue
        # 逐平台/账号草稿保存许可：小红书永久关闭；未启用的平台拒绝入队。
        runtime_mode.require_platform_save(draft['platform'], "草稿交付入队")
        connection.execute("""INSERT INTO delivery_tasks(draft_id,state,stage,updated_at)
            VALUES(?,'queued','等待交付',?) ON CONFLICT(draft_id) DO UPDATE SET
            state='queued',stage='等待交付',cancel_requested=0,error=NULL,updated_at=excluded.updated_at
            WHERE delivery_tasks.state NOT IN ('queued','running','quarantined')""", (draft_id, service.now()))


def enqueue(draft_id):
    from . import review, runtime_mode, service
    with db.transaction() as connection:
        draft = service._row(connection, 'drafts', draft_id)
        topic, version = service._current(connection, draft['version_id'])
        service.require(topic['status'] == 'approved', '请先批准当前选题')
        service.require(draft['state'] not in ('blocked', 'stale', 'published'), '当前草稿不能排队，请先处理版本或审核状态')
        # 逐平台/账号草稿保存许可：小红书永久关闭；未启用的平台拒绝。
        runtime_mode.require_platform_save(draft['platform'], "草稿排入交付队列")
        # 逐平台审阅：必须审阅通过且指纹有效，否则拒绝入队（版本/账号/素材变化 → 旧确认失效）。
        status = review.current(dict(draft))
        service.require(status.get('approved') is True, '；'.join(status.get('blocking') or ['请先逐平台审阅通过']) )
        enqueue_in(connection, [draft_id])
    return service.get_draft(draft_id)


def cancel(draft_id):
    from . import service
    with db.transaction() as connection:
        row = connection.execute('SELECT * FROM delivery_tasks WHERE draft_id=?', (draft_id,)).fetchone()
        service.require(row is not None and row['state'] in ('queued', 'running'), '交付任务已经结束')
        service.require(row['stage'] != 'platform', '平台保存已经开始，请等待回读结果后核对，避免重复提交')
        connection.execute("UPDATE delivery_tasks SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE draft_id=?", (service.now(), draft_id))
    return service.get_draft(draft_id)


def is_cancelled(draft_id):
    if _stop.is_set():
        return True
    with db.connect() as connection:
        row = connection.execute("""SELECT q.cancel_requested,t.status,t.current_version_id,d.version_id,
            t.revision,v.input_revision FROM delivery_tasks q JOIN drafts d ON d.id=q.draft_id
            JOIN topics t ON t.id=d.topic_id JOIN versions v ON v.id=d.version_id WHERE q.draft_id=?""", (draft_id,)).fetchone()
    return not row or bool(row['cancel_requested']) or row['status'] != 'approved' or row['current_version_id'] != row['version_id'] or row['revision'] != row['input_revision']


def progress(draft_id, stage, message):
    from . import service
    with db.transaction() as connection:
        connection.execute("UPDATE delivery_tasks SET stage=?,message=?,updated_at=? WHERE draft_id=? AND state='running'", (stage, message, service.now(), draft_id))


def recover():
    from . import runtime_mode, service
    if not runtime_mode.platform_operations_allowed():
        # 本地内容模式：旧交付任务已在 V3 迁移中隔离，不在这里恢复。
        return
    with db.transaction() as connection:
        for row in connection.execute("SELECT q.*,d.remote_id,d.state AS draft_state FROM delivery_tasks q JOIN drafts d ON d.id=q.draft_id WHERE q.state='running'").fetchall():
            if row['cancel_requested']:
                state = 'cancelled'
            elif row['stage'] != 'platform':
                state = 'queued'
                if row['draft_state'] in ('syncing', 'unknown') and not row['remote_id']:
                    connection.execute("UPDATE drafts SET state='local',error='交付准备中断，已排队恢复',updated_at=? WHERE id=?", (service.now(), row['draft_id']))
            else:
                state = 'queued' if row['remote_id'] else 'waiting'
            connection.execute("UPDATE delivery_tasks SET state=?,error=?,updated_at=? WHERE draft_id=?", (state, '上次运行中断；已有平台标识只回读，无标识的未知保存需人工核对' if state == 'waiting' else None, service.now(), row['draft_id']))


def run_one():
    from . import runtime_mode, service
    if not runtime_mode.platform_operations_allowed():
        return False
    with db.transaction() as connection:
        row = connection.execute("SELECT * FROM delivery_tasks WHERE state='queued' ORDER BY updated_at LIMIT 1").fetchone()
        if not row:
            return False
        draft_id = row['draft_id']
        connection.execute("UPDATE delivery_tasks SET state='running',stage='preflight',attempts=attempts+1,updated_at=? WHERE draft_id=?", (service.now(), draft_id))
    state, error = 'failed', None
    try:
        if is_cancelled(draft_id):
            raise DeliveryCancelled()
        with db.connect() as connection:
            draft = service._row(connection, "drafts", draft_id)
            runtime_mode.require_platform_save(draft["platform"], "草稿交付执行")
            from . import review
            status = review.current(dict(draft))
            service.require(status.get("approved") is True, "；".join(status.get("blocking") or ["审阅已失效，请重新审阅"]))
        result = service.sync_draft(draft_id, verify_media=True,
                                    cancelled=lambda: is_cancelled(draft_id),
                                    progress=lambda stage, message: progress(draft_id, stage, message))
        media = result.get('media') or {}
        state = 'succeeded' if result['state'] in ('synced', 'browser_saved', 'reviewed', 'published') and media.get('status') in ('verified', 'not_required') else 'waiting'
        error = result.get('error') or (media.get('error') or '请核对草稿、图片及封面交付状态' if state == 'waiting' else None)
    except DeliveryCancelled:
        state, error = 'cancelled', '交付已停止，已有正文与图片保留'
        with db.transaction() as connection:
            connection.execute("UPDATE drafts SET state='failed',error=?,updated_at=? WHERE id=? AND state='syncing'", (error, service.now(), draft_id))
    except Exception as exc:
        error = getattr(exc, 'detail', None) or str(exc)
        with db.transaction() as connection:
            # A crash during platform I/O is uncertain, never permission to resubmit.
            connection.execute("UPDATE drafts SET state='unknown',error=?,updated_at=? WHERE id=? AND state='syncing'", ('保存过程异常，请核对已知草稿记录', service.now(), draft_id))
    with db.transaction() as connection:
        connection.execute('UPDATE delivery_tasks SET state=?,error=?,updated_at=? WHERE draft_id=?', (state, str(error)[:1200] if error else None, service.now(), draft_id))
    return True


def _loop():
    while not _stop.is_set():
        try:
            if not run_one():
                _stop.wait(0.7)
        except Exception:
            logging.exception('Delivery worker iteration failed')
            _stop.wait(2)


def start():
    global _thread
    from . import runtime_mode
    if not runtime_mode.platform_operations_allowed():
        # 本地内容模式：交付线程不启动，旧任务保持隔离。
        return
    if _thread and _thread.is_alive():
        return
    recover()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='workbench-delivery', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=8)
