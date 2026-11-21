import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Ruler } from 'lucide-react';
import { api } from './api';
import type { Version } from './types';
import './audit.css';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;

interface Finding {
  code: string; severity: 'passed' | 'warning' | 'blocked';
  message: string; skipped?: boolean; evidence?: Record<string, unknown>;
}
interface AuditRow {
  platform: string; template_id: string; status: string;
  findings: Finding[]; evidence: Record<string, unknown>; updated_at: string;
}
interface Payload { version_id: string; results: AuditRow[]; worst: string; note: string }

import { PLATFORM_NAMES } from './platforms';
const names: Record<string, string> = PLATFORM_NAMES;
const tone: Record<string, string> = { passed: 'green', warning: 'amber', blocked: 'red' };
const label: Record<string, string> = { passed: '通过', warning: '有警告', blocked: '需修订' };
const codeName: Record<string, string> = {
  title_overflow: '标题长度', body_overflow: '正文长度', item_count: '条目数量',
  image_blur: '图片尺寸', cover_occlusion: '封面遮挡',
};

export default function LayoutAuditPanel({ version, busy, run }: { version: Version | null; busy: string; run: Run }) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const versionId = version?.id || '';

  const load = useCallback(() => {
    if (!versionId) return;
    setLoading(true);
    setError('');
    void api<Payload>(`/versions/${versionId}/layout-audit`)
      .then(setData)
      .catch(reason => { setError(reason instanceof Error ? reason.message : '排版审计读取失败'); setData(null); })
      .finally(() => setLoading(false));
  }, [versionId]);

  useEffect(() => { load(); }, [load]);

  if (!version) return null;

  const rerun = async () => {
    const ok = await run('重跑排版审计', () => api(`/versions/${versionId}/layout-audit`, 'POST', {}), '排版审计已重跑。');
    if (ok) load();
  };

  return <div className="audit-box">
    <div className="audit-head">
      <h3><Ruler size={14} />排版检查</h3>
      <div className="audit-actions">
        {data && data.results.length > 0 && <span className={`tag ${tone[data.worst] || 'muted'}`}>{label[data.worst] || data.worst}</span>}
        <button type="button" className="btn small" disabled={!!busy || loading} onClick={() => void rerun()}><RefreshCw size={13} />重跑</button>
      </div>
    </div>
    {error && <p className="callout warning" role="alert">{error}</p>}
    {loading && !data && <p className="muted-note">正在读取审计结果…</p>}
    {!loading && data && data.results.length === 0 && <p className="muted-note">这个版本还没有排版审计结果。审核通过时会自动跑一次，也可以点右上角重跑。</p>}
    {data && data.results.map(row => <div className="audit-platform" key={`${row.platform}-${row.template_id}`}>
      <div className="audit-platform-head">
        <strong><span className={`platform-dot ${row.platform}`} />{names[row.platform] || row.platform}</strong>
        <span className={`tag ${tone[row.status] || 'muted'}`}>{label[row.status] || row.status}</span>
      </div>
      <ul className="audit-findings">{row.findings.map((finding, index) =>
        <li key={index} className={finding.severity}>
          <span className="audit-code">{codeName[finding.code] || finding.code}</span>
          <span>{finding.message}</span>
          {finding.skipped && <em>已跳过</em>}
        </li>)}</ul>
    </div>)}
    {data && <p className="audit-note">{data.note}</p>}
  </div>;
}
