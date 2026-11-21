import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from './api';
import type { Platform } from './types';

interface PlatformAccessItem {
  platform: Platform; enabled: boolean; account_label: string; enabled_at: string | null;
  blocked: boolean; blocked_reason: string; draft_capability: { status: string; note: string };
}
interface PlatformAccessView { platforms: PlatformAccessItem[]; global_released: boolean; mode: string; }

const names: Record<Platform, string> = { wechat: '公众号', toutiao: '今日头条', xiaohongshu: '小红书', weixin_channels: '视频号', kuaishou: '快手', douyin: '抖音' };

export default function PlatformAccessSettings() {
  const [view, setView] = useState<PlatformAccessView | null>(null);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const load = async () => {
    try {
      const data = await api<PlatformAccessView>('/platform-access');
      setView(data);
      const next: Record<string, string> = {};
      data.platforms.forEach(item => { next[item.platform] = item.account_label || ''; });
      setLabels(next); setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  useEffect(() => { void load(); }, []);
  const submit = async (platform: Platform, enabled: boolean) => {
    setBusy(platform); setError('');
    try { setView(await api<PlatformAccessView>('/platform-access', 'POST', { platform, enabled, account_label: labels[platform] || '' })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(''); }
  };
  return <section className="panel platform-access-settings" aria-label="逐平台草稿保存许可">
    <div className="panel-title"><div><h2>逐平台草稿保存许可</h2><small>全局平台操作关闭时即使启用也不会自动保存；这里先做逐平台/账号登记，供将来选择性放行</small></div><button type="button" className="btn small" onClick={() => void load()}><RefreshCw size={14} />刷新</button></div>
    {error && <p className="callout warning" role="alert">{error}</p>}
    {view && <p className="muted-note">全局平台操作：{view.global_released ? '已发布（本轮不生效）' : '未发布（关闭）'} · 运行模式：{view.mode === 'local_content_only' ? '本地内容模式' : view.mode}</p>}
    <div className="platform-access-list">{view?.platforms.map(item => <div className={`platform-access-row${item.blocked ? ' blocked' : ''}`} key={item.platform}>
      <span className={`platform-dot ${item.platform}`} /><strong>{names[item.platform]}</strong>
      <span className={`tag ${item.blocked ? 'muted' : item.enabled ? 'green' : 'amber'}`}>{item.blocked ? '永久关闭' : item.enabled ? '已启用' : '未启用'}</span>
      <small>{item.blocked ? item.blocked_reason : item.draft_capability.note}</small>
      {!item.blocked && <label>账号<input value={labels[item.platform] || ''} onChange={event => setLabels({ ...labels, [item.platform]: event.target.value })} placeholder="填写该平台使用的账号" /></label>}
      {!item.blocked && <button type="button" className="btn small" disabled={!!busy} onClick={() => void submit(item.platform, !item.enabled)}>{busy === item.platform ? '保存中…' : item.enabled ? '停用' : '启用'}</button>}
    </div>)}</div>
    {!view && !error && <p role="status">正在读取逐平台许可…</p>}
  </section>;
}
