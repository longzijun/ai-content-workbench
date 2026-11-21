"""Delivery requires real version-bound images and truthful remote media proof.

No browser or HTTP client may be used by these tests. PNGs are tiny valid pixel
fixtures; the trusted-file manifest code is real, while providers are simulated.
"""
from copy import deepcopy
from pathlib import Path
import struct
import zlib

import httpx
import pytest

from backend.app import artifacts, browser_bridge, config, db, image_assets, service


SOURCE = "https://example.invalid/original-report"
ACCOUNT = "测试账号"


def payload(platform="toutiao", prompts=True):
    return {"strategy": "先核实再审核，人工发布", "works": [{"platform": platform,
            "format": "note" if platform == "xiaohongshu" else "article", "title": "审核草稿中的图片素材",
            "body": "这是一份用于审核流程测试的完整正文。事实和图片都要在发布前核实。", "tags": [],
            "image_prompts": ["AI 生成的中性示意图，非真实事件照片"] if prompts else [], "source_urls": [SOURCE]}]}


def png():
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b"")


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")

    def forbidden(*args, **kwargs):
        pytest.fail("Media delivery tests must not use real HTTP, browser, credentials or Comfy")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(browser_bridge, "_load_playwright", forbidden)
    monkeypatch.setattr(browser_bridge, "run_in_page", forbidden)
    monkeypatch.setattr(config, "get_wechat_credentials", forbidden)
    monkeypatch.setattr(artifacts, "sync_platform_draft", forbidden)
    monkeypatch.setattr(image_assets, "generate_work_assets", forbidden)
    source = tmp_path / "pixel-fixture.png"
    source.write_bytes(png())
    db.init_db()
    return {"runtime": runtime, "vault": vault, "source": source, "monkeypatch": monkeypatch}


def frozen(world, platform="toutiao", prompts=True, autosync=False):
    topic = service.create_topic({"title": "审核草稿素材", "brief": "测试中仅保存私人草稿", "platforms": [platform], "sources": [{"url": SOURCE, "title": "原始报道", "note": ""}], "content_line": "A", "stance_ids": []})
    topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    job = service.generate(topic["id"], topic["revision"])
    assert service.claim_next_job()["id"] == job["id"]
    if autosync:
        version = service.complete_job(job["id"], payload(platform, prompts), "simulated-text-provider")
    else:
        with world["monkeypatch"].context() as isolated:
            isolated.setattr(service, "_sync_created", lambda *_: None)
            version = service.complete_job(job["id"], payload(platform, prompts), "simulated-text-provider")
    draft = next(item for item in service.state()["drafts"] if item["version_id"] == version["id"])
    return topic, version, draft, job


def register(world, topic, version_id, work):
    version = service.get_version(version_id)
    return image_assets.register_assets(config.get_settings(), topic["id"], version_id, work["platform"],
        [str(world["source"])], ["AI 测试示意图"], provider="codex_imagegen",
        source={"kind": "generated", "version_hash": version["hash"], "work_hash": image_assets.work_asset_hash(work)},
        allowed_source_roots=[world["source"].parent])


def register_source(world, topic, version_id):
    """按 2026-10-20 源生图契约注册测试源图（真实 register_assets）。"""
    version = service.get_version(version_id)
    work = version["payload"]["works"][0]
    return image_assets.register_assets(config.get_settings(), topic["id"], version_id, work["platform"],
        [str(world["source"])], ["源生图｜来自原文：原始报道"], provider="source",
        source={"kind": "source", "rights_confirmed": True, "license": "source_publisher_material_user_decides",
                "authorization_id": "user-source-image-policy-2026-10-20", "caption": "", "credit": "原始报道",
                "version_hash": version["hash"], "work_hash": image_assets.work_asset_hash(work)},
        allowed_source_roots=[world["source"].parent])


def forbid_generation(world):
    def forbidden_generation(*args, **kwargs):
        pytest.fail("2026-10-20 决策：交付链路不得触发 AI 生图")
    world["monkeypatch"].setattr(image_assets, "generate_work_assets", forbidden_generation)


def remote_result(draft, media=None, **changes):
    result = {"state": "synced", "destination": "platform", "remote_id": "browser:toutiao:7412345678901234567",
              "account_label": ACCOUNT, "error": None,
              "media": media if media is not None else {"status": "verified", "expected_count": 1, "verified_count": 1, "cover_verified": True}}
    result.update(changes)
    return result


def stage(topic_id, job_id):
    return next(job["stage"] for job in service.get_topic(topic_id)["jobs"] if job["id"] == job_id)


def test_real_registered_image_is_bound_before_platform_io_and_exposed_by_version(world):
    forbid_generation(world)
    captured = []
    def save(draft, version, topic, settings):
        manifest = image_assets.get_asset_manifest(settings, topic["id"], version["id"], draft["platform"])
        assert manifest["status"] == "ready" and len(manifest["assets"]) == 1
        assert Path(manifest["assets"][0]["path"]).read_bytes() == png()
        captured.append(version["id"])
        return remote_result(draft)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", save)
    topic, version, draft, job = frozen(world)
    register_source(world, topic, version["id"])
    result = service.sync_draft(draft["id"])
    assert captured == [version["id"]] and result["state"] == "synced"
    asset = service.get_version(version["id"])["image_assets"]["toutiao"]["assets"][0]
    assert asset["width"] == 1 and asset["height"] == 1 and asset["bytes"] > 0
    assert asset["url"] == f'/api/versions/{version["id"]}/assets/toutiao/{asset["id"]}'
    assert result["media"]["status"] == "verified"
    assert "等待人工审核" in stage(topic["id"], job["id"])


def test_tampered_assets_still_block_platform_delivery(world):
    """资产完整性问题（corrupt=文件被改动）仍拦截交付。"""
    forbid_generation(world)
    topic, version, draft, job = frozen(world)
    manifest = register_source(world, topic, version["id"])
    Path(manifest["assets"][0]["path"]).write_bytes(b"corrupted image bytes")
    result = service.sync_draft(draft["id"])
    assert result["state"] == "failed" and not result["remote_id"]
    assert result["media"]["status"] != "verified"
    assert "待处理" in stage(topic["id"], job["id"])


def test_without_source_images_delivers_text_draft_and_never_triggers_ai_generation(world):
    """2026-10-20 决策：无源生图按无图交付，AI 生图入口在交付链路必须不可达。"""
    forbid_generation(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft",
        lambda draft, *args: remote_result(draft, media={"status": "not_required", "expected_count": 0, "verified_count": 0, "cover_verified": False}))
    topic, version, draft, job = frozen(world, autosync=True)
    assert draft["state"] == "synced" and draft["remote_id"]
    assert draft["media"]["status"] == "not_required"
    assert "待处理" not in stage(topic["id"], job["id"])


def test_corrupted_real_asset_does_not_reach_platform_or_claim_media_delivery(world):
    forbid_generation(world)
    topic, version, draft, job = frozen(world)
    manifest = register(world, topic, version["id"], version["payload"]["works"][0])
    Path(manifest["assets"][0]["path"]).write_bytes(b"corrupted image bytes")
    corrupted = image_assets.get_asset_manifest(config.get_settings(), topic["id"], version["id"], "toutiao")
    assert corrupted["status"] == "corrupt" and corrupted["assets"] == []
    result = service.sync_draft(draft["id"])
    assert result["state"] == "failed" and not result["remote_id"]
    assert result["media"]["status"] != "verified"
    assert "待处理" in stage(topic["id"], job["id"])


def test_text_saved_without_media_proof_keeps_delivery_pending(world):
    forbid_generation(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda draft, *args: remote_result(draft, media={"status": "unverified", "expected_count": 1, "verified_count": 0, "cover_verified": False}))
    topic, version, draft, job = frozen(world)
    register_source(world, topic, version["id"])
    result = service.sync_draft(draft["id"])
    assert result["state"] == "synced" and result["media"]["status"] == "unverified"
    assert "待处理" in stage(topic["id"], job["id"])


@pytest.mark.parametrize("media", [
    {"status": "verified", "expected_count": 1, "verified_count": 0, "cover_verified": False},
    {"status": "verified", "expected_count": 2, "verified_count": 1, "cover_verified": True},
    {"status": "not_required", "expected_count": 0, "verified_count": 0, "cover_verified": False},
    {"status": "verified", "expected_count": True, "verified_count": 1, "cover_verified": True},
])
def test_inconsistent_remote_media_claim_never_marks_complete_delivery(world, media):
    forbid_generation(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda draft, *args: remote_result(draft, media=deepcopy(media)))
    topic, version, draft, job = frozen(world)
    register_source(world, topic, version["id"])
    result = service.sync_draft(draft["id"])
    assert result["media"]["status"] == "unverified"
    assert "待处理" in stage(topic["id"], job["id"])


def test_malformed_media_proof_retains_known_id_but_cannot_complete_delivery(world):
    forbid_generation(world)
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", lambda draft, *args: remote_result(draft, media="verified"))
    topic, version, draft, job = frozen(world)
    register_source(world, topic, version["id"])
    result = service.sync_draft(draft["id"])
    assert result["remote_id"] == "browser:toutiao:7412345678901234567"
    assert result["media"]["status"] == "unverified"
    assert "待处理" in stage(topic["id"], job["id"])


@pytest.mark.parametrize("state", ["synced", "unknown"])
def test_registering_images_after_known_id_never_generates_or_recreates_the_draft(world, state):
    topic, version, draft, job = frozen(world)
    register(world, topic, version["id"], version["payload"]["works"][0])
    remote_id = "browser:toutiao:7412345678901234567"
    with db.transaction() as connection:
        connection.execute("UPDATE drafts SET state=?,remote_id=?,account_label=? WHERE id=?", (state, remote_id, ACCOUNT, draft["id"]))
    readbacks = []
    def only_known_read(draft, *args):
        assert draft["remote_id"] == remote_id
        readbacks.append(remote_id)
        return remote_result(draft, media={"status": "unverified", "expected_count": 1, "verified_count": 0, "cover_verified": False})
    world["monkeypatch"].setattr(artifacts, "sync_platform_draft", only_known_read)
    result = service.sync_draft(draft["id"])
    assert result["remote_id"] == remote_id and result["state"] == "synced"
    assert readbacks == ([] if state == "synced" else [remote_id])


def test_generated_asset_source_policy_retains_ai_label_review_and_never_authorizes_publish(world):
    topic, version, draft, job = frozen(world)
    register(world, topic, version["id"], version["payload"]["works"][0])
    result = service.get_version(version["id"])
    policy = result["source_policy"]["toutiao"]
    assert any(finding["code"] == "ai_image_label_review" for finding in policy["findings"])
    assert policy["publication_authorized"] is False and policy["internal_sources"][0]["url"] == SOURCE


def test_workbench_only_destination_avoids_provider_and_platform_and_does_not_claim_delivery(world):
    topic, version, draft, job = frozen(world)
    with db.transaction() as connection:
        connection.execute("UPDATE drafts SET destination='workbench' WHERE id=?", (draft["id"],))
    result = service.sync_draft(draft["id"])
    assert result["state"] == "local" and not result["remote_id"]
    assert "待处理" in stage(topic["id"], job["id"])
