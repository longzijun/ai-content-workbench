"""共享事实底稿（V3 阶段2）。

一个选题一份底稿、可修订；三平台作品都从同一底稿分别改编。规则：

- 底稿四态：``draft``（可改）→ ``ready``（缺口补齐，可投产）→
  ``locked``（已进入生产快照）。locked 后内容再变化会产生新 revision，
  旧 revision 的历史作品指纹随之失配 → 强制重审（见 review.py）。
- 缺关键事实（受众/观点/已确认事实/来源）时 ``gaps()`` 非空，生产入口
  直接拒绝并逐条列出，任务停在「待补资料」。
- ``private_info``（不可公开信息）与 ``author_experience``（作者真实经历）
  只进 prompt 约束，不产生虚构亲测；缺实测时作品按资料整理表达。
- ``prompt_text()`` 是进模型快照的唯一形态：纯文本，带上「只用已确认
  事实」的硬规则；底稿原文留在工作台受控区，不直接混进交付包。
"""
from __future__ import annotations

import json

from . import db
from .service import now, require

JSON_FIELDS = ("sources", "facts", "open_questions", "asset_list")

#: 底稿材料类型（批次B）。原创/真实经历不强迫外链；资料整理/改编仍要求来源。
MATERIAL_TYPES = ("original", "research", "adaptation")
MATERIAL_TYPE_LABELS = {"original": "个人原创/真实经历", "research": "资料整理", "adaptation": "已有文章改编"}
#: 旧底稿没有 material_type，按「资料整理」从严处理，保持既有来源门槛不变。
DEFAULT_MATERIAL_TYPE = "research"


def _view(row) -> dict:
    result = dict(row)
    for key in JSON_FIELDS:
        result[key] = json.loads(result.get(key) or "[]")
    result["platform_angles"] = json.loads(result.get("platform_angles") or "{}")
    return result


def get(topic_id: str) -> dict | None:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM fact_bases WHERE topic_id=? ORDER BY revision DESC LIMIT 1",
            (topic_id,)).fetchone()
    return _view(row) if row else None


def gaps(fact_base: dict) -> list[str]:
    """关键事实缺口。非空即「待补资料」，不允许投产。"""
    items = []
    if not str(fact_base.get("audience") or "").strip():
        items.append("受众问题未填写")
    if not str(fact_base.get("core_view") or "").strip():
        items.append("核心观点未填写")
    facts = [item for item in (fact_base.get("facts") or []) if isinstance(item, dict)]
    if not any(item.get("status") == "confirmed" and str(item.get("claim") or "").strip() for item in facts):
        items.append("至少需要一条已确认（confirmed）的事实依据")
    # 来源门槛：原创/真实经历不强迫外链 —— 作者自己的材料与经历即为依据，
    # 不要求编造虚假网址。资料整理/改编（含旧底稿，无 material_type）仍要求
    # 至少一个带链接的来源：外部事实、数字与引用继续要求来源或待核标记。
    if (fact_base.get("material_type") or DEFAULT_MATERIAL_TYPE) != "original":
        sources = [item for item in (fact_base.get("sources") or []) if isinstance(item, dict)]
        if not any(str(item.get("url") or "").strip() for item in sources):
            items.append("至少需要一个带链接的来源")
    return items


def _normalize(data: dict) -> dict:
    def text(value, limit=20000):
        return str(value or "")[:limit]

    def items(value):
        return [item for item in (value or []) if isinstance(item, dict)]

    material_type = data.get("material_type")
    return {
        "audience": text(data.get("audience")),
        "core_view": text(data.get("core_view")),
        "sources": items(data.get("sources")),
        "facts": items(data.get("facts")),
        "open_questions": items(data.get("open_questions")),
        "author_experience": text(data.get("author_experience")),
        "private_info": text(data.get("private_info")),
        "asset_list": items(data.get("asset_list")),
        "platform_angles": data.get("platform_angles") if isinstance(data.get("platform_angles"), dict) else {},
        "material_type": material_type if material_type in MATERIAL_TYPES else None,
        "raw_text": str(data.get("raw_text") or "")[:30000],
    }


def save(topic_id: str, data: dict) -> dict:
    """保存底稿。

    draft 态原地更新（不涨 revision）；ready/locked 态内容变化 → 新
    revision 且回到 draft（需重新确认才能投产），历史作品指纹随之失配。
    """
    values = _normalize(data)
    with db.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM fact_bases WHERE topic_id=? ORDER BY revision DESC LIMIT 1",
            (topic_id,)).fetchone()
        if current is not None and current["status"] == "draft":
            connection.execute(
                "UPDATE fact_bases SET audience=?,core_view=?,sources=?,facts=?,open_questions=?,"
                "author_experience=?,private_info=?,asset_list=?,platform_angles=?,material_type=?,raw_text=?,updated_at=? WHERE id=?",
                (values["audience"], values["core_view"], json.dumps(values["sources"], ensure_ascii=False),
                 json.dumps(values["facts"], ensure_ascii=False), json.dumps(values["open_questions"], ensure_ascii=False),
                 values["author_experience"], values["private_info"],
                 json.dumps(values["asset_list"], ensure_ascii=False), json.dumps(values["platform_angles"], ensure_ascii=False),
                 values["material_type"], values["raw_text"], now(), current["id"]))
            row = connection.execute("SELECT * FROM fact_bases WHERE id=?", (current["id"],)).fetchone()
        else:
            from .service import new_id
            revision = (current["revision"] + 1) if current is not None else 1
            new = new_id()
            connection.execute(
                "INSERT INTO fact_bases(id,topic_id,revision,audience,core_view,sources,facts,open_questions,"
                "author_experience,private_info,asset_list,platform_angles,material_type,raw_text,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new, topic_id, revision, values["audience"], values["core_view"],
                 json.dumps(values["sources"], ensure_ascii=False), json.dumps(values["facts"], ensure_ascii=False),
                 json.dumps(values["open_questions"], ensure_ascii=False), values["author_experience"],
                 values["private_info"], json.dumps(values["asset_list"], ensure_ascii=False),
                 json.dumps(values["platform_angles"], ensure_ascii=False), values["material_type"],
                 values["raw_text"], "draft", now(), now()))
            row = connection.execute("SELECT * FROM fact_bases WHERE id=?", (new,)).fetchone()
    return _view(row)


def require_revision(topic_id: str, revision: int) -> str:
    from .service import new_id
    return new_id()


def mark_ready(topic_id: str) -> dict:
    fact_base = get(topic_id)
    require(fact_base is not None, "请先建立共享事实底稿", 409)
    missing = gaps(fact_base)
    require(not missing, "事实底稿还有缺口：" + "；".join(missing), 409)
    with db.transaction() as connection:
        connection.execute("UPDATE fact_bases SET status='ready',updated_at=? WHERE id=?", (now(), fact_base["id"]))
    return get(topic_id)


def lock(connection, topic_id: str) -> dict:
    """投产前锁定底稿（在调用方的事务内执行）。"""
    row = connection.execute(
        "SELECT * FROM fact_bases WHERE topic_id=? ORDER BY revision DESC LIMIT 1",
        (topic_id,)).fetchone()
    require(row is not None, "请先建立共享事实底稿", 409)
    view = _view(row)
    missing = gaps(view)
    require(not missing, "事实底稿还有缺口：" + "；".join(missing), 409)
    require(row["status"] in ("ready", "locked"), "请先确认事实底稿", 409)
    if row["status"] != "locked":
        connection.execute("UPDATE fact_bases SET status='locked',updated_at=? WHERE id=?", (now(), row["id"]))
    return view


def prompt_text(fact_base: dict) -> str:
    """进模型快照的底稿文本。只用已确认事实；未实测不得写亲测。"""
    lines = ["共享事实底稿（修订 %s，三平台创作必须严格依据，不得外溢到没有的事实）：" % fact_base.get("revision", 1)]
    lines.append("受众问题：%s" % (fact_base.get("audience") or "（未填写）"))
    lines.append("核心观点：%s" % (fact_base.get("core_view") or "（未填写）"))
    facts = fact_base.get("facts") or []
    if facts:
        lines.append("逐项事实：")
        for item in facts:
            status = {"confirmed": "已确认", "unverified": "未核实", "disputed": "有争议"}.get(item.get("status"), item.get("status") or "未标注")
            lines.append("- [%s] %s（来源：%s；资料日期：%s）" % (
                status, item.get("claim") or "", item.get("source_url") or "未记录", item.get("source_date") or "未记录"))
    else:
        lines.append("逐项事实：（无）")
    questions = [item.get("question") for item in (fact_base.get("open_questions") or []) if item.get("question")]
    lines.append("待核问题：%s" % ("；".join(questions) if questions else "（无）"))
    lines.append("作者真实经历/实测：%s" % (fact_base.get("author_experience") or "（未提供——不得虚构「我亲测」「我用了一个月」等经历，按资料整理与方法说明表达）"))
    lines.append("不可公开信息：%s（以上内容不得进入任何作品）" % (fact_base.get("private_info") or "（无）"))
    angles = fact_base.get("platform_angles") or {}
    if angles:
        lines.append("三平台角度：" + "；".join("%s：%s" % (key, value) for key, value in angles.items()))
    assets = [item.get("name") for item in (fact_base.get("asset_list") or []) if item.get("name")]
    lines.append("素材清单：%s" % ("；".join(assets) if assets else "（无——不为凑数自动取图）"))
    lines.append("硬规则：只允许使用上面已确认的事实；未确认事实要么省略，要么明确标注待核实；不编造经历、测试、引用、平台规则或数字。")
    return "\n".join(lines)
