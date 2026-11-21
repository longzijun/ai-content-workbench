"""四大方向分类与六维可解释评分。

设计原则（每一条都是为了不把"算出来的数"伪装成"知道的事"）：

1. **每个维度都给依据**。评分不是黑箱分数，每维都带 `basis`（用了哪些事实）
   与 `confidence`（这些事实够不够硬），界面和人工判断都要能追问"凭什么"。
2. **缺数据就降置信度并写明缺口**，绝不填一个好看的中间值。没有发布时间就是
   没有，时效性只能按采集时间估算，并在 `data_gaps` 里写出来。
3. **高风险不因高分放行**。评分只回答"值不值得做"，不回答"能不能发"。
   `publishable` 是另一道闸，未核实 + 高风险一律 False，与总分无关。
4. **模型输出与规则输出分开存**。规则是确定性基线（`method: rule`），
   模型分析是增强层（`method: model`），落在独立表，可弃用、可重算，
   不回写原始候选。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .trend_sources import DIRECTION_AI, DIRECTION_LIFE, DIRECTION_SOCIETY, DIRECTION_TECH

#: 账号发文目标比例（任务书给定）。受众匹配度按方向查这张表。
DIRECTION_WEIGHTS = {DIRECTION_AI: 0.50, DIRECTION_TECH: 0.25, DIRECTION_SOCIETY: 0.15, DIRECTION_LIFE: 0.10}

#: 六维权重，合计 1.0。改这里必须同步改 DIMENSION_LABELS 与测试。
WEIGHTS = {
    "heat": 0.20,        # 热度与趋势
    "timeliness": 0.20,  # 时效性
    "audience": 0.20,    # 账号受众匹配
    "value": 0.15,       # 内容价值
    "originality": 0.15,  # 原创开发空间
    "credibility": 0.10,  # 信息可信度
}
DIMENSION_LABELS = {
    "heat": "热度与趋势",
    "timeliness": "时效性",
    "audience": "账号受众匹配",
    "value": "内容价值",
    "originality": "原创开发空间",
    "credibility": "信息可信度",
}

#: 二级主题关键词。命中只说明"这条线索属于该主题的表达范畴"，
#: 不说明它已被核实，也不替代分类判断。
_SUBTOPIC_RULES = {
    DIRECTION_AI: {
        "模型发布": ("发布", "推出", "上线", "开源", "新版本", "release", "launch", "unveil"),
        "智能体与工具": ("智能体", "agent", "助手", "copilot", "工具链", "mcp"),
        "研究与论文": ("论文", "研究", "arxiv", "benchmark", "评测", "方法"),
        "基础设施": ("推理", "部署", "量化", "显卡", "显存", "vllm", "ollama", "llama.cpp"),
    },
    DIRECTION_TECH: {
        "产品与硬件": ("发布", "开售", "芯片", "手机", "硬件", "处理器", "gpu"),
        "平台与开发": ("api", "sdk", "开源", "框架", "更新日志", "changelog", "github"),
        "行业与商业": ("融资", "收购", "上市", "营收", "财报", "裁员"),
        "安全与风险": ("漏洞", "泄露", "攻击", "cve", "风险"),
    },
    DIRECTION_SOCIETY: {
        "政策与治理": ("政策", "新规", "通知", "部门", "监管", "试点", "意见"),
        "公共事件": ("事故", "地震", "火灾", "通报", "调查", "处置"),
        "民生与经济": ("医保", "社保", "就业", "房价", "物价", "消费", "利率"),
        "舆论与观点": ("争议", "回应", "道歉", "质疑", "网友"),
    },
    DIRECTION_LIFE: {
        "效率工具": ("工具", "效率", "技巧", "教程", "app", "软件", "插件"),
        "消费与省钱": ("省钱", "避坑", "性价比", "测评", "推荐", "折扣"),
        "生活观察": ("生活", "日常", "习惯", "健康", "饮食", "旅行"),
        "跨界思考": ("为什么", "思考", "观察", "启发", "底层"),
    },
}

#: A/B/C 内容线相关性（独立于四方向素材标签）。四方向回答「这条内容属于什么题材」，
#: A/B/C 回答「这条线索适合哪条内容线」。命中只是关键词层面的
#: 相关性提示，不替代人工判断。
_CONTENT_LINE_RULES = {
    "A": ("实践工作流", ("研发", "法规", "标准", "方案", "调研", "操作", "模板", "测试", "验证", "知识管理", "检索", "工作流", "质量管理")),
    "B": ("工具实测", ("AI", "大模型", "智能体", "agent", "实测", "评测", "工具", "效率", "教程", "上手", "体验", "能用", "噱头", "开源", "模型", "LLM", "GPT")),
    "C": ("项目复盘", ("副业", "变现", "写作", "小说", "自媒体", "个人", "经历", "实践", "观点", "踩坑", "复盘", "记录", "感悟")),
}

#: 高风险题材。命中不等于不能写，只是**不能凭热榜就当事实**，
#: 必须有可访问的原始出处且经人工核实，才可能进入可发布推荐。
RISK_TERMS = ("遇难", "死亡", "身亡", "坠亡", "事故", "地震", "火灾", "爆炸", "坍塌",
              "被查", "落马", "刑拘", "逮捕", "判刑", "起诉", "诈骗", "性侵",
              "疫情", "中毒", "召回", "致癌")

#: 分层可信度基线。signal（热榜）本来就只是"有人在看"，不是事实。
_TIER_CREDIBILITY = {"official": 0.90, "interpretation": 0.65, "signal": 0.30}
_TIER_VALUE = {"official": 0.80, "interpretation": 0.65, "signal": 0.35}


def _tokens(text):
    return (text or "").casefold()


def _hits(text, terms):
    low = _tokens(text)
    return [term for term in terms if term.casefold() in low]


def classify(title, summary="", source_directions=()):
    """四大方向 + 二级主题。规则法，确定性，不调模型。

    来源声明的方向优先——那是采集时就确定的归属；来源未归类时才回退到
    关键词。两者都有时在 `basis` 里写清楚，避免"看起来是算出来的"。
    """
    text = f"{title or ''} {summary or ''}"
    directions = list(source_directions or [])
    basis = []
    if directions:
        basis.append("来源声明方向：" + "、".join(_label(d) for d in directions))
    scores = {}
    for direction, groups in _SUBTOPIC_RULES.items():
        score = 0
        for terms in groups.values():
            score += len(_hits(text, terms))
        scores[direction] = score
    keyword_best = max(scores, key=lambda key: scores[key]) if scores else DIRECTION_AI
    if not directions:
        if scores[keyword_best] > 0:
            directions = [keyword_best]
            basis.append(f"来源未声明方向，按关键词判定为{_label(keyword_best)}")
        else:
            directions = [DIRECTION_AI]
            basis.append("来源未声明方向且无关键词命中，归为 AI 核心待人工调整")
    primary = _primary_direction(directions, scores, basis)
    subtopics = []
    for name, terms in _SUBTOPIC_RULES.get(primary, {}).items():
        if _hits(text, terms):
            subtopics.append(name)
    return {"direction": primary, "directions": directions, "subtopics": subtopics,
            "method": "rule", "basis": basis}


def _primary_direction(directions, scores, basis):
    """多方向时选证据最强的那个，而不是列表第一项。

    候选的方向是各来源声明的并集，顺序取决于哪个源先被采集 —— 直接取第一项
    会让归并顺序决定归类。实测「西藏那曲发生 7 级地震」被 tr_douyin 采集
    （声明 life,society），取第一项就归成了"生活观察"，推荐结果变成
    "小红书 + 步骤教程"，荒谬。
    """
    if len(directions) <= 1:
        return directions[0]
    best = max(directions, key=lambda d: (scores.get(d, 0), DIRECTION_WEIGHTS.get(d, 0.0)))
    if best != directions[0]:
        evidence = scores.get(best, 0)
        detail = f"关键词证据 {evidence} 处" if evidence else "无关键词证据，按账号占比"
        basis.append(f"来源声明了 {len(directions)} 个方向，{detail}，主方向取{_label(best)}")
    return best


def _label(direction):
    from .trend_sources import DIRECTION_LABELS
    return DIRECTION_LABELS.get(direction, direction)


def article_tags(title, summary=""):
    """文章内容标签（多标签），只看标题与摘要的关键词，与来源声明方向无关。

    来源声明方向回答「这个来源通常发什么」，文章标签回答「这条内容在讲什么」。
    科技媒体的 AI 报道：来源标签是科技，文章标签应含 AI。二者分开，避免
    「来源预设方向优先」把命中 AI 术语的内容误归到其他方向。返回按命中数降序
    的方向列表；无关键词命中返回空列表（由调用方决定如何兜底）。
    """
    text = f"{title or ''} {summary or ''}"
    hits = []
    for direction, groups in _SUBTOPIC_RULES.items():
        score = 0
        for terms in groups.values():
            score += len(_hits(text, terms))
        if score > 0:
            hits.append((direction, score))
    hits.sort(key=lambda item: item[1], reverse=True)
    return [direction for direction, _ in hits]


def content_lines(title, summary=""):
    """A/B/C 内容线相关性（独立于四方向素材标签），返回命中降序的列表。

    四方向回答「这条内容属于什么题材」，A/B/C 回答「这条线索适合哪条内容线」。
    C 线以作者真实记录和人工导入材料为主，这里只做
    关键词提示，不把新闻自动补进 C 线。
    """
    text = f"{title or ''} {summary or ''}"
    lines = []
    for line, (name, terms) in _CONTENT_LINE_RULES.items():
        score = len(_hits(text, terms))
        if score > 0:
            lines.append({"line": line, "name": name, "score": score})
    lines.sort(key=lambda item: item["score"], reverse=True)
    return lines


def _age_hours(stamp, now):
    if not isinstance(stamp, datetime):
        return None
    return max(0.0, (now - stamp).total_seconds() / 3600)


def _parse(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _heat_dimension(observations):
    """热度与趋势。没有热度数据时给 0 分 + 置信度 0，而不是给个中位数。"""
    records = [item for item in observations if isinstance(item, dict)]
    if not records:
        return 0.0, 0.0, ["本条没有采集记录，无法评估热度"]
    publishers = {item.get("publisher_id") for item in records if item.get("publisher_id")}
    source_ids = {item.get("source_id") for item in records if item.get("source_id")}
    normalized = [item.get("heat_normalized") for item in records
                  if isinstance(item.get("heat_normalized"), (int, float))]
    ranks = [item.get("rank") for item in records if isinstance(item.get("rank"), int) and item["rank"] > 0]
    basis = []
    score, confidence = 0.0, 0.2
    if normalized:
        score = max(normalized)
        confidence = 0.6
        basis.append(f"批内名次归一值 {max(normalized):.2f}")
    elif ranks:
        # 没有批内基准时只用名次做一个粗信号，并明确标注为弱证据。
        score = max(0.0, 1.0 - (min(ranks) - 1) / 50.0)
        confidence = 0.3
        basis.append(f"榜单最好名次 {min(ranks)}（无批内基准，仅作弱信号）")
    else:
        basis.append("该来源不提供名次或热度值")
    cross = len(publishers) or len(source_ids)
    if cross >= 3:
        # 多个独立来源同时出现才是真信号；采集轮次多不算——同一个源每轮
        # 都会留一条记录，把轮次当成"热度"只会让采集频率高的源虚高。
        score = min(1.0, score + 0.10)
        confidence = min(1.0, confidence + 0.25)
        basis.append(f"{cross} 个独立来源/出版方同时出现")
    elif cross == 2:
        score = min(1.0, score + 0.05)
        confidence = min(1.0, confidence + 0.15)
        basis.append("2 个独立来源/出版方同时出现")
    return round(min(1.0, score), 4), round(confidence, 2), basis


def _timeliness_dimension(candidate, now):
    published = _parse(candidate.get("published_at"))
    discovered = _parse(candidate.get("discovered_at") or candidate.get("last_seen_at"))
    if published is not None:
        hours = _age_hours(published, now)
        if hours <= 6:
            value, confidence = 1.0, 0.9
        elif hours <= 24:
            value, confidence = 0.8, 0.9
        elif hours <= 72:
            value, confidence = 0.5, 0.8
        elif hours <= 168:
            value, confidence = 0.25, 0.7
        else:
            value, confidence = 0.1, 0.7
        basis = [f"来源标注发布于 {hours:.1f} 小时前"]
        return value, confidence, basis, []
    if discovered is not None:
        hours = _age_hours(discovered, now)
        value = 0.8 if hours <= 24 else 0.5 if hours <= 72 else 0.25
        basis = [f"来源未提供发布时间，按首次采集时间估算（{hours:.1f} 小时前）"]
        return value, 0.4, basis, ["来源未提供发布时间，时效性为估算值"]
    return 0.0, 0.0, ["既无发布时间也无采集时间"], ["缺少任何时间信息"]


def _audience_dimension(direction, keywords):
    weight = DIRECTION_WEIGHTS.get(direction, 0.15)
    # 方向占比最高只给到 0.85，剩下 0.15 留给"命中自定义关键词"。
    # 否则最高占比方向（AI 0.5）一上来就满分，关键词是否命中在分数上完全
    # 看不出来——那等于把用户自己维护的关键词作废了。
    value = min(0.85, weight / 0.5 * 0.85)
    basis = [f"方向「{_label(direction)}」目标占比 {weight:.0%}"]
    if keywords:
        basis.append(f"命中自定义关键词 {len(keywords)} 个：{'、'.join(keywords[:5])}")
        value = min(1.0, value + 0.15 * min(1.0, len(keywords) / 3.0))
    return round(value, 4), 0.8, basis


def _value_dimension(tiers, summary, subtopics):
    best = max((_TIER_VALUE.get(tier, 0.3) for tier in tiers), default=0.3)
    basis = []
    if tiers:
        basis.append("来源分层：" + "、".join(sorted(set(tiers))))
    value = best
    length = len((summary or "").strip())
    if length >= 80:
        value = min(1.0, value + 0.1)
        basis.append(f"摘要 {length} 字，信息量较足")
    elif length:
        basis.append(f"摘要仅 {length} 字")
    else:
        basis.append("无摘要")
    if subtopics:
        basis.append("二级主题：" + "、".join(subtopics))
    return round(min(1.0, value), 4), 0.6, basis


def _originality_dimension(tiers, subtopics, publishers):
    """原创开发空间：已被多家媒体写过的事，可发挥的空间就小。"""
    value, basis = 0.5, []
    if "interpretation" not in tiers:
        value += 0.25
        basis.append("尚无中文解读层覆盖，可作第一手解读")
    else:
        basis.append("已有中文解读层覆盖，需找差异化角度")
    if "official" in tiers and "interpretation" not in tiers:
        value += 0.1
        basis.append("有官方原始发布，可基于原文做深度解读")
    if any(name in ("效率工具", "智能体与工具", "平台与开发", "消费与省钱") for name in subtopics):
        value += 0.15
        basis.append("属于教程/工具类题材，原创空间较大")
    count = len([item for item in publishers if item])
    if count <= 1:
        value += 0.1
        basis.append("目前只有单一来源，尚未被广泛覆盖")
    else:
        basis.append(f"已有 {count} 个出版方覆盖")
    return round(min(1.0, value), 4), 0.5, basis


def _credibility_dimension(tiers, publishers, url):
    best = max((_TIER_CREDIBILITY.get(tier, 0.3) for tier in tiers), default=0.3)
    basis = []
    if tiers:
        basis.append("最高来源分层：" + max(tiers, key=lambda t: _TIER_CREDIBILITY.get(t, 0.3)))
    value = best
    count = len([item for item in publishers if item])
    if count >= 3:
        value = min(1.0, value + 0.1)
        basis.append(f"{count} 个独立出版方")
    elif count == 2:
        basis.append("2 个独立出版方")
    else:
        basis.append("仅单一来源，未形成交叉印证")
    if not is_original_report(url):
        basis.append("链接不是可访问的原始报道页（榜单/聚合页或缺失）")
        value = min(value, 0.2)
    return round(min(1.0, value), 4), 0.7, basis


#: TrendRadar 各平台经同一个公益接口抓取，落到候选的 url 是这个接口地址，
#: 不是报道页——它和百度/头条热榜页一样属于"榜单"，不能当原始出处。
_AGGREGATOR_HOST = "newsnow.busiyi.world"


def is_original_report(url):
    """这条链接是不是可访问的原始报道页。

    复用 research.is_index（工作台判断"能不能开始写"的唯一判据），不另写
    一套 is_index 组合——两处判定漂移的代价是用户在这里看到"可发布"、
    到生产环节却被闸门拦下。
    """
    from urllib.parse import urlsplit
    from .research import is_index
    if not isinstance(url, str) or not url.strip():
        return False
    if is_index(url):
        return False
    return (urlsplit(url).hostname or "").lower() != _AGGREGATOR_HOST


def risk_flags(title, summary, tiers, publishers, url=""):
    """风险标记。只标风险，不下结论。"""
    flags = []
    text = f"{title or ''} {summary or ''}"
    hits = _hits(text, RISK_TERMS)
    if hits:
        flags.append("涉及高风险题材（" + "、".join(hits[:4]) + "），发稿前须核对官方通报与最新进展")
    if tiers and set(tiers) <= {"signal"}:
        flags.append("目前只有热榜线索，没有可访问的原始报道，不能当作已核实事实")
    if url and not is_original_report(url):
        flags.append("当前链接指向榜单或聚合页，不是可访问的原始报道，需补一条原文链接")
    if len([item for item in publishers if item]) <= 1:
        flags.append("仅单一来源，未形成交叉印证")
    return flags


# --------------------------------------------------------------------------
# 本地模型增强层：按需调用，失败一律回落到规则，不假装模型参与过
# --------------------------------------------------------------------------

#: 模型只能在这四个方向里选一个。用 enum 而不是自由文本，避免它编出第五类。
_MODEL_DIRECTIONS = (DIRECTION_AI, DIRECTION_TECH, DIRECTION_SOCIETY, DIRECTION_LIFE)

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": list(_MODEL_DIRECTIONS)},
        "subtopics": {"type": "array", "items": {"type": "string"}},
        "angle": {"type": "string"},
        "confidence": {"type": "integer"},
        "cautions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "subtopics", "angle", "confidence", "cautions"],
    "additionalProperties": False,
}


def classify_instruction(source_directions=()):
    declared = "、".join(str(item) for item in (source_directions or [])) or "未声明"
    return (
        "你是新媒体账号的内容方向判断助手。账号发文目标比例：AI 核心 50%、"
        "科技与互联网 25%、社会热点与个人观点 15%、生活观察与跨界 10%。\n"
        f"采集来源声明的方向是：{declared}。\n"
        "请判断这条线索最适合归到哪一个方向（只能从给定枚举里选一个），"
        "给出不超过 2 个二级主题、一句话的内容切入角度、0 到 100 的整数把握度，"
        "以及需要作者注意的风险提示（没有就给空数组）。\n"
        "严格依据给定的标题与摘要，不要补充你自己的知识，不要编造标题里没有的事实、"
        "数据或结论。信息不足以判断时把把握度调低，并把疑问写进 cautions。\n"
        "只返回 JSON，不要输出思考过程。\n"
        f"必须符合此 JSON Schema：{json.dumps(_CLASSIFY_SCHEMA, ensure_ascii=False)}"
    )


#: 分类任务的 token 预算。实测（本机 qwen3.5-9b）：给 600 时推理型模型会把
#: 预算花在思考上，输出被判为截断，三次里失败两次；给 2000 后三次全通过。
#: 输出本身不到 100 token，多出来的全是推理开销，不是为了让它多写。
CLASSIFY_MAX_TOKENS = 2000
CLASSIFY_TEMPERATURE = 0.2


def build_classify_request(candidate, model, source_directions=()):
    body = (f"标题：{candidate.get('title') or ''}\n"
            f"摘要：{(candidate.get('summary') or '')[:400]}\n")
    return {'model': model,
            'messages': [{'role': 'user', 'content': body + classify_instruction(source_directions)}],
            'temperature': CLASSIFY_TEMPERATURE, 'max_tokens': CLASSIFY_MAX_TOKENS, 'stream': True,
            'response_format': {'type': 'json_schema',
                                'json_schema': {'name': 'radar_classification', 'strict': True,
                                                'schema': _CLASSIFY_SCHEMA}}}


def validate_classification(payload):
    """校验模型输出。任何一条不满足就整体丢弃，不做静默纠正。

    静默纠正会把"模型说的"和"界面显示的"拆成两件事，结论就无法追溯。
    """
    if not isinstance(payload, dict):
        raise ValueError("分类结果不是对象")
    direction = payload.get('direction')
    if direction not in _MODEL_DIRECTIONS:
        raise ValueError(f"方向 {direction!r} 不在四方向枚举内")
    raw_subtopics = payload.get('subtopics')
    if not isinstance(raw_subtopics, list):
        raise ValueError("二级主题必须是数组")
    subtopics = [str(item).strip()[:20] for item in raw_subtopics if str(item).strip()][:2]
    angle = payload.get('angle')
    if not isinstance(angle, str) or not angle.strip():
        raise ValueError("缺少内容切入角度")
    confidence = payload.get('confidence')
    if type(confidence) is not int or not 0 <= confidence <= 100:
        raise ValueError("把握度必须是 0 到 100 的整数")
    raw_cautions = payload.get('cautions')
    if not isinstance(raw_cautions, list):
        raise ValueError("风险提示必须是数组")
    cautions = [str(item).strip()[:120] for item in raw_cautions if str(item).strip()][:3]
    return {'direction': direction, 'subtopics': subtopics, 'angle': angle.strip()[:200],
            'confidence': confidence, 'cautions': cautions}


def classify_with_model(candidate, *, ask_model, model, parse_json):
    """用本地模型复核分类。失败一律返回 None，由调用方回落到规则结论。

    `ask_model(request)` 返回模型原始文本，`parse_json(raw)` 把文本解析成
    对象。两者都从外部注入 —— 本模块不自己发请求，否则「模型服务不可用」
    与「模型返回无效」两种失败会混在一起，降级路径就分不清了。
    """
    request = build_classify_request(candidate, model,
                                     candidate.get('directions') or [])
    try:
        raw = ask_model(request)
        payload = parse_json(raw)
        return validate_classification(payload)
    except Exception:
        return None


def evaluate(candidate, observations=None, now=None, keywords=None,
             model_classification=None):
    """产出六维评分、方向分类与风险标记。

    返回总分（0–100，整数便于排序）与逐维依据；`data_gaps` 列出所有用估算
    代替的地方，`estimated` 为 True 时界面必须显式标注"估算/数据不足"。

    `model_classification` 传入时用它覆盖方向分类（仍然只覆盖分类，不覆盖
    六个维度的分数——维度分数必须由可核对的事实推导，不能让模型随手给）。
    """
    moment = now or datetime.now(timezone.utc)
    records = [item for item in (observations or []) if isinstance(item, dict)]
    title = str(candidate.get("title") or "")
    summary = str(candidate.get("summary") or "")
    tiers = [item.get("tier") for item in records if item.get("tier")]
    publishers = {item.get("publisher_id") for item in records if item.get("publisher_id")}

    classification = classify(title, summary, candidate.get("directions") or [])
    model_note = None
    if model_classification:
        direction = model_classification["direction"]
        model_note = {
            "angle": model_classification["angle"],
            "confidence": model_classification["confidence"],
            "cautions": model_classification["cautions"],
        }
        rest = [item for item in classification["directions"] if item != direction]
        classification = {
            "direction": direction,
            "directions": [direction] + rest,
            "subtopics": model_classification["subtopics"] or classification["subtopics"],
            "method": "model",
            "basis": [f"本地模型判定为{_label(direction)}，把握度 {model_classification['confidence']}",
                      "规则基线：" + "；".join(classification["basis"])],
        }
    matched_keywords = [word for word in (keywords or []) if word and word.casefold() in _tokens(title + " " + summary)]

    heat, heat_conf, heat_basis = _heat_dimension(records)
    time_value, time_conf, time_basis, time_gaps = _timeliness_dimension(candidate, moment)
    audience, aud_conf, aud_basis = _audience_dimension(classification["direction"], matched_keywords)
    value, val_conf, val_basis = _value_dimension(tiers, summary, classification["subtopics"])
    originality, org_conf, org_basis = _originality_dimension(tiers, classification["subtopics"], publishers)
    credibility, cred_conf, cred_basis = _credibility_dimension(tiers, publishers, candidate.get("url"))

    dimensions = {
        "heat": (heat, heat_conf, heat_basis),
        "timeliness": (time_value, time_conf, time_basis),
        "audience": (audience, aud_conf, aud_basis),
        "value": (value, val_conf, val_basis),
        "originality": (originality, org_conf, org_basis),
        "credibility": (credibility, cred_conf, cred_basis),
    }
    total = 0.0
    breakdown = []
    for key, (score, confidence, basis) in dimensions.items():
        weight = WEIGHTS[key]
        total += score * weight
        breakdown.append({"key": key, "label": DIMENSION_LABELS[key], "weight": weight,
                          "score": round(score, 4), "confidence": confidence, "basis": basis})

    data_gaps = list(time_gaps)
    if not records:
        data_gaps.append("没有采集记录，热度与可信度无法评估")
    if not candidate.get("published_at"):
        data_gaps.append("来源未提供事件发布时间")
    if not summary:
        data_gaps.append("无摘要，内容价值只能按来源分层粗略判断")

    flags = risk_flags(title, summary, tiers, publishers, candidate.get("url") or "")
    # 评分与可发布是两道闸：分数再高，未核实或高风险的内容也不自动放行。
    # 热榜只说明"有人在看"，不说明"确有此事"，所以只有热榜线索的一律
    # 留在"待核实"这一侧，由人工补原始出处后再决定。
    cross = len([item for item in publishers if item])
    unverified = (bool(tiers) and set(tiers) <= {"signal"}) or not is_original_report(
        candidate.get("url") or "")
    high_risk = any("高风险题材" in flag for flag in flags)
    if high_risk:
        publishable = not (unverified or cross <= 1)
        reason = "" if publishable else "高风险题材缺少独立原始出处，仅凭热榜不作为可发布推荐"
    elif unverified:
        publishable = False
        reason = ("当前链接是榜单或聚合页，不是可访问的原始报道，需人工补一条原文链接"
                  if not is_original_report(candidate.get("url") or "")
                  else "目前只有热榜线索，缺少可访问的原始报道，不自动推荐为可发布内容")
    else:
        publishable = True
        reason = ""

    return {
        "total": round(total * 100),
        "dimensions": breakdown,
        "direction": classification["direction"],
        "subtopics": classification["subtopics"],
        "content_lines": content_lines(title, summary),
        "classification_method": classification["method"],
        "classification_basis": classification["basis"],
        "model": model_note,
        "risk_flags": flags,
        "publishable": publishable,
        "publishable_reason": reason,
        "data_gaps": data_gaps,
        "estimated": bool(data_gaps),
    }
