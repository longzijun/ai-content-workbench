"""Advisor invariants: the enum is the gate, degradation is explicit, no silent fixing."""
import json

import pytest

from backend.app import format_advisor, format_rules, formats
from backend.app.format_advisor import AdvisorError

NOW = __import__("datetime").datetime(2026, 10, 22, 12, 0,
                                       tzinfo=__import__("datetime").timezone.utc)


def evaluation(platform="wechat", **overrides):
    """规则引擎的求值结果。overrides 只改指定字段，其余保持基线。"""
    topic = {"title": "怎么在本地跑通一个开源模型", "body": "配置步骤",
             "kind": "", "source_urls": ["https://example.com/a", "https://example.com/b"],
             "items": ["一", "二", "三"]}
    topic.update(overrides)
    return format_rules.evaluate(
        platform=platform, topic=topic, now=NOW,
        capabilities={"checked": True, "implemented": True,
                      "write_evidence": "session_save_readback", "storage_scope": "platform"})


def empty_evaluation():
    """素材全无 → 可行集合必空。"""
    return evaluation(source_urls=[], items=[])


def feasible_of(platform="wechat"):
    return [row["content_form"] for row in evaluation(platform)["feasible"]]


def good(recommended, **extra):
    payload = {"recommended": recommended, "confidence": 80,
               "reason": "这个形式贴合选题的操作属性", "alternatives": [],
               "questions_for_user": []}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def echo(text):
    """假的 ask_model：记录请求，返回预置文本。"""
    calls = []

    def ask(request):
        calls.append(request)
        return text

    ask.calls = calls
    return ask


@pytest.fixture(autouse=True)
def loaded_model(monkeypatch):
    """默认把模型名固定住。

    advise() 会经 _model_name 读 LM Studio 已加载实例，那是一次真实 HTTP
    探测 —— 测试里必须替掉，否则每个用例都在打本地服务，且探不通时全部
    落到 unavailable，测不到模型路径。
    """
    monkeypatch.setattr(format_advisor, "_model_name", lambda settings: "local/test-model")


_DEFAULT = object()


def advise(text=_DEFAULT, evaluation_result=None, **kwargs):
    """跑 advise() 的便捷包装。

    text 省略时用「模型返回合法结果」这条默认路径；显式传任何值（含 None）
    都按「模型返回了这个东西」处理，因此 None 也能测到。
    """
    result = evaluation_result if evaluation_result is not None else evaluation()
    if text is _DEFAULT:
        text = good(format_advisor.rule_ranked(result)[0])
    return format_advisor.advise(result, {"title": "t", "body": "b"},
                                 settings=object(), ask_model=echo(text), **kwargs)


# --------------------------------------------------------------------------
# schema：enum 由可行集合生成是本模块的核心设计
# --------------------------------------------------------------------------

def test_schema_enum_comes_from_the_feasible_set():
    """模型在受约束解码下物理上无法生成集合外的形式。"""
    feasible = feasible_of()
    schema = format_advisor._schema(feasible)
    assert schema["properties"]["recommended"]["enum"] == feasible
    assert schema["properties"]["alternatives"]["items"]["enum"] == feasible


def test_schema_enum_excludes_blocked_forms():
    """硬约束排除的形式绝不出现在 enum 里 —— 这是「不扩大集合」的机制保证。"""
    result = empty_evaluation()
    blocked = {row["content_form"] for row in result["blocked"]}
    assert blocked, "本用例前提是有形式被拦"
    schema = format_advisor._schema([r["content_form"] for r in result["feasible"]])
    allowed = set(schema["properties"]["recommended"]["enum"])
    assert not (allowed & blocked)


def test_schema_is_strict_all_the_way_down():
    """每一层 object 都必须关掉额外键，否则 strict 解码可能产出多余字段。"""
    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, "object 层必须关闭额外键"
            assert set(node.get("properties", {})) == set(node.get("required", [])), \
                "strict 要求 properties 与 required 完全一致"
        for value in node.values():
            walk(value)

    walk(format_advisor._schema(feasible_of()))


def test_schema_pins_alternatives_and_questions():
    schema = format_advisor._schema(feasible_of())
    assert schema["properties"]["alternatives"]["maxItems"] == format_advisor.MAX_ALTERNATIVES
    assert schema["properties"]["questions_for_user"]["maxItems"] == 3
    assert schema["properties"]["confidence"]["type"] == "integer"
    assert schema["properties"]["confidence"]["minimum"] == 0
    assert schema["properties"]["confidence"]["maximum"] == 100


def test_schema_enum_preserves_the_rule_engine_order():
    """enum 顺序跟随规则引擎的排序结果，推荐器不自行重排 —— 排序语义由规则引擎定义。"""
    feasible = feasible_of()
    schema = format_advisor._schema(feasible)
    assert schema["properties"]["recommended"]["enum"] == feasible
    assert schema["properties"]["recommended"]["enum"] == [
        row["content_form"] for row in evaluation()["feasible"]]


# --------------------------------------------------------------------------
# validate：四条约束
# --------------------------------------------------------------------------

def test_recommended_outside_feasible_set_is_rejected_wholesale():
    """越界整体丢弃，绝不静默纠正 —— 纠正会让理由与结论对不上。"""
    feasible = feasible_of()
    outside = next(f for f in formats.CONTENT_FORM_IDS if f not in feasible)
    with pytest.raises(AdvisorError, match="不在可行集合内"):
        format_advisor.validate({"recommended": outside, "confidence": 90, "reason": "编的",
                                 "alternatives": [], "questions_for_user": []}, feasible)


def test_validate_never_silently_substitutes_a_valid_form():
    """同一个 payload 里 recommended 越界时，不得悄悄改成 feasible[0]。"""
    feasible = feasible_of()
    outside = next(f for f in formats.CONTENT_FORM_IDS if f not in feasible)
    with pytest.raises(AdvisorError):
        format_advisor.parse(good(outside), feasible)


@pytest.mark.parametrize("value", [True, False, 1.0, "80", None, [], {}])
def test_confidence_must_be_a_real_int_not_bool(value):
    """bool 是 int 的子类，必须用 type(x) is not int 挡掉。"""
    feasible = feasible_of()
    with pytest.raises(AdvisorError, match="信心值必须是整数"):
        format_advisor.validate({"recommended": feasible[0], "confidence": value,
                                 "reason": "理由", "alternatives": [],
                                 "questions_for_user": []}, feasible)


@pytest.mark.parametrize("value", [-1, 101, 9999])
def test_confidence_bounds_are_checked_in_code_not_only_in_schema(value):
    """降级路径不强制 schema，边界必须在代码里独立校验。"""
    feasible = feasible_of()
    with pytest.raises(AdvisorError, match="超出"):
        format_advisor.validate({"recommended": feasible[0], "confidence": value,
                                 "reason": "理由", "alternatives": [],
                                 "questions_for_user": []}, feasible)


@pytest.mark.parametrize("payload", [
    "不是对象", 42, None, [],
    {"confidence": 80, "reason": "r", "alternatives": [], "questions_for_user": []},
    {"recommended": 123, "confidence": 80, "reason": "r", "alternatives": [], "questions_for_user": []},
    {"recommended": "brief", "confidence": 80, "alternatives": [], "questions_for_user": []},
    {"recommended": "brief", "confidence": 80, "reason": "  ", "alternatives": [], "questions_for_user": []},
    {"recommended": "brief", "confidence": 80, "reason": "r", "questions_for_user": []},
])
def test_malformed_payloads_are_rejected(payload):
    with pytest.raises(AdvisorError):
        format_advisor.validate(payload, feasible_of())


def test_alternatives_are_deduped_capped_and_exclude_the_recommendation():
    feasible = feasible_of()
    others = [f for f in feasible if f != feasible[0]]
    payload = {"recommended": feasible[0], "confidence": 80, "reason": "理由",
               "alternatives": [feasible[0]] + others * 4 + ["micro"],
               "questions_for_user": []}
    result = format_advisor.validate(payload, feasible)
    assert feasible[0] not in result["alternatives"]
    assert len(result["alternatives"]) <= format_advisor.MAX_ALTERNATIVES
    assert all(item in feasible for item in result["alternatives"])
    assert len(set(result["alternatives"])) == len(result["alternatives"])


def test_questions_are_trimmed_and_capped():
    feasible = feasible_of()
    payload = {"recommended": feasible[0], "confidence": 80, "reason": "理由",
               "alternatives": [],
               "questions_for_user": ["问题" + str(i) * 100 for i in range(6)]}
    result = format_advisor.validate(payload, feasible)
    assert len(result["questions_for_user"]) == 3
    assert all(len(item) <= 200 for item in result["questions_for_user"])


def test_validate_tolerates_missing_questions_key():
    """questions_for_user 缺失时按空处理，不算致命。"""
    feasible = feasible_of()
    result = format_advisor.validate({"recommended": feasible[0], "confidence": 80,
                                      "reason": "理由", "alternatives": []}, feasible)
    assert result["questions_for_user"] == []


def test_parse_repairs_mechanical_json_faults():
    """复用 local_generation 的小模型 JSON 修复，不另造一套。"""
    feasible = feasible_of()
    raw = ('{"recommended": "%s", "confidence": 80, '
           '"reason": "他说这是"最好的选择", "alternatives": [], '
           '"questions_for_user": []}' % feasible[0])
    assert format_advisor.parse(raw, feasible)["recommended"] == feasible[0]


def test_parse_raises_advisor_error_on_unrepairable_json():
    with pytest.raises(AdvisorError):
        format_advisor.parse("完全不是 JSON", feasible_of())


# --------------------------------------------------------------------------
# request 构造
# --------------------------------------------------------------------------

def test_request_uses_strict_schema_and_low_temperature():
    feasible = feasible_of()
    labels = {f: formats.get_content_form(f)["label"] for f in feasible}
    request = format_advisor.build_request(feasible, labels,
                                           {"title": "标题", "body": "说明"},
                                           model="local/test")
    assert request["temperature"] == format_advisor.ADVISOR_TEMPERATURE
    assert request["temperature"] < 0.4, "排序要稳定，不要文采"
    assert request["max_tokens"] == format_advisor.ADVISOR_MAX_TOKENS
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["recommended"]["enum"] == feasible


def test_request_lists_every_feasible_form_with_its_label():
    feasible = feasible_of()
    labels = {f: formats.get_content_form(f)["label"] for f in feasible}
    request = format_advisor.build_request(feasible, labels,
                                           {"title": "t", "body": "b"}, model="m")
    text = request["messages"][0]["content"]
    for form in feasible:
        assert form in text and labels[form] in text


def test_native_request_shape_matches_the_generation_contract():
    """native 分支用 'input' 键，stream_payload 据此判定协议。"""
    feasible = feasible_of()
    labels = {f: formats.get_content_form(f)["label"] for f in feasible}
    request = format_advisor.build_request(feasible, labels, {"title": "t", "body": "b"},
                                           model="m", native=True)
    assert 'input' in request and 'messages' not in request
    assert request['reasoning'] == 'off'
    assert request['max_output_tokens'] == format_advisor.ADVISOR_MAX_TOKENS


def test_request_instruction_forbids_fabrication():
    feasible = feasible_of()
    labels = {f: formats.get_content_form(f)["label"] for f in feasible}
    text = format_advisor.build_request(feasible, labels, {"title": "t", "body": "b"},
                                        model="m")["messages"][0]["content"]
    assert "不要编造" in text
    assert "不要提出集合外的形式" in text
    assert "questions_for_user" in text


# --------------------------------------------------------------------------
# 四级降级
# --------------------------------------------------------------------------

def test_empty_feasible_set_never_calls_the_model():
    """空集合是硬约束结论，让模型在没有选项时选只会诱导编造。"""
    calls = []

    def ask(request):
        calls.append(request)
        return good("brief")

    result = format_advisor.advise(
        empty_evaluation(), {"title": "t", "body": "b"},
        settings=object(), ask_model=ask)
    assert calls == [], "可行集合为空时一次模型请求都不该发"
    assert result["status"] == "rule_only"
    assert result["recommended"] == ""
    assert result["alternatives"] == []


def test_unavailable_model_degrades_to_rule_ranking():
    feasible = feasible_of()

    def boom(request):
        raise RuntimeError("连接被拒绝")

    result = format_advisor.advise(evaluation(), {"title": "t", "body": "b"},
                                   settings=object(), ask_model=boom)
    assert result["status"] == "unavailable"
    assert result["recommended"] == feasible[0]
    assert result["rule_ranked"] == feasible
    assert "连接被拒绝" in result["reason"]
    assert result["confidence"] == 0, "降级结论不得冒充有模型参与"


def test_invalid_model_output_degrades_to_rule_ranking():
    """无法解析的输出必须降级，而不是让 JSONDecodeError 冒泡。"""
    result = advise("不是 JSON")
    assert result["status"] == "failed"
    assert result["recommended"] == feasible_of()[0]
    assert "无法采信" in result["reason"]


@pytest.mark.parametrize("raw", ["", "   ", None, 123, "{", "[1,2"])
def test_unparsable_outputs_all_degrade(raw):
    """空输出、非字符串、截断 JSON 都要落到降级路径。"""
    result = advise(raw)
    assert result["status"] == "failed"
    assert result["confidence"] == 0


def test_parse_wraps_json_errors_into_advisor_error():
    """parse() 必须把解析失败统一成 AdvisorError，否则 advise 的捕获漏网。"""
    with pytest.raises(AdvisorError, match="没有返回内容|不是可解析的 JSON"):
        format_advisor.parse("不是 JSON", feasible_of())


def test_out_of_set_recommendation_degrades_rather_than_snapping():
    feasible = feasible_of()
    outside = next(f for f in formats.CONTENT_FORM_IDS if f not in feasible)
    result = advise(good(outside))
    assert result["status"] == "failed"
    assert result["recommended"] == feasible[0]
    assert result["confidence"] == 0
    assert "无法采信" in result["reason"]


def test_low_confidence_falls_back_to_rule_first_but_keeps_the_reason():
    feasible = feasible_of()
    result = advise(good(feasible[1], confidence=10))
    assert result["status"] == "local"
    assert result["recommended"] == feasible[0], "模型没把握时不该让它主导"
    assert result["confidence"] == 10, "模型自己的信心值如实保留"
    assert "低于" in result["note"]
    assert result["reason"], "模型理由保留供参考"


def test_high_confidence_keeps_the_model_pick():
    feasible = feasible_of()
    pick = feasible[-1]
    result = advise(good(pick, confidence=85))
    assert result["status"] == "local"
    assert result["recommended"] == pick
    assert result["rule_ranked"] == feasible, "规则排序始终随结论一起返回，便于追溯"


def test_missing_model_hook_degrades_without_asking():
    result = format_advisor.advise(evaluation(), {"title": "t", "body": "b"},
                                   settings=None, ask_model=None)
    assert result["status"] == "unavailable"


def test_unloaded_model_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(format_advisor, "_model_name",
                        lambda settings: (_ for _ in ()).throw(AdvisorError("未加载")))
    result = format_advisor.advise(evaluation(), {"title": "t", "body": "b"},
                                   settings=object(), ask_model=echo(good("brief")))
    assert result["status"] == "unavailable"
    assert "未加载" in result["reason"]


def test_advise_never_falls_back_to_codex(monkeypatch):
    """一次 200 token 的排序不值得拉起沙箱子进程，且会让审计记录混同。"""
    import backend.app.adapters as adapters

    def forbidden(*args, **kwargs):
        raise AssertionError("推荐器不得调用 Codex")

    monkeypatch.setattr(adapters, "_run_codex", forbidden)
    assert advise().get("status") in {"local", "unavailable", "failed"}
    source = open(format_advisor.__file__, encoding="utf-8").read()
    # docstring 里提到 _run_codex 是为了说明「为什么不用」，代码区不得真的调它
    code = "\n".join(line for line in source.splitlines()
                      if not line.strip().startswith("#") and '"""' not in line)
    assert "_run_codex(" not in code
    assert "generate_payload(" not in code
    assert "codex-last-success.json" in source, "不用 Codex 的理由要留在文档里"


def test_fallback_never_claims_the_model_was_used():
    for status in ("unavailable", "failed", "rule_only"):
        result = format_advisor.fallback(evaluation(), status, "detail")
        assert result["status"] == status
        assert result["confidence"] == 0
        assert result["rule_ranked"]


def test_advisor_result_is_json_serializable():
    feasible = feasible_of()
    result = advise(good(feasible[0]))
    assert json.loads(json.dumps(result, ensure_ascii=False))["status"] == "local"


def test_advisor_never_expands_the_feasible_set():
    """推荐结果永远落在规则引擎给出的集合内 —— 这是本模块的核心红线。"""
    feasible = feasible_of()
    for pick in feasible:
        result = advise(good(pick))
        assert result["recommended"] in feasible
        assert all(item in feasible for item in result["alternatives"])


def test_module_docstring_states_the_no_codex_rationale():
    source = open(format_advisor.__file__, encoding="utf-8").read()
    assert "codex-last-success.json" in source
    assert "不扩大可行集合" in source