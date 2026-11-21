"""运营数据 CSV 导入与观测记录（V3 阶段4）。

- 支持 UTF-8 BOM 中文表头；字段映射、导入预览、逐行错误、重复检测两段式：
  先预览（不落观测），确认后再提交。
- 每条观测保留平台、作品或账号范围、统计窗口、记录时间、来源、原始指标
  名称与值。缺失记未知（NULL），绝不记 0；曝光/阅读/播放各自保留。
- 重复导入不累计制造重复观测：相同（平台+作品+指标+窗口+值）判重跳过。
- 不依赖平台接口；截图证据路径可登记但不自动识别（本机无 OCR 时不冒充）。
"""
from __future__ import annotations

import csv
import io
import json

from . import db, platform_registry
from .service import _row, encode, new_id, now, require

PLATFORMS = platform_registry.PLATFORM_IDS

#: 表头映射：中文/英文别名 → 观测字段。metric_name 与 platform 必填。
HEADER_MAP = {
    "平台": "platform", "platform": "platform",
    "作品": "work_ref", "作品标识": "work_ref", "work_ref": "work_ref", "标题": "work_ref",
    "范围": "scope", "scope": "scope",
    "指标": "metric_name", "指标名称": "metric_name", "metric_name": "metric_name",
    "数值": "metric_value", "值": "metric_value", "metric_value": "metric_value", "value": "metric_value",
    "统计开始": "window_start", "窗口开始": "window_start", "window_start": "window_start",
    "统计结束": "window_end", "窗口结束": "window_end", "window_end": "window_end",
    "来源": "source", "source": "source",
    "备注": "note", "note": "note",
    "记录时间": "observed_at", "observed_at": "observed_at", "统计时间": "observed_at",
}


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GB18030 的 CSV")


def _map_row(row: dict, mapping: dict) -> tuple[dict | None, str]:
    mapped = {}
    for header, field in mapping.items():
        mapped[field] = str(row.get(header) or "").strip()
    if not mapped.get("platform"):
        return None, "缺少平台"
    if mapped["platform"] not in PLATFORMS:
        return None, f"平台必须是 {' / '.join(PLATFORMS)} 之一"
    if not mapped.get("metric_name"):
        return None, "缺少指标名称"
    value = mapped.get("metric_value") or ""
    if value:
        try:
            mapped["metric_value"] = float(value.replace(",", ""))
        except ValueError:
            return None, f"数值无法解析：{value}"
    else:
        mapped["metric_value"] = None  # 缺失记未知，不记 0
    mapped.setdefault("scope", "work" if mapped.get("work_ref") else "account")
    mapped.setdefault("source", "csv")
    mapped.setdefault("observed_at", "")
    return mapped, ""


def preview(filename: str, raw: bytes) -> dict:
    """解析并预览，不落观测库。"""
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    mapping = {}
    unmapped = []
    for header in reader.fieldnames:
        field = HEADER_MAP.get((header or "").strip())
        if field:
            mapping[header] = field
        else:
            unmapped.append(header)
    rows, errors = [], []
    for index, row in enumerate(reader, start=2):  # 第 1 行是表头
        mapped, error = _map_row(row, mapping)
        if error:
            errors.append({"row": index, "error": error})
        else:
            mapped["row"] = index
            rows.append(mapped)
    batch_id = new_id()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO import_batches(id,filename,row_count,mapping,errors,state,created_at) VALUES(?,?,?,?,?,'preview',?)",
            (batch_id, filename[:300], len(rows) + len(errors), encode(mapping), encode(errors), now()))
    return {"batch_id": batch_id, "filename": filename, "mapping": mapping, "unmapped_headers": unmapped,
            "rows": rows, "errors": errors, "row_count": len(rows) + len(errors),
            "note": "预览未写入观测库；确认后提交。重复行会在提交时判定并跳过。"}


def _validate_mapped(row: dict) -> tuple[dict | None, str]:
    """校验预览阶段已映射的行（键是字段名，不是表头）。"""
    platform = str(row.get("platform") or "").strip()
    if platform not in PLATFORMS:
        return None, f"平台必须是 {' / '.join(PLATFORMS)} 之一"
    metric_name = str(row.get("metric_name") or "").strip()
    if not metric_name:
        return None, "缺少指标名称"
    value = row.get("metric_value")
    if value in (None, ""):
        value = None  # 缺失记未知，不记 0
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None, f"数值无法解析：{value}"
    return {
        "platform": platform,
        "scope": str(row.get("scope") or ("work" if row.get("work_ref") else "account")),
        "work_ref": str(row.get("work_ref") or "")[:300],
        "metric_name": metric_name[:100],
        "metric_value": value,
        "window_start": str(row.get("window_start") or "")[:40],
        "window_end": str(row.get("window_end") or "")[:40],
        "source": str(row.get("source") or "csv")[:60],
        "note": str(row.get("note") or "")[:2000],
        "observed_at": str(row.get("observed_at") or "")[:40],
    }, ""


def commit_rows(batch_id: str, rows: list[dict]) -> dict:
    with db.transaction() as connection:
        batch = _row(connection, "import_batches", batch_id)
        require(batch["state"] == "preview", "该批次已提交或已放弃", 409)
        imported, duplicates, errors = 0, 0, []
        for row in rows:
            mapped, error = _validate_mapped(row)
            if error:
                errors.append({"row": row.get("row"), "error": error})
                continue
            existing = connection.execute(
                "SELECT 1 FROM observations WHERE platform=? AND work_ref=? AND metric_name=? "
                "AND COALESCE(window_start,'')=? AND COALESCE(window_end,'')=? AND COALESCE(metric_value,-999999)=? LIMIT 1",
                (mapped["platform"], mapped.get("work_ref", ""), mapped["metric_name"],
                 mapped.get("window_start", ""), mapped.get("window_end", ""),
                 mapped["metric_value"] if mapped["metric_value"] is not None else -999999)).fetchone()
            if existing:
                duplicates += 1
                continue
            connection.execute(
                "INSERT INTO observations(id,batch_id,platform,scope,work_ref,metric_name,metric_value,"
                "window_start,window_end,source,note,observed_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), batch_id, mapped["platform"], mapped["scope"], mapped.get("work_ref", ""),
                 mapped["metric_name"], mapped["metric_value"], mapped.get("window_start", "") or None,
                 mapped.get("window_end", "") or None, mapped.get("source", "csv"),
                 mapped.get("note", ""), mapped.get("observed_at", "") or now(), now()))
            imported += 1
        connection.execute(
            "UPDATE import_batches SET state='committed',imported_count=?,duplicate_count=?,error_count=?,errors=? WHERE id=?",
            (imported, duplicates, len(errors), encode(errors), batch_id))
    return {"batch_id": batch_id, "imported": imported, "duplicates": duplicates, "errors": errors}


def record_manual(data: dict) -> dict:
    """手工登记单条观测（含截图证据路径）。"""
    platform = data.get("platform")
    require(platform in PLATFORMS, f"平台必须是 {' / '.join(PLATFORMS)} 之一", 422)
    require(bool(str(data.get("metric_name") or "").strip()), "请填写指标名称", 422)
    value = data.get("metric_value")
    if value in (None, ""):
        value = None
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            require(False, "数值无法解析", 422)
    observation_id = new_id()
    observed_at = str(data.get("observed_at") or "")[:40] or now()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO observations(id,platform,scope,work_ref,metric_name,metric_value,window_start,"
            "window_end,source,evidence_path,note,observed_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, platform, data.get("scope") or ("work" if data.get("work_ref") else "account"),
             str(data.get("work_ref") or "")[:300], str(data.get("metric_name"))[:100], value,
             str(data.get("window_start") or "")[:40] or None, str(data.get("window_end") or "")[:40] or None,
             str(data.get("source") or "manual")[:60], str(data.get("evidence_path") or "")[:400],
             str(data.get("note") or "")[:2000], observed_at, now()))
    return get_observation(observation_id)


def get_observation(observation_id: str) -> dict:
    with db.connect() as connection:
        row = _row(connection, "observations", observation_id)
    item = dict(row)
    item["known"] = item["metric_value"] is not None
    return item


def list_observations(platform: str | None = None, work_ref: str | None = None) -> list[dict]:
    query = "SELECT * FROM observations"
    params: list = []
    if platform or work_ref:
        query += " WHERE"
        if platform:
            query += " platform=?"
            params.append(platform)
        if work_ref:
            query += " AND" if platform else ""
            query += " work_ref=?"
            params.append(work_ref)
    query += " ORDER BY COALESCE(observed_at, recorded_at) DESC LIMIT 500"
    with db.connect() as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["known"] = item["metric_value"] is not None
        result.append(item)
    return result


def list_batches() -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM import_batches ORDER BY created_at DESC LIMIT 50").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["mapping"] = json.loads(item.get("mapping") or "{}")
        item["errors"] = json.loads(item.get("errors") or "[]")
        result.append(item)
    return result
