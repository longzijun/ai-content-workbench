"""SQLite persistence. Every workflow mutation uses an immediate transaction."""
from contextlib import contextmanager
import sqlite3

from .config import get_settings


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    directory = get_settings().runtime_dir / "data"
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "workbench.sqlite3", timeout=20, factory=Connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


@contextmanager
def transaction():
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
            platforms TEXT NOT NULL, sources TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','parked')),
            current_version_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS versions (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
            number INTEGER NOT NULL, input_revision INTEGER NOT NULL,
            payload TEXT NOT NULL, hash TEXT NOT NULL, provider TEXT NOT NULL,
            review TEXT, created_at TEXT NOT NULL,
            UNIQUE(topic_id, number)
        );
        CREATE TRIGGER IF NOT EXISTS immutable_version
        BEFORE UPDATE OF payload, hash, topic_id, number, input_revision, provider ON versions
        BEGIN SELECT RAISE(ABORT, 'version payload is immutable'); END;
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
            kind TEXT NOT NULL DEFAULT 'generate', state TEXT NOT NULL,
            stage TEXT NOT NULL, provider TEXT, error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            input_revision INTEGER NOT NULL, input_snapshot TEXT NOT NULL,
            output_version_id TEXT REFERENCES versions(id),
            events TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS active_topic_job
        ON jobs(topic_id) WHERE state IN ('queued','running');
        CREATE TABLE IF NOT EXISTS publications (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
            version_id TEXT NOT NULL REFERENCES versions(id), platform TEXT NOT NULL,
            account_label TEXT NOT NULL, ai_disclosure TEXT NOT NULL,
            scheduled_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL, url TEXT, published_at TEXT
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
            version_id TEXT NOT NULL REFERENCES versions(id), platform TEXT NOT NULL,
            title TEXT NOT NULL, account_label TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, destination TEXT NOT NULL DEFAULT 'platform',
            remote_id TEXT, remote_url TEXT, error TEXT,
            facts_confirmed INTEGER NOT NULL DEFAULT 0,
            ai_disclosure TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(version_id, platform)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY, topic_id TEXT REFERENCES topics(id),
            title TEXT NOT NULL, body TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_tasks (
            draft_id TEXT PRIMARY KEY REFERENCES drafts(id),
            state TEXT NOT NULL, stage TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
            id TEXT PRIMARY KEY, topic_id TEXT, message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_records (
            topic_id TEXT PRIMARY KEY REFERENCES topics(id),
            version_id TEXT NOT NULL REFERENCES versions(id),
            baseline TEXT, result TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trend_runs (
            id TEXT PRIMARY KEY, state TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            source_results TEXT NOT NULL DEFAULT '[]', error TEXT,
            new_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS active_trend_run
        ON trend_runs(state) WHERE state='running';
        CREATE TABLE IF NOT EXISTS trend_candidates (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, title_key TEXT NOT NULL,
            url TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', published_at TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','saved','ignored')),
            topic_id TEXT REFERENCES topics(id)
        );
        CREATE INDEX IF NOT EXISTS trend_title_key ON trend_candidates(title_key);
        CREATE TABLE IF NOT EXISTS trend_observations (
            candidate_id TEXT NOT NULL REFERENCES trend_candidates(id),
            source_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES trend_runs(id),
            source_name TEXT NOT NULL, source_url TEXT NOT NULL, url TEXT NOT NULL,
            publisher_id TEXT NOT NULL, kind TEXT NOT NULL,
            fetched_at TEXT NOT NULL, rank INTEGER, heat_value REAL, heat_label TEXT,
            PRIMARY KEY(candidate_id,source_id,run_id)
        );
        CREATE INDEX IF NOT EXISTS trend_observation_url ON trend_observations(url);
        """)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(drafts)')}
        if 'media' not in columns:
            connection.execute("ALTER TABLE drafts ADD COLUMN media TEXT NOT NULL DEFAULT '{}'")
        # P3 统一字段：热度标准化与去重可解释性。写入的仍是原始口径，归一值只作
        # 批内百分位，两者并存，避免"归一之后原来的热搜指数就看不到了"。
        for table, column, ddl in (
            ("trend_observations", "heat_normalized", "ALTER TABLE trend_observations ADD COLUMN heat_normalized REAL"),
            ("trend_observations", "merge_reason", "ALTER TABLE trend_observations ADD COLUMN merge_reason TEXT"),
            ("trend_candidates", "directions", "ALTER TABLE trend_candidates ADD COLUMN directions TEXT NOT NULL DEFAULT '[]'"),
        ):
            present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if column not in present:
                connection.execute(ddl)
        # 软删除与内容管理元数据。同样走 PRAGMA 守卫而非迁移：ALTER 不幂等，
        # 且这两张表是 init_db 的原始 13 表，改它们属于 schema 基线变更。
        # 已删除的行一律不出现在 /api/state 与 /api/content 的默认查询里。
        for table, column, ddl in (
            ("topics", "deleted_at", "ALTER TABLE topics ADD COLUMN deleted_at TEXT"),
            ("topics", "archived", "ALTER TABLE topics ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
            ("topics", "favorite", "ALTER TABLE topics ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"),
            ("topics", "priority", "ALTER TABLE topics ADD COLUMN priority INTEGER NOT NULL DEFAULT 3"),
            ("topics", "tags", "ALTER TABLE topics ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
            ("drafts", "deleted_at", "ALTER TABLE drafts ADD COLUMN deleted_at TEXT"),
        ):
            present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if column not in present:
                connection.execute(ddl)
        for table, column, ddl in (
            ("topics", "content_line", "ALTER TABLE topics ADD COLUMN content_line TEXT"),
            ("topics", "stance_ids", "ALTER TABLE topics ADD COLUMN stance_ids TEXT NOT NULL DEFAULT '[]'"),
            ("topics", "risk_confirmation", "ALTER TABLE topics ADD COLUMN risk_confirmation TEXT"),
            ("topics", "video_target_duration_seconds", "ALTER TABLE topics ADD COLUMN video_target_duration_seconds INTEGER NOT NULL DEFAULT 60"),
        ):
            present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if column not in present:
                connection.execute(ddl)
        connection.execute('CREATE INDEX IF NOT EXISTS topics_live ON topics(deleted_at, archived, updated_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS drafts_live ON drafts(deleted_at, updated_at)')
        # Forward-only migrations. Never rewrites the tables above; failures are
        # recorded rather than raised so the service still boots degraded.
        from . import migrations
        migrations.apply(connection)
        # V3 本地内容模式列守卫必须放在 migrations.apply **之后**：
        # production_slots 等表由迁移创建，全新库在守卫执行时还不存在，
        # 放前面会被跳过，首启动就缺列（state() 直接报错）。
        for table, column, ddl in (
            ("delivery_tasks", "quarantine_reason", "ALTER TABLE delivery_tasks ADD COLUMN quarantine_reason TEXT"),
            ("delivery_tasks", "quarantined_at", "ALTER TABLE delivery_tasks ADD COLUMN quarantined_at TEXT"),
            ("production_slots", "paused", "ALTER TABLE production_slots ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"),
            ("jobs", "paused", "ALTER TABLE jobs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"),
            # 底稿快速生产（批次B）：材料类型（原创/资料/改编）与粘贴原文。
            # fact_bases 由 migrations 创建，故守卫放在 migrations.apply 之后。
            ("fact_bases", "material_type", "ALTER TABLE fact_bases ADD COLUMN material_type TEXT"),
            ("fact_bases", "raw_text", "ALTER TABLE fact_bases ADD COLUMN raw_text TEXT NOT NULL DEFAULT ''"),
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if column not in present:
                connection.execute(ddl)
        # Seeding depends on a migration having landed. If the migration failed
        # the table is absent; skip rather than raise, so the "degraded boot"
        # promise above actually holds. The next start retries the migration.
        from . import brand
        try:
            brand.ensure_default(connection)
            from . import layout_templates
            layout_templates.seed(connection)
        except Exception:
            connection.rollback()
        # V3 首次数据迁移放在最后：brand 播种失败会 rollback，迁移必须在其后
        # 才能保证落库。幂等由 runtime_state 标记保证，中断后下次启动重跑安全。
        from . import v3_migration
        v3_migration.migrate(connection)
        # 工作稿独立持久化，不创建冻结版本或改变审阅状态。
        from . import content_studio
        content_studio.init_schema(connection)
        from . import candidate_drafts, auto_materials, draft_workflow
        candidate_drafts.init_schema(connection)
        auto_materials.init_schema(connection)
        draft_workflow.init_schema(connection)
        from . import execution_runtime
        execution_runtime.init_schema(connection)
        from . import research_queue
        research_queue.init_schema(connection)
        from . import access
        access.init_schema(connection)
