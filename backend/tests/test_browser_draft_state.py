"""Crash/duplicate safety only; fake UI IDs do not certify any platform adapter."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading

import pytest

from backend.app import browser_draft_state as state


WORK = {"platform": "toutiao", "title": "测试标题", "body": "尚未公开的测试正文", "tags": ["测试"], "images": ["asset-1"]}
ACCOUNT = "测试账号"
# A plausible UI ID shape used only inside a temporary ledger; no live platform call.
UI_ID = "7517424010146334720"


@pytest.fixture
def ledger(tmp_path):
    return state.BrowserDraftLedger(tmp_path / "runtime")


def claim(ledger, **changes):
    arguments = {"topic_id": "topic-1", "version_id": "version-1", "platform": "toutiao", "content": WORK, "account_label": ACCOUNT}
    arguments.update(changes)
    return ledger.claim(**arguments)


def stored(ledger):
    return ledger.load("topic-1", "version-1", "toutiao")


def test_stable_key_scopes_topic_version_platform_and_not_mutable_content():
    first = state.operation_key("topic-1", "version-1", "toutiao")
    assert first == state.operation_key("topic-1", "version-1", "toutiao")
    assert len(first) == 64
    assert len({first, state.operation_key("topic-2", "version-1", "toutiao"), state.operation_key("topic-1", "version-2", "toutiao"), state.operation_key("topic-1", "version-1", "wechat")}) == 4


def test_content_hash_is_canonical_and_binds_all_intended_ui_fields():
    assert state.content_hash(WORK) == state.content_hash(dict(reversed(list(WORK.items()))))
    for field, value in (("title", "改标题"), ("body", "改正文"), ("tags", ["改标签"]), ("images", ["asset-2"])):
        assert state.content_hash({**WORK, field: value}) != state.content_hash(WORK)
    with pytest.raises(ValueError):
        state.content_hash({"invalid": float("nan")})


def test_reading_absent_ledger_does_not_create_runtime_files(ledger):
    assert stored(ledger) is None
    assert not ledger.directory.exists()


def test_creation_is_persisted_as_writing_before_ui_action_is_authorized(ledger):
    ticket = claim(ledger)
    assert ticket.action == "create"
    assert ticket.state == "writing" and ticket.remote_id is None
    record = stored(ledger)
    assert record["state"] == "writing"
    assert record["operation_id"] == ticket.operation_id
    assert record["content_hash"] == state.content_hash(WORK)
    on_disk = (ledger.directory / (ticket.key + ".json")).read_text("utf-8")
    assert WORK["body"] not in on_disk and WORK["title"] not in on_disk
    assert "synced" not in on_disk


def test_restarted_writing_operation_cannot_create_another_draft(ledger):
    first = claim(ledger)
    restarted = state.BrowserDraftLedger(ledger.directory.parents[1])
    again = claim(restarted)
    assert again.action == "blocked" and again.state == "writing"
    assert again.operation_id == first.operation_id
    assert "不会重复创建" in again.reason


def test_unknown_without_id_never_authorizes_creation(ledger):
    ticket = claim(ledger)
    ledger.mark_unknown(ticket)
    for _ in range(3):
        again = claim(ledger)
        assert again.action == "blocked" and again.state == "unknown"
        assert again.remote_id is None


def test_observed_id_permits_only_read_and_survives_restart(ledger):
    ticket = claim(ledger)
    record = ledger.record_saved(ticket, UI_ID)
    assert record["remote_id"] == UI_ID and record["state"] == "saved"
    restarted = state.BrowserDraftLedger(ledger.directory.parents[1])
    again = claim(restarted)
    assert again.action == "read" and again.remote_id == UI_ID
    assert again.operation_id == ticket.operation_id


def test_uncertain_readback_preserves_id_and_only_authorizes_read(ledger):
    ticket = claim(ledger)
    ledger.record_saved(ticket, UI_ID)
    ledger.mark_unknown(ticket)
    again = claim(ledger)
    assert again.action == "read" and again.state == "unknown"
    assert again.remote_id == UI_ID
    # A verified readback may restore saved evidence, while retaining the same ID.
    ledger.record_saved(again, UI_ID)
    assert stored(ledger)["state"] == "saved"


@pytest.mark.parametrize("saved", [False, True])
def test_account_change_cannot_reuse_claim_or_existing_remote_id(ledger, saved):
    original = claim(ledger)
    if saved:
        ledger.record_saved(original, UI_ID)
    again = claim(ledger, account_label="另一个账号")
    assert again.action == "blocked" and "账号" in again.reason
    with pytest.raises(state.LedgerConflict):
        ledger.record_saved(again, UI_ID)
    assert stored(ledger)["account_label"] == ACCOUNT


@pytest.mark.parametrize("saved", [False, True])
def test_changed_content_requires_new_version_and_does_not_modify_original(ledger, saved):
    original = claim(ledger)
    if saved:
        ledger.record_saved(original, UI_ID)
    before = stored(ledger)
    again = claim(ledger, content={**WORK, "body": "同版本被修改"})
    assert again.action == "blocked" and "新版本" in again.reason
    assert stored(ledger) == before
    assert claim(ledger, version_id="version-2", content={**WORK, "body": "新版本正文"}).action == "create"


def test_existing_remote_id_cannot_be_replaced(ledger):
    ticket = claim(ledger)
    ledger.record_saved(ticket, UI_ID)
    again = claim(ledger)
    with pytest.raises(state.LedgerConflict):
        ledger.record_saved(again, "7517424010146334721")
    assert stored(ledger)["remote_id"] == UI_ID


def test_late_result_must_match_operation_hash_and_account(ledger):
    ticket = claim(ledger)
    for forged in (replace(ticket, operation_id="f" * 32), replace(ticket, content_hash="f" * 64), replace(ticket, account_label="另一个账号"), replace(ticket, version_id="version-2")):
        with pytest.raises(state.LedgerConflict):
            ledger.record_saved(forged, UI_ID)
    assert stored(ledger)["state"] == "writing"
    # A late real ID from the same interrupted attempt is useful evidence.
    ledger.mark_unknown(ticket)
    ledger.record_saved(ticket, UI_ID)
    assert claim(ledger).action == "read"


def test_concurrent_instances_allow_exactly_one_initial_creation(ledger):
    barrier = threading.Barrier(12)

    def reserve(_):
        other = state.BrowserDraftLedger(ledger.directory.parents[1])
        barrier.wait(timeout=5)
        return claim(other)

    with ThreadPoolExecutor(max_workers=12) as pool:
        claims = list(pool.map(reserve, range(12)))
    assert sum(ticket.action == "create" for ticket in claims) == 1
    assert sum(ticket.action == "blocked" for ticket in claims) == 11
    assert len({ticket.operation_id for ticket in claims}) == 1


def test_claim_write_failure_never_authorizes_ui_creation_and_cleans_temp(ledger, monkeypatch):
    def fail_replace(source, destination):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(state.LedgerError, match="原子落盘"):
        claim(ledger)
    assert stored(ledger) is None
    assert list(ledger.directory.glob("*.tmp")) == []


def test_id_write_failure_keeps_prior_writing_evidence_and_blocks_duplicate(ledger, monkeypatch):
    ticket = claim(ledger)
    original = (ledger.directory / (ticket.key + ".json")).read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated interruption after UI save")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(state.LedgerError):
        ledger.record_saved(ticket, UI_ID)
    assert (ledger.directory / (ticket.key + ".json")).read_bytes() == original
    assert claim(ledger).action == "blocked"
    assert list(ledger.directory.glob("*.tmp")) == []


@pytest.mark.parametrize("remote_id", ["", "https://mp.toutiao.com/editor?id=7517424010146334720&token=private", "Cookie:session=value", "access_token:private", "draft/id", "draft?id=1", "draft\n1", None])
def test_remote_evidence_rejects_urls_cookies_and_credential_fields(ledger, remote_id):
    ticket = claim(ledger)
    with pytest.raises(ValueError):
        ledger.record_saved(ticket, remote_id)
    assert stored(ledger)["state"] == "writing"
    assert stored(ledger)["remote_id"] is None


@pytest.mark.parametrize("changes", [{"topic_id": "../outside"}, {"version_id": "https://example.invalid/?token=secret"}, {"platform": "other"}, {"account_label": "https://mp.weixin.qq.com/?token=secret"}, {"account_label": "cookie:session-secret"}, {"account_label": ""}])
def test_invalid_identity_is_rejected_before_any_ledger_write(ledger, changes):
    with pytest.raises(ValueError):
        claim(ledger, **changes)
    assert not ledger.directory.exists()


@pytest.mark.parametrize("damage", ["not json", "invalid bytes", "missing id", "unexpected credentials", "wrong key"])
def test_corrupt_or_foreign_ledger_fails_closed(ledger, damage):
    ticket = claim(ledger)
    path = ledger.directory / (ticket.key + ".json")
    record = stored(ledger)
    if damage == "not json":
        path.write_text("{broken", "utf-8")
    elif damage == "invalid bytes":
        path.write_bytes(b"\xff\xfe\x00")
    else:
        if damage == "missing id":
            record["state"] = "saved"
        elif damage == "unexpected credentials":
            record["cookie"] = "test-only-private"
        elif damage == "wrong key":
            record["key"] = "f" * 64
        path.write_text(json.dumps(record), "utf-8")
    with pytest.raises(state.LedgerError):
        claim(ledger)
    with pytest.raises(state.LedgerError):
        stored(ledger)
