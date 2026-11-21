"""Single-owner authentication. Legacy anonymous sessions are never accepted."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from fastapi import HTTPException

from . import db

COOKIE = "workbench_session"
LIFETIME_SECONDS = 43200


def stamp():
    return datetime.now(timezone.utc).isoformat()


def init_schema(connection=None):
    if connection is None:
        with db.transaction() as active:
            init_schema(active)
        return
    connection.execute("CREATE TABLE IF NOT EXISTS auth_owner (id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_sessions (id TEXT PRIMARY KEY, token TEXT NOT NULL, device_name TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_failures (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_at TEXT NOT NULL)")


def configured():
    try:
        with db.connect() as connection:
            return connection.execute("SELECT 1 FROM auth_owner WHERE id=1").fetchone() is not None
    except Exception:
        return False


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def setup_password(password, *, reset=False):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("密码须为 12 到 256 个字符")
    init_schema()
    salt = secrets.token_bytes(16)
    encoded = "scrypt$16384$8$1$" + salt.hex() + "$" + _hash(password, salt).hex()
    with db.transaction() as connection:
        if connection.execute("SELECT 1 FROM auth_owner WHERE id=1").fetchone() and not reset:
            raise ValueError("账号已经初始化；重设密码需要本机显式 reset 操作")
        connection.execute("INSERT INTO auth_owner(id,password_hash,created_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash", (encoded, stamp()))
        connection.execute("DELETE FROM auth_sessions")
        connection.execute("DELETE FROM auth_failures")


def current(session_id):
    if not session_id:
        return None
    try:
        with db.connect() as connection:
            row = connection.execute("SELECT * FROM auth_sessions WHERE id=? AND expires_at>?", (session_id, stamp())).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def login(password, device_name, key):
    if not configured():
        raise HTTPException(status_code=503, detail="请先在工作台主机初始化登录密码")
    moment = datetime.now(timezone.utc)
    cutoff = (moment - timedelta(minutes=15)).isoformat()
    # All ingress may be a local proxy. A global limiter avoids trusting any
    # client-supplied forwarded address to bypass the password attempt budget.
    key = "owner-login"
    with db.transaction() as connection:
        failure = connection.execute("SELECT * FROM auth_failures WHERE key=?", (key,)).fetchone()
        if failure and failure["window_at"] > cutoff and failure["attempts"] >= 10:
            raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试")
        owner = connection.execute("SELECT password_hash FROM auth_owner WHERE id=1").fetchone()
        connection.execute("INSERT INTO auth_failures(key,attempts,window_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN window_at<? THEN 1 ELSE attempts+1 END,window_at=CASE WHEN window_at<? THEN excluded.window_at ELSE window_at END", (key, moment.isoformat(), cutoff, cutoff))
    valid = False
    try:
        parts = owner["password_hash"].split("$")
        if len(parts) == 6 and parts[:4] == ["scrypt", "16384", "8", "1"]:
            valid = hmac.compare_digest(_hash(password, bytes.fromhex(parts[4])), bytes.fromhex(parts[5]))
    except (ValueError, TypeError):
        pass
    if not valid:
        raise HTTPException(status_code=401, detail="密码不正确")
    session = {"id": secrets.token_urlsafe(32), "token": secrets.token_urlsafe(32),
               "device_name": device_name.strip()[:100] or "浏览器", "created_at": moment.isoformat(),
               "expires_at": (moment + timedelta(seconds=LIFETIME_SECONDS)).isoformat()}
    with db.transaction() as connection:
        current_owner = connection.execute("SELECT password_hash FROM auth_owner WHERE id=1").fetchone()
        if not current_owner or not hmac.compare_digest(current_owner["password_hash"], owner["password_hash"]):
            raise HTTPException(status_code=401, detail="登录配置已更新，请使用新密码重试")
        connection.execute("DELETE FROM auth_failures WHERE key=?", (key,))
        connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (stamp(),))
        connection.execute("INSERT INTO auth_sessions(id,token,device_name,created_at,expires_at) VALUES(:id,:token,:device_name,:created_at,:expires_at)", session)
    return session


def list_sessions(session_id):
    with db.connect() as connection:
        rows = connection.execute("SELECT id,device_name,created_at,expires_at FROM auth_sessions WHERE expires_at>? ORDER BY created_at DESC", (stamp(),)).fetchall()
    return [{**dict(row), "current": row["id"] == session_id} for row in rows]


def revoke(session_id):
    with db.transaction() as connection:
        connection.execute("DELETE FROM auth_sessions WHERE id=?", (session_id,))
