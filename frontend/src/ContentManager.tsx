import { useCallback, useLayoutEffect, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Archive, ArrowUpDown, ArrowUpRight, CheckCircle2, Copy, FileText, Inbox, MoreHorizontal, RotateCcw, Search, Trash2, X } from 'lucide-react';
import { api } from './api';
import { PLATFORM_IDS, PLATFORM_NAMES } from './platforms';
import type { DashboardPayload, Platform, State } from './types';

type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;
type Flash = (message: string, error?: boolean) => void;

export interface ContentRow {
  id: string; content_type: '选题' | '平台草稿'; title: string; brief?: string; state: string;
  platforms: Platform[]; created_at: string; updated_at: string; thumbnail: string | null;
  priority: number; favorite: boolean; archived: boolean; tags: string[]; version_count?: number;
  draft_count?: number; version_number?: number | null; topic_title?: string; remote_id?: string | null;
  remote_url_present?: boolean; platform?: Platform; source?: string;
}
interface ContentList { total: number; page: number; page_size: number; pages: number; sort: string; order: string; items: ContentRow[]; facets: { platforms: Record<string, number>; states: Record<string, number>; content_types: Record<string, number> } }
interface Facets { platforms: string[]; topic_states: string[]; draft_states: string[]; priorities: { value: number; label: string }[]; tags: string[]; sorts: { value: string; label: string }[] }
interface BatchResult { requested: number; succeeded: number; failed: number; action: string; entity_type: string; results: { id: string; ok: boolean; message: string }[] }
interface TrashItem { id: string; entity_type: string; entity_id: string; title: string; previous_state: string; deleted_at: string; deleted_source: string }
interface TrashList { total: number; page: number; pages: number; items: TrashItem[] }
interface AuditRow { id: string; entity_type: string; entity_id: string; action: string; result: string; detail: string; created_at: string }
interface AuditList { total: number; items: AuditRow[] }
interface DetailPayload { entity: Record<string, unknown>; history: AuditRow[] }
interface RefsPayload { can_purge: boolean; blockers: string[] }

const STATE_LABEL: Record<string, string> = {
  pending: '待确认', approved: '已批准', rejected: '已拒绝', parked: '暂存',
  local: '本地草稿', needs_connection: '未存入平台', syncing: '同步中', unknown: '状态待核对',
  synced: '平台草稿', browser_saved: '平台草稿', reviewed: '已提交发布', published: '已发布',
  stale: '版本已过期', blocked: '需修订', failed: '同步失败',
};
const ACTION_LABEL: Record<string, string> = {
  delete: '删除', restore: '恢复', purge: '永久删除', archive: '归档', unarchive: '取消归档',
  duplicate: '复制', update_meta: '修改属性', batch_archive: '批量归档', batch_tag: '批量标签',
  batch_priority: '批量优先级', batch_favorite: '批量收藏',
};

function tone(state: string) {
  if (['approved', 'synced', 'browser_saved', 'reviewed', 'published'].includes(state)) return 'green';
  if (['rejected', 'failed', 'blocked'].includes(state)) return 'red';
  if (['pending', 'needs_connection', 'syncing', 'unknown', 'stale'].includes(state)) return 'amber';
  return 'muted';
}
function stamp(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}
const PLATFORM_NAME: Record<string, string> = PLATFORM_NAMES;

/* -------------------------------------------------------------- 确认弹窗 */

interface ConfirmSpec {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => Promise<void>;
}

function ConfirmModal({ spec, onClose }: { spec: ConfirmSpec; onClose: () => void }) {
  // 独立的 pending：App 的 busy 是全局的，这里要的是「本次确认正在提交」。
  // 缺了它用户可以连点确认按钮，重复提交同一个删除/永久删除请求。
  const [pending, setPending] = useState(false);
  const submit = async () => {
    if (pending) return;
    setPending(true);
    try { await spec.onConfirm(); }
    finally { setPending(false); }
  };
  return <div className="cm-modal-scrim" role="dialog" aria-modal="true" onClick={e => { if (e.target === e.currentTarget && !pending) onClose(); }}>
    <div className="cm-modal">
      <header><h3>{spec.title}</h3><button className="icon-btn" aria-label="关闭" disabled={pending} onClick={onClose}><X size={16} /></button></header>
      <div className="body">{spec.body}</div>
      <footer>
        <button className="btn" disabled={pending} onClick={onClose}>取消</button>
        <button className={`btn ${spec.danger ? 'danger' : 'primary'}`} disabled={pending} onClick={() => { void submit(); }}>
          {pending ? '处理中…' : spec.confirmLabel}
        </button>
      </footer>
    </div>
  </div>;
}

/* -------------------------------------------------------------- 批量结果 */

function BatchResultModal({ result, onClose }: { result: BatchResult; onClose: () => void }) {
  return <div className="cm-modal-scrim" role="dialog" aria-modal="true" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="cm-modal">
      <header><h3>批量操作结果</h3><button className="icon-btn" aria-label="关闭" onClick={onClose}><X size={16} /></button></header>
      <div className="body">
        <p>共提交 {result.requested} 条：<strong style={{ color: 'var(--saas-success)' }}>成功 {result.succeeded}</strong>、
          <strong style={{ color: result.failed ? 'var(--saas-danger)' : 'var(--saas-text-secondary)' }}>失败 {result.failed}</strong>。</p>
        {result.failed > 0 && <div className="cm-warn-note">部分条目未成功，失败原因逐条列出。请只对失败项重试，不要重复提交全部。</div>}
        <div className="cm-results">
          {result.results.map(r => <div key={r.id}>
            <span className={r.ok ? 'ok' : 'bad'}>{r.ok ? '成功' : '失败'}</span>
            <span className="cm-muted">{r.id.slice(0, 12)}</span>
            <span>{r.message}</span>
          </div>)}
        </div>
      </div>
      <footer><button className="btn primary" onClick={onClose}>知道了</button></footer>
    </div>
  </div>;
}

/* -------------------------------------------------------------- 内容管理 */

export function ContentManager({ run, flash, busy, onOpenTopic, onOpenDrafts }: {
  run: Run; flash: Flash; busy: string;
  onOpenTopic: (topicId: string) => void;
  onOpenDrafts: (topicId: string, versionId: string, platform: Platform) => void;
}) {
  const [kind, setKind] = useState('all');
  const [q, setQ] = useState('');
  const [platform, setPlatform] = useState('');
  const [state, setState] = useState('');
  const [sort, setSort] = useState('updated_at');
  const [order, setOrder] = useState('desc');
  const [page, setPage] = useState(1);
  const [list, setList] = useState<ContentList | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [menu, setMenu] = useState('');
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [batchResult, setBatchResult] = useState<BatchResult | null>(null);
  const [detail, setDetail] = useState<DetailPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [menuDropUp, setMenuDropUp] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ kind, q, platform, state, sort, order, page: String(page), page_size: '20' });
      const data = await api<ContentList>(`/content?${params.toString()}`);
      setList(data);
    } catch (error) {
      flash(error instanceof Error ? error.message : '读取内容列表失败', true);
    } finally { setLoading(false); }
  }, [kind, q, platform, state, sort, order, page, flash]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { api<Facets>('/content/facets').then(setFacets).catch(() => setFacets(null)); }, []);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu('');
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [menu]);
  // 表格内部滚动后，靠下的行打开操作菜单会超出滚动容器底部被裁掉。
  // 打开时量一次：下方放不下就向上翻转（drop-up）。
  useLayoutEffect(() => {
    if (!menu) { setMenuDropUp(false); return; }
    const el = menuRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const scroller = el.closest('.cm-tablescroll');
    const limit = scroller ? scroller.getBoundingClientRect().bottom : window.innerHeight;
    setMenuDropUp(rect.bottom > limit - 8);
  }, [menu]);

  // 换筛选条件后回到第一页，并清掉跨页残留的选择
  const patch = (next: Partial<{ kind: string; q: string; platform: string; state: string; sort: string; order: string }>) => {
    setPage(1); setSelected([]);
    if (next.kind !== undefined) setKind(next.kind);
    if (next.q !== undefined) setQ(next.q);
    if (next.platform !== undefined) setPlatform(next.platform);
    if (next.state !== undefined) setState(next.state);
    if (next.sort !== undefined) setSort(next.sort);
    if (next.order !== undefined) setOrder(next.order);
  };

  const rows = list?.items ?? [];
  const allChecked = rows.length > 0 && rows.every(r => selected.includes(r.id));
  const toggleAll = () => setSelected(allChecked ? selected.filter(id => !rows.some(r => r.id === id)) : [...new Set([...selected, ...rows.map(r => r.id)])]);
  const toggleOne = (id: string) => setSelected(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const entityOf = (row: ContentRow) => (row.content_type === '选题' ? 'topic' : 'draft');

  const openDetail = async (row: ContentRow) => {
    setMenu('');
    try { setDetail(await api<DetailPayload>(`/content/${entityOf(row)}/${row.id}`)); }
    catch (error) { flash(error instanceof Error ? error.message : '读取详情失败', true); }
  };

  const doDelete = (row: ContentRow) => {
    setMenu('');
    setConfirm({
      title: '移入回收站', danger: true, confirmLabel: '确认移入回收站',
      body: <><p>将「{row.title}」移入回收站。</p>
        <p className="cm-muted">这是软删除：记录仍在数据库中，可随时恢复。平台草稿的远程草稿不会被删除。</p></>,
      onConfirm: async () => {
        const ok = await run('移入回收站', () => api(`/content/${entityOf(row)}/${row.id}/delete`, 'POST', {}), '已移入回收站，可从回收站恢复。');
        if (ok) { setSelected([]); await load(); setConfirm(null); }
      },
    });
  };

  const doDuplicate = async (row: ContentRow) => {
    setMenu('');
    if (row.content_type !== '选题') { flash('目前只支持复制选题。', true); return; }
    const ok = await run('复制选题', () => api(`/content/topic/${row.id}/duplicate`, 'POST', {}), '已复制为新选题，状态为待确认。');
    if (ok) await load();
  };

  const doArchive = async (row: ContentRow, archived: boolean) => {
    setMenu('');
    if (row.content_type !== '选题') { flash('平台草稿不支持归档，请用回收站。', true); return; }
    const ok = await run(archived ? '归档' : '取消归档', () => api(`/content/topic/${row.id}/archive`, 'POST', { archived }), archived ? '已归档。' : '已取消归档。');
    if (ok) await load();
  };

  const doPurge = (row: ContentRow) => {
    setMenu('');
    const entity = entityOf(row);
    setConfirm({
      title: '永久删除', danger: true, confirmLabel: '我确认永久删除',
      body: <p>正在检查关联数据…</p>,
      onConfirm: async () => {
        let refs: RefsPayload = { can_purge: true, blockers: [] };
        try { refs = await api<RefsPayload>(`/content/${entity}/${row.id}/references`); } catch { /* 保持默认可删，实际由后端把关 */ }
        if (!refs.can_purge) {
          setConfirm({
            title: '无法永久删除', danger: true, confirmLabel: '我知道了',
            body: <><p>「{row.title}」仍有关联数据，永久删除已阻止：</p>
              <div className="cm-danger-note">{refs.blockers.map(b => <div key={b}>· {b}</div>)}</div>
              <p className="cm-muted">请先处理这些关联，或改为「移入回收站」。</p></>,
            onConfirm: async () => setConfirm(null),
          });
          return;
        }
        const done = await run('永久删除', () => api(`/content/${entity}/${row.id}/purge?confirm=true`, 'DELETE'), '已永久删除。');
        if (done) { setSelected([]); await load(); setConfirm(null); }
      },
    });
  };

  const doBatch = (action: 'archive' | 'delete' | 'tag' | 'priority' | 'favorite', payload: Record<string, unknown>, label: string) => {
    const entity = kind === 'draft' ? 'draft' : 'topic';
    const targets = rows.filter(r => selected.includes(r.id) && entityOf(r) === entity);
    if (!targets.length) { flash(`所选条目中没有${entity === 'topic' ? '选题' : '草稿'}，无法执行。`, true); return; }
    if (action === 'delete') {
      setConfirm({
        title: '批量移入回收站', danger: true, confirmLabel: `确认删除 ${targets.length} 条`,
        body: <><p>将 {targets.length} 条记录移入回收站（软删除，可恢复）。</p>
          <p className="cm-muted">平台草稿的远程草稿不会被删除。</p></>,
        onConfirm: async () => {
          const result = await api<BatchResult>('/content/batch', 'POST', { entity_type: entity, ids: targets.map(t => t.id), action, payload });
          setBatchResult(result); setSelected([]); await load(); setConfirm(null);
        },
      });
      return;
    }
    void run(label, async () => {
      const result = await api<BatchResult>('/content/batch', 'POST', { entity_type: entity, ids: targets.map(t => t.id), action, payload });
      setBatchResult(result); setSelected([]); await load();
    });
  };

  const header = (key: string, label: string) => <th className="sortable" onClick={() => {
    if (sort === key) patch({ order: order === 'desc' ? 'asc' : 'desc' }); else patch({ sort: key, order: 'desc' });
  }}>{label}{sort === key && <span className="sortmark">{order === 'desc' ? '↓' : '↑'}</span>}</th>;

  return <>
    <div className="cm-toolbar">
      <select value={kind} onChange={e => patch({ kind: e.target.value })} aria-label="内容类型">
        <option value="all">全部类型{list?.facets.content_types['选题'] !== undefined && ` (${(list.facets.content_types['选题'] || 0) + (list.facets.content_types['平台草稿'] || 0)})`}</option>
        <option value="topic">选题{list?.facets.content_types['选题'] !== undefined && ` (${list.facets.content_types['选题']})`}</option>
        <option value="draft">平台草稿{list?.facets.content_types['平台草稿'] !== undefined && ` (${list.facets.content_types['平台草稿']})`}</option>
      </select>
      <select value={platform} onChange={e => patch({ platform: e.target.value })} aria-label="目标平台">
        <option value="">全部平台</option>
        {PLATFORM_IDS.map(p => <option key={p} value={p}>{PLATFORM_NAME[p]}{list?.facets.platforms[p] !== undefined && ` (${list.facets.platforms[p]})`}</option>)}
      </select>
      <select value={state} onChange={e => patch({ state: e.target.value })} aria-label="状态">
        <option value="">全部状态</option>
        {[...new Set([...(facets?.topic_states ?? []), ...(facets?.draft_states ?? [])])].map(s =>
          <option key={s} value={s}>{STATE_LABEL[s] ?? s}{list?.facets.states[s] !== undefined && ` (${list.facets.states[s]})`}</option>)}
      </select>
      <select value={sort} onChange={e => patch({ sort: e.target.value })} aria-label="排序字段">
        {(facets?.sorts ?? [{ value: 'updated_at', label: '最后修改' }]).map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
      </select>
      <button className="cm-rowbtn" onClick={() => patch({ order: order === 'desc' ? 'asc' : 'desc' })} title="切换升序降序">
        <ArrowUpDown size={12} /> {order === 'desc' ? '降序' : '升序'}
      </button>
      <span className="sep" />
      <input className="grow" placeholder="搜索标题或简介…" value={q} onChange={e => { setQ(e.target.value); setPage(1); }} aria-label="关键词搜索" />
      {(q || platform || state || kind !== 'all') && <button className="cm-rowbtn" onClick={() => patch({ q: '', platform: '', state: '', kind: 'all' })}>清除筛选</button>}
      <span className="cm-count">{loading ? '读取中…' : list ? `共 ${list.total} 条` : ''}</span>
    </div>

    {selected.length > 0 && <div className="cm-bulkbar">
      <CheckCircle2 size={14} />已选 {selected.length} 条
      <span style={{ fontWeight: 400 }}>批量操作逐条结算，失败项会单独列出</span>
      <button className="cm-rowbtn" disabled={!!busy} onClick={() => doBatch('archive', { archived: true }, '批量归档')}>批量归档</button>
      <button className="cm-rowbtn" disabled={!!busy} onClick={() => doBatch('archive', { archived: false }, '取消归档')}>取消归档</button>
      <button className="cm-rowbtn" disabled={!!busy} onClick={() => { const v = window.prompt('添加标签（多个用逗号分隔）'); if (v) doBatch('tag', { add: v.split(/[,，]/).map(s => s.trim()).filter(Boolean) }, '批量打标签'); }}>批量标签</button>
      <button className="cm-rowbtn" disabled={!!busy} onClick={() => doBatch('favorite', { favorite: true }, '批量收藏')}>批量收藏</button>
      <button className="cm-rowbtn danger" disabled={!!busy} onClick={() => doBatch('delete', {}, '批量删除')}>批量删除</button>
      <button className="cm-rowbtn" onClick={() => setSelected([])}>取消选择</button>
    </div>}

    <div className="cm-tablewrap">
      {rows.length === 0 ? <div className="empty" style={{ border: 'none', margin: 20 }}>
        <div className="empty-icon"><Inbox size={19} /></div>
        <h3>{loading ? '读取中' : '没有匹配的内容'}</h3>
        <p>{loading ? '正在从本机数据库查询。' : q || platform || state ? '换个关键词或清除筛选再试。' : '创建选题或保存平台草稿后，这里会显示可管理的全部内容。'}</p>
      </div> : <>
        <div className="cm-tablescroll">
          <table className="cm-table">
            <thead><tr>
              <th className="col-check"><input type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="全选本页" /></th>
              {header('title', '内容标题')}
              <th>类型</th>
              <th>目标平台</th>
              <th>缩略图</th>
              <th>状态</th>
              {header('created_at', '创建时间')}
              {header('updated_at', '最后修改')}
              <th>同步状态</th>
              <th className="col-actions">操作</th>
            </tr></thead>
            <tbody>
              {rows.map(row => {
                const entity = entityOf(row);
                return <tr key={`${entity}-${row.id}`} className={selected.includes(row.id) ? 'selected' : ''}>
                  <td className="col-check"><input type="checkbox" checked={selected.includes(row.id)} onChange={() => toggleOne(row.id)} aria-label={`选择 ${row.title}`} /></td>
                  <td>
                    <span className="cm-title" title={row.title}>{row.title}</span>
                    <span className="cm-sub">
                      {row.content_type === '选题'
                        ? `${row.version_count ?? 0} 个版本 · ${row.draft_count ?? 0} 条草稿`
                        : `${row.topic_title ? `${row.topic_title} · ` : ''}版本 ${row.version_number ?? '—'}`}
                      {row.tags.length > 0 && ` · ${row.tags.join('、')}`}
                      {row.favorite && ' · ★ 已收藏'}
                    </span>
                  </td>
                  <td><span className="tag muted">{row.content_type}</span></td>
                  <td>{(row.platforms || []).length === 0 ? <span className="cm-muted">—</span> :
                    row.platforms.map(p => <span key={p} className="tag blue" style={{ marginRight: 3 }}>{PLATFORM_NAME[p] ?? p}</span>)}</td>
                  <td>{row.thumbnail
                    ? <img className="cm-thumb" src={row.thumbnail} alt="" loading="lazy" />
                    : <span className="cm-thumbempty">无</span>}</td>
                  <td><span className={`tag ${tone(row.state)}`}>{STATE_LABEL[row.state] ?? row.state}</span>
                    {row.archived && <span className="tag muted" style={{ marginLeft: 3 }}>已归档</span>}</td>
                  <td className="cm-muted">{stamp(row.created_at)}</td>
                  <td className="cm-muted">{stamp(row.updated_at)}</td>
                  <td>{row.content_type === '平台草稿'
                    ? (row.remote_id
                      ? <span className="tag green">已存平台</span>
                      : <span className="tag amber">未同步</span>)
                    : <span className="cm-muted">—</span>}</td>
                  <td className="col-actions">
                    <span className="cm-actionscell">
                      <button className="cm-rowbtn" disabled={!!busy} onClick={() => row.content_type === '选题' ? onOpenTopic(row.id) : onOpenDrafts(row.id, '', (row.platform ?? 'wechat') as Platform)}>编辑</button>
                      <span className="cm-menuwrap">
                        <button className="cm-rowbtn" disabled={!!busy} onClick={e => { e.stopPropagation(); setMenu(menu === row.id ? '' : row.id); }} aria-label="更多操作"><MoreHorizontal size={13} /></button>
                        {menu === row.id && <div ref={menuRef} className={`cm-menu${menuDropUp ? ' drop-up' : ''}`} onClick={e => e.stopPropagation()}>
                          <button disabled={!!busy} onClick={() => void openDetail(row)}><FileText size={13} />查看详情与记录</button>
                          <button disabled={!!busy || row.content_type !== '选题'} onClick={() => void doDuplicate(row)} title={row.content_type === '选题' ? '' : '仅选题支持复制'}><Copy size={13} />复制内容</button>
                          <button disabled={!!busy || row.content_type !== '选题'} onClick={() => void doArchive(row, !row.archived)} title={row.content_type === '选题' ? '' : '仅选题支持归档'}><Archive size={13} />{row.archived ? '取消归档' : '归档'}</button>
                          <hr />
                          <button className="danger" disabled={!!busy} onClick={() => doDelete(row)}><Trash2 size={13} />移入回收站</button>
                          <button className="danger" disabled={!!busy} onClick={() => doPurge(row)}><Trash2 size={13} />永久删除</button>
                        </div>}
                      </span>
                    </span>
                  </td>
                </tr>;
              })}
            </tbody>
          </table>
        </div>
        <div className="cm-pager">
          <button className="cm-rowbtn" disabled={page <= 1 || loading} onClick={() => setPage(p => Math.max(1, p - 1))}>上一页</button>
          <span>第 {list?.page ?? page} / {list?.pages ?? 1} 页</span>
          <button className="cm-rowbtn" disabled={page >= (list?.pages ?? 1) || loading} onClick={() => setPage(p => p + 1)}>下一页</button>
          <span className="cm-count">每页 20 条 · 筛选与排序由服务端执行</span>
        </div>
      </>}
    </div>

    {confirm && <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />}
    {batchResult && <BatchResultModal result={batchResult} onClose={() => setBatchResult(null)} />}
    {detail && <div className="cm-modal-scrim" role="dialog" aria-modal="true" onClick={e => { if (e.target === e.currentTarget) setDetail(null); }}>
      <div className="cm-modal">
        <header><h3>内容详情与操作记录</h3><button className="icon-btn" aria-label="关闭" onClick={() => setDetail(null)}><X size={16} /></button></header>
        <div className="body">
          <p><strong>{String(detail.entity.title ?? '')}</strong></p>
          <p className="cm-muted">ID {String(detail.entity.id ?? '')}</p>
          <p className="cm-muted" style={{ marginTop: 10 }}>操作记录（{detail.history.length} 条）</p>
          {detail.history.length === 0 ? <p className="cm-muted">暂无操作记录。</p> :
            <ul className="cm-history">{detail.history.map(h => <li key={h.id}>
              <time>{stamp(h.created_at)}</time>
              <span className="act">{ACTION_LABEL[h.action] ?? h.action}</span>
              <span className={h.result === 'success' ? 'tag green' : 'tag red'}>{h.result === 'success' ? '成功' : '失败'}</span>
              {h.detail && <span className="cm-muted">{h.detail}</span>}
            </li>)}</ul>}
        </div>
        <footer><button className="btn primary" onClick={() => setDetail(null)}>关闭</button></footer>
      </div>
    </div>}
  </>;
}

/* ------------------------------------------------------------------ 回收站 */

export function TrashBin({ run, flash }: { run: Run; flash: Flash }) {
  const [list, setList] = useState<TrashList | null>(null);
  const [q, setQ] = useState('');
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  const load = useCallback(async () => {
    try { setList(await api<TrashList>(`/trash?q=${encodeURIComponent(q)}`)); }
    catch (error) { flash(error instanceof Error ? error.message : '读取回收站失败', true); }
  }, [q, flash]);
  useEffect(() => { void load(); }, [load]);

  const restore = async (item: TrashItem) => {
    const ok = await run('恢复', () => api(`/content/${item.entity_type}/${item.entity_id}/restore`, 'POST', {}), '已恢复。');
    if (ok) await load();
  };

  const purge = (item: TrashItem) => {
    setConfirm({
      title: '永久删除', danger: true, confirmLabel: '我确认永久删除',
      body: <><p>「{item.title}」将从数据库中彻底移除，无法恢复。</p>
        <div className="cm-danger-note">若存在关联草稿、版本或发布记录，系统会拒绝删除并说明原因。本操作不可撤销。</div></>,
      onConfirm: async () => {
        const done = await run('永久删除', () => api(`/content/${item.entity_type}/${item.entity_id}/purge?confirm=true`, 'DELETE'), '已永久删除。');
        if (done) { await load(); setConfirm(null); }
      },
    });
  };

  return <>
    <div className="cm-toolbar">
      <span style={{ fontSize: 12, color: 'var(--saas-text-secondary)' }}>
        回收站保留删除前的完整快照，可随时恢复。永久删除需显式确认并通过引用检查。
      </span>
      <input className="grow" placeholder="搜索已删除内容…" value={q} onChange={e => setQ(e.target.value)} aria-label="搜索回收站" />
      <button className="cm-rowbtn" onClick={() => void load()}>刷新</button>
      <span className="cm-count">共 {list?.total ?? 0} 条</span>
    </div>
    <div className="cm-tablewrap">
      {!list?.items.length ? <div className="empty" style={{ border: 'none', margin: 20 }}>
        <div className="empty-icon"><Trash2 size={19} /></div>
        <h3>回收站是空的</h3>
        <p>在内容管理里删除的记录会移到这里，不会立即消失。</p>
      </div> : <div className="cm-tablescroll"><table className="cm-table" style={{ minWidth: 720 }}>
        <thead><tr><th>内容标题</th><th>类型</th><th>删除前状态</th><th>删除时间</th><th>删除来源</th><th className="col-actions">操作</th></tr></thead>
        <tbody>{list.items.map(item => <tr key={item.id}>
          <td><span className="cm-title" title={item.title}>{item.title || '(无标题)'}</span><span className="cm-sub">{item.entity_id.slice(0, 16)}</span></td>
          <td><span className="tag muted">{item.entity_type === 'topic' ? '选题' : item.entity_type === 'draft' ? '草稿' : item.entity_type}</span></td>
          <td><span className={`tag ${tone(item.previous_state)}`}>{STATE_LABEL[item.previous_state] ?? (item.previous_state || '未知')}</span></td>
          <td className="cm-muted">{stamp(item.deleted_at)}</td>
          <td className="cm-muted">{item.deleted_source}</td>
          <td className="col-actions"><span className="cm-actionscell">
            <button className="cm-rowbtn" onClick={() => void restore(item)}><RotateCcw size={12} />恢复</button>
            <button className="cm-rowbtn danger" onClick={() => purge(item)}>永久删除</button>
          </span></td>
        </tr>)}</tbody>
      </table></div>}
    </div>
    {confirm && <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />}
  </>;
}

/* ---------------------------------------------------------------- 操作日志 */

export function AuditLogPage({ flash }: { flash: Flash }) {
  const [list, setList] = useState<AuditList | null>(null);
  const [entityType, setEntityType] = useState('');
  const [result, setResult] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(0);
  const limit = 50;

  useEffect(() => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(page * limit) });
    if (entityType) params.set('entity_type', entityType);
    if (result) params.set('result', result);
    api<AuditList>(`/audit-logs?${params.toString()}`).then(setList)
      .catch(error => flash(error instanceof Error ? error.message : '读取操作日志失败', true));
  }, [entityType, result, page, flash]);

  const filtered = useMemo(() => {
    const rows = list?.items ?? [];
    if (!q.trim()) return rows;
    const needle = q.trim().toLowerCase();
    return rows.filter(r => `${r.action} ${r.detail} ${r.entity_id}`.toLowerCase().includes(needle));
  }, [list, q]);

  return <>
    <div className="cm-toolbar">
      <select value={entityType} onChange={e => { setEntityType(e.target.value); setPage(0); }} aria-label="对象类型">
        <option value="">全部对象</option>
        <option value="topic">选题</option>
        <option value="draft">草稿</option>
      </select>
      <select value={result} onChange={e => { setResult(e.target.value); setPage(0); }} aria-label="执行结果">
        <option value="">全部结果</option>
        <option value="success">仅成功</option>
        <option value="failed">仅失败</option>
      </select>
      <input className="grow" placeholder="在当前页内搜索动作或对象…" value={q} onChange={e => setQ(e.target.value)} aria-label="搜索日志" />
      <span className="cm-count">共 {list?.total ?? 0} 条 · 第 {page + 1} 页</span>
    </div>
    <div className="cm-tablewrap">
      {!filtered.length ? <div className="empty" style={{ border: 'none', margin: 20 }}>
        <div className="empty-icon"><Search size={19} /></div>
        <h3>没有匹配的操作记录</h3>
        <p>新建、编辑、复制、归档、删除、恢复、平台同步与失败都会记录在此。日志不保存任何凭据。</p>
      </div> : <div className="cm-tablescroll"><table className="cm-table" style={{ minWidth: 640 }}>
        <thead><tr><th>时间</th><th>动作</th><th>对象</th><th>结果</th><th>详情</th></tr></thead>
        <tbody>{filtered.map(r => <tr key={r.id}>
          <td className="cm-muted">{stamp(r.created_at)}</td>
          <td><strong>{ACTION_LABEL[r.action] ?? r.action}</strong></td>
          <td className="cm-muted">{r.entity_type === 'topic' ? '选题' : r.entity_type === 'draft' ? '草稿' : r.entity_type} · {r.entity_id.slice(0, 10)}</td>
          <td><span className={`tag ${r.result === 'success' ? 'green' : 'red'}`}>{r.result === 'success' ? '成功' : '失败'}</span></td>
          <td className="cm-muted">{r.detail || '—'}</td>
        </tr>)}</tbody>
      </table></div>}
    </div>
    {!!list?.total && list.total > limit && <div className="cm-pager" style={{ borderTop: 'none', borderRadius: '0 0 8px 8px' }}>
      <button className="cm-rowbtn" disabled={page === 0} onClick={() => setPage(p => Math.max(0, p - 1))}>上一页</button>
      <span>第 {page + 1} / {Math.ceil(list.total / limit)} 页</span>
      <button className="cm-rowbtn" disabled={(page + 1) * limit >= list.total} onClick={() => setPage(p => p + 1)}>下一页</button>
    </div>}
  </>;
}
/* ---------------------------------------------------------------- 数据分析 */

export function AnalyticsPanel({ state, dashboard, onGo }: {
  state: State;
  dashboard: DashboardPayload | null;
  onGo: (page: 'overview' | 'trends' | 'topics' | 'projects' | 'content' | 'drafts' | 'schedule' | 'analytics' | 'trash' | 'logs' | 'settings') => void;
}) {
  const drafts = state.drafts;
  const byPlatform = PLATFORM_IDS.map(p => ({
    platform: p, name: PLATFORM_NAME[p],
    total: drafts.filter(d => d.platform === p).length,
    saved: drafts.filter(d => d.platform === p && d.remote_id).length,
    waiting: drafts.filter(d => d.platform === p && !d.remote_id).length,
  }));
  const byState = Object.entries(
    drafts.reduce<Record<string, number>>((acc, d) => { acc[d.state] = (acc[d.state] ?? 0) + 1; return acc; }, {})
  ).sort((a, b) => b[1] - a[1]);
  const topicStatus = (['pending', 'approved', 'rejected', 'parked'] as const).map(s => ({
    state: s, label: STATE_LABEL[s], count: state.topics.filter(t => t.status === s).length,
  }));
  const jobStatus = (['queued', 'running', 'succeeded', 'failed', 'cancelled'] as const).map(s => ({
    state: s, count: state.jobs.filter(j => j.state === s).length,
  }));
  const maxPlatform = Math.max(1, ...byPlatform.map(x => x.total));
  const maxState = Math.max(1, ...byState.map(x => x[1]));

  return <>
    <div className="cm-toolbar">
      <span style={{ fontSize: 12, color: 'var(--saas-text-secondary)' }}>
        以下数字全部来自本机数据库实时统计。平台阅读量、互动等回流数据尚未接入，因此本版不展示。
      </span>
      <span className="cm-count">草稿 {drafts.length} 条 · 选题 {state.topics.length} 条 · 任务 {state.jobs.length} 条</span>
    </div>

    <section className="panel">
      <div className="panel-title"><h2>各平台草稿分布</h2></div>
      {byPlatform.map(row => <div key={row.platform} style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 5 }}>
          <span>{row.name}</span>
          <span className="cm-muted">{row.total} 条 · 已存平台 {row.saved} · 待同步 {row.waiting}</span>
        </div>
        <div style={{ height: 8, background: 'var(--saas-surface-alt)', borderRadius: 4, overflow: 'hidden', border: '1px solid var(--saas-border)' }}>
          <div style={{ width: `${row.total / maxPlatform * 100}%`, height: '100%', background: row.platform === 'wechat' ? '#1f7a4d' : row.platform === 'toutiao' ? '#b42318' : '#2563a8' }} />
        </div>
      </div>)}
      <p className="cm-muted" style={{ marginTop: 10 }}>已存平台 = 该草稿记录中存在 remote_id；待同步 = 尚未取得平台侧标识。</p>
    </section>

    <div className="dashboard-grid">
      <section className="panel">
        <div className="panel-title"><h2>草稿状态构成</h2><button className="text-link" onClick={() => onGo('drafts')}>去草稿箱<ArrowUpRight size={13} /></button></div>
        {byState.length === 0 ? <p className="cm-muted">暂无数据</p> : byState.map(([s, n]) => <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 7, fontSize: 12 }}>
          <span className={`tag ${tone(s)}`} style={{ width: 92, justifyContent: 'center' }}>{STATE_LABEL[s] ?? s}</span>
          <div style={{ flex: 1, height: 6, background: 'var(--saas-surface-alt)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${n / maxState * 100}%`, height: '100%', background: 'var(--saas-primary)' }} />
          </div>
          <span style={{ width: 30, textAlign: 'right' }}>{n}</span>
        </div>)}
      </section>

      <section className="panel">
        <div className="panel-title"><h2>选题状态构成</h2><button className="text-link" onClick={() => onGo('topics')}>去选题中心<ArrowUpRight size={13} /></button></div>
        {topicStatus.map(row => <div key={row.state} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, padding: '6px 0', borderBottom: '1px solid var(--saas-border)' }}>
          <span><span className={`tag ${tone(row.state)}`}>{row.label}</span></span>
          <strong>{row.count}</strong>
        </div>)}
        <div className="stat-label" style={{ margin: '14px 0 8px' }}>生成任务</div>
        {jobStatus.filter(j => j.count > 0).map(row => <div key={row.state} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, padding: '5px 0' }}>
          <span className="cm-muted">{row.state}</span><span>{row.count}</span>
        </div>)}
        {jobStatus.every(j => j.count === 0) && <p className="cm-muted">暂无任务记录</p>}
      </section>
    </div>

    <section className="panel">
      <div className="panel-title"><h2>已知数据边界</h2></div>
      <ul style={{ fontSize: 12, color: 'var(--saas-text-secondary)', lineHeight: 1.9, paddingLeft: 18, margin: 0 }}>
        <li>阅读量、涨粉、互动等平台侧指标<b>尚未接入</b>，本版不显示，也不以估算值填充。</li>
        <li>发布记录为人工登记，{dashboard?.stats.published ?? 0} 条；平台真实发布状态需你自行核对。</li>
        <li>平台连接状态取自最近一次会话观察，不代表当前会话仍有效。</li>
        <li>定时任务依赖服务进程运行，关机或服务停止期间不生产、也不补发。</li>
      </ul>
    </section>
  </>;
}
