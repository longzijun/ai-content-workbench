"""平台草稿链路隔离验证：排队后撤权/换账号/改稿/超时防重复（假适配器）。

默认 conftest 放行 platform_operations_allowed；假适配器 mock sync_platform_draft，
验证 worker 执行前的逐平台许可 + 审阅指纹复核，以及结果不明时不重复创建。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import artifacts, config, db, delivery, fact_base, main, runtime_mode, service, worker
from backend.app.runtime_mode import platform_draft_supported as _real_supported
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
from backend.tests.test_workflow import completed


@pytest.fixture
def gate(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(worker, "start_worker", lambda: None)
    monkeypatch.setattr(worker, "stop_worker", lambda: None)
    from backend.app import adapters, image_assets
    monkeypatch.setattr(adapters, "get_health", lambda settings: {"cloud": {"enabled": False}})
    monkeypatch.setattr(image_assets, "generate_work_assets", lambda *a, **k: {"status": "ready", "assets": [], "error": None})
    monkeypatch.setattr(artifacts, "sync_platform_draft", lambda *a, **k: {"state": "needs_connection", "error": "未存入平台"})
    with TestClient(main.app) as client:
        bootstrap = authenticated_bootstrap(client)
        client.headers["X-Workbench-Token"] = bootstrap.json()["token"]
        yield {"client": client, "monkeypatch": monkeypatch, "runtime": runtime}


def _reviewed(draft_id):
    return {"result": "approved", "comment": "", "account_label": "公众号-主账号", "ai_disclosure": "AI辅助生成，已人工核实", "facts_confirmed": True}


def _ready_draft(gate):
    """completed → 启用许可 → 逐平台审阅通过（review-platform）→ 入队。"""
    _, version, drafts = completed(None)
    draft = drafts[0]
    runtime_mode.set_platform_access("wechat", True, "公众号-主账号")
    response = gate["client"].post(f"/api/drafts/{draft['id']}/review-platform", json=_reviewed(draft["id"]))
    assert response.status_code == 200, response.text
    response = gate["client"].post(f"/api/drafts/{draft['id']}/queue", json={})
    assert response.status_code == 200, response.text
    with db.connect() as connection:
        row = connection.execute("SELECT state FROM delivery_tasks WHERE draft_id=?", (draft["id"],)).fetchone()
    assert row and row["state"] == "queued"
    return service.get_draft(draft["id"])


def _task(draft_id):
    with db.connect() as connection:
        return dict(connection.execute("SELECT state, error FROM delivery_tasks WHERE draft_id=?", (draft_id,)).fetchone())


def test_revoke_after_enqueue_blocks_run(gate):
    """排队后撤权：worker 执行前复核逐平台许可，拒绝对外保存。"""
    draft = _ready_draft(gate)
    calls = []
    gate["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda d, v, t, s: (calls.append(d["id"]) or {"state": "synced", "remote_id": "remote-1"}))
    gate["monkeypatch"].setattr(runtime_mode, "platform_draft_supported", _real_supported)
    runtime_mode.set_platform_access("wechat", False)  # 排队后撤权
    assert delivery.run_one() is True
    task = _task(draft["id"])
    assert task["state"] == "failed" and "未启用" in (task["error"] or "")
    assert calls == []


def test_account_change_after_enqueue_invalidates_review(gate):
    """排队后换账号：审阅指纹失效，worker 拒绝。"""
    draft = _ready_draft(gate)
    calls = []
    gate["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda d, v, t, s: (calls.append(d["id"]) or {"state": "synced", "remote_id": "remote-2"}))
    with db.transaction() as connection:
        connection.execute("UPDATE drafts SET account_label='换掉后的账号' WHERE id=?", (draft["id"],))
    assert delivery.run_one() is True
    task = _task(draft["id"])
    assert task["state"] == "failed" and "重审" in (task["error"] or "")
    assert calls == []


def test_body_edit_after_enqueue_invalidates_review(gate):
    """排队后改稿：审阅指纹失效，worker 拒绝。"""
    draft = _ready_draft(gate)
    calls = []
    gate["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda d, v, t, s: (calls.append(d["id"]) or {"state": "synced", "remote_id": "remote-3"}))
    fact = fact_base.get(draft["topic_id"]) or {
        "audience": "测试受众", "core_view": "测试观点", "sources": [{"url": "https://example.com/reference", "title": "测试来源", "note": ""}],
        "facts": [], "author_experience": "", "private_info": "", "open_questions": [], "platform_angles": {"wechat": "测试"}
    }
    fact["facts"] = [{"status": "confirmed", "claim": "排队后改掉的底稿事实", "source_url": "https://example.com/reference"}]
    fact_base.save(draft["topic_id"], fact)
    assert delivery.run_one() is True
    task = _task(draft["id"])
    assert task["state"] == "failed"
    assert calls == []


def test_unknown_sync_result_never_recreates(gate):
    """结果不明（unknown）且无 remote_id 时，再次同步被拒绝，不重复创建。"""
    _, version, drafts = completed(None)
    draft = drafts[0]
    runtime_mode.set_platform_access("wechat", True, "公众号-主账号")
    gate["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda d, v, t, s: {"state": "unknown", "remote_id": None, "error": "保存结果不确定"})
    result = service.sync_draft(draft["id"], verify_media=True)
    assert result["state"] == "unknown"
    current = service.get_draft(draft["id"])
    assert current["state"] == "unknown" and not current["remote_id"]
    with pytest.raises(HTTPException) as error:
        service.sync_draft(draft["id"], verify_media=True)
    assert "核对" in str(error.value.detail)


def test_reviewed_draft_can_be_enqueued(gate):
    """回归：审阅通过的草稿能够入队（enqueue_in 不再跳过 reviewed）。"""
    draft = _ready_draft(gate)
    assert draft["state"] == "reviewed"
