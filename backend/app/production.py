"""每日批量生产：把已批准的选题按人工排期送进既有的生成队列。

最关键的一条：**本模块不生成内容**。它只调用 service.generate() 建立任务就
立刻返回，实际生成永远发生在 worker 线程里。这样做的理由是本工作台把
数据库当作恢复的唯一权威（worker / delivery / trends 都在启动时把 running
打回）；如果调度器自己生成，就会出现第二套执行语义与第二套恢复来源。
因此调度器的任何 bug 都不可能影响生成串行性、取消语义或恢复语义。

红线 —— 本模块不做的事：
  不触碰 drafts / delivery_tasks（只用 service.generate() 这一条公开路径）；
  代码层不 import delivery，投递永远是人类点「推入平台草稿箱」之后的事；
  不创建平台草稿，不发布任何内容。

关于「只到本地草稿」这条表述的更正（2026-10-22）：`service.generate()` 建出的
job 由 worker 执行，worker 完成时调用 `complete_job(..., defer_delivery=True)`
把草稿**加入持久交付队列**，再由 delivery worker 保存到各平台草稿箱。
所以定时生产的真实结果是「平台草稿箱里出现一份待审核草稿」，不是「只在本机」。
发布仍然必须由人在平台上点击 —— 这一条没有变。本模块自身依然不 import
delivery、不写 delivery_tasks；自动入队发生在 worker 的既有链路里。

审批池的表达是**两重**的：topics.status='approved'（既有红线）与存在一行
production_slots（新增，显式人工排期）。只用 approved 会把「批准」悄悄扩张
成「批准即排期」——任何批准过但还没排期的选题都会被每天捞出来生产。

过期不补发：服务停三天再启动，不应该一次产出三天的量。

时区：**所有「哪一天」的判断都按 Asia/Shanghai**，不按 UTC。北京时间
10-22 06:30 在 UTC 下是 10-21 22:30，直接用 `datetime.date()` 会把批次
记到前一天，run 的 UNIQUE(batch_date) 也会错位。本机没有 tzdata 包，
ZoneInfo('Asia/Shanghai') 会抛 ZoneInfoNotFoundError，因此回落到固定
+08:00 —— 中国自 1991 年起不实行夏令时，两者等价。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from . import db, service


def _china_tz():
    """优先真正的 IANA 时区；本机没装 tzdata 时回落到固定 +08:00。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


#: 批次与生产日期统一用这个时区判定。中国无夏令时，固定偏移与 IANA 等价。
CN_TZ = _china_tz()
CN_TZ_NAME = "Asia/Shanghai"

#: 单日最多生产几个选题。**按当日累计计数**，不是单次领取上限 ——
#: 定时器每 2 秒 tick 一次，只限制单次领取量等于没有上限。
MAX_TOPICS_PER_DAY = 3
#: 批次内各选题之间的错峰分钟数。把本机模型的忙碌窗口摊开，
#: 避免本机模型操作长时间占用。
#: 这个值现在**真的参与入队时刻计算**：第 n 个选题要等到
#: 批次开始 + n*STAGGER_MINUTES 才建任务。
STAGGER_MINUTES = 20
#: 判定「过期」的宽限：到期后 0 分钟内不再排产。
GRACE = timedelta(0)
#: 计入当日配额的任务状态。failed 不占额度（没生产成功），
#: planned 占额度（已登记、准备建任务，防止并发重复领）。
QUOTA_STATES = ("planned", "job_created", "produced")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime = None) -> str:
    return (moment or _now()).isoformat()


def _local(moment: datetime = None) -> datetime:
    """把任意时刻转成北京时间。naive 时刻按 UTC 解释（DB 里存的就是 UTC）。"""
    value = moment or _now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ)


def _date_str(moment: datetime = None) -> str:
    """批次日期 = 北京时间的日历日。**不是 UTC 日期。**"""
    return _local(moment).date().isoformat()


def idempotency_key(topic_id: str, revision: int, batch_date: str) -> str:
    """跨重启稳定的幂等键。不含时间戳，否则重启后会当成新任务重复生产。"""
    payload = json.dumps({'topic_id': topic_id, 'revision': int(revision),
                          'batch_date': batch_date}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


# --------------------------------------------------------------------------
# 人工排期
# --------------------------------------------------------------------------

def schedule_slot(topic_id: str, batch_date: str, *, expires_at: str = '',
                  note: str = '', expected_revision: int = None) -> dict:
    """给一个已批准的选题排期。必须在选题已批准的前提下调用。

    这是显式的人工动作：定时器只消费 slot，不会自己造 slot。
    """
    if not isinstance(batch_date, str) or not batch_date.strip():
        raise ValueError('请指定生产日期')
    day = batch_date.strip()
    # batch_date 参与字符串比较（`< today` 判错过），格式不合法会让整条
    # 排期逻辑失真，所以在入口就挡住。
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError('生产日期格式必须是 YYYY-MM-DD')
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError('生产日期不是合法日期')
    with db.transaction() as connection:
        topic = connection.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if topic is None:
            raise ValueError('选题不存在')
        revision = int(expected_revision if expected_revision is not None else topic["revision"])
        service.require(topic["status"] == "approved",
                        "请先人工批准选题范围，再排期生产", 409)
        existing = connection.execute(
            "SELECT id,state FROM production_slots WHERE topic_id=? AND topic_revision=? AND batch_date=?",
            (topic_id, revision, day)).fetchone()
        stamp = _stamp()
        if existing:
            connection.execute(
                "UPDATE production_slots SET expires_at=?,note=?,updated_at=? WHERE id=?",
                (str(expires_at or '') or None, str(note)[:200], stamp, existing["id"]))
            return {'id': existing['id'], 'state': existing['state'], 'created': False}
        slot_id = service.new_id()
        connection.execute(
            "INSERT INTO production_slots(id,topic_id,topic_revision,batch_date,expires_at,state,"
            "note,created_at,updated_at) VALUES(?,?,?,?,?, 'scheduled', ?,?,?)",
            (slot_id, topic_id, revision, day, str(expires_at or '') or None,
             str(note)[:200], stamp, stamp))
        return {'id': slot_id, 'state': 'scheduled', 'created': True}


def cancel_slot(slot_id: str) -> dict:
    """取消尚未开始生产的排期。已在生成中的任务另由 cancel_job 处理。"""
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT state FROM production_slots WHERE id=?", (slot_id,)).fetchone()
        if row is None:
            raise ValueError('排期不存在')
        service.require(row["state"] in ('scheduled', 'queued'), '该排期已开始生产，无法取消', 409)
        connection.execute("UPDATE production_slots SET state='cancelled',updated_at=? WHERE id=?",
                           (_stamp(), slot_id))
        connection.execute(
            "UPDATE production_items SET state='cancelled',updated_at=? WHERE slot_id=? AND state IN ('planned','job_created')",
            (_stamp(), slot_id))
        return {'id': slot_id, 'state': 'cancelled'}


def resume_slot(slot_id: str) -> dict:
    """恢复 V3 迁移暂停的排期。恢复后仍只生成本地内容，不补做停服期间遗漏的批次。"""
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT state,paused FROM production_slots WHERE id=?", (slot_id,)).fetchone()
        if row is None:
            raise ValueError('排期不存在')
        service.require(row["state"] in ('scheduled', 'queued'), '该排期已结案，不能恢复', 409)
        service.require(bool(row["paused"]), '该排期未被暂停', 409)
        connection.execute("UPDATE production_slots SET paused=0,updated_at=? WHERE id=?",
                           (_stamp(), slot_id))
        connection.execute(
            "UPDATE production_items SET message=?,updated_at=? WHERE slot_id=? AND state='planned'",
            ('排期已恢复；本地内容模式只生成本地作品', _stamp(), slot_id))
        return {'id': slot_id, 'state': row["state"], 'paused': False}


def list_slots(state: str = None) -> list:
    """读出排期，按生产日期与选题排序。纯读。paused 供前端区分「迁移暂停」与「主动取消」。"""
    query = ("SELECT id,topic_id,topic_revision,batch_date,expires_at,state,note,created_at,updated_at,paused"
             " FROM production_slots")
    params = []
    if state:
        query += " WHERE state=?"
        params.append(state)
    query += " ORDER BY batch_date, topic_id"
    with db.connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 批次
# --------------------------------------------------------------------------

def tick(now: datetime = None) -> dict:
    """跑一次批次。纯函数式入口，便于单测直接调用。

    tick 只做两件事：过期的排期结清、本日到期的排期送进生成队列。
    它不生成内容，也不投递草稿。

    空轮询（本轮没有新任务）**不得改写已成功批次的结果** —— 定时器每 2 秒
    跑一次，如果每次空转都把 run 打成 failed，批次状态就永远反映不了真实结果。
    """
    moment = now or _now()
    today = _date_str(moment)
    _close_stale_slots(moment, today)
    run = _ensure_run(today, moment)
    picked, waiting = _claim_slots(run, today, moment)
    created = []
    for position, slot in picked:
        created.append(_act(slot, run, moment, position))
    _finish_run(run, created, moment, waiting)
    return {'batch_date': today, 'run_id': run['id'], 'items': created,
            'created': sum(1 for item in created if item['state'] == 'job_created'),
            'waiting': waiting}


def _close_stale_slots(moment, today):
    """过期与错过两条规则。

    规则一：过了 expires_at 的排期直接结清为 expired，不建任务。
    规则二：早于今天的排期结清为 missed —— 服务停了三天再启动，不补发。
    """
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT id,expires_at,batch_date FROM production_slots "
            "WHERE state IN ('scheduled','queued') AND paused=0").fetchall()
        for row in rows:
            if row["batch_date"] < today:
                _close_slot(connection, row["id"], 'missed',
                            '生产日期已过，未补发；如仍要生产请重新排期')
                continue
            if not row["expires_at"]:
                continue
            expiry = _parse(row["expires_at"])
            if expiry is not None and moment >= expiry + GRACE:
                _close_slot(connection, row["id"], 'expired',
                            '选题已过期，按「过期内容不自动补发」跳过')
                connection.execute(
                    "UPDATE production_items SET state='expired',message=?,updated_at=? "
                    "WHERE slot_id=? AND state IN ('planned','job_created')",
                    ('选题已过期，未创建生成任务', _stamp(moment), row["id"]))


def _ensure_run(today, moment=None):
    """一天至多一批。重启后再跑同一天会取回同一条 run，不会开第二条。

    `started_at` 必须原样返回：错峰时刻以它为锚点，重启后漂移就会重复错峰。
    新建时用传入的 moment 而不是真实时钟 —— 否则测试里注入的时刻与锚点不一致。

    **已结案的批次不得每次 tick 都翻回 running。** 旧实现无条件
    `SET state='running'`，于是 /api/production 里能看到
    `state=running` 但同时 `finished_at` 有值 —— 定时器每 2 秒 tick 一次，
    这个脏状态几乎常驻，用户看到的是「进行中」而批次其实早就完成了。
    """
    with db.transaction() as connection:
        row = connection.execute("SELECT * FROM production_runs WHERE batch_date=?", (today,)).fetchone()
        if row:
            if row["state"] in ('stopped', 'failed'):
                # 被中断或失败的批次允许本日重跑；清掉旧的完成时间。
                connection.execute(
                    "UPDATE production_runs SET state='running',finished_at=NULL,error=NULL WHERE id=?",
                    (row["id"],))
                current = dict(row)
                current.update({'state': 'running', 'finished_at': None})
                return current
            return dict(row)
        run_id = service.new_id()
        started = _stamp(moment)
        connection.execute(
            "INSERT INTO production_runs(id,batch_date,state,started_at,summary) VALUES(?,?,'running',?,'{}')",
            (run_id, today, started))
        return {'id': run_id, 'batch_date': today, 'state': 'running', 'started_at': started}


def _used_quota(today: str) -> int:
    """当日已经占用的生产额度。按 run.batch_date 汇总，跨重启有效。"""
    marks = ",".join("?" for _ in QUOTA_STATES)
    with db.connect() as connection:
        row = connection.execute(
            f"SELECT COUNT(*) AS n FROM production_items i "
            f"JOIN production_runs r ON r.id = i.run_id "
            f"WHERE r.batch_date = ? AND i.state IN ({marks})",
            (today, *QUOTA_STATES)).fetchone()
    return int(row["n"])


def _claim_slots(run, today, moment):
    """取出本批要处理的排期，按**当日累计**配额截断，并落实错峰。

    返回 `(picked, waiting)`：
      picked  —— [(position, slot)]，position 是该选题在当日序列里的序号，
                 用来算错峰时刻；
      waiting —— 已具备条件但还没到错峰时刻的排期数量，>0 时批次不能结束。

    历史缺陷：旧实现只按 `len(picked) >= MAX_TOPICS_PER_DAY` 截断单次领取，
    tick 每 2 秒一次，于是「每天最多 3 个」会被三秒内连续三轮领完。
    """
    used = _used_quota(today)
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT s.*, t.status AS topic_status, t.revision AS current_revision "
            "FROM production_slots s JOIN topics t ON t.id = s.topic_id "
            "WHERE s.state='scheduled' AND s.paused=0 AND s.batch_date=? ORDER BY s.created_at, s.id",
            (today,)).fetchall()

    eligible = []
    for row in rows:
        item = dict(row)
        if item["topic_status"] != "approved" or item["current_revision"] != item["topic_revision"]:
            with db.transaction() as connection:
                _close_slot(connection, item["id"], 'skipped',
                            '选题状态或范围已变化，未排入本批')
            continue
        eligible.append(item)

    picked = []
    waiting = 0
    for index, item in enumerate(eligible):
        # position 是**当日累计序号**，不是本轮下标 —— 用下标会让错峰在
        # 第二轮归零（前一个已 queued 不在 eligible 里），20 分钟间隔失效。
        position = used + index
        if position >= MAX_TOPICS_PER_DAY:
            # 配额已满：当天不再生产。留到明天会按「不补发」变 missed，
            # 对用户没有信息量，所以直接结清并写清下一步。
            with db.transaction() as connection:
                _close_slot(connection, item["id"], 'skipped',
                            f'已达当日生产上限 {MAX_TOPICS_PER_DAY} 个，未排入本批；'
                            '如仍要生产请重新排期到次日')
            continue
        if moment < _stagger_moment(run, position):
            waiting += 1
            continue
        picked.append((position, item))
    return picked, waiting


def _stagger_moment(run, position: int) -> datetime:
    """本批次第 position 个选题最早可以建任务的时刻。

    锚点是 `production_runs.started_at`（不随 tick 漂移，跨重启稳定）。
    """
    started = _parse(run.get("started_at")) if isinstance(run, dict) else None
    if started is None:
        started = _now()
    if position <= 0:
        return started
    return started + timedelta(minutes=STAGGER_MINUTES * position)


def _act(slot, run, moment, position: int = 0):
    """两阶段写入。这是全模块唯一一处跨事务写入，必须这样切。

    阶段一（短事务）：判条件 + INSERT production_items(state='planned') 并提交。
    阶段二（事务外）：调 service.generate()。它自己开事务；若手持写事务，
    BEGIN IMMEDIATE 会阻塞 20 秒然后抛 database is locked。
    阶段三（短事务）：回填 job_id。

    阶段一先提交是关键：即使阶段二抛错，这条 item 也已经以 planned 状态
    留在库里，重启后会被识别为「已登记未建任务」，不会静默丢失。
    """
    key = idempotency_key(slot["topic_id"], slot["topic_revision"], run["batch_date"])
    stamp = _stamp(moment)
    item_id = service.new_id()

    with db.transaction() as connection:
        try:
            connection.execute(
                "INSERT INTO production_items(id,run_id,slot_id,topic_id,topic_revision,state,"
                "idempotency_key,message,created_at,updated_at) VALUES(?,?,?,?,?, 'planned', ?,?,?,?)",
                (item_id, run["id"], slot["id"], slot["topic_id"], slot["topic_revision"],
                 key, '已登记，等待创建生成任务', stamp, stamp))
        except Exception:
            # 幂等键或 (run,slot) 撞上：这一批已经处理过这个选题。
            row = connection.execute(
                "SELECT id,state,job_id,message FROM production_items WHERE idempotency_key=?",
                (key,)).fetchone()
            if row:
                # 旧实现到这里直接 return，slot 永远停在 scheduled：每次 tick 都
                # 捞一次、每次都什么都不做，既不重试也不结案。这里补上结清。
                if row["state"] in ('failed', 'skipped', 'expired'):
                    _close_slot(connection, slot["id"], 'skipped',
                                f'本批已处理过该选题（{row["state"]}），不再自动重试；'
                                '请查看失败原因后重新排期')
                return dict(row)
            raise
        connection.execute("UPDATE production_slots SET state='queued',updated_at=? WHERE id=?",
                           (stamp, slot["id"]))

    try:
        job = service.generate(slot["topic_id"], slot["topic_revision"])
    except Exception as error:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE production_items SET state='failed',message=?,updated_at=? WHERE id=?",
                (str(error)[:300], _stamp(moment), item_id))
            # 失败不再退回 scheduled 反复空转：结清并写明下一步，用户处理后重排。
            _close_slot(connection, slot["id"], 'skipped',
                        f'生成任务创建失败：{str(error)[:200]}；'
                        '已停止自动重试，请处理后重新排期')
        return {'id': item_id, 'state': 'failed', 'job_id': None, 'position': position,
                'message': str(error)[:300]}

    with db.transaction() as connection:
        connection.execute(
            "UPDATE production_items SET state='job_created',job_id=?,message=?,updated_at=? WHERE id=?",
            (job["id"], '生成任务已创建，等待工作台 worker 执行', _stamp(moment), item_id))
    return {'id': item_id, 'state': 'job_created', 'job_id': job['id'],
            'position': position, 'message': ''}


def _run_totals(run_id: str) -> dict:
    """本批次累计结果。**不是本轮 tick 的结果** —— 空轮询不该把成功改写成失败。"""
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT state, COUNT(*) AS n FROM production_items WHERE run_id=? GROUP BY state",
            (run_id,)).fetchall()
    totals = {row["state"]: int(row["n"]) for row in rows}
    totals['items'] = sum(totals.values())
    return totals


def _finish_run(run, items, moment, waiting: int = 0):
    """收尾。三条规则：

    1. 还有排期在等错峰时刻 → 保持 running，不写 finished_at；
    2. 本批累计建出过任务 → succeeded（哪怕这一轮是空转）；
    3. 一条任务都没建出来 → failed，但**空批次（无 item）算 succeeded**：
       当天没有排期不是失败。
    """
    totals = _run_totals(run['id'])
    if waiting > 0:
        summary = json.dumps({
            'items': totals['items'],
            'job_created': totals.get('job_created', 0),
            'failed': totals.get('failed', 0),
            'waiting_stagger': waiting,
            'degraded_note': '服务未运行期间没有定时；过期与错过的排期不补发',
        }, ensure_ascii=False)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE production_runs SET state='running',finished_at=NULL,summary=? WHERE id=?",
                (summary, run['id']))
        return

    if totals.get('job_created', 0) > 0 or totals['items'] == 0:
        state = 'succeeded'
    else:
        state = 'failed'
    summary = json.dumps({
        'items': totals['items'],
        'job_created': totals.get('job_created', 0),
        'failed': totals.get('failed', 0),
        'skipped': totals.get('skipped', 0),
        'degraded_note': '服务未运行期间没有定时；过期与错过的排期不补发',
    }, ensure_ascii=False)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE production_runs SET state=?,finished_at=?,summary=? WHERE id=?",
            (state, _stamp(moment), summary, run['id']))


def _close_slot(connection, slot_id, state, message):
    """结清一个排期，并把原因写进 slot 自身与它的未完成 item。

    原因必须落在 slot 上而不只是 item 上 —— 排期列表是用户日常会看的地方，
    只在 item 里留原因的话，用户看到一个「expired」却不知道该不该重新排期。
    """
    stamp = _stamp()
    connection.execute(
        "UPDATE production_slots SET state=?,note=?,updated_at=? WHERE id=?",
        (state, message, stamp, slot_id))
    if message:
        connection.execute(
            "UPDATE production_items SET message=?,updated_at=? "
            "WHERE slot_id=? AND state IN ('planned','job_created')",
            (message, stamp, slot_id))


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recover(now: datetime = None) -> dict:
    """启动时把中断的批次标记为 stopped，并结清卡住的 queued 排期。

    一个遗留的 running 批次不表示上次生产成功 —— 与 trends 的处理一致。
    """
    moment = now or _now()
    with db.transaction() as connection:
        stuck = connection.execute(
            "SELECT id,batch_date FROM production_runs WHERE state='running'").fetchall()
        for row in stuck:
            connection.execute(
                "UPDATE production_runs SET state='stopped',finished_at=?,error=? WHERE id=?",
                (_stamp(moment), '上次批次被中断；已完成的选题保留', row["id"]))
        # queued 的排期退回 scheduled：任务可能建了也可能没建，退回最安全，
        # 幂等键会挡住重复生产。
        returned = connection.execute(
            "UPDATE production_slots SET state='scheduled',updated_at=? WHERE state='queued'",
            (_stamp(moment),)).rowcount
        planned = connection.execute(
            "SELECT id,slot_id FROM production_items WHERE state='planned'").fetchall()
        for row in planned:
            connection.execute(
                "UPDATE production_items SET state='skipped',message=?,updated_at=? WHERE id=?",
                ('重启前已登记但未创建任务；幂等键会防止重复生产', _stamp(moment), row["id"]))
    return {'stopped_runs': len(stuck), 'returned_slots': returned}


def status(now: datetime = None) -> dict:
    """供设置页与测试读取的概览。纯读。

    now 可注入基准时刻。与 tick() 同一套注入方式：否则测试里写死的日期
    一旦跨天，status() 用真实时钟算出另一个 batch_date，配额就恒为 0 ——
    这正是 test_daily_limit_is_cumulative_across_repeated_ticks 在 2026-10-23
    失败的原因（NOW 写死 10-22，而当天已是 10-23）。
    """
    batch = _date_str(now)
    with db.connect() as connection:
        slots = {row["state"]: row["n"] for row in connection.execute(
            "SELECT state,COUNT(*) AS n FROM production_slots GROUP BY state")}
        items = {row["state"]: row["n"] for row in connection.execute(
            "SELECT state,COUNT(*) AS n FROM production_items GROUP BY state")}
        runs = [dict(row) for row in connection.execute(
            "SELECT batch_date,state,started_at,finished_at FROM production_runs "
            "ORDER BY batch_date DESC LIMIT 14")]
    used = _used_quota(batch)
    return {
        'slots': slots,
        'items': items,
        'recent_runs': runs,
        'today': {'batch_date': batch, 'quota_used': used,
                  'quota_limit': MAX_TOPICS_PER_DAY,
                  'quota_remaining': max(0, MAX_TOPICS_PER_DAY - used)},
        'limits': {'max_topics_per_day': MAX_TOPICS_PER_DAY,
                   'stagger_minutes': STAGGER_MINUTES,
                   'stagger_enforced': True,
                   'timezone': CN_TZ_NAME},
        'note': '服务不运行就没有定时；过期与错过的排期不补发。'
                '批次只建立生成任务，不投递平台草稿。',
    }
