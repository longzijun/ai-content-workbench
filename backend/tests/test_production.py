"""Daily production: idempotency, expiry, and the two-phase write.

The load-bearing invariant is in test_tick_repeatedly_never_locks_the_database:
service.generate() opens its own transaction, so calling it while holding a
write transaction would block on BEGIN IMMEDIATE and raise "database is
locked". The two-phase write in _act() is what avoids that.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend.app import db, production, scheduler, service

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)
TODAY = "2026-10-22"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config_module := __import__("backend.app.config", fromlist=["config"]),
                        "discover_codex", lambda: "")
    monkeypatch.setattr(__import__("backend.app.adapters", fromlist=["adapters"]),
                        "_loaded_local_model_info", lambda _: {})
    db.init_db()
    yield config_module.get_settings()
    # 后台线程会读全局 settings：测试结束前必须停干净，否则它会在
    # monkeypatch 还原后继续往下一个测试（或生产库）写 tick。
    scheduler.stop()


def approve(topic_id="t1", revision=1, platforms=("wechat",)):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,content_line,stance_ids,created_at,updated_at)"
            " VALUES(?,?,'b',?,'[]',?,'approved','A','[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (topic_id, "选题 " + topic_id, json.dumps(list(platforms)), revision))
    return topic_id


def rows(sql, *params):
    with db.connect() as connection:
        return [dict(row) for row in connection.execute(sql, params)]


# --------------------------------------------------------------------------
# 红线：定时器不碰投递链路，也不新增状态值
# --------------------------------------------------------------------------

def test_production_never_touches_drafts_or_delivery_tasks(world, monkeypatch):
    """批次只到 drafts.state='local'，投递是人类的动作。

    用 SQLite 原生的 trace 回调守：全程不得出现任何写 drafts /
    delivery_tasks 的语句。sqlite3.Connection 是不可 monkeypatch 的内置类型，
    set_trace_callback 才是为这种场景设计的钩子。
    """
    approve()
    production.schedule_slot("t1", TODAY, expires_at=(NOW + timedelta(hours=2)).isoformat())
    seen = []
    original_connect = db.connect

    def traced():
        connection = original_connect()
        connection.set_trace_callback(lambda sql: seen.append(sql.strip()))
        return connection

    monkeypatch.setattr(db, "connect", traced)
    production.tick(NOW)

    joined = " ".join(seen).lower()
    assert joined, "trace 回调应捕获到语句"
    for forbidden in ("insert into drafts", "update drafts", "insert into delivery_tasks",
                      "update delivery_tasks"):
        assert forbidden not in joined, f"批次不得触碰投递链路（{forbidden}）"


def test_production_does_not_import_delivery(world):
    import ast

    tree = ast.parse(open(production.__file__, encoding="utf-8").read())
    imported = {alias.name.split('.')[-1] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    for forbidden in ("delivery", "browser_drafts", "artifacts", "browser_bridge"):
        assert forbidden not in imported


def test_production_does_not_generate_content_itself(world, monkeypatch):
    """调度器只建任务，生成永远发生在 worker 线程里。"""
    approve()
    production.schedule_slot("t1", TODAY)
    called = []

    def forbidden(*args, **kwargs):
        called.append(args)
        raise AssertionError("调度器不得直接调用生成执行器")

    from backend.app import adapters, local_generation
    monkeypatch.setattr(adapters, "generate_payload", forbidden)
    monkeypatch.setattr(local_generation, "stream_payload_with_retry", forbidden)
    production.tick(NOW)
    assert called == [], "tick 只应建立 jobs 记录"
    assert rows("SELECT COUNT(*) AS n FROM jobs WHERE state='queued'")[0]["n"] == 1


# --------------------------------------------------------------------------
# 跨事务：两阶段写入
# --------------------------------------------------------------------------

def test_tick_repeatedly_never_locks_the_database(world):
    """service.generate() 自己开事务；持写事务调它会 BEGIN IMMEDIATE 阻塞 20 秒
    然后抛 database is locked。连跑三次是这条不变式的守门测试。"""
    approve()
    production.schedule_slot("t1", TODAY)
    for _ in range(3):
        production.tick(NOW)


def test_two_phase_write_leaves_an_item_even_when_generate_fails(world, monkeypatch):
    """阶段一先提交：即使建任务失败，item 也留在库里，重启后不会静默丢失。

    失败后的 slot 结清为 skipped 而不是退回 scheduled：退回会让它每 2 秒
    被 tick 捞一次、每次撞 (run,slot) 唯一约束后什么都不做 —— 既不重试
    也不结案，在排期列表里永远显示「待生产」。
    """
    approve()
    production.schedule_slot("t1", TODAY)

    def boom(topic_id, revision):
        raise ValueError("选题范围已变化")

    monkeypatch.setattr(service, "generate", boom)
    result = production.tick(NOW)
    assert result["created"] == 0
    items = rows("SELECT state,message FROM production_items")
    assert len(items) == 1
    assert items[0]["state"] == "failed"
    slot = rows("SELECT state,note FROM production_slots")[0]
    assert slot["state"] == "skipped", "失败的排期必须结清，不能留在 scheduled 空转"
    assert "重新排期" in slot["note"], "结清原因要写清下一步"


def test_failed_slot_is_not_picked_again_on_the_next_tick(world, monkeypatch):
    """失败结清后，后续 tick 不得反复领取它。"""
    approve()
    production.schedule_slot("t1", TODAY)
    monkeypatch.setattr(service, "generate", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    production.tick(NOW)
    for _ in range(3):
        assert production.tick(NOW)["created"] == 0
    assert rows("SELECT COUNT(*) AS n FROM production_items")[0]["n"] == 1


# --------------------------------------------------------------------------
# 幂等：四道闸门
# --------------------------------------------------------------------------

def test_one_run_per_day_regardless_of_restarts(world):
    approve()
    production.schedule_slot("t1", TODAY)
    for _ in range(3):
        production.tick(NOW)
    runs = rows("SELECT batch_date FROM production_runs")
    assert len(runs) == 1, "一天至多一批"


def test_repeated_ticks_do_not_create_duplicate_jobs(world):
    approve()
    production.schedule_slot("t1", TODAY)
    first = production.tick(NOW)
    assert first["created"] == 1
    for _ in range(3):
        assert production.tick(NOW)["created"] == 0
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1


def test_idempotency_key_is_stable_across_processes(world):
    first = production.idempotency_key("t1", 1, TODAY)
    assert first == production.idempotency_key("t1", 1, TODAY)
    assert first != production.idempotency_key("t1", 2, TODAY)
    assert first != production.idempotency_key("t2", 1, TODAY)
    assert first != production.idempotency_key("t1", 1, "2026-10-23")
    assert len(first) == 32


def test_same_slot_on_different_days_is_two_batches(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.schedule_slot("t1", "2026-10-23")
    production.tick(NOW)
    production.tick(NOW + timedelta(days=1))
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT COUNT(*) AS n FROM production_runs")[0]["n"] == 2


def test_repeat_schedule_updates_instead_of_duplicating(world):
    approve()
    first = production.schedule_slot("t1", TODAY, note="第一次")
    second = production.schedule_slot("t1", TODAY, note="改一下")
    assert second["created"] is False
    assert second["id"] == first["id"]
    slots = production.list_slots()
    assert len(slots) == 1 and slots[0]["note"] == "改一下"


# --------------------------------------------------------------------------
# 审批池是两重的
# --------------------------------------------------------------------------

def test_unapproved_topic_cannot_be_scheduled(world):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at)"
            " VALUES('t-pending','未批准','','[\"wechat\"]','[]',1,'pending',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    with pytest.raises(Exception) as error:
        production.schedule_slot("t-pending", TODAY)
    assert "批准" in str(error.value)


def test_approval_alone_does_not_enqueue_production(world):
    """只用 approved 会把「批准」悄悄扩张成「批准即排期」。"""
    approve()
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT COUNT(*) AS n FROM production_items")[0]["n"] == 0


def test_topic_losing_approval_after_scheduling_is_skipped(world):
    approve()
    production.schedule_slot("t1", TODAY)
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET status='parked' WHERE id='t1'")
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_slots")[0]["state"] == "skipped"


def test_revision_change_after_scheduling_is_skipped(world):
    approve()
    production.schedule_slot("t1", TODAY)
    with db.transaction() as connection:
        connection.execute("UPDATE topics SET revision=2 WHERE id='t1'")
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_slots")[0]["state"] == "skipped"


# --------------------------------------------------------------------------
# 过期不补发
# --------------------------------------------------------------------------

def test_expired_slot_creates_no_job(world):
    approve()
    production.schedule_slot("t1", TODAY, expires_at=(NOW - timedelta(hours=1)).isoformat())
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_slots")[0]["state"] == "expired"
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0


def test_slot_from_a_previous_day_is_missed_not_replayed(world):
    """服务停三天再启动，不应该一次产出三天的量。"""
    approve()
    production.schedule_slot("t1", "2026-10-19")
    production.tick(NOW)
    assert rows("SELECT state FROM production_slots")[0]["state"] == "missed"
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0


def test_expired_message_states_the_no_backfill_rule(world):
    approve()
    production.schedule_slot("t1", TODAY, expires_at=(NOW - timedelta(hours=1)).isoformat())
    production.tick(NOW)
    slot = rows("SELECT * FROM production_slots")[0]
    assert "过期" in slot["note"] or "过期" in json.dumps(slot)


def test_recover_marks_stuck_runs_as_stopped(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    with db.transaction() as connection:
        connection.execute("UPDATE production_runs SET state='running',finished_at=NULL WHERE id="
                           "(SELECT id FROM production_runs LIMIT 1)")
    report = production.recover(NOW)
    assert report["stopped_runs"] == 1
    assert rows("SELECT state FROM production_runs")[0]["state"] == "stopped"
    assert rows("SELECT state FROM production_slots")[0]["state"] == "scheduled", \
        "重启后幂等键会挡住重复生产，slot 退回待排期最安全"


def test_recover_is_safe_to_run_when_nothing_is_stuck(world):
    assert production.recover(NOW)["stopped_runs"] == 0


# --------------------------------------------------------------------------
# 上限与状态
# --------------------------------------------------------------------------

def test_daily_limit_is_enforced(world, monkeypatch):
    # 这条只测「超额怎么处理」，先把错峰关掉，否则一轮 tick 只建一个。
    monkeypatch.setattr(production, "STAGGER_MINUTES", 0)
    for index in range(production.MAX_TOPICS_PER_DAY + 2):
        approve(f"t{index}")
        production.schedule_slot(f"t{index}", TODAY)
    assert production.tick(NOW)["created"] == production.MAX_TOPICS_PER_DAY
    over = rows("SELECT state,note FROM production_slots WHERE state='skipped'")
    assert len(over) == 2, "超额排期要结清并给出原因，不能静默留到次日变 missed"
    assert all("上限" in row["note"] for row in over)


def test_daily_limit_is_cumulative_across_repeated_ticks(world, monkeypatch):
    """连续 tick 不能绕开日配额。

    旧 `_claim_slots()` 只限制**单次领取量**，而定时器每 2 秒 tick 一次：
    五个待产排期会在三秒内被三轮领完，日配额形同虚设。这条测试守的是
    「当日累计」而不是「单轮上限」。
    """
    for index in range(5):
        approve(f"t{index}")
        production.schedule_slot(f"t{index}", TODAY)
    # 把错峰关掉，让 tick 之间只受配额约束（错峰另有专门测试）。
    monkeypatch.setattr(production, "STAGGER_MINUTES", 0)
    created = 0
    for _ in range(5):
        created += production.tick(NOW)["created"]
    assert created == production.MAX_TOPICS_PER_DAY
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == production.MAX_TOPICS_PER_DAY
    assert production.status(NOW)["today"]["quota_used"] == production.MAX_TOPICS_PER_DAY
    assert production.status(NOW)["today"]["quota_remaining"] == 0


def test_cancel_slot_prevents_production(world):
    approve()
    slot = production.schedule_slot("t1", TODAY)
    production.cancel_slot(slot["id"])
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_slots")[0]["state"] == "cancelled"


def test_run_summary_records_the_degraded_boot_note(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    summary = json.loads(rows("SELECT summary FROM production_runs")[0]["summary"])
    assert summary["job_created"] == 1
    assert "不补发" in summary["degraded_note"]


def test_status_shape_is_stable(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    state = production.status(NOW)
    assert set(state) == {"slots", "items", "recent_runs", "today", "limits", "note"}
    assert state["limits"]["max_topics_per_day"] == production.MAX_TOPICS_PER_DAY
    assert state["limits"]["timezone"] == "Asia/Shanghai"
    assert state["today"]["quota_limit"] == production.MAX_TOPICS_PER_DAY
    assert json.loads(json.dumps(state, ensure_ascii=False)) == state


def test_list_slots_can_filter_by_state(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    assert len(production.list_slots("queued")) == 1
    assert production.list_slots("cancelled") == []


# --------------------------------------------------------------------------
# 定时器本身
# --------------------------------------------------------------------------

def test_scheduler_does_not_run_before_the_production_hour(world):
    """传入时刻带 +08:00（本机时区）时，凌晨 5 点不到点、7 点到点。

    刻意用带时区的时刻，而不是依赖测试机本身的时区 —— 否则换个时区跑
    就会飘。生产时刻按本机本地时间判定，用户说「早上六点」指屏幕上的六点。
    """
    local = timezone(timedelta(hours=8))
    assert scheduler.tick(datetime(2026, 10, 22, 5, 0, tzinfo=local)) is False
    assert scheduler.tick(datetime(2026, 10, 22, 7, 0, tzinfo=local)) is True


def test_scheduler_tick_is_idempotent_within_the_minute(world):
    local = timezone(timedelta(hours=8))
    approve()
    production.schedule_slot("t1", TODAY)
    at = datetime(2026, 10, 22, 7, 0, tzinfo=local)
    assert scheduler.tick(at) is True
    assert scheduler.tick(at) is True
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1


def test_start_and_stop_are_safe_to_repeat(world):
    scheduler.start()
    scheduler.start()
    assert scheduler.status()["running"] is True
    scheduler.stop()
    scheduler.stop()
    assert scheduler.status()["running"] is False


def test_scheduler_does_not_register_any_system_task(world):
    """本机 schtasks/WMI 被安全策略拦截，定时必须完全在进程内。"""
    import ast

    tree = ast.parse(open(scheduler.__file__, encoding="utf-8").read())
    imported = {alias.name.split('.')[-1] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    for forbidden in ("subprocess", "apscheduler"):
        assert forbidden not in imported
    assert "不依赖任何 OS 级计划任务" in open(scheduler.__file__, encoding="utf-8").read()


def test_scheduler_status_declares_the_service_boundary():
    state = scheduler.status()
    assert "服务不运行就没有定时" in state["note"]
    assert state["production_hour_local"] == 6


def test_background_process_stops_the_scheduler_before_the_worker():
    """D4：生命周期已迁至独立后台进程，仍须先停止创建任务的调度器。"""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'scripts' / 'worker.py').read_text('utf-8')
    stop_all = source.split('finally:')[-1]
    assert stop_all.index('scheduler.stop()') < stop_all.index('worker.stop_worker()')


# --------------------------------------------------------------------------
# 表结构
# --------------------------------------------------------------------------

def test_three_tables_exist_with_the_expected_constraints(world):
    assert rows("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'production_%'")
    slot_states = rows("SELECT sql FROM sqlite_master WHERE name='production_slots'")[0]["sql"]
    for state in ("scheduled", "queued", "produced", "skipped", "expired", "missed", "cancelled"):
        assert f"'{state}'" in slot_states
    item_sql = rows("SELECT sql FROM sqlite_master WHERE name='production_items'")[0]["sql"]
    assert "UNIQUE(run_id, slot_id)" in item_sql
    assert "UNIQUE(idempotency_key)" in item_sql


def test_idempotency_key_constraint_is_enforced(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    run_id = rows("SELECT id FROM production_runs")[0]["id"]
    slot_id = rows("SELECT id FROM production_slots")[0]["id"]
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO production_items(id,run_id,slot_id,topic_id,topic_revision,state,"
                "idempotency_key,message,created_at,updated_at) VALUES(?,?,?,?,?, 'planned',?,?,?,?)",
                ("dup", run_id, slot_id, "t1", 1,
                 production.idempotency_key("t1", 1, TODAY), "", "2026-01-01", "2026-01-01"))


def test_production_items_reference_a_real_job(world):
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    job_id = rows("SELECT job_id FROM production_items")[0]["job_id"]
    assert rows("SELECT id FROM jobs WHERE id=?", job_id), "item 必须指向真实存在的 job"


# --------------------------------------------------------------------------
# 批次日期按北京时间，不跟 UTC 走
# --------------------------------------------------------------------------

def test_batch_date_uses_beijing_calendar_day(world):
    """北京时间 10-22 06:30 在 UTC 下是 10-21 22:30。

    旧实现 `_date_str()` 直接取 UTC 日期，批次会被记到前一天：北京时间
    早六点的生产，run 的 batch_date 是昨天，和用户在界面上看到的日期不一致。
    """
    beijing = timezone(timedelta(hours=8))
    moment = datetime(2026, 10, 22, 6, 30, tzinfo=beijing)
    assert moment.astimezone(timezone.utc).date().isoformat() == "2026-10-21", \
        "这条时刻必须跨 UTC 日界，否则测不到缺陷"
    approve()
    production.schedule_slot("t1", "2026-10-22")
    result = production.tick(moment)
    assert result["batch_date"] == "2026-10-22"
    assert result["created"] == 1
    assert rows("SELECT batch_date FROM production_runs")[0]["batch_date"] == "2026-10-22"


def test_slot_for_beijing_today_is_not_missed_at_early_morning(world):
    """北京凌晨排的今天，不能被判成「昨天已过、不补发」。"""
    beijing = timezone(timedelta(hours=8))
    moment = datetime(2026, 10, 22, 6, 0, tzinfo=beijing)
    approve()
    production.schedule_slot("t1", "2026-10-22")
    production.tick(moment)
    assert rows("SELECT state FROM production_slots")[0]["state"] == "queued"


def test_bad_batch_date_is_rejected_at_the_entry(world):
    """batch_date 参与字符串比较，格式脏数据会让整条排期逻辑失真。"""
    approve()
    for bad in ("2026/10/22", "2026-13-01", "明天", "20261022", ""):
        with pytest.raises(ValueError):
            production.schedule_slot("t1", bad)


# --------------------------------------------------------------------------
# 错峰：第 n 个选题等 n*20 分钟
# --------------------------------------------------------------------------

def test_stagger_is_enforced_between_slots(world):
    """20 分钟错峰必须真的推迟入队，不能只写在配置里。

    旧实现 `STAGGER_MINUTES` 只出现在 `status()` 的展示字段里，三个选题
    会在同一个 tick 内全部建任务。
    """
    for index in range(3):
        approve(f"t{index}")
        production.schedule_slot(f"t{index}", TODAY)

    first = production.tick(NOW)
    assert first["created"] == 1
    assert first["waiting"] == 2, "其余两个在等错峰时刻"

    assert production.tick(NOW + timedelta(minutes=5))["created"] == 0
    assert production.tick(NOW + timedelta(minutes=20))["created"] == 1
    assert production.tick(NOW + timedelta(minutes=39))["created"] == 0
    assert production.tick(NOW + timedelta(minutes=40))["created"] == 1
    assert rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 3


def test_run_stays_running_while_slots_wait_for_stagger(world):
    """还有排期在等错峰时，批次不能提前结案。"""
    for index in range(2):
        approve(f"t{index}")
        production.schedule_slot(f"t{index}", TODAY)
    production.tick(NOW)
    run = rows("SELECT state,finished_at FROM production_runs")[0]
    assert run["state"] == "running"
    assert run["finished_at"] is None
    production.tick(NOW + timedelta(minutes=20))
    run = rows("SELECT state,finished_at FROM production_runs")[0]
    assert run["state"] == "succeeded"


# --------------------------------------------------------------------------
# 空轮询不得改写批次结果
# --------------------------------------------------------------------------

def test_empty_poll_does_not_flip_a_succeeded_run_to_failed(world):
    """定时器每 2 秒跑一次；空转不能把已经成功的批次改成 failed。

    旧 `_finish_run()` 只看本轮 items：第三轮没有新任务时 `any(...)` 为假，
    同一批次被写成 failed，批次状态永远反映不了真实结果。
    """
    approve()
    production.schedule_slot("t1", TODAY)
    assert production.tick(NOW)["created"] == 1
    for _ in range(3):
        assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_runs")[0]["state"] == "succeeded"
    summary = json.loads(rows("SELECT summary FROM production_runs")[0]["summary"])
    assert summary["job_created"] == 1, "累计口径：job_created 不随空轮询归零"


def test_empty_batch_with_no_slots_is_not_a_failure(world):
    """当天没有任何排期，不是失败。"""
    assert production.tick(NOW)["created"] == 0
    assert rows("SELECT state FROM production_runs")[0]["state"] == "succeeded"


def test_settled_run_is_not_reactivated_by_the_next_tick(world):
    """已结案的批次不能被下一次 tick 翻回 running。

    定时器每 2 秒跑一次。旧 `_ensure_run()` 无条件 `SET state='running'`，
    于是 `/api/production` 里看到的几乎永远是「state=running 且
    finished_at 有值」——用户看到「进行中」，批次其实早就完成了。
    """
    first = production._ensure_run(TODAY, NOW)
    production._finish_run(first, [], NOW, 0)
    assert rows("SELECT state FROM production_runs")[0]["state"] == "succeeded"

    again = production._ensure_run(TODAY, NOW + timedelta(seconds=2))
    assert again["state"] == "succeeded", "已结案的批次不应被重新激活"

    run = rows("SELECT state,finished_at FROM production_runs")[0]
    assert run["state"] == "succeeded"
    assert not (run["state"] == "running" and run["finished_at"]), \
        "running 与 finished_at 不得同时出现"


def test_interrupted_run_is_reactivated_and_clears_finished_at(world):
    """被中断的批次允许本日重跑，且必须清掉旧的完成时间。"""
    approve()
    production.schedule_slot("t1", TODAY)
    production.tick(NOW)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE production_runs SET state='stopped',finished_at='2026-10-22T00:00:00+00:00'")
    again = production._ensure_run(TODAY, NOW + timedelta(minutes=1))
    assert again["state"] == "running"
    run = rows("SELECT state,finished_at FROM production_runs")[0]
    assert run["state"] == "running"
    assert run["finished_at"] is None
