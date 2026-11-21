import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, FilePlus2, LoaderCircle, Plus, Sparkles, Trash2, Wand2 } from 'lucide-react';
import { api } from './api';
import { PLATFORM_IDS as allPlatforms, PLATFORM_NAMES as platformNames, isVideoPlatform } from './platforms';
import type { ContentLine, Platform } from './types';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;
type MaterialType = 'original' | 'research' | 'adaptation';
interface FactItem { claim: string; status: 'confirmed' | 'unverified' | 'disputed'; source_url: string; source_date: string }
interface QuestionItem { question: string }
interface Extraction { title: string; audience: string; core_view: string; facts: FactItem[]; open_questions: QuestionItem[]; platform_angles: Record<string, string>; material_type: string }

const MATERIAL_LABELS: Record<MaterialType, string> = { original: '个人原创/真实经历', research: '资料整理', adaptation: '已有文章改编' };
const MATERIAL_HINTS: Record<MaterialType, string> = {
  original: '你自己的观点、实测或真实经历即为依据，不强迫编造外链。',
  research: '引用外部事实、数字或他人的话，需提供来源链接，缺来源的事实会标「待核实」。',
  adaptation: '在已有文章基础上改写，同样要求来源链接。',
};
const FACT_STATUS_LABELS: Record<FactItem['status'], string> = { confirmed: '已确认', unverified: '待核实', disputed: '有争议' };
const CONTENT_LINES: { id: ContentLine; name: string }[] = [
  { id: 'A', name: 'A 线 · 干货方法' }, { id: 'B', name: 'B 线 · 观点态度' }, { id: 'C', name: 'C 线 · 案例故事' },
];
const STORAGE_KEY = 'workbench:quickdraft:v1';

interface SavedState {
  draftText: string; materialType: MaterialType; title: string; audience: string; core_view: string;
  facts: FactItem[]; open_questions: QuestionItem[]; platform_angles: Record<string, string>;
  platforms: Platform[]; content_line: ContentLine; duration: number; sourceLinks: string;
}

function blank(): SavedState {
  return { draftText: '', materialType: 'original', title: '', audience: '', core_view: '', facts: [], open_questions: [], platform_angles: {}, platforms: ['wechat', 'toutiao'], content_line: 'A', duration: 60, sourceLinks: '' };
}

function loadSaved(): SavedState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return blank();
    return { ...blank(), ...JSON.parse(raw) };
  } catch { return blank(); }
}

export default function QuickDraft({ busy, run, flash, onProduced }: { busy: string; run: Run; flash: (message: string, error?: boolean) => void; onProduced: (topicId: string) => void }) {
  const [state, setState] = useState<SavedState>(loadSaved);
  const [extracting, setExtracting] = useState(false);
  const [producing, setProducing] = useState(false);
  const [gaps, setGaps] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ topic_id: string; job_id: string } | null>(null);
  const savedRef = useRef(false);

  const set = (patch: Partial<SavedState>) => setState(prev => ({ ...prev, ...patch }));

  // 自动保存：任何输入变化都写入本机 localStorage，切后台/刷新可恢复。
  useEffect(() => {
    if (!savedRef.current) { savedRef.current = true; return; }
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { /* 忽略存储失败 */ }
  }, [state]);

  const hasVideo = state.platforms.some(p => isVideoPlatform(p));

  const canProduce = state.title.trim() && state.platforms.length > 0;

  const doExtract = async () => {
    if (!state.draftText.trim()) { flash('请先粘贴底稿内容', true); return; }
    setError(''); setGaps([]);
    await run('提炼底稿要点', async () => {
      setExtracting(true);
      try {
        const result = await api<Extraction>('/quick-draft/extract', 'POST', { draft_text: state.draftText, material_type: state.materialType });
        set({ title: result.title || state.title, audience: result.audience, core_view: result.core_view, facts: result.facts, open_questions: result.open_questions, platform_angles: result.platform_angles });
        flash('已提炼要点，可编辑后确认生产');
      } finally { setExtracting(false); }
    });
  };

  const doProduce = async () => {
    if (!canProduce) { flash('请填写标题并选择目标平台，资料由系统自动整理', true); return; }
    setError(''); setGaps([]);
    await run('批准并创作', async () => {
      setProducing(true);
      try {
        const sources = state.sourceLinks.split('\n').map(s => s.trim()).filter(Boolean)
          .filter(s => /^https?:\/\//i.test(s))
          .map(url => ({ url, title: '', note: '', platform: 'unknown' }));
        const payload = {
          candidate_mode: true, title: state.title, material_type: state.materialType, platforms: state.platforms, content_line: state.content_line,
          video_target_duration_seconds: state.duration, sources,
          audience: state.audience, core_view: state.core_view, facts: state.facts, open_questions: state.open_questions,
          author_experience: '', private_info: '', asset_list: [], platform_angles: state.platform_angles, raw_text: state.draftText,
        };
        const res = await api<{ topic: { id: string }; job: { id: string }; fact_base: { gaps: string[] } }>('/quick-draft/produce', 'POST', payload);
        setResult({ topic_id: res.topic.id, job_id: res.job.id });
        flash('创作请求已确认，系统正在整理资料并生成工作稿');
        onProduced(res.topic.id);
      } finally { setProducing(false); }
    });
  };

  const factRows = useMemo(() => state.facts, [state.facts]);
  const updateFact = (index: number, patch: Partial<FactItem>) => set({ facts: state.facts.map((f, i) => i === index ? { ...f, ...patch } : f) });
  const addFact = () => set({ facts: [...state.facts, { claim: '', status: 'unverified', source_url: '', source_date: '' }] });
  const removeFact = (index: number) => set({ facts: state.facts.filter((_, i) => i !== index) });
  const updateQuestion = (index: number, value: string) => set({ open_questions: state.open_questions.map((q, i) => i === index ? { question: value } : q) });
  const addQuestion = () => set({ open_questions: [...state.open_questions, { question: '' }] });
  const removeQuestion = (index: number) => set({ open_questions: state.open_questions.filter((_, i) => i !== index) });

  const busyFlag = !!busy || extracting || producing;

  if (result) {
    return <section className="panel quickdraft-done"><div className="empty"><div className="empty-icon"><FilePlus2 size={26} /></div><h3>创作已开始</h3><p>选题已建立，系统自动整理资料并逐平台生成工作稿。进入内容工坊即可查看已保存内容、继续编辑和定稿。</p><div className="quickdraft-done-meta"><span className="tag muted">选题</span><span>{result.topic_id}</span></div><button className="btn primary" onClick={() => onProduced(result.topic_id)}>前往内容工坊查看<ArrowUpRight size={15} /></button></div></section>;
  }

  return <div className="quickdraft-page">
    <section className="radar-intro quickdraft-intro"><div className="radar-mark"><FilePlus2 size={25} /></div><div><strong>把你的底稿直接变成各平台稿件。</strong><p>粘贴已有内容、填写标题并选择平台，就能开始创作。系统自动整理资料；提炼要点和补充个人观点都是可选操作。</p></div></section>

    {error && <div className="callout error" role="alert">{error}</div>}
    {gaps.length > 0 && <div className="callout warning" role="alert"><strong>事实底稿还有缺口</strong><p>{gaps.join('；')}</p></div>}

    <div className="quickdraft-grid">
      <section className="panel">
        <div className="panel-title"><h2>1 · 粘贴底稿</h2><span className="tag">自动保存到本机</span></div>
        <div className="quickdraft-material"><label className="field"><span>材料类型</span><select aria-label="材料类型" value={state.materialType} onChange={e => set({ materialType: e.target.value as MaterialType })}>{Object.entries(MATERIAL_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select><small>{MATERIAL_HINTS[state.materialType]}</small></label></div>
        <label className="field"><span>底稿原文</span><textarea aria-label="底稿原文" rows={12} placeholder="把你写好的内容、实测经历或要改编的文章粘在这里…" value={state.draftText} onChange={e => set({ draftText: e.target.value })} /></label>
        <button className="btn primary" disabled={busyFlag || !state.draftText.trim()} onClick={() => void doExtract()}>{extracting ? <LoaderCircle className="spin" size={15} /> : <Wand2 size={15} />}{extracting ? '正在提炼…' : '提炼要点'}</button>
      </section>

      <section className="panel">
        <div className="panel-title"><h2>2 · 标题与可选写作方向</h2><span className="tag muted">{state.facts.filter(f => f.status === 'confirmed').length} 条已确认</span></div>
        <label className="field"><span>选题标题</span><input aria-label="选题标题" value={state.title} onChange={e => set({ title: e.target.value })} /></label>
        <label className="field"><span>受众问题（选填）</span><textarea aria-label="受众问题" rows={2} value={state.audience} onChange={e => set({ audience: e.target.value })} /></label>
        <label className="field"><span>建议写作角度（选填）</span><textarea aria-label="核心观点" rows={2} value={state.core_view} onChange={e => set({ core_view: e.target.value })} /></label>

        <div className="quickdraft-facts">
          <div className="quickdraft-facts-head"><strong>逐项事实</strong><button className="text-link" onClick={addFact}><Plus size={13} />添加</button></div>
          {factRows.length === 0 && <p className="muted">系统会在创作时自动整理资料；这里可选补充已知事实。</p>}
          {factRows.map((fact, index) => <div className="quickdraft-fact" key={index}>
            <input aria-label={`事实 ${index + 1}`} value={fact.claim} placeholder="事实主张" onChange={e => updateFact(index, { claim: e.target.value })} />
            <select aria-label={`事实状态 ${index + 1}`} value={fact.status} onChange={e => updateFact(index, { status: e.target.value as FactItem['status'] })}>{Object.entries(FACT_STATUS_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
            {fact.status !== 'confirmed' && <input aria-label={`事实来源 ${index + 1}`} value={fact.source_url} placeholder="来源链接（可留空，标待核实）" onChange={e => updateFact(index, { source_url: e.target.value })} />}
            <input aria-label={`事实日期 ${index + 1}`} value={fact.source_date} placeholder="日期" onChange={e => updateFact(index, { source_date: e.target.value })} />
            <button className="icon-btn" aria-label={`删除事实 ${index + 1}`} onClick={() => removeFact(index)}><Trash2 size={14} /></button>
          </div>)}
        </div>

        <div className="quickdraft-facts">
          <div className="quickdraft-facts-head"><strong>待核问题</strong><button className="text-link" onClick={addQuestion}><Plus size={13} />添加</button></div>
          {state.open_questions.map((q, index) => <div className="quickdraft-fact" key={index}><input aria-label={`待核问题 ${index + 1}`} value={q.question} placeholder="写之前仍需核实的点" onChange={e => updateQuestion(index, e.target.value)} /><button className="icon-btn" aria-label={`删除问题 ${index + 1}`} onClick={() => removeQuestion(index)}><Trash2 size={14} /></button></div>)}
        </div>
      </section>
    </div>

    <section className="panel">
      <div className="panel-title"><h2>3 · 选择生产范围</h2></div>
      <div className="quickdraft-scope">
        <label className="field"><span>目标平台</span><div className="platform-checks">{allPlatforms.map(p => <label key={p}><input type="checkbox" checked={state.platforms.includes(p)} onChange={e => set({ platforms: e.target.checked ? [...state.platforms, p] : state.platforms.filter(x => x !== p) })} /><span className={`platform-dot ${p}`} />{platformNames[p]}</label>)}</div></label>
        <label className="field"><span>内容线</span><select aria-label="内容线" value={state.content_line} onChange={e => set({ content_line: e.target.value as ContentLine })}>{CONTENT_LINES.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
        {hasVideo && <label className="field"><span>视频目标时长（秒）</span><input aria-label="视频目标时长" type="number" min={10} max={600} value={state.duration} onChange={e => set({ duration: Number(e.target.value) })} /></label>}
        {state.materialType !== 'original' && <label className="field"><span>来源链接（每行一个，选填）</span><textarea aria-label="来源链接" rows={3} placeholder="https://…" value={state.sourceLinks} onChange={e => set({ sourceLinks: e.target.value })} /><small>外部事实、数字与引用需要来源；没有依据的断言会省略或作为待核提示，不阻止保存工作稿。</small></label>}
        <p className="muted">图文正文长度按内容需要由生成规则安排；视频平台产出结构化脚本（成片导入与草稿能力见后续批次）。</p>
      </div>
      <div className="quickdraft-actions"><button className="btn primary" disabled={busyFlag || !canProduce} onClick={() => void doProduce()}>{producing ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}{producing ? '正在建立创作任务…' : '批准并创作'}</button>{!canProduce && <small>填写标题并选择目标平台即可开始，其他资料会自动整理。</small>}</div>
    </section>
  </div>;
}
