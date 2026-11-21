import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import { PLATFORM_NAMES, isVideoPlatform } from './platforms';
import { VideoPreview } from './VideoScript';
import type { ImageAsset, ImageAssetManifest, Platform, Work } from './types';

type Entry = { platform: Platform; work: Work | null; status_label?: string; assets?: ImageAsset[] | ImageAssetManifest; draft?: { id: string; account_label?: string; ai_disclosure?: string } | null; review?: { fingerprint?: string; approved: boolean; blocking?: string[]; latest?: { account_label?: string; ai_disclosure?: string } | null } };
type Preparation = { version_id: string | null; fact_base_revision: number; ready: boolean; blocking: string[]; platforms: Entry[] };
type HistoryItem = { id: string; version_id: string; zip_path?: string };
type Props = { topicId: string; currentVersionId?: string; currentFactRevision?: number; currentReviewFingerprints?: Record<string, string>; canDeliver?: boolean };

export default function PublishPreparation({ topicId, currentVersionId, currentFactRevision, currentReviewFingerprints, canDeliver = true }: Props) {
  const [data, setData] = useState<Preparation | null>(null);
  const [active, setActive] = useState<Platform | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [fallback, setFallback] = useState('');
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const fingerprintKey = JSON.stringify(currentReviewFingerprints || {});
  const load = useCallback(async () => {
    try { const next = await api<Preparation>(`/topics/${topicId}/publish-preparation`); setData(next); setActive(previous => next.platforms.some(entry => entry.platform === previous) ? previous : next.platforms[0]?.platform || null); setError(''); }
    catch (failure) { setError(String(failure)); setData(null); }
  }, [topicId]);
  useEffect(() => { void load(); }, [load, currentVersionId, currentFactRevision, fingerprintKey]);
  const current = !!data && (!currentVersionId || currentVersionId === data.version_id) && (currentFactRevision === undefined || currentFactRevision === data.fact_base_revision);
  const ready = !!data?.ready && current && canDeliver;
  const entry = data?.platforms.find(item => item.platform === active);
  const assets = Array.isArray(entry?.assets) ? entry.assets : entry?.assets?.assets || [];
  const validateReady = async (): Promise<Entry> => {
    if (!ready || !entry?.work) throw new Error('请先完成当前版本审阅。');
    const fresh = await api<Preparation>(`/topics/${topicId}/publish-preparation`);
    const selected = fresh.platforms.find(item => item.platform === active);
    const identity = (item: Entry) => JSON.stringify({ work: item.work, fingerprint: item.review?.fingerprint });
    if (!fresh.ready || fresh.version_id !== data?.version_id || fresh.fact_base_revision !== data.fact_base_revision || !selected?.work || identity(selected) !== identity(entry)) {
      setData(fresh); setFallback(''); throw new Error('正式稿或审阅状态已变化，请重新核对发布准备。');
    }
    return selected;
  };
  const copy = async (kind: 'title' | 'body' | 'html') => {
    let selected: Entry;
    try { selected = await validateReady(); } catch (failure) { setMessage(String(failure)); setFallback(''); return; }
    const work = selected.work!;
    const text = kind === 'title' ? work.title : work.text || work.body;
    try {
      if (kind === 'html' && work.html && globalThis.ClipboardItem && navigator.clipboard?.write) await navigator.clipboard.write([new ClipboardItem({ 'text/html': new Blob([work.html], { type: 'text/html' }), 'text/plain': new Blob([text], { type: 'text/plain' }) })]);
      else await navigator.clipboard.writeText(text);
      setMessage('已复制。发布后请到发布台账登记。'); setFallback('');
    } catch { setMessage('浏览器未完成复制，请长按选择下方文本。'); setFallback(text); }
  };
  const saveImage = async (asset: ImageAsset, index: number) => {
    if (!ready || !asset.url?.startsWith('/api/')) return;
    try { await validateReady(); const response = await fetch(asset.url, { credentials: 'same-origin', cache: 'no-store' }); if (!response.ok) throw new Error('图片读取失败'); const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `${String(index + 1).padStart(2, '0')}-${asset.filename || asset.id}.jpg`; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 10000); setMessage('已请求保存图片。若手机只打开预览，请长按图片保存。'); }
    catch (failure) { setMessage(String(failure)); }
  };
  const loadHistory = async () => { try { setHistory((await api<{ items: HistoryItem[] }>(`/topics/${topicId}/material-packages`)).items); } catch (failure) { setMessage(String(failure)); } };
  return <section className="panel publish-preparation"><div className="panel-title"><div><h2>发布准备</h2><small>{data?.version_id ? `正式版本 ${data.version_id.slice(0, 8)} · 底稿 R${data.fact_base_revision}` : '等待正式作品'}</small></div><button type="button" className="btn small" onClick={() => void load()}>重新核对</button></div>
    {error && <p className="callout error" role="alert">{error}</p>}
    <p className={`callout ${ready ? '' : 'warning'}`}>{ready ? '所选平台已审阅。复制内容或保存素材后，在官方应用人工发布。' : '先完成当前正式版本的全部平台审阅。复制或下载不会被记为已发布。'}</p>
    {!current && data && <p className="studio-review-warning">当前打开的版本或底稿不是最新基线，请先核对。</p>}
    {!!data?.blocking?.length && <ul>{data.blocking.map((item, i) => <li key={i}>{item}</li>)}</ul>}
    <div className="platform-picker">{data?.platforms.map(item => <button type="button" className={`btn small ${active === item.platform ? 'primary' : ''}`} key={item.platform} onClick={() => { setActive(item.platform); setMessage(''); setFallback(''); }}>{PLATFORM_NAMES[item.platform]}</button>)}</div>
    {entry?.work && <div className="publish-platform"><h3>{PLATFORM_NAMES[entry.platform]} · {entry.status_label || (ready ? '发布准备就绪' : '待完成审阅')}</h3><p className="muted-note">账号：{entry.draft?.account_label || entry.review?.latest?.account_label || '待填写'} · AI 标识：{entry.draft?.ai_disclosure || entry.review?.latest?.ai_disclosure || '待确认'}</p>
      {isVideoPlatform(entry.platform) ? ready ? <VideoPreview work={entry.work} beforeCopy={async () => { await validateReady(); }} /> : <p className="muted-note">可在工坊预览和改稿，通过审阅后复制拍摄脚本。</p> : <><h4>{entry.work.title}</h4><div className="actions"><button type="button" className="btn small" disabled={!ready} onClick={() => void copy('title')}>复制标题</button><button type="button" className="btn small" disabled={!ready} onClick={() => void copy('body')}>复制纯文本</button>{entry.work.html && <button type="button" className="btn small" disabled={!ready} onClick={() => void copy('html')}>复制富文本</button>}</div><div className="preparation-text">{entry.work.body}</div></>}
      {!!assets.length && <div className="preparation-assets">{assets.map((asset, index) => <figure key={asset.id}>{asset.url?.startsWith('/api/') && <img src={asset.url} alt={asset.caption || `图片 ${index + 1}`} loading="lazy" />}<figcaption>{String(index + 1).padStart(2, '0')} · {asset.caption || (index === 0 ? '封面候选' : '正文图片')}<div><button type="button" className="btn small" disabled={!ready} onClick={() => void saveImage(asset, index)}>保存图片</button>{asset.url?.startsWith('/api/') && <a href={asset.url} target="_blank" rel="noreferrer">打开原图</a>}</div></figcaption></figure>)}</div>}
    </div>}
    {message && <p role="status">{message}</p>}{fallback && <label className="field"><span>浏览器未完成复制，可长按选中下方文本</span><textarea rows={7} value={fallback} readOnly onFocus={event => event.currentTarget.select()} /></label>}
    <details onToggle={event => { if (event.currentTarget.open) void loadHistory(); }}><summary>历史 ZIP 归档（只读）</summary><p className="muted-note">历史材料包保留溯源，日常发布使用上方准备页。</p>{history.length ? history.map(item => <p key={item.id}>版本 {item.version_id.slice(0, 8)} · <a href={`/api/material-packages/${encodeURIComponent(item.id)}/download`}>下载历史归档</a></p>) : <p className="muted-note">暂无历史材料包。</p>}</details>
  </section>;
}
