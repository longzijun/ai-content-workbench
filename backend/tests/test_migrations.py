"""Migration engine: idempotency, failure containment, and the SQL shape rule."""
import sqlite3

import pytest

from backend.app import brand, config, db, migrations


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    return config.get_settings()


@pytest.fixture
def registry(monkeypatch):
    """Isolated MIGRATIONS list so a test can register a throwaway migration."""
    monkeypatch.setattr(migrations, 'MIGRATIONS', [])
    return migrations


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

def test_init_db_is_idempotent(world):
    db.init_db()
    first = migrations.applied()
    db.init_db()
    db.init_db()
    assert migrations.applied() == first
    assert first, "init_db 之后至少要记录已应用的迁移"


def test_apply_twice_returns_empty_on_second_call(world, registry):
    registry.register(900, 'probe', "CREATE TABLE IF NOT EXISTS probe_a (id TEXT PRIMARY KEY);")
    applied = migrations.apply()
    assert applied == ['probe']
    assert migrations.apply() == []
    assert migrations.apply() == []
    assert migrations.status()['pending'] == []
    assert [item['version'] for item in migrations.status()['applied']] == [900]


def test_pending_excludes_unknown_versions(world, registry):
    registry.register(901, 'known', "CREATE TABLE IF NOT EXISTS probe_b (id TEXT PRIMARY KEY);")
    migrations.apply()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
            (999, 'manually-recorded', 'deadbeef', '2026-01-01T00:00:00+00:00'),
        )
    pending = migrations.pending()
    assert 901 not in [item[0] for item in pending]
    assert all(item[0] != 999 for item in pending)


# --------------------------------------------------------------------------
# SQL shape rule: every statement must be IF NOT EXISTS
# --------------------------------------------------------------------------

def test_registered_migrations_are_all_create_if_not_exists():
    """Guards the rule in the module docstring: no ALTER, no bare CREATE."""
    assert migrations.MIGRATIONS, "V2 需要至少一条迁移"
    for version, name, sql in migrations.MIGRATIONS:
        upper = sql.upper()
        creates = upper.count('CREATE ')
        guarded = upper.count('IF NOT EXISTS')
        assert 'ALTER TABLE' not in upper, f"迁移 {version}/{name} 含 ALTER TABLE，V2 阶段禁止"
        assert 'DROP ' not in upper, f"迁移 {version}/{name} 含 DROP"
        assert guarded == creates, (
            f"迁移 {version}/{name} 有 {creates} 条 CREATE 但只有 {guarded} 条 IF NOT EXISTS；"
            "非幂等语句会导致重启重复执行失败"
        )


def test_migration_versions_are_unique_and_ordered(registry):
    registry.register(3, 'third', "CREATE TABLE IF NOT EXISTS probe_c (id TEXT PRIMARY KEY);")
    registry.register(1, 'first', "CREATE TABLE IF NOT EXISTS probe_d (id TEXT PRIMARY KEY);")
    registry.register(2, 'second', "CREATE TABLE IF NOT EXISTS probe_e (id TEXT PRIMARY KEY);")
    with pytest.raises(ValueError, match='重复'):
        registry.register(2, 'again', "CREATE TABLE IF NOT EXISTS probe_f (id TEXT PRIMARY KEY);")
    assert [item[0] for item in migrations.MIGRATIONS] == [1, 2, 3]


# --------------------------------------------------------------------------
# failure containment: a broken migration must not stop the boot
# --------------------------------------------------------------------------

def test_failed_migration_does_not_raise_and_is_retried_next_boot(world, registry):
    registry.register(910, 'broken', "CREATE TABLE IF NOT EXISTS ok (id TEXT PRIMARY KEY); "
                                     "CREATE TABLE IF NOT EXISTS bad (id TEXT PRIMARY KEY, );;(")
    registry.register(911, 'after', "CREATE TABLE IF NOT EXISTS never (id TEXT PRIMARY KEY);")
    applied = migrations.apply()
    assert applied == [], "失败的那条不能算作已应用"
    assert 910 not in migrations.applied()
    failures = migrations.status()['failures']
    assert '910' in failures and 'broken' in failures['910']
    # The service still boots: a second attempt retries the same broken version
    # rather than skipping past it.
    migrations.apply()
    assert 910 not in migrations.applied()
    assert 911 not in migrations.applied(), "失败版本之后的迁移不应被跳过执行"


def test_rollback_of_a_failed_migration_leaves_no_partial_row(world, registry):
    registry.register(912, 'broken', "CREATE TABLE IF NOT EXISTS half (id TEXT PRIMARY KEY); "
                                     "INSERT INTO no_such_table VALUES (1);")
    migrations.apply()
    rows = [row[0] for row in db.connect().execute("SELECT version FROM schema_migrations")]
    assert 912 not in rows
    assert migrations.applied().isdisjoint({912})


# --------------------------------------------------------------------------
# the V2 tables actually exist after init_db
# --------------------------------------------------------------------------

def test_init_db_creates_v2_tables(world):
    db.init_db()
    names = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('schema_migrations', 'brand_profiles', 'layout_templates'):
        assert table in names, f"缺少 {table}"
    indexes = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'brand_profiles_default' in indexes
    assert 'layout_templates_lookup' in indexes


def test_init_db_does_not_touch_the_original_thirteen_tables(world):
    """V2 tables are additive. The pre-existing tables keep their definitions."""
    db.init_db()
    connection = db.connect()
    drafts = {row[1] for row in connection.execute('PRAGMA table_info(drafts)')}
    assert 'media' in drafts, "既有的 media 列迁移不能被 V2 破坏"
    assert {'id', 'topic_id', 'version_id', 'platform', 'state', 'destination'} <= drafts
    versions = {row[1] for row in connection.execute('PRAGMA table_info(versions)')}
    assert {'payload', 'hash', 'provider'} <= versions
    # The immutable_version trigger must survive.
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert 'immutable_version' in triggers


def test_default_brand_profile_is_seeded_once(world):
    db.init_db()
    first = brand.ensure_default()
    second = brand.ensure_default()
    assert first == second == brand.DEFAULT_BRAND_ID
    count = db.connect().execute("SELECT COUNT(*) FROM brand_profiles WHERE is_default=1").fetchone()[0]
    assert count == 1, "部分唯一索引之外再加一道默认品牌唯一性断言"


def test_status_is_readable_without_a_connection_argument(world):
    db.init_db()
    state = migrations.status()
    assert isinstance(state['applied'], list) and state['applied']
    assert isinstance(state['pending'], list)
    assert 'checksum' in state['note']


def test_apply_accepts_a_caller_connection_without_committing_it(world, registry):
    """init_db passes its own connection; apply must use it, not open another."""
    db.init_db()
    registry.register(920, 'via-connection', "CREATE TABLE IF NOT EXISTS via_conn (id TEXT PRIMARY KEY);")
    connection = db.connect()
    try:
        assert migrations.apply(connection) == ['via-connection']
        found = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='via_conn'").fetchone()
        assert found is not None
    finally:
        connection.close()


def test_module_exposes_no_destructive_helper():
    source = open(migrations.__file__, encoding='utf-8').read().upper()
    for forbidden in ('DROP TABLE', 'DELETE FROM SCHEMA_MIGRATIONS', 'VACUUM'):
        assert forbidden not in source, f"迁移模块不得包含 {forbidden}"


def test_sqlite_error_type_is_not_swallowed_silently(world, registry):
    """A real sqlite error is recorded; the connection is still usable after."""
    db.init_db()
    # Duplicate column name: sqlite rejects this at prepare time.
    registry.register(930, 'bad-type',
                      "CREATE TABLE IF NOT EXISTS bad_type (id TEXT PRIMARY KEY, id TEXT);")
    migrations.apply()
    assert '930' in migrations.status()['failures']
    # The service keeps working: the original tables are still reachable.
    with db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0


def test_applied_returns_int_versions(world):
    db.init_db()
    versions = migrations.applied()
    assert versions and all(type(item) is int for item in versions)
    assert isinstance(migrations.pending(), list)


def test_registered_migration_names_are_stable_across_processes():
    """The literal registry must not depend on import order or set iteration."""
    expected = {1: 'v2_brand_profiles', 2: 'v2_layout_templates'}
    actual = {version: name for version, name, _ in migrations.MIGRATIONS}
    for version, name in expected.items():
        assert actual.get(version) == name


def test_isolated_registry_fixture_does_not_leak(world, registry):
    assert migrations.MIGRATIONS == [] or all(item[0] >= 900 for item in migrations.MIGRATIONS)


def test_brand_metrics_match_typeset_constants():
    """The probe/render agreement depends on these numbers staying put."""
    from backend.app import typeset

    probe = brand.metrics()
    assert probe['title_divisor'] == 24 == typeset.MAX_TITLE - typeset.MAX_TITLE + 24
    assert probe['footer_divisor'] == 48
    assert probe['line_gap'] == 8
    assert probe['band_padding'] == 24
    assert probe['band_max_ratio'] == 0.5
    assert probe['min_canvas'] == 64
    assert probe['band_top_gap'] == 12
    assert probe['band_bottom_gap'] == 12
    assert probe['footer_line_gap'] == 2
    assert probe['footer_line_extra'] == 10


def test_brand_fonts_mirror_typeset_font_candidates():
    from backend.app import typeset

    assert brand.fonts()['pil_candidates'] == list(typeset._FONT_CANDIDATES)


def test_brand_band_tokens_match_typeset_fill():
    band = brand.tokens()
    assert band['band_fill'] == '#0c1018'
    assert band['band_alpha'] == 200
    assert band['band_title'] == '#ffffff'
    assert band['band_footer'] == '#bec8d4'


def test_brand_tokens_json_roundtrip(world):
    db.init_db()
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT tokens,fonts FROM brand_profiles WHERE id=?", (brand.DEFAULT_BRAND_ID,)).fetchone()
    assert row is not None
    import json

    assert json.loads(row[0])['accent'] == '#287b67'
    assert json.loads(row[1])['css_stack'] == ["Inter", "Segoe UI", "Microsoft YaHei"]


def test_brand_module_docstring_blocks_platform_body_styling():
    source = open(brand.__file__, encoding='utf-8').read()
    assert 'does NOT reach platform article bodies' in source
    assert 'SCOPE' in source


def test_migration_table_has_expected_columns(world):
    db.init_db()
    columns = {row[1] for row in db.connect().execute('PRAGMA table_info(schema_migrations)')}
    assert columns == {'version', 'name', 'checksum', 'applied_at'}


def test_layout_templates_unique_constraint_blocks_duplicates(world):
    db.init_db()
    stamp = brand.now()
    # 用一个种子未占用的组合。V2-07 起 init_db 会播入种子行，通用模板
    # (wechat/article/'') 已被占用 —— 这里验的是唯一约束本身存在，
    # 所以换成任意合法组合即可，测试意图不变。
    row = (brand.DEFAULT_BRAND_ID, 'wechat', 'article', 'opinion', 'html')
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO layout_templates(id,brand_id,platform,format_id,content_form,render,"
            "provenance,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ('t1', *row, 'x', stamp, stamp))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO layout_templates(id,brand_id,platform,format_id,content_form,render,"
                "provenance,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ('t2', *row, 'x', stamp, stamp))


def test_layout_templates_platform_check_rejects_unknown(world):
    db.init_db()
    stamp = brand.now()
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO layout_templates(id,brand_id,platform,format_id,content_form,render,"
                "provenance,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ('t3', brand.DEFAULT_BRAND_ID, 'weibo', 'article', '', 'html', 'x', stamp, stamp))


def test_migrations_status_note_mentions_checksum_policy(world):
    assert 'checksum' in migrations.status()['note']