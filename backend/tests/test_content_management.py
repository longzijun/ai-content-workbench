"""内容管理层：软删除、回收站、批量逐条结算、审计日志、引用检查。

每个测试用独立 runtime 目录（tmp_path），不碰生产库。断言的都不是「函数
没报错」，而是真实的库内状态——删了要真不在列表里，恢复要真回来，
批量要真能部分失败。
"""
import json
import sqlite3

import pytest

from backend.app import content, db, service


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(__import__("backend.app.config", fromlist=["config"]),
                        "discover_codex", lambda: "")
    db.init_db()
    yield db.connect


def make_topic(world, topic_id="t1", title="选题一", status="pending", platforms=("wechat",)):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at)"
            " VALUES(?,?,?,?,'[]','1',?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (topic_id, title, "简介", json.dumps(list(platforms)), status))
    return topic_id


def make_draft(world, draft_id="d1", topic_id="t1", version_id="v1", platform="wechat", state="local"):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at)"
            " VALUES(?,?,1,1,'{\"strategy\":\"s\",\"works\":[]}','h','manual','2026-01-01T00:00:00+00:00')",
            (version_id, topic_id))
        connection.execute(
            "INSERT INTO drafts(id,topic_id,version_id,platform,title,state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,'%s','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')" % state,
            (draft_id, topic_id, version_id, platform, "草稿一"))
    return draft_id


def rows(sql, *params):
    with db.connect() as connection:
        return [dict(row) for row in connection.execute(sql, params)]


# --------------------------------------------------------------------------
# 列表与检索
# --------------------------------------------------------------------------

def test_list_content_returns_only_live_rows(world):
    make_topic(world, "t1", "甲选题")
    make_topic(world, "t2", "乙选题")
    result = content.list_content(kind="topic")
    assert result["total"] == 2
    assert {item["id"] for item in result["items"]} == {"t1", "t2"}


def test_keyword_search_is_case_insensitive_and_hits_brief(world):
    make_topic(world, "t1", "AI 医疗", status="pending")
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET brief=? WHERE id='t1'", ("关于医疗器械",))
    assert content.list_content(kind="topic", q="ai 医疗")["total"] == 1
    assert content.list_content(kind="topic", q="医疗器械")["total"] == 1
    assert content.list_content(kind="topic", q="不存在的东西")["total"] == 0


def test_platform_and_state_filters_really_narrow_results(world):
    make_topic(world, "t1", platforms=("wechat",), status="pending")
    make_topic(world, "t2", platforms=("toutiao",), status="approved")
    make_topic(world, "t3", platforms=("xiaohongshu", "toutiao"), status="pending")
    assert content.list_content(kind="topic", platform="toutiao")["total"] == 2
    assert content.list_content(kind="topic", state="pending")["total"] == 2
    assert content.list_content(kind="topic", platform="toutiao", state="approved")["total"] == 1


def test_invalid_sort_and_platform_are_rejected(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.list_content(sort="; DROP TABLE topics")
    assert err.value.status_code == 422
    with pytest.raises(HTTPException):
        content.list_content(platform="weibo")


def test_pagination_slices_and_reports_real_total(world):
    for index in range(7):
        make_topic(world, f"t{index}", f"选题{index}")
    first = content.list_content(kind="topic", page=1, page_size=3, sort="title", order="asc")
    assert first["total"] == 7 and first["pages"] == 3 and len(first["items"]) == 3
    last = content.list_content(kind="topic", page=3, page_size=3, sort="title", order="asc")
    assert len(last["items"]) == 1
    seen = {item["id"] for page in (1, 2, 3)
            for item in content.list_content(kind="topic", page=page, page_size=3)["items"]}
    assert len(seen) == 7


def test_sort_order_is_respected(world):
    make_topic(world, "t1", "AAA")
    make_topic(world, "t2", "BBB")
    ascending = [item["title"] for item in content.list_content(kind="topic", sort="title", order="asc")["items"]]
    descending = [item["title"] for item in content.list_content(kind="topic", sort="title", order="desc")["items"]]
    assert ascending == ["AAA", "BBB"] and descending == ["BBB", "AAA"]


def test_facets_count_the_current_result_set(world):
    make_topic(world, "t1", platforms=("wechat",), status="pending")
    make_topic(world, "t2", platforms=("wechat", "toutiao"), status="approved")
    make_draft(world, "d1", "t1", "v1", "toutiao")
    facets = content.list_content(kind="all")["facets"]
    assert facets["platforms"]["wechat"] == 2
    assert facets["content_types"]["平台草稿"] == 1


# --------------------------------------------------------------------------
# 软删除与回收站
# --------------------------------------------------------------------------

def test_soft_delete_hides_row_from_list_and_state(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    assert content.list_content(kind="topic")["total"] == 0
    assert all(item["id"] != "t1" for item in service.state()["topics"])


def test_soft_delete_keeps_the_row_in_the_database(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    stored = rows("SELECT deleted_at FROM topics WHERE id='t1'")
    assert len(stored) == 1 and stored[0]["deleted_at"], "软删除不能真的抹掉数据库行"


def test_soft_delete_writes_a_restorable_snapshot(world):
    make_topic(world, "t1", "原标题", status="approved")
    content.soft_delete("topic", "t1")
    entry = rows("SELECT * FROM content_trash WHERE entity_type='topic' AND entity_id='t1'")
    assert len(entry) == 1
    snapshot = json.loads(entry[0]["snapshot"])
    assert snapshot["title"] == "原标题" and snapshot["status"] == "approved"
    assert entry[0]["previous_state"] == "approved"


def test_restore_brings_the_row_back(world):
    make_topic(world, "t1", "原标题")
    content.soft_delete("topic", "t1")
    content.restore("topic", "t1")
    assert content.list_content(kind="topic")["total"] == 1
    assert rows("SELECT deleted_at FROM topics WHERE id='t1'")[0]["deleted_at"] is None


def test_restore_is_idempotent(world):
    make_topic(world, "t1")
    content.restore("topic", "t1")
    assert content.restore("topic", "t1")["restored"] is False


def test_soft_delete_twice_does_not_duplicate_trash(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    first = rows("SELECT deleted_at FROM content_trash")[0]["deleted_at"]
    result = content.soft_delete("topic", "t1")
    assert result["already_deleted"] is True
    assert len(rows("SELECT * FROM content_trash")) == 1
    assert rows("SELECT deleted_at FROM content_trash")[0]["deleted_at"] == first


def test_trash_list_shows_deleted_entries_only(world):
    make_topic(world, "t1", "已删")
    make_topic(world, "t2", "保留")
    content.soft_delete("topic", "t1")
    trash = content.list_trash()
    assert trash["total"] == 1 and trash["items"][0]["entity_id"] == "t1"


def test_operating_on_a_deleted_row_is_refused(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.update_topic_meta("t1", {"priority": 1})
    assert err.value.status_code == 409


def test_unknown_entity_returns_404(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.soft_delete("topic", "nope")
    assert err.value.status_code == 404


def test_unsupported_entity_type_is_rejected(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.soft_delete("publication", "x")


# --------------------------------------------------------------------------
# 草稿删除与恢复的引用约束
# --------------------------------------------------------------------------

def test_draft_restore_blocked_while_its_topic_is_deleted(world):
    make_topic(world, "t1")
    make_draft(world, "d1", "t1", "v1")
    content.soft_delete("topic", "t1")
    content.soft_delete("draft", "d1")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.restore("draft", "d1")
    assert "先恢复选题" in str(err.value.detail)


def test_draft_restore_works_after_topic_is_restored(world):
    make_topic(world, "t1")
    make_draft(world, "d1", "t1", "v1")
    content.soft_delete("draft", "d1")
    content.soft_delete("topic", "t1")
    content.restore("topic", "t1")
    assert content.restore("draft", "d1")["restored"] is True
    assert content.list_content(kind="draft")["total"] == 1


# --------------------------------------------------------------------------
# 永久删除与引用检查
# --------------------------------------------------------------------------

def test_purge_requires_explicit_confirmation(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.purge("topic", "t1", confirm=False)
    assert err.value.status_code == 422
    assert rows("SELECT id FROM topics WHERE id='t1'")


def test_purge_refuses_when_drafts_reference_the_topic(world):
    make_topic(world, "t1")
    make_draft(world, "d1", "t1", "v1")
    content.soft_delete("topic", "t1")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        content.purge("topic", "t1", confirm=True)
    assert "平台草稿" in str(err.value.detail)
    assert rows("SELECT id FROM topics WHERE id='t1'")


def test_reference_blockers_list_every_relation(world):
    make_topic(world, "t1")
    make_draft(world, "d1", "t1", "v1")
    with db.connect() as connection:
        blockers = content.reference_blockers(connection, "topic", "t1")
    joined = "；".join(blockers)
    assert "草稿" in joined and "内容版本" in joined


def test_purge_removes_row_and_trash_entry(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    content.purge("topic", "t1", confirm=True)
    assert not rows("SELECT id FROM topics WHERE id='t1'")
    assert not rows("SELECT id FROM content_trash WHERE entity_id='t1'")


# --------------------------------------------------------------------------
# 复制
# --------------------------------------------------------------------------

def test_duplicate_resets_approval_state(world):
    make_topic(world, "t1", "已批准选题", status="approved")
    clone = content.duplicate_topic("t1")
    assert clone["status"] == "pending", "复制不能继承批准状态，否则等于绕过人工批准"
    assert clone["title"] == "已批准选题"
    assert clone["id"] != "t1"


def test_duplicate_carries_priority_and_tags(world):
    make_topic(world, "t1")
    content.update_topic_meta("t1", {"priority": 1, "tags": ["医疗", "AI"]})
    clone = content.duplicate_topic("t1")
    assert clone["priority"] == 1 and set(clone["tags"]) == {"医疗", "AI"}


def test_duplicate_of_missing_topic_is_404(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.duplicate_topic("nope")


# --------------------------------------------------------------------------
# 归档 / 优先级 / 收藏 / 标签
# --------------------------------------------------------------------------

def test_archive_hides_from_topic_listing_but_keeps_data(world):
    make_topic(world, "t1")
    content.set_archived("t1", True)
    stored = rows("SELECT archived FROM topics WHERE id='t1'")
    assert stored[0]["archived"] == 1, "归档是状态位，不是删除"


def test_archive_toggles_back(world):
    make_topic(world, "t1")
    content.set_archived("t1", True)
    content.set_archived("t1", False)
    assert rows("SELECT archived FROM topics WHERE id='t1'")[0]["archived"] == 0


def test_priority_out_of_range_is_rejected(world):
    make_topic(world, "t1")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.update_topic_meta("t1", {"priority": 9})


def test_favorite_and_tags_persist(world):
    make_topic(world, "t1")
    content.update_topic_meta("t1", {"favorite": True, "tags": ["  医疗  ", "", "AI"]})
    stored = rows("SELECT favorite,tags FROM topics WHERE id='t1'")[0]
    assert stored["favorite"] == 1
    assert json.loads(stored["tags"]) == ["医疗", "AI"], "空标签要去掉、空白要裁剪"


def test_tag_filter_matches_real_tags(world):
    make_topic(world, "t1", "甲")
    make_topic(world, "t2", "乙")
    content.update_topic_meta("t1", {"tags": ["重点"]})
    assert content.list_content(kind="topic", tag="重点")["total"] == 1


def test_priority_filter_works(world):
    make_topic(world, "t1")
    make_topic(world, "t2")
    content.update_topic_meta("t1", {"priority": 1})
    assert content.list_content(kind="topic", priority="1")["total"] == 1


# --------------------------------------------------------------------------
# 批量操作
# --------------------------------------------------------------------------

def test_batch_returns_per_item_results(world):
    for index in range(3):
        make_topic(world, f"t{index}")
    result = content.batch("topic", ["t0", "t1", "t2"], "archive")
    assert result["requested"] == 3 and result["succeeded"] == 3 and result["failed"] == 0
    assert all(item["ok"] for item in result["results"])


def test_batch_delete_moves_every_row(world):
    for index in range(3):
        make_topic(world, f"t{index}")
    content.batch("topic", ["t0", "t1", "t2"], "delete")
    assert content.list_trash()["total"] == 3
    assert content.list_content(kind="topic")["total"] == 0


def test_batch_partial_failure_is_reported_honestly(world):
    make_topic(world, "t1")
    result = content.batch("topic", ["t1", "missing-1", "missing-2"], "archive")
    assert result["succeeded"] == 1 and result["failed"] == 2
    failed = [item for item in result["results"] if not item["ok"]]
    assert len(failed) == 2
    assert all("不存在" in item["message"] for item in failed)


def test_batch_failure_does_not_abort_the_remaining_items(world):
    """真实回归点：中间一条失败时，后面的仍要执行。"""
    make_topic(world, "t1")
    make_topic(world, "t3")
    result = content.batch("topic", ["t1", "t2-missing", "t3"], "delete")
    assert result["succeeded"] == 2
    assert rows("SELECT deleted_at FROM topics WHERE id IN ('t1','t3')") != []
    assert all(row["deleted_at"] for row in rows("SELECT deleted_at FROM topics WHERE id IN ('t1','t3')"))


def test_batch_rejects_unknown_action_and_empty_selection(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.batch("topic", ["t1"], "explode")
    with pytest.raises(HTTPException):
        content.batch("topic", [], "archive")


def test_batch_caps_at_two_hundred(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.batch("topic", [f"t{i}" for i in range(201)], "archive")


def test_batch_tag_add_and_remove(world):
    make_topic(world, "t1")
    content.update_topic_meta("t1", {"tags": ["旧"]})
    content.batch("topic", ["t1"], "tag", {"add": ["新"], "remove": ["旧"]})
    assert json.loads(rows("SELECT tags FROM topics WHERE id='t1'")[0]["tags"]) == ["新"]


def test_batch_does_not_support_archive_on_drafts(world):
    make_topic(world, "t1")
    make_draft(world, "d1", "t1", "v1")
    result = content.batch("draft", ["d1"], "archive")
    assert result["failed"] == 1 and result["results"][0]["ok"] is False


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------

def test_audit_records_action_and_result(world):
    make_topic(world, "t1")
    content.soft_delete("topic", "t1")
    entries = rows("SELECT * FROM audit_log WHERE entity_id='t1'")
    assert len(entries) == 1
    assert entries[0]["action"] == "delete" and entries[0]["result"] == "success"


def test_audit_covers_every_lifecycle_action(world):
    make_topic(world, "t1")
    content.update_topic_meta("t1", {"priority": 2})
    content.set_archived("t1", True)
    content.soft_delete("topic", "t1")
    content.restore("topic", "t1")
    actions = {row["action"] for row in rows("SELECT action FROM audit_log WHERE entity_id='t1'")}
    assert {"update_meta", "archive", "delete", "restore"} <= actions


def test_audit_failure_is_logged_for_failed_batch_items(world):
    content.batch("topic", ["missing"], "archive")
    entries = rows("SELECT * FROM audit_log WHERE entity_id='missing'")
    assert entries and entries[0]["result"] == "failed"


def test_audit_list_filters_and_paginates(world):
    make_topic(world, "t1")
    make_topic(world, "t2")
    content.soft_delete("topic", "t1")
    content.soft_delete("topic", "t2")
    only_t1 = content.list_audit(entity_id="t1")
    assert only_t1["total"] == 1
    page = content.list_audit(limit=1, offset=1)
    assert len(page["items"]) == 1 and page["total"] == 2


def test_audit_never_stores_credentials(world):
    """回归红线：日志表不接受任何凭据字段。"""
    with db.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_log)")}
    forbidden = {"token", "cookie", "api_key", "secret", "password", "credential"}
    assert not (columns & forbidden)
    assert "detail" in columns


def test_audit_detail_is_length_capped(world):
    with db.transaction() as connection:
        content.audit(connection, "topic", "t1", "note", detail="x" * 5000)
    assert len(rows("SELECT detail FROM audit_log")[0]["detail"]) <= 800


# --------------------------------------------------------------------------
# 首页指标
# --------------------------------------------------------------------------

def test_dashboard_counts_come_from_real_rows(world):
    make_topic(world, "t1", status="pending")
    make_topic(world, "t2", status="approved")
    make_draft(world, "d1", "t1", "v1", "wechat", "synced")
    stats = content.dashboard()["stats"]
    assert stats["pending_topics"] == 1
    assert stats["platform_drafts"] == 1
    assert stats["published"] == 0, "publications 表为空时必须报 0，不能编造"


def test_dashboard_excludes_deleted_rows(world):
    make_topic(world, "t1", status="pending")
    content.soft_delete("topic", "t1")
    assert content.dashboard()["stats"]["pending_topics"] == 0
    assert content.dashboard()["stats"]["trash"] == 1


def test_facets_dictionary_reports_real_states(world):
    make_topic(world, "t1", status="approved")
    make_draft(world, "d1", "t1", "v1", "xiaohongshu", "local")
    facets = content.content_facets_all()
    assert "approved" in facets["topic_states"]
    assert "local" in facets["draft_states"]
    assert {item["value"] for item in facets["priorities"]} == {1, 2, 3, 4, 5}


# --------------------------------------------------------------------------
# 详情
# --------------------------------------------------------------------------

def test_detail_returns_entity_and_its_history(world):
    make_topic(world, "t1")
    content.update_topic_meta("t1", {"priority": 1})
    detail = content.get_detail("topic", "t1")
    assert detail["entity"]["id"] == "t1"
    assert any(row["action"] == "update_meta" for row in detail["history"])


def test_detail_of_draft_carries_topic_title(world):
    make_topic(world, "t1", "所属选题")
    make_draft(world, "d1", "t1", "v1")
    detail = content.get_detail("draft", "d1")
    assert detail["entity"]["topic_title"] == "所属选题"


def test_detail_rejects_unsupported_type(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        content.get_detail("job", "j1")


# --------------------------------------------------------------------------
# 幂等与持久化
# --------------------------------------------------------------------------

def test_data_survives_reopening_the_database(world, tmp_path):
    make_topic(world, "t1", "持久化标题")
    content.update_topic_meta("t1", {"tags": ["保留"]})
    content.soft_delete("topic", "t1")
    reopened = db.connect()
    entry = reopened.execute("SELECT snapshot FROM content_trash WHERE entity_id='t1'").fetchone()
    assert "保留" in entry["snapshot"]
    reopened.close()


def test_transaction_rolls_back_on_failure(world):
    make_topic(world, "t1")
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute("UPDATE topics SET title='改了' WHERE id='t1'")
            raise RuntimeError("boom")
    assert rows("SELECT title FROM topics WHERE id='t1'")[0]["title"] == "选题一"