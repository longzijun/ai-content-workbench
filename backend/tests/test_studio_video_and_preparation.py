"""Structured editing and review-bound preparation, isolated synthetic fixtures."""
from copy import deepcopy
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from backend.app import adapters, content_studio as studio, db, fact_base, local_generation, main, platform_registry, publish_preparation, review, service
from backend.tests.auth_helpers import bootstrap
from backend.tests.test_video_c1 import SOURCE, freeze, script, text_work, topic

pytestmark = pytest.mark.local_content_only


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(adapters, "get_health", lambda *_: {})
    db.init_db()
    value = topic(platform_registry.PLATFORM_IDS)
    fact_base.save(value["id"], {"audience": "隔离测试", "core_view": "核对操作结果", "sources": [SOURCE],
                                "facts": [{"claim": SOURCE["note"], "status": "confirmed", "source_url": SOURCE["url"]}]})
    fact_base.mark_ready(value["id"])
    payload = {"strategy": "隔离验证", "works": [script(p) if platform_registry.is_video_platform(p) else text_work(p)
                                                for p in platform_registry.PLATFORM_IDS]}
    version = freeze(value, payload)
    return value, version


def save(value, version, platform="douyin", **changes):
    current = studio.read_copy(value["id"], version["id"], platform)
    request = {key: deepcopy(current[key]) for key in ("title", "body", "tags", "source_urls", "cover_text", "shots", "target_duration_seconds")}
    request.update(expected_revision=current["revision"], **changes)
    return studio.save_copy(value["id"], version["id"], platform, request)


def test_video_copy_is_persistent_derived_and_can_freeze_six_platforms(world):
    value, version = world
    saved = save(value, version, body="不应成为真相的伪造正文", cover_text="修改封面文字")
    assert saved["body"] == "\n\n".join(s["spoken_text"] for s in saved["shots"])
    assert "伪造" not in saved["body"]
    result = studio.commit_copies(value["id"], {"base_version_id": version["id"],
        "expected_revisions": {p: 1 if p == "douyin" else 0 for p in platform_registry.PLATFORM_IDS}})
    works = result["version"]["payload"]["works"]
    assert len(works) == 6
    assert next(w for w in works if w["platform"] == "douyin")["cover_text"] == "修改封面文字"
    assert studio.read_copy(value["id"], version["id"], "douyin")["stale"]


def test_incomplete_video_is_savable_but_not_freezable_and_cas_protects(world):
    value, version = world
    current = studio.read_copy(value["id"], version["id"], "douyin")
    shots = deepcopy(current["shots"])
    shots[1]["spoken_text"] = ""
    saved = save(value, version, shots=shots)
    assert saved["revision"] == 1
    with pytest.raises(HTTPException) as error:
        studio.commit_copies(value["id"], {"base_version_id": version["id"], "expected_revisions": {p: 1 if p == "douyin" else 0 for p in platform_registry.PLATFORM_IDS}})
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        studio.save_copy(value["id"], version["id"], "douyin", {"expected_revision": 0, "title": current["title"], "shots": current["shots"]})
    assert error.value.status_code == 409
    assert studio.read_copy(value["id"], version["id"], "douyin")["shots"][1]["spoken_text"] == ""


def test_video_suggestion_targets_stable_shot_and_revision(world, monkeypatch):
    value, version = world
    copy = save(value, version)
    target = copy["shots"][1]["spoken_text"]
    request = {"expected_revision": copy["revision"], "directive": "简化表达，不增加事实", "before": target,
               "body_fingerprint": studio.body_fingerprint(target), "shot_id": copy["shots"][1]["id"], "field": "spoken_text"}
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {"id": "test-model"})
    monkeypatch.setattr(local_generation, "stream_payload_with_retry", lambda *a, **k: json.dumps({"after": target, "reason": "保留已核对操作"}, ensure_ascii=False))
    suggestion = studio.suggest(value["id"], version["id"], "douyin", request)
    assert suggestion["shot_id"] == request["shot_id"] and suggestion["field"] == "spoken_text"
    assert studio.validate_suggestion(value["id"], version["id"], "douyin", suggestion["id"])["id"] == suggestion["id"]
    save(value, version, cover_text="另一设备修改")
    with pytest.raises(HTTPException) as error:
        studio.validate_suggestion(value["id"], version["id"], "douyin", suggestion["id"])
    assert error.value.status_code == 409


def test_preparation_binds_all_selected_reviews_and_unfrozen_work(world):
    value, version = world
    initial = publish_preparation.get(value["id"])
    assert initial["ready"] is False and len(initial["platforms"]) == 6
    for entry in initial["platforms"]:
        review.record(entry["draft"]["id"], "approved", "", "隔离测试账号", "人工确认标识", True, version["id"], initial["fact_base_revision"])
    ready = publish_preparation.get(value["id"])
    assert ready["ready"] is True
    video = next(entry for entry in ready["platforms"] if entry["platform"] == "douyin")
    assert video["copy_text"] == video["work"]["body"] and video["shooting_script"]
    assert "待拍摄" in video["status_label"]
    save(value, version, cover_text="尚未冻结的改动")
    after = publish_preparation.get(value["id"])
    assert after["ready"] is False
    assert any("尚未冻结" in item for item in after["blocking"])
    assert next(entry for entry in after["platforms"] if entry["platform"] == "douyin")["work"]["cover_text"] == video["work"]["cover_text"]


def test_retired_handoff_and_zip_write_endpoints_return_gone(world):
    value, _ = world
    with TestClient(main.app) as client:
        client.headers["X-Workbench-Token"] = bootstrap(client).json()["token"]
        assert client.post(f"/api/topics/{value['id']}/workbuddy-handoff", json={"expected_revision": value["revision"]}).status_code == 410
        assert client.post("/api/workbuddy-handoffs/old/import", json={}).status_code == 410
        assert client.post(f"/api/topics/{value['id']}/material-packages", json={}).status_code == 410
        assert client.get(f"/api/topics/{value['id']}/publish-preparation").status_code == 200


def test_concurrent_freeze_cannot_return_old_preparation_as_current(world, monkeypatch):
    value, version = world
    original_console = review.console
    changed = False

    def freezing_console(topic_id):
        nonlocal changed
        data = original_console(topic_id)
        if not changed:
            changed = True
            freeze(value, version["payload"])
        return data

    monkeypatch.setattr(review, "console", freezing_console)
    with pytest.raises(HTTPException) as error:
        publish_preparation.get(value["id"])
    assert error.value.status_code == 409
