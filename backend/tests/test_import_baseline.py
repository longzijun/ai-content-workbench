"""Obsidian import consumes only its snapshot and preserves subsequent edits."""
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import adapters, artifacts, config, db, main, service, worker


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(worker, "start_worker", lambda: None)
    monkeypatch.setattr(worker, "stop_worker", lambda: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Import tests must not contact a provider or a real platform")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(config, "get_wechat_credentials", forbidden)
    calls = []

    def disconnected(draft, *args):
        calls.append(draft["id"])
        return {"state": "needs_connection", "error": "平台未接入"}

    monkeypatch.setattr(artifacts, "sync_platform_draft", disconnected)
    db.init_db()
    return {"vault": vault, "runtime": runtime, "monkeypatch": monkeypatch, "calls": calls}


def generated(platform="toutiao", title="可编辑的工作稿"):
    topic = service.create_topic({"title": "工作稿同步", "brief": "测试导入与同步", "platforms": [platform], "sources": [], "content_line": "A", "stance_ids": []})
    topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": 1})
    job = service.generate(topic["id"], 1)
    assert service.claim_next_job()["id"] == job["id"]
    payload = {"strategy": "编辑后重新审核", "works": [{"platform": platform, "format": "article", "title": title, "body": "导入前的正文。", "tags": [], "image_prompts": [], "source_urls": []}]}
    version = service.complete_job(job["id"], payload, "test-provider")
    return topic, version


def externally_edited_copy(topic, version):
    result = service.sync_topic(topic["id"], version["id"])
    assert result["status"] == "written"
    path = Path(result["path"])
    modified = path.read_text("utf-8").replace("导入前的正文。", "已经在 Obsidian 修改的正文。")
    path.write_text(modified, encoding="utf-8")
    return path


def test_imported_external_edit_becomes_baseline_and_next_sync_succeeds(isolated):
    topic, version = generated()
    old_draft = next(draft for draft in service.state()["drafts"] if draft["version_id"] == version["id"])
    path = externally_edited_copy(topic, version)
    imported_bytes = path.read_bytes()
    imported = service.import_topic(topic["id"], version["id"])

    assert imported["provider"] == "obsidian-import"
    assert imported["payload"]["works"][0]["body"] == "已经在 Obsidian 修改的正文。"
    assert path.read_bytes() == imported_bytes
    assert service.get_draft(old_draft["id"])["state"] == "stale"
    with db.connect() as connection:
        record = connection.execute("SELECT * FROM sync_records WHERE topic_id=?", (topic["id"],)).fetchone()
        assert record["version_id"] == imported["id"]
        assert record["baseline"] == hashlib.sha256(imported_bytes).hexdigest()

    synced = service.sync_topic(topic["id"], imported["id"])
    assert synced["status"] == "written"
    assert "已经在 Obsidian 修改的正文。" in path.read_text("utf-8")
    assert imported["id"] in path.read_text("utf-8")

    # A further user edit is a new event, and must still be preserved as a conflict.
    latest_bytes = path.read_bytes() + "\n新的外部记录，尚未导入。\n".encode("utf-8")
    path.write_bytes(latest_bytes)
    conflict = service.sync_topic(topic["id"], imported["id"])
    assert conflict["status"] == "conflict"
    assert path.read_bytes() == latest_bytes
    assert Path(conflict["conflict_path"]).exists()


def test_external_edit_after_snapshot_is_not_incorporated_into_import_baseline(isolated):
    topic, version = generated()
    path = externally_edited_copy(topic, version)
    imported_bytes = path.read_bytes()
    real_import = artifacts.import_working_copy
    newer_bytes = imported_bytes + "\n读取快照之后新增的外部记录。\n".encode("utf-8")

    def changed_after_read(*args):
        result = real_import(*args)
        path.write_bytes(newer_bytes)
        return result

    isolated["monkeypatch"].setattr(artifacts, "import_working_copy", changed_after_read)
    imported = service.import_topic(topic["id"], version["id"])
    with db.connect() as connection:
        baseline = connection.execute("SELECT baseline FROM sync_records WHERE topic_id=?", (topic["id"],)).fetchone()["baseline"]
    assert baseline == hashlib.sha256(imported_bytes).hexdigest()
    assert baseline != hashlib.sha256(newer_bytes).hexdigest()
    conflict = service.sync_topic(topic["id"], imported["id"])
    assert conflict["status"] == "conflict"
    assert path.read_bytes() == newer_bytes


def test_ready_is_public_and_checks_origin_without_exposing_settings(isolated):
    with TestClient(main.app) as client:
        result = client.get("/api/ready")
        assert result.status_code == 200
        assert result.json() == {"app_id": "self-media-workbench", "version": "0.2.0",
                                 "api_layout": "web-worker", "release_id": "development"}
        assert not client.cookies
        assert client.get("/api/ready", headers={"Origin": "https://foreign.example"}).status_code == 403
        assert client.get("/api/ready", headers={"Host": "foreign.example"}).status_code == 400


@pytest.mark.parametrize("length,blocked", [(32, False), (33, True)])
def test_wechat_title_rule_matches_official_draft_limit(isolated, length, blocked):
    if blocked:
        with pytest.raises(HTTPException) as error:
            generated(platform="wechat", title="字" * length)
        assert error.value.status_code == 422
        assert "公众号标题需为 1 到 32 字" in error.value.detail
        assert isolated["calls"] == []
        return
    _, version = generated(platform="wechat", title="字" * length)
    assert version["review"]["status"] == "passed"
    draft = next(item for item in service.state()["drafts"] if item["version_id"] == version["id"])
    assert draft["state"] == "needs_connection"
    assert bool(isolated["calls"])
