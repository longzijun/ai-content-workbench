"""Isolated review-policy behavior; no account/browser/network access."""
from copy import deepcopy
import json

import pytest

from backend.app.platform_source_policy import get_platform_policy, review_source_policy


def work(platform="wechat", body="来源：百度。本文梳理现有公开信息。", **extra):
    return {"platform": platform, "title": "一个事实问题", "body": body, "image_prompts": [], **extra}


def codes(result):
    return {item["code"] for item in result["findings"]}


@pytest.mark.parametrize("platform", ["wechat", "toutiao", "xiaohongshu", "xhs"])
def test_platform_rules_have_first_party_links_and_distinguish_ui_unknowns(platform):
    result = get_platform_policy(platform)
    assert result["checked_at"] == "2026-10-12"
    assert result["prompt_rules"] and result["official_sources"] and result["unknowns"]
    assert result["native_ai_label_verified"] is False
    assert all(source["url"].startswith("https://") and source["scope"] and source["verification"] for source in result["official_sources"])
    if platform == "xhs":
        assert result["platform"] == "xiaohongshu"
    json.dumps(result, ensure_ascii=False)


def test_policy_return_is_not_shared_mutable_configuration():
    result = get_platform_policy("wechat")
    result["prompt_rules"].clear()
    result["official_sources"][0]["url"] = "https://invalid.example/"
    assert get_platform_policy("wechat")["prompt_rules"]
    assert "invalid.example" not in get_platform_policy("wechat")["official_sources"][0]["url"]


def test_neutral_baidu_name_alone_is_not_link_promotion_violation():
    result = review_source_policy(work(), [])
    assert result["requires_review"] is False
    assert result["findings"] == []
    assert result["private_draft_allowed"] is True and result["publication_authorized"] is False


@pytest.mark.parametrize("url", [
    "https://www.baidu.com/s?wd=event",
    "https://top.baidu.com/board?tab=realtime",
    "https://www.toutiao.com/trending/7695197376592298531/",
    "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
])
def test_hotspot_and_search_entry_is_fact_gap_without_promotion_finding(url):
    result = review_source_policy(work(body="来源：今日头条或百度公开线索。"), [{"title": "线索", "url": url}])
    assert codes(result) == {"fact_gap"}
    assert result["requires_review"] is True


def test_internal_attribution_never_becomes_body_link_and_is_preserved_without_mutation():
    original = [{"title": "官方公告", "url": "https://openai.com/index/example", "published_at": None, "summary": "待核问题", "metadata": {"publisher": "openai"}}]
    expected = deepcopy(original)
    result = review_source_policy(work(), original)
    assert result["requires_review"] is False
    assert result["internal_evidence_preserved"] is True and result["internal_sources"] == expected
    result["internal_sources"][0]["metadata"]["publisher"] = "changed"
    assert original == expected


@pytest.mark.parametrize("platform,body", [
    ("wechat", "出处见 https://openai.com/index/report 。"),
    ("toutiao", "原文：https://mp.weixin.qq.com/s/example"),
    ("xhs", "来源链接：www.bbc.com/news/articles/example"),
])
def test_external_citation_needs_review_without_claiming_it_is_banned(platform, body):
    result = review_source_policy(work(platform, body), [])
    assert codes(result) == {"external_link_review"}
    assert "区分中性引用" in result["findings"][0]["message"]
    assert result["private_draft_allowed"] is True


@pytest.mark.parametrize("platform,body", [
    ("wechat", "站内资料：https://mp.weixin.qq.com/s/example"),
    ("toutiao", "站内文章：https://www.toutiao.com/article/7695197376592298531/"),
    ("xhs", "社区公约：https://pgy.xiaohongshu.com/help/detail?id=example"),
])
def test_own_platform_url_is_not_cross_platform_link_warning(platform, body):
    assert "external_link_review" not in codes(review_source_policy(work(platform, body), []))


@pytest.mark.parametrize("body", ["关注我的公众号获取资料", "微信号：example", "前往小红书主页联系作者", "扫码关注领取文档"])
def test_accounts_and_calls_to_cross_platform_action_require_human_review(body):
    result = review_source_policy(work(body=body), [])
    assert "cross_platform_account_review" in codes(result)
    assert result["publication_authorized"] is False


def test_qr_in_image_prompt_requires_actual_destination_review():
    result = review_source_policy(work(image_prompts=["页面底部放一个二维码"]), [])
    assert "qr_destination_review" in codes(result)
    assert result["private_draft_allowed"] is True


def test_image_prompts_do_not_pretend_actual_images_were_generated():
    result = review_source_policy(work(image_prompts=["AI示意图：简洁实验室，不要文字"]), [])
    assert not codes(result)
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("asset", [{}, {"license": "unknown"}, {"license": "待核实"}, "unannotated-image"])
def test_actual_asset_unknown_license_always_requests_review(asset):
    assert "asset_rights_review" in codes(review_source_policy(work(), [], [asset]))


def test_attribution_license_needs_credit_but_credit_is_not_a_rights_approval():
    result = review_source_policy(work(), [], [{"license": "CC-BY-4.0"}])
    assert "asset_credit_review" in codes(result)
    result = review_source_policy(work(), [], [{"license": "CC-BY-4.0", "credit": "作者、原地址与许可声明"}])
    assert "asset_credit_review" not in codes(result)
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("field", ["is_ai_generated", "ai_generated", "generated_by_ai"])
def test_actual_ai_image_is_not_certified_by_workbench_caption_or_private_draft_save(field):
    result = review_source_policy(work(), [], [{"license": "creator-owned", field: True, "credit": "AI示意图"}])
    assert "ai_image_label_review" in codes(result)
    assert "平台原生AI声明" in result["findings"][0]["message"]
    assert result["publication_authorized"] is False


def test_xhs_commercial_rules_have_scope_and_not_blanket_ban_on_neutral_sources():
    policy = get_platform_policy("xhs")
    assert any("商业" in source["scope"] for source in policy["official_sources"])
    assert any("普通笔记" in item for item in policy["unknowns"])
    assert not review_source_policy(work("xhs", "据BBC公开报道，仍需核查原始事实。"), []) ["requires_review"]


@pytest.mark.parametrize("bad_work,bad_sources,bad_assets", [
    ([], [], None), (work(), {}, None), (work(), [], {}), (work("invalid"), [], None),
])
def test_invalid_contract_rejects_instead_of_silently_approving(bad_work, bad_sources, bad_assets):
    with pytest.raises(ValueError):
        review_source_policy(bad_work, bad_sources, bad_assets)
