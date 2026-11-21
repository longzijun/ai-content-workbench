"""C0 service gates, migration defaults, fingerprints and freeze behavior."""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from backend.app import adapters, config, content_studio, db, main, service, style_library

SOURCE = {"url": "https://example.invalid/c0", "title": "隔离原始材料", "note": "原始报道，已提供具体工具设置和输出结果。"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    content_studio.init_schema()
    return tmp_path


def make_topic(title="工具导入与结果核对", brief="说明具体设置和输出结果", content_line="A", stance_ids=None):
    return service.create_topic({
        "title": title, "brief": brief, "platforms": ["wechat"], "sources": [SOURCE],
        "content_line": content_line, "stance_ids": stance_ids or [],
    })


def approve(topic):
    return service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})


def payload(body="打开工具的导入菜单，选择文件。\n\n输出结果有 2 条，和修改前一致。"):
    return {"strategy": "C0 隔离测试", "works": [{
        "platform": "wechat", "format": "article", "title": "工具导入结果核对", "body": body,
        "tags": ["AI"], "image_prompts": [], "source_urls": [SOURCE["url"]],
    }]}


def insert_version(topic, value=None):
    with db.transaction() as connection:
        row = service._row(connection, "topics", topic["id"])
        version_id, _ = service._insert_version(connection, row, value or payload(), "test:c0")
    return service.get_version(version_id)


def test_c0_schema_migration_is_idempotent(world):
    db.init_db()
    with db.connect() as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(topics)")]
    assert columns.count("content_line") == 1
    assert columns.count("stance_ids") == 1
    assert columns.count("risk_confirmation") == 1


def test_old_topic_defaults_to_null_content_line_and_cannot_generate(world):
    topic = make_topic(content_line=None)
    assert topic["content_line"] is None
    assert topic["stance_ids"] == []
    topic = approve(topic)
    with pytest.raises(HTTPException, match="明确选择 A/B/C 内容线") as error:
        service.generate(topic["id"], topic["revision"])
    assert error.value.status_code == 409


def test_explicit_content_line_and_stances_enter_immutable_job_snapshot(world):
    topic = approve(make_topic(content_line="B", stance_ids=["example-check-the-result"]))
    job = service.generate(topic["id"], topic["revision"])
    with db.connect() as connection:
        snapshot = json.loads(service._row(connection, "jobs", job["id"])["input_snapshot"])
    assert snapshot["content_line"] == "B"
    assert snapshot["stance_ids"] == ["example-check-the-result"]


def test_unknown_stance_id_cannot_be_silently_used(world):
    topic = approve(make_topic(stance_ids=["model-invented-position"]))
    with pytest.raises(HTTPException, match="未确认或无效的观点 ID"):
        service.generate(topic["id"], topic["revision"])


def test_finance_and_current_affairs_require_current_revision_confirmation(world):
    topic = make_topic(title="最新金融政策怎样影响工具选择")
    assert topic["risk_confirmation"] is None
    with pytest.raises(HTTPException, match="当前选题涉及金融、时事"):
        approve(topic)
    confirmed = service.confirm_topic_risks(topic["id"], {
        "expected_revision": topic["revision"], "risks": ["finance", "current_affairs"],
    })
    assert confirmed["risk_confirmation"]["revision"] == topic["revision"]
    approved = approve(confirmed)
    job = service.generate(approved["id"], approved["revision"])
    assert job["state"] == "queued"
    changed = service.patch_topic(topic["id"], {
        "expected_revision": approved["revision"], "brief": "修订范围后需要重新确认",
    })
    assert changed["revision"] == approved["revision"] + 1
    assert changed["risk_confirmation"] is None
    with pytest.raises(HTTPException, match="当前选题涉及金融、时事"):
        approve(changed)


def test_risk_confirmation_rejects_stale_revision_and_incomplete_risks(world):
    topic = make_topic(title="最新金融政策怎样影响工具选择")
    with pytest.raises(HTTPException, match="完整覆盖"):
        service.confirm_topic_risks(topic["id"], {"expected_revision": topic["revision"], "risks": ["finance"]})
    with pytest.raises(HTTPException, match="已修订"):
        service.confirm_topic_risks(topic["id"], {"expected_revision": topic["revision"] + 1, "risks": ["finance", "current_affairs"]})


def test_crypto_topic_is_rejected_at_creation(world):
    with pytest.raises(HTTPException, match="币圈或虚拟货币") as error:
        make_topic(title="比特币和虚拟货币工具实测")
    assert error.value.status_code == 422


def test_material_and_prompt_fingerprints_cover_editorial_choices(world, monkeypatch):
    base = {"title": "工具测试", "brief": "", "platforms": ["wechat"], "sources": [SOURCE],
            "content_line": "A", "stance_ids": []}
    line_changed = {**base, "content_line": "B"}
    stance_changed = {**base, "stance_ids": ["example-check-the-result"]}
    assert adapters.material_fingerprint(base) != adapters.material_fingerprint(line_changed)
    assert adapters.material_fingerprint(base) != adapters.material_fingerprint(stance_changed)
    assert adapters.prompt_fingerprint(base) != adapters.prompt_fingerprint(line_changed)
    assert adapters.prompt_fingerprint(base) != adapters.prompt_fingerprint(stance_changed)
    database_shaped = {**base, "platforms": json.dumps(["wechat"]),
                       "sources": json.dumps([SOURCE], ensure_ascii=False), "stance_ids": "[]"}
    assert adapters._prompt(database_shaped) == adapters._prompt(base)
    before_style_change = adapters.prompt_fingerprint(base)
    original = style_library.prompt_for
    monkeypatch.setattr(style_library, "prompt_for", lambda *args, **kwargs: original(*args, **kwargs) + "\n校验规则版本：test-v2")
    assert adapters.prompt_fingerprint(base) != before_style_change


def test_first_person_experience_requires_material_evidence(world):
    topic = approve(make_topic())
    with pytest.raises(HTTPException, match="第一人称经历或实测"):
        insert_version(topic, payload("我亲测了这个工具。\n\n输出结果有 2 条。"))
    evidenced = dict(SOURCE)
    evidenced["note"] = "作者原始记录：我亲测了这个工具，输出结果有 2 条。"
    evidenced_topic = service.create_topic({"title": "工具有据实测", "brief": "作者提供了实测记录",
                                           "platforms": ["wechat"], "sources": [evidenced], "content_line": "B", "stance_ids": []})
    insert_version(approve(evidenced_topic), payload("我亲测了这个工具。\n\n输出结果有 2 条。"))


def test_medical_investment_and_crypto_claims_block_formal_freeze(world):
    topic = approve(make_topic())
    for body in (
        "这个药品保证疗效，治愈率百分之百。\n\n工具输出了 2 条结果。",
        "建议现在满仓买入这只股票，目标价翻倍。\n\n工具输出了 2 条结果。",
        "这款工具支持虚拟货币交易。\n\n输出结果有 2 条。",
    ):
        with pytest.raises(HTTPException, match="正式版本未冻结"):
            insert_version(topic, payload(body))
    with db.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM versions WHERE topic_id=?", (topic["id"],)).fetchone()[0]
    assert count == 0


def test_negated_investment_terms_are_warnings_not_blockers(world):
    """否定/引用语境的“抄底/投资建议”不误拦；真实建议仍阻断。"""
    topic = approve(make_topic())
    for body in (
        "普通人不该盲目跟风抄底或恐慌抛售。\n\n工具输出了 2 条结果。",
        "不把金价波动解读为投资建议或抄底信号。\n\n工具输出了 2 条结果。",
    ):
        version = insert_version(topic, payload(body))
        assert version["review"]["status"] == "passed"
    with pytest.raises(HTTPException, match="正式版本未冻结"):
        insert_version(topic, payload("建议现在满仓买入这只股票。\n\n工具输出了 2 条结果。"))


def test_company_and_internal_terms_are_warnings_not_freeze_blockers(world):
    topic = approve(make_topic())
    version = insert_version(topic, payload("某示例有限公司的内部资料仅作风险演示。\n\n工具输出结果有 2 条。"))
    assert version["review"]["status"] == "passed"
    messages = [item["message"] for item in version["review"]["findings"]]
    assert any("公司名" in item for item in messages)
    assert any("内部资料" in item for item in messages)


def test_manual_working_copy_can_save_but_cannot_commit_invalid_formal_version(world):
    topic = approve(make_topic())
    version = insert_version(topic)
    current = content_studio.read_copy(topic["id"], version["id"], "wechat")
    saved = content_studio.save_copy(topic["id"], version["id"], "wechat", {
        "expected_revision": current["revision"], "title": current["title"],
        "body": "我亲测了这个工具，但材料没有这段经历。", "tags": current["tags"], "source_urls": current["source_urls"],
    })
    assert saved["revision"] == 1
    with pytest.raises(HTTPException, match="正式版本未冻结"):
        content_studio.commit_copies(topic["id"], {"base_version_id": version["id"], "expected_revisions": {"wechat": 1}})
    assert len(service.get_topic(topic["id"])["versions"]) == 1


def test_legacy_paused_job_does_not_inherit_new_editorial_values(world):
    topic = approve(make_topic())
    job = service.generate(topic["id"], topic["revision"])
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET input_snapshot='{}',paused=1 WHERE id=?", (job["id"],))
    with pytest.raises(HTTPException, match="历史任务快照缺少或已改变内容线") as error:
        service.resume_paused_job(job["id"])
    assert error.value.status_code == 409
    with db.connect() as connection:
        assert service._row(connection, "jobs", job["id"])["paused"] == 1


def test_style_library_and_risk_confirmation_http_contract(world):
    client = TestClient(main.app)
    try:
        assert client.get("/api/style-library").status_code == 401
        token = authenticated_bootstrap(client).json()["token"]
        client.headers["X-Workbench-Token"] = token
        library_response = client.get("/api/style-library")
        assert library_response.status_code == 200
        library = library_response.json()
        assert library["library_id"] == "example-editorial-style"
        assert library["version"] == "example-v1.0"
        assert library["voice"]["status"] == "missing"
        assert set(library["content_lines"]) == {"A", "B", "C"}

        topic = make_topic(title="最新金融政策怎样影响工具选择")
        path = f"/api/topics/{topic['id']}/risk-confirmation"
        assert client.post(path, json={"expected_revision": topic["revision"], "risks": ["finance"]}).status_code == 422
        response = client.post(path, json={"expected_revision": topic["revision"], "risks": ["finance", "current_affairs"]})
        assert response.status_code == 200
        confirmed = response.json()
        assert set(confirmed["required_risk_confirmations"]) == {"finance", "current_affairs"}
        assert confirmed["risk_confirmation"]["revision"] == topic["revision"]
        assert set(confirmed["risk_confirmation"]["risks"]) == {"finance", "current_affairs"}
    finally:
        client.close()
