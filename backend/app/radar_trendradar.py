"""TrendRadar 独立进程触发与输出适配器。

TrendRadar（v6.11.0，GNU GPL-3.0）是**独立进程**：它自己采集、自己写 SQLite。
工作台只触发单次进程并读取其输出，不复制源码、不改写逻辑、不启用第二套常驻调度。
GPL 的传染范围因此止于 TrendRadar 自身进程；二者通过数据文件通信，不是衍生作品。

采集或读取失败一律如实上报，绝不用上一次的榜单或模拟数据顶替。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from urllib.parse import urlsplit

from .config import get_settings
from .trend_sources import (DIRECTION_AI, DIRECTION_LIFE, DIRECTION_SOCIETY, DIRECTION_TECH,
                            MAX_ITEMS, TIER_NOTES)

#: 所有 TrendRadar 来源共用的前缀，避免与内置来源 id 撞车。
PREFIX = "tr_"

#: 平台 → (中文名, 内容方向, 文章域名白名单, 发布平台)
#: 方向按 P2 清单（WB-05）的分类映射填写；域名白名单用于二次校验上游 URL，
#: 即使 TrendRadar 自己已做过域名安全检查（P1 已测），这里也不放行异常数据。
#: 域名白名单取自 2026-10-23 对 TrendRadar 当日输出库的实测（每平台逐条 URL
#: 取 hostname），不是凭印象填的：B 站热榜实际落在 search.bilibili.com，
#: 凤凰网会同时出现 news. / original. 两个域。填错会让整平台条目被静默丢弃。
PLATFORMS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], str]] = {
    "toutiao": ("今日头条热榜", (DIRECTION_SOCIETY, DIRECTION_TECH), ("www.toutiao.com",), "toutiao"),
    "baidu": ("百度热搜", (DIRECTION_SOCIETY,), ("www.baidu.com", "top.baidu.com", "baike.baidu.com"), "external"),
    "thepaper": ("澎湃新闻热榜", (DIRECTION_SOCIETY,), ("www.thepaper.cn",), "external"),
    "zhihu": ("知乎热榜", (DIRECTION_SOCIETY, DIRECTION_LIFE), ("www.zhihu.com",), "external"),
    "bilibili-hot-search": ("哔哩哔哩热搜", (DIRECTION_LIFE, DIRECTION_TECH), ("search.bilibili.com", "www.bilibili.com", "m.bilibili.com"), "external"),
    "douyin": ("抖音热榜", (DIRECTION_LIFE, DIRECTION_SOCIETY), ("www.douyin.com", "douyin.com"), "external"),
    "ifeng": ("凤凰网热点", (DIRECTION_SOCIETY,), ("news.ifeng.com", "original.ifeng.com", "www.ifeng.com", "finance.ifeng.com"), "external"),
    "tieba": ("百度贴吧热榜", (DIRECTION_LIFE,), ("tieba.baidu.com",), "external"),
    "wallstreetcn-hot": ("华尔街见闻热榜", (DIRECTION_TECH, DIRECTION_SOCIETY), ("wallstreetcn.com", "www.wallstreetcn.com"), "external"),
    "cls-hot": ("财联社热榜", (DIRECTION_TECH, DIRECTION_SOCIETY), ("www.cls.cn", "cls.cn"), "external"),
}

#: TrendRadar 实际抓取的公开接口（NewsNow 公益端点）。写成真实地址而不是
#: 「由 TrendRadar 提供」，是为了让每条线索都能追溯到可访问的出处。
SOURCE_URL_TEMPLATE = "https://newsnow.busiyi.world/api/s?id={platform}&latest"

#: 超过这个时长没有新输出，就如实报"未更新"，而不是把旧榜单当实时热点。
MAX_OUTPUT_AGE_HOURS = 24


def _output_dir() -> Path | None:
    """TrendRadar 输出目录。环境变量优先，其次 runtime 下的配置文件。"""
    raw = os.environ.get("WORKBENCH_TRENDRADAR_OUTPUT", "").strip()
    if not raw:
        marker = get_settings().runtime_dir / "data" / "trendradar-output.txt"
        try:
            raw = marker.read_text("utf-8").strip()
        except OSError:
            raw = ""
    if not raw:
        return None
    try:
        return Path(raw)
    except (TypeError, ValueError):
        return None


def configure(path: str) -> dict:
    """写入输出目录配置。只做路径记录，不校验远端、不启动任何进程。"""
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("TrendRadar 输出目录不能为空")
    directory = Path(raw)
    marker = get_settings().runtime_dir / "data" / "trendradar-output.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(directory), "utf-8")
    return state()


def _python_path() -> Path | None:
    """TrendRadar 专用解释器；不能用工作台环境代替，二者依赖 deliberately 隔离。"""
    raw = os.environ.get("WORKBENCH_TRENDRADAR_PYTHON", "").strip()
    if not raw:
        marker = get_settings().runtime_dir / "data" / "trendradar-python.txt"
        try:
            raw = marker.read_text("utf-8").strip()
        except OSError:
            raw = ""
    return Path(raw) if raw else None


def _project_root(directory: Path) -> Path | None:
    """约定输出为 <TrendRadar>/output/news，根目录不接受任意命令配置。"""
    if directory.name != "news" or directory.parent.name != "output":
        return None
    root = directory.parent.parent
    return root if (root / "trendradar" / "__main__.py").is_file() else None


def refresh_output(timeout_seconds: int = 150) -> dict:
    """由工作台触发一次独立 TrendRadar 进程，并验证确实产生了新输出。

    不启动第二套常驻调度，不开启通知。失败时明确报错，调用方不得改读旧榜单。
    """
    directory = _output_dir()
    if directory is None or not directory.is_dir():
        return {"status": "error", "error": "TrendRadar 输出目录未配置或不存在"}
    root = _project_root(directory)
    python = _python_path()
    if root is None:
        return {"status": "error", "error": "TrendRadar 输出目录不符合 <项目>/output/news 约定"}
    if python is None or not python.is_file():
        return {"status": "error", "error": "未配置有效的 TrendRadar 专用 Python"}
    before = sorted(directory.glob("*.db"))
    before_signature = (before[-1].name, before[-1].stat().st_mtime_ns) if before else None
    started = datetime.now(timezone.utc)
    environment = os.environ.copy()
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    # 与隔离验证一致：直连公益接口，不继承本机残留代理。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    command = [str(python), "-m", "trendradar"]
    try:
        completed = subprocess.run(
            command, cwd=str(root), env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        stdout, stderr, returncode = completed.stdout or "", completed.stderr or "", completed.returncode
    except (OSError, subprocess.SubprocessError) as error:
        stdout = getattr(error, "stdout", "") or ""
        stderr = getattr(error, "stderr", "") or str(error)
        returncode = None
    log_dir = get_settings().runtime_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ("trendradar-" + started.strftime("%Y%m%d-%H%M%S") + ".log")
    log_path.write_text(
        f"started_at={started.isoformat()}\nreturncode={returncode}\n\nSTDOUT\n{stdout}\n\nSTDERR\n{stderr}",
        "utf-8")
    if returncode != 0:
        detail = (stderr.strip() or stdout.strip()).splitlines()
        message = detail[-1][:240] if detail else "进程未正常完成"
        return {"status": "error", "error": f"TrendRadar 采集失败：{message}", "log": str(log_path)}
    after = sorted(directory.glob("*.db"))
    signature = (after[-1].name, after[-1].stat().st_mtime_ns) if after else None
    if not signature or signature == before_signature:
        return {"status": "error", "error": "TrendRadar 进程结束但没有更新输出数据库", "log": str(log_path)}
    latest, reason = _latest_db()
    if latest is None:
        return {"status": "error", "error": reason, "log": str(log_path)}
    return {"status": "ok", "database": str(latest), "log": str(log_path),
            "finished_at": datetime.now(timezone.utc).isoformat()}


def state() -> dict:
    directory = _output_dir()
    if directory is None:
        return {"configured": False, "output_dir": None, "latest_db": None, "latest_db_at": None,
                "platforms": [], "note": "未配置 TrendRadar 输出目录，热榜平台不参与采集。"}
    if not directory.is_dir():
        return {"configured": True, "output_dir": str(directory), "latest_db": None, "latest_db_at": None,
                "platforms": [], "note": f"输出目录不存在：{directory}", "error": "输出目录不存在"}
    files = sorted(directory.glob("*.db"))
    if not files:
        return {"configured": True, "output_dir": str(directory), "latest_db": None, "latest_db_at": None,
                "platforms": [], "note": "输出目录里还没有数据库，需先由 TrendRadar 采集一次。",
                "error": "尚无 TrendRadar 输出"}
    latest = files[-1]
    stamp = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
    age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
    platforms = []
    try:
        connection = sqlite3.connect(f"file:{latest}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT DISTINCT platform_id FROM news_items").fetchall()
        finally:
            connection.close()
        known = {row[0] for row in rows}
        platforms = [{"id": PREFIX + key, "platform_id": key, "name": PLATFORMS[key][0], "present": key in known}
                     for key in PLATFORMS if key in known] or [
            {"id": PREFIX + key, "platform_id": key, "name": PLATFORMS[key][0], "present": False} for key in PLATFORMS]
    except sqlite3.Error as error:
        return {"configured": True, "output_dir": str(directory), "latest_db": latest.name,
                "latest_db_at": stamp.isoformat(), "platforms": [],
                "error": f"TrendRadar 输出库无法读取：{error}"}
    note = None
    if age_hours > MAX_OUTPUT_AGE_HOURS:
        note = f"最新输出 {latest.name} 已 {age_hours:.0f} 小时未更新，不作为实时热点。"
    return {"configured": True, "output_dir": str(directory), "latest_db": latest.name,
            "latest_db_at": stamp.isoformat(), "age_hours": round(age_hours, 1),
            "platforms": platforms, "note": note}


def catalog() -> list[dict]:
    """把 10 个热榜平台呈现成与内置来源同构的目录条目。

    即使尚未配置 TrendRadar 也照样列出：它们是已验证过的来源（P1 两轮 10/10），
    缺的只是运行时输出。列出来才能在来源管理里看见、启停、分类。
    """
    entries = []
    for platform_id, (name, directions, _hosts, platform) in PLATFORMS.items():
        entries.append({
            "id": PREFIX + platform_id,
            "name": name,
            "url": SOURCE_URL_TEMPLATE.format(platform=platform_id),
            "kind": "trendradar",
            "category": "society" if DIRECTION_SOCIETY in directions else "technology",
            "platform": platform,
            "tier": "signal",
            "min_interval_minutes": 0,
            "directions": list(directions),
            "max_items": MAX_ITEMS,
        })
    return entries


def _latest_db():
    directory = _output_dir()
    if directory is None or not directory.is_dir():
        return None, "未配置 TrendRadar 输出目录"
    files = sorted(directory.glob("*.db"))
    if not files:
        return None, "TrendRadar 尚无输出数据库"
    latest = files[-1]
    stamp = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
    if datetime.now(timezone.utc) - stamp > timedelta(hours=MAX_OUTPUT_AGE_HOURS):
        return None, f"TrendRadar 最新输出已超过 {MAX_OUTPUT_AGE_HOURS} 小时未更新"
    return latest, None


def _stamp(value, fallback):
    """TrendRadar 写的是本机本地时间字符串，没有时区标记，统一按本地时区解读。"""
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        parsed = datetime.fromisoformat(value.strip().replace("/", "-"))
    except (TypeError, ValueError):
        return fallback
    local = parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
    return local.astimezone(timezone.utc)


def collect(platform_id: str) -> dict:
    """读取一个热榜平台的最新榜单。只读本地 SQLite，不发起任何网络请求。"""
    key = platform_id[len(PREFIX):] if platform_id.startswith(PREFIX) else platform_id
    name, directions, hosts, platform = PLATFORMS.get(key, (platform_id, (), (), "external"))
    now = datetime.now(timezone.utc)
    result = {"source_id": platform_id, "source_name": name,
              "source_url": SOURCE_URL_TEMPLATE.format(platform=key),
              "kind": "trendradar", "platform": platform,
              "status": "error", "fetched_at": now.isoformat(), "items": [], "error": None}
    database, reason = _latest_db()
    if database is None:
        result["error"] = reason
        return result
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT title, url, mobile_url, rank, crawl_count, first_crawl_time, last_crawl_time "
                "FROM news_items WHERE platform_id=? ORDER BY rank ASC, id ASC", (key,)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        result["error"] = f"TrendRadar 输出库读取失败：{error}"
        return result
    items = []
    for row in rows:
        title = (row["title"] or "").strip()[:300]
        url = (row["url"] or "").strip()
        if not title or not url:
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            continue
        if hosts and (parsed.hostname or "").lower() not in hosts:
            continue  # 上游出现异常域名，宁可丢弃也不放进证据链
        discovered = _stamp(row["first_crawl_time"], now)
        items.append({
            "title": title,
            "url": url,
            "summary": f"{PLATFORMS.get(key, (key,))[0]}线索，未提供事件原文摘要；正文与发布时间需补原始出处。",
            # 采集记录只留内部追踪字段，不混充原文摘要或正文事实。
            "internal_tracking": {
                "crawl_count": int(row["crawl_count"] or 1),
                "last_crawl_time": row["last_crawl_time"] or None,
            },
            # 热榜不提供事件发布时间：保持未知，不用抓取时间冒充发布时间。
            "published_at": None,
            "rank": row["rank"],
            "heat_value": None,
            "heat_label": "榜单名次",
            "category": "society" if DIRECTION_SOCIETY in directions else "technology",
            "directions": list(directions),
            "is_hotspot_index": True,
            "rank_verified": True,
            "signal_uncertainties": ["热榜名次只说明平台内的关注度，不等于全网热度或事件真实性。",
                                     "榜单未提供事件发布时间，抓取时间不等于发布时间。"],
        })
    if not items:
        result["error"] = "TrendRadar 该平台本次没有可核验的条目"
        return result
    result["items"] = items[:MAX_ITEMS]
    result["status"] = "ok"
    return result
