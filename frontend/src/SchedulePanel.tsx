import { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarClock, Play, RefreshCw, Trash2 } from 'lucide-react';
import { api } from './api';
import type { ProductionOverview, ProductionSlotRow, Topic } from './types';
import './schedule.css';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;

const slotStates: Record<string, { label: string; tone: string }> = {
  scheduled: { label: '待生产', tone: 'amber' },
  queued: { label: '已入队', tone: 'amber' },
  produced: { label: '已产出', tone: 'green' },
  skipped: { label: '已跳过', tone: 'red' },
  expired: { label: '已过期', tone: 'red' },
  missed: { label: '已错过', tone: 'red' },
  cancelled: { label: '已取消', tone: 'muted' },
};
const runStates: Record<string, string> = { running: '进行中', succeeded: '已完成', failed: '失败', stopped: '被中断' };

function beijingToday() {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  const pick = (type: string) => parts.find(part => part.type === type)?.value || '';
  return `${pick('year')}-${pick('month')}-${pick('day')}`;
}

function time(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value
    : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

export default function SchedulePanel({ topics, busy, run }: { topics: Topic[]; busy: string; run: Run }) {
  const [data, setData] = useState<ProductionOverview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [topicId, setTopicId] = useState('');
  const [batchDate, setBatchDate] = useState(beijingToday);
  const [note, setNote] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    void api<ProductionOverview>('/production')
      .then(result => { if (active) setData(result); })
      .catch(reason => { if (active) { setError(reason instanceof Error ? reason.message : '排期读取失败'); setData(null); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);

  const approved = useMemo(() => topics.filter(topic => topic.status === 'approved'), [topics]);
  const titleOf = useCallback((id: string) => topics.find(topic => topic.id === id)?.title || id, [topics]);
  const after = useCallback(() => setReload(value => value + 1), []);

  const submit = async () => {
    if (!topicId) return;
    const ok = await run('排入生产计划',
      () => api(`/topics/${topicId}/schedule`, 'POST', { batch_date: batchDate, note, expected_revision: null }),
      `已排入 ${batchDate} 的生产计划。`);
    if (ok) { setNote(''); after(); }
  };

  const cancel = async (slot: ProductionSlotRow) => {
    if (!window.confirm(`取消「${titleOf(slot.topic_id)}」在 ${slot.batch_date} 的排期？`)) return;
    const ok = await run('取消排期', () => api(`/production/slots/${slot.id}`, 'DELETE'), '排期已取消。');
    if (ok) after();
  };

  const runNow = async () => {
    if (!window.confirm('立刻跑一次批次判断？到点的排期会真的建立生成任务并进入 worker 队列，不只是预览。')) return;
    const ok = await run('立刻跑一次批次', () => api('/production/run', 'POST', {}), '批次判断已执行，结果见下方。');
    if (ok) after();
  };

  const production = data?.production;
  const scheduler = data?.scheduler;
  const slots = data?.slots || [];
  const today = production?.today;

  return <section className="panel schedule-panel" aria-label="每日排期" aria-busy={loading}>
    <div className="panel-title">
      <div><h2>每日排期</h2><small>已批准的选题按北京时间排队，到点由工作台自动生产并存入平台草稿箱</small></div>
      <div className="schedule-actions">
        <button type="button" className="btn small" disabled={loading} onClick={() => setReload(value => value + 1)}><RefreshCw size={14} />刷新</button>
        <button type="button" className="btn small" disabled={!!busy || loading} onClick={() => void runNow()}><Play size={14} />立刻跑一次</button>
      </div>
    </div>

    {error && <p className="callout warning" role="alert">{error}</p>}
    {loading && !data && <p role="status">正在读取排期与批次记录…</p>}

    {production && scheduler && <>
      <div className="schedule-metrics">
        <div><span>定时器</span><strong className={scheduler.running ? 'ok' : 'off'}>{scheduler.running ? '运行中' : '未运行'}</strong></div>
        <div><span>每日生产时刻</span><strong>{String(scheduler.production_hour_local).padStart(2, '0')}:00</strong><small>{scheduler.timezone}</small></div>
        <div><span>今日配额</span><strong>{today ? `${today.quota_used} / ${today.quota_limit}` : '—'}</strong><small>{today ? `剩余 ${today.quota_remaining} 个` : ''}</small></div>
        <div><span>选题间错峰</span><strong>{production.limits.stagger_minutes} 分钟</strong><small>{production.limits.stagger_enforced ? '已生效' : '未生效'}</small></div>
      </div>

      {!scheduler.running && <p className="callout warning">定时器当前没有运行，服务启动后才会按点生产。服务停止或电脑关机期间错过排期不会补发。</p>}

      <div className="schedule-form">
        <label className="field"><span>选题</span>
          <select value={topicId} onChange={event => setTopicId(event.target.value)} disabled={!!busy}>
            <option value="">{approved.length ? '选择一个已批准选题' : '没有已批准的选题'}</option>
            {approved.map(topic => <option key={topic.id} value={topic.id}>{topic.title}</option>)}
          </select>
          <small>只有人工批准过的选题可以排期；批准本身不等于排期。</small>
        </label>
        <label className="field"><span>生产日期</span>
          <input type="date" value={batchDate} onChange={event => setBatchDate(event.target.value)} disabled={!!busy} />
          <small>按北京时间计算，早于今天的日期会直接判为错过。</small>
        </label>
        <label className="field"><span>备注（可选）</span>
          <input type="text" value={note} maxLength={200} placeholder="例如：等这条新闻的第二篇跟进" onChange={event => setNote(event.target.value)} disabled={!!busy} />
        </label>
        <button type="button" className="btn primary schedule-submit" disabled={!topicId || !!busy} onClick={() => void submit()}><CalendarClock size={15} />排入生产</button>
      </div>

      <div className="schedule-list">
        <div className="schedule-list-title"><h3>排期记录 <small>{slots.length}</small></h3><span>最近批次：{production.recent_runs[0] ? `${production.recent_runs[0].batch_date} · ${runStates[production.recent_runs[0].state] || production.recent_runs[0].state}` : '还没有批次'}</span></div>
        {slots.length === 0 ? <p className="muted-note">还没有排期。批准一个选题后在上方排入生产日期，工作台会在当天到点生成内容并存入各平台草稿箱。</p>
          : <table className="schedule-table">
            <thead><tr><th>生产日期</th><th>选题</th><th>状态</th><th>说明</th><th>更新时间</th><th aria-label="操作" /></tr></thead>
            <tbody>{slots.map(slot => {
              const meta = slotStates[slot.state] || { label: slot.state, tone: 'muted' };
              const canCancel = slot.state === 'scheduled' || slot.state === 'queued';
              return <tr key={slot.id}>
                <td className="mono">{slot.batch_date}</td>
                <td className="schedule-topic">{titleOf(slot.topic_id)}</td>
                <td><span className={`tag ${meta.tone}`}>{meta.label}</span></td>
                <td className="schedule-note">{slot.note || '—'}</td>
                <td className="mono">{time(slot.updated_at)}</td>
                <td>{canCancel && <button type="button" className="btn small danger" disabled={!!busy} title="取消这条排期" onClick={() => void cancel(slot)}><Trash2 size={13} />取消</button>}</td>
              </tr>;
            })}</tbody>
          </table>}
      </div>

      <p className="subtle-note">{production.note} 批次只建立生成任务；正文生成后由交付队列保存到各平台草稿箱，公开发布仍需要你在平台后台自己点。</p>
    </>}
  </section>;
}
