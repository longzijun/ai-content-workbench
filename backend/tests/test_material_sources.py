"""Isolated Commons metadata, rights, version binding and byte safety tests."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from backend.app import material_sources as materials


NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
TOPIC = {"id": "isolated-topic", "title": "James Webb Space Telescope", "sources": []}
WORK = {"platform": "wechat", "title": "望远镜的资料梳理", "body": "仍需核查事件事实。", "image_prompts": []}
ORIGINAL = "https://upload.wikimedia.org/wikipedia/commons/a/ab/James_Webb_Space_Telescope.png"
THUMBNAIL = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/James_Webb_Space_Telescope.png/1280px-James_Webb_Space_Telescope.png"
DESCRIPTION = "https://commons.wikimedia.org/wiki/File:James_Webb_Space_Telescope.png"
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(materials, "_registry", {})
    monkeypatch.setattr(materials, "_now", lambda: NOW)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pixels():
    buffer = BytesIO()
    Image.new("RGB", (4, 3), "blue").save(buffer, "PNG")
    return buffer.getvalue()


def page(pixels, **metadata):
    ext = {"LicenseShortName": "CC0", "LicenseUrl": "https://creativecommons.org/publicdomain/zero/1.0/",
           "Artist": "<b>Original Author</b>", "DateTimeOriginal": "2022-07-27", "UsageTerms": "Creative Commons CC0 1.0 Universal",
           "Copyrighted": "False", "AttributionRequired": "False", "ImageDescription": "资料描述<script>evil()</script>"}
    ext.update(metadata)
    return {"pageid": 11097151, "ns": 6, "title": "File:James Webb Space Telescope.png", "imagerepository": "local", "index": 1,
            "imageinfo": [{"url": ORIGINAL + "?utm_source=commons.wikimedia.org", "thumburl": THUMBNAIL,
                           "descriptionurl": DESCRIPTION, "mime": "image/png", "size": len(pixels), "sha1": hashlib.sha1(pixels).hexdigest(),
                           "extmetadata": {key: {"value": value} for key, value in ext.items()}}],
            "revisions": [{"revid": 1283243612, "slots": {"main": {"content": "{{Information|description=Reference photograph}}\n{{cc-zero}}"}}}]}


def patch_pages(monkeypatch, pages):
    calls = []
    def get(params):
        calls.append(params)
        return deepcopy(pages)
    monkeypatch.setattr(materials, "_api_get", get)
    return calls


def discover(monkeypatch, pixels, selected=None, topic=None, work=None):
    selected = selected if selected is not None else page(pixels)
    calls = patch_pages(monkeypatch, [selected])
    result = materials.discover_material_candidates(topic or TOPIC, work or WORK)
    return result, calls


def test_discovery_returns_real_metadata_and_exact_subject_but_not_event_photo_or_publication(monkeypatch, pixels, tmp_path):
    result, calls = discover(monkeypatch, pixels)
    assert result["error"] is None and result["checked_at"] == "2026-10-12T15:00:00Z"
    candidate = result["candidates"][0]
    assert candidate["eligible"] is True and candidate["relevance"]["status"] == "exact_subject"
    assert candidate["relevance"]["matched_subjects"] and candidate["review_reasons"] == []
    assert candidate["description_url"] == DESCRIPTION and candidate["original_url"] == ORIGINAL
    assert candidate["artist"] == "Original Author" and "evil" not in candidate["description"]
    assert candidate["date"] == "2022-07-27" and candidate["date_status"] == "known"
    assert "资料图片" in candidate["caption"] and "非本次事件" in candidate["caption"]
    assert candidate["publication_authorized"] is False and candidate["depicts_verified_event"] is False
    assert calls[0]["gsrnamespace"] == "6" and calls[0]["gsrlimit"] == "5"
    assert list(tmp_path.iterdir()) == []
    json.dumps(result, ensure_ascii=False)


def test_returned_candidate_mutation_cannot_change_trusted_download_record(monkeypatch, pixels, tmp_path):
    result, _ = discover(monkeypatch, pixels)
    candidate = result["candidates"][0]
    monkeypatch.setattr(materials, "_download", lambda official: (pixels, materials._image_metadata(pixels, "image/png")))
    candidate["original_url"] = "http://127.0.0.1/private"
    candidate["license"] = "model says owned"
    downloaded = materials.download_authorized_candidate(candidate["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert downloaded["source"]["original_url"] == ORIGINAL and downloaded["source"]["license"] == "CC0"


def test_pd_self_worldwide_release_can_be_reference_candidate_with_template_evidence(monkeypatch, pixels):
    selected = page(pixels, LicenseShortName="Public domain", LicenseUrl="", UsageTerms="Public domain")
    selected["revisions"][0]["slots"]["main"]["content"] = "{{Information}}\n{{PD-self}}"
    result, _ = discover(monkeypatch, pixels, selected)
    candidate = result["candidates"][0]
    assert candidate["eligible"] is True and candidate["license_url"] == "https://commons.wikimedia.org/wiki/Template:PD-self"


@pytest.mark.parametrize("license_name,template", [("CC BY 4.0", "{{cc-by-4.0}}"), ("Public domain", "{{PD-USGov}}"), ("Public domain", "{{PD-old}}"), ("unknown", ""), ("CC0", "<!--{{cc-zero}}-->"), ("CC0", "<nowiki>{{cc-zero}}</nowiki>")])
def test_other_licenses_regional_pd_and_comment_examples_need_review_not_auto_authorization(monkeypatch, pixels, license_name, template):
    selected = page(pixels, LicenseShortName=license_name)
    selected["revisions"][0]["slots"]["main"]["content"] = template
    result, _ = discover(monkeypatch, pixels, selected)
    assert result["candidates"][0]["eligible"] is False
    assert result["candidates"][0]["review_reasons"]


@pytest.mark.parametrize("metadata", [
    {"Restrictions": "trademarked"}, {"DeletionReason": "copyright disputed"}, {"Copyrighted": "True"},
    {"AttributionRequired": "True"}, {"UsageTerms": "Creative Commons Attribution"}, {"LicenseUrl": "javascript:alert(1)"},
    {"Artist": ""}, {"DateTimeOriginal": ""}, {"DateTimeOriginal": "circa 2022"}, {"DateTimeOriginal": "Taken on 2 June 2022"},
    {"DateTimeOriginal": "2026-10-13"}, {"DateTimeOriginal": "2022-13-01"},
    {"DateTimeOriginal": "2026-10-12T23:00:00Z"}, {"DateTimeOriginal": "2026-10-12T10:00:00"},
])
def test_unknown_conflicting_restricted_or_future_metadata_stays_human_review(monkeypatch, pixels, metadata):
    result, _ = discover(monkeypatch, pixels, page(pixels, **metadata))
    candidate = result["candidates"][0]
    assert candidate["eligible"] is False and candidate["requires_review"] is True
    assert "javascript:" not in (candidate["license_url"] or "")


def test_nested_self_cczero_supported_but_competing_license_is_not_auto_selected(monkeypatch, pixels):
    selected = page(pixels)
    selected["revisions"][0]["slots"]["main"]["content"] = "{{self|cc-zero}}"
    assert discover(monkeypatch, pixels, selected)[0]["candidates"][0]["eligible"] is True
    selected["revisions"][0]["slots"]["main"]["content"] = "{{self|cc-zero|cc-by-sa-4.0}}"
    assert discover(monkeypatch, pixels, selected)[0]["candidates"][0]["eligible"] is False


@pytest.mark.parametrize("title", ["Technology and travel", "科技旅游", "AI", "旅游的图片", "最新科技新闻"])
def test_generic_topic_words_do_not_prove_event_related_material(monkeypatch, pixels, title):
    selected = page(pixels)
    selected["title"] = "File:" + title + ".png"
    selected["imageinfo"][0]["descriptionurl"] = "https://commons.wikimedia.org/wiki/File:" + title.replace(" ", "_") + ".png"
    result, _ = discover(monkeypatch, pixels, selected, topic={"id": "generic", "title": title})
    assert result["candidates"][0]["eligible"] is False
    assert result["candidates"][0]["relevance"]["matched_subjects"] == []


def test_model_subject_and_license_never_make_unrelated_photo_eligible(monkeypatch, pixels):
    model_work = {**WORK, "title": "James Webb Space Telescope", "license": "CC0", "rights_confirmed": True, "material_subjects": ["James Webb Space Telescope"]}
    result, _ = discover(monkeypatch, pixels, topic={"id": "unrelated", "title": "一个普通旅游话题"}, work=model_work)
    assert result["candidates"][0]["eligible"] is False
    selected = page(pixels, LicenseShortName="unknown")
    assert discover(monkeypatch, pixels, selected, work=model_work)[0]["candidates"][0]["eligible"] is False


def test_no_trusted_topic_title_does_not_search_model_work_title(monkeypatch):
    monkeypatch.setattr(materials, "_api_get", lambda *_: pytest.fail("no trusted title must not request"))
    result = materials.discover_material_candidates({}, WORK)
    assert result["error"] and result["candidates"] == []


def test_subject_matching_uses_real_source_title_and_avoids_implied_event_photography(monkeypatch, pixels):
    result, _ = discover(monkeypatch, pixels, topic={"id": "source", "title": "望远镜资料", "sources": [{"title": "James Webb Space Telescope"}]})
    assert result["candidates"][0]["eligible"] is True
    assert result["candidates"][0]["depicts_verified_event"] is False


def test_declaration_of_people_or_logo_needs_non_copyright_review(monkeypatch, pixels):
    selected = page(pixels)
    selected["title"] = "File:James Webb Space Telescope logo.png"
    selected["imageinfo"][0]["descriptionurl"] = "https://commons.wikimedia.org/wiki/File:James_Webb_Space_Telescope_logo.png"
    result, _ = discover(monkeypatch, pixels, selected)
    assert result["candidates"][0]["eligible"] is False


def test_missing_revision_file_hash_or_oversized_original_not_auto_eligible(monkeypatch, pixels):
    for field, value in [("sha1", None), ("size", None), ("size", materials.MAX_IMAGE_BYTES + 1), ("mime", "image/svg+xml")]:
        selected = page(pixels)
        selected["imageinfo"][0][field] = value
        assert not discover(monkeypatch, pixels, selected)[0]["candidates"][0]["eligible"]
    selected = page(pixels)
    selected["revisions"][0].pop("revid")
    assert not discover(monkeypatch, pixels, selected)[0]["candidates"][0]["eligible"]


@pytest.mark.parametrize("url", [
    "http://upload.wikimedia.org/wikipedia/commons/a/ab/x.png", "https://upload.wikimedia.org.evil.invalid/wikipedia/commons/a/ab/x.png",
    "https://user:password@upload.wikimedia.org/wikipedia/commons/a/ab/x.png", "https://upload.wikimedia.org:444/wikipedia/commons/a/ab/x.png",
    "https://upload.wikimedia.org/wikipedia/en/a/ab/x.png", "https://upload.wikimedia.org/wikipedia/commons/a/ab/%2e%2e/secret.png",
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/%252e%252e.png", "https://upload.wikimedia.org/wikipedia/commons/a/ab/x.png?token=secret",
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/x.png#secret", "https://upload.wikimedia.org/wikipedia/commons/a/bb/x.png",
])
def test_upload_urls_reject_private_unregistered_paths_tracking_credentials_and_traversal(url):
    assert materials._upload_url(url) is None


@pytest.mark.parametrize("bad_id", [ORIGINAL, {"original_url": ORIGINAL, "license": "CC0"}, "commons:1:000000000000000000000000"])
def test_raw_url_forged_candidate_and_unregistered_id_never_download(monkeypatch, tmp_path, bad_id):
    monkeypatch.setattr(materials, "_api_get", lambda *_: pytest.fail("must not revalidate unknown id"))
    monkeypatch.setattr(materials, "_download", lambda *_: pytest.fail("must not download unknown id"))
    with pytest.raises(materials.MaterialSourceError):
        materials.download_authorized_candidate(bad_id, SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert not list(tmp_path.iterdir())


def test_download_is_bound_to_work_topic_and_official_metadata_version(monkeypatch, pixels, tmp_path):
    result, _ = discover(monkeypatch, pixels)
    candidate_id = result["candidates"][0]["id"]
    monkeypatch.setattr(materials, "_download", lambda *_: pytest.fail("changed work must not download"))
    for topic, work in [({**TOPIC, "id": "different"}, WORK), (TOPIC, {**WORK, "body": "改后正文"}), ({**TOPIC, "title": "另一个主体"}, WORK)]:
        with pytest.raises(materials.MaterialSourceError):
            materials.download_authorized_candidate(candidate_id, SimpleNamespace(runtime_dir=tmp_path), topic, work)
    changed = page(pixels)
    changed["revisions"][0]["revid"] += 1
    patch_pages(monkeypatch, [changed])
    with pytest.raises(materials.MaterialSourceError, match="已变化"):
        materials.download_authorized_candidate(candidate_id, SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)


def test_human_review_candidate_not_downloaded_even_if_caller_alters_returned_eligible(monkeypatch, pixels, tmp_path):
    result, _ = discover(monkeypatch, pixels, page(pixels, LicenseShortName="unknown"))
    candidate = result["candidates"][0]
    candidate["eligible"] = True
    monkeypatch.setattr(materials, "_download", lambda *_: pytest.fail("must not trust returned flag"))
    with pytest.raises(materials.MaterialSourceError):
        materials.download_authorized_candidate(candidate["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)


def test_registered_candidate_expires_without_blind_download(monkeypatch, pixels, tmp_path):
    elapsed = [1.0]
    monkeypatch.setattr(materials.time, "monotonic", lambda: elapsed[0])
    result, _ = discover(monkeypatch, pixels)
    elapsed[0] += materials.REGISTRY_TTL_SECONDS + 1
    with pytest.raises(materials.MaterialSourceError, match="过期"):
        materials.download_authorized_candidate(result["candidates"][0]["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)


def test_download_preserves_bytes_watermarks_and_source_date_then_reuses_fixed_file(monkeypatch, pixels, tmp_path):
    result, calls = discover(monkeypatch, pixels)
    monkeypatch.setattr(materials, "_download", lambda *_: (pixels, materials._image_metadata(pixels, "image/png")))
    downloaded = materials.download_authorized_candidate(result["candidates"][0]["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    path = Path(downloaded["path"])
    assert path.is_relative_to(tmp_path / "material-downloads") and path.read_bytes() == pixels
    assert downloaded["metadata"]["width"] == 4 and downloaded["metadata"]["height"] == 3
    assert downloaded["source"]["rights_confirmed"] is True and downloaded["source"]["material_date"] == "2022-07-27"
    assert downloaded["source"]["depicts_verified_event"] is False and downloaded["source"]["publication_authorized"] is False
    assert downloaded["source"]["review_required"] is True and downloaded["source"]["credit"] == "Original Author"
    assert calls[-1]["pageids"] == "11097151"
    assert not list(path.parent.glob("*.tmp"))
    second = materials.download_authorized_candidate(result["candidates"][0]["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert second["path"] == downloaded["path"]


def test_mismatched_download_hash_or_modified_fixed_file_is_not_overwritten(monkeypatch, pixels, tmp_path):
    result, _ = discover(monkeypatch, pixels)
    candidate_id = result["candidates"][0]["id"]
    buffer = BytesIO()
    Image.new("RGB", (4, 3), "red").save(buffer, "PNG")
    other = buffer.getvalue()
    monkeypatch.setattr(materials, "_download", lambda *_: (other, materials._image_metadata(other, "image/png")))
    with pytest.raises(materials.MaterialSourceError, match="哈希或大小不符"):
        materials.download_authorized_candidate(candidate_id, SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(materials, "_download", lambda *_: (pixels, materials._image_metadata(pixels, "image/png")))
    first = materials.download_authorized_candidate(candidate_id, SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    Path(first["path"]).write_bytes(b"altered fixed file")
    with pytest.raises(materials.MaterialSourceError, match="被修改"):
        materials.download_authorized_candidate(candidate_id, SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert Path(first["path"]).read_bytes() == b"altered fixed file"


@pytest.mark.parametrize("format_name,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_supported_images_require_full_pixel_decode(format_name, mime):
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(buffer, format_name)
    data = buffer.getvalue()
    assert materials._image_metadata(data, mime)["width"] == 3
    with pytest.raises(materials.MaterialSourceError):
        materials._image_metadata(data[:20], mime)


def test_html_and_mime_mismatch_or_oversized_pixels_are_not_valid_images(pixels, monkeypatch):
    for data, mime in [(b"<html>captcha</html>", "image/png"), (pixels, "image/jpeg")]:
        with pytest.raises(materials.MaterialSourceError):
            materials._image_metadata(data, mime)
    monkeypatch.setattr(materials, "MAX_PIXELS", 4)
    with pytest.raises(materials.MaterialSourceError, match="像素"):
        materials._image_metadata(pixels, "image/png")


def install_transport(monkeypatch, handler):
    calls = []
    def wrapped(request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(materials.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(wrapped), **kwargs))
    return calls


def response(body, mime="application/json", status=200, **headers):
    return httpx.Response(status, headers={"content-type": mime, **headers}, stream=httpx.ByteStream(body))


def test_real_contract_api_is_fixed_anonymous_namespace6_and_bounded_to_five(monkeypatch, pixels):
    calls = install_transport(monkeypatch, lambda request: response(json.dumps({"query": {"pages": [page(pixels)]}}).encode()))
    result = materials.discover_material_candidates(TOPIC, WORK)
    assert result["error"] is None and len(result["candidates"]) == 1
    assert str(calls[0].url).startswith(materials.API_URL + "?")
    assert calls[0].url.params["generator"] == "search" and calls[0].url.params["gsrnamespace"] == "6"
    assert calls[0].url.params["gsrlimit"] == "5" and "extmetadata" in calls[0].url.params["iiprop"]
    assert "content" in calls[0].url.params["rvprop"]
    assert "cookie" not in calls[0].headers and "authorization" not in calls[0].headers
    assert calls[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("body,mime,status,headers", [
    (b"<html>verify</html>", "text/html", 200, {}), (b"{}", "application/json", 302, {"location": "https://commons.wikimedia.org/login"}),
    (b"{}", "application/json", 403, {}), (b"bad-json", "application/json", 200, {}),
    (b'{"error":{"code":"maxlag"}}', "application/json", 200, {}),
    (b'{"warnings":{"bad":"fields"},"query":{"pages":[]}}', "application/json", 200, {}),
])
def test_login_error_or_changed_api_never_makes_fake_candidates(monkeypatch, body, mime, status, headers):
    calls = install_transport(monkeypatch, lambda request: response(body, mime, status, **headers))
    result = materials.discover_material_candidates(TOPIC, WORK)
    assert result["error"] and result["candidates"] == [] and len(calls) == 1


def test_bounded_api_body_and_download_size_limits(monkeypatch):
    monkeypatch.setattr(materials, "MAX_JSON_BYTES", 10)
    install_transport(monkeypatch, lambda request: response(b"x" * 11))
    assert materials.discover_material_candidates(TOPIC, WORK)["error"]
    monkeypatch.setattr(materials, "MAX_IMAGE_BYTES", 10)
    install_transport(monkeypatch, lambda request: response(b"x" * 11, "image/png"))
    with pytest.raises(materials.MaterialSourceError, match="大小限制"):
        materials._download({"original_url": ORIGINAL, "thumbnail_url": THUMBNAIL})


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://upload.wikimedia.org/wikipedia/commons/a/ab/different-image.png", "https://commons.wikimedia.org/login", "https://upload.wikimedia.org/wikipedia/en/a/ab/x.png"])
def test_download_redirect_cannot_switch_to_other_image_or_private_destination(monkeypatch, target):
    calls = install_transport(monkeypatch, lambda request: response(b"", status=302, location=target))
    with pytest.raises(materials.MaterialSourceError, match="重定向"):
        materials._download({"original_url": ORIGINAL, "thumbnail_url": THUMBNAIL})
    assert len(calls) == 1


def test_download_redirect_loop_and_non_image_response_stop_without_false_success(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: response(b"", status=302, location=ORIGINAL))
    with pytest.raises(materials.MaterialSourceError):
        materials._download({"original_url": ORIGINAL, "thumbnail_url": THUMBNAIL})
    assert len(calls) == materials.MAX_REDIRECTS + 1
    install_transport(monkeypatch, lambda request: response(b"<html>login</html>", "text/html"))
    with pytest.raises(materials.MaterialSourceError):
        materials._download({"original_url": ORIGINAL, "thumbnail_url": THUMBNAIL})


def test_network_read_failure_does_not_echo_tokens_or_claim_material_authorization(monkeypatch):
    def fail(request):
        raise httpx.ReadTimeout("secret-token=private", request=request)
    install_transport(monkeypatch, fail)
    result = materials.discover_material_candidates(TOPIC, WORK)
    assert result["error"] and "private" not in result["error"] and result["candidates"] == []


def test_full_internal_id_flow_rechecks_official_api_then_preserves_real_image_bytes(monkeypatch, pixels, tmp_path):
    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return response(json.dumps({"query": {"pages": [page(pixels)]}}).encode())
        assert str(request.url) == ORIGINAL
        return response(pixels, "image/png")
    calls = install_transport(monkeypatch, handler)
    result = materials.discover_material_candidates(TOPIC, WORK)
    downloaded = materials.download_authorized_candidate(result["candidates"][0]["id"], SimpleNamespace(runtime_dir=tmp_path), TOPIC, WORK)
    assert len(calls) == 3
    assert calls[1].url.params["pageids"] == "11097151" and str(calls[2].url) == ORIGINAL
    assert Path(downloaded["path"]).read_bytes() == pixels
    assert all("cookie" not in call.headers and "authorization" not in call.headers for call in calls)


def test_redirected_material_directory_cannot_escape_authorized_output_root(monkeypatch, pixels, tmp_path):
    result, _ = discover(monkeypatch, pixels)
    monkeypatch.setattr(materials, "_download", lambda *_: (pixels, materials._image_metadata(pixels, "image/png")))
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    try:
        (runtime / "material-downloads").symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows may lack symlink privilege. Simulate the same resolved-path
        # result without weakening or skipping the guard behavior under test.
        original_resolve = Path.resolve
        prefix = runtime / "material-downloads"
        def redirected_resolve(value, *args, **kwargs):
            if value.is_relative_to(prefix):
                return outside / value.relative_to(prefix)
            return original_resolve(value, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", redirected_resolve)
    with pytest.raises(materials.MaterialSourceError, match="离开runtime"):
        materials.download_authorized_candidate(result["candidates"][0]["id"], SimpleNamespace(runtime_dir=runtime), TOPIC, WORK)
    assert not list(outside.iterdir())
