"""Visible official-platform sessions isolated in a dedicated Playwright thread.

No browser starts during health checks. Only an explicit open_login action creates
the project's Edge profile; account state comes from the current visible page.
"""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
from queue import Queue
import re
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid

from .config import get_settings


PLATFORM_URLS = {
    "wechat": "https://mp.weixin.qq.com/",
    "toutiao": "https://mp.toutiao.com/",
    "xiaohongshu": "https://creator.xiaohongshu.com/",
}
_HOME_PATHS = {"wechat": "/cgi-bin/home", "toutiao": "/profile_v4/index", "xiaohongshu": "/new/home"}
_RUN_HOME_URLS = {"wechat": PLATFORM_URLS["wechat"], "toutiao": PLATFORM_URLS["toutiao"].rstrip("/") + _HOME_PATHS["toutiao"], "xiaohongshu": PLATFORM_URLS["xiaohongshu"].rstrip("/") + _HOME_PATHS["xiaohongshu"]}
CDP_PORT = 9227

_DOM_SNAPSHOT = r"""() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const elements = [];
  for (const el of document.querySelectorAll('button,a,input,nav,h1,h2,h3,[role],[class*="account"],[class*="nickname"],[class*="user-name"],[class*="user_name"],[class*="username"],[class*="userName"],[class*="name"],[class*="profile"],div,span')) {
    if (!visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const text = tag === 'input' ? '' : clean(el.innerText).slice(0, 300);
    const label = clean(el.getAttribute('aria-label') || el.getAttribute('placeholder')).slice(0, 200);
    const accountNode = /account|nickname|user[-_]?name|name-box|profile|shead_name|auth-avator-name/.test(String(el.className || '').toLowerCase());
    if ((tag === 'div' || tag === 'span') && (text.length > 80 || (el.childElementCount > 0 && !accountNode))) continue;
    if (!text && !label) continue;
    let href = null;
    if (tag === 'a' && el.getAttribute('href')) {
      try { const u = new URL(el.getAttribute('href'), location.href); if (u.protocol === 'https:') href = u.origin + u.pathname; } catch {}
    }
    elements.push({tag, role: el.getAttribute('role') || '', text, label,
      class_name: typeof el.className === 'string' ? el.className.slice(0, 200) : '', href});
    if (elements.length >= 180) break;
  }
  return {text: String(document.body?.innerText || '').slice(0, 24000), elements};
}"""

_lock = threading.RLock()
_lifecycle_lock = threading.RLock()
_commands: Queue | None = None
_thread: threading.Thread | None = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty(platform):
    return {
        "platform": platform, "state": "disconnected", "account_label": None,
        "login_url": PLATFORM_URLS[platform], "message": "尚未打开平台后台",
        "browser_open": False, "last_checked": None,
        "draft_capability_verified": False,
    }


_status = {platform: _empty(platform) for platform in PLATFORM_URLS}


def _platform(platform):
    if platform not in PLATFORM_URLS:
        raise ValueError("不支持的平台")
    return platform


def _cached(platform):
    with _lock:
        return dict(_status[platform])


def _set(platform, **values):
    with _lock:
        previous = _status[platform]
        account_changed = "account_label" in values and values["account_label"] != previous["account_label"]
        _status[platform].update(values)
        # A verified draft remains verified only for the same connected account.
        if _status[platform]["state"] != "connected" or account_changed:
            _status[platform]["draft_capability_verified"] = False
        return dict(_status[platform])


def get_status(settings=None):
    """Return cached observations without loading Playwright or opening Edge."""
    with _lock:
        return {platform: dict(value) for platform, value in _status.items()}


def _safe_url(url):
    try:
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            return ""
        origin = "https://" + parsed.hostname
        if parsed.port and parsed.port != 443:
            origin += ":" + str(parsed.port)
        if parsed.hostname not in {urlparse(value).hostname for value in PLATFORM_URLS.values()}:
            return origin
        return origin + (parsed.path or "/")
    except (ValueError, TypeError):
        return ""


def _safe_text(value, limit=24000):
    value = str(value or "")[:limit]
    # Some rendered error messages contain a URL. Strip its query and fragment.
    value = re.sub(r"https?://[^\s<>\"']+", lambda match: _safe_url(match.group(0)) or "[链接]", value)
    value = re.sub(r"(?i)\b(?:token|access_token|refresh_token|id_token|authorization|app_secret|appsecret|session_token|api_key|cookies?)\s*[:=]\s*[^\s,;]+", "[凭据已省略]", value)
    return value


def _snapshot(raw):
    if not isinstance(raw, dict):
        return {"text": "", "elements": []}
    elements = []
    for item in raw.get("elements", [])[:180]:
        if not isinstance(item, dict):
            continue
        elements.append({
            "tag": _safe_text(item.get("tag"), 30), "role": _safe_text(item.get("role"), 40),
            "text": _safe_text(item.get("text"), 300), "label": _safe_text(item.get("label"), 200),
            "class_name": _safe_text(item.get("class_name"), 200),
            "href": _safe_url(item.get("href")) if item.get("href") else None,
        })
    return {"text": _safe_text(raw.get("text")), "elements": elements}


def _evidence(platform, snapshot, url):
    """Require visible account identity and authenticated workspace evidence."""
    expected_host = urlparse(PLATFORM_URLS[platform]).hostname
    if urlparse(url).hostname != expected_host:
        return "needs_verification", None, "当前页面尚未回到对应平台后台，请完成登录后再次检查"
    text = snapshot["text"]
    if re.search(r"拖动.{0,12}(?:滑块|拼图)|完成.{0,10}验证|安全验证|扫码验证|请输入验证码|人机验证|验证身份", text):
        return "needs_verification", None, "请在浏览器完成平台验证后再次检查连接"
    labels = [item["text"] or item["label"] for item in snapshot["elements"] if item["tag"] in ("button", "a", "input") or item["role"] == "button"]
    login_control = any(re.fullmatch(r"(?:使用)?(?:立即|账号|帐号|手机|短信|扫码|微信|二维码|密码|验证码)?(?:登录|登陆)(?:/注册)?|登录/注册|手机号|手机号码|密码|验证码", label.strip()) for label in labels)
    if login_control or re.search(r"扫码(?:登录|登陆)|微信扫一扫.{0,40}(?:登录|登陆)|使用(?:账号|帐号)登录|请(?:使用|用).{0,12}(?:微信|小红书).{0,12}(?:扫码|扫描)|登录后(?:即可|可)|请先登录", text):
        return "login_required", None, "请在已打开的官方后台完成登录，再点击检查连接"

    workspace_labels = {
        "wechat": {"首页", "内容管理", "素材管理", "图文素材", "草稿箱", "新的创作", "发表记录", "用户管理", "数据统计"},
        "toutiao": {"首页", "主页", "内容管理", "作品管理", "草稿箱", "收益数据", "作品数据", "粉丝数据", "发布文章", "创作中心", "粉丝管理", "数据中心", "创作收益"},
        "xiaohongshu": {"创作中心", "发布笔记", "笔记管理", "数据中心", "账号管理", "创作服务"},
    }[platform]
    seen = set()
    account_candidates = []
    logout = False
    for item in snapshot["elements"]:
        name = item["text"].strip()
        if name in workspace_labels:
            seen.add(name)
        if name in {"退出登录", "退出登陆", "登出"}:
            logout = True
        class_name = item["class_name"].lower()
        account_class = re.search(r"nickname|user[-_]?name|username|name-box|account.{0,20}(?:name|info)|profile.{0,20}name", class_name)
        if platform == "toutiao" and re.search(r"(?:^|\s)(?:shead_name|auth-avator-name)(?:\s|$)", class_name):
            account_class = True
        if account_class and name and len(name) <= 80 and "\n" not in name:
            if name not in workspace_labels and not re.search(r"登录|注册|创作中心|公众号平台|小红书创作|头条号平台|账号管理|帐号管理", name):
                account_candidates.append(name)
    if account_candidates and len(seen) >= 2:
        return "connected", account_candidates[0], "已从当前后台页面确认登录账号；保存草稿能力仍待实际验证"
    if logout and len(seen) >= 2:
        return "needs_verification", None, "页面显示后台登录迹象，但尚未识别账号名称，请等待进一步核对"
    return "needs_verification", None, "尚未从当前页面取得充分登录证据，请完成登录后检查连接"


def _load_playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def _edge_executable():
    for environment in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(environment)
        if root:
            candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.is_file():
                return candidate.resolve()
    raise RuntimeError("Microsoft Edge unavailable")


def _powershell_json(script):
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("Cannot verify dedicated browser process")
    return json.loads(completed.stdout.strip()) if completed.stdout.strip() else None


def _process_info(pid):
    pid = int(pid)
    if pid <= 0:
        return None
    return _powershell_json(f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")


def _listener_info(port):
    port = int(port)
    value = _powershell_json(f"@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object LocalAddress,OwningProcess) | ConvertTo-Json -Compress")
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _command_line_args(value):
    if isinstance(value, list):
        return value
    if os.name != "nt":
        raise RuntimeError("Dedicated Edge bridge requires Windows")
    count = ctypes.c_int()
    parser = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
    result = parser(str(value or ""), ctypes.byref(count))
    if not result:
        return []
    try:
        return [result[index] for index in range(count.value)]
    finally:
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(result, ctypes.c_void_p))


def _flag(args, name):
    for index, argument in enumerate(args):
        if argument == name and index + 1 < len(args):
            return args[index + 1]
        if argument.startswith(name + "="):
            return argument[len(name) + 1:]
    return None


def _verified_process(pid, profile, executable, listeners):
    process = _process_info(pid)
    if not isinstance(process, dict) or int(process.get("ProcessId", 0)) != int(pid):
        return False
    if str(Path(process.get("ExecutablePath") or "").resolve()).casefold() != str(executable.resolve()).casefold():
        return False
    args = _command_line_args(process.get("CommandLine"))
    command_profile = _flag(args, "--user-data-dir")
    if not command_profile or Path(command_profile).resolve() != profile.resolve():
        return False
    if _flag(args, "--remote-debugging-port") != str(CDP_PORT):
        return False
    if _flag(args, "--remote-debugging-address") != "127.0.0.1":
        return False
    return bool(listeners) and all(int(item.get("OwningProcess", 0)) == int(pid) and item.get("LocalAddress") in ("127.0.0.1", "::1") for item in listeners)


def _existing_edge(settings):
    """Find a proven *running* project Edge, without creating files or processes."""
    runtime = Path(settings.runtime_dir).resolve()
    profile = runtime / "browser-profile"
    manifest = runtime / "browser-session.json"
    if not profile.is_dir() or profile.resolve() != profile or not manifest.is_file() or manifest.resolve() != manifest:
        return None
    try:
        if manifest.stat().st_size > 8192:
            return None
        record = json.loads(manifest.read_text("utf-8"))
        if not isinstance(record, dict) or record.get("port") != CDP_PORT:
            return None
        recorded_profile = record.get("profile")
        if not isinstance(recorded_profile, str) or not Path(recorded_profile).is_absolute() or Path(recorded_profile).resolve() != profile:
            return None
        pid = record.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        executable = _edge_executable()
        if record.get("executable") and Path(record["executable"]).resolve() != executable.resolve():
            return None
        listeners = _listener_info(CDP_PORT)
        if not _verified_process(pid, profile, executable, listeners):
            return None
        return f"http://127.0.0.1:{CDP_PORT}"
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        return None


def _ensure_edge(settings):
    """Launch or reuse only a proven project-profile Edge process on loopback."""
    runtime = Path(settings.runtime_dir).resolve()
    profile = runtime / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    if profile.resolve() != profile:
        raise RuntimeError("Project browser profile cannot point outside its own directory")
    executable = _edge_executable()
    manifest = runtime / "browser-session.json"
    listeners = _listener_info(CDP_PORT)
    previous = None
    if manifest.exists():
        try:
            previous = json.loads(manifest.read_text("utf-8"))
        except (ValueError, OSError):
            previous = None
    if isinstance(previous, dict) and previous.get("port") == CDP_PORT and previous.get("profile") == str(profile):
        if _verified_process(previous.get("pid", 0), profile, executable, listeners):
            return f"http://127.0.0.1:{CDP_PORT}"
    if listeners:
        raise RuntimeError("Dedicated browser port is owned by an unverified process")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(runtime), creationflags=flags, close_fds=True,
    )
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        # 先用端口判定。Edge 的 Startup Boost 预加载进程（--no-startup-window）
        # 会接管启动请求并 spawn 真正的 browser process，启动器随后以 0 退出；
        # 因此"启动器进程退出"属正常交棒，不能当作失败依据，否则会把已经成功
        # 启动的实例误判为失败（2026-10-15 实测：端口在 0.5 秒内即被监听）。
        listeners = _listener_info(CDP_PORT)
        if listeners:
            owners = {int(item.get("OwningProcess", 0)) for item in listeners}
            for owner in sorted(owners):
                if owner > 0 and _verified_process(owner, profile, executable, listeners):
                    # 记录实际持有端口的进程，而不是可能已经交棒退出的启动器
                    record = {"pid": owner, "port": CDP_PORT, "profile": str(profile), "executable": str(executable)}
                    temporary = manifest.with_name(manifest.name + "." + uuid.uuid4().hex + ".tmp")
                    temporary.write_text(json.dumps(record, ensure_ascii=False), "utf-8")
                    temporary.replace(manifest)
                    return f"http://127.0.0.1:{CDP_PORT}"
        code = process.poll()
        if code is not None and code != 0:
            raise RuntimeError("Dedicated Edge launch failed")
        time.sleep(0.2)
    raise RuntimeError("Dedicated Edge launch could not be verified")


class _BrowserSession:
    def __init__(self, settings, allow_launch=True):
        self.runtime_dir = Path(settings.runtime_dir).resolve()
        self.profile = self.runtime_dir / "browser-profile"
        endpoint = _ensure_edge(settings) if allow_launch else _existing_edge(settings)
        if endpoint is None:
            raise RuntimeError("No verified running project Edge session")
        self.manager = _load_playwright()
        self.playwright = self.manager.__enter__()
        self.context = None
        self.pages = {}
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(endpoint, timeout=20000)
            contexts = self.browser.contexts
            if not contexts:
                raise RuntimeError("Dedicated browser has no persistent context")
            self.context = contexts[0]
        except BaseException:
            self.manager.__exit__(None, None, None)
            raise

    def is_open(self):
        if self.context is None:
            return False
        browser = self.browser
        return browser is not None and browser.is_connected()

    def open(self, platform):
        page = self.pages.get(platform)
        if page is None or page.is_closed():
            host = urlparse(PLATFORM_URLS[platform]).hostname
            existing = next((candidate for candidate in reversed(self.context.pages) if not candidate.is_closed() and urlparse(candidate.url).hostname == host), None)
            blank = next((candidate for candidate in self.context.pages if not candidate.is_closed() and candidate.url in ("about:blank", "")), None)
            page = existing if existing is not None else blank if blank is not None else self.context.new_page()
            self.pages[platform] = page
            if existing is None:
                page.goto(PLATFORM_URLS[platform], wait_until="domcontentloaded", timeout=30000)
        elif urlparse(page.url).hostname != urlparse(PLATFORM_URLS[platform]).hostname:
            page.goto(PLATFORM_URLS[platform], wait_until="domcontentloaded", timeout=30000)
        inspected = self.inspect(platform)
        adopted = self.pages.get(platform)
        if adopted is not None and not adopted.is_closed():
            adopted.bring_to_front()
        return inspected

    def inspect(self, platform, screenshot=False):
        mapped = self.pages.get(platform)
        if not self.is_open():
            status = _set(platform, state="disconnected", account_label=None, browser_open=False, message="对应后台页面已关闭，请重新打开", last_checked=_now())
            return {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}}
        host = urlparse(PLATFORM_URLS[platform]).hostname
        candidates = [page for page in reversed(self.context.pages) if not page.is_closed() and urlparse(page.url).hostname == host]
        if mapped is not None and not mapped.is_closed() and mapped not in candidates:
            candidates.append(mapped)
        if not candidates:
            status = _set(platform, state="disconnected", account_label=None, browser_open=True, message="对应后台页面已关闭，请重新打开", last_checked=_now())
            return {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}}
        # Editor tabs often omit account identity. Keep recent tabs first so a
        # new login/challenge still blocks; reserve room for existing dashboards
        # and the mapped page without navigating, refreshing, or opening a tab.
        inspected_candidates = list(candidates[:3])
        home_pages = [page for page in candidates if urlparse(page.url).hostname == host and urlparse(page.url).path.rstrip("/") == _HOME_PATHS[platform]][:2]
        for candidate in home_pages + [mapped] + candidates:
            if candidate is None or candidate not in candidates or candidate in inspected_candidates:
                continue
            if len(inspected_candidates) >= 6:
                break
            inspected_candidates.append(candidate)
        selected = None
        for candidate in inspected_candidates:
            sanitized = _safe_url(candidate.url)
            try:
                raw = candidate.evaluate(_DOM_SNAPSHOT)
            except Exception:
                # Login redirects can destroy the old JS context between observations.
                try:
                    candidate.wait_for_load_state("domcontentloaded", timeout=5000)
                    raw = candidate.evaluate(_DOM_SNAPSHOT)
                    sanitized = _safe_url(candidate.url)
                except Exception:
                    continue
            observed = _snapshot(raw)
            evidence = _evidence(platform, observed, sanitized)
            if selected is None or evidence[0] == "connected":
                selected = candidate, sanitized, observed, evidence
            if evidence[0] == "connected":
                break
            if candidate is candidates[0] and (evidence[0] == "login_required" or evidence[2] == "请在浏览器完成平台验证后再次检查连接"):
                # A new login/challenge supersedes an older dashboard; never bypass it.
                break
        if selected is None:
            status = _set(platform, state="needs_verification", account_label=None, browser_open=True, message="后台页面仍在跳转，请稍后检查连接", last_checked=_now())
            return {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}}
        page, sanitized_url, snapshot, evidence = selected
        self.pages[platform] = page
        state, account, message = evidence
        status = _set(platform, state=state, account_label=account, message=message, browser_open=True, last_checked=_now())
        result = {"platform": platform, "status": status, "url": sanitized_url, "snapshot": snapshot}
        # Login QR codes and verification challenges are credentials, so never persist them.
        if screenshot and state == "connected":
            directory = self.runtime_dir / "browser-qa"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (platform + "-" + uuid.uuid4().hex + ".png")
            try:
                page.screenshot(path=str(path), full_page=False, timeout=10000)
                result["screenshot_path"] = str(path)
            except Exception:
                result["screenshot_message"] = "页面观察已完成，截图未保存"
        return result

    def close(self):
        # Edge is an independent project process. Stop the driver connection only.
        # Neither Browser.close nor Context.close is called, so login tabs survive.
        self.manager.__exit__(None, None, None)
        self.context = None
        self.pages.clear()

    def _confirm_run_account(self, platform, inspected):
        """A run may confirm cookies via a new official home, never an old editor.

        check/inspect/get_status never call this. An explicit login/challenge
        cannot be bypassed by creating another homepage.
        """
        status = inspected["status"]
        if status["state"] == "connected" or not status["browser_open"]:
            return inspected
        observed_state, _, observed_message = _evidence(platform, inspected["snapshot"], PLATFORM_URLS[platform])
        if status["state"] == "login_required" or observed_state == "login_required" or observed_message == "请在浏览器完成平台验证后再次检查连接":
            return inspected
        if status["state"] not in ("disconnected", "needs_verification"):
            return inspected
        if status["state"] == "needs_verification" and not inspected.get("url"):
            # A destroyed/loading JS context proves neither absence of a login
            # challenge nor an accountless editor; do not replace that flow.
            return inspected
        home = self.context.new_page()
        self.pages[platform] = home
        home.goto(_RUN_HOME_URLS[platform], wait_until="domcontentloaded", timeout=30000)
        deadline = time.monotonic() + 8
        while True:
            sanitized = _safe_url(home.url)
            try:
                raw = home.evaluate(_DOM_SNAPSHOT)
            except Exception:
                home.wait_for_load_state("domcontentloaded", timeout=5000)
                raw = home.evaluate(_DOM_SNAPSHOT)
                sanitized = _safe_url(home.url)
            snapshot = _snapshot(raw)
            state, account, message = _evidence(platform, snapshot, sanitized)
            parsed = urlparse(home.url)
            official = (parsed.scheme == "https" and parsed.hostname == urlparse(PLATFORM_URLS[platform]).hostname
                        and not parsed.username and not parsed.password and parsed.port in (None, 443))
            if not official:
                state, account, message = "needs_verification", None, "官方首页尚未确认登录，请完成当前跳转或验证后检查连接"
            status = _set(platform, state=state, account_label=account, browser_open=True, message=message, last_checked=_now())
            result = {"platform": platform, "status": status, "url": sanitized, "snapshot": snapshot}
            # Only this new home may confirm the account. Do not accept an older
            # dashboard while this page redirects to login or verification.
            blocked_state, _, blocked_message = _evidence(platform, snapshot, PLATFORM_URLS[platform])
            if (state == "connected" or blocked_state == "login_required"
                    or blocked_message == "请在浏览器完成平台验证后再次检查连接"
                    or not official
                    or time.monotonic() >= deadline):
                return result
            time.sleep(0.2)

    def run(self, platform, callback):
        try:
            inspected = self.inspect(platform)
            inspected = self._confirm_run_account(platform, inspected)
        except BaseException:
            _set(platform, state="error", account_label=None, draft_capability_verified=False, last_checked=_now(), message="无法确认当前后台登录状态，请检查连接后重试")
            return {"state": "needs_connection", "account_label": None, "error": "无法确认当前官方后台账号，未执行页面操作；请检查连接后重试"}
        status = inspected["status"]
        account = status["account_label"]
        if status["state"] != "connected" or not account:
            return {"state": "needs_connection", "account_label": account, "error": "请先在官方后台完成登录并确认账号，再保存草稿"}
        try:
            result = callback(self.pages[platform], account)
            if not isinstance(result, dict):
                return {"state": "unknown", "account_label": account, "error": "后台操作没有返回可确认结果，请检查平台草稿箱"}
            result = dict(result)
            result.setdefault("account_label", account)
            if result.get("error"):
                result["error"] = _safe_text(result["error"], 2000)
            for field in ("remote_url", "url"):
                if result.get(field):
                    result[field] = _safe_url(result[field]) or None
            return result
        except BaseException:
            return {"state": "unknown", "account_label": account, "error": "后台操作发生中断，保存结果待核对；请先检查平台草稿箱，避免重复提交"}


def _run(commands):
    browser = None
    try:
        while True:
            operation, platform, settings, options, future = commands.get()
            if not future.set_running_or_notify_cancel():
                continue
            if operation == "stop":
                try:
                    if browser is not None:
                        browser.close()
                    future.set_result(None)
                except BaseException:
                    future.set_result(None)
                break
            try:
                entered_ui = False
                if operation == "open":
                    if browser is not None and not browser.is_open():
                        browser.close()
                        browser = None
                    if browser is None:
                        browser = _BrowserSession(settings)
                    if browser.runtime_dir != Path(settings.runtime_dir).resolve():
                        raise RuntimeError("different workbench runtime")
                    result = browser.open(platform)["status"]
                else:
                    if browser is not None and browser.runtime_dir != Path(settings.runtime_dir).resolve():
                        raise RuntimeError("different workbench runtime")
                    if browser is not None and not browser.is_open():
                        browser.close()
                        browser = None
                    if browser is None:
                        # This attaches only after checking the existing project
                        # manifest/profile/process/listener. It never launches,
                        # opens, navigates or changes an official platform tab.
                        browser = _BrowserSession(settings, allow_launch=False)
                    if operation == "run":
                        entered_ui = True
                        result = browser.run(platform, options["callback"])
                    else:
                        inspected = browser.inspect(platform, screenshot=options.get("capture_screenshot", False))
                        result = inspected if operation == "inspect" else inspected["status"]
                future.set_result(result)
            except BaseException:
                if operation == "run" and entered_ui:
                    future.set_result({"state": "unknown", "account_label": _cached(platform)["account_label"], "error": "后台操作结果未确认，请先检查平台草稿箱，避免重复提交"})
                    continue
                if operation == "run":
                    future.set_result(_not_running_result(operation, platform))
                    continue
                if operation != "open" and (browser is None or browser.runtime_dir != Path(settings.runtime_dir).resolve()):
                    future.set_result(_not_running_result(operation, platform))
                    continue
                opened = False
                try:
                    opened = browser is not None and browser.is_open()
                except BaseException:
                    pass
                status = _set(platform, state="error", account_label=None, browser_open=opened, last_checked=_now(), message="无法完成后台页面操作，请检查 Edge 浏览器和网络后重试")
                result = {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}} if operation == "inspect" else status
                future.set_result(result)
    finally:
        for platform in PLATFORM_URLS:
            _set(platform, state="disconnected", account_label=None, browser_open=False, message="浏览器连接已关闭", last_checked=_now())


def _not_running_result(operation, platform):
    status = _set(platform, state="disconnected", account_label=None, browser_open=False,
                  message="未找到可恢复的专用 Edge 会话，请在设置打开官方后台并确认登录", last_checked=_now())
    if operation == "inspect":
        return {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}}
    if operation == "run":
        return {"state": "needs_connection", "account_label": None, "error": status["message"] + "；尚未执行页面操作"}
    return status


def _submit(operation, platform, settings=None, wait_seconds=45, **options):
    global _commands, _thread
    from . import runtime_mode
    runtime_mode.require_platform_operations("平台浏览器操作")
    platform = _platform(platform)
    with _lifecycle_lock:
        if _thread is None or not _thread.is_alive():
            _commands = Queue()
            _thread = threading.Thread(target=_run, args=(_commands,), name="workbench-platform-browser", daemon=True)
            _thread.start()
        future = Future()
        _commands.put((operation, platform, settings or get_settings(), options, future))
    try:
        return future.result(timeout=wait_seconds)
    except FutureTimeout:
        if operation == "run":
            if future.cancel():
                return {"state": "failed", "account_label": _cached(platform)["account_label"], "error": "后台操作排队超时，任务已取消，尚未执行任何页面操作"}
            return {"state": "unknown", "account_label": _cached(platform)["account_label"], "error": "后台操作等待超时，保存结果待核对；请先检查平台草稿箱，避免重复提交"}
        status = _set(platform, state="needs_verification", message="浏览器操作仍在处理，请稍后检查连接", last_checked=_now())
        return {"platform": platform, "status": status, "url": "", "snapshot": {"text": "", "elements": []}} if operation == "inspect" else status


def open_login(platform, settings=None):
    _platform(platform)
    if _cached(platform)["state"] != "connected":
        _set(platform, state="login_required", account_label=None, message="正在打开官方后台，请在浏览器完成登录", last_checked=_now())
    return _submit("open", platform, settings)


def check_connection(platform, settings=None):
    return _submit("check", platform, settings)


def inspect_platform(platform, settings=None, capture_screenshot=False):
    return _submit("inspect", platform, settings, capture_screenshot=capture_screenshot)


def run_in_page(platform, callback, settings=None, timeout_seconds=90):
    """Run trusted UI-only work in the existing session after a live account check."""
    if not callable(callback):
        raise ValueError("页面操作必须可调用")
    if not 0 < timeout_seconds <= 300:
        raise ValueError("页面操作等待时间需在 0 到 300 秒之间")
    return _submit("run", platform, settings, wait_seconds=timeout_seconds, callback=callback)


def mark_draft_verified(platform, account_label, settings=None):
    """Called by the draft adapter only after visible remote save confirmation."""
    platform = _platform(platform)
    with _lock:
        status = _status[platform]
        status["draft_capability_verified"] = bool(status["state"] == "connected" and account_label and status["account_label"] == account_label)
        return dict(status)


def stop_browser():
    global _commands, _thread
    with _lifecycle_lock:
        if _thread is None or not _thread.is_alive():
            _commands, _thread = None, None
            for platform in PLATFORM_URLS:
                _set(platform, state="disconnected", account_label=None, browser_open=False, message="浏览器连接已关闭")
            return
        future = Future()
        _commands.put(("stop", None, None, {}, future))
        try:
            future.result(timeout=40)
        except FutureTimeout:
            return
        _thread.join(timeout=3)
        if not _thread.is_alive():
            _commands, _thread = None, None
