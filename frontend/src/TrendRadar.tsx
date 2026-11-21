import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Clock3, ExternalLink, LoaderCircle, Plus, Radar, RefreshCw, Save, Search, Settings2, TrendingUp } from 'lucide-react';
import { api } from './api';
import type { Platform, Topic, TrendCandidate, TrendClassification, TrendDirection, TrendRadarState, TrendRefresh, TrendSettings, TrendSourcePlatform } from './types';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;
import { PLATFORM_IDS as platforms, PLATFORM_NAMES as platformNames } from './platforms';
const directionNames: Record<TrendDirection, string> = { ai: 'AI', tech: '科技', society: '社会', life: '生活' };
const errorCategoryNames: Record<string, string> = { timeout: '超时', http: 'HTTP 错误', parse: '解析失败', empty: '空内容', stale: '缓存过期', connection: '连接失败', other: '其他' };
function compareCandidates(a: TrendCandidate, b: TrendCandidate) { return b.score.total - a.score.total || b.last_seen_at.localeCompare(a.last_seen_at); }
const classes: Record<TrendClassification, { label: string; tone: string; note: string }> = {
  hot: { label: '已上榜', tone: 'amber', note: '有公开榜单记录；具体名次与抓取时间见下方。' },
  rising: { label: '升温信号', tone: 'green', note: '同一来源的多次记录显示榜位上升或指数增加，仍需持续跟踪。' },
  watch: { label: '潜在线索', tone: 'blue', note: '值得提前跟踪的新消息或变化；尚无足够热度证据，不能保证成为热点。' },
  context: { label: '背景资料', tone: 'muted', note: '适合补充背景，当前不作为热点判断。' },
};
function time(value?: string | null) { if (!value) return '未知'; if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value; const date = new Date(value); return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); }
function validURL(value: string) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; } }
function sourcePlatform(value?: TrendSourcePlatform) { return value === 'xhs' ? 'xiaohongshu' : value || 'external'; }
function sourcePlatformName(value?: TrendSourcePlatform) { const platform = sourcePlatform(value); return platform === 'external' ? '外部公开来源' : `${platformNames[platform]}公开来源`; }
function hasPlatformSource(candidate: TrendCandidate, platform: Platform) { return candidate.sources.some(source => sourcePlatform(source.platform) === platform); }
function SourceLink({ url, children }: { url: string; children: React.ReactNode }) { const href = validURL(url); return href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}<ExternalLink size={11} /></a> : <span>{children}</span>; }

export default function TrendRadar({ busy, run, flash, onSelected }: { busy: string; run: Run; flash: (message: string, error?: boolean) => void; onSelected: (id: string) => void }) {
  const [data, setData] = useState<TrendRadarState | null>(null);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<'all' | TrendClassification>('all');
  const [platformFilter, setPlatformFilter] = useState<'all' | Platform>('all');
  const [directionFilter, setDirectionFilter] = useState<'all' | TrendDirection>('all');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [contentLineFilter, setContentLineFilter] = useState<'all' | 'A' | 'B' | 'C'>('all');
  const [poolFilter, setPoolFilter] = useState<'all' | 'recommended' | 'needs_source'>('all');
  const [showIgnored, setShowIgnored] = useState(false);
  const [displayLimit, setDisplayLimit] = useState(12);
  const [showSettings, setShowSettings] = useState(false);
  const [form, setForm] = useState<TrendSettings | null>(null);
  const [keywords, setKeywords] = useState('');
  const [formError, setFormError] = useState('');
  const inFlight = useRef(false);
  const initialized = useRef(false);
  const seenRun = useRef<string | null>(null);
  const sourcePanel = useRef<HTMLElement | null>(null);
  const load = useCallback(async () => {
    const result = await api<TrendRadarState>('/trends');
    setData(result); setError('');
    if (!initialized.current) { initialized.current = true; setForm(result.settings); setKeywords(result.settings.keywords.join('、')); }
    return result;
  }, []);
  useEffect(() => { setDisplayLimit(12); }, [filter, search, showIgnored, platformFilter, directionFilter, sourceFilter, contentLineFilter, poolFilter]);
  useEffect(() => {
    let live = true;
    const read = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const result = await load();
        if (live && result.refresh.run_id && result.refresh.run_id === seenRun.current && result.refresh.state !== 'running') {
          seenRun.current = null;
          const abnormal = result.refresh.state === 'failed' || result.refresh.state === 'partial';
          const message = result.refresh.state === 'failed'
            ? result.refresh.error || '本次热点抓取失败，请查看来源状态。'
            : result.refresh.state === 'partial'
              ? result.refresh.error || '部分来源抓取失败，请查看来源状态。'
              : '热点抓取完成，可查看候选与来源状态。';
          flash(message, abnormal);
        }
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : '无法读取热点雷达');
      } finally { inFlight.current = false; }
    };
    void read();
    const id = window.setInterval(() => void read(), 3000);
    return () => { live = false; window.clearInterval(id); };
  }, [load, flash]);
  const refresh = async () => {
    await run('启动热点抓取', async () => { const result = await api<TrendRefresh>('/trends/refresh', 'POST', {}); if (result.state === 'running') { seenRun.current = result.run_id; flash('热点抓取已启动，结果会在完成后更新。'); } else if (result.state === 'failed') { throw new Error(result.error || '热点抓取未能启动'); } await load(); });
  };
  const saveSettings = async (event: React.FormEvent) => {
    event.preventDefault(); if (!form) return;
    const words = Array.from(new Set(keywords.split(/[、,，\n]/).map(s => s.trim()).filter(Boolean)));
    if (words.length > 20 || words.some(s => s.length > 60)) { setFormError('最多 20 个关键词，每个不超过 60 字。'); return; }
    if (!Number.isInteger(form.interval_minutes) || form.interval_minutes < 15 || form.interval_minutes > 1440 || !Number.isInteger(form.window_days) || form.window_days < 1 || form.window_days > 30) { setFormError('监测间隔需为 15～1440 分钟，消息窗口需为 1～30 天的整数。'); return; }
    if (form.enabled_sources.length === 0) { setFormError('请至少选择一个监测来源。'); return; }
    setFormError('');
    await run('保存热点监测设置', async () => { await api('/trends/settings', 'POST', { ...form, keywords: words }); const result = await load(); setForm(result.settings); setKeywords(result.settings.keywords.join('、')); }, '热点监测设置已保存。');
  };
  const select = async (candidate: TrendCandidate, selectedPlatforms: Platform[]) => {
    if (candidate.topic_id) { onSelected(candidate.topic_id); return; }
    let topicId = '';
    const ok = await run('加入待确认选题', async () => { const result = await api<{ topic: Topic; already_selected: boolean }>(`/trends/${candidate.id}/select`, 'POST', { platforms: selectedPlatforms }); topicId = result.topic.id; await load(); flash(result.already_selected ? '这条候选已有选题，已打开原记录。' : '已加入待确认选题；批准后才会开始创作。'); });
    if (ok && topicId) onSelected(topicId);
  };
  const ignore = async (candidate: TrendCandidate) => { await run('暂不关注该候选', async () => { await api(`/trends/${candidate.id}/ignore`, 'POST', {}); await load(); }, '已移入暂不关注，可勾选显示记录。'); };
  if (!data) return <section className="panel radar-loading">{error ? <div className="callout error" role="alert"><strong>热点雷达暂时无法读取</strong><p>{error}</p><button className="btn" onClick={() => void load().catch(e => setError(String(e.message || e)))}><RefreshCw size={14} />重新读取</button></div> : <div className="loading-line"><LoaderCircle className="spin" size={17} />正在读取实际监测结果…</div>}</section>;
  const running = data.refresh.state === 'running';
  const candidates = data.candidates.filter(c =>
    (showIgnored || c.status !== 'ignored')
    && (filter === 'all' || c.classification === filter)
    && (directionFilter === 'all' || c.direction === directionFilter || (c.article_tags || []).includes(directionFilter))
    && (contentLineFilter === 'all' || (c.score.content_lines || []).some(cl => cl.line === contentLineFilter))
    && (poolFilter === 'all' || (poolFilter === 'recommended' ? !!c.score.publishable : !c.score.publishable))
    && (sourceFilter === 'all' || c.sources.some(source => source.source_id === sourceFilter))
    && `${c.title} ${c.suggested_title} ${c.summary}`.toLowerCase().includes(search.toLowerCase()),
  );
  candidates.sort((a, b) => platformFilter === 'all'
    ? compareCandidates(a, b)
    : Number(hasPlatformSource(b, platformFilter)) - Number(hasPlatformSource(a, platformFilter)) || compareCandidates(a, b));
  const orderedCandidates = candidates;
  const sourceCounts = data.refresh.source_counts;
  const visible = data.candidates.filter(c => c.status !== 'ignored');
  const activeSources = data.sources.filter(s => data.settings.enabled_sources.includes(s.id));
  const failedSources = activeSources.filter(s => s.status === 'error');
  const platformSource = data.platform_sources?.find(source => sourcePlatform(source.platform) === platformFilter);
  return <div className="radar-page">
    <section className="radar-intro"><div className="radar-mark"><Radar size={25} /></div><div><strong>抓住正在发生的事，也留意刚出现的变化。</strong><p>公开热榜提供当前信号；持续快照与新闻源帮助发现升温线索。候选先进入待确认选题。</p></div><button className="btn primary" disabled={!!busy || running} onClick={() => void refresh()}>{running ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}{running ? '正在抓取…' : '立即抓取'}</button></section>
    <div className="radar-monitor-strip"><span><span className={`status-dot ${data.settings.enabled ? 'on' : ''}`} />自动监测{data.settings.enabled ? '已开启' : '已关闭'}{data.settings.enabled && ` · 每 ${data.settings.interval_minutes} 分钟`}</span><span><Clock3 size={12} />最近完成：{time(data.refresh.last_finished_at)}</span><span>下次计划：{data.settings.enabled ? time(data.refresh.next_refresh_at) : '未安排'}</span>{sourceCounts && <span className="radar-run-counts">本轮：成功 {sourceCounts.ok} · 失败 {sourceCounts.error} · 未到期 {sourceCounts.not_due}</span>}{data.funnel && data.funnel.collected > 0 && <span className="radar-run-counts">漏斗：采到 {data.funnel.collected} · 保留 {data.funnel.kept} · 重复 {data.funnel.duplicates} · 丢弃 {data.funnel.dropped}</span>}<button className="text-link radar-source-shortcut" onClick={() => { sourcePanel.current?.scrollIntoView({ block: 'start' }); sourcePanel.current?.focus({ preventScroll: true }); }}>查看来源状态<ArrowUpRight size={13} /></button><button className="text-link" aria-expanded={showSettings} onClick={() => setShowSettings(!showSettings)}><Settings2 size={14} />{showSettings ? '收起设置' : '调整监测'}</button></div>
    {error && <div className="callout error" role="alert">读取更新失败：{error}。以下为上次读取的结果。</div>}
    {data.refresh.state === 'failed' && <div className="callout error" role="alert">最近一次抓取失败：{data.refresh.error || '请查看下方来源状态。'} 旧候选保留，其时间不会刷新。</div>}
    {data.refresh.state === 'partial' && <div className="callout warning" role="alert">最近一次抓取部分成功：{data.refresh.error || '请查看下方来源状态。'} 已成功来源正常更新，旧候选继续保留。</div>}
    {failedSources.length > 0 && <div className="callout warning radar-source-warning">{failedSources.length} / {activeSources.length} 个已启用来源抓取失败，候选覆盖不完整。可在来源状态中查看具体原因。</div>}
    {showSettings && form && <form className="panel radar-settings" onSubmit={event => void saveSettings(event)}><div className="panel-title"><h2>热点监测设置</h2><span className="tag">服务运行时自动监测</span></div><div className="radar-settings-grid"><div><label className="check-line"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />开启自动监测</label><label className="field"><span>关注范围</span><select aria-label="关注范围" value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value as TrendSettings['scope'] })}><option value="broad">科技与社会热点</option><option value="ai">AI 工具与知识工作</option></select></label><div className="two-fields"><label className="field"><span>监测间隔（分钟）</span><input aria-label="监测间隔（分钟）" type="number" min="15" max="1440" step="1" value={form.interval_minutes} onChange={e => setForm({ ...form, interval_minutes: Number(e.target.value) })} /></label><label className="field"><span>消息窗口（天）</span><input aria-label="消息窗口（天）" type="number" min="1" max="30" step="1" value={form.window_days} onChange={e => setForm({ ...form, window_days: Number(e.target.value) })} /></label></div><label className="field"><span>关注关键词</span><textarea aria-label="关注关键词" rows={2} value={keywords} placeholder="例如：机器人、就业、教育、消费" onChange={e => setKeywords(e.target.value)} /><small>逗号、顿号或换行分隔；关键词用于提高相关候选的推荐优先级。</small></label></div><fieldset className="radar-source-checks"><legend>监测来源</legend>{data.sources.map(source => <label key={source.id}><input type="checkbox" checked={form.enabled_sources.includes(source.id)} onChange={e => setForm({ ...form, enabled_sources: e.target.checked ? [...form.enabled_sources, source.id] : form.enabled_sources.filter(id => id !== source.id) })} /><span>{source.name}<small>{source.kind === 'ranking' ? '公开热点' : '新闻 / 官方消息'}</small></span></label>)}<p>来源不可达时保留错误与原始时间。监测需要本机服务持续运行；电脑休眠时暂停。</p></fieldset></div>{formError && <p className="form-error" role="alert">{formError}</p>}<button className="btn primary" type="submit" disabled={!!busy}><Save size={15} />保存监测设置</button></form>}
    <div className="radar-counts">{(['hot', 'rising', 'watch'] as TrendClassification[]).map(key => <button key={key} onClick={() => setFilter(filter === key ? 'all' : key)} className={filter === key ? 'selected' : ''}><span>{classes[key].label}</span><strong>{visible.filter(c => c.classification === key).length}</strong><small>{key === 'hot' ? '有当前榜单依据' : key === 'rising' ? '仍需后续验证' : '尚未证明成为热点'}</small></button>)}</div>
    <div className="radar-platform-target"><label><span>目标发布平台</span><select aria-label="热点目标发布平台" value={platformFilter} onChange={event => setPlatformFilter(event.target.value as 'all' | Platform)}><option value="all">全部平台 · 六维评分</option>{platforms.map(platform => <option key={platform} value={platform}>{platformNames[platform]} · 本平台来源优先</option>)}</select></label><p>选定平台后，本平台线索先显示，其他来源继续保留；加入选题时仍可调整发布平台。</p></div>
    {data.platform_sources?.length ? <div className="radar-platform-availability">{data.platform_sources.map(source => <span key={source.platform}><i className={`platform-dot ${sourcePlatform(source.platform)}`} />{platformNames[sourcePlatform(source.platform) as Platform] || source.platform}：{source.available ? '公开来源已接入' : '自有公开来源未接入'}</span>)}</div> : <p className="radar-platform-unknown">发布平台来源状态暂未提供，候选来源以各条记录为准。</p>}
    {platformFilter !== 'all' && <div className={`callout ${platformSource?.available ? '' : 'warning'} radar-platform-note`}><strong>{platformSource?.available ? `${platformNames[platformFilter]}本平台来源优先` : `${platformNames[platformFilter]}自有公开来源${platformSource ? '未接入' : '待确认'}`}</strong><p>{platformSource?.reason || '尚无可核对的本平台采集能力记录，以下外部线索不能当作该平台热点证据。'}</p>{platformSource?.checked_at && <small>来源检查：{time(platformSource.checked_at)}</small>}</div>}
    <div className="radar-filter"><div className="filter-tabs">{([{ id: 'all', label: '全部候选' }, ...Object.entries(classes).map(([id, value]) => ({ id, label: value.label }))] as { id: 'all' | TrendClassification; label: string }[]).map(item => <button key={item.id} className={filter === item.id ? 'active' : ''} onClick={() => setFilter(item.id)}>{item.label}</button>)}</div><label className="radar-search"><Search size={15} /><input aria-label="搜索热点候选" placeholder="搜索候选…" value={search} onChange={e => setSearch(e.target.value)} /></label></div>
    <div className="radar-discovery-controls" aria-label="候选筛选与排序">
      <label><span>内容方向</span><select aria-label="候选内容方向" value={directionFilter} onChange={event => setDirectionFilter(event.target.value as 'all' | TrendDirection)}><option value="all">全部方向</option>{Object.entries(directionNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      <label><span>候选来源</span><select aria-label="候选来源" value={sourceFilter} onChange={event => setSourceFilter(event.target.value)}><option value="all">全部来源</option>{activeSources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
      <label><span>内容线</span><select aria-label="候选内容线" value={contentLineFilter} onChange={event => setContentLineFilter(event.target.value as 'all' | 'A' | 'B' | 'C')}><option value="all">全部内容线</option><option value="A">A · 实践工作流</option><option value="B">B · 工具实测</option><option value="C">C · 项目复盘</option></select></label>
      <label><span>线索池</span><select aria-label="候选线索池" value={poolFilter} onChange={event => setPoolFilter(event.target.value as 'all' | 'recommended' | 'needs_source')}><option value="all">推荐池 + 待补原文</option><option value="recommended">推荐池（有原始出处）</option><option value="needs_source">待补原文线索</option></select></label>
    </div>
    <div className="radar-list-heading"><span>{orderedCandidates.length} 条候选 · 按六维综合评分{platformFilter !== 'all' && ` · ${platformNames[platformFilter]}来源优先`}</span><label><input type="checkbox" checked={showIgnored} onChange={e => setShowIgnored(e.target.checked)} />显示暂不关注</label></div>
    <div className="radar-layout"><section className="radar-candidates" aria-label="热点候选">{candidates.length === 0 ? <div className="panel empty"><div className="empty-icon"><Radar size={25} /></div><h3>{search || filter !== 'all' ? '没有匹配候选' : running ? '正在收集热点线索' : '还没有可用候选'}</h3><p>{search || filter !== 'all' ? '调整筛选或搜索词。' : '抓取完成后显示实际来源与证据；来源不可达时不会用演示内容补齐。'}</p></div> : orderedCandidates.slice(0, displayLimit).map(candidate => <CandidateCard key={candidate.id + "-" + platformFilter} candidate={candidate} preferredPlatform={platformFilter === "all" ? undefined : platformFilter} busy={!!busy || running} select={selected => void select(candidate, selected)} ignore={() => void ignore(candidate)} />)}{orderedCandidates.length > displayLimit && <div className="radar-load-more"><span>已显示 {Math.min(displayLimit, orderedCandidates.length)} / {orderedCandidates.length} 条候选</span><button className="btn" onClick={() => setDisplayLimit(value => value + 12)}>再显示 12 条</button></div>}</section><aside ref={sourcePanel} tabIndex={-1} className="panel radar-sources"><div className="panel-title"><h2>来源状态</h2><span className="tag">{activeSources.filter(s => s.status === 'ok').length} / {activeSources.length} 可用</span></div>{data.sources.map(source => <div className={`radar-source ${data.settings.enabled_sources.includes(source.id) ? '' : 'disabled'}`} key={source.id}><div><SourceLink url={source.url}>{source.name}</SourceLink><span className={`tag ${!data.settings.enabled_sources.includes(source.id) ? 'muted' : source.status === 'ok' ? 'green' : source.status === 'error' ? 'red' : 'muted'}`}>{!data.settings.enabled_sources.includes(source.id) ? '未启用' : source.status === 'ok' ? '正常' : source.status === 'error' ? '失败' : source.status === 'not_due' ? '未到间隔' : '待抓取'}</span></div><p>{data.tier_labels?.[source.tier] || '来源'} · {source.kind === 'ranking' ? '公开热点' : '新闻 / 官方消息'} · {source.item_count} 条{source.last_run?.status ? ` · 本轮保留 ${source.last_run.kept} / 重复 ${source.last_run.duplicates} / 丢弃 ${source.last_run.dropped}` : ''}</p><small>抓取时间：{time(source.last_fetched_at)}{source.next_at ? ` · 下次不早于 ${time(source.next_at)}` : ''}</small>{source.status === 'not_due' && <p className="radar-source-note">本轮未到采集间隔，沿用上次结果。</p>}{source.health?.degraded && <p className="radar-source-error">连续 {source.health.consecutive_failures} 次未成功，已放慢采集；仍会自动重试。{source.health.error_category ? ` 类型：${errorCategoryNames[source.health.error_category] || source.health.error_category}` : ''}{source.health.last_error ? ` · 最近一次：${source.health.last_error}` : ''}</p>}{source.error && <p className="radar-source-error">{source.error}{source.last_run?.error_category ? `（${errorCategoryNames[source.last_run.error_category] || source.last_run.error_category}）` : ''}</p>}</div>)}<div className="radar-boundary"><TrendingUp size={15} /><p>优先级用于安排选题审阅；“升温”需要连续观测支持，“潜在线索”只是值得跟进的变化。任何等级都不保证未来成为热点。</p></div></aside></div>
  </div>;
}

function CandidateCard({ candidate, busy, select, ignore, preferredPlatform }: { candidate: TrendCandidate; busy: boolean; select: (value: Platform[]) => void; ignore: () => void; preferredPlatform?: Platform }) {
  const [selected, setSelected] = useState<Platform[]>(preferredPlatform ? [preferredPlatform] : candidate.recommended_platforms);
  const [expanded, setExpanded] = useState(false);
  const classification = classes[candidate.classification] || classes.watch;
  const contentLines = candidate.score.content_lines || [];
  const hasHeat = candidate.sources.some(s => s.heat_value !== null && s.heat_value !== undefined);
  const scoreText = `六维综合 ${candidate.score.total}/100`;
  return <article className={`panel radar-candidate ${candidate.status === 'ignored' ? 'ignored' : ''}`}>
    <div className="radar-candidate-top"><span className={`tag ${classification.tone}`}>{classification.label}</span><span className="tag muted">{directionNames[candidate.direction] || '未分类'}</span>{candidate.article_tags?.length > 0 && <span className="tag blue">文章 · {candidate.article_tags.map(t => directionNames[t] || t).join('、')}</span>}{contentLines.length > 0 && <span className="tag blue">内容线 · {contentLines.slice(0, 2).map(cl => cl.name).join('、')}</span>}{!candidate.score.publishable && <span className="tag amber">待补原文</span>}{preferredPlatform && <span className={`tag ${hasPlatformSource(candidate, preferredPlatform) ? 'green' : 'amber'}`}>{hasPlatformSource(candidate, preferredPlatform) ? `${platformNames[preferredPlatform]}本平台线索` : '跨平台线索 · 审核出处'}</span>}{candidate.status === 'saved' && <span className="tag green">已加入选题</span>}{candidate.status === 'ignored' && <span className="tag">暂不关注</span>}<small className="radar-score">{scoreText}</small></div>
    <h3><SourceLink url={candidate.url}>{candidate.title}</SourceLink></h3>
    {candidate.summary && <p className="radar-summary">{candidate.summary}</p>}
    <button className="text-link radar-expand" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起详情' : '查看详情与判断依据'}</button>
    {expanded && <>
    <div className="radar-dates"><span>发布时间：{time(candidate.published_at)}</span><span>首次发现：{time(candidate.first_seen_at)}</span><span>最近发现：{time(candidate.last_seen_at)}</span></div>
    <div className="radar-evidence"><strong>判断依据</strong><p>{classification.note}</p>{candidate.reasons.length > 0 && <ul>{candidate.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}<div className="radar-evidence-sources">{candidate.sources.map((source, index) => <div key={`${source.source_id}-${index}`}><SourceLink url={source.url}>{source.source_name}</SourceLink><span>{sourcePlatformName(source.platform)}</span><span>{source.rank === null || source.rank === undefined ? '名次未提供' : `榜单第 ${source.rank} 名`}</span><span>{source.heat_value === null || source.heat_value === undefined ? '热度未提供' : `${source.heat_label || '来源热度'} ${source.heat_value.toLocaleString('zh-CN')}`}</span><small>抓取 {time(source.fetched_at)}</small></div>)}</div>{!hasHeat && <small className="radar-unknown">未取得可比热度数据；RSS 新闻不等于公众热度。</small>}{candidate.uncertainties.length > 0 && <ul className="radar-uncertainties">{candidate.uncertainties.map((uncertainty, index) => <li key={index}>{uncertainty}</li>)}</ul>}</div>
    <div className="radar-angle"><span>建议切入</span><strong>{candidate.suggested_title || candidate.title}</strong>{candidate.angle && <p>{candidate.angle}</p>}</div>
    </>}
    {candidate.topic_id ? <div className="radar-card-actions"><button className="btn" disabled={busy} onClick={() => select(selected)}>查看选题<ArrowUpRight size={14} /></button><small>保留已有记录，不会重复创建。</small></div> : <><div className="radar-platforms">{platforms.map(platform => <label key={platform}><input aria-label={`${candidate.title}：${platformNames[platform]}`} type="checkbox" checked={selected.includes(platform)} onChange={e => setSelected(e.target.checked ? [...selected, platform] : selected.filter(p => p !== platform))} />{platformNames[platform]}</label>)}</div><div className="radar-card-actions"><button className="btn primary" disabled={busy || selected.length === 0} onClick={() => select(selected)}><Plus size={14} />加入待确认选题</button>{candidate.status !== 'ignored' && <button className="text-link" disabled={busy} onClick={ignore}>暂不关注</button>}<small>加入后需人工批准，再开始创作。</small></div></>}
  </article>;
}
