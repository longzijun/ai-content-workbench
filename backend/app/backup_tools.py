"""Archive verification and restore to an empty, explicitly selected directory."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

INTEGRITY_FILE = 'backup-integrity.json'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def attach_integrity(path: Path):
    with zipfile.ZipFile(path, 'a', zipfile.ZIP_DEFLATED) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or INTEGRITY_FILE in names:
            raise ValueError('备份包含重复或已有完整性清单，未覆盖')
        records = {name: {'sha256': _sha(archive.read(name)), 'bytes': archive.getinfo(name).file_size}
                   for name in names}
        archive.writestr(INTEGRITY_FILE, json.dumps({'schema': 1, 'files': records}, ensure_ascii=False, indent=2))
    return verify_archive(path)


def _safe_name(info):
    name = PurePosixPath(info.filename)
    mode = info.external_attr >> 16
    if name.is_absolute() or '..' in name.parts or '\\' in info.filename or ':' in info.filename or stat.S_ISLNK(mode):
        raise ValueError('备份中包含不安全路径或符号链接')
    return name


def verify_archive(path: Path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('备份 ZIP CRC 校验失败')
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('备份中存在重复路径')
        for info in archive.infolist():
            _safe_name(info)
        if INTEGRITY_FILE not in names:
            raise ValueError('此备份没有完整性清单；旧备份请使用隔离旧版恢复验证流程')
        records = json.loads(archive.read(INTEGRITY_FILE))['files']
        if set(records) != set(names) - {INTEGRITY_FILE}:
            raise ValueError('备份文件集合与完整性清单不符')
        for name, item in records.items():
            data = archive.read(name)
            if len(data) != item['bytes'] or _sha(data) != item['sha256']:
                raise ValueError('备份文件 SHA-256 校验失败：' + name)
        if 'data/workbench.sqlite3' not in names:
            raise ValueError('备份缺少工作台数据库')
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'workbench.sqlite3'
            database.write_bytes(archive.read('data/workbench.sqlite3'))
            with closing(sqlite3.connect(database)) as connection:
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('备份数据库完整性检查失败')
                rows = connection.execute('SELECT id,hash,payload FROM versions').fetchall()
                versions = {row[0]: row[1] for row in rows}
                if any(_sha(row[2].encode('utf-8')) != row[1] for row in rows):
                    raise ValueError('备份正式版本内容与冻结哈希不一致')
                known_topics = {row[0] for row in connection.execute('SELECT id FROM topics')}
            manifest_count = 0
            for name in names:
                parts = PurePosixPath(name).parts
                if len(parts) != 5 or parts[0] != 'assets' or parts[-1] != 'manifest.json':
                    continue
                manifest_count += 1
                manifest = json.loads(archive.read(name))
                if parts[1] not in known_topics or parts[2] not in versions:
                    raise ValueError('备份素材清单指向不存在的选题或版本')
                for asset in manifest.get('assets', []):
                    filename = asset.get('filename') or Path(str(asset.get('path', ''))).name
                    target = '/'.join(parts[:-1]) + '/' + filename
                    if target not in records or _sha(archive.read(target)) != asset.get('sha256'):
                        raise ValueError('备份素材引用缺失或校验不一致')
        return {'verified': True, 'files': len(records), 'versions': len(versions), 'asset_manifests': manifest_count}


def restore_archive(path: Path, destination: Path):
    verification = verify_archive(path)
    target = destination.resolve()
    if target == Path(target.anchor) or (target.exists() and any(target.iterdir())):
        raise ValueError('恢复目标必须是明确指定的新目录或空目录；不会覆盖现有运行数据')
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = _safe_name(info)
            output = (target / Path(*name.parts)).resolve()
            if not output.is_relative_to(target):
                raise ValueError('恢复路径越界')
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(info))
    for manifest_path in (target / 'assets').glob('*/*/*/manifest.json'):
        manifest = json.loads(manifest_path.read_text('utf-8'))
        for asset in manifest.get('assets', []):
            filename = asset.get('filename') or Path(str(asset.get('path', ''))).name
            candidate = (manifest_path.parent / filename).resolve()
            if not candidate.is_relative_to(manifest_path.parent.resolve()) or _sha(candidate.read_bytes()) != asset['sha256']:
                raise ValueError('恢复素材路径或校验无效')
            asset['path'] = str(candidate)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    return {**verification, 'destination': str(target), 'started_services': False,
            'note': '已隔离恢复；未启动监测、生成或任何平台操作，登录与机器凭据需重新配置。'}
