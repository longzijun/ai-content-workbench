"""Layout audit: severity is justified, evidence is honest, nothing is silently fixed."""
import json
from io import BytesIO

import pytest
from PIL import Image

from backend.app import brand, db, layout_audit, layout_templates, richtext


def png(width, height):
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    db.init_db()
    from backend.app import config
    return config.get_settings()


def template(platform="xiaohongshu", content_form=""):
    from backend.app.platform_rules import expected_format
    return layout_templates.resolve(brand.DEFAULT_BRAND_ID, platform,
                                    expected_format(platform), content_form)


@pytest.fixture
def version_id(world):
    """造一个真实版本。layout_audits 有指向 versions 的外键，审计记录
    必须挂在一个真实版本上 —— 这也让「审计绑定具体版本」成为结构性保证。"""
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at)"
            " VALUES('t-audit','审计用选题','','[\"xiaohongshu\"]','[]',1,'approved',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
        connection.execute(
            "INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at)"
            " VALUES('v-audit','t-audit',1,1,'{}','h','local:test','2026-01-01T00:00:00+00:00')")
    return "v-audit"


def work(title="短标题", body="要点一\n要点二\n要点三\n要点四\n要点五\n要点六"):
    return {"title": title, "body": body}


def codes(result, severity=None):
    return {item["code"] for item in result["findings"]
            if severity is None or item["severity"] == severity}


# --------------------------------------------------------------------------
# 纯函数：不写库、不联网
# --------------------------------------------------------------------------

def test_audit_is_a_pure_function(world, monkeypatch):
    """audit() 除传入的图片字节外不得有 IO —— 不读文件、不写库、不联网。

    先把模板取好再设哨兵：template() 属于另一个模块，本来就该读库，
    本测试要验的是 audit() 本身。
    """
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("审计不得发起网络请求")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)

    resolved = template()

    from backend.app import db as database

    class Sentinel:
        def __call__(self, *args, **kwargs):
            raise AssertionError("audit() 不得开事务写库")

    monkeypatch.setattr(database, "transaction", Sentinel())
    result = layout_audit.audit(resolved, work(), cover_bytes=png(1200, 900))
    assert result["status"] in {"passed", "warning", "blocked"}


def test_only_record_performs_io(world, version_id):
    """落库由 record() 单独承担。"""
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    layout_audit.record(result, version_id)
    rows = layout_audit.history(version_id)
    assert len(rows) == 1
    assert rows[0]["status"] == result["status"]


def test_audit_result_is_json_serializable(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


# --------------------------------------------------------------------------
# 分级：每一档都有理由
# --------------------------------------------------------------------------

def test_all_good_input_passes_or_only_warns(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    assert result["status"] in {"passed", "warning"}
    assert "body_overflow" not in codes(result, "blocked")


def test_title_overflow_is_a_warning_not_a_block(world):
    """改标题即可，不必阻断。"""
    result = layout_audit.audit(template(), work(title="标" * 120), cover_bytes=png(800, 383))
    assert "title_overflow" in codes(result, "warning")
    assert result["status"] != "blocked", "标题溢出只是提醒"


def test_body_overflow_blocks(world):
    """平台硬约束，超了就是不能用。"""
    result = layout_audit.audit(template(), work(body="字" * 1200), cover_bytes=png(1200, 900))
    assert "body_overflow" in codes(result, "blocked")
    assert result["status"] == "blocked"


def test_body_overflow_does_not_truncate(world):
    """截断会破坏 BrowserDraftLedger 的 content_hash 绑定，草稿回读会失效。"""
    body = "字" * 1200
    result = layout_audit.audit(template(), work(body=body), cover_bytes=png(1200, 900))
    assert "不会自动截断" in " ".join(item["message"] for item in result["findings"])


def test_image_blur_blocks_on_small_cover(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(320, 240))
    assert "image_blur" in codes(result, "blocked")
    assert result["status"] == "blocked"


def test_small_body_image_is_reported_separately(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900),
                                body_images=[png(1200, 900), png(200, 150)])
    assert "image_blur" in codes(result, "blocked")
    assert any("body[1]" in item["message"] for item in result["findings"])


def test_corrupt_image_is_blocked_not_crashed(world):
    result = layout_audit.audit(template(), work(), cover_bytes=b"not an image at all")
    assert "image_blur" in codes(result, "blocked")
    assert any("损坏" in item["message"] for item in result["findings"])


def test_missing_images_skip_instead_of_guessing(world):
    result = layout_audit.audit(template(), work())
    skipped = [item for item in result["findings"] if item.get("skipped")]
    assert any(item["code"] == "image_blur" for item in skipped)
    assert result["status"] != "blocked", "没有图片不等于图片不合格"


# --------------------------------------------------------------------------
# cover_occlusion：明确跳过，不猜
# --------------------------------------------------------------------------

def test_cover_occlusion_is_skipped_with_a_stated_reason(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    finding = next(item for item in result["findings"] if item["code"] == "cover_occlusion")
    assert finding["severity"] == "passed"
    assert finding["skipped"] is True
    assert finding["evidence"]["skipped_reason"] == "no_subject_annotation"
    assert "猜测" in finding["message"] or "跳过" in finding["message"]


def test_cover_occlusion_is_off_for_platforms_without_the_check(world):
    wechat = template("wechat")
    assert wechat["checks"]["cover_occlusion"] is False
    result = layout_audit.audit(wechat, work(), cover_bytes=png(1200, 900))
    assert "cover_occlusion" not in codes(result)


# --------------------------------------------------------------------------
# 测的是可见文本，不是 Markdown 原文
# --------------------------------------------------------------------------

def test_body_measurement_uses_visible_text(world):
    """加粗标记在画面上不存在，数它会系统性偏大。"""
    template_x = template()
    markup_heavy = ("**加粗标题**\n\n" * 40 + "正文")
    result = layout_audit.audit(template_x, work(body=markup_heavy),
                                cover_bytes=png(1200, 900))
    finding = next(item for item in result["findings"] if item["code"] == "body_overflow")
    if "visible_chars" in finding.get("evidence", {}):
        assert finding["evidence"]["visible_chars"] < len(markup_heavy)


def test_markup_only_body_does_not_trip_the_ceiling(world):
    """500 字的加粗 Markdown，可见文本远低于 1000 字上限，不该误报。"""
    body = "**加粗**" * 120
    result = layout_audit.audit(template(), work(body=body), cover_bytes=png(1200, 900))
    assert "body_overflow" not in codes(result, "blocked")


def test_item_count_counts_rendered_blocks_not_markup_prefixes(world):
    checklist = template(content_form="checklist")
    with_items = layout_audit.audit(checklist, work(body="一\n二\n三\n四\n五\n六"),
                                   cover_bytes=png(1200, 900))
    assert "item_count" not in codes(with_items, "warning")
    without = layout_audit.audit(checklist, work(body="一整段没有分项的文字。"),
                                 cover_bytes=png(1200, 900))
    assert "item_count" in codes(without, "warning")


def test_item_count_check_only_runs_for_checklist(world):
    general = template()
    assert general["checks"].get("item_count", False) is False
    result = layout_audit.audit(general, work(body="一整段。"), cover_bytes=png(1200, 900))
    assert "item_count" not in codes(result)


# --------------------------------------------------------------------------
# provenance：数值不是平台公布的
# --------------------------------------------------------------------------

def test_every_result_carries_the_provenance_statement(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    assert "非平台官方规格" in result["evidence"]["provenance"]
    assert "不代表平台公开要求" in result["evidence"]["provenance"]


def test_image_blur_evidence_says_it_is_not_quality_analysis(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(320, 240))
    finding = next(item for item in result["findings"]
                   if item["code"] == "image_blur" and item["severity"] == "blocked")
    assert "非图像质量分析" in finding["evidence"]["basis"]
    assert "非平台官方规格" in finding["evidence"]["provenance"]


def test_evidence_names_what_was_measured(world):
    result = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    assert result["evidence"]["measured_on"] == "editor_visible_text"
    assert result["evidence"]["skeleton"]


# --------------------------------------------------------------------------
# 不接入投递判定
# --------------------------------------------------------------------------

def test_audit_is_not_wired_into_sync_draft():
    """审计是只读评估，不参与草稿投递的状态计算。

    用 AST 检查而不是字符串匹配 —— docstring 里提到 sync_draft 是为了说明
    「本模块不碰它」，字符串匹配分不清说明与调用。
    """
    import ast

    tree = ast.parse(open(layout_audit.__file__, encoding="utf-8").read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    for forbidden in ("sync_draft", "sync_platform_draft", "enqueue_in", "record_draft"):
        assert forbidden not in called, f"审计不得调用投递链路（{forbidden}）"
    imported = {alias.name.split('.')[-1] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    for forbidden in ("browser_drafts", "delivery", "artifacts", "adapters"):
        assert forbidden not in imported, f"审计不得 import 投递链路（{forbidden}）"


def test_module_docstring_states_the_non_blocking_policy():
    doc = layout_audit.__doc__
    assert "不接入投递判定" in doc
    assert "不自动截断" in doc


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------

def test_record_upserts_on_re_audit(world, version_id):
    first = layout_audit.audit(template(), work(), cover_bytes=png(1200, 900))
    layout_audit.record(first, version_id)
    second = layout_audit.audit(template(), work(body="字" * 1200), cover_bytes=png(1200, 900))
    layout_audit.record(second, version_id)
    rows = layout_audit.history(version_id)
    assert len(rows) == 1, "重跑审计应覆盖而非堆积"
    assert rows[0]["status"] == "blocked"


def test_history_is_sorted_by_platform(world, version_id):
    layout_audit.record(layout_audit.audit(template("wechat"), work(), cover_bytes=png(1200, 900)), version_id)
    layout_audit.record(layout_audit.audit(template("toutiao"), work(), cover_bytes=png(1200, 900)), version_id)
    platforms = [row["platform"] for row in layout_audit.history(version_id)]
    assert platforms == sorted(platforms)


def test_history_returns_copies(world, version_id):
    layout_audit.record(layout_audit.audit(template(), work(), cover_bytes=png(1200, 900)), version_id)
    first = layout_audit.history(version_id)
    first[0]["findings"].append("篡改")
    assert "篡改" not in layout_audit.history(version_id)[0]["findings"]


# --------------------------------------------------------------------------
# 排序与去重
# --------------------------------------------------------------------------

def test_findings_are_sorted_by_severity_then_code(world):
    result = layout_audit.audit(template(), work(title="标" * 120, body="字" * 1200),
                                cover_bytes=png(320, 240))
    order = {"passed": 0, "warning": 1, "blocked": 2}
    keys = [(-order[item["severity"]], item["code"]) for item in result["findings"]]
    assert keys == sorted(keys)


def test_result_status_is_the_worst_finding(world):
    result = layout_audit.audit(template(), work(title="标" * 120, body="字" * 1200),
                                cover_bytes=png(1200, 900))
    severities = {item["severity"] for item in result["findings"]}
    assert result["status"] == ("blocked" if "blocked" in severities
                                else "warning" if "warning" in severities else "passed")


def test_every_finding_has_a_code_a_severity_and_a_message(world):
    result = layout_audit.audit(template(), work(title="标" * 120, body="字" * 1200),
                                cover_bytes=png(320, 240))
    for item in result["findings"]:
        assert item["code"] and item["severity"] in {"passed", "warning", "blocked"}
        assert item["message"].strip()


def test_audit_reports_the_template_it_used(world):
    result = layout_audit.audit(template(content_form="checklist"), work(),
                                cover_bytes=png(1200, 900))
    assert result["template_id"] == template(content_form="checklist")["id"]
    assert result["content_form"] == "checklist"