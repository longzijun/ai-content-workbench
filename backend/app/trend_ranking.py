"""Explain collected news signals without claiming future popularity.

The collector, not this offline module, must attest that ``kind='ranking'`` is
an actual public ranking and that ``source_id`` identifies one specific board.
RSS/news ordering is never a ranking signal. Publisher identity, preferably
``publisher_id``, is separate from feed/source identity.

Always returns a JSON-serializable dict, including ``eligible``. Broad mode
does not filter AI-related keywords. AI/ai_only modes use AI terms or explicit
user-supplied focus keywords; caller filters ``eligible=False`` results.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import urlparse


#: 榜单类来源。TrendRadar 各平台是有真实名次的热榜（P1 实测 rank 1..N），
#: 与百度/头条同性质，必须能让名次与升温判断生效；不在这组里的话，
#: rank 会被当成"RSS 条目序号"而拒不采信。
RANKING_KINDS = {"ranking", "public_ranking", "hotlist", "hot_list", "trendradar"}
HOT_TOP_N = 10
RANKING_MAX_AGE_HOURS = 2
MIN_COMPARISON_MINUTES = 5
MAX_COMPARISON_HOURS = 24
MIN_RANK_GAIN = 5
MIN_HEAT_GROWTH = 0.25
MIN_HEAT_DELTA = 1

_AI_TERMS = ("人工智能", "大模型", "生成式", "机器学习", "深度学习", "智能体", "AI", "LLM", "GPT", "OpenAI", "ChatGPT", "Claude", "Anthropic", "Gemini", "Codex", "Qwen", "DeepSeek", "artificial intelligence", "generative AI")
_TECH_TERMS = _AI_TERMS + ("科技", "芯片", "半导体", "机器人", "软件", "开源", "工具", "技术", "算法", "technology", "software", "semiconductor", "robotics")
_PUBLIC_TERMS = ("社会", "政策", "新规", "法律", "法院", "警方", "事故", "灾害", "安全", "健康", "医疗", "药物", "疫苗", "疫情", "就业", "教育", "投资", "股票", "利率", "房贷", "医保", "民生", "public policy", "court", "health", "medical", "vaccine", "accident", "regulation", "election")
_LIFE_TERMS = ("就业", "教育", "交通", "医保", "社保", "房贷", "消费", "养老", "健康", "学习", "办公", "工具")
_NOVELTY = re.compile(r"发布|上线|推出|开源|新规|政策|首发|新版本|重大更新|征求意见|试点|\b(?:launch(?:es|ed)?|release[sd]?|announc\w*|unveil\w*|new policy|open.source)\b", re.I)
_EVENT_TYPES = {"policy", "tool_release", "first_release", "release", "launch", "announcement"}


def _text(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _matches(text, terms):
    found = []
    for term in terms:
        term = _text(term)
        if not term:
            continue
        if term.isascii() and re.fullmatch(r"[A-Za-z0-9 ]+", term):
            matched = bool(re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, re.I))
        else:
            matched = term.casefold() in text.casefold()
        if matched and term not in found:
            found.append(term)
    return found


def _timestamp(value):
    if value is None or value == "":
        return None, "missing"
    try:
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, str):
            try:
                stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                stamp = parsedate_to_datetime(value)
        else:
            return None, "invalid"
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None, "invalid"
        return stamp.astimezone(timezone.utc), "valid"
    except (ValueError, TypeError, OverflowError):
        return None, "invalid"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError):
        return None


def _rank(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            value = int(value)
        except ValueError:
            return None
    return value if isinstance(value, int) and value > 0 else None


def _source_url(observation):
    value = _text(observation.get("source_url") or observation.get("url"))
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _host(url):
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _publisher_keys(observations):
    """Do not turn several feeds from one publisher into independent sources."""
    known_hosts = defaultdict(set)
    for observation in observations:
        publisher = _text(observation.get("publisher_id")).casefold()
        host = _host(_source_url(observation))
        if publisher and host:
            known_hosts[host].add(publisher)
    keys = set()
    for observation in observations:
        publisher = _text(observation.get("publisher_id")).casefold()
        host = _host(_source_url(observation))
        if publisher:
            keys.add("publisher:" + publisher)
        elif host and len(known_hosts[host]) == 1:
            keys.add("publisher:" + next(iter(known_hosts[host])))
        elif host:
            keys.add("host:" + host)
    return keys


def count_cross_sources(observations):
    """Count publisher/hostname identities, not verified independent reporting."""
    records = [item for item in observations if isinstance(item, dict)] if isinstance(observations, (list, tuple)) else []
    return len(_publisher_keys(records))


def _unique_append(items, value):
    if value not in items:
        items.append(value)


def _neutral_title(title, public_affairs, technology):
    title = re.sub(r"^(?:(?:震惊|重磅|惊天|全网炸锅|必看)[！!：:，,\s]*)+", "", title).strip()
    if not title or re.search(r"(?:所有人|全民).{0,8}(?:完了|末日|灾难)|吓疯|不看.{0,8}后悔", title):
        return "先核对事实：这条消息能确认到哪一步"
    title = re.sub(r"[！!]{2,}", "", title)
    if public_affairs:
        return title + "：已知事实与待确认问题"
    if technology:
        return title + "：变化了什么、适用条件与限制"
    return title + "：先核对来源与背景"


def rank_event(event, now, scope="broad", keywords=None, *, window_days=7):
    """Return factual signal labels and editorial priority, never a prediction.

    Ranking evidence expires after two hours. Compare the latest board sample
    with the nearest earlier sample 5 minutes to 24 hours away, never the most
    favorable historical sample. Heat comparison additionally requires the same
    nonempty heat_label, a positive baseline, >=25% gain and >=1 original unit.
    A new/unknown publication date does not invent a first-release claim. A
    current public board may still evidence current rank for an older article.
    """
    if not isinstance(event, dict):
        raise ValueError("event必须是dict")
    current, status = _timestamp(now)
    if current is None or status != "valid":
        raise ValueError("now必须包含可识别时区")
    if scope not in {"broad", "ai", "ai_only", "ai-only"}:
        raise ValueError("scope应为broad或ai_only")
    if isinstance(window_days, bool) or not isinstance(window_days, (int, float)) or not math.isfinite(window_days) or not 0 < window_days <= 365:
        raise ValueError("window_days必须在0到365天之间")
    if keywords is None:
        keywords = []
    if not isinstance(keywords, (list, tuple)) or any(not isinstance(term, str) for term in keywords):
        raise ValueError("keywords必须为字符串列表")

    title, summary = _text(event.get("title")), _text(event.get("summary"))
    text = title + " " + summary
    matched = _matches(text, keywords)
    ai_matched = _matches(text, _AI_TERMS)
    eligible = bool(title) and (scope == "broad" or bool(ai_matched or matched))
    reasons, uncertainties = [], []
    published, publication_status = _timestamp(event.get("published_at"))
    publication_age = (current - published).total_seconds() / 3600 if published else None
    if published and publication_age < 0:
        publication_status = "future"
        _unique_append(uncertainties, "发布时间晚于当前时间，需核对时区或日期；不当作已经发生的近期新闻。")
    elif published and publication_age > window_days * 24:
        publication_status = "old"
        _unique_append(uncertainties, f"原文发布时间已超过{window_days:g}天窗口；若再次上榜也不能称为新近首发。")
    elif published:
        publication_status = "recent"
    elif publication_status == "invalid":
        _unique_append(uncertainties, "发布时间无效或缺少时区，不能判断是否近期或首发。")
    else:
        _unique_append(uncertainties, "事件发布时间未知，不能据抓取时间声称近期首发。")

    observations = event.get("observations") or []
    if not isinstance(observations, (list, tuple)):
        observations = []
        _unique_append(uncertainties, "采集记录格式不完整，未据此判断热度。")
    records = []
    for raw in observations:
        if not isinstance(raw, dict):
            continue
        moment_field = "observed_at" if "observed_at" in raw else "fetched_at"
        stamp, date_status = _timestamp(raw.get(moment_field))
        if stamp is None:
            _unique_append(uncertainties, "部分采集记录没有有效时间或时区，未作为当前榜单和升温证据。")
            continue
        if stamp > current:
            _unique_append(uncertainties, "部分采集时间在未来，未作为当前榜单和升温证据。")
            continue
        kind = _text(raw.get("kind")).lower()
        rank = _rank(raw.get("rank"))
        heat = _number(raw.get("heat_value"))
        source_id = _text(raw.get("source_id"))
        if kind in RANKING_KINDS and not source_id:
            _unique_append(uncertainties, "公开榜采集缺少具体榜单source_id，未用于当前排名和趋势判断。")
            continue
        if kind not in RANKING_KINDS and (raw.get("rank") is not None or raw.get("heat_value") is not None):
            _unique_append(uncertainties, "新闻或RSS条目序号与数值不作为公开热榜证据。")
        if raw.get("heat_value") is not None and heat is None:
            _unique_append(uncertainties, "部分热度值未规范化为有限非负数，未用于热度变化计算。")
        if raw.get("rank") is not None and rank is None:
            _unique_append(uncertainties, "部分榜位不是有效正整数，未用于热点或榜位升温判断。")
        records.append({"source_id": source_id, "source_name": _text(raw.get("source_name")) or source_id or "未命名来源", "source_url": _source_url(raw), "publisher_id": _text(raw.get("publisher_id")), "kind": "ranking" if kind in RANKING_KINDS else kind, "board_id": _text(raw.get("board_id") or raw.get("chart_id")) or source_id, "stamp": stamp, "rank": rank, "heat_value": heat, "heat_label": _text(raw.get("heat_label"))})

    recent_records = [record for record in records if current - record["stamp"] <= timedelta(days=window_days)]
    source_entries = len({record["source_id"] or record["source_url"] for record in recent_records if record["source_id"] or record["source_url"]})
    publishers = _publisher_keys(recent_records)
    if source_entries > len(publishers) and len(publishers) <= 1 and source_entries > 1:
        _unique_append(uncertainties, "多个来源入口可能来自同一发布者，不能算独立多来源确认。")

    groups = defaultdict(list)
    for record in records:
        if record["kind"] == "ranking":
            groups[(record["source_id"], record["board_id"])].append(record)
    current_boards, historical_boards, comparisons, rising_boards = [], [], [], []

    def evidence(record):
        return {key: record[key] for key in ("source_id", "source_name", "source_url", "publisher_id", "board_id", "rank", "heat_value", "heat_label")} | {"observed_at": record["stamp"].isoformat(), "age_minutes": round((current - record["stamp"]).total_seconds() / 60, 2)}

    for board_key, samples in sorted(groups.items()):
        buckets = defaultdict(list)
        for sample in samples:
            buckets[sample["stamp"]].append(sample)
        deduplicated = []
        latest_conflicted = False
        latest_sample_time = max(buckets)
        for moment, duplicates in buckets.items():
            signatures = {(sample["rank"], sample["heat_value"], sample["heat_label"]) for sample in duplicates}
            if len(signatures) > 1:
                _unique_append(uncertainties, "同一榜单同一抓取时间存在冲突记录，未挑选较高热度或较好榜位。")
                latest_conflicted = latest_conflicted or moment == latest_sample_time
                continue
            deduplicated.append(duplicates[0])
        deduplicated.sort(key=lambda sample: sample["stamp"])
        if not deduplicated:
            continue
        latest = deduplicated[-1]
        if latest_conflicted:
            historical_boards.append(evidence(latest))
            _unique_append(uncertainties, "最新榜单采样有冲突，旧榜位只保留为历史线索，不当作当前热点。")
            continue
        if current - latest["stamp"] > timedelta(hours=RANKING_MAX_AGE_HOURS):
            historical_boards.append(evidence(latest))
            _unique_append(uncertainties, "榜单抓取已超过2小时，仅保留历史信号，不能称为当前热点或升温。")
            continue
        current_boards.append(evidence(latest))
        baselines = [sample for sample in deduplicated[:-1] if timedelta(minutes=MIN_COMPARISON_MINUTES) <= latest["stamp"] - sample["stamp"] <= timedelta(hours=MAX_COMPARISON_HOURS)]
        if not baselines:
            if len(deduplicated) == 1:
                _unique_append(uncertainties, "该榜目前只有一次有效抓取，不能判断升温或传播速度。")
            else:
                _unique_append(uncertainties, "没有相隔5分钟至24小时的同榜有效基线，不能判断升温。")
            continue
        baseline = baselines[-1]
        rank_gain = baseline["rank"] - latest["rank"] if baseline["rank"] is not None and latest["rank"] is not None else None
        growth, heat_delta = None, None
        same_metric = bool(baseline["heat_label"] and baseline["heat_label"] == latest["heat_label"])
        if baseline["heat_value"] is not None and latest["heat_value"] is not None:
            if not same_metric:
                _unique_append(uncertainties, "热度字段口径缺失或已变化，未比较该数值；不同来源的热度也不合并。")
            elif baseline["heat_value"] <= 0:
                _unique_append(uncertainties, "热度基线为零，不能计算可靠增长比例。")
            else:
                heat_delta = latest["heat_value"] - baseline["heat_value"]
                growth = heat_delta / baseline["heat_value"]
                if not math.isfinite(growth):
                    growth = None
                    _unique_append(uncertainties, "热度数值跨度异常，未计算增长比例或据此判断升温。")
        by_rank = rank_gain is not None and rank_gain >= MIN_RANK_GAIN
        by_heat = growth is not None and growth >= MIN_HEAT_GROWTH and heat_delta >= MIN_HEAT_DELTA
        comparison = {"source_id": latest["source_id"], "source_name": latest["source_name"], "board_id": latest["board_id"], "baseline_at": baseline["stamp"].isoformat(), "current_at": latest["stamp"].isoformat(), "interval_minutes": round((latest["stamp"] - baseline["stamp"]).total_seconds() / 60, 2), "previous_rank": baseline["rank"], "current_rank": latest["rank"], "rank_gain": rank_gain, "heat_label": latest["heat_label"] if same_metric else None, "previous_heat": baseline["heat_value"] if same_metric else None, "current_heat": latest["heat_value"] if same_metric else None, "heat_growth_fraction": round(growth, 6) if growth is not None else None, "heat_delta": heat_delta, "qualifies_by": (["rank"] if by_rank else []) + (["heat"] if by_heat else [])}
        comparisons.append(comparison)
        if by_rank or by_heat:
            rising_boards.append(comparison)
        if by_heat and rank_gain is not None and rank_gain < 0:
            _unique_append(uncertainties, "同榜热度数值上升但榜位下降，两项指标方向不同；不据此推断后续传播。")

    hot_boards = [record for record in current_boards if record["rank"] is not None and record["rank"] <= HOT_TOP_N]
    recent_publication = publication_status == "recent"
    original_link = _source_url({"url": event.get("url")})
    novelty = bool(_NOVELTY.search(text) or _text(event.get("event_type")).lower() in _EVENT_TYPES or event.get("is_first_release") is True)
    multi_source = len(publishers) > 1 and recent_publication
    watch_signals = []
    if recent_publication and novelty and (original_link or source_entries):
        watch_signals.append("近期来源出现发布、政策或工具变化等事件线索；需核查原文，不保证会成为热点。")
    if multi_source:
        watch_signals.append(f"在{len(publishers)}个发布者或网址来源中出现线索，需核查是否转载；不是已验证事实。")
        _unique_append(uncertainties, "多来源出现可能是转载或同稿分发，尚未验证报道独立性。")
    if any(record["rank"] is not None for record in current_boards):
        watch_signals.append("已在近期公开榜出现，但未以此推断未来热度或涨粉效果。")
    if any(record['rank'] is None and record['heat_value'] is not None for record in current_boards):
        watch_signals.append('近期官方热点入口提供了原始关注指标，具体名次尚未核实，先作为观察线索。')
        _unique_append(uncertainties, '来源未提供已核实名次；原始关注指标不等于阅读量或全网热度。')

    classification = "context"
    priority = 28 if recent_publication else 18
    if publication_status == "old":
        priority = 12
    if hot_boards:
        classification = "hot"
        best_rank = min(record["rank"] for record in hot_boards)
        priority = 88 + (HOT_TOP_N - best_rank) // 2
        for record in hot_boards[:3]:
            reasons.append(f"{record['source_name']}最近2小时的公开榜记录为第{record['rank']}位，进入前{HOT_TOP_N}；这只说明该榜当前排序。")
        _unique_append(uncertainties, "公开榜排名不等于阅读量、涨粉概率或全网影响力。")
    elif rising_boards:
        classification, priority = "rising", 74 + min(len(rising_boards), 3)
    elif watch_signals:
        classification, priority = "watch", 54 + min(len(watch_signals) * 3, 9)
        reasons.extend(watch_signals)
        _unique_append(uncertainties, "观察线索不保证会成为热点，仍需后续采样和事实核查。")
    else:
        reasons.append("目前仅有普通新闻或背景线索，没有有效当前公开榜或可比较的升温证据；仍可作为普通选题。")

    if classification in {"hot", "rising"}:
        for comparison in rising_boards[:3]:
            if "rank" in comparison["qualifies_by"]:
                reasons.append(f"{comparison['source_name']}同榜相隔{comparison['interval_minutes']:g}分钟，榜位从{comparison['previous_rank']}升至{comparison['current_rank']}，上升{comparison['rank_gain']}位。")
            if "heat" in comparison["qualifies_by"]:
                reasons.append(f"{comparison['source_name']}同口径“{comparison['heat_label']}”比最近有效基线增加{comparison['heat_growth_fraction'] * 100:g}%，达到25%且至少1原始单位的规则；不是跨平台热度比较。")
        if classification == "rising":
            _unique_append(uncertainties, "升温仅指同榜已观测指标变化，不能保证后续持续上升。")
    if matched and eligible:
        priority += 3
        reasons.append("匹配所选关键词，仅增加选题推荐优先级，不改变热点证据要求。")
    if publication_status == "future":
        classification, priority = "context", 0
        reasons = ["发布时间处于未来，先核对时间字段；当前不把它列为已发生的热点或新近首发。"]
    if not eligible:
        classification, priority = "context", 0
        reasons = ["缺少可识别标题，无法关联事件与榜单证据。" if not title else "当前为AI范围筛选，未匹配AI或用户指定关键词；广泛新闻模式下不会因此排除。"]

    public_affairs = bool(_matches(text, _PUBLIC_TERMS))
    technology = bool(_matches(text, _TECH_TERMS))
    if public_affairs:
        _unique_append(uncertainties, "社会、法律、政策或健康相关信息须核对官方或一手来源、时点和适用对象，不据标题作武断判断。")
        angle = "先梳理已知事实、时间线和来源，再解释适用对象与具体影响；明确仍待核实的问题，避免制造恐慌。"
    elif technology:
        angle = "核对这条线索实际发布或改变了什么、适用条件与限制，再判断谁需要行动、需要做什么；不虚构个人亲测。"
    else:
        angle = "梳理来源、时间与背景，区分已知事实和待确认问题，再判断这条线索对谁有实际影响。"
    brief = "素材线索：" + (summary or title or "缺少标题与摘要") + "\n写作任务：" + angle + "先打开原文核查，不将采集摘要当作已验证事实。"
    platforms = ["wechat", "toutiao"]
    if technology or _matches(text, _LIFE_TERMS):
        platforms.append("xiaohongshu")
    if not eligible:
        platforms = []
    priority = min(100, max(0, int(priority)))
    level = "高" if priority >= 85 else "较高" if priority >= 65 else "中" if priority >= 40 else "低"
    return {"eligible": eligible, "classification": classification, "priority": priority, "priority_label": f"推荐优先级：{level}（{priority}/100）", "reasons": reasons, "uncertainties": uncertainties, "suggested_title": _neutral_title(title, public_affairs, technology), "angle": angle, "brief": brief, "recommended_platforms": platforms,
            "signal_evidence": {"priority_semantics": "内容选题推荐优先级，不是预测热度、传播量或涨粉概率", "publication": {"status": publication_status, "published_at": published.isoformat() if published else None, "age_hours": round(publication_age, 3) if publication_age is not None else None, "window_days": window_days}, "scope": {"mode": scope, "matched_keywords": matched, "matched_ai_terms": ai_matched}, "sources": {"entry_count": source_entries, "publisher_or_host_count": len(publishers), "publisher_or_host_keys": sorted(publishers), "independence_verified": False}, "ranking": {"hot_top_n": HOT_TOP_N, "freshness_limit_hours": RANKING_MAX_AGE_HOURS, "comparison_interval_minutes": [MIN_COMPARISON_MINUTES, MAX_COMPARISON_HOURS * 60], "minimum_rank_gain": MIN_RANK_GAIN, "minimum_heat_growth_fraction": MIN_HEAT_GROWTH, "minimum_heat_delta": MIN_HEAT_DELTA, "current": current_boards, "historical": historical_boards, "comparisons": comparisons}}}
