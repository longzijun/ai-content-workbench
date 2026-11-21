"""Persistent, source-backed trend discovery; candidates never authorize writing.

The monitor uses only fixed public-source adapters. Selection creates a pending
topic in the existing workflow; neither a score nor an imported headline can
approve a topic, generate a draft, or publish anything.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

from . import db, service
from .config import get_settings

_lock = threading.RLock()
_stop = threading.Event()
_monitor: threading.Thread | None = None
_refresh_thread: threading.Thread | None = None
_MANUAL_REFRESH_KEY = 'trend_manual_refresh'


def _now():
    return datetime.now(timezone.utc)


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _catalog():
    """内置来源 + TrendRadar 热榜平台，合成统一目录。

    TrendRadar 平台即使尚未配置输出也照样列出：它们是 P1 已验证过的来源，
    缺的只是运行时数据，列出来才能在来源管理里被看见、启停和分类。
    """
    from .trend_sources import source_catalog
    from . import radar_trendradar
    return source_catalog() + radar_trendradar.catalog()


def _collect(source_id):
    """按来源 id 分派采集。未知 id 一律拒绝，绝不落到网络层。"""
    from .trend_sources import collect_source
    from . import radar_trendradar
    if source_id.startswith(radar_trendradar.PREFIX):
        return radar_trendradar.collect(source_id)
    return collect_source(source_id)


def _settings_path():
    return get_settings().runtime_dir / 'data' / 'trend-settings.json'


def settings():
    defaults = {'enabled': False, 'interval_minutes': 60, 'scope': 'broad', 'keywords': [],
                'enabled_sources': [s['id'] for s in _catalog()], 'window_days': 7}
    baseline = dict(defaults)
    path = _settings_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text('utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('invalid configuration')
            defaults.update({key: raw[key] for key in defaults if key in raw})
            _validate_settings(defaults)
        except (OSError, ValueError, TypeError):
            # A damaged config cannot authorize automatic networking.
            defaults = baseline
    return defaults


def _validate_settings(current):
    allowed = {s['id'] for s in _catalog()}
    ids = current['enabled_sources']
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in allowed for key in ids):
        raise ValueError('只能选择已实现的公开来源，且至少选择一个')
    if current['scope'] not in ('broad', 'ai') or not isinstance(current['enabled'], bool):
        raise ValueError('热点监测范围或开关无效')
    if type(current['interval_minutes']) is not int or not 15 <= current['interval_minutes'] <= 1440:
        raise ValueError('监测间隔需为 15 到 1440 分钟')
    if type(current['window_days']) is not int or not 1 <= current['window_days'] <= 30:
        raise ValueError('资讯时间窗口需为 1 到 30 天')
    words = current['keywords']
    if not isinstance(words, list) or len(words) > 20 or any(not isinstance(w, str) or not w.strip() or len(w.strip()) > 60 for w in words):
        raise ValueError('关键词最多 20 个，每个不超过 60 字')


def update_settings(values):
    with _lock:
        current = settings()
        unknown = set(values) - set(current)
        if unknown:
            raise ValueError('未知热点设置字段')
        current.update(values)
        _validate_settings(current)
        words = current['keywords']
        current['keywords'] = list(dict.fromkeys(w.strip() for w in words))
        current['enabled_sources'] = list(dict.fromkeys(current['enabled_sources']))
        path = _settings_path()
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), 'utf-8')
        temporary.replace(path)
        return current


def _result_counts(raw):
    counts = {'ok': 0, 'error': 0, 'not_due': 0, 'pending': 0, 'attempted': 0}
    try:
        results = json.loads(raw or '[]')
    except (TypeError, ValueError):
        results = []
    for result in results:
        status = result.get('status') if isinstance(result, dict) else 'pending'
        if status not in counts:
            status = 'pending'
        counts[status] += 1
    counts['attempted'] = counts['ok'] + counts['error']
    return counts


def _refresh_info(connection, config):
    request = connection.execute('SELECT value FROM runtime_state WHERE key=?', (_MANUAL_REFRESH_KEY,)).fetchone()
    queued = bool(request and request['value'] == 'pending')
    row = connection.execute('SELECT * FROM trend_runs ORDER BY started_at DESC LIMIT 1').fetchone()
    if row is None:
        return {'state': 'idle', 'run_id': None, 'last_started_at': None, 'last_finished_at': None,
                'next_refresh_at': _now().isoformat() if config['enabled'] else None, 'error': None,
                'source_counts': _result_counts(None), 'queued': queued}
    stamp = _date(row['finished_at']) or _date(row['started_at'])
    following = stamp + timedelta(minutes=config['interval_minutes']) if stamp and config['enabled'] else None
    if row['state'] == 'failed' and row['finished_at'] is None and config['enabled']:
        following = _now()  # One recovery scan; normal failures retain their interval.
    return {'state': row['state'], 'run_id': row['id'], 'last_started_at': row['started_at'],
            'last_finished_at': row['finished_at'], 'next_refresh_at': following.isoformat() if following else None,
            'error': row['error'], 'source_counts': _result_counts(row['source_results']), 'queued': queued}


def _title_key(title):
    return re.sub(r'[\W_]+', '', title.casefold(), flags=re.UNICODE)


#: 连续失败到几次算「降级」。降级后采集间隔按 DEGRADED_MULTIPLIER 放大：
#: 既不再每轮去撞一个已经挂掉的源，也保留自动恢复的机会。
DEGRADED_AFTER = 3
DEGRADED_MULTIPLIER = 3


def _health_map():
    """读回来源健康记录。纯读。"""
    with db.connect() as connection:
        return {row['source_id']: dict(row)
                for row in connection.execute('SELECT * FROM trend_source_health')}


def _due_sources(source_ids, now=None):
    """按**分层间隔**挑出本轮真正该采的源。

    返回 `(due, skipped)`。skipped 不是失败：它只说明「还没到下次采集时间」。
    必须在结果里如实标注 —— 否则界面会把「本轮没采」显示成「这个源没数据」，
    而这正是原建议里「整批 succeeded 不代表所有来源成功」那条的镜像问题。

    间隔取 `last_attempt_at` 而不是 `last_success_at`：一个一直失败的源如果按
    成功时间算间隔，就会每轮都被重试，把超时时间浪费在同一个地方。
    """
    moment = now or _now()
    catalog = {source['id']: source for source in _catalog()}
    health = _health_map()
    due, skipped = [], []
    for source_id in source_ids:
        source = catalog.get(source_id)
        if source is None:
            continue
        every = int(source.get('min_interval_minutes') or 0)
        record = health.get(source_id, {})
        failures = int(record.get('consecutive_failures') or 0)
        if failures >= DEGRADED_AFTER:
            every = max(every, 60) * DEGRADED_MULTIPLIER
        last = _date(record.get('last_attempt_at'))
        if every and last and (moment - last) < timedelta(minutes=every):
            skipped.append({
                'source_id': source_id, 'status': 'not_due', 'fetched_at': None,
                'item_count': 0, 'error': None,
                'next_at': (last + timedelta(minutes=every)).isoformat(),
            })
            continue
        due.append(source_id)
    return due, skipped


def _record_health(source_id, status, error=''):
    """记一次采集尝试。成功清零，失败累加 —— 这是降级判断的唯一依据。"""
    stamp = _now().isoformat()
    with db.transaction() as connection:
        row = connection.execute(
            'SELECT consecutive_failures FROM trend_source_health WHERE source_id=?',
            (source_id,)).fetchone()
        failures = int(row['consecutive_failures']) if row else 0
        if status == 'ok':
            connection.execute(
                'INSERT INTO trend_source_health(source_id,consecutive_failures,last_success_at,'
                'last_attempt_at,last_error,updated_at) VALUES(?,0,?,?,\'\',?) '
                'ON CONFLICT(source_id) DO UPDATE SET consecutive_failures=0,'
                'last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,'
                'last_error=\'\',updated_at=excluded.updated_at',
                (source_id, stamp, stamp, stamp))
        else:
            connection.execute(
                'INSERT INTO trend_source_health(source_id,consecutive_failures,last_success_at,'
                'last_attempt_at,last_error,updated_at) VALUES(?,?,NULL,?,?,?) '
                'ON CONFLICT(source_id) DO UPDATE SET '
                'consecutive_failures=excluded.consecutive_failures,'
                'last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,'
                'updated_at=excluded.updated_at',
                (source_id, failures + 1, stamp, str(error)[:300], stamp))


#: 出版方归并。目的不是给来源贴好看的标签，而是**防止同一家机构的多个 feed 被
#: 当成互相独立的信源**：cross-source 计数会被虚增，从而抬高"多家报道"的分量。
#: 例：GitHub Blog / GitHub Changelog / 各仓库 Releases 同属 github。
#: 按来源 id 显式指定出版方。用于那些"抓取地址相同但发布方不同"的源 ——
#: TrendRadar 各平台共用 NewsNow 接口，按 URL 归并会把它们错算成同一家。
_PUBLISHER_OVERRIDES = {
    "tr_toutiao": "toutiao",
    "tr_baidu": "baidu",
    "tr_thepaper": "thepaper",
    "tr_zhihu": "zhihu",
    "tr_bilibili-hot-search": "bilibili",
    "tr_douyin": "douyin",
    "tr_ifeng": "ifeng",
    "tr_tieba": "tieba",
    "tr_wallstreetcn-hot": "wallstreetcn",
    "tr_cls-hot": "cls",
}

_PUBLISHER_RULES = (
    ("baidu", ("baidu.com",)),
    ("bbc", ("bbci.co.uk", "bbc.co.uk", "bbc.com")),
    ("google", ("blog.google", "research.google", "deepmind.google")),
    ("arxiv", ("arxiv.org", "export.arxiv.org")),
    ("github", ("github.com", "github.blog")),
    ("openai", ("openai.com",)),
    ("ollama", ("ollama.com",)),
    ("hackernews", ("hn.algolia.com",)),
    ("ithome", ("ithome.com",)),
    ("ifanr", ("ifanr.com",)),
    ("sspai", ("sspai.com",)),
    ("infoq", ("infoq.cn",)),
    ("ruanyifeng", ("ruanyifeng.com",)),
    ("chinanews", ("chinanews.com.cn",)),
    ("toutiao", ("toutiao.com",)),
)


def _publisher(source):
    """出版方归并。

    TrendRadar 的 10 个平台都经同一个公益接口抓取，若按 URL 归并会被算成
    一家出版方，cross-source 计数被压到 1。它们实际是不同的平台，因此按
    平台显式指定出版方（见 _PUBLISHER_OVERRIDES）。
    """
    from . import radar_trendradar
    override = _PUBLISHER_OVERRIDES.get(source['id'])
    if override:
        return override
    host = (urlparse(source['url']).hostname or '').lower()
    for publisher, suffixes in _PUBLISHER_RULES:
        if any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes):
            return publisher
    return host


def _decode_directions(raw):
    """候选表的 directions 是 JSON 字符串；解析失败按"未分类"处理而不是抛错。"""
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if item]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(item) for item in parsed if item] if isinstance(parsed, list) else []


def _direction_balanced_rows(rows, limit=400):
    """先对窗口内**全部**候选做廉价方向分类，再按账号占比均衡取样。

    旧的 `LIMIT 500` 按最近发现时间截断，会把排名靠后的 AI 候选整体丢掉
    （实测 AI 首条在第 732 位）。这里只用规则法 `classify`（不评分、不调模型），
    把全窗口候选按方向分桶、桶内按新近度排序，再按账号目标占比分配配额；
    某方向候选不足时把空额优先让给 AI，再顺延给其它方向，保证 AI 方向不被
    时间排序整体挤出。
    """
    from . import radar_scoring
    from .trend_sources import DIRECTIONS
    order = list(DIRECTIONS)  # ai / tech / society / life，顺序同时是优先级
    buckets = {direction: [] for direction in order}
    unclassified = []
    for row in rows:
        primary = radar_scoring.classify(
            row['title'], row['summary'], _decode_directions(row['directions'])
        )['direction']
        bucket = buckets.get(primary)
        if bucket is None:
            unclassified.append(row)
        else:
            bucket.append(row)
    for bucket in buckets.values():
        bucket.sort(key=lambda r: r['last_seen_at'] or '', reverse=True)
    unclassified.sort(key=lambda r: r['last_seen_at'] or '', reverse=True)

    weights = radar_scoring.DIRECTION_WEIGHTS
    quotas = {direction: round(limit * weights[direction]) for direction in order}
    quotas[order[0]] += limit - sum(quotas.values())  # 四舍五入误差补到 AI

    picked = []
    for direction in order:
        picked.extend(buckets[direction][:quotas[direction]])

    # 剩余名额：AI 优先补齐（候选远多于配额时，靠后的 AI 也有机会进），
    # 再按科技/社会/生活顺序，最后才用未归类候选兜底。
    if len(picked) < limit:
        chosen_ids = {row['id'] for row in picked}
        overflow = []
        for direction in order:
            overflow.extend(row for row in buckets[direction] if row['id'] not in chosen_ids)
        overflow.extend(unclassified)
        picked.extend(overflow[:limit - len(picked)])
    return picked


def _score_candidate(item, observations, config):
    """六维评分。评分只回答"值不值得做"，不批准任何创作动作。

    观察记录里只存了 ``kind``，没有来源分层（tier）。分层在来源定义上，
    所以这里按 source_id 补齐 —— 否则"官方发布"和"热榜线索"会被算成一样
    可信，评分就失去意义。
    """
    from . import radar_scoring
    tiers, source_directions = {}, {}
    for source in _catalog():
        tiers[source['id']] = source.get('tier') or 'signal'
        source_directions[source['id']] = list(source.get('directions') or [])
    for observation in observations:
        observation['tier'] = tiers.get(observation['source_id'], 'signal')
    directions = _decode_directions(item.get('directions'))
    if not directions:
        # 候选入库时还没打方向（老数据），用各来源声明方向的并集补。
        for observation in observations:
            for direction in source_directions.get(observation['source_id'], []):
                if direction not in directions:
                    directions.append(direction)
    payload = {
        'title': item.get('title'),
        'summary': item.get('summary'),
        'url': item.get('url'),
        'published_at': item.get('published_at'),
        'discovered_at': item.get('first_seen_at'),
        'last_seen_at': item.get('last_seen_at'),
        'directions': directions,
    }
    return radar_scoring.evaluate(payload, observations, now=_now(),
                                  keywords=config.get('keywords') or [])


def _candidate_view(connection, row, config):
    from .trend_ranking import rank_event
    from . import radar_scoring
    item = dict(row)
    observations = [dict(o) for o in connection.execute(
        'SELECT * FROM trend_observations WHERE candidate_id=? ORDER BY fetched_at DESC LIMIT 180', (row['id'],))]
    item['observations'] = observations
    ranked = rank_event(item, _now(), scope=config['scope'], keywords=config['keywords'], window_days=config['window_days'])
    item.update(ranked)
    latest = {}
    provenance = {source['id']:source.get('platform','external') for source in _catalog()}
    for observation in observations:
        if observation['source_id'] not in latest:
            latest[observation['source_id']] = {key: observation[key] for key in ('source_id', 'source_name', 'url', 'rank', 'heat_value', 'heat_label', 'fetched_at')}
            latest[observation['source_id']]['platform'] = provenance.get(observation['source_id'],'external')
    item['sources'] = list(latest.values())
    item['score'] = _score_candidate(item, observations, config)
    item['direction'] = item['score']['direction']
    # 来源标签（来源声明方向）与文章标签（标题/摘要关键词多标签）分开：
    # 科技媒体的 AI 报道，来源标签是 tech，文章标签应含 ai，二者不互相顶替。
    source_directions = {source['id']: list(source.get('directions') or []) for source in _catalog()}
    source_tags = []
    for observation in observations:
        for direction in source_directions.get(observation['source_id'], []):
            if direction not in source_tags:
                source_tags.append(direction)
    item['source_tags'] = source_tags
    item['article_tags'] = radar_scoring.article_tags(item['title'], item.get('summary') or '')
    item['platform'] = _platform_recommendation(item, item['score'], observations)
    item.pop('observations', None)
    item.pop('title_key', None)
    return item


def _platform_recommendation(item, score, observations):
    """三平台匹配与推荐。只推荐，不创作：本函数不建选题、不生成草稿。"""
    from . import radar_platforms
    return radar_platforms.recommend(item, score, observations)


#: 采集失败的稳定分类。界面据此区分「超时 / 连接 / 解析 / 空内容 / 过期缓存」，
#: 而不是把所有失败都叫「不可用」。
_ERROR_CATEGORIES = (
    ("timeout", ("超时",)),
    ("http", ("HTTP", "不可访问", "响应损坏", "访问", "403", "429", "拒绝")),
    ("parse", ("格式", "解析", "编码", "JSON", "XML", "DOCTYPE", "变化")),
    ("empty", ("空内容", "未返回", "没有可")),
    ("stale", ("缓存", "过期", "日期")),
    ("connection", ("连接", "网络", "TLS", "DNS", "域名")),
)


def _classify_error(error) -> str | None:
    """把来源错误文案归到稳定类别。无文案（成功）返回 None。"""
    text = str(error or "")
    if not text:
        return None
    for category, keywords in _ERROR_CATEGORIES:
        if any(keyword in text for keyword in keywords):
            return category
    return "other"


def _funnel(connection):
    """最新一轮采集的漏斗与逐源记账。

    采到 → 保留 / 重复 / 丢弃 分别计数：抓取成功但全是重复或超窗，显示
    「成功、无新增」，不与真实故障混淆；逐源明细供来源面板解释「新增 0」。
    """
    run = connection.execute('SELECT id FROM trend_runs ORDER BY started_at DESC LIMIT 1').fetchone()
    if run is None:
        return None
    rows = connection.execute('SELECT * FROM radar_source_runs WHERE run_id=?', (run['id'],)).fetchall()
    funnel = {'run_id': run['id'], 'collected': 0, 'kept': 0, 'duplicates': 0, 'dropped': 0,
              'sources_attempted': 0, 'sources_ok': 0, 'sources_error': 0, 'per_source': {}}
    for row in rows:
        funnel['collected'] += int(row['item_count'] or 0)
        funnel['kept'] += int(row['kept_count'] or 0)
        funnel['duplicates'] += int(row['duplicate_count'] or 0)
        funnel['dropped'] += int(row['dropped_count'] or 0)
        funnel['sources_attempted'] += 1
        if row['status'] == 'ok':
            funnel['sources_ok'] += 1
        elif row['status'] == 'error':
            funnel['sources_error'] += 1
        funnel['per_source'][row['source_id']] = {
            'status': row['status'], 'item_count': row['item_count'], 'kept': row['kept_count'],
            'duplicates': row['duplicate_count'], 'dropped': row['dropped_count'],
            'error': row['error'], 'error_category': _classify_error(row['error']),
            'finished_at': row['finished_at'],
        }
    return funnel


def state(filters=None, cursor=None, limit=50):
    filters = filters or {}
    config = settings()
    with db.connect() as connection:
        refresh = _refresh_info(connection, config)
        run = connection.execute('SELECT source_results FROM trend_runs ORDER BY started_at DESC LIMIT 1').fetchone()
        latest_results = {r['source_id']: r for r in json.loads(run['source_results'])} if run else {}
        funnel = _funnel(connection)
        run_details = funnel['per_source'] if funnel else {}
        sources = []
        health = _health_map()
        for source in _catalog():
            result = latest_results.get(source['id'], {})
            record = health.get(source['id'], {})
            failures = int(record.get('consecutive_failures') or 0)
            source_run = run_details.get(source['id'], {})
            # 调度状态与来源健康分开：`status` 只描述本轮「采没采、成没成」，
            # 连续失败/上次错误放在 `health` 里，界面据此分别展示，避免把
            # 「未到抓取间隔」误报成来源不可用，也不把持续失败藏进「未到间隔」。
            status = result.get('status', 'pending')
            sources.append({
                **source,
                'status': status,
                # not_due 时本轮没有 fetched_at，用上次成功时间补，避免显示成「从没采过」。
                'last_fetched_at': result.get('fetched_at') or record.get('last_success_at'),
                'item_count': result.get('item_count', 0),
                'error': result.get('error'),
                'next_at': result.get('next_at'),
                'health': {
                    'consecutive_failures': failures,
                    'last_success_at': record.get('last_success_at'),
                    'last_error': record.get('last_error'),
                    'error_category': _classify_error(record.get('last_error')),
                    'degraded': failures >= DEGRADED_AFTER,
                },
                # 最近一次实际采集的漏斗记账：保留/重复/丢弃分开，解释「新增 0」。
                'last_run': source_run or {
                    'status': None, 'item_count': 0, 'kept': 0, 'duplicates': 0, 'dropped': 0,
                    'error': None, 'error_category': None, 'finished_at': None,
                },
            })
        cutoff = (_now() - timedelta(days=max(config['window_days'], 2))).isoformat()
        rows = connection.execute(
            'SELECT * FROM trend_candidates WHERE last_seen_at>=? OR status="saved"', (cutoff,)).fetchall()
        # 后端先筛选再排序分页：对窗口内候选做规则法评分，再按条件过滤、
        # 排序、游标分页。不再先取 400/200 交给前端后筛。
        candidates = []
        for row in rows:
            item = _candidate_view(connection, row, config)
            if item.get('eligible', True):
                candidates.append(item)
        direction = filters.get('direction')
        if direction:
            candidates = [c for c in candidates if c.get('direction') == direction]
        content_line = filters.get('content_line')
        if content_line:
            candidates = [c for c in candidates if any(cl.get('line') == content_line for cl in c.get('score', {}).get('content_lines', []))]
        classification = filters.get('classification')
        if classification:
            candidates = [c for c in candidates if c.get('classification') == classification]
        publishable = filters.get('publishable')
        if publishable is not None:
            candidates = [c for c in candidates if bool(c.get('publishable')) == publishable]
        candidates.sort(key=lambda c: (c['score']['total'], c['last_seen_at']), reverse=True)
        total = len(candidates)
        start = 0
        if cursor:
            for index, candidate in enumerate(candidates):
                if candidate.get('id') == cursor:
                    start = index + 1
                    break
        page = candidates[start:start + limit]
        next_cursor = page[-1].get('id') if len(page) == limit and start + limit < total else None
        from .trend_sources import platform_source_capabilities, TIER_LABELS, TIER_NOTES
        return {'settings': config, 'refresh': refresh, 'sources': sources,
                'candidates': page,
                'candidate_total': total,
                'candidate_shown': len(page),
                'next_cursor': next_cursor,
                'funnel': funnel,
                'platform_sources': platform_source_capabilities(),
                'tier_labels': TIER_LABELS, 'tier_notes': TIER_NOTES}


def _record_source(run_id, source, result):
    """把一次采集落到候选表与观察表，并留下可核对的计数。

    关键是把「没采到东西」拆开：源没产出 / 时间窗外被丢弃 / 与既有候选合并，
    三种情况在界面上都表现为"新增 0"，但处置完全不同。因此每次采集都写
    radar_source_runs，把抓取数、保留数、重复数、丢弃数分别记账。
    """
    from . import radar_pipeline
    config = settings()
    items = result.get('items', [])
    # 批内最大名次：把 rank 归一成百分位时才用得上，跨源热度值一律不归一。
    max_rank = radar_pipeline.batch_max_rank(items)
    count = duplicates = dropped = 0
    with db.transaction() as connection:
        for article in items:
            title = str(article.get('title') or '').strip()[:300]
            raw_url = str(article.get('url') or '')
            url = radar_pipeline.canonical_url(raw_url) or raw_url
            if not title or not url:
                dropped += 1
                continue
            published = _date(article.get('published_at'))
            # Old/future news is not a fresh discovery. Rankings normally omit
            # event publication times; retain that explicit unknown instead.
            fetched = _date(result.get('fetched_at')) or _now()
            if published and (published > fetched + timedelta(minutes=10) or published < fetched - timedelta(days=config['window_days'])):
                dropped += 1
                continue
            key = _title_key(title)
            existing, reason = radar_pipeline.find_merge_target(connection, url, title, fetched)
            if existing is not None:
                duplicates += 1
            candidate_id = existing['id'] if existing else radar_pipeline.candidate_id_for(url)
            directions = service.encode(sorted(set(list(source.get('directions') or [])
                                                   + list(article.get('directions') or []))))
            if existing is None:
                connection.execute('INSERT INTO trend_candidates(id,title,title_key,url,summary,published_at,first_seen_at,last_seen_at,directions) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (candidate_id, title, key, url, str(article.get('summary') or '')[:400], published.isoformat() if published else None, fetched.isoformat(), fetched.isoformat(), directions))
                count += 1
            else:
                connection.execute('UPDATE trend_candidates SET last_seen_at=?,summary=CASE WHEN ?!="" THEN ? ELSE summary END,published_at=COALESCE(published_at,?),'
                                   'directions=CASE WHEN directions="[]" THEN ? ELSE directions END WHERE id=?',
                                   (fetched.isoformat(), str(article.get('summary') or '')[:400], str(article.get('summary') or '')[:400], published.isoformat() if published else None, directions, candidate_id))
            connection.execute('INSERT OR REPLACE INTO trend_observations(candidate_id,source_id,run_id,source_name,source_url,url,publisher_id,kind,fetched_at,rank,heat_value,heat_label,heat_normalized,merge_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (candidate_id, source['id'], run_id, source['name'], source['url'], url, _publisher(source), source['kind'], fetched.isoformat(),
                                article.get('rank'), article.get('heat_value'), article.get('heat_label'),
                                radar_pipeline.normalize_heat(article.get('rank'), max_rank), reason))
        # Keep bounded history for actual rate comparisons, not fabricated trends.
        connection.execute('DELETE FROM trend_observations WHERE fetched_at<?', ((_now() - timedelta(days=31)).isoformat(),))
        radar_pipeline.retention(connection)
        radar_pipeline.record_source_run(
            connection, run_id, source['id'], result.get('status') or 'error',
            item_count=len(items), kept=count, duplicates=duplicates, dropped=dropped,
            error=result.get('error') or '')
    return count


def _log_failed_run(run_id, source_id, error):
    """写一条失败采集日志。不抛异常：记账失败不该拖垮整轮采集。"""
    from . import radar_pipeline
    try:
        with db.transaction() as connection:
            radar_pipeline.record_source_run(connection, run_id, source_id, 'error', error=error)
    except Exception:
        pass


def _refresh(run_id, source_ids):
    due, skipped = _due_sources(source_ids)
    results = list(skipped)
    new_count = 0
    sources = {s['id']: s for s in _catalog()}
    try:
        if not due:
            # 本轮一个源都没到期：这是正常结果，不是失败。标 failed 会让
            # 「按分层间隔采集」看起来像故障。
            with db.transaction() as connection:
                connection.execute(
                    'UPDATE trend_runs SET state=?,finished_at=?,source_results=?,new_count=0,error=? WHERE id=?',
                    ('succeeded', _now().isoformat(), service.encode(results), None, run_id))
            return

        # TrendRadar 仍是独立进程，但由工作台统一触发。一次运行刷新整个输出库，
        # 随后各平台只读自己的数据；运行失败时不得回读旧榜单冒充本轮结果。
        from . import radar_trendradar
        trendradar_due = [source_id for source_id in due if source_id.startswith(radar_trendradar.PREFIX)]
        prefailed = {}
        if trendradar_due:
            attempt = radar_trendradar.refresh_output()
            if attempt.get('status') != 'ok':
                for source_id in trendradar_due:
                    prefailed[source_id] = {
                        'source_id': source_id, 'status': 'error', 'fetched_at': _now().isoformat(),
                        'items': [], 'error': attempt.get('error') or 'TrendRadar 本轮采集失败'}

        def handle_result(source_id, result):
            nonlocal new_count
            try:
                if result.get('status') == 'ok':
                    new_count += _record_source(run_id, sources[source_id], result)
                    _record_health(source_id, 'ok')
                else:
                    _record_health(source_id, 'error', result.get('error') or '')
                    _log_failed_run(run_id, source_id, result.get('error') or '')
                results.append({key: result.get(key) for key in ('source_id', 'status', 'fetched_at', 'error')}
                               | {'item_count': len(result.get('items', []))})
            except Exception:
                _record_health(source_id, 'error', '来源读取异常')
                _log_failed_run(run_id, source_id, '来源读取异常')
                results.append({'source_id': source_id, 'status': 'error', 'fetched_at': _now().isoformat(),
                                'item_count': 0, 'error': '此公开来源未完成读取，已有候选保留，未用模拟数据替代'})
            with db.transaction() as connection:
                connection.execute('UPDATE trend_runs SET source_results=? WHERE id=?', (service.encode(results), run_id))

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='trend-source') as pool:
            for source_id, result in prefailed.items():
                handle_result(source_id, result)
            futures = {pool.submit(_collect, source_id): source_id for source_id in due if source_id not in prefailed}
            for future in as_completed(futures):
                source_id = futures[future]
                try:
                    result = future.result()
                except Exception:
                    result = {'source_id': source_id, 'status': 'error', 'fetched_at': _now().isoformat(),
                              'items': [], 'error': '来源读取异常'}
                handle_result(source_id, result)

        attempted = [result for result in results if result['status'] in ('ok', 'error')]
        okay = sum(result['status'] == 'ok' for result in attempted)
        errors = sum(result['status'] == 'error' for result in attempted)
        run_state = 'partial' if okay and errors else 'succeeded' if okay else 'failed'
        error = (f'部分公开来源抓取失败（成功 {okay} / 失败 {errors}），候选覆盖不完整，请查看来源状态'
                 if run_state == 'partial' else
                 None if run_state == 'succeeded' else '全部公开来源暂不可用，请查看来源状态后重试')
        with db.transaction() as connection:
            connection.execute('UPDATE trend_runs SET state=?,finished_at=?,source_results=?,new_count=?,error=? WHERE id=?',
                               (run_state, _now().isoformat(), service.encode(results), new_count, error, run_id))
            if new_count and okay:
                service._notice(connection, None, '热点候选已更新', f'发现 {new_count} 条近期候选线索，请打开热点雷达查看依据并选择。选题尚未批准，不会自动创作或发布。')
    except Exception:
        with db.transaction() as connection:
            connection.execute('UPDATE trend_runs SET state="failed",finished_at=?,error=? WHERE id=?', (_now().isoformat(), '热点收集发生中断，已有候选保留；可再次检查公开来源', run_id))
    finally:
        with db.transaction() as connection:
            connection.execute('DELETE FROM runtime_state WHERE key=? AND value=?', (_MANUAL_REFRESH_KEY, run_id))


def request_refresh():
    """API only persists intent; only the singleton Worker launches collection."""
    config = settings()
    with db.transaction() as connection:
        running = connection.execute('SELECT 1 FROM trend_runs WHERE state="running" LIMIT 1').fetchone()
        if not running:
            connection.execute('INSERT INTO runtime_state(key,value,updated_at) VALUES(?,"pending",?) '
                               'ON CONFLICT(key) DO NOTHING', (_MANUAL_REFRESH_KEY, _now().isoformat()))
        return _refresh_info(connection, config)


def refresh():
    with _lock:
        config = settings()
        with db.transaction() as connection:
            running = connection.execute('SELECT * FROM trend_runs WHERE state="running" LIMIT 1').fetchone()
            if running:
                return _refresh_info(connection, config)
            run_id = service.new_id()
            connection.execute('INSERT INTO trend_runs(id,state,started_at) VALUES(?,"running",?)', (run_id, _now().isoformat()))
            result = _refresh_info(connection, config)
        _launch_refresh(run_id, config['enabled_sources'])
        return result


def _launch_refresh(run_id, source_ids):
    global _refresh_thread
    _refresh_thread = threading.Thread(target=_refresh, args=(run_id, source_ids), name='workbench-trend-refresh', daemon=True)
    _refresh_thread.start()


def select(candidate_id, platforms):
    service.require(bool(platforms) and len(platforms) == len(set(platforms)) and set(platforms).issubset(service.PLATFORMS), '至少选择一个有效平台', 422)
    with db.transaction() as connection:
        row = connection.execute('SELECT * FROM trend_candidates WHERE id=?', (candidate_id,)).fetchone()
        service.require(row is not None, '热点候选不存在', 404)
        if row['topic_id']:
            return {'topic': service._topic(connection, service._row(connection, 'topics', row['topic_id'])), 'already_selected': True}
        item = _candidate_view(connection, row, settings())
        service.require(item.get('eligible', True), '该候选不符合当前筛选范围，请先调整热点设置')
        title = item['suggested_title'][:300]
        uncertainties = '；'.join(item['uncertainties'])
        brief = (item['brief'] + '\n\n来源线索：' + item['title'] + '\n推荐角度：' + item['angle'] + '\n仍需核实：' + uncertainties + '\n热榜只说明关注度，来源摘记不是已经核实的事实。不编造亲历，不将未经证实的指控写成事实。')[:12000]
        source_entries = []
        publication_note = item['published_at'] or '来源未提供；抓取时间不等于发布时间'
        ordered_sources = sorted(item['sources'], key=lambda s: ('xiaohongshu' if s.get('platform')=='xhs' else s.get('platform')) in platforms, reverse=True)
        for source in ordered_sources[:10]:
            source_entries.append({'url': source['url'], 'title': item['title'],
                'platform':source.get('platform','external'),
                'note': ('来源：' + source['source_name'] + '；候选记录的发布时间：' + publication_note + '；原始摘记：' + item['summary'] + '；证据状态：仅用于选题线索，发表前核实原始报道及最新进展。')[:6000]})
        brief += '\n\n发布平台自身热点优先；其他平台线索须单独核实。内部保留出处，正文署名、链接、账号、二维码与素材许可按目标平台要求审核，不自动插入导流链接。'
        topic_id, stamp = service.new_id(), service.now()
        connection.execute('INSERT INTO topics(id,title,brief,platforms,sources,status,created_at,updated_at) VALUES(?,?,?,?,?,"pending",?,?)', (topic_id, title, brief, service.encode(platforms), service.encode(source_entries), stamp, stamp))
        connection.execute('UPDATE trend_candidates SET status="saved",topic_id=? WHERE id=?', (topic_id, candidate_id))
        service._activity(connection, topic_id, '从热点候选加入待确认选题；没有批准或启动创作')
        return {'topic': service._topic(connection, service._row(connection, 'topics', topic_id)), 'already_selected': False}


def ignore(candidate_id):
    with db.transaction() as connection:
        row = connection.execute('SELECT * FROM trend_candidates WHERE id=?', (candidate_id,)).fetchone()
        service.require(row is not None, '热点候选不存在', 404)
        service.require(row['topic_id'] is None, '已加入选题的候选请在选题库处理')
        connection.execute('UPDATE trend_candidates SET status="ignored" WHERE id=?', (candidate_id,))
        return _candidate_view(connection, connection.execute('SELECT * FROM trend_candidates WHERE id=?', (candidate_id,)).fetchone(), settings())


def _loop():
    while not _stop.is_set():
        try:
            monitor_tick()
            from .execution_runtime import component_event
            component_event('trend_monitor', success=True)
        except Exception as exc:
            from .execution_runtime import component_event
            component_event('trend_monitor', error=exc)
        _stop.wait(2)


def monitor_tick():
    config = settings()
    manual_run = None
    with db.transaction() as connection:
        request = connection.execute('SELECT value FROM runtime_state WHERE key=?', (_MANUAL_REFRESH_KEY,)).fetchone()
        running = connection.execute('SELECT 1 FROM trend_runs WHERE state="running" LIMIT 1').fetchone()
        if request and request['value'] == 'pending' and not running:
            manual_run = service.new_id()
            connection.execute('INSERT INTO trend_runs(id,state,started_at) VALUES(?,"running",?)', (manual_run, _now().isoformat()))
            connection.execute('UPDATE runtime_state SET value=?,updated_at=? WHERE key=?', (manual_run, _now().isoformat(), _MANUAL_REFRESH_KEY))
    if manual_run:
        _launch_refresh(manual_run, config['enabled_sources'])
        return True
    if not config['enabled']:
        return False
    with db.connect() as connection:
        info = _refresh_info(connection, config)
    due = _date(info['next_refresh_at'])
    if info['state'] != 'running' and (due is None or due <= _now()):
        refresh()
        return True
    return False


def start_monitor():
    global _monitor
    with _lock:
        if _monitor and _monitor.is_alive():
            return
        # A persisted running marker after exit never means a successful scan.
        with db.transaction() as connection:
            connection.execute('UPDATE trend_runs SET state="failed",finished_at=NULL,error=? WHERE state="running"', ('上次监测被中断；历史候选保留，恢复后补查一次',))
            connection.execute('UPDATE runtime_state SET value="pending",updated_at=? WHERE key=? AND value!="pending"', (_now().isoformat(), _MANUAL_REFRESH_KEY))
        _stop.clear()
        _monitor = threading.Thread(target=_loop, name='workbench-trend-monitor', daemon=True)
        _monitor.start()


def stop_monitor():
    global _monitor
    _stop.set()
    if _monitor:
        _monitor.join(timeout=3)
    if _refresh_thread and _refresh_thread.is_alive():
        _refresh_thread.join(timeout=25)
    _monitor = None
