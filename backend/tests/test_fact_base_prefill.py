"""本篇资料 AI 辅助整理（prefill）：选题材料 → 本地模型提炼 → 结构化草稿。

安全边界：
- 只返回草稿不写库，作者审阅后仍走 fact-base 保存/确认；
- 资料整理/改编类型下，模型标的 confirmed 一律降级为 unverified；
- 选题说明与来源均为空时 422，不让模型凭空编内容。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from backend.app import config, db, quick_draft, service


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return tmp_path


def _topic(world, **changes):
    data = {
        "title": "乌镇峰会年年刷屏",
        "brief": "今年峰会把 AI 从演示推向场景落地，我想写清这对普通从业者的意义。",
        "platforms": ["wechat", "toutiao", "xiaohongshu"],
        "sources": [{"url": "https://example.com/report", "title": "峰会官方综述", "note": ""}],
        "content_line": "A",
        "stance_ids": [],
    }
    data.update(changes)
    return service.create_topic(data)


def _fake_extraction(monkeypatch, result):
    monkeypatch.setattr(quick_draft, "_run_extraction", lambda prompt, workspace: result)


def test_prefill_returns_draft_without_touching_fact_base(world, monkeypatch):
    topic = _topic(world)
    _fake_extraction(monkeypatch, {
        "title": "乌镇峰会", "audience": "关注 AI 落地的从业者",
        "core_view": "场景落地比演示更重要",
        "facts": [{"claim": "峰会展示了多个落地方案", "status": "confirmed",
                   "source_url": "https://example.com/report", "source_date": ""}],
        "open_questions": [{"question": "落地案例的真实规模"}],
        "platform_angles": {"wechat": "深度解读"},
    })
    result = quick_draft.prefill(topic["id"], "research")
    assert result["audience"] == "关注 AI 落地的从业者"
    # research 类型：模型标的 confirmed 必须降级
    assert result["facts"][0]["status"] == "unverified"
    # 选题登记来源并入返回
    assert result["sources"] == [{"url": "https://example.com/report", "title": "峰会官方综述"}]
    # 不写库
    from backend.app import fact_base
    assert fact_base.get(topic["id"]) is None


def test_prefill_original_keeps_confirmed(world, monkeypatch):
    topic = _topic(world, sources=[])
    _fake_extraction(monkeypatch, {
        "title": "T", "audience": "A", "core_view": "C",
        "facts": [{"claim": "我自己的实测结论", "status": "confirmed", "source_url": "", "source_date": ""}],
        "open_questions": [], "platform_angles": {},
    })
    result = quick_draft.prefill(topic["id"], "original")
    assert result["facts"][0]["status"] == "confirmed"
    assert result["sources"] == []


def test_prefill_rejects_empty_material(world):
    topic = _topic(world, brief="", sources=[])
    with pytest.raises(HTTPException) as error:
        quick_draft.prefill(topic["id"], "original")
    assert error.value.status_code == 422
    assert "没有可整理的素材" in str(error.value.detail)


def test_prefill_rejects_invalid_material_type(world):
    topic = _topic(world)
    with pytest.raises(HTTPException) as error:
        quick_draft.prefill(topic["id"], "nonsense")
    assert error.value.status_code == 422


def test_prefill_unknown_topic_404(world):
    with pytest.raises(HTTPException) as error:
        quick_draft.prefill("no-such-topic", "original")
    assert error.value.status_code == 404
