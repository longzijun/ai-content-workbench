"""Automatic materials preserve provenance and scope final checks to used work."""
import json

import pytest
from fastapi import HTTPException

from backend.app import auto_materials, config, db, fact_base, research, review, service

REAL_COLLECT = research.collect


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    auto_materials.init_schema()
    monkeypatch.setattr(research, "cached", lambda topic: None)
    monkeypatch.setattr(research, "collect", lambda *a, **kw: {"documents": [], "errors": ["读取暂时失败"]})
    return monkeypatch


def running_topic(brief="给普通读者说明怎样用提纲整理自己的写作思路。", sources=None):
    topic = service.create_topic({"title": "写作资料整理", "brief": brief, "platforms": ["wechat"],
                                  "sources": sources or [], "content_line": "A", "stance_ids": []})
    service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    service.generate(topic["id"], topic["revision"])
    return service.claim_next_job()["topic"]


def test_empty_form_is_automatic_and_never_creates_confirmed_fact(world):
    topic = running_topic()
    prepared = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    material = prepared["_auto_materials"]
    assert material["mode"] == "limited"
    assert material["audience"]
    assert material["suggested_angle"]
    assert not material["facts"] and material["facts_verified"] is False
    assert fact_base.get(topic["id"]) is None
    assert "不得补写我亲测" in prepared["_fact_base"]


def test_title_only_without_readable_sources_is_outline(world):
    topic = running_topic(brief="")
    material = auto_materials.prepare(topic, lambda: False, lambda *a: None)["_auto_materials"]
    assert material["mode"] == "outline"
    assert any(item["blocking"] for item in material["issues"])
    assert "仅有提纲" in auto_materials.freeze_gaps(material, {"works": []})[0]


def test_brief_only_remains_limited_without_pretending_to_read_original(world):
    topic = running_topic()
    world.setattr(research, "collect", lambda *a, **kw: {"documents": [], "errors": []})
    prepared = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert prepared["_auto_materials"]["mode"] == "limited"
    assert not prepared["_research"]["documents"]
    assert "不代表作者亲历或确认" in prepared["_fact_base"]


def test_real_documents_not_links_become_evidence_and_blocked_domains_omitted(world):
    topic = running_topic(brief="")
    world.setattr(research, "collect", lambda *a, **kw: {"documents": [
        {"url": "https://example.com/article", "title": "原文", "body": "有据原文" * 100},
        {"url": "https://www.xiaohongshu.com/explore/one", "title": "禁止后台触达", "body": "不能用"}], "errors": []})
    material = auto_materials.prepare(topic, lambda: False, lambda *a: None)["_auto_materials"]
    assert material["mode"] == "evidenced"
    assert material["evidence_urls"] == ["https://example.com/article"]
    assert material["facts_verified"] is False


def test_retry_reuses_persisted_materials_without_network(world):
    topic = running_topic()
    first = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    world.setattr(research, "collect", lambda *a, **kw: pytest.fail("must use durable snapshot"))
    second = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert first["_auto_materials"] == second["_auto_materials"]


def test_child_retry_keeps_parent_material_snapshot(world):
    topic = running_topic()
    first = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    service.fail_job(topic["_generation_run"], "只重试未完成的平台")
    service.generate(topic["id"], topic["revision"])
    child = service.claim_next_job()["topic"]
    child["_candidate_parent_job"] = topic["_generation_run"]
    world.setattr(research, "collect", lambda *a, **kw: pytest.fail("must reuse parent snapshot"))
    result = auto_materials.prepare(child, lambda: False, lambda *a: None)
    assert result["_auto_materials"] == first["_auto_materials"]


def test_source_collection_stops_after_cancellation(world):
    topic = running_topic()
    topic["sources"] = [{"url": "https://example.com/one"}, {"url": "https://example.com/two"}]
    calls = []
    def read(url, deadline):
        calls.append(url)
        return {"url": url, "title": "已读原文", "body": "原文" * 200}
    world.setattr(research, "read_article", read)
    # Use the real collector; the fixture otherwise stubs it to avoid network.
    from backend.app.adapters import TaskCancelled
    with pytest.raises(TaskCancelled):
        REAL_COLLECT(topic, discover=False, collect_images=False, cancelled=lambda: bool(calls))
    assert calls == ["https://example.com/one"]


def test_private_info_excluded_and_fact_table_untouched(world):
    topic = running_topic()
    fact_base.save(topic["id"], {"raw_text": "公开内容，秘密123", "private_info": "秘密123",
                                "facts": [{"claim": "秘密123", "status": "unverified"}]})
    original = fact_base.get(topic["id"])
    result = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert "秘密123" not in json.dumps(result["_auto_materials"], ensure_ascii=False)
    assert "秘密123" not in result["_fact_base"]
    assert fact_base.get(topic["id"]) == original


def test_cancel_does_not_save_or_convert_to_warning(world):
    topic = running_topic()
    from backend.app.adapters import TaskCancelled
    with pytest.raises(TaskCancelled):
        auto_materials.prepare(topic, lambda: True, lambda *a: None)
    with db.connect() as connection:
        assert not connection.execute("SELECT 1 FROM auto_material_snapshots").fetchone()


def test_scope_change_rejects_late_material_write(world):
    topic = running_topic()
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET revision=revision+1 WHERE id=?", (topic["id"],))
    with pytest.raises(HTTPException):
        auto_materials.prepare(topic, lambda: False, lambda *a: None)


def test_only_used_unknown_claims_and_actual_unread_urls_block():
    material = {"mode": "limited", "evidence_urls": ["https://example.com/read"],
                "facts": [{"claim": "未采用的数字为999", "status": "unverified"}]}
    work = {"platform": "wechat", "title": "整理方法", "body": "仅谈有依据的方法。", "source_urls": []}
    assert auto_materials.freeze_gaps(material, {"works": [work]}) == []
    work["body"] += "未采用的数字为999"
    assert "事实仍待核实" in auto_materials.freeze_gaps(material, {"works": [work]})[0]
    work["body"] = "方法说明"
    work["source_urls"] = ["https://example.com/unread"]
    assert "尚无可用原文" in auto_materials.freeze_gaps(material, {"works": [work]})[0]


def test_version_binding_avoids_unused_form_gaps_but_preserves_manual_review(world):
    topic = running_topic()
    material = auto_materials.prepare(topic, lambda: False, lambda *a: None)["_auto_materials"]
    payload = {"strategy": "说明方法", "works": [{"platform": "wechat", "format": "article",
        "title": "整理写作资料的方法", "body": "先列出问题，再按依据整理材料，最后核对引用和适用范围。",
        "tags": [], "source_urls": []}]}
    with db.transaction() as connection:
        row = service._row(connection, "topics", topic["id"])
        version_id, ids = service._insert_version(connection, row, payload, "candidate:test")
        auto_materials.bind_version(connection, version_id, {"_auto_materials": material})
    assert review.console(topic["id"])["gaps"] == []
    with pytest.raises(HTTPException, match="请人工确认"):
        review.record(ids[0], "approved", "", "账号", "AI 辅助创作", False)
    review.record(ids[0], "approved", "", "账号", "AI 辅助创作", True)
    assert review.current(service.get_draft(ids[0]))["approved"]
    # An unrelated unused fact-form addition does not invalidate this bound work.
    fact_base.save(topic["id"], {"facts": [{"claim": "未采用内容", "status": "unverified"}]})
    assert review.current(service.get_draft(ids[0]))["approved"]


def test_legacy_version_keeps_original_fact_form_gate(world):
    topic = running_topic()
    payload = {"strategy": "说明方法", "works": [{"platform": "wechat", "format": "article",
        "title": "整理写作资料的方法", "body": "先列出问题，再按依据整理材料，最后核对引用和适用范围。",
        "tags": [], "source_urls": []}]}
    with db.transaction() as connection:
        service._insert_version(connection, service._row(connection, "topics", topic["id"]), payload, "old-path")
    assert review.console(topic["id"])["gaps"] == ["尚未建立共享事实底稿"]


def test_new_numeric_claim_needs_evidence_but_hypothetical_example_is_explicit():
    material = {"mode": "limited", "author_text": "项目耗时3天", "evidence_urls": [], "facts": []}
    work = {"platform": "wechat", "body": "项目耗时3天。转化率提升20%。", "source_urls": []}
    gaps = auto_materials.freeze_gaps(material, {"works": [work]})
    assert len(gaps) == 1 and "20%" in gaps[0]
    work["body"] = "项目耗时3天。例如，假设转化率提升20%，可以对比结果。"
    assert not auto_materials.freeze_gaps(material, {"works": [work]})


def test_hypothetical_number_does_not_exempt_later_reported_result():
    material = {"mode": "limited", "author_text": "", "evidence_urls": [], "facts": []}
    work = {"platform": "wechat", "body": "例如，假设提升20%。实际测试提升20%。", "source_urls": []}
    assert "20%" in auto_materials.freeze_gaps(material, {"works": [work]})[0]


def _video_world():
    from backend.tests.test_video_c1 import script, SOURCE
    topic = service.create_topic({"title": "工具导入结果核对", "brief": SOURCE["note"],
        "platforms": ["douyin"], "sources": [SOURCE], "content_line": "B", "stance_ids": [],
        "video_target_duration_seconds": 60})
    topic = service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})
    material = {"mode": "limited", "author_text": SOURCE["note"], "evidence_urls": [SOURCE["url"]],
                "facts": [{"claim": "测试覆盖全部岗位", "status": "unverified", "source_url": ""}],
                "material_fingerprint": "video-fixture"}
    payload = {"strategy": "核对导入步骤", "works": [script()]}
    with db.transaction() as connection:
        version_id, _ = service._insert_version(connection, service._row(connection, "topics", topic["id"]),
                                                payload, "candidate:test", material_snapshot=material)
        auto_materials.bind_version(connection, version_id, {"_auto_materials": material})
    return topic, material, payload, version_id


def _freeze_video(entry, topic, material, payload, version_id):
    from backend.app import candidate_drafts, schemas
    if entry == "version":
        # Authoring schema intentionally has no derived body: the server must
        # inspect shot text rather than trusting/expecting that field.
        authored = schemas.VersionCreate.model_validate({"base_version_id": version_id, "payload": payload})
        return service.create_version(topic["id"], authored.payload.model_dump(), version_id)
    service.generate(topic["id"], topic["revision"])
    snapshot = service.claim_next_job()["topic"]
    snapshot["_auto_materials"] = material
    candidate = candidate_drafts.publish(snapshot, "douyin", payload)
    candidate_drafts.finish_job(snapshot["_generation_run"])
    return candidate_drafts.commit(topic["id"], {"candidate_ids": {"douyin": candidate["id"]},
        "expected_revisions": {"douyin": candidate["revision"]}, "expected_version_id": version_id})


@pytest.mark.parametrize("entry", ["version", "candidate"])
@pytest.mark.parametrize("field", ["spoken_text", "visual_notes", "subtitle_points", "unverified_claim", "source_refs"])
def test_video_freeze_checks_all_real_text_and_shot_sources(world, entry, field):
    topic, material, payload, version_id = _video_world()
    shot = payload["works"][0]["shots"][1]
    if field in ("spoken_text", "visual_notes"):
        shot[field] = "转化率提升20%。"
    elif field == "subtitle_points":
        shot[field] = ["转化率提升20%"]
    elif field == "unverified_claim":
        shot["visual_notes"] = "画面展示测试覆盖全部岗位。"
    else:
        shot["source_refs"] = ["https://example.invalid/not-read"]
    with pytest.raises(HTTPException) as caught:
        _freeze_video(entry, topic, material, payload, version_id)
    assert caught.value.status_code == 422
    assert ("20%" in str(caught.value.detail) or "测试覆盖全部岗位" in str(caught.value.detail)
            or "分镜" in str(caught.value.detail))
    with db.connect() as connection:
        assert service._row(connection, "topics", topic["id"])["current_version_id"] == version_id


@pytest.mark.parametrize("entry", ["version", "candidate"])
def test_video_numeric_claim_is_allowed_when_actual_author_material_supports_it(world, entry):
    topic, material, payload, version_id = _video_world()
    material["author_text"] += "本次记录：转化率提升20%。"
    # Bind the improved material through a new baseline instead of mutating the
    # existing immutable version's evidence.
    with db.transaction() as connection:
        version_id, _ = service._insert_version(connection, service._row(connection, "topics", topic["id"]),
            payload, "candidate:test", material_snapshot=material)
        auto_materials.bind_version(connection, version_id, {"_auto_materials": material})
    shot = payload["works"][0]["shots"][1]
    shot["spoken_text"] = "转化率提升20%。"
    shot["visual_notes"] = "画面展示本次记录：转化率提升20%。"
    shot["subtitle_points"] = ["转化率提升20%"]
    assert not auto_materials.freeze_gaps(material, payload)
    assert _freeze_video(entry, topic, material, payload, version_id)


def test_incomplete_video_remains_editable_and_reports_structure_issue():
    work = {"platform": "douyin", "format": "video_script", "title": "待完善的分镜", "shots": [
        {"spoken_text": "转化率提升20%。", "visual_notes": "", "subtitle_points": [], "source_refs": []}]}
    gaps = auto_materials.freeze_gaps({"mode": "limited", "author_text": "", "evidence_urls": [], "facts": []},
                                     {"works": [work]})
    assert any("分镜尚待完善" in item for item in gaps)
    assert any("20%" in item for item in gaps)


def test_registered_topic_and_fact_sources_merge_and_model_only_sees_read_evidence(world):
    original_sources = [{"url": "https://example.com/unread", "title": "尚未读到的旧报道"},
                        {"url": "https://www.baidu.com/s?wd=topic", "title": "搜索线索"}]
    topic = running_topic(sources=original_sources)
    fact_base.save(topic["id"], {"sources": [
        {"url": " https://example.com/unread ", "title": "重复来源"},
        {"url": "https://example.com/new-article", "title": "资料区补充的新原文"}]})
    before_fact = fact_base.get(topic["id"])
    cache_inputs, collect_inputs = [], []
    def cached(value):
        cache_inputs.append(value["sources"])
        return None
    def collect(value, **kwargs):
        collect_inputs.append(value["sources"])
        return {"documents": [{"url": "https://example.com/new-article", "title": "已读取的文章标题",
            "body": "实际原文内容" * 100}], "errors": ["旧来源暂时不可读"]}
    world.setattr(research, "cached", cached)
    world.setattr(research, "collect", collect)
    prepared = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    urls = ["https://example.com/unread", "https://www.baidu.com/s?wd=topic", "https://example.com/new-article"]
    assert [item["url"] for item in cache_inputs[0]] == urls
    assert cache_inputs[0] == collect_inputs[0]
    assert [item["url"] for item in prepared["sources"]] == [urls[-1]]
    assert prepared["sources"][0]["title"] == "已读取的文章标题"
    assert [item["url"] for item in prepared["_auto_materials"]["registered_sources"]] == urls
    assert prepared["_auto_materials"]["source_urls"] == urls
    assert topic["sources"] == original_sources
    assert service.get_topic(topic["id"])["sources"] == original_sources
    assert fact_base.get(topic["id"]) == before_fact
    from backend.app import adapters
    model_sources = adapters.generation_material(prepared)["sources"]
    assert [item["url"] for item in model_sources] == [urls[-1]]


def test_adding_fact_source_after_initial_preparation_reads_new_original(world):
    topic = running_topic()
    calls = []
    def collect(value, **kwargs):
        urls = [item["url"] for item in value["sources"]]
        calls.append(urls)
        return {"documents": [{"url": url, "title": "新读原文", "body": "原文内容" * 100}
                               for url in urls], "errors": []}
    world.setattr(research, "collect", collect)
    first = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert not first["sources"]
    fact_base.save(topic["id"], {"sources": [{"url": "https://example.com/fact-added", "title": "新来源"}]})
    second = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert calls == [[], ["https://example.com/fact-added"]]
    assert second["_auto_materials"]["evidence_urls"] == ["https://example.com/fact-added"]
    assert second["_auto_materials"]["input_fingerprint"] != first["_auto_materials"]["input_fingerprint"]
    assert service.get_topic(topic["id"])["sources"] == []


def test_fact_only_source_reaches_real_public_reader_without_changing_topic(world):
    topic = running_topic(brief="")
    url = "https://example.com/fact-original"
    fact_base.save(topic["id"], {"sources": [{"url": url, "title": "仅在资料区登记"}]})
    read = []
    def article(address, deadline):
        read.append(address)
        return {"url": address, "title": "实际读取的原文", "body": "可追溯原文内容" * 100,
                "fetched_at": "2026-11-04T00:00:00Z", "published_at": "2026-11-03"}
    world.setattr(research, "collect", REAL_COLLECT)
    world.setattr(research, "read_article", article)
    prepared = auto_materials.prepare(topic, lambda: False, lambda *a: None)
    assert read == [url]
    assert prepared["_auto_materials"]["mode"] == "evidenced"
    assert prepared["_research"]["documents"][0]["body"].startswith("可追溯原文内容")
    assert prepared["sources"] == [{"url": url, "platform": "external", "title": "实际读取的原文"}]
    assert service.get_topic(topic["id"])["sources"] == []
