"""底稿快速生产（批次B）：粘贴底稿 → 提炼 → 一次确认即生产。

覆盖材料类型门禁（原创不强迫外链 / 资料要求来源）、提炼归一化（推断不自动
标已确认）、以及 produce 复用既有生产链路（选题批准 + 底稿锁定 + 任务排队）。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from backend.app import config, db, fact_base, quick_draft, service


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return tmp_path


def _draft(**changes):
    data = {
        "title": "本地模型写周报的真实用法",
        "material_type": "original",
        "platforms": ["wechat", "toutiao"],
        "content_line": "A",
        "video_target_duration_seconds": 60,
        "sources": [],
        "audience": "想用 AI 提效但怕踩坑的职场人",
        "core_view": "先把事实核清楚，再让 AI 参与生产",
        "facts": [{"claim": "用本地模型写周报要先给结构再让它填", "status": "confirmed",
                   "source_url": "", "source_date": ""}],
        "open_questions": [],
        "author_experience": "",
        "private_info": "",
        "asset_list": [],
        "platform_angles": {},
        "raw_text": "我自己试下来，让本地模型写周报，先给它一个结构模板，它填得会更稳。",
    }
    data.update(changes)
    return data


def test_fact_base_gaps_original_does_not_force_url():
    fact = {"audience": "职场人", "core_view": "先核事实再用 AI",
            "material_type": "original", "sources": [],
            "facts": [{"claim": "先给结构再让模型填", "status": "confirmed"}]}
    assert fact_base.gaps(fact) == []


def test_fact_base_gaps_research_requires_url():
    fact = {"audience": "职场人", "core_view": "先核事实再用 AI",
            "material_type": "research", "sources": [],
            "facts": [{"claim": "引用外部报道", "status": "confirmed"}]}
    assert "至少需要一个带链接的来源" in fact_base.gaps(fact)


def test_fact_base_gaps_legacy_no_material_type_stays_strict():
    # 旧底稿没有 material_type，必须沿用既有的来源门槛（资料整理从严）。
    fact = {"audience": "职场人", "core_view": "先核事实再用 AI",
            "sources": [], "facts": [{"claim": "x", "status": "confirmed"}]}
    assert "至少需要一个带链接的来源" in fact_base.gaps(fact)


def test_normalize_extraction_original_keeps_confirmed():
    result = {"title": "T", "audience": "A", "core_view": "C", "facts": [
        {"claim": "我自己的经历", "status": "confirmed", "source_url": "", "source_date": ""},
        {"claim": "引用某报道数字", "status": "unverified", "source_url": "", "source_date": ""},
    ], "open_questions": [{"question": "数字待核"}], "platform_angles": {"wechat": "角度"}}
    normalized = quick_draft._normalize_extraction(result, "original")
    assert [f["status"] for f in normalized["facts"]] == ["confirmed", "unverified"]
    assert normalized["open_questions"] == [{"question": "数字待核"}]


def test_normalize_extraction_research_downgrades_confirmed():
    # 资料整理的外部引用不能自动标已确认 —— 模型推断不冒充已核实事实。
    result = {"title": "T", "audience": "A", "core_view": "C", "facts": [
        {"claim": "外部报道", "status": "confirmed", "source_url": "", "source_date": ""},
    ], "open_questions": [], "platform_angles": {}}
    normalized = quick_draft._normalize_extraction(result, "research")
    assert normalized["facts"][0]["status"] == "unverified"


def test_extract_requires_draft_text(world):
    with pytest.raises(HTTPException) as error:
        quick_draft.extract({"draft_text": "   ", "material_type": "original"})
    assert error.value.status_code == 422


def test_produce_original_creates_topic_fact_base_and_job(world):
    result = quick_draft.produce(_draft())
    topic = result["topic"]
    assert topic["status"] == "approved"
    assert result["fact_base"]["material_type"] == "original"
    assert result["fact_base"]["status"] == "locked"  # produce_topic 已锁定底稿
    job = result["job"]
    assert job["state"] == "queued"
    with db.connect() as connection:
        snapshot = json.loads(connection.execute(
            "SELECT input_snapshot FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
    assert snapshot["_fact_base"].startswith("共享事实底稿")
    assert snapshot["content_line"] == "A"


def test_produce_research_without_source_is_rejected(world):
    with pytest.raises(HTTPException) as error:
        quick_draft.produce(_draft(material_type="research", sources=[]))
    assert error.value.status_code == 409
    assert "带链接的来源" in error.value.detail


def test_produce_blocks_crypto_topic(world):
    with pytest.raises(HTTPException) as error:
        quick_draft.produce(_draft(title="比特币炒币指南", raw_text="介绍虚拟货币和加密货币玩法"))
    assert error.value.status_code == 422
    assert "币圈" in error.value.detail
