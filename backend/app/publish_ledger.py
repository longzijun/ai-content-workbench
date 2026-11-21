"""人工发布台账（V3 阶段3）。

规则（执行提示词 6.2 / 规划第七步）：

- 发布由用户在官方应用或后台完成。工作台只登记：平台、账号标签、版本、
  实际发布时间、链接、平台内是否改稿、以及明确的「我确认已发布」。
- 发布状态只有「未登记／用户确认已发布」；**链接完整度**与**最终稿回填
  状态**是两个独立待办字段，可以同时缺失，互不覆盖已发布事实。
- 未来时间不能登记成已经发生的发布（按北京时间判定）。
- 链接只做本地格式与平台归属检查，不自动访问。
- 平台内改过稿：允许粘贴最终正文或上传最终画面，形成独立的发布稿记录，
  原审定稿保留。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import db, platform_registry
from .service import _row, encode, new_id, now, require

CN_TZ = timezone(timedelta(hours=8))
PLATFORM_LABELS = {platform: platform_registry.get(platform)["label"] for platform in platform_registry.PLATFORM_IDS}
PLATFORM_HOSTS = {
    "wechat": ("mp.weixin.qq.com",),
    "toutiao": ("mp.toutiao.com", "toutiao.com", "www.toutiao.com"),
    "xiaohongshu": ("xiaohongshu.com", "www.xiaohongshu.com"),
}


def _parse_moment(value: str) -> datetime:
    text = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        require(False, "发布时间格式不正确，请用 ISO 时间，如 2026-10-26T09:30", 422)
    if moment.tzinfo is None:
        #  naive 一律按北京时间解释（用户在本机录入的就是本地时间）
        moment = moment.replace(tzinfo=CN_TZ)
    return moment


def _check_url(url: str, platform: str) -> tuple[str, str]:
    """本地格式与平台归属检查。不访问网络。"""
    text = str(url or "").strip()
    if not text:
        return "missing", ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "invalid_format", "链接格式不正确"
    if platform_registry.is_video_platform(platform):
        # Keep an honest record without inventing share-link domain patterns
        # or making a network request to a creator platform.
        return "present", "视频平台链接仅通过格式检查，平台归属与实际发布内容请人工核对"
    host = parsed.hostname.lower()
    if host not in PLATFORM_HOSTS.get(platform, ()):
        label = PLATFORM_LABELS.get(platform, platform)
        return "invalid_format", f"链接域名与{label}不符，请确认平台与链接对应"
    return "present", ""


def register(topic_id: str, data: dict) -> dict:
    """登记一次人工发布。"""
    platform = data["platform"]
    moment = _parse_moment(data["published_at"])
    require(moment <= datetime.now(timezone.utc), "发布时间不能是未来时间；计划中的发布请先不登记", 422)
    url_status, url_note = _check_url(data.get("url"), platform)
    version_id = str(data.get("version_id") or "")
    draft_id = None
    with db.transaction() as connection:
        topic = _row(connection, "topics", topic_id)
        if not version_id:
            version_id = topic["current_version_id"] or ""
        require(version_id, "没有可登记的版本", 409)
        version = _row(connection, "versions", version_id)
        require(version["topic_id"] == topic_id, "所选版本不属于此选题", 422)
        works = json.loads(version["payload"])["works"]
        require(any(work["platform"] == platform for work in works), "所选版本没有此平台作品", 422)
        row = connection.execute(
            "SELECT id FROM drafts WHERE topic_id=? AND version_id=? AND platform=? AND deleted_at IS NULL",
            (topic_id, version_id, platform)).fetchone()
        if row:
            draft_id = row["id"]
        record_id = new_id()
        connection.execute(
            "INSERT INTO publish_records(id,topic_id,draft_id,version_id,platform,account_label,confirmed,"
            "published_at,url,url_status,platform_edited,final_text,final_backfill,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
            (record_id, topic_id, draft_id, version_id, platform, str(data.get("account_label") or "")[:200],
             moment.isoformat(), str(data.get("url") or "")[:2000], url_status,
             1 if data.get("platform_edited") else 0,
             str(data.get("final_text") or "")[:200000],
             "done" if str(data.get("final_text") or "").strip() else "pending",
             str(data.get("note") or "")[:4000], now(), now()))
        if url_note:
            connection.execute("UPDATE publish_records SET note=? WHERE id=?", (url_note[:4000], record_id))
    return get_record(record_id)


def backfill(record_id: str, data: dict) -> dict:
    """回填链接与/或最终稿。两个字段独立；都不覆盖已发布事实。"""
    with db.transaction() as connection:
        row = _row(connection, "publish_records", record_id)
        require(row["confirmed"] == 1, "只能回填已确认发布的记录", 409)
        updates, params = [], []
        if data.get("url") is not None:
            url_status, url_note = _check_url(data["url"], row["platform"])
            updates.append("url=?,url_status=?")
            params += [str(data["url"])[:2000], url_status]
            if url_note:
                updates.append("note=?")
                params.append(url_note[:4000])
        if data.get("final_text") is not None:
            text = str(data["final_text"])[:200000]
            updates.append("final_text=?,final_backfill=?")
            params += [text, "done" if text.strip() else "pending"]
        require(updates, "没有要回填的内容", 422)
        updates.append("updated_at=?")
        params.append(now())
        params.append(record_id)
        connection.execute(f"UPDATE publish_records SET {','.join(updates)} WHERE id=?", params)
    return get_record(record_id)


def get_record(record_id: str) -> dict:
    with db.connect() as connection:
        row = _row(connection, "publish_records", record_id)
    return _view(row)


def _view(row) -> dict:
    item = dict(row)
    item["url_verification"] = "manual_required" if platform_registry.is_video_platform(item["platform"]) and item.get("url") else "local_format_only"
    item["confirmed"] = bool(item["confirmed"])
    item["platform_edited"] = bool(item["platform_edited"])
    item["published_at_local"] = _to_local(item.get("published_at"))
    return item


def _to_local(value) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(CN_TZ).isoformat()
    except ValueError:
        return str(value)


def list_records(topic_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM publish_records"
    params: tuple = ()
    if topic_id:
        query += " WHERE topic_id=?"
        params = (topic_id,)
    query += " ORDER BY created_at DESC"
    with db.connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [_view(row) for row in rows]
