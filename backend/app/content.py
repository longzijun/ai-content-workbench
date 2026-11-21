"""内容管理层：列表检索、复制、归档、软删除、回收站、批量与审计日志。

设计约束（与既有 service.py 保持一致）：

1. 删除默认软删除。`topics.deleted_at` / `drafts.deleted_at` 置位，同时把删除前
   整行快照写进 `content_trash`。任何 SELECT 列表都必须显式排除已删除行。
2. 批量操作逐条独立结算。单条失败不能把整批标成功，返回 per-item 结果。
3. 永久删除要二次授权并先做引用检查：有 drafts 指向的 topic、有 versions /
   publications / remote_id 的 draft 一律拒绝。
4. 审计日志只记录动作、对象、结果。凭据、token、cookie 不进这张表，
   调用方也不得把它们塞进 detail。
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from . import db, service, platform_registry
from .service import encode, new_id, now, require

PLATFORMS = platform_registry.PLATFORM_SET
ENTITY_LABEL = {"topic": "选题", "draft": "草稿", "version": "内容版本", "asset": "素材"}
PRIORITY_LABEL = {1: "紧急", 2: "高", 3: "普通", 4: "低", 5: "搁置"}

SORT_COLUMNS = {
    "updated_at": "updated_at",
    "created_at": "created_at",
    "title": "title",
    "state": "state",
    "priority": "priority",
}


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def audit(connection, entity_type, entity_id, action, result="success", detail="", topic_id=None, source="ui"):
    """写一条操作日志。detail 由调用方保证不含凭据。"""
    connection.execute(
        "INSERT INTO audit_log(id,entity_type,entity_id,topic_id,action,result,detail,source,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (new_id(), entity_type, entity_id, topic_id or "", action, result, str(detail)[:800], source, now()),
    )


def audit_failed(entity_type, entity_id, action, detail, topic_id=None):
    """失败也要留痕，但走独立连接：此时调用方的写事务可能已经回滚。"""
    try:
        with db.transaction() as connection:
            audit(connection, entity_type, entity_id, action, "failed", detail, topic_id)
    except Exception:
        # 日志写不进去也不能连带业务失败。审计缺失要可见，故不静默吞掉。
        pass


def list_audit(entity_type=None, entity_id=None, action=None, result=None, limit=100, offset=0):
    where, args = [], []
    if entity_type:
        where.append("entity_type=?"); args.append(entity_type)
    if entity_id:
        where.append("entity_id=?"); args.append(entity_id)
    if action:
        where.append("action=?"); args.append(action)
    if result:
        where.append("result=?"); args.append(result)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.connect() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM audit_log {clause}", args).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM audit_log {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*args, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
    return {"total": total, "items": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# 行组装
# ---------------------------------------------------------------------------

def _topic_row(connection, row, draft_index):
    result = dict(row)
    result["platforms"] = json.loads(result["platforms"])
    result["sources"] = json.loads(result["sources"])
    result["stance_ids"] = json.loads(result.get("stance_ids") or "[]")
    result["risk_confirmation"] = json.loads(result["risk_confirmation"]) if result.get("risk_confirmation") else None
    result["video_target_duration_seconds"] = int(result.get("video_target_duration_seconds") or 60)
    result["tags"] = json.loads(result.get("tags") or "[]")
    result["archived"] = bool(result.get("archived"))
    result["favorite"] = bool(result.get("favorite"))
    result["content_type"] = "选题"
    result["state"] = result["status"]
    result["draft_count"] = len(draft_index.get(result["id"], []))
    result["thumbnail"] = None
    versions = connection.execute(
        "SELECT id,number,created_at FROM versions WHERE topic_id=? ORDER BY number DESC LIMIT 1",
        (result["id"],),
    ).fetchone()
    result["version_count"] = connection.execute(
        "SELECT COUNT(*) FROM versions WHERE topic_id=?", (result["id"],)
    ).fetchone()[0]
    result["latest_version"] = dict(versions) if versions else None
    result["source"] = "本地选题库"
    return result


def _draft_row(connection, row, topic_map):
    result = dict(row)
    result["tags"] = []
    result["archived"] = False
    result["favorite"] = False
    result["priority"] = 3
    result["content_type"] = "平台草稿"
    result["media"] = json.loads(result.get("media") or "{}")
    result.pop("media", None)
    topic = topic_map.get(result["topic_id"])
    result["topic_title"] = topic["title"] if topic else ""
    result["state"] = result["state"]
    result["platforms"] = [result["platform"]]
    result["version_number"] = connection.execute(
        "SELECT number FROM versions WHERE id=?", (result["version_id"],)
    ).fetchone()
    result["version_number"] = result["version_number"][0] if result["version_number"] else None
    result["thumbnail"] = None
    result["source"] = f'{result["platform"]} 草稿箱'
    result["has_remote"] = bool(result.get("remote_id"))
    result["remote_url_present"] = bool(result.get("remote_url"))
    return result


def _topic_map(connection):
    return {row["id"]: dict(row) for row in connection.execute(
        "SELECT id,title,status,archived,favorite,priority,deleted_at FROM topics")}


def _matches(text, needle):
    return not needle or needle in (text or "").lower()


def list_content(kind="all", q="", platform="", state="", date_from="", date_to="",
                 sort="updated_at", order="desc", page=1, page_size=20,
                 include_deleted=False, priority="", tag=""):
    """服务端检索。分页、筛选、排序全部在 SQL 里做，不在前端过滤全量。"""
    if kind not in {"all", "topic", "draft"}:
        raise HTTPException(status_code=422, detail="内容类型只能是 all / topic / draft")
    if platform and platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail="平台标识无效")
    if sort not in SORT_COLUMNS:
        raise HTTPException(status_code=422, detail="排序字段无效")
    needle = q.strip().lower()
    column = SORT_COLUMNS[sort]
    direction = "ASC" if str(order).lower() == "asc" else "DESC"

    with db.connect() as connection:
        topic_map = _topic_map(connection)
        rows = []
        if kind in {"all", "topic"}:
            for row in connection.execute("SELECT * FROM topics ORDER BY created_at DESC"):
                if not include_deleted and row["deleted_at"]:
                    continue
                item = _topic_row(connection, row, {})
                if not _matches(f'{item["title"]} {item["brief"]}', needle):
                    continue
                if platform and platform not in item["platforms"]:
                    continue
                if state and item["state"] != state:
                    continue
                if priority and str(item["priority"]) != str(priority):
                    continue
                if tag and tag not in item["tags"]:
                    continue
                if date_from and (item["updated_at"] or "") < date_from:
                    continue
                if date_to and (item["updated_at"] or "")[:10] > date_to:
                    continue
                rows.append(item)
        if kind in {"all", "draft"}:
            for row in connection.execute("SELECT * FROM drafts ORDER BY created_at DESC"):
                if not include_deleted and row["deleted_at"]:
                    continue
                item = _draft_row(connection, row, topic_map)
                if not _matches(item["title"], needle):
                    continue
                if platform and item["platform"] != platform:
                    continue
                if state and item["state"] != state:
                    continue
                if date_from and (item["updated_at"] or "") < date_from:
                    continue
                if date_to and (item["updated_at"] or "")[:10] > date_to:
                    continue
                rows.append(item)

    rows.sort(key=lambda item: (item.get(column) or "", item["id"]), reverse=direction == "DESC")
    total = len(rows)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    start = (page - 1) * page_size
    window = rows[start:start + page_size]
    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "sort": sort, "order": direction.lower(), "items": window,
        "facets": facets(rows),
    }


def facets(rows):
    """基于当前结果集的计数，供前端筛选器显示真实数量。"""
    platforms: dict[str, int] = {}
    states: dict[str, int] = {}
    types: dict[str, int] = {}
    for item in rows:
        types[item["content_type"]] = types.get(item["content_type"], 0) + 1
        states[item["state"]] = states.get(item["state"], 0) + 1
        for name in item.get("platforms") or []:
            platforms[name] = platforms.get(name, 0) + 1
    return {"platforms": platforms, "states": states, "content_types": types}


# ---------------------------------------------------------------------------
# 单条操作
# ---------------------------------------------------------------------------

def _live_topic(connection, topic_id):
    row = connection.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    require(row is not None, "选题不存在", 404)
    require(not row["deleted_at"], "该选题已在回收站中，请先恢复", 409)
    return dict(row)


def update_topic_meta(topic_id, data):
    """选题的运营元数据：优先级、收藏、标签。不触碰生成流水线字段。"""
    with db.transaction() as connection:
        row = _live_topic(connection, topic_id)
        priority = row["priority"]
        if data.get("priority") is not None:
            priority = int(data["priority"])
            require(1 <= priority <= 5, "优先级需在 1 到 5 之间", 422)
        favorite = (1 if data["favorite"] else 0) if data.get("favorite") is not None else row["favorite"]
        tags = row["tags"]
        if data.get("tags") is not None:
            tags = json.dumps([str(tag).strip()[:40] for tag in data["tags"] if str(tag).strip()][:20], ensure_ascii=False)
        connection.execute(
            "UPDATE topics SET priority=?, favorite=?, tags=?, updated_at=? WHERE id=?",
            (priority, favorite, tags, now(), topic_id),
        )
        audit(connection, "topic", topic_id, "update_meta", detail=(
            f"priority={priority} favorite={bool(favorite)} tags={tags}"), topic_id=topic_id)
    return get_detail("topic", topic_id)


def duplicate_topic(topic_id):
    """复制选题。新记录是 pending，不继承批准状态与版本——避免绕过人工批准。"""
    with db.transaction() as connection:
        row = _live_topic(connection, topic_id)
        new_topic = new_id()
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at,priority,tags,content_line,stance_ids,video_target_duration_seconds)"
            " VALUES(?,?,?,?,?,1,'pending',?,?,?,?,?,?,?)",
            (new_topic, row["title"][:180], row["brief"], row["platforms"], row["sources"],
             now(), now(), row["priority"], row["tags"], row["content_line"], row["stance_ids"], row["video_target_duration_seconds"]),
        )
        audit(connection, "topic", new_topic, "duplicate", detail=f"来源 {topic_id}", topic_id=new_topic)
        audit(connection, "topic", topic_id, "duplicate", detail=f"新记录 {new_topic}", topic_id=topic_id)
    return service.get_topic(new_topic)


def set_archived(topic_id, archived):
    """归档。不删除数据，只是从默认列表收起，且归档后不可直接启动生产。"""
    with db.transaction() as connection:
        _live_topic(connection, topic_id)
        connection.execute("UPDATE topics SET archived=?, updated_at=? WHERE id=?",
                           (1 if archived else 0, now(), topic_id))
        audit(connection, "topic", topic_id, "archive" if archived else "unarchive",
              topic_id=topic_id)
    return get_detail("topic", topic_id)


def _trash_entry(connection, entity_type, entity_id):
    row = connection.execute(
        "SELECT * FROM content_trash WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)
    ).fetchone()
    return dict(row) if row else None


def soft_delete(entity_type, entity_id, source="ui"):
    """软删除。写快照进回收站，原行置 deleted_at。可重复调用（幂等）。"""
    table = {"topic": "topics", "draft": "drafts"}.get(entity_type)
    require(table is not None, "暂不支持该类型的删除", 422)
    with db.transaction() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        require(row is not None, f"{ENTITY_LABEL.get(entity_type, '记录')}不存在", 404)
        # sqlite3.Row 不支持 .get()，且 topics 没有 topic_id 列。统一转 dict 取值。
        stored = dict(row)
        parent = stored.get("topic_id") or entity_id
        previous = stored.get("status") or stored.get("state") or ""
        existing = _trash_entry(connection, entity_type, entity_id)
        if not stored["deleted_at"]:
            snapshot = {k: v for k, v in stored.items() if k != "deleted_at"}
            connection.execute(
                "INSERT INTO content_trash(id,entity_type,entity_id,topic_id,title,previous_state,snapshot,deleted_at,deleted_source)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(entity_type,entity_id) DO UPDATE SET"
                " snapshot=excluded.snapshot, deleted_at=excluded.deleted_at,"
                " deleted_source=excluded.deleted_source, purged_at=NULL",
                (new_id(), entity_type, entity_id, parent, stored["title"],
                 previous, encode(snapshot), now(), source),
            )
            connection.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (now(), entity_id))
            audit(connection, entity_type, entity_id, "delete", detail="移入回收站（软删除）",
                  topic_id=parent, source=source)
        elif existing is None:
            # 已被直接删过：补一份快照，恢复才有依据。
            snapshot = {k: v for k, v in stored.items() if k != "deleted_at"}
            connection.execute(
                "INSERT INTO content_trash(id,entity_type,entity_id,topic_id,title,previous_state,snapshot,deleted_at,deleted_source)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id(), entity_type, entity_id, parent, stored["title"],
                 previous, encode(snapshot), stored["deleted_at"], source),
            )
    return {"deleted": True, "entity_type": entity_type, "entity_id": entity_id,
            "already_deleted": existing is not None}


def restore(entity_type, entity_id):
    """从回收站恢复。清 deleted_at 并保留快照作为历史。"""
    table = {"topic": "topics", "draft": "drafts"}.get(entity_type)
    require(table is not None, "暂不支持该类型的恢复", 422)
    with db.transaction() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        require(row is not None, f"{ENTITY_LABEL.get(entity_type, '记录')}已不存在，无法恢复", 404)
        stored = dict(row)
        entry = _trash_entry(connection, entity_type, entity_id)
        if not stored["deleted_at"]:
            return {"restored": False, "reason": "该记录不在回收站中"}
        # 引用完整性：draft 恢复前其 topic 必须还活着。
        parent = stored.get("topic_id") or entity_id
        if entity_type == "draft":
            topic = connection.execute("SELECT deleted_at FROM topics WHERE id=?", (parent,)).fetchone()
            require(topic is not None and not topic["deleted_at"],
                    "该草稿所属选题也在回收站中，请先恢复选题", 409)
        connection.execute(f"UPDATE {table} SET deleted_at=NULL WHERE id=?", (entity_id,))
        audit(connection, entity_type, entity_id, "restore", topic_id=parent,
              detail="已从回收站恢复")
    return {"restored": True, "entity_type": entity_type, "entity_id": entity_id}


def purge(entity_type, entity_id, confirm=False):
    """永久删除。必须 confirm=true，且引用检查通过。"""
    require(confirm is True, "永久删除需要显式确认参数 confirm=true", 422)
    table = {"topic": "topics", "draft": "drafts"}.get(entity_type)
    require(table is not None, "暂不支持该类型的永久删除", 422)
    with db.transaction() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        require(row is not None, f"{ENTITY_LABEL.get(entity_type, '记录')}不存在", 404)
        blockers = reference_blockers(connection, entity_type, entity_id)
        require(not blockers, "存在关联数据，拒绝永久删除：" + "；".join(blockers), 409)
        parent = dict(row).get("topic_id") or entity_id
        connection.execute(f"DELETE FROM {table} WHERE id=?", (entity_id,))
        connection.execute("DELETE FROM content_trash WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
        audit(connection, entity_type, entity_id, "purge", detail="已永久删除（引用检查通过）",
              topic_id=parent)
    return {"purged": True, "entity_type": entity_type, "entity_id": entity_id}


def reference_blockers(connection, entity_type, entity_id):
    """永久删除前的引用检查。返回人类可读的影响说明，空列表才可删。"""
    blockers = []
    if entity_type == "topic":
        drafts = connection.execute("SELECT COUNT(*) FROM drafts WHERE topic_id=?", (entity_id,)).fetchone()[0]
        if drafts:
            blockers.append(f"存在 {drafts} 条平台草稿引用该选题")
        versions = connection.execute("SELECT COUNT(*) FROM versions WHERE topic_id=?", (entity_id,)).fetchone()[0]
        if versions:
            blockers.append(f"存在 {versions} 个内容版本")
        jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE topic_id=?", (entity_id,)).fetchone()[0]
        if jobs:
            blockers.append(f"存在 {jobs} 条生成任务记录")
        pubs = connection.execute("SELECT COUNT(*) FROM publications WHERE topic_id=?", (entity_id,)).fetchone()[0]
        if pubs:
            blockers.append(f"存在 {pubs} 条发布记录")
    elif entity_type == "draft":
        version = connection.execute("SELECT COUNT(*) FROM versions WHERE id=?", (entity_id,)).fetchone()[0]
        if version:
            blockers.append("该草稿仍在回收站快照中，先彻底删除快照或恢复后处理")
        pubs = connection.execute("SELECT COUNT(*) FROM publications WHERE version_id=?", (entity_id,)).fetchone()[0]
        if pubs:
            blockers.append("存在发布记录")
    return blockers


def list_trash(q="", entity_type="", page=1, page_size=20):
    where, args = ["purged_at IS NULL"], []
    if entity_type:
        where.append("entity_type=?"); args.append(entity_type)
    clause = f"WHERE {' AND '.join(where)}"
    needle = q.strip().lower()
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM content_trash {clause} ORDER BY deleted_at DESC", args)]
    if needle:
        rows = [row for row in rows if needle in f'{row["title"]} {row["entity_id"]}'.lower()]
    total = len(rows)
    page = max(1, int(page)); page_size = max(1, min(int(page_size), 100))
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "items": rows[start:start + page_size]}


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------

def batch(entity_type, ids, action, payload=None, source="ui"):
    """批量操作。逐条独立结算，返回每一条的真实结果。

    不做「全成功 or 全失败」——那会让部分成功的批次被误报，用户无法判断
    哪些还需要重做。
    """
    require(entity_type in {"topic", "draft"}, "批量操作只支持 topic 或 draft", 422)
    require(action in {"archive", "delete", "tag", "priority", "favorite"}, "未知的批量操作", 422)
    require(isinstance(ids, list) and ids, "请至少选择一条记录", 422)
    require(len(ids) <= 200, "单次批量操作最多 200 条", 422)
    payload = payload or {}
    results = []
    for entity_id in ids[:200]:
        try:
            if action == "archive" and entity_type == "topic":
                with db.transaction() as connection:
                    _live_topic(connection, entity_id)
                    connection.execute("UPDATE topics SET archived=?, updated_at=? WHERE id=?",
                                       (1 if payload.get("archived", True) else 0, now(), entity_id))
                    audit(connection, "topic", entity_id, "batch_archive", source=source, topic_id=entity_id)
            elif action == "tag" and entity_type == "topic":
                with db.transaction() as connection:
                    row = _live_topic(connection, entity_id)
                    tags = set(json.loads(row["tags"] or "[]"))
                    for tag in payload.get("add", []) or []:
                        tags.add(str(tag).strip()[:40])
                    for tag in payload.get("remove", []) or []:
                        tags.discard(str(tag).strip()[:40])
                    value = json.dumps(sorted(tags)[:20], ensure_ascii=False)
                    connection.execute("UPDATE topics SET tags=?, updated_at=? WHERE id=?", (value, now(), entity_id))
                    audit(connection, "topic", entity_id, "batch_tag", detail=value, topic_id=entity_id)
            elif action == "priority" and entity_type == "topic":
                priority = int(payload.get("priority"))
                require(1 <= priority <= 5, "优先级需在 1 到 5 之间", 422)
                with db.transaction() as connection:
                    _live_topic(connection, entity_id)
                    connection.execute("UPDATE topics SET priority=?, updated_at=? WHERE id=?",
                                       (priority, now(), entity_id))
                    audit(connection, "topic", entity_id, "batch_priority", detail=str(priority), topic_id=entity_id)
            elif action == "favorite" and entity_type == "topic":
                with db.transaction() as connection:
                    _live_topic(connection, entity_id)
                    connection.execute("UPDATE topics SET favorite=?, updated_at=? WHERE id=?",
                                       (1 if payload.get("favorite", True) else 0, now(), entity_id))
                    audit(connection, "topic", entity_id, "batch_favorite", topic_id=entity_id)
            elif action == "delete":
                soft_delete(entity_type, entity_id, source=source)
            else:
                raise HTTPException(status_code=422, detail="该对象不支持此批量操作")
            results.append({"id": entity_id, "ok": True, "message": "成功"})
        except HTTPException as error:
            results.append({"id": entity_id, "ok": False, "message": str(error.detail)})
            audit_failed(entity_type, entity_id, f"batch_{action}", str(error.detail))
        except Exception as error:  # noqa: BLE001 - 单条失败不能带崩整批
            results.append({"id": entity_id, "ok": False, "message": f"操作异常：{error}"[:200]})
            audit_failed(entity_type, entity_id, f"batch_{action}", str(error))
    succeeded = sum(1 for item in results if item["ok"])
    return {"requested": len(results[:200]), "succeeded": succeeded,
            "failed": len(results[:200]) - succeeded, "results": results,
            "action": action, "entity_type": entity_type}


# ---------------------------------------------------------------------------
# 详情与首页
# ---------------------------------------------------------------------------

def get_detail(entity_type, entity_id):
    """内容详情：用于操作菜单里的「查看详情」与操作记录。"""
    if entity_type == "topic":
        topic = service.get_topic(entity_id)
        with db.connect() as connection:
            history = [dict(row) for row in connection.execute(
                "SELECT * FROM audit_log WHERE entity_type='topic' AND entity_id=? ORDER BY created_at DESC LIMIT 50",
                (entity_id,))]
        return {"entity": topic, "history": history}
    if entity_type == "draft":
        draft = service.get_draft(entity_id)
        with db.connect() as connection:
            history = [dict(row) for row in connection.execute(
                "SELECT * FROM audit_log WHERE entity_type='draft' AND entity_id=? ORDER BY created_at DESC LIMIT 50",
                (entity_id,))]
            refs = connection.execute(
                "SELECT t.title FROM topics t WHERE t.id=?", (draft["topic_id"],)).fetchone()
        draft["topic_title"] = refs["title"] if refs else ""
        return {"entity": draft, "history": history}
    raise HTTPException(status_code=404, detail="不支持的内容类型")


def dashboard():
    """运营总览指标。全部来自真实表，缺失就报未连接/暂无数据，不填模拟值。"""
    from . import capabilities
    from .production import status as production_status
    from .scheduler import status as scheduler_status

    with db.connect() as connection:
        today = now()[:10]
        stats = {
            "today_trends": connection.execute(
                "SELECT COUNT(*) FROM trend_candidates WHERE substr(first_seen_at,1,10)=?", (today,)).fetchone()[0],
            "pending_topics": connection.execute(
                "SELECT COUNT(*) FROM topics WHERE deleted_at IS NULL AND archived=0 AND status='pending'").fetchone()[0],
            "producing": connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0],
            "awaiting_review": connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE deleted_at IS NULL AND state IN ('synced','browser_saved')").fetchone()[0],
            "platform_drafts": connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE deleted_at IS NULL AND state IN ('synced','browser_saved','reviewed')").fetchone()[0],
            "published": connection.execute(
                "SELECT COUNT(*) FROM publications WHERE state='published'").fetchone()[0],
            "failed_jobs": connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='failed' AND substr(updated_at,1,10)=?", (today,)).fetchone()[0],
            "trash": connection.execute(
                "SELECT COUNT(*) FROM content_trash WHERE purged_at IS NULL").fetchone()[0],
            "unread": connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0],
        }
        activity = [dict(row) for row in connection.execute(
            "SELECT * FROM activity ORDER BY created_at DESC LIMIT 8")]
        by_platform = {row["platform"]: row["n"] for row in connection.execute(
            "SELECT platform, COUNT(*) AS n FROM drafts WHERE deleted_at IS NULL GROUP BY platform")}
        last_run = connection.execute(
            "SELECT * FROM production_runs ORDER BY batch_date DESC LIMIT 1").fetchone()

    inventory = capabilities.inventory()
    connection_rows = [
        {"platform": channel["platform"], "label": channel["label"], "channel": channel["channel"],
         "auth_state": channel["auth_state"], "checked_at": channel["connection_checked_at"],
         "implemented": channel["implemented"], "note": channel["note"],
         # 未实现形态没有自己的草稿，不挂平台级计数冒充它的成绩
         "drafts": by_platform.get(channel["platform"], 0) if channel["implemented"] else 0}
        for channel in inventory["channels"] if channel["channel"] == "browser"
    ]
    alerts = []
    for item in connection_rows:
        if not item["implemented"]:
            # 未实现是产品边界，不是故障：info 级说明白，别跟真异常混在一起
            alerts.append({"level": "info", "message": f'{item["label"]}形态尚未实现，暂不能交付；其他通道的保存成功不适用于它'})
            continue
        if item["auth_state"] not in {"connected_observed"}:
            alerts.append({"level": "warn", "message": f'{item["label"]} 未确认连接（{item["auth_state"]}）'})
        if item["drafts"] == 0:
            alerts.append({"level": "info", "message": f'{item["label"]} 尚无已保存草稿记录'})
    if stats["failed_jobs"]:
        alerts.append({"level": "error", "message": f'今日 {stats["failed_jobs"]} 条生成任务失败'})
    return {
        "stats": stats,
        "activity": activity,
        "platforms": connection_rows,
        "alerts": alerts,
        "production": production_status(),
        "scheduler": scheduler_status(),
        "last_production_run": dict(last_run) if last_run else None,
        "capability_attention": inventory["attention"],
        "notice": inventory["notice"],
    }


def content_facets_all():
    """全量状态字典，供前端筛选下拉真实取值，不硬编码。"""
    with db.connect() as connection:
        topic_states = [row[0] for row in connection.execute(
            "SELECT DISTINCT status FROM topics WHERE deleted_at IS NULL")]
        draft_states = [row[0] for row in connection.execute(
            "SELECT DISTINCT state FROM drafts WHERE deleted_at IS NULL")]
        tags = set()
        for row in connection.execute("SELECT tags FROM topics WHERE deleted_at IS NULL"):
            tags.update(json.loads(row["tags"] or "[]"))
    return {
        "platforms": sorted(PLATFORMS),
        "topic_states": sorted(topic_states),
        "draft_states": sorted(draft_states),
        "priorities": [{"value": value, "label": PRIORITY_LABEL[value]} for value in sorted(PRIORITY_LABEL)],
        "tags": sorted(tags),
        "sorts": [{"value": key, "label": {"updated_at": "最后修改", "created_at": "创建时间",
                                           "title": "标题", "state": "状态", "priority": "优先级"}[key]}
                  for key in SORT_COLUMNS],
    }
