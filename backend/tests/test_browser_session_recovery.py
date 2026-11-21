"""Cold-process recovery of a verified existing project Edge, with no real CDP."""
import json
from pathlib import Path
import threading

import pytest

from backend.app import browser_bridge as bridge
from backend.app.config import Settings


def authenticated(platform):
    menus = {"wechat": ["首页", "内容管理"], "toutiao": ["作品管理", "草稿箱"], "xiaohongshu": ["发布笔记", "笔记管理"]}[platform]
    return {"text": "恢复账号 " + " ".join(menus), "elements": [
        {"tag": "span", "role": "", "text": "恢复账号", "label": "", "class_name": "user-name", "href": None},
        *[{"tag": "a", "role": "", "text": text, "label": "", "class_name": "", "href": None} for text in menus],
    ]}


class Page:
    def __init__(self, owner, platform):
        self.owner = owner
        self.url = bridge.PLATFORM_URLS[platform].rstrip("/") + bridge._HOME_PATHS[platform] + "?token=never-return"
        self.snapshot = authenticated(platform)
        self.closed = False

    def is_closed(self):
        self.owner.record("is_closed")
        return self.closed

    def evaluate(self, script):
        self.owner.record("evaluate")
        assert script == bridge._DOM_SNAPSHOT
        return self.snapshot

    def wait_for_load_state(self, *args, **kwargs):
        self.owner.record("wait_for_load_state")

    def goto(self, *args, **kwargs):
        pytest.fail("Recovering a session must never navigate an existing tab")

    def bring_to_front(self):
        pytest.fail("Connection recovery must not change the active tab")


class Context:
    def __init__(self, owner):
        self.pages = [Page(owner, platform) for platform in bridge.PLATFORM_URLS]

    def new_page(self):
        pytest.fail("Connection recovery must not create a tab")

    def close(self):
        pytest.fail("Stopping the bridge must not close independent Edge")


class Browser:
    def __init__(self, owner):
        self.owner = owner

    @property
    def contexts(self):
        self.owner.record("contexts")
        return [self.owner.context]

    def is_connected(self):
        self.owner.record("is_connected")
        return self.owner.connected

    def close(self):
        pytest.fail("Recovery must not close independent Edge")


class Playwright:
    def __init__(self):
        self.calls, self.connects = [], 0
        self.connected = False
        self.attach_failure = False
        self.context = Context(self)
        self.chromium = self

    def record(self, name):
        self.calls.append((name, threading.get_ident()))

    def __enter__(self):
        self.record("enter")
        return self

    def __exit__(self, *args):
        self.record("exit")
        self.connected = False

    def connect_over_cdp(self, endpoint, **kwargs):
        self.record("attach")
        assert endpoint == f"http://127.0.0.1:{bridge.CDP_PORT}"
        self.connects += 1
        if self.attach_failure:
            raise RuntimeError("CDP failed token=never-return")
        self.connected = True
        return Browser(self)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    bridge.stop_browser()
    runtime = tmp_path / "runtime"
    profile = runtime / "browser-profile"
    profile.mkdir(parents=True)
    executable = tmp_path / "Edge" / "msedge.exe"
    executable.parent.mkdir()
    executable.touch()
    record = {"pid": 456, "port": bridge.CDP_PORT, "profile": str(profile), "executable": str(executable)}
    manifest = runtime / "browser-session.json"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    info = {"ProcessId": 456, "ExecutablePath": str(executable), "CommandLine": [str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={bridge.CDP_PORT}", "--remote-debugging-address=127.0.0.1"]}
    listeners = [{"OwningProcess": 456, "LocalAddress": "127.0.0.1"}]
    fake = Playwright()
    monkeypatch.setattr(bridge, "_load_playwright", lambda: fake)
    monkeypatch.setattr(bridge, "_edge_executable", lambda: executable)
    monkeypatch.setattr(bridge, "_process_info", lambda pid: info)
    monkeypatch.setattr(bridge, "_listener_info", lambda port: listeners)
    monkeypatch.setattr(bridge, "_ensure_edge", lambda *_: pytest.fail("Implicit recovery must not enter a launch-capable path"))
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Implicit recovery must not launch Edge"))
    settings = Settings(runtime_dir=runtime, vault_path=str(tmp_path / "vault"))
    yield {"fake": fake, "settings": settings, "record": record, "manifest": manifest, "process": info, "listeners": listeners}
    bridge.stop_browser()


def test_cached_status_has_no_recovery_or_browser_side_effects(recovery, monkeypatch):
    monkeypatch.setattr(bridge, "_existing_edge", lambda *_: pytest.fail("Cache reads must not inspect a process"))
    status = bridge.get_status(recovery["settings"])
    assert all(value["state"] == "disconnected" for value in status.values())
    assert recovery["fake"].connects == 0 and recovery["fake"].calls == []


@pytest.mark.parametrize("platform", list(bridge.PLATFORM_URLS))
def test_direct_run_after_app_restart_recovers_only_verified_running_edge(recovery, platform):
    fake, settings = recovery["fake"], recovery["settings"]
    received = []
    pages_before = list(fake.context.pages)

    def callback(page, account):
        received.append((page, account, threading.get_ident()))
        return {"state": "observed"}

    assert bridge.get_status(settings)[platform]["state"] == "disconnected"
    result = bridge.run_in_page(platform, callback, settings)
    assert result["state"] == "observed" and result["account_label"] == "恢复账号"
    assert received[0][0] is pages_before[list(bridge.PLATFORM_URLS).index(platform)]
    assert fake.connects == 1 and fake.context.pages == pages_before
    assert {thread_id for _, thread_id in fake.calls} == {received[0][2]}
    assert received[0][2] != threading.get_ident()
    assert "never-return" not in str(result)


@pytest.mark.parametrize("operation", ["check", "inspect"])
def test_connection_check_can_recover_without_open_login_or_tab_changes(recovery, operation):
    settings = recovery["settings"]
    result = bridge.check_connection("wechat", settings) if operation == "check" else bridge.inspect_platform("wechat", settings)
    status = result if operation == "check" else result["status"]
    assert status["state"] == "connected" and status["account_label"] == "恢复账号"
    assert "never-return" not in str(result)
    assert recovery["fake"].connects == 1


def test_driver_restart_then_direct_check_preserves_and_recovers_existing_tabs(recovery):
    fake, settings = recovery["fake"], recovery["settings"]
    pages = list(fake.context.pages)
    assert bridge.check_connection("toutiao", settings)["state"] == "connected"
    bridge.stop_browser()
    assert bridge.get_status(settings)["toutiao"]["state"] == "disconnected"
    assert bridge.check_connection("toutiao", settings)["state"] == "connected"
    assert fake.connects == 2 and fake.context.pages == pages and all(not page.closed for page in pages)


def test_lost_driver_connection_revalidates_before_attaching_again(recovery):
    fake, settings = recovery["fake"], recovery["settings"]
    assert bridge.check_connection("xiaohongshu", settings)["state"] == "connected"
    fake.connected = False
    assert bridge.run_in_page("xiaohongshu", lambda *_: {"state": "observed"}, settings)["state"] == "observed"
    assert fake.connects == 2


@pytest.mark.parametrize("case", ["missing_manifest", "bad_json", "oversized_manifest", "wrong_profile", "wrong_port", "wrong_executable", "bad_pid", "pid_reused", "normal_profile", "public_listener", "foreign_listener", "no_listener"])
def test_invalid_existing_session_is_never_attached_and_callback_never_runs(recovery, case):
    record, manifest = recovery["record"], recovery["manifest"]
    if case == "missing_manifest":
        manifest.unlink()
    elif case == "bad_json":
        manifest.write_text("{ invalid", encoding="utf-8")
    elif case == "oversized_manifest":
        manifest.write_text(" " * 8193, encoding="utf-8")
    elif case in ("wrong_profile", "wrong_port", "wrong_executable", "bad_pid"):
        key, value = {"wrong_profile": ("profile", str(manifest.parent / "normal-profile")), "wrong_port": ("port", 9222), "wrong_executable": ("executable", str(manifest.parent / "other.exe")), "bad_pid": ("pid", True)}[case]
        record[key] = value
        manifest.write_text(json.dumps(record), encoding="utf-8")
    elif case == "pid_reused":
        recovery["process"]["ExecutablePath"] = str(manifest.parent / "other.exe")
    elif case == "normal_profile":
        recovery["process"]["CommandLine"][1] = "--user-data-dir=" + str(manifest.parent / "normal-profile")
    elif case == "public_listener":
        recovery["listeners"][0]["LocalAddress"] = "0.0.0.0"
    elif case == "foreign_listener":
        recovery["listeners"][0]["OwningProcess"] = 999
    else:
        recovery["listeners"].clear()
    callbacks = []
    result = bridge.run_in_page("wechat", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == []
    assert recovery["fake"].connects == 0
    assert bridge.get_status(recovery["settings"])["wechat"]["state"] == "disconnected"


def test_missing_project_profile_does_not_create_files_or_load_playwright(recovery):
    runtime = recovery["settings"].runtime_dir.parent / "fresh-runtime"
    settings = Settings(runtime_dir=runtime)
    assert bridge.run_in_page("wechat", lambda *_: pytest.fail("No UI work"), settings)["state"] == "needs_connection"
    assert not runtime.exists() and recovery["fake"].connects == 0


def test_failed_cdp_attach_is_clear_preflight_failure_and_redacts_raw_error(recovery):
    recovery["fake"].attach_failure = True
    callbacks = []
    result = bridge.run_in_page("toutiao", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == []
    assert "never-return" not in str(result)
    assert recovery["fake"].calls[-1][0] == "exit"


def test_different_runtime_cannot_borrow_already_connected_project_browser(recovery):
    assert bridge.check_connection("wechat", recovery["settings"])["state"] == "connected"
    other = Settings(runtime_dir=recovery["settings"].runtime_dir.parent / "different-runtime")
    callbacks = []
    result = bridge.run_in_page("wechat", lambda *_: callbacks.append(True), other)
    assert result["state"] == "needs_connection" and callbacks == []
    assert recovery["fake"].connects == 1


@pytest.mark.parametrize("text,expected", [("请先登录", "login_required"), ("请完成安全验证", "needs_verification")])
def test_recovered_current_login_or_challenge_blocks_callback(recovery, text, expected):
    page = Page(recovery["fake"], "wechat")
    page.url = "https://mp.weixin.qq.com/verify?token=never-return"
    page.snapshot = {"text": text, "elements": []}
    recovery["fake"].context.pages.append(page)
    callbacks = []
    result = bridge.run_in_page("wechat", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == []
    assert bridge.get_status(recovery["settings"])["wechat"]["state"] == expected
    assert "never-return" not in str(result)


def accountless(recovery, platform, remove=False):
    host = bridge.PLATFORM_URLS[platform].rstrip("/")
    for page in list(recovery["fake"].context.pages):
        if page.url.startswith(host):
            if remove:
                page.closed = True
            else:
                page.url = host + "/editor/existing?token=never-return"
                page.snapshot = {"text": "标题 正文 保存草稿", "elements": [{"tag": "button", "role": "", "text": "保存草稿", "label": "", "class_name": "", "href": None}]}


def new_home_factory(recovery, platform, snapshot=None, redirected_url=None, fail=False):
    fake = recovery["fake"]
    visits, opened = [], []

    class FreshHome(Page):
        def goto(self, url, **kwargs):
            fake.record("fresh_home_goto")
            assert url == bridge._RUN_HOME_URLS[platform]
            assert "token" not in url
            visits.append(url)
            if fail:
                raise RuntimeError("navigation interrupted token=never-return")
            self.url = redirected_url or bridge.PLATFORM_URLS[platform].rstrip("/") + bridge._HOME_PATHS[platform] + "?token=never-return"
            self.snapshot = snapshot or authenticated(platform)

    def create():
        fake.record("fresh_home_new_page")
        home = FreshHome(fake, platform)
        home.url = "about:blank"
        fake.context.pages.append(home)
        opened.append(home)
        return home

    fake.context.new_page = create
    return visits, opened


@pytest.mark.parametrize("platform", list(bridge.PLATFORM_URLS))
@pytest.mark.parametrize("remove", [False, True])
def test_run_opens_independent_official_home_when_only_accountless_editor_or_no_tab(recovery, platform, remove):
    fake, settings = recovery["fake"], recovery["settings"]
    accountless(recovery, platform, remove)
    original_pages = list(fake.context.pages)
    original_urls = [page.url for page in original_pages]
    visits, opened = new_home_factory(recovery, platform)
    received = []
    result = bridge.run_in_page(platform, lambda page, account: (received.append((page, account)) or {"state": "observed"}), settings)
    assert result["state"] == "observed" and received == [(opened[0], "恢复账号")]
    assert visits == [bridge._RUN_HOME_URLS[platform]] and len(opened) == 1
    assert [page.url for page in original_pages] == original_urls
    assert fake.context.pages[:-1] == original_pages and fake.connects == 1
    assert "never-return" not in str(result)
    assert len({thread_id for _, thread_id in fake.calls}) == 1


@pytest.mark.parametrize("operation", ["status", "check", "inspect"])
def test_accountless_editor_observation_never_creates_or_navigates_home(recovery, operation):
    accountless(recovery, "toutiao")
    visits, opened = new_home_factory(recovery, "toutiao")
    if operation == "status":
        bridge.get_status(recovery["settings"])
        assert recovery["fake"].connects == 0
    elif operation == "check":
        assert bridge.check_connection("toutiao", recovery["settings"])["state"] == "needs_verification"
    else:
        assert bridge.inspect_platform("toutiao", recovery["settings"])["status"]["state"] == "needs_verification"
    assert visits == [] and opened == []


@pytest.mark.parametrize("text", ["请先登录", "请完成安全验证"])
def test_known_latest_login_or_challenge_cannot_trigger_fresh_home_navigation(recovery, text):
    accountless(recovery, "wechat")
    page = Page(recovery["fake"], "wechat")
    page.url = "https://mp.weixin.qq.com/verify"
    page.snapshot = {"text": text, "elements": []}
    recovery["fake"].context.pages.append(page)
    visits, opened = new_home_factory(recovery, "wechat")
    callbacks = []
    assert bridge.run_in_page("wechat", lambda *_: callbacks.append(True), recovery["settings"])["state"] == "needs_connection"
    assert callbacks == [] and visits == [] and opened == []


@pytest.mark.parametrize("text", ["请先登录", "请完成安全验证"])
def test_fresh_home_login_or_challenge_is_not_account_confirmation(recovery, text):
    accountless(recovery, "toutiao")
    visits, opened = new_home_factory(recovery, "toutiao", snapshot={"text": text, "elements": []})
    callbacks = []
    result = bridge.run_in_page("toutiao", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == []
    assert len(opened) == 1 and len(visits) == 1


@pytest.mark.parametrize("url", ["https://passport.example.invalid/login?token=never-return", "http://mp.toutiao.com/profile_v4/index", "https://mp.toutiao.com:444/profile_v4/index", "https://user@mp.toutiao.com/profile_v4/index"])
def test_official_home_redirect_to_untrusted_origin_cannot_confirm_account(recovery, url):
    accountless(recovery, "toutiao")
    new_home_factory(recovery, "toutiao", redirected_url=url)
    callbacks = []
    result = bridge.run_in_page("toutiao", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == []
    assert "never-return" not in str(result)


def test_failed_fresh_home_navigation_is_preflight_failure_without_callback_or_token_leak(recovery):
    accountless(recovery, "wechat")
    new_home_factory(recovery, "wechat", fail=True)
    callbacks = []
    result = bridge.run_in_page("wechat", lambda *_: callbacks.append(True), recovery["settings"])
    assert result["state"] == "needs_connection" and callbacks == [] and "never-return" not in str(result)
