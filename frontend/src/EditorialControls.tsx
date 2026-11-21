import { useEffect, useState } from 'react';
import { api } from './api';
import type { ContentLine, StyleLibrary } from './types';

const FALLBACK_NAMES: Record<ContentLine, string> = {
  A: '实践工作流',
  B: '工具实测',
  C: '项目复盘',
};

export type EditorialSelection = { content_line: ContentLine | null; stance_ids: string[] };

export default function EditorialControls({ value, onChange, disabled = false }: {
  value: EditorialSelection;
  onChange: (value: EditorialSelection) => void;
  disabled?: boolean;
}) {
  const [library, setLibrary] = useState<StyleLibrary | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    api<StyleLibrary>('/style-library')
      .then(data => { if (active) { setLibrary(data); setError(''); } })
      .catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, []);
  const lines = library?.content_lines;
  return <div className="editorial-controls">
    <div className="field">
      <span>内容线 <small>保存选题可留空，生成前必须明确选择</small></span>
      <select aria-label="内容线" value={value.content_line || ''} disabled={disabled} onChange={event => onChange({
        ...value, content_line: (event.target.value || null) as ContentLine | null,
      })}>
        <option value="">暂不选择</option>
        {(['A', 'B', 'C'] as ContentLine[]).map(line => <option key={line} value={line}>
          {line} · {lines?.[line]?.name || FALLBACK_NAMES[line]}（编辑方向 {lines?.[line]?.editorial_share ?? { A: 40, B: 40, C: 20 }[line]}%）
        </option>)}
      </select>
      <small className="cm-muted">40/40/20 仅作编辑方向，不自动改变排期。</small>
    </div>
    <details className="editorial-stances">
      <summary>作者观点（已选 {value.stance_ids.length} 条）<small>只注入你明确选择的 confirmed 观点</small></summary>
      {error && <p className="form-error">观点库读取失败：{error}</p>}
      {!library && !error && <p className="cm-muted">正在读取观点库…</p>}
      <div className="editorial-stance-list">
        {(library?.stances || []).filter(item => item.confirmed).map(item => <label key={item.id} className="checkbox">
          <input type="checkbox" checked={value.stance_ids.includes(item.id)} disabled={disabled} onChange={event => onChange({
            ...value,
            stance_ids: event.target.checked ? [...value.stance_ids, item.id] : value.stance_ids.filter(id => id !== item.id),
          })} />
          <span><strong>{item.statement}</strong><small>{item.reason}</small></span>
        </label>)}
      </div>
      <p className="cm-muted">不选择时，模型不得替作者猜测立场。</p>
    </details>
  </div>;
}
