"""Durable duplicate protection for draft-only operations on official web UIs.

Claim immediately before the first UI write. A successful claim is already
persisted as ``writing``; a crash therefore cannot authorize another creation.
The caller must obtain a real UI draft ID and independently read back its
contents. This ledger records that ID; it does not prove platform synchronization.
It never stores content, cookies, browser storage, or credential-bearing URLs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Literal
import uuid


_PLATFORMS = {"wechat", "toutiao", "xiaohongshu"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PRIVATE = re.compile(r"https?://|(?:access[_-]?token|authorization|cookie|password|secret|session[_-]?id)\s*[:=]", re.I)
_FIELDS = {"schema_version", "key", "topic_id", "version_id", "platform", "content_hash", "account_label", "operation_id", "state", "remote_id", "created_at", "updated_at"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class LedgerError(RuntimeError):
    """The ledger cannot authorize a UI write; callers must fail closed."""


class LedgerConflict(LedgerError):
    """An operation or draft ID no longer matches the durable claim."""


@dataclass(frozen=True)
class DraftClaim:
    key: str
    topic_id: str
    version_id: str
    platform: str
    content_hash: str
    account_label: str
    operation_id: str
    action: Literal["create", "read", "blocked"]
    state: Literal["writing", "saved", "unknown"]
    remote_id: str | None = None
    reason: str | None = None


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or _PRIVATE.search(value):
        raise ValueError(label + "必须是独立ID，不能包含URL或凭据")
    return value


def _account(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value) or _PRIVATE.search(value):
        raise ValueError("账号标识必须是当前后台可见的账号名，不能包含URL或凭据")
    return value.strip()


def operation_key(topic_id: str, version_id: str, platform: str) -> str:
    """Stable across restarts and draft DB row IDs, scoped to one version/platform."""
    _identifier(topic_id, "选题ID")
    _identifier(version_id, "版本ID")
    if platform not in _PLATFORMS:
        raise ValueError("不支持的平台")
    raw = json.dumps([topic_id, version_id, platform], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_hash(content: dict | str) -> str:
    """Bind all intended UI fields; pass the work including title/body/assets/tags."""
    if not isinstance(content, (dict, str)) or not content:
        raise ValueError("草稿内容不能为空")
    try:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("草稿内容必须可规范化为JSON") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrowserDraftLedger:
    """Atomic on-disk claims, with process/thread locks only around ledger access.

    No method clears an uncertain operation. ``claim.action == 'read'`` permits
    only opening an existing draft and verifying it, never creating another.
    Save/edit URLs must remain exclusively in the authenticated browser session.
    """

    def __init__(self, runtime_dir: Path | str):
        self.directory = Path(runtime_dir).resolve() / "data" / "browser-draft-ledger"
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(str(self.directory), threading.RLock())

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".ledger.lock").open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                if os.name == "nt":
                    import msvcrt
                    deadline = time.monotonic() + 5
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            if time.monotonic() >= deadline:
                                raise LedgerError("草稿证明账本正在使用，未授权重复写入") from exc
                            time.sleep(0.025)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not _DIGEST.fullmatch(key):
            raise ValueError("草稿证明key无效")
        return self.directory / (key + ".json")

    def _read(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            if path.stat().st_size > 65_536:
                raise LedgerError("草稿证明账本大小异常，禁止重复创建")
            text = path.read_text("utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise LedgerError("草稿证明账本无法读取，禁止重复创建") from exc
        try:
            record = json.loads(text)
            if not isinstance(record, dict) or set(record) != _FIELDS or record["schema_version"] != 1:
                raise ValueError("schema")
            if record["key"] != key or operation_key(record["topic_id"], record["version_id"], record["platform"]) != key:
                raise ValueError("key")
            if not isinstance(record["content_hash"], str) or not _DIGEST.fullmatch(record["content_hash"]):
                raise ValueError("hash")
            if _account(record["account_label"]) != record["account_label"]:
                raise ValueError("account")
            if not isinstance(record["operation_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["operation_id"]):
                raise ValueError("operation")
            if record["state"] not in {"writing", "saved", "unknown"}:
                raise ValueError("state")
            if record["remote_id"] is not None:
                _identifier(record["remote_id"], "平台草稿ID")
            if record["state"] == "saved" and not record["remote_id"]:
                raise ValueError("missing ID")
            for field in ("created_at", "updated_at"):
                parsed = datetime.fromisoformat(record[field])
                if parsed.tzinfo is None:
                    raise ValueError("timestamp")
            return record
        except (ValueError, TypeError, KeyError) as exc:
            raise LedgerError("草稿证明账本损坏或格式不匹配，禁止重复创建") from exc

    def _write(self, record: dict) -> None:
        path = self._path(record["key"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise LedgerError("草稿证明未能原子落盘，不能继续平台写入") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self, topic_id: str, version_id: str, platform: str) -> dict | None:
        """Read evidence only. Absence alone never authorizes a creation."""
        key = operation_key(topic_id, version_id, platform)
        if not self._path(key).exists():
            return None
        with self._locked():
            return self._read(key)

    def claim(self, topic_id: str, version_id: str, platform: str, content: dict | str, account_label: str) -> DraftClaim:
        """Atomically reserve a first UI write, or return read/blocked instructions."""
        key = operation_key(topic_id, version_id, platform)
        digest = content_hash(content)
        account = _account(account_label)
        with self._locked():
            record = self._read(key)
            if record is None:
                now = _now()
                record = {"schema_version": 1, "key": key, "topic_id": topic_id, "version_id": version_id, "platform": platform, "content_hash": digest, "account_label": account, "operation_id": uuid.uuid4().hex, "state": "writing", "remote_id": None, "created_at": now, "updated_at": now}
                self._write(record)
                action, reason = "create", None
            elif record["content_hash"] != digest:
                action, reason = "blocked", "同一版本的内容已改变，请创建新版本后再保存草稿"
            elif record["account_label"] != account:
                action, reason = "blocked", "草稿绑定的后台账号已改变，请切回原账号核对"
            elif record["remote_id"]:
                action, reason = "read", None
            else:
                action, reason = "blocked", "上次保存进行中或结果不确定，请在平台核对；不会重复创建"
            return DraftClaim(key, topic_id, version_id, platform, digest, account, record["operation_id"], action, record["state"], record["remote_id"], reason)

    def _bound_record(self, claim: DraftClaim) -> dict:
        if not isinstance(claim, DraftClaim) or claim.action not in {"create", "read"}:
            raise LedgerConflict("没有可执行的草稿证明claim")
        record = self._read(claim.key)
        fields = ("topic_id", "version_id", "platform", "content_hash", "account_label", "operation_id")
        if record is None or any(record[field] != getattr(claim, field) for field in fields):
            raise LedgerConflict("草稿证明已改变，禁止记录旧操作结果")
        return record

    def record_saved(self, claim: DraftClaim, remote_id: str) -> dict:
        """Persist a real ID observed from the official UI; never generate one.

        This is not a ``synced`` result. The adapter still needs title/body readback.
        Existing IDs cannot be replaced, including after an uncertain readback.
        """
        remote_id = _identifier(remote_id, "平台草稿ID")
        with self._locked():
            record = self._bound_record(claim)
            if record["remote_id"] and record["remote_id"] != remote_id:
                raise LedgerConflict("已有平台草稿ID，禁止替换或创建另一个草稿")
            record.update(state="saved", remote_id=remote_id, updated_at=_now())
            self._write(record)
            return record

    def mark_unknown(self, claim: DraftClaim) -> dict:
        """Persist uncertainty without losing any already observed remote ID."""
        with self._locked():
            record = self._bound_record(claim)
            record.update(state="unknown", updated_at=_now())
            self._write(record)
            return record
