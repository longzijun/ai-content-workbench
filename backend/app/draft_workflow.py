"""Approval and durable request deduplication for editable local drafts."""
import json

from . import db, service


def init_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS draft_creation_requests (
        topic_id TEXT NOT NULL REFERENCES topics(id), request_id TEXT NOT NULL,
        request_hash TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        created_at TEXT NOT NULL, PRIMARY KEY(topic_id, request_id))""")


def create(topic_id, data):
    platforms = data["platforms"]
    service.require(platforms and len(platforms) == len(set(platforms)) and
                    set(platforms) <= service.PLATFORMS, "请选择有效且不重复的平台", 422)
    identity = service.digest({"revision": data["expected_revision"], "platforms": sorted(platforms)})
    with db.transaction() as connection:
        topic = service._row(connection, "topics", topic_id)
        service.require(not topic.get("deleted_at"), "选题已删除", 404)
        previous = connection.execute(
            "SELECT * FROM draft_creation_requests WHERE topic_id=? AND request_id=?",
            (topic_id, data["request_id"])).fetchone()
        if previous:
            service.require(previous["request_hash"] == identity, "同一创作请求不能更改选题范围，请刷新后重试", 409)
            job = service._job(service._row(connection, "jobs", previous["job_id"]))
        else:
            service.require(topic["revision"] == data["expected_revision"], "选题范围已变化，请查看当前修订后创作", 409)
            service.require(set(platforms) == set(json.loads(topic["platforms"])), "创作平台须与当前选题范围一致", 422)
            # This user action approves creation, never factual accuracy or publication.
            service._require_generation_ready(topic)
            if topic["status"] != "approved":
                connection.execute("UPDATE topics SET status='approved',updated_at=? WHERE id=?",
                                   (service.now(), topic_id))
                service._activity(connection, topic_id, "人工批准并开始创作；资料与成稿仍待审阅")
                topic["status"] = "approved"
            active = connection.execute(
                "SELECT * FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic_id,)).fetchone()
            if active:
                snapshot = json.loads(active["input_snapshot"])
                service.require(active["input_revision"] == topic["revision"] and snapshot.get("_candidate_mode"),
                                "该选题已有任务，请先查看当前任务进度", 409)
                job = service._job(active)
            else:
                job = service._create_job(connection, topic, candidate_mode=True)
            connection.execute("INSERT INTO draft_creation_requests VALUES(?,?,?,?,?)",
                               (topic_id, data["request_id"], identity, job["id"], service.now()))
    return {"job": job, "topic": service.get_topic(topic_id)}


def candidate_topic(candidate_id):
    with db.connect() as connection:
        row = connection.execute("SELECT topic_id FROM candidate_drafts WHERE id=?", (candidate_id,)).fetchone()
        service.require(row is not None, "工作稿不存在", 404)
        return row["topic_id"]
