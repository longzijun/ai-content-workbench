"""三平台差异化匹配与推荐。

只做推荐，不做创作：本模块不建选题、不生成草稿、不碰平台账号。它回答的是
"这条线索更适合在哪写、写成什么、要准备什么"，**回不回答"写不写"——那是人的事**。

三条设计约束：

1. **每个匹配度都给依据**。三个平台各给 `fit` 与 `basis`，界面上要能追问
   "为什么是它"，不能只丢一个排序结果。
2. **建议形式复用既有的叙事形式目录**（`formats.CONTENT_FORM_IDS`）。另起一套
   形式词表会让推荐出来的形式在生成端不存在，推荐就落不了地。
3. **推荐不等于批准**。`confirmed_required` 恒为 True；没有人工确认，这条推荐
   不会变成任何创作动作。
"""
from __future__ import annotations

from .platform_rules import NAMES, TITLE_LIMITS
from .trend_sources import (DIRECTION_AI, DIRECTION_LIFE,
                            DIRECTION_SOCIETY, DIRECTION_TECH)

#: 三平台画像。偏好来自任务书给的定位，不是拍脑袋的权重。
#: preferred_directions 的取值是"该方向在这个平台的相对契合度"（0..1）。
PLATFORM_PROFILES = {
    'wechat': {
        'name': NAMES['wechat'],
        'positioning': '干货、深度解读、系列、教程、科技解读、有价值的观点',
        'preferred_directions': {DIRECTION_AI: 1.0, DIRECTION_TECH: 0.75,
                                 DIRECTION_SOCIETY: 0.35, DIRECTION_LIFE: 0.30},
        'preferred_subtopics': ('模型发布', '研究与论文', '智能体与工具', '基础设施',
                                '平台与开发', '行业与商业', '舆论与观点', '跨界思考'),
        'preferred_forms': ('tutorial', 'case', 'opinion'),
        'avoid_forms': ('brief',),
        'needs_depth': True,
        'title_patterns': ('把结论放在前半句，如「X 到底改变了什么」',
                           '用「怎么做 / 为什么」句式，突出可复用方法'),
    },
    'toutiao': {
        'name': NAMES['toutiao'],
        'positioning': '社会、科技、评论、讨论、时效性短内容',
        'preferred_directions': {DIRECTION_SOCIETY: 1.0, DIRECTION_TECH: 0.8,
                                 DIRECTION_AI: 0.6, DIRECTION_LIFE: 0.35},
        'preferred_subtopics': ('公共事件', '政策与治理', '民生与经济', '舆论与观点',
                                '产品与硬件', '行业与商业', '安全与风险'),
        'preferred_forms': ('brief', 'opinion'),
        'avoid_forms': ('tutorial',),
        'needs_depth': False,
        'title_patterns': ('点明事件与争议点，留出讨论空间',
                           '用「怎么看 / 谁受影响」句式，短平快'),
    },
    'xiaohongshu': {
        'name': NAMES['xiaohongshu'],
        'positioning': '工具实测、免费方案、教程、省钱避坑、生活技巧、视觉化',
        'preferred_directions': {DIRECTION_LIFE: 1.0, DIRECTION_AI: 0.7,
                                 DIRECTION_TECH: 0.55, DIRECTION_SOCIETY: 0.20},
        'preferred_subtopics': ('效率工具', '消费与省钱', '生活观察', '智能体与工具',
                                '基础设施', '跨界思考'),
        'preferred_forms': ('tutorial', 'checklist', 'case'),
        'avoid_forms': ('brief',),
        'needs_depth': False,
        'title_patterns': ('用「亲测 / 免费 / 避坑」这类可感词开头',
                           '标题里给出明确收益，如「3 步搞定 X」'),
    },
}

PLATFORM_IDS = ('wechat', 'toutiao', 'xiaohongshu')


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _tier_strength(tiers):
    """来源分层强度。热榜只有线索，官方发布能直接支撑深度内容。"""
    if 'official' in tiers:
        return 1.0
    if 'interpretation' in tiers:
        return 0.7
    return 0.35


def _materials(platform, tier_strength, subtopics, has_original):
    """所需素材。说清楚"还缺什么"，而不是笼统写"配图"。"""
    items = []
    if not has_original:
        items.append('补一条可访问的原始报道链接（当前只有热榜线索）')
    if platform == 'xiaohongshu':
        items.append('配图 3–6 张：实测截图或成品图，首图决定点击')
        items.append('正文控制在 1000 字内，步骤拆成短句')
    if platform == 'wechat':
        items.append('需要可展开的深度材料：原文要点、数据出处、自己的判断')
        if tier_strength < 0.7:
            items.append('至少补充 1 个权威来源，否则深度立不住')
    if platform == 'toutiao':
        items.append('时效优先：事件最新进展与官方通报')
        items.append('准备一个明确的讨论点，评论区才有东西接')
    if '效率工具' in subtopics or '智能体与工具' in subtopics:
        items.append('工具名、版本与操作步骤（能照着复现）')
    if '研究与论文' in subtopics:
        items.append('论文原文链接与一两句人话解读')
    return items


def _difficulty(platform, form_id, tier_strength, summary_length):
    """创作难度。复用叙事形式的复杂度作为基线，再用材料齐备度修正。"""
    from .formats import get_content_form
    form = get_content_form(form_id)
    base = {'low': 0.25, 'medium': 0.5, 'high': 0.8}.get(form.get('complexity'), 0.5)
    value = base
    if tier_strength < 0.7:
        value += 0.1
    if summary_length < 60:
        value += 0.1
    value = _clamp(value)
    level = 'low' if value < 0.4 else 'medium' if value < 0.7 else 'high'
    labels = {'low': '低', 'medium': '中', 'high': '高'}
    return {'level': level, 'label': labels[level], 'value': round(value, 2),
            'basis': f"形式「{form.get('label', form_id)}」复杂度 {form.get('complexity')}"
                     + ('，材料尚不齐备' if tier_strength < 0.7 or summary_length < 60 else '，材料较齐备')}


def _form_choice(platform, kinds, subtopics, tier_strength):
    """选形式并给出契合强度（0..1）。取值必须是生成端真实存在的 id。

    打分复用 `format_rules.KIND_WEIGHTS`（题材 → 各形式权重，负值即不合适），
    不自己编一套偏好：否则会出现"地震速报被推荐写成步骤教程"这种
    推荐与生成端判断对不上的结果。多题材命中时权重累加。
    """
    from .formats import CONTENT_FORM_IDS
    from .format_rules import KIND_WEIGHTS
    profile = PLATFORM_PROFILES[platform]
    preferred = [item for item in profile['preferred_forms'] if item in CONTENT_FORM_IDS]
    if not preferred:
        return CONTENT_FORM_IDS[0], '平台无显式偏好，取默认形式', 0.4
    # 题材明确的强规则先于权重：这些组合是刻意指定的，不该被通用权重盖掉。
    if '研究与论文' in subtopics and 'case' in preferred:
        return 'case', '论文/研究类题材适合拆成案例讲', 0.9
    if '效率工具' in subtopics and 'tutorial' in preferred:
        return 'tutorial', '工具题材写成步骤教程最容易被收藏', 0.9
    if '公共事件' in subtopics:
        if 'brief' in preferred:
            return 'brief', '公共事件先出要点速报，抢时效', 0.9
        if 'opinion' in preferred:
            return 'opinion', '公共事件在以深度为主的平台上适合做观点解读', 0.7
    if tier_strength < 0.7:
        if 'brief' in preferred:
            return 'brief', '只有热榜线索时先做速报，别硬写深度', 0.7
        if 'opinion' in preferred:
            return 'opinion', '只有热榜线索时写观点短评，不装成深度教程', 0.6
    scores = {}
    for kind in kinds:
        for form, weight in KIND_WEIGHTS.get(kind, {}).items():
            scores[form] = scores.get(form, 0) + weight
    if scores:
        best = max(preferred, key=lambda form: scores.get(form, 0))
        raw = scores.get(best, 0)
        # 权重表取值大致落在 -2..3，线性映射到 0..1 供匹配度使用。
        strength = _clamp((raw + 2) / 5)
        if raw > 0:
            return best, f"题材「{'、'.join(kinds)}」与该形式的契合权重最高（{raw}）", strength
        # 所有形式都不被这个题材看好时，也要取"最不违和"的那个，而不是硬回
        # 平台首选——否则新闻题材在公众号上会被推荐成步骤教程。
        from .formats import get_content_form
        label = get_content_form(best).get('label', best)
        return best, (f"题材「{'、'.join(kinds)}」没有明确合适的形式，"
                      f"取相对最不违和的「{label}」"), strength
    return preferred[0], '题材类型不明确，按平台首选形式', 0.4


def _infer_kinds(title, summary):
    """命中的题材类型（可能多个）。复用 format_rules.KIND_TERMS 这张表，
    不另写一套关键词——两处词表漂移的后果是推荐的形式与生成端的判断对不上。

    生成端只在唯一命中时采用；推荐端保留全部命中并累加权重，因为真实标题
    经常同时命中两类（「两个模型对比评测」既是 review 又是 news），
    只取唯一命中会让大量标题落到"按平台首选"的兜底上。
    """
    from .format_rules import KIND_TERMS
    haystack = f"{title or ''} {summary or ''}".casefold()
    return [kind for kind in sorted(KIND_TERMS)
            if any(term.casefold() in haystack for term in KIND_TERMS[kind])]


def _platform_fit(platform, direction, subtopics, tier_strength, fresh_hours,
                  has_original, summary_length, form_fit=0.4, form_label=''):
    profile = PLATFORM_PROFILES[platform]
    basis = []
    direction_fit = profile['preferred_directions'].get(direction, 0.3)
    basis.append(f"方向契合 {direction_fit:.0%}（{profile['positioning']}）")

    hits = [name for name in subtopics if name in profile['preferred_subtopics']]
    subtopic_fit = min(1.0, 0.35 + 0.35 * len(hits)) if hits else 0.15
    basis.append(f"二级主题命中 {len(hits)} 个：{'、'.join(hits) or '无'}")

    # 材料支撑。非深度平台给一点补偿，但不能太多：+0.2 会让时效型平台在
    # 所有题材上系统性领先，深度题材就被错误地推去发快讯了。
    depth_fit = tier_strength if profile['needs_depth'] else min(1.0, tier_strength + 0.1)
    basis.append('材料支撑' + ('足够写深度' if depth_fit >= 0.7 else '偏薄，宜短平快'))

    basis.append(f"形式契合 {form_fit:.0%}（{form_label}）")

    if fresh_hours is None:
        fresh_fit = 0.4
        basis.append('无发布时间，时效按采集时间估算')
    elif fresh_hours <= 24:
        fresh_fit = 1.0 if not profile['needs_depth'] else 0.8
        basis.append(f"{fresh_hours:.0f} 小时内，{'时效敏感平台吃满' if not profile['needs_depth'] else '可写深度'}")
    elif fresh_hours <= 72:
        fresh_fit = 0.6
        basis.append(f"{fresh_hours:.0f} 小时前，时效已开始衰减")
    else:
        fresh_fit = 0.25 if not profile['needs_depth'] else 0.5
        basis.append(f"{fresh_hours:.0f} 小时前，"
                     + ('短视频/速报优势已消失' if not profile['needs_depth'] else '但仍可做深度解读'))

    # 形式契合占 15%，时效降到 10%：平台之间的差异主要由"能不能给出合适的
    # 形式"决定，而不是"谁更快"——后者会让所有题材都被推去发快讯。
    fit = (direction_fit * 0.35 + subtopic_fit * 0.20 + depth_fit * 0.20
           + form_fit * 0.15 + fresh_fit * 0.10)
    if not has_original:
        fit -= 0.08
        basis.append('缺原始出处，扣减 8%')
    if summary_length < 60:
        fit -= 0.05
        basis.append('摘要过短，判断依据有限，扣减 5%')
    return round(_clamp(fit), 4), basis


def recommend(candidate, score=None, observations=None):
    """给出三平台匹配度与推荐。纯函数，不写库。

    `candidate` 至少给 title / summary / url / published_at；`score` 是
    `radar_scoring.evaluate` 的结果（可选，用于取方向与二级主题）。
    """
    from datetime import datetime, timezone

    observations = [item for item in (observations or []) if isinstance(item, dict)]
    tiers = [item.get('tier') for item in observations if item.get('tier')]
    summary = str(candidate.get('summary') or '')
    direction = (score or {}).get('direction') or DIRECTION_AI
    subtopics = list((score or {}).get('subtopics') or [])
    has_original = _is_original(candidate.get('url'))
    tier_strength = _tier_strength(tiers)

    published = candidate.get('published_at')
    fresh_hours = None
    if isinstance(published, str) and published:
        try:
            parsed = datetime.fromisoformat(published.replace('Z', '+00:00'))
            if parsed.tzinfo:
                fresh_hours = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)
        except (ValueError, TypeError):
            fresh_hours = None

    kinds = _infer_kinds(str(candidate.get("title") or ""), summary)

    platforms = []
    for platform in PLATFORM_IDS:
        form_id, form_reason, form_fit = _form_choice(platform, kinds, subtopics, tier_strength)
        from .formats import get_content_form
        form = get_content_form(form_id)
        fit, basis = _platform_fit(platform, direction, subtopics, tier_strength,
                                   fresh_hours, has_original, len(summary.strip()),
                                   form_fit, form.get('label', form_id))
        platforms.append({
            'id': platform,
            'name': PLATFORM_PROFILES[platform]['name'],
            'fit': fit,
            'basis': basis,
            'form': form_id,
            'form_label': form.get('label', form_id),
            'form_reason': form_reason,
            'title_direction': PLATFORM_PROFILES[platform]['title_patterns'],
            'title_limit': TITLE_LIMITS[platform],
            'difficulty': _difficulty(platform, form_id, tier_strength, len(summary.strip())),
            'materials': _materials(platform, tier_strength, subtopics, has_original),
        })
    platforms.sort(key=lambda item: item['fit'], reverse=True)
    primary = platforms[0]
    gap = primary['fit'] - (platforms[1]['fit'] if len(platforms) > 1 else 0.0)
    return {
        'primary': primary['id'],
        'primary_name': primary['name'],
        'confidence': 'high' if gap >= 0.15 else 'medium' if gap >= 0.05 else 'low',
        'reason': _reason(primary, platforms, gap),
        'platforms': platforms,
        # 推荐永远不等于批准：没有人工确认，这条推荐不会变成任何创作动作。
        'confirmed_required': True,
    }


def _reason(primary, platforms, gap):
    if gap < 0.05:
        others = '、'.join(item['name'] for item in platforms[1:])
        return (f"{primary['name']}与{others}匹配度接近（差 {gap:.2f}），"
                f"优先{primary['name']}只是因为它更适合「{primary['form_label']}」；"
                f"若想一稿多发，按各平台形式分别改写")
    return (f"{primary['name']}匹配度最高（{primary['fit']:.0%}），"
            f"建议写成「{primary['form_label']}」——{primary['form_reason']}")


def _is_original(url):
    from .radar_scoring import is_original_report
    return is_original_report(url)
