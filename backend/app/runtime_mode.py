"""持久化运行模式与平台触达闸门（V3）。

公开版通过服务端闸门执行本地内容模式。即使磁盘上的旧设置包含平台许可，
工作台也会阻止自动登录、检查账号和保存平台草稿。

- 唯一本轮授权的模式是 ``local_content_only``：工作台只生产本地内容
  （共享事实底稿、三平台作品、本地排版、集中审阅、材料包、人工发布台账）。
- ``platform_operations`` 模式在代码里存在，但本轮未发布；即使数据库里
  被写成该值也不生效，防止配置被改开后自动恢复账号触达。
- 新安装、旧库升级、表缺失、值损坏：一律按 ``local_content_only``。

所有可能触达平台的入口（HTTP 端点、delivery worker、浏览器桥、平台 API
适配器、研究抓取、独立脚本）都必须经过 :func:`require_platform_operations`
或 :func:`platform_operations_allowed`，不接受「只在 UI 上禁用」。
"""
from __future__ import annotations

from fastapi import HTTPException

MODE_LOCAL_CONTENT_ONLY = "local_content_only"
MODE_PLATFORM_OPERATIONS = "platform_operations"
MODES = (MODE_LOCAL_CONTENT_ONLY, MODE_PLATFORM_OPERATIONS)
DEFAULT_MODE = MODE_LOCAL_CONTENT_ONLY

_KEY = "execution_mode"
_TABLE = "runtime_state"

# 公开版的内容发布由用户在官方应用中完成。
PLATFORM_OPERATIONS_RELEASED = False

#: 允许启用草稿保存的平台白名单。小红书永久锁（见 XHS_PLATFORM）；
#: 视频号/快手/抖音等平台本轮不进白名单，避免未验证的自动触达。
DRAFT_SAVE_WHITELIST = frozenset({"wechat", "toutiao"})

# 旧接口被拒绝时返回的稳定错误码（前端与测试都认这个码）。
PLATFORM_DISABLED_CODE = "platform_operations_disabled"
PLATFORM_DISABLED_MESSAGE = "当前为本地内容模式，禁止平台操作"


def _connect():
    from . import db
    return db.connect()


def _ensure_table(connection) -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")


def get_mode(connection=None) -> str:
    """读取运行模式。任何异常都回落默认模式（fail-closed）。"""
    if not PLATFORM_OPERATIONS_RELEASED:
        return DEFAULT_MODE
    owned = connection is None
    if owned:
        connection = _connect()
    try:
        _ensure_table(connection)
        row = connection.execute(f"SELECT value FROM {_TABLE} WHERE key=?", (_KEY,)).fetchone()
        value = row[0] if row else DEFAULT_MODE
        return value if value in MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE
    finally:
        if owned:
            connection.close()


def set_mode(value: str, connection=None) -> str:
    """保存运行模式，供支持平台操作的构建使用。"""
    if value not in MODES:
        raise HTTPException(status_code=422, detail={"code": "unknown_mode", "message": "未知的运行模式"})
    owned = connection is None
    if owned:
        connection = _connect()
    try:
        _ensure_table(connection)
        from .service import now
        connection.execute(
            f"INSERT INTO {_TABLE}(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (_KEY, value, now()))
        if owned:
            connection.commit()
        return value
    finally:
        if owned:
            connection.close()


def platform_operations_allowed(connection=None) -> bool:
    """平台自动操作（浏览器、平台接口、草稿保存、账号检查）当前是否放行。"""
    if not PLATFORM_OPERATIONS_RELEASED:
        return False
    return get_mode(connection) == MODE_PLATFORM_OPERATIONS


def require_platform_operations(action: str) -> None:
    """平台触达闸门。不放行时抛出稳定的 403，调用方不得捕获后重试。"""
    if not platform_operations_allowed():
        raise HTTPException(status_code=403, detail={
            "code": PLATFORM_DISABLED_CODE,
            "message": f"{PLATFORM_DISABLED_MESSAGE}（{action}）。"
                       "发布请在各平台官方应用或后台人工完成，工作台不再自动保存草稿或浏览页面。"})


# ---------------------------------------------------------------- 逐平台/账号草稿保存许可（批次D）

#: 小红书永久关闭自动触达：即使其余平台启用，小红书也绝不写入。
XHS_PLATFORM = "xiaohongshu"
_ACCESS_TABLE = "platform_access"


def _access_rows(connection=None):
    from . import db
    owned = connection is None
    if owned:
        connection = db.connect()
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_ACCESS_TABLE} ("
            "platform TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "account_label TEXT NOT NULL DEFAULT '', enabled_at TEXT, updated_at TEXT NOT NULL)")
        rows = connection.execute(f"SELECT * FROM {_ACCESS_TABLE}").fetchall()
        return {row["platform"]: dict(row) for row in rows}
    finally:
        if owned:
            connection.close()


def platform_draft_supported(platform: str, connection=None) -> bool:
    """某平台草稿保存是否在该平台层面放行：白名单内 + 已启用 + 非小红书。

    这是**逐平台许可层**，独立于全局放行闸门（platform_operations_allowed）。
    入口必须**同时**过全局闸门（require_platform_operations）与本层
    （require_platform_save）两道：全局关则一律不写，全局开后仍需逐平台启用。
    """
    if platform == XHS_PLATFORM:
        return False  # 小红书永久关闭自动触达，不因任何配置改变。
    if platform not in DRAFT_SAVE_WHITELIST:
        return False  # 白名单外平台（视频号/快手/抖音等）本轮不放行草稿保存。
    record = _access_rows(connection).get(platform, {})
    return bool(record.get("enabled"))


def require_platform_save(platform: str, action: str) -> None:
    """逐平台草稿保存闸门。不放行时抛稳定 403；小红书单独给明确文案。"""
    if platform == XHS_PLATFORM:
        raise HTTPException(status_code=403, detail={
            "code": PLATFORM_DISABLED_CODE,
            "message": "小红书保持关闭自动触达，不保存草稿、不登录、不浏览；请在小红书官方应用人工发布。"})
    if not platform_draft_supported(platform):
        raise HTTPException(status_code=403, detail={
            "code": PLATFORM_DISABLED_CODE,
            "message": f"该平台草稿保存未启用（{action}）。请先逐平台/账号启用并完成真实账号核验。"})


def platform_access_view(connection=None) -> dict:
    """逐平台草稿保存许可视图：启用状态 + 账号标签 + 小红书永久关闭 + 草稿能力。"""
    from .platform_registry import PLATFORM_IDS, draft_capability
    rows = _access_rows(connection)
    platforms = []
    for platform in PLATFORM_IDS:
        record = rows.get(platform, {})
        platforms.append({
            "platform": platform,
            "enabled": bool(record.get("enabled")) and platform != XHS_PLATFORM,
            "account_label": record.get("account_label", ""),
            "enabled_at": record.get("enabled_at"),
            "blocked": platform == XHS_PLATFORM,
            "blocked_reason": "小红书保持关闭自动触达" if platform == XHS_PLATFORM else "",
            "draft_capability": draft_capability(platform),
        })
    return {"platforms": platforms,
            "global_released": PLATFORM_OPERATIONS_RELEASED,
            "mode": get_mode(connection)}


def set_platform_access(platform: str, enabled: bool, account_label: str = "", connection=None) -> dict:
    """启用/停用某平台草稿保存。小红书一律拒绝；白名单外平台不允许启用；停用不要求账号标签。"""
    from . import service
    if platform == XHS_PLATFORM:
        raise HTTPException(status_code=409, detail={
            "code": "platform_blocked",
            "message": "小红书保持关闭自动触达，不能启用草稿保存。"})
    if enabled and platform not in DRAFT_SAVE_WHITELIST:
        raise HTTPException(status_code=409, detail={
            "code": "platform_not_whitelisted",
            "message": "该平台本轮未开放草稿保存；当前仅公众号与今日头条可启用。"})
    label = str(account_label or "").strip()[:200]
    if enabled and not label:
        raise HTTPException(status_code=422, detail={"code": "account_required", "message": "启用平台草稿保存必须填写账号标签"})
    from . import db
    if connection is not None:
        connection.execute(
            f"INSERT INTO {_ACCESS_TABLE}(platform,enabled,account_label,enabled_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(platform) DO UPDATE SET enabled=excluded.enabled,account_label=excluded.account_label,"
            "enabled_at=excluded.enabled_at,updated_at=excluded.updated_at",
            (platform, 1 if enabled else 0, label, service.now() if enabled else None, service.now()))
    else:
        with db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {_ACCESS_TABLE}(platform,enabled,account_label,enabled_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(platform) DO UPDATE SET enabled=excluded.enabled,account_label=excluded.account_label,"
                "enabled_at=excluded.enabled_at,updated_at=excluded.updated_at",
                (platform, 1 if enabled else 0, label, service.now() if enabled else None, service.now()))
    return platform_access_view()


def mode_view(connection=None) -> dict:
    """给设置页 / 总览 / 启动自检使用的模式视图。"""
    mode = get_mode(connection)
    local = mode == MODE_LOCAL_CONTENT_ONLY
    return {
        "mode": mode,
        "mode_label": "本地内容模式" if local else "平台操作模式",
        "platform_operations": platform_operations_allowed(connection),
        "platform_operations_released": PLATFORM_OPERATIONS_RELEASED,
        "note": ("平台自动操作已关闭：工作台不启动浏览器、不保存平台草稿、不调用平台接口、"
                 "不抓取小红书链接。发布与互动请在各平台官方应用人工完成。") if local else
                "平台操作模式已开启：仅公众号与今日头条可经逐平台启用后保存草稿，"
                "小红书及其余平台保持关闭；公开发布仍在官方应用人工完成。",
    }
