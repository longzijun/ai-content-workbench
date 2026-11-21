"""Dedicated-thread browser connection tests; no real browser or authentication."""
from pathlib import Path
import json
import threading

import pytest

from backend.app import browser_bridge as bridge
from backend.app.config import Settings


def element(text, class_name="", tag="button", **extra):
    return {"tag": tag, "role": "", "text": text, "label": "", "class_name": class_name, "href": None, **extra}


def logged_in(platform):
    labels = {"wechat": ["首页", "内容管理"], "toutiao": ["作品管理", "数据中心"], "xiaohongshu": ["发布笔记", "笔记管理"]}[platform]
    return {"text": "测试账号 " + " ".join(labels), "elements": [element("测试账号", "user-name", "span"), *[element(label, tag="a") for label in labels]]}


class FakePage:
    def __init__(self, owner):
        self.owner = owner
        self.url = "about:blank"
        self.closed = False
        self.evaluations = 0
        self.snapshot = {"text": "扫码登录", "elements": [element("登录")]}

    def is_closed(self):
        self.owner.record("is_closed")
        return self.closed

    def goto(self, url, **kwargs):
        self.owner.record("goto")
        self.url = url

    def bring_to_front(self):
        self.owner.record("bring_to_front")

    def wait_for_load_state(self, state, **kwargs):
        self.owner.record("wait_for_load_state")

    def evaluate(self, script):
        self.owner.record("evaluate")
        self.evaluations += 1
        assert "input" in script
        assert ".value" not in script
        assert "cookie" not in script
        return self.snapshot

    def screenshot(self, path, **kwargs):
        self.owner.record("screenshot")
        Path(path).write_bytes(b"fake QA screenshot")


class FakeBrowser:
    def __init__(self, owner):
        self.owner = owner

    def is_connected(self):
        self.owner.record("is_connected")
        return not self.owner.closed and self.owner.driver_connected

    @property
    def contexts(self):
        return [self.owner.context]


class FakeContext:
    def __init__(self, owner):
        self.owner = owner
        self.pages = [FakePage(owner)]
        self.browser = FakeBrowser(owner)

    def new_page(self):
        self.owner.record("new_page")
        page = FakePage(self.owner)
        self.pages.append(page)
        return page

    def close(self):
        self.owner.record("context_close")
        self.owner.closed = True
        for page in self.pages:
            page.closed = True


class FakePlaywright:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.started = 0
        self.launch_options = None
        self.connect_options = None
        self.driver_connected = False
        self.context = None
        self.chromium = self
        self.launch_error = None

    def record(self, operation):
        self.calls.append((operation, threading.get_ident()))

    def __enter__(self):
        self.record("playwright_enter")
        self.started += 1
        self.driver_connected = True
        return self

    def __exit__(self, *args):
        self.record("playwright_exit")
        self.driver_connected = False

    def connect_over_cdp(self, endpoint, **kwargs):
        self.record("connect_over_cdp")
        if self.launch_error:
            raise self.launch_error
        self.connect_options = {"endpoint": endpoint, **kwargs}
        if self.context is None:
            self.context = FakeContext(self)
        return self.context.browser


@pytest.fixture
def browser(tmp_path, monkeypatch):
    bridge.stop_browser()
    fake = FakePlaywright()
    monkeypatch.setattr(bridge, "_load_playwright", lambda: fake)

    def ensure_edge(settings):
        fake.record("ensure_edge")
        fake.launch_options = {"profile": str(settings.runtime_dir / "browser-profile")}
        return f"http://127.0.0.1:{bridge.CDP_PORT}"

    monkeypatch.setattr(bridge, "_ensure_edge", ensure_edge)
    settings = Settings(runtime_dir=tmp_path / "runtime", vault_path=str(tmp_path / "vault"), codex_path="")
    yield fake, settings
    bridge.stop_browser()


def test_status_check_and_inspection_do_not_start_browser(browser):
    fake, settings = browser
    cached = bridge.get_status(settings)
    assert set(cached) == {"wechat", "toutiao", "xiaohongshu"}
    assert all(not item["browser_open"] and item["state"] == "disconnected" for item in cached.values())
    assert bridge.check_connection("wechat", settings)["state"] == "disconnected"
    assert bridge.inspect_platform("wechat", settings)["snapshot"] == {"text": "", "elements": []}
    assert fake.started == 0
    assert not (settings.runtime_dir / "browser-profile").exists()


def test_explicit_login_uses_project_edge_profile_and_reuses_platform_tab(browser):
    fake, settings = browser
    first = bridge.open_login("wechat", settings)
    assert first["state"] == "login_required" and first["browser_open"]
    assert fake.connect_options["endpoint"] == f"http://127.0.0.1:{bridge.CDP_PORT}"
    assert Path(fake.launch_options["profile"]) == settings.runtime_dir / "browser-profile"
    assert fake.context.pages[0].url == bridge.PLATFORM_URLS["wechat"]
    bridge.open_login("wechat", settings)
    assert len(fake.context.pages) == 1
    bridge.open_login("toutiao", settings)
    bridge.open_login("xiaohongshu", settings)
    assert len(fake.context.pages) == 3
    assert {page.url for page in fake.context.pages} == set(bridge.PLATFORM_URLS.values())
    assert fake.started == 1


@pytest.mark.parametrize("platform", list(bridge.PLATFORM_URLS))
def test_only_visible_account_and_authenticated_workspace_confirm_login(browser, platform):
    fake, settings = browser
    bridge.open_login(platform, settings)
    page = fake.context.pages[0]
    page.snapshot = {"text": "普通介绍页面", "elements": [element("测试账号", "user-name", "span")]}
    assert bridge.check_connection(platform, settings)["state"] == "needs_verification"
    page.snapshot = logged_in(platform)
    connected = bridge.check_connection(platform, settings)
    assert connected["state"] == "connected"
    assert connected["account_label"] == "测试账号"
    assert connected["draft_capability_verified"] is False
    assert bridge.get_status(settings)[platform] == connected


@pytest.mark.parametrize("challenge", ["安全验证", "请拖动滑块完成验证", "请输入验证码"])
def test_platform_verification_is_never_treated_as_connected(browser, challenge):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    snapshot = logged_in("wechat")
    snapshot["text"] += " " + challenge
    fake.context.pages[0].snapshot = snapshot
    result = bridge.check_connection("wechat", settings)
    assert result["state"] == "needs_verification"
    assert result["account_label"] is None


def test_visible_login_control_overrides_workspace_like_marketing_content(browser):
    fake, settings = browser
    bridge.open_login("xiaohongshu", settings)
    snapshot = logged_in("xiaohongshu")
    snapshot["elements"].append(element("立即登录"))
    fake.context.pages[0].snapshot = snapshot
    assert bridge.check_connection("xiaohongshu", settings)["state"] == "login_required"


def test_inspection_strips_query_tokens_and_never_captures_login_qr(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    page = fake.context.pages[0]
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?token=secret-value#private-fragment"
    page.snapshot = {"text": "扫码登录 https://mp.weixin.qq.com/?token=secret-value access_token=other-secret", "elements": [element("登录", href="https://mp.weixin.qq.com/path?token=secret-value")]}
    inspected = bridge.inspect_platform("wechat", settings, capture_screenshot=True)
    assert inspected["url"] == "https://mp.weixin.qq.com/cgi-bin/home"
    assert "secret-value" not in str(inspected)
    assert "other-secret" not in str(inspected)
    assert "private-fragment" not in str(inspected)
    assert "screenshot_path" not in inspected
    assert not any(operation == "screenshot" for operation, _ in fake.calls)


def test_connected_inspection_can_save_runtime_qa_and_all_operations_share_thread(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    fake.context.pages[0].snapshot = logged_in("toutiao")
    inspected = bridge.inspect_platform("toutiao", settings, capture_screenshot=True)
    path = Path(inspected["screenshot_path"])
    assert path.is_relative_to(settings.runtime_dir / "browser-qa") and path.exists()
    bridge.stop_browser()
    assert not fake.closed
    assert not any(operation == "context_close" for operation, _ in fake.calls)
    thread_ids = {thread_id for _, thread_id in fake.calls}
    assert len(thread_ids) == 1
    assert threading.get_ident() not in thread_ids
    assert all(not item["browser_open"] for item in bridge.get_status(settings).values())


def test_browser_errors_do_not_expose_raw_credential_bearing_messages(browser):
    fake, settings = browser
    fake.launch_error = RuntimeError("failed https://mp.weixin.qq.com/?token=private-secret")
    result = bridge.open_login("wechat", settings)
    assert result["state"] == "error"
    assert "private-secret" not in str(result)
    assert result["browser_open"] is False
    assert any(operation == "playwright_exit" for operation, _ in fake.calls)


def test_unsupported_platform_cannot_open_an_arbitrary_site(browser):
    fake, settings = browser
    with pytest.raises(ValueError, match="不支持"):
        bridge.open_login("https://untrusted.example/", settings)
    assert fake.started == 0


def test_wechat_observed_scan_login_text_is_classified_as_login_required(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    fake.context.pages[0].snapshot = {"text": "使用账号登录 微信扫一扫，选择公众平台账号登录", "elements": [element("使用账号登录", tag="a")]}
    assert bridge.check_connection("wechat", settings)["state"] == "login_required"


def test_run_requires_existing_browser_and_live_connected_account(browser):
    fake, settings = browser
    callbacks = []

    def operation(page, account):
        callbacks.append(account)
        return {"state": "synced", "remote_id": "remote"}

    assert bridge.run_in_page("wechat", operation, settings)["state"] == "needs_connection"
    assert fake.started == 0
    bridge.open_login("wechat", settings)
    assert bridge.run_in_page("wechat", operation, settings)["state"] == "needs_connection"
    assert callbacks == []
    fake.context.pages[0].snapshot = logged_in("wechat")
    result = bridge.run_in_page("wechat", operation, settings)
    assert result["state"] == "synced"
    assert result["account_label"] == "测试账号"
    assert callbacks == ["测试账号"]


def test_run_callback_executes_on_browser_thread_and_redacts_error_query(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    fake.context.pages[0].snapshot = logged_in("toutiao")
    callback_threads = []

    def operation(page, account):
        callback_threads.append(threading.get_ident())
        page.bring_to_front()
        return {"state": "unknown", "error": "see https://mp.toutiao.com/path?token=secret-fragment", "remote_url": "https://mp.toutiao.com/path?token=secret-fragment"}

    result = bridge.run_in_page("toutiao", operation, settings)
    assert result["state"] == "unknown"
    assert "secret-fragment" not in str(result)
    assert set(callback_threads) == {thread_id for _, thread_id in fake.calls}
    assert threading.get_ident() not in callback_threads


def test_run_callback_exception_is_unknown_and_does_not_destroy_connected_session(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    fake.context.pages[0].snapshot = logged_in("wechat")

    def interrupted(page, account):
        raise RuntimeError("save interrupted https://mp.weixin.qq.com/?token=private-value")

    result = bridge.run_in_page("wechat", interrupted, settings)
    assert result["state"] == "unknown"
    assert "private-value" not in str(result)
    assert bridge.get_status(settings)["wechat"]["state"] == "connected"


def test_running_timeout_is_unknown_and_queued_timeout_cancels_late_ui_work(browser):
    fake, settings = browser
    bridge.open_login("xiaohongshu", settings)
    fake.context.pages[0].snapshot = logged_in("xiaohongshu")
    started = threading.Event()
    release = threading.Event()
    queued_calls = []

    def running(page, account):
        started.set()
        release.wait(timeout=3)
        return {"state": "synced", "remote_id": "first"}

    def queued(page, account):
        queued_calls.append(True)
        return {"state": "synced", "remote_id": "should-not-exist"}

    try:
        result = bridge.run_in_page("xiaohongshu", running, settings, timeout_seconds=0.05)
        assert started.is_set()
        assert result["state"] == "unknown"
        cancelled = bridge.run_in_page("xiaohongshu", queued, settings, timeout_seconds=0.05)
        assert cancelled["state"] == "failed"
    finally:
        release.set()
    assert bridge.check_connection("xiaohongshu", settings)["state"] == "connected"
    assert queued_calls == []


def test_draft_capability_persists_for_same_live_account_and_resets_on_account_change(browser):
    fake, settings = browser
    bridge.open_login("xiaohongshu", settings)
    page = fake.context.pages[0]
    page.snapshot = logged_in("xiaohongshu")
    bridge.check_connection("xiaohongshu", settings)
    marked = bridge.mark_draft_verified("xiaohongshu", "测试账号", settings)
    assert marked["draft_capability_verified"] is True
    assert bridge.check_connection("xiaohongshu", settings)["draft_capability_verified"] is True
    assert bridge.inspect_platform("xiaohongshu", settings)["status"]["draft_capability_verified"] is True
    assert bridge.open_login("xiaohongshu", settings)["draft_capability_verified"] is True
    page.snapshot["elements"][0]["text"] = "另一个账号"
    changed = bridge.check_connection("xiaohongshu", settings)
    assert changed["account_label"] == "另一个账号"
    assert changed["draft_capability_verified"] is False
    bridge.mark_draft_verified("xiaohongshu", "另一个账号", settings)
    page.snapshot = {"text": "扫码登录", "elements": [element("登录")]}
    assert bridge.check_connection("xiaohongshu", settings)["draft_capability_verified"] is False


def test_visible_sidebar_leaf_text_is_available_for_login_evidence(browser):
    fake, settings = browser
    assert ",div,span" in bridge._DOM_SNAPSHOT
    assert "el.childElementCount" in bridge._DOM_SNAPSHOT
    bridge.open_login("xiaohongshu", settings)
    fake.context.pages[0].snapshot = {"text": "首页 发布笔记 笔记管理", "elements": [element("测试账号", "account-name", "span"), element("发布笔记", tag="span"), element("笔记管理", tag="div")]}
    assert bridge.check_connection("xiaohongshu", settings)["state"] == "connected"


@pytest.mark.parametrize("closed_initial", [False, True])
def test_authenticated_official_popup_is_adopted_after_login(browser, closed_initial):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    original = fake.context.pages[0]
    original.closed = closed_initial
    popup = fake.context.new_page()
    popup.url = "https://mp.weixin.qq.com/cgi-bin/home?token=never-return"
    popup.snapshot = logged_in("wechat")
    checked = bridge.check_connection("wechat", settings)
    assert checked["state"] == "connected"
    received_pages = []

    def operation(page, account):
        received_pages.append(page)
        return {"state": "synced", "remote_id": "popup-draft"}

    assert bridge.run_in_page("wechat", operation, settings)["state"] == "synced"
    assert received_pages == [popup]
    inspected = bridge.inspect_platform("wechat", settings)
    assert inspected["url"] == "https://mp.weixin.qq.com/cgi-bin/home"


def test_new_verification_popup_cannot_be_bypassed_using_old_connected_tab(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    fake.context.pages[0].snapshot = logged_in("wechat")
    assert bridge.check_connection("wechat", settings)["state"] == "connected"
    popup = fake.context.new_page()
    popup.url = "https://mp.weixin.qq.com/verify"
    popup.snapshot = {"text": "请完成安全验证", "elements": []}
    callbacks = []
    result = bridge.run_in_page("wechat", lambda page, account: callbacks.append(True), settings)
    assert result["state"] == "needs_connection"
    assert bridge.get_status(settings)["wechat"]["state"] == "needs_verification"
    assert callbacks == []


def test_navigation_context_replacement_is_retried_without_raw_error(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    page = fake.context.pages[0]
    page.snapshot = logged_in("wechat")
    original = page.evaluate
    attempts = []

    def navigating(script):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("execution context destroyed token=secret")
        return original(script)

    page.evaluate = navigating
    result = bridge.check_connection("wechat", settings)
    assert result["state"] == "connected"
    assert len(attempts) == 2
    assert any(operation == "wait_for_load_state" for operation, _ in fake.calls)
    assert "secret" not in str(result)


def test_actual_toutiao_shead_name_identity_is_platform_specific(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    fake.context.pages[0].snapshot = {"text": "内容管理 数据中心", "elements": [element("测试账号", "shead_name", "div"), element("内容管理", tag="span"), element("数据中心", tag="span")]}
    assert bridge.check_connection("toutiao", settings)["state"] == "connected"
    other = dict(fake.context.pages[0].snapshot)
    assert bridge._evidence("wechat", bridge._snapshot(other), "https://mp.weixin.qq.com/")[0] != "connected"


def test_actual_toutiao_auth_avator_name_and_observed_workspace_labels(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    fake.context.pages[0].snapshot = {"text": "主页 文章 微头条 作品管理 草稿箱 收益数据", "elements": [element("测试账号", "auth-avator-name", "div"), element("草稿箱", tag="span"), element("收益数据", tag="span")]}
    assert bridge.check_connection("toutiao", settings)["state"] == "connected"


def test_bridge_restart_reconnects_existing_project_tabs_without_closing_edge(browser):
    fake, settings = browser
    bridge.open_login("wechat", settings)
    page = fake.context.pages[0]
    page.snapshot = logged_in("wechat")
    assert bridge.check_connection("wechat", settings)["state"] == "connected"
    goto_before = len([operation for operation, _ in fake.calls if operation == "goto"])
    bridge.stop_browser()
    assert not fake.closed and not page.closed
    reopened = bridge.open_login("wechat", settings)
    assert reopened["state"] == "connected"
    assert len(fake.context.pages) == 1
    assert len([operation for operation, _ in fake.calls if operation == "goto"]) == goto_before
    assert len([operation for operation, _ in fake.calls if operation == "connect_over_cdp"]) == 2


@pytest.mark.parametrize("invalid", ["normal_profile", "other_executable", "wrong_port", "public_listener", "foreign_pid"])
def test_cdp_process_verification_rejects_normal_or_unrelated_browsers(tmp_path, monkeypatch, invalid):
    profile = tmp_path / "runtime" / "browser-profile"
    executable = tmp_path / "Edge" / "msedge.exe"
    info = {"ProcessId": 456, "ExecutablePath": str(executable), "CommandLine": [str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={bridge.CDP_PORT}", "--remote-debugging-address=127.0.0.1"]}
    listeners = [{"OwningProcess": 456, "LocalAddress": "127.0.0.1"}]
    if invalid == "normal_profile":
        info["CommandLine"][1] = f"--user-data-dir={tmp_path / 'normal-profile'}"
    elif invalid == "other_executable":
        info["ExecutablePath"] = str(tmp_path / "other.exe")
    elif invalid == "wrong_port":
        info["CommandLine"][2] = "--remote-debugging-port=9222"
    elif invalid == "public_listener":
        listeners[0]["LocalAddress"] = "0.0.0.0"
    else:
        listeners[0]["OwningProcess"] = 999
    monkeypatch.setattr(bridge, "_process_info", lambda pid: info)
    assert bridge._verified_process(456, profile, executable, listeners) is False


def test_cdp_reuse_requires_matching_project_manifest_and_verified_listener(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = runtime / "browser-profile"
    executable = tmp_path / "msedge.exe"
    settings = Settings(runtime_dir=runtime)
    (runtime / "browser-session.json").write_text(json.dumps({"pid": 456, "port": bridge.CDP_PORT, "profile": str(profile)}), encoding="utf-8")
    info = {"ProcessId": 456, "ExecutablePath": str(executable), "CommandLine": [str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={bridge.CDP_PORT}", "--remote-debugging-address=127.0.0.1"]}
    monkeypatch.setattr(bridge, "_edge_executable", lambda: executable)
    monkeypatch.setattr(bridge, "_listener_info", lambda port: [{"OwningProcess": 456, "LocalAddress": "127.0.0.1"}])
    monkeypatch.setattr(bridge, "_process_info", lambda pid: info)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Existing project Edge must not be spawned again"))
    assert bridge._ensure_edge(settings) == f"http://127.0.0.1:{bridge.CDP_PORT}"


def test_cdp_port_occupied_without_project_manifest_is_never_attached(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_edge_executable", lambda: tmp_path / "msedge.exe")
    monkeypatch.setattr(bridge, "_listener_info", lambda port: [{"OwningProcess": 999, "LocalAddress": "127.0.0.1"}])
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Cannot launch on a foreign occupied port"))
    with pytest.raises(RuntimeError, match="unverified"):
        bridge._ensure_edge(Settings(runtime_dir=tmp_path / "runtime"))


def test_launcher_handoff_to_preloaded_edge_is_not_a_failure(tmp_path, monkeypatch):
    """回归 2026-10-15：Edge 的 Startup Boost 预加载进程（--no-startup-window）
    会接管启动请求并 spawn 真正的 browser process，启动器随即以 0 退出。

    实测该场景下端口在 0.5 秒内已被监听、浏览器完全可用；旧实现一看到启动器
    退出就抛 "Dedicated Edge launch exited"，把成功启动误判为失败。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = runtime / "browser-profile"
    executable = tmp_path / "msedge.exe"
    settings = Settings(runtime_dir=runtime)

    launcher_pid, owner_pid = 111, 222
    state = {"probes": 0}

    def listeners(port):
        state["probes"] += 1
        # 第一次是启动前的占用检查（此时端口空闲），之后端口已被真正的主进程接管
        if state["probes"] == 1:
            return []
        return [{"OwningProcess": owner_pid, "LocalAddress": "127.0.0.1"}]

    class Launcher:
        pid = launcher_pid

        def poll(self):
            return 0  # 已交棒退出，这是正常行为

    monkeypatch.setattr(bridge, "_edge_executable", lambda: executable)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: Launcher())
    monkeypatch.setattr(bridge, "_listener_info", listeners)
    monkeypatch.setattr(bridge, "_process_info", lambda pid: {
        "ProcessId": pid, "ExecutablePath": str(executable),
        "CommandLine": [str(executable), f"--user-data-dir={profile}",
                        f"--remote-debugging-port={bridge.CDP_PORT}",
                        "--remote-debugging-address=127.0.0.1"],
    })

    assert bridge._ensure_edge(settings) == f"http://127.0.0.1:{bridge.CDP_PORT}"
    record = json.loads((runtime / "browser-session.json").read_text("utf-8"))
    # 必须记录实际持有端口的进程，而不是已退出的启动器，否则重启后无法复用
    assert record["pid"] == owner_pid


def test_launcher_nonzero_exit_is_still_reported_as_failure(tmp_path, monkeypatch):
    """交棒退出（0）不算失败，但真正的非零退出仍必须判定失败。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "browser-profile").mkdir()
    settings = Settings(runtime_dir=runtime)
    state = {"probes": 0}

    def listeners(port):
        state["probes"] += 1
        return []

    class Launcher:
        pid = 111

        def poll(self):
            return 1

    monkeypatch.setattr(bridge, "_edge_executable", lambda: tmp_path / "msedge.exe")
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: Launcher())
    monkeypatch.setattr(bridge, "_listener_info", listeners)
    with pytest.raises(RuntimeError, match="failed"):
        bridge._ensure_edge(settings)


def add_accountless_editors(fake, platform, count=10):
    host = {"wechat": "mp.weixin.qq.com", "toutiao": "mp.toutiao.com", "xiaohongshu": "creator.xiaohongshu.com"}[platform]
    for index in range(count):
        editor = fake.context.new_page()
        editor.url = f"https://{host}/editor/{index}"
        editor.snapshot = {"text": "标题 正文 保存草稿", "elements": [element("保存草稿")]}


@pytest.mark.parametrize("platform,home_path", [("wechat", "/cgi-bin/home"), ("toutiao", "/profile_v4/index"), ("xiaohongshu", "/new/home")])
def test_old_account_home_remains_visible_after_many_accountless_editor_tabs(browser, platform, home_path):
    fake, settings = browser
    bridge.open_login(platform, settings)
    # The mapped page also lacks identity; the old home must be promoted on its
    # own, rather than merely benefiting from the mapped-page fallback.
    fake.context.pages[0].snapshot = {"text": "编辑器", "elements": []}
    home = fake.context.new_page()
    host = bridge.PLATFORM_URLS[platform].rstrip("/")
    home.url = host + home_path + "?token=never-return"
    home.snapshot = logged_in(platform)
    add_accountless_editors(fake, platform, count=11)
    pages_before = list(fake.context.pages)
    fake.calls.clear()
    inspected = bridge.inspect_platform(platform, settings)
    assert inspected["status"]["state"] == "connected"
    assert inspected["status"]["account_label"] == "测试账号"
    assert inspected["url"] == host + home_path
    assert "never-return" not in str(inspected)
    assert home.evaluations > 0
    assert len([operation for operation, _ in fake.calls if operation == "evaluate"]) <= 6
    assert fake.context.pages == pages_before and all(not page.closed for page in pages_before)
    assert not any(operation in {"goto", "new_page", "context_close", "bring_to_front"} for operation, _ in fake.calls)


def test_old_mapped_page_is_checked_even_when_it_is_not_a_recognized_home_route(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    mapped = fake.context.pages[0]
    mapped.url = "https://mp.toutiao.com/profile_v4/"
    mapped.snapshot = logged_in("toutiao")
    add_accountless_editors(fake, "toutiao", count=10)
    fake.calls.clear()
    assert bridge.check_connection("toutiao", settings)["state"] == "connected"
    assert mapped.evaluations > 0
    assert len([operation for operation, _ in fake.calls if operation == "evaluate"]) <= 6
    assert not any(operation in {"goto", "new_page", "context_close"} for operation, _ in fake.calls)


@pytest.mark.parametrize("challenge,expected", [("请完成安全验证", "needs_verification"), ("请先登录", "login_required")])
def test_latest_challenge_blocks_old_connected_home_even_with_many_editors(browser, challenge, expected):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    home = fake.context.pages[0]
    home.url = "https://mp.toutiao.com/profile_v4/index"
    home.snapshot = logged_in("toutiao")
    add_accountless_editors(fake, "toutiao", count=11)
    latest = fake.context.new_page()
    latest.url = "https://mp.toutiao.com/verify"
    latest.snapshot = {"text": challenge, "elements": []}
    evaluations_before = home.evaluations
    page_count = len(fake.context.pages)
    fake.calls.clear()
    callbacks = []
    result = bridge.run_in_page("toutiao", lambda *args: callbacks.append(True), settings)
    assert result["state"] == "needs_connection" and callbacks == []
    assert bridge.get_status(settings)["toutiao"]["state"] == expected
    assert latest.evaluations == 1 and home.evaluations == evaluations_before
    assert len(fake.context.pages) == page_count
    assert len([operation for operation, _ in fake.calls if operation == "evaluate"]) == 1
    assert not any(operation in {"goto", "new_page", "context_close", "bring_to_front"} for operation, _ in fake.calls)


def test_accountless_many_tab_inspection_remains_bounded_and_does_not_navigate(browser):
    fake, settings = browser
    bridge.open_login("toutiao", settings)
    fake.context.pages[0].snapshot = {"text": "编辑器", "elements": []}
    add_accountless_editors(fake, "toutiao", count=15)
    fake.calls.clear()
    assert bridge.check_connection("toutiao", settings)["state"] == "needs_verification"
    assert len([operation for operation, _ in fake.calls if operation == "evaluate"]) == 6
    assert not any(operation in {"goto", "new_page", "context_close"} for operation, _ in fake.calls)
