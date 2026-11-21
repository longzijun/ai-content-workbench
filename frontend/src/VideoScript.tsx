import { useState } from 'react';
import type { VideoShot, Work } from './types';

export const videoBody = (shots: VideoShot[]) => shots.map(shot => shot.spoken_text).join('\n\n');
export const shotSeconds = (shot: VideoShot) => Math.max(1, Math.ceil(Array.from(shot.spoken_text.replace(/\s/g, '')).length / 4));
const VISUAL_NAMES: Record<VideoShot['visual_type'], string> = { talking_head: '露脸', screen_recording: '录屏', material: '素材', mixed: '混合' };
const ROLE_NAMES: Record<VideoShot['role'], string> = { opening: '开场钩子', body: '内容镜头', closing: '结尾结论' };
const ordered = (shots: VideoShot[]): VideoShot[] => shots.map((shot, i) => ({ ...shot, order: i + 1, role: i === 0 ? 'opening' : i === shots.length - 1 ? 'closing' : 'body' }));

export function videoScriptText(work: Pick<Work, 'title' | 'cover_text' | 'shots' | 'target_duration_seconds'>): string {
  const shots = work.shots || [];
  return [`标题：${work.title}`, `封面文字：${work.cover_text || ''}`, `目标 ${work.target_duration_seconds || 60} 秒 · 预计 ${shots.reduce((sum, shot) => sum + shotSeconds(shot), 0)} 秒`, ...shots.map((shot, i) => `\n镜头 ${i + 1} · ${ROLE_NAMES[shot.role]} · ${VISUAL_NAMES[shot.visual_type]} · 预计 ${shotSeconds(shot)} 秒\n口播：${shot.spoken_text}\n画面：${shot.visual_notes}\n字幕：${shot.subtitle_points.filter(Boolean).join(' / ')}${shot.source_refs.filter(Boolean).length ? `\n依据：${shot.source_refs.filter(Boolean).join(' / ')}` : ''}`)].join('\n');
}

export function VideoFields({ value, disabled, onChange, onSuggest }: { value: Pick<Work, 'cover_text' | 'shots' | 'target_duration_seconds'>; disabled: boolean; onChange: (patch: Pick<Work, 'cover_text' | 'shots' | 'target_duration_seconds'>) => void; onSuggest?: (shotId: string, field: 'spoken_text' | 'visual_notes') => void }) {
  const shots = value.shots || [];
  const changeShots = (next: VideoShot[]) => onChange({ ...value, shots: ordered(next) });
  const patchShot = (id: string, patch: Partial<VideoShot>) => changeShots(shots.map(shot => shot.id === id ? { ...shot, ...patch } : shot));
  const move = (index: number, delta: number) => { const next = [...shots]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; changeShots(next); };
  const add = () => {
    const shot: VideoShot = { id: `shot_${globalThis.crypto?.randomUUID?.().replace(/-/g, '') || `${Date.now()}_${Math.random().toString(36).slice(2)}`}`, order: shots.length, role: 'body', spoken_text: '', visual_type: 'screen_recording', visual_notes: '', subtitle_points: [], source_refs: [] };
    const index = shots.length > 1 ? shots.length - 1 : shots.length;
    changeShots([...shots.slice(0, index), shot, ...shots.slice(index)]);
  };
  return <fieldset className="video-script-editor" disabled={disabled}>
    <div className="video-script-config"><label>封面文字<textarea rows={2} value={value.cover_text || ''} onChange={event => onChange({ ...value, cover_text: event.target.value })} /></label><label>目标时长（秒）<input type="number" min={10} max={600} value={value.target_duration_seconds ?? 60} onChange={event => onChange({ ...value, target_duration_seconds: Number(event.target.value) })} /></label></div>
    <p className="muted-note">分镜是口播的唯一来源。预计时长按每秒约 4 字计算，实际拍摄后另行核对。</p>
    {shots.map((shot, index) => <section className="video-shot-card" key={shot.id} aria-label={`镜头 ${index + 1}`}>
      <header><strong>镜头 {index + 1} · {ROLE_NAMES[shot.role]}</strong><span>预计 {shotSeconds(shot)} 秒</span><div className="video-shot-actions"><button type="button" disabled={disabled || index === 0} onClick={() => move(index, -1)} aria-label={`镜头 ${index + 1} 上移`}>上移</button><button type="button" disabled={disabled || index === shots.length - 1} onClick={() => move(index, 1)} aria-label={`镜头 ${index + 1} 下移`}>下移</button><button type="button" disabled={disabled || shots.length <= 2} onClick={() => changeShots(shots.filter(item => item.id !== shot.id))}>删除</button></div></header>
      <label>口播<textarea aria-label={`镜头 ${index + 1} 口播`} rows={4} value={shot.spoken_text} onChange={event => patchShot(shot.id, { spoken_text: event.target.value })} /></label>
      {onSuggest && <button type="button" className="text-link" onClick={() => onSuggest(shot.id, 'spoken_text')}>为这段口播提出建议</button>}
      <label>画面类型<select value={shot.visual_type} onChange={event => patchShot(shot.id, { visual_type: event.target.value as VideoShot['visual_type'] })}>{Object.entries(VISUAL_NAMES).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <label>画面与操作说明<textarea rows={3} value={shot.visual_notes} onChange={event => patchShot(shot.id, { visual_notes: event.target.value })} placeholder="记录待拍摄或待准备的画面，不代表素材已经完成" /></label>
      {onSuggest && <button type="button" className="text-link" onClick={() => onSuggest(shot.id, 'visual_notes')}>为这段画面说明提出建议</button>}
      <label>字幕重点（每行一项）<textarea rows={2} value={shot.subtitle_points.join('\n')} onChange={event => patchShot(shot.id, { subtitle_points: event.target.value.split('\n') })} onBlur={() => patchShot(shot.id, { subtitle_points: shot.subtitle_points.filter(text => text.trim()) })} /></label>
      <label>依据引用（每行一项）<textarea rows={2} value={shot.source_refs.join('\n')} onChange={event => patchShot(shot.id, { source_refs: event.target.value.split('\n') })} onBlur={() => patchShot(shot.id, { source_refs: shot.source_refs.filter(text => text.trim()) })} /></label>
    </section>)}
    <button type="button" className="btn" disabled={disabled || shots.length >= 40} onClick={add}>添加内容镜头</button>
  </fieldset>;
}

export function VideoPreview({ work, beforeCopy, highlight }: { work: Work; beforeCopy?: () => Promise<void>; highlight?: string }) {
  const [message, setMessage] = useState('');
  const [fallback, setFallback] = useState('');
  const shots = work.shots || [];
  const estimated = shots.reduce((sum, shot) => sum + shotSeconds(shot), 0);
  const copyText = async (text: string) => { if (beforeCopy) { try { await beforeCopy(); } catch (failure) { setMessage(String(failure)); setFallback(''); return; } } try { await navigator.clipboard.writeText(text); setMessage('已复制。'); setFallback(''); } catch { setMessage('请长按选择并复制下方文本。'); setFallback(text); } };
  return <div className="video-script-preview"><span className="tag amber">脚本就绪 · 待拍摄</span><h3>{work.title}</h3><p className="video-cover-text">封面文字：{work.cover_text || '待补充'}</p><p className="muted-note">目标 {work.target_duration_seconds || 60} 秒 · 预计 {estimated} 秒，估算不代表成片时长。</p>{Math.abs(estimated - (work.target_duration_seconds || 60)) > Math.max(5, (work.target_duration_seconds || 60) * .2) && <p className="studio-review-warning">预计时长与目标偏差较大，请核对口播节奏。</p>}<div className="actions"><button type="button" className="btn small" onClick={() => void copyText(videoBody(shots))}>复制纯口播</button><button type="button" className="btn small" onClick={() => void copyText(videoScriptText(work))}>复制完整拍摄脚本</button></div>{message && <p role="status">{message}</p>}{fallback && <textarea readOnly rows={8} value={fallback} aria-label="待复制脚本文本" onFocus={event => event.currentTarget.select()} />}{shots.map((shot, index) => <section className={`video-shot-preview${highlight && (shot.spoken_text.includes(highlight) || shot.visual_notes.includes(highlight)) ? ' studio-highlight' : ''}`} key={shot.id}><header><strong>镜头 {index + 1} · {ROLE_NAMES[shot.role]}</strong><span>{VISUAL_NAMES[shot.visual_type]} · 预计 {shotSeconds(shot)} 秒</span></header><p className="video-spoken">{shot.spoken_text}</p><p><strong>画面：</strong>{shot.visual_notes || '待补拍摄说明'}</p>{shot.subtitle_points.filter(Boolean).length > 0 && <p><strong>字幕：</strong>{shot.subtitle_points.filter(Boolean).join(' / ')}</p>}{shot.source_refs.filter(Boolean).length > 0 && <details><summary>依据引用</summary>{shot.source_refs.filter(Boolean).map((source, i) => <p key={i}>{source}</p>)}</details>}</section>)}<p className="muted-note">此处为拍摄脚本，素材与录屏仍须准备；成片与公开发布由你完成。</p></div>;
}
