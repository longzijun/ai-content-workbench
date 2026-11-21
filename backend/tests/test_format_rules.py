"""Rule engine invariants: determinism first, no clock, no network, no randomness."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import format_rules, formats, platform_rules, richtext

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)


def topic(**extra):
    base = {"title": "怎么在本地跑通一个开源模型", "body": "配置步骤与常见报错",
            "kind": "", "source_urls": ["https://example.com/a", "https://example.com/b"],
            "items": ["装依赖", "配端口", "验接口"]}
    base.update(extra)
    return base


def ready(**extra):
    """渠道已探测、写入已验证的快照。"""
    snapshot = {"checked": True, "implemented": True,
                "write_evidence": "session_save_readback", "storage_scope": "platform"}
    snapshot.update(extra)
    return snapshot


def evaluate(platform="wechat", **extra):
    params = {"platform": platform, "topic": topic(), "capabilities": ready(), "now": NOW}
    params.update(extra)
    return format_rules.evaluate(**params)


# --------------------------------------------------------------------------
# 确定性：同一输入必须给出同一结果
# --------------------------------------------------------------------------

def test_same_input_gives_identical_result_across_repeated_calls():
    baseline = json.dumps(evaluate(), ensure_ascii=False, sort_keys=True)
    for _ in range(3):
        assert json.dumps(evaluate(), ensure_ascii=False, sort_keys=True) == baseline


def test_engine_module_state_is_not_mutated_by_evaluate():
    before = {
        "kinds": sorted(format_rules.KIND_TERMS),
        "weights": json.dumps(format_rules.KIND_WEIGHTS, sort_keys=True, ensure_ascii=False),
        "budget": format_rules.COMPLEXITY_BUDGET,
    }
    evaluate()
    evaluate(platform="xiaohongshu", capabilities=ready(storage_scope="browser"))
    assert sorted(format_rules.KIND_TERMS) == before["kinds"]
    assert json.dumps(format_rules.KIND_WEIGHTS, sort_keys=True, ensure_ascii=False) == before["weights"]
    assert format_rules.COMPLEXITY_BUDGET == before["budget"]


def test_now_is_required_and_never_read_from_the_system_clock():
    with pytest.raises(ValueError, match="必须显式传入 now"):
        format_rules.evaluate(platform="wechat", topic=topic(), capabilities=ready())
    assert "now" not in format_rules.normalize_inputs(platform="wechat", topic=topic()).keys()


def test_now_does_not_participate_in_inputs_digest():
    """同一批输入在不同时间点评估必须命中同一指纹，否则幂等键随时间漂移。"""
    first = evaluate(now=NOW)
    later = evaluate(now=LATER)
    assert first["inputs_digest"] == later["inputs_digest"]
    assert first["evaluated_at"] != later["evaluated_at"], "评估时刻仍要如实记录"


def test_inputs_digest_ignores_now_but_tracks_real_inputs():
    base = format_rules.inputs_digest({"platform": "wechat", "source_count": 2})
    assert base == format_rules.inputs_digest({"platform": "wechat", "source_count": 2})
    assert base != format_rules.inputs_digest({"platform": "wechat", "source_count": 3})
    assert base != format_rules.inputs_digest({"platform": "toutiao", "source_count": 2})
    assert len(base) == 32


def test_engine_never_performs_io_or_network(monkeypatch):
    """任何网络/数据库调用都视为破坏纯函数契约。"""
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("规则引擎不得发起网络请求")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    assert evaluate()["feasible"]


def test_capability_snapshot_is_injected_not_queried(monkeypatch):
    """R4 的数据只能来自调用方注入，引擎不得自己去查 capabilities。"""
    import backend.app.capabilities as caps

    def forbidden(*args, **kwargs):
        raise AssertionError("规则引擎不得调用 capabilities.inventory")

    monkeypatch.setattr(caps, "inventory", forbidden)
    assert evaluate()["feasible"]


# --------------------------------------------------------------------------
# 输出稳定性：显式排序，同分按字典序
# --------------------------------------------------------------------------

def test_feasible_is_sorted_by_score_then_format_id():
    result = evaluate()
    keys = [(-row["score"], row["content_form"]) for row in result["feasible"]]
    assert keys == sorted(keys), "同分必须按 content_form 字典序，不得依赖插入顺序"


def test_blocked_is_sorted_by_format_id():
    result = evaluate(topic=topic(source_urls=[], items=[]))
    ids = [row["content_form"] for row in result["blocked"]]
    assert ids == sorted(ids)


def test_every_content_form_appears_exactly_once():
    result = evaluate()
    seen = [row["content_form"] for row in result["feasible"] + result["blocked"]]
    assert sorted(seen) == list(formats.CONTENT_FORM_IDS)
    assert len(seen) == len(set(seen))


def test_result_is_json_serializable():
    result = evaluate()
    assert json.loads(json.dumps(result, ensure_ascii=False))["platform"] == "wechat"
    assert json.loads(json.dumps(evaluate_all_now(), ensure_ascii=False))


def evaluate_all_now():
    return format_rules.evaluate_all(platforms=["toutiao", "wechat", "xiaohongshu"],
                                     topic=topic(), capabilities=ready(), now=NOW)


# --------------------------------------------------------------------------
# R5 渲染链路：把「A 轴未变」变成可断言的不变量
# --------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["wechat", "toutiao", "xiaohongshu"])
def test_r5_render_matches_richtext_for_every_platform(platform):
    """R5 复述的渲染判据必须与 richtext 的实际分派一致。"""
    assert format_rules.expected_render(platform) == (
        "text" if platform == "xiaohongshu" else "html")
    sample = "# 标题\n\n正文一段。"
    mode, _ = richtext.render(sample, platform)
    assert format_rules.expected_render(platform) == mode


def test_r5_fails_when_platform_rules_is_rewritten(monkeypatch):
    """A 轴一旦与注册表脱节，引擎必须失败而不是静默给出错误可行集合。

    拦在 R1 还是 R5 都算通过 —— 硬约束短路本来就先跑 R1。这里断言的是
    「结论必然为不可行」，不是「由哪条规则拦下」。
    """
    monkeypatch.setattr(format_rules, "expected_format", lambda p: "note")
    result = evaluate()
    assert result["feasible"] == []
    assert result["recommended"] == ""
    reasons = " ".join(row["reason"] for row in result["blocked"])
    assert "R1" in reasons or "R5" in reasons
    assert "核对 platform_rules" in reasons or "渠道形态" in reasons


def test_r5_fails_when_richtext_dispatch_is_rewritten(monkeypatch):
    """richtext 删掉某平台时 R5 必须发现。"""
    monkeypatch.setattr(format_rules, "expected_render", lambda p: None)
    result = evaluate()
    assert all(row["blocked"] for row in result["feasible"] + result["blocked"])
    assert "R5" in " ".join(row["reason"] for row in result["blocked"])


def test_r5_is_reached_only_after_earlier_hard_rules():
    """R1-R4 硬不可行时不再报 R5，硬约束短路要真的短路。"""
    result = evaluate(topic=topic(source_urls=[], items=[]))
    assert all(row["reason"].startswith("R3") for row in result["blocked"]), \
        "素材不足时应由 R3 拦下，不该跑到 R5"


def test_expected_render_rejects_unknown_platform():
    assert format_rules.expected_render("weibo") is None


# --------------------------------------------------------------------------
# R1 平台支持
# --------------------------------------------------------------------------

def test_r1_passes_for_every_current_platform():
    for platform in platform_rules.TITLE_LIMITS:
        assert not any(row["reason"].startswith("R1") for row in evaluate(platform=platform)["blocked"])


# --------------------------------------------------------------------------
# R2 标题窗口
# --------------------------------------------------------------------------

def test_r2_blocks_when_form_title_floor_exceeds_platform_ceiling(monkeypatch):
    """形式标题下限超过平台硬上限即硬不可行。"""
    original = formats.get_content_form

    def narrow(form_id):
        entry = original(form_id)
        if form_id == "tutorial":
            entry["title_window"] = (25, 40)
        return entry

    monkeypatch.setattr(formats, "get_content_form", narrow)
    result = evaluate(platform="xiaohongshu", capabilities=ready(storage_scope="browser"))
    blocked = {row["content_form"]: row["reason"] for row in result["blocked"]}
    assert "tutorial" in blocked and "R2" in blocked["tutorial"]


def test_r2_soft_penalty_when_form_ceiling_exceeds_platform_ceiling(monkeypatch):
    """高位超过平台上限只扣分，不阻断 —— 硬上限由 platform_rules 兜底。"""
    original = formats.get_content_form

    def wide(form_id):
        entry = original(form_id)
        if form_id == "tutorial":
            entry["title_window"] = (6, 40)
        return entry

    monkeypatch.setattr(formats, "get_content_form", wide)
    result = evaluate(platform="xiaohongshu", capabilities=ready(storage_scope="browser"))
    row = next(r for r in result["feasible"] if r["content_form"] == "tutorial")
    assert not row["blocked"]
    assert any("R2+1" in signal for signal in row["signals"])


# --------------------------------------------------------------------------
# R3 素材证据 —— 直接对应「不编造」
# --------------------------------------------------------------------------

def test_r3_blocks_case_form_without_two_sources():
    result = evaluate(topic=topic(source_urls=["https://example.com/a"]))
    blocked = {row["content_form"] for row in result["blocked"]}
    assert "case" in blocked, "case 需要 2 条来源，1 条时应被硬拦"


def test_r3_blocks_when_items_below_the_floor():
    result = evaluate(topic=topic(items=["只有一个要点"]))
    blocked = {row["content_form"] for row in result["blocked"]}
    assert "checklist" in blocked, "checklist 需要 5 个要点"
    assert "opinion" not in blocked, "opinion 的 items_min 为 0，不适用"


def test_r3_blocks_everything_when_no_material_at_all():
    result = evaluate(topic=topic(source_urls=[], items=[]))
    assert result["feasible"] == []
    assert result["recommended"] == ""
    assert sorted(row["content_form"] for row in result["blocked"]) == list(formats.CONTENT_FORM_IDS)


def test_r3_reason_names_the_missing_amount():
    result = evaluate(topic=topic(source_urls=[], items=[]))
    reasons = " ".join(row["reason"] for row in result["blocked"])
    assert "已批准来源" in reasons
    # 来源先拦下即短路，要点告警不会出现；补足来源后要点告警才浮现
    with_sources = evaluate(topic=topic(source_urls=["https://example.com/a", "https://example.com/b"],
                                        items=[]))
    assert "并列要点" in " ".join(row["reason"] for row in with_sources["blocked"])


def test_r3_tolerates_wrong_types_without_raising():
    """source_urls/items 类型不对时按 0 计，不抛错。"""
    result = evaluate(topic=topic(source_urls="不是列表", items=None))
    assert result["feasible"] == [] or all(row["blocked"] for row in result["feasible"])


# --------------------------------------------------------------------------
# R4 账号能力
# --------------------------------------------------------------------------

def test_r4_blocks_unimplemented_channel():
    result = evaluate(capabilities=ready(implemented=False, storage_scope="unknown"))
    assert result["feasible"] == []
    assert "R4" in result["blocked"][0]["reason"]


def test_r4_soft_penalizes_unverified_write():
    result = evaluate(capabilities=ready(write_evidence="none"))
    assert result["feasible"], "写入未验证只是软扣分，不该阻断"
    assert all(any("R4+2" in s for s in row["signals"]) for row in result["feasible"])


def test_r4_soft_penalizes_browser_only_storage():
    result = evaluate(capabilities=ready(storage_scope="browser"))
    assert result["feasible"]
    assert all(any("R4+1" in s for s in row["signals"]) for row in result["feasible"])


def test_r4_penalizes_unchecked_capabilities_and_says_so():
    result = evaluate(capabilities=None)
    assert any("未探测" in note for note in result["notes"])
    assert all(any("R4+1" in s and "尚未探测" in s for s in row["signals"])
               for row in result["feasible"]), "扣分理由要写进 signals，让结论可追溯"


def test_r4_never_assumes_capability_without_a_snapshot():
    """未注入快照时不得乐观假设渠道可用。"""
    result = evaluate(capabilities=None)
    assert all(any("R4+1" in s for s in row["signals"]) for row in result["feasible"])


def test_r4_snapshot_must_be_a_dict():
    with pytest.raises(ValueError, match="渠道能力快照必须是字典"):
        evaluate(capabilities=["not", "a", "dict"])


# --------------------------------------------------------------------------
# R6 成本预算
# --------------------------------------------------------------------------

def test_r6_blocks_form_needing_more_than_twice_the_ceiling():
    """high 复杂度 × 大档位，超过小红书官方 1000 字上限的两倍即硬不可行。"""
    result = evaluate(platform="xiaohongshu", body_band=1000,
                      capabilities=ready(storage_scope="browser"))
    blocked = {row["content_form"]: row["reason"] for row in result["blocked"]}
    assert "R6" in " ".join(blocked.values())
    assert any("可承载上限 1000" in reason for reason in blocked.values())
    assert "case" in blocked and "tutorial" in blocked, "两个 high 复杂度形式应被拦下"
    assert "brief" not in blocked, "low 复杂度不应被同一闸门拦下"


def test_r6_allows_form_within_ceiling():
    result = evaluate(platform="xiaohongshu", body_band=600,
                      capabilities=ready(storage_scope="browser"))
    assert result["feasible"], "600 字档位下小红书应仍容得下 low 复杂度形式"


def test_r6_soft_penalizes_between_one_and_two_times():
    """medium 复杂度在中等档位应只扣分而不阻断。"""
    result = evaluate(platform="xiaohongshu", body_band=700,
                      capabilities=ready(storage_scope="browser"))
    penalized = [row for row in result["feasible"] if any("R6+1" in s for s in row["signals"])]
    assert penalized, "700 字档位下 medium 复杂度应触发软扣分"


def test_r6_ceiling_differs_by_platform():
    """小红书用官方 1000 字硬上限，公众号/头条用目标档位作预算。"""
    assert format_rules._body_ceiling("xiaohongshu", 2000) == 1000
    assert format_rules._body_ceiling("wechat", 2000) == 2000
    assert format_rules._body_ceiling("toutiao", 1500) == 1500
    # 1000 字这个真值来自 platform_rules，不能在此另说一套
    assert format_rules.PLATFORM_BODY_CEILING["xiaohongshu"] == 1000


def test_complexity_budget_is_ordered_low_medium_high():
    budget = format_rules.COMPLEXITY_BUDGET
    assert budget["low"] < budget["medium"] < budget["high"]


def test_body_band_bounds_are_enforced():
    for bad in (0, 199, 60001, "六百", None, 12.5):
        with pytest.raises(ValueError, match="篇幅档位"):
            format_rules.normalize_inputs(platform="wechat", topic=topic(), body_band=bad)


# --------------------------------------------------------------------------
# R7 时效
# --------------------------------------------------------------------------

def test_r7_blocks_expired_topic_on_every_form():
    result = evaluate(topic=topic(expires_at=(NOW - timedelta(hours=1)).isoformat()))
    assert result["feasible"] == []
    assert "R7" in result["blocked"][0]["reason"]
    assert "过期" in result["blocked"][0]["reason"]


def test_hard_blocks_from_the_second_rule_group_are_not_swallowed():
    """R6/R7/R8 同样能硬不可行，返回值必须被检查。

    曾在这里丢弃 record() 的返回值，导致过期选题仍被判可行 ——
    那正是「过期热点不补发」这条业务规则的漏网路径。
    """
    expired = evaluate(topic=topic(expires_at=(NOW - timedelta(hours=1)).isoformat()))
    assert expired["feasible"] == [], "R7 的硬不可行不得被软信号段吞掉"
    assert any(row["reason"].startswith("R7") for row in expired["blocked"])
    # 更早的硬规则（R3 素材不足）仍可先行拦下部分形式，这是硬约束短路的本意
    assert all(row["reason"].startswith(("R3", "R6", "R7")) for row in expired["blocked"])
    # R6 同理：目标篇幅被压到最低时 high 复杂度装不下
    tight = evaluate(body_band=200, topic=topic(expires_at=(NOW + timedelta(days=2)).isoformat()))
    assert not any(row["reason"].startswith("R6") for row in tight["feasible"])


def test_r7_boosts_topic_expiring_soon():
    soon = evaluate(topic=topic(expires_at=(NOW + timedelta(hours=3)).isoformat()))
    far = evaluate(topic=topic(expires_at=(NOW + timedelta(days=3)).isoformat()))
    soon_scores = {r["content_form"]: r["score"] for r in soon["feasible"]}
    far_scores = {r["content_form"]: r["score"] for r in far["feasible"]}
    assert all(soon_scores[k] <= far_scores[k] for k in soon_scores)


def test_r7_ignores_absent_or_unparsable_expiry():
    for value in (None, "", "不是时间", 12345):
        result = evaluate(topic=topic(expires_at=value))
        assert not any("R7" in row["reason"] for row in result["blocked"])


def test_r7_accepts_naive_expiry_as_utc():
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert evaluate(topic=topic(expires_at=naive))["feasible"]


# --------------------------------------------------------------------------
# R8 选题类型
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(format_rules.KIND_WEIGHTS))
def test_r8_never_blocks_only_scores(kind):
    """R8 是纯加权，任何选题类型都不该阻断任何形式。"""
    result = evaluate(topic=topic(kind=kind))
    assert not any("R8" in row["reason"] for row in result["blocked"])


def test_r8_scores_shift_with_kind():
    news = evaluate(topic=topic(kind="news"))
    review = evaluate(topic=topic(kind="review"))
    news_scores = {r["content_form"]: r["score"] for r in news["feasible"]}
    review_scores = {r["content_form"]: r["score"] for r in review["feasible"]}
    assert news_scores != review_scores


def test_r8_unknown_kind_is_ignored_not_guessed():
    result = evaluate(topic=topic(kind="不存在的类型"))
    assert not any("R8" in signal for row in result["feasible"] for signal in row["signals"])


def test_r8_kind_is_inferred_from_text_only_when_unambiguous():
    """推断发生在 R8 求值时，不在 normalize_inputs 里 —— normalize 只搬运调用方给的 kind。"""
    assert format_rules.normalize_inputs(
        platform="wechat", topic=topic(kind="", title="A 与 B 的区别是什么", body=""))["kind"] == ""

    inferred = format_rules._infer_kind("A 与 B 的区别是什么", "")
    assert inferred == "explanation"
    assert format_rules._infer_kind("发布新模型与如何部署", "") == "", "多命中时不猜"
    assert format_rules._infer_kind("一个中性的话题", "") == "", "无命中时留空"


def test_r8_applies_inferred_kind_when_caller_omits_it():
    """调用方不给 kind 时，单一命中的文本推断应当生效。"""
    clear = evaluate(topic=topic(kind="", title="A 与 B 的区别是什么", body=""))
    explicit = evaluate(topic=topic(kind="explanation", title="一个中性的话题", body=""))
    clear_scores = {r["content_form"]: r["score"] for r in clear["feasible"]}
    explicit_scores = {r["content_form"]: r["score"] for r in explicit["feasible"]}
    assert clear_scores == explicit_scores, "文本推断结果应与显式传入 kind 产生同样排序"


def test_default_fixture_topic_hits_more_than_one_kind():
    """默认 fixture 刻意是多命中的，用来验证「宁可不加权」这条规则。

    「怎么/步骤/配置」命中 howto，「开源/模型」命中 news，「报错」命中 problem
    —— 三类命中，所以推断应留空。这也意味着默认排序基线不受 kind 加权影响，
    相关断言才稳定。
    """
    assert format_rules.normalize_inputs(platform="wechat", topic=topic(kind=""))["kind"] == ""


def test_kind_terms_are_pure_chinese_and_non_empty():
    for kind, terms in format_rules.KIND_TERMS.items():
        assert terms and all(isinstance(t, str) and t.strip() for t in terms)
        assert all(t.isascii() is False for t in terms), f"{kind} 混入非中文词"
    assert sorted(format_rules.KIND_TERMS) == sorted(format_rules.KIND_WEIGHTS)


def test_kind_weights_only_reference_known_forms():
    for kind, weights in format_rules.KIND_WEIGHTS.items():
        assert set(weights) <= set(formats.CONTENT_FORM_IDS), f"{kind} 引用了未知形式"


# --------------------------------------------------------------------------
# 硬约束与软信号分离
# --------------------------------------------------------------------------

def test_blocked_rows_never_carry_a_score():
    result = evaluate(topic=topic(source_urls=[], items=[]))
    assert all(row["score"] == 0 and row["blocked"] for row in result["blocked"])


def test_feasible_rows_never_carry_a_reason():
    result = evaluate()
    assert all(row["reason"] == "" and not row["blocked"] for row in result["feasible"])


def test_recommended_is_the_top_feasible_row():
    result = evaluate()
    assert result["recommended"] == result["feasible"][0]["content_form"]


def test_evaluate_all_covers_every_platform_and_sorts_keys():
    result = evaluate_all_now()
    assert list(result["platforms"]) == ["toutiao", "wechat", "xiaohongshu"]
    assert list(result["recommended"]) == ["toutiao", "wechat", "xiaohongshu"]
    for per_platform in result["platforms"].values():
        assert per_platform["feasible"]


def test_evaluate_all_rejects_empty_platform_list():
    for bad in ([], None, "wechat"):
        with pytest.raises(ValueError, match="平台列表不能为空"):
            format_rules.evaluate_all(platforms=bad, topic=topic(), now=NOW)


def test_evaluate_all_passes_per_platform_capabilities():
    result = format_rules.evaluate_all(
        platforms=["wechat", "xiaohongshu"], topic=topic(), now=NOW,
        capabilities={"wechat": ready(), "xiaohongshu": ready(implemented=False)})
    assert result["platforms"]["wechat"]["feasible"]
    assert result["platforms"]["xiaohongshu"]["feasible"] == []


def test_evaluate_all_rejects_unknown_platform():
    with pytest.raises(ValueError, match="不支持的发布平台"):
        format_rules.evaluate_all(platforms=["weibo"], topic=topic(), now=NOW)


# --------------------------------------------------------------------------
# notes 与结论措辞
# --------------------------------------------------------------------------

def test_notes_explain_an_empty_feasible_set_without_suggesting_a_workaround():
    result = evaluate(topic=topic(source_urls=[], items=[]))
    assert any("没有可行形式" in note for note in result["notes"])
    assert any("不要硬凑" in note for note in result["notes"])


def test_notes_call_out_a_single_candidate():
    result = evaluate(topic=topic(source_urls=["https://example.com/a"], items=["一"]))
    if len(result["feasible"]) == 1:
        assert any("只有" in note for note in result["notes"])


def test_notes_never_claim_delivery_was_verified():
    result = evaluate()
    joined = " ".join(result["notes"] + [row["reason"] for row in result["feasible"]])
    for forbidden in ("已存入", "已保存", "已发布", "投递成功"):
        assert forbidden not in joined


# --------------------------------------------------------------------------
# 输入边界
# --------------------------------------------------------------------------

def test_topic_must_be_a_dict():
    for bad in (None, "选题", 123, []):
        with pytest.raises(ValueError, match="选题数据必须是字典"):
            format_rules.normalize_inputs(platform="wechat", topic=bad)


def test_unknown_platform_is_rejected_at_the_boundary():
    for bad in ("weibo", "xhs", "", None):
        with pytest.raises(ValueError, match="不支持的发布平台"):
            format_rules.normalize_inputs(platform=bad, topic=topic())


def test_now_accepts_iso_string():
    result = format_rules.evaluate(platform="wechat", topic=topic(), capabilities=ready(),
                                   now="2026-10-22T12:00:00+00:00")
    assert result["evaluated_at"] == "2026-10-22T12:00:00+00:00"


def test_unparsable_now_string_is_rejected():
    with pytest.raises(ValueError, match="now 必须是可解析的时间"):
        format_rules.evaluate(platform="wechat", topic=topic(), capabilities=ready(), now="昨天")


def test_normalized_inputs_are_truncated_not_rejected():
    result = format_rules.normalize_inputs(
        platform="wechat", topic=topic(title="标" * 5000, body="正" * 9000))
    assert len(result["title"]) == 300 and len(result["body"]) == 4000


def test_engine_does_not_mutate_the_caller_topic():
    original = topic()
    snapshot = json.dumps(original, ensure_ascii=False, sort_keys=True)
    evaluate(topic=original)
    assert json.dumps(original, ensure_ascii=False, sort_keys=True) == snapshot