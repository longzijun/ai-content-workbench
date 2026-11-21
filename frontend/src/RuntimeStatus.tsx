import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import type { Settings } from './types';

interface ComponentStatus { state: string; heartbeat_at: string; online: boolean; detail: { last_success_at?: string; last_error?: string | null; last_error_at?: string } }
interface RuntimeSnapshot {
  schema_ready: boolean; worker_online: boolean; model_busy: boolean; startup_mode: string; requires_awake_host: boolean;
  queue: Record<string, number>; components: Record<string, ComponentStatus>;
  backup: { last_success_at: string | null; last_error: string | null; verified_backups: number; off_disk_copy: boolean; copy_verified: boolean; retention: { daily: number; weekly: number } };
}
const componentNames: Record<string, string> = { worker: '后台执行器', scheduler: '生产调度', trends: '热点监测', backup: '自动备份', heartbeat: '模型协调心跳', production: '生产调度', monitor: '热点监测' };
const queueNames: Record<string, string> = { queued: '排队', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', pending: '待处理' };
const stamp = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '尚无记录';

export default function RuntimeStatus({ settings, onSettings, modelAvailable }: { settings: Settings; onSettings: (settings: Settings) => void; modelAvailable?: boolean }) {
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);
  const [error, setError] = useState('');
  const [directory, setDirectory] = useState(settings.backup_copy_dir || '');
  const [savedDirectory, setSavedDirectory] = useState(settings.backup_copy_dir || '');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const refresh = useCallback(async () => {
    try { setSnapshot(await api<RuntimeSnapshot>('/runtime-status')); setError(''); }
    catch (failure) { setError(failure instanceof Error ? failure.message : '运行状态读取失败'); }
  }, []);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 10000); return () => window.clearInterval(timer); }, [refresh]);
  const saveDirectory = async () => {
    setSaving(true); setMessage('');
    try { const saved = await api<Settings>('/settings', 'POST', { backup_copy_dir: directory.trim() }); onSettings(saved); setSavedDirectory(saved.backup_copy_dir || ''); setDirectory(saved.backup_copy_dir || ''); setMessage(saved.backup_copy_dir ? '副本目录已保存，下一次自动备份时验证。' : '副本目录已清空，本机自动备份继续保留。'); await refresh(); }
    catch (failure) { setMessage(failure instanceof Error ? failure.message : '目录未保存'); }
    finally { setSaving(false); }
  };
  const backup = snapshot?.backup;
  const copyLabel = backup?.off_disk_copy ? '异盘副本已验证' : backup?.copy_verified ? '副本文件已校验，异盘身份待核实' : savedDirectory ? '已配置副本目录，尚未完成副本验证' : '未配置副本目录，尚无异盘验证';
  return <section className="panel runtime-status"><div className="panel-title"><div><h2>运行与备份状态</h2><small>页面、生成器和备份分别显示，不因模型离线把全站标为故障。</small></div><button type="button" className="btn small" onClick={() => void refresh()}>刷新状态</button></div>
    <div className="runtime-status-grid"><div><span>页面服务</span><strong>{error ? '状态读取需重试' : snapshot ? '可用' : '读取中…'}</strong></div><div><span>后台执行器</span><strong>{snapshot ? snapshot.worker_online ? '在线' : '离线 · 编辑仍可使用' : '待读取'}</strong></div><div><span>本地模型</span><strong>{modelAvailable === false ? '未就绪 · 可继续编辑' : snapshot ? snapshot.model_busy ? '正在处理任务' : '执行器空闲' : '待读取'}</strong></div><div><span>排队 / 执行中</span><strong>{snapshot ? `${snapshot.queue.queued || 0} / ${snapshot.queue.running || 0}` : '—'}</strong></div></div>
    {error && <p className="form-error" role="alert">{error}</p>}{snapshot && !snapshot.schema_ready && <p className="callout warning">运行数据需要升级，请由维护者检查迁移状态。</p>}
    {snapshot && <><p className="muted-note">启动方式：{snapshot.startup_mode === 'user_login' ? 'Windows 用户登录后运行' : snapshot.startup_mode}。{snapshot.requires_awake_host && '电脑需保持开机、联网且未休眠。'}</p><p className="muted-note">任务：{Object.entries(snapshot.queue).map(([state, count]) => `${queueNames[state] || state} ${count}`).join(' · ') || '暂无任务'}</p><div className="runtime-components">{Object.entries(snapshot.components).map(([name, component]) => <div className="runtime-component" key={name}><div><strong>{componentNames[name] || name}</strong><span className={`tag ${component.online ? 'green' : 'amber'}`}>{component.online ? component.state === 'degraded' ? '运行异常' : '在线' : '离线'}</span></div><small>最近心跳：{stamp(component.heartbeat_at)} · 最近成功：{stamp(component.detail.last_success_at)}</small>{component.detail.last_error && <p className="form-error">{component.detail.last_error}<small>异常时间：{stamp(component.detail.last_error_at)}</small></p>}</div>)}</div></>}
    <div className="runtime-backup"><h3>自动备份</h3><p>最近成功：{stamp(backup?.last_success_at)}</p><p>已校验备份：{backup?.verified_backups ?? '—'} 份{backup?.retention && ` · 保留 ${backup.retention.daily} 个日备份与 ${backup.retention.weekly} 个周备份`}</p><p>{copyLabel}</p>{backup?.last_error && <p className="form-error" role="alert">最近备份错误：{backup.last_error}</p>}<label className="field"><span>备份副本目录（工作台电脑上的绝对目录）</span><input value={directory} onChange={event => setDirectory(event.target.value)} disabled={saving} placeholder="留空表示尚未配置" autoComplete="off" /><small>目录位于工作台电脑，不是手机。不同盘符也可能属于同一物理磁盘，实际异盘保护需核实。</small></label><button type="button" className="btn" disabled={saving || directory.trim() === savedDirectory} onClick={() => void saveDirectory()}>{saving ? '保存中…' : '保存副本目录'}</button>{message && <p role="status">{message}</p>}</div>
  </section>;
}
