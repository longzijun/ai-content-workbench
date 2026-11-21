"""Read-only, review-bound publishing preparation without a ZIP dependency."""
import json

from . import content_studio, db, review, service, video_script


def get(topic_id):
    data = review.console(topic_id)
    blocking = list(data["gaps"])
    with db.connect() as connection:
        topic = service._row(connection, "topics", topic_id)
        version = service._row(connection, "versions", data["version_id"]) if data["version_id"] else None
        if topic.get("deleted_at") or topic["status"] != "approved":
            blocking.append("选题当前未处于已批准状态")
        if topic["current_version_id"] != data["version_id"]:
            service.require(False, "作品版本已变化，请重新打开发布准备页", 409)
        if not version:
            blocking.append("尚未冻结正式版本")
        elif version["input_revision"] != topic["revision"]:
            blocking.append("选题范围已变化，请重新确认并冻结当前作品")
        copies = {row["platform"]: json.loads(row["content"]) for row in connection.execute(
            "SELECT platform,content FROM studio_working_copies WHERE topic_id=? AND base_version_id=?",
            (topic_id, data["version_id"] or ""))}
    global_blocking = list(blocking)
    for entry in data["platforms"]:
        platform, work = entry["platform"], entry.get("work")
        item_blocking = list(global_blocking) + list((entry.get("review") or {}).get("blocking") or [])
        if not work or not entry.get("draft"):
            item_blocking.append("缺少当前正式作品")
        if not (entry.get("review") or {}).get("approved") and not item_blocking:
            item_blocking.append("尚未完成当前版本审阅")
        copy = copies.get(platform)
        if copy and work:
            keys = ("title", "body", "tags", "source_urls")
            if video_script.is_video_work(work):
                keys += ("cover_text", "shots", "target_duration_seconds")
                copy = content_studio._video_copy(copy)
            if any(copy.get(key) != work.get(key) for key in keys):
                item_blocking.append("当前工作稿尚未冻结；此处仅显示已冻结的正式稿")
        entry["blocking"] = item_blocking
        entry["ready"] = not item_blocking
        entry["status_label"] = (("脚本就绪 · 待拍摄" if work and video_script.is_video_work(work) else "图文准备就绪")
                                 if entry["ready"] else "发布准备尚未就绪")
        if work:
            entry["copy_text"] = video_script.spoken_text(work) if video_script.is_video_work(work) else work.get("body", "")
            entry["shooting_script"] = video_script.shooting_script(work) if video_script.is_video_work(work) else None
            entry["assets"] = service._asset_view(topic_id, data["version_id"], platform)
            if not video_script.is_video_work(work):
                from . import body_export
                from .config import get_settings
                rendered = body_export.build(work, platform, get_settings(), topic_id, data["version_id"])
                entry["work"] = {**work, "html": rendered["html"], "text": rendered["text"],
                                 "images_embedded": rendered["images"], "export_warnings": rendered["warnings"]}
        blocking.extend(f"{platform}：{message}" for message in item_blocking)
    # Review retrieval spans immutable versions, SQLite and asset manifests. A
    # concurrent freeze/fact/account/asset update must not bless the old view.
    latest = review.console(topic_id)
    identity = lambda view: service.digest({"version": view["version_id"], "fact": view["fact_base"],
        "gaps": view["gaps"], "platforms": [{"platform": item["platform"], "review": item.get("review")} for item in view["platforms"]]})
    service.require(identity(latest) == identity(data), "版本、事实或审阅状态已变化，请刷新发布准备页", 409)
    with db.connect() as connection:
        fresh = service._row(connection, "topics", topic_id)
    service.require(fresh["current_version_id"] == data["version_id"] and fresh["revision"] == topic["revision"],
                    "作品范围已变化，请刷新发布准备页", 409)
    return {**data, "fact_base_revision": (data["fact_base"] or {}).get("revision", 0),
            "ready": bool(data["platforms"]) and not blocking, "blocking": blocking}
