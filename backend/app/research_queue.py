"""Persistent, Worker-owned automatic source prefetch requests.

An API call only enqueues. The singleton consumer performs bounded research,
which cannot approve a topic or modify its scope. Automatic prefetch deliberately
does not update the topic-wide image pool; explicit research retains that option.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import uuid

from . import db, research
from .config import get_settings
from .execution_runtime import InstanceBusy, ProcessLock

_thread = None
_stop = threading.Event()
_consumer_lock = None
_lifecycle = threading.RLock()
_log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS research_requests (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        revision INTEGER NOT NULL,
        input_fingerprint TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','stale')),
        owner_id TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        result_summary TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(topic_id,revision,input_fingerprint)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS research_requests_pending ON research_requests(state,created_at)")


def _snapshot(topic):
    value = dict(topic)
    sources = value.get('sources') or []
    if isinstance(sources, str):
        sources = json.loads(sources)
    return {'id': value['id'], 'revision': value['revision'], 'title': value['title'],
            'sources': deepcopy(sources)}


def _fingerprint(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _view(row):
    value = dict(row)
    value.pop('snapshot', None)
    if value.get('result_summary'):
        value['result_summary'] = json.loads(value['result_summary'])
    return value


def enqueue(topic: dict) -> dict:
    """Persist an idempotent request; never performs network I/O or starts threads."""
    snapshot = _snapshot(topic)
    fingerprint = _fingerprint(snapshot)
    from .service import require
    with db.transaction() as connection:
        current = connection.execute('SELECT * FROM topics WHERE id=?', (snapshot['id'],)).fetchone()
        require(current is not None and not current['deleted_at'], '选题不存在或已删除', 404)
        require(_fingerprint(_snapshot(current)) == fingerprint, '选题已修订，请刷新后重新采集', 409)
        existing = connection.execute(
            'SELECT * FROM research_requests WHERE topic_id=? AND revision=? AND input_fingerprint=?',
            (snapshot['id'], snapshot['revision'], fingerprint)).fetchone()
        if existing:
            return _view(existing)
        stamp, identifier = _now(), uuid.uuid4().hex
        connection.execute("UPDATE research_requests SET state='stale',error='选题修订已变化，旧请求不再执行',updated_at=? "
                           "WHERE topic_id=? AND state='queued' AND input_fingerprint<>?",
                           (stamp, snapshot['id'], fingerprint))
        connection.execute("INSERT INTO research_requests(id,topic_id,revision,input_fingerprint,snapshot,state,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,'queued',?,?)",
                           (identifier, snapshot['id'], snapshot['revision'], fingerprint,
                            json.dumps(snapshot, ensure_ascii=False), stamp, stamp))
        return _view(connection.execute('SELECT * FROM research_requests WHERE id=?', (identifier,)).fetchone())


def status(topic_id: str | None = None) -> dict:
    """Read-only operational status; never creates tables or enqueues work."""
    with db.connect() as connection:
        where = 'WHERE topic_id=?' if topic_id else ''
        args = (topic_id,) if topic_id else ()
        rows = connection.execute(f'SELECT * FROM research_requests {where} ORDER BY created_at DESC LIMIT 100', args).fetchall()
    return {'requests': [_view(row) for row in rows]}


def _still_current(connection, request):
    topic = connection.execute('SELECT * FROM topics WHERE id=?', (request['topic_id'],)).fetchone()
    return bool(topic and not topic['deleted_at'] and
                topic['revision'] == request['revision'] and
                _fingerprint(_snapshot(topic)) == request['input_fingerprint'])


def _recover_after_lock():
    """Only called after acquiring the OS singleton lock, never by an API boot."""
    with db.transaction() as connection:
        rows = connection.execute("SELECT * FROM research_requests WHERE state IN ('running','queued')").fetchall()
        for row in rows:
            if not _still_current(connection, row):
                connection.execute("UPDATE research_requests SET state='stale',owner_id=NULL,error='选题已修订或删除，旧请求已丢弃',updated_at=? WHERE id=?", (_now(), row['id']))
            elif row['state'] == 'running':
                connection.execute("UPDATE research_requests SET state='queued',owner_id=NULL,error='上次自动预采集被中断，等待 Worker 重新读取',updated_at=? WHERE id=?", (_now(), row['id']))


def _claim(owner_id):
    with db.transaction() as connection:
        rows = connection.execute("SELECT * FROM research_requests WHERE state='queued' ORDER BY created_at").fetchall()
        for row in rows:
            if not _still_current(connection, row):
                connection.execute("UPDATE research_requests SET state='stale',error='选题已修订或删除，旧请求已丢弃',updated_at=? WHERE id=?", (_now(), row['id']))
                continue
            connection.execute("UPDATE research_requests SET state='running',owner_id=?,attempt=attempt+1,error=NULL,updated_at=? WHERE id=? AND state='queued'",
                               (owner_id, _now(), row['id']))
            return dict(connection.execute('SELECT * FROM research_requests WHERE id=?', (row['id'],)).fetchone())
    return None


def _run_once(owner_id):
    request = _claim(owner_id)
    if request is None:
        return False
    result, error = None, None
    try:
        # The existing cache is keyed by topic/revision/sources. No latest-topic
        # record or image pool is overwritten if scope changes during this call.
        result = research.collect(json.loads(request['snapshot']), collect_images=False)
        if not result.get('documents'):
            error = str(result.get('message') or '未取得可读原始材料')[:1000]
    except Exception as exception:
        error = str(exception)[:1000]
        _log.warning('自动原文预采集未完成 %s: %s', request['id'], error)
    with db.transaction() as connection:
        current = connection.execute('SELECT * FROM research_requests WHERE id=?', (request['id'],)).fetchone()
        if not current or current['state'] != 'running' or current['owner_id'] != owner_id or current['attempt'] != request['attempt']:
            return True
        if not _still_current(connection, current):
            state, error = 'stale', '采集期间选题已修订或删除；旧结果不作为当前材料'
        else:
            state = 'failed' if error else 'succeeded'
        summary = None if result is None else json.dumps({
            'document_count': len(result.get('documents') or []),
            'errors': result.get('errors') or [], 'source_images': 0,
            'facts_verified': False, 'message': result.get('message') or '',
        }, ensure_ascii=False)
        connection.execute('UPDATE research_requests SET state=?,result_summary=?,error=?,updated_at=? WHERE id=?',
                           (state, summary, error, _now(), request['id']))
    return True


def _loop(owner_id):
    try:
        while not _stop.is_set():
            try:
                worked = _run_once(owner_id)
            except Exception:
                _log.exception('自动原文队列轮询异常')
                worked = False
            if not worked:
                _stop.wait(1)
    finally:
        with _lifecycle:
            global _consumer_lock
            if _consumer_lock is not None:
                _consumer_lock.release()
                _consumer_lock = None


def start(owner_id: str):
    """Called by the unique Worker after its instance lock is acquired."""
    global _thread, _consumer_lock
    with _lifecycle:
        if _thread is not None and _thread.is_alive():
            return
        lock = ProcessLock(get_settings().runtime_dir / 'research-consumer.lock')
        if not lock.acquire():
            raise InstanceBusy('已有 Worker 正在处理自动原文队列')
        _consumer_lock = lock
        try:
            _recover_after_lock()
            _stop.clear()
            _thread = threading.Thread(target=_loop, args=(owner_id,), name='workbench-source-prefetch', daemon=True)
            _thread.start()
        except Exception:
            lock.release()
            _consumer_lock = None
            raise


def stop():
    _stop.set()
    thread = _thread
    if thread is not None and thread is not threading.current_thread():
        # Bounded network I/O may outlast this join. The thread keeps its OS lock
        # until it exits, rather than allowing overlapping consumers on restart.
        thread.join(timeout=2)
