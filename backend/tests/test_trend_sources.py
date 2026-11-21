"""Public source boundary tests; all network responses are isolated simulations."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import json

import httpx
import pytest

from backend.app import trend_sources as sources


NOW = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(sources, "_utcnow", lambda: NOW)


def rss(items=None, date=None, summary="公开摘要", extra=""):
    if items is None:
        date = date or format_datetime(NOW - timedelta(hours=1))
        items = f'<item><title>科技新闻 &amp; 研究</title><link>https://www.bbc.co.uk/news/articles/document123?utm_source=rss&amp;id=456</link><description><![CDATA[{summary}]]></description><pubDate>{date}</pubDate>{extra}</item>'
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><lastBuildDate>{format_datetime(NOW)}</lastBuildDate>{items}</channel></rss>'


def patch_content(monkeypatch, text, content_type="text/xml; charset=utf-8", **headers):
    payload = text.encode("utf-8") if isinstance(text, str) else text
    monkeypatch.setattr(sources, "_fetch", lambda source: (payload, {"content-type": content_type, **headers}))


def baidu(rows):
    return '<html><meta charset="utf-8"><!--s-data:' + json.dumps({"data": {"cards": [{"component": "hotList", "content": rows}]}}, ensure_ascii=False) + '--></html>'


def row(index=0, **changes):
    return {"word": "真实热点候选", "rawUrl": "https://www.baidu.com/s?wd=%E7%83%AD%E7%82%B9&sa=fyb_news&rsv_dl=fyb_news", "desc": "公开页面描述", "hotScore": "123456", "index": index, **changes}


def transport(monkeypatch, handler):
    original = httpx.Client
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(record), **kwargs))
    return requests


def response(body=b"", status=200, **headers):
    return httpx.Response(status, headers={"content-type": "text/xml; charset=utf-8", **headers}, stream=httpx.ByteStream(body))


def test_catalog_is_fixed_and_returns_copies():
    catalog = sources.source_catalog()
    ids = {source["id"] for source in catalog}
    # 固定目录：历史来源一个都不能被改掉，且目录只能来自模块内的 _SOURCES 常量，
    # 运行时无法往里塞新的抓取地址。
    assert {"baidu_realtime", "bbc_world", "bbc_technology", "google_ai",
            "google_research", "openai_news", "toutiao_hot_board"} <= ids
    assert ids == {source.id for source in sources._SOURCES}
    assert all(set(source) == {"id", "name", "url", "kind", "category", "platform",
                               "tier", "min_interval_minutes", "directions", "max_items"}
               for source in catalog)
    assert all(source["kind"] in ("ranking", "rss", "json") for source in catalog)
    # 方向只能是四大类之一，且不许出现空方向（那等于没归类，界面会漏展示）。
    assert all(source["directions"] and set(source["directions"]) <= set(sources.DIRECTIONS)
               for source in catalog)
    assert all(0 < source["max_items"] <= sources.MAX_ITEMS for source in catalog)
    catalog[0]["url"] = "http://127.0.0.1/private"
    assert sources.source_catalog()[0]["url"] == "https://top.baidu.com/board?tab=realtime"


def test_output_per_source_respects_its_declared_cap(monkeypatch):
    """OpenAI 单次返回上千条，按源声明的上限收窄，避免单源淹没候选表。"""
    items = "".join(f'<item><title>Old {index}</title><link>https://openai.com/index/old-{index}</link><pubDate>{format_datetime(NOW - timedelta(days=20, minutes=index))}</pubDate></item>' for index in range(60))
    items += f'<item><title>Newest at feed end</title><link>https://openai.com/index/newest</link><pubDate>{format_datetime(NOW - timedelta(minutes=1))}</pubDate></item>'
    patch_content(monkeypatch, rss(items))
    cap = next(source.max_items for source in sources._SOURCES if source.id == "openai_news")
    result = sources.collect_source("openai_news")
    assert result["status"] == "ok" and len(result["items"]) == cap
    # 关键不变式：先按日期排好再截断，否则 feed 末尾的最新条目会被旧条目挤掉。
    assert result["items"][0]["title"] == "Newest at feed end"
    assert result["items"][0]["published_at"] == "2026-10-12T12:59:00Z"
    dates = [item["published_at"] for item in result["items"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.parametrize("source_id", ["https://example.com/feed", "file:///secret", "not-configured", [], None])
def test_unknown_source_never_fetches(monkeypatch, source_id):
    monkeypatch.setattr(sources, "_fetch", lambda *_: pytest.fail("unknown source must not fetch"))
    with pytest.raises(ValueError, match="固定公开来源"):
        sources.collect_source(source_id)


def test_baidu_real_zero_based_indexes_match_visible_ranks_and_pinned_is_unranked(monkeypatch):
    patch_content(monkeypatch, baidu([row(isTop=True), row(0), row(8, hotScore=None), row(None, hotScore="invalid")]), "text/html; charset=utf-8")
    result = sources.collect_source("baidu_realtime")
    assert result["status"] == "ok" and result["error"] is None
    assert [item["rank"] for item in result["items"]] == [None, 1, 9, None]
    assert result["items"][0]["heat_value"] == 123456
    assert result["items"][0]["heat_label"] == "热搜指数"
    assert result["items"][2]["heat_value"] is None and result["items"][2]["heat_label"] is None
    assert all(item["published_at"] is None for item in result["items"])
    assert all("sa=" not in item["url"] and "rsv_" not in item["url"] and "wd=" in item["url"] for item in result["items"])
    assert result["fetched_at"] == "2026-10-12T13:00:00Z"


def test_candidate_text_caps_do_not_emit_html_scripts_or_article_fulltext(monkeypatch):
    patch_content(monkeypatch, rss(summary="<p>安全摘要</p><script>privateSecret()</script>" + "字" * 900, extra="<content>这篇全文不允许输出</content>"))
    result = sources.collect_source("bbc_world")
    item = result["items"][0]
    assert item["title"] == "科技新闻 & 研究" and len(item["summary"]) == 400
    assert "privateSecret" not in item["summary"] and "全文" not in item["summary"] and "<" not in item["summary"]
    assert item["url"] == "https://www.bbc.co.uk/news/articles/document123?id=456"
    assert item["published_at"] == "2026-10-12T12:00:00Z"
    assert item["rank"] is None and item["heat_value"] is None and item["heat_label"] is None


def test_output_is_bounded_to_50_candidates_for_both_kinds(monkeypatch):
    patch_content(monkeypatch, baidu([row(index, word="字" * 500) for index in range(70)]), "text/html")
    result = sources.collect_source("baidu_realtime")
    assert len(result["items"]) == 50 and all(len(item["title"]) == 300 for item in result["items"])
    items = "".join(f'<item><title>新闻 {index}</title><link>https://www.bbc.co.uk/articles/{index}</link><pubDate>{format_datetime(NOW)}</pubDate></item>' for index in range(70))
    patch_content(monkeypatch, rss(items))
    assert len(sources.collect_source("bbc_world")["items"]) == 50


def test_a_future_publication_date_is_normalised_and_declared(monkeypatch):
    """源方时钟快于本机时，归一到抓取时刻并保留原值，而不是编一个时间或整条丢弃。

    实测 InfoQ 中国比本机快约 1 小时。若照原样入库，下游会当成「未来新闻」，
    时效性评分与新鲜度排序都会失真。
    """
    future = format_datetime(NOW + timedelta(minutes=52))
    patch_content(monkeypatch, rss(f'<item><title>未来时间条目</title><link>https://www.infoq.cn/article/one</link><pubDate>{future}</pubDate></item>'))
    result = sources.collect_source("infoq_cn")
    assert result["status"] == "ok" and len(result["items"]) == 1
    item = result["items"][0]
    assert item["published_at"] == "2026-10-12T13:00:00Z", "应归一到抓取时刻"
    # 原值以 ISO 形式保留（不是回显源方原始字符串），便于直接比较与排序。
    assert item["published_at_adjusted_from"] == "2026-10-12T13:52:00Z"
    assert any("晚于本机时钟" in line for line in item["signal_uncertainties"])


def test_a_publication_date_far_in_the_future_is_dropped_not_normalised(monkeypatch):
    """超出容忍上限的未来时间不是时钟偏差，是脏数据：丢弃，不替它归一。"""
    absurd = format_datetime(NOW + timedelta(days=40))
    patch_content(monkeypatch, rss(f'<item><title>脏数据</title><link>https://www.infoq.cn/article/bad</link><pubDate>{absurd}</pubDate></item>'))
    result = sources.collect_source("infoq_cn")
    assert result["status"] == "error" and result["items"] == []


def test_a_doctype_inside_article_cdata_does_not_reject_the_feed(monkeypatch):
    """正文 CDATA 里嵌的 HTML 带 <!DOCTYPE html> 不是文档类型声明。

    实测 github.blog 就是这种情况：全文正则会把整个正常订阅源误杀成
    「含 DOCTYPE」。真正的危害只来自 XML 序言里的 DTD/实体声明。
    """
    body = rss(f'<item><title>正常文章</title><link>https://www.bbc.co.uk/news/articles/x</link>'
               f'<description><![CDATA[<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.0//EN"><p>正文</p>]]></description>'
               f'<pubDate>{format_datetime(NOW - timedelta(hours=1))}</pubDate></item>')
    patch_content(monkeypatch, body)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "ok" and len(result["items"]) == 1


def test_a_doctype_in_the_xml_prolog_is_still_rejected(monkeypatch):
    """序言里的 DOCTYPE 才是真威胁（可带实体声明），必须照旧拒绝。"""
    body = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]>'
            '<rss version="2.0"><channel><item><title>t</title>'
            '<link>https://www.bbc.co.uk/news/articles/x</link>'
            f'<pubDate>{format_datetime(NOW - timedelta(hours=1))}</pubDate></item></channel></rss>')
    patch_content(monkeypatch, body)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and "DOCTYPE" in result["error"]


def test_atom_updated_is_accepted_when_there_is_no_published(monkeypatch):
    """Atom 用 <published>/<updated>。GitHub Releases 只给 <updated>，
    漏掉它会导致整个源被判「无发布日期」而整条拒收。"""
    entry = ('<entry><title>v0.12.0</title>'
             '<link href="https://github.com/ollama/ollama/releases/tag/v0.12.0"/>'
             f'<updated>{NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}</updated></entry>')
    atom = f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'
    patch_content(monkeypatch, atom, "application/atom+xml; charset=utf-8")
    result = sources.collect_source("github_ollama_releases")
    assert result["status"] == "ok" and len(result["items"]) == 1
    assert result["items"][0]["published_at"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_transient_timeout_is_retried_once_before_giving_up(monkeypatch):
    """超时类故障重试一次；确定性错误不重试（重放不会让 HTML 变成 RSS）。"""
    calls = []

    def flaky(source):
        calls.append(source.id)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("slow")
        return (rss(f'<item><title>t</title><link>https://www.bbc.co.uk/news/articles/x</link>'
                    f'<pubDate>{format_datetime(NOW - timedelta(hours=1))}</pubDate></item>').encode("utf-8"),
                {"content-type": "text/xml"})

    monkeypatch.setattr(sources, "_fetch", flaky)
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "ok" and len(calls) == 2


def test_a_content_error_is_not_retried(monkeypatch):
    calls = []

    def always_bad(source):
        calls.append(source.id)
        raise sources.SourceError("来源未提供可核验的文章发布日期")

    monkeypatch.setattr(sources, "_fetch", always_bad)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and len(calls) == 1


def test_stale_rss_is_explicit_error_even_with_new_feed_build_date(monkeypatch):
    patch_content(monkeypatch, rss(date=format_datetime(NOW - timedelta(days=8))))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and "7 天" in result["error"]


@pytest.mark.parametrize("headers", [{"age": "3601"}, {"date": format_datetime(NOW - timedelta(days=2))}])
def test_stale_baidu_response_is_not_current_hotspot(monkeypatch, headers):
    patch_content(monkeypatch, baidu([row()]), "text/html", **headers)
    result = sources.collect_source("baidu_realtime")
    assert result["status"] == "error" and result["items"] == []


def test_atom_supports_relative_first_party_links_and_utc_dates(monkeypatch):
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI research</title><link rel="alternate" href="/blog/research-result?utm_medium=rss&amp;id=7"/><summary>Research summary</summary><published>2026-10-12T14:00:00+02:00</published><updated>2026-10-12T15:00:00+02:00</updated></entry></feed>'
    patch_content(monkeypatch, atom)
    result = sources.collect_source("google_research")
    assert result["status"] == "ok"
    assert result["items"][0]["url"] == "https://research.google/blog/research-result?id=7"
    assert result["items"][0]["published_at"] == "2026-10-12T12:00:00Z"


def test_missing_item_date_is_null_and_not_replaced_by_feed_build_time(monkeypatch):
    dated = f'<item><title>Dated</title><link>https://www.bbc.co.uk/dated</link><pubDate>{format_datetime(NOW)}</pubDate></item>'
    undated = '<item><title>Undated</title><link>https://www.bbc.co.uk/undated</link></item>'
    patch_content(monkeypatch, rss(undated + dated))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "ok" and result["items"][-1]["published_at"] is None
    assert result["items"][0]["title"] == "Dated"
    patch_content(monkeypatch, rss(undated))
    assert sources.collect_source("bbc_world")["status"] == "error"


def test_out_of_range_timezone_conversion_is_an_unknown_date_not_a_crash(monkeypatch):
    assert sources._date("0001-01-01T00:00:00+01:00") is None
    patch_content(monkeypatch, rss(date="0001-01-01T00:00:00+01:00"))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == []


@pytest.mark.parametrize("text", ["<html>captcha required</html>", "<rss><channel><item>", rss(""), rss('<item><title>Unsafe</title><link>http://127.0.0.1/private</link></item>')])
def test_invalid_or_empty_feed_does_not_fabricate_candidates(monkeypatch, text):
    patch_content(monkeypatch, text)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and result["error"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_doctype_and_external_entities_are_rejected_after_decoding(monkeypatch, encoding):
    xml = '<!DOCTYPE rss [<!ENTITY leak SYSTEM "file:///C:/secret">]>' + rss()
    patch_content(monkeypatch, xml.encode(encoding))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and "DOCTYPE" in result["error"]


def test_character_encoding_is_taken_from_header_or_html_meta(monkeypatch):
    document = baidu([row()])
    patch_content(monkeypatch, document.encode("gb18030"), "text/html; charset=gb18030")
    assert sources.collect_source("baidu_realtime")["items"][0]["title"] == "真实热点候选"
    document = document.replace('charset="utf-8"', 'charset="gb18030"')
    patch_content(monkeypatch, document.encode("gb18030"), "text/html")
    assert sources.collect_source("baidu_realtime")["status"] == "ok"


@pytest.mark.parametrize("value", ["javascript:alert(1)", "file:///C:/secret", "http://127.0.0.1/news", "http://[::1]/news", "http://localhost/news", "https://www.bbc.co.uk.evil.invalid/news", "https://user:password@www.bbc.co.uk/news", "https://www.bbc.co.uk:1234/news", "https://www.bbc.co.uk\\@evil.invalid/news", "https://www.bbc.co.uk/news\nprivate"])
def test_article_link_rejects_nonpublic_or_unapproved_destinations(value):
    assert sources._canonical_article(value, sources._BY_ID["bbc_world"]) is None


def test_public_article_canonicalization_preserves_document_ids():
    assert sources._canonical_article("/news?id=7", sources._BY_ID["bbc_world"]) is None  # feed host is not a news article host
    value = "http://www.bbc.co.uk/news?id=7&docid=88&utm_source=rss&fbclid=tracking#part"
    assert sources._canonical_article(value, sources._BY_ID["bbc_world"]) == "http://www.bbc.co.uk/news?id=7&docid=88"
    assert sources._canonical_article("/index/ai?id=9", sources._BY_ID["openai_news"]) == "https://openai.com/index/ai?id=9"


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://feeds.bbci.co.uk/private-api", "http://feeds.bbci.co.uk/news/world/rss.xml", "https://user:pass@feeds.bbci.co.uk/news/world/rss.xml", "https://feeds.bbci.co.uk/news/world/rss.xml?extra=1", "https://feeds.bbci.co.uk/news/technology/rss.xml"])
def test_redirects_cannot_expand_the_fixed_source_boundary(monkeypatch, location):
    requests = transport(monkeypatch, lambda request: response(status=302, location=location))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == []
    assert len(requests) == 1


def test_redirect_loop_is_bounded(monkeypatch):
    requests = transport(monkeypatch, lambda request: response(status=301, location=sources._BY_ID["bbc_world"].url))
    assert sources.collect_source("bbc_world")["status"] == "error"
    assert len(requests) == sources.MAX_REDIRECTS + 1


def test_fetch_uses_only_fixed_endpoint_and_explicit_timeouts(monkeypatch):
    requests = transport(monkeypatch, lambda request: response(rss().encode()))
    assert sources.collect_source("bbc_world")["status"] == "ok"
    assert str(requests[0].url) == sources._BY_ID["bbc_world"].url
    assert requests[0].headers["accept-encoding"] == "identity"
    assert set(requests[0].extensions["timeout"]) == {"connect", "read", "write", "pool"}
    assert all(0 < value <= 10 for value in requests[0].extensions["timeout"].values())


@pytest.mark.parametrize("with_length", [False, True])
def test_network_body_limit_is_enforced_with_or_without_content_length(monkeypatch, with_length):
    body = b"x" * (sources.MAX_BYTES + 1)
    headers = {"content-length": str(len(body))} if with_length else {}
    transport(monkeypatch, lambda request: response(body, **headers))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and "2 MiB" in result["error"]


def test_compressed_response_cannot_expand_past_body_limit(monkeypatch):
    compressed = gzip.compress(b"x" * (sources.MAX_BYTES + 100))
    transport(monkeypatch, lambda request: response(compressed, **{"content-encoding": "gzip"}))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and "2 MiB" in result["error"]


def test_gzip_public_feed_is_supported_with_bounded_output(monkeypatch):
    transport(monkeypatch, lambda request: response(gzip.compress(rss().encode()), **{"content-encoding": "gzip"}))
    assert sources.collect_source("bbc_world")["status"] == "ok"


def test_timeout_does_not_expose_raw_network_messages_or_fake_data(monkeypatch):
    def fail(request):
        raise httpx.ReadTimeout("private-token=secret", request=request)
    transport(monkeypatch, fail)
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == [] and "超时" in result["error"]
    assert "secret" not in result["error"]


def test_slow_tiny_raw_chunks_are_checked_before_an_aggregate_buffer_fills(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: elapsed[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                elapsed[0] += 1.0
                yield b"x"

    transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/xml"}, stream=SlowStream()))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and "时间预算" in result["error"]
    assert elapsed[0] <= 11


@pytest.mark.parametrize("status,media", [(403, "text/xml"), (200, "text/html"), (500, "text/xml")])
def test_unavailable_or_verification_response_is_explicit_error(monkeypatch, status, media):
    transport(monkeypatch, lambda request: response(b"<html>verify identity</html>", status, **{"content-type": media}))
    result = sources.collect_source("bbc_world")
    assert result["status"] == "error" and result["items"] == []
