import { useEffect, useState } from 'react';
import { api, setToken } from './api';

export type AuthStatus = { configured: boolean; authenticated: boolean };
type DeviceSession = { id: string; device_name: string; created_at: string; expires_at: string; current: boolean };

export function LoginPanel({ status, onLoggedIn, onCheck, expired }: { status: AuthStatus | null; onLoggedIn: () => Promise<void>; onCheck: () => Promise<void>; expired: boolean }) {
  const [password, setPassword] = useState('');
  const [device, setDevice] = useState(/Android|iPhone|iPad/i.test(navigator.userAgent) ? '我的手机' : '我的电脑');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const login = async () => {
    if (!password || busy) return;
    setBusy(true); setError('');
    try { await api('/auth/login', 'POST', { password, device_name: device.trim() || '我的设备' }); setPassword(''); await onLoggedIn(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : '登录未完成，请重试。'); }
    finally { setBusy(false); }
  };
  return <div className="auth-screen" role="dialog" aria-modal="true" aria-labelledby="auth-title"><section className="auth-card"><span className="auth-brand">AI 内容工作台</span><h1 id="auth-title">登录运营工作台</h1><p>{expired ? '会话已到期。重新登录后继续，当前页面中的未保存输入仍保留。' : '电脑和手机共用同一份工作内容。'}</p>
    {status?.configured === false ? <div className="callout warning"><strong>请先在电脑完成首次密码设置</strong><p>由工作台维护者在电脑上运行初始化工具，再回到这里登录。</p><button type="button" className="btn" onClick={() => void onCheck()}>我已设置，重新检查</button></div> : <form onSubmit={event => { event.preventDefault(); void login(); }}><label className="field"><span>工作台密码</span><input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} autoFocus disabled={busy} required /></label><label className="field"><span>这台设备的名称</span><input value={device} maxLength={80} autoComplete="off" onChange={event => setDevice(event.target.value)} disabled={busy} /></label><button className="btn primary" type="submit" disabled={busy || !password}>{busy ? '登录中…' : '登录'}</button></form>}
    {error && <p className="form-error" role="alert">{error}</p>}<p className="auth-note">请使用你自己的访问地址。模型和任务仍在电脑上运行。</p></section></div>;
}

export function DeviceSessions() {
  const [sessions, setSessions] = useState<DeviceSession[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = async () => { try { setSessions((await api<{ sessions: DeviceSession[] }>('/auth/sessions')).sessions); setError(''); } catch (failure) { setError(String(failure)); } };
  useEffect(() => { void load(); }, []);
  const revoke = async (session: DeviceSession) => {
    if (session.current && !window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true })) && !window.confirm('还有未保存修改。确认撤销当前登录并放弃这些输入？')) return;
    if (!window.confirm(`撤销“${session.device_name}”的登录？该设备下次操作需要重新登录。`)) return;
    setBusy(true);
    try { await api(`/auth/sessions/${encodeURIComponent(session.id)}`, 'DELETE'); if (session.current) { setToken(''); location.reload(); } else await load(); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  };
  const logout = async () => {
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true })) && !window.confirm('还有未保存修改。确认退出并放弃这些输入？')) return;
    setBusy(true);
    try { await api('/auth/logout', 'POST', {}); setToken(''); location.reload(); }
    catch (failure) { setError(String(failure)); setBusy(false); }
  };
  return <section className="panel device-sessions"><div className="panel-title"><h2>登录设备</h2><button type="button" className="btn small" disabled={busy} onClick={() => void load()}>刷新设备</button></div><p className="muted-note">每台设备独立登录。撤销其他设备不会退出当前设备。</p>{sessions.map(session => <div className="device-session" key={session.id}><div><strong>{session.device_name || '未命名设备'}{session.current && ' · 当前设备'}</strong><small>有效期至 {new Date(session.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small></div><button type="button" className="btn small" disabled={busy} onClick={() => void revoke(session)}>撤销登录</button></div>)}{!sessions.length && !error && <p>正在读取登录设备…</p>}{error && <p className="form-error" role="alert">{error}</p>}<button type="button" className="btn" disabled={busy} onClick={() => void logout()}>退出当前设备</button></section>;
}
