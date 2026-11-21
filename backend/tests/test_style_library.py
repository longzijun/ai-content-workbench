"""C0: confirmed fictional style resources and explicit editorial selection."""
import copy
import hashlib
import json

import pytest

from backend.app import adapters, style_library


def test_bundled_example_resource_is_confirmed_and_voice_gap_is_explicit():
    data = style_library.load_library()
    assert data["version"] == "example-v1.0"
    assert data["rules_version"] == "example-editorial-rules-v1"
    assert data["voice"]["status"] == "missing"
    assert len(data["stances"]) == 3
    assert all(item["confirmed"] for item in data["stances"])
    assert data["voice_selected"]["id"] == "example-style-note"
    assert data["platform_samples"] == []


def test_example_sample_and_selected_content_line_are_always_explicit():
    text = style_library.prompt_for(["wechat"], "A")
    assert "example-style-note" in text
    assert "先拿几份练习文件试一次。" in text
    assert "本次走 A 线：实践工作流" in text
    assert "不是本次选题的事实来源" in text


def test_only_user_selected_confirmed_stances_are_injected():
    text = style_library.prompt_for(["wechat"], "B", ["example-check-the-result", "example-compare-same-input"])
    assert "工作流需要一个可核对的结果。" in text
    assert "比较工具时应使用相同的输入。" in text
    assert "复盘先整理执行记录。" not in text
    assert "example-keep-a-log" not in text


def test_no_stance_selection_never_lets_model_guess_author_position():
    text = style_library.prompt_for(["wechat"], "C")
    assert "本次未选择观点 ID" in text
    assert "不得替作者猜测或冒充作者立场" in text


def test_empty_platform_sample_set_is_valid_for_all_current_platforms():
    text = style_library.prompt_for(["wechat", "toutiao", "xiaohongshu"], "A")
    assert "example-style-note" in text
    assert "confirmed 平台样稿" not in text


def test_unconfirmed_or_unknown_stance_id_is_rejected():
    with pytest.raises(RuntimeError, match="观点不存在或尚未 confirmed"):
        style_library.prompt_for(["wechat"], "A", ["invented-by-model"])


def test_invalid_content_line_is_rejected_by_prompt_constructor():
    with pytest.raises(RuntimeError, match="内容线必须为 A、B 或 C"):
        style_library.prompt_for(["wechat"], "D")


def test_unconfirmed_library_cannot_enter_generation(tmp_path):
    data = style_library.load_library()
    data["status"] = "draft"
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="confirmed"):
        style_library.prompt_for(["wechat"], "A", path=path)


def test_at_most_one_enabled_sample_per_platform(tmp_path):
    data = copy.deepcopy(style_library.load_library())
    sample = {
        "id": "one", "platform": "wechat", "format": "article", "title": "样稿",
        "source_document": "作者确认", "confirmed": True, "enabled": True,
        "usage": "style_only", "paragraphs": ["只学语气。"],
    }
    data["platform_samples"] = [sample, {**sample, "id": "two"}]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="最多只能有一篇启用样稿"):
        style_library.prompt_for(["wechat"], "A", path=path)


def test_optional_platform_sample_is_injected_only_for_selected_platform(tmp_path):
    data = copy.deepcopy(style_library.load_library())
    data["platform_samples"] = [{
        "id": "wechat-confirmed", "platform": "wechat", "format": "article", "title": "样稿标题",
        "source_document": "作者确认", "confirmed": True, "enabled": True,
        "usage": "style_only", "paragraphs": ["这是一段只用于语气参考的样稿。"],
    }]
    path = tmp_path / "with-sample.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    selected = style_library.prompt_for(["wechat"], "A", path=path)
    other = style_library.prompt_for(["toutiao"], "A", path=path)
    assert "这是一段只用于语气参考的样稿。" in selected
    assert "这是一段只用于语气参考的样稿。" not in other


def test_fingerprint_uses_exact_resource_bytes(tmp_path):
    data = style_library.load_library()
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "style.json"
    path.write_bytes(compact)
    assert style_library.fingerprint(path) == hashlib.sha256(compact).hexdigest()
    path.write_bytes(compact + b"\n")
    assert style_library.fingerprint(path) == hashlib.sha256(compact + b"\n").hexdigest()


def test_full_prompt_precedes_topic_data_and_carries_explicit_choices():
    text = adapters._prompt({
        "title": "全新选题", "brief": "", "platforms": ["wechat"], "sources": [],
        "content_line": "B", "stance_ids": ["example-check-the-result"],
    })
    assert text.index("作者文风规则") < text.index("已批准选题数据")
    assert "本次走 B 线：工具实测" in text
    assert "工作流需要一个可核对的结果。" in text
    assert '"content_line": "B"' in text
    assert '"stance_ids": ["example-check-the-result"]' in text
