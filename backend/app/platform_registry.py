"""Single content-platform contract shared by API, generation and frontend.

These are local content capabilities, not platform login or upload claims.
Creative title/duration defaults are project parameters, not official limits.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

PlatformId = Literal["wechat", "toutiao", "xiaohongshu", "weixin_channels", "kuaishou", "douyin"]
REGISTRY_SCHEMA_VERSION = "platform-registry-v1"
VIDEO_SCRIPT_SCHEMA_VERSION = "video-script-v1"

_REGISTRY = {
    "wechat": {
        "id": "wechat", "label": "微信公众号", "supported_formats": ["article"],
        "schema_version": "text-work-v1", "local_generation": True, "external_operations": False,
        "title_range": [1, 32], "creative_rules_version": "existing-project-rules-v1",
    },
    "toutiao": {
        "id": "toutiao", "label": "今日头条", "supported_formats": ["article"],
        "schema_version": "text-work-v1", "local_generation": True, "external_operations": False,
        "title_range": [2, 30], "creative_rules_version": "existing-project-rules-v1",
    },
    "xiaohongshu": {
        "id": "xiaohongshu", "label": "小红书", "supported_formats": ["note"],
        "schema_version": "text-work-v1", "local_generation": True, "external_operations": False,
        "title_range": [1, 20], "creative_rules_version": "existing-project-rules-v1",
    },
    "weixin_channels": {
        "id": "weixin_channels", "label": "视频号", "supported_formats": ["video_script"],
        "schema_version": VIDEO_SCRIPT_SCHEMA_VERSION, "local_generation": True, "external_operations": False,
        "title_range": [1, 60], "default_target_duration_seconds": 60,
        "creative_rules_version": "project-creative-defaults-v1",
    },
    "kuaishou": {
        "id": "kuaishou", "label": "快手", "supported_formats": ["video_script"],
        "schema_version": VIDEO_SCRIPT_SCHEMA_VERSION, "local_generation": True, "external_operations": False,
        "title_range": [1, 60], "default_target_duration_seconds": 60,
        "creative_rules_version": "project-creative-defaults-v1",
    },
    "douyin": {
        "id": "douyin", "label": "抖音", "supported_formats": ["video_script"],
        "schema_version": VIDEO_SCRIPT_SCHEMA_VERSION, "local_generation": True, "external_operations": False,
        "title_range": [1, 60], "default_target_duration_seconds": 60,
        "creative_rules_version": "project-creative-defaults-v1",
    },
}

PLATFORM_IDS = tuple(_REGISTRY)
TEXT_PLATFORM_IDS = tuple(key for key, value in _REGISTRY.items() if "video_script" not in value["supported_formats"])
VIDEO_PLATFORM_IDS = tuple(key for key, value in _REGISTRY.items() if value["supported_formats"] == ["video_script"])
PLATFORM_SET = frozenset(PLATFORM_IDS)
VISUAL_TYPES = {
    "talking_head": "露脸",
    "screen_recording": "录屏",
    "material": "素材",
    "mixed": "混合",
}


def get(platform: str) -> dict:
    if platform not in _REGISTRY:
        raise ValueError("不支持的发布平台")
    return deepcopy(_REGISTRY[platform])


def expected_format(platform: str) -> str:
    formats = get(platform)["supported_formats"]
    if len(formats) != 1:
        raise ValueError("平台输出形态未唯一配置")
    return formats[0]


def is_video_platform(platform: str) -> bool:
    return platform in VIDEO_PLATFORM_IDS


def public_registry() -> dict:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "video_script_schema_version": VIDEO_SCRIPT_SCHEMA_VERSION,
        "visual_types": deepcopy(VISUAL_TYPES),
        "note": "仅声明本地内容产出能力；不代表平台登录、自动上传或官方时长/字数限制。",
        "platforms": [deepcopy(_REGISTRY[key]) for key in PLATFORM_IDS],
    }


#: 「只保存草稿」的真实能力（批次E）。只声明有官方依据、且确实只写后台草稿箱
#: （不调用发布）的能力；拿不到可核验依据的一律标 unverified，不把「上传素材」
#: 或「调用发布 API」冒充「保存草稿成功」。
DRAFT_CAPABILITY = {
    "wechat": {"status": "available", "note": "已有官方草稿 API 与网页适配；需真实账号核验写权限与回读。"},
    "toutiao": {"status": "available", "note": "已有网页草稿定位与回读；需重新验证当前后台。"},
    "xiaohongshu": {"status": "disabled", "note": "保持关闭自动触达，不保存草稿、不登录、不浏览。"},
    "weixin_channels": {"status": "unverified", "note": "普通视频草稿 API 未取得可核验官方依据，列为待验证，不承诺可接。"},
    "kuaishou": {"status": "unverified", "note": "photo/publish 属发布流程，无「仅存草稿」通道；脚本就绪不等于可上传。"},
    "douyin": {"status": "unverified", "note": "创建视频属发布流程，无「仅存草稿」通道；脚本就绪不等于可上传。"},
}


def draft_capability(platform: str) -> dict:
    """某平台「只保存草稿」的能力声明。未声明平台一律 unverified（fail-closed）。"""
    return deepcopy(DRAFT_CAPABILITY.get(platform, {"status": "unverified", "note": "未取得可核验的草稿保存依据。"}))


def draft_capabilities() -> dict:
    return {platform: draft_capability(platform) for platform in PLATFORM_IDS}
