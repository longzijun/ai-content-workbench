"""集中审阅：逐平台制品指纹与审阅记录（V3 阶段2）。

设计要点（执行提示词 5.4）：

- 同一批次一次审阅，但**逐平台**记录结果（approved / returned /
  pending_material），审阅绑定平台、账号标签、正文、图片及顺序、封面、
  AI 标识与明确版本。
- 每个平台一条独立制品指纹：只改小红书稿件不会撤销公众号/头条的审阅；
  共享事实底稿 revision 进指纹，底稿一变，全部平台失配并要求重审。
- 指纹失配 = 需重审，不是历史审阅失效——历史记录永久保留。
- 审核、导出、生成都不写「已发布」；发布只由人工登记产生。
"""
from __future__ import annotations

import hashlib
import json

from . import auto_materials, db, video_script
from .service import encode, new_id, now, require

RESULTS = ("approved", "returned", "pending_material")


def _asset_ids(topic_id: str, version_id: str, platform: str) -> tuple[list[str], str]:
    from .service import _asset_view
    try:
        assets = _asset_view(topic_id, version_id, platform)
    except Exception:
        return [], ""
    ids = [asset.get("id") for asset in assets.get("assets") or [] if isinstance(asset, dict) and asset.get("id")]
    cover = assets.get("cover") or {}
    cover_id = cover.get("id") if isinstance(cover, dict) else ""
    return ids, str(cover_id or "")


def fingerprint(draft: dict, version: dict, fact_base_revision) -> str:
    """平台制品指纹：版本 + 底稿修订 + 标题正文 + 图片顺序 + 封面 + 账号 + AI 标识。"""
    payload = json.loads(version["payload"]) if isinstance(version["payload"], str) else version["payload"]
    work = next((item for item in payload["works"] if item["platform"] == draft["platform"]), None)
    require(work is not None, "当前版本没有该平台的作品", 422)
    asset_ids, cover_id = _asset_ids(draft["topic_id"], version["id"], draft["platform"])
    bound_material = auto_materials.version_materials(version["id"])
    # New workflow reviews bind the exact frozen material snapshot. Changes to
    # unused loose fact-form fields must not invalidate an unrelated work.
    material_revision = (bound_material.get("material_fingerprint") or
                         hashlib.sha256(encode(bound_material).encode()).hexdigest()) if bound_material is not None else fact_base_revision
    material = "\n".join([
        "platform=" + str(draft["platform"]),
        "version=" + str(version["id"]),
        "fact_base_revision=" + str(material_revision),
        "title=" + str(work.get("title") or ""),
        "body=" + str(work.get("body") or ""),
        "assets=" + ",".join(asset_ids),
        "cover=" + cover_id,
        "account=" + str(draft.get("account_label") or ""),
        "ai_disclosure=" + str(draft.get("ai_disclosure") or ""),
    ])
    if video_script.is_video_work(work):
        # Keep historical text fingerprints byte-for-byte compatible. Script
        # review also binds visual plans, subtitles, timing and shot order.
        material += "\nvideo_script=" + json.dumps(
            video_script.normalize_work(work), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def latest(draft_id: str) -> dict | None:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM platform_reviews WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",
            (draft_id,)).fetchone()
    return dict(row) if row else None


def history(draft_id: str) -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM platform_reviews WHERE draft_id=? ORDER BY created_at DESC",
            (draft_id,)).fetchall()
    return [dict(row) for row in rows]


def record(draft_id: str, result: str, comment: str, account_label: str, ai_disclosure: str, facts_confirmed: bool,
           expected_version_id: str | None = None, expected_fact_revision: int | None = None) -> dict:
    """记录一次平台审阅。result 为 approved 时草稿进入 reviewed。

    2026-11-04 起账号标签与 AI 标识不再硬校验：留空时按平台名与
    「AI 辅助创作」兜底（仍写入草稿并进指纹），由作者在平台后台管理
    实际账号与标识设置。"""
    from . import fact_base
    from .platform_registry import get as platform_info
    from .service import _draft, _row, _version
    require(result in RESULTS, "未知的审阅结果", 422)
    require(facts_confirmed is True, "请人工确认事实与引用", 422)
    with db.transaction() as connection:
        draft = _row(connection, "drafts", draft_id)
        account_label = str(account_label).strip() or platform_info(draft["platform"])["label"]
        ai_disclosure = str(ai_disclosure).strip() or "AI 辅助创作"
        require(draft["state"] not in ("stale", "published"), "旧版本或已发布的草稿不能审阅")
        version = _row(connection, "versions", draft["version_id"])
        topic = _row(connection, "topics", draft["topic_id"])
        material_gaps = auto_materials.workflow_gaps(version)
        if result == "approved" and material_gaps is not None:
            require(not material_gaps, "当前稿件资料检查未通过：" + "；".join(material_gaps), 422)
        require(topic["current_version_id"] == version["id"], "当前版本已变化，请重新查看并审阅", 409)
        if expected_version_id is not None:
            require(version["id"] == expected_version_id, "预览与被审阅的版本不一致，请刷新后核对", 409)
        fact = fact_base.get(draft["topic_id"])
        revision = fact["revision"] if fact else 0
        if expected_fact_revision is not None:
            require(revision == expected_fact_revision, "事实底稿已变化，请重新核对后确认", 409)
        # 指纹必须落在「应用本次审阅字段之后」的状态上：账号标签与 AI 标识
        # 正是审阅时才写入的字段，用更新前的行算会让指纹永远失配。
        reviewed_state = dict(draft)
        reviewed_state["account_label"] = str(account_label)[:200]
        reviewed_state["ai_disclosure"] = str(ai_disclosure)[:500]
        stamp = fingerprint(reviewed_state, dict(version), revision)
        review_id = new_id()
        connection.execute(
            "INSERT INTO platform_reviews(id,draft_id,platform,version_id,fingerprint,result,comment,reviewed_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (review_id, draft_id, draft["platform"], version["id"], stamp, result, str(comment or "")[:2000], "author", now()))
        # 账号标签与 AI 标识随审阅落库（进入指纹的字段）
        connection.execute(
            "UPDATE drafts SET account_label=?,ai_disclosure=?,facts_confirmed=1,"
            "state=CASE WHEN ?='approved' THEN 'reviewed' ELSE state END,"
            "error=CASE WHEN ?='approved' THEN NULL ELSE COALESCE(NULLIF(error,''),?) END,updated_at=? WHERE id=?",
            (str(account_label)[:200], str(ai_disclosure)[:500], result, result, str(comment or "")[:2000], now(), draft_id))
        row = connection.execute("SELECT * FROM platform_reviews WHERE id=?", (review_id,)).fetchone()
        draft_view = _draft(_row(connection, "drafts", draft_id))
    return {"review": dict(row), "draft": draft_view}


def current(draft: dict) -> dict:
    """审阅面板用：最新审阅 + 指纹是否仍然有效 + 阻断项。"""
    from . import fact_base
    from .service import _row, _version
    with db.connect() as connection:
        version = _row(connection, "versions", draft["version_id"])
    fact = fact_base.get(draft["topic_id"])
    revision = fact["revision"] if fact else 0
    stamp = fingerprint(draft, dict(version), revision)
    review = latest(draft["id"])
    valid = bool(review and review["fingerprint"] == stamp)
    blocking = []
    if review is None:
        blocking.append("尚未审阅")
    elif not valid:
        blocking.append("底稿或稿件已变化，需重审")
    elif review["result"] == "returned":
        blocking.append("审阅退回：" + (review["comment"] or "待修改"))
    elif review["result"] == "pending_material":
        blocking.append("待补素材")
    payload = json.loads(version["payload"])
    work = next((item for item in payload["works"] if item["platform"] == draft["platform"]), None)
    material_gaps = auto_materials.workflow_gaps(version, [work] if work else [])
    if material_gaps is not None:
        blocking.extend(material_gaps)
    return {
        "latest": review,
        "fingerprint": stamp,
        "fingerprint_valid": valid,
        "approved": bool(valid and review and review["result"] == "approved" and not material_gaps),
        "blocking": blocking,
        "fact_base_revision": revision,
    }


def console(topic_id: str) -> dict:
    """集中审阅页数据：共享事实 + 缺口 + 三平台作品与审阅状态。"""
    from . import fact_base
    from .service import _draft, _row, _version
    fact = fact_base.get(topic_id)
    with db.connect() as connection:
        topic = _row(connection, "topics", topic_id)
        version = _row(connection, "versions", topic["current_version_id"]) if topic["current_version_id"] else None
        platforms = []
        if version is not None:
            payload = json.loads(version["payload"])
            works = {item["platform"]: item for item in payload["works"]}
            drafts = {row["platform"]: dict(row) for row in connection.execute(
                "SELECT * FROM drafts WHERE version_id=? AND deleted_at IS NULL", (version["id"],)).fetchall()}
            for platform in json.loads(topic["platforms"]):
                work = works.get(platform)
                draft = drafts.get(platform)
                entry = {"platform": platform, "work": work, "draft": _draft(draft) if draft else None}
                if draft is not None:
                    entry["review"] = current(dict(draft))
                platforms.append(entry)
    return {
        "topic_id": topic_id,
        "fact_base": fact,
        "gaps": (auto_materials.workflow_gaps(version)
                 if version and auto_materials.version_materials(version["id"]) is not None
                 else fact_base.gaps(fact) if fact else ["尚未建立共享事实底稿"]),
        "version_id": version["id"] if version else None,
        "platforms": platforms,
    }
