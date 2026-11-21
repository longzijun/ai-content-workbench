"""Idle daily backup with verified retention; no OS scheduled tasks required."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, db, execution_runtime
from .backup_tools import verify_archive
from .config import get_settings


def _read_ledger(path):
    if not path.exists():
        return {"schema": 1, "items": [], "last_attempt_date": None}
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('invalid ledger')
        return data
    except (OSError, ValueError, TypeError):
        return {'schema': 1, 'items': [], 'invalid': True, 'last_error': '自动备份清单损坏；未覆盖历史清单或清理文件，请先核对。'}


def _save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _retained_ids(items):
    ordered = sorted(items, key=lambda item: item["created_at"], reverse=True)
    daily, weekly = {}, {}
    for item in ordered:
        moment = datetime.fromisoformat(item["created_at"])
        daily.setdefault(moment.date().isoformat(), item["id"])
        year, week, _ = moment.isocalendar()
        weekly.setdefault(f"{year}-{week}", item["id"])
    return set(list(daily.values())[:7]) | set(list(weekly.values())[:4])


def tick(moment=None):
    moment = moment or datetime.now(timezone.utc)
    # A day is an operating day in Beijing, not the host's timezone.
    from datetime import timedelta
    today = moment.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    settings = get_settings()
    root = (settings.runtime_dir / "backups").resolve()
    ledger_path = root / "automatic-backups.json"
    ledger = _read_ledger(ledger_path)
    if ledger.get('invalid'):
        execution_runtime.component_event('backup', error=ledger['last_error'])
        return False
    if ledger.get("last_attempt_date") == today:
        return False
    with db.connect() as connection:
        active = connection.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone()
    if active or execution_runtime.model_busy():
        return False
    mutex = execution_runtime.ProcessLock(settings.runtime_dir / "automatic-backup.lock")
    if not mutex.acquire():
        return False
    try:
        ledger = _read_ledger(ledger_path)
        if ledger.get("last_attempt_date") == today:
            return False
        ledger["last_attempt_date"] = today
        ledger["last_error"] = None
        _save(ledger_path, ledger)
        result = artifacts.create_backup(settings)
        path = Path(result["path"]).resolve()
        verification = verify_archive(path)
        item = {"id": path.stem, "filename": path.name, "created_at": result["created_at"],
                "sha256": _hash(path), "verified": verification["verified"], "off_disk_copy": False,
                "copy_verified": False}
        # A separately configured path is never silently invented. A different
        # folder on the same physical disk must not be described as off-disk.
        target = settings.backup_copy_dir.strip()
        if target:
            destination_root = Path(target).resolve()
            if destination_root.anchor.casefold() == root.anchor.casefold():
                raise ValueError("异盘备份目标仍位于同一盘符，未记为异盘副本")
            destination_root.mkdir(parents=True, exist_ok=True)
            copy = destination_root / path.name
            shutil.copy2(path, copy)
            if _hash(copy) != item["sha256"]:
                raise ValueError("异盘备份副本校验不一致")
            item["copy_verified"] = True
            # Different drive letters can still be partitions of the same
            # physical disk. Without verified hardware identity we report a
            # verified copy, not a completed off-disk disaster recovery setup.
            item["off_disk_verification_required"] = True
        ledger["items"].append(item)
        keep = _retained_ids(ledger["items"])
        remaining = []
        for old in ledger["items"]:
            candidate = (root / old["filename"]).resolve()
            if old["id"] in keep or not candidate.is_relative_to(root) or candidate.parent != root:
                remaining.append(old)
                continue
            # Only remove our own previously verified, unchanged archive. Never
            # glob/delete the runtime or a user-created/manual backup.
            if old.get("verified") and candidate.is_file() and _hash(candidate) == old["sha256"]:
                candidate.unlink()
            else:
                remaining.append(old)
        ledger["items"] = remaining
        ledger["last_success_at"] = result["created_at"]
        _save(ledger_path, ledger)
        execution_runtime.component_event("backup", success=True)
        return True
    except Exception as exc:
        ledger["last_error"] = str(exc)[:800]
        _save(ledger_path, ledger)
        execution_runtime.component_event("backup", error=exc)
        return False
    finally:
        mutex.release()


def status():
    ledger = _read_ledger(get_settings().runtime_dir / "backups" / "automatic-backups.json")
    return {"last_success_at": ledger.get("last_success_at"), "last_error": ledger.get("last_error"),
            "verified_backups": sum(bool(item.get("verified")) for item in ledger["items"]),
            "off_disk_copy": any(item.get("off_disk_copy") for item in ledger["items"]),
            "copy_verified": any(item.get("copy_verified") for item in ledger["items"]),
            "retention": {"daily": 7, "weekly": 4}}
