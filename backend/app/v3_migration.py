"""V3 首次数据迁移（幂等，只跑一次）。

执行提示词阶段 1「旧任务迁移」的落地：

1. **旧平台交付任务隔离**：非终态 ``delivery_tasks`` 统一转为
   ``quarantined``，原状态/阶段/尝试次数/消息压缩进 ``quarantine_reason``
   （JSON），并打上隔离时间。隔离态跨重启保存：``enqueue_in`` 不会把
   quarantined 重新排队，普通重试与批量重试也够不到它。
2. **旧生产排期与未完成生成暂停**：``production_slots.paused=1``、
   ``jobs.paused=1``（仅 queued），首次迁移后生效；用户在排期页看到
   清单后可手动恢复，恢复后也只生成本地内容。
3. **结果未知的远端保存不动**：``drafts.state='unknown'`` 保持原样，
   标注「历史结果待人工核对」，不自动查平台（sync_draft 已被模式闸门挡住）。

幂等由 ``runtime_state`` 里的迁移标记保证：第二次执行直接返回，不重复
插入、不改写历史、不生成额外交付任务。回滚只恢复本地文件与数据，不会
恢复账号自动化权限。
"""
from __future__ import annotations

import json

from . import runtime_mode

_MARKER_KEY = "v3_quarantine_migration"
_QUARANTINE_STATES = ("queued", "running", "waiting")


def _marker(connection) -> str | None:
    runtime_mode._ensure_table(connection)
    row = connection.execute(
        "SELECT value FROM runtime_state WHERE key=?", (_MARKER_KEY,)).fetchone()
    return row[0] if row else None


def migration_marker(connection) -> str | None:
    """迁移标记时间戳（ISO）。None 表示尚未迁移。"""
    return _marker(connection)


def _set_marker(connection, stamp: str) -> None:
    connection.execute(
        "INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (_MARKER_KEY, stamp, stamp))


def _columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def migrate(connection) -> dict:
    """执行（或跳过）V3 首次迁移。connection 必须是可写连接。"""
    from .service import now
    report = {"already_migrated": False, "quarantined": 0, "slots_paused": 0,
              "jobs_paused": 0, "unknown_drafts_flagged": 0}
    if _marker(connection):
        report["already_migrated"] = True
        return report
    stamp = now()

    # 1. 旧平台交付任务隔离
    if {"quarantine_reason", "quarantined_at"} <= _columns(connection, "delivery_tasks"):
        rows = connection.execute(
            "SELECT * FROM delivery_tasks WHERE state IN (?,?,?)",
            _QUARANTINE_STATES).fetchall()
        for row in rows:
            reason = json.dumps({
                "from_state": row["state"],
                "from_stage": row["stage"],
                "attempts": row["attempts"],
                "message": (row["message"] or "")[:200],
                "reason": "V3 本地内容模式迁移：平台自动操作已关闭，任务隔离待人工核对",
            }, ensure_ascii=False)
            connection.execute(
                "UPDATE delivery_tasks SET state='quarantined',stage='已隔离',"
                "quarantine_reason=?,quarantined_at=?,cancel_requested=1,updated_at=? "
                "WHERE draft_id=?", (reason, stamp, stamp, row["draft_id"]))
            report["quarantined"] += 1

    # 2. 旧生产排期与未完成生成暂停（列由 db.init_db 的 PRAGMA 守卫补齐）
    if "paused" in _columns(connection, "production_slots"):
        cursor = connection.execute(
            "UPDATE production_slots SET paused=1,updated_at=? "
            "WHERE state IN ('scheduled','queued') AND paused=0", (stamp,))
        report["slots_paused"] = cursor.rowcount or 0
    if "paused" in _columns(connection, "jobs"):
        cursor = connection.execute(
            "UPDATE jobs SET paused=1,updated_at=? WHERE state='queued' AND paused=0", (stamp,))
        report["jobs_paused"] = cursor.rowcount or 0

    # 3. 结果未知的远端保存：只登记提示，不改状态、不查平台
    drafts = connection.execute("SELECT id FROM drafts WHERE state='unknown'").fetchall()
    report["unknown_drafts_flagged"] = len(drafts)

    _set_marker(connection, stamp)
    return report


def quarantined_tasks(connection) -> list[dict]:
    """隔离清单（只读视图，供前端展示）。"""
    if {"quarantine_reason", "quarantined_at"} <= _columns(connection, "delivery_tasks"):
        rows = connection.execute(
            "SELECT q.draft_id,q.state,q.stage,q.attempts,q.quarantine_reason,q.quarantined_at,"
            "d.platform,d.title,d.state AS draft_state,d.remote_id "
            "FROM delivery_tasks q JOIN drafts d ON d.id=q.draft_id "
            "WHERE q.state='quarantined' ORDER BY q.quarantined_at DESC").fetchall()
        return [dict(row) for row in rows]
    return []
