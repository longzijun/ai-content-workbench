/**
 * V3 本地内容工作流面板：事实底稿 → 三平台生产 → 集中审阅 → 材料包 →
 * 人工发布台账 → 数据导入与周复盘。
 *
 * 设计约束（执行提示词 2026-10-25）：
 * - 不出现任何「连接平台 / 自动保存草稿」入口；平台操作在后端被模式闸门拒绝。
 * - 待审与已审定状态分开；材料包只在三平台全部审过后可构建。
 * - 发布登记只有「用户确认已发布」；链接与最终稿是两个独立待办。
 * - 空数据显示空状态与导入入口，不虚构运营结果。
 */
import { cloneElement, isValidElement, useCallback, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { createDrafts } from './draftCreation';
import { api, apiRaw } from './api';

import type { Platform } from './types';
import { PLATFORM_IDS as PLATFORMS, PLATFORM_NAMES as NAMES } from './platforms';
export { default as PublishPreparation } from './PublishPreparation';

function Btn({ children, onClick, disabled, tone = '', title }: { children: ReactNode; onClick?: () => void; disabled?: boolean; tone?: string; title?: string }) {
  return <button type="button" className={`btn ${tone}`} onClick={onClick} disabled={disabled} title={title}>{children}</button>;
}
function Tag({ children, tone = 'muted' }: { children: ReactNode; tone?: string }) {
  return <span className={`tag ${tone}`}>{children}</span>;
}
function Field({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  return <label className="field" htmlFor={id}><span>{label}</span>{isValidElement(children) ? cloneElement(children as ReactElement<{ id?: string }>, { id }) : children}</label>;
}

// ---------------------------------------------------------------- 模式横幅

export function ModeBanner({ mode }: { mode: { mode: string; mode_label: string; platform_operations: boolean; note: string } | null }) {
  const [open, setOpen] = useState(false);
  if (!mode || mode.platform_operations) return null;
  return (
    <div className="mode-banner">
      <div className="mode-strip">
        <strong>本地内容模式 · 平台自动操作已关闭</strong>
        <span className="mode-summary">不启动浏览器 · 不存平台草稿 · 不调平台接口 · 发布请人工完成</span>
        <button type="button" className="mode-more" aria-expanded={open} onClick={() => setOpen(v => !v)}>{open ? '收起' : '详情'}</button>
      </div>
      {open && <p className="mode-detail">{mode.note}</p>}
    </div>
  );
}

// ---------------------------------------------------------------- 事实底稿

type FactBase = {
  revision: number; status: string; audience: string; core_view: string;
  sources: { url: string; title?: string }[]; facts: { claim: string; source_url?: string; source_date?: string; status: string }[];
  open_questions: { question: string }[]; author_experience: string; private_info: string;
  asset_list: unknown[]; platform_angles: Record<string, string>;
  material_type?: 'original' | 'research' | 'adaptation' | null; raw_text?: string;
};

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

export function FactBasePanel({ topicId, topicSources = [], onChanged, onDirtyChange }: { topicId: string; topicSources?: FactBase['sources']; onChanged?: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const empty: FactBase = { revision: 0, status: 'draft', audience: '', core_view: '', sources: [], facts: [], open_questions: [], author_experience: '', private_info: '', asset_list: [], platform_angles: {}, material_type: 'research', raw_text: '' };
  const initialSources = useRef(topicSources);
  const requestId = useRef(0);
  const [fact, setFact] = useState<FactBase>(empty);
  const [gaps, setGaps] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [flash, setFlash] = useState('');
  const [error, setError] = useState('');
  const [prefillOpen, setPrefillOpen] = useState(false);
  const [prefillBusy, setPrefillBusy] = useState(false);
  const load = useCallback(async () => {
    const request = ++requestId.current;
    setLoading(true); setError('');
    try {
      const data = await api<{ fact_base: FactBase | null; gaps: string[] }>(`/topics/${topicId}/fact-base`);
      if (request !== requestId.current) return;
      setFact(data.fact_base || { revision: 0, status: 'draft', audience: '', core_view: '', sources: initialSources.current.filter(source => source.url?.trim()), facts: [], open_questions: [], author_experience: '', private_info: '', asset_list: [], platform_angles: {}, material_type: 'research', raw_text: '' });
      setGaps(data.gaps || []); setDirty(!data.fact_base && initialSources.current.some(source => source.url?.trim()));
    } catch (e) { if (request === requestId.current) setError(errorMessage(e)); }
    finally { if (request === requestId.current) setLoading(false); }
  }, [topicId]);
  useEffect(() => { void load(); return () => { requestId.current++; }; }, [load]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    const navigate = (event: Event) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', guard); window.addEventListener('workbench:before-navigate', navigate);
    return () => { window.removeEventListener('beforeunload', guard); window.removeEventListener('workbench:before-navigate', navigate); };
  }, [dirty]);
  const set = (key: keyof FactBase, value: unknown) => { setFact(current => ({ ...current, [key]: value } as FactBase)); setDirty(true); setFlash(''); };
  const save = async () => {
    setBusy(true); setFlash('');
    try {
      // Fact links and the source list describe the same evidence. Keep both in
      // the saved base so a visible fact URL cannot leave an invisible gap.
      const sources = fact.sources.map(source => ({ ...source, url: String(source.url || '').trim() }));
      const known = new Set(sources.map(source => source.url).filter(Boolean));
      for (const item of fact.facts) {
        const url = item.source_url?.trim();
        if (url && !known.has(url)) { sources.push({ url, title: '' }); known.add(url); }
      }
      const payload = { audience: fact.audience, core_view: fact.core_view, sources, facts: fact.facts, open_questions: fact.open_questions, author_experience: fact.author_experience, private_info: fact.private_info, asset_list: fact.asset_list, platform_angles: fact.platform_angles, material_type: fact.material_type || 'research', raw_text: fact.raw_text || '' };
      const data = await api<{ fact_base: FactBase; gaps: string[] }>(`/topics/${topicId}/fact-base`, 'POST', payload);
      setFact(data.fact_base); setGaps(data.gaps); setDirty(false);
      setFlash(data.gaps.length ? `已保存，还有 ${data.gaps.length} 项待补资料。` : '底稿已保存，可以确认。'); onChanged?.();
    } catch (e) { setFlash(errorMessage(e)); }
    finally { setBusy(false); }
  };
  const ready = async () => {
    setBusy(true); setFlash('');
    try {
      const data = await api<{ fact_base: FactBase; gaps: string[] }>(`/topics/${topicId}/fact-base/ready`, 'POST', {});
      setFact(data.fact_base); setGaps(data.gaps); setFlash('底稿已确认，可以开始生产。'); onChanged?.();
    } catch (e) { setFlash(errorMessage(e)); }
    finally { setBusy(false); }
  };
  const prefill = async () => {
    if (prefillBusy || busy) return;
    if ((dirty || fact.revision > 0) && !window.confirm('AI 整理会用结果替换当前表单内容（含未保存修改）。已保存的旧修订不受影响。继续？')) return;
    setPrefillBusy(true); setFlash('');
    const request = ++requestId.current;
    try {
      const draft = await api<{ audience: string; core_view: string; facts: FactBase['facts']; open_questions: FactBase['open_questions']; platform_angles: Record<string, string>; sources: FactBase['sources'] }>(`/topics/${topicId}/fact-base/prefill`, 'POST', { material_type: fact.material_type || 'research' });
      if (request !== requestId.current) return;
      setFact(current => ({
        ...current,
        audience: draft.audience || current.audience,
        core_view: draft.core_view || current.core_view,
        facts: draft.facts.length ? draft.facts : current.facts,
        open_questions: draft.open_questions.length ? draft.open_questions : current.open_questions,
        platform_angles: Object.keys(draft.platform_angles).length ? draft.platform_angles : current.platform_angles,
        sources: draft.sources.length ? draft.sources : current.sources,
      }));
      setDirty(true); setPrefillOpen(false);
      setFlash('AI 整理已填入表单，请逐条核对（外部事实默认「待核实」），确认后再保存。');
    } catch (e) { if (request === requestId.current) setFlash(errorMessage(e)); }
    finally { if (request === requestId.current) setPrefillBusy(false); }
  };
  if (loading) return <p className="muted-note" role="status">正在读取本篇资料…</p>;
  if (error) return <div className="callout error" role="alert">{error}<Btn onClick={() => void load()}>重新读取</Btn></div>;
  return <section className="panel fact-editor"><fieldset disabled={busy || prefillBusy} style={{ border: 0, padding: 0, minWidth: 0 }}>
    <div className="panel-title"><h2>本篇资料</h2><Tag tone={fact.status === 'draft' ? 'amber' : 'green'}>{fact.revision ? `R${fact.revision} · ${fact.status === 'locked' ? '已用于生产' : fact.status === 'ready' ? '已确认' : '待确认'}` : '尚未建立'}</Tag><Btn disabled={busy || prefillBusy || fact.status === 'locked'} onClick={() => setPrefillOpen(open => !open)} title="用本地模型从选题标题、说明与来源提炼草稿">{prefillBusy ? '整理中…' : 'AI 辅助整理'}</Btn></div>
    <Field label="材料类型"><select value={fact.material_type || 'research'} onChange={e => set('material_type', e.target.value)}><option value="original">个人原创/真实经历</option><option value="research">资料整理</option><option value="adaptation">已有文章改编</option></select></Field>
    <p className="muted-note">原创经历不强制外链；资料整理和改编需原文链接。事实仍须逐条核对后标记「已确认」。</p>
    {prefillOpen && <div className="prefill-box"><p>本地模型会根据选题标题、说明与已登记来源提炼下方草稿。<strong>不直接保存</strong>，填入后由你逐条核对。外部事实默认标「待核实」，不会冒充已确认。</p><Btn tone="primary" disabled={prefillBusy} onClick={() => void prefill()}>{prefillBusy ? '本地模型整理中…' : '开始整理'}</Btn></div>}
    <Field label="写给谁，解决什么问题"><textarea rows={2} value={fact.audience} onChange={e => set('audience', e.target.value)} /></Field>
    <Field label="我的判断与核心观点"><textarea rows={3} value={fact.core_view} onChange={e => set('core_view', e.target.value)} /></Field>
    <details open><summary>事实与来源（{fact.facts.length}）</summary>
      {fact.facts.map((f, i) => <fieldset className="fact-item" key={i}><legend>事实 {i + 1}</legend>
        <Field label="事实内容"><textarea rows={2} value={f.claim} onChange={e => set('facts', fact.facts.map((v, n) => n === i ? { ...v, claim: e.target.value } : v))} /></Field>
        <Field label="来源网址"><input type="url" value={f.source_url || ''} onChange={e => set('facts', fact.facts.map((v, n) => n === i ? { ...v, source_url: e.target.value } : v))} /></Field>
        <Field label="确认状态"><select value={f.status} onChange={e => set('facts', fact.facts.map((v, n) => n === i ? { ...v, status: e.target.value } : v))}><option value="unverified">待核实</option><option value="confirmed">已确认</option><option value="disputed">有争议</option></select></Field>
        <Btn disabled={busy} onClick={() => set('facts', fact.facts.filter((_, n) => n !== i))}>移除这条事实</Btn>
      </fieldset>)}
      <Btn disabled={busy} onClick={() => set('facts', [...fact.facts, { claim: '', status: 'unverified', source_url: '', source_date: '' }])}>添加事实</Btn>
    </details>
    <details open><summary>原始资料（{fact.sources.length}）</summary><p className="muted-note">事实条目中填写的来源网址也会在保存时加入这里。</p>{fact.sources.map((source, i) => <div className="fact-item" key={i}><Field label="资料名称"><input value={source.title || ''} onChange={e => set('sources', fact.sources.map((v, n) => n === i ? { ...v, title: e.target.value } : v))} /></Field><Field label="原始网址"><input type="url" value={source.url} onChange={e => set('sources', fact.sources.map((v, n) => n === i ? { ...v, url: e.target.value } : v))} /></Field><Btn onClick={() => set('sources', fact.sources.filter((_, n) => n !== i))}>移除资料</Btn></div>)}<Btn onClick={() => set('sources', [...fact.sources, { title: '', url: '' }])}>添加资料</Btn></details>
    <details><summary>更多资料与约束（原始资料、实测经历、平台角度等）</summary>
      <Field label="我的实测或真实经历（可留空）"><textarea rows={3} value={fact.author_experience} onChange={e => set('author_experience', e.target.value)} /></Field>
      <Field label="待补充／待核实的问题"><textarea rows={3} value={fact.open_questions.map(q => q.question).join('\n')} onChange={e => set('open_questions', e.target.value.split('\n').filter(Boolean).map(question => ({ question })))} /></Field>
      <details><summary>各平台表达角度</summary>{PLATFORMS.map(p => <Field key={p} label={NAMES[p]}><textarea rows={2} value={fact.platform_angles[p] || ''} onChange={e => set('platform_angles', { ...fact.platform_angles, [p]: e.target.value })} /></Field>)}</details>
      <details><summary>不可公开信息</summary><Field label="仅作创作约束，不进入作品"><textarea rows={2} value={fact.private_info} onChange={e => set('private_info', e.target.value)} /></Field></details>
    </details>
    {gaps.length > 0 && <div className="callout warning"><strong>待补资料</strong><ul>{gaps.map(g => <li key={g}>{g}</li>)}</ul></div>}
    {dirty && fact.status !== 'draft' && <p className="muted-note">保存后会建立底稿新修订，相关作品需要重新检查。</p>}
    <div className="actions"><Btn tone="primary" disabled={busy || (!dirty && fact.revision > 0)} onClick={() => void save()}>保存资料</Btn><Btn disabled={busy || dirty || gaps.length > 0 || !fact.revision || fact.status !== 'draft'} onClick={() => void ready()}>确认底稿</Btn></div>
    {dirty && <p className="muted-note">资料尚未保存</p>}{flash && <p className="muted-note" role="status">{flash}</p>}
  </fieldset></section>;
}

// ---------------------------------------------------------------- 生产与集中审阅

type ReviewEntry = { platform: Platform; work: { title: string; body: string; tags?: string[] } | null; draft: { id: string; state: string } | null; review: { latest: { result: string; comment: string; created_at: string } | null; approved: boolean; fingerprint_valid: boolean; blocking: string[] } };
type ConsoleData = { fact_base: FactBase | null; gaps: string[]; version_id: string | null; platforms: ReviewEntry[] };

export function ReviewConsole({ topicId }: { topicId: string }) {
  const [data, setData] = useState<ConsoleData | null>(null);
  const [active, setActive] = useState<Platform>('wechat');
  const [form, setForm] = useState({ result: 'approved', comment: '', account_label: '', ai_disclosure: 'AI 辅助创作', facts_confirmed: false });
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState('');
  const load = useCallback(async () => {
    try { setData(await api<ConsoleData>(`/topics/${topicId}/review`)); } catch { setData(null); }
  }, [topicId]);
  useEffect(() => { void load(); }, [load]);
  if (!data) return <p className="muted-note">尚未生成正式作品。先创作工作稿，再检查定稿。</p>;
  const entry = data.platforms.find(item => item.platform === active);
  const record = async () => {
    if (!entry?.draft) return;
    setBusy(true); setFlash('');
    try {
      await api(`/drafts/${entry.draft.id}/review-platform`, 'POST', form);
      setFlash(`${NAMES[active]}审阅已记录。`);
      await load();
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  return (
    <section className="panel">
      <div className="panel-title"><h2>集中审阅</h2><Tag tone="muted">{data.version_id ? `版本 ${data.version_id.slice(0, 8)}` : '无版本'}</Tag></div>
      {data.gaps.length > 0 && <div className="callout warning"><strong>底稿还有缺口</strong><p>{data.gaps.join('；')}</p></div>}
      <div className="tabs">
        {data.platforms.map(item => (
          <button key={item.platform} className={active === item.platform ? 'tab active' : 'tab'} onClick={() => setActive(item.platform)}>
            {NAMES[item.platform]}
            {item.review.approved ? <Tag tone="green">已通过</Tag> : item.review.blocking.length ? <Tag tone="amber">待处理</Tag> : <Tag tone="muted">未审</Tag>}
          </button>
        ))}
      </div>
      {entry?.work ? (
        <>
          <h3>{entry.work.title}</h3>
          <pre className="work-preview">{entry.work.body}</pre>
          {entry.review.blocking.length > 0 && <div className="callout warning"><strong>发布准备尚未就绪</strong><ul>{entry.review.blocking.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
          <div className="review-form">
            <Field label="审阅结果">
              <select value={form.result} onChange={e => setForm({ ...form, result: e.target.value })}>
                <option value="approved">通过</option>
                <option value="returned">退回修改</option>
                <option value="pending_material">待补素材</option>
              </select>
            </Field>
            <Field label="修改意见"><textarea rows={2} value={form.comment} onChange={e => setForm({ ...form, comment: e.target.value })} /></Field>
            <label className="checkbox"><input type="checkbox" checked={form.facts_confirmed} onChange={e => setForm({ ...form, facts_confirmed: e.target.checked })} />我确认事实与引用（规则检查通过不代替事实确认）</label>
            <Btn tone="primary" disabled={busy || !form.facts_confirmed} onClick={() => void record()}>记录本平台审阅</Btn>
            {flash && <span className="muted-note">{flash}</span>}
          </div>
        </>
      ) : <p className="muted-note">该平台暂无作品。</p>}
    </section>
  );
}

export function ProducePanel({ topicId, onProduced, allowedPlatforms, active = false }: { topicId: string; onProduced: () => void; allowedPlatforms?: Platform[]; refreshKey?: number; materialsDirty?: boolean; active?: boolean; onMaterials?: () => void }) {
  const selected = allowedPlatforms || ['wechat', 'toutiao', 'xiaohongshu'] as Platform[];
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [flash, setFlash] = useState('');
  const submitting = useRef(false);
  useEffect(() => { if (active) setSubmitted(false); }, [active]);
  const produce = async () => {
    if (active || submitted || submitting.current) return;
    submitting.current = true; setBusy(true); setFlash('');
    try {
      const topic = await api<{ id: string; revision: number; platforms: Platform[] }>(`/topics/${topicId}`);
      await createDrafts(topic, selected);
      setSubmitted(true); setFlash('创作请求已确认，系统自动整理资料，已有工作稿将逐平台开放编辑。'); onProduced();
    } catch (error) { setFlash(`创作请求尚未确认：${errorMessage(error)} 可再次点击继续。`); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <section className="panel"><div className="panel-title"><h2>选择平台并创作</h2></div><p className="muted-note">系统自动整理资料，无需先填写或确认整张资料表。视频平台生成拍摄脚本。</p><div className="platform-picker">{selected.map(p => <Tag key={p}>{NAMES[p]}</Tag>)}</div>{(active || submitted) && <p role="status">创作任务正在处理，已有草稿可继续编辑。</p>}<Btn tone="primary" disabled={busy || active || submitted || !selected.length} onClick={() => void produce()}>{busy ? '正在确认请求…' : '开始创作'}</Btn>{flash && <p role="status">{flash}</p>}</section>;
}

// ---------------------------------------------------------------- 材料包

type PackageItem = { id: string; version_id: string; state: string; zip_path: string; manifest: { title: string; fact_base_revision?: number; platforms: Record<string, { label: string; images: unknown[]; review?: { fingerprint?: string } }> } };

export function MaterialPackages({ topicId }: { topicId: string; currentVersionId?: string; currentFactRevision?: number; currentReviewFingerprints?: Record<string, string>; canDeliver?: boolean }) {
  const [items, setItems] = useState<PackageItem[]>([]);
  const [error, setError] = useState('');
  useEffect(() => { void api<{ items: PackageItem[] }>(`/topics/${topicId}/material-packages`).then(data => setItems(data.items)).catch(failure => setError(String(failure))); }, [topicId]);
  return <section className="panel"><h2>历史 ZIP 归档</h2><p className="muted-note">仅保留历史下载，日常发布使用工坊中的发布准备页。</p>{error && <p role="alert">{error}</p>}{items.length ? items.map(item => <p key={item.id}>版本 {item.version_id.slice(0, 8)} · <a href={`/api/material-packages/${encodeURIComponent(item.id)}/download`}>下载历史 ZIP</a></p>) : <p>暂无历史归档。</p>}</section>;
}

// ---------------------------------------------------------------- 发布台账

type PublishRecord = { id: string; platform: Platform; account_label: string; confirmed: boolean; published_at_local: string; url: string; url_status: string; platform_edited: boolean; final_backfill: string };

export function PublishLedger({ topicId }: { topicId: string | null }) {
  const [items, setItems] = useState<PublishRecord[]>([]);
  const [form, setForm] = useState({ platform: 'wechat' as Platform, account_label: '', published_at: '', url: '', platform_edited: false, final_text: '', note: '' });
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState('');
  const load = useCallback(async () => {
    try {
      const path = topicId ? `/topics/${topicId}/publish-records` : '/publish-records';
      setItems((await api<{ items: PublishRecord[] }>(path)).items);
    } catch { setItems([]); }
  }, [topicId]);
  useEffect(() => { void load(); }, [load]);
  const register = async () => {
    setBusy(true); setFlash('');
    try {
      await api(`/topics/${topicId}/publish-records`, 'POST', form);
      setFlash('已登记：用户确认已发布。');
      setForm({ ...form, url: '', final_text: '', note: '' });
      await load();
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  const backfill = async (id: string, url: string, finalText: string) => {
    setBusy(true); setFlash('');
    try {
      await api(`/publish-records/${id}/backfill`, 'POST', { ...(url ? { url } : {}), ...(finalText ? { final_text: finalText } : {}) });
      setFlash('已回填。');
      await load();
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  const nowLocal = () => { const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
  return (
    <section className="panel">
      <div className="panel-title"><h2>人工发布台账</h2></div>
      <p className="muted-note">发布由你在官方应用完成，这里只登记。链接完整度与最终稿回填是两个独立待办，可同时缺失，不覆盖已发布事实。</p>
      {topicId ? (
      <div className="review-form">
        <Field label="平台"><select value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value as Platform })}>{PLATFORMS.map(p => <option key={p} value={p}>{NAMES[p]}</option>)}</select></Field>
        <Field label="账号标签"><input value={form.account_label} onChange={e => setForm({ ...form, account_label: e.target.value })} /></Field>
        <Field label="实际发布时间（北京时间）"><input type="datetime-local" value={form.published_at} onChange={e => setForm({ ...form, published_at: e.target.value })} /><Btn onClick={() => setForm({ ...form, published_at: nowLocal() })}>填当前时间</Btn></Field>
        <Field label="公开链接（可后补）"><input value={form.url} onChange={e => setForm({ ...form, url: e.target.value })} placeholder="https://…" /></Field>
        <Field label="平台内是否改过稿"><label className="checkbox"><input type="checkbox" checked={form.platform_edited} onChange={e => setForm({ ...form, platform_edited: e.target.checked })} />改过稿（可粘贴最终正文）</label></Field>
        {form.platform_edited && <Field label="最终正文（发布稿留档）"><textarea rows={3} value={form.final_text} onChange={e => setForm({ ...form, final_text: e.target.value })} /></Field>}
        <Btn tone="primary" disabled={busy || !form.published_at} onClick={() => void register()}>我确认已发布</Btn>
        {flash && <span className="muted-note">{flash}</span>}
      </div>
      ) : <p className="muted-note">在「选题中心」选择选题后可在此登记该选题的发布；下表为全部登记记录。</p>}
      {items.length > 0 && (
        <table className="table"><thead><tr><th>平台</th><th>账号</th><th>发布时间</th><th>链接</th><th>最终稿</th><th>操作</th></tr></thead><tbody>
          {items.map(item => (
            <tr key={item.id}>
              <td>{NAMES[item.platform]}</td>
              <td>{item.account_label || '—'}</td>
              <td>{item.published_at_local?.replace('T', ' ').slice(0, 16) || '—'}</td>
              <td><Tag tone={item.url_status === 'present' ? 'green' : item.url_status === 'invalid_format' ? 'red' : 'amber'}>{item.url_status === 'present' ? '已填' : item.url_status === 'invalid_format' ? '格式/归属不符' : '待补'}</Tag></td>
              <td><Tag tone={item.final_backfill === 'done' ? 'green' : 'amber'}>{item.final_backfill === 'done' ? '已回填' : '待回填'}</Tag></td>
              <td><button className="btn" onClick={() => { const url = window.prompt('补填公开链接（留空跳过）', item.url || ''); const text = window.prompt('粘贴平台内最终正文（留空跳过）', ''); void backfill(item.id, url || '', text || ''); }}>回填</button></td>
            </tr>
          ))}
        </tbody></table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- 数据导入与复盘

export function DataImportPanel() {
  const [preview, setPreview] = useState<{ batch_id: string; mapping: Record<string, string>; unmapped_headers: string[]; rows: Record<string, unknown>[]; errors: { row: number; error: string }[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState('');
  const [observations, setObservations] = useState<{ id: string; platform: string; work_ref: string; metric_name: string; metric_value: number | null; known: boolean }[]>([]);
  const [manual, setManual] = useState({ platform: 'wechat', metric_name: '', metric_value: '', work_ref: '' });
  const loadObservations = useCallback(async () => {
    try { setObservations((await api<{ items: typeof observations }>('/observations')).items); } catch { setObservations([]); }
  }, []);
  useEffect(() => { void loadObservations(); }, [loadObservations]);
  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true); setFlash('');
    try {
      const text = await file.text();
      const response = await apiRaw('/data-imports', text, { 'X-Filename': file.name });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail ? String(data.detail) : '预览失败');
      setPreview(data);
      setFlash(`预览完成：${data.rows.length} 行可导入，${data.errors.length} 行有错误。`);
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  const commit = async () => {
    if (!preview) return;
    setBusy(true); setFlash('');
    try {
      const data = await api<{ imported: number; duplicates: number; errors: unknown[] }>(`/data-imports/${preview.batch_id}/commit`, 'POST', { rows: preview.rows });
      setFlash(`已导入 ${data.imported} 条，重复跳过 ${data.duplicates} 条。`);
      setPreview(null);
      await loadObservations();
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  const addManual = async () => {
    setBusy(true); setFlash('');
    try {
      await api('/observations', 'POST', { ...manual, metric_value: manual.metric_value === '' ? null : Number(manual.metric_value) });
      setManual({ ...manual, metric_name: '', metric_value: '', work_ref: '' });
      await loadObservations();
      setFlash('已登记观测。');
    } catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  return (
    <section className="panel">
      <div className="panel-title"><h2>运营数据导入</h2></div>
      <p className="muted-note">支持 UTF-8 BOM 中文表头 CSV（平台/作品/指标/数值/统计开始/统计结束/来源）。预览后确认才入账；重复内容自动跳过；缺失数值记未知，不记 0。</p>
      <input type="file" accept=".csv,text/csv" onChange={e => void upload(e.target.files?.[0])} disabled={busy} />
      {preview && (
        <>
          <h3>预览（未入账）</h3>
          <p className="muted-note">映射：{Object.entries(preview.mapping).map(([k, v]) => `${k}→${v}`).join('，') || '无'}{preview.unmapped_headers.length > 0 && `；未识别列：${preview.unmapped_headers.join('、')}`}</p>
          <table className="table"><thead><tr><th>行</th><th>平台</th><th>作品</th><th>指标</th><th>数值</th></tr></thead><tbody>
            {preview.rows.map((row, index) => <tr key={index}><td>{String(row.row)}</td><td>{String(row.platform)}</td><td>{String(row.work_ref || '—')}</td><td>{String(row.metric_name)}</td><td>{row.metric_value === null ? '未知' : String(row.metric_value)}</td></tr>)}
            {preview.errors.map((item, index) => <tr key={`e${index}`}><td>{item.row}</td><td colSpan={4} style={{ color: 'red' }}>{item.error}</td></tr>)}
          </tbody></table>
          <Btn tone="primary" disabled={busy || preview.rows.length === 0} onClick={() => void commit()}>确认导入 {preview.rows.length} 条</Btn>
        </>
      )}
      <h3>手工登记观测</h3>
      <div className="review-form">
        <Field label="平台"><select value={manual.platform} onChange={e => setManual({ ...manual, platform: e.target.value as Platform })}>{PLATFORMS.map(p => <option key={p} value={p}>{NAMES[p]}</option>)}</select></Field>
        <Field label="指标名称"><input value={manual.metric_name} onChange={e => setManual({ ...manual, metric_name: e.target.value })} placeholder="如：阅读、曝光、收藏" /></Field>
        <Field label="数值（未知留空）"><input value={manual.metric_value} onChange={e => setManual({ ...manual, metric_value: e.target.value })} /></Field>
        <Field label="作品标识"><input value={manual.work_ref} onChange={e => setManual({ ...manual, work_ref: e.target.value })} /></Field>
        <Btn disabled={busy || !manual.metric_name} onClick={() => void addManual()}>登记</Btn>
      </div>
      {flash && <p className="muted-note">{flash}</p>}
      {observations.length > 0 && (
        <table className="table"><thead><tr><th>平台</th><th>作品</th><th>指标</th><th>数值</th></tr></thead><tbody>
          {observations.slice(0, 20).map(item => <tr key={item.id}><td>{NAMES[item.platform as Platform] || item.platform}</td><td>{item.work_ref || '账号级'}</td><td>{item.metric_name}</td><td>{item.known ? String(item.metric_value) : '未知'}</td></tr>)}
        </tbody></table>
      )}
      {observations.length === 0 && <p className="muted-note">暂无观测数据。没有真实数据时，复盘显示空状态，不生成虚构结论。</p>}
    </section>
  );
}

type WeeklyPayload = { week_start: string; week_end: string; empty_state: boolean; coverage: Record<string, { observations: number; known_values: number; works: number; missing: boolean }>; published_count: number; comparison: { platform: string; works: { work_ref: string; metrics: Record<string, number> }[]; comparable: boolean; note: string }[]; attribution_note: string | null; feedback: { total: number; organized: number }; actions: string[] };

export function WeeklyReviewPanel() {
  const [week, setWeek] = useState(() => { const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.toISOString().slice(0, 10); });
  const [payload, setPayload] = useState<WeeklyPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState('');
  const generate = async () => {
    setBusy(true); setFlash('');
    try { setPayload(await api<WeeklyPayload>(`/weekly-review?week=${week}&refresh=true`)); setFlash('复盘已生成。'); }
    catch (error) { setFlash(String(error)); }
    setBusy(false);
  };
  return (
    <section className="panel">
      <div className="panel-title"><h2>周复盘</h2></div>
      <p className="muted-note">一页结论：数据覆盖与缺失、同平台比较、读者反馈、下周三个行动。样本不足时明确说明，不宣称算法规律。</p>
      <Field label="周起始日（周一）"><input type="date" value={week} onChange={e => setWeek(e.target.value)} /></Field>
      <Btn tone="primary" disabled={busy} onClick={() => void generate()}>生成本周复盘</Btn>
      {flash && <span className="muted-note">{flash}</span>}
      {payload && (
        <div className="weekly-review">
          <h3>{payload.week_start} ~ {payload.week_end}</h3>
          {payload.empty_state && <div className="callout warning"><strong>空状态</strong><p>本周没有导入或登记任何数据。复盘不生成虚构结论，请先导入后台数据。</p></div>}
          <h4>数据覆盖</h4>
          <table className="table"><thead><tr><th>平台</th><th>观测</th><th>已知值</th><th>作品</th></tr></thead><tbody>
            {Object.entries(payload.coverage).map(([platform, info]) => <tr key={platform}><td>{NAMES[platform as Platform] || platform}</td><td>{info.observations}</td><td>{info.known_values}</td><td>{info.works}</td></tr>)}
          </tbody></table>
          <h4>同平台比较</h4>
          {payload.comparison.map(item => (
            <div key={item.platform}>
              <strong>{NAMES[item.platform as Platform] || item.platform}</strong>
              <p className="muted-note">{item.note}</p>
              {item.works.map(work => <p key={work.work_ref}>{work.work_ref}：{Object.entries(work.metrics).map(([k, v]) => `${k} ${v}`).join('，') || '无已知值'}</p>)}
            </div>
          ))}
          {payload.attribution_note && <div className="callout warning"><strong>归因限制</strong><p>{payload.attribution_note}</p></div>}
          <h4>本周已登记发布：{payload.published_count} 次；读者反馈 {payload.feedback.total} 条（已整理 {payload.feedback.organized} 条）</h4>
          <h4>下周三个行动</h4>
          <ol>{payload.actions.map((action, index) => <li key={index}>{action}</li>)}</ol>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- 页面组装

export function V3ReviewPage({ topicId }: { topicId: string | null }) {
  if (!topicId) return <p className="muted-note">请先在「选题中心」或「AI 创作中心」选择一个选题。</p>;
  return (<>
    <FactBasePanel topicId={topicId} />
    <ProducePanel topicId={topicId} onProduced={() => undefined} />
    <ReviewConsole topicId={topicId} />
    <MaterialPackages topicId={topicId} />
  </>);
}

export function V3PublishPage({ topicId }: { topicId: string | null }) {
  return <PublishLedger topicId={topicId} />;
}

export function V3DataPage() {
  return (<>
    <DataImportPanel />
    <WeeklyReviewPanel />
  </>);
}
