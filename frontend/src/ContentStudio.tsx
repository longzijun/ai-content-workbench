import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, ArrowRight, BookOpen, Check, CheckCircle2, ChevronDown, Copy, Download, Eye, EyeOff, FileText, History, LoaderCircle, PanelLeftClose, PanelLeftOpen, Save, Sparkles, Undo2, X } from 'lucide-react';
import { api } from './api';
import CandidateStudio from './CandidateStudio';
import { FactBasePanel, ProducePanel } from './V3Panels';
import { PLATFORM_NAMES as NAMES, isVideoPlatform } from './platforms';
import { VideoFields, VideoPreview, videoBody } from './VideoScript';
import type { Platform, Topic, Version, Work } from './types';
import './content-studio.css';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;
type Editable = Pick<Work, 'title' | 'body' | 'tags' | 'source_urls' | 'cover_text' | 'shots' | 'target_duration_seconds'>;
type StudioDraft = Editable & { revision: number; updated_at: string | null; body_fingerprint: string; stale: boolean };
type Suggestion = { id: string; before: string; after: string; reason: string; provider: string; work_revision: number; body_fingerprint: string; advisories?: string[]; shot_id?: string; field?: 'spoken_text' | 'visual_notes' };
type ReviewEntry = { platform: Platform; draft: { id: string } | null; review?: { approved: boolean; blocking: string[]; fingerprint?: string } };
type ReviewData = { version_id: string | null; fact_base: { revision: number } | null; gaps: string[]; platforms: ReviewEntry[] };
type Props = {
  topic: Topic | null; version: Version | null; topics: Topic[]; initialPlatform?: Platform | null;
  onPlatformChange?: (platform: Platform) => void;
  busy: string; run: Run; onVersion: (id: string) => void; onTopic: (id: string) => void;
  onPublish: () => void; onChooseTopic?: () => void; refresh?: () => void;
  handoff?: ReactNode; tools?: ReactNode; mode?: { platform_operations: boolean } | null;
};
type SaveState = 'loading' | 'clean' | 'dirty' | 'saving' | 'saved' | 'error';
type EditorHandle = { flush: () => Promise<StudioDraft | null>; requestSuggestion: () => void };

const editable = (work: Editable): Editable => ({ title: work.title, body: work.shots ? videoBody(work.shots) : work.body, tags: (work.tags || []).map(item => item.trim()).filter(Boolean), source_urls: (work.source_urls || []).map(item => item.trim()).filter(Boolean), ...(work.shots ? { cover_text: work.cover_text || '', shots: work.shots, target_duration_seconds: work.target_duration_seconds || 60 } : {}) });
const same = (a: Editable, b: Editable) => JSON.stringify(editable(a)) === JSON.stringify(editable(b));
const fingerprint = async (value: string) => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))).map(byte => byte.toString(16).padStart(2, '0')).join('');
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const stamp = (value?: string | null) => value ? new Date(value).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '';
const localImage = (value?: string) => value && (value.startsWith('/api/') || /^data:image\/(png|jpeg|webp);base64,/.test(value)) ? value : undefined;

/** 工作台三栏布局：拖拽宽度 + 面板显隐，本机记忆（localStorage，不占数据库）。 */
type StudioLayout = { left: number; right: number; hideLeft: boolean; hideRight: boolean };
const LAYOUT_KEY = 'workbench.studio.layout';
const DEFAULT_LAYOUT: StudioLayout = { left: 250, right: 350, hideLeft: false, hideRight: false };
function loadLayout(): StudioLayout {
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY);
    if (!raw) return DEFAULT_LAYOUT;
    const value = JSON.parse(raw);
    return {
      left: Math.min(480, Math.max(200, Number(value.left) || DEFAULT_LAYOUT.left)),
      right: Math.min(560, Math.max(260, Number(value.right) || DEFAULT_LAYOUT.right)),
      hideLeft: !!value.hideLeft, hideRight: !!value.hideRight,
    };
  } catch { return DEFAULT_LAYOUT; }
}
function useStudioLayout() {
  const [layout, setLayout] = useState<StudioLayout>(loadLayout);
  useEffect(() => { try { window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* 隐私模式等写入失败时静默，布局当次有效 */ } }, [layout]);
  return { layout, setLayout };
}
function useSplitterDrag(deskRef: { current: HTMLDivElement | null }, setLayout: React.Dispatch<React.SetStateAction<StudioLayout>>) {
  return (side: 'left' | 'right') => (event: React.PointerEvent<HTMLDivElement>) => {
    const desk = deskRef.current;
    if (!desk) return;
    event.preventDefault();
    const splitter = event.currentTarget;
    splitter.classList.add('dragging');
    const startX = event.clientX;
    const current = loadLayout();
    const startWidth = side === 'left' ? current.left : current.right;
    const sign = side === 'left' ? 1 : -1;
    const move = (e: PointerEvent) => {
      const next = Math.round(startWidth + sign * (e.clientX - startX));
      const clamped = side === 'left' ? Math.min(480, Math.max(200, next)) : Math.min(560, Math.max(260, next));
      desk.style.setProperty(side === 'left' ? '--studio-col-left' : '--studio-col-right', `${clamped}px`);
      setLayout(previous => ({ ...previous, [side]: clamped }));
    };
    const up = () => {
      splitter.classList.remove('dragging');
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
}


export default function ContentStudio(props: Props) {
  if (!props.topic) return <section className="studio-empty panel"><BookOpen size={32} /><h2>从一个选题开始创作</h2><p>资料、正文、平台预览与审阅，都在这张工作桌上完成。</p><div className="studio-topic-options">{props.topics.filter(t => t.status === 'approved').map(t => <button className="studio-topic-card" key={t.id} onClick={() => props.onTopic(t.id)}><strong>{t.title}</strong><span>{t.current_version ? `继续编辑 V${t.current_version.number}` : '整理资料，开始生产'}<ArrowRight size={14} /></span></button>)}</div>{!props.topics.some(t => t.status === 'approved') && <p className="muted-note">还没有已批准选题。先确认写作方向与材料。</p>}<button className="btn primary" onClick={props.onChooseTopic}>前往选题中心</button></section>;
  return <CandidateStudio key={props.topic.id} topic={props.topic} version={props.version} busy={props.busy} onVersion={props.onVersion} onChooseTopic={props.onChooseTopic} refresh={props.refresh} initialPlatform={props.initialPlatform} onPlatformChange={props.onPlatformChange} official={<StudioTopic key={`${props.topic.id}:${props.version?.id || 'new'}`} {...props} topic={props.topic} />} />;
}

function StudioTopic({ topic, version, topics, initialPlatform, onPlatformChange, busy, run, onVersion, onTopic, onPublish, refresh, handoff, tools, mode }: Props & { topic: Topic }) {
  const [platform, setPlatform] = useState<Platform>(initialPlatform && topic.platforms.includes(initialPlatform) ? initialPlatform : topic.platforms[0] || 'wechat');
  const [materialsOpen, setMaterialsOpen] = useState(false);
  const [materialsDirty, setMaterialsDirty] = useState(false);
  const [mobileTab, setMobileTab] = useState<'editor' | 'preview'>('editor');
  const [review, setReview] = useState<ReviewData | null>(null);
  const [reviewError, setReviewError] = useState('');
  const [platformAccess, setPlatformAccess] = useState<{ platforms: { platform: Platform; enabled: boolean; account_label: string; blocked: boolean }[]; global_released: boolean } | null>(null);
  const [reviewTick, setReviewTick] = useState(0);
  const [changed, setChanged] = useState<Partial<Record<Platform, boolean>>>({});
  const [highlight, setHighlight] = useState<{ platform: Platform; text: string } | null>(null);
  const highlightTimer = useRef<number | null>(null);
  const [message, setMessage] = useState('');
  const [committing, setCommitting] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [editorPending, setEditorPending] = useState(false);
  const handle = useRef<EditorHandle | null>(null);
  const factRevisionSeen = useRef<number | null>(null);
  const deskRef = useRef<HTMLDivElement | null>(null);
  const { layout, setLayout } = useStudioLayout();
  const startDrag = useSplitterDrag(deskRef, setLayout);
  const historical = !!version && version.id !== topic.current_version?.id;
  const enteredCurrent = useRef(!historical);
  const locallyEdited = useRef(false);
  const activeJobs = (topic.jobs || []).filter(job => ['queued', 'running'].includes(job.state));
  const showMaterials = () => {
    setLayout(current => ({ ...current, hideLeft: false }));
    setMaterialsOpen(true);
    window.requestAnimationFrame(() => {
      const panel = deskRef.current?.querySelector<HTMLElement>('.studio-materials');
      panel?.scrollIntoView({ block: 'nearest' });
      panel?.querySelector<HTMLElement>('select, textarea, input')?.focus();
    });
  };
  const work = version?.payload.works.find(item => item.platform === platform);
  const hasChanges = Object.values(changed).some(Boolean);
  const activeEntry = review?.platforms.find(entry => entry.platform === platform);
  const approvedCount = review?.platforms.filter(entry => entry.review?.approved).length || 0;

  const loadReview = useCallback(async () => {
    try {
      const data = await api<ReviewData>(`/topics/${topic.id}/review`);
      const revision = data.fact_base?.revision || 0;
      if (factRevisionSeen.current !== null && revision !== factRevisionSeen.current) setMessage(old => old || '共享底稿已在另一处更新，请重新核对本篇资料与当前审阅；未保存输入仍保留。');
      factRevisionSeen.current = revision; setReview(data); setReviewError('');
    }
    catch (error) { setReviewError(String(error)); }
  }, [topic.id]);
  useEffect(() => { void loadReview(); }, [loadReview, reviewTick]);
  useEffect(() => { void api<{ platforms: { platform: Platform; enabled: boolean; account_label: string; blocked: boolean }[]; global_released: boolean }>('/platform-access').then(setPlatformAccess).catch(() => {}); }, []);
  useEffect(() => { const timer = window.setInterval(() => { void loadReview(); }, 4000); return () => window.clearInterval(timer); }, [loadReview]);
  useEffect(() => {
    if (!activeJobs.length) return;
    const timer = window.setInterval(() => { refresh?.(); void loadReview(); }, 3000);
    return () => window.clearInterval(timer);
  }, [activeJobs.length, refresh, loadReview]);
  useEffect(() => {
    if (!version || historical) return;
    let current = true;
    void Promise.all(topic.platforms.map(async p => {
      const original = version.payload.works.find(w => w.platform === p);
      if (!original || p === platform) return;
      try {
        const draft = await api<StudioDraft>(`/topics/${topic.id}/studio-draft/${p}?base_version_id=${encodeURIComponent(version.id)}`);
        if (current) setChanged(previous => ({ ...previous, [p]: !same(editable(original), draft) }));
      } catch { /* The active editor reports load errors; unopened platforms stay unavailable to commit. */ }
    }));
    return () => { current = false; };
  }, [topic.id, version, historical, platform]);

  const flushAnd = async (action: () => void) => {
    if (switching || committing) return;
    setSwitching(true);
    try {
      if (handle.current && !await handle.current.flush()) { setMessage('当前工作稿尚未保存成功，请先重试保存。'); return; }
      if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) { setMessage('请先保存本篇资料或处理未保存的工作稿，再切换。'); setMaterialsOpen(true); return; }
      action();
    } finally { setSwitching(false); }
  };
  const saveVersion = async () => {
    if (!version || historical || committing) return;
    setCommitting(true); setMessage('');
    try {
      if (handle.current && !await handle.current.flush()) throw new Error('工作稿尚未保存成功，请先处理保存错误。');
      if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) throw new Error('请先保存本篇资料，再保存正式版本。');
      const drafts = await Promise.all(topic.platforms.map(async p => ({ platform: p, draft: await api<StudioDraft>(`/topics/${topic.id}/studio-draft/${p}?base_version_id=${encodeURIComponent(version.id)}`) })));
      if (drafts.some(item => item.draft.stale)) throw new Error('已出现更新版本，请保留工作稿并刷新后对照。');
      const expected_revisions = Object.fromEntries(drafts.map(item => [item.platform, item.draft.revision]));
      const result = await api<{ version: Version }>(`/topics/${topic.id}/studio-commit`, 'POST', { base_version_id: version.id, expected_revisions });
      onVersion(result.version.id); refresh?.(); setMessage('全部工作稿已保存为新版本，请重新审阅。');
    } catch (error) { setMessage(String(error)); }
    finally { setCommitting(false); }
  };
  const openCurrentVersion = () => {
    if (!topic.current_version) return;
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) {
      setMessage('当前输入已保留。可先处理保存，或在新标签打开当前版本。'); return;
    }
    onPlatformChange?.(platform); onVersion(topic.current_version.id);
  };
  useEffect(() => {
    if (!historical || !enteredCurrent.current || locallyEdited.current || materialsDirty || editorPending || switching || committing || busy || !topic.current_version) return;
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) return;
    onPlatformChange?.(platform); onVersion(topic.current_version.id);
  }, [historical, topic.current_version?.id, materialsDirty, editorPending, switching, committing, busy, platform, onVersion, onPlatformChange]);

  const restoreVersion = async () => {
    if (!version || !topic.current_version) return;
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) { setMessage('请先保存本篇资料，或下载尚未保存的正文，再恢复历史版本。'); setMaterialsOpen(true); return; }
    setMessage('');
    await run('恢复历史内容为新版本', async () => {
      const next = await api<Version>(`/topics/${topic.id}/versions`, 'POST', { payload: version.payload, base_version_id: topic.current_version!.id });
      onVersion(next.id); refresh?.();
    }, '历史内容已恢复为新版本，需重新审阅。');
  };
  const saveToPlatform = async () => {
    if (!version || historical) return;
    const access = platformAccess?.platforms.find(item => item.platform === platform);
    if (!access?.enabled || access.blocked) { setMessage('该平台草稿保存未启用，请先在系统设置逐平台/账号启用。'); return; }
    if (hasChanges || materialsDirty) { setMessage('请先保存本篇资料和工作稿为正式版本，再保存到平台草稿箱。'); return; }
    if (!activeEntry?.review?.approved) { setMessage('请先完成本平台审阅，再保存到平台草稿箱。'); return; }
    await run('保存到平台草稿箱', async () => {
      const draft = await api<{ id: string }>(`/versions/${version.id}/drafts`, 'POST', { platform, account_label: access.account_label });
      await api(`/drafts/${draft.id}/queue`, 'POST', {});
    }, '已请求保存到平台草稿箱，请在草稿箱核对真实回读结果。');
  };
  const platformClosed = !mode?.platform_operations;
  const platformAccessEntry = platformAccess?.platforms.find(item => item.platform === platform);
  const platformSaveDisabled = platformClosed ? '平台操作已关闭，请人工发布' : !platformAccessEntry ? '逐平台许可读取中' : platformAccessEntry.blocked ? '该平台保持关闭自动触达' : !platformAccessEntry.enabled ? '该平台草稿保存未启用' : historical ? '历史版本只读' : (hasChanges || materialsDirty) ? '请先保存为正式版本' : !activeEntry?.review?.approved ? '请先审阅通过' : '';
  const handleApplied = (text: string) => {
    setHighlight({ platform, text });
    if (highlightTimer.current) window.clearTimeout(highlightTimer.current);
    highlightTimer.current = window.setTimeout(() => setHighlight(null), 2600);
  };

  return <div className={`content-studio ${materialsOpen ? 'materials-open' : ''}`} onInputCapture={() => { locallyEdited.current = true; }}>
    <header className="studio-heading">
      <div className="studio-heading-copy"><div className="studio-eyebrow"><span className="studio-dot" />创作进行时<span>一份材料，多平台表达</span></div><h2>{topic.title}</h2><p>你的观点是主线，AI 只做你决定采用的那部分。</p></div>
      <div className="studio-context">
        <label><span>当前选题</span><select aria-label="当前选题" value={topic.id} onChange={event => { const value = event.currentTarget.value; void flushAnd(() => onTopic(value)); }}>{topics.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        {version && <label><span><History size={12} />版本记录</span><select aria-label="版本记录" value={version.id} onChange={event => { const value = event.currentTarget.value; void flushAnd(() => onVersion(value)); }}>{(topic.versions?.length ? topic.versions : [version]).map(item => <option key={item.id} value={item.id}>V{item.number}{item.id === topic.current_version?.id ? ' · 当前' : ' · 历史'}</option>)}</select></label>}
      </div>
    </header>
    <div className="studio-toolbar">
      <button className="studio-material-toggle" aria-expanded={materialsOpen} onClick={() => setMaterialsOpen(open => !open)}>{materialsOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}本篇资料</button>
      <p className="studio-desk-hint"><span className="studio-ink-dot" />你的文字 <span className="studio-marker-swatch" />AI 建议，等你决定</p>
      {version && <span className={`studio-version-badge ${historical ? 'history' : ''}`}>{historical ? '历史版本 · 只读' : '当前正式版本'} V{version.number}</span>}
      <div className="layout-toggles" role="group" aria-label="面板显隐">
        <button type="button" className={layout.hideLeft ? 'off' : ''} onClick={() => setLayout(current => ({ ...current, hideLeft: !current.hideLeft }))} title={layout.hideLeft ? '显示本篇资料栏' : '隐藏本篇资料栏'}>{layout.hideLeft ? <EyeOff size={12} /> : <Eye size={12} />}资料</button>
        <button type="button" className={layout.hideRight ? 'off' : ''} onClick={() => setLayout(current => ({ ...current, hideRight: !current.hideRight }))} title={layout.hideRight ? '显示预览与审阅栏' : '隐藏预览与审阅栏'}>{layout.hideRight ? <EyeOff size={12} /> : <Eye size={12} />}预览</button>
      </div>
    </div>
    {message && <div className="studio-message" role="status"><AlertCircle size={16} /><span>{message}</span><button aria-label="关闭提示" onClick={() => setMessage('')}><X size={14} /></button></div>}
    {activeJobs.map(job => <div className="studio-job" key={job.id}><LoaderCircle size={16} className="spin" /><div><strong>正在生产内容</strong><p>{job.events?.at(-1)?.message || job.stage || '请稍候，真实结果将在完成后显示。'}</p></div><button className="btn small" disabled={!!busy} onClick={() => void run('取消生产', () => api(`/jobs/${job.id}/cancel`, 'POST', {}), '已请求取消。')}>取消</button></div>)}
    {historical && <div className="studio-history-note"><History size={18} /><span>右侧展示历史正式版。恢复操作使用这份正式版，保留的工作稿可另行下载。</span><button className="btn small" disabled={!!busy || !!activeJobs.length} onClick={() => void restoreVersion()}>恢复此历史正式版本</button></div>}
    {historical && topic.current_version && <div className="callout warning" role="status">当前查看 V{version?.number}，最新正式版本为 V{topic.current_version.number}。<button className="btn small" onClick={openCurrentVersion}>打开当前版本 V{topic.current_version.number}</button><a className="btn small" href={`?page=projects&topic=${encodeURIComponent(topic.id)}&version=${encodeURIComponent(topic.current_version.id)}&platform=${platform}`} target="_blank" rel="noreferrer">在新标签打开当前版本</a></div>}
    <div className="studio-mobile-tabs"><button className={mobileTab === 'editor' ? 'active' : ''} onClick={() => setMobileTab('editor')}>正文编辑</button><button className={mobileTab === 'preview' ? 'active' : ''} onClick={() => setMobileTab('preview')}>预览与审阅</button></div>
    {materialsOpen && <button className="studio-material-scrim" aria-label="关闭资料侧栏" onClick={() => setMaterialsOpen(false)} />}
    <div ref={deskRef} className={`studio-desk mobile-${mobileTab}${layout.hideLeft ? ' hide-left' : ''}${layout.hideRight ? ' hide-right' : ''}`} style={{ '--studio-col-left': `${layout.left}px`, '--studio-col-right': `${layout.right}px` } as React.CSSProperties}>
      <aside className="studio-materials" aria-label="本篇资料"><div className="studio-section-caption"><BookOpen size={15} /><h3>本篇资料</h3><button aria-label="收起资料" onClick={() => setMaterialsOpen(false)}><PanelLeftClose size={15} /></button></div><div className="studio-materials-scroll"><FactBasePanel key={topic.id} topicId={topic.id} topicSources={topic.sources} onDirtyChange={setMaterialsDirty} onChanged={() => { factRevisionSeen.current = null; setReviewTick(tick => tick + 1); refresh?.(); }} /><details className="studio-source-list"><summary>选题原始来源 · {topic.sources.length}<ChevronDown size={13} /></summary>{topic.sources.map((source, index) => <div key={`${source.url}:${index}`}><strong>{source.title || '来源资料'}</strong><a href={/^https?:\/\//.test(source.url) ? source.url : undefined} target="_blank" rel="noreferrer">{source.url}</a>{source.note && <p>{source.note}</p>}</div>)}</details></div></aside>
      <div className="studio-splitter studio-splitter-left" role="separator" aria-orientation="vertical" aria-label="调整资料栏宽度" onPointerDown={startDrag('left')} />
      {work && version ? <StudioDraftScope key={`${topic.id}:${version.id}:${platform}`} topic={topic} version={version} work={work} readOnly={historical || topic.status !== 'approved'} locked={committing || switching || !!activeJobs.length} onPreview={() => {}} onApplied={handleApplied} onPendingChange={setEditorPending} onChanged={value => setChanged(old => ({ ...old, [platform]: value }))} register={value => { handle.current = value; }}>
        <section className="studio-editor-column" aria-label="正文编辑">
          <DraftEditor />
          {handoff && <fieldset className="studio-handoff studio-operation-group" disabled={materialsDirty || hasChanges || editorPending || historical}>{(materialsDirty || hasChanges || editorPending) && <p className="studio-review-warning">先保存本篇资料，并将修改中的工作稿保存为正式版本，再导出或导入全文任务包。</p>}{historical && <p className="studio-review-warning">切回当前版本后，可继续全文交接生成。</p>}{handoff}</fieldset>}
          {tools && <fieldset className="studio-tools studio-operation-group" disabled={materialsDirty || hasChanges || editorPending}>{(materialsDirty || hasChanges || editorPending) && <p className="studio-review-warning">请先保存本篇资料和工作稿为正式版本，再执行会切换版本的工具。</p>}<details><summary>版本、素材与资料库工具</summary>{tools}</details></fieldset>}
        </section>
        <div className={`studio-prism-beams active-${platform}`} aria-hidden="true"><svg viewBox="0 0 48 240" preserveAspectRatio="none"><path d="M0 138h12" stroke="#1c2230" /><path className="wechat" d="M12 138C30 138 24 62 48 62" stroke="#2ba471" /><path className="toutiao" d="M12 138C30 138 28 120 48 120" stroke="#ee7a4b" /><path className="xiaohongshu" d="M12 138C30 138 25 178 48 178" stroke="#e5484d" /><circle cx="12" cy="138" r="3.5" fill="#1c2230" /></svg></div>
        <div className="studio-splitter studio-splitter-right" role="separator" aria-orientation="vertical" aria-label="调整预览栏宽度" onPointerDown={startDrag('right')} />
        <aside className="studio-preview-column" aria-label="平台预览与审阅"><div className="studio-prism-tabs"><div className="studio-prism-caption"><span>PLATFORM EDITIONS</span><strong>多种表达，同一份事实</strong></div><div className="studio-platforms" role="tablist" aria-label="平台版本">{topic.platforms.map(p => <button type="button" role="tab" id={`studio-tab-${p}`} aria-controls="studio-platform-panel" aria-selected={p === platform} className={`${p} ${p === platform ? 'active' : ''}`} key={p} disabled={switching || committing} onClick={() => void flushAnd(() => { setPlatform(p); onPlatformChange?.(p); })}><span className={`platform-dot ${p}`} /><span>{NAMES[p]}<small>{!version?.payload.works.some(item => item.platform === p) ? '尚未生成' : historical ? '历史正式稿' : changed[p] ? '工作稿有修改' : review?.version_id === version?.id && review?.platforms.find(entry => entry.platform === p)?.review?.approved ? '已审阅' : '待审阅'}</small></span>{review?.version_id === version?.id && review?.platforms.find(entry => entry.platform === p)?.review?.approved && !changed[p] && <CheckCircle2 size={13} />}</button>)}</div><p>分别写给每个平台，不是简单删短。</p></div><div className="studio-platform-stage" id="studio-platform-panel" role="tabpanel" aria-labelledby={`studio-tab-${platform}`}><StudioRightPanel platform={platform} work={work} version={version} historical={historical} highlight={highlight?.platform === platform ? highlight.text : undefined} reviewChildren={historical ? <p className="studio-preview-note">历史正式内容仅供查看，不能记录当前审阅。</p> : <PlatformReview key={`${version.id}:${platform}:${review?.fact_base?.revision || 0}`} topicId={topic.id} versionId={version.id} factRevision={review?.fact_base?.revision || 0} entry={review?.version_id === version.id ? activeEntry : undefined} blocked={materialsDirty || !!changed[platform] || !!activeJobs.length || committing || topic.status !== 'approved'} error={reviewError} onRecorded={() => { setReviewTick(tick => tick + 1); refresh?.(); }} />} /></div></aside>
      </StudioDraftScope> : <>
        <section className="studio-editor-column" aria-label="正文编辑"><div className="studio-no-work"><FileText size={38} /><h3>{topic.status === 'approved' ? '从共同事实开始，写出所选平台作品' : '先确认这次要写什么'}</h3><p>系统会自动整理选题资料，并生成所选平台的可编辑工作稿。</p>{materialsDirty && <p className="studio-review-warning">补充资料有未保存修改，请先保存；无需先确认整张资料表。</p>}{topic.status === 'approved' && <fieldset className="studio-operation-group" ><ProducePanel topicId={topic.id} allowedPlatforms={topic.platforms} refreshKey={reviewTick} materialsDirty={materialsDirty} active={!!activeJobs.length} onMaterials={showMaterials} onProduced={() => { refresh?.(); setReviewTick(tick => tick + 1); }} /></fieldset>}</div></section>
        <div className={`studio-prism-beams active-${platform}`} aria-hidden="true"><svg viewBox="0 0 48 240" preserveAspectRatio="none"><path d="M0 138h12" stroke="#1c2230" /><path className="wechat" d="M12 138C30 138 24 62 48 62" stroke="#2ba471" /><path className="toutiao" d="M12 138C30 138 28 120 48 120" stroke="#ee7a4b" /><path className="xiaohongshu" d="M12 138C30 138 25 178 48 178" stroke="#e5484d" /><circle cx="12" cy="138" r="3.5" fill="#1c2230" /></svg></div>
        <div className="studio-splitter studio-splitter-right" role="separator" aria-orientation="vertical" aria-label="调整预览栏宽度" onPointerDown={startDrag('right')} />
        <aside className="studio-preview-column" aria-label="平台预览与审阅"><div className="studio-prism-tabs"><div className="studio-prism-caption"><span>PLATFORM EDITIONS</span><strong>多种表达，同一份事实</strong></div><div className="studio-platforms" role="tablist" aria-label="平台版本">{topic.platforms.map(p => <button type="button" role="tab" id={`studio-tab-${p}`} aria-controls="studio-platform-panel" aria-selected={p === platform} className={`${p} ${p === platform ? 'active' : ''}`} key={p} disabled={switching || committing} onClick={() => void flushAnd(() => { setPlatform(p); onPlatformChange?.(p); })}><span className={`platform-dot ${p}`} /><span>{NAMES[p]}<small>{!version?.payload.works.some(item => item.platform === p) ? '尚未生成' : historical ? '历史正式稿' : changed[p] ? '工作稿有修改' : review?.version_id === version?.id && review?.platforms.find(entry => entry.platform === p)?.review?.approved ? '已审阅' : '待审阅'}</small></span>{review?.version_id === version?.id && review?.platforms.find(entry => entry.platform === p)?.review?.approved && !changed[p] && <CheckCircle2 size={13} />}</button>)}</div><p>分别写给每个平台，不是简单删短。</p></div><div className="studio-platform-stage"><div className="studio-preview-empty"><FileText size={28} /><p>真实生成完成后，平台版本会显示在这里。</p></div></div></aside>
      </>}
    </div>
    <footer className="studio-footer"><div className="studio-progress"><span className="studio-progress-title">本篇进度</span><span className={version ? 'complete' : ''}>{version ? <CheckCircle2 size={14} /> : <span className="studio-step-number">1</span>}整理资料</span><ArrowRight size={12} /><span className={version ? 'complete' : ''}>{version ? <CheckCircle2 size={14} /> : <span className="studio-step-number">2</span>}平台作品</span><ArrowRight size={12} /><span><span className="studio-step-number">3</span>审阅 {approvedCount}/{topic.platforms.length}</span></div><div className="studio-footer-actions">{hasChanges && <span>全部工作稿将一起保存，新版本需重新审阅</span>}{version && !historical && <button className="studio-primary" disabled={!hasChanges || !!busy || committing || !!activeJobs.length || switching} onClick={() => void saveVersion()}>{committing ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存全部工作稿为新版本</button>}{version && !historical && <button className="btn small" disabled={!!platformSaveDisabled || !!busy || committing || switching} title={platformSaveDisabled} onClick={() => void saveToPlatform()}>保存到平台草稿箱</button>}<button className="btn small" onClick={() => void flushAnd(onPublish)}>人工发布登记<ArrowRight size={14} /></button></div></footer>
  </div>;
}

type StudioDraftApi = {
  edit: Editable; change: (patch: Partial<Editable>) => void;
  tagsInput: string; setTagsInput: (value: string) => void;
  urlsInput: string; setUrlsInput: (value: string) => void;
  state: SaveState; error: string; stateLabel: string;
  suggestion: Suggestion | null; suggesting: boolean; accepting: boolean; justAccepted: boolean;
  directive: string; setDirective: (value: string | ((previous: string) => string)) => void;
  selection: { start: number; end: number } | null; setSelection: (value: { start: number; end: number } | null) => void;
  videoTarget: { shot_id: string; field: 'spoken_text' | 'visual_notes' } | null; setVideoTarget: (value: { shot_id: string; field: 'spoken_text' | 'visual_notes' } | null) => void;
  undo: { before: Editable; after: Editable } | null;
  requestSuggestion: () => Promise<void>; accept: () => Promise<void>; reject: () => void; undoSuggestion: () => void;
  flush: () => Promise<StudioDraft | null>; readLatest: () => Promise<void>; resolveConflict: (keepMine: boolean) => Promise<void>;
  conflictDraft: StudioDraft | null; retainedDraft: StudioDraft | null; downloadRecovery: (value?: Editable) => void;
  loaded: boolean; original: Editable; readOnly: boolean; locked: boolean; video: boolean; work: Work; topic: Topic; version: Version; suggestionCardRef: { current: HTMLDivElement | null };
};
const StudioDraftCtx = createContext<StudioDraftApi | null>(null);
const useDraft = () => {
  const ctx = useContext(StudioDraftCtx);
  if (!ctx) throw new Error('StudioDraft context 缺失');
  return ctx;
};

function useStudioDraft({ topic, version, work, readOnly, locked, onPreview, onApplied, onChanged, onPendingChange }: { topic: Topic; version: Version; work: Work; readOnly: boolean; locked: boolean; onPreview: (value: Editable) => void; onApplied: (text: string) => void; onChanged: (value: boolean) => void; onPendingChange: (value: boolean) => void }): StudioDraftApi {
  const original = editable(work);
  const [edit, setEdit] = useState<Editable>(original);
  const [tagsInput, setTagsInput] = useState(original.tags.join('\n'));
  const [urlsInput, setUrlsInput] = useState(original.source_urls.join('\n'));
  const [state, setState] = useState<SaveState>(readOnly ? 'clean' : 'loading');
  const [error, setError] = useState('');
  const [lastSaved, setLastSaved] = useState('');
  const [saveEpoch, setSaveEpoch] = useState(0);
  const [conflictDraft, setConflictDraft] = useState<StudioDraft | null>(null);
  const [retainedDraft, setRetainedDraft] = useState<StudioDraft | null>(null);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const [justAccepted, setJustAccepted] = useState(false);
  const [directive, setDirective] = useState('');
  const [selection, setSelection] = useState<{ start: number; end: number } | null>(null);
  const [videoTarget, setVideoTarget] = useState<{ shot_id: string; field: 'spoken_text' | 'visual_notes' } | null>(null);
  const [undo, setUndo] = useState<{ before: Editable; after: Editable } | null>(null);
  const suggestionRange = useRef<{ start: number; end: number } | null>(null);
  const suggestionCardRef = useRef<HTMLDivElement>(null);
  const editRef = useRef(edit);
  const savedRef = useRef<Editable>(original);
  const revision = useRef(0);
  const serverDraft = useRef<StudioDraft | null>(null);
  const saving = useRef<Promise<StudioDraft | null> | null>(null);
  const alive = useRef(true);
  const loaded = useRef(readOnly);
  const readOnlyRef = useRef(readOnly);
  const lockedRef = useRef(locked);
  const saveFailed = useRef(false);
  const editingTools = useRef(false);
  editingTools.current = suggesting || accepting || !!suggestion || !!directive.trim();
  const path = `/topics/${topic.id}/studio-draft/${work.platform}?base_version_id=${encodeURIComponent(version.id)}`;
  editRef.current = edit;
  readOnlyRef.current = readOnly;
  lockedRef.current = locked;

  const flush = useCallback(async (): Promise<StudioDraft | null> => {
    if (!loaded.current) return null;
    if (readOnlyRef.current) return serverDraft.current || { ...editRef.current, revision: 0, updated_at: null, body_fingerprint: '', stale: false };
    if (saving.current) { const result = await saving.current; if (!result) return null; return flush(); }
    if (same(editRef.current, savedRef.current) && serverDraft.current && revision.current > 0) { if (alive.current) setState('saved'); return serverDraft.current; }
    const sent = copy(editRef.current);
    if (alive.current) { setState('saving'); setError(''); }
    const pending = api<StudioDraft>(path, 'PUT', { ...sent, expected_revision: revision.current }).then(result => {
      revision.current = result.revision; serverDraft.current = result; savedRef.current = sent; saveFailed.current = false;
      if (alive.current) { setLastSaved(result.updated_at || ''); setSaveEpoch(epoch => epoch + 1); setState(same(editRef.current, sent) ? 'saved' : 'dirty'); }
      return result;
    }).catch((failure: unknown) => {
      saveFailed.current = true;
      if (alive.current) { setError(String(failure)); setState('error'); }
      return null;
    }).finally(() => { saving.current = null; });
    saving.current = pending;
    return pending;
  }, [path]);

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    void api<StudioDraft>(path).then(draft => {
      if (cancelled || loaded.current && !readOnlyRef.current) return;
      if (readOnlyRef.current) {
        loaded.current = true; serverDraft.current = draft;
        if (draft.revision > 0 && !same(original, draft)) setRetainedDraft(draft);
        return;
      }
      const next = editable(draft);
      revision.current = draft.revision; serverDraft.current = draft; savedRef.current = next; editRef.current = next;
      loaded.current = true; setEdit(next); setTagsInput(next.tags.join('\n')); setUrlsInput(next.source_urls.join('\n')); setState(draft.revision ? 'saved' : 'clean'); setLastSaved(draft.updated_at || '');
      if (draft.stale) { setState('error'); setError('这份工作稿的基础版本已经变化，请对照当前正式版本。'); }
    }).catch(failure => { if (!cancelled && !loaded.current) { setError(String(failure)); if (!readOnlyRef.current) setState('error'); } });
    return () => { cancelled = true; void flush(); alive.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  useEffect(() => {
    if (readOnly) return;
    let cancelled = false;
    let checking = false;
    const checkRemote = async () => {
      if (checking || document.visibilityState === 'hidden' || saving.current || lockedRef.current) return;
      checking = true;
      try {
        const draft = await api<StudioDraft>(path);
        if (cancelled || readOnlyRef.current || saving.current || lockedRef.current || loaded.current && draft.revision <= revision.current) return;
        if (draft.stale) {
          if (!same(editRef.current, savedRef.current)) {
            saveFailed.current = true;
            setState('error'); setError('已有新的正式版本，当前输入已保留，可在新标签打开当前版本。');
          }
          return;
        }
        if (!same(editRef.current, savedRef.current) || saveFailed.current || editingTools.current) {
          saveFailed.current = true;
          setConflictDraft(draft); setState('error');
          setError('另一台设备更新了这份工作稿。当前输入已保留，请对照服务器新稿后选择。');
          return;
        }
        const next = editable(draft);
        loaded.current = true; revision.current = draft.revision; serverDraft.current = draft; savedRef.current = next; editRef.current = next;
        setEdit(next); setTagsInput(next.tags.join('\n')); setUrlsInput(next.source_urls.join('\n'));
        setSelection(null); setUndo(null); setVideoTarget(null);
        setLastSaved(draft.updated_at || ''); setState('saved'); setError('');
      } catch {
        // Keep the current editor through a network interruption; retry on the next check.
      } finally { checking = false; }
    };
    const onVisible = () => { if (document.visibilityState === 'visible') void checkRemote(); };
    const timer = window.setInterval(() => { void checkRemote(); }, 4000);
    window.addEventListener('focus', onVisible);
    window.addEventListener('online', onVisible);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true; window.clearInterval(timer);
      window.removeEventListener('focus', onVisible);
      window.removeEventListener('online', onVisible);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [path, readOnly]);
  useEffect(() => { onPreview(edit); onChanged(!same(edit, original)); }, [edit]);
  useEffect(() => { onPendingChange(state === 'loading' || state === 'saving' || state === 'error' || editingTools.current || !same(edit, savedRef.current)); }, [edit, state, suggesting, accepting, suggestion, directive, onPendingChange]);
  useEffect(() => {
    if (!loaded.current || readOnly || state === 'saving' || same(edit, savedRef.current) || saveFailed.current) return;
    const timer = window.setTimeout(() => { void flush(); }, 1200);
    return () => window.clearTimeout(timer);
  }, [edit, readOnly, flush, state, saveEpoch]);
  useEffect(() => {
    const beforeLeave = (event: BeforeUnloadEvent) => {
      if (loaded.current && (!same(editRef.current, savedRef.current) || saving.current)) { event.preventDefault(); event.returnValue = ''; }
    };
    const beforeNavigate = (event: Event) => { if (loaded.current && (!same(editRef.current, savedRef.current) || saving.current)) event.preventDefault(); };
    window.addEventListener('beforeunload', beforeLeave);
    window.addEventListener('workbench:before-navigate', beforeNavigate);
    return () => { window.removeEventListener('beforeunload', beforeLeave); window.removeEventListener('workbench:before-navigate', beforeNavigate); };
  }, []);
  useEffect(() => {
    if (readOnly && loaded.current && !same(editRef.current, savedRef.current)) setError('基础版本或选题状态已变化。当前输入仍保留在页面中，请先下载恢复副本，再对照最新版本。');
  }, [readOnly]);

  const change = (patch: Partial<Editable>) => {
    saveFailed.current = false;
    const next = { ...editRef.current, ...patch }; if (next.shots) next.body = videoBody(next.shots); editRef.current = next; setEdit(next);
    setState(saving.current ? 'saving' : same(next, savedRef.current) ? (revision.current > 0 ? 'saved' : 'clean') : 'dirty'); setError('');
  };
  const requestSuggestion = async () => {
    if (readOnly || locked || suggesting || !directive.trim()) return;
    setSuggesting(true); setError(''); setSuggestion(null);
    try {
      const draft = await flush();
      if (!draft) throw new Error('请先保存工作稿，再请求修改建议。');
      const current = copy(editRef.current);
      const target = isVideoPlatform(work.platform) ? videoTarget || (current.shots?.[0] ? { shot_id: current.shots[0].id, field: 'spoken_text' as const } : null) : null;
      const targetText = target ? current.shots?.find(shot => shot.id === target.shot_id)?.[target.field] : current.body;
      if (targetText === undefined) throw new Error('分镜已变化，请重新选择目标。');
      const range = target ? { start: 0, end: targetText.length } : selection && selection.end > selection.start ? selection : { start: 0, end: current.body.length };
      const before = targetText.slice(range.start, range.end);
      if (!before.trim()) throw new Error('先写入正文，或选中希望修改的段落。');
      const result = await api<Suggestion>(path.replace('?base_version_id=', '/suggestions?base_version_id='), 'POST', {
        expected_revision: draft.revision, directive: directive.trim(), before,
        prefix: Array.from(targetText.slice(0, range.start)).slice(-80).join(''), suffix: Array.from(targetText.slice(range.end)).slice(0, 80).join(''), body_fingerprint: target ? await fingerprint(targetText) : draft.body_fingerprint, ...(target || {}),
      });
      if (!alive.current) return;
      if (!same(current, editRef.current)) throw new Error('正文在生成建议期间已变化，请重新请求建议。');
      suggestionRange.current = range;
      setSuggestion(result);
      requestAnimationFrame(() => { const card = suggestionCardRef.current; if (!card) return; card.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); card.focus({ preventScroll: true }); });
    } catch (failure) { if (alive.current) setError(String(failure)); }
    finally { if (alive.current) setSuggesting(false); }
  };
  const accept = async () => {
    if (!suggestion || accepting || readOnly || locked) return;
    setAccepting(true); setError('');
    try {
      await api(path.replace('?base_version_id=', `/suggestions/${encodeURIComponent(suggestion.id)}?base_version_id=`));
      if (!alive.current) return;
      if (readOnlyRef.current || lockedRef.current) throw new Error('已进入保存或切换，这条建议未采用，请稍后重试。');
      const current = editRef.current;
      const targetShot = suggestion.shot_id ? current.shots?.find(shot => shot.id === suggestion.shot_id) : undefined;
      const targetField = suggestion.field || 'spoken_text';
      const targetText = targetShot ? targetShot[targetField] : current.body;
      const fingerprintMatches = suggestion.shot_id ? (targetShot ? await fingerprint(targetText) === suggestion.body_fingerprint : false) : serverDraft.current?.body_fingerprint === suggestion.body_fingerprint;
      if (!alive.current) return;
      if (readOnlyRef.current || lockedRef.current) throw new Error('已进入保存或切换，这条建议未采用，请稍后重试。');
      if (!same(current, savedRef.current) || !same(current, editRef.current)) throw new Error('正文或分镜已变化，这条建议已过期。请重新生成。');
      if (!fingerprintMatches) throw new Error('正文或分镜已变化，这条建议已过期。请重新生成。');
      const range = suggestionRange.current;
      if (!range || targetText.slice(range.start, range.end) !== suggestion.before) throw new Error('原文位置已变化，请重新选中后生成建议。');
      const replacement = targetText.slice(0, range.start) + suggestion.after + targetText.slice(range.end);
      const next = suggestion.shot_id ? { ...current, shots: current.shots?.map(shot => shot.id === suggestion.shot_id ? { ...shot, [targetField]: replacement } : shot) } : { ...current, body: replacement };
      setUndo({ before: copy(current), after: copy(next) }); change(next); setSuggestion(null); setSelection(null); suggestionRange.current = null;
      setJustAccepted(true); onApplied(suggestion.after); window.setTimeout(() => { if (alive.current) setJustAccepted(false); }, 2600);
    } catch (failure) { if (alive.current) setError(String(failure)); }
    finally { if (alive.current) setAccepting(false); }
  };
  const undoSuggestion = () => {
    if (!undo) return;
    if (!same(editRef.current, undo.after)) { setError('接受建议后又有人工编辑，为保护新文字，已停止自动撤销。可参照原句手动修改。'); return; }
    change(undo.before); setUndo(null);
  };
  const readLatest = async () => {
    try { setConflictDraft(await api<StudioDraft>(path)); }
    catch (failure) { setError(String(failure)); }
  };
  const resolveConflict = async (keepMine: boolean) => {
    if (!conflictDraft || conflictDraft.stale) return;
    const latest = editable(conflictDraft);
    revision.current = conflictDraft.revision; serverDraft.current = conflictDraft; savedRef.current = latest;
    setConflictDraft(null); setError(''); saveFailed.current = false;
    if (keepMine) { await flush(); }
    else { editRef.current = latest; setEdit(latest); setTagsInput(latest.tags.join('\n')); setUrlsInput(latest.source_urls.join('\n')); setState('saved'); setLastSaved(conflictDraft.updated_at || ''); }
  };
  const stateLabel: Record<SaveState, string> = { loading: '读取工作稿…', clean: '与正式版本一致', dirty: '有修改待保存', saving: '正在保存工作稿…', saved: `工作稿已保存${lastSaved ? ` · ${stamp(lastSaved)}` : ''}`, error: '保存或读取需处理' };
  const downloadRecovery = (value: Editable = editRef.current) => {
    const blob = new Blob([JSON.stringify({ topic_id: topic.id, base_version_id: version.id, platform: work.platform, ...value }, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `工作稿恢复-${work.platform}.json`; anchor.click(); URL.revokeObjectURL(url);
  };

  return { edit, change, tagsInput, setTagsInput, urlsInput, setUrlsInput, state, error, stateLabel: stateLabel[state], suggestion, suggesting, accepting, justAccepted, directive, setDirective, selection, setSelection, videoTarget, setVideoTarget, undo, requestSuggestion, accept, reject: () => setSuggestion(null), undoSuggestion, flush, readLatest, resolveConflict, conflictDraft, retainedDraft, downloadRecovery, loaded: loaded.current, original, readOnly, locked, video: isVideoPlatform(work.platform), work, topic, version, suggestionCardRef };
}

function StudioDraftScope({ topic, version, work, readOnly, locked, onPreview, onApplied, onChanged, onPendingChange, register, children }: { topic: Topic; version: Version; work: Work; readOnly: boolean; locked: boolean; onPreview: (value: Editable) => void; onApplied: (text: string) => void; onChanged: (value: boolean) => void; onPendingChange: (value: boolean) => void; register: (value: EditorHandle | null) => void; children: ReactNode }) {
  const draft = useStudioDraft({ topic, version, work, readOnly, locked, onPreview, onApplied, onChanged, onPendingChange });
  useEffect(() => { register({ flush: draft.flush, requestSuggestion: () => { void draft.requestSuggestion(); } }); return () => register(null); }, [draft.flush, draft.requestSuggestion, register]);
  return <StudioDraftCtx.Provider value={draft}>{children}</StudioDraftCtx.Provider>;
}

function DraftEditor() {
  const draft = useDraft();
  return <div className="studio-paper">
    <div className="studio-paper-meta"><span><i className={`platform-dot ${draft.work.platform}`} />{NAMES[draft.work.platform]}工作稿<span className={`studio-save-dot ${draft.state}`} />{draft.readOnly ? same(draft.edit, draft.original) ? '历史或只读正式稿' : '保留工作稿 · 只读' : draft.stateLabel}</span><span>{Array.from(draft.edit.body).length.toLocaleString()} 字符</span></div>
    {draft.readOnly && !same(draft.edit, draft.original) && <div className="studio-retained-draft"><History size={16} /><span>基线已过期，当前显示保留工作稿；右侧为历史正式版。请下载这份工作稿后对照合并。</span><button onClick={() => draft.downloadRecovery()}><Download size={13} />下载保留工作稿</button></div>}
    {draft.retainedDraft && <div className="studio-retained-draft"><History size={16} /><span>此版本另有保留工作稿，下面仍展示历史正式版。</span><button onClick={() => draft.downloadRecovery(draft.retainedDraft!)}><Download size={13} />下载保留工作稿</button></div>}
    {draft.error && <div className="studio-editor-error" role="alert"><AlertCircle size={16} /><span>{draft.error}</span>{draft.state === 'error' && draft.loaded && <button onClick={() => { void draft.flush(); }}>重试保存</button>}<button onClick={() => void draft.readLatest()}>读取最新稿对照</button><button onClick={() => draft.downloadRecovery()}><Download size={12} />下载恢复副本</button></div>}
    {draft.conflictDraft && <div className="studio-conflict" role="region" aria-label="工作稿冲突对照"><h3>对照两个工作稿</h3><p>当前输入仍保留在编辑器中。下面是服务器上的最新工作稿（修订 {draft.conflictDraft.revision}）。</p><textarea readOnly aria-label="服务器最新工作稿" value={`${draft.conflictDraft.title}\n\n${draft.conflictDraft.body}`} rows={6} />{draft.conflictDraft.stale ? <p>基础版本已经变化。请先下载当前输入，再切到最新正式版本进行合并。</p> : <div className="actions"><button className="btn small" onClick={() => void draft.resolveConflict(false)}>采用服务器最新工作稿</button><button className="btn small" onClick={() => { if (window.confirm('将当前输入保存为新的工作稿修订，替换刚读取的服务器工作稿？正式版本保持不变。')) void draft.resolveConflict(true); }}>保留当前输入并重新保存</button></div>}</div>}
    <EditorFields withSelection />
    {!draft.readOnly && <div className="studio-ai-composer"><div className="studio-ai-caption"><Sparkles size={15} /><strong>一起把这段写好</strong><span>{draft.selection ? `已选中 ${draft.selection.end - draft.selection.start} 字符` : '未选择段落，将针对全文提出建议'}</span>{draft.selection && <button onClick={() => draft.setSelection(null)}>取消选区</button>}</div><div className="studio-ai-shortcuts">{['把开头说具体', '删去空话，保留信息', '把我的判断写清楚'].map(text => <button key={text} onClick={() => draft.setDirective(text)}>{text}</button>)}</div>{draft.video && <div className="video-suggestion-target"><label>修改镜头<select value={draft.videoTarget?.shot_id || draft.edit.shots?.[0]?.id || ''} onChange={event => draft.setVideoTarget({ shot_id: event.target.value, field: draft.videoTarget?.field || 'spoken_text' })}>{draft.edit.shots?.map((shot, i) => <option value={shot.id} key={shot.id}>镜头 {i + 1}</option>)}</select></label><label>修改字段<select value={draft.videoTarget?.field || 'spoken_text'} onChange={event => draft.setVideoTarget({ shot_id: draft.videoTarget?.shot_id || draft.edit.shots?.[0]?.id || '', field: event.target.value as 'spoken_text' | 'visual_notes' })}><option value="spoken_text">口播</option><option value="visual_notes">画面说明</option></select></label></div>}<div className="studio-ai-input"><textarea value={draft.directive} onChange={event => draft.setDirective(event.target.value)} rows={2} placeholder="告诉 AI 你想怎么改。缺少实测或例子时，请先补入资料。" aria-label="修改要求" /><button className="studio-ai-submit" disabled={!draft.directive.trim() || draft.suggesting || draft.state === 'loading'} onClick={() => void draft.requestSuggestion()}>{draft.suggesting ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}{draft.suggesting ? '正在修改' : '提出建议'}</button></div><div className="studio-ai-bottom"><small>使用本地模型 · 建议由你决定是否采用</small><button disabled={draft.state === 'saving'} onClick={() => { void draft.flush(); }}><Save size={13} />立即保存工作稿</button>{draft.undo && <button onClick={draft.undoSuggestion}><Undo2 size={13} />撤销上次采用</button>}</div></div>}
    <SuggestionCard />
  </div>;
}

function EditorFields({ withSelection }: { withSelection?: boolean }) {
  const draft = useDraft();
  const { edit, change, readOnly, locked, loaded, video, tagsInput, urlsInput, setTagsInput, setUrlsInput, setSelection, setVideoTarget, setDirective } = draft;
  return <>
    <label className="studio-title-field"><span className="sr-only">标题</span><textarea rows={2} value={edit.title} readOnly={readOnly || locked || !loaded} placeholder="写一个准确、具体的标题" onChange={event => change({ title: event.target.value })} aria-label="文章标题" /></label>
    {video ? <VideoFields value={edit} disabled={readOnly || locked || !loaded} onChange={change} onSuggest={(shot_id, field) => { setVideoTarget({ shot_id, field }); setDirective(previous => previous || '保留事实与原意，让表达更清楚'); }} /> : <label className="studio-body-field"><span className="sr-only">正文</span><textarea value={edit.body} readOnly={readOnly || locked || !loaded} onChange={event => { change({ body: event.target.value }); setSelection(null); }} onSelect={withSelection ? (event => { const element = event.currentTarget; setSelection(element.selectionEnd > element.selectionStart ? { start: element.selectionStart, end: element.selectionEnd } : null); }) : undefined} placeholder="写下你对这件事的理解，或先整理资料后生成初稿。" aria-label="文章正文" /></label>}
    <details className="studio-work-details"><summary>话题与引用来源<ChevronDown size={14} /></summary><label>话题标签<textarea rows={2} value={tagsInput} readOnly={readOnly || locked || !loaded} onChange={event => { setTagsInput(event.target.value); change({ tags: event.target.value.split('\n').map(text => text.trim()).filter(Boolean) }); }} placeholder="每行一个话题" /></label><label>引用来源<textarea rows={2} value={urlsInput} readOnly={readOnly || locked || !loaded} onChange={event => { setUrlsInput(event.target.value); change({ source_urls: event.target.value.split('\n').map(text => text.trim()).filter(Boolean) }); }} placeholder="每行一个原始来源链接" /></label></details>
  </>;
}

function SuggestionCard() {
  const draft = useDraft();
  if (!draft.suggestion && !draft.justAccepted) return null;
  if (draft.justAccepted && !draft.suggestion) return <div className="studio-suggestion studio-suggestion-accepted" role="status"><Check size={15} /><strong>已采用到工作稿</strong><span>当前平台正文与预览已同步更新</span></div>;
  const suggestion = draft.suggestion!;
  return <div className="studio-suggestion" ref={draft.suggestionCardRef} tabIndex={-1}><div className="studio-suggestion-heading"><Sparkles size={15} /><strong>修改建议</strong><span>尚未改动原文</span></div><div className="studio-suggestion-before"><small>原文</small><p>{suggestion.before}</p></div><div className="studio-suggestion-after"><small>建议</small><p>{suggestion.after}</p></div><p className="studio-suggestion-reason">{suggestion.reason}</p>{suggestion.advisories?.length ? <ul>{suggestion.advisories.map((advisory, index) => <li key={index}>{advisory}</li>)}</ul> : null}<div className="studio-suggestion-actions"><button className="studio-primary" disabled={draft.accepting || draft.readOnly || draft.locked} onClick={() => void draft.accept()}>{draft.accepting ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}采用到工作稿</button><button className="btn small" disabled={draft.accepting} onClick={draft.reject}>拒绝</button></div></div>;
}

function StudioRightPanel({ platform, work, version, historical, highlight, reviewChildren }: { platform: Platform; work: Work; version: Version; historical: boolean; highlight?: string; reviewChildren?: ReactNode }) {
  const draft = useDraft();
  const previewWork = historical ? work : { ...work, ...draft.edit };
  return <>
    <div className="studio-section-caption"><FileText size={15} /><h3>{NAMES[platform]}预览</h3><span>{historical ? '历史正式版' : same(draft.edit, draft.original) ? '正式版' : '工作稿'} · 本地预览</span></div>
    <div className="studio-split-preview"><LocalPreview work={previewWork} version={version} platform={platform} highlight={highlight} /></div>
    {!historical && !draft.readOnly && <div className="studio-split-caption"><i className={`platform-dot ${platform}`} />直接修改下方文字，上方预览实时更新<span className={`studio-right-save ${draft.state}`}>{draft.stateLabel}</span></div>}
    {!historical && !draft.readOnly && <div className="studio-split-edit"><EditorFields /></div>}
    {reviewChildren}
  </>;
}

function LocalPreview({ work, version, platform, highlight }: { work: Work; version: Version; platform: Platform; highlight?: string }) {
  const [copied, setCopied] = useState('');
  const highlightParagraphs = highlight ? highlight.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean) : [];
  if (isVideoPlatform(platform)) return <VideoPreview work={work} highlight={highlight} />;
  const assets = version.image_assets?.[platform]?.assets || [];
  const images = assets.filter(asset => localImage(asset.url));
  const mark = (kind: string) => { setCopied(kind); window.setTimeout(() => setCopied(''), 1800); };
  const copyText = async (kind: string, text: string) => { try { await navigator.clipboard.writeText(text); mark(kind); } catch { setCopied(''); } };
  const copyHtml = async () => {
    try {
      if (work.html && globalThis.ClipboardItem && navigator.clipboard?.write) await navigator.clipboard.write([new ClipboardItem({ 'text/html': new Blob([work.html], { type: 'text/html' }), 'text/plain': new Blob([work.text || work.body], { type: 'text/plain' }) })]);
      else await navigator.clipboard.writeText(work.text || work.body);
      mark('html');
    } catch { setCopied(''); }
  };
  const saveImage = async (asset: { url?: string; filename?: string; id: string }, index: number) => {
    const url = localImage(asset.url);
    if (!url) return;
    try { const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' }); if (!response.ok) throw new Error('图片读取失败'); const blob = await response.blob(); const objectUrl = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = objectUrl; link.download = `${String(index + 1).padStart(2, '0')}-${asset.filename || asset.id}.jpg`; link.click(); window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10000); mark(`img-${asset.id}`); } catch { setCopied(''); }
  };
  return <div className={`studio-local-preview ${platform}`}><div className="studio-preview-format"><span>{platform === 'xiaohongshu' ? '图文笔记' : '文章'} · {work.format}</span><span className="studio-preview-copy">{work.html && <button onClick={() => void copyHtml()} aria-label="复制富文本">{copied === 'html' ? <Check size={13} /> : <Copy size={13} />}{copied === 'html' ? '已复制' : '复制富文本'}</button>}<button onClick={() => void copyText('body', work.body)} aria-label="复制预览正文">{copied === 'body' ? <Check size={13} /> : <Copy size={13} />}{copied === 'body' ? '已复制' : '复制正文'}</button></span></div>{images[0] && <figure className="studio-cover-wrap"><img className="studio-cover" src={localImage(images[0].url)} alt={images[0].caption || '封面预览'} /><button className="studio-img-save" onClick={() => void saveImage(images[0], 0)}>{copied === `img-${images[0].id}` ? '已请求保存' : '保存封面'}</button></figure>}<h3>{work.title || '尚无标题'}</h3><div className="studio-preview-body">{work.body.split(/\n\s*\n/).map((paragraph, index) => <p key={index} className={highlight && (work.body === highlight || highlightParagraphs.some(part => paragraph.includes(part) || part.includes(paragraph))) ? 'studio-highlight' : undefined}>{paragraph}</p>)}</div>{work.tags.length > 0 && <p className="studio-preview-tags">{work.tags.map(tag => `#${tag.replace(/^#/, '')}`).join(' ')}</p>}{images.length > 1 && <div className="studio-preview-images">{images.slice(1).map((asset, index) => <figure key={asset.id}><img src={localImage(asset.url)} alt={asset.caption || '正文配图'} /><figcaption>{asset.caption || ''}<button className="studio-img-save" onClick={() => void saveImage(asset, index + 1)}>{copied === `img-${asset.id}` ? '已请求保存' : '保存图片'}</button></figcaption></figure>)}</div>}{!assets.length && <p className="studio-preview-note">当前版本暂无已登记图片。</p>}<p className="studio-preview-note">本地预览，平台实际呈现请在人工上传后核对。复制或保存不记为已发布。</p></div>;
}

function PlatformReview({ topicId, versionId, factRevision, entry, blocked, error, onRecorded }: { topicId: string; versionId: string; factRevision: number; entry: ReviewEntry | undefined; blocked: boolean; error: string; onRecorded: () => void }) {
  const [form, setForm] = useState({ result: 'approved', account_label: '', ai_disclosure: 'AI 辅助创作', comment: '', facts_confirmed: false });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const record = async () => {
    if (!entry?.draft || blocked) return;
    setBusy(true); setMessage('');
    try { const current = await api<ReviewData>(`/topics/${topicId}/review`); if (current.version_id !== versionId || (current.fact_base?.revision || 0) !== factRevision) throw new Error('正式版本或事实底稿已变化，请重新核对后审阅。'); await api(`/drafts/${entry.draft.id}/review-platform`, 'POST', { ...form, expected_version_id: versionId, expected_fact_revision: factRevision }); setMessage('本平台审阅已记录。'); setForm(previous => ({ ...previous, facts_confirmed: false })); onRecorded(); }
    catch (failure) { setMessage(String(failure)); }
    finally { setBusy(false); }
  };
  return <div className="studio-review"><div className="studio-review-caption"><CheckCircle2 size={16} /><h3>本平台审阅</h3>{entry?.review?.approved && <span>已通过</span>}</div>{error && <p className="studio-editor-error" role="alert">{error}</p>}{blocked && <div className="studio-review-warning">工作稿有修改或正在生产。请先保存为正式版本，再确认看到的内容。</div>}{entry?.review?.blocking?.length ? <ul className="studio-review-issues">{entry.review.blocking.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}<fieldset disabled={blocked || busy || !entry?.draft}><label>审阅结果<select value={form.result} onChange={event => setForm({ ...form, result: event.target.value })}><option value="approved">通过</option><option value="returned">退回修改</option><option value="pending_material">待补素材</option></select></label><label>审阅意见<textarea rows={2} value={form.comment} onChange={event => setForm({ ...form, comment: event.target.value })} placeholder="需要修改或补充的内容" /></label><label className="studio-fact-confirm"><input type="checkbox" checked={form.facts_confirmed} onChange={event => setForm({ ...form, facts_confirmed: event.target.checked })} /><span>我已确认这份正式稿的事实与引用</span></label><button className="btn primary" disabled={blocked || busy || !form.facts_confirmed || !entry?.draft} onClick={() => void record()}>{busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}记录本平台审阅</button></fieldset>{message && <p role="status" className="studio-preview-note">{message}</p>}</div>;
}
