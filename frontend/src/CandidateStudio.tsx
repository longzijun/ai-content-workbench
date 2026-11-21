import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, ArrowRight, BookOpen, CheckCircle2, FileText, LoaderCircle, RefreshCw, Save } from 'lucide-react';
import { api } from './api';
import { createDrafts, readableError } from './draftCreation';
import { PLATFORM_NAMES as NAMES, isVideoPlatform } from './platforms';
import { VideoFields, VideoPreview, videoBody } from './VideoScript';
import { FactBasePanel } from './V3Panels';
import type { Platform, Topic, Version, Work } from './types';
import './candidate-studio.css';

type Editable = Pick<Work, 'title' | 'body' | 'tags' | 'source_urls' | 'cover_text' | 'shots' | 'target_duration_seconds'>;
export type CandidateDraft = Editable & { id: string; topic_id: string; job_id: string; platform: Platform; input_revision: number; revision: number; state: string; issues: string[]; created_at?: string; updated_at: string; stale?: boolean; material_mode?: string; material_summary?: string; committed_version_id?: string | null };
type CandidateList = { candidates: CandidateDraft[]; materials_summary?: string | { mode?: string; summary?: string; audience?: string; suggested_angle?: string; documents?: { url: string; title?: string }[]; sources?: unknown[]; issues?: string[] } };
type Props = { topic: Topic; version: Version | null; busy: string; onVersion: (id: string) => void; onChooseTopic?: () => void; refresh?: () => void; official: ReactNode; initialPlatform?: Platform | null; onPlatformChange?: (platform: Platform) => void };
type Handle = { flush: () => Promise<CandidateDraft | null> };
const editable = (draft: Editable): Editable => ({ title: draft.title || '', body: draft.shots ? videoBody(draft.shots) : draft.body || '', tags: (draft.tags || []).map(item => item.trim()).filter(Boolean), source_urls: (draft.source_urls || []).map(item => item.trim()).filter(Boolean), ...(draft.shots ? { cover_text: draft.cover_text || '', shots: draft.shots, target_duration_seconds: draft.target_duration_seconds || 60 } : {}) });
const same = (a: Editable, b: Editable) => JSON.stringify(editable(a)) === JSON.stringify(editable(b));
const time = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '';
const stateName = (draft: CandidateDraft) => draft.stale ? '旧范围 · 只读' : ({ generating: '生成中', draft: '待检查并定稿', ready: '待检查并定稿', needs_changes: '有待修改项', incomplete: '不完整草稿', interrupted: '暂时中断', failed: '暂未出稿', committed: '已定稿' }[draft.state] || '可编辑工作稿');

/** Candidate drafts live independently from frozen versions, including before the first version. */
export default function CandidateStudio({ topic, version, busy, onVersion, onChooseTopic, refresh, official, initialPlatform, onPlatformChange }: Props) {
  const [data, setData] = useState<CandidateList>({ candidates: [] });
  const [loadError, setLoadError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [view, setView] = useState<'auto' | 'candidates' | 'official'>('auto');
  const [platform, setPlatform] = useState<Platform>(initialPlatform && topic.platforms.includes(initialPlatform) ? initialPlatform : topic.platforms[0] || 'wechat');
  const selectionKey = `workbench:candidate-selection:${topic.id}:${topic.revision}`;
  const [selected, setSelected] = useState<Partial<Record<Platform, string>>>(() => { try { return JSON.parse(sessionStorage.getItem(selectionKey) || '{}'); } catch { return {}; } });
  useEffect(() => { try { sessionStorage.setItem(selectionKey, JSON.stringify(selected)); } catch { /* Selected IDs are optional browser state. */ } }, [selectionKey, selected]);
  const [message, setMessage] = useState('');
  const [creating, setCreating] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [optionalOpen, setOptionalOpen] = useState(false);
  const handle = useRef<Handle | null>(null);
  const creatingRef = useRef(false);
  const active = topic.jobs.filter(job => ['queued', 'running'].includes(job.state));
  const load = useCallback(async () => {
    try {
      const result = await api<CandidateList>(`/topics/${topic.id}/candidate-drafts`);
      setData(result); setLoadError(''); setLoaded(true);
    } catch (error) { setLoadError(readableError(error)); setLoaded(true); }
  }, [topic.id]);
  useEffect(() => { void load(); const timer = window.setInterval(() => { void load(); }, 3000); return () => window.clearInterval(timer); }, [load]);
  useEffect(() => { if (!active.length) return; const timer = window.setInterval(() => refresh?.(), 3000); return () => window.clearInterval(timer); }, [active.length, refresh]);
  const current = data.candidates.filter(item => item.input_revision === topic.revision && !item.stale);
  const forPlatform = (p: Platform) => current.filter(item => item.platform === p).sort((a, b) => (b.created_at || b.updated_at).localeCompare(a.created_at || a.updated_at));
  useEffect(() => {
    // Pin the first displayed candidate. Polling must never switch an editing surface to a new generation.
    setSelected(previous => {
      const next = { ...previous }; let changed = false;
      topic.platforms.forEach(p => {
        if (next[p] && current.some(item => item.platform === p && item.id === next[p])) return;
        const first = forPlatform(p)[0];
        if (first) { next[p] = first.id; changed = true; }
      });
      return changed ? next : previous;
    });
  }, [data.candidates, topic.revision]);
  const chosen = (p: Platform) => forPlatform(p).find(item => item.id === selected[p]) || forPlatform(p)[0];
  const candidate = chosen(platform);
  const fresh = current.some(item => !item.committed_version_id && (!version || (item.created_at || item.updated_at) > version.created_at));
  const showCandidates = view === 'candidates' || view === 'auto' && (!version || fresh);
  const transition = async (action: () => void) => {
    if (switching || committing) return;
    setSwitching(true);
    try {
      if (handle.current && !await handle.current.flush()) { setMessage('工作稿尚未保存，当前输入已保留。请处理保存问题后继续。'); return; }
      if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) { setMessage('有尚未保存的修改，请先保存后继续。'); return; }
      action();
    } finally { setSwitching(false); }
  };
  const create = async () => {
    if (creatingRef.current || active.length) return;
    if (handle.current && !await handle.current.flush()) { setMessage('请先保存当前工作稿，再开始新一轮创作。'); return; }
    if (!window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true }))) { setMessage('请先保存当前修改，再开始创作。'); return; }
    creatingRef.current = true; setCreating(true); setMessage('');
    try { await createDrafts(topic); setView('candidates'); setMessage('创作请求已确认，系统正在自动整理资料。'); refresh?.(); void load(); }
    catch (error) { setMessage(`创作请求尚未确认：${readableError(error)} 已保留本次请求，可再次点击继续。`); }
    finally { creatingRef.current = false; setCreating(false); }
  };
  const retry = async (id: string) => {
    if (creatingRef.current) return;
    creatingRef.current = true; setCreating(true); setMessage('');
    try { if (handle.current && !await handle.current.flush()) throw new Error('请先保存当前工作稿。'); await api(`/jobs/${id}/retry`, 'POST', {}); setMessage('已请求继续，已有稿件与人工修改会保留。'); refresh?.(); void load(); }
    catch (error) { setMessage(readableError(error)); }
    finally { creatingRef.current = false; setCreating(false); }
  };
  const commit = async () => {
    if (committing) return;
    setCommitting(true); setMessage('');
    try {
      const saved = handle.current ? await handle.current.flush() : null;
      if (handle.current && !saved) throw new Error('工作稿尚未保存成功，请先处理保存问题。');
      const result = await api<CandidateList>(`/topics/${topic.id}/candidate-drafts`);
      const selectedDrafts = topic.platforms.map(p => {
        const id = chosen(p)?.id;
        return result.candidates.find(item => item.id === id && !item.stale && item.input_revision === topic.revision);
      });
      if (selectedDrafts.some(item => !item)) throw new Error('请等待所有已批准平台有可用工作稿后再统一定稿；已有稿件可以继续编辑。');
      const candidates = selectedDrafts as CandidateDraft[];
      const response = await api<{ version: Version }>(`/topics/${topic.id}/candidate-commit`, 'POST', {
        expected_version_id: version?.id || null,
        candidate_ids: Object.fromEntries(candidates.map(item => [item.platform, item.id])),
        expected_revisions: Object.fromEntries(candidates.map(item => [item.platform, item.revision])),
      });
      setView('official'); onVersion(response.version.id); refresh?.(); void load();
    } catch (error) { setMessage(readableError(error)); }
    finally { setCommitting(false); }
  };
  const latestJob = topic.jobs[0];
  useEffect(() => {
    if (latestJob && !['queued', 'running'].includes(latestJob.state)) setMessage(previous => previous.startsWith('创作请求已确认') ? (latestJob.state === 'succeeded' ? '工作稿已保存，可继续编辑并检查定稿。' : '创作暂时中断，已有工作稿保留；可继续未完成的平台。') : previous);
  }, [latestJob?.state, latestJob?.id]);
  const resumable = !active.length && latestJob && ['failed', 'cancelled', 'succeeded'].includes(latestJob.state) && (!version || fresh || latestJob.updated_at > version.created_at);
  const materials = typeof data.materials_summary === 'object' ? data.materials_summary : undefined;
  const summary = typeof data.materials_summary === 'string' ? data.materials_summary : materials?.summary || [materials?.audience && `建议受众：${materials.audience}`, materials?.suggested_angle && `写作角度：${materials.suggested_angle}`, materials?.documents?.length && `已读取原文 ${materials.documents.length} 篇`].filter(Boolean).join('；');
  const mode = candidate?.material_mode || (typeof data.materials_summary === 'object' ? data.materials_summary.mode : '');

  return <div className="candidate-workspace">
    <div className="candidate-view-bar"><div><BookOpen size={17} /><span>先出工作稿，在预览中修改和检查</span></div><div className="actions">
      {version && <><button className={`btn small ${showCandidates ? 'primary' : ''}`} disabled={switching || committing} onClick={() => void transition(() => setView('candidates'))}>可编辑工作稿</button><button className={`btn small ${!showCandidates ? 'primary' : ''}`} disabled={switching || committing} onClick={() => void transition(() => setView('official'))}>正式版本与审阅</button></>}
      {!showCandidates && <button className="btn small" disabled={!!busy || creating || !!active.length} onClick={() => void create()}>开始新一轮创作</button>}
    </div></div>
    {!showCandidates ? official : <>
      <header className="candidate-heading"><div><span className="studio-eyebrow">自动整理资料 · 本地创作</span><h2>{topic.title}</h2><p>工作稿自动保存。标题、事实与平台要求可在这里继续修改。</p></div><button className="btn small" disabled={switching || committing} onClick={() => void transition(() => onChooseTopic?.())}>返回选题中心</button></header>
      {(message || loadError) && <div className="callout warning" role="status"><AlertCircle size={16} /><p>{message || `工作稿暂时无法读取：${loadError}`}</p>{loadError && <button className="btn small" onClick={() => void load()}>重新读取</button>}</div>}
      <section className="candidate-material-summary panel"><div><BookOpen size={17} /><strong>本篇资料 · 系统自动整理</strong><span className="tag muted">{mode === 'outline' ? '目前仅有提纲材料' : mode === 'limited' ? '部分资料可用' : summary ? '资料已整理' : active.length ? '整理中' : '开始创作时自动准备'}</span></div><p>{summary || candidate?.material_summary || '系统会使用选题说明、已有原文与来源准备材料。补充个人观点或经历是可选操作。'}</p>
        <details><summary>查看来源与补充我的想法（选填）</summary><div className="candidate-source-list">{(materials?.documents?.length ? materials.documents : topic.sources).map((source, index) => <p key={`${source.url}:${index}`}>{/^https?:\/\//i.test(source.url) ? <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a> : source.title || '来源待读取'}</p>)}{!topic.sources.length && !materials?.documents?.length && <p>尚未登记来源，系统将在创作任务中查找可用材料。</p>}{materials?.issues?.map((issue, index) => <p key={index}>{issue}</p>)}</div><button className="btn small" aria-expanded={optionalOpen} onClick={() => setOptionalOpen(open => !open)}>{optionalOpen ? '收起补充资料' : '补充我的想法'}</button>{optionalOpen && <FactBasePanel topicId={topic.id} topicSources={topic.sources} onChanged={() => { refresh?.(); void load(); }} />}</details>
      </section>
      <div className="candidate-platform-tabs" role="tablist" aria-label="平台工作稿">{topic.platforms.map(p => { const value = chosen(p); return <button type="button" role="tab" aria-selected={p === platform} aria-controls="candidate-work-panel" id={`candidate-tab-${p}`} className={p === platform ? 'active' : ''} key={p} disabled={switching || committing} onClick={() => void transition(() => { setPlatform(p); onPlatformChange?.(p); })}><span className={`platform-dot ${p}`} /><span>{NAMES[p]}<small>{value ? stateName(value) : active.length ? '等待出稿' : '尚无工作稿'}</small></span>{value?.body && <CheckCircle2 size={14} />}</button>; })}</div>
      {active.map(job => <div className="studio-job" role="status" key={job.id}><LoaderCircle size={17} className="spin" /><div><strong>{job.stage || '正在创作'}</strong><p>{job.events.at(-1)?.message || '后台正在处理，已有草稿可以编辑。'}</p></div><button className="btn small" disabled={creating} onClick={() => void api(`/jobs/${job.id}/cancel`, 'POST', {}).then(() => { setMessage('已请求取消，已有工作稿将保留。'); refresh?.(); }).catch(error => setMessage(readableError(error)))}>取消任务</button></div>)}
      <div id="candidate-work-panel" role="tabpanel" aria-labelledby={`candidate-tab-${platform}`}>
        {candidate ? <>
          <div className="candidate-revisions"><label>本平台工作稿<select aria-label="选择工作稿" value={candidate.id} disabled={switching || committing} onChange={event => { const id = event.target.value; void transition(() => setSelected(old => ({ ...old, [platform]: id }))); }}>{forPlatform(platform).map((draft, index) => <option value={draft.id} key={draft.id}>{index === 0 ? '最新生成' : '历史候选'} · {time(draft.created_at || draft.updated_at)} · {stateName(draft)}</option>)}</select></label><span>切换候选前会保存当前输入；新生成内容不会覆盖你的修改。</span></div>
          <CandidateEditor key={candidate.id} candidate={candidate} locked={committing || switching} register={value => { handle.current = value; }} onSaved={() => { void load(); }} />
        </> : <section className="panel candidate-empty"><FileText size={34} /><h3>{!loaded ? '正在读取工作稿' : active.length ? `${NAMES[platform]}工作稿正在准备` : '从选题直接开始创作'}</h3><p>{active.length ? '这个平台一有可用内容就会出现在这里；你可以先编辑其他平台。' : '无需先填写资料表。系统自动整理材料，再逐个平台生成可编辑工作稿。'}</p>{!active.length && <button className="btn primary" disabled={!!busy || creating || topic.status === 'rejected' || topic.status === 'parked'} onClick={() => void create()}>{creating ? '正在确认创作请求…' : '开始创作'}</button>}</section>}
      </div>
      <footer className="candidate-footer"><p>先保存工作稿，再统一检查定稿；通过后进入正式版本审阅。</p><div className="actions">{resumable && <button className="btn" disabled={!!busy || creating || committing} onClick={() => void retry(latestJob.id)}><RefreshCw size={15} />继续未完成的平台</button>}{candidate && !active.length && <button className="btn" disabled={!!busy || creating || committing} onClick={() => void create()}>重新整理并创作</button>}<button className="btn primary" disabled={!!busy || committing || switching || !!active.length || topic.platforms.some(p => !chosen(p))} onClick={() => void commit()}>{committing ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}检查并定稿<ArrowRight size={15} /></button></div></footer>
    </>}
  </div>;
}

function CandidateEditor({ candidate, locked, register, onSaved }: { candidate: CandidateDraft; locked: boolean; register: (value: Handle | null) => void; onSaved: () => void }) {
  const [edit, setEdit] = useState<Editable>(() => editable(candidate));
  const [state, setState] = useState<'saved' | 'dirty' | 'saving' | 'error'>('saved');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState<CandidateDraft | null>(null);
  const [savedAt, setSavedAt] = useState(candidate.updated_at);
  const [issues, setIssues] = useState(candidate.issues);
  const [copyMessage, setCopyMessage] = useState('');
  const editRef = useRef(edit), savedRef = useRef(edit), serverRef = useRef(candidate);
  const saving = useRef<Promise<CandidateDraft | null> | null>(null);
  const blocked = useRef(false), alive = useRef(true);
  editRef.current = edit;
  const readOnly = !!candidate.stale;
  const flush = useCallback(async (): Promise<CandidateDraft | null> => {
    if (saving.current) { const done = await saving.current; return done ? flush() : null; }
    if (blocked.current) return null;
    if (same(editRef.current, savedRef.current)) return serverRef.current;
    if (candidate.stale) return null;
    const sent = editable(editRef.current);
    if (alive.current) { setState('saving'); setError(''); }
    const promise = api<CandidateDraft>(`/candidate-drafts/${candidate.id}`, 'PUT', { ...sent, expected_revision: serverRef.current.revision }).then(result => {
      serverRef.current = result; savedRef.current = sent;
      if (alive.current) { setSavedAt(result.updated_at); setIssues(result.issues); setState(same(editRef.current, sent) ? 'saved' : 'dirty'); onSaved(); }
      return result;
    }).catch(async failure => {
      if (alive.current) { setError(readableError(failure)); setState('error'); }
      blocked.current = true;
      if (typeof failure === 'object' && failure && 'status' in failure && failure.status === 409) {
        try { const latest = await api<CandidateDraft>(`/candidate-drafts/${candidate.id}`); if (alive.current) setConflict(latest); } catch { /* Preserve local input when the latest copy is unavailable. */ }
      }
      return null;
    }).finally(() => { saving.current = null; });
    saving.current = promise; return promise;
  }, [candidate.id, candidate.stale, onSaved]);
  const flushRef = useRef(flush); flushRef.current = flush;
  useEffect(() => { alive.current = true; register({ flush: () => flushRef.current() }); return () => { alive.current = false; register(null); }; }, [candidate.id]);
  useEffect(() => {
    if (candidate.revision === serverRef.current.revision || saving.current) return;
    if (same(editRef.current, savedRef.current)) {
      const next = editable(candidate); savedRef.current = next; editRef.current = next; serverRef.current = candidate;
      setEdit(next); setSavedAt(candidate.updated_at); setIssues(candidate.issues); setState('saved');
    } else { setConflict(candidate); blocked.current = true; setState('error'); setError('另一窗口已修改此稿；你的输入已保留，请对照合并。'); }
  }, [candidate.revision]);
  useEffect(() => { if (state !== 'dirty' || readOnly || locked) return; const timer = window.setTimeout(() => { void flushRef.current(); }, 900); return () => window.clearTimeout(timer); }, [edit, state, readOnly, locked]);
  useEffect(() => {
    const guard = (event: Event) => { if (!same(editRef.current, savedRef.current) || saving.current) event.preventDefault(); };
    const unload = (event: BeforeUnloadEvent) => { if (!same(editRef.current, savedRef.current) || saving.current) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('workbench:before-navigate', guard); window.addEventListener('beforeunload', unload);
    return () => { window.removeEventListener('workbench:before-navigate', guard); window.removeEventListener('beforeunload', unload); };
  }, []);
  const change = (patch: Partial<Editable>) => { setEdit(previous => ({ ...previous, ...patch })); if (!blocked.current) setState('dirty'); };
  const resolve = async (keepMine: boolean) => {
    if (!conflict || conflict.stale) return;
    serverRef.current = conflict; savedRef.current = editable(conflict); blocked.current = false; setConflict(null); setError('');
    if (keepMine) { setState('dirty'); await flushRef.current(); }
    else { const next = editable(conflict); editRef.current = next; setEdit(next); setState('saved'); setSavedAt(conflict.updated_at); setIssues(conflict.issues); }
  };
  const preview: Work = { ...candidate, ...edit, platform: candidate.platform, format: candidate.shots ? 'video_script' : 'article', image_prompts: [] };
  const focusIssue = (issue: string) => { document.getElementById(/标题/.test(issue) ? 'candidate-title' : /来源|引用/.test(issue) ? 'candidate-sources' : 'candidate-body')?.focus(); };
  const download = () => { const blob = new Blob([JSON.stringify(editRef.current, null, 2)], { type: 'application/json;charset=utf-8' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `${NAMES[candidate.platform]}-未保存工作稿.json`; a.click(); URL.revokeObjectURL(url); };
  return <div className="candidate-editor-grid">
    <section className="panel candidate-editor" aria-label="工作稿编辑"><header><h3>{NAMES[candidate.platform]}工作稿</h3><span role="status">{readOnly ? '旧范围 · 只读' : state === 'saving' ? '正在保存…' : state === 'dirty' ? '有修改，等待保存' : state === 'error' ? '尚未保存' : `已保存 ${time(savedAt)}`}</span></header>
      {error && <div className="callout error" role="alert"><p>{error}</p>{!conflict && <button className="btn small" onClick={() => { blocked.current = false; void flushRef.current(); }}>重试保存</button>}<button className="btn small" onClick={download}>下载当前输入</button></div>}
      {conflict && <div className="callout warning"><strong>发现另一窗口的新修订 R{conflict.revision}</strong><details><summary>对照服务器稿件</summary><h4>{conflict.title}</h4><pre>{conflict.body}</pre></details><div className="actions"><button className="btn small" disabled={conflict.stale} onClick={() => void resolve(true)}>以当前输入合并保存</button><button className="btn small" disabled={conflict.stale} onClick={() => void resolve(false)}>采用服务器稿件</button></div></div>}
      <fieldset disabled={locked || readOnly}><label>标题<input id="candidate-title" aria-label="工作稿标题" maxLength={300} value={edit.title} onChange={event => change({ title: event.target.value })} /></label>
        {isVideoPlatform(candidate.platform) ? <VideoFields value={edit} disabled={locked || readOnly} onChange={value => change({ ...value, body: videoBody(value.shots || []) })} /> : <label>正文<textarea id="candidate-body" aria-label="工作稿正文" rows={24} maxLength={150000} value={edit.body} onChange={event => change({ body: event.target.value })} /></label>}
        <label>标签（每行一项）<textarea rows={3} aria-label="工作稿标签" value={edit.tags.join('\n')} onChange={event => change({ tags: event.target.value.split('\n') })} /></label>
        <label>来源链接（每行一项）<textarea id="candidate-sources" rows={3} aria-label="工作稿来源" value={edit.source_urls.join('\n')} onChange={event => change({ source_urls: event.target.value.split('\n') })} /></label>
      </fieldset><button className="btn" disabled={readOnly || locked || state === 'saving' || !!conflict} onClick={() => { blocked.current = false; void flushRef.current(); }}><Save size={15} />保存工作稿</button>
    </section>
    <aside className="panel candidate-preview" aria-label="预览与问题"><h3>预览与待处理问题</h3><p className="muted-note">{stateName(candidate)} · 本地保存与编辑不会直接发布。</p>
      {issues.length > 0 && <div className="candidate-issues" role="status"><strong>定稿前请检查</strong><ul>{issues.map((issue, index) => <li key={`${issue}:${index}`}><button className="text-link" onClick={() => focusIssue(issue)}>{issue}</button></li>)}</ul></div>}
      {candidate.material_mode === 'outline' && <p className="callout warning">当前材料仅能支持提纲，请勿当作完整事实报道。</p>}
      {isVideoPlatform(candidate.platform) ? <VideoPreview work={preview} beforeCopy={async () => { if (!await flushRef.current()) throw new Error('请先保存工作稿。'); }} /> : <article className="candidate-article"><p className="muted-note">可直接在预览中修改，与左侧编辑同步保存。</p><input className="candidate-preview-title" aria-label="预览标题" disabled={locked || readOnly} maxLength={300} value={edit.title} placeholder="待拟标题" onChange={event => change({ title: event.target.value })} /><textarea className="candidate-preview-body" aria-label="预览正文" disabled={locked || readOnly} rows={Math.max(15, Math.min(40, edit.body.split('\n').length + Math.ceil(edit.body.length / 34)))} maxLength={150000} value={edit.body} onChange={event => change({ body: event.target.value })} /></article>}
      <button className="btn small" onClick={() => void (async () => { try { if (!await flushRef.current()) throw new Error('工作稿尚未保存，请先处理保存问题。'); await navigator.clipboard.writeText(`${edit.title}\n\n${edit.body}`); setCopyMessage('已复制当前工作稿。'); } catch (failure) { setCopyMessage(readableError(failure)); } })()}>复制当前文字</button>{copyMessage && <p role="status">{copyMessage}</p>}
    </aside>
  </div>;
}
