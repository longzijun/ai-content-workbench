"""Read-only Commons discovery and an internal, evidence-bound downloader.

Discovery never downloads image bytes or accepts model rights assertions. An
eligible candidate is a reference illustration, not verified event photography
or approval to publish. Downloads accept only IDs registered by this module.

Official contracts checked 2026-10-12:
https://www.mediawiki.org/wiki/API:Search
https://www.mediawiki.org/wiki/API:Imageinfo
https://www.mediawiki.org/wiki/API:Revisions
https://www.mediawiki.org/wiki/Extension:CommonsMetadata#Returned_data
https://commons.wikimedia.org/wiki/Template:PD-self
https://creativecommons.org/publicdomain/zero/1.0/
https://commons.wikimedia.org/wiki/Commons:Non-copyright_restrictions
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit
import warnings

import httpx
from PIL import Image, UnidentifiedImageError


API_URL = "https://commons.wikimedia.org/w/api.php"
MAX_CANDIDATES = 5
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 20_000_000
TIMEOUT_SECONDS = 12.0
MAX_REDIRECTS = 2
REGISTRY_TTL_SECONDS = 30 * 60
_registry = {}
_lock = threading.RLock()
_HEADERS = {"User-Agent": "SelfMediaWorkbench/0.1 (public material metadata; local desktop)", "Accept-Encoding": "identity"}
_MIMES = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}
_GENERIC = {"ai", "artificial intelligence", "technology", "science", "travel", "tourism", "news", "society", "innovation", "future", "research", "computer", "robot", "people", "world", "image", "photo", "gpt", "旅游", "科技", "人工智能", "社会", "新闻", "热点", "研究", "未来", "机器人", "图片", "旅行", "最新消息"}
_EXT_KEYS = "LicenseShortName|LicenseUrl|Artist|DateTimeOriginal|ImageDescription|ObjectName|UsageTerms|Copyrighted|AttributionRequired|Attribution|Credit|Restrictions|DeletionReason"


class MaterialSourceError(ValueError):
    """Controlled errors never echo remote content, credentials or raw paths."""


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _plain(value, limit=1000):
    if not isinstance(value, str):
        return ""
    parser = _Plain()
    parser.feed(value[:100_000])
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:limit]


def _norm(value):
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _binding(topic, work):
    if not isinstance(topic, dict) or not isinstance(work, dict):
        raise MaterialSourceError("素材检索需要可信选题和冻结作品")
    value = {"topic_id": topic.get("id"), "topic_title": topic.get("title"), "sources": topic.get("sources", []),
             "work": {key: work.get(key) for key in ("platform", "title", "body", "image_prompts")}}
    try:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        raise MaterialSourceError("选题或作品元数据无法可靠绑定") from None


def _subjects(topic):
    # topic/source titles are trusted workflow input. Model work.body, prompts,
    # license and keywords are never authority for matching or licensing.
    titles = [_plain(topic.get("title"), 200)]
    sources = topic.get("sources", [])
    if isinstance(sources, list):
        titles += [_plain(source.get("title"), 200) for source in sources[:20] if isinstance(source, dict)]
    result = []
    for title in titles:
        if not title:
            continue
        possible = [title]
        possible += re.findall(r'[“「"]([^”」"]{4,80})[”」"]', title)
        possible += re.findall(r"(?<!\w)(?:[A-Z][a-z]+(?:\s+|$)){2,8}", title)
        possible += re.findall(r"(?<!\w)(?:[A-Z]{3,8}(?:[- ]\d+(?:\.\d+)?)?|[A-Za-z]+[- ]\d+(?:\.\d+)?|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)(?!\w)", title)
        for subject in possible:
            subject = subject.strip()
            normalized = _norm(subject)
            residual = normalized
            for generic in sorted(_GENERIC | {"and", "the", "of", "a", "an", "or", "in", "to", "的", "与", "和", "新"}, key=len, reverse=True):
                residual = residual.replace(generic, "") if re.search(r"[\u3400-\u9fff]", generic) else re.sub(r"\b" + re.escape(generic) + r"\b", "", residual)
            if (normalized and normalized not in _GENERIC and len(normalized) >= 4
                    and len(re.sub(r"\W", "", residual)) >= 3
                    and not re.fullmatch(r"[0-9 ]+", normalized) and normalized not in [_norm(item) for item in result]):
                result.append(subject)
    return result[:20]


def _upload_url(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
            return None
        if any(not key.lower().startswith("utm_") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return None
        decoded = unquote(parsed.path)
        if unquote(decoded) != decoded or re.search(r"[\x00-\x1f\x7f\\]", decoded):
            return None
        parts = decoded.split("/")
        if any(part in {".", ".."} for part in parts):
            return None
        match = re.fullmatch(r"/wikipedia/commons/(?:thumb/)?([0-9a-f])/([0-9a-f]{2})/([^/]+)(?:/([^/]+))?", decoded)
        if not match or match.group(2)[0] != match.group(1) or bool(match.group(4)) != decoded.startswith("/wikipedia/commons/thumb/"):
            return None
        return urlunsplit(("https", "upload.wikimedia.org", parsed.path, "", ""))
    except (ValueError, TypeError):
        return None


def _description_url(value, title):
    try:
        parsed = urlsplit(value)
        expected = "/wiki/" + title.replace("_", " ")
        if (parsed.scheme == "https" and parsed.hostname == "commons.wikimedia.org" and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                and unquote(parsed.path).replace("_", " ") == expected):
            return value
    except (TypeError, ValueError):
        pass
    return None


def _read(response, limit, deadline):
    if time.monotonic() >= deadline:
        raise MaterialSourceError("素材来源读取超过时间预算")
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        raise MaterialSourceError("素材来源未返回限定的原始响应格式")
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise MaterialSourceError("素材响应超过大小限制")
    chunks, size = [], 0
    for chunk in response.iter_raw():
        if time.monotonic() > deadline:
            raise MaterialSourceError("素材来源读取超过时间预算")
        size += len(chunk)
        if size > limit:
            raise MaterialSourceError("素材响应超过大小限制")
        chunks.append(chunk)
    if not size:
        raise MaterialSourceError("素材来源返回空内容")
    return b"".join(chunks)


def _api_get(params):
    params = {"action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo|revisions",
              "iiprop": "url|extmetadata|mime|size|sha1", "iiurlwidth": "1280", "iiextmetadatalanguage": "en",
              "iiextmetadatafilter": _EXT_KEYS, "rvprop": "ids|content", "rvslots": "main", "maxlag": "5", **params}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    with httpx.Client(headers={**_HEADERS, "Accept": "application/json"}, timeout=TIMEOUT_SECONDS, trust_env=True, follow_redirects=False) as client:
        with client.stream("GET", API_URL, params=params) as response:
            if response.status_code != 200:
                raise MaterialSourceError(f"Commons官方元数据暂不可访问（HTTP {response.status_code}），未生成素材授权；可改用AI示意图")
            if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                raise MaterialSourceError("Commons官方来源未返回元数据JSON，可能需要验证或页面已变化；未生成素材授权")
            payload = _read(response, MAX_JSON_BYTES, deadline)
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("error") or data.get("warnings"):
            raise MaterialSourceError("Commons官方检索未完整成功，未生成素材授权")
        pages = data.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            raise MaterialSourceError("Commons官方元数据结构已变化")
        return pages[:MAX_CANDIDATES]
    except (ValueError, TypeError, AttributeError):
        raise MaterialSourceError("Commons官方元数据无法可靠解析") from None


def _meta(info, name, limit=1000):
    ext = info.get("extmetadata", {})
    entry = ext.get(name, {}) if isinstance(ext, dict) else {}
    return _plain(entry.get("value"), limit) if isinstance(entry, dict) else ""


def _license_url(value):
    try:
        parsed = urlsplit(value)
        if (not re.search(r"[\x00-\x20\x7f\\]", value) and parsed.scheme == "https"
                and parsed.hostname in {"creativecommons.org", "commons.wikimedia.org", "www.gnu.org", "gnu.org"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443) and not parsed.query and not parsed.fragment):
            return value
    except (TypeError, ValueError):
        pass
    return ""


def _file_hash(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    if re.fullmatch(r"[0-9a-z]{16,31}", value) and int(value, 36) < 2**160:
        return f"{int(value, 36):040x}"
    return None


def _known_date(raw):
    # Keep the source's original text. Upload timestamp never substitutes for
    # creation date, and prose/approximate dates remain explicitly unverified.
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:T[0-9:.+-]+Z?)?", raw)
    if not match:
        return False
    try:
        value = date.fromisoformat(match.group(1))
        if "T" in raw:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > _now():
                return False
        return value <= _now().date()
    except ValueError:
        return False


def _candidate(page, subjects):
    if not isinstance(page, dict) or page.get("ns") != 6 or page.get("missing") or page.get("imagerepository") not in {None, "local"}:
        return None
    pageid, title = page.get("pageid"), page.get("title")
    if isinstance(pageid, bool) or not isinstance(pageid, int) or pageid <= 0 or not isinstance(title, str) or not title.startswith("File:"):
        return None
    infos = page.get("imageinfo", [])
    if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
        return None
    info, reasons = infos[0], []
    original, thumbnail = _upload_url(info.get("url")), _upload_url(info.get("thumburl"))
    description_url = _description_url(info.get("descriptionurl"), title)
    if not original or not description_url:
        return None
    license_name, license_url = _meta(info, "LicenseShortName"), _license_url(_meta(info, "LicenseUrl"))
    artist, material_date = _meta(info, "Artist"), _meta(info, "DateTimeOriginal")
    revisions = page.get("revisions", [])
    revision = revisions[0] if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict) else {}
    slots = revision.get("slots", {})
    slot = slots.get("main", {}) if isinstance(slots, dict) else {}
    wikitext = slot.get("content", "") if isinstance(slot, dict) else ""
    if not isinstance(wikitext, str):
        wikitext = ""
    wikitext = re.sub(r"<!--.*?-->|<(?:nowiki|pre|syntaxhighlight|source)\b[^>]*>.*?</(?:nowiki|pre|syntaxhighlight|source)>", "", wikitext, flags=re.S | re.I)
    # Metadata license fields are documented as unreliable with multi-licensing.
    # Require an explicit current description-page license template as well.
    templates = {name.strip().casefold().replace("_", " ") for name in re.findall(r"\{\{\s*([^|{}\n]+)", wikitext)}
    for parameters in re.findall(r"\{\{\s*self\s*\|([^{}]+)", wikitext, re.I):
        templates.update(value.strip().casefold() for value in parameters.split("|") if "=" not in value)
    cc0 = (license_name.casefold() in {"cc0", "cc0 1.0", "cc0 1.0 universal"}
           and license_url.rstrip("/") == "https://creativecommons.org/publicdomain/zero/1.0"
           and bool(templates & {"cc-zero", "cc0"}) and _meta(info, "Copyrighted").casefold() == "false")
    pd_self = (license_name.casefold() == "public domain" and "pd-self" in templates
               and _meta(info, "Copyrighted").casefold() == "false")
    competing = any(re.match(r"(?:cc[ -]?by|gfdl|pd[- ](?!self(?:$| ))|permission|no permission|copyvio|delete|disputed)", name) for name in templates)
    restrictions = _meta(info, "Restrictions") or _meta(info, "DeletionReason")
    usage = _meta(info, "UsageTerms").casefold()
    usage_consistent = (cc0 and bool(re.fullmatch(r"(?:creative commons )?cc0(?: 1\.0)?(?: universal)?", usage))) or (pd_self and usage == "public domain")
    if not (cc0 or pd_self) or competing or restrictions or not usage_consistent:
        reasons.append("许可缺失、非明确CC0/PD-self、存在其他许可或限制；需人工核对文件原页，不以模型许可声明代替授权。")
    if pd_self and not license_url:
        license_url = "https://commons.wikimedia.org/wiki/Template:PD-self"
    if not artist:
        reasons.append("作者或权利人信息未明确，需人工核对。")
    if _meta(info, "AttributionRequired").casefold() == "true":
        reasons.append("元数据要求署名，不能按无条件素材自动使用。")
    if isinstance(revision.get("revid"), bool) or not isinstance(revision.get("revid"), int) or revision["revid"] <= 0:
        reasons.append("文件说明页版本未核实，需重新核对授权。")
    if not _known_date(material_date):
        reasons.append("素材创作日期未知、非明确日期或在未来，不能以上传/抓取时间冒充。")
    file_subject = title[5:].rsplit(".", 1)[0].replace("_", " ")
    normalized = " " + _norm(file_subject) + " "
    matches = [subject for subject in subjects if (_norm(subject) in normalized if re.search(r"[\u3400-\u9fff]", subject) else " " + _norm(subject) + " " in normalized)]
    if not matches:
        reasons.append("文件原标题未确切匹配选题的具体主体；泛科技/旅游词或搜索命中不能证明相关。")
    if re.search(r"\b(?:logo|portrait|person|people|trademark|insignia|emblem|seal)\b|肖像|人像|商标|标志|徽章", file_subject, re.I):
        reasons.append("文件名涉及人物、标志或商标等额外权利，需人工核对；开放版权许可不涵盖全部权利。")
    if info.get("mime") not in {item[0] for item in _MIMES.values()}:
        reasons.append("原文件不是支持的PNG/JPEG/WebP栅格图片，需人工选择安全格式。")
    if isinstance(info.get("size"), bool) or not isinstance(info.get("size"), int) or not 0 < info["size"] <= MAX_IMAGE_BYTES:
        reasons.append("原图大小未知或超过16MiB限制，需人工处理。")
    file_sha1 = _file_hash(info.get("sha1"))
    if not file_sha1:
        reasons.append("官方文件版本哈希未提供，不能绑定并复核实际下载字节。")
    result = {"provider": "wikimedia_commons", "pageid": pageid, "title": _plain(title, 400), "description_url": description_url,
              "thumbnail_url": thumbnail, "original_url": original, "license": license_name or "unknown", "license_url": license_url or None,
              "artist": artist or None, "date": material_date or None, "date_status": "known" if _known_date(material_date) else "unknown",
              "description": _meta(info, "ImageDescription", 1200), "credit": _meta(info, "Attribution") or _meta(info, "Credit") or artist,
              "relevance": {"status": "exact_subject" if matches else "needs_review", "matched_subjects": matches, "basis": "仅比较可信选题/来源标题中的具体主体与Commons文件原标题；不推断事件实拍。"},
              "eligible": not reasons, "requires_review": bool(reasons), "review_reasons": reasons,
              "caption": "资料图片，素材日期：" + (material_date or "未知") + "；非本次事件现场照片",
              "depicts_verified_event": False, "publication_authorized": False, "revision_id": revision.get("revid"),
              "file_sha1": file_sha1, "mime_type": info.get("mime"), "file_size": info.get("size")}
    result["id"] = "commons:" + str(pageid) + ":" + hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    return result


def discover_material_candidates(topic, work):
    """At most five official metadata candidates; no image bytes or file writes."""
    result = {"checked_at": _iso(_now()), "candidates": [], "error": None}
    try:
        binding = _binding(topic, work)
        query = _plain(topic.get("title"), 160)
        if not query:
            raise MaterialSourceError("没有可信的选题原题，未使用模型输出编造素材检索或授权")
        subjects = _subjects(topic)
        pages = _api_get({"generator": "search", "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": str(MAX_CANDIDATES)})
        candidates = [_candidate(page, subjects) for page in pages]
        candidates = [item for item in candidates if item is not None][:MAX_CANDIDATES]
        with _lock:
            now = time.monotonic()
            for key in list(_registry):
                if now - _registry[key]["registered_at"] > REGISTRY_TTL_SECONDS:
                    del _registry[key]
            for candidate in candidates:
                _registry[(binding, candidate["id"])] = {"candidate": deepcopy(candidate), "registered_at": now}
            while len(_registry) > 256:
                del _registry[next(iter(_registry))]
        result["candidates"] = candidates
    except MaterialSourceError as error:
        result["error"] = str(error)
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, RecursionError):
        result["error"] = "Commons官方检索暂不可用，未取得授权素材；可改用AI示意图"
    return result


def _image_metadata(data, declared_mime):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise MaterialSourceError("下载图片为空或超过16MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                format_name, width, height = image.format, image.width, image.height
                if format_name not in _MIMES or getattr(image, "n_frames", 1) != 1 or not 1 <= width <= 16384 or not 1 <= height <= 16384 or width * height > MAX_PIXELS:
                    raise MaterialSourceError("素材图片格式、帧数或像素尺寸不支持")
                if declared_mime != _MIMES[format_name][0]:
                    raise MaterialSourceError("素材响应类型与实际图片字节不一致")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()  # Complete raster decode, rather than header-only.
        return {"width": width, "height": height, "mime_type": _MIMES[format_name][0], "extension": _MIMES[format_name][1],
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    except MaterialSourceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MaterialSourceError("下载内容不能完整解码为安全的真实栅格图片") from None


def _download(candidate):
    # Only approved URLs for this exact Commons file can be redirect targets.
    urls = {value for value in (candidate["original_url"], candidate.get("thumbnail_url")) if value}
    url, deadline = candidate["original_url"], time.monotonic() + TIMEOUT_SECONDS
    with httpx.Client(headers=_HEADERS, timeout=TIMEOUT_SECONDS, trust_env=True, follow_redirects=False) as client:
        for count in range(MAX_REDIRECTS + 1):
            if _upload_url(url) != url or url not in urls:
                raise MaterialSourceError("素材下载地址不是已登记文件的固定Commons上传路径")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MaterialSourceError("素材下载超过时间预算")
            with client.stream("GET", url, timeout=remaining) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = _upload_url(urljoin(url, response.headers.get("location", "")))
                    if count == MAX_REDIRECTS or target not in urls:
                        raise MaterialSourceError("素材重定向超限或离开已登记图片，未继续下载")
                    url = target
                    continue
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if response.status_code != 200 or mime not in {item[0] for item in _MIMES.values()}:
                    raise MaterialSourceError("素材下载未返回成功的图片内容")
                data = _read(response, MAX_IMAGE_BYTES, deadline)
                return data, _image_metadata(data, mime)
    raise MaterialSourceError("素材下载未获得可验证结果")


def download_authorized_candidate(candidate_id, settings, topic, work):
    """Internal only: take a registered ID, recheck evidence, save exact bytes.

    No raw URL, model license, arbitrary destination, cropping, watermark
    removal or event-photography claim is accepted by this interface.
    """
    if not isinstance(candidate_id, str) or not re.fullmatch(r"commons:[1-9][0-9]{0,15}:[0-9a-f]{24}", candidate_id):
        raise MaterialSourceError("需要本模块真实检索登记的素材ID，不能传入图片URL或模型授权")
    binding = _binding(topic, work)
    with _lock:
        record = deepcopy(_registry.get((binding, candidate_id)))
    if not record or time.monotonic() - record["registered_at"] > REGISTRY_TTL_SECONDS or not record["candidate"]["eligible"]:
        raise MaterialSourceError("素材尚未登记、已过期、未授权或与冻结选题/作品不匹配，未下载")
    candidate = record["candidate"]
    try:
        pages = _api_get({"pageids": str(candidate["pageid"])})
        refreshed = next((_candidate(page, _subjects(topic)) for page in pages if isinstance(page, dict) and page.get("pageid") == candidate["pageid"]), None)
        if not refreshed or not refreshed["eligible"] or refreshed["id"] != candidate_id:
            raise MaterialSourceError("Commons素材说明页或授权证据已变化，需要重新检索审核，未下载")
        data, metadata = _download(candidate)
        if hashlib.sha1(data).hexdigest() != candidate["file_sha1"] or len(data) != candidate["file_size"]:
            raise MaterialSourceError("下载图片与官方授权的文件版本哈希或大小不符，未保存")
        runtime = Path(settings.runtime_dir)
        if not runtime.is_absolute():
            raise MaterialSourceError("素材保存需要可信的绝对runtime目录")
        runtime = runtime.resolve()
        directory = runtime / "material-downloads" / binding
        if not directory.resolve().is_relative_to(runtime):
            raise MaterialSourceError("素材输出目录离开runtime，未保存")
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(runtime):
            raise MaterialSourceError("素材输出目录被重定向，未保存")
        path = directory / (metadata["sha256"] + metadata["extension"])
        if path.exists():
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_IMAGE_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
                raise MaterialSourceError("已有固定素材文件被修改，未覆盖")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        source = {"kind": "material", "rights_confirmed": True, "authorization_id": candidate_id, "license": candidate["license"],
                  "license_url": candidate["license_url"], "rights_holder": candidate["artist"], "credit": candidate["credit"],
                  "source_url": candidate["description_url"], "original_url": candidate["original_url"], "material_date": candidate["date"],
                  "caption": candidate["caption"], "depicts_verified_event": False, "review_required": True, "publication_authorized": False,
                  "metadata_checked_at": _iso(_now()), "commons_revision_id": candidate["revision_id"]}
        return {"path": str(path.resolve()), "metadata": metadata, "source": source}
    except MaterialSourceError:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
        raise MaterialSourceError("素材授权复核、下载或保存未成功，未登记为可用图片") from None
