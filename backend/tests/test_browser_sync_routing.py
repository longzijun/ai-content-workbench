"""Route by durable draft identity and usable provider, never stale UI cache."""
import httpx
import pytest

from backend.app import artifacts, browser_bridge, browser_drafts
from backend.app.config import Settings


@pytest.fixture
def route(tmp_path, monkeypatch):
    settings = Settings(runtime_dir=tmp_path / "runtime")
    work = {"platform": "wechat", "format": "article", "title": "草稿路由校验", "body": "需要审核的完整正文。"}
    draft = {"id": "draft", "platform": "wechat", "topic_id": "topic", "version_id": "version", "state": "needs_connection", "remote_id": None}
    version = {"id": "version", "payload": {"works": [work]}}
    topic = {"id": "topic"}
    calls = []

    def browser_sync(*args):
        calls.append(args)
        return {"state": "needs_connection", "error": "需要真实后台连接"}

    monkeypatch.setattr(browser_drafts, "sync_browser_draft", browser_sync)
    monkeypatch.setattr(browser_bridge, "get_status", lambda *_: pytest.fail("Cached UI state cannot choose a save route"))
    return draft, version, topic, settings, calls


@pytest.mark.parametrize("case", ["no_api", "unverified", "missing_cover"])
def test_wechat_new_draft_uses_browser_live_preflight_when_api_is_not_ready(route, monkeypatch, case):
    draft, version, topic, settings, calls = route
    if case != "no_api":
        settings.wechat_app_id = "configured-app"
        settings.wechat_thumb_media_id = "cover"
        settings.wechat_capability_verified = True
        if case == "unverified":
            settings.wechat_capability_verified = False
        else:
            settings.wechat_thumb_media_id = ""
    monkeypatch.setattr(artifacts, "_wechat_token", lambda *_: pytest.fail("Unready API must not receive draft creation"))
    result = artifacts.sync_platform_draft(draft, version, topic, settings)
    assert result["state"] == "needs_connection" and len(calls) == 1


@pytest.mark.parametrize("platform", ["wechat", "toutiao", "xiaohongshu"])
def test_known_browser_id_stays_on_browser_even_if_api_is_ready(route, monkeypatch, platform):
    draft, version, topic, settings, calls = route
    draft.update(platform=platform, remote_id="browser:" + platform + ":real-recorded-id", state="unknown")
    settings.wechat_app_id, settings.wechat_thumb_media_id, settings.wechat_capability_verified = "configured-app", "cover", True
    monkeypatch.setattr(artifacts, "_wechat_token", lambda *_: pytest.fail("Browser ID must not cross into API provider"))
    artifacts.sync_platform_draft(draft, version, topic, settings)
    assert len(calls) == 1 and calls[0][0]["remote_id"] == draft["remote_id"]


def test_known_api_id_stays_read_only_on_api_with_cold_browser_cache(route, monkeypatch):
    draft, version, topic, settings, calls = route
    draft.update(remote_id="recorded-api-media-id", state="unknown")
    settings.wechat_app_id = "configured-app"
    read_ids = []
    monkeypatch.setattr(artifacts, "_wechat_token", lambda *_: "test-token")

    def remote_get(token, remote_id, *args):
        read_ids.append(remote_id)
        return {"state": "synced", "remote_id": remote_id}

    monkeypatch.setattr(artifacts, "_remote_get", remote_get)
    result = artifacts.sync_platform_draft(draft, version, topic, settings)
    assert result["state"] == "synced" and read_ids == [draft["remote_id"]] and calls == []


def test_fully_verified_configured_api_keeps_existing_new_draft_route(route, monkeypatch):
    draft, version, topic, settings, calls = route
    settings.wechat_app_id, settings.wechat_thumb_media_id, settings.wechat_capability_verified = "configured-app", "cover", True
    monkeypatch.setattr(artifacts, "_wechat_token", lambda *_: "test-token")
    api_calls = []

    def post(url, **kwargs):
        api_calls.append(url)
        assert url.endswith("/draft/add") or url.endswith("/draft/get")
        data = {"media_id": "actual-api-id"} if url.endswith("/draft/add") else {"news_item": [{"title": version["payload"]["works"][0]["title"], "content": "<p>需要审核的完整正文。</p>"}]}
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    monkeypatch.setattr(artifacts.httpx, "post", post)
    result = artifacts.sync_platform_draft(draft, version, topic, settings)
    assert result["state"] == "synced" and calls == [] and len(api_calls) == 2


def test_uncertain_api_attempt_without_id_still_blocks_another_create(route, monkeypatch):
    draft, version, topic, settings, calls = route
    draft["state"] = "unknown"
    settings.wechat_app_id, settings.wechat_thumb_media_id, settings.wechat_capability_verified = "configured-app", "cover", True
    monkeypatch.setattr(artifacts, "_wechat_token", lambda *_: pytest.fail("Uncertain creation must not retry"))
    result = artifacts.sync_platform_draft(draft, version, topic, settings)
    assert result["state"] == "unknown" and calls == []
