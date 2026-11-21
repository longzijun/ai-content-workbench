import { useEffect, useState } from 'react';

let pendingWorker: ServiceWorker | null = null;
export function registerWorkbenchApp() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  void navigator.serviceWorker.register('/service-worker.js').then(registration => {
    const notify = () => { if (registration.waiting) { pendingWorker = registration.waiting; window.dispatchEvent(new Event('workbench:update-ready')); } };
    notify(); registration.addEventListener('updatefound', () => { registration.installing?.addEventListener('statechange', notify); });
  }).catch(() => { /* Web operation remains available if installation is unsupported. */ });
}

export function ConnectivityNotice() {
  const [online, setOnline] = useState(navigator.onLine);
  const [update, setUpdate] = useState(!!pendingWorker);
  const [message, setMessage] = useState('');
  useEffect(() => {
    const connected = () => { setOnline(true); window.dispatchEvent(new Event('workbench:reconnected')); };
    const disconnected = () => setOnline(false);
    const updated = () => setUpdate(true);
    window.addEventListener('online', connected); window.addEventListener('offline', disconnected); window.addEventListener('workbench:update-ready', updated);
    return () => { window.removeEventListener('online', connected); window.removeEventListener('offline', disconnected); window.removeEventListener('workbench:update-ready', updated); };
  }, []);
  const installUpdate = () => {
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) { setMessage('先保存当前输入，再更新应用。'); return; }
    navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), { once: true });
    pendingWorker?.postMessage({ type: 'ACTIVATE_UPDATE' });
  };
  if (online && !update && !message) return null;
  return <div className="connectivity-notice" role="status">{!online && <span>网络已断开。新修改尚未同步，审核和发布登记须联网后操作。</span>}{update && <><span>工作台有可用更新。</span><button type="button" disabled={!online} onClick={installUpdate}>保存后更新</button></>}{message && <span>{message}</span>}</div>;
}
