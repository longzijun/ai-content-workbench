"""Minimal forward-only schema migration. Never rewrites existing tables.

init_db() still owns the 13 original CREATE TABLE IF NOT EXISTS statements and
stays unchanged. This module only appends: new tables, new indexes, new columns.

Two constraints on the SQL registered here:

1. Every statement must be CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT
   EXISTS. ALTER TABLE ADD COLUMN is not idempotent and needs a PRAGMA
   table_info guard, exactly like the drafts.media column in init_db(). V2
   migrations therefore cannot use ALTER.
2. One executescript per migration. sqlite3.executescript commits any open
   transaction before running, so a migration is not atomic as a unit. If it
   fails partway, schema_migrations keeps no row for that version and the next
   start re-runs the same IF NOT EXISTS batch, which is harmless by design.

checksum is recorded but deliberately NOT verified on startup. Editing an
already-applied migration is a code review problem; refusing to boot over it
would be worse than recording it. status() exists for human audit.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

MIGRATIONS: list[tuple[int, str, str]] = []


def register(version: int, name: str, sql: str) -> None:
    """Add a migration. Versions must be unique and are applied in order."""
    if any(existing == version for existing, _, _ in MIGRATIONS):
        raise ValueError(f"迁移版本 {version} 重复")
    MIGRATIONS.append((version, name, sql))
    MIGRATIONS.sort(key=lambda item: item[0])


def _ensure_table(connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT    NOT NULL,
            checksum   TEXT    NOT NULL,
            applied_at TEXT    NOT NULL
        )
    """)


def applied(connection=None) -> set[int]:
    """Versions already recorded. Opens its own connection when none is given."""
    if connection is not None:
        _ensure_table(connection)
        return {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    from .db import connect
    with connect() as owned:
        _ensure_table(owned)
        return {row[0] for row in owned.execute("SELECT version FROM schema_migrations")}


def pending(connection=None) -> list[tuple[int, str, str]]:
    done = applied(connection)
    return [item for item in MIGRATIONS if item[0] not in done]


def apply(connection=None) -> list[str]:
    """Apply every pending migration. Returns the names applied by THIS call.

    Passing a connection reuses the caller's (init_db's) connection. Omitting it
    opens a short transaction of its own. Both paths are safe to call twice:
    the second call returns an empty list.

    Never raises. A migration that fails leaves schema_migrations unchanged for
    that version, so the next start retries it; the service still boots and the
    V2 endpoints report degraded rather than the whole app failing to start.
    """
    if connection is not None:
        return _apply_with(connection)
    from .db import transaction
    with transaction() as owned:
        return _apply_with(owned)


def _apply_with(connection) -> list[str]:
    _ensure_table(connection)
    _FAILURES.clear()
    done = []
    known = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, name, sql in MIGRATIONS:
        if version in known:
            continue
        try:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                (version, name, _checksum(sql), _now()),
            )
        except Exception as error:  # keep booting; retry on the next start
            try:
                connection.rollback()
            except Exception:
                pass
            _record_failure(version, name, error)
            break
        known.add(version)
        done.append(name)
    return done


def status(connection=None) -> dict:
    """Read-only audit view for the settings page and for tests."""
    done = applied(connection)
    pending_items = [item for item in MIGRATIONS if item[0] not in done]
    return {
        "applied": [
            {"version": version, "name": name}
            for version, name, _ in MIGRATIONS if version in done
        ],
        "pending": [{"version": version, "name": name} for version, name, _ in pending_items],
        "failures": dict(_FAILURES),
        "note": "checksum 只记录不校验；已应用的迁移被改动属于代码评审问题，不会阻止启动。",
    }


_FAILURES: dict[str, str] = {}


def _record_failure(version: int, name: str, error: Exception) -> None:
    _FAILURES[str(version)] = f"{name} 迁移失败：{error}"[:500]


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# V2 migrations
# ---------------------------------------------------------------------------

register(1, "v2_brand_profiles", """
CREATE TABLE IF NOT EXISTS brand_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    tokens      TEXT NOT NULL,
    fonts       TEXT NOT NULL,
    provenance  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS brand_profiles_default
    ON brand_profiles(is_default) WHERE is_default = 1;
""")

register(2, "v2_layout_templates", """
CREATE TABLE IF NOT EXISTS layout_templates (
    id            TEXT PRIMARY KEY,
    brand_id      TEXT NOT NULL REFERENCES brand_profiles(id),
    platform      TEXT NOT NULL CHECK(platform IN ('wechat','toutiao','xiaohongshu')),
    format_id     TEXT NOT NULL,
    content_form  TEXT NOT NULL DEFAULT '',
    render        TEXT NOT NULL CHECK(render IN ('html','text')),
    skeleton      TEXT NOT NULL DEFAULT '[]',
    title_style   TEXT NOT NULL DEFAULT '{}',
    media_spec    TEXT NOT NULL DEFAULT '{}',
    checks        TEXT NOT NULL DEFAULT '{}',
    provenance    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(brand_id, platform, format_id, content_form)
);
CREATE INDEX IF NOT EXISTS layout_templates_lookup
    ON layout_templates(brand_id, platform, content_form, enabled);
""")

register(3, "v2_layout_audits", """
CREATE TABLE IF NOT EXISTS layout_audits (
    version_id   TEXT NOT NULL REFERENCES versions(id),
    platform     TEXT NOT NULL,
    template_id  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(status IN ('passed','warning','blocked')),
    findings     TEXT NOT NULL DEFAULT '[]',
    evidence     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY(version_id, platform, template_id)
);
CREATE INDEX IF NOT EXISTS layout_audits_version ON layout_audits(version_id);
""")

register(4, "v2_production", """
CREATE TABLE IF NOT EXISTS production_slots (
    id              TEXT PRIMARY KEY,
    topic_id        TEXT NOT NULL REFERENCES topics(id),
    topic_revision  INTEGER NOT NULL,
    batch_date      TEXT NOT NULL,
    expires_at      TEXT,
    state           TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(state IN ('scheduled','queued','produced','skipped','expired','missed','cancelled')),
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(topic_id, topic_revision, batch_date)
);
CREATE INDEX IF NOT EXISTS production_slots_pending
    ON production_slots(state, batch_date);
CREATE TABLE IF NOT EXISTS production_runs (
    id          TEXT PRIMARY KEY,
    batch_date  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'running'
        CHECK(state IN ('running','succeeded','failed','stopped')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    UNIQUE(batch_date)
);
CREATE TABLE IF NOT EXISTS production_items (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES production_runs(id),
    slot_id         TEXT NOT NULL REFERENCES production_slots(id),
    topic_id        TEXT NOT NULL REFERENCES topics(id),
    topic_revision  INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'planned'
        CHECK(state IN ('planned','job_created','produced','skipped','failed','expired','cancelled')),
    job_id          TEXT REFERENCES jobs(id),
    idempotency_key TEXT NOT NULL,
    message         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(run_id, slot_id),
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS production_items_state ON production_items(state);
""")

register(5, "v2_topic_format_decisions", """
CREATE TABLE IF NOT EXISTS topic_format_decisions (
    topic_id      TEXT NOT NULL REFERENCES topics(id),
    platform      TEXT NOT NULL,
    adopted       TEXT NOT NULL DEFAULT '',
    recommended   TEXT NOT NULL DEFAULT '',
    confidence    INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    alternatives  TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'rule_only',
    rule_ranked   TEXT NOT NULL DEFAULT '[]',
    inputs_digest TEXT NOT NULL DEFAULT '',
    decided_by    TEXT NOT NULL DEFAULT 'recommendation'
        CHECK(decided_by IN ('recommendation','manual')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY(topic_id, platform)
);
CREATE INDEX IF NOT EXISTS topic_format_decisions_topic
    ON topic_format_decisions(topic_id);
""")

register(6, "v2_trend_source_health", """
CREATE TABLE IF NOT EXISTS trend_source_health (
    source_id            TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at      TEXT,
    last_attempt_at      TEXT,
    last_error           TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL
);
""")

register(7, "v2_content_management", """
-- 回收站：软删除的统一登记处。snapshot 保留删除前整行，恢复时不依赖重算。
CREATE TABLE IF NOT EXISTS content_trash (
    id             TEXT PRIMARY KEY,
    entity_type    TEXT NOT NULL CHECK(entity_type IN ('topic','draft','version','asset')),
    entity_id      TEXT NOT NULL,
    topic_id       TEXT,
    title          TEXT NOT NULL DEFAULT '',
    previous_state TEXT NOT NULL DEFAULT '',
    snapshot       TEXT NOT NULL DEFAULT '{}',
    deleted_at     TEXT NOT NULL,
    deleted_source TEXT NOT NULL DEFAULT 'ui',
    purged_at      TEXT,
    UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS content_trash_order ON content_trash(deleted_at DESC);
-- 操作日志：与 activity 分开。activity 是给人看的流水，本表是给审计和
-- 「这条内容发生过什么」用的结构化记录，只存动作与结果，绝不存凭据。
CREATE TABLE IF NOT EXISTS audit_log (
    id           TEXT PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    topic_id     TEXT,
    action       TEXT NOT NULL,
    result       TEXT NOT NULL CHECK(result IN ('success','failed')),
    detail       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'ui',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_entity ON audit_log(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_time ON audit_log(created_at DESC);
""")

register(8, "v3_radar_source_runs", """
-- 采集尝试明细。与 trend_runs 区分开：trend_runs 记一整轮的成败，本表记每个源
-- 每次尝试的原始结果（抓取条数、保留数、重复数、丢弃数、错误）。没有它，
-- 「这个源到底试过没有、是没到采集时间还是挂了」只能靠猜。
CREATE TABLE IF NOT EXISTS radar_source_runs (
    id              TEXT PRIMARY KEY,
    run_id          TEXT,
    source_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    http_status     INTEGER,
    bytes           INTEGER,
    item_count      INTEGER NOT NULL DEFAULT 0,
    kept_count      INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    dropped_count   INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS radar_source_runs_source ON radar_source_runs(source_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS radar_source_runs_run ON radar_source_runs(run_id);
""")

register(9, "v3_local_content_mode", """
-- V3 运行模式与本地内容工作流。执行提示词 2026-10-25：
-- 平台自动操作全部关闭，工作台终点是本地材料包 + 人工发布台账。
CREATE TABLE IF NOT EXISTS runtime_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 共享事实底稿：一个选题一份底稿、可修订；三平台作品都从它改编。
CREATE TABLE IF NOT EXISTS fact_bases (
    id               TEXT PRIMARY KEY,
    topic_id         TEXT NOT NULL REFERENCES topics(id),
    revision         INTEGER NOT NULL DEFAULT 1,
    audience         TEXT NOT NULL DEFAULT '',
    core_view        TEXT NOT NULL DEFAULT '',
    sources          TEXT NOT NULL DEFAULT '[]',
    facts            TEXT NOT NULL DEFAULT '[]',
    open_questions   TEXT NOT NULL DEFAULT '[]',
    author_experience TEXT NOT NULL DEFAULT '',
    private_info     TEXT NOT NULL DEFAULT '',
    asset_list       TEXT NOT NULL DEFAULT '[]',
    platform_angles  TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','ready','locked')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_bases_topic ON fact_bases(topic_id, revision);
-- 逐平台审阅记录：独立制品指纹，只改一个平台不撤销其他平台的审阅。
CREATE TABLE IF NOT EXISTS platform_reviews (
    id          TEXT PRIMARY KEY,
    draft_id    TEXT NOT NULL REFERENCES drafts(id),
    platform    TEXT NOT NULL,
    version_id  TEXT NOT NULL REFERENCES versions(id),
    fingerprint TEXT NOT NULL,
    result      TEXT NOT NULL CHECK(result IN ('approved','returned','pending_material')),
    comment     TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT 'author',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS platform_reviews_draft ON platform_reviews(draft_id, created_at DESC);
-- 材料包：审定批次的本地交付入口与 ZIP。
CREATE TABLE IF NOT EXISTS material_packages (
    id         TEXT PRIMARY KEY,
    topic_id   TEXT NOT NULL REFERENCES topics(id),
    version_id TEXT NOT NULL REFERENCES versions(id),
    state      TEXT NOT NULL DEFAULT 'ready' CHECK(state IN ('ready','superseded')),
    manifest   TEXT NOT NULL DEFAULT '{}',
    zip_path   TEXT NOT NULL DEFAULT '',
    entry_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
-- 人工发布登记：发布状态只有「未登记／用户确认已发布」；
-- 链接完整度与最终稿回填是独立字段，可同时缺失，不覆盖已发布事实。
CREATE TABLE IF NOT EXISTS publish_records (
    id              TEXT PRIMARY KEY,
    topic_id        TEXT NOT NULL REFERENCES topics(id),
    draft_id        TEXT REFERENCES drafts(id),
    version_id      TEXT NOT NULL REFERENCES versions(id),
    platform        TEXT NOT NULL,
    account_label   TEXT NOT NULL DEFAULT '',
    confirmed       INTEGER NOT NULL DEFAULT 0,
    published_at    TEXT,
    url             TEXT NOT NULL DEFAULT '',
    url_status      TEXT NOT NULL DEFAULT 'missing'
        CHECK(url_status IN ('missing','present','invalid_format')),
    platform_edited INTEGER NOT NULL DEFAULT 0,
    final_text      TEXT NOT NULL DEFAULT '',
    final_backfill  TEXT NOT NULL DEFAULT 'pending'
        CHECK(final_backfill IN ('pending','done','not_required')),
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publish_records_topic ON publish_records(topic_id, created_at DESC);
-- CSV 导入批次：预览/提交两段式，重复检测在提交时结算。
CREATE TABLE IF NOT EXISTS import_batches (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL DEFAULT '',
    row_count       INTEGER NOT NULL DEFAULT 0,
    imported_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    mapping         TEXT NOT NULL DEFAULT '{}',
    errors          TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL DEFAULT 'preview'
        CHECK(state IN ('preview','committed','discarded')),
    created_at      TEXT NOT NULL
);
-- 运营数据观测：原始指标名与值分开存，缺失记未知（NULL），不记 0。
-- metric_value 必须是 REAL：TEXT 列会把 1200.0 存成 '1200.0'，判重时
-- 与 REAL 参数跨类型比较永不相等（重复导入会重复入账）。
CREATE TABLE IF NOT EXISTS observations (
    id           TEXT PRIMARY KEY,
    batch_id     TEXT REFERENCES import_batches(id),
    platform     TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'work',
    work_ref     TEXT NOT NULL DEFAULT '',
    metric_name  TEXT NOT NULL,
    metric_value REAL,
    window_start TEXT,
    window_end   TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',
    evidence_path TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    observed_at  TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_platform ON observations(platform, observed_at DESC);
CREATE INDEX IF NOT EXISTS observations_work ON observations(work_ref);
-- 匿名读者反馈：作者摘录，AI 只整理不发送。
CREATE TABLE IF NOT EXISTS feedback_items (
    id        TEXT PRIMARY KEY,
    topic_id  TEXT REFERENCES topics(id),
    platform  TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL,
    summary   TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','organized')),
    created_at TEXT NOT NULL
);
-- 周复盘：一页结论，样本不足时明确说明，不虚构。
CREATE TABLE IF NOT EXISTS weekly_reviews (
    id         TEXT PRIMARY KEY,
    week_start TEXT NOT NULL,
    week_end   TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
""")

register(10, "v3_platform_access", """
-- 逐平台/逐账号草稿保存许可（批次D）。默认全部关闭；小红书永久关闭自动触达。
-- 启用动作仍是用户显式操作，且受 runtime_mode 全局放行闸门约束。
CREATE TABLE IF NOT EXISTS platform_access (
    platform      TEXT PRIMARY KEY,
    enabled       INTEGER NOT NULL DEFAULT 0,
    account_label TEXT NOT NULL DEFAULT '',
    enabled_at    TEXT,
    updated_at    TEXT NOT NULL
);
""")
