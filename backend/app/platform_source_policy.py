"""Source/asset review guidance grounded in first-party rules, not legal advice.

This module never approves publication or strips internal evidence. Findings
require human review, while permitting the existing private-draft workflow.
Platform features, account permissions and native AI switches remain distinct.
"""
from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse

from . import platform_registry


CHECKED_AT = "2026-10-12"
_ALIASES = {"xhs": "xiaohongshu"}
_HOSTS = {
    "wechat": {"mp.weixin.qq.com", "weixin.qq.com", "www.weixin.qq.com", "wx.qq.com", "wechat.com", "www.wechat.com"},
    "toutiao": {"toutiao.com", "www.toutiao.com", "mp.toutiao.com", "baike.toutiao.com"},
    "xiaohongshu": {"xiaohongshu.com", "www.xiaohongshu.com", "creator.xiaohongshu.com", "pgy.xiaohongshu.com", "ad.xiaohongshu.com"},
}
_COMMON_RULES = [
    "内部始终保留原题、来源地址、发布/抓取时间、摘要和待核问题；热点榜或搜索入口只证明线索出现，不证明事件事实。",
    "正文中性注明来源名称、原题和日期；转载、图片使用和跨平台链接分别核查，不以署名代替授权。",
    "跨平台关注、账号、二维码、私信领资料等导流内容先交人工审核；不把中性来源名称一概判违规。",
    "AI图片应明确为生成/示意内容并保留已有标识；发布时由用户核查平台声明功能，工作台文字记录不等于平台原生标识。",
]
_POLICIES = {
    "wechat": {
        "verified_rules": ["官方运营规范限制侵犯著作权和隐私的内容，限制奖励、强制等诱导关注或分享行为。", "规范5.3涉及微信平台信息内容的使用，公开可读不等于已获转载或商业采集授权。"],
        "unknowns": ["本轮未取得可完整核对的公众号专用AI标识操作细则或当前账号原生开关证据。", "公众号普通正文外链的可用范围及当前账号权限需在实际编辑器核查；不能据此宣布所有引用外链被禁止。"],
        "official_sources": [
            {"title": "微信公众平台运营规范", "url": "https://mp.weixin.qq.com/cgi-bin/readtemplate?t=business/faq_operation_tmpl&type=info&lang=zh_CN", "scope": "微信公众号运营", "verification": "public_official_page"},
            {"title": "关于印发《人工智能生成合成内容标识办法》的通知", "url": "https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm", "scope": "通用生成合成内容标识；非公众号界面说明", "verification": "public_regulator_page"},
        ],
    },
    "toutiao": {
        "verified_rules": ["社区规范限制未经授权使用他人作品、虚假信息、营销导流和旧闻误作新闻。", "图文帮助文档描述站内文章链接及扩展外链功能；文档存在不证明当前账号已具备功能或任意推广获准。", "头条安全中心公告要求主动标明AI生成并保留已有AI标识，涉及文本、图片、音频和视频。"],
        "unknowns": ["当前账号扩展外链功能与原生AI声明开关尚未在本模块核验。", "社会公共事务、健康及财经专业内容适用的主体资质与发布范围仍需人工核查。"],
        "official_sources": [
            {"title": "今日头条社区规范", "url": "https://baike.toutiao.com/detail/236/238/0?enter_from=left_navigation&tt_from=copy_link", "updated_at": "2025-12-01", "scope": "头条创作者", "verification": "public_official_page"},
            {"title": "图文创作", "url": "https://baike.toutiao.com/detail/211/212/214", "updated_at": "2025-02-19", "scope": "图文内链和扩展外链功能说明", "verification": "public_official_page"},
            {"title": "今日头条关于全面落实AI生成内容标识的重要通知", "url": "https://www.toutiao.com/article/7543937957493883411/", "updated_at": "2025-08-29", "scope": "头条安全中心公告", "verification": "official_search_excerpt"},
        ],
    },
    "xiaohongshu": {
        "verified_rules": ["社区公约2.0要求尊重原创、核实存疑来源；转载须取得同意并注明出处。", "社区公约2.0建议对AI生成或辅助的文案、图片、视频主动说明，可以在文字或图片简介等处说明。", "蒲公英商业内容审核和聚光广告规范限制站外联系方式、二维码及商业导流；这些条款有商业/广告适用范围。"],
        "unknowns": ["广告或专业号的限制不能直接解释为普通笔记所有学术/新闻来源名称或链接均违规。", "普通账号具体外链呈现和当前原生AI声明开关须用户在编辑器核查。"],
        "official_sources": [
            {"title": "小红书社区公约2.0", "url": "https://pgy.xiaohongshu.com/help/detail?id=1eda0a065dd894063c2e029a49e8f6a1&userType=4", "updated_at": "2026-07-31", "scope": "社区用户与经营者", "verification": "official_search_excerpt"},
            {"title": "蒲公英内容审核说明", "url": "https://pgy.xiaohongshu.com/help/detail?id=6495c527d1eedeeb48fb18b1f875650e&userType=4", "scope": "商业合作内容；非全部普通笔记", "verification": "official_search_excerpt"},
            {"title": "聚光广告审核规范", "url": "https://ad.xiaohongshu.com/next_help/docs/8dc5bd9c45c9a90cb9912f3400d43f92", "scope": "广告物料", "verification": "official_search_excerpt"},
        ],
    },
}
_URL = re.compile(r"https?://[^\s<>\"'\]\)）]+|www\.[A-Za-z0-9.-]+(?:/[^\s<>\"'\]\)）]*)?", re.I)
_CONTACT = re.compile(r"(?:微信号|公众号(?:账号|号)|QQ(?:号)?|邮箱|手机号|小红书(?:号|账号)|头条(?:号|账号))\s*[:：]\s*\S+", re.I)
_CTA = re.compile(r"(?:关注|搜索|添加|加我|移步|前往|转到|私信|联系|主页).{0,24}(?:公众号|微信|小红书|头条号|微博|QQ|站外|其他平台)|(?:公众号|微信|小红书|头条号|微博|QQ).{0,24}(?:关注|加我|领取|进群|私信|联系)|扫码.{0,20}(?:关注|添加|联系|进群|领取|获取|下载)", re.I)


def _platform(value):
    value = _ALIASES.get(value, value)
    if value not in platform_registry.PLATFORM_SET:
        raise ValueError("不支持的平台来源规则")
    return value


def get_platform_policy(platform):
    platform = _platform(platform)
    if platform_registry.is_video_platform(platform):
        return {
            "platform": platform, "checked_at": None, "verified_rules": [], "official_sources": [],
            "unknowns": ["该视频平台的具体发布规则、账号权限和原生 AI 标识开关尚未核验，请发布前人工核对。"],
            "prompt_rules": list(_COMMON_RULES), "cross_platform_promotion_default": "requires_review",
            "native_ai_label_verified": False,
        }
    result = deepcopy(_POLICIES[platform])
    result.update(platform=platform, checked_at=CHECKED_AT, prompt_rules=list(_COMMON_RULES), cross_platform_promotion_default="requires_review", native_ai_label_verified=False)
    return result


def review_source_policy(work, sources, assets=None):
    """Return review warnings; never delete sources or forbid private draft save.

    Assets are actual images/metadata, not a claim that image prompts rendered.
    Explicit license metadata remains a declaration for human confirmation.
    """
    if not isinstance(work, dict) or not isinstance(sources, (list, tuple)):
        raise ValueError("work须为dict，sources须为列表")
    platform = _platform(work.get("platform"))
    if assets is None:
        assets = work.get("assets", [])
    if not isinstance(assets, (list, tuple)):
        raise ValueError("assets须为列表")
    body = "\n".join(value for value in (work.get("title"), work.get("body")) if isinstance(value, str))
    image_prompts = work.get("image_prompts", [])
    if not isinstance(image_prompts, (list, tuple)):
        image_prompts = []
    prompt_values = [value for value in image_prompts if isinstance(value, str)]
    tasks = work.get("image_tasks", [])
    if isinstance(tasks, list):
        prompt_values.extend(str(item.get("prompt")) for item in tasks
                             if isinstance(item, dict) and isinstance(item.get("prompt"), str))
    images_text = "\n".join(prompt_values)
    findings = []

    def flag(code, message):
        if not any(item["code"] == code for item in findings):
            findings.append({"severity": "warning", "code": code, "message": message, "platform": platform})

    for match in _URL.finditer(body + "\n" + images_text):
        try:
            url = match.group(0)
            host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
        except ValueError:
            host = ""
        if host not in _HOSTS.get(platform, set()):
            flag("external_link_review", "正文或图片提示含站外链接，需区分中性引用与跨平台导流，并核查当前账号外链功能；内部来源地址继续保留。")
    if _CONTACT.search(body + "\n" + images_text) or _CTA.search(body + "\n" + images_text):
        flag("cross_platform_account_review", "内容含其他平台账号、联系方式或关注/领资料导流，须按该平台适用规则人工审核，不自动写入公开发布指令。")
    if re.search(r"二维码|QR\s*code", images_text, re.I) or re.search(r"(?:扫码|二维码).{0,24}(?:关注|添加|领取|联系|进群|站外)", body, re.I):
        flag("qr_destination_review", "图片或文案包含二维码导流线索，需核实实际跳转目标、必要性和平台许可；不能通过拆字或暗号规避。")

    for source in sources:
        if not isinstance(source, dict):
            continue
        url = source.get("url") or source.get("source_url") or ""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            search_index = ((host in {"www.baidu.com", "baidu.com"} and parsed.path in {"/s", "/"})
                            or (host == "top.baidu.com" and parsed.path == "/board")
                            or (host in {"www.toutiao.com", "toutiao.com"} and (parsed.path.startswith("/trending/") or parsed.path == "/hot-event/hot-board/")))
        except (ValueError, TypeError):
            search_index = False
        kind = source.get("kind") or source.get("evidence_kind")
        if search_index or kind in {"ranking", "public_ranking", "hotlist", "hot_list", "search"} or source.get("is_hotspot_index") is True:
            flag("fact_gap", "热点榜/搜索入口只能证明线索出现，不能作为原始事实确认；需补充原始报道或公告。中性注明“百度”等出处本身不作为导流违规。")

    for asset in assets:
        if not isinstance(asset, dict):
            flag("asset_rights_review", "实际图片的授权和署名信息未明确，需人工补全。")
            continue
        license_value = asset.get("license")
        license_text = license_value.strip() if isinstance(license_value, str) else ""
        if not license_text or license_text.casefold() in {"unknown", "unverified", "pending", "未知", "待核实"}:
            flag("asset_rights_review", "实际图片的许可证或使用授权未明确，不能以找到图片或注明来源代替授权。")
        credit = asset.get("credit")
        if asset.get("credit_required") is True or re.search(r"(?:CC[ -]?BY|署名|attribution)", license_text, re.I):
            if not isinstance(credit, str) or not credit.strip():
                flag("asset_credit_review", "图片许可要求署名，但credit未明确，需补齐作者、来源及许可信息。")
        if asset.get("is_ai_generated") is True or asset.get("ai_generated") is True or asset.get("generated_by_ai") is True:
            flag("ai_image_label_review", "实际图片由AI生成，需确认生成/示意说明并保留已有标识；发布前核查平台原生AI声明，私人草稿保存不代表声明已完成。")
    if work.get("ai_generated_images") is True:
        flag("ai_image_label_review", "稿件包含AI生成图片，需人工核查图片说明、已有标识及平台原生AI声明。")
    policy = get_platform_policy(platform)
    return {"platform": platform, "checked_at": CHECKED_AT, "requires_review": bool(findings), "findings": findings,
            "publication_checks": ["核对原始事实来源和适用日期。", "核对引用、图片授权与必要署名。", "核对站外链接/账号/二维码及AI声明的实际呈现。"],
            "internal_evidence_preserved": True, "internal_sources": deepcopy(list(sources)), "private_draft_allowed": True,
            "publication_authorized": False, "official_sources": policy["official_sources"], "unknowns": policy["unknowns"]}
