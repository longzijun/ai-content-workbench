import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from './api';
import './capabilities.css';

interface Channel {
  id: string; label: string; implemented: boolean; configured: boolean | null;
  storage_scope: string; auth_state: string; connection_checked_at: string | null;
  read_evidence: string; write_evidence: string; note: string;
  history: { available: boolean; saved_record_count: number | null; latest_record_updated_at: string | null };
}
interface Inventory {
  generated_at: string; notice: string; channels: Channel[];
  attention: { available: boolean; unknown_count: number | null; waiting_count: number | null; overlap_count: number | null; distinct_draft_count: number | null };
}
const scopes: Record<string, string> = { platform: '平台草稿箱', browser: '本机专用浏览器', unknown: '保存范围未验证' };
const auth: Record<string, string> = { connected_observed: '最近检查已登录', login_required_observed: '最近检查需登录', needs_verification_observed: '最近检查需验证', error_observed: '最近检查异常', unknown: '当前登录未确认' };
const reads: Record<string, string> = { session_save_readback: '本次会话曾回读成功', settings_read_probe: '已验证草稿读取（时间未记录）', none: '读取未验证' };
const writes: Record<string, string> = { session_save_readback: '本次会话曾保存并回读', historical_records: '仅有历史保存记录', none: '写入未验证' };
function time(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未记录'; }

export default function PlatformCapabilities({ refreshKey }: { refreshKey: string }) {
  const [data, setData] = useState<Inventory | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    void api<Inventory>('/capabilities').then(result => { if (active) setData(result); })
      .catch(reason => { if (active) { setError(reason instanceof Error ? reason.message : '能力记录读取失败'); setData(null); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refresh, refreshKey]);
  return <section className="panel capability-panel" aria-label="平台能力总览" aria-busy={loading}>
    <div className="panel-title"><div><h2>平台能力总览</h2><small>按内容形式、接入方式和保存位置查看</small></div>
      <button type="button" className="btn small" disabled={loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} />刷新记录</button></div>
    {error && <p className="callout warning" role="alert">{error}</p>}
    {loading && !data && <p role="status">正在读取本机能力记录…</p>}
    {data && <>
      <p className="muted-note">{data.notice}</p>
      {data.attention.available ? <p className="capability-attention">保存结果待核对 {data.attention.unknown_count} 份，等待交付处理 {data.attention.waiting_count} 份，其中重叠 {data.attention.overlap_count} 份，合计涉及 {data.attention.distinct_draft_count} 份草稿。请在草稿页逐项核对，结果不明时不要重复创建。</p> : <p className="callout warning">暂时无法读取草稿记录，历史数量未知。</p>}
      <div className="capability-grid">{data.channels.map(channel => <article key={channel.id} className="capability-card">
        <div className="capability-heading"><h3>{channel.label}</h3><span className={`tag ${channel.implemented ? 'muted' : 'amber'}`}>{channel.implemented ? '已有实现' : '尚未接入'}</span></div>
        <strong className="capability-scope">{scopes[channel.storage_scope] ?? '保存范围未验证'}</strong>
        <dl><div><dt>登录观察</dt><dd>{auth[channel.auth_state] ?? '当前登录未确认'}</dd></div>
          <div><dt>读取证据</dt><dd>{reads[channel.read_evidence] ?? '读取未验证'}</dd></div>
          <div><dt>写入证据</dt><dd>{writes[channel.write_evidence] ?? '写入未验证'}</dd></div>
          <div><dt>已保存记录</dt><dd>{channel.history.available ? `${channel.history.saved_record_count} 份（所有历史账号）` : '未知'}</dd></div></dl>
        <p>{channel.note}</p>
        {channel.connection_checked_at && <small>连接检查：{time(channel.connection_checked_at)}</small>}
        {channel.history.latest_record_updated_at && <small>保存记录最近更新：{time(channel.history.latest_record_updated_at)}</small>}
      </article>)}</div>
      <p className="subtle-note">记录汇总时间：{time(data.generated_at)}。此视图不会改变交付通道或授权发布。</p>
    </>}
  </section>;
}
