"""材料包（V3 阶段3）：审定批次的本地交付入口与 ZIP。

边界（执行提示词第六节）：

- 只有「三平台审阅全部通过」的批次才允许导出。缺素材、退回、待重审、
  版本过期都阻断，并逐平台说明缺什么——不静默导出旧稿。
- 待审作品可本地预览（preview 入口），但与「已审定发布包」在状态上明确
  分开；预览不自动加载任何平台外部资源，不弹专用登录浏览器。
- ZIP 内按平台组织：标题/正文/摘要/话题文本 + 封面与配图（按顺序命名）
  + AI 标识与署名提醒 + 不含凭据的清单。底稿原文、提示词、审计记录留在
  工作台受控区，不进交付包。
- 小红书图文卡片直接引用已审定文字排版产生的资产文件，不重新生成、
  不以提示词冒充图片。
"""
from __future__ import annotations

import html
import json
import zipfile
from pathlib import Path

from . import db
from .config import get_settings
from .review import console
from .service import _asset_view, _row, encode, new_id, now, require

PLATFORM_LABELS = {"wechat": "公众号", "toutiao": "今日头条", "xiaohongshu": "小红书"}
PLATFORM_FILES = {
    "wechat": {"title": "标题.txt", "body": "正文.md", "extra": "摘要.txt"},
    "toutiao": {"title": "标题.txt", "body": "正文.md", "extra": None},
    "xiaohongshu": {"title": "标题.txt", "body": "笔记正文.md", "extra": "话题建议.txt"},
}


def _safe_name(text: str) -> str:
    keep = []
    for char in str(text or "")[:60]:
        keep.append(char if char.isalnum() or char in "_-（）()——" else "_")
    return "".join(keep).strip("_") or "batch"


def _platform_texts(console_data: dict) -> dict:
    """每个平台的标题/正文/摘要/话题纯文本（复制用）。"""
    result = {}
    for entry in console_data["platforms"]:
        platform = entry["platform"]
        work = entry.get("work") or {}
        body = str(work.get("body") or "")
        texts = {"title": str(work.get("title") or ""), "body": body}
        if platform == "wechat":
            paragraphs = [item.strip() for item in body.split("\n\n") if item.strip()]
            texts["extra"] = paragraphs[0][:120] if paragraphs else ""
        elif platform == "xiaohongshu":
            tags = work.get("tags") or []
            texts["extra"] = " ".join("#" + str(tag) for tag in tags)
        result[platform] = texts
    return result


def _copy_assets(zip_file: zipfile.ZipFile, topic_id: str, version_id: str, platform: str, folder: str) -> list[dict]:
    """按顺序复制平台资产进 ZIP，返回清单条目。"""
    entries = []
    try:
        assets = _asset_view(topic_id, version_id, platform)
    except Exception:
        return entries
    for index, asset in enumerate(assets.get("assets") or [], start=1):
        path = Path(str(asset.get("path") or ""))
        if not path.is_file():
            continue
        name = f"{folder}/{index:02d}-{path.name}"
        zip_file.write(path, name)
        entries.append({"order": index, "file": name, "source": asset.get("source") or {},
                        "ai_generated": bool(asset.get("ai_generated")),
                        "attribution": asset.get("attribution") or ""})
    return entries


def build(topic_id: str) -> dict:
    """构建（或重建）材料包。返回包记录；未全部审过时 409 并说明阻断项。"""
    data = console(topic_id)
    missing = [item for item in data["gaps"]]
    blocked = []
    for entry in data["platforms"]:
        review = entry.get("review") or {}
        for item in review.get("blocking") or []:
            blocked.append(f"{PLATFORM_LABELS.get(entry['platform'], entry['platform'])}：{item}")
    require(not missing, "共享事实底稿还有缺口：" + "；".join(missing), 409)
    require(not blocked, "以下平台还不能进材料包——" + "；".join(blocked), 409)
    require(data["version_id"], "当前选题没有可交付的版本", 409)

    settings = get_settings()
    topic = None
    with db.connect() as connection:
        topic = _row(connection, "topics", topic_id)
    version_id = data["version_id"]
    package_id = new_id()
    folder_name = f"{_safe_name(topic['title'])}-v{version_id[:8]}"
    # Rebuilding the same content version must not overwrite a prior export.
    export_dir = settings.runtime_dir / "exports" / "material-packages" / package_id
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / f"{folder_name}.zip"
    texts = _platform_texts(data)
    manifest = {
        "topic_id": topic_id, "title": topic["title"], "version_id": version_id,
        "fact_base_revision": data["fact_base"]["revision"] if data["fact_base"] else None,
        "created_at": now(), "platforms": {}, "review": {},
        "notice": "本包由工作台本地生成，不含任何账号凭据；发布请在官方应用人工完成。",
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for platform, files in PLATFORM_FILES.items():
            texts_for = texts.get(platform) or {"title": "", "body": ""}
            platform_dir = f"{folder_name}/{platform}"
            zip_file.writestr(f"{platform_dir}/{files['title']}", texts_for["title"])
            zip_file.writestr(f"{platform_dir}/{files['body']}", texts_for["body"])
            if files["extra"]:
                zip_file.writestr(f"{platform_dir}/{files['extra']}", texts_for.get("extra", ""))
            entries = _copy_assets(zip_file, topic_id, version_id, platform, f"{platform_dir}/images")
            entry = next(item for item in data["platforms"] if item["platform"] == platform)
            review = entry.get("review") or {}
            manifest["platforms"][platform] = {
                "label": PLATFORM_LABELS[platform], "title": texts_for["title"],
                "files": [files["title"], files["body"]] + ([files["extra"]] if files["extra"] else []),
                "images": entries,
                "ai_disclosure": (entry.get("draft") or {}).get("ai_disclosure") or "AI 辅助创作，发布前请按平台要求确认 AI 标识。",
                "review": {"result": (review.get("latest") or {}).get("result"),
                           "fingerprint": review.get("fingerprint"),
                           "at": (review.get("latest") or {}).get("created_at")},
            }
        entry_html = _entry_html(manifest, texts)
        zip_file.writestr(f"{folder_name}/index.html", entry_html)
        (export_dir / "index.html").write_text(entry_html, encoding="utf-8")
        manifest["entry"] = f"{folder_name}/index.html"
        zip_file.writestr(f"{folder_name}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO material_packages(id,topic_id,version_id,state,manifest,zip_path,entry_path,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (package_id, topic_id, version_id, "ready", encode(manifest), str(zip_path), str(export_dir / "index.html"), now()))
        connection.execute(
            "UPDATE material_packages SET state='superseded' WHERE topic_id=? AND id<>? AND state='ready'",
            (topic_id, package_id))
    return {"id": package_id, "topic_id": topic_id, "version_id": version_id,
            "zip_path": str(zip_path), "entry_path": str(export_dir / "index.html"),
            "manifest": manifest}


def _entry_html(manifest: dict, texts: dict) -> str:
    """自包含入口预览页：不加载任何平台外部资源，无外链脚本。"""
    sections = []
    for platform, info in manifest["platforms"].items():
        texts_for = texts.get(platform) or {"title": "", "body": ""}
        images = "".join(f"<li>{html.escape(item['file'])}</li>" for item in info["images"]) or "<li>（无配图）</li>"
        sections.append(f"""
        <section class="platform" id="{html.escape(platform)}">
          <h2>{html.escape(info['label'])}</h2>
          <h3>标题</h3><textarea readonly rows="2">{html.escape(texts_for['title'])}</textarea>
          <h3>正文</h3><textarea readonly rows="18">{html.escape(texts_for['body'])}</textarea>
          <h3>图片文件（按上传顺序）</h3><ul>{images}</ul>
          <p class="notice">{html.escape(info['ai_disclosure'])}</p>
        </section>""")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>发布材料包 · {html.escape(manifest['title'])}</title>
<style>
 body{{font-family:'Microsoft YaHei',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2328}}
 textarea{{width:100%;font-size:14px;padding:8px;border:1px solid #d0d7de;border-radius:6px}}
 .platform{{border:1px solid #d0d7de;border-radius:8px;padding:16px;margin:16px 0}}
 .notice{{color:#9a6700;background:#fff8c5;padding:8px;border-radius:6px;font-size:13px}}
 h1{{font-size:20px}} h2{{font-size:17px}} h3{{font-size:14px;margin:12px 0 4px}}
</style></head><body>
<h1>发布材料包 · {html.escape(manifest['title'])}</h1>
<p>版本 <code>{html.escape(manifest['version_id'][:8])}</code> · 生成时间 {html.escape(manifest['created_at'])}</p>
<p class="notice">{html.escape(manifest['notice'])}本页不加载任何外部资源；复制正文后到各平台官方后台人工发布。</p>
{''.join(sections)}
</body></html>"""


def list_packages(topic_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM material_packages"
    params: tuple = ()
    if topic_id:
        query += " WHERE topic_id=?"
        params = (topic_id,)
    query += " ORDER BY created_at DESC"
    with db.connect() as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["manifest"] = json.loads(item.get("manifest") or "{}")
        result.append(item)
    return result


def get(package_id: str) -> dict:
    with db.connect() as connection:
        row = _row(connection, "material_packages", package_id)
    item = dict(row)
    item["manifest"] = json.loads(item.get("manifest") or "{}")
    return item
