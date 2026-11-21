"""Brand visual baseline. The single source of truth for derived visuals.

DEFAULT_PROFILE mirrors frontend/src/style.css :root. The two must change in the
same commit; test_brand.py pins the hex values against the real file so a
one-sided edit fails the suite. Runtime CSS parsing is deliberately avoided:
frontend/dist may be absent, and the two sides are expected to agree by review,
not by file IO at runtime.

SCOPE — read this before wiring anything up. The brand profile reaches produced
output in exactly two places:

  1. the image information band drawn by typeset / typeset_engine
  2. the in-app preview and audit surfaces

It does NOT reach platform article bodies. The visual result of a draft on
WeChat/Toutiao/Xiaohongshu is decided by that platform's own editor, and the
workbench has no authority over it. Injecting brand styling into platform
bodies would also break richtext.py's conservative semantic-tag strategy
(strong / h2-h3 only, nothing else), which exists so that read-back comparison
stays stable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

#: Values below are copied from frontend/src/style.css :root. See module docstring.
DEFAULT_PROFILE = {
    "tokens": {
        # --- mirrored from CSS variables ---
        "accent": "#287b67",       # 墨绿 · --accent
        "accent_dark": "#1a5e4e",  # --accent-dark
        "line": "#e5e9e1",         # --line
        "muted": "#83908a",        # --muted
        "surface": "#fffefa",      # --surface
        "canvas": "#f7f7f2",       # 宣纸米白 · body background
        "ink": "#263b37",          # 主文字 · :root color
        "ink_soft": "#334942",     # 正文 · .draft-body / .body-editor
        "heading_soft": "#456459",  # 次级标题 · .field>span 等
        # --- used only by the PIL information band (typeset.py:69/72/76) ---
        "band_fill": "#0c1018",    # rgba(12,16,24,200)
        "band_alpha": 200,
        "band_title": "#ffffff",
        "band_footer": "#bec8d4",  # rgb(190,200,212)
        # --- preview only, never rendered onto a produced image ---
        "preview_bg": "#ffffff",
        "preview_text": "#334942",
    },
    "fonts": {
        # CSS stack, mirrors style.css font-family.
        "css_stack": ["Inter", "Segoe UI", "Microsoft YaHei"],
        # PIL candidates, mirrors typeset._FONT_CANDIDATES in try order.
        "pil_candidates": ["msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc"],
        "weight_note": "PIL 只用 TTC 内单一字重，不做加粗模拟；需要强调时另选字重文件。",
    },
    "metrics": {
        # Every number here is arithmetic copied from typeset_image() so that
        # probe_cover_geometry() cannot drift away from the renderer.
        "title_divisor": 24,       # title font size = max(18, width // 24)
        "footer_divisor": 48,      # small font size = max(12, width // 48)
        "band_padding": 24,        # horizontal inset, typeset.py:65/72
        "band_top_gap": 12,        # band_top + 12, typeset.py:70
        "band_bottom_gap": 12,     # trailing 12 in the height formula, typeset.py:67
        "line_gap": 8,             # line_height = title_font.size + 8
        "footer_line_gap": 2,      # y + 2 before the footer, typeset.py:76
        "footer_line_extra": 10,   # small_font.size + 10, typeset.py:67
        "band_max_ratio": 0.5,     # min(band_height, height // 2), typeset.py:68
        "band_radius": 0,          # currently square corners
        "min_canvas": 64,          # images below this are rejected, typeset.py:59
    },
}

#: The six CSS-sourced values that must stay identical to style.css.
PINNED_CSS_HEX = ("#287b67", "#1a5e4e", "#e5e9e1", "#83908a", "#fffefa", "#f7f7f2")

#: Token key <-> CSS variable name. test_brand.py asserts both directions.
CSS_TOKEN_MAP = {
    "accent": "--accent",
    "accent_dark": "--accent-dark",
    "line": "--line",
    "muted": "--muted",
    "surface": "--surface",
    "canvas": "background",
}

DEFAULT_BRAND_ID = "brand-default"


def tokens() -> dict:
    return _deep_copy(DEFAULT_PROFILE["tokens"])


def fonts() -> dict:
    return _deep_copy(DEFAULT_PROFILE["fonts"])


def metrics() -> dict:
    return _deep_copy(DEFAULT_PROFILE["metrics"])


def _deep_copy(value):
    """Callers get an independent object. css_stack and pil_candidates are lists,
    and a shallow dict() copy would still share them with the module constant."""
    return json.loads(json.dumps(value))


def as_json(profile: dict | None = None) -> str:
    return json.dumps(profile or DEFAULT_PROFILE, ensure_ascii=False, sort_keys=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_default(connection=None) -> str:
    """Insert the default brand profile if absent. Returns its id.

    Idempotent. Safe to call on every start; the unique partial index on
    is_default makes a second default impossible even under a race.
    """
    from . import db
    context = db.transaction() if connection is None else None
    if context is None:
        return _ensure_with(connection)
    with context as owned:
        return _ensure_with(owned)


def _ensure_with(connection) -> str:
    stamp = now()
    connection.execute(
        "INSERT INTO brand_profiles(id,name,is_default,tokens,fonts,provenance,created_at,updated_at)"
        " VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (
            DEFAULT_BRAND_ID,
            "默认品牌视觉基线",
            json.dumps(tokens(), ensure_ascii=False, sort_keys=True),
            json.dumps(fonts(), ensure_ascii=False, sort_keys=True),
            "取自 frontend/src/style.css :root，图片信息带参数取自 backend/app/typeset.py",
            stamp,
            stamp,
        ),
    )
    row = connection.execute(
        "SELECT id FROM brand_profiles WHERE is_default = 1"
    ).fetchone()
    return row[0] if row else DEFAULT_BRAND_ID