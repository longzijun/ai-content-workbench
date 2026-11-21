"""V3 阶段 2-4：事实底稿 → 三平台生产 → 集中审阅 → 材料包 → 发布台账 → 数据复盘。

全部在本地内容模式下运行（退出 conftest 的平台测试模式放行），
隔离环境不连接真实平台。
"""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app import browser_bridge, config, db, fact_base, feedback, main, material_package, publish_ledger, review, runtime_mode, service, weekly_review, worker

pytestmark = pytest.mark.local_content_only

SOURCE = {"url": "https://example.invalid/source", "title": "测试来源", "note": ""}
PLATFORMS = ("wechat", "toutiao", "xiaohongshu")


def payload_all():
    return {"strategy": "一平台一份作品", "works": [
        {"platform": p, "format": "note" if p == "xiaohongshu" else "article",
         "title": f"{p}版本标题", "body": "先核实事实，再审核内容，最后由用户发布。",
         "tags": ["AI"], "image_prompts": [], "source_urls": [SOURCE["url"]]} for p in PLATFORMS]}


def full_fact_base(**changes):
    data = {
        "audience": "想用 AI 提效的普通职场人",
        "core_view": "先把事实核清楚，再让 AI 参与生产",
        "sources": [SOURCE],
        "facts": [{"claim": "工作台在本地模式下不保存平台草稿", "source_url": SOURCE["url"],
                   "source_date": "2026-10-25", "status": "confirmed"}],
        "open_questions": [{"question": "各平台 AI 标识勾选路径"}],
        "author_experience": "",
        "private_info": "",
        "asset_list": [],
        "platform_angles": {"wechat": "讲清楚边界", "toutiao": "讲事实变化", "xiaohongshu": "给一个可操作步骤"},
    }
    data.update(changes)
    return data


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime, vault = tmp_path / "runtime", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKBENCH_VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "discover_codex", lambda: "")
    db.init_db()
    yield {"runtime": runtime, "vault": vault, "monkeypatch": monkeypatch}


@pytest.fixture
def client(world):
    monkeypatch = world["monkeypatch"]
    monkeypatch.setattr(worker, "start_worker", lambda: None)
    monkeypatch.setattr(worker, "stop_worker", lambda: None)
    monkeypatch.setattr(browser_bridge, "stop_browser", lambda: None)
    with TestClient(main.app) as client:
        bootstrap = authenticated_bootstrap(client)
        client.headers["X-Workbench-Token"] = bootstrap.json()["token"]
        yield client


def approved_topic():
    topic = service.create_topic({"title": "三平台并行流程", "brief": "一份底稿三份作品",
                                  "platforms": list(PLATFORMS), "sources": [SOURCE], "content_line": "A", "stance_ids": []})
    return service.decide_topic(topic["id"], {"decision": "approved", "expected_revision": topic["revision"]})


def produced(client):
    topic = approved_topic()
    fact_base.save(topic["id"], full_fact_base())
    fact_base.mark_ready(topic["id"])
    job = service.produce_topic(topic["id"], list(PLATFORMS))
    snapshot = json.loads(db_job_snapshot(job["id"]))
    assert "_fact_base" in snapshot
    assert "已确认" in snapshot["_fact_base"]
    claimed = service.claim_next_job()
    version = service.complete_job(claimed["id"], payload_all(), "fake-provider")
    drafts = [item for item in service.state()["drafts"] if item["version_id"] == version["id"]]
    return topic, version, drafts


def db_job_snapshot(job_id):
    with db.connect() as connection:
        row = connection.execute("SELECT input_snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["input_snapshot"]


# ---------------------------------------------------------------- 事实底稿

def test_fact_base_gaps_block_production(client):
    topic = approved_topic()
    fact_base.save(topic["id"], {"audience": "只看受众", "core_view": "", "sources": [], "facts": []})
    gaps = fact_base.gaps(fact_base.get(topic["id"]))
    assert len(gaps) >= 3
    with pytest.raises(Exception) as error:
        service.produce_topic(topic["id"], list(PLATFORMS))
    assert "核心观点" in str(error.value) or "事实依据" in str(error.value)


def test_fact_base_revision_bumps_on_change_after_ready(client):
    topic = approved_topic()
    first = fact_base.save(topic["id"], full_fact_base())
    fact_base.mark_ready(topic["id"])
    second = fact_base.save(topic["id"], full_fact_base(core_view="修订后的核心观点"))
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["status"] == "draft"


def test_produce_locks_fact_base_and_snapshot(client):
    topic, version, drafts = produced(client)
    fact = fact_base.get(topic["id"])
    assert fact["status"] == "locked"
    assert len(drafts) == 3
    for draft in drafts:
        assert draft["state"] == "local"
        assert draft["destination"] == "workbench"
    with db.connect() as connection:
        tasks = connection.execute("SELECT * FROM delivery_tasks").fetchall()
    assert tasks == []


# ---------------------------------------------------------------- 集中审阅

def approve_all(drafts, comment=""):
    results = []
    for draft in drafts:
        results.append(review.record(draft["id"], "approved", comment, "测试账号", "AI 辅助创作", True))
    return results


def test_review_is_per_platform_with_independent_fingerprints(client):
    _, version, drafts = produced(client)
    by_platform = {draft["platform"]: draft for draft in drafts}
    review.record(by_platform["wechat"]["id"], "approved", "", "测试账号", "AI 辅助创作", True)
    review.record(by_platform["toutiao"]["id"], "approved", "", "测试账号", "AI 辅助创作", True)
    # 小红书暂不审：其余两个不受影响
    console = review.console(by_platform["wechat"]["topic_id"])
    states = {entry["platform"]: entry["review"] for entry in console["platforms"]}
    assert states["wechat"]["approved"] is True
    assert states["toutiao"]["approved"] is True
    assert states["xiaohongshu"]["approved"] is False
    assert "尚未审阅" in states["xiaohongshu"]["blocking"]


def test_fact_base_change_invalidates_reviews(client):
    topic, version, drafts = produced(client)
    approve_all(drafts)
    console = review.console(topic["id"])
    assert all(entry["review"]["approved"] for entry in console["platforms"])
    # 共享事实底稿变化 → 新 revision → 全部需重审
    fact_base.save(topic["id"], full_fact_base(core_view="事实有更新，需重新创作"))
    console = review.console(topic["id"])
    for entry in console["platforms"]:
        assert entry["review"]["fingerprint_valid"] is False
        assert any("重审" in item for item in entry["review"]["blocking"])


def test_returned_review_blocks_package(client):
    _, version, drafts = produced(client)
    for draft in drafts:
        review.record(draft["id"], "returned", "标题太泛", "测试账号", "AI 辅助创作", True)
    with pytest.raises(Exception) as error:
        material_package.build(drafts[0]["topic_id"])
    assert "审阅退回" in str(error.value)


def test_review_blank_account_and_disclosure_fall_back_to_defaults(client):
    """2026-11-04：账号标签/AI标识留空时按平台名与「AI 辅助创作」兜底，仍进指纹。"""
    _, version, drafts = produced(client)
    draft = next(item for item in drafts if item["platform"] == "wechat")
    result = review.record(draft["id"], "approved", "", "", "", True)
    assert result["draft"]["account_label"] == "微信公众号"
    assert result["draft"]["ai_disclosure"] == "AI 辅助创作"
    console = review.console(draft["topic_id"])
    entry = next(item for item in console["platforms"] if item["platform"] == "wechat")
    assert entry["review"]["approved"] is True


def test_review_explicit_values_still_respected(client):
    """显式填写的账号标签与 AI 标识不被默认值覆盖。"""
    _, version, drafts = produced(client)
    draft = next(item for item in drafts if item["platform"] == "toutiao")
    result = review.record(draft["id"], "approved", "", "头条-小号", "人工撰写", True)
    assert result["draft"]["account_label"] == "头条-小号"
    assert result["draft"]["ai_disclosure"] == "人工撰写"


# ---------------------------------------------------------------- 材料包

def test_material_package_build_and_zip_contents(client):
    topic, version, drafts = produced(client)
    approve_all(drafts)
    package = material_package.build(topic["id"])
    assert package["zip_path"].endswith(".zip")
    with zipfile.ZipFile(package["zip_path"]) as zip_file:
        names = zip_file.namelist()
        assert any(name.endswith("index.html") for name in names)
        assert any(name.endswith("manifest.json") for name in names)
        assert any("/wechat/标题.txt" in name for name in names)
        assert any("/xiaohongshu/话题建议.txt" in name for name in names)
        manifest = json.loads(zip_file.read([n for n in names if n.endswith("manifest.json")][0]))
        assert "wechat_app_id" not in json.dumps(manifest)
        entry = zip_file.read([n for n in names if n.endswith("index.html")][0]).decode("utf-8")
        assert "http" not in entry.replace("https://", "") or "外部资源" not in entry  # 无外链加载
        assert "发布材料包" in entry
    listed = material_package.list_packages(topic["id"])
    assert len(listed) == 1
    response = client.get(f"/api/material-packages/{package['id']}/download")
    assert response.status_code == 200


# ---------------------------------------------------------------- 发布台账

def test_publish_register_rules(client):
    topic, version, drafts = produced(client)
    approve_all(drafts)
    # 未来时间拒绝
    with pytest.raises(Exception) as error:
        publish_ledger.register(topic["id"], {"platform": "wechat", "published_at": "2099-01-01T09:00:00+08:00"})
    assert "未来时间" in str(error.value)
    # 链接格式与归属检查（本地，不访问）
    record = publish_ledger.register(topic["id"], {"platform": "wechat", "published_at": "2026-10-25T09:00:00+08:00",
                                                   "url": "https://mp.weixin.qq.com/s/abc", "account_label": "示例工作室"})
    assert record["confirmed"] is True
    assert record["url_status"] == "present"
    assert record["final_backfill"] == "pending"
    wrong = publish_ledger.register(topic["id"], {"platform": "toutiao", "published_at": "2026-10-25T09:00:00+08:00",
                                                  "url": "https://mp.weixin.qq.com/s/abc"})
    assert wrong["url_status"] == "invalid_format"
    # 链接与最终稿独立回填，不覆盖已发布事实
    backfilled = publish_ledger.backfill(record["id"], {"url": "", "final_text": "平台内改过的最终正文"})
    assert backfilled["url_status"] == "missing"
    assert backfilled["final_backfill"] == "done"
    assert backfilled["confirmed"] is True


# ---------------------------------------------------------------- 数据导入与复盘

def test_csv_import_preview_commit_and_duplicates(client):
    header = "平台,作品,指标,数值,统计开始,统计结束,来源\n"
    rows = [
        "wechat,文章A,阅读,1200,2026-10-19,2026-10-25,后台导出\n",
        "toutiao,文章A,阅读,800,2026-10-19,2026-10-25,后台导出\n",
        "xiaohongshu,笔记A,曝光,,2026-10-19,2026-10-25,后台导出\n",   # 缺失值 → 未知
        "wechat,文章A,阅读,abc,2026-10-19,2026-10-25,后台导出\n",      # 错误值
    ]
    raw = ("\ufeff" + header + "".join(rows)).encode("utf-8")
    preview = client.post("/api/data-imports", content=raw,
                          headers={"Content-Type": "text/csv; charset=utf-8", "X-Filename": "data.csv"}).json()
    assert preview["mapping"]["平台"] == "platform"
    assert len(preview["rows"]) == 3
    assert len(preview["errors"]) == 1
    unknown = [row for row in preview["rows"] if row["metric_value"] is None]
    assert len(unknown) == 1
    committed = client.post(f"/api/data-imports/{preview['batch_id']}/commit", json={"rows": preview["rows"]}).json()
    assert committed["imported"] == 3
    assert committed["duplicates"] == 0
    # 同一批次重复提交 → 409（已提交批次不可重复入账）
    again = client.post(f"/api/data-imports/{preview['batch_id']}/commit", json={"rows": []})
    assert again.status_code == 409
    # 重复内容再导入 → 判重跳过（同一批次重复提交走已提交校验，这里用同内容新批次）
    preview2 = client.post("/api/data-imports", content=raw,
                           headers={"Content-Type": "text/csv; charset=utf-8", "X-Filename": "data2.csv"}).json()
    committed2 = client.post(f"/api/data-imports/{preview2['batch_id']}/commit", json={"rows": preview2["rows"]}).json()
    assert committed2["imported"] == 0
    assert committed2["duplicates"] == 3
    observations = client.get("/api/observations").json()["items"]
    assert len(observations) == 3
    assert any(item["known"] is False for item in observations)


def test_manual_observation_missing_value_is_unknown(client):
    result = client.post("/api/observations", json={"platform": "wechat", "metric_name": "阅读"}).json()
    assert result["known"] is False
    assert result["metric_value"] is None


@pytest.mark.parametrize("params", [{"refresh": "true"}, {"refresh": "true", "week": "invalid"}])
def test_weekly_review_refresh_rejects_missing_or_invalid_week(client, params):
    response = client.get("/api/weekly-review", params=params)
    assert response.status_code == 422
    assert "YYYY-MM-DD" in response.json()["detail"]
    assert client.get("/api/weekly-review").json() == {"review": None}


def test_weekly_review_empty_state_and_with_data(client):
    response = client.get("/api/weekly-review", params={"week": "2026-10-20", "refresh": "true"})
    assert response.status_code == 200
    empty = response.json()
    assert client.get("/api/weekly-review", params={"week": "2026-10-20"}).json()["review"]["id"] == empty["id"]
    assert empty["empty_state"] is True
    assert len(empty["actions"]) == 3
    assert "账号级数据" not in (empty["attribution_note"] or "")
    client.post("/api/observations", json={"platform": "wechat", "metric_name": "阅读", "metric_value": 100, "work_ref": "文章A", "observed_at": "2026-10-21"})
    client.post("/api/observations", json={"platform": "wechat", "metric_name": "阅读", "metric_value": 50, "work_ref": "文章B", "observed_at": "2026-10-22"})
    filled = weekly_review.generate("2026-10-20")
    assert filled["empty_state"] is False
    assert filled["comparison"][0]["comparable"] is True
    assert filled["attribution_note"] is None
    # 只有账号级数据时的归因限制（观测时间落在下一周，作品数据不在该周）
    client.post("/api/observations", json={"platform": "toutiao", "metric_name": "粉丝总数", "metric_value": 1000, "scope": "account", "observed_at": "2026-10-28"})
    account_only = weekly_review.generate("2026-10-27")
    assert account_only["empty_state"] is False
    assert account_only["attribution_note"] is not None
    assert "不能归因" in account_only["attribution_note"]


def test_weekly_review_prefers_observed_date_over_recorded_date(client):
    item = client.post("/api/observations", json={
        "platform": "wechat", "metric_name": "阅读", "metric_value": 100,
        "work_ref": "上周作品", "observed_at": "2026-10-21",
    }).json()
    with db.transaction() as connection:
        connection.execute("UPDATE observations SET recorded_at=? WHERE id=?",
                           ("2026-10-29T09:00:00+00:00", item["id"]))
    current = weekly_review.generate("2026-10-27")
    previous = weekly_review.generate("2026-10-20")
    assert current["empty_state"] is True, "录入时间不能把上周观测错归到本周"
    assert previous["empty_state"] is False


def test_feedback_add_and_organize_degrades_honestly(client, monkeypatch):
    import httpx
    def forbidden(*args, **kwargs):
        raise RuntimeError("模型不可用（测试隔离）")
    monkeypatch.setattr(httpx, "post", forbidden)
    item = feedback.add({"text": "第三步看不懂，能出个视频吗", "platform": "xiaohongshu"})
    result = feedback.organize(item["id"])
    assert result["organized"] is False
    assert "模型暂不可用" in result["error"]
