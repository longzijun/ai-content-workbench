from __future__ import annotations

import ctypes
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = PROJECT_ROOT / "runtime"
_lock = threading.RLock()


def authorized_vault(runtime: Path) -> str:
    """Resolve the explicitly authorized vault or use an isolated local folder."""
    path = os.environ.get("WORKBENCH_VAULT_PATH") or runtime / "vault"
    return str(Path(path).resolve())


def discover_codex() -> str:
    """Preserve the legacy settings hook with discovery disabled."""
    return ""


@dataclass
class Settings:
    runtime_dir: Path
    vault_path: str = ""
    codex_path: str = ""
    local_base_url: str = "http://127.0.0.1:1234/v1"
    local_model: str = ""
    task_timeout_seconds: int = 300
    draft_destination: str = "platform"
    wechat_app_id: str = ""
    wechat_thumb_media_id: str = ""
    wechat_capability_verified: bool = False
    comfy_base_url: str = "http://127.0.0.1:8188"
    image_checkpoint_name: str = ""
    image_max_assets: int = 5
    backup_copy_dir: str = ""

    def __post_init__(self) -> None:
        if not self.vault_path:
            self.vault_path = authorized_vault(self.runtime_dir)


def get_settings(runtime_dir: Path | str | None = None) -> Settings:
    with _lock:
        runtime = Path(runtime_dir or os.environ.get("WORKBENCH_RUNTIME_DIR", DEFAULT_RUNTIME)).resolve()
        for name in ("data", "logs", "task-workspaces", "exports", "backups"):
            (runtime / name).mkdir(parents=True, exist_ok=True)
        path = runtime / "settings.json"
        raw = json.loads(path.read_text("utf-8")) if path.exists() else {}
        allowed = {key for key in Settings.__dataclass_fields__ if key != "runtime_dir"}
        values = {key: value for key, value in raw.items() if key in allowed}
        values.setdefault("codex_path", discover_codex())
        values["vault_path"] = authorized_vault(runtime)
        return Settings(runtime_dir=runtime, **values)


def _write_settings(settings: Settings) -> None:
    values = asdict(settings)
    values.pop("runtime_dir")
    path = settings.runtime_dir / "settings.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def update_settings(values: dict) -> Settings:
    with _lock:
        settings = get_settings()
        writable = {"vault_path", "codex_path", "local_base_url", "local_model", "task_timeout_seconds",
                    "comfy_base_url", "image_checkpoint_name", "image_max_assets", "backup_copy_dir"}
        unexpected = set(values) - writable
        if unexpected:
            raise ValueError("不支持的设置字段：" + ", ".join(sorted(unexpected)))
        if "local_base_url" in values:
            _require_loopback_url(str(values["local_base_url"]), "本地模型地址")
        if "comfy_base_url" in values:
            _require_loopback_url(str(values["comfy_base_url"]), "ComfyUI 地址")
        if "image_checkpoint_name" in values:
            values["image_checkpoint_name"] = str(values["image_checkpoint_name"]).strip()[:200]
        if "image_max_assets" in values:
            values["image_max_assets"] = int(values["image_max_assets"])
            if not 1 <= values["image_max_assets"] <= 10:
                raise ValueError("单次出图数量需在 1 到 10 张之间")
        if "task_timeout_seconds" in values:
            values["task_timeout_seconds"] = int(values["task_timeout_seconds"])
            if not 30 <= values["task_timeout_seconds"] <= 1800:
                raise ValueError("任务超时需在30到1800秒之间")
        if "vault_path" in values:
            vault = Path(str(values["vault_path"])).resolve()
            permitted = Path(settings.vault_path)
            if vault != permitted:
                raise ValueError("写入目录需由启动命令授权。请使用 --vault 选项。")
            values["vault_path"] = str(permitted)
        if "codex_path" in values and values["codex_path"]:
            path = Path(str(values["codex_path"]))
            if path.name.lower() != "codex.exe" or not path.is_file():
                raise ValueError("请选择存在的 codex.exe 运行程序")
        if "backup_copy_dir" in values and values["backup_copy_dir"]:
            backup = Path(str(values["backup_copy_dir"]))
            if not backup.is_absolute() or backup.resolve() == Path(backup.anchor):
                raise ValueError("备份副本目录必须是明确的绝对目录，不能是磁盘根目录")
        for key, value in values.items():
            setattr(settings, key, value)
        _write_settings(settings)
        return settings


def _require_loopback_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query:
        raise ValueError(f"{label}必须是本机回环 HTTP 地址")


def settings_to_dict(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    result = asdict(settings)
    result["runtime_dir"] = str(settings.runtime_dir)
    result.pop("wechat_app_id", None)
    result.pop("wechat_thumb_media_id", None)
    result.pop("wechat_capability_verified", None)
    app_id = settings.wechat_app_id
    result["integrations"] = {
        "wechat": {
            "configured": _credential_path(settings).exists() and bool(app_id),
            "capability_verified": settings.wechat_capability_verified,
            "app_id_masked": app_id[:4] + "…" + app_id[-4:] if app_id else "",
            "thumb_media_id": settings.wechat_thumb_media_id,
        },
        "toutiao": {"configured": False, "capability_verified": False, "reason": "尚无已验证的文章/微头条官方草稿接口接入"},
        "xiaohongshu": {"configured": False, "capability_verified": False, "reason": "尚无已验证的普通创作者官方草稿接口接入"},
    }
    result["cloud_enabled"] = False
    return result


def _credential_path(settings: Settings) -> Path:
    return settings.runtime_dir / "data" / "wechat.credentials"


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("凭据保存需要 Windows DPAPI，不支持明文回退")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = library.CryptUnprotectData if decrypt else library.CryptProtectData
    ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(target.data, ctypes.c_void_p))


def save_wechat_credentials(app_id: str, app_secret: str, thumb_media_id: str) -> Settings:
    if not app_id.strip() or not app_secret.strip():
        raise ValueError("请输入 AppID 和 AppSecret，凭据不会回显")
    if len(app_id) > 100 or len(app_secret) > 300 or len(thumb_media_id) > 300:
        raise ValueError("接口配置长度不正确")
    with _lock:
        settings = get_settings()
        cipher = _dpapi(json.dumps({"app_id": app_id.strip(), "app_secret": app_secret.strip()}).encode())
        path = _credential_path(settings)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(cipher)
        temporary.replace(path)
        settings.wechat_app_id = app_id.strip()
        settings.wechat_thumb_media_id = thumb_media_id.strip()
        settings.wechat_capability_verified = False
        _write_settings(settings)
        return settings


def get_wechat_credentials(settings: Settings) -> dict | None:
    path = _credential_path(settings)
    if not path.exists():
        return None
    return json.loads(_dpapi(path.read_bytes(), decrypt=True))


def set_wechat_verified(verified: bool) -> None:
    with _lock:
        settings = get_settings()
        settings.wechat_capability_verified = verified
        _write_settings(settings)
