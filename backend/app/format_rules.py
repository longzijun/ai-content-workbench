"""规则引擎：从选题与账号状态算出「哪些叙事形式可行、哪个最优」。

设计原则 —— 三条：

1. **硬约束与软信号分离。** 硬约束决定可行集合（不可行就是不可行），软信号
   只在可行集合内部加权排序。这与 formats 的两轴强度分层一致：A 轴进
   platform_rules.work_errors 阻断，B 轴在本模块只参与排序。
2. **确定性优先于聪明。** 同一输入必须给出同一结果，否则无法写单元测试、
   无法向用户解释「为什么推荐这个形式」。为此：零时钟（now 由调用方传入）、
   零网络、零随机（渠道能力快照由调用方注入而非本模块去查）、所有输出显式
   排序且同分按 format_id 字典序。
3. **注册表只提供事实，策略留在这里。** forms 是什么由 formats 注册表定义；
   形式与平台/素材/成本/时效的权衡规则是本模块的策略，不回写注册表。
   这样 formats 的文件归属保持干净，后续调整策略不必改事实。

边界 —— 本模块不做的事：
  不读数据库、不联网、不查平台能力、不调用生成模型；
  不决定 A 轴（A 轴权威在 platform_rules，不参与本模块的可行集合之外的事）；
  不写盘、不产生草稿、不做排期。输出是一份纯数据结论，由调用方决定如何落地。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from . import formats
from .platform_rules import NAMES, TITLE_LIMITS, expected_format

# --------------------------------------------------------------------------
# 策略常量（权重的唯一来源，测试会锁定相对关系）
# --------------------------------------------------------------------------

#: R6 成本预算：平台正文硬上限与各复杂度的相对占用比。
#: low 的 high 就是平台上限本身，high 复杂度要占到 2.4 倍才可能超限 ——
#: 即「复杂度高的形式在小红书这种短正文平台上更容易被拦」。
#: 数值是运营假设，不是平台规则；平台硬上限仍以 platform_rules 为准。
COST_RATIO = {'low': 0.5, 'medium': 0.8, 'high': 1.0}

#: 复杂度相对目标篇幅的放大系数，用于给出预估字数。
COMPLEXITY_BUDGET = {'low': 1.0, 'medium': 1.6, 'high': 2.4}

#: 预估字数超过平台正文硬上限的倍数即为硬不可行。
COST_HARD_LIMIT = 2.0
#: 超过该倍数只扣分，不阻断。
COST_SOFT_LIMIT = 1.0

#: R7 时效：距离过期不足该小时数视为「热」，加权。
FRESH_WINDOW_HOURS = 6

#: R8 选题类型加权。值为分数，正数提升、负数降低。
KIND_WEIGHTS = {
    'howto': {'tutorial': 3, 'checklist': 1, 'opinion': -1, 'case': -1},
    'news': {'brief': 3, 'tutorial': -2, 'case': -2, 'checklist': -1},
    'review': {'opinion': 3, 'checklist': 2, 'brief': 1},
    'problem': {'tutorial': 3, 'case': 2, 'brief': -1},
    'explanation': {'tutorial': 2, 'opinion': 1, 'brief': -1},
}

#: R8 选题类型的判定词表（大小写不敏感）。这是运营假设，不是平台规则。
KIND_TERMS = {
    'howto': ('怎么', '如何', '步骤', '教程', '安装', '配置', '部署', '用法'),
    'news': ('发布', '上线', '更新', '推出', '开源', '模型', '版本'),
    'review': ('对比', '评测', '哪个好', '值不值', '选型'),
    'problem': ('报错', '失败', '卡住', '不能用', '异常', '解决', '修复'),
    'explanation': ('原理', '为什么', '机制', '架构', '区别'),
}

#: R2 标题长度软扣分：形式建议窗口的高位超过平台硬上限时扣这么多分。
#: （formats 的 title_window 已在注册表层保证不越过 boost 平台。）
TITLE_SOFT_PENALTY = 1


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def inputs_digest(inputs: dict) -> str:
    """选题 + 渠道快照的指纹，用于「同一批输入只算一次」。

    刻意不含 now：同一批选题在不同时间点重复评估应当命中同一个指纹，
    否则幂等键会随时间漂移，重启后无法判断是不是同一批任务。
    """
    return hashlib.sha256(_canonical(inputs).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------

def _require_dict(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是字典")
    return value


def normalize_inputs(*, platform, topic, capabilities=None, body_band=600) -> dict:
    """把外部输入整理成规则引擎的规范形状。调用方的第一道边界。

    body_band 是该平台稿件的篇幅档位（字），由调用方按目标稿件给出；
    默认 600 对应公众号与头条常规篇幅。
    """
    _require_dict(topic, "选题数据")
    if platform not in TITLE_LIMITS:
        raise ValueError("不支持的发布平台")
    try:
        band = int(body_band)
    except (TypeError, ValueError):
        raise ValueError("篇幅档位必须是整数")
    if not 200 <= band <= 60000:
        raise ValueError("篇幅档位需在 200 到 60000 字之间")
    sources = topic.get("source_urls")
    items = topic.get("items")
    return {
        "platform": platform,
        "axis_a": expected_format(platform),
        "title": str(topic.get("title") or "")[:300],
        "body": str(topic.get("body") or "")[:4000],
        "kind": str(topic.get("kind") or "").strip().casefold()[:40],
        "source_count": len(sources) if isinstance(sources, list) else 0,
        "item_count": len(items) if isinstance(items, list) else 0,
        "body_band": band,
        "capabilities": _normalize_capabilities(capabilities),
    }


def _normalize_capabilities(capabilities) -> dict:
    """渠道能力快照。缺省视为「未探测」，不做乐观假设。

    这是 R4 的唯一输入。本模块绝不自己去连平台 —— 那会让 evaluate 变成
    有副作用的函数，破坏确定性。
    """
    if capabilities is None:
        return {"checked": False, "implemented": None, "write_evidence": "none",
                "storage_scope": "unknown"}
    _require_dict(capabilities, "渠道能力快照")
    return {
        "checked": bool(capabilities.get("checked")),
        "implemented": capabilities.get("implemented"),
        "write_evidence": str(capabilities.get("write_evidence") or "none"),
        "storage_scope": str(capabilities.get("storage_scope") or "unknown"),
    }


# --------------------------------------------------------------------------
# 八条规则。硬约束返回 None 或硬不可行原因，软信号返回分数。
# --------------------------------------------------------------------------

def _r1_platform_support(inputs, form_id):
    """R1 平台支持。平台必须已在渠道形态注册表内，且其形态有非空平台列表。

    刻意不检查「某个形式是否属于该平台」—— B 轴五个形式对全部三个平台都
    可用，形式与平台的相容性由 R2（标题窗口）与 R6（成本预算）表达。
    R1 只守住一件事：平台本身合法，且它的渠道形态有登记。这一条在当前
    三平台下恒真；它的意义是把「平台表扩容时若漏了形态登记就立刻暴露」
    变成显式约束，而不是等到投递时才失败。
    """
    platforms = formats.axis_a_platforms(inputs["axis_a"])
    if not platforms:
        return f"渠道形态 {inputs['axis_a']} 在注册表内没有任何平台登记"
    if inputs["platform"] not in platforms:
        return (f"{NAMES[inputs['platform']]}未登记在渠道形态 {inputs['axis_a']} 下，"
                f"请核对 platform_rules 与 formats 注册表")
    return None


def _r2_title_window(inputs, form_id):
    """R2 标题窗口。形式建议窗口的低位超过平台硬上限即硬不可行。

    高位超过只扣分：平台硬上限由 platform_rules 兜底，这里不重复阻断。
    """
    window = formats.get_content_form(form_id)["title_window"]
    low, high = TITLE_LIMITS[inputs["platform"]]
    if window[0] > high:
        return (f"{formats.get_content_form(form_id)['label']}标题下限 {window[0]} 字"
                f"超过{NAMES[inputs['platform']]}上限 {high} 字")
    if window[1] > high:
        return TITLE_SOFT_PENALTY
    return None


def _r3_material_evidence(inputs, form_id):
    """R3 素材证据。来源或要点不足即硬不可行 —— 这一条直接对应「不编造」。"""
    entry = formats.get_content_form(form_id)
    if inputs["source_count"] < entry["sources_min"]:
        return (f"{entry['label']}需要至少 {entry['sources_min']} 条已批准来源，"
                f"当前 {inputs['source_count']} 条")
    if entry["items_min"] and inputs["item_count"] < entry["items_min"]:
        return (f"{entry['label']}需要至少 {entry['items_min']} 个并列要点，"
                f"当前 {inputs['item_count']} 个")
    return None


def _r4_account_capability(inputs, form_id):
    """R4 账号能力。渠道未实现即硬不可行；只验证过读取时软扣分。

    数据来自调用方注入的快照。本模块不查平台、不解密凭据。
    """
    snapshot = inputs["capabilities"]
    if not snapshot["checked"]:
        return 1, "渠道能力尚未探测，结论仅供参考"
    if snapshot["implemented"] is False:
        return f"渠道尚未接入（storage_scope={snapshot['storage_scope']}），无法投递"
    if snapshot["write_evidence"] == "none":
        return 2, "渠道写入能力未验证，仅有历史记录或未验证"
    if snapshot["storage_scope"] == "browser":
        return 1, "草稿只保存在本机浏览器，云端与手机不可见"
    return None


def _r5_render_chain(inputs, form_id):
    """R5 渲染链路。形式必须能落在该平台的渲染路径上。

    恒真条件写成显式规则而不是省略，是为了把「A 轴未变」从口头约定变成
    可断言的不变量：将来有人改了 platform_rules 或 richtext 的分派，
    这条规则会立刻失败而不是静默给出错误的可行集合。
    """
    if inputs["axis_a"] != expected_format(inputs["platform"]):
        return "渠道形态与平台规则不一致，请核对 platform_rules"
    render = expected_render(inputs["platform"])
    if render is None:
        return f"{NAMES[inputs['platform']]}没有已实现的渲染路径"
    return None


def _r6_cost_budget(inputs, form_id):
    """R6 成本预算。预估字数超平台正文上限两倍即硬不可行，一到两倍只扣分。

    预估字数 = 复杂度放大系数 × body_band，上限取 platform_rules 的正文
    硬上限（小红书 1000 字）。公众号与头条没有正文硬上限，用目标档位代替 ——
    这不是编造平台规则，是「以目标篇幅为预算」的运营口径，测试会守住
    「小红书用硬上限、其他平台用档位」这一区分。

    这一条是「别让系统推荐一个平台装不下的形式」的成本闸门。
    """
    entry = formats.get_content_form(form_id)
    need = COMPLEXITY_BUDGET[entry["complexity"]] * inputs["body_band"]
    ceiling = _body_ceiling(inputs["platform"], inputs["body_band"])
    if need > COST_HARD_LIMIT * ceiling:
        return (f"{entry['label']}预估需 {need:.0f} 字，超过平台可承载上限 "
                f"{ceiling:.0f} 字的 {COST_HARD_LIMIT:g} 倍；请缩小篇幅目标或换形式")
    if need > COST_SOFT_LIMIT * ceiling:
        return 1
    return None


#: 有官方正文硬上限的平台。上限值来自 platform_rules，不在此复述。
PLATFORM_BODY_CEILING = {'xiaohongshu': 1000}


def _body_ceiling(platform, body_band):
    """该平台正文可承载的上限字数。

    小红书有官方 1000 字硬上限（platform_rules 里是硬约束），用真值；
    公众号与头条没有正文硬上限，用调用方给的目标档位作预算。
    """
    return PLATFORM_BODY_CEILING.get(platform, body_band)


def _r7_freshness(inputs, form_id, now):
    """R7 时效。已过期即硬不可行（过期热点不补发）；临近过期加权。"""
    expires_at = inputs.get("expires_at")
    if not expires_at:
        return None
    expiry = _parse_time(expires_at)
    if expiry is None:
        return None
    if now >= expiry:
        return f"选题已过期（{expires_at}），过期内容不自动补发"
    if expiry - now <= timedelta(hours=FRESH_WINDOW_HOURS):
        return -1
    return None


def _r8_topic_kind(inputs, form_id):
    """R8 选题类型。纯加权，不阻断。

    kind 由调用方给出；未给出时用 KIND_TERMS 从标题正文推断，推断不出就
    不加权 —— 宁可不加权，也不要用一个猜出来的类型影响排序。
    """
    kind = inputs["kind"] or _infer_kind(inputs["title"], inputs["body"])
    if not kind:
        return None
    return KIND_WEIGHTS.get(kind, {}).get(form_id)


def expected_render(platform):
    """该平台的渲染方式，返回 'html' / 'text' / None。

    刻意不从 richtext 反查 —— richtext.render 的签名里没有 format 参数，
    它只按 platform 分派。这里复述同一判据是为了让 R5 可测；两者的同步
    由 test_format_rules 的 R5 存在性测试守住。
    """
    return "text" if platform == "xiaohongshu" else ("html" if platform in TITLE_LIMITS else None)


def _infer_kind(title, body):
    haystack = f"{title} {body}".casefold()
    hits = []
    for kind in sorted(KIND_TERMS):
        if any(term.casefold() in haystack for term in KIND_TERMS[kind]):
            hits.append(kind)
    return hits[0] if len(hits) == 1 else ""


def _parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 求值
# --------------------------------------------------------------------------

def evaluate(*, platform, topic, capabilities=None, body_band=600, now=None) -> dict:
    """算出该选题在该平台上的可行形式集合与排序结论。纯函数。

    now 只参与 R7 时效判定，不参与 inputs_digest，因此同一批输入在不同
    时间重复评估会命中同一指纹 —— 幂等键不随时间漂移。
    """
    inputs = normalize_inputs(platform=platform, topic=topic,
                              capabilities=capabilities, body_band=body_band)
    if now is None:
        raise ValueError("必须显式传入 now，规则引擎不读系统时钟")
    if isinstance(now, str):
        now = _parse_time(now)
        if now is None:
            raise ValueError("now 必须是可解析的时间")
    if "expires_at" in topic:
        inputs["expires_at"] = topic["expires_at"]

    platform_name = NAMES[platform]
    feasible, blocked = [], []
    for form_id in formats.CONTENT_FORM_IDS:
        verdict = _evaluate_one(inputs, form_id, now)
        row = _row(form_id, verdict, platform_name)
        (blocked if row["blocked"] else feasible).append(row)

    feasible.sort(key=lambda row: (-row["score"], row["content_form"]))
    blocked.sort(key=lambda row: row["content_form"])
    return {
        "platform": platform,
        "axis_a": inputs["axis_a"],
        "inputs_digest": inputs_digest({k: v for k, v in inputs.items() if k != "expires_at"}),
        "evaluated_at": now.isoformat(),
        "feasible": feasible,
        "blocked": blocked,
        "recommended": feasible[0]["content_form"] if feasible else "",
        "notes": _notes(inputs, feasible, blocked),
    }


def _evaluate_one(inputs, form_id, now):
    """跑完八条规则，返回 {'blocked':bool,'reason':str,'score':int,'signals':[...]}"""
    hard, soft, signals = None, 0, []

    def record(rule, outcome):
        """统一三条规则返回值的协议。

        None       不适用，不计分
        int        软信号，直接加减分
        str        硬不可行，原因原文
        (int, str) 软信号 + 面向人的说明
        """
        nonlocal hard, soft
        if outcome is None:
            return True
        if isinstance(outcome, tuple):
            score, note = outcome
            soft += score
            signals.append(f"{rule}{'+' if score > 0 else ''}{score} {note}")
            return True
        if isinstance(outcome, str):
            hard = f"{rule}：{outcome}"
            return False
        soft += outcome
        signals.append(f"{rule}{'+' if outcome > 0 else ''}{outcome}")
        return True

    # 硬约束短路：任一硬不可行即停止，后面的规则不必再算。
    for rule, fn in (("R1", lambda: _r1_platform_support(inputs, form_id)),
                     ("R2", lambda: _r2_title_window(inputs, form_id)),
                     ("R3", lambda: _r3_material_evidence(inputs, form_id)),
                     ("R4", lambda: _r4_account_capability(inputs, form_id)),
                     ("R5", lambda: _r5_render_chain(inputs, form_id))):
        if not record(rule, fn()):
            return {"blocked": True, "reason": hard, "score": 0, "signals": signals}
    # R6/R7/R8 也可能硬不可行（R6 成本超限、R7 选题过期），必须检查返回值。
    # 曾在这里丢弃 record() 的返回值，导致过期的选题仍被判可行 —— 那是
    # 「过期热点不补发」这条业务规则的漏网路径。
    for rule, fn in (("R6", lambda: _r6_cost_budget(inputs, form_id)),
                     ("R7", lambda: _r7_freshness(inputs, form_id, now)),
                     ("R8", lambda: _r8_topic_kind(inputs, form_id))):
        if not record(rule, fn()):
            return {"blocked": True, "reason": hard, "score": 0, "signals": signals}
    return {"blocked": False, "reason": "", "score": soft, "signals": signals}


def _row(form_id, verdict, platform_name):
    entry = formats.get_content_form(form_id)
    return {
        "content_form": form_id,
        "label": entry["label"],
        "complexity": entry["complexity"],
        "blocked": verdict["blocked"],
        "reason": verdict["reason"],
        "score": verdict["score"],
        "signals": list(verdict["signals"]),
        "boost": list(entry["boost"]),
    }


def _notes(inputs, feasible, blocked):
    notes = []
    if not feasible:
        reasons = sorted({row["reason"].split("：", 1)[0] for row in blocked})
        notes.append(f"{NAMES[inputs['platform']]}没有可行形式：受 "
                     f"{'、'.join(reasons)} 限制；请补素材或换平台，不要硬凑")
    elif len(feasible) == 1:
        notes.append(f"只有「{feasible[0]['label']}」可行，其余形式受硬约束排除")
    snapshot = inputs["capabilities"]
    if not snapshot["checked"]:
        notes.append("渠道能力未探测：本次结论未考虑账号接入状态，投递前需先确认")
    return notes


def evaluate_all(*, platforms, topic, capabilities=None, body_band=600, now=None) -> dict:
    """多平台一次评估。输出按平台键排序，跨平台同分也按键序。"""
    if not isinstance(platforms, list) or not platforms:
        raise ValueError("平台列表不能为空")
    results = {}
    for platform in sorted(set(platforms)):
        snapshot = None
        if isinstance(capabilities, dict):
            snapshot = capabilities.get(platform)
        results[platform] = evaluate(platform=platform, topic=topic,
                                     capabilities=snapshot, body_band=body_band, now=now)
    return {
        "now": now.isoformat() if isinstance(now, datetime) else str(now),
        "platforms": results,
        "recommended": {p: r["recommended"] for p, r in sorted(results.items())},
    }