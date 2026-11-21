"""Read-only capability inventory. Observations never authorize delivery."""
from datetime import datetime, timezone
import sqlite3

from . import browser_bridge, platform_registry
from .config import get_settings


def _history(settings):
    path = settings.runtime_dir / 'data' / 'workbench.sqlite3'
    if not path.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        # One statement gives a consistent snapshot; no schema initialization.
        return [dict(row) for row in connection.execute('''
            SELECT d.platform, d.state, d.destination, d.remote_id, d.updated_at,
                   t.state AS delivery_state
            FROM drafts d LEFT JOIN delivery_tasks t ON t.draft_id=d.id
        ''')]
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()


def _saved_channel(row):
    if row['state'] not in {'synced', 'browser_saved', 'reviewed', 'published'}:
        return None
    remote_id = str(row['remote_id'] or '')
    platform = row['platform']
    if remote_id.startswith(f'browser:{platform}:') and remote_id.removeprefix(f'browser:{platform}:'):
        expected_scope = 'browser' if platform == 'xiaohongshu' else 'platform'
        if row['destination'] == expected_scope:
            return f'{platform}:browser'
    elif platform == 'wechat' and remote_id and not remote_id.startswith('browser:') and row['destination'] == 'platform':
        return 'wechat:official_api'
    return None


def inventory(settings=None):
    settings = settings or get_settings()
    observations = browser_bridge.get_status(settings)  # Cache only, never launch/probe.
    records = _history(settings)
    rows = records or []
    channels = []
    for platform, label, content_format, scope in (
        ('wechat', '公众号文章 · 网页', 'article', 'platform'),
        ('toutiao', '头条文章 · 网页', 'article', 'platform'),
        ('xiaohongshu', '小红书笔记 · 网页', 'note', 'browser'),
    ):
        cached = observations.get(platform, {})
        observed_at = cached.get('last_checked')
        state = cached.get('state', 'disconnected')
        auth = state + '_observed' if observed_at and state in {'connected', 'login_required', 'needs_verification', 'error'} else 'unknown'
        verified = bool(observed_at and state == 'connected' and cached.get('account_label') and cached.get('draft_capability_verified'))
        channels.append({
            'id': platform + ':browser', 'platform': platform, 'label': label,
            'channel': 'browser', 'format': content_format, 'implemented': True,
            'configured': None, 'storage_scope': scope, 'auth_state': auth,
            'connection_checked_at': observed_at,
            'read_evidence': 'session_save_readback' if verified else 'none',
            'write_evidence': 'session_save_readback' if verified else 'none',
            'note': '仅存于本机专用 Edge，云端与手机可见性未验证。' if scope == 'browser' else '网页保存与回读已实现，每份草稿仍需独立确认。',
        })
    configured = bool(settings.wechat_app_id and (settings.runtime_dir / 'data' / 'wechat.credentials').is_file())
    channels.append({
        'id': 'wechat:official_api', 'platform': 'wechat', 'label': '公众号文章 · 官方 API',
        'channel': 'official_api', 'format': 'article', 'implemented': True,
        'configured': configured, 'storage_scope': 'platform', 'auth_state': 'unknown',
        'connection_checked_at': None,
        'read_evidence': 'settings_read_probe' if configured and settings.wechat_capability_verified else 'none',
        'write_evidence': 'none',
        'note': '读取验证不代表写入权限；账号权限、永久封面与正文图片交付仍需核验。' if configured else '尚未配置此工作台的官方 API 凭据，可继续使用网页通道。',
    })
    for identity, platform, label, channel, content_format, scope, note in (
        ('toutiao:short_post', 'toutiao', '微头条', 'browser', 'short_post', 'unknown', '当前工作台尚未实现微头条，文章通道的历史成功不适用于微头条。'),
        ('wechat:image_post', 'wechat', '公众号贴图', 'browser', 'image_post', 'unknown', '当前工作台尚未实现公众号贴图（图片消息），文章通道的历史成功不适用于贴图。'),
        ('xiaohongshu:cloud', 'xiaohongshu', '小红书 · 云端草稿', 'official_api', 'note', 'unknown', '当前工作台没有已验证的云端草稿通道。'),
    ):
        channels.append({'id': identity, 'platform': platform, 'label': label, 'channel': channel,
                         'format': content_format, 'implemented': False, 'configured': None,
                         'storage_scope': scope, 'auth_state': 'unknown', 'connection_checked_at': None,
                         'read_evidence': 'none', 'write_evidence': 'none', 'note': note})
    for channel in channels:
        saved = [row for row in rows if _saved_channel(row) == channel['id']]
        channel['history'] = {
            'available': records is not None,
            'saved_record_count': len(saved) if records is not None else None,
            # updated_at is not necessarily the time of saving. Never call it last_success_at.
            'latest_record_updated_at': max((row['updated_at'] for row in saved), default=None),
            'account_scope': 'all_recorded_accounts',
        }
        if saved and channel['write_evidence'] == 'none':
            channel['write_evidence'] = 'historical_records'
    unknown = sum(row['state'] == 'unknown' for row in rows)
    waiting = sum(row['delivery_state'] == 'waiting' for row in rows)
    overlap = sum(row['state'] == 'unknown' and row['delivery_state'] == 'waiting' for row in rows)
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'observation_only': True,
        'local_content': platform_registry.public_registry(),
        'channels': channels,
        'attention': {'available': records is not None,
                      'unknown_count': unknown if records is not None else None,
                      'waiting_count': waiting if records is not None else None,
                      'overlap_count': overlap if records is not None else None,
                      'distinct_draft_count': unknown + waiting - overlap if records is not None else None},
        'notice': '仅汇总本机记录与最近连接观察，不重新登录、探测或保存草稿。历史记录可能属于其他账号，不证明当前权限或会话仍有效。',
    }
