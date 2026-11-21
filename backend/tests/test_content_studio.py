"""Content Studio persistence/concurrency and real executor boundary checks."""
from copy import deepcopy
import json

from fastapi import HTTPException
import httpx
import pytest

from backend.app import adapters, config, content_studio as studio, db, fact_base, local_generation, service

pytestmark = pytest.mark.local_content_only
PLATFORMS = ["wechat", "toutiao", "xiaohongshu"]
SOURCE = {"url": "https://example.invalid/studio", "title": "隔离测试材料", "note": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    studio.init_schema()
    topic = service.create_topic({"title": "让正文说明具体问题", "brief": "测试内容工坊保存与修改机制",
                                  "platforms": PLATFORMS, "sources": [SOURCE], "content_line": "A", "stance_ids": []})
    topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    fact_base.save(topic["id"], {
        "audience": "需要整理材料的作者", "core_view": "先讲清楚问题，再调整表达", "sources": [SOURCE],
        "facts": [{"claim": "工作稿可以在本机保存", "status": "confirmed", "source_url": SOURCE["url"]}],
    })
    fact_base.mark_ready(topic["id"])
    payload = {"strategy": "隔离测试数据，不是真实发布稿", "works": [{
        "platform": platform, "format": "note" if platform == "xiaohongshu" else "article",
        "content_form": "howto", "title": "把正文写清楚", "body": "开头说清问题。\n\n中间解释过程。\n\n结尾给出结果。",
        "tags": ["写作", "AI"], "image_prompts": [], "source_urls": [SOURCE["url"]],
    } for platform in PLATFORMS]}
    with db.transaction() as connection:
        row = service._row(connection, "topics", topic["id"])
        version_id, _ = service._insert_version(connection, row, payload, "test:fixture")
    return {"topic": topic, "version_id": version_id, "payload": payload, "monkeypatch": monkeypatch}


def read(world, platform="wechat"):
    return studio.read_copy(world["topic"]["id"], world["version_id"], platform)


def save(world, platform="wechat", **changes):
    current = read(world, platform)
    request = {"expected_revision": current["revision"], **{key: current[key] for key in ("title", "body", "tags", "source_urls")}}
    request.update(changes)
    return studio.save_copy(world["topic"]["id"], world["version_id"], platform, request)


def suggest_request(copy, **changes):
    body = copy["body"]
    data = {"expected_revision": copy["revision"], "directive": "把开头说得更具体，不新增事实",
            "before": "开头说清问题。", "prefix": "", "suffix": "\n\n中间解释过程。",
            "body_fingerprint": copy["body_fingerprint"]}
    data.update(changes)
    return data


def run_suggest(world, request):
    return studio.suggest(world["topic"]["id"], world["version_id"], "wechat", request)


def stub_model(world, result=None, side_effect=None, native=False):
    monkeypatch = world["monkeypatch"]
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda settings: {
        "id": "local/test", "capabilities": {"reasoning": {"allowed_options": ["off"] if native else []}},
    })
    calls = []

    def stream(settings, request, cancel, progress, diagnostic):
        calls.append(deepcopy(request))
        if side_effect:
            side_effect()
        return json.dumps(result if result is not None else {"after": "先说明要解决的问题。", "reason": "让开头直接交代目的。"}, ensure_ascii=False)

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", stream)
    monkeypatch.setattr(adapters, "_run_codex", lambda *args, **kwargs: pytest.fail("不得调用 Codex 兜底"))
    return calls


def test_initial_copy_is_pure_read_and_does_not_create_working_row(world, monkeypatch):
    statements = []
    connect = db.connect

    def traced():
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(db, "connect", traced)
    copy = read(world)
    assert copy["revision"] == 0
    assert copy["body"] == world["payload"]["works"][0]["body"]
    assert copy["stale"] is False
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM studio_working_copies").fetchone()[0] == 0


def test_saved_copy_persists_reconnect_and_does_not_freeze_or_invalidate(world):
    with db.connect() as connection:
        old_version_count = connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        old_drafts = [dict(row) for row in connection.execute("SELECT * FROM drafts ORDER BY id")]
    saved = save(world, body="持久化工作稿，刷新后继续编辑。")
    studio.init_schema()  # Equivalent to another startup: rows survive idempotent init.
    recovered = read(world)
    assert recovered == saved
    assert recovered["revision"] == 1
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == old_version_count
        assert [dict(row) for row in connection.execute("SELECT * FROM drafts ORDER BY id")] == old_drafts
        assert connection.execute("SELECT current_version_id FROM topics").fetchone()[0] == world["version_id"]


def test_stale_client_cannot_overwrite_another_window(world):
    first = save(world, body="窗口甲的改稿")
    with pytest.raises(HTTPException) as error:
        save(world, expected_revision=0, body="窗口乙拿旧修订号保存")
    assert error.value.status_code == 409
    assert read(world)["body"] == first["body"]


def test_platform_copies_are_isolated(world):
    save(world, "wechat", body="公众号的工作稿")
    save(world, "xiaohongshu", body="小红书的工作稿")
    assert read(world, "wechat")["body"] == "公众号的工作稿"
    assert read(world, "xiaohongshu")["body"] == "小红书的工作稿"
    assert read(world, "toutiao")["revision"] == 0


def test_commit_all_copies_atomically_preserves_unedited_fields_and_old_rows(world):
    save(world, "wechat", body="公众号改稿")
    save(world, "xiaohongshu", body="小红书改稿")
    result = studio.commit_copies(world["topic"]["id"], {
        "base_version_id": world["version_id"], "expected_revisions": {"wechat": 1, "toutiao": 0, "xiaohongshu": 1},
    })
    works = {work["platform"]: work for work in result["version"]["payload"]["works"]}
    assert works["wechat"]["body"] == "公众号改稿"
    assert works["xiaohongshu"]["body"] == "小红书改稿"
    assert works["toutiao"]["body"] == world["payload"]["works"][1]["body"]
    assert all(work["content_form"] == "howto" for work in works.values())
    assert read(world)["stale"] is True
    assert read(world)["body"] == "公众号改稿"
    with pytest.raises(HTTPException) as error:
        save(world, body="旧页继续保存")
    assert error.value.status_code == 409


def test_commit_revision_conflict_rolls_back_entire_new_version(world):
    save(world, "wechat", body="窗口甲更新的工作稿")
    with pytest.raises(HTTPException) as error:
        studio.commit_copies(world["topic"]["id"], {
            "base_version_id": world["version_id"], "expected_revisions": {"wechat": 0, "toutiao": 0, "xiaohongshu": 0},
        })
    assert error.value.status_code == 409
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert connection.execute("SELECT current_version_id FROM topics").fetchone()[0] == world["version_id"]


def test_commit_requires_full_approved_platform_scope(world):
    with pytest.raises(HTTPException) as error:
        studio.commit_copies(world["topic"]["id"], {"base_version_id": world["version_id"], "expected_revisions": {"wechat": 0}})
    assert error.value.status_code == 422


def test_incomplete_working_copy_is_durable_but_cannot_be_frozen(world):
    save(world, title="", body="")
    with pytest.raises(HTTPException) as error:
        studio.commit_copies(world["topic"]["id"], {
            "base_version_id": world["version_id"], "expected_revisions": {"wechat": 1, "toutiao": 0, "xiaohongshu": 0},
        })
    assert error.value.status_code == 422
    assert read(world)["body"] == ""
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1


def test_unapproved_topic_and_active_job_reject_working_copy_writes(world):
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET status='parked' WHERE id=?", (world["topic"]["id"],))
    with pytest.raises(HTTPException) as error:
        save(world)
    assert error.value.status_code == 409
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET status='approved' WHERE id=?", (world["topic"]["id"],))
    service.generate(world["topic"]["id"], world["topic"]["revision"])
    with pytest.raises(HTTPException) as error:
        save(world)
    assert error.value.status_code == 409


@pytest.mark.parametrize("native", [False, True])
def test_suggestion_uses_local_executor_shared_material_and_never_changes_copy(world, native):
    copy = save(world)
    calls = stub_model(world, native=native)
    suggestion = run_suggest(world, suggest_request(copy))
    assert suggestion["before"] == "开头说清问题。"
    assert suggestion["after"] == "先说明要解决的问题。"
    assert suggestion["provider"] == "local:local/test"
    assert suggestion["work_revision"] == copy["revision"]
    assert read(world) == copy
    assert len(calls) == 1
    prompt = calls[0]["input"] if native else calls[0]["messages"][0]["content"]
    assert "共享事实底稿" in prompt
    assert "不默认介绍职业" in prompt
    assert "只改给定选中文字" in prompt
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM studio_suggestions").fetchone()[0] == 1


def test_repeated_text_requires_unique_context(world):
    copy = save(world, body="第一处：相同句子。\n第二处：相同句子。")
    calls = stub_model(world)
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy, before="相同句子。", prefix="", suffix=""))
    assert error.value.status_code == 409
    assert calls == []
    selected = run_suggest(world, suggest_request(copy, before="相同句子。", prefix="第二处：", suffix=""))
    assert selected["before"] == "相同句子。"
    assert selected["prefix"] == "第二处："


def test_body_revision_or_fingerprint_mismatch_rejected_before_model(world):
    copy = save(world)
    calls = stub_model(world)
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy, body_fingerprint="0" * 64))
    assert error.value.status_code == 409
    assert calls == []


def test_working_copy_change_during_model_call_rejects_old_suggestion(world):
    copy = save(world)
    stub_model(world, side_effect=lambda: save(world, body="作者在等待建议时手动修改。"))
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 409
    assert read(world)["body"] == "作者在等待建议时手动修改。"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM studio_suggestions").fetchone()[0] == 0


def test_material_change_during_model_call_rejects_old_suggestion(world):
    copy = save(world)

    def change_fact():
        fact = fact_base.get(world["topic"]["id"])
        fact_base.save(world["topic"]["id"], {**fact, "core_view": "作者更新了判断"})
        fact_base.mark_ready(world["topic"]["id"])

    stub_model(world, side_effect=change_fact)
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 409
    assert read(world) == copy


def test_model_unavailable_keeps_text_without_fallback(world, monkeypatch):
    copy = save(world)
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda settings: {})
    monkeypatch.setattr(adapters, "_run_codex", lambda *args, **kwargs: pytest.fail("不得兜底"))
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 503
    assert read(world) == copy


def test_invalid_output_or_prompt_leak_is_not_a_successful_suggestion(world):
    copy = save(world)
    stub_model(world, result={"after": "材料边界：请只根据选题输出正文。", "reason": "测试不应通过"})
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 422
    assert read(world) == copy


def test_private_material_is_not_sent_to_model_and_leaked_output_is_rejected(world):
    fact = fact_base.get(world["topic"]["id"])
    secret = "内部测试口令不得公开甲乙丙"
    fact_base.save(world["topic"]["id"], {**fact, "private_info": secret})
    fact_base.mark_ready(world["topic"]["id"])
    copy = save(world)
    calls = stub_model(world, result={"after": secret, "reason": "错误地泄露了不可公开信息"})
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 422
    assert secret not in json.dumps(calls[0], ensure_ascii=False)
    assert read(world) == copy


def test_connection_failure_is_reported_without_erasing_copy(world, monkeypatch):
    copy = save(world)
    stub_model(world)

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("isolated unavailable model")

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", unavailable)
    with pytest.raises(HTTPException) as error:
        run_suggest(world, suggest_request(copy))
    assert error.value.status_code == 503
    assert read(world) == copy


def test_acceptance_guard_rechecks_material_after_generation(world):
    copy = save(world)
    stub_model(world)
    suggestion = run_suggest(world, suggest_request(copy))
    assert studio.validate_suggestion(world["topic"]["id"], world["version_id"], "wechat", suggestion["id"]) == suggestion
    fact = fact_base.get(world["topic"]["id"])
    fact_base.save(world["topic"]["id"], {**fact, "core_view": "生成建议以后更新判断"})
    fact_base.mark_ready(world["topic"]["id"])
    with pytest.raises(HTTPException) as error:
        studio.validate_suggestion(world["topic"]["id"], world["version_id"], "wechat", suggestion["id"])
    assert error.value.status_code == 409


def test_acceptance_guard_rejects_wrong_platform_and_new_working_revision(world):
    copy = save(world)
    stub_model(world)
    suggestion = run_suggest(world, suggest_request(copy))
    with pytest.raises(HTTPException) as error:
        studio.validate_suggestion(world["topic"]["id"], world["version_id"], "toutiao", suggestion["id"])
    assert error.value.status_code == 404
    save(world, body="作者已手动改过正文")
    with pytest.raises(HTTPException) as error:
        studio.validate_suggestion(world["topic"]["id"], world["version_id"], "wechat", suggestion["id"])
    assert error.value.status_code == 409
