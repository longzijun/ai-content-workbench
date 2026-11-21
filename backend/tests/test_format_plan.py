"""Axis-B persistence: the workbench writes content_form, never the model.

No database, no network. These tests also cover the wiring inside
local_generation.generate_platforms, because the interesting invariant lives
there: with no pre-set decision the request and the resulting payload must be
byte-identical to the behaviour before this card.
"""
import json
from copy import deepcopy

import pytest

from backend.app import format_plan, formats, local_generation
from backend.app.platform_rules import NAMES

DECISION = format_plan.DECISION_KEY


def work(platform="wechat", **extra):
    base = {"platform": platform,
            "format": "note" if platform == "xiaohongshu" else "article",
            "title": "一个具体问题", "body": "先阅读原文并核对日期。",
            "tags": [], "image_prompts": [], "source_urls": []}
    base.update(extra)
    return base


def topic_with(adopted, platform="wechat", **extra):
    decision = {"platforms": {platform: {"adopted": adopted}}}
    topic = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": [platform], DECISION: decision}
    topic.update(extra)
    return topic


# --------------------------------------------------------------------------
# resolve 的四条优先级
# --------------------------------------------------------------------------

def test_no_decision_yields_empty_string():
    assert format_plan.resolve({"title": "x"}, "wechat", work()) == ""


def test_decision_is_applied():
    assert format_plan.resolve(topic_with("tutorial"), "wechat", work()) == "tutorial"


def test_existing_work_value_wins_over_the_decision():
    """人工在编辑器里指定的形式优先于预置决策。"""
    assert format_plan.resolve(topic_with("tutorial"), "wechat",
                               work(content_form="case")) == "case"


def test_unknown_decision_value_is_not_silently_rewritten():
    """未知形式归一为空串，绝不改写成某个「看起来最像」的形式。"""
    assert format_plan.resolve(topic_with("micro"), "wechat", work()) == ""


def test_decision_is_scoped_to_its_platform():
    topic = topic_with("tutorial", "wechat")
    assert format_plan.resolve(topic, "xiaohongshu", work("xiaohongshu")) == ""


def test_malformed_decision_payloads_are_ignored():
    """决策结构不对时按「没有决策」处理，不抛错 —— 决策是可选输入。"""
    for broken in (None, [], "tutorial", 42, {"platforms": None},
                   {"platforms": []}, {"platforms": {"wechat": None}},
                   {"platforms": {"wechat": "tutorial"}}):
        topic = {"title": "x", DECISION: broken}
        assert format_plan.resolve(topic, "wechat", work()) == ""


def test_decision_value_is_normalised():
    assert format_plan.resolve(topic_with("  TUTORIAL  "), "wechat", work()) == "tutorial"


def test_blank_existing_value_falls_through_to_the_decision():
    assert format_plan.resolve(topic_with("tutorial"), "wechat",
                               work(content_form="   ")) == "tutorial"


# --------------------------------------------------------------------------
# decision_for
# --------------------------------------------------------------------------

def test_decision_for_returns_the_platform_slice():
    decision = format_plan.decision_for(topic_with("brief", "toutiao"), "toutiao")
    assert decision == {"adopted": "brief"}


def test_decision_for_returns_empty_when_absent():
    assert format_plan.decision_for({"title": "x"}, "wechat") == {}


def test_decision_for_does_not_mutate_the_topic():
    topic = topic_with("tutorial")
    before = json.dumps(topic, sort_keys=True, ensure_ascii=False)
    format_plan.decision_for(topic, "wechat")
    assert json.dumps(topic, sort_keys=True, ensure_ascii=False) == before


# --------------------------------------------------------------------------
# resolve 不改传入的 work
# --------------------------------------------------------------------------

def test_resolve_never_mutates_the_work():
    original = work()
    snapshot = deepcopy(original)
    format_plan.resolve(topic_with("tutorial"), "wechat", original)
    assert original == snapshot


# --------------------------------------------------------------------------
# 落库接线：generate_platforms 里的注入
# --------------------------------------------------------------------------

def _stub_generation(monkeypatch, reply):
    """把模型调用换成固定回复，其余链路保持真实。"""
    calls = []

    def fake_stream(settings, request, cancel, progress, diagnostic):
        calls.append(request)
        return reply

    monkeypatch.setattr(local_generation, "stream_payload_with_retry", fake_stream)
    monkeypatch.setattr(local_generation, "stream_payload", fake_stream)
    return calls


def _reply(platform="wechat"):
    return json.dumps({"strategy": "依据资料写作", "works": [work(platform)]},
                      ensure_ascii=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    from backend.app import adapters, config

    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(adapters, "_loaded_local_model_info",
                        lambda _: {"id": "local/test", "capabilities": {}})
    return config.get_settings()


def run(world, monkeypatch, topic):
    _stub_generation(monkeypatch, _reply(topic["platforms"][0]))
    return local_generation.generate_platforms(topic, world, "local/test",
                                               lambda: False, lambda *a, **k: None)


def test_generation_writes_the_decided_form_into_the_payload(world, monkeypatch):
    payload, _ = run(world, monkeypatch, topic_with("tutorial"))
    assert payload["works"][0]["content_form"] == "tutorial"


def test_generation_leaves_content_form_empty_without_a_decision(world, monkeypatch):
    topic = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": ["wechat"]}
    payload, _ = run(world, monkeypatch, topic)
    assert payload["works"][0]["content_form"] == ""


def test_generation_payload_shape_is_unchanged_without_a_decision(world, monkeypatch):
    """无决策时 works 的键集合与改动前一致 —— 多出来的只能是 content_form 空串。"""
    topic = {"id": "t1", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": ["wechat"]}
    payload, _ = run(world, monkeypatch, topic)
    keys = set(payload["works"][0])
    assert keys - {"content_form"} == {
        "platform", "format", "title", "body", "tags", "image_prompts", "source_urls"}


def test_generation_request_carries_no_decision_without_one(world, monkeypatch):
    """无决策时不注入任何提示 —— 请求体与 V2-11 之前逐字节相同。

    V2-05 时这条断言的是「有无决策请求体都相同」；V2-11 起决策会以
    _format_hint 注入提示（那是本意），所以现在只断言「无决策时无变化」，
    「有决策时带提示」由 test_format_injection 覆盖。
    """
    calls = _stub_generation(monkeypatch, _reply())
    plain = {"id": "t", "revision": 1, "title": "选题", "brief": "",
             "sources": [], "platforms": ["wechat"]}
    for _ in range(2):
        local_generation.generate_platforms(dict(plain), world, "local/test",
                                            lambda: False, lambda *a, **k: None)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert "_format_hint" not in json.dumps(calls[0], ensure_ascii=False)


def test_generation_does_not_touch_the_checkpoint_file(world, monkeypatch):
    """检查点写在注入之前，重启恢复的 payload 必须仍是无决策形状。

    恢复路径需要 topic 带 _generation_run，否则 generate_platforms 不读检查点。
    """
    calls = _stub_generation(monkeypatch, _reply())
    topic = topic_with("tutorial", _generation_run={"id": "run-1"})
    local_generation.generate_platforms(topic, world, "local/test",
                                        lambda: False, lambda *a, **k: None)
    checkpoints = list((world.runtime_dir / "task-workspaces").rglob("wechat.json"))
    assert checkpoints, "应有逐平台检查点"
    saved = json.loads(checkpoints[0].read_text("utf-8"))
    assert "content_form" not in saved["works"][0], "检查点不应被注入污染"
    # 第二次运行从检查点恢复，注入仍会发生
    calls.clear()
    payload, _ = local_generation.generate_platforms(topic, world, "local/test",
                                                     lambda: False, lambda *a, **k: None)
    assert calls == [], "应从检查点恢复，不再请求模型"
    assert payload["works"][0]["content_form"] == "tutorial"


# --------------------------------------------------------------------------
# 落库：content_form 参与 hash
# --------------------------------------------------------------------------

def test_content_form_participates_in_the_frozen_hash():
    from backend.app.service import digest, encode

    base = {"strategy": "s", "works": [work()]}
    plain = digest(encode(base))
    with_form = digest(encode({**base, "works": [{**work(), "content_form": "tutorial"}]}))
    assert plain != with_form, "同一内容但叙事形式不同，hash 必须不同"


def test_legacy_payload_without_content_form_still_validates():
    from backend.app.schemas import Payload

    legacy = {"strategy": "s", "works": [work()]}
    assert "content_form" not in json.dumps(legacy)
    assert Payload.model_validate(legacy).works[0].content_form == ""


def test_written_form_survives_a_schema_round_trip(world, monkeypatch):
    from backend.app.schemas import Payload

    payload, _ = run(world, monkeypatch, topic_with("checklist"))
    frozen = Payload.model_validate(payload).model_dump()
    assert frozen["works"][0]["content_form"] == "checklist"


# --------------------------------------------------------------------------
# 告警接线
# --------------------------------------------------------------------------

def test_warnings_only_fire_when_a_form_was_actually_set():
    assert format_plan.warnings_for(work()) == []
    assert format_plan.warnings_for(work(content_form="micro"))


def test_warnings_fire_for_an_under_sourced_case():
    result = format_plan.warnings_for(work(content_form="case", source_urls=[]))
    assert any("案例拆解" in item for item in result)


def test_warnings_never_block():
    """告警只是提醒，不抛错 —— 阻断是 platform_rules 的职责，且只管 A 轴。"""
    for form in formats.CONTENT_FORM_IDS:
        assert isinstance(format_plan.warnings_for(work(content_form=form)), list)


# --------------------------------------------------------------------------
# 文件归属与红线
# --------------------------------------------------------------------------

def test_format_plan_does_not_import_database_or_network_layers():
    source = open(format_plan.__file__, encoding="utf-8").read()
    code = "\n".join(line for line in source.splitlines()
                      if not line.strip().startswith("#") and '"""' not in line)
    for forbidden in ("import db", "from .db", "import httpx", "from .adapters",
                      "sqlite", "transaction"):
        assert forbidden not in code


def test_local_generation_only_gained_the_decision_wiring():
    """本卡对 local_generation 的改动限于决策相关，别顺手改别的。

    V2-11 起又多了 hint 注入与检查点键并入决策，所以不再数固定次数；
    改为断言「只出现决策相关的调用」，且几处关键调用都在。
    """
    source = open(local_generation.__file__, encoding="utf-8").read()
    assert "import format_plan" in source
    # 2026-11-01：形式回填改为两通道共用的 apply_content_forms（内部仍走
    # resolve）。断言的是「决策接线还在」，不是某一次调用的写法。
    for expected in ("format_plan.apply_content_forms(", "format_plan.hint_for(",
                     "format_plan.DECISION_KEY"):
        assert expected in source, f"缺少 {expected}"
    # 决策之外的东西不该被引入
    for forbidden in ("import delivery", "from .delivery", "sync_draft"):
        assert forbidden not in source


def test_a_axis_is_untouched_by_the_decision():
    """写 B 轴不得影响 A 轴 —— 这是 V2 第一个架构判断的守门测试。"""
    assert local_generation.TITLE_LIMITS is not None
    payload_work = work()
    format_plan.resolve(topic_with("tutorial"), "wechat", payload_work)
    assert local_generation.validate_work(payload_work) is None, "A 轴校验通过"
    with_form = {**payload_work, "content_form": "micro"}
    assert local_generation.validate_work(with_form) is None, \
        "B 轴的非法值不得影响 A 轴校验结果"


def test_decision_key_is_namespaced_to_avoid_collisions():
    assert DECISION.startswith("_")
    assert DECISION not in work()
    assert DECISION not in {"id", "title", "brief", "sources", "platforms"}


def test_platform_names_are_not_duplicated_in_format_plan():
    """平台中文名只有一个来源。"""
    assert "公众号" not in open(format_plan.__file__, encoding="utf-8").read()
    assert NAMES["wechat"] == "公众号"

def test_apply_content_forms_is_shared_by_both_generation_channels():
    """人工交接与本地通道共用同一段形式回填，避免一条通道落空串。

    交接过导入时模型不会自填 content_form（受约束解码物理上不输出该键），
    若不在落库前补，作品会显示「未指定叙事形式」而正文其实按形式写成。
    """
    topic = topic_with("case")
    payload = {"strategy": "s", "works": [work(), work(content_form="tutorial")]}
    touched = format_plan.apply_content_forms(topic, payload, "wechat")
    assert payload["works"][0]["content_form"] == "case"
    assert payload["works"][1]["content_form"] == "tutorial", "人工指定优先，不被决策覆盖"
    assert touched == ["wechat"]
