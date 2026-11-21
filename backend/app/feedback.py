"""匿名读者反馈（V3 阶段4）。

- 反馈由作者从平台匿名摘录后录入，可关联选题。
- AI 只做整理（常见问题、续篇题目、回复草稿），**不发送**任何评论或私信。
- 模型不可用时如实返回未整理状态，不用占位文本冒充整理结果。
"""
from __future__ import annotations

import json

from . import db
from .config import get_settings
from .service import _row, encode, new_id, now, require


def add(data: dict) -> dict:
    text = str(data.get("text") or "").strip()
    require(text, "请填写反馈内容", 422)
    topic_id = str(data.get("topic_id") or "") or None
    if topic_id:
        with db.connect() as connection:
            _row(connection, "topics", topic_id)
    feedback_id = new_id()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO feedback_items(id,topic_id,platform,text,status,created_at) VALUES(?,?,?,?,'new',?)",
            (feedback_id, topic_id, str(data.get("platform") or "")[:20], text[:20000], now()))
    return get(feedback_id)


def get(feedback_id: str) -> dict:
    with db.connect() as connection:
        row = _row(connection, "feedback_items", feedback_id)
    return _view(row)


def _view(row) -> dict:
    item = dict(row)
    item["organized"] = item["status"] == "organized"
    return item


def list_items(topic_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM feedback_items"
    params: tuple = ()
    if topic_id:
        query += " WHERE topic_id=?"
        params = (topic_id,)
    query += " ORDER BY created_at DESC LIMIT 200"
    with db.connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [_view(row) for row in rows]


def organize(feedback_id: str) -> dict:
    """用本地模型整理一条反馈。不可用时降级并如实说明。"""
    item = get(feedback_id)
    settings = get_settings()
    base = settings.local_base_url.rstrip("/")
    prompt = (
        "你是自媒体工作台的内容整理助手。下面是一条读者匿名反馈。请整理成 JSON："
        '{"common_questions":[string],"sequel_ideas":[string],"reply_draft":string}。'
        "只依据反馈原文，不编造读者没说的问题；回复草稿保持克制、不承诺做不到的事。"
        "严格输出 JSON，不要代码围栏。\n反馈原文：" + item["text"])
    try:
        import httpx
        response = httpx.post(
            base + "/chat/completions",
            json={"messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 1200, "stream": False},
            timeout=min(120, settings.task_timeout_seconds))
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content[content.index("{"):content.rindex("}") + 1])
        summary = json.dumps(parsed, ensure_ascii=False)
    except Exception as error:
        return {"feedback": item, "organized": False,
                "error": "本地模型暂不可用，未生成整理结果（%s）；可稍后重试或人工整理。" % type(error).__name__}
    with db.transaction() as connection:
        connection.execute("UPDATE feedback_items SET summary=?,status='organized' WHERE id=?", (summary[:8000], feedback_id))
    result = get(feedback_id)
    result["organized_payload"] = parsed
    return result
