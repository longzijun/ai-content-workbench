"""Public generation material shared by full-text and local-edit requests.

No private_info value enters these snapshots. Cached research retains its original
verification state and never becomes a confirmed fact merely by being projected.
"""
from __future__ import annotations

import json

from . import format_decisions, research


def studied_material(topic: dict) -> dict | None:
    studied = research.cached(topic)
    if not studied:
        return None
    documents = [document for document in (studied.get("documents") or []) if isinstance(document, dict)]
    if not documents:
        return None
    return {"documents": documents, "errors": studied.get("errors") or [], "facts_verified": False}


def snapshot(connection, topic: dict, fact_text: str | None) -> dict:
    result = {
        key: topic.get(key)
        for key in ("id", "title", "brief", "revision", "status", "content_line", "stance_ids")
    }
    raw_stances = result["stance_ids"]
    if isinstance(raw_stances, str):
        raw_stances = json.loads(raw_stances or "[]")
    result["stance_ids"] = list(raw_stances or [])
    result["platforms"] = json.loads(topic["platforms"]) if isinstance(topic.get("platforms"), str) else list(topic.get("platforms") or [])
    result["sources"] = json.loads(topic["sources"]) if isinstance(topic.get("sources"), str) else list(topic.get("sources") or [])
    if any(platform in {"weixin_channels", "kuaishou", "douyin"} for platform in result["platforms"]):
        result["video_target_duration_seconds"] = int(topic.get("video_target_duration_seconds") or 60)
    if fact_text:
        result["_fact_base"] = fact_text
    studied = studied_material({**topic, "sources": result["sources"]})
    if studied:
        result["_research"] = studied
    decision = format_decisions.snapshot(topic["id"], connection)
    if decision:
        result[format_decisions.DECISION_KEY] = decision
    return result


def material_source(value: dict) -> str:
    if value.get("_fact_base"):
        return "fact_base"
    if value.get("_research"):
        return "cached_research"
    return "none"
