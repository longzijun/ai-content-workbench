"""Versioned, approval-bound workflow with durable local recovery records."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import threading
import uuid
from urllib.parse import urlparse

from fastapi import HTTPException

from . import db, editorial_rules, material_context, platform_registry, video_script
from .config import get_settings
from .schemas import StoredPayload


PLATFORMS = platform_registry.PLATFORM_SET
_draft_locks: dict[str, threading.Lock] = {}
_draft_locks_guard = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def require(condition, detail, status=409):
    if not condition:
        raise HTTPException(status_code=status, detail=detail)


def _row(connection, table, entity_id):
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    require(row is not None, "记录不存在", 404)
    return dict(row)


def _activity(connection, topic_id, message):
    connection.execute("INSERT INTO activity(id,topic_id,message,created_at) VALUES(?,?,?,?)", (new_id(), topic_id, message, now()))


def _notice(connection, topic_id, title, body):
    connection.execute("INSERT INTO notifications(id,topic_id,title,body,created_at) VALUES(?,?,?,?,?)", (new_id(), topic_id, title, body, now()))


def _job(row):
    result = dict(row)
    result["cancel_requested"] = bool(result["cancel_requested"])
    result["paused"] = bool(result.get("paused"))
    result["events"] = json.loads(result["events"])
    result["candidate_mode"] = bool(json.loads(result.get("input_snapshot") or "{}").get("_candidate_mode"))
    result.pop("input_snapshot", None)
    return result


def _draft(row):
    result = dict(row)
    result["facts_confirmed"] = bool(result["facts_confirmed"])
    result['media'] = json.loads(result.get('media') or '{}')
    result["deleted_at"] = result.get("deleted_at")
    with db.connect() as delivery_connection:
        delivery_row = delivery_connection.execute('SELECT * FROM delivery_tasks WHERE draft_id=?', (result['id'],)).fetchone()
    result['delivery'] = dict(delivery_row) if delivery_row else None
    result['images'] = _asset_view(result['topic_id'], result['version_id'], result['platform'])
    return result


def _asset_view(topic_id, version_id, platform):
    from .image_assets import get_asset_manifest
    manifest = get_asset_manifest(get_settings(), topic_id, version_id, platform)
    result = {**manifest, 'assets': [{**asset, 'url': f'/api/versions/{version_id}/assets/{platform}/{asset["id"]}'} for asset in manifest.get('assets', [])]}
    candidates = manifest.get('candidates')
    if isinstance(candidates, dict):
        result['candidates'] = {index: [{**candidate, 'url': f'/api/versions/{version_id}/assets/{platform}/{candidate["id"]}'}
                                        for candidate in group if isinstance(candidate, dict) and candidate.get('id')]
                                for index, group in candidates.items() if isinstance(group, list)}
    return result


def _version(connection, row):
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    result["review"] = json.loads(result["review"]) if result["review"] else None
    result['image_assets'] = {w['platform']: _asset_view(result['topic_id'], result['id'], w['platform']) for w in result['payload']['works']}
    from .platform_source_policy import review_source_policy
    source_row = connection.execute('SELECT sources FROM topics WHERE id=?', (result['topic_id'],)).fetchone()
    sources = json.loads(source_row['sources']) if source_row else []
    from . import auto_materials
    material = auto_materials.version_materials(result["id"], connection)
    if material:
        sources = json.loads(_material_topic({"sources": sources}, material)["sources"])
    result['source_policy'] = {w['platform']: review_source_policy(w, sources, result['image_assets'][w['platform']].get('assets')) for w in result['payload']['works']}
    sync = connection.execute("SELECT result FROM sync_records WHERE topic_id=? AND version_id=?", (result["topic_id"], result["id"])).fetchone()
    result["sync"] = json.loads(sync["result"]) if sync else None
    return result


def _topic(connection, row):
    result = dict(row)
    result["platforms"] = json.loads(result["platforms"])
    result["sources"] = json.loads(result["sources"])
    result["stance_ids"] = json.loads(result.get("stance_ids") or "[]")
    result["risk_confirmation"] = json.loads(result["risk_confirmation"]) if result.get("risk_confirmation") else None
    result["required_risk_confirmations"] = editorial_rules.required_confirmations(result)
    result["video_target_duration_seconds"] = int(result.get("video_target_duration_seconds") or 60)
    # V2-18：软删除与运营元数据字段。这四列由 init_db 的 PRAGMA 守卫补齐，
    # 缺失时退回默认值，避免旧库在读路径上炸掉。
    result["tags"] = json.loads(result.get("tags") or "[]")
    result["archived"] = bool(result.get("archived"))
    result["favorite"] = bool(result.get("favorite"))
    result["priority"] = int(result.get("priority") or 3)
    result["deleted_at"] = result.get("deleted_at")
    result["versions"] = [_version(connection, item) for item in connection.execute("SELECT * FROM versions WHERE topic_id=? ORDER BY number DESC", (result["id"],))]
    result["current_version"] = next((version for version in result["versions"] if version["id"] == result["current_version_id"]), None)
    result["jobs"] = [_job(item) for item in connection.execute("SELECT * FROM jobs WHERE topic_id=? ORDER BY created_at DESC", (result["id"],))]
    return result


def get_topic(topic_id):
    with db.connect() as connection:
        return _topic(connection, _row(connection, "topics", topic_id))


def get_version(version_id):
    with db.connect() as connection:
        return _version(connection, _row(connection, "versions", version_id))


def get_job(job_id):
    with db.connect() as connection:
        return _job(_row(connection, "jobs", job_id))


def get_draft(draft_id):
    with db.connect() as connection:
        return _draft(_row(connection, "drafts", draft_id))


def _current(connection, version_id):
    version = _row(connection, "versions", version_id)
    topic = _row(connection, "topics", version["topic_id"])
    require(topic["current_version_id"] == version_id and topic["revision"] == version["input_revision"], "版本或选题范围已变化，请使用当前版本")
    require(topic["status"] == "approved", "选题当前尚未批准")
    return topic, version


def _invalidate(connection, topic_id, reason):
    connection.execute("UPDATE publications SET state='stale' WHERE topic_id=? AND state IN ('approved','exported')", (topic_id,))
    connection.execute("UPDATE drafts SET state='stale',error=?,updated_at=? WHERE topic_id=? AND state NOT IN ('published','stale')", (reason, now(), topic_id))


def create_topic(data):
    item_id, stamp = new_id(), now()
    require("crypto" not in editorial_rules.topic_risks(data), "币圈或虚拟货币类选题已阻断", 422)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,content_line,stance_ids,video_target_duration_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_id, data["title"].strip(), data.get("brief", ""), encode(data["platforms"]), encode(data.get("sources", [])),
             data.get("content_line"), encode(data.get("stance_ids") or []), data.get("video_target_duration_seconds", 60), stamp, stamp),
        )
        _activity(connection, item_id, "创建选题；等待人工批准生产范围")
        return _topic(connection, _row(connection, "topics", item_id))


def patch_topic(topic_id, data):
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        require(topic["revision"] == data["expected_revision"], "选题已被更新，请刷新后再修改")
        require("crypto" not in editorial_rules.topic_risks({**topic, **data}), "币圈或虚拟货币类选题已阻断", 422)
        changed = False
        for key in ("title", "brief", "platforms", "sources", "content_line", "stance_ids", "video_target_duration_seconds"):
            if key not in data or (data[key] is None and key != "content_line"):
                continue
            value = data[key]
            if key == "title":
                value = value.strip()
                require(bool(value), "选题标题不能为空", 422)
            if key == "platforms":
                require(len(value) == len(set(value)), "目标平台不能重复", 422)
            value = encode(value) if key in ("platforms", "sources", "stance_ids") else value
            if value != topic[key]:
                changed = True
                connection.execute(f"UPDATE topics SET {key}=? WHERE id=?", (value, topic_id))
        if changed:
            connection.execute("UPDATE topics SET revision=revision+1,status='pending',risk_confirmation=NULL,updated_at=? WHERE id=?", (now(), topic_id))
            connection.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE topic_id=? AND state IN ('queued','running')", (now(), topic_id))
            _invalidate(connection, topic_id, "选题范围修改，旧版本审批失效")
            _activity(connection, topic_id, "更新选题范围，撤销旧版本交付并等待重新批准")
        return _topic(connection, _row(connection, "topics", topic_id))


def _topic_for_rules(topic: dict) -> dict:
    topic = dict(topic)
    return {
        **topic,
        "platforms": json.loads(topic["platforms"]) if isinstance(topic.get("platforms"), str) else topic.get("platforms"),
        "sources": json.loads(topic["sources"]) if isinstance(topic.get("sources"), str) else topic.get("sources"),
        "stance_ids": json.loads(topic.get("stance_ids") or "[]") if isinstance(topic.get("stance_ids"), str) else (topic.get("stance_ids") or []),
        "risk_confirmation": json.loads(topic["risk_confirmation"]) if isinstance(topic.get("risk_confirmation"), str) and topic.get("risk_confirmation") else topic.get("risk_confirmation"),
    }


def _require_generation_ready(topic: dict):
    errors = editorial_rules.generation_errors(_topic_for_rules(topic))
    require(not errors, "；".join(errors), 409)


def _require_risk_confirmation(topic: dict):
    candidate = {**_topic_for_rules(topic), "content_line": "A", "stance_ids": []}
    errors = editorial_rules.generation_errors(candidate)
    require(not errors, "；".join(errors), 409)


def confirm_topic_risks(topic_id, data):
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        require(topic["revision"] == data["expected_revision"], "选题已修订，请刷新后重新确认风险", 409)
        candidate = _topic_for_rules(topic)
        require("crypto" not in editorial_rules.topic_risks(candidate), "币圈或虚拟货币类选题已阻断", 422)
        required = set(editorial_rules.required_confirmations(candidate))
        require(bool(required), "当前选题没有需要确认的金融或时事风险", 422)
        require(set(data["risks"]) == required, "确认项必须完整覆盖当前选题的金融/时事风险", 422)
        confirmation = {"revision": topic["revision"], "risks": sorted(required), "confirmed_at": now()}
        connection.execute("UPDATE topics SET risk_confirmation=?,updated_at=? WHERE id=?", (encode(confirmation), now(), topic_id))
        _activity(connection, topic_id, "人工确认当前修订的金融/时事风险：" + editorial_rules.risk_labels(required))
        return _topic(connection, _row(connection, "topics", topic_id))


def decide_topic(topic_id, data):
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        require(topic["revision"] == data["expected_revision"], "选题范围已变化，请重新查看后批准")
        decision = data["decision"]
        if decision == "approved":
            _require_risk_confirmation(topic)
            # 来源检查必须提前到批准入口。2026-10-22 两次真实撞墙：用户批准
            # 热榜候选 → 点「开始生产」→ 才被 409 拒绝，流程是倒的 —— 批准
            # 本身就该确认「这条选题具备可生产的材料」。与 generate 入口
            # 共用同一判据与文案，两处永不漂移。
            # topics 行的 sources 是 JSON 字符串；判据只认已解析的 list，
            # 直接传行会让 has_original_report 拿到 str 而误判通过。
            from .research import require_original_sources
            try:
                require_original_sources({'sources': json.loads(topic["sources"] or '[]')})
            except ValueError as error:
                require(False, str(error), 409)
        connection.execute("UPDATE topics SET status=?,updated_at=? WHERE id=?", (decision, now(), topic_id))
        if decision != "approved":
            connection.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE topic_id=? AND state IN ('queued','running')", (now(), topic_id))
            _invalidate(connection, topic_id, "选题批准已撤回")
        _activity(connection, topic_id, f"人工设置选题状态：{decision}（范围修订 {topic['revision']}）")
        return _topic(connection, _row(connection, "topics", topic_id))


def _create_job(connection, topic, resume_run=None, fact_base_text=None, candidate_mode=False):
    _require_generation_ready(topic)
    job_id, stamp = new_id(), now()
    snapshot = {key: topic[key] for key in ("id", "title", "brief", "revision", "status", "content_line")}
    snapshot["stance_ids"] = json.loads(topic.get("stance_ids") or "[]") if isinstance(topic.get("stance_ids"), str) else list(topic.get("stance_ids") or [])
    snapshot["platforms"] = json.loads(topic["platforms"])
    if any(platform_registry.is_video_platform(platform) for platform in snapshot["platforms"]):
        snapshot["video_target_duration_seconds"] = int(topic.get("video_target_duration_seconds") or 60)
    snapshot["sources"] = json.loads(topic["sources"])
    snapshot["_generation_run"] = resume_run or job_id
    if candidate_mode:
        snapshot["_candidate_mode"] = True
    # 共享事实底稿（V3）：渲染成纯文本进快照，local_generation 经 _prompt 的
    # _PROMPT_EXTRA_KEYS 白名单透传。没有底稿时不加这个键，prompt 逐字节不变。
    if fact_base_text:
        snapshot["_fact_base"] = fact_base_text
    # 形式决策必须进快照。local_generation 是从**这份快照**里读
    # `_format_decision` 的；不写进来的话，规则引擎与推荐器的结论会在建任务
    # 这一步被静默丢掉，生成出的仍然是没有任何形式约束的通用稿。
    # 没有采纳决策时**不加这个键** —— 空决策会让无决策路径的 prompt 变形。
    from . import format_decisions
    decision = format_decisions.snapshot(topic["id"], connection)
    if decision:
        snapshot[format_decisions.DECISION_KEY] = decision
    require(not connection.execute("SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic["id"],)).fetchone(), "该选题已有正在排队或运行的任务")
    connection.execute("INSERT INTO jobs(id,topic_id,state,stage,input_revision,input_snapshot,events,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, topic["id"], "queued", "等待执行", topic["revision"], encode(snapshot), encode([{"time": stamp, "message": "已排入自动创作队列"}]), stamp, stamp))
    _activity(connection, topic["id"], "创建自动创作任务")
    return _job(_row(connection, "jobs", job_id))


def _require_writable_sources(row):
    """建任务前确认来源里至少有一条原始报道。

    放在这里而不是只靠 worker 的 prepare_approved：worker 阶段拒绝会让用户
    先看到「执行失败」，再被要求去补来源 —— 而那是他点击生成时就该知道的。
    更糟的是它会诱使重复点「继续未完成的生成」，产生一串注定失败的 job。
    可预判的失败应当在入口拒绝，且不留下无效任务记录。

    只解析 sources 一列，不走 _topic —— 后者会加载全部版本与任务，对一次
    前置检查来说没必要。
    """
    from .research import require_original_sources
    try:
        require_original_sources({'sources': json.loads(row["sources"])})
    except ValueError as error:
        require(False, str(error), 409)


def generate(topic_id, expected_revision):
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        require(topic["revision"] == expected_revision, "选题范围已变化，请重新批准")
        require(topic["status"] == "approved", "请先人工批准选题范围")
        _require_writable_sources(topic)
        return _create_job(connection, topic)


def produce_topic(topic_id, platforms):
    """V3 三平台生产入口：一份共享事实底稿 → 一次发起三平台创作。

    与 generate 的区别：底稿必须存在且无缺口；投产即锁定底稿，底稿文本
    进模型快照。本地内容模式下产出只到本地作品，不排平台交付。
    """
    from . import fact_base
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        require(topic["status"] == "approved", "请先人工批准选题范围")
        approved_platforms = set(json.loads(topic["platforms"]))
        require(set(platforms) == approved_platforms, "生产平台必须与已批准平台一一对应", 422)
        _require_writable_sources(topic)
        locked = fact_base.lock(connection, topic_id)
        job = _create_job(connection, topic, fact_base_text=fact_base.prompt_text(locked))
        _activity(connection, topic_id, f"发起三平台生产（共享事实底稿修订 {locked['revision']} 已锁定）")
        return job


def cancel_job(job_id):
    with db.transaction() as connection:
        job = _row(connection, "jobs", job_id)
        require(job["state"] in ("queued", "running"), "任务已经结束")
        events = json.loads(job["events"]) + [{"time": now(), "message": "用户请求取消任务"}]
        connection.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,events=?,updated_at=? WHERE id=?", (encode(events), now(), job_id))
        return _job(_row(connection, "jobs", job_id))


def retry_job(job_id):
    with db.transaction() as connection:
        old = _row(connection, "jobs", job_id)
        old_snapshot = json.loads(old["input_snapshot"])
        candidate_mode = bool(old_snapshot.get("_candidate_mode"))
        require(old["state"] in (("failed", "cancelled", "succeeded") if candidate_mode else ("failed", "cancelled")), "只能继续已结束的任务")
        topic = _row(connection, "topics", old["topic_id"])
        require(topic["status"] == "approved", "请先重新批准选题")
        require(topic["revision"] == old["input_revision"], "原任务范围已变化，请从选题创建新任务")
        if candidate_mode:
            active = connection.execute("SELECT * FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic["id"],)).fetchone()
            if active:
                return _job(active)
            result = _create_job(connection, topic, candidate_mode=True)
            snapshot = json.loads(_row(connection, "jobs", result["id"])["input_snapshot"])
            snapshot["_candidate_parent_job"] = old["id"]
            connection.execute("UPDATE jobs SET input_snapshot=? WHERE id=?", (encode(snapshot), result["id"]))
            return result
        # Legacy generation keeps its original source requirements.
        _require_writable_sources(topic)
        old_snapshot = json.loads(old["input_snapshot"])
        run_key = old_snapshot.get("_generation_run") or old["id"]
        # 共享事实底稿必须跟着任务走。丢掉它等于换了一套材料：检查点身份变化，
        # 已完成平台续不上，还可能按「无底稿」的口径重写一遍。底稿已被修订时
        # 明确要求从选题重新发起生产 —— 不静默采用新底稿，也不静默退回无底稿。
        frozen = old_snapshot.get("_fact_base")
        if frozen:
            from . import fact_base
            current = fact_base.get(old["topic_id"])
            require(fact_base.prompt_text(current) == frozen if current else False,
                    "共享事实底稿已修订；重试沿用原任务锁定的底稿。若要采用新底稿，请从选题重新发起生产。", 409)
        return _create_job(connection, topic, run_key, fact_base_text=frozen)


def resume_paused_job(job_id):
    """恢复 V3 迁移暂停的未完成生成任务。只生成本地内容，不排平台交付。"""
    with db.transaction() as connection:
        job = _row(connection, "jobs", job_id)
        require(job["state"] == "queued", "只能恢复排队中的暂停任务", 409)
        require(bool(job["paused"]), "该任务未被暂停", 409)
        topic = _row(connection, "topics", job["topic_id"])
        require(topic["status"] == "approved", "请先重新批准选题")
        require(topic["revision"] == job["input_revision"], "原任务范围已变化，请从选题创建新任务")
        _require_generation_ready(topic)
        candidate = _topic_for_rules(topic)
        old_snapshot = json.loads(job["input_snapshot"])
        require(old_snapshot.get("content_line") == candidate.get("content_line") and (old_snapshot.get("stance_ids") or []) == candidate.get("stance_ids"),
                "历史任务快照缺少或已改变内容线/观点；请明确选择后重新发起生成", 409)
        connection.execute("UPDATE jobs SET paused=0,updated_at=? WHERE id=?", (now(), job_id))
        _activity(connection, topic["id"], "恢复暂停的生成任务；本地内容模式只生成本地作品")
        return _job(_row(connection, "jobs", job_id))


def claim_next_job(owner_id=None):
    with db.transaction() as connection:
        row = connection.execute("SELECT j.* FROM jobs j LEFT JOIN job_retry_backoff b ON b.job_id=j.id "
                                 "WHERE j.state='queued' AND j.paused=0 AND (b.next_retry_at IS NULL OR b.next_retry_at<=?) "
                                 "ORDER BY j.created_at LIMIT 1", (now(),)).fetchone()
        if row is None:
            return None
        job = dict(row)
        topic = _row(connection, "topics", job["topic_id"])
        if job["cancel_requested"] or topic["status"] != "approved" or topic["revision"] != job["input_revision"]:
            connection.execute("UPDATE jobs SET state='cancelled',stage='范围已失效',updated_at=? WHERE id=?", (now(), job["id"]))
            return None
        connection.execute("UPDATE jobs SET state='running',stage='选择执行模型',updated_at=? WHERE id=? AND state='queued'", (now(), job["id"]))
        result = _job(_row(connection, "jobs", job["id"]))
        result["topic"] = json.loads(job["input_snapshot"])
        if owner_id:
            from . import execution_runtime as execution
            result["execution"] = execution.acquire_job(connection, job["id"], owner_id)
            result["topic"]["_execution_attempt"] = result["execution"]["attempt"]
        connection.execute("DELETE FROM job_retry_backoff WHERE job_id=?", (job['id'],))
        return result


def job_progress(job_id, stage, message, provider=None):
    with db.transaction() as connection:
        job = _row(connection, "jobs", job_id)
        if job["state"] != "running":
            return
        from .execution_runtime import require_job_owner
        require_job_owner(connection, job_id)
        events = (json.loads(job["events"]) + [{"time": now(), "message": str(message)}])[-200:]
        connection.execute("UPDATE jobs SET stage=?,provider=COALESCE(?,provider),events=?,updated_at=? WHERE id=? AND state='running'", (str(stage), provider, encode(events), now(), job_id))


def job_cancelled(job_id):
    with db.connect() as connection:
        row = connection.execute("SELECT j.state,j.cancel_requested,j.input_revision,t.revision,t.status FROM jobs j JOIN topics t ON t.id=j.topic_id WHERE j.id=?", (job_id,)).fetchone()
        from .execution_runtime import owns_job
        return not row or row["state"] != "running" or bool(row["cancel_requested"]) or row["status"] != "approved" or row["input_revision"] != row["revision"] or not owns_job(connection, job_id)


def recover_jobs(include_drafts=True):
    count = 0
    with db.transaction() as connection:
        for job in connection.execute("SELECT * FROM jobs WHERE state='running'").fetchall():
            lease = connection.execute("SELECT * FROM job_leases WHERE job_id=?", (job['id'],)).fetchone()
            if lease and lease['lease_expires_at'] > now():
                continue
            if lease:
                topic = _row(connection, 'topics', job['topic_id'])
                cancelled = job['cancel_requested'] or topic['status'] != 'approved' or topic['revision'] != job['input_revision']
                can_retry = not cancelled and not job['paused'] and lease['attempt'] < 3
                state = 'cancelled' if cancelled else ('queued' if can_retry else 'failed')
                message = '执行器中断；已验证租约超时，等待续跑' if can_retry else '执行器多次中断，请检查后人工重试'
                connection.execute("UPDATE jobs SET state=?,stage=?,error=?,updated_at=? WHERE id=?", (state, message, None if can_retry else message, now(), job['id']))
                count += 1
                continue
            state = "cancelled" if job["cancel_requested"] else "failed"
            connection.execute("UPDATE jobs SET state=?,stage='运行中断',error=?,updated_at=? WHERE id=?", (state, "工作台上次运行中断；可人工重试，不会重复交付", now(), job["id"]))
            _notice(connection, job["topic_id"], "自动任务运行中断", "工作台重启后保留原任务记录；请查看任务并按需重试。")
            count += 1
        for draft in connection.execute("SELECT * FROM drafts WHERE state='syncing'").fetchall() if include_drafts else []:
            browser_local = draft["destination"] == "browser"
            message = "保存过程被中断，请在本机专用 Edge 核对小红书浏览器草稿，避免重复创建" if browser_local else "同步过程被中断，平台可能已保存；请核对平台草稿箱，避免重复创建"
            connection.execute("UPDATE drafts SET state='unknown',error=?,updated_at=? WHERE id=?", (message, now(), draft["id"]))
            _notice(connection, draft["topic_id"], "浏览器草稿保存结果待核对" if browser_local else "平台草稿同步结果待核对", message + "。不会自动重复提交。")
    return count


def fail_job(job_id, error, provider=None):
    with db.transaction() as connection:
        job = _row(connection, "jobs", job_id)
        if job["state"] not in ("running", "queued"):
            return _job(job)
        from .execution_runtime import require_job_owner
        require_job_owner(connection, job_id)
        topic = _row(connection, "topics", job["topic_id"])
        cancelled = job["cancel_requested"] or topic["status"] != "approved" or topic["revision"] != job["input_revision"]
        state = "cancelled" if cancelled else "failed"
        message = str(error)[:4000]
        events = json.loads(job["events"]) + [{"time": now(), "message": "任务已取消" if cancelled else f"任务失败：{message}"}]
        connection.execute("UPDATE jobs SET state=?,stage=?,error=?,provider=COALESCE(?,provider),events=?,updated_at=? WHERE id=?", (state, "已取消" if cancelled else "执行失败", None if cancelled else message, provider, encode(events), now(), job_id))
        if not cancelled:
            if json.loads(job["input_snapshot"]).get("_candidate_mode"):
                stage = "创作暂时中断，已有工作稿和资料已保留，可继续编辑或恢复创作"
                connection.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, job_id))
                _notice(connection, job["topic_id"], "创作暂时中断，可继续", message + "；" + stage)
            else:
                _notice(connection, job["topic_id"], "自动创作未完成", message)
        return _job(_row(connection, "jobs", job_id))


def defer_transient_job(job_id, error):
    """Bounded retry for transport failures only; caller classifies the error."""
    from datetime import timedelta
    from . import execution_runtime as execution
    with db.transaction() as connection:
        job = _row(connection, 'jobs', job_id)
        execution.require_job_owner(connection, job_id)
        lease = connection.execute('SELECT * FROM job_leases WHERE job_id=?', (job_id,)).fetchone()
        topic = _row(connection, 'topics', job['topic_id'])
        if not lease or lease['attempt'] >= 3 or job['cancel_requested'] or topic['status'] != 'approved' or topic['revision'] != job['input_revision']:
            return False
        due = (datetime.now(timezone.utc) + timedelta(seconds=5 * 2 ** (lease['attempt'] - 1))).isoformat()
        connection.execute("UPDATE jobs SET state='queued',stage='连接暂时失败，等待有限自动重试',error=?,updated_at=? WHERE id=?", (str(error)[:1200], now(), job_id))
        connection.execute('INSERT INTO job_retry_backoff VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET next_retry_at=excluded.next_retry_at', (job_id, due))
        return True


def _material_topic(topic, material):
    if not material:
        return topic
    result = dict(topic)
    sources = json.loads(topic["sources"]) if isinstance(topic.get("sources"), str) else list(topic.get("sources") or [])
    known = {item.get("url") for item in sources}
    titles = {item.get("url"): item.get("title", "") for item in material.get("documents", [])}
    for url in material.get("evidence_urls", []):
        if url not in known:
            sources.append({"url": url, "title": titles.get(url, "本篇已读取材料"), "platform": "external"})
            known.add(url)
    result["sources"] = encode(sources)
    return result


def _rule_review(topic, payload, version_id):
    topic = dict(topic)
    from . import auto_materials
    material = topic.get("_auto_materials") or auto_materials.version_materials(version_id)
    topic = _material_topic(topic, material)
    findings = []
    expected = set(json.loads(topic["platforms"]))
    works = payload["works"]
    actual = [work["platform"] for work in works]
    if set(actual) != expected or len(actual) != len(set(actual)):
        findings.append({"severity": "blocker", "message": "各目标平台必须且只能有一份作品，平台范围须匹配已批准选题。"})
    source_urls = {source["url"] for source in json.loads(topic["sources"])}
    if not source_urls:
        findings.append({"severity": "warning", "message": "未提供来源；涉及功能、价格、数据或新闻时请补充并人工核实。"})
    rules_topic = _topic_for_rules(topic)
    from . import humanize_lint
    from .platform_rules import work_errors
    for work in works:
        platform = work["platform"]
        is_video = video_script.is_video_work(work)
        title, body = work["title"].strip(), work["body"].strip()
        lint_text = video_script.validation_text(work) if is_video else body
        if is_video:
            findings.extend({'severity': 'blocker', 'platform': platform, 'message': message}
                            for message in video_script.reference_errors(work, rules_topic))
        if not title or not body:
            findings.append({"severity": "blocker", "platform": platform, "message": "标题与正文不能为空。"})
        if re.search(r"\bTODO\b|\bTBD\b|占位(?:符|文本)|INSERT[_ ]HERE"
                     r"|【[^】]{0,24}(?:待核实|待补充|待填|占位)[^】]{0,24}】"
                     r"|[（(][^）)]{0,24}(?:待核实|待补充|待填|占位)[^）)]{0,24}[）)]",
                     title + "\n" + lint_text, re.I):
            findings.append({"severity": "blocker", "platform": platform, "message": "正文仍有占位或待核实内容，请编辑后重新审核。"})
        for url in work.get("source_urls", []):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                findings.append({"severity": "blocker", "platform": platform, "message": "引用链接格式无效。"})
            elif url not in source_urls:
                findings.append({"severity": "blocker", "platform": platform, "message": f"引用未纳入已批准选题来源：{url}"})
        for message in work_errors(work):
            findings.append({"severity": "blocker", "platform": platform, "message": message})
        violations, advisories = humanize_lint.check(lint_text, work.get("format", "article"))
        findings.extend({"severity": "blocker", "platform": platform, "message": message} for message in violations)
        findings.extend({"severity": "warning", "platform": platform, "message": message} for message in advisories)
        for finding in editorial_rules.text_findings(title + "\n" + lint_text, rules_topic):
            findings.append({**finding, "platform": platform})
    findings.append({"severity": "warning", "message": "规则检查无法确认事实真实性、版权或账号权限；人工确认事实后才可批准发布。"})
    return {"status": "blocked" if any(item["severity"] == "blocker" for item in findings) else "passed", "findings": findings, "created_at": now(), "version_id": version_id}


def _insert_version(connection, topic, payload, provider, material_snapshot=None):
    topic = dict(topic)
    if material_snapshot is not None:
        topic = {**_material_topic(topic, material_snapshot), "_auto_materials": material_snapshot}
    payload = StoredPayload.model_validate(video_script.normalize_payload(payload, _topic_for_rules(topic))).model_dump()
    platforms = [work["platform"] for work in payload["works"]]
    require(len(platforms) == len(set(platforms)) and set(platforms) == set(json.loads(topic["platforms"])), "作品必须与已批准平台一一匹配，每个平台一份", 422)
    version_id, stamp = new_id(), now()
    number = connection.execute("SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE topic_id=?", (topic["id"],)).fetchone()[0]
    review = _rule_review(topic, payload, version_id)
    blockers = [item["message"] for item in review["findings"] if item["severity"] == "blocker"]
    require(not blockers, "统一检查未通过，正式版本未冻结：" + "；".join(blockers), 422)
    _invalidate(connection, topic["id"], "已有新版本，旧草稿需重新审核")
    connection.execute("INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,review,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (version_id, topic["id"], number, topic["revision"], encode(payload), digest(payload), provider, encode(review), stamp))
    connection.execute("UPDATE topics SET current_version_id=?,updated_at=? WHERE id=?", (version_id, stamp, topic["id"]))
    # V3 本地内容模式：新作品只落工作台（destination=workbench），不排平台交付。
    from . import runtime_mode
    local_only = not runtime_mode.platform_operations_allowed()
    destination = 'workbench' if local_only else get_settings().draft_destination
    draft_ids = []
    for work in payload["works"]:
        if work["platform"] not in json.loads(topic["platforms"]):
            continue
        if connection.execute("SELECT 1 FROM drafts WHERE version_id=? AND platform=?", (version_id, work["platform"])).fetchone():
            continue
        draft_id = new_id()
        state = "blocked" if review["status"] == "blocked" else "local"
        work_destination = "workbench" if platform_registry.is_video_platform(work["platform"]) else destination
        connection.execute("INSERT INTO drafts(id,topic_id,version_id,platform,title,state,destination,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (draft_id, topic["id"], version_id, work["platform"], work["title"], state, work_destination, "规则审核有阻断项，未同步平台" if state == "blocked" else None, stamp, stamp))
        draft_ids.append(draft_id)
    _activity(connection, topic["id"], f"生成冻结版本 {number}；已建立各平台本地作品" if local_only else f"生成冻结版本 {number}；已建立各平台草稿任务")
    if review["status"] == "blocked":
        _notice(connection, topic["id"], "草稿需修改后才能同步", "新版本存在规则阻断项，已保存在工作台；请打开正文修订。尚未存入各平台后台。")
    elif local_only:
        _notice(connection, topic["id"], "各平台作品已生成", "已建立各平台本地作品，请在集中审阅中核对事实、正文或视频脚本；发布由你在各平台官方应用人工完成。")
    else:
        _notice(connection, topic["id"], "内容生成完成，正在处理草稿箱", "已建立平台草稿任务。草稿列表会显示实际同步结果或未存入原因，审核后由你在平台发布。")
    return version_id, draft_ids


def _sync_created(draft_ids):
    for draft_id in draft_ids:
        try:
            draft = get_draft(draft_id)
            if platform_registry.is_video_platform(draft["platform"]):
                continue
            if draft["state"] != "blocked":
                sync_draft(draft_id)
        except Exception as error:
            with db.transaction() as connection:
                draft = _row(connection, "drafts", draft_id)
                if draft["state"] in ("stale", "published", "synced", "browser_saved", "reviewed", "blocked"):
                    continue
                if draft["state"] in ("syncing", "unknown") or draft["remote_id"]:
                    connection.execute("UPDATE drafts SET state='unknown',error=COALESCE(NULLIF(error,''),?),updated_at=? WHERE id=?", ("平台同步发生中断，保存结果待核对；请先检查平台草稿箱，避免重复创建", now(), draft_id))
                    _notice(connection, draft["topic_id"], "平台草稿同步结果待核对", "同步过程发生中断，平台可能已经保存；保留已知草稿标识。请核对平台草稿箱，不会自动重复提交。")
                else:
                    # No durable claim or remote evidence exists: failure preceded platform I/O.
                    connection.execute("UPDATE drafts SET state='failed',error=?,updated_at=? WHERE id=?", (str(error)[:2000], now(), draft_id))
                    _notice(connection, draft["topic_id"], "草稿同步前检查未完成", "正文已保存在工作台，尚未向平台提交；请查看错误后重试。")


def complete_job(job_id, payload, provider, defer_delivery=False):
    draft_ids = []
    with db.transaction() as connection:
        job = _row(connection, "jobs", job_id)
        require(job["state"] == "running", "任务不是运行状态")
        from .execution_runtime import require_job_owner
        require_job_owner(connection, job_id)
        topic = _row(connection, "topics", job["topic_id"])
        if job["cancel_requested"] or topic["status"] != "approved" or topic["revision"] != job["input_revision"]:
            connection.execute("UPDATE jobs SET state='cancelled',stage='已取消，丢弃未交付结果',updated_at=? WHERE id=?", (now(), job_id))
            return None
        version_id, draft_ids = _insert_version(connection, topic, payload, provider)
        from . import runtime_mode
        local_only = not runtime_mode.platform_operations_allowed()
        events = json.loads(job["events"]) + [{"time": now(), "message": "创作完成；冻结版本并建立本地作品" if local_only else "创作完成；冻结版本并建立平台草稿"}]
        connection.execute("UPDATE jobs SET state='succeeded',stage='正文已生成，正在准备图片与草稿',provider=?,output_version_id=?,events=?,updated_at=? WHERE id=?", (provider, version_id, encode(events), now(), job_id))
        if defer_delivery and not local_only:
            from .delivery import enqueue_in
            enqueue_in(connection, [draft_id for draft_id in draft_ids
                                    if not platform_registry.is_video_platform(_row(connection, "drafts", draft_id)["platform"])])
    _register_source_images(topic, version_id, payload)
    from . import runtime_mode as _mode
    if not defer_delivery and _mode.platform_operations_allowed():
        _sync_created(draft_ids)
    update_delivery_summary(version_id)
    return get_version(version_id)


def update_delivery_summary(version_id):
    from . import runtime_mode
    local_only = not runtime_mode.platform_operations_allowed()
    with db.transaction() as connection:
        drafts = [dict(r) for r in connection.execute('SELECT * FROM drafts WHERE version_id=?', (version_id,))]
        if local_only:
            # 本地内容模式：完成判据是「作品就绪待审」，与平台保存无关。
            ready = drafts and all(d['state'] in ('local', 'blocked') for d in drafts)
            stage = '三平台作品已生成，等待集中审阅' if ready else '作品准备中'
        else:
            complete = drafts and all(d['state'] in ('synced','browser_saved','reviewed','published') and json.loads(d.get('media') or '{}').get('status') in ('verified','not_required') for d in drafts)
            stage = '正文与图片已保存，等待人工审核' if complete else '正文已生成，图片或平台草稿待处理'
        connection.execute("UPDATE jobs SET stage=?,updated_at=? WHERE output_version_id=? AND state='succeeded'", (stage, now(), version_id))


def _register_source_images(topic, version_id, payload):
    """版本冻结钩子：把已抓取的源生图注册为版本资产。

    用户 2026-10-20 决策：源生图自动进各平台草稿箱，替换与否由用户决定。
    任何失败都不阻断正文交付主链路（无图交付由媒体校验按 not_required 兜底）。
    """
    try:
        works = payload.get('works') if isinstance(payload, dict) else None
        if isinstance(works, list) and works:
            from .source_images import register_for_works
            text_works = [work for work in works if not video_script.is_video_work(work)]
            if text_works:
                register_for_works(get_settings(), dict(topic), version_id, text_works)
    except Exception as error:
        logging.getLogger(__name__).warning('源生图注册钩子异常（不阻断主链路）: %s', error)


def create_version(topic_id, payload, base_version_id, provider="manual", defer_delivery=False):
    with db.transaction() as connection:
        topic, _ = _current(connection, base_version_id)
        require(topic["id"] == topic_id, "基础版本不属于此选题")
        _require_generation_ready(topic)
        require(not connection.execute("SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic_id,)).fetchone(), "创作任务运行中，请完成或取消后编辑版本")
        from . import auto_materials
        materials = auto_materials.version_materials(base_version_id, connection)
        if materials is not None:
            issues = auto_materials.freeze_gaps(materials, payload)
            require(not issues, "定稿还有问题：" + "；".join(issues), 422)
        version_id, draft_ids = _insert_version(connection, topic, payload, provider, material_snapshot=materials)
        auto_materials.inherit_version(connection, version_id, base_version_id)
        from . import runtime_mode
        if defer_delivery and runtime_mode.platform_operations_allowed():
            from .delivery import enqueue_in
            enqueue_in(connection, [draft_id for draft_id in draft_ids
                                    if not platform_registry.is_video_platform(_row(connection, "drafts", draft_id)["platform"])])
    _register_source_images(topic, version_id, payload)
    from . import runtime_mode as _mode
    if not defer_delivery and _mode.platform_operations_allowed():
        _sync_created(draft_ids)
    return get_version(version_id)


def review_version(version_id):
    with db.transaction() as connection:
        topic, version = _current(connection, version_id)
        review = _rule_review(topic, json.loads(version["payload"]), version_id)
        connection.execute("UPDATE versions SET review=? WHERE id=?", (encode(review), version_id))
        _activity(connection, topic["id"], f"执行当前版本规则审核：{review['status']}")
    # 排版审计在写事务之外跑：它要读模板表、写 layout_audits，而上面的事务
    # 还持着写锁。审计失败不得让审核失败 —— 审核是红线，排版检查是辅助信息。
    try:
        from . import layout_flow
        layout_flow.run_for_version(version_id)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('排版审计未完成（不阻断审核）：%s', error)
    return review


def _approved_version(connection, version_id, facts_confirmed):
    topic, version = _current(connection, version_id)
    review = json.loads(version["review"]) if version["review"] else None
    require(review and review["status"] == "passed", "请先通过当前版本规则审核")
    require(facts_confirmed is True, "请人工确认事实与引用", 422)
    require(version["hash"] == digest(json.loads(version["payload"])), "冻结内容校验失败")
    return topic, version


def _work(version, platform):
    payload = json.loads(version["payload"]) if isinstance(version["payload"], str) else version["payload"]
    works = [work for work in payload["works"] if work["platform"] == platform]
    require(len(works) == 1, "当前版本没有该平台的唯一作品", 422)
    return works[0]


def plan_version_images(version_id, platform):
    """半自动视觉策划：只产出提案，不修改版本与资产，由用户确认后另建版本。"""
    from . import visual_planner
    version = get_version(version_id)
    work = _work(version, platform)
    return visual_planner.plan_image_tasks(get_settings(), platform, work['title'], work['body'])


def _regenerate_version_asset(version_id, platform, index):
    """单图重生成：重排队指定位并同步执行；与 sync_draft 的同步出图语义一致。"""
    from .image_assets import generate_work_assets, regenerate_asset
    version = get_version(version_id)
    with db.connect() as connection:
        topic = dict(_row(connection, 'topics', version['topic_id']))
    settings = get_settings()
    manifest = regenerate_asset(settings, version['topic_id'], version_id, platform, index)
    if manifest['status'] == 'generating':
        generate_work_assets(settings, topic, version_id, _work(version, platform))
    return _asset_view(version['topic_id'], version_id, platform)


def regenerate_version_asset(version_id, platform, index):
    from .heartbeat import busy_operation
    with busy_operation():
        return _regenerate_version_asset(version_id, platform, index)


def select_version_asset(version_id, platform, index, asset_id):
    """候选版本切换：切换后立即整册复验，失败即 corrupt 呈现。"""
    from .image_assets import select_asset
    version = get_version(version_id)
    select_asset(get_settings(), version['topic_id'], version_id, platform, index, asset_id)
    return _asset_view(version['topic_id'], version_id, platform)


def _date(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "时间需包含时区", 422)
        return stamp.astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="时间格式无效，请使用带时区的 ISO 时间")


def approve_version(version_id, data):
    scheduled = _date(data["scheduled_at"])
    instant = datetime.now(timezone.utc)
    require(scheduled >= instant - timedelta(minutes=5), "发布时间不能早于当前时间", 422)
    require(scheduled <= instant + timedelta(hours=24), "本次批准仅适用于未来 24 小时内的发布时间", 422)
    require(bool(data["account_label"].strip()) and bool(data["ai_disclosure"].strip()), "请填写账号与 AI 内容标识", 422)
    with db.transaction() as connection:
        topic, version = _approved_version(connection, version_id, data["facts_confirmed"])
        _work(version, data["platform"])
        publication_id = new_id()
        connection.execute("INSERT INTO publications(id,topic_id,version_id,platform,account_label,ai_disclosure,scheduled_at,expires_at,payload_hash,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (publication_id, topic["id"], version_id, data["platform"], data["account_label"].strip(), data["ai_disclosure"].strip(), scheduled.isoformat(), (instant + timedelta(hours=24)).isoformat(), version["hash"], "approved", now()))
        _activity(connection, topic["id"], f"人工批准版本 {version['number']} 的 {data['platform']} 发布意向；等待人工发布")
        return _row(connection, "publications", publication_id)


def revoke_publication(publication_id):
    with db.transaction() as connection:
        publication = _row(connection, "publications", publication_id)
        require(publication["state"] in ("approved", "exported"), "只能撤回尚未发布的有效批准")
        connection.execute("UPDATE publications SET state='revoked' WHERE id=?", (publication_id,))
        return _row(connection, "publications", publication_id)


def _valid_publication(connection, publication_id):
    publication = _row(connection, "publications", publication_id)
    require(publication["state"] in ("approved", "exported"), "该发布批准已失效或已经记录发布")
    topic, version = _current(connection, publication["version_id"])
    require(publication["payload_hash"] == version["hash"], "批准内容与版本不匹配")
    require(_date(publication["expires_at"]) > datetime.now(timezone.utc), "发布批准已超过 24 小时有效期，请重新审批")
    return publication, _version(connection, version), _topic(connection, topic)


def export_publication(publication_id):
    from . import artifacts
    with db.transaction() as connection:
        publication, version, topic = _valid_publication(connection, publication_id)
        path = artifacts.export_publication(publication, version, topic, get_settings().runtime_dir / "exports")
        connection.execute("UPDATE publications SET state='exported' WHERE id=?", (publication_id,))
        _activity(connection, topic["id"], "创建批准版本的可选离线备份；未记录发布")
        return path


def record_publication(publication_id, data):
    require(data.get("confirmation") is True, "需明确确认已经人工发布", 422)
    published = _date(data["published_at"])
    require(published <= datetime.now(timezone.utc) + timedelta(minutes=5), "不能记录未来的已发布时间", 422)
    with db.transaction() as connection:
        publication, version, topic = _valid_publication(connection, publication_id)
        connection.execute("UPDATE publications SET state='published',url=?,published_at=? WHERE id=?", (data["url"], published.isoformat(), publication_id))
        _activity(connection, topic["id"], "用户确认已人工发布，并登记链接")
        return _row(connection, "publications", publication_id)


def create_draft(version_id, data):
    with db.transaction() as connection:
        topic, version = _current(connection, version_id)
        work = _work(version, data["platform"])
        existing = connection.execute("SELECT * FROM drafts WHERE version_id=? AND platform=?", (version_id, data["platform"])).fetchone()
        if existing:
            draft_id = existing["id"]
            if data.get("account_label") and not existing["remote_id"] and existing["state"] not in ("syncing", "unknown", "published", "reviewed"):
                connection.execute("UPDATE drafts SET account_label=?,updated_at=? WHERE id=?", (data["account_label"].strip(), now(), draft_id))
        else:
            draft_id, stamp = new_id(), now()
            review = json.loads(version["review"]) if version["review"] else None
            state = "local" if review and review["status"] == "passed" else "blocked"
            from . import runtime_mode
            local_only = not runtime_mode.platform_operations_allowed() or platform_registry.is_video_platform(data["platform"])
            destination = "workbench" if local_only else get_settings().draft_destination
            connection.execute("INSERT INTO drafts(id,topic_id,version_id,platform,title,account_label,state,destination,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (draft_id, topic["id"], version_id, data["platform"], work["title"], data.get("account_label", "").strip(), state, destination, stamp, stamp))
    if platform_registry.is_video_platform(data["platform"]):
        return get_draft(draft_id)
    return sync_draft(draft_id) if get_draft(draft_id)["state"] not in ("blocked", "published", "reviewed") else get_draft(draft_id)


def _native_browser_id(value, platform):
    if not isinstance(value, str):
        return False
    if platform in ("wechat", "toutiao"):
        return bool(re.fullmatch(r"[1-9][0-9]{0,31}", value))
    if platform == "xiaohongshu":
        try:
            native = uuid.UUID(value)
            return str(native) == value.lower() and native.variant == uuid.RFC_4122 and native.version in range(1, 9)
        except (ValueError, AttributeError):
            return False
    return False


def _browser_id(value, platform):
    prefix = "browser:" + platform + ":"
    return isinstance(value, str) and value.startswith(prefix) and _native_browser_id(value[len(prefix):], platform)


def _recover_browser_id(connection, draft, version, settings):
    """Recover saved evidence after DB writeback failed; never claim a UI write."""
    from .browser_draft_state import BrowserDraftLedger, LedgerError, content_hash
    try:
        evidence = BrowserDraftLedger(settings.runtime_dir).load(draft["topic_id"], draft["version_id"], draft["platform"])
    except (LedgerError, ValueError):
        raise HTTPException(status_code=409, detail="草稿证明无法确认，请先核对原后台；不会重复创建")
    if not evidence or not evidence.get("remote_id"):
        return draft
    require(evidence["content_hash"] == content_hash(_work(version, draft["platform"])), "草稿证明内容与冻结版本不一致，请核对原草稿")
    require(not draft["account_label"] or draft["account_label"].strip() == evidence["account_label"], "草稿证明账号与原绑定账号不一致，请切回原账号核对")
    require(_native_browser_id(evidence["remote_id"], draft["platform"]), "草稿证明缺少确知的平台原生草稿ID，请先在原后台核对")
    remote_id = "browser:" + draft["platform"] + ":" + evidence["remote_id"]
    destination = "browser" if draft["platform"] == "xiaohongshu" else "platform"
    connection.execute("UPDATE drafts SET remote_id=?,account_label=?,destination=?,updated_at=? WHERE id=?", (remote_id, draft["account_label"] or evidence["account_label"], destination, now(), draft["id"]))
    return _row(connection, "drafts", draft["id"])


def sync_draft(draft_id, verify_media=False, cancelled=None, progress=None):
    from . import artifacts, runtime_mode
    from .delivery import DeliveryCancelled
    runtime_mode.require_platform_operations("平台草稿同步")
    stop_check = cancelled or (lambda: False)
    report = progress or (lambda stage, message: None)
    with _draft_locks_guard:
        lock = _draft_locks.setdefault(draft_id, threading.Lock())
    with lock:
        with db.transaction() as connection:
            draft = _row(connection, "drafts", draft_id)
            require(not platform_registry.is_video_platform(draft["platform"]), "视频脚本仅在本地准备，不支持平台自动交付", 403)
            topic, version = _current(connection, draft["version_id"])
            require(draft["state"] not in ("stale", "published"), "旧版本或已发布的草稿不可再次同步")
            require(draft["state"] != "syncing", "此草稿正在同步，请等待结果")
            if draft["state"] == "unknown" and not draft["remote_id"]:
                draft = _recover_browser_id(connection, draft, version, get_settings())
            require(draft["state"] != "unknown" or bool(draft["remote_id"]), "平台是否已保存尚不确定，请先核对平台草稿箱，避免重复创建")
            review = json.loads(version["review"]) if version["review"] else None
            require(review and review["status"] == "passed", "当前版本存在规则阻断项，不能存入平台草稿箱")
            if draft["state"] == "browser_saved":
                require(draft["destination"] == "browser" and draft["platform"] == "xiaohongshu" and _browser_id(draft["remote_id"], draft["platform"]), "浏览器草稿保存证明不完整，请核对原草稿")
            media_done = json.loads(draft.get('media') or '{}').get('status') == 'verified'
            if (not verify_media or draft['state']=='reviewed' or media_done) and draft["state"] in ("synced", "browser_saved", "reviewed") and (draft["remote_id"] or draft["destination"] == "workbench"):
                return _draft(draft)
            topic = _topic(connection, topic)
            version = _version(connection, version)
            connection.execute("UPDATE drafts SET state='syncing',error=NULL,updated_at=? WHERE id=?", (now(), draft_id))
        work = _work(version, draft['platform'])
        from .platform_rules import validate_work
        try:
            validate_work(work)
        except ValueError as error:
            with db.transaction() as connection:
                connection.execute("UPDATE drafts SET state='blocked',error=?,updated_at=? WHERE id=?", (str(error), now(), draft_id))
            return get_draft(draft_id)
        if stop_check():
            raise DeliveryCancelled()
        images = _asset_view(topic['id'], version['id'], draft['platform'])
        # 2026-10-20 决策（赵老师）：AI 生图彻底退出交付链路，配图只使用版本
        # 冻结时注册的源生图（research 抓取）；没有注册成功的平台按无图交付，
        # 不再触发 ComfyUI/AI 生成。work 上的 image_prompts/image_tasks 仅作
        # 旧版本兼容字段，不再驱动任何生成。
        needs_images = bool(images.get('assets')) or images.get('status') in ('corrupt', 'blocked')
        image_warning = None
        if draft["destination"] == "workbench":
            result = {"state": "local", "error": None}
        else:
            if stop_check():
                raise DeliveryCancelled()
            try:
                if needs_images and not draft.get('remote_id') and images['status'] in ('corrupt', 'blocked'):
                    # 资产完整性（文件被改动）或版本绑定（正文与请求不一致）问题仍拦截交付。
                    reason = '文件校验失败' if images['status'] == 'corrupt' else '请求与冻结版本正文不一致'
                    result = {'state': 'failed', 'error': '图片资产' + reason + '，未向平台创建草稿：' + str(images.get('error') or images['status'])}
                else:
                    if needs_images and not draft.get('remote_id') and images['status'] != 'ready':
                        # 2026-10-18 决策（赵老师）：出图失败不再阻断平台交付。
                        # 文字草稿照常进入平台草稿箱，素材以当前可用集合（可能为空）交付，
                        # 媒体状态保持待核对，由用户在平台人工补图或事后重试出图。
                        image_warning = '图片未生成完成（' + str(images.get('error') or images['status']) + '）；草稿按当前素材交付，可在平台人工补图'
                    report('platform', '正在保存或回读平台草稿，请等待核验结果')
                    if stop_check():
                        raise DeliveryCancelled()
                    result = artifacts.sync_platform_draft(_draft(draft), version, topic, get_settings())
            except DeliveryCancelled:
                raise
            except Exception:
                result = {"state": "unknown", "remote_id": draft.get("remote_id"), "remote_url": draft.get("remote_url"), "error": "平台同步发生中断，保存结果不确定；请核对平台草稿箱后再处理"}
            if not isinstance(result, dict):
                result = {"state": "unknown", "remote_id": draft.get("remote_id"), "error": "平台未返回有效确认，保存结果待核对；不会重复提交"}
        state = result.get("state", "failed")
        destination = result.get("destination", draft["destination"])
        if destination not in ("platform", "browser", "workbench"):
            destination = draft["destination"]
            state = "unknown"
            result["error"] = "草稿交付目的地未确认，请核对原草稿"
        if state not in ("local", "needs_connection", "synced", "browser_saved", "blocked", "failed", "unknown"):
            state = "failed"
            result["error"] = "平台返回未知的草稿状态"
        if destination in ("platform", "browser") and state == "local":
            state = "needs_connection"
            result["error"] = result.get("error") or "尚未存入平台，正文只保存在工作台"
        if state == "synced" and not result.get("remote_id"):
            state = "unknown"
            result["error"] = "平台没有返回真实草稿标识，无法确认已存入后台"
        if state == "synced" and (destination != "platform" or draft["platform"] == "xiaohongshu"):
            state = "unknown"
            result["error"] = "小红书网页草稿目前仅保存在本机专用 Edge，不能确认平台云端草稿同步" if draft["platform"] == "xiaohongshu" else "平台草稿交付目的地不匹配，请核对原草稿"
        if state == "browser_saved" and (destination != "browser" or draft["platform"] != "xiaohongshu" or not _browser_id(result.get("remote_id"), draft["platform"]) or not result.get("account_label")):
            state = "unknown"
            result["error"] = "本机浏览器草稿缺少真实标识、账号或保存证明，请在专用 Edge 核对"
        if draft["remote_id"] and result.get("remote_id") and draft["remote_id"] != result["remote_id"]:
            state = "unknown"
            result["remote_id"] = draft["remote_id"]
            result["error"] = "回读草稿标识与原记录不一致，保留原ID；不会创建或替换草稿"
        if draft["remote_id"] and draft["account_label"] and result.get("account_label") and draft["account_label"] != result["account_label"]:
            state = "unknown"
            result["account_label"] = draft["account_label"]
            result["error"] = "当前后台账号与原草稿绑定账号不一致，请切回原账号核对"
        with db.transaction() as connection:
            latest = _row(connection, "topics", draft["topic_id"])
            if latest["current_version_id"] != draft["version_id"] or latest["revision"] != version["input_revision"] or latest["status"] != "approved":
                state = "stale"
                result["error"] = "同步期间内容范围已变化，请审核新版本；原平台草稿保留为历史记录"
            connection.execute("UPDATE drafts SET state=?,destination=?,remote_id=COALESCE(?,remote_id),remote_url=COALESCE(?,remote_url),error=?,account_label=CASE WHEN ? IS NOT NULL THEN ? ELSE account_label END,updated_at=? WHERE id=?", (state, destination, result.get("remote_id"), result.get("remote_url"), result.get("error"), result.get("account_label"), result.get("account_label"), now(), draft_id))
            media = result.get('media') if isinstance(result.get('media'),dict) else None
            media = media or {'status':'not_required' if not images.get('assets') else 'unverified', 'expected_count':len(images.get('assets', [])), 'verified_count':0, 'cover_verified':False}
            expected_count = len(images.get('assets', []))
            valid_count = type(media.get('verified_count')) is int and media['verified_count'] == expected_count
            if media.get('status') == 'verified' and (images['status'] != 'ready' or expected_count == 0 or type(media.get('expected_count')) is not int or media.get('expected_count') != expected_count or not valid_count or media.get('cover_verified') is not True):
                media = {**media, 'status':'unverified', 'expected_count':expected_count, 'error':'平台图片或封面回读证据不完整，请核对'}
            if media.get('status') == 'not_required' and expected_count:
                media = {**media, 'status':'unverified', 'expected_count':expected_count, 'error':'该作品有图片要求，尚未确认交付'}
            if image_warning and media.get('status') != 'verified':
                media = {**media, 'status':'unverified', 'expected_count':expected_count, 'error':image_warning}
            if state not in ('synced','browser_saved','reviewed','published') and media.get('status') == 'verified':
                media = {**media, 'status':'unverified'}
            connection.execute('UPDATE drafts SET media=? WHERE id=?', (encode(media), draft_id))
            platform_name = {"wechat": "微信公众号", "toutiao": "今日头条", "xiaohongshu": "小红书"}[draft["platform"]]
            if state == "synced":
                body = "请打开平台草稿箱核对正文、图片、账号和 AI 标识，审核后由你发布。工作台没有自动发布。"
                if image_warning:
                    body = image_warning + "。可重试出图或人工补图后，再核对图片交付。" + body
                _notice(connection, draft["topic_id"], f"{platform_name}草稿已存入后台", body)
            elif state == "browser_saved":
                body = "草稿仅保存在本机专用 Edge 浏览器，不代表平台云端或手机端草稿。请在同一浏览器核对正文、图片、账号和 AI 标识，由你审核后发布。"
                if image_warning:
                    body = image_warning + "。可重试出图或人工补图后，再核对图片交付。" + body
                _notice(connection, draft["topic_id"], "小红书草稿已保存到专用 Edge", body)
            elif state == "needs_connection":
                _notice(connection, draft["topic_id"], f"{platform_name}草稿未存入平台", result.get("error") or "正文已保存在工作台，尚未存入平台后台。请按提示处理后重试保存。")
            elif state == "local":
                _notice(connection, draft["topic_id"], "工作台草稿待审核", "当前交付目的地设为工作台；草稿尚未进入平台后台。")
            else:
                _notice(connection, draft["topic_id"], f"{platform_name}草稿同步需处理", result.get("error") or "请查看草稿状态。")
            _activity(connection, draft["topic_id"], f"{platform_name}草稿同步结果：{state}")
            response = _draft(_row(connection, "drafts", draft_id))
        update_delivery_summary(draft['version_id'])
        if state in ("synced", "browser_saved"):
            _sync_obsidian_after_draft(draft["topic_id"], draft["version_id"])
        return response


def _sync_obsidian_after_draft(topic_id, version_id):
    """平台草稿保存成功后，把同一版本工作稿同步写入 Obsidian 库。"""
    try:
        result = sync_topic(topic_id, version_id)
    except Exception as error:
        with db.transaction() as connection:
            _notice(connection, topic_id, "Obsidian 工作稿同步待处理", "平台草稿已保存，但工作稿未写入 Obsidian 库；可在工作台点击「同步工作稿」重试：" + str(error)[:300])
        return None
    if isinstance(result, dict) and result.get("status") == "written":
        with db.transaction() as connection:
            _notice(connection, topic_id, "工作稿已同步到 Obsidian", "平台草稿保存成功后，同一版本工作稿已写入 Obsidian 库：" + str(result.get("path") or ""))
    return result


def open_draft(draft_id):
    """Open and verify a saved browser draft without editing or changing approval."""
    from . import browser_drafts, runtime_mode
    runtime_mode.require_platform_operations("打开平台已存草稿")
    with _draft_locks_guard:
        lock = _draft_locks.setdefault(draft_id, threading.Lock())
    with lock:
        with db.connect() as connection:
            draft = _row(connection, "drafts", draft_id)
            require(draft["state"] in ("synced", "browser_saved", "reviewed", "unknown", "stale"), "尚无可打开的已保存浏览器草稿")
            require(_browser_id(draft["remote_id"], draft["platform"]), "只有已确认浏览器草稿ID才能打开原草稿")
            version = _row(connection, "versions", draft["version_id"])
            require(version["topic_id"] == draft["topic_id"] and version["hash"] == digest(json.loads(version["payload"])), "冻结版本校验失败，不能打开草稿")
            _work(version, draft["platform"])
            topic = _topic(connection, _row(connection, "topics", draft["topic_id"]))
            version = _version(connection, version)
        try:
            result = browser_drafts.open_browser_draft(_draft(draft), version, topic, get_settings())
        except Exception:
            return {"state": "unknown", "error": "未能确认原浏览器草稿已打开，请在专用 Edge 核对；工作台没有修改正文"}
        if not isinstance(result, dict):
            return {"state": "unknown", "error": "后台未返回草稿打开证明，请在专用 Edge 核对"}
        if result.get("state") == "opened":
            account = result.get("account_label")
            if result.get("remote_id") != draft["remote_id"] or not account or (draft["account_label"] and account != draft["account_label"]) or result.get("content_verified") is not True:
                return {"state": "unknown", "error": "已打开页面但账号、草稿标识或正文回读尚未确认，请核对原草稿"}
        return result


def draft_content(draft_id):
    from . import body_export
    with db.connect() as connection:
        draft = _row(connection, "drafts", draft_id)
        version = _row(connection, "versions", draft["version_id"])
        work = _work(version, draft["platform"])
    export = body_export.build(work, draft["platform"], get_settings(), draft["topic_id"], version["id"])
    work["html"] = export["html"]
    work["text"] = export["text"]
    work["images_embedded"] = export["images"]
    work["export_warnings"] = export["warnings"]
    return {"work": work, "version_id": version["id"], "topic_id": draft["topic_id"]}


def review_draft(draft_id, data):
    require(bool(data["account_label"].strip()) and bool(data["ai_disclosure"].strip()), "请填写账号和 AI 内容标识", 422)
    with db.transaction() as connection:
        draft = _row(connection, "drafts", draft_id)
        topic, version = _approved_version(connection, draft["version_id"], data["facts_confirmed"])
        allowed = {"platform": ("synced", "reviewed"), "browser": ("browser_saved", "reviewed"), "workbench": ("local", "reviewed")}.get(draft["destination"], ())
        require(draft["state"] in allowed, "请先确认草稿已真实存入所选草稿箱")
        if draft["destination"] == "browser":
            require(draft["platform"] == "xiaohongshu" and _browser_id(draft["remote_id"], draft["platform"]), "本机浏览器草稿缺少真实保存标识")
        require(not draft["account_label"] or draft["account_label"] == data["account_label"].strip(), "审核账号须匹配该草稿已绑定的账号")
        connection.execute("UPDATE drafts SET state='reviewed',account_label=?,ai_disclosure=?,facts_confirmed=1,updated_at=? WHERE id=?", (data["account_label"].strip(), data["ai_disclosure"].strip(), now(), draft_id))
        _activity(connection, topic["id"], f"人工审核 {draft['platform']} 草稿，等待用户在平台发布")
        return _draft(_row(connection, "drafts", draft_id))


def record_draft(draft_id, data):
    require(data.get("confirmation") is True, "请明确确认已经人工发布", 422)
    published = _date(data["published_at"])
    require(published <= datetime.now(timezone.utc) + timedelta(minutes=5), "不能登记未来已发布时间", 422)
    with db.transaction() as connection:
        draft = _row(connection, "drafts", draft_id)
        topic, version = _approved_version(connection, draft["version_id"], bool(draft["facts_confirmed"]))
        require(draft["state"] == "reviewed", "请先人工审核当前草稿")
        require(bool(draft["account_label"]) and bool(draft["ai_disclosure"]), "审核信息不完整")
        publication_id = new_id()
        connection.execute("INSERT INTO publications(id,topic_id,version_id,platform,account_label,ai_disclosure,scheduled_at,expires_at,payload_hash,state,created_at,url,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (publication_id, topic["id"], version["id"], draft["platform"], draft["account_label"], draft["ai_disclosure"], published.isoformat(), published.isoformat(), version["hash"], "published", now(), data["url"], published.isoformat()))
        connection.execute("UPDATE drafts SET state='published',remote_url=?,updated_at=? WHERE id=?", (data["url"], now(), draft_id))
        _activity(connection, topic["id"], f"用户确认 {draft['platform']} 已人工发布并登记链接")
        return _draft(_row(connection, "drafts", draft_id))


def read_notification(notification_id):
    with db.transaction() as connection:
        _row(connection, "notifications", notification_id)
        connection.execute("UPDATE notifications SET read=1 WHERE id=?", (notification_id,))
        result = _row(connection, "notifications", notification_id)
        result["read"] = True
        return result


def sync_topic(topic_id, version_id):
    from . import artifacts
    with db.transaction() as connection:
        topic, version = _current(connection, version_id)
        require(topic["id"] == topic_id, "版本不属于此选题")
        baseline_row = connection.execute("SELECT baseline FROM sync_records WHERE topic_id=?", (topic_id,)).fetchone()
        baseline = baseline_row["baseline"] if baseline_row else None
        result = artifacts.sync_version(_topic(connection, topic), _version(connection, version), get_settings(), baseline)
        next_baseline = result.get("baseline", result.get("content_hash", baseline)) if result.get("status") == "written" else baseline
        connection.execute("INSERT INTO sync_records(topic_id,version_id,baseline,result,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET version_id=excluded.version_id,baseline=excluded.baseline,result=excluded.result,updated_at=excluded.updated_at", (topic_id, version_id, next_baseline, encode(result), now()))
        _activity(connection, topic_id, "同步工作稿到 Obsidian" if result.get("status") == "written" else "Obsidian 工作稿存在外部修改，保存冲突候选稿")
        return result


def import_topic(topic_id, base_version_id):
    from . import artifacts
    topic = get_topic(topic_id)
    require(topic["current_version_id"] == base_version_id, "基础版本已变化，请刷新后再导入")
    result = artifacts.import_working_copy(topic, get_settings())
    require(isinstance(result, dict) and isinstance(result.get("payload"), dict), "工作稿缺少有效的结构化内容", 422)
    source_version = result.get("base_version_id", result.get("source_version_id", result.get("version_id")))
    require(source_version is None or source_version == base_version_id, "工作稿来源版本与当前基础版本不一致，请先解决冲突")
    baseline = result.get("baseline")
    require(isinstance(baseline, str) and re.fullmatch(r"[0-9a-f]{64}", baseline), "工作稿缺少有效的导入快照校验值", 422)
    with db.transaction() as connection:
        current_topic, _ = _current(connection, base_version_id)
        require(current_topic["id"] == topic_id, "基础版本不属于此选题")
        require(not connection.execute("SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic_id,)).fetchone(), "创作任务运行中，请完成或取消后导入")
        version_id, draft_ids = _insert_version(connection, current_topic, result["payload"], "obsidian-import")
        previous = connection.execute("SELECT result FROM sync_records WHERE topic_id=?", (topic_id,)).fetchone()
        sync_result = {"status": "imported", "baseline": baseline, "message": "Obsidian 修改已导入为新版本，后续同步将继续检查外部变更。"}
        if previous:
            previous_result = json.loads(previous["result"])
            if previous_result.get("path"):
                sync_result["path"] = previous_result["path"]
        connection.execute("INSERT INTO sync_records(topic_id,version_id,baseline,result,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET version_id=excluded.version_id,baseline=excluded.baseline,result=excluded.result,updated_at=excluded.updated_at", (topic_id, version_id, baseline, encode(sync_result), now()))
        _activity(connection, topic_id, "导入 Obsidian 工作稿并更新已读取快照的同步基线")
    from . import runtime_mode
    if runtime_mode.platform_operations_allowed():
        _sync_created(draft_ids)
    return get_version(version_id)


def backup():
    from . import artifacts
    return artifacts.create_backup(get_settings())


def state():
    from . import runtime_mode, v3_migration
    with db.connect() as connection:
        # 软删除的行不进工作台状态。要看已删除内容请走 /api/trash。
        topics = [_topic(connection, row) for row in connection.execute("SELECT * FROM topics WHERE deleted_at IS NULL ORDER BY updated_at DESC")]
        jobs = [_job(row) for row in connection.execute("SELECT * FROM jobs ORDER BY created_at DESC")]
        publications = [dict(row) for row in connection.execute("SELECT * FROM publications ORDER BY created_at DESC")]
        drafts = [_draft(row) for row in connection.execute("SELECT * FROM drafts WHERE deleted_at IS NULL ORDER BY updated_at DESC")]
        notifications = []
        for row in connection.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 200"):
            item = dict(row)
            item["read"] = bool(item["read"])
            notifications.append(item)
        activity = [dict(row) for row in connection.execute("SELECT * FROM activity ORDER BY created_at DESC LIMIT 100")]
        quarantined = v3_migration.quarantined_tasks(connection)
        paused_slots = [dict(row) for row in connection.execute("SELECT * FROM production_slots WHERE paused=1 ORDER BY created_at DESC")]
        paused_jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE paused=1 AND state='queued' ORDER BY created_at DESC")]
        overview = {"topics": len(topics), "pending_topics": sum(topic["status"] == "pending" for topic in topics), "active_jobs": sum(job["state"] in ("queued", "running") for job in jobs), "drafts": len(drafts), "needs_connection": sum(draft["state"] == "needs_connection" for draft in drafts), "pending_review": sum(draft["state"] in ("synced", "browser_saved", "local") for draft in drafts), "published": sum(item["state"] == "published" for item in publications), "unread_notifications": sum(not item["read"] for item in notifications), "quarantined_deliveries": len(quarantined), "paused_slots": len(paused_slots), "paused_jobs": len(paused_jobs), "unknown_drafts": sum(draft["state"] == "unknown" for draft in drafts)}
        return {"topics": topics, "jobs": jobs, "publications": publications, "drafts": drafts, "notifications": notifications, "activity": activity, "overview": overview,
                "runtime_mode": runtime_mode.mode_view(connection), "quarantined_deliveries": quarantined,
                "paused_slots": paused_slots, "paused_jobs": paused_jobs}
