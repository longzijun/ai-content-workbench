"""Bounded indexes from fixed public first-party pages and official RSS feeds.

Only this module's fixed endpoints are fetched. Article links are validated for
display and evidence; they are never followed. Collection does not approve a
topic, generate content, or act on any creator account.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zlib

import httpx


MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 50
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
#: 对瞬时故障的重试次数与退避。只针对"再试一次可能就好"的失败（超时、连接中断），
#: 不重试内容类错误 —— 源返回了 HTML，重放一次也不会突然变成 RSS。
#: 与 TrendRadar 的退避策略同思路：上限很小，避免把超时源拖成整轮的瓶颈。
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 1.5

#: 四大内容方向。与账号发帖目标比例对应（AI 50% / 科技 25% / 社会 15% / 生活 10%）。
#: 一个源可属多个方向；方向只说明「这条线索适合哪类选题」，不等于该源已被核实。
DIRECTION_AI = "ai"
DIRECTION_TECH = "tech"
DIRECTION_SOCIETY = "society"
DIRECTION_LIFE = "life"
DIRECTIONS = (DIRECTION_AI, DIRECTION_TECH, DIRECTION_SOCIETY, DIRECTION_LIFE)
DIRECTION_LABELS = {
    DIRECTION_AI: "AI 核心",
    DIRECTION_TECH: "科技与互联网",
    DIRECTION_SOCIETY: "社会热点与个人观点",
    DIRECTION_LIFE: "生活观察与跨界",
}

#: 源方时钟快于本机时的容忍上限。超过则整条丢弃（那不是时钟偏差，是脏数据）；
#: 在容忍内则钳到抓取时刻并如实标注，避免"未来新闻"污染时效性与新鲜度排序。
FUTURE_TOLERANCE = timedelta(hours=24)


@dataclass(frozen=True)
class _Source:
    id: str
    name: str
    url: str
    kind: str
    category: str
    article_hosts: tuple[str, ...]
    max_age_days: int
    redirect_urls: tuple[str, ...] = ()
    platform: str = "external"
    #: 来源分层，决定它是什么性质的证据，而不是它有多重要：
    #:   official        官方原始发布（厂商/机构自己的公告与研究）
    #:   interpretation  可信中文解读（媒体或机构的二次整理）
    #:   signal          热点线索（榜单、聚合入口，只说明「有人在看」）
    tier: str = "signal"
    #: 该源最短采集间隔（分钟）。0 表示每轮都采。
    #: 作用不是「更重要就采得更勤」，而是「更慢的源不必陪着热榜一起被拉」——
    #: 官方 RSS 一天更新一两次，每 3 小时拉一次与本机限流之间做了取舍。
    min_interval_minutes: int = 0
    #: 四大内容方向。空元组表示尚未归类（历史源在分类落地前的默认）。
    directions: tuple[str, ...] = ()
    #: 单次抓取最多保留多少条。OpenAI/arXiv 这类一次返回上千条的源必须收窄，
    #: 否则候选表会被单源淹没，也让"发现新东西"变成"反复看到同一批旧条目"。
    max_items: int = MAX_ITEMS
    #: JSON 源的字段映射。items 为列表所在键；url_template 用 {field} 取条目内字段，
    #: 这样外部文章链接（如 HN 的外链）不会被当成第一方证据 URL。
    json_items: str = ""
    json_title: str = ""
    json_date: str = ""
    json_heat: str = ""
    json_summary: str = ""
    json_url_template: str = ""

    def public(self):
        keys = ("id", "name", "url", "kind", "category", "platform", "tier",
                "min_interval_minutes", "directions", "max_items")
        return {key: getattr(self, key) for key in keys}


#: 每个源的 URL、article_hosts 与重定向行为均经 2026-10-23 实测核对
#: （见交付文档 WB-05 与 P3-host-audit.json）。article_hosts 是白名单而非装饰：
#: 写错会让 _canonical_article 静默丢弃该源的全部条目。
_SOURCES = (
    _Source("baidu_realtime", "百度实时热搜", "https://top.baidu.com/board?tab=realtime", "ranking", "society", ("www.baidu.com", "top.baidu.com"), 1, tier="signal", min_interval_minutes=0, directions=(DIRECTION_SOCIETY,)),
    _Source("bbc_world", "BBC 国际新闻", "https://feeds.bbci.co.uk/news/world/rss.xml", "rss", "world", ("www.bbc.co.uk", "www.bbc.com", "bbc.co.uk", "bbc.com"), 7, tier="official", min_interval_minutes=180, directions=(DIRECTION_SOCIETY,)),
    _Source("bbc_technology", "BBC 科技新闻", "https://feeds.bbci.co.uk/news/technology/rss.xml", "rss", "technology", ("www.bbc.co.uk", "www.bbc.com", "bbc.co.uk", "bbc.com"), 7, tier="official", min_interval_minutes=180, directions=(DIRECTION_TECH,)),
    _Source("google_ai", "Google AI 官方动态", "https://blog.google/innovation-and-ai/technology/ai/rss/", "rss", "ai", ("blog.google",), 90, ("https://blog.google/technology/ai/rss/",), tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    _Source("google_research", "Google Research 官方研究", "https://research.google/blog/rss/", "rss", "ai", ("research.google",), 90, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    _Source("openai_news", "OpenAI 官方动态", "https://openai.com/news/rss.xml", "rss", "ai", ("openai.com", "www.openai.com"), 90, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,), max_items=30),
    _Source("toutiao_hot_board", "今日头条公开热点", "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "ranking", "society", ("www.toutiao.com",), 1, platform="toutiao", tier="signal", min_interval_minutes=0, directions=(DIRECTION_SOCIETY, DIRECTION_TECH)),
    # --- P3 新增：P2 实测通过的官方 RSS / Atom / JSON 源 ---
    _Source("deepmind_blog", "Google DeepMind 官方博客", "https://deepmind.google/blog/rss.xml", "rss", "ai", ("deepmind.google",), 90, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,), max_items=30),
    _Source("arxiv_cs_ai", "arXiv cs.AI 最新论文", "https://export.arxiv.org/rss/cs.AI", "rss", "ai", ("arxiv.org", "export.arxiv.org"), 3, tier="official", min_interval_minutes=720, directions=(DIRECTION_AI,)),
    _Source("arxiv_cs_cl", "arXiv cs.CL 最新论文", "https://export.arxiv.org/rss/cs.CL", "rss", "ai", ("arxiv.org", "export.arxiv.org"), 3, tier="official", min_interval_minutes=720, directions=(DIRECTION_AI,)),
    _Source("github_ollama_releases", "Ollama 版本发布", "https://github.com/ollama/ollama/releases.atom", "rss", "ai", ("github.com",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    # 实测：comfyanonymous/ComfyUI 会 301 到 Comfy-Org/ComfyUI，直接用终态地址。
    _Source("github_comfyui_releases", "ComfyUI 版本发布", "https://github.com/Comfy-Org/ComfyUI/releases.atom", "rss", "ai", ("github.com",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    _Source("github_claude_code_releases", "Claude Code 版本发布", "https://github.com/anthropics/claude-code/releases.atom", "rss", "ai", ("github.com",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    _Source("github_openclaw_releases", "OpenClaw 版本发布", "https://github.com/openclaw/openclaw/releases.atom", "rss", "ai", ("github.com",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_AI,)),
    _Source("ollama_blog", "Ollama 官方博客", "https://ollama.com/blog/rss.xml", "rss", "ai", ("ollama.com", "www.ollama.com"), 60, tier="official", min_interval_minutes=240, directions=(DIRECTION_AI,)),
    _Source("github_blog", "GitHub 官方博客", "https://github.blog/feed/", "rss", "technology", ("github.blog",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_TECH,)),
    _Source("github_changelog", "GitHub 平台更新日志", "https://github.blog/changelog/feed/", "rss", "technology", ("github.blog",), 30, tier="official", min_interval_minutes=180, directions=(DIRECTION_TECH,)),
    # HN 官方 Algolia 端点。news.ycombinator.com/rss 本机不可达，故走 API。
    # 条目 URL 用第一方讨论页（item?id=），外链只留在摘要说明里，不当证据 URL。
    _Source("hackernews_front", "Hacker News 首页", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30", "json", "technology", ("news.ycombinator.com",), 2, tier="signal", min_interval_minutes=30, directions=(DIRECTION_TECH,),
            json_items="hits", json_title="title", json_date="created_at", json_heat="points", json_url_template="https://news.ycombinator.com/item?id={objectID}"),
    _Source("ithome", "IT之家", "https://www.ithome.com/rss/", "rss", "technology", ("www.ithome.com",), 7, tier="interpretation", min_interval_minutes=30, directions=(DIRECTION_TECH,), max_items=30),
    _Source("ifanr", "爱范儿", "https://www.ifanr.com/feed", "rss", "technology", ("www.ifanr.com",), 7, tier="interpretation", min_interval_minutes=30, directions=(DIRECTION_TECH,), max_items=30),
    _Source("sspai", "少数派", "https://sspai.com/feed", "rss", "technology", ("sspai.com",), 14, tier="interpretation", min_interval_minutes=60, directions=(DIRECTION_TECH, DIRECTION_LIFE), max_items=20),
    _Source("infoq_cn", "InfoQ 中国", "https://www.infoq.cn/feed", "rss", "technology", ("www.infoq.cn",), 7, tier="interpretation", min_interval_minutes=30, directions=(DIRECTION_TECH,), max_items=30),
    _Source("ruanyifeng", "阮一峰科技爱好者周刊", "https://www.ruanyifeng.com/blog/atom.xml", "rss", "technology", ("www.ruanyifeng.com",), 40, tier="interpretation", min_interval_minutes=720, directions=(DIRECTION_TECH, DIRECTION_LIFE), max_items=10),
    _Source("chinanews", "中国新闻网滚动新闻", "https://www.chinanews.com.cn/rss/scroll-news.xml", "rss", "society", ("www.chinanews.com.cn",), 2, tier="official", min_interval_minutes=30, directions=(DIRECTION_SOCIETY,), max_items=30),
)

#: 分层说明。interpretation 层在 P3 之前是空的（当时 7 个源里没有中文解读类），
#: 现由 IT之家 / 爱范儿 / 少数派 / InfoQ 中国 / 阮一峰周刊填充。
#: 这五家是媒体或作者的二次整理，可作背景与选题切入口，不能替代原文核实。
TIER_LABELS = {
    "official": "官方原始",
    "interpretation": "中文解读",
    "signal": "热点线索",
}
TIER_NOTES = {
    "official": "厂商或机构自己的发布，可作事实起点；仍需核对原文与时间。",
    "interpretation": "媒体二次整理，可作背景，不能替代原文。",
    "signal": "榜单与聚合入口，只说明有人在看，不是事实证据。",
}
_BY_ID = {source.id: source for source in _SOURCES}
SOURCE_CATALOG = tuple(source.public() for source in _SOURCES)
_TRACKING_KEYS = frozenset({"sa", "spm", "gclid", "fbclid", "dclid", "msclkid", "ref", "ref_src", "ref_url", "ocid", "cmpid", "campaign", "source", "token", "access_token", "auth", "password"})
_TRACKING_PREFIXES = ("utm_", "rsv_", "at_")
_XML_FORBIDDEN = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
#: 注释与 CDATA：里面的内容对 XML 解析器是惰性的，其中的 DOCTYPE 字符串
#: 不构成文档类型声明，查找真实声明前先把它们剔除。
_INERT_BLOCKS = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>", re.S)


class SourceError(RuntimeError):
    """Controlled source errors, without reflecting remote HTML or credentials."""


def get_source_catalog():
    """Return copies; mutating the public catalog cannot add fetch endpoints."""
    return [source.public() for source in _SOURCES]


def source_catalog():
    """Public integration name used by the monitor and source picker."""
    return get_source_catalog()


def platform_source_capabilities():
    """Known public-source coverage, distinct from logged-in creator access.

    Unavailable entries are not monitor source IDs. A successful public check
    on the recorded date does not promise later requests remain available.
    """
    return [
        {"platform": "wechat", "available": False, "status": "unavailable", "source_ids": [], "checked_at": "2026-10-12",
         "reference_url": "https://mp.weixin.qq.com/", "reason": "尚未核实无需登录且有可解析热点条目的公众号官方公开榜单。",
         "review_fallback": "用户在官方端人工查看；保留真实出处和待核问题，不把百度或其他外部榜单标为微信热点。"},
        {"platform": "toutiao", "available": True, "status": "public_index", "source_ids": ["toutiao_hot_board"], "checked_at": "2026-10-12",
         "reference_url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "reason": "核查时官方公开地址匿名返回成功JSON与热点条目，运行时仍逐次校验响应。",
         "uncertainties": ["响应没有显式名次，rank保持未知，不把数组顺序当真实名次。", "HotValue是公开原始指标，单位与计算口径未披露；不等于全网热度或未来热度。", "热点聚合入口不是事件原始事实证据。"]},
        {"platform": "xhs", "available": False, "status": "unavailable", "source_ids": [], "checked_at": "2026-10-12",
         "reference_url": "https://www.xiaohongshu.com/explore", "reason": "核查时官方发现页匿名请求返回重定向，未取得可解析的公开热点榜；不进入登录或验证流程。",
         "review_fallback": "用户在官方端人工查看；保留真实出处和待核问题，不使用私有接口或把其他平台热榜冒充小红书热点。"},
    ]


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li") and not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li") and not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    parser = _Text()
    parser.feed(value)
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _canonical_article(value, source):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(urljoin(source.url, value))
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("http", "https") or host not in source.article_hosts
                or parsed.username or parsed.password or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)):
            return None
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(_TRACKING_PREFIXES)]
        return urlunsplit((parsed.scheme, host, parsed.path or "/", urlencode(query), ""))
    except (TypeError, ValueError):
        return None


def _endpoint_allowed(value, source):
    # Exact URLs deliberately reject a first-party private API redirect too.
    return value == source.url or value in source.redirect_urls


def _read_limited(response, deadline):
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "deflate"):
        raise SourceError("来源使用了暂不支持的压缩方式")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in ("gzip", "deflate") else None
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BYTES:
        raise SourceError("来源响应超过 2 MiB 限额")
    chunks, raw_total, total = [], 0, 0
    if time.monotonic() >= deadline:
        raise SourceError("来源读取时间预算已耗尽")
    for raw in response.iter_raw():
        if time.monotonic() > deadline:
            raise SourceError("来源读取时间预算已耗尽")
        raw_total += len(raw)
        if raw_total > MAX_BYTES:
            raise SourceError("来源响应超过 2 MiB 限额")
        chunk = decoder.decompress(raw, MAX_BYTES - total + 1) if decoder else raw
        total += len(chunk)
        if total > MAX_BYTES:
            raise SourceError("来源解压后的内容超过 2 MiB 限额")
        chunks.append(chunk)
    if decoder and (not decoder.eof or decoder.unused_data):
        raise SourceError("来源压缩内容不完整或格式异常")
    if not total:
        raise SourceError("来源返回了空内容")
    return b"".join(chunks)


def _fetch(source):
    url, deadline = source.url, time.monotonic() + TIMEOUT_SECONDS
    headers = {"User-Agent": "SelfMediaWorkbench/0.1 (public source index)", "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/html;q=0.8", "Accept-Encoding": "identity"}
    with httpx.Client(follow_redirects=False, headers=headers) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            if not _endpoint_allowed(url, source):
                raise SourceError("来源重定向到固定公开地址之外，已停止访问")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("来源读取时间预算已耗尽")
            # HTTPX timeouts apply to each I/O operation, not an absolute wall
            # deadline. Redirects and each raw block also check the total budget.
            per_io = min(TIMEOUT_SECONDS, remaining)
            timeout = httpx.Timeout(connect=per_io, read=per_io, write=per_io, pool=per_io)
            with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    next_url = urljoin(url, location) if location else ""
                    if redirect_count == MAX_REDIRECTS or not _endpoint_allowed(next_url, source):
                        raise SourceError("来源重定向超限或离开固定公开地址，已停止访问")
                    url = next_url
                    continue
                if response.status_code != 200:
                    raise SourceError(f"来源暂不可访问（HTTP {response.status_code}）")
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if source.kind == "json" or source.id == "toutiao_hot_board":
                    allowed = ("application/json",)
                else:
                    allowed = ("text/html", "application/xhtml+xml") if source.kind == "ranking" else ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
                if media not in allowed:
                    raise SourceError("来源未返回预期的公开榜单、官方 RSS/XML 或 JSON")
                response_headers = dict(response.headers)
                return _read_limited(response, deadline), response_headers
    raise SourceError("来源重定向超限")


def _decode(payload, headers, kind):
    if len(payload) > MAX_BYTES:
        raise SourceError("来源内容超过 2 MiB 限额")
    if payload.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif payload.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    else:
        message = Message()
        message["content-type"] = headers.get("content-type", "")
        encoding = message.get_content_charset()
        prefix = payload[:2048].decode("ascii", errors="ignore")
        if not encoding:
            declaration = re.search(r'<\?xml[^>]+encoding\s*=\s*["\']([^"\']+)', prefix, re.I) if kind == "rss" else re.search(r'<meta[^>]+charset\s*=\s*["\']?([a-z0-9_-]+)', prefix, re.I)
            encoding = declaration.group(1) if declaration else "utf-8"
    try:
        return payload.decode(encoding, errors="strict")
    except (UnicodeError, LookupError, TypeError):
        raise SourceError("来源文字编码无法可靠解析") from None


def _date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 10**18:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value):
        return int(value)
    return None


def _parse_baidu(text, source):
    match = re.search(r"<!--s-data:(.*?)-->", text, re.S)
    if not match:
        raise SourceError("百度公开榜单数据未识别，可能页面变化或需要人工验证")
    try:
        data = json.loads(match.group(1))
        cards = data["data"]["cards"]
        rows = [row for card in cards if isinstance(card, dict) and card.get("component") == "hotList" for row in card.get("content", [])]
    except (ValueError, TypeError, KeyError):
        raise SourceError("百度公开榜单数据格式已变化") from None
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("word") or row.get("query"), 300)
        url = _canonical_article(row.get("rawUrl") or row.get("url"), source)
        if not title or not url:
            continue
        index, heat = _number(row.get("index")), _number(row.get("hotScore"))
        # Observed public DOM shows 1 for the first ordinary row, whose s-data
        # index is 0. A separately pinned isTop row is not part of the ranking.
        rank = index + 1 if row.get("isTop") is not True and index is not None else None
        items.append({"title": title, "url": url, "summary": _text(row.get("desc"), 400), "published_at": None,
                      "rank": rank, "heat_value": heat, "heat_label": "热搜指数" if heat is not None else None, "category": source.category})
        if len(items) == MAX_ITEMS:
            break
    if not items or not any(item["rank"] is not None for item in items):
        raise SourceError("百度榜单未提供可核验的普通条目排名")
    return items


def _parse_toutiao(text, source):
    """Parse only the observed anonymous public hot-board response shape.

    This response has no explicit rank field. Array order is deliberately not
    promoted to a verified rank, including the separate fixed/pinned section.
    No article bodies, account endpoints, images or app deep links are fetched.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise SourceError("头条公开热点未返回可解析JSON，可能需登录、验证或页面已变化") from None
    if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("data"), list):
        raise SourceError("头条公开热点未返回成功条目，未生成候选")
    fixed = data.get("fixed_top_data", [])
    if not isinstance(fixed, list):
        raise SourceError("头条公开热点置顶数据格式已变化")
    items, seen = [], set()
    # Keep the ordinary list first so the bounded result cannot be occupied by
    # a large pinned section; fixed entries are useful context, not ranked hot.
    for row, pinned in [(row, False) for row in data["data"]] + [(row, True) for row in fixed]:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("Title"), 300)
        url = _canonical_article(row.get("Url"), source)
        if not title or not url:
            continue
        parsed = urlsplit(url)
        if not re.fullmatch(r"/(?:trending|article)/[1-9][0-9]{0,31}/?", parsed.path):
            continue
        # The public Url includes analytics/event queries (including log_pb).
        # The native path contains the stable ID; none of those queries belong
        # in internal evidence or need be replayed to inspect the source later.
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if url in seen:
            continue
        heat = None if pinned else _number(row.get("HotValue"))
        label = _text(row.get("LabelDesc"), 200)
        items.append({"title": title, "url": url, "summary": "平台标签：" + label if label else "", "published_at": None,
                      "rank": None, "heat_value": heat, "heat_label": "头条HotValue（公开原始值，单位未披露）" if heat is not None else None,
                      "category": source.category, "is_hotspot_index": True, "is_pinned": pinned,
                      "rank_verified": False, "signal_uncertainties": ["未核实名次；不按数组顺序标热榜排名。", "热点聚合入口不等于事件事实确认。"]})
        seen.add(url)
        if len(items) == MAX_ITEMS:
            break
    if not items:
        raise SourceError("头条公开热点没有可核验的公开条目")
    return items


def _local(element):
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _child(element, *names):
    return next((child for child in element if _local(child) in names), None)


def _node_text(element):
    return " ".join(element.itertext()) if element is not None else ""


def _xml_prolog_declares_doctype(text):
    """在剔除注释与 CDATA 之后查找 DOCTYPE / ENTITY 声明。

    不能对原文全文匹配：正文常把整段 HTML 塞进 CDATA，里面带 `<!DOCTYPE html>`
    （实测 github.blog 正是如此），那只是惰性文本而非文档类型声明，全文匹配会
    把这类正常订阅源整条误杀。注释里的同类字符串同样惰性。剔除这两类之后，
    真正出现在文档结构里的 DOCTYPE/ENTITY 仍会被拦下。
    """
    return bool(_XML_FORBIDDEN.search(_INERT_BLOCKS.sub("", text)))


def _parse_rss(text, source, now):
    if _xml_prolog_declares_doctype(text):
        raise SourceError("来源 XML 声明里含 DOCTYPE 或实体声明，已拒绝解析")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError, RecursionError):
        raise SourceError("来源 RSS/XML 格式无法可靠解析") from None
    if _local(root) == "rss":
        channel = _child(root, "channel")
        rows = [node for node in channel if _local(node) == "item"] if channel is not None else []
        atom = False
    elif _local(root) == "feed":
        rows, atom = [node for node in root if _local(node) == "entry"], True
    else:
        raise SourceError("来源并非已识别的官方 RSS 或 Atom 订阅")
    items, dates, seen = [], [], set()
    for row in rows:
        title = _text(_node_text(_child(row, "title")), 300)
        if atom:
            link = next((node.get("href") for node in row if _local(node) == "link" and node.get("rel", "alternate") == "alternate"), None)
        else:
            link = _node_text(_child(row, "link"))
            if not link:
                guid = _child(row, "guid")
                link = _node_text(guid) if guid is not None and guid.get("isPermaLink", "true").lower() == "true" else None
        url = _canonical_article(link, source)
        if not title or not url or url in seen:
            continue
        # Atom 用 <published>/<updated>，RSS 用 <pubDate>。两边标签不能混用：
        # GitHub Releases 只给 <updated>，漏掉它会导致整个源被判为「无日期」而拒收。
        date_tags = ("published", "updated") if atom else ("pubDate", "date")
        published = _date(_node_text(_child(row, *date_tags)))
        if published is not None and published > now:
            # 源方时钟快于本机（实测 InfoQ 中国快约 1 小时）。容忍内归一到抓取时刻
            # 并如实标注原值；超出容忍说明是脏数据，直接丢弃而不是替它编一个时间。
            if published > now + FUTURE_TOLERANCE:
                continue
            adjusted, published = _iso(published), now
        else:
            adjusted = None
        if published is not None:
            dates.append(published)
        description = _child(row, "description", "summary")
        # Never substitute full <content>/<encoded> article bodies.
        item = {"title": title, "url": url, "summary": _text(_node_text(description), 400),
                "published_at": _iso(published) if published else None, "rank": None, "heat_value": None, "heat_label": None, "category": source.category}
        if adjusted:
            item["published_at_adjusted_from"] = adjusted
            item["signal_uncertainties"] = [f"来源标注的发布时间（{adjusted}）晚于本机时钟，已归一到抓取时间；准确发布时间须以原文为准。"]
        items.append(item)
        seen.add(url)
    if not items:
        raise SourceError("来源没有可核验的公开候选条目")
    if not dates:
        raise SourceError("来源未提供可核验的文章发布日期，无法确认是否过期")
    if max(dates) < now - timedelta(days=source.max_age_days):
        raise SourceError(f"来源最新文章已超过 {source.max_age_days} 天，暂不作为当前热点")
    # A feed need not be chronological. All dates are normalized UTC ISO strings;
    # undated entries sort after dated ones, preserving their original order.
    items.sort(key=lambda item: item["published_at"] or "", reverse=True)
    return items[:max(1, int(source.max_items or MAX_ITEMS))]


def _parse_json(text, source, now):
    """Parse a fixed public JSON endpoint using the source's declared field map.

    Only the keys declared on the source are read. The endpoint itself is fixed
    in _SOURCES; no pagination, no search, no authenticated route is followed.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise SourceError("来源未返回可解析 JSON，可能已变化或需要登录") from None
    rows = data.get(source.json_items) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("来源 JSON 中未找到条目列表，公开结构可能已变化")
    items, dates, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(str(row.get(source.json_title) or "")[:500], 300)
        raw_url = source.json_url_template.format(**row) if source.json_url_template else str(row.get("url") or "")
        url = _canonical_article(raw_url, source)
        if not title or not url or url in seen:
            continue
        published = _date(str(row.get(source.json_date) or "")) if source.json_date else None
        if published is not None and published > now:
            if published > now + FUTURE_TOLERANCE:
                continue
            published = now
        if published is not None:
            dates.append(published)
        heat = _number(row.get(source.json_heat)) if source.json_heat else None
        summary = _text(str(row.get(source.json_summary) or "")[:500], 400) if source.json_summary else ""
        items.append({"title": title, "url": url, "summary": summary,
                      "published_at": _iso(published) if published else None,
                      "rank": None, "heat_value": float(heat) if heat is not None else None,
                      "heat_label": "HN 社区投票分" if heat is not None else None,
                      "category": source.category})
        seen.add(url)
    if not items:
        raise SourceError("来源没有可核验的公开候选条目")
    if max(dates, default=None) is None:
        raise SourceError("来源未提供可核验的条目时间，无法确认是否过期")
    if max(dates) < now - timedelta(days=source.max_age_days):
        raise SourceError(f"来源最新条目已超过 {source.max_age_days} 天，暂不作为当前热点")
    items.sort(key=lambda item: item["published_at"] or "", reverse=True)
    return items[:max(1, int(source.max_items or MAX_ITEMS))]


def _fetch_with_retry(source):
    """抓一次，遇到瞬时故障退避后再试，最多 MAX_ATTEMPTS 次。

    重试只覆盖传输层失败（超时、连接中断）。SourceError 一律不重试：那是
    源给出的确定性答复（内容类型不对、重定向越界、XML 不合规），重放无益。
    """
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _fetch(source)
        except (httpx.TimeoutException, httpx.TransportError, OSError) as error:
            last = error
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise last


def collect_source(source_id):
    """Collect one known source; an unavailable source always has empty items."""
    if not isinstance(source_id, str) or source_id not in _BY_ID:
        raise ValueError("未知热点来源；仅支持固定公开来源目录")
    source, now = _BY_ID[source_id], _utcnow()
    result = {"source_id": source.id, "source_name": source.name, "source_url": source.url, "kind": source.kind,
              "platform": source.platform, "status": "error", "fetched_at": _iso(now), "items": [], "error": None}
    try:
        payload, headers = _fetch_with_retry(source)
        text = _decode(payload, headers, source.kind)
        if source.kind == "ranking":
            age = headers.get("age", "0")
            if age.isdigit() and int(age) > 3600:
                raise SourceError("榜单缓存已超过一小时，无法当作实时热点")
            served_at = _date(headers.get("date"))
            if served_at is not None and served_at < now - timedelta(days=1):
                raise SourceError("榜单响应日期已过期，无法当作实时热点")
            result["items"] = _parse_toutiao(text, source) if source.id == "toutiao_hot_board" else _parse_baidu(text, source)
        elif source.kind == "json":
            result["items"] = _parse_json(text, source, now)
        else:
            result["items"] = _parse_rss(text, source, now)
        for item in result["items"]:
            item["platform"] = source.platform
        result["status"] = "ok"
    except SourceError as error:
        result["error"] = str(error)
    except httpx.TimeoutException:
        result["error"] = "来源读取超时，请稍后重试"
    except (httpx.HTTPError, OSError, zlib.error):
        result["error"] = "来源暂不可访问或响应损坏，请稍后重试"
    except (ValueError, TypeError, KeyError, RecursionError):
        result["error"] = "来源公开数据格式已变化，未生成候选"
    return result
