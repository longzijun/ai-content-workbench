"""Six-platform script behavior with isolated fixtures, never publishing content."""
from copy import deepcopy
import json
import re

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app import (adapters, artifacts, capabilities, config, content, data_import, db, formats, image_assets,
                         local_generation, platform_registry, publish_ledger,
                         review, schemas, service, video_script, weekly_review)

pytestmark = pytest.mark.local_content_only
SOURCE = {"url": "https://example.invalid/c1", "title": "隔离测试依据", "note": "导入菜单允许选择文件，输出两项用于核对。"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return config.get_settings()


def script(platform="douyin", target=60):
    return {
        "platform": platform, "format": "video_script", "content_type": "video_script",
        "title": "工具导入结果核对", "cover_text": "核对导入结果", "target_duration_seconds": target,
        "shots": [
            {"id": "opening", "role": "opening", "spoken_text": "工具导入结果怎么核对？",
             "visual_type": "talking_head", "visual_notes": "待拍摄，展示导入菜单。",
             "subtitle_points": ["导入结果"], "source_refs": [SOURCE["url"]]},
            {"id": "step", "role": "body", "spoken_text": "打开工具的导入菜单，选择文件。输出两项后逐项核对。",
             "visual_type": "screen_recording", "visual_notes": "待录屏，展示选择文件操作。",
             "subtitle_points": ["选择文件", "逐项核对"], "source_refs": [SOURCE["url"]]},
            {"id": "closing", "role": "closing", "spoken_text": "先核对输出，再保存结果。",
             "visual_type": "mixed", "visual_notes": "待拍摄，展示保存按钮。",
             "subtitle_points": ["核对后保存"], "source_refs": []},
        ], "tags": ["AI"], "source_urls": [SOURCE["url"]],
    }


def text_work(platform):
    return {"platform": platform, "format": "note" if platform == "xiaohongshu" else "article",
            "title": "工具导入结果核对", "body": "打开工具的导入菜单，选择文件。输出两项后逐项核对。",
            "tags": ["AI"], "image_prompts": [], "source_urls": [SOURCE["url"]]}


def topic(platforms):
    result = service.create_topic({"title": "工具导入结果核对", "brief": SOURCE["note"],
                                   "platforms": list(platforms), "sources": [SOURCE],
                                   "content_line": "B", "stance_ids": [], "video_target_duration_seconds": 60})
    return service.decide_topic(result["id"], {"decision": "approved", "expected_revision": result["revision"]})


def freeze(value, payload):
    with db.transaction() as connection:
        version_id, _ = service._insert_version(connection, service._row(connection, "topics", value["id"]), payload, "test:c1")
    return service.get_version(version_id)


@pytest.mark.parametrize("platform", platform_registry.VIDEO_PLATFORM_IDS)
def test_video_platform_strict_schema_and_server_derivation(world, platform):
    value = script(platform)
    authored = schemas.VideoScriptWork.model_validate(value).model_dump()
    normalized = video_script.normalize_work(authored)
    assert normalized["body"] == "\n\n".join(shot["spoken_text"] for shot in value["shots"])
    assert normalized["opening_hook"] == value["shots"][0]["spoken_text"]
    assert normalized["closing_conclusion"] == value["shots"][-1]["spoken_text"]
    assert normalized["estimated_duration_seconds"] == sum(shot["duration_estimate_seconds"] for shot in normalized["shots"])
    assert [shot["order"] for shot in normalized["shots"]] == [1, 2, 3]
    spoof = {**normalized, "body": "伪造派生正文", "opening_hook": "伪造开场", "estimated_duration_seconds": 999}
    assert video_script.normalize_work(spoof) == normalized
    assert schemas.StoredVideoScriptWork.model_validate(normalized).body == normalized["body"]
    assert adapters._schema({"platforms": [platform]})["properties"]["works"]["items"]["properties"]["platform"]["enum"] == [platform]


@pytest.mark.parametrize("mutation", ["duplicate_id", "wrong_role", "blank_spoken", "wrong_visual", "article_platform", "wrong_duration"])
def test_video_structure_rejects_invalid_authoring(world, mutation):
    value = script()
    if mutation == "duplicate_id":
        value["shots"][1]["id"] = value["shots"][0]["id"]
    elif mutation == "wrong_role":
        value["shots"][0]["role"] = "body"
    elif mutation == "blank_spoken":
        value["shots"][1]["spoken_text"] = " "
    elif mutation == "wrong_visual":
        value["shots"][1]["visual_type"] = "uploaded"
    elif mutation == "article_platform":
        value["platform"] = "wechat"
    else:
        value["target_duration_seconds"] = 5
    with pytest.raises(ValueError):
        video_script.normalize_work(value)


def test_six_platform_selection_and_formal_freeze(world):
    platforms = list(platform_registry.PLATFORM_IDS)
    parsed = schemas.TopicCreate(title="六平台隔离验证", platforms=platforms, content_line="B")
    assert len(parsed.platforms) == 6
    value = topic(platforms)
    payload = {"strategy": "隔离结构验证", "works": [
        script(platform) if platform_registry.is_video_platform(platform) else text_work(platform)
        for platform in platforms]}
    version = freeze(value, payload)
    assert {work["platform"] for work in version["payload"]["works"]} == set(platforms)
    console = review.console(value["id"])
    assert len(console["platforms"]) == 6
    assert all(item["draft"]["destination"] == "workbench" for item in console["platforms"])
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM delivery_tasks").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["spoken_text", "visual_notes", "subtitle_points", "target_duration_seconds", "cover_text"])
def test_script_review_fingerprint_binds_entire_script(world, field):
    value = topic(["douyin"])
    version = freeze(value, {"strategy": "", "works": [script()]})
    entry = review.console(value["id"])["platforms"][0]
    draft = entry["draft"]
    before = review.fingerprint(draft, version, 0)
    changed = deepcopy(version)
    work = changed["payload"]["works"][0]
    if field == "subtitle_points":
        work["shots"][1][field] = ["新的字幕重点"]
    elif field in ("spoken_text", "visual_notes"):
        work["shots"][1][field] += "补充说明。"
    elif field == "target_duration_seconds":
        work[field] = 90
    else:
        work[field] += "新版"
    assert review.fingerprint(draft, changed, 0) != before


def test_persisted_text_payload_hash_is_not_changed_by_video_schema(world):
    value = topic(["wechat"])
    old_payload = {"strategy": "旧字节保留", "works": [text_work("wechat")]}
    raw = json.dumps(old_payload, ensure_ascii=False, separators=(",", ":"))
    old_hash = service.digest(old_payload)
    with db.transaction() as connection:
        connection.execute("INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at) VALUES('legacy',?,1,1,?,?,?,'2026-01-01')",
                           (value["id"], raw, old_hash, "test:legacy"))
    service.get_version("legacy")
    db.init_db()
    with db.connect() as connection:
        stored = connection.execute("SELECT payload,hash FROM versions WHERE id='legacy'").fetchone()
    assert stored["payload"] == raw
    assert stored["hash"] == old_hash


@pytest.mark.parametrize("platform", platform_registry.VIDEO_PLATFORM_IDS)
def test_video_manual_register_import_and_weekly_coverage(world, platform):
    value = topic([platform])
    version = freeze(value, {"strategy": "", "works": [script(platform)]})
    saved = publish_ledger.register(value["id"], {"platform": platform, "version_id": version["id"],
                                      "published_at": "2026-01-01T10:00:00+08:00", "url": "https://example.invalid/shared"})
    assert saved["confirmed"] is True
    assert saved["url_status"] == "present"
    assert saved["url_verification"] == "manual_required"
    assert "仅通过格式检查" in saved["note"]
    preview = data_import.preview("fixture.csv", f"platform,metric_name,metric_value\n{platform},plays,2\n".encode())
    assert not preview["errors"]
    assert preview["rows"][0]["platform"] == platform
    assert set(weekly_review.generate("2026-01-01")["coverage"]) == set(platform_registry.PLATFORM_IDS)


def test_video_never_sends_automatic_image_or_platform_work(world, monkeypatch):
    value = topic(["douyin"])
    version = freeze(value, {"strategy": "", "works": [script()]})
    from backend.app import source_images
    monkeypatch.setattr(source_images, "register_for_works", lambda *args: pytest.fail("video must not register article source images"))
    service._register_source_images(value, version["id"], version["payload"])
    with pytest.raises(image_assets.ImageAssetError, match="不执行图片或视频生成"):
        image_assets.generate_work_assets(world, value, version["id"], version["payload"]["works"][0])


def test_video_duration_is_bound_to_approved_input_and_material_fingerprint(world):
    value = topic(["douyin"])
    before = adapters.material_fingerprint(value)
    changed = {**value, "video_target_duration_seconds": 90}
    assert adapters.material_fingerprint(changed) != before
    with pytest.raises(ValueError, match="已批准选题"):
        video_script.normalize_work(script(target=90), value)


def test_shot_reference_cannot_invent_a_source_outside_approved_material(world):
    value = topic(["douyin"])
    bad = script()
    bad["shots"][1]["source_refs"] = ["https://example.invalid/invented"]
    with pytest.raises(ValueError, match="引用未纳入"):
        adapters.validate_generated_payload({"strategy": "", "works": [bad]}, value)
    allowed = {**value, "_fact_base": json.dumps({"source_url": "https://example.invalid/invented"})}
    assert video_script.reference_errors(bad, allowed) == []
    assert video_script.reference_errors(bad, {**value, "_fact_base": 'https://example.invalid/invented-longer'})


def test_mixed_generation_uses_platform_schema_and_resumes_valid_checkpoints(world, monkeypatch):
    value = topic(["wechat", "douyin"])
    value["_generation_run"] = "fixture-run"
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    calls = []

    def fake_stream(settings, request, cancel, progress, path):
        calls.append(request)
        platform = path.name.split(".")[0]
        target = re.search(r'"video_target_duration_seconds":\s*(\d+)', request["messages"][0]["content"])
        work = script(platform, int(target.group(1)) if target else 60) if platform_registry.is_video_platform(platform) else text_work(platform)
        return json.dumps({"strategy": "隔离适配器输出", "works": [work]}, ensure_ascii=False)

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", fake_stream)
    first, provider = local_generation.generate_platforms(value, world, "fixture-model", lambda: False, lambda *args: None)
    assert len(calls) == 2
    assert calls[1]["response_format"]["json_schema"]["schema"]["properties"]["works"]["items"]["properties"]["format"]["enum"] == ["video_script"]
    assert {work["platform"] for work in first["works"]} == {"wechat", "douyin"}
    second, _ = local_generation.generate_platforms(value, world, "fixture-model", lambda: False, lambda *args: None)
    assert len(calls) == 2
    assert second == first
    local_generation.generate_platforms({**value, "video_target_duration_seconds": 90}, world, "fixture-model", lambda: False, lambda *args: None)
    assert len(calls) == 4


def test_worker_attempt_has_separate_files_but_reuses_valid_finished_platform(world, monkeypatch):
    value = {**topic(["douyin"]), "_generation_run": "attempt-run", "_execution_attempt": 1}
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    calls = []

    def fake_stream(settings, request, cancel, progress, path):
        calls.append(path)
        return json.dumps({"strategy": "隔离结构验证", "works": [script()]}, ensure_ascii=False)

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", fake_stream)
    first, _ = local_generation.generate_platforms(value, world, "fixture-model", lambda: False, lambda *args: None)
    second, _ = local_generation.generate_platforms({**value, "_execution_attempt": 2}, world, "fixture-model", lambda: False, lambda *args: None)
    assert first == second and len(calls) == 1
    paths = list((world.runtime_dir / "task-workspaces").glob("content-*/attempt-*/douyin.json"))
    assert {path.parent.name for path in paths} == {"attempt-1", "attempt-2"}
    previous = next(path for path in paths if path.parent.name == "attempt-1")
    current = next(path for path in paths if path.parent.name == "attempt-2")
    original = current.read_bytes()
    previous.write_text("stale partial output", "utf-8")
    assert current.read_bytes() == original


def test_six_platform_content_filters_and_topic_clone_keep_creative_scope(world):
    value = topic(["weixin_channels", "douyin"])
    changed = service.patch_topic(value["id"], {"expected_revision": value["revision"], "video_target_duration_seconds": 90})
    cloned = content.duplicate_topic(changed["id"])
    assert cloned["platforms"] == ["weixin_channels", "douyin"]
    assert cloned["content_line"] == "B" and cloned["video_target_duration_seconds"] == 90
    assert cloned["status"] == "pending" and cloned["risk_confirmation"] is None
    results = content.list_content(platform="douyin")
    assert {item["id"] for item in results["items"]} >= {value["id"], cloned["id"]}
    assert set(content.facets(results["items"])["platforms"]) == {"weixin_channels", "douyin"}


def test_local_capability_inventory_and_output_formats_cover_all_six(world, monkeypatch):
    monkeypatch.setattr(capabilities.browser_bridge, "get_status", lambda _: {})
    snapshot = capabilities.inventory(world)
    local = snapshot["local_content"]["platforms"]
    assert {item["id"] for item in local} == set(platform_registry.PLATFORM_IDS)
    assert all(item["external_operations"] is False for item in local)
    assert formats.axis_a_platforms("video_script") == platform_registry.VIDEO_PLATFORM_IDS


def test_video_obsidian_roundtrip_preserves_shots_and_rejects_independent_body_edit(world):
    value = topic(["douyin"])
    version = freeze(value, {"strategy": "", "works": [script()]})
    saved = artifacts.sync_version(value, version, world)
    imported = artifacts.import_working_copy(value, world)
    assert imported["payload"]["works"][0]["shots"] == version["payload"]["works"][0]["shots"]
    from pathlib import Path
    path = Path(saved["path"])
    path.write_text(path.read_text("utf-8").replace("工具导入结果怎么核对？", "手工改了展示口播", 1), "utf-8")
    with pytest.raises(ValueError, match="分镜唯一派生"):
        artifacts.import_working_copy(value, world)
