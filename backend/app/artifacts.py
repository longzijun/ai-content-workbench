from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import re
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .adapters import _wechat_token
from .config import Settings
from . import platform_registry, video_script


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _work_path(topic: dict, settings: Settings) -> Path:
    topic_id = str(topic["id"])
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", topic_id):
        raise ValueError("选题ID不符合文件路径规则")
    root = (Path(settings.vault_path) / "工作台" / "03-内容项目").resolve()
    path = root / topic_id / "工作稿.md"
    if not path.resolve().is_relative_to(root):
        raise ValueError("同步路径超出项目目录")
    return path


def _markdown(topic: dict, version: dict) -> str:
    payload = version["payload"]
    title = str(topic["title"]).replace("\n", " ")
    lines = ["---", "类型: 工作稿", "项目: 自媒体AI智能运营工作台", "选题ID: " + str(topic["id"]), "版本ID: " + str(version["id"]), "状态: 工作稿（不构成批准）", "---", "# " + title, "", "> 可直接编辑各平台标题、正文及内容策略，在工作台导入为新版本；保留工作稿标记和元数据。审批与发布历史不会被笔记修改。", "", "## 内容策略", "<!-- workbench:strategy -->", payload.get("strategy", ""), "<!-- workbench:end:strategy -->", ""]
    metadata_works = []
    for work in payload["works"]:
        platform = work["platform"]
        lines.extend(["", "## " + platform + " 稿件", "<!-- workbench:work:" + platform + " -->", "# " + work["title"], "", work["body"], "<!-- workbench:end:" + platform + " -->", ""])
        metadata_works.append({key: value for key, value in work.items() if key not in {"body", "title"}})
    lines.extend(["## 素材与关联元数据", "", "> 可修改标签及图片提示词。正文在上方直接编辑，元数据中的ID与平台字段请保留。", "", "~~~workbench-json", json.dumps({"topic_id": topic["id"], "base_version_id": version["id"], "works": metadata_works}, ensure_ascii=False, indent=2), "~~~", ""])
    return "\n".join(lines)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_version(topic: dict, version: dict, settings: Settings, baseline: str | None = None) -> dict:
    path = _work_path(topic, settings)
    from .image_assets import get_asset_manifest, get_asset_file
    image_lines = ['', '## 实际图片与出处（只读记录）', '', '> 配图绑定本内容版本；图片存在不构成发布批准。']
    for work in version['payload']['works']:
        platform = work['platform']
        manifest = get_asset_manifest(settings, topic['id'], version['id'], platform)
        if manifest['status'] != 'ready':
            continue
        directory = path.parent / '素材' / version['id'] / platform
        if not directory.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError('图片同步目录离开当前选题范围')
        directory.mkdir(parents=True, exist_ok=True)
        for asset in manifest['assets']:
            source = get_asset_file(settings,topic['id'],version['id'],platform,asset['id'])
            target = directory / source.name
            data = source.read_bytes()
            if target.exists() and _sha(target.read_bytes()) != asset['sha256']:
                raise ValueError('Obsidian 中固定版本图片发生变化，原图保留，请核对')
            if not target.exists():
                _atomic_write(target, data)
            relative = target.relative_to(path.parent).as_posix()
            image_lines.extend(['', f'### {platform} · {asset["role"]}', f'![{asset.get("caption") or "实际配图"}]({relative})', f'来源：{asset["provider"]}；许可：{asset.get("license") or "待核对"}；SHA256：{asset["sha256"]}', f'图片说明：{asset.get("caption") or "请审核素材日期、来源及授权"}'])
    data = (_markdown(topic, version) + '\n'.join(image_lines) + '\n').encode("utf-8")
    if path.exists():
        current = _sha(path.read_bytes())
        if current == _sha(data):
            return {"path": str(path), "status": "written", "baseline": current, "message": "该版本已同步，未重复写入"}
        if not baseline or current != baseline:
            conflict = path.with_name("待合并-" + str(version["id"]) + ".md")
            _atomic_write(conflict, data)
            return {"path": str(path), "status": "conflict", "conflict_path": str(conflict), "message": "Obsidian工作稿已发生外部修改，原稿保留；新候选版本另存为待合并文件"}
    _atomic_write(path, data)
    return {"path": str(path), "status": "written", "baseline": _sha(data), "message": "已同步到Obsidian工作稿；授权仍由工作台管理"}


def import_working_copy(topic: dict, settings: Settings) -> dict:
    path = _work_path(topic, settings)
    if not path.exists():
        raise ValueError("尚未同步工作稿，请先写入Obsidian")
    if path.stat().st_size > 2_000_000:
        raise ValueError("工作稿文件过大，无法导入")
    source_bytes = path.read_bytes()
    text = source_bytes.decode("utf-8-sig")
    match = re.search(r"^~~~workbench-json\s*\n(.*?)\n~~~\s*$", text, re.M | re.S)
    if not match:
        raise ValueError("找不到可导入JSON区块，请保留workbench-json围栏")
    record = json.loads(match.group(1))
    if record.get("topic_id") != topic["id"]:
        raise ValueError("笔记中的选题ID与当前项目不一致")
    if "payload" in record:
        payload = record["payload"]
    else:
        strategy = re.search(r"<!-- workbench:strategy -->\s*\n(.*?)\n<!-- workbench:end:strategy -->", text, re.S)
        if not strategy:
            raise ValueError("内容策略工作稿标记缺失")
        works = []
        for item in record.get("works", []):
            platform = item.get("platform", "")
            if platform not in platform_registry.PLATFORM_SET:
                raise ValueError("工作稿元数据中的平台无效")
            match_work = re.search(r"<!-- workbench:work:" + platform + r" -->\s*\n# ([^\n]+)\n(.*?)\n<!-- workbench:end:" + platform + r" -->", text, re.S)
            if not match_work:
                raise ValueError("找不到平台稿件，请保留标题和工作稿标记：" + platform)
            if video_script.is_video_work(item):
                authored = {**item, "title": match_work.group(1).strip()}
                if match_work.group(2).strip() != video_script.spoken_text(authored):
                    raise ValueError("视频口播由分镜唯一派生，请在内容工坊修改分镜；不能仅修改展示正文后导入")
                works.append(video_script.normalize_work(authored))
            else:
                works.append({**item, "title": match_work.group(1).strip(), "body": match_work.group(2).strip()})
        payload = {"strategy": strategy.group(1).strip(), "works": works}
    return {"payload": payload, "base_version_id": record.get("base_version_id"), "source_version_id": record.get("base_version_id"), "baseline": _sha(source_bytes)}


def _html_body(body: str) -> str:
    result, paragraph, code = [], [], []
    in_code = False

    def flush() -> None:
        if paragraph:
            result.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def inline(text: str) -> str:
        escaped = html.escape(text)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return escaped

    for line in body.splitlines():
        if line.strip().startswith("```"):
            flush()
            if in_code:
                result.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code.clear()
            in_code = not in_code
        elif in_code:
            code.append(line)
        elif not line.strip():
            flush()
        elif match := re.match(r"^(#{1,6})\s+(.+)$", line):
            flush()
            level = min(len(match.group(1)), 4)
            result.append(f"<h{level}>" + inline(match.group(2)) + f"</h{level}>")
        elif re.match(r"^[-*]\s+", line):
            flush()
            result.append("<p>• " + inline(line[2:]) + "</p>")
        elif line.startswith(">"):
            flush()
            result.append("<blockquote>" + inline(line.lstrip("> ")) + "</blockquote>")
        else:
            paragraph.append(inline(line))
    flush()
    if code:
        result.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    return "\n".join(result)


def _visible_text(markup: str) -> str:
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    reader = Reader()
    reader.feed(markup)
    return re.sub(r"\s+", "", "".join(reader.parts))


def _remote_get(token: str, media_id: str, expected_title: str | None = None, expected_content: str | None = None) -> dict:
    try:
        response = httpx.post("https://api.weixin.qq.com/cgi-bin/draft/get", params={"access_token": token}, json={"media_id": media_id}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("errcode", 0) or not data.get("news_item"):
            return {"state": "unknown", "remote_id": media_id, "error": "草稿已有远端ID，但回读未确认，错误码：" + str(data.get("errcode", "unknown")) + "；请在平台核对"}
        first = data["news_item"][0]
        if expected_title is not None and first.get("title") != expected_title:
            return {"state": "unknown", "remote_id": media_id, "error": "远端草稿标题与当前稿件不一致，请在平台核对后再审核"}
        if expected_content is not None and (not isinstance(first.get("content"), str) or _visible_text(first["content"]) != _visible_text(expected_content)):
            return {"state": "unknown", "remote_id": media_id, "error": "草稿已有远端ID，但回读正文与当前稿件不一致，请在平台核对后再审核"}
        return {"state": "synced", "remote_id": media_id, "remote_url": first.get("url") or None, "error": None}
    except Exception:
        return {"state": "unknown", "remote_id": media_id, "error": "平台已返回草稿ID，但回读未完成；可再次查询，请勿重复创建"}


def sync_platform_draft(draft: dict, version: dict, topic: dict, settings: Settings) -> dict:
    from . import runtime_mode
    runtime_mode.require_platform_operations("平台草稿保存")
    platform = draft["platform"]
    names = {"wechat": "微信公众号", "toutiao": "今日头条", "xiaohongshu": "小红书"}
    browser_id = str(draft.get('remote_id') or '').startswith('browser:')
    browser_route = platform != 'wechat' or browser_id
    if platform == 'wechat' and not draft.get('remote_id') and not (settings.wechat_app_id and settings.wechat_capability_verified and settings.wechat_thumb_media_id):
        # Cached observations reset on app restart and cannot determine whether
        # the independent project Edge is still logged in. The browser adapter
        # restores only a proven running session, then checks live DOM identity.
        browser_route = True
    if browser_route:
        from .browser_drafts import sync_browser_draft
        return sync_browser_draft(draft, version, topic, settings)
    work = next((x for x in version["payload"]["works"] if x["platform"] == platform), None)
    account_label = "微信公众号（AppID尾号" + settings.wechat_app_id[-4:] + "）"

    def read_remote(token: str, media_id: str) -> dict:
        result = _remote_get(token, media_id, work["title"] if work else None, _html_body(work["body"]) if work else None)
        result["account_label"] = account_label
        return result

    if draft.get("remote_id"):
        if draft.get("account_label") and draft["account_label"] != account_label:
            return {"state": "unknown", "remote_id": draft["remote_id"], "error": "微信配置账号已改变，请恢复原账号后查询此草稿"}
        try:
            return read_remote(_wechat_token(settings), draft["remote_id"])
        except Exception:
            return {"state": "unknown", "remote_id": draft["remote_id"], "error": "无法验证现有平台草稿，请在后台核对"}
    if draft.get("state") == "unknown":
        return {"state": "unknown", "error": "上次保存结果未确认，请先在微信后台核对，不能自动再次创建"}
    if not settings.wechat_app_id or not settings.wechat_capability_verified:
        return {"state": "needs_connection", "error": "请在设置保存微信接口凭据并验证草稿箱读取权限；尚未存入平台"}
    if not settings.wechat_thumb_media_id:
        return {"state": "needs_connection", "error": "还需要微信公众号的永久封面素材media_id；图片提示词不能代替真实封面"}
    if not work:
        return {"state": "blocked", "error": "当前版本没有对应公众号稿件"}
    if len(work["title"]) > 32:
        return {"state": "blocked", "error": "公众号标题超过当前官方草稿接口的32字限制，请修改后新建版本"}
    try:
        token = _wechat_token(settings)
    except Exception as exc:
        return {"state": "needs_connection", "error": str(exc)[:300] if isinstance(exc, (RuntimeError, ValueError)) else "微信认证未完成"}
    article = {"article_type": "news", "title": work["title"], "content": _html_body(work["body"]), "thumb_media_id": settings.wechat_thumb_media_id, "need_open_comment": 1, "only_fans_can_comment": 0}
    # This adapter only saves a private draft. It never invokes freepublish or mass send.
    try:
        response = httpx.post("https://api.weixin.qq.com/cgi-bin/draft/add", params={"access_token": token}, json={"articles": [article]}, timeout=25)
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        return {"state": "unknown", "error": "平台保存请求超时，可能已生成草稿，请先在微信后台核对；不会盲目重复创建"}
    except Exception:
        return {"state": "unknown", "error": "平台保存结果未确认，请先在微信后台核对"}
    if data.get("errcode", 0):
        return {"state": "failed", "error": "微信未接受草稿，错误码：" + str(data["errcode"]) + "；请核对权限、封面和正文要求"}
    media_id = data.get("media_id")
    if not isinstance(media_id, str) or not media_id:
        return {"state": "unknown", "error": "平台响应缺少草稿ID，结果需在后台核对"}
    return read_remote(token, media_id)


def export_publication(publication: dict, version: dict, topic: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (publication["id"] + ".zip")
    work = next(x for x in version["payload"]["works"] if x["platform"] == publication["platform"])
    plain = "# " + work["title"] + "\n\n" + work["body"]
    prompt_lines = list(work.get("image_prompts", []))
    for task in work.get("image_tasks", []) if isinstance(work.get("image_tasks", []), list) else []:
        if isinstance(task, dict) and task.get("prompt"):
            prompt_lines.append(f"[{task.get('page_role', 'body')}] {task['prompt']}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("稿件.md", plain)
        archive.writestr("图片提示词.txt", "\n\n".join(prompt_lines))
        archive.writestr("manifest.json", json.dumps({"publication_id": publication["id"], "version_id": version["id"], "hash": version["hash"], "platform": work["platform"], "generated_images": False}, ensure_ascii=False, indent=2))
        archive.writestr("index.html", '<!doctype html><meta charset="utf-8"><title>稿件恢复副本</title><pre>' + html.escape(plain) + "</pre>")
    return target


def _create_backup(settings: Settings) -> dict:
    when = datetime.now(timezone.utc)
    path = settings.runtime_dir / "backups" / ("workbench-" + when.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".partial")
    with tempfile.TemporaryDirectory(dir=settings.runtime_dir / "backups") as temporary:
        database_copy = Path(temporary) / "workbench.sqlite3"
        source = settings.runtime_dir / "data" / "workbench.sqlite3"
        if source.exists():
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(database_copy)) as dst:
                src.backup(dst)
                # Session cookies/write tokens are credentials, not content.
                # Scrub only the private backup copy; never log users out of the
                # live database as a side effect of making a backup.
                for table in ('sessions', 'auth_sessions', 'auth_failures'):
                    if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        dst.execute('DELETE FROM "' + table + '"')
                dst.commit()
        snapshot_versions = None
        if database_copy.exists():
            with closing(sqlite3.connect(database_copy)) as snapshot:
                snapshot_versions = {row[0] for row in snapshot.execute('SELECT id FROM versions')}
        files = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            if database_copy.exists():
                archive.write(database_copy, "data/workbench.sqlite3")
                files += 1
            # Keep duplicate-prevention IDs with the content backup; exclude all
            # browser profiles, cookies and local image/cache data.
            ledger = settings.runtime_dir / 'data' / 'browser-draft-ledger'
            if ledger.exists():
                for proof in ledger.glob('*.json'):
                    archive.write(proof, 'data/browser-draft-ledger/' + proof.name)
                    files += 1
            settings_path = settings.runtime_dir / "settings.json"
            if settings_path.exists():
                archive.write(settings_path, "settings.json")
                files += 1
            trend_settings = settings.runtime_dir / 'data' / 'trend-settings.json'
            if trend_settings.exists():
                archive.write(trend_settings, 'data/trend-settings.json')
                files += 1
            assets_root = settings.runtime_dir / 'assets'
            if assets_root.is_dir():
                from .image_assets import get_asset_manifest, get_asset_file
                for asset_manifest in assets_root.glob('*/*/*/manifest.json'):
                    if asset_manifest.is_symlink() or not asset_manifest.resolve().is_relative_to(assets_root.resolve()):
                        continue
                    relative = asset_manifest.relative_to(assets_root)
                    topic_id, version_id, platform = relative.parts[:3]
                    # A version created after the SQLite snapshot belongs to the
                    # next backup, not this database's asset reference set.
                    if snapshot_versions is not None and version_id not in snapshot_versions:
                        continue
                    manifest = get_asset_manifest(settings, topic_id, version_id, platform)
                    if manifest['status']=='corrupt':
                        raise ValueError('图片资产损坏，备份未完成，请先核对固定版本图片')
                    frozen_manifest = asset_manifest.read_bytes()
                    frozen_assets = json.loads(frozen_manifest).get('assets', [])
                    archive.writestr('assets/' + relative.as_posix(), frozen_manifest)
                    files += 1
                    for asset in frozen_assets:
                        source = get_asset_file(settings,topic_id,version_id,platform,asset['id'])
                        frozen_bytes = source.read_bytes()
                        if _sha(frozen_bytes) != asset.get('sha256'):
                            raise ValueError('备份期间图片内容改变，未生成可用备份')
                        archive.writestr('assets/' + source.relative_to(assets_root).as_posix(), frozen_bytes)
                        files += 1
            project = Path(settings.vault_path) / "工作台" / "03-内容项目"
            hashes = {}
            if project.exists():
                for note in project.rglob("*.md"):
                    if note.is_symlink():
                        continue
                    relative = note.relative_to(project)
                    data = note.read_bytes()
                    archive.writestr("obsidian/" + relative.as_posix(), data)
                    hashes[relative.as_posix()] = _sha(data)
                    files += 1
            archive.writestr("manifest.json", json.dumps({"created_at": when.isoformat(), "files": hashes, "credentials_included": False, "browser_profile_included": False, 'workbench_image_assets_included':True, "note": "保留内容、防重复保存账本与工作台实际图片；不含平台登录或浏览器数据。小红书原生本地草稿数据库不在备份内。恢复后重新登录并核对已知草稿。"}, ensure_ascii=False, indent=2))
    return {"path": str(path), "files": files, "created_at": when.isoformat()}


def create_backup(settings: Settings) -> dict:
    from .execution_runtime import ProcessLock
    from .backup_tools import attach_integrity
    with ProcessLock(settings.runtime_dir / 'backup.lock'):
        result = _create_backup(settings)
        try:
            result['verification'] = attach_integrity(Path(result['path']))
        except Exception:
            # Keep an unverified archive explicitly separate from valid backups.
            original = Path(result['path'])
            original.replace(original.with_suffix('.unverified'))
            raise
        verified = Path(result['path']).with_suffix('.zip')
        Path(result['path']).replace(verified)
        result['path'] = str(verified)
        return result
