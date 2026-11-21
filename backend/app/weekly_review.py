"""周复盘（V3 阶段4）：一页结论，样本不足时明确说明，不虚构运营结果。

回答四问：记录覆盖与缺失、同平台同发布时长内容表现、可核实读者反馈、
人工耗时与返工、下周三个行动。只有账号总粉丝变化时不归因到单篇。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db, platform_registry
from .service import _row, encode, new_id, now, require

CN_TZ = timezone(timedelta(hours=8))


def _week_bounds(week_start: str) -> tuple[str, str]:
    try:
        start = datetime.fromisoformat(week_start).date()
    except ValueError:
        require(False, "week 参数格式应为 YYYY-MM-DD", 422)
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def _in_week(item: dict, start: str, end: str, *fields: str) -> bool:
    """任一日期字段落在本周窗口内即算本周记录。"""
    for field in fields:
        value = str(item.get(field) or "")[:10]
        if value and start <= value <= end:
            return True
    return False


def generate(week_start: str) -> dict:
    start, end = _week_bounds(week_start)
    with db.connect() as connection:
        all_observations = [dict(row) for row in connection.execute(
            "SELECT * FROM observations ORDER BY recorded_at DESC LIMIT 2000").fetchall()]
        records = [dict(row) for row in connection.execute(
            "SELECT * FROM publish_records WHERE confirmed=1 ORDER BY published_at DESC").fetchall()]
        all_feedback = [dict(row) for row in connection.execute(
            "SELECT * FROM feedback_items ORDER BY created_at DESC LIMIT 200").fetchall()]
    # 观测归属实际发生周；recorded_at 只在缺观测日期时兜底。
    # 两个日期做 OR 会把上周作品数据错归到本周录入动作，污染账号级归因判断。
    observations = [
        item for item in all_observations
        if start <= str(item.get("observed_at") or item.get("recorded_at") or "")[:10] <= end
    ]
    feedback = [item for item in all_feedback if _in_week(item, start, end, "created_at")]

    platforms = platform_registry.PLATFORM_IDS
    coverage = {}
    for platform in platforms:
        items = [item for item in observations if item["platform"] == platform]
        known = [item for item in items if item["metric_value"] is not None]
        coverage[platform] = {
            "observations": len(items), "known_values": len(known),
            "works": len({item["work_ref"] for item in items if item["work_ref"]}),
            "missing": not items,
        }
    published = [item for item in records if start <= (item.get("published_at") or "")[:10] <= end]

    comparison = []
    for platform in platforms:
        by_work: dict[str, dict] = {}
        for item in observations:
            if item["platform"] != platform or not item["work_ref"]:
                continue
            bucket = by_work.setdefault(item["work_ref"], {"metrics": {}, "known": 0})
            if item["metric_value"] is not None:
                bucket["metrics"][item["metric_name"]] = item["metric_value"]
                bucket["known"] += 1
        works = [{"work_ref": key, **value} for key, value in by_work.items()]
        comparison.append({
            "platform": platform,
            "works": works,
            "comparable": len([w for w in works if w["known"] >= 1]) >= 2,
            "note": "同平台内比较；曝光/阅读/播放口径分别保留，不跨平台相加。" if len(works) >= 2
                    else "样本不足（少于 2 部有数据的作品），不做表现比较。",
        })

    account_only = [item for item in observations if item["scope"] == "account" and not item["work_ref"]]
    attribution_note = None
    if account_only and not any(item["work_ref"] for item in observations):
        attribution_note = "本周只有账号级数据（如总粉丝变化），不能归因到任何单篇作品。"

    organized = [item for item in feedback if item["status"] == "organized"]
    actions = []
    missing_platforms = [p for p, info in coverage.items() if info["missing"]]
    if missing_platforms:
        actions.append(f"补齐 {'、'.join(missing_platforms)} 的后台数据导入，当前复盘缺少这些平台的记录。")
    else:
        actions.append("保持当前导入节奏，核对缺失指标（未知值）并在后台导出对应字段。")
    if published:
        actions.append(f"对本周已登记的 {len(published)} 次发布做最终稿回填与链接核对。")
    else:
        actions.append("本周无发布登记；如有发布请在台账补登记，否则复盘样本继续为空。")
    if organized:
        actions.append("从整理后的读者反馈里挑 1 条高频问题，作为下周选题或续篇。")
    else:
        actions.append("录入至少 3 条匿名读者反馈并整理，作为下周选题输入。")

    payload = {
        "week_start": start, "week_end": end,
        "generated_at": now(),
        "coverage": coverage,
        "published_count": len(published),
        "comparison": comparison,
        "attribution_note": attribution_note,
        "feedback": {"total": len(feedback), "organized": len(organized)},
        "actions": actions[:3],
        "empty_state": not observations and not published and not feedback,
        "note": "本页只汇总工作台内登记与导入的数据；没有数据时显示空状态，不生成虚构运营结论。",
    }
    review_id = new_id()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO weekly_reviews(id,week_start,week_end,payload,created_at) VALUES(?,?,?,?,?)",
            (review_id, start, end, encode(payload), now()))
    return {"id": review_id, **payload}


def latest(week_start: str | None = None) -> dict | None:
    query = "SELECT * FROM weekly_reviews"
    params: tuple = ()
    if week_start:
        start, _ = _week_bounds(week_start)
        query += " WHERE week_start=?"
        params = (start,)
    query += " ORDER BY created_at DESC LIMIT 1"
    with db.connect() as connection:
        row = connection.execute(query, params).fetchone()
    if not row:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.get("payload") or "{}")
    return item
