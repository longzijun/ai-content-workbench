"""批次D：逐平台/账号草稿保存许可（fail-closed，小红书永久关闭）。

标 local_content_only 退出 conftest 的平台测试模式，测的是**真实的逐平台许可层**
（platform_draft_supported / require_platform_save / set_platform_access）。
2026-11-04 用户授权解锁全局闸门：仅公众号/头条进白名单可启用，小红书永久锁、
白名单外平台不放行。两道闸门（全局 + 逐平台）依然独立。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app import config, db, runtime_mode

# 退出 conftest 的平台测试模式：这里测的是**真实的逐平台许可层**（不 monkeypatch）。
pytestmark = pytest.mark.local_content_only


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    return tmp_path


def test_global_release_flag_stays_off(world):
    assert runtime_mode.PLATFORM_OPERATIONS_RELEASED is False
    assert runtime_mode.DRAFT_SAVE_WHITELIST == frozenset({"wechat", "toutiao"})


def test_non_whitelist_platform_never_enabled(world):
    # 白名单外平台（视频号/快手/抖音等）即使填了账号标签也不允许启用。
    for platform in ("weixin_channels", "kuaishou", "douyin"):
        with pytest.raises(HTTPException) as error:
            runtime_mode.set_platform_access(platform, True, "账号")
        assert error.value.status_code == 409
        assert runtime_mode.platform_draft_supported(platform) is False


def test_all_platforms_disabled_by_default(world):
    # 没有任何平台被启用时，逐平台许可层不放行。
    assert runtime_mode.platform_draft_supported("wechat") is False
    assert runtime_mode.platform_draft_supported("toutiao") is False


def test_xiaohongshu_never_enabled(world):
    with pytest.raises(HTTPException) as error:
        runtime_mode.set_platform_access("xiaohongshu", True, "账号")
    assert error.value.status_code == 409
    assert runtime_mode.platform_draft_supported("xiaohongshu") is False


def test_enable_requires_account_label(world):
    with pytest.raises(HTTPException) as error:
        runtime_mode.set_platform_access("wechat", True, "")
    assert error.value.status_code == 422


def test_enable_then_per_platform_allowed(world):
    view = runtime_mode.set_platform_access("wechat", True, "公众号-主账号")
    wechat = next(p for p in view["platforms"] if p["platform"] == "wechat")
    assert wechat["enabled"] is True and wechat["account_label"] == "公众号-主账号"
    # 逐平台许可层已放行（全局发布标志由 test_global_release_flag_stays_off 单独守住）。
    assert runtime_mode.platform_draft_supported("wechat") is True
    assert runtime_mode.platform_draft_supported("toutiao") is False


def test_disable_clears_enabled(world):
    runtime_mode.set_platform_access("toutiao", True, "头条账号")
    view = runtime_mode.set_platform_access("toutiao", False)
    toutiao = next(p for p in view["platforms"] if p["platform"] == "toutiao")
    assert toutiao["enabled"] is False
    assert runtime_mode.platform_draft_supported("toutiao") is False


def test_require_platform_save_blocks_xhs(world):
    with pytest.raises(HTTPException) as error:
        runtime_mode.require_platform_save("xiaohongshu", "测试")
    assert error.value.status_code == 403
    assert "小红书" in str(error.value.detail)


def test_access_view_marks_xhs_blocked(world):
    view = runtime_mode.platform_access_view()
    xhs = next(p for p in view["platforms"] if p["platform"] == "xiaohongshu")
    assert xhs["blocked"] is True and xhs["enabled"] is False
    assert "关闭" in xhs["blocked_reason"]


def test_draft_capability_matrix_is_honest(world):
    """批次E：未验证的「只保存草稿」能力必须明确显示未接通，不冒充已支持。"""
    from backend.app import platform_registry
    assert platform_registry.draft_capability("wechat")["status"] == "available"
    assert platform_registry.draft_capability("toutiao")["status"] == "available"
    assert platform_registry.draft_capability("xiaohongshu")["status"] == "disabled"
    # 视频号无官方草稿 API 依据；快手/抖音「创建视频」属发布流程，均非「仅存草稿」。
    assert platform_registry.draft_capability("weixin_channels")["status"] == "unverified"
    assert platform_registry.draft_capability("kuaishou")["status"] == "unverified"
    assert platform_registry.draft_capability("douyin")["status"] == "unverified"


def test_access_view_exposes_draft_capability(world):
    view = runtime_mode.platform_access_view()
    weixin = next(p for p in view["platforms"] if p["platform"] == "weixin_channels")
    assert weixin["draft_capability"]["status"] == "unverified"
    wechat = next(p for p in view["platforms"] if p["platform"] == "wechat")
    assert wechat["draft_capability"]["status"] == "available"
