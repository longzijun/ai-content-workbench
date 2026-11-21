"""Editable, durable generation candidates independent of frozen versions."""
from __future__ import annotations

from copy import deepcopy
import json

from . import db, service, execution_runtime as execution


def init_schema(connection=None):
    if connection is None:
        with db.transaction() as active:
            init_schema(active)
        return
    connection.execute("""CREATE TABLE IF NOT EXISTS candidate_drafts (
        id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
        job_id TEXT NOT NULL REFERENCES jobs(id), input_revision INTEGER NOT NULL,
        platform TEXT NOT NULL, attempt INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL,
        raw_output TEXT NOT NULL, issues TEXT NOT NULL, material_snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS candidate_topic ON candidate_drafts(topic_id,created_at)")


def _content(work, platform):
    work = work if isinstance(work, dict) else {}
    text = lambda key, limit: work.get(key, "")[:limit] if isinstance(work.get(key, ""), str) else ""
    result = {"platform": platform, "format": work.get("format") or (
        "video_script" if platform in ("weixin_channels", "kuaishou", "douyin") else
        "note" if platform == "xiaohongshu" else "article"),
        "title": text("title", 300), "body": text("body", 150000),
        "tags": [x[:200] for x in work.get("tags", [])[:50] if isinstance(x, str)] if isinstance(work.get("tags"), list) else [],
        "source_urls": [x[:4000] for x in work.get("source_urls", [])[:100] if isinstance(x, str)] if isinstance(work.get("source_urls"), list) else [],
        "image_prompts": [x[:4000] for x in work.get("image_prompts", [])[:20] if isinstance(x, str)] if isinstance(work.get("image_prompts"), list) else []}
    for key in ("content_form", "cover_text", "opening_hook", "closing_conclusion"):
        if isinstance(work.get(key), str):
            result[key] = work[key][:1000]
    for key in ("target_duration_seconds", "estimated_duration_seconds"):
        if type(work.get(key)) is int and 0 <= work[key] <= 3600:
            result[key] = work[key]
    if isinstance(work.get('shots'), list):
        shots = []
        seen = set()
        for index, shot in enumerate(work['shots'][:40], 1):
            if not isinstance(shot, dict):
                continue
            identifier = shot.get('id') if isinstance(shot.get('id'), str) else str(index)
            identifier = identifier[:64]
            if not identifier or identifier in seen:
                identifier = 'candidate-shot-' + str(index)
            seen.add(identifier)
            normalized = {'id': identifier, 'order': index}
            for key, limit in (('role', 100), ('spoken_text', 6000), ('visual_type', 100), ('visual_notes', 6000)):
                normalized[key] = shot.get(key, '')[:limit] if isinstance(shot.get(key, ''), str) else ''
            for key, count, length in (('subtitle_points', 12, 500), ('source_refs', 30, 4000)):
                normalized[key] = [item[:length] for item in shot.get(key, [])[:count] if isinstance(item, str)] if isinstance(shot.get(key), list) else []
            shots.append(normalized)
        result['shots'] = shots
    return result


def _view(connection, row):
    row = dict(row)
    payload = json.loads(row["payload"])
    work = payload["works"][0]
    topic = service._row(connection, "topics", row["topic_id"])
    material = json.loads(row["material_snapshot"])
    return {key: row[key] for key in ("id", "topic_id", "job_id", "input_revision", "platform", "attempt", "revision", "state", "stage", "created_at", "updated_at")} | {
        **work, "issues": json.loads(row["issues"]), "content": work,
        "material_mode": material.get("mode", "limited"),
        "stale": bool(topic.get("deleted_at") or topic["revision"] != row["input_revision"] or topic["status"] != "approved")}


def _row(connection, topic_id, candidate_id):
    row = connection.execute("SELECT * FROM candidate_drafts WHERE id=? AND topic_id=?", (candidate_id, topic_id)).fetchone()
    service.require(row is not None, "工作稿不存在", 404)
    return row


def _messages(issues):
    return [str(item.get('message') or '资料仍需核对')[:2000] if isinstance(item, dict) else str(item)[:2000] for item in issues or []][:50]


def list_for_topic(topic_id):
    with db.connect() as connection:
        service._row(connection, "topics", topic_id)
        rows = connection.execute("SELECT * FROM candidate_drafts WHERE topic_id=? ORDER BY created_at DESC,rowid DESC", (topic_id,)).fetchall()
        saved = connection.execute('SELECT snapshot FROM auto_material_snapshots WHERE topic_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (topic_id,)).fetchone()
        material = json.loads(saved['snapshot']) if saved else json.loads(rows[0]['material_snapshot']) if rows else {}
        summary = {key: material.get(key) for key in ('mode', 'audience', 'suggested_angle')}
        summary['documents'] = [{'url': str(item.get('url') or '')[:4000], 'title': str(item.get('title') or '')[:300]} for item in material.get('documents', [])[:20] if isinstance(item, dict)]
        summary['issues'] = _messages(material.get('issues'))
        return {"candidates": [_view(connection, row) for row in rows], 'materials_summary': summary}


def read(topic_id, candidate_id):
    with db.connect() as connection:
        return _view(connection, _row(connection, topic_id, candidate_id))


def publish(topic, platform, payload=None, raw="", issues=None, state="draft", stage="generated"):
    """Each model attempt is a new candidate; an author-edited row is never overwritten."""
    job_id = topic.get("_generation_run")
    with db.transaction() as connection:
        job = service._row(connection, "jobs", job_id)
        execution.require_job_owner(connection, job_id)
        current = service._row(connection, "topics", job["topic_id"])
        service.require(job["state"] == "running" and not job["cancel_requested"] and
                        current["status"] == "approved" and not current.get("deleted_at") and
                        current["revision"] == job["input_revision"] == topic["revision"],
                        "任务范围已失效，已保留旧工作稿并丢弃迟到结果", 409)
        service.require(platform in json.loads(current["platforms"]), "平台不属于批准范围", 422)
        works = payload.get("works", []) if isinstance(payload, dict) else []
        work = next((w for w in works if isinstance(w, dict) and w.get("platform", platform) == platform), {})
        content = _content(work, platform)
        normalized = {"strategy": str((payload or {}).get("strategy") or "候选工作稿，尚未定稿")[:10000], "works": [content]}
        stamp, identifier = service.now(), service.new_id()
        connection.execute("INSERT INTO candidate_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            identifier, current["id"], job_id, current["revision"], platform,
            int(topic.get("_execution_attempt") or 1), 0, state, stage, service.encode(normalized),
            str(raw)[:150000], service.encode(_messages(issues)),
            service.encode(topic.get("_auto_materials") or {}), stamp, stamp))
        return _view(connection, _row(connection, current["id"], identifier))


def save(topic_id, candidate_id, data):
    from .content_studio import WorkingCopySave, _video_copy
    from . import auto_materials, platform_rules, video_script
    data = data if isinstance(data, WorkingCopySave) else WorkingCopySave.model_validate(data)
    with db.transaction() as connection:
        row = _row(connection, topic_id, candidate_id)
        current = _view(connection, row)
        service.require(not current["stale"], "选题范围已变化，旧稿已保留；请查看当前范围后合并", 409)
        service.require(current["revision"] == data.expected_revision, "工作稿已被另一窗口修改，当前输入请保留后对照合并", 409)
        payload = json.loads(row["payload"])
        work = payload["works"][0]
        work.update(data.model_dump(include={"title", "body", "tags", "source_urls"}))
        service.require(all(len(x) <= 4000 for x in work["source_urls"]) and all(len(x) <= 200 for x in work["tags"]), "来源或标签过长", 422)
        if video_script.is_video_work(work):
            work.update({key: getattr(data, key) for key in ("cover_text", "shots", "target_duration_seconds") if getattr(data, key) is not None})
            work.update(_video_copy(work))
        else:
            service.require(not ({"cover_text", "shots", "target_duration_seconds"} & data.model_fields_set), "图文工作稿不接受分镜字段", 422)
        issues = []
        try:
            findings = platform_rules.work_errors(work)
            issues = [str(i.get("message", i)) if isinstance(i, dict) else str(i) for i in findings]
        except (ValueError, TypeError) as error:
            issues = [str(error)]
        issues += auto_materials.freeze_gaps(json.loads(row['material_snapshot']), payload)
        state = "needs_changes" if issues else "draft"
        if not work.get("body", "").strip():
            state, issues = "incomplete", ["正文尚未完成"] + issues
        connection.execute("UPDATE candidate_drafts SET payload=?,revision=revision+1,state=?,issues=?,updated_at=? WHERE id=? AND revision=?", (
            service.encode(payload), state, service.encode(issues), service.now(), candidate_id, data.expected_revision))
        return _view(connection, _row(connection, topic_id, candidate_id))


def commit(topic_id, data):
    from . import auto_materials
    candidate_ids, revisions = data.get("candidate_ids") or {}, data.get("expected_revisions") or {}
    with db.transaction() as connection:
        topic = service._row(connection, "topics", topic_id)
        service.require(not topic.get("deleted_at") and topic["status"] == "approved", "请先批准当前选题", 409)
        service.require(topic.get("current_version_id") == data.get("expected_version_id"),
                        "正式版本已被另一窗口更新，请查看新版本后再确认定稿；工作稿已保留", 409)
        platforms = json.loads(topic["platforms"])
        service.require(set(candidate_ids) == set(revisions) == set(platforms), "请提供所有批准平台的工作稿及修订号", 422)
        service.require(not connection.execute("SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic_id,)).fetchone(), "请等待当前创作结束或取消后定稿；工作稿仍可编辑", 409)
        works, materials = [], []
        for platform in platforms:
            row = _row(connection, topic_id, candidate_ids[platform])
            service.require(row["platform"] == platform and row["input_revision"] == topic["revision"] and row["revision"] == revisions[platform], "工作稿范围或修订已变化，请重新查看后定稿", 409)
            works.append(json.loads(row["payload"])["works"][0])
            materials.append(json.loads(row["material_snapshot"]))
        fingerprints = {service.digest(material) for material in materials}
        service.require(all(material.get('mode') in ('evidenced', 'limited', 'outline') for material in materials),
                        '工作稿尚无完整资料快照，请继续自动整理资料；原稿已保留', 409)
        service.require(len(fingerprints) == 1, "各平台资料快照不同，请使用同批资料重新创作后合并定稿；原稿已保留", 409)
        snapshot = {"_auto_materials": materials[0]}
        payload = {"strategy": "作者在内容工坊核对并冻结候选工作稿", "works": works}
        gaps = auto_materials.freeze_gaps(materials[0], payload)
        service.require(not gaps, "当前稿仍需处理：" + "；".join(gaps), 422)
        service._require_generation_ready(topic)
        try:
            version_id, _ = service._insert_version(connection, topic, payload, "candidate:studio", material_snapshot=materials[0])
        except (ValueError, TypeError) as error:
            service.require(False, "稿件尚不能定稿，请检查标题、正文及平台要求；工作稿已保留：" + str(error), 422)
        auto_materials.bind_version(connection, version_id, snapshot)
    service._register_source_images(topic, version_id, payload)
    return {"version": service.get_version(version_id)}


def _lineage(connection, topic):
    ids, current = [], topic.get('_candidate_parent_job')
    for _ in range(50):
        if not current or current in ids:
            break
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (current,)).fetchone()
        if not row or row['topic_id'] != topic['id'] or row['input_revision'] != topic['revision']:
            break
        ids.append(current)
        current = json.loads(row['input_snapshot']).get('_candidate_parent_job')
    return ids


def reusable_parent(topic, platform):
    with db.connect() as connection:
        for job_id in _lineage(connection, topic):
            rows = connection.execute('SELECT * FROM candidate_drafts WHERE job_id=? AND platform=? ORDER BY revision DESC,created_at DESC,rowid DESC', (job_id, platform)).fetchall()
            for row in rows:
                material = json.loads(row['material_snapshot'])
                if material != (topic.get('_auto_materials') or {}):
                    continue
                candidate = _view(connection, row)
                if not candidate['stale'] and candidate['state'] in ('draft', 'needs_changes') and candidate['body'].strip():
                    return candidate
    return None


def finish_job(job_id, provider="local"):
    with db.transaction() as connection:
        job = service._row(connection, "jobs", job_id)
        execution.require_job_owner(connection, job_id)
        topic = service._row(connection, "topics", job["topic_id"])
        if job["state"] != "running" or job["cancel_requested"] or topic["status"] != "approved" or topic["revision"] != job["input_revision"]:
            connection.execute("UPDATE jobs SET state='cancelled',stage='已取消，已有工作稿保留',updated_at=? WHERE id=?", (service.now(), job_id))
            return
        snapshot = json.loads(job['input_snapshot'])
        lineage = [job_id] + _lineage(connection, snapshot)
        rows = connection.execute("SELECT * FROM candidate_drafts WHERE job_id IN (" + ','.join('?' for _ in lineage) + ") ORDER BY created_at DESC,rowid DESC", lineage).fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(row["platform"], row)
        available = sum(bool(json.loads(row["payload"])["works"][0].get("body", "").strip()) for row in latest.values())
        total = len(json.loads(topic["platforms"]))
        stage = f"已保存{available}/{total}个平台工作稿，待预览检查并定稿" if available else "暂未生成正文，资料与任务已保留，可继续创作"
        if any(row["state"] == "incomplete" for row in latest.values()):
            stage += "（含不完整稿）"
        if available and all(json.loads(row['material_snapshot']).get('mode') == 'outline' for row in latest.values()):
            stage = '目前仅有提纲，已保存并可编辑；尚未形成完整正文'
        state = "succeeded" if available else "failed"
        events = json.loads(job["events"]) + [{"time": service.now(), "message": stage}]
        connection.execute("UPDATE jobs SET state=?,stage=?,provider=?,error=?,events=?,updated_at=? WHERE id=?", (
            state, stage, provider, None if available else stage, service.encode(events), service.now(), job_id))
        service._notice(connection, topic["id"], "工作稿已保存，待预览" if available else "创作暂时中断", stage)
