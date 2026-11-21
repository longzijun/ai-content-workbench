"""Platform-source fixtures emulate observed public shapes, not live acceptance."""
from datetime import datetime, timezone
import json

import httpx
import pytest

from backend.app import trend_sources as sources


NOW = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(sources, "_utcnow", lambda: NOW)


def row(identifier="7695197376592298531", **changes):
    return {"Title": "平台公开事件线索", "Url": f"https://www.toutiao.com/trending/{identifier}/?event_type=hot_board&log_pb=analytics&topic_id={identifier}", "HotValue": "123456", "LabelDesc": "新事件上榜", **changes}


def board(rows=None, pinned=None, **changes):
    return {"status": "success", "data": [row()] if rows is None else rows, "fixed_top_data": [] if pinned is None else pinned, **changes}


def patch_content(monkeypatch, data):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    monkeypatch.setattr(sources, "_fetch", lambda source: (text.encode(), {"content-type": "application/json; charset=utf-8"}))


def test_catalog_has_true_native_toutiao_and_external_baidu_not_wechat_or_xhs():
    catalog = {item["id"]: item for item in sources.source_catalog()}
    assert catalog["toutiao_hot_board"]["platform"] == "toutiao"
    assert catalog["baidu_realtime"]["platform"] == "external"
    assert all(item["platform"] == "external" for key, item in catalog.items() if key != "toutiao_hot_board")
    assert not any(item["platform"] in {"wechat", "xhs"} for item in catalog.values())


def test_unavailable_platforms_are_explicit_capabilities_not_polling_sources():
    capabilities = {item["platform"]: item for item in sources.platform_source_capabilities()}
    assert capabilities["wechat"]["available"] is False and capabilities["xhs"]["available"] is False
    assert capabilities["wechat"]["source_ids"] == capabilities["xhs"]["source_ids"] == []
    assert capabilities["wechat"]["status"] == capabilities["xhs"]["status"] == "unavailable"
    assert capabilities["toutiao"]["source_ids"] == ["toutiao_hot_board"]
    assert "未核实名次" in " ".join(capabilities["toutiao"]["uncertainties"]) or "没有显式名次" in " ".join(capabilities["toutiao"]["uncertainties"])
    assert "百度" in capabilities["wechat"]["review_fallback"]
    capabilities["wechat"]["source_ids"].append("fake")
    assert sources.platform_source_capabilities()[0]["source_ids"] == []


def test_toutiao_response_keeps_real_heat_no_array_rank_no_fabricated_date(monkeypatch):
    patch_content(monkeypatch, board([row(), row("7695197376592298532", HotValue=None, LabelDesc=None)]))
    result = sources.collect_source("toutiao_hot_board")
    assert result["status"] == "ok" and result["platform"] == "toutiao"
    assert result["fetched_at"] == "2026-10-12T13:00:00Z"
    first, second = result["items"]
    assert first["heat_value"] == 123456 and "单位未披露" in first["heat_label"]
    assert second["heat_value"] is None and second["heat_label"] is None
    assert all(item["rank"] is None and item["rank_verified"] is False and item["published_at"] is None for item in result["items"])
    assert first["summary"] == "平台标签：新事件上榜"
    assert first["url"] == "https://www.toutiao.com/trending/7695197376592298531/"
    assert first["platform"] == "toutiao" and first["is_hotspot_index"] is True
    assert "log_pb" not in first["url"] and "topic_id" not in first["url"]
    json.dumps(result, ensure_ascii=False)


def test_new_unverified_rank_field_does_not_become_confirmed_real_rank(monkeypatch):
    patch_content(monkeypatch, board([row(Rank=1, rank=1, index=0)]))
    assert sources.collect_source("toutiao_hot_board")["items"][0]["rank"] is None


def test_pinned_is_not_ranked_and_its_arbitrary_heat_is_not_ordinary_hotvalue(monkeypatch):
    fixed = row("7695197376592298533", Url="https://www.toutiao.com/article/7695197376592298533/", HotValue=999999999)
    patch_content(monkeypatch, board(pinned=[fixed]))
    pinned = sources.collect_source("toutiao_hot_board")["items"][-1]
    assert pinned["is_pinned"] is True and pinned["rank"] is None and pinned["heat_value"] is None and pinned["heat_label"] is None


@pytest.mark.parametrize("data", [
    "<html>请登录或验证</html>", {"status": "verify", "data": [row()]},
    {"status": "login", "data": [row()]}, {"status": "success", "data": {}},
    board([], []), board(fixed_top_data={}), [],
])
def test_login_captcha_invalid_or_empty_response_never_fabricates_native_candidates(monkeypatch, data):
    patch_content(monkeypatch, data)
    result = sources.collect_source("toutiao_hot_board")
    assert result["status"] == "error" and result["items"] == [] and result["error"]


@pytest.mark.parametrize("url", [
    "https://www.toutiao.com.evil.invalid/trending/123/", "http://127.0.0.1/trending/123/",
    "https://user:password@www.toutiao.com/trending/123/", "https://www.toutiao.com/private-api/123/",
    "https://www.toutiao.com/trending/000123/", "https://www.toutiao.com/trending/0/",
    "javascript:alert(1)", "file:///C:/secret", "https://mp.toutiao.com/draft/123",
])
def test_toutiao_candidates_are_only_known_public_native_article_or_trending_paths(monkeypatch, url):
    patch_content(monkeypatch, board([row(Url=url)]))
    result = sources.collect_source("toutiao_hot_board")
    assert result["status"] == "error" and result["items"] == []


@pytest.mark.parametrize("value", [True, -1, 1.5, "9999999999999999999", "invalid", None])
def test_invalid_heat_is_unknown_not_synthetic_heat(monkeypatch, value):
    patch_content(monkeypatch, board([row(HotValue=value)]))
    item = sources.collect_source("toutiao_hot_board")["items"][0]
    assert item["heat_value"] is None and item["heat_label"] is None


def test_toutiao_bounds_and_deduplicates_native_ids_and_does_not_emit_image_or_schema(monkeypatch):
    rows = [row(str(7695197376592298500 + i), Image={"url": "https://private.example/image"}, Schema="snssdk://private", Title="<b>公开线索</b>" + "字" * 500) for i in range(80)]
    patch_content(monkeypatch, board([rows[0], rows[0], *rows[1:]]))
    items = sources.collect_source("toutiao_hot_board")["items"]
    assert len(items) == sources.MAX_ITEMS and len({item["url"] for item in items}) == sources.MAX_ITEMS
    assert all(len(item["title"]) == 300 for item in items)
    assert "private.example" not in json.dumps(items) and "snssdk" not in json.dumps(items)


@pytest.mark.parametrize("media,status,redirect", [
    ("text/html", 200, None), ("application/json", 302, "https://www.toutiao.com/login/"),
    ("application/json", 302, "https://www.toutiao.com/private-api/"), ("application/json", 403, None),
])
def test_toutiao_network_requires_anonymous_fixed_public_json_not_html_or_redirect(monkeypatch, media, status, redirect):
    original = httpx.Client
    requests = []
    def handler(request):
        requests.append(request)
        headers = {"content-type": media}
        if redirect:
            headers["location"] = redirect
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(json.dumps(board()).encode()))
    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = sources.collect_source("toutiao_hot_board")
    assert result["status"] == "error" and result["items"] == []
    assert len(requests) == 1 and "cookie" not in requests[0].headers and "authorization" not in requests[0].headers


def test_actual_fixed_public_json_mime_works_with_bounded_anonymous_fetch(monkeypatch):
    original = httpx.Client
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"}, stream=httpx.ByteStream(json.dumps(board()).encode()))
    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    assert sources.collect_source("toutiao_hot_board")["status"] == "ok"
    assert len(requests) == 1 and str(requests[0].url) == sources._BY_ID["toutiao_hot_board"].url
    assert requests[0].headers["accept-encoding"] == "identity"
    assert all(0 < value <= sources.TIMEOUT_SECONDS for value in requests[0].extensions["timeout"].values())
