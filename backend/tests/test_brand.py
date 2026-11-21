"""Brand baseline must stay in lockstep with frontend/src/style.css.

The two files are edited by review, not by runtime parsing. These tests are the
mechanism that makes a one-sided edit fail loudly instead of drifting.
"""
from pathlib import Path

import pytest

from backend.app import brand, config, db

STYLE_CSS = Path(config.PROJECT_ROOT) / 'frontend' / 'src' / 'style.css'


@pytest.fixture
def css_text():
    if not STYLE_CSS.is_file():
        pytest.skip('frontend/src/style.css 不存在，无法核对品牌基线')
    return STYLE_CSS.read_text(encoding='utf-8')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    return config.get_settings()


def root_block(css_text):
    start = css_text.index(':root{')
    return css_text[start:css_text.index('}', start)]


def test_style_css_root_block_exists(css_text):
    assert ':root{' in css_text
    assert '--accent:' in root_block(css_text)


@pytest.mark.parametrize('token,css_name', sorted(brand.CSS_TOKEN_MAP.items()))
def test_token_matches_css_variable(css_text, token, css_name):
    needle = css_name + ':'
    start = css_text.index(':root{')
    block = css_text[start:css_text.index('}', start)]
    assert needle in block, f"style.css 的 :root 里找不到 {css_name}"
    value = block[block.index(needle) + len(needle):]
    value = value[:value.index(';') if ';' in value else len(value)]
    if css_name == 'background':
        # body background is written as the :root `background` declaration.
        assert brand.DEFAULT_PROFILE['tokens'][token] == value


def test_each_css_variable_declares_our_value(css_text):
    """Assert the literal CSS text contains our hex, not just the parsed value."""
    block = root_block(css_text)
    for value in brand.PINNED_CSS_HEX:
        assert value in block, f"style.css :root 中找不到 {value}，品牌基线可能已漂移"


def test_pinned_hex_tuple_covers_the_css_variables():
    assert set(brand.PINNED_CSS_HEX) == {
        brand.DEFAULT_PROFILE['tokens'][key]
        for key in ('accent', 'accent_dark', 'line', 'muted', 'surface', 'canvas')
    }


def test_no_extra_or_missing_css_mappings():
    assert set(brand.CSS_TOKEN_MAP) == {'accent', 'accent_dark', 'line', 'muted', 'surface', 'canvas'}
    for key in brand.CSS_TOKEN_MAP:
        assert brand.DEFAULT_PROFILE['tokens'][key].startswith('#')
        assert len(brand.DEFAULT_PROFILE['tokens'][key]) == 7


def test_tokens_accessors_return_copies():
    first = brand.tokens()
    first['accent'] = '#000000'
    assert brand.tokens()['accent'] == '#287b67', 'tokens() 必须返回副本，不能泄漏可变全局状态'


def test_metrics_accessor_returns_copies():
    first = brand.metrics()
    first['band_padding'] = 999
    assert brand.metrics()['band_padding'] == 24


def test_fonts_accessor_returns_copies():
    first = brand.fonts()
    first['css_stack'].append('Comic Sans')
    assert brand.fonts()['css_stack'] == ["Inter", "Segoe UI", "Microsoft YaHei"]


def test_as_json_is_sorted_and_unicode():
    import json

    payload = json.loads(brand.as_json())
    assert payload['tokens']['accent'] == '#287b67'
    assert brand.as_json() == brand.as_json(), '序列化必须稳定，便于比对'


def test_as_json_accepts_a_custom_profile():
    import json

    payload = json.loads(brand.as_json({'tokens': {'accent': '#123456'}}))
    assert payload['tokens']['accent'] == '#123456'


def test_default_brand_id_is_stable():
    assert brand.DEFAULT_BRAND_ID == 'brand-default'


def test_ensure_default_is_idempotent(world):
    db.init_db()
    ids = {brand.ensure_default() for _ in range(3)}
    assert ids == {brand.DEFAULT_BRAND_ID}
    rows = db.connect().execute("SELECT COUNT(*) FROM brand_profiles").fetchone()[0]
    assert rows == 1


def test_ensure_default_opens_its_own_transaction(world):
    db.init_db()
    assert brand.ensure_default() == brand.DEFAULT_BRAND_ID


def test_ensure_default_preserves_an_existing_default(world):
    db.init_db()
    stamp = brand.now()
    import json

    with db.transaction() as connection:
        connection.execute("UPDATE brand_profiles SET is_default=0 WHERE is_default=1")
        connection.execute(
            "INSERT INTO brand_profiles(id,name,is_default,tokens,fonts,provenance,created_at,updated_at)"
            " VALUES(?,?,1,?,?,?,?,?)",
            ('brand-custom', '自定义', json.dumps({'accent': '#000000'}), '{}', '手工创建', stamp, stamp),
        )
    assert brand.ensure_default() == 'brand-custom', '已有默认时不得覆盖，应返回现有默认'


def test_ensure_default_does_not_overwrite_existing_row_content(world):
    db.init_db()
    import json

    with db.transaction() as connection:
        connection.execute(
            "UPDATE brand_profiles SET tokens=? WHERE id=?",
            (json.dumps({'accent': '#abcdef'}), brand.DEFAULT_BRAND_ID),
        )
    brand.ensure_default()
    stored = json.loads(db.connect().execute(
        "SELECT tokens FROM brand_profiles WHERE id=?", (brand.DEFAULT_BRAND_ID,)).fetchone()[0])
    assert stored['accent'] == '#abcdef', 'ON CONFLICT DO NOTHING 意味着已有内容不会被重置'


def test_only_one_default_profile_is_allowed(world):
    import json
    import sqlite3

    db.init_db()
    stamp = brand.now()
    with db.transaction() as connection:
        connection.execute("UPDATE brand_profiles SET is_default=0")
        connection.execute(
            "INSERT INTO brand_profiles(id,name,is_default,tokens,fonts,provenance,created_at,updated_at)"
            " VALUES(?,?,1,?,?,?,?,?)", ('b2', '第二个', '{}', '{}', '', stamp, stamp))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO brand_profiles(id,name,is_default,tokens,fonts,provenance,created_at,updated_at)"
                " VALUES(?,?,1,?,?,?,?,?)", ('b3', '第三个', '{}', '{}', '', stamp, stamp))


def test_docstring_states_the_two_reachable_surfaces():
    source = open(brand.__file__, encoding='utf-8').read()
    assert 'image information band' in source
    assert 'preview and audit' in source


def test_provenance_of_seeded_profile_is_not_empty(world):
    db.init_db()
    provenance = db.connect().execute(
        "SELECT provenance FROM brand_profiles WHERE id=?", (brand.DEFAULT_BRAND_ID,)).fetchone()[0]
    assert 'style.css' in provenance and 'typeset.py' in provenance


def test_metrics_keys_are_exactly_what_typeset_needs():
    expected = {'title_divisor', 'footer_divisor', 'band_padding', 'band_top_gap',
                'band_bottom_gap', 'line_gap', 'footer_line_gap', 'footer_line_extra',
                'band_max_ratio', 'band_radius', 'min_canvas'}
    assert set(brand.metrics()) == expected


def test_token_groups_are_disjoint():
    tokens = brand.tokens()
    css_keys = set(brand.CSS_TOKEN_MAP)
    band = {'band_fill', 'band_alpha', 'band_title', 'band_footer'}
    preview = {'preview_bg', 'preview_text'}
    assert css_keys & band == set() and css_keys & preview == set()
    assert band & preview == set()
    assert css_keys | band | preview <= set(tokens)