"""Provider selection, account isolation and Windows credential guarantees."""
from copy import deepcopy
import os

import httpx
import pytest

from backend.app import adapters, artifacts, config


def test_local_is_used_without_probing_codex(tmp_path, monkeypatch):
    """本地模型可用时直接本地生成，Codex 不再参与路由或健康探测。"""
    settings = config.Settings(runtime_dir=tmp_path)
    monkeypatch.setattr(adapters, "_probe_local", lambda _: {"available": True, "models": ["qwen/qwen3-14b"]})
    monkeypatch.setattr(adapters, "_run_local", lambda *args: ({"works": []}, "local:test"))
    monkeypatch.setattr(adapters, "_probe_codex", lambda _: pytest.fail("new tasks must not probe codex"))
    monkeypatch.setattr(adapters, "_run_codex", lambda *args: pytest.fail("codex must not generate"))
    payload, provider = adapters.generate_payload({}, settings, lambda: False, lambda *args: None)
    assert provider == "local:test" and payload == {"works": []}


def test_local_service_unavailable_fails_without_codex_fallback(tmp_path, monkeypatch):
    settings = config.Settings(runtime_dir=tmp_path)
    monkeypatch.setattr(adapters, "_probe_local", lambda _: {"available": False, "models": []})
    monkeypatch.setattr(adapters, "_probe_codex", lambda _: pytest.fail("must not probe codex"))
    monkeypatch.setattr(adapters, "_run_codex", lambda *args: pytest.fail("must not fall back to codex"))
    with pytest.raises(adapters.ExecutorUnavailable, match="本地服务并加载模型"):
        adapters.generate_payload({}, settings, lambda: False, lambda *args: None)


def test_local_executor_unavailable_does_not_trigger_codex(tmp_path, monkeypatch):
    settings = config.Settings(runtime_dir=tmp_path)
    monkeypatch.setattr(adapters, "_probe_local", lambda _: {"available": True, "models": ["qwen/qwen3-14b"]})

    def unavailable(*args):
        raise adapters.ExecutorUnavailable("local service down")

    monkeypatch.setattr(adapters, "_run_local", unavailable)
    monkeypatch.setattr(adapters, "_run_codex", lambda *args: pytest.fail("must not fall back to codex"))
    with pytest.raises(adapters.ExecutorUnavailable, match="local service down"):
        adapters.generate_payload({}, settings, lambda: False, lambda *args: None)


@pytest.mark.parametrize("exception", [adapters.TaskCancelled(), RuntimeError("sandbox denied"), ValueError("invalid payload")])
def test_cancel_security_and_invalid_output_do_not_trigger_codex_fallback(tmp_path, monkeypatch, exception):
    monkeypatch.setattr(adapters, "_probe_local", lambda _: {"available": True, "models": ["qwen/qwen3-14b"]})

    def failure(*args):
        raise exception

    monkeypatch.setattr(adapters, "_run_local", failure)
    monkeypatch.setattr(adapters, "_probe_codex", lambda _: {"available": True, "authenticated": True})
    monkeypatch.setattr(adapters, "_run_codex", lambda *args: pytest.fail("must not retry rejected execution"))
    with pytest.raises(type(exception)):
        adapters.generate_payload({}, config.Settings(runtime_dir=tmp_path), lambda: False, lambda *args: None)


def test_codex_environment_cannot_silently_use_api_key(monkeypatch):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        monkeypatch.setenv(key, "diagnostic-placeholder")
    environment = adapters._codex_environment()
    assert not any(key in environment for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"))


def test_rotating_wechat_secret_invalidates_token_cache(tmp_path, monkeypatch):
    settings = config.Settings(runtime_dir=tmp_path, wechat_app_id="wx-test")
    credentials = {"app_id": "wx-test", "app_secret": "fake-secret-one"}
    monkeypatch.setattr(adapters, "get_wechat_credentials", lambda _: credentials)
    monkeypatch.setattr(adapters, "_tokens", {})
    sent = []

    def post(url, **kwargs):
        sent.append(deepcopy(kwargs["json"]))
        return httpx.Response(200, json={"access_token": f"token-{len(sent)}", "expires_in": 7200}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert adapters._wechat_token(settings) == "token-1"
    assert adapters._wechat_token(settings) == "token-1"
    credentials["app_secret"] = "fake-secret-two"
    assert adapters._wechat_token(settings) == "token-2"
    assert len(sent) == 2
    settings.wechat_app_id = "different-account"
    with pytest.raises(ValueError, match="不一致"):
        adapters._wechat_token(settings)
    assert len(sent) == 2


def test_wechat_readback_mismatch_does_not_claim_synced(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(200, json={"news_item": [{"title": "另一个标题"}]}, request=httpx.Request("POST", url)))
    result = artifacts._remote_get("fake-token", "remote-id", "当前标题")
    assert result["state"] == "unknown" and result["remote_id"] == "remote-id"


def test_wechat_readback_verifies_body_without_requiring_identical_html(monkeypatch):
    data = {"news_item": [{"title": "当前标题", "content": '<p style="margin:0">正文 <strong>内容</strong></p>'}]}
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(200, json=data, request=httpx.Request("POST", url)))
    assert artifacts._remote_get("fake-token", "remote-id", "当前标题", "<p>正文内容</p>")["state"] == "synced"
    result = artifacts._remote_get("fake-token", "remote-id", "当前标题", "<p>不同的正文</p>")
    assert result["state"] == "unknown" and result["remote_id"] == "remote-id"


def test_existing_draft_cannot_be_read_using_different_account(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_wechat_token", lambda _: pytest.fail("account changed; must not contact platform"))
    settings = config.Settings(runtime_dir=tmp_path, wechat_app_id="different-account")
    draft = {"platform": "wechat", "state": "unknown", "remote_id": "existing", "account_label": "微信公众号（AppID尾号1234）"}
    result = artifacts.sync_platform_draft(draft, {"payload": {"works": []}}, {}, settings)
    assert result["state"] == "unknown" and result["remote_id"] == "existing"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_encrypts_and_roundtrips_without_plaintext():
    value = b"diagnostic-placeholder-no-real-credentials"
    encrypted = config._dpapi(value)
    assert value not in encrypted
    assert config._dpapi(encrypted, decrypt=True) == value
