import { useCallback, useEffect, useState } from 'react';
import { Check, Sparkles, X } from 'lucide-react';
import { api } from './api';
import type { Platform, Topic } from './types';
import './format.css';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;

interface Row {
  content_form: string; label: string; complexity: string;
  blocked: boolean; reason: string; score: number; signals: string[];
}
interface Advice {
  platform: Platform; recommended: string; confidence: number; reason: string;
  alternatives: string[]; status: string; rule_ranked: string[]; notes: string;
  evaluation_notes: string[]; feasible: Row[]; blocked: Row[];
  catalog: Record<string, string>; adopted: string; decided_by: string;
  decided_at: string | null; stale: boolean;
}
interface Payload { topic_id: string; generated_at: string; platforms: Record<Platform, Advice> }

import { PLATFORM_NAMES as names, PLATFORM_IDS as order } from './platforms';
const statusText: Record<string, string> = {
  local: '本地模型推荐', unavailable: '规则排序（未调用模型）',
  failed: '模型输出不可用，已改用规则排序', rule_only: '没有可行形式',
};

export default function FormatAdvisor({ topic, busy, run }: { topic: Topic | null; busy: string; run: Run }) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const topicId = topic?.id || '';

  const load = useCallback(() => {
    if (!topicId) return;
    setLoading(true);
    setError('');
    void api<Payload>(`/topics/${topicId}/formats`)
      .then(setData)
      .catch(reason => { setError(reason instanceof Error ? reason.message : '形式建议读取失败'); setData(null); })
      .finally(() => setLoading(false));
  }, [topicId]);

  useEffect(() => { load(); }, [load, topic?.revision]);

  if (!topic) return null;

  const targets = order.filter(platform => topic.platforms.includes(platform));
  const labelOf = (payload: Payload, platform: Platform, id: string) =>
    payload.platforms[platform]?.catalog?.[id] || id;

  const adopt = async (platform: Platform, formId: string, advice: Advice) => {
    const ok = await run('保存内容形式',
      () => api(`/topics/${topicId}/formats/${platform}`, 'PUT', {
        adopted: formId, by: advice.recommended === formId ? 'recommendation' : 'manual',
      }),
      `已选定「${labelOf(data as Payload, platform, formId)}」，下次生成会按这个形式组织内容。`);
    if (ok) load();
  };

  const clear = async (platform: Platform) => {
    const ok = await run('撤销内容形式',
      () => api(`/topics/${topicId}/formats/${platform}`, 'DELETE'),
      '已撤销，生成时恢复通用写法。');
    if (ok) load();
  };

  return <section className="format-advisor">
    <div className="format-heading">
      <h3><Sparkles size={14} />内容形式建议</h3>
      <span>{loading ? '读取中…' : '按规则引擎排序，人工可覆盖'}</span>
    </div>
    {error && <p className="callout warning" role="alert">{error}</p>}
    {!error && data && targets.length === 0 && <p className="muted-note">先在「目标平台」里选一个平台。</p>}
    {!error && data && targets.map(platform => {
      const advice = data.platforms[platform];
      if (!advice) return null;
      return <div className="format-platform" key={platform}>
        <div className="format-platform-head">
          <strong><span className={`platform-dot ${platform}`} />{names[platform]}</strong>
          {advice.adopted
            ? <span className="tag green">已选「{labelOf(data, platform, advice.adopted)}」{advice.decided_by === 'manual' ? ' · 人工' : ''}</span>
            : advice.recommended
              ? <span className="tag amber">推荐「{labelOf(data, platform, advice.recommended)}」</span>
              : <span className="tag red">无可行形式</span>}
        </div>
        <p className="format-reason">{statusText[advice.status] || advice.status}{advice.recommended ? ` · 推荐：${labelOf(data, platform, advice.recommended)}` : ''}{advice.reason ? ` · ${advice.reason}` : ''}</p>
        {advice.stale && <p className="format-stale">选题范围已变，这条建议是按旧输入算的，请重新选择。</p>}
        {advice.feasible.length > 0 ? <div className="format-options">
          {advice.feasible.map(row => <button key={row.content_form} type="button"
            className={`format-option ${advice.adopted === row.content_form ? 'active' : ''}`}
            disabled={!!busy}
            title={row.signals.join('；') || row.reason}
            onClick={() => void adopt(platform, row.content_form, advice)}>
            <span>{row.label}{advice.recommended === row.content_form && <em>推荐</em>}</span>
            <small>{row.score > 0 ? `+${row.score}` : row.score} 分 · {row.complexity === 'high' ? '重' : row.complexity === 'medium' ? '中' : '轻'}</small>
            {advice.adopted === row.content_form && <Check size={13} />}
          </button>)}
        </div> : <div className="format-blocked">
          {advice.evaluation_notes.map((note, index) => <p key={index}>{note}</p>)}
          {advice.evaluation_notes.length === 0 && advice.blocked.slice(0, 3).map(row => <p key={row.content_form}>{row.label}：{row.reason || '受硬约束排除'}</p>)}
        </div>}
        {advice.adopted && <button type="button" className="format-clear" disabled={!!busy} onClick={() => void clear(platform)}><X size={12} />撤销，按通用写法生成</button>}
      </div>;
    })}
    <p className="format-footnote">形式只决定「怎么组织」，不决定写什么事实。选定后在建任务时随快照带进生成；不选则完全不注入，请求与之前一致。</p>
  </section>;
}
