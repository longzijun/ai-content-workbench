"""排版审计接进真实流程：模板解析、落库、以及审核时的自动触发。

这条链最容易的失败方式是「静默不执行」：模块写好了但没人调用，于是每份真实
稿件都没有被检查过，而页面上一切正常。所以这里的断言都落在「有没有真的写进
layout_audits」上，而不是「函数能不能返回」。
"""
import json
from datetime import datetime, timezone

import pytest

from backend.app import adapters, brand, config, db, layout_audit, layout_flow, layout_templates

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    monkeypatch.setattr(adapters, "_loaded_local_model_info", lambda _: {})
    db.init_db()
    return config.get_settings()


def topic(topic_id="t1"):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO topics(id,title,brief,platforms,sources,revision,status,created_at,updated_at)"
            " VALUES(?,?,?,'[\"wechat\",\"toutiao\"]','[]',1,'approved',?,?)",
            (topic_id, "标题", "说明", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    return topic_id


def version(topic_id="t1", version_id="v1", works=None):
    payload = {"strategy": "s", "works": works if works is not None else [work()]}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO versions(id,topic_id,number,input_revision,payload,hash,provider,created_at)"
            " VALUES(?,?,1,1,?,?,'manual',?)",
            (version_id, topic_id, json.dumps(payload, ensure_ascii=False), "h", "2026-01-01T00:00:00+00:00"))
        # _current() 要求 current_version_id 指向该版本，否则 review_version 直接 409。
        connection.execute("UPDATE topics SET current_version_id=? WHERE id=?", (version_id, topic_id))
    return version_id


def work(platform="wechat", title="一个足够长的标题用来测窗口", body="正文" * 120, **extra):
    item = {"platform": platform, "format": "article", "title": title, "body": body,
            "tags": [], "image_prompts": [], "source_urls": []}
    item.update(extra)
    return item


# --------------------------------------------------------------------------
# 编排：真的写进 layout_audits
# --------------------------------------------------------------------------

def test_run_writes_audit_rows_for_every_work(world):
    topic()
    version(works=[work("wechat"), work("toutiao")])
    report = layout_flow.run_for_version("v1")
    assert len(report["results"]) == 2
    assert report["errors"] == []
    stored = layout_audit.history("v1")
    assert len(stored) == 2, "审计结果必须落库，否则等于没跑"
    assert {row["platform"] for row in stored} == {"wechat", "toutiao"}


def test_run_reports_that_images_were_not_checked(world):
    topic()
    version()
    report = layout_flow.run_for_version("v1")
    assert report["images_checked"] is False
    assert report["image_platforms"] == []
    assert "跳过" in report["note"], "没查图片就要说清楚，不能让人以为查过"


def test_images_are_really_checked_when_the_version_has_them(world, tmp_path):
    """有图时 images_checked 必须为 True，且 image_blur 真的按宽度判定。

    造一张 400×300 的封面：模板下限是 800 宽，所以它必须被判 blocked。
    这条测试的意义是证明「读到了图」，不是只看返回布尔值。
    """
    from PIL import Image
    from pathlib import Path

    topic()
    version(works=[work("wechat")])
    settings = config.get_settings()
    directory = Path(settings.runtime_dir) / "assets" / "t1" / "v1" / "wechat"
    directory.mkdir(parents=True, exist_ok=True)
    cover = directory / "cover.png"
    Image.new("RGB", (400, 300), "white").save(cover)
    (directory / "manifest.json").write_text(
        json.dumps({"assets": [{"role": "cover", "path": str(cover)}]}), "utf-8")

    report = layout_flow.run_for_version("v1")
    assert report["images_checked"] is True
    assert report["image_platforms"] == ["wechat"]
    blur = [finding for result in report["results"]
            for finding in result["findings"] if finding["code"] == "image_blur"]
    assert blur, "有图时必须产出 image_blur 结论"
    assert blur[0]["severity"] == "blocked", "400 宽低于模板 800 下限，应判 blocked"
    assert blur[0]["evidence"]["basis"] == "尺寸下限，非图像质量分析"


def test_asset_outside_the_version_directory_is_ignored(world, tmp_path):
    """manifest 里的绝对路径可以被改写；目录外的文件一律不读。"""
    from pathlib import Path

    topic()
    version(works=[work("wechat")])
    settings = config.get_settings()
    directory = Path(settings.runtime_dir) / "assets" / "t1" / "v1" / "wechat"
    directory.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not a real image")
    (directory / "manifest.json").write_text(
        json.dumps({"assets": [{"role": "cover", "path": str(outside)}]}), "utf-8")

    report = layout_flow.run_for_version("v1")
    assert report["images_checked"] is False, "目录外的路径不得被读入"


def test_missing_template_is_reported_not_crashed(world):
    topic()
    version(works=[work("wechat"), work("weibo")])
    report = layout_flow.run_for_version("v1")
    assert len(report["results"]) == 1
    assert len(report["errors"]) == 1
    assert "模板" in report["errors"][0]["message"]


def test_rerun_overwrites_instead_of_duplicating(world):
    topic()
    version()
    layout_flow.run_for_version("v1")
    layout_flow.run_for_version("v1")
    assert len(layout_audit.history("v1")) == 1, "同一版本同一模板只保留一条"


def test_unknown_version_raises(world):
    with pytest.raises(ValueError, match="版本不存在"):
        layout_flow.run_for_version("nope")


def test_results_for_version_returns_worst_status(world):
    topic()
    version(works=[work("wechat", body="短正文")])
    layout_flow.run_for_version("v1")
    payload = layout_flow.results_for_version("v1")
    assert payload["worst"] in ("passed", "warning", "blocked")
    assert payload["results"]


# --------------------------------------------------------------------------
# 模板解析：content_form 决定模板
# --------------------------------------------------------------------------

def test_template_resolution_falls_back_to_the_generic_one(world):
    brand_id = brand.ensure_default()
    generic = layout_templates.resolve(brand_id, "wechat", "article", "")
    fallback = layout_templates.resolve(brand_id, "wechat", "article", "tutorial")
    assert fallback["id"] == generic["id"], "未种子的 content_form 回落到通用模板"


def test_seeded_template_exists_for_each_platform(world):
    brand_id = brand.ensure_default()
    for platform, format_id in (("wechat", "article"), ("toutiao", "article"), ("xiaohongshu", "note")):
        template = layout_templates.resolve(brand_id, platform, format_id, "")
        assert template["platform"] == platform


# --------------------------------------------------------------------------
# 审核即触发：这是「模板与审计进入真实流程」的最小证据
# --------------------------------------------------------------------------

def test_review_triggers_the_layout_audit(world):
    from backend.app import service
    topic()
    version(works=[work("wechat")])
    service.review_version("v1")
    assert layout_audit.history("v1"), "审核必须真的跑一遍排版审计并落库"


def test_audit_failure_does_not_break_the_review(world, monkeypatch):
    """审核是红线流程；审计只是辅助信息，坏了也不能把审核拖垮。"""
    from backend.app import service

    def boom(_version_id):
        raise RuntimeError("模拟审计崩溃")

    monkeypatch.setattr(layout_flow, "run_for_version", boom)
    topic()
    version(works=[work("wechat")])
    review = service.review_version("v1")
    assert review["status"] in ("passed", "blocked"), "审核结果仍要正常返回"
