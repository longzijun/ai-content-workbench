"""Versioned editorial voice, selected stance and optional platform-style assets.

The bundled fictional sample teaches tone only. Platform samples are optional; an empty
set is valid. Complete author voice is explicitly marked missing rather than being
reconstructed from quotations. Exact resource bytes participate in checkpoints.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from . import platform_registry

DEFAULT_LIBRARY_PATH = Path(__file__).resolve().parent / "style_samples" / "example_v1.json"
PLATFORM_NAMES = {platform: platform_registry.get(platform)["label"] for platform in platform_registry.PLATFORM_IDS}
EXPECTED_FORMATS = {platform: platform_registry.expected_format(platform) for platform in platform_registry.PLATFORM_IDS}
CONTENT_LINES = {"A", "B", "C"}


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate(data: dict) -> None:
    if not isinstance(data, dict):
        raise RuntimeError("文风样本库根节点必须是对象")
    for key in ("library_id", "version", "rules_version"):
        if not _text(data.get(key)):
            raise RuntimeError(f"文风样本库缺少有效字段：{key}")
    if data.get("status") != "confirmed":
        raise RuntimeError("只有 confirmed 状态的文风样本库才能进入生成提示词")
    kernel = data.get("author_kernel")
    if not isinstance(kernel, list) or not kernel or not all(_text(part) for part in kernel):
        raise RuntimeError("文风样本库缺少有效的作者内核")
    lines = data.get("content_lines")
    if not isinstance(lines, dict) or set(lines) != CONTENT_LINES:
        raise RuntimeError("文风样本库必须完整定义 A/B/C 内容线")
    for key, line in lines.items():
        if not isinstance(line, dict) or not _text(line.get("name")) or not _text(line.get("prompt")):
            raise RuntimeError(f"内容线 {key} 定义不完整")
        if type(line.get("editorial_share")) is not int:
            raise RuntimeError(f"内容线 {key} 的编辑比例无效")
    voice = data.get("voice")
    if not isinstance(voice, dict) or voice.get("status") not in {"missing", "confirmed"}:
        raise RuntimeError("原声样本必须明确标记 missing 或 confirmed")
    if voice["status"] == "confirmed" and not _text(voice.get("text")):
        raise RuntimeError("confirmed 原声样本缺少完整原文")
    selected = data.get("voice_selected")
    if not isinstance(selected, dict) or selected.get("confirmed") is not True:
        raise RuntimeError("缺少作者确认的选定文风样本")
    if selected.get("usage") != "style_only" or not _text(selected.get("id")) or not _text(selected.get("title")):
        raise RuntimeError("选定文风样本的用途或标识无效")
    if not isinstance(selected.get("paragraphs"), list) or not selected["paragraphs"] or not all(
        _text(part) for part in selected["paragraphs"]
    ):
        raise RuntimeError("选定文风样本正文段落无效")
    stances = data.get("stances")
    if not isinstance(stances, list):
        raise RuntimeError("观点库必须是数组")
    identifiers = set()
    for stance in stances:
        if not isinstance(stance, dict) or not all(_text(stance.get(key)) for key in ("id", "statement", "reason")):
            raise RuntimeError("观点条目缺少 id、statement 或 reason")
        if stance["id"] in identifiers or type(stance.get("confirmed")) is not bool:
            raise RuntimeError("观点条目重复或 confirmed 标记无效：" + stance["id"])
        identifiers.add(stance["id"])
    samples = data.get("platform_samples")
    if not isinstance(samples, list):
        raise RuntimeError("platform_samples 必须是数组；允许为空")
    active = set()
    for sample in samples:
        if not isinstance(sample, dict) or not all(_text(sample.get(key)) for key in ("id", "title", "source_document")):
            raise RuntimeError("平台样稿缺少有效字段")
        platform = sample.get("platform")
        if platform not in PLATFORM_NAMES or sample.get("format") != EXPECTED_FORMATS[platform]:
            raise RuntimeError("平台样稿平台或格式无效：" + sample["id"])
        if sample.get("usage") != "style_only" or type(sample.get("enabled")) is not bool or sample.get("confirmed") is not True:
            raise RuntimeError("平台样稿必须 confirmed、style_only，并有布尔 enabled：" + sample["id"])
        if not isinstance(sample.get("paragraphs"), list) or not sample["paragraphs"] or not all(
            _text(part) for part in sample["paragraphs"]
        ):
            raise RuntimeError("平台样稿正文段落无效：" + sample["id"])
        if sample["enabled"] and platform in active:
            raise RuntimeError(f"{PLATFORM_NAMES[platform]}最多只能有一篇启用样稿")
        if sample["enabled"]:
            active.add(platform)


def _read(path: Path | str) -> tuple[dict, bytes]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise RuntimeError(f"文风样本库读取失败：{source}") from error
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"文风样本库不是有效 UTF-8 JSON：{source}") from error
    _validate(data)
    return data, raw


def load_library(path: Path | str = DEFAULT_LIBRARY_PATH) -> dict:
    data, _ = _read(path)
    return data


def fingerprint(path: Path | str = DEFAULT_LIBRARY_PATH) -> str:
    _, raw = _read(path)
    return hashlib.sha256(raw).hexdigest()


def confirmed_stances(path: Path | str = DEFAULT_LIBRARY_PATH) -> dict[str, dict]:
    data, _ = _read(path)
    return {item["id"]: item for item in data["stances"] if item["confirmed"]}


def prompt_for(
    platforms: Iterable[str],
    content_line: str | None = None,
    stance_ids: Iterable[str] | None = None,
    path: Path | str = DEFAULT_LIBRARY_PATH,
) -> str:
    """Render confirmed style and explicitly selected stances, never guessed ones."""
    data, _ = _read(path)
    selected_platforms = list(dict.fromkeys(platform for platform in platforms if platform in PLATFORM_NAMES))
    if content_line is not None and content_line not in CONTENT_LINES:
        raise RuntimeError("内容线必须为 A、B 或 C")
    chunks = [
        "【作者文风规则｜样本库 " + data["version"] + "｜规则 " + data["rules_version"] + "】\n"
        + "\n\n".join(data["author_kernel"])
    ]
    if content_line:
        line = data["content_lines"][content_line]
        chunks.append(f"【内容线 {content_line}｜{line['name']}】\n{line['prompt']}")
    voice = data["voice"]
    if voice["status"] == "confirmed":
        chunks.append("【作者完整原声｜只学语言特征】\n" + voice["text"])
    selected = data["voice_selected"]
    chunks.append(
        f"【作者选定文风样本｜{selected['id']}】\n"
        f"标题参考：{selected['title']}\n正文参考：\n" + "\n\n".join(selected["paragraphs"])
    )
    available = {item["id"]: item for item in data["stances"]}
    requested = list(dict.fromkeys(stance_ids or []))
    chosen = []
    for identifier in requested:
        stance = available.get(identifier)
        if not isinstance(stance, dict) or not stance.get("confirmed"):
            raise RuntimeError("观点不存在或尚未 confirmed：" + identifier)
        chosen.append(stance)
    if chosen:
        chunks.append("【本次明确选择的作者观点｜不替代本次事实材料】\n" + "\n\n".join(
            f"{item['id']}\n观点：{item['statement']}\n理由：{item['reason']}" for item in chosen
        ))
    else:
        chunks.append("【观点边界】本次未选择观点 ID；不得替作者猜测或冒充作者立场，只根据本次材料写清有据判断。")
    chunks.append(
        "【样本使用边界】选定样本只用于学习语气、节奏和判断方式，不是本次选题的事实来源。"
        "不得复用样本的经历、数字、标题、具体结论或职业场景，除非本次材料明确提供。"
    )
    for platform in selected_platforms:
        matches = [sample for sample in data["platform_samples"] if sample["platform"] == platform and sample["enabled"]]
        if len(matches) > 1:
            raise RuntimeError(f"{PLATFORM_NAMES[platform]}最多只能有一篇启用的文风样稿")
        if matches:
            sample = matches[0]
            chunks.append(
                f"【{PLATFORM_NAMES[platform]}confirmed 平台样稿｜{sample['id']}】\n"
                f"标题参考：{sample['title']}\n正文参考：\n" + "\n\n".join(sample["paragraphs"])
            )
    return "\n\n".join(chunks)
