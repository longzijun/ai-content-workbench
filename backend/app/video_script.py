"""Strict video_script normalization, derived views and duration estimates.

Shots are the sole editable source of spoken content. Derived body/opening/closing
fields are rebuilt on every freeze and may never diverge from the shot array.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from urllib.parse import urlparse

from . import platform_registry

SHOT_ROLES = {"opening", "body", "closing"}
DEFAULT_SPEECH_CHARS_PER_SECOND = 4.0
MIN_SHOT_SECONDS = 1
EMPTY_SHOT_VISUAL_SECONDS = 2
TARGET_MIN_SECONDS = 10
TARGET_MAX_SECONDS = 600

_INPUT_WORK_FIELDS = {
    "platform", "format", "content_type", "script_schema_version", "title", "cover_text",
    "shots", "target_duration_seconds", "tags", "source_urls", "image_prompts", "image_tasks",
    "content_form",
}
_DERIVED_WORK_FIELDS = {
    "body", "opening_hook", "closing_conclusion", "estimated_duration_seconds",
    "duration_estimate_basis", "duration_delta_seconds", "duration_warning",
}
_INPUT_SHOT_FIELDS = {
    "id", "order", "role", "spoken_text", "visual_type", "visual_notes",
    "subtitle_points", "source_refs", "duration_estimate_seconds",
}


def is_video_work(work: dict) -> bool:
    if not isinstance(work, dict):
        return False
    return (work.get("format") == "video_script" or work.get("content_type") == "video_script"
            or platform_registry.is_video_platform(str(work.get("platform") or "")))


def uses_video(topic: dict) -> bool:
    platforms = topic.get("platforms") or []
    return any(platform_registry.is_video_platform(platform) for platform in platforms)


def _text(value, label: str, maximum: int, *, blank=False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise ValueError(f"{label}无效或超过 {maximum} 字")
    return value.strip() if not blank else value


def _string_list(value, label: str, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{label}必须是数组且最多 {maximum_items} 项")
    return [_text(item, label, maximum_chars) for item in value]


def estimate_shot_seconds(spoken_text: str, speech_rate: float = DEFAULT_SPEECH_CHARS_PER_SECOND) -> int:
    if not isinstance(speech_rate, (int, float)) or isinstance(speech_rate, bool) or speech_rate <= 0:
        raise ValueError("语速配置必须为正数")
    compact = re.sub(r"\s+", "", spoken_text)
    if not compact:
        return EMPTY_SHOT_VISUAL_SECONDS
    return max(MIN_SHOT_SECONDS, math.ceil(len(compact) / speech_rate))


def _normalize_shot(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict) or set(raw) - _INPUT_SHOT_FIELDS:
        raise ValueError("分镜字段不完整或含未知字段")
    identifier = _text(raw.get("id"), "分镜 ID", 64)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("分镜 ID 只能包含字母、数字、下划线或连字符")
    role = raw.get("role")
    if role not in SHOT_ROLES:
        raise ValueError("分镜 role 必须为 opening/body/closing")
    visual_type = raw.get("visual_type")
    if visual_type not in platform_registry.VISUAL_TYPES:
        raise ValueError("分镜 visual_type 不在固定枚举中")
    spoken = _text(raw.get("spoken_text"), "分镜口播", 6000)
    visual_notes = _text(raw.get("visual_notes"), "画面说明", 6000)
    subtitles = _string_list(raw.get("subtitle_points"), "字幕重点", 12, 500)
    refs = _string_list(raw.get("source_refs"), "分镜依据引用", 30, 4000)
    if not refs and role == "body" and not visual_notes.strip():
        raise ValueError("无来源的过渡分镜也必须保留画面说明")
    return {
        "id": identifier, "order": index, "role": role, "spoken_text": spoken,
        "visual_type": visual_type, "visual_notes": visual_notes,
        "subtitle_points": subtitles, "source_refs": refs,
        "duration_estimate_seconds": estimate_shot_seconds(spoken),
    }


def normalize_work(raw: dict, topic: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("视频脚本必须是对象")
    if set(raw) - _INPUT_WORK_FIELDS - _DERIVED_WORK_FIELDS:
        raise ValueError("视频脚本含未知字段")
    platform = raw.get("platform")
    config = platform_registry.get(platform)
    if config["supported_formats"] != ["video_script"]:
        raise ValueError("图文平台不能使用 video_script 作品类型")
    if raw.get("format") not in (None, "video_script") or raw.get("content_type") not in (None, "video_script"):
        raise ValueError("视频平台的作品类型必须为 video_script")
    if raw.get("script_schema_version") not in (None, platform_registry.VIDEO_SCRIPT_SCHEMA_VERSION):
        raise ValueError("视频脚本结构版本不受支持")
    title = _text(raw.get("title"), "脚本标题", 300)
    low, high = config["title_range"]
    if not low <= len(title) <= high:
        raise ValueError(f"{config['label']}标题需为 {low} 到 {high} 字（项目创作参数）")
    cover = _text(raw.get("cover_text"), "封面文字", 100)
    target = raw.get("target_duration_seconds")
    if target is None and topic is not None:
        target = topic.get("video_target_duration_seconds")
    if target is None:
        target = config["default_target_duration_seconds"]
    if type(target) is not int or not TARGET_MIN_SECONDS <= target <= TARGET_MAX_SECONDS:
        raise ValueError(f"目标时长须为 {TARGET_MIN_SECONDS} 到 {TARGET_MAX_SECONDS} 秒的整数")
    if topic is not None and topic.get("video_target_duration_seconds") is not None:
        if target != topic["video_target_duration_seconds"]:
            raise ValueError("视频目标时长与已批准选题不一致，请重新确认生产范围")
    shots_raw = raw.get("shots")
    if not isinstance(shots_raw, list) or not 2 <= len(shots_raw) <= 40:
        raise ValueError("视频脚本须包含 2 到 40 个有序分镜")
    shots = [_normalize_shot(shot, index) for index, shot in enumerate(shots_raw, 1)]
    ids = [shot["id"] for shot in shots]
    if len(ids) != len(set(ids)):
        raise ValueError("分镜 ID 不能重复")
    if shots[0]["role"] != "opening" or shots[-1]["role"] != "closing":
        raise ValueError("首镜必须为 opening，尾镜必须为 closing")
    if any(shot["role"] != "body" for shot in shots[1:-1]):
        raise ValueError("开场与收尾之间只能使用 body 分镜")
    if sum(shot["role"] == "opening" for shot in shots) != 1 or sum(shot["role"] == "closing" for shot in shots) != 1:
        raise ValueError("脚本必须且只能有一个开场和一个收尾")
    tags = _string_list(raw.get("tags", []), "标签", 50, 200)
    source_urls = _string_list(raw.get("source_urls", []), "来源链接", 100, 4000)
    body = "\n\n".join(shot["spoken_text"] for shot in shots)
    estimated = sum(shot["duration_estimate_seconds"] for shot in shots)
    delta = estimated - target
    tolerance = max(5, math.ceil(target * 0.2))
    warning = ""
    if abs(delta) > tolerance:
        warning = f"预计时长与目标相差 {delta:+d} 秒，请调整口播或分镜；估算不是成片时长"
    return {
        "platform": platform, "format": "video_script", "content_type": "video_script",
        "script_schema_version": platform_registry.VIDEO_SCRIPT_SCHEMA_VERSION,
        "content_form": str(raw.get("content_form") or ""),
        "title": title, "cover_text": cover,
        "opening_hook": shots[0]["spoken_text"], "shots": shots,
        "closing_conclusion": shots[-1]["spoken_text"], "body": body,
        "target_duration_seconds": target, "estimated_duration_seconds": estimated,
        "duration_estimate_basis": {
            "speech_chars_per_second": DEFAULT_SPEECH_CHARS_PER_SECOND,
            "minimum_shot_seconds": MIN_SHOT_SECONDS,
            "empty_shot_visual_seconds": EMPTY_SHOT_VISUAL_SECONDS,
            "method": "去空白口播字数/语速，逐镜头向上取整后求和；不是测得成片时长",
        },
        "duration_delta_seconds": delta, "duration_warning": warning,
        "tags": tags, "source_urls": source_urls,
        "image_prompts": [], "image_tasks": [],
    }


def normalize_payload(payload: dict, topic: dict | None = None) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("works"), list):
        return payload
    result = deepcopy(payload)
    result["works"] = [normalize_work(work, topic) if is_video_work(work) else work for work in result["works"]]
    return result


def validation_text(work: dict) -> str:
    """One final-text projection used by style, risk and placeholder checks."""
    normalized = normalize_work(work)
    chunks = [normalized["title"], normalized["cover_text"]]
    for shot in normalized["shots"]:
        chunks.extend([shot["spoken_text"], shot["visual_notes"], *shot["subtitle_points"]])
    return "\n".join(chunk for chunk in chunks if chunk)


def reference_errors(work: dict, topic: dict) -> list[str]:
    """Bind explicit shot URL references to approved or locked source material.

    Plain fact/experience labels remain human review items; they are not treated
    as proof. This check never fetches a URL or upgrades a claim to verified.
    """
    sources = topic.get('sources') or []
    if isinstance(sources, str):
        sources = json.loads(sources)
    allowed = {str(source.get('url') or '').strip() for source in sources if isinstance(source, dict)}
    locked = str(topic.get('_fact_base') or '')
    errors = []
    for shot in work.get('shots') or []:
        for reference in shot.get('source_refs') or []:
            if not re.match(r'^https?://', reference, re.I):
                continue
            parsed = urlparse(reference)
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not re.search(r'\s', reference)
            in_locked = bool(locked and re.search(re.escape(reference) + r'''(?=$|[\s"'，。；、）\]\},])''', locked))
            if not valid or (reference not in allowed and not in_locked):
                message = f"分镜 {shot.get('id') or '?'} 引用未纳入已批准来源或锁定底稿：{reference}"
                if message not in errors:
                    errors.append(message)
    return errors


def spoken_text(work: dict) -> str:
    return normalize_work(work)["body"]


def shooting_script(work: dict) -> str:
    normalized = normalize_work(work)
    lines = [f"标题：{normalized['title']}", f"封面文字：{normalized['cover_text']}",
             f"目标时长：{normalized['target_duration_seconds']} 秒（创作参数）",
             f"预计时长：{normalized['estimated_duration_seconds']} 秒（非成片时长）", ""]
    for shot in normalized["shots"]:
        lines.extend([
            f"分镜 {shot['order']}｜{shot['id']}｜{shot['role']}｜预计 {shot['duration_estimate_seconds']} 秒",
            f"口播：{shot['spoken_text']}",
            f"画面（{platform_registry.VISUAL_TYPES[shot['visual_type']]}）：{shot['visual_notes']}",
            "字幕重点：" + ("；".join(shot["subtitle_points"]) or "无"),
            "依据：" + ("；".join(shot["source_refs"]) or "纯过渡/作者确认材料，发布前人工核对"),
            "",
        ])
    if normalized["duration_warning"]:
        lines.append("提醒：" + normalized["duration_warning"])
    return "\n".join(lines).rstrip()


def generation_schema(platform: str) -> dict:
    config = platform_registry.get(platform)
    if config["supported_formats"] != ["video_script"]:
        raise ValueError("只有视频平台使用脚本 schema")
    shot = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "string"}, "role": {"type": "string", "enum": ["opening", "body", "closing"]},
            "spoken_text": {"type": "string"}, "visual_type": {"type": "string", "enum": list(platform_registry.VISUAL_TYPES)},
            "visual_notes": {"type": "string"}, "subtitle_points": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "role", "spoken_text", "visual_type", "visual_notes", "subtitle_points", "source_refs"],
    }
    work = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "platform": {"type": "string", "enum": [platform]},
            "format": {"type": "string", "enum": ["video_script"]},
            "content_type": {"type": "string", "enum": ["video_script"]},
            "title": {"type": "string"}, "cover_text": {"type": "string"},
            "shots": {"type": "array", "items": shot}, "target_duration_seconds": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "source_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["platform", "format", "content_type", "title", "cover_text", "shots", "target_duration_seconds", "tags", "source_urls"],
    }
    return {"type": "object", "additionalProperties": False,
            "properties": {"strategy": {"type": "string"}, "works": {"type": "array", "items": work}},
            "required": ["strategy", "works"]}


def prompt_instructions(topic: dict, platform: str) -> str:
    target = topic.get("video_target_duration_seconds") or platform_registry.get(platform)["default_target_duration_seconds"]
    return f"""
本次只生成 {platform} 的一件 video_script，不得退回 article/note，也不得生成视频文件或声称已经拍摄、录屏、剪辑、上传。
shots 是口播唯一来源。首镜 role=opening，尾镜 role=closing，中间仅使用 body；每镜提供稳定且唯一的 id、口语化 spoken_text、visual_type、visual_notes、subtitle_points、source_refs。
visual_type 只能取 talking_head（露脸）、screen_recording（录屏）、material（素材）、mixed（混合）。没有真实素材或录屏时在 visual_notes 明确写待拍摄/待素材，不得把计划写成已完成事实。
target_duration_seconds 使用用户已确认的 {target} 秒；这是创作目标，不是平台限制。模型无需自报总估算秒数，服务端按分镜统一计算。
opening_hook、closing_conclusion、body 和 estimated_duration_seconds 不由模型输出，服务端只从 shots 派生。字幕与画面说明不得新增无依据结论，AI 标识只保留在审阅元数据，不塞入口播或字幕。
只返回严格 JSON，不输出 Markdown 围栏、执行提示或内部检查报告。
"""
