"""每日生产定时器。进程内的第二个 tick 循环，不依赖任何 OS 级计划任务。

为什么不加 APScheduler：
本项目把数据库当作恢复的唯一权威 —— worker、delivery、trends 都在启动时
把遗留的 running 打回。APScheduler 自带 jobstore 会引入第二套真相：本机任务
到底跑没跑过，要查两个地方。这正是 trends 已有 tick 循环的原因，本模块照抄
它的形状，认知成本为零、依赖为零。

不引入 APScheduler 的附带好处：新增依赖就有可能在 import 期读 HTTP_PROXY，
而本机启动脚本要专门清理 Clash 遗留的代理变量才能起来。多一个依赖就多一个
这种坑。

边界 —— 本模块不做的事：
  不生成内容（只调 production.tick 建任务）；不投递草稿；
  不在服务未运行时补跑（过期与错过的排期由 production 结清为 missed）。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from . import production

#: tick 间隔。与 trends 的 2 秒一致 —— 批次本身很轻，不需要更密。
TICK_SECONDS = 2
#: 每天的生产时刻（本地时间）。凌晨 6 点：热点已过夜、用户早上能看到结果。
DEFAULT_HOUR = 6

_loop_thread = None
_stop = threading.Event()
_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(moment: datetime) -> datetime:
    """把传入时刻转成**北京时间**（不是「本机时区」）。

    生产时刻按北京时间判定：用户说「早上六点」指的是北京时间六点。用
    `astimezone()` 转本机时区在用户换了系统时区后会漂，所以固定用
    `production.CN_TZ`（Asia/Shanghai，本机无 tzdata 时回落 +08:00）。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(production.CN_TZ)


def _due_today(now: datetime = None) -> bool:
    return _local(now or _now()).hour >= DEFAULT_HOUR


def tick(now: datetime = None) -> bool:
    """跑一次批次判断。返回 True 表示进入了批次流程。

    单独抽成纯函数是为了可测：时点判断不该依赖真实时钟。
    到点之后同一分钟内多次调用是安全的 —— production.tick 的幂等键会挡住
    重复生产，不依赖这里做去重。
    """
    moment = now or _now()
    if not _due_today(moment):
        return False
    production.tick(moment)
    return True


def _loop():
    # 启动后先 recover：遗留的 running 批次不代表上次生产成功。
    try:
        production.recover()
    except Exception as exc:
        from .execution_runtime import component_event
        component_event('scheduler', error=exc)
    while not _stop.is_set():
        try:
            tick()
            from .execution_runtime import component_event
            component_event('scheduler', success=True)
        except Exception as exc:
            from .execution_runtime import component_event
            component_event('scheduler', error=exc)
        _stop.wait(TICK_SECONDS)


def start():
    """启动定时器。重复调用不会起第二个线程。"""
    global _loop_thread
    with _lock:
        if _loop_thread and _loop_thread.is_alive():
            return
        _stop.clear()
        _loop_thread = threading.Thread(target=_loop, name='workbench-production', daemon=True)
        _loop_thread.start()


def stop():
    """停止定时器。逆序停，绝不 kill 进程。"""
    global _loop_thread
    with _lock:
        _stop.set()
        if _loop_thread and _loop_thread.is_alive():
            _loop_thread.join(timeout=3)
        _loop_thread = None


def status() -> dict:
    return {
        'running': bool(_loop_thread and _loop_thread.is_alive()),
        'tick_seconds': TICK_SECONDS,
        'production_hour_local': DEFAULT_HOUR,
        'timezone': production.CN_TZ_NAME,
        'note': '服务不运行就没有定时；本定时器只在独立后台执行进程内生效，'
                '不注册任何系统级计划任务。',
    }
