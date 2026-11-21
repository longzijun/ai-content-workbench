"""统一数据处理层：去重、事件合并、热度标准化、异常过滤、采集日志、保留策略。

这一层是 TrendRadar 输出与官方 RSS/Atom/JSON 汇合之后的公共部分，两端共用同一套
规则，避免"热榜一套去重、订阅源另一套"导致同一事件在库里躺着两份。

边界（不改也不越）：
  - 不做语义理解，不宣称"已确认为同一事实"——合并只说明"高度相似"；
  - 不跨源归一化原始热度值。各家口径不同（热搜指数 / HotValue / 投票分），
    硬凑成一个分数就是编数据。归一化只在**同一批次同一榜单内**按名次做；
  - 不删除用户的选题与草稿，保留策略只作用于采集明细。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: 标题相似度阈值。刻意取保守值：实测"发布/推出""提升/增强"这类同义改写，
#: 两条标题的 Jaccard 只有约 0.54 —— 低于阈值，不会被自动合并。
#:
#: 这是有意的选择：自动合并只能证明"高度相似"，不能证明"同一事实"。合并错了
#: 会丢掉一条独立线索（不可逆），不合并只是多显示一条（可人工处理）。因此
#: 阈值只放行标点/格式差异造成的重复，同义改写留给人工判断。
MERGE_THRESHOLD = 0.72
#: 合并窗口（天）。超过这个间隔的相似标题不合并：那更可能是同一话题的新进展，
#: 合到一起会把"旧事件"和"新事件"混成一条，反而看不出演化。
MERGE_WINDOW_DAYS = 3
#: 参与相似度比较的最短标题。太短的标题（如"地震"） Token 少，Jaccard 会失真。
MIN_TITLE_LENGTH = 8

_TRACKING_KEYS = frozenset({"sa", "spm", "gclid", "fbclid", "dclid", "msclkid", "ref",
                            "ref_src", "ref_url", "ocid", "cmpid", "campaign", "source",
                            "token", "access_token", "auth", "password", "utm_id", "share_token"})
_TRACKING_PREFIXES = ("utm_", "rsv_", "at_", "_ga", "wt_")
#: 归一化时统一去掉的主机名前缀。m. / mobile. 是同一站点，不该算两个来源。
_HOST_PREFIXES = ("www.", "m.", "mobile.", "news.")


def canonical_url(value) -> str | None:
    """URL 规范化：小写协议与主机、去跟踪参数、去 fragment、去末尾斜线。

    与 trend_sources._canonical_article 的区别：那个是**带白名单的证据校验**，
    这个是纯形态归一，用于跨源比较，不做任何可信性判断。
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    if parsed.port not in (None, 80 if parsed.scheme == "http" else 443):
        return None
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(_TRACKING_PREFIXES)]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme, host, path, urlencode(query), ""))


def title_key(title) -> str:
    """去标点、数字与大小写后的标题指纹，用于精确去重。"""
    if not isinstance(title, str):
        return ""
    return re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE)


def _tokens(title):
    """中英混排分词：中文按 2-gram，英文数字按词。

    中文没有空格，按字切会让"地震"/"地域"这种完全不同的词产生大量偶然重合，
    2-gram 更接近实际语义单元，也避开引入分词库。
    """
    han = re.findall(r"[\u4e00-\u9fff]+", title)
    latin = re.findall(r"[a-z0-9]+", title.casefold())
    grams = set()
    for chunk in han:
        if len(chunk) == 1:
            grams.add(chunk)
        for index in range(len(chunk) - 1):
            grams.add(chunk[index:index + 2])
    return grams | set(latin)


def similarity(left, right) -> float:
    """标题相似度（Jaccard）。任一方过短则返回 0，避免短标题误并。"""
    if not isinstance(left, str) or not isinstance(right, str):
        return 0.0
    if len(left.strip()) < MIN_TITLE_LENGTH or len(right.strip()) < MIN_TITLE_LENGTH:
        return 0.0
    first, second = _tokens(left), _tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def normalize_heat(rank, max_rank=None) -> float | None:
    """同一批次同一榜单内，把名次归一到 0..1（第 1 名 ≈ 1.0）。

    只在知道本批最大名次时才归一；否则返回 None。跨源的原始热度值
    （热搜指数 / HotValue / 投票分）单位与口径都不同，一律不做归一，
    原值照存、标签照标，让使用者自己看口径。
    """
    if not isinstance(rank, (int, float)) or rank is True or rank <= 0:
        return None
    if not isinstance(max_rank, (int, float)) or max_rank <= 0 or max_rank < rank:
        return None
    if max_rank == 1:
        return 1.0
    return round(1.0 - (rank - 1) / max_rank, 4)


def batch_max_rank(items) -> int | None:
    """本批次里出现过的最大名次，用于把 rank 归一成批内百分位。"""
    ranks = [item.get("rank") for item in items
             if isinstance(item, dict) and isinstance(item.get("rank"), int) and item["rank"] > 0]
    return max(ranks) if ranks else None


def candidate_id_for(url) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def find_merge_target(connection, url, title, fetched, window_days=MERGE_WINDOW_DAYS):
    """在同一时间窗内找可以合并的既有候选。

    三级递进，命中即返回，不做"最像的那一条"式模糊选择：
      1. 规范化 URL 完全相同（最强，跨源同文）
      2. 标题指纹完全相同
      3. 标题 Token Jaccard 达阈值
    第 3 级只在标题都不短时启用，且窗口内只取最近的一条。
    """
    canonical = canonical_url(url)
    if not canonical:
        return None, None
    row = connection.execute(
        "SELECT c.* FROM trend_candidates c JOIN trend_observations o ON o.candidate_id=c.id "
        "WHERE o.url=? ORDER BY c.last_seen_at DESC LIMIT 1", (canonical,)).fetchone()
    if row is not None:
        return row, "same_url"
    row = connection.execute("SELECT * FROM trend_candidates WHERE url=? LIMIT 1", (canonical,)).fetchone()
    if row is not None:
        return row, "same_url"
    key = title_key(title)
    if not key:
        return None, None
    floor = (fetched - timedelta(days=window_days)).isoformat()
    row = connection.execute(
        "SELECT * FROM trend_candidates WHERE title_key=? AND last_seen_at>=? "
        "ORDER BY last_seen_at DESC LIMIT 1", (key, floor)).fetchone()
    if row is not None:
        return row, "same_title"
    if len(title or "") < MIN_TITLE_LENGTH:
        return None, None
    for other in connection.execute(
            "SELECT * FROM trend_candidates WHERE last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 400",
            (floor,)):
        if similarity(title, other["title"]) >= MERGE_THRESHOLD:
            return other, "similar_title"
    return None, None


def record_source_run(connection, run_id, source_id, status, *, attempt=1, http_status=None,
                      bytes_read=None, item_count=0, kept=0, duplicates=0, dropped=0,
                      error="", started_at=None, finished_at=None):
    """写一条采集尝试明细。失败也要写，否则"这个源到底试过没有"没有证据。"""
    now = datetime.now(timezone.utc)
    connection.execute(
        "INSERT INTO radar_source_runs(id,run_id,source_id,status,attempt,http_status,bytes,"
        "item_count,kept_count,duplicate_count,dropped_count,error,started_at,finished_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(f"{run_id}:{source_id}:{now.isoformat()}".encode("utf-8")).hexdigest()[:32],
         run_id, source_id, status, int(attempt), http_status, bytes_read,
         int(item_count), int(kept), int(duplicates), int(dropped), str(error)[:300],
         (started_at or now).isoformat(), (finished_at or now).isoformat()))


def retention(connection, now=None, run_days=31):
    """保留策略：采集明细按天滚动，候选本身不动。

    候选表保存的是"发现过什么"，删了就没法复盘；采集明细只服务于排障与
    频率调优，留 31 天足够看出趋势。
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=run_days)).isoformat()
    connection.execute("DELETE FROM radar_source_runs WHERE finished_at<?", (cutoff,))
    return {"runs_before_days": run_days}


def direction_catalog():
    """四大方向及其说明，供 UI 与分类展示使用。单一事实来源在 trend_sources。"""
    from .trend_sources import DIRECTIONS, DIRECTION_LABELS
    return [{"id": key, "label": DIRECTION_LABELS[key]} for key in DIRECTIONS]
