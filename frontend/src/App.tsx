import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertTriangle, ArrowUpRight, Bell, BookOpen, CalendarClock, CheckCircle2, CheckCheck, ChevronRight, CircleCheck, Check, ClipboardCheck, ClipboardList, Copy, Database, ExternalLink, FileSpreadsheet, FilePlus2, FolderPen, Inbox, LayoutDashboard, LoaderCircle, MoreHorizontal, Package, Pencil, PieChart, Plus, Radar, RefreshCw, Save, Search, Settings as SettingsIcon, ShieldCheck, Square, TrendingUp, X } from 'lucide-react';
import { createDrafts } from './draftCreation';
import { api, setToken } from './api';
import { LoginPanel, DeviceSessions, type AuthStatus } from './AuthPanel';
import { ConnectivityNotice } from './pwa';
import TrendRadar from './TrendRadar';
import PlatformCapabilities from './PlatformCapabilities';
import PlatformAccessSettings from './PlatformAccessSettings';
import SchedulePanel from './SchedulePanel';
import FormatAdvisor from './FormatAdvisor';
import LayoutAuditPanel from './LayoutAuditPanel';
import ContentStudio from './ContentStudio';
import RuntimeStatus from './RuntimeStatus';
import { PLATFORM_IDS as platforms, PLATFORM_NAMES as names, isVideoPlatform } from './platforms';
import EditorialControls from './EditorialControls';
import { AuditLogPage, AnalyticsPanel, ContentManager, TrashBin } from './ContentManager';
import { ModeBanner, V3DataPage, V3PublishPage } from './V3Panels';
import QuickDraft from './QuickDraft';
import type { Bootstrap, BrowserBridge, BrowserStatus, DashboardPayload, Draft, DraftMedia, Health, ImageAsset, ImageAssetManifest, Job, Platform, Settings, Source, SourcePolicy, State, Topic, Version, Work, ContentLine } from './types';

type Page = 'overview' | 'trends' | 'topics' | 'quickdraft' | 'projects' | 'content' | 'drafts' | 'review' | 'publish' | 'data' | 'schedule' | 'analytics' | 'trash' | 'logs' | 'settings';
type Run = (label: string, task: () => Promise<unknown>, success?: string) => Promise<boolean>;
const backends: Partial<Record<Platform, string>> = { wechat: 'https://mp.weixin.qq.com/', toutiao: 'https://mp.toutiao.com/', xiaohongshu: 'https://creator.xiaohongshu.com/' };
// 八项一级导航。内容管理是本次新增的表格页，回收站与操作日志作为其子页收在
// 内容管理分组下，避免主导航层级过深。
const NAV_GROUPS: { caption: string; items: { id: Page; name: string; icon: typeof Inbox; subtitle: string }[] }[] = [
  { caption: '运营', items: [
    { id: 'overview', name: '运营总览', icon: LayoutDashboard, subtitle: '今日真实进度、平台连接与异常提醒' },
    { id: 'quickdraft', name: '底稿快速生产', icon: FilePlus2, subtitle: '粘贴你的底稿，提炼要点，一次生成各平台稿件' },
    { id: 'trends', name: '智能信息雷达', icon: Radar, subtitle: '跟踪已上榜事件与升温信号，发现值得提前关注的选题' },
    { id: 'topics', name: '选题中心', icon: ClipboardList, subtitle: '明确方向与来源，确认后交给 AI' },
    { id: 'projects', name: '内容工坊', icon: FolderPen, subtitle: '资料、图文与脚本编辑、平台预览和审阅' },
  ]},
  { caption: '内容', items: [
    { id: 'content', name: '内容管理', icon: FileSpreadsheet, subtitle: '表格化管理全部选题与平台草稿，支持搜索、筛选与批量操作' },
    { id: 'review', name: '审阅与发布准备', icon: ClipboardCheck, subtitle: '共享事实底稿、所选平台审阅与发布准备' },
    { id: 'publish', name: '发布台账', icon: Package, subtitle: '人工发布登记、链接与最终稿回填' },
    { id: 'data', name: '数据复盘', icon: PieChart, subtitle: '导入运营数据、匿名反馈与每周复盘' },
    { id: 'drafts', name: '平台草稿箱', icon: Inbox, subtitle: '本地作品与历史草稿记录；发布请在官方后台人工完成' },
    { id: 'schedule', name: '发布排期', icon: CalendarClock, subtitle: '已批准选题按北京时间排队，到点自动生产' },
    { id: 'analytics', name: '数据分析', icon: TrendingUp, subtitle: '按平台与状态查看内容分布与产出情况' },
  ]},
  { caption: '系统', items: [
    { id: 'trash', name: '回收站', icon: Database, subtitle: '已删除内容保留快照，可恢复或永久删除' },
    { id: 'logs', name: '操作日志', icon: BookOpen, subtitle: '记录新建、编辑、归档、删除、同步与失败' },
    { id: 'settings', name: '系统设置', icon: SettingsIcon, subtitle: '管理本机服务、资料库与平台接入' },
  ]},
];
const pages: { id: Page; name: string; icon: typeof Inbox; subtitle: string }[] = NAV_GROUPS.flatMap(g => g.items);
const RAIL_PAGES: Page[] = ['overview', 'trends', 'topics', 'quickdraft', 'projects', 'content', 'publish', 'data'];
const RAIL_NAMES: Partial<Record<Page, string>> = { overview: '工作桌', trends: '信息雷达', topics: '选题', quickdraft: '快速生产', projects: '内容工坊', content: '内容库', publish: '发布台账', data: '复盘' };
function PrismMark() { return <svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="12" fill="#1c2230" /><path d="M6 20h10M17 11l9 18H11z" fill="none" stroke="#fbfcfd" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /><path d="M24 18l10-6" stroke="#2ba471" strokeWidth="2" strokeLinecap="round" /><path d="M25 22l9-1" stroke="#ee7a4b" strokeWidth="2" strokeLinecap="round" /><path d="M26 26l8 5" stroke="#e5484d" strokeWidth="2" strokeLinecap="round" /></svg>; }
const topicStates = { pending: '待确认', approved: '已批准', rejected: '已拒绝', parked: '暂存' };
const jobStates = { queued: '排队中', running: '生成中', succeeded: '正文已生成', failed: '失败', cancelled: '已取消' };
const draftStates = { local: '本机恢复快照', needs_connection: '未存入平台', syncing: '存入中', unknown: '保存结果待核对', synced: '已存入平台', browser_saved: '已存专用浏览器草稿', reviewed: '已人工审核', published: '已发布 · 人工记录', stale: '版本已过期', blocked: '需修订', failed: '同步失败' };
const emptyState: State = { topics: [], jobs: [], publications: [], drafts: [], notifications: [], activity: [], overview: {} };
function time(value?: string | null) { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); }
function stateTone(state: string) { return ['approved', 'passed', 'succeeded', 'synced', 'browser_saved', 'reviewed', 'published'].includes(state) ? 'green' : ['blocked', 'failed', 'rejected'].includes(state) ? 'red' : ['pending', 'needs_connection', 'queued', 'running', 'syncing', 'unknown'].includes(state) ? 'amber' : 'muted'; }
function isAwaitingReview(draft: Draft) { return (draft.destination === 'platform' && draft.state === 'synced') || (draft.destination === 'browser' && draft.state === 'browser_saved'); }
function mediaVerified(draft: Draft) { const media = draft.media; return media?.status === 'verified' && media.expected_count > 0 && media.verified_count >= media.expected_count && (draft.platform === 'xiaohongshu' || media.cover_verified === true); }
function draftStateLabel(draft: Draft) { return ['synced', 'browser_saved'].includes(draft.state) && !mediaVerified(draft) ? '正文已保存 · 素材待核对' : draftStates[draft.state]; }
function draftStateTone(draft: Draft) { return ['synced', 'browser_saved'].includes(draft.state) && !mediaVerified(draft) ? 'amber' : stateTone(draft.state); }
function matchesDraftFilter(draft: Draft, filter: string) { return filter === 'all' || (filter === 'synced' ? isAwaitingReview(draft) : draft.state === filter); }
function browserSaveVerified(status: BrowserStatus | null | undefined) { return status?.draft_capability_verified === true || status?.verified_draft_capability === true; }
function safeURL(value: string | null, fallback: string) { return value && /^https?:\/\//i.test(value) ? value : fallback; }
function stageName(value: string) { const labels: Record<string, string> = { queued: '等待生产', routing: '选择推理服务', researching: '读取来源正文', creating: '创建内容', generating: '生成正文', validating: '检查结构与规则', syncing: '保存各平台草稿', completed: '正文已生成，实际交付请查看平台草稿状态', succeeded: '正文已生成，实际交付请查看平台草稿状态', generating_assets: '准备实际图片素材', syncing_media: '上传并核对图片素材', media_pending: '图片素材交付待处理', delivery_pending: '正文已生成，草稿交付待处理', delivery_completed: '正文与素材已交付，等待人工审核', delivered: '正文与素材已交付，等待人工审核', cancelled: '已取消', failed: '生成失败' }; return labels[value] || value || '等待处理'; }
function Tag({ children, tone = 'muted' }: { children: ReactNode; tone?: string }) { return <span className={`tag ${tone}`}>{children}</span>; }
function Empty({ icon: Icon = Inbox, title, children, action }: { icon?: typeof Inbox; title: string; children: ReactNode; action?: ReactNode }) { return <div className="empty"><div className="empty-icon"><Icon size={25} /></div><h3>{title}</h3><p>{children}</p>{action}</div>; }
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
function Btn({ children, onClick, disabled, tone = '', type = 'button', title }: { children: ReactNode; onClick?: () => void; disabled?: boolean; tone?: string; type?: 'button' | 'submit'; title?: string }) { return <button type={type} className={`btn ${tone}`} onClick={onClick} disabled={disabled} title={title}>{children}</button>; }
function PlatformChecks({ value, change, disabled }: { value: Platform[]; change: (v: Platform[]) => void; disabled?: boolean }) { return <div className="platform-checks">{platforms.map(p => <label key={p}><input type="checkbox" checked={value.includes(p)} disabled={disabled} onChange={e => change(e.target.checked ? [...value, p] : value.filter(x => x !== p))} /><span className={`platform-dot ${p}`} />{names[p]}</label>)}</div>; }

function policyItems(policy?: SourcePolicy) {
  const values = [...(policy?.review_items || []), ...(policy?.reasons || []), ...(policy?.findings || []), ...(policy?.issues || [])];
  return Array.from(new Set(values.map(value => { if (typeof value === 'string') return value; if (value && typeof value === 'object') { const item = value as Record<string, unknown>; return typeof item.message === 'string' ? item.message : typeof item.description === 'string' ? item.description : typeof item.title === 'string' ? item.title : ''; } return ''; }).filter(Boolean)));
}
function SourceReviewNote({ policy }: { policy?: SourcePolicy }) {
  if (!policy?.requires_review) return null;
  const items = policyItems(policy);
  return <div className="callout warning source-review-note"><strong>来源与素材需要人工审核</strong><p>{policy.message || '请核对引用出处、素材授权及事件说明。以下是待确认事项。'}</p>{items.length > 0 && <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul>}</div>;
}
const clipboardCtor = (globalThis as { ClipboardItem?: typeof ClipboardItem }).ClipboardItem;

async function copyImageBlobToClipboard(previewURL: string): Promise<void> {
  const response = await fetch(previewURL);
  if (!response.ok) throw new Error(`读取图片失败：HTTP ${response.status}`);
  const blob = await response.blob();
  const png = blob.type === 'image/png' ? blob : await new Promise<Blob>((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        canvas.getContext('2d')!.drawImage(image, 0, 0);
        canvas.toBlob(value => value ? resolve(value) : reject(new Error('PNG 转换失败')), 'image/png');
      } catch (error) { reject(error instanceof Error ? error : new Error('PNG 转换失败')); }
    };
    image.onerror = () => reject(new Error('图片解码失败'));
    image.src = URL.createObjectURL(blob);
  });
  if (!clipboardCtor || !navigator.clipboard?.write) throw new Error('浏览器不支持图片复制');
  await navigator.clipboard.write([new clipboardCtor({ 'image/png': png })]);
}

function AssetPreview({ asset, index, flash }: { asset: ImageAsset; index: number; flash?: (message: string, error?: boolean) => void }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [asset.url]);
  const previewURL = asset.url && /^\/api\/versions\/[A-Za-z0-9_-]+\/assets\/(wechat|toutiao|xiaohongshu|weixin_channels|kuaishou|douyin)\/[0-9a-f]{24}$/.test(asset.url) ? asset.url : null;
  // 溯源字段后端写的是 source_url / source_title（见 source_images._asset_provenance）。
  // 这里同时兼容历史的 title / source_name / url 写法，避免旧版本素材显示成「无来源」。
  const source = asset.source as Record<string, unknown> | undefined;
  const text = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim() : '';
  const sourceTitle = text(source?.source_title) || text(source?.title) || text(source?.source_name);
  const sourcePublisher = text(source?.publisher);
  // 只放行 http/https，杜绝 javascript: 之类的伪协议进入 href。
  const rawURL = text(source?.source_url) || text(source?.url);
  const sourceURL = /^https?:\/\//i.test(rawURL) ? rawURL : null;
  const licenseURL = /^https?:\/\//i.test(text(asset.license_url)) ? text(asset.license_url) : null;
  const license = asset.license === 'provider_generation_terms' ? '生成服务条款，发布前审阅' : asset.license === 'local_model_terms_unverified' ? '本地模型条款待核对' : !asset.license || asset.license === 'unknown' ? '授权信息待确认' : asset.license === 'source_publisher_material_user_decides' ? '来源发布方素材，授权由你确认' : asset.license;
  const providerLabel = asset.provider === 'codex_imagegen' ? 'Codex 图像生成' : asset.provider === 'comfyui' ? '本机 ComfyUI' : asset.provider === 'source' ? '原文源生图' : '已登记素材';
  const hasLink = !!sourceURL;
  const copyImage = async () => {
    if (!previewURL) return;
    try { await copyImageBlobToClipboard(previewURL); flash?.('图片已复制，到平台编辑器按 Ctrl+V 粘贴。'); }
    catch (error) { flash?.(error instanceof Error ? `复制图片未完成：${error.message}` : '浏览器未允许复制图片，请用预览大图手动保存。', true); }
  };
  return <figure className="asset-preview"><div className={`asset-image ${failed || !previewURL ? 'unavailable' : ''}`}>{previewURL && !failed ? <a href={previewURL} target="_blank" rel="noreferrer"><img src={previewURL} alt={asset.caption || `${asset.role === 'cover' ? '封面' : '正文图片'} ${index + 1}`} loading="lazy" onError={() => setFailed(true)} /></a> : <span>{failed ? '图片预览读取失败，请核对实际文件' : '该图片暂无可用预览入口'}</span>}</div><figcaption><div><strong>{asset.role === 'cover' ? '封面' : `正文图片 ${index + 1}`}</strong><Tag tone={asset.is_ai_generated ? 'amber' : 'muted'}>{asset.is_ai_generated ? 'AI 生成示意图' : '来源素材'}</Tag>{!asset.depicts_verified_event && !asset.is_ai_generated && <Tag tone="amber">非事件现场照片</Tag>}</div>{asset.caption && <p className="asset-caption">{asset.caption}</p>}<p>来源：{providerLabel}{sourceTitle ? ` · ${sourceTitle}` : ' · 未记录原文标题'}{sourcePublisher ? ` · ${sourcePublisher}` : ''}</p><p>许可：{license}{asset.rights_holder && ` · 权利人 ${asset.rights_holder}`}</p><div className="asset-links">{previewURL && !failed && <button className="asset-link" onClick={() => void copyImage()}><Copy size={12} />复制图片</button>}{hasLink ? <a className="asset-link" href={sourceURL!} target="_blank" rel="noreferrer"><ExternalLink size={12} />查看原始来源</a> : <span className="asset-link disabled" title="该素材未记录原文链接，无法溯源">未记录原文链接</span>}{licenseURL && <a className="asset-link" href={licenseURL} target="_blank" rel="noreferrer"><ExternalLink size={12} />许可条款</a>}<span className={`asset-link plain ${asset.depicts_verified_event ? 'verified' : ''}`}>{asset.depicts_verified_event ? '已确认为事件现场照片' : '未确认是否对应正文事件'}</span></div>{asset.review_required && <small>请检查画面、授权范围与 AI 标识。</small>}</figcaption></figure>;
}
function AssetPanel({ manifest, policy, media, platform, versionId, run, flash }: { manifest?: ImageAssetManifest; policy?: SourcePolicy; media?: DraftMedia; platform: Platform; versionId?: string; run?: Run; flash?: (message: string, error?: boolean) => void }) {
  const status = manifest?.status || 'not_started';
  const labels: Record<string, string> = { not_started: '尚未准备图片', ready: '本机图片已就绪', generating: '正在准备图片', failed: '图片准备失败', unknown: '图片结果待核对', cancelled: '图片任务已取消', blocked: '图片需处理', corrupt: '图片文件校验失败', needs_review: '素材待审核' };
  const assets = manifest?.assets || [];
  const candidates = manifest?.material_search?.candidates || [];
  const materialLink = (value?: string) => { try { const url = new URL(value || ''); return url.protocol === 'https:' && url.hostname === 'commons.wikimedia.org' && !url.username && !url.password ? url.href : null; } catch { return null; } };
  const verified = media?.status === 'verified' && media.expected_count > 0 && media.verified_count >= media.expected_count && (platform === 'xiaohongshu' || media.cover_verified);
  const selectCandidate = (index: number, assetId: string) => { if (!versionId || !run) return; void run('切换候选图片', () => api(`/versions/${versionId}/assets/${platform}/select/${index}`, 'POST', { asset_id: assetId }), '已切换该图片位的候选版本。'); };
  const slotCandidates = (index: number) => (manifest?.candidates?.[String(index)] || []).filter(candidate => candidate.id && candidate.url && /^\/api\/versions\/[A-Za-z0-9_-]+\/assets\/(wechat|toutiao|xiaohongshu|weixin_channels|kuaishou|douyin)\/[0-9a-f]{24}$/.test(candidate.url) && candidate.id !== manifest?.selected?.[String(index)]);
  return <section className="asset-panel"><div className="asset-panel-heading"><h3>实际图片素材</h3><Tag tone={status === 'ready' ? 'green' : ['failed', 'corrupt'].includes(status) ? 'red' : 'amber'}>{status === 'generating' && <LoaderCircle className="spin" size={12} />}{labels[status] || '图片状态待核对'}</Tag></div>{manifest?.requested_count !== undefined && <p className="muted-note">本轮实际图片：{manifest.produced_count ?? assets.length} / {manifest.requested_count}{(manifest.remaining_count ?? 0) > 0 && `，还需完成 ${manifest.remaining_count} 张`}{(manifest.unprocessed_prompt_count ?? 0) > 0 && `；另有 ${manifest.unprocessed_prompt_count} 项提示词尚未执行`}</p>}{assets.length > 0 ? <div className="asset-grid">{assets.map((asset, index) => { const slot = asset.prompt_index ?? index; const alternatives = slotCandidates(slot); return <div key={`${asset.id}-${index}`} className="asset-slot"><AssetPreview asset={asset} index={index} flash={flash} />{alternatives.length > 0 && <div className="asset-slot-actions"><div className="candidate-strip">{alternatives.map(candidate => <button key={candidate.id} className="candidate-thumb" title="切换到该候选版本" onClick={() => selectCandidate(slot, candidate.id)}><img src={candidate.url!} alt="候选版本" loading="lazy" /></button>)}</div><small className="muted-note">有 {alternatives.length} 个历史候选可回退</small></div>}</div>; })}</div> : <p className="muted-note">{status === 'generating' ? '正在生成或整理真实图片文件，请等待实际结果。' : '当前版本尚无已验证图片文件。保存草稿时会按配置准备素材；提示词本身不算图片。'}</p>}{manifest?.material_search && <details className="material-candidates"><summary>素材查找与授权审阅（{candidates.length} 项）</summary>{manifest.material_search.error && <p className="muted-note">素材查找：{manifest.material_search.error}。当前实际使用的图片见上方。</p>}{candidates.map(candidate => { const href = materialLink(candidate.description_url); return <div className="material-candidate" key={candidate.id}><strong>{candidate.title || '素材候选'}</strong><Tag tone={candidate.asset_id ? 'green' : 'amber'}>{candidate.asset_id ? '已采用资料图' : '候选 · 尚未采用'}</Tag><p>许可：{candidate.license || '未知'} · 作者：{candidate.artist || '待核对'} · 素材日期：{candidate.date || '未知'}</p>{candidate.relevance?.basis && <p>{candidate.relevance.basis}</p>}{candidate.review_reasons?.map((reason, index) => <p key={index}>{reason}</p>)}{candidate.download_error && <p>{candidate.download_error}</p>}{href && <a className="text-link" href={href} target="_blank" rel="noreferrer">审阅原始素材说明<ExternalLink size={11} /></a>}</div>; })}<p className="muted-note">相关性、许可与日期不明确的素材不会自动采用。使用平台图库时，也请核对授权范围及必要署名。</p></details>}{manifest?.error && <div className="callout error"><strong>图片准备尚未完成</strong><p>{manifest.error}</p></div>}{manifest?.warnings?.length ? <ul className="asset-warnings">{manifest.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}{media && <div className={`media-verification ${verified ? 'verified' : ''}`}><strong>{verified ? '平台正文与图片已回读核对' : '平台图片交付仍需核对'}</strong><span>图片回读：{media.verified_count || 0} / {media.expected_count > 0 ? media.expected_count : '待确认'}</span>{platform !== 'xiaohongshu' && <span>封面：{media.cover_verified ? '已核对' : '待核对'}</span>}{media.error && <p>{media.error}</p>}</div>}<SourceReviewNote policy={policy} /></section>;
}

export default function App() {
  const [page, setPage] = useState<Page>(() => { const value = new URLSearchParams(location.search).get('page'); return pages.some(p => p.id === value) ? value as Page : 'overview'; });
  const [state, setState] = useState<State>(emptyState);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [ready, setReady] = useState(false);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [loginRequired, setLoginRequired] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const [topicId, setTopicId] = useState(() => new URLSearchParams(location.search).get('topic') || '');
  const [versionId, setVersionId] = useState(() => new URLSearchParams(location.search).get('version') || '');
  const [workPlatform, setWorkPlatform] = useState<Platform | null>(() => { const p = new URLSearchParams(location.search).get('platform'); return platforms.includes(p as Platform) ? p as Platform : null; });
  const [newTopic, setNewTopic] = useState(false);
  const [search, setSearch] = useState('');
  const [navOpen, setNavOpen] = useState(false);
  const [quickQuery, setQuickQuery] = useState('');
  const [quickOpen, setQuickOpen] = useState(false);
  const quickInput = useRef<HTMLInputElement>(null);
  const quickHost = useRef<HTMLDivElement>(null);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [desktopAlerts, setDesktopAlerts] = useState(() => { try { return 'Notification' in window && Notification.permission === 'granted' && localStorage.getItem('workbench-desktop-alerts') === 'on'; } catch { return false; } });
  const [mode, setMode] = useState<{ mode: string; mode_label: string; platform_operations: boolean; note: string } | null>(null);
  const seenNotices = useRef<Set<string> | null>(null);
  const pollInFlight = useRef(false);
  const flash = useCallback((message: string, error = false) => setToast({ message, error }), []);
  const refresh = useCallback(async () => { const data = await api<State>('/state'); setState({ ...emptyState, ...data }); setConnectionError(''); return data; }, []);
  // 总览指标与回收站数量：独立端点，读取失败时保持上一次的值而不是回落到
  // 模拟数字。绝不用占位数值冒充真实数据。
  const refreshDashboard = useCallback(async () => {
    try { setDashboard(await api<DashboardPayload>('/dashboard')); }
    catch { setDashboard(current => current); }
    try { const trash = await api<{ total: number }>('/trash?page_size=1'); setTrashCount(trash.total || 0); }
    catch { /* 回收站数量读不到就不显示计数，不影响其他功能 */ }
  }, []);
  const [trashCount, setTrashCount] = useState(0);
  const initialize = useCallback(async () => {
    try { const status = await api<AuthStatus>('/auth/status'); setAuthStatus(status); if (!status.authenticated) { setToken(''); setLoginRequired(true); return; } const data = await api<Bootstrap>('/bootstrap'); setToken(data.token); setSettings(data.settings); setHealth(data.health); await refresh(); await refreshDashboard(); setReady(true); setLoginRequired(false); }
    catch (error) { setConnectionError(error instanceof Error ? error.message : '连接失败'); }
    try { setMode(await api<{ mode: string; mode_label: string; platform_operations: boolean; note: string }>('/runtime-mode')); }
    catch { /* 模式横幅读不到不阻塞工作台使用 */ }
  }, [refresh, refreshDashboard]);
  useEffect(() => { void initialize(); }, [initialize]);
  useEffect(() => { const handler = () => { setToken(''); setLoginRequired(true); }; window.addEventListener('workbench:auth-required', handler); return () => window.removeEventListener('workbench:auth-required', handler); }, []);
  useEffect(() => {
    if (!ready || loginRequired) return;
    const id = window.setInterval(async () => {
      if (pollInFlight.current) return;
      pollInFlight.current = true;
      try { await refresh(); } catch (error) { setConnectionError(error instanceof Error ? error.message : '无法读取工作台状态'); } finally { pollInFlight.current = false; }
    }, 4000);
    return () => window.clearInterval(id);
  }, [ready, loginRequired, refresh]);
  useEffect(() => { if (!toast) return; const id = window.setTimeout(() => setToast(null), toast.error ? 12000 : 6500); return () => window.clearTimeout(id); }, [toast]);
  useEffect(() => {
    if (!ready) return;
    if (!seenNotices.current) { seenNotices.current = new Set(state.notifications.map(n => n.id)); return; }
    for (const notice of state.notifications) {
      if (seenNotices.current.has(notice.id)) continue;
      seenNotices.current.add(notice.id);
      if (!desktopAlerts || !('Notification' in window) || Notification.permission !== 'granted' || notice.read) continue;
      try { const popup = new Notification(notice.title, { body: notice.body, tag: `workbench-${notice.id}` }); popup.onclick = () => { window.focus(); if (notice.topic_id) setTopicId(notice.topic_id); setPage(notice.topic_id ? 'drafts' : 'trends'); setSearch(''); popup.close(); }; } catch { /* 应用内提醒始终可用 */ }
    }
  }, [ready, state.notifications, desktopAlerts]);
  const toggleDesktopAlerts = async () => {
    if (desktopAlerts) { setDesktopAlerts(false); try { localStorage.removeItem('workbench-desktop-alerts'); } catch { /* 当前会话仍生效 */ } flash('已关闭桌面提醒，应用内提醒继续保留。'); return; }
    if (!('Notification' in window)) { flash('当前浏览器不支持桌面通知；请查看应用内提醒。', true); return; }
    try { const permission = await Notification.requestPermission(); if (permission !== 'granted') { flash('桌面通知未获允许，应用内提醒继续保留。'); return; } setDesktopAlerts(true); try { localStorage.setItem('workbench-desktop-alerts', 'on'); } catch { /* 当前会话仍生效 */ } flash('桌面提醒已开启，仅保持此页面打开时有效。'); } catch { flash('桌面通知未能开启，应用内提醒继续保留。', true); }
  };
  const run: Run = async (label, task, success) => {
    if (busy) return false;
    setBusy(label);
    try { await task(); await refresh(); if (success) flash(success); return true; }
    catch (error) { flash(error instanceof Error ? error.message : '操作失败，请重试。', true); try { await refresh(); } catch { /* 保留原操作错误 */ } return false; }
    finally { setBusy(''); }
  };
  const mayNavigate = () => window.dispatchEvent(new Event('workbench:before-navigate', { cancelable: true })) || window.confirm('有尚未保存的修改，确定离开当前内容？');
  const go = (next: Page) => { if (!mayNavigate()) return; setPage(next); setSearch(''); setNavOpen(false); setQuickOpen(false); };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setQuickOpen(false); setNavOpen(false); quickInput.current?.blur(); }
      if (event.key === '/' && !(event.target instanceof Element && event.target.closest('input,textarea,select,[contenteditable]'))) { event.preventDefault(); quickInput.current?.focus(); setQuickOpen(true); }
    };
    const outside = (event: PointerEvent) => { if (event.target instanceof Node && !quickHost.current?.contains(event.target)) setQuickOpen(false); };
    window.addEventListener('keydown', key); window.addEventListener('pointerdown', outside);
    return () => { window.removeEventListener('keydown', key); window.removeEventListener('pointerdown', outside); };
  }, []);
  useEffect(() => {
    const params = new URLSearchParams(); params.set('page', page);
    if (topicId) params.set('topic', topicId);
    if (versionId) params.set('version', versionId);
    if (workPlatform) params.set('platform', workPlatform);
    history.replaceState(null, '', `${location.pathname}?${params.toString()}`);
  }, [page, topicId, versionId, workPlatform]);
  const openTopic = (id: string | null, next: Page = 'projects') => { if (!mayNavigate()) return; if (!id) { go('trends'); return; } setTopicId(id); setVersionId(''); setWorkPlatform(null); setNewTopic(false); go(next); };
  const topic = state.topics.find(t => t.id === topicId) ?? state.topics.find(t => t.status === 'approved') ?? state.topics[0] ?? null;
  const studioVersion = topic?.versions.find(v => v.id === versionId) ?? topic?.current_version ?? null;
  useEffect(() => {
    // Pin the displayed base. Polling must never replace an editor containing
    // unsaved text merely because another window produced a newer version.
    if (!topic) return;
    if (topic.id !== topicId) { setTopicId(topic.id); setVersionId(topic.current_version?.id || ''); }
    else if (!versionId && topic.current_version) setVersionId(topic.current_version.id);
  }, [topic?.id, topic?.current_version?.id, topicId, versionId]);
  const currentPage = pages.find(p => p.id === page)!;
  const pending = state.topics.filter(t => t.status === 'pending').length;
  const activeJobs = state.jobs.filter(j => ['queued', 'running'].includes(j.state));
  const unread = state.notifications.filter(n => !n.read);
  const unreadDrafts = unread.filter(n => !!n.topic_id);
  const unreadTrends = unread.filter(n => !n.topic_id);
  const filteredTopics = state.topics.filter(t => `${t.title} ${t.brief}`.toLowerCase().includes(search.toLowerCase()));
  const quickPages = pages.filter(item => `${item.name} ${item.subtitle}`.includes(quickQuery.trim())).slice(0, 5);
  const quickTopics = state.topics.filter(item => `${item.title} ${item.brief}`.toLowerCase().includes(quickQuery.trim().toLowerCase())).slice(0, 4);
  const railCount = (id: Page) => id === 'topics' ? pending : id === 'trends' ? unreadTrends.length : id === 'drafts' ? unreadDrafts.length : id === 'trash' ? trashCount : 0;

  return <div className="app-shell prism-shell">
    <ConnectivityNotice />
    {loginRequired && <LoginPanel status={authStatus} expired={ready} onLoggedIn={initialize} onCheck={initialize} />}
    {navOpen && <button className="nav-scrim" aria-label="关闭菜单" onClick={() => setNavOpen(false)} />}
    <aside className="sidebar prism-rail">
      <button className="prism-brand" aria-label="AI 内容工作台首页" title="AI 内容工作台首页" onClick={() => go('overview')}><PrismMark /></button>
      <nav className="prism-rail-main" aria-label="主导航">{RAIL_PAGES.map(id => { const item = pages.find(p => p.id === id)!; const active = page === id || id === 'projects' && page === 'review'; return <button type="button" key={id} className={`nav-item prism-rail-item ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} aria-label={item.name} title={item.name} onClick={() => go(id)}><item.icon size={20} /><span>{RAIL_NAMES[id]}</span>{railCount(id) > 0 && <i className="prism-rail-count">{railCount(id) > 99 ? '99+' : railCount(id)}</i>}</button>; })}</nav>
      <div className="prism-rail-bottom"><button type="button" className={`nav-item prism-rail-item ${navOpen ? 'active' : ''}`} aria-label="更多功能" aria-expanded={navOpen} aria-controls="prism-navigation" onClick={() => setNavOpen(open => !open)}><MoreHorizontal size={20} /><span>更多</span></button><button type="button" className={`nav-item prism-rail-item ${page === 'settings' ? 'active' : ''}`} aria-label="系统设置" title="系统设置" onClick={() => go('settings')}><SettingsIcon size={19} /><span>设置</span></button><div className="prism-avatar" title="本地工作空间">创</div></div>
    </aside>
    {navOpen && <aside id="prism-navigation" className="prism-navigation" aria-label="全部功能"><div className="prism-navigation-heading"><div><strong>你的工作空间</strong><small>AI 内容工作台</small></div><button className="icon-btn" aria-label="关闭全部功能" onClick={() => setNavOpen(false)}><X size={18} /></button></div>{NAV_GROUPS.map(group => <div key={group.caption}><p className="sidebar-caption">{group.caption}</p><nav aria-label={`${group.caption}全部功能`}>{group.items.map(item => <button className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} key={item.id} onClick={() => go(item.id)}><item.icon size={16} /><span>{item.name}</span>{railCount(item.id) > 0 && <span className="nav-count">{railCount(item.id)}</span>}</button>)}</nav></div>)}<p className="prism-navigation-note"><ShieldCheck size={15} />AI 辅助创作，你掌握发布权。</p></aside>}
    <div className="workspace">
      <header className="topbar prism-topbar"><div className="prism-studio-label"><strong>AI <span>内容工作台</span></strong><small>创作者工作空间</small></div><div className="prism-quickfind" ref={quickHost}><label><Search size={16} /><input ref={quickInput} aria-label="搜索页面或选题" aria-expanded={quickOpen} aria-controls="prism-quick-results" placeholder="搜索选题，或跳转到工作台功能…" value={quickQuery} onFocus={() => setQuickOpen(true)} onChange={event => { setQuickQuery(event.target.value); setQuickOpen(true); }} onKeyDown={event => { if (event.key === 'Enter') { if (quickPages[0]) go(quickPages[0].id); else if (quickTopics[0]) openTopic(quickTopics[0].id); } }} /><kbd>/</kbd></label>{quickOpen && <div id="prism-quick-results" className="prism-quick-results"><small>工作台功能</small>{quickPages.map(item => <button key={item.id} onClick={() => go(item.id)}><item.icon size={15} /><span>{item.name}</span><ArrowUpRight size={13} /></button>)}{quickTopics.length > 0 && <small>真实选题</small>}{quickTopics.map(item => <button key={item.id} onClick={() => { setQuickOpen(false); openTopic(item.id); }}><FileSpreadsheet size={15} /><span>{item.title}</span><ChevronRight size={13} /></button>)}{!quickPages.length && !quickTopics.length && <p>没有匹配结果，换个关键词。</p>}<p className="prism-search-note">只搜索本地选题与功能，不会发起 AI 请求。</p></div>}</div><div className="topbar-right"><span className={`prism-service ${ready && !connectionError ? 'connected' : ''}`}><i />{ready && !connectionError ? '本机已连接' : '连接中'}</span>{activeJobs.length > 0 && <span className="live-job"><LoaderCircle className="spin" size={14} />{activeJobs.length} 项任务</span>}<span className="today">{new Date().toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'short' })}</span></div></header>
      <main className={`compact-head ${page === 'projects' || page === 'review' ? 'studio-workspace' : ''}`}>
        <div className="page-heading"><div><div className="eyebrow">你的内容工作空间</div><h1>{page === 'overview' ? '今天，把一个好想法写出来。' : currentPage.name}</h1><p>{page === 'overview' ? `赵公子，${pending} 个选题待确认，${activeJobs.length} 项内容正在生产。每一份作品，都由你定稿。` : currentPage.subtitle}</p></div>{['topics', 'drafts'].includes(page) && <input className="search" aria-label="搜索选题和草稿" placeholder="搜索选题、内容…" value={search} onChange={e => setSearch(e.target.value)} />}{page === 'overview' && <><Btn tone="primary" onClick={() => go('quickdraft')}><FilePlus2 size={16} />粘贴底稿快速生产</Btn><Btn onClick={() => { setNewTopic(true); go('topics'); }}><Plus size={16} />新建选题</Btn></>}</div>
        <ModeBanner mode={mode} />
        {connectionError && <div className="alert error"><div><strong>本机服务连接异常</strong><p>{connectionError}。请确认后端正在 当前地址运行。</p></div><Btn onClick={() => void initialize()}><RefreshCw size={15} />重新连接</Btn></div>}
        {!ready ? <Empty icon={LoaderCircle} title={connectionError ? '等待工作台服务启动' : '正在读取你的工作空间'}>连接成功后显示实际选题、任务和平台草稿，不加载演示内容。</Empty> : <>
          {page === 'overview' && <>
            <section className="prism-next" aria-label="继续创作"><div className="prism-next-copy"><span className="prism-kicker"><i />YOUR NEXT GOOD STORY</span><h2>{topic?.title || '不急着发，先写一篇值得读的。'}</h2><p>{topic ? `${topicStates[topic.status]} · ${topic.current_version ? `V${topic.current_version.number} 正式稿` : '还没有正式稿'} · ${topic.sources.length} 条来源资料` : '从信息雷达发现线索，或把你自己的实测经历变成一个选题。'}</p><div className="prism-next-actions"><Btn tone="primary" onClick={() => topic ? openTopic(topic.id) : go('topics')}>{topic?.current_version ? '继续写这篇' : '打开内容工坊'}<ArrowUpRight size={15} /></Btn><button className="text-link" onClick={() => go('trends')}>找一个值得写的选题<ArrowUpRight size={13} /></button></div></div><div className="prism-flow" aria-label="一份材料，三平台表达"><span className="prism-flow-source"><FileSpreadsheet size={20} />共同事实</span><svg viewBox="0 0 120 144" aria-hidden="true"><path d="M0 72h30C65 72 55 22 120 22" stroke="#2ba471" /><path d="M30 72h90" stroke="#ee7a4b" /><path d="M30 72C65 72 55 122 120 122" stroke="#e5484d" /><circle cx="30" cy="72" r="4" fill="#1c2230" /></svg><div>{platforms.map(p => <button key={p} onClick={() => { if (topic?.current_version) { setWorkPlatform(p); setTopicId(topic.id); setVersionId(''); go('projects'); } else go('topics'); }}><i className={`platform-dot ${p}`} />{names[p]}<ArrowUpRight size={12} /></button>)}</div><small>同一份事实，不同的表达</small></div></section>
            {(() => {
              const stats = dashboard?.stats;
              const cards = [
                { label: '今日新增资讯', number: stats?.today_trends, note: '信息雷达今日采集', icon: Radar, page: 'trends' as Page },
                { label: '待处理选题', number: stats?.pending_topics, note: '批准后才启动生成', icon: ClipboardList, page: 'topics' as Page },
                { label: '创作中内容', number: stats?.producing, note: '运行中的生成任务', icon: FolderPen, page: 'projects' as Page },
                { label: '待核对草稿', number: stats?.awaiting_review, note: '历史平台草稿记录', icon: CheckCheck, page: 'drafts' as Page },
                { label: '平台草稿', number: stats?.platform_drafts, note: '各平台草稿箱记录', icon: Inbox, page: 'content' as Page },
                { label: '已发布内容', number: stats?.published, note: '人工记录的发布结果', icon: CircleCheck, page: 'analytics' as Page },
              ];
              return <div className="stats-grid">{cards.map(card => <button
                key={card.label}
                className={`stat-card${card.number === 0 ? ' is-zero' : ''}`}
                onClick={() => go(card.page)}>
                <div className="stat-label">{card.label}<card.icon size={15} /></div>
                <strong>{card.number === undefined ? '—' : card.number}</strong>
                <small>{card.note}</small>
              </button>)}</div>;
            })()}

            <div className="dashboard-grid">
              <section className="panel">
                <div className="panel-title"><h2>异常与失败提醒</h2>
                  {dashboard?.alerts.length ? <Tag tone="amber">{dashboard.alerts.length} 条</Tag> : <Tag tone="green">无异常</Tag>}</div>
                {!dashboard ? <p className="cm-muted">正在读取总览数据…</p>
                  : dashboard.alerts.length === 0 ? <p className="cm-muted">当前没有需要处理的异常。</p>
                  : <div className="notice-list">{dashboard.alerts.map((alert, index) => <div key={index} className={`notice ${alert.level === 'error' ? 'alert error' : ''}`}>
                      <span className="notice-dot" /><div><strong>{alert.level === 'error' ? '失败' : alert.level === 'warn' ? '待确认' : '提示'}</strong>
                      <p>{alert.message}</p></div>
                      <button className="text-link" onClick={() => go('settings')}>去检查</button>
                    </div>)}</div>}
              </section>

              <section className="panel">
                <div className="panel-title"><h2>平台历史记录</h2>
                  <button className="text-link" onClick={() => go('settings')}>系统设置<ArrowUpRight size={13} /></button></div>
                <div className="cm-platforms">
                  {!dashboard ? <p className="cm-muted">正在读取平台状态…</p>
                    : dashboard.platforms.map(item => <div key={item.platform + item.channel} className="cm-platform">
                      <div className="name">
                        {names[item.platform]}
                        <Tag tone={!item.implemented ? 'muted' : item.auth_state === 'connected_observed' ? 'green' : item.auth_state === 'unknown' ? 'amber' : 'red'}>
                          {!item.implemented ? '未实现' : item.auth_state === 'connected_observed' ? '已连接' : item.auth_state === 'unknown' ? '未确认' : item.auth_state}
                        </Tag>
                      </div>
                      <div className="meta">{item.label}</div>
                      <div className="meta">{item.implemented ? `草稿记录 ${item.drafts} 条 · 最近核查 ${item.checked_at ? time(item.checked_at) : '从未'}` : (item.note || '该形态尚未实现，暂不能交付。')}</div>
                    </div>)}
                </div>
                <p className="cm-muted" style={{ marginTop: 10 }}>{dashboard?.notice}</p>
              </section>
            </div>

            <div className="dashboard-grid">
              <section className="panel">
                <div className="panel-title"><h2>最近运营任务</h2>
                  <button className="text-link" onClick={() => go('projects')}>全部任务<ArrowUpRight size={13} /></button></div>
                {state.jobs.length === 0 ? <p className="cm-muted">还没有生成任务记录。</p>
                  : state.jobs.slice(0, 6).map(job => <div key={job.id} className="recent-topic">
                    <div><strong>{state.topics.find(t => t.id === job.topic_id)?.title ?? '未知选题'}</strong>
                      <small>{stageName(job.stage)} · {time(job.updated_at)}</small></div>
                    <Tag tone={stateTone(job.state)}>{jobStates[job.state] ?? job.state}</Tag>
                  </div>)}
              </section>

              <section className="panel">
                <div className="panel-title"><h2>自动化任务运行状态</h2>
                  <button className="text-link" onClick={() => go('schedule')}>发布排期<ArrowUpRight size={13} /></button></div>
                <div className="stat-label" style={{ marginBottom: 8 }}>本地生产定时器 · 不自动发布</div>
                <p><span className={`status-dot ${dashboard?.scheduler.running ? 'on' : ''}`} />
                  {dashboard?.scheduler.running ? `运行中，每 ${dashboard.scheduler.tick_seconds} 秒检查一次` : '未运行'}</p>
                <p className="cm-muted">{dashboard?.scheduler.note}</p>
                <div className="stat-label" style={{ margin: '12px 0 8px' }}>最近批次</div>
                {dashboard?.last_production_run
                  ? <><p>{dashboard.last_production_run.batch_date} · {dashboard.last_production_run.state}</p>
                    <p className="cm-muted">{time(dashboard.last_production_run.started_at)}{dashboard.last_production_run.finished_at && ` → ${time(dashboard.last_production_run.finished_at)}`}</p></>
                  : <p className="cm-muted">暂无数据</p>}
              </section>
            </div>

            <section className="panel service-strip">
              <div><span className="status-dot on" />推理服务</div>
              <span>本地模型：{health?.local.available ? '已连接' : '未连接'}</span>
              <span>本地模型生产 · 图文与视频脚本</span>
              <button className="text-link" onClick={() => go('settings')}>检查设置<ArrowUpRight size={13} /></button>
            </section>
          </>}
          {page === 'trends' && <TrendRadar busy={busy} run={run} flash={flash} onSelected={id => openTopic(id, 'topics')} />}
          {page === 'quickdraft' && <QuickDraft busy={busy} run={run} flash={flash} onProduced={id => openTopic(id, 'projects')} />}
          {page === 'topics' && <div className="split-layout"><section className="panel topic-list"><div className="panel-title"><h2>选题库 <small>{state.topics.length}</small></h2><Btn tone="small" onClick={() => setNewTopic(true)}><Plus size={15} />新建</Btn></div>{filteredTopics.length === 0 ? <Empty icon={ClipboardList} title={search ? '没有匹配选题' : '还没有选题'}>{search ? '试试其他关键词。' : '创建选题后，人工批准再开始生产。'}</Empty> : filteredTopics.map(t => <button className={`topic-card ${!newTopic && topic?.id === t.id ? 'selected' : ''}`} key={t.id} onClick={() => { setTopicId(t.id); setNewTopic(false); }}><div className="card-top"><Tag tone={stateTone(t.status)}>{topicStates[t.status]}</Tag><small>{time(t.updated_at)}</small></div><h3>{t.title}</h3><p>{t.brief || '暂无选题说明'}</p><div className="platform-chips">{t.platforms.map(p => <span key={p}><i className={`platform-dot ${p}`} />{names[p]}</span>)}</div></button>)}</section><TopicForm key={newTopic ? 'new' : topic?.id ?? 'none'} topic={newTopic ? null : topic} busy={busy} run={run} onSaved={id => { setTopicId(id); setNewTopic(false); }} onOpen={id => openTopic(id)} /></div>}
          {(page === 'projects' || page === 'review') && <ContentStudio topic={topic} version={studioVersion} topics={state.topics} initialPlatform={workPlatform} onPlatformChange={setWorkPlatform} busy={busy} run={run} onVersion={setVersionId} onTopic={id => openTopic(id)} onChooseTopic={() => go('topics')} onPublish={() => go('publish')} refresh={() => { void refresh(); }} mode={mode} tools={topic ? <StudioTools topic={topic} version={studioVersion} busy={busy} run={run} onVersion={setVersionId} /> : undefined} />}
          {page === 'drafts' && <Drafts drafts={state.drafts} topics={state.topics} search={search} busy={busy} run={run} flash={flash} onEdit={(id, vid, platform) => { setTopicId(id); setVersionId(vid); setWorkPlatform(platform); go('projects'); }} />}
          {page === 'schedule' && <SchedulePanel topics={state.topics} busy={busy} run={run} />}
          {page === 'content' && <ContentManager run={run} flash={flash} busy={busy}
            onOpenTopic={id => openTopic(id, 'projects')}
            onOpenDrafts={(topicId, versionId, platform) => { setTopicId(topicId); setVersionId(versionId); setWorkPlatform(platform); go('projects'); }} />}
          {page === 'trash' && <TrashBin run={run} flash={flash} />}
          {page === 'logs' && <AuditLogPage flash={flash} />}
          {page === 'analytics' && <AnalyticsPanel state={state} dashboard={dashboard} onGo={go} />}
          {page === 'settings' && settings && <SettingsPage settings={settings} health={health} setSettings={setSettings} setHealth={setHealth} busy={busy} run={run} desktopAlerts={desktopAlerts} toggleDesktopAlerts={toggleDesktopAlerts} mode={mode} />}
          {page === 'publish' && <V3PublishPage topicId={topicId} />}
          {page === 'data' && <V3DataPage />}
        </>}
      </main>
    </div>
    {busy && <div className="busy-indicator" role="status"><LoaderCircle size={16} className="spin" />{busy}…</div>}
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}><span>{toast.error ? '操作未完成：' : ''}{toast.message}</span><button className="icon-btn" aria-label="关闭提示" onClick={() => setToast(null)}><X size={16} /></button></div>}
  </div>;
}

interface SourceInspection {
  items: { url: string; title: string; kind: 'index' | 'original'; kind_label: string; hint: string }[];
  original_count: number; index_count: number; total: number;
  usable: boolean; message: string; action: string | null;
}
interface ResearchResult {
  sources: Source[]; errors: string[]; message: string;
  documents?: { url: string; title: string; fetched_at: string; published_at: string | null }[];
  source_images?: number; inspection?: SourceInspection;
}

/* 来源提示：自动创作会继续查找可用原文，不阻断本地工作稿。 */
function SourceGate({ inspection, busy, onResearch }: {
  inspection: SourceInspection | null; busy: string; onResearch: () => void;
}) {
  if (!inspection || inspection.total === 0) return null;
  const bad = inspection.items.filter(i => i.kind === 'index');
  if (inspection.usable) {
    return <div className="source-gate ok">
      <CheckCircle2 size={14} />
      <span>来源体检通过：{inspection.original_count} 条原始报道可用于写作</span>
      <span className="cm-muted">（{inspection.index_count} 条搜索页/热榜不作为正文依据）</span>
    </div>;
  }
  return <div className="source-gate warning">
    <AlertTriangle size={14} />
    <div>
      <strong>这些来源需要补查原文</strong>
      <p>可以继续创作。系统会自动查找可用原文，资料不足时明确提供有限草稿或提纲。</p>
      <ul>{bad.map(item => <li key={item.url}>
        <a href={item.url} target="_blank" rel="noreferrer">{item.title || item.url.slice(0, 60)}<ExternalLink size={11} /></a>
        <span className="cm-muted">{item.hint}</span>
      </li>)}</ul>
      <p className="cm-muted">也可以提前检索原文并加入来源，作为可选补充。</p>
    </div>
    <button className="btn primary" disabled={!!busy} onClick={onResearch}>检索并读取原文</button>
  </div>;
}

function TopicForm({ topic, busy, run, onSaved, onOpen }: { topic: Topic | null; busy: string; run: Run; onSaved: (id: string) => void; onOpen: (id: string) => void }) {
  const [title, setTitle] = useState(topic?.title ?? '');
  const [brief, setBrief] = useState(topic?.brief ?? '');
  const [videoDuration, setVideoDuration] = useState(topic?.video_target_duration_seconds || 60);
  const [selected, setSelected] = useState<Platform[]>(topic?.platforms ?? platforms);
  const [sources, setSources] = useState<Source[]>(topic?.sources ?? []);
  const [contentLine, setContentLine] = useState<ContentLine | null>(topic?.content_line ?? null);
  const [stanceIds, setStanceIds] = useState<string[]>(topic?.stance_ids ?? []);
  const [riskAccepted, setRiskAccepted] = useState(false);
  const [validation, setValidation] = useState('');
  const [research, setResearch] = useState<ResearchResult | null>(null);
  const [inspection, setInspection] = useState<SourceInspection | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    setTitle(topic?.title ?? ''); setBrief(topic?.brief ?? ''); setSelected(topic?.platforms ?? platforms); setSources(topic?.sources ?? []);
    setContentLine(topic?.content_line ?? null); setStanceIds(topic?.stance_ids ?? []); setRiskAccepted(false); setResearch(null);
  }, [topic?.id, topic?.revision]);

  // 来源一变就重新体检。这是「批准前预检」的关键：不等到点批准那一刻才知道。
  const filled = sources.filter(s => s.url && s.url.trim());
  useEffect(() => {
    if (!filled.length) { setInspection(null); setChecking(false); return; }
    let cancelled = false;
    setChecking(true);
    api<SourceInspection>('/sources/inspect', 'POST', { sources: filled })
      .then(data => { if (!cancelled) setInspection(data); })
      .catch(() => { if (!cancelled) setInspection(null); })
      .finally(() => { if (!cancelled) setChecking(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filled.map(s => s.url.trim()))]);

  const dirty = videoDuration !== (topic?.video_target_duration_seconds || 60) || title !== (topic?.title ?? '') || brief !== (topic?.brief ?? '') || JSON.stringify(selected) !== JSON.stringify(topic?.platforms ?? []) || JSON.stringify(sources) !== JSON.stringify(topic?.sources ?? []) || contentLine !== (topic?.content_line ?? null) || JSON.stringify(stanceIds) !== JSON.stringify(topic?.stance_ids ?? []);
  const canGenerate = !!contentLine && !!title.trim() && selected.length > 0;
  const creationPending = useRef(false);
  const requiredRisks = topic?.required_risk_confirmations || [];
  const riskConfirmed = !!topic?.risk_confirmation && topic.risk_confirmation.revision === topic.revision &&
    JSON.stringify([...topic.risk_confirmation.risks].sort()) === JSON.stringify([...requiredRisks].sort());

  const save = () => {
    if (!title.trim() || selected.length === 0) { setValidation('请填写选题标题，并至少选择一个平台。'); return; }
    if (sources.some(s => s.url.trim() && !/^https?:\/\//i.test(s.url.trim()))) { setValidation('来源地址需要以 http:// 或 https:// 开头。'); return; }
    setValidation('');
    void run('保存选题', async () => { const payload = { title: title.trim(), brief, platforms: selected, sources: sources.filter(s => s.url.trim() || s.title.trim() || s.note.trim()), content_line: contentLine, stance_ids: stanceIds, video_target_duration_seconds: videoDuration, ...(topic ? { expected_revision: topic.revision } : {}) }; const result = await api<Topic>(topic ? `/topics/${topic.id}` : '/topics', topic ? 'PATCH' : 'POST', payload); onSaved(result.id); }, '选题已保存。');
  };

  // 把检索读回的原文候选并入来源列表。按 URL 去重，已在列表里的不重复添加。
  const adoptResearch = () => {
    const fresh = (research?.sources || []).filter(s => s.url && /^https?:\/\//i.test(s.url));
    if (!fresh.length) { setValidation('检索没有取回可用的原文链接。请检查网络或手动补充来源。'); return; }
    const known = new Set(sources.map(s => s.url.trim()));
    const added = fresh.filter(s => !known.has(s.url.trim()));
    if (!added.length) { setValidation('检索到的原文都已在来源列表里，无需重复添加。'); return; }
    setSources([...sources, ...added]);
    setValidation(`已加入 ${added.length} 条原文来源。保存后可批准并创作。`);
  };

  const decide = (decision: 'approved' | 'rejected' | 'parked', enterStudio = false) => {
    if (!topic || creationPending.current) return;
    if (!title.trim() || !selected.length) { setValidation('请填写标题并选择目标平台。'); return; }
    if (enterStudio && !contentLine) { setValidation('请先选择 A/B/C 内容线并保存选题，再确认写作范围。'); return; }
    if (decision === 'approved' && requiredRisks.length > 0 && !riskConfirmed && !riskAccepted) {
      setValidation('请勾选确认当前修订的金融/时事风险，再批准选题。'); return;
    }
    creationPending.current = true;
    void run(enterStudio ? '批准并创作' : '更新选题决定', async () => {
      let currentTopic = topic;
      if (enterStudio && dirty) {
        currentTopic = await api<Topic>(`/topics/${topic.id}`, 'PATCH', { title: title.trim(), brief, platforms: selected, sources: sources.filter(source => source.url.trim() || source.title.trim() || source.note.trim()), content_line: contentLine, stance_ids: stanceIds, video_target_duration_seconds: videoDuration, expected_revision: topic.revision });
        onSaved(currentTopic.id);
      }
      const currentRisks = currentTopic.required_risk_confirmations || [];
      const currentRiskConfirmed = currentTopic.risk_confirmation?.revision === currentTopic.revision;
      if (decision === 'approved' && currentRisks.length && !currentRiskConfirmed) {
        if (dirty || !riskAccepted) throw new Error('选题已保存；请核对新修订的金融/时事风险并明确确认后，再批准创作。');
        await api<Topic>(`/topics/${currentTopic.id}/risk-confirmation`, 'POST', { expected_revision: currentTopic.revision, risks: currentRisks });
      }
      if (enterStudio) { await createDrafts(currentTopic); onOpen(currentTopic.id); }
      else await api<Topic>(`/topics/${currentTopic.id}/decision`, 'POST', { decision, expected_revision: currentTopic.revision });
    }, enterStudio ? '创作已开始，系统自动整理资料；工作稿一生成就能编辑。' : '决定已保存。').finally(() => { creationPending.current = false; });
  };

  const active = topic?.jobs.some(j => ['queued', 'running'].includes(j.state));
  const researchOriginals = (research?.sources || []).filter(s => s.url && /^https?:\/\//i.test(s.url));
  return <section className="panel editor-panel"><div className="panel-title"><div><h2>{topic ? '选题详情' : '新建选题'}</h2><small>{topic ? `范围版本 R${topic.revision} · ${time(topic.updated_at)}` : '先明确讲什么，再确认如何生产'}</small></div>{topic && <Tag tone={stateTone(topic.status)}>{topicStates[topic.status]}</Tag>}</div>
    <Field label="选题标题"><input value={title} onChange={e => setTitle(e.target.value)} placeholder="例如：普通用户如何判断 AI 回答是否可信" maxLength={240} /></Field>
    <Field label="选题说明" hint="写清受众、核心问题、你的判断、想用的素材和不能公开的信息。缺少个人经历时采用客观表达。"><textarea rows={7} value={brief} onChange={e => setBrief(e.target.value)} placeholder="这条内容要帮谁解决什么问题？" /></Field>
    <EditorialControls value={{ content_line: contentLine, stance_ids: stanceIds }} disabled={!!busy} onChange={value => { setContentLine(value.content_line); setStanceIds(value.stance_ids); }} />
    {topic && requiredRisks.length > 0 && <div className="risk-confirmation">
      <strong>当前修订涉及{requiredRisks.map(risk => risk === 'finance' ? '金融' : '时事').join('、')}内容</strong>
      {riskConfirmed ? <p className="cm-muted">已对 R{topic.revision} 明确确认；修订后需重新确认。</p> : <label className="checkbox"><input type="checkbox" checked={riskAccepted} disabled={dirty || !!busy} onChange={event => setRiskAccepted(event.target.checked)} /><span>我已核对当前 R{topic.revision} 的材料与表达边界，确认继续。不会自动确认荐股、具体投资或虚拟货币内容。</span></label>}
      {dirty && <p className="cm-muted">先保存本次修改，再对新的修订号确认。</p>}
    </div>}
    <div className="field"><span>目标平台</span><PlatformChecks value={selected} change={setSelected} /></div>{selected.some(isVideoPlatform) && <Field label="视频目标时长（秒）" hint="创作目标，可设 10–600 秒；不是平台上传上限。"><input type="number" min={10} max={600} value={videoDuration} onChange={event => setVideoDuration(Number(event.target.value))} /></Field>}
    <FormatAdvisor topic={topic} busy={busy} run={run} />

    <div className="sources-header"><h3>参考来源</h3>
      <Btn tone="small" onClick={() => setSources([...sources, { url: '', title: '', note: '' }])}><Plus size={14} />添加来源</Btn>
    </div>
    <p className="muted-note">原文链接有助于准确创作；搜索页或热榜会由系统继续查找可用原文。缺少来源也可先开始整理。</p>

    {sources.map((s, i) => <div className="source-row" key={i}><input aria-label={`来源 ${i + 1} 地址`} placeholder="原文网址（不要用搜索页）" value={s.url} onChange={e => setSources(sources.map((x, n) => n === i ? { ...x, url: e.target.value } : x))} /><input aria-label={`来源 ${i + 1} 标题`} placeholder="标题" value={s.title} onChange={e => setSources(sources.map((x, n) => n === i ? { ...x, title: e.target.value } : x))} /><button className="icon-btn" aria-label={`删除来源 ${i + 1}`} disabled={!!busy} onClick={() => setSources(sources.filter((_, n) => n !== i))}><X size={15} /></button></div>)}
    {sources.length === 0 && <p className="muted-note">尚未登记来源。系统会自动整理已有说明并补查材料，资料不足时明确提供有限草稿或提纲。</p>}

    {topic && <div className="research-actions">
      <Btn tone="small" disabled={!!busy} onClick={() => void run('检索并读取原文', async () => { const data = await api<ResearchResult>(`/topics/${topic.id}/research`, 'POST', {}); setResearch(data); if (data.inspection && data.sources.length) setInspection(data.inspection); }, '原文读取完成，核对后加入来源。')}>
        <Search size={14} />检索并读取原文
      </Btn>
      {checking && <span className="cm-muted">来源体检中…</span>}
    </div>}

    {research && <div className="callout">
      <p>{research.message}</p>
      {researchOriginals.length > 0 && <div className="research-adopt">
        <p className="cm-muted">取回 {researchOriginals.length} 篇报道正文{research.source_images ? `，另取得 ${research.source_images} 张原文配图` : ''}：</p>
        {researchOriginals.map((source, i) => <div key={i} className="research-item">
          <a href={safeURL(source.url, '#')} target="_blank" rel="noreferrer">{source.title || source.url}<ExternalLink size={11} /></a>
          <small>{source.url.slice(0, 88)}</small>
        </div>)}
        <Btn tone="primary" onClick={adoptResearch}><Check size={14} />把 {researchOriginals.length} 条原文加入来源</Btn>
      </div>}
      {research.errors?.length > 0 && <details><summary>检索过程中的 {research.errors.length} 条提示</summary>{research.errors.map((error, i) => <p key={i} className="cm-muted">{error}</p>)}</details>}
    </div>}

    <SourceGate inspection={inspection} busy={busy} onResearch={() => {
      if (!topic) return;
      void run('检索并读取原文', async () => { const data = await api<ResearchResult>(`/topics/${topic.id}/research`, 'POST', {}); setResearch(data); if (data.inspection && data.sources.length) setInspection(data.inspection); }, '原文读取完成，核对后加入来源。');
    }} />

    {validation && <p className="form-error">{validation}</p>}
    <div className="editor-actions">
      <Btn tone="primary" disabled={!!busy} onClick={save}><Save size={15} />保存选题</Btn>
      {topic && <>
        <Btn disabled={!!busy || active || !canGenerate} onClick={() => decide('approved', true)}
          title={!contentLine ? '请先选择 A/B/C 内容线' : '批准当前范围，自动整理资料并生成可编辑工作稿'}>批准并创作</Btn>
        <Btn disabled={!!busy || dirty} onClick={() => decide('parked')}>暂存</Btn>
        <Btn disabled={!!busy || dirty} onClick={() => decide('rejected')}>拒绝</Btn>
      </>}
    </div>
  </section>;
}

function JobCard({ job, busy, run }: { job: Job; busy: string; run: Run }) {
  const resumable = ['failed', 'cancelled'].includes(job.state) || job.candidate_mode && job.state === 'succeeded';
  const label = job.candidate_mode && job.state === 'succeeded' ? '工作稿已保存' : jobStates[job.state];
  return <div className="job-card"><div className="card-top"><strong>{['queued', 'running'].includes(job.state) ? <LoaderCircle size={15} className="spin" /> : <CircleCheck size={15} />}{job.candidate_mode ? '工作稿创作' : '生成任务'}<Tag tone={stateTone(job.state)}>{label}</Tag></strong><small>{time(job.updated_at)}</small></div><p>{stageName(job.stage)}{job.provider ? ` · ${job.provider}` : ''}</p>{job.error && <div className="callout error">{job.error}</div>}{job.cancel_requested && ['running', 'queued'].includes(job.state) && <p className="muted-note">已请求取消，等待生成进程退出。</p>}{job.events?.length > 0 && <details><summary>任务日志（{job.events.length}）</summary><div className="job-events">{job.events.map((e, i) => <p key={i}><time>{time(e.time)}</time>{e.message}</p>)}</div></details>}<div className="action-bar">{['queued', 'running'].includes(job.state) && <Btn tone="small" disabled={!!busy || job.cancel_requested} onClick={() => void run('取消任务', () => api(`/jobs/${job.id}/cancel`, 'POST', {}), '取消请求已提交。')}><Square size={13} />取消生成</Btn>}{resumable && <Btn tone="small" disabled={!!busy} onClick={() => void run('重试生成', () => api(`/jobs/${job.id}/retry`, 'POST', {}), '新的生成任务已提交。')}><RefreshCw size={13} />继续未完成的生成</Btn>}</div></div>;
}

function StudioTools({ topic, version, busy, run, onVersion }: { topic: Topic; version: Version | null; busy: string; run: Run; onVersion: (id: string) => void }) {
  const [result, setResult] = useState<{ status: string; message?: string; path?: string } | null>(null);
  const historical = !!version && version.id !== topic.current_version?.id;
  return <div className="studio-advanced">
    {topic.jobs.slice(0, 3).map(job => <JobCard key={job.id} job={job} busy={busy} run={run} />)}
    {version && <>
      <LayoutAuditPanel version={version} busy={busy} run={run} />
      {version.payload.works.filter(work => !isVideoPlatform(work.platform)).map(work => <details key={work.platform}><summary>{names[work.platform]}素材管理</summary><AssetPanel manifest={version.image_assets?.[work.platform]} policy={version.source_policy?.[work.platform]} platform={work.platform} versionId={version.id} run={historical ? undefined : run} /></details>)}
      <section className="panel"><h3>Obsidian 工作稿</h3><p className="muted-note">同步到专属库的“工作台”子树；外部改稿按当前版本导入，有冲突时保留两份。</p><div className="actions">
        <Btn disabled={!!busy || historical} onClick={() => void run('同步工作稿', async () => { setResult(await api(`/topics/${topic.id}/sync`, 'POST', { version_id: version.id })); })}>同步到 Obsidian</Btn>
        <Btn disabled={!!busy || historical || topic.status !== 'approved'} onClick={() => { if (window.confirm('将库内改稿作为新版本导入，当前审核需要重新确认。继续？')) void run('导入库内改稿', async () => { const next = await api<Version>(`/topics/${topic.id}/import`, 'POST', { base_version_id: version.id }); onVersion(next.id); }); }}>导入库内改稿</Btn>
      </div>{result && <p role="status">{result.message}{result.path && <code>{result.path}</code>}</p>}</section>
    </>}
  </div>;
}

function Drafts({ drafts, topics, search, busy, run, flash, onEdit }: { drafts: Draft[]; topics: Topic[]; search: string; busy: string; run: Run; flash: (message: string, error?: boolean) => void; onEdit: (topicId: string, versionId: string, platform: Platform) => void }) {
  const [filter, setFilter] = useState('all');
  const [platformFilter, setPlatformFilter] = useState('all');
  const [draftId, setDraftId] = useState('');
  const filtered = drafts.filter(d => matchesDraftFilter(d, filter) && (platformFilter === 'all' || d.platform === platformFilter) && `${d.title} ${topics.find(t => t.id === d.topic_id)?.title ?? ''}`.toLowerCase().includes(search.toLowerCase()));
  // V2-28：左列按选题聚合，同一选题的多平台草稿只出一张卡，不再成列重复；
  // 平台维度交给筛选行的平台按钮。切平台时优先落回当前选题的对应草稿，
  // 避免选中项被筛掉后详情静默跳到第一条。
  const currentTopicId = drafts.find(d => d.id === draftId)?.topic_id;
  const draft = filtered.find(d => d.id === draftId) ?? filtered.find(d => d.topic_id === currentTopicId) ?? filtered[0] ?? null;
  const groups = new Map<string, Draft[]>();
  for (const d of filtered) { const list = groups.get(d.topic_id); if (list) list.push(d); else groups.set(d.topic_id, [d]); }
  const pick = (p: string) => {
    setPlatformFilter(p);
    if (p !== 'all' && draft) {
      const target = drafts.find(d => d.topic_id === draft.topic_id && d.platform === p);
      if (target) setDraftId(target.id);
    }
  };
  // 专用 Edge 会话丢失（浏览器被关/重启）时，保存动作不会执行，全部落到
  // needs_connection。恢复条把「打开浏览器登录 + 一键重试」摆到台面上，
  // 不用再去设置页翻入口，也不用逐条点重试。
  const unsaved = drafts.filter(d => d.state === 'needs_connection');
  const unsavedPlatforms = [...new Set(unsaved.map(d => d.platform))];
  const retryAll = async () => {
    const queued: string[] = [];
    const skipped: string[] = [];
    // 批量端点单次上限 50，超出就分批，不静默丢条
    for (let start = 0; start < unsaved.length; start += 50) {
      const batch = await api<{ queued: string[]; skipped: { draft_id: string; reason: string }[] }>('/drafts/queue', 'POST', { draft_ids: unsaved.slice(start, start + 50).map(d => d.id) });
      queued.push(...batch.queued);
      skipped.push(...batch.skipped.map(s => s.reason));
    }
    const skippedNote = skipped.length > 0 ? `；跳过 ${skipped.length} 条（${skipped[0]}）` : '';
    flash(`已排入 ${queued.length} 条重试${skippedNote}。保存结果以平台草稿箱实际回读为准。`);
    return { queued: queued.length, skipped: skipped.length };
  };
  return <><div className="alert draft-alert"><Inbox size={21} /><div><strong>这里追踪草稿的实际保存位置：</strong><p>「未存入平台」= 保存未完成，先看详情里的原因再重试；「已存专用浏览器草稿」= 仅存于工作台专用 Edge 网页编辑器，未确认平台云端同步。完成后由你人工审核发布。</p></div></div>
    {unsaved.length > 0 && <div className="callout warning draft-recovery">
      <div><strong>专用 Edge 会话未恢复，{unsaved.length} 条草稿未存入平台。</strong><p>工作台只附着已打开的专用 Edge，不会替你启动它；先打开后台完成登录（通常无需重新扫码），再一键重试。</p></div>
      <div className="action-bar">{unsavedPlatforms.map(p => <Btn key={p} tone="small" disabled={!!busy} onClick={() => void run(`打开${names[p]}后台`, () => api(`/integrations/browser/${p}/login`, 'POST', {}), `已在专用 Edge 打开${names[p]}后台，请完成登录后返回本页重试。`)}><ExternalLink size={14} />打开{names[p]}后台</Btn>)}<Btn tone="primary small" disabled={!!busy} onClick={() => void run('批量重试保存', retryAll, '重试已排入交付队列，请在平台草稿箱核对实际结果。')}><RefreshCw size={14} />全部重试保存（{unsaved.length}）</Btn></div>
    </div>}
    <div className="filter-bar"><div className="filter-tabs">{[{ id: 'all', label: '全部' }, { id: 'needs_connection', label: '未存入' }, { id: 'synced', label: '待审核' }, { id: 'blocked', label: '需修订' }, { id: 'published', label: '已发布' }].map(f => <button key={f.id} className={filter === f.id ? 'active' : ''} onClick={() => setFilter(f.id)}>{f.label}<span>{drafts.filter(d => matchesDraftFilter(d, f.id)).length}</span></button>)}</div><div className="platform-tabs" role="group" aria-label="按平台切换草稿">{[{ id: 'all', label: '全部平台' }, ...platforms.map(p => ({ id: p as string, label: names[p] }))].map(p => <button key={p.id} className={platformFilter === p.id ? 'active' : ''} onClick={() => pick(p.id)}>{p.id !== 'all' && <i className={`platform-dot ${p.id}`} />}{p.label}</button>)}</div></div><div className="split-layout draft-layout"><section className="panel draft-list">{filtered.length === 0 ? <Empty title={drafts.length === 0 ? '生成后，草稿会来到这里' : '没有匹配草稿'}>AI 生成成功后创建各平台的草稿交付记录，保留真实保存结果。</Empty> : [...groups.entries()].map(([tid, list]) => {
          const latest = list.reduce((a, b) => a.updated_at > b.updated_at ? a : b);
          const selected = !!draft && list.some(d => d.id === draft.id);
          const stuck = list.some(d => ['needs_connection', 'unknown'].includes(d.state));
          return <button className={`draft-card group-card ${selected ? 'selected' : ''}`} key={tid} onClick={() => { if (!selected) setDraftId(latest.id); }}><div className="card-top"><span className="platform-dots">{platforms.map(p => { const pd = list.find(d => d.platform === p); return <i key={p} className={`platform-dot ${p}${pd ? '' : ' off'}`} title={pd ? `${names[p]}：${draftStateLabel(pd)}` : `${names[p]}：暂无草稿`} />; })}</span>{list.length === 1 ? <Tag tone={draftStateTone(list[0])}>{draftStateLabel(list[0])}</Tag> : <Tag tone={stuck ? 'amber' : 'muted'}>{list.length} 份草稿</Tag>}</div><h3>{latest.title}</h3><small>{topics.find(t => t.id === tid)?.title ?? tid}</small><div className="draft-card-foot"><span>{list.length > 1 ? platforms.flatMap(p => { const n = list.filter(d => d.platform === p).length; return n > 1 ? [`${names[p]} ×${n}`] : n === 1 ? [names[p]] : []; }).join(' · ') : (latest.account_label || '账号待确认')}</span><time>{time(latest.updated_at)}</time></div></button>;
        })}</section>{draft ? <DraftDetail key={draft.id} draft={draft} topic={topics.find(t => t.id === draft.topic_id) ?? null} busy={busy} run={run} flash={flash} onEdit={onEdit} /> : <section className="panel"><Empty title="选择一份平台草稿">查看正文、保存状态和人工审核记录。</Empty></section>}</div></>;
}

function DraftDetail({ draft, topic, busy, run, flash, onEdit }: { draft: Draft; topic: Topic | null; busy: string; run: Run; flash: (message: string, error?: boolean) => void; onEdit: (topicId: string, versionId: string, platform: Platform) => void }) {
  const [work, setWork] = useState<Work | null>(null);
  const [contentError, setContentError] = useState('');
  const [account, setAccount] = useState(draft.account_label ?? '');
  const [disclosure, setDisclosure] = useState('');
  const [facts, setFacts] = useState(false);
  const [publishConfirm, setPublishConfirm] = useState(false);
  const [url, setUrl] = useState('');
  const [publishedAt, setPublishedAt] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => { let alive = true; api<{ work: Work }>(`/drafts/${draft.id}/content`).then(data => { if (alive) setWork(data.work); }).catch(error => { if (alive) setContentError(error.message); }).finally(() => { if (alive) setLoading(false); }); return () => { alive = false; }; }, [draft.id]);
  const current = draft.version_id === topic?.current_version?.id;
  const reviewPassed = topic?.current_version?.review?.status === 'passed';
  const version = topic?.versions.find(value => value.id === draft.version_id) ?? (topic?.current_version?.id === draft.version_id ? topic.current_version : null);
  const imageManifest = draft.images ?? version?.image_assets?.[draft.platform];
  const sourcePolicy = draft.source_policy ?? version?.source_policy?.[draft.platform];
  const imagesVerified = mediaVerified(draft);
  const genuinelySynced = draft.destination === 'platform' && !!draft.remote_id && ['synced', 'reviewed', 'published'].includes(draft.state);
  const browserSaved = draft.destination === 'browser' && !!draft.remote_id && ['browser_saved', 'reviewed', 'published'].includes(draft.state);
  const browserManaged = draft.platform === 'xiaohongshu' || draft.destination === 'browser' || draft.remote_id?.startsWith('browser:') === true;
  const publicLink = draft.state === 'published' && !!draft.remote_url && /^https?:\/\//i.test(draft.remote_url);
  const reviewable = current && ((draft.state === 'synced' && genuinelySynced) || (draft.state === 'browser_saved' && browserSaved));
  const copy = async (value: string) => { try { await navigator.clipboard.writeText(value); flash('已复制。平台草稿交付状态保持不变。'); } catch { flash('浏览器未允许复制，请在正文中选择文字后长按选中文本并复制。', true); } };
  const copyRich = async (html: string, text: string) => {
    // 富文本剪贴板：text/html 给平台编辑器渲染（含内嵌图片），text/plain 兜底。
    if (clipboardCtor && navigator.clipboard?.write) {
      try { await navigator.clipboard.write([new clipboardCtor({ 'text/html': html, 'text/plain': text })]); flash('已复制富文本正文（含图片）。粘贴到平台编辑器后请核对图片与排版。'); return; } catch { /* 降级为纯文本 */ }
    }
    try { await navigator.clipboard.writeText(text); flash('当前浏览器不支持富文本复制，已复制纯文本。', true); }
    catch { flash('浏览器未允许复制，请在正文中选择文字后长按选中文本并复制。', true); }
  };
  return <section className="panel draft-detail"><div className="panel-title"><div><h2>{names[draft.platform]}草稿</h2><small>{draft.account_label || '账号待确认'} · {time(draft.updated_at)}</small></div><Tag tone={draftStateTone(draft)}>{draftStateLabel(draft)}</Tag></div>
    <div className={`delivery-state ${(genuinelySynced || browserSaved) && imagesVerified ? 'delivered' : ''}`}><Inbox size={21} /><div><strong>{browserSaved ? imagesVerified ? '正文与图片已保存到专用 Edge 的本地网页草稿' : '专用 Edge 已保存正文，图片交付待核对' : genuinelySynced ? imagesVerified ? '正文与图片已保存到平台草稿箱' : '平台已保存正文，图片交付待核对' : draft.state === 'blocked' ? '版本未通过规则审核，草稿未送达平台' : draft.state === 'stale' ? '存在更新版本，本草稿已过期' : draft.state === 'needs_connection' ? '保存未完成，尚未送入平台' : '尚未确认草稿保存成功'}</strong><p>{browserSaved ? '这份草稿保存在工作台专用 Edge 中，尚未确认平台云端同步。请使用下方按钮在同一专用浏览器中审核，保留该浏览器的本地草稿与登录状态。' : genuinelySynced ? `平台草稿 ID：${draft.remote_id}` : draft.state === 'blocked' ? '修订正文并通过规则审核后，重新同步会自动准备图片并上传到平台草稿箱。当前图片尚未生成，平台后台不会出现这份草稿。' : draft.state === 'stale' ? '过期版本不会生成图片。请选择最新草稿继续操作；对新版本重新审核通过并同步后，才会出图并送平台。' : draft.state === 'needs_connection' ? '保存动作尚未执行完成，原因见下方提示；处理后在下方的操作栏重试保存。本机保留内容恢复快照，实际保存位置和结果以这里的状态为准。' : '本机保留内容恢复快照；实际草稿保存位置和结果以这里的状态为准。'}</p>{draft.error && <p className="form-error">{draft.error}</p>}</div></div>
    {draft.delivery && <div className="callout"><p>交付任务：{({ queued: '排队中', running: '执行中', succeeded: '已完成', waiting: '等待处理', failed: '失败', cancelled: '已停止' } as Record<string, string>)[draft.delivery.state] || draft.delivery.state} · {draft.delivery.message || draft.delivery.stage}</p>{draft.delivery.error && <p>{draft.delivery.error}</p>}{['queued', 'running'].includes(draft.delivery.state) && <Btn disabled={!!busy || !!draft.delivery.cancel_requested || draft.delivery.stage === 'platform'} onClick={() => void run('停止交付', () => api(`/drafts/${draft.id}/cancel-delivery`, 'POST', {}), '已请求停止，已有正文和图片保留。')}>停止交付</Btn>}</div>}
    <div className="action-bar">{publicLink ? <a className="btn" href={safeURL(draft.remote_url, backends[draft.platform] || '#')} target="_blank" rel="noreferrer"><ExternalLink size={15} />查看已发布内容</a> : browserManaged ? <Btn disabled={!!busy || draft.state === 'syncing' || !draft.remote_id} title={!draft.remote_id ? '尚未取得已保存草稿标识，请先核对保存记录' : undefined} onClick={() => void run('在专用浏览器打开草稿', () => api(`/drafts/${draft.id}/open`, 'POST', {}), '已请求在工作台专用 Edge 打开对应草稿，请在那里核对与审核。')}><ExternalLink size={15} />在专用浏览器审核草稿</Btn> : <a className="btn" href={safeURL(draft.remote_url, backends[draft.platform] || '#')} target="_blank" rel="noreferrer"><ExternalLink size={15} />打开平台后台</a>}{['local', 'needs_connection', 'failed', 'unknown'].includes(draft.state) && current && <Btn tone="primary" disabled={!!busy} onClick={() => void run(draft.state === 'unknown' ? '核对保存记录' : '尝试保存草稿', () => api(`/drafts/${draft.id}/queue`, 'POST', {}), '草稿已排入交付队列，可查看进度；不确定的保存只核对已有记录。')}><RefreshCw size={15} />{draft.state === 'unknown' ? '核对保存记录' : '重试保存草稿'}</Btn>}{draft.state === 'syncing' && <Btn disabled><LoaderCircle className="spin" size={15} />存入中，请等待</Btn>}<Btn onClick={() => onEdit(draft.topic_id, draft.version_id, draft.platform)}><Pencil size={15} />{current ? '编辑内容' : '查看历史版本'}</Btn></div>
    {draft.state === 'unknown' && <div className="callout warning">草稿保存结果尚未确定。{draft.remote_id ? '使用已获得的草稿标识回读核对，避免重复创建。' : '先核对后台，工作台会读取已有保存记录；无已知 ID 时不会再创建。'}</div>}
    {!imagesVerified && current && ['synced', 'browser_saved'].includes(draft.state) && <div className="action-bar"><Btn disabled={!!busy} onClick={() => void run('核对图片交付', () => api(`/drafts/${draft.id}/queue`, 'POST', {}), '已读取图片交付结果，请查看实际回读状态。')}><RefreshCw size={14} />核对图片交付</Btn></div>}
    {loading ? <p className="loading-line"><LoaderCircle size={15} className="spin" />正在读取草稿正文…</p> : contentError ? <div className="callout error">{contentError}</div> : work && <><div className="copy-heading"><h3>{work.title}</h3><Btn tone="small" onClick={() => void copy(work.title)}><Copy size={13} />复制标题</Btn></div><div className="draft-body">{work.body}</div>{work.tags?.length > 0 && <div className="platform-chips">{work.tags.map((tag, i) => <span key={i}>{tag}</span>)}</div>}{work.export_warnings && work.export_warnings.length > 0 && <div className="callout warning">{work.export_warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div>}{work.html ? <div className="action-bar"><Btn tone="small" onClick={() => void copyRich(work.html!, work.text || work.body)}><Copy size={13} />复制正文（富文本）</Btn><Btn tone="small" onClick={() => void copy(work.text || work.body)}><Copy size={13} />复制纯文本</Btn><small className="muted-note">富文本含图片，粘贴到公众号/头条编辑器后请核对图片顺序；粘贴异常时改用纯文本 + 单图「复制图片」。</small></div> : <div className="action-bar"><Btn tone="small" onClick={() => void copy(work.text || work.body)}><Copy size={13} />复制正文</Btn><small className="muted-note">复制仅辅助手动编辑，不代表平台已接收。</small></div>}<AssetPanel manifest={imageManifest} policy={sourcePolicy} media={draft.media} platform={draft.platform} versionId={draft.version_id} run={run} flash={flash} />{work.image_prompts?.length > 0 && <details><summary>图片生成说明与提示词</summary>{work.image_prompts.map((prompt, i) => <pre className="prompt" key={i}>{prompt}</pre>)}</details>}</>}
    {reviewable && <div className="manual-review"><h3>人工审核</h3><p>{browserSaved ? '在专用 Edge 中核实内容、来源、排版、实际图片授权与 AI 标识。记录审核不代表草稿已同步到云端或完成发布。' : '在平台后台核实内容与引用，检查实际封面、配图、素材授权及 AI 标识。'}</p>{!imagesVerified && <div className="callout warning">正文保存已确认，图片与封面仍需回读核对；完整交付确认后再记录审核。</div>}{!reviewPassed && <div className="callout warning">当前内容规则检查尚未通过。请回内容项目运行检查或修订。</div>}<Field label="审核账号"><input value={account} onChange={e => setAccount(e.target.value)} placeholder="填写实际平台账号名称" /></Field><Field label="AI 内容标识说明"><textarea rows={2} value={disclosure} onChange={e => setDisclosure(e.target.value)} placeholder="记录将如何按平台要求标识 AI 辅助内容" /></Field><label className="check-line"><input type="checkbox" checked={facts} onChange={e => setFacts(e.target.checked)} /><span>我已人工核实事实、引用、素材授权与账号，并审阅{browserSaved ? '专用浏览器中的网页' : '平台'}草稿。</span></label><Btn tone="primary" disabled={!!busy || !facts || !account.trim() || !disclosure.trim() || !reviewPassed || !imagesVerified} onClick={() => void run('记录人工审核', () => api(`/drafts/${draft.id}/review`, 'POST', { facts_confirmed: true, account_label: account.trim(), ai_disclosure: disclosure.trim() }), browserSaved ? '本地网页草稿的人工审核已记录。请在专用 Edge 中人工发布。' : '人工审核已记录。请在平台后台完成发布。')}><ShieldCheck size={15} />确认已审核</Btn></div>}
    {draft.state === 'reviewed' && current && <div className="manual-review"><h3>发布后回填</h3><p>完成平台手动发布后，填入真实公开链接和实际时间。工作台不会代你发布。</p><Field label="实际发布链接"><input type="url" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://…" /></Field><Field label="实际发布时间" hint="输入北京时间；保存时转换为 UTC。"><input type="datetime-local" value={publishedAt} onChange={e => setPublishedAt(e.target.value)} /></Field><label className="check-line"><input type="checkbox" checked={publishConfirm} onChange={e => setPublishConfirm(e.target.checked)} /><span>我已在平台完成发布，以上为实际记录。</span></label><Btn tone="primary" disabled={!!busy || !publishConfirm || !/^https?:\/\//i.test(url.trim()) || !publishedAt} onClick={() => void run('回填实际发布记录', () => api(`/drafts/${draft.id}/record`, 'POST', { url: url.trim(), published_at: new Date(`${publishedAt}:00+08:00`).toISOString(), confirmation: true }), '已记录你确认的真实发布。')}><CheckCheck size={15} />记录已发布</Btn></div>}
    {!current && <div className="callout warning">这份草稿对应旧内容版本。请选择最新草稿继续审核与发布。</div>}
  </section>;
}

interface Integration { configured?: boolean; capability_verified?: boolean; app_id_masked?: string; thumb_media_id?: string; error?: string; message?: string }
function BrowserConnection({ platform, status, available, loading, busy, action, platformClosed }: { platform: Platform; status: BrowserStatus | null; available: boolean; loading: boolean; busy: string; action: (platform: Platform, operation: 'login' | 'check') => void; platformClosed?: boolean }) {
  const labels: Record<BrowserStatus['state'], string> = { disconnected: '未连接', login_required: '等待你登录', connected: '登录已确认', needs_verification: '需要你完成验证', error: '连接异常', disabled: '服务未启用' };
  const disabled = !available || loading || !!busy || status?.state === 'disabled';
  const verified = browserSaveVerified(status);
  return <div className="browser-connection"><div className="browser-connection-title"><strong>官方网页草稿通道</strong><Tag tone={status?.state === 'connected' ? 'green' : ['login_required', 'needs_verification', 'error'].includes(status?.state ?? '') ? 'amber' : 'muted'}>{loading && !status ? '正在检查连接' : status ? labels[status.state] : '等待浏览器服务'}</Tag></div>
    <p>打开工作台专用的 Edge 官方后台页面，由你扫码或填写密码。工作台不读取密码。</p>
    {status && <div className="browser-account"><span>账号：{status.account_label || '尚未识别'}</span><span>{status.browser_open ? '独立窗口已打开' : '独立窗口未打开'}</span></div>}
    {status?.message && !verified && <p className={`browser-message ${status.state === 'error' ? 'form-error' : ''}`}>{status.message}</p>}
    <div className="action-bar">{platformClosed ? <p className="muted-note">本地内容模式：平台浏览器操作已关闭，工作台不打开后台、不检查登录。发布请在官方应用人工完成。</p> : <><Btn tone="small" disabled={disabled} onClick={() => action(platform, 'login')}><ExternalLink size={14} />{status?.state === 'connected' ? '打开后台页面' : '打开登录页面'}</Btn><Btn tone="small" disabled={disabled} onClick={() => action(platform, 'check')}><RefreshCw size={14} />检查登录</Btn></>}</div>
    <small className="browser-capability">{verified ? platform === 'xiaohongshu' ? '本地网页草稿保存已验证：仅保存在工作台专用 Edge 中，尚未确认平台云端同步。每份草稿仍需独立保存与审核。' : '网页草稿保存已验证：已通过实际保存与回读。每份草稿的交付状态仍以对应记录为准。' : status?.state === 'connected' ? '登录已确认，实际草稿与图片保存结果请到平台草稿箱查看。' : '在专用 Edge 登录后，由实际内容保存与回读验证网页通道；官方 API 权限单独核验。'}</small>{status?.last_checked && <small className="browser-last-checked">最近检查：{time(status.last_checked)}</small>}
  </div>;
}
function SettingsPage({ settings, health, setSettings, setHealth, busy, run, desktopAlerts, toggleDesktopAlerts, mode }: { settings: Settings; health: Health | null; setSettings: (v: Settings) => void; setHealth: (v: Health) => void; busy: string; run: Run; desktopAlerts: boolean; toggleDesktopAlerts: () => Promise<void>; mode: { mode: string; mode_label: string; platform_operations: boolean; note: string } | null }) {
  const platformClosed = !!(mode && !mode.platform_operations);
  const [form, setForm] = useState({ codex_path: settings.codex_path ?? '', local_base_url: settings.local_base_url ?? 'http://127.0.0.1:1234', local_model: settings.local_model ?? '', task_timeout_seconds: settings.task_timeout_seconds ?? 600, vault_path: settings.vault_path ?? '' });
  const [appId, setAppId] = useState('');
  const [secret, setSecret] = useState('');
  const [thumb, setThumb] = useState('');
  const [backupPath, setBackupPath] = useState('');
  const [browserBridge, setBrowserBridge] = useState<BrowserBridge | null>(null);
  const [browserLoading, setBrowserLoading] = useState(true);
  const [browserError, setBrowserError] = useState('');
  const refreshBrowser = useCallback(async () => { const next = await api<BrowserBridge>('/integrations/browser/status'); setBrowserBridge(next); setBrowserError(next.error ?? ''); setBrowserLoading(false); return next; }, []);
  useEffect(() => {
    let active = true;
    let checking = false;
    const update = async () => {
      if (checking) return;
      checking = true;
      try { const next = await api<BrowserBridge>('/integrations/browser/status'); if (active) { setBrowserBridge(next); setBrowserError(next.error ?? ''); } }
      catch (error) { if (active) { setBrowserError(error instanceof Error ? error.message : '浏览器连接服务暂不可用'); setBrowserBridge(null); } }
      finally { checking = false; if (active) setBrowserLoading(false); }
    };
    void update();
    const id = window.setInterval(() => void update(), 6000);
    return () => { active = false; window.clearInterval(id); };
  }, []);
  const browserAction = (platform: Platform, operation: 'login' | 'check') => {
    void run(operation === 'login' ? `打开${names[platform]}独立登录页` : `检查${names[platform]}登录`, async () => {
      try { await api<BrowserStatus>(`/integrations/browser/${platform}/${operation}`, 'POST', {}); }
      finally { await refreshBrowser(); }
    }, operation === 'login' ? '已请求打开独立官方页面，请在该窗口完成登录与验证。' : '登录检查已完成，请查看该平台的真实连接状态。');
  };
  const integrations = (health as unknown as { integrations?: Record<string, Integration> })?.integrations ?? (settings.integrations as Record<string, Integration> | undefined) ?? {};
  const wx = integrations.wechat ?? {};
  useEffect(() => { setThumb(wx.thumb_media_id ?? ''); }, [wx.thumb_media_id]);
  const check = async () => { setHealth(await api<Health>('/health')); setSettings(await api<Settings>('/settings')); };
  return <div className="settings-layout"><RuntimeStatus settings={settings} onSettings={setSettings} modelAvailable={health?.local.available} /><DeviceSessions />{mode && !mode.platform_operations && <div className="callout warning" style={{ gridColumn: '1 / -1' }}><strong>本地内容模式 · 平台自动操作已关闭</strong><p>{mode.note}</p></div>}<PlatformCapabilities refreshKey={JSON.stringify([browserBridge?.platforms, wx.configured, wx.capability_verified])} /><PlatformAccessSettings /><section className="panel"><div className="panel-title"><div><h2>生成与资料库</h2><small>正文与脚本仅走本地模型</small></div><Btn tone="small" disabled={!!busy} onClick={() => void run('检查服务', check, '服务状态已刷新。')}><RefreshCw size={14} />检查连接</Btn></div><div className="health-grid"><div><span>本地模型</span><Tag tone={health?.local.available ? 'green' : 'muted'}>{health?.local.available ? '已连接' : '未连接'}</Tag><small>{health?.local.models?.join('、') || '未发现可用模型'}</small>{health?.local.error && <p>{health.local.error}</p>}</div></div><p className="muted-note">历史交接记录保留只读溯源。模型未就绪时会显示实际状态。</p><div className="two-fields"><Field label="LM Studio / 本地模型地址"><input value={form.local_base_url} onChange={e => setForm({ ...form, local_base_url: e.target.value })} placeholder="http://127.0.0.1:1234" /></Field><Field label="当前加载模型" hint="正文与配图策划统一跟随 LM Studio 当前加载模型，请在 LM Studio 切换。"><input readOnly list="local-models" value={health?.local.loaded_model || '未加载模型'} placeholder="使用本地服务实际模型 ID" /><datalist id="local-models">{health?.local.models?.map(m => <option value={m} key={m} />)}</datalist></Field></div><Field label="每个平台生成超时（秒）" hint="各选定平台分别计时；已完成稿件保留，可重试继续。"><input type="number" min={30} max={1800} step={30} value={form.task_timeout_seconds} onChange={e => setForm({ ...form, task_timeout_seconds: Number(e.target.value) })} /></Field><Field label="Obsidian 专属库路径" hint="同步仅写入“工作台”子树，不搬移原有笔记与模板。"><input value={form.vault_path} onChange={e => setForm({ ...form, vault_path: e.target.value })} /></Field><Btn tone="primary" disabled={!!busy || !form.vault_path.trim() || form.task_timeout_seconds < 30 || form.task_timeout_seconds > 1800} onClick={() => void run('保存设置', async () => { setSettings(await api<Settings>('/settings', 'POST', form)); setHealth(await api<Health>('/health')); }, '设置已保存。')}><Save size={15} />保存设置</Btn><div className="subtle-note">云端 API 本版禁用，不产生云端 API 费用。</div></section>
    <div className="settings-secondary"><section className="panel"><div className="panel-title"><div><h2>平台连接与草稿接入</h2><small>本地内容模式：平台自动操作已关闭；以下为历史接入状态，仅作记录</small></div></div><div className="connection-explainer"><strong>本地内容模式</strong><p>工作台在本地内容模式下不打开平台后台、不保存平台草稿、不检查登录、不调用平台接口。发布与互动请在官方应用人工完成；历史接入状态保留供核对，不会自动恢复。</p></div>{browserError && <div className="callout warning">浏览器连接服务：{browserError}</div>}<div className="integration-heading"><span className="platform-dot wechat" /><strong>微信公众号</strong><Tag tone={wx.configured ? 'amber' : 'muted'}>{wx.capability_verified ? 'API 读取已验证 · 写入待验证' : wx.configured ? 'API 已配置 · 待核验' : 'API 未接入'}</Tag></div><BrowserConnection platform="wechat" status={browserBridge?.platforms.wechat ?? null} available={browserBridge?.available ?? false} loading={browserLoading} busy={busy} action={browserAction} platformClosed={platformClosed} /><details className="api-connection-details"><summary>公众号官方 API 接入 · AppID 与草稿权限</summary>{wx.app_id_masked && <p className="muted-note">已保存 AppID：{wx.app_id_masked}</p>}<p className="muted-note">使用有权限的公众号官方草稿接口，需要该账号已上传的封面素材 ID。权限核验和每次同步结果以平台返回为准。</p><Field label="AppID"><input autoComplete="off" value={appId} onChange={e => setAppId(e.target.value)} placeholder="填写此公众号 AppID" /></Field><Field label="AppSecret" hint="仅用于本机平台接入，保存后清空，不会回显。"><input type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} placeholder="填写后保存，不从服务读取" /></Field><Field label="已上传封面 thumb_media_id"><input autoComplete="off" value={thumb} onChange={e => setThumb(e.target.value)} placeholder="此账号的有效永久封面素材 ID" /></Field><div className="action-bar"><Btn tone="primary small" disabled={!!busy || !appId.trim() || !secret.trim() || !thumb.trim()} onClick={() => void run('保存公众号接入', async () => { await api('/integrations/wechat', 'POST', { app_id: appId.trim(), app_secret: secret, thumb_media_id: thumb.trim() }); setSecret(''); setAppId(''); await check(); }, '公众号配置已保存，密钥已从表单清空。请核验草稿能力。')}><Save size={14} />保存接入</Btn><Btn tone="small" disabled={!!busy || !wx.configured || platformClosed} onClick={() => void run('核验公众号草稿权限', async () => { try { await api('/integrations/wechat/check', 'POST', {}); } finally { await check(); } }, '已读取平台权限核验结果。')}><ShieldCheck size={14} />核验权限</Btn></div>{(wx.error || wx.message) && <div className="callout warning">{wx.error || wx.message}</div>}</details>
    {(['toutiao', 'xiaohongshu'] as Platform[]).map(p => <div className="unconnected-platform" key={p}><div><span className={`platform-dot ${p}`} /><strong>{names[p]}</strong><Tag tone={browserSaveVerified(browserBridge?.platforms[p]) ? "green" : "amber"}>{browserSaveVerified(browserBridge?.platforms[p]) ? p === "xiaohongshu" ? "本地网页保存已验证" : "网页草稿保存已验证" : "待草稿回读验证"}</Tag></div><p>{p === "xiaohongshu" ? "使用专用 Edge 的官方网页编辑器保留本地草稿，并从同一浏览器打开审核。当前不把本地草稿标为平台云端已同步。" : "通过专用 Edge 的官方网页保存平台草稿，并以保存后的真实草稿标识和正文回读确认结果。"}</p><BrowserConnection platform={p} status={browserBridge?.platforms[p] ?? null} available={browserBridge?.available ?? false} loading={browserLoading} busy={busy} action={browserAction} platformClosed={platformClosed} /></div>)}</section><section className="panel"><div className="panel-title"><h2>备份与运行边界</h2></div><p className="muted-note">备份内容记录、版本和同步资料。依赖、模型与运行数据库不放入 Obsidian 库。</p><Btn disabled={!!busy} onClick={() => void run('创建本机备份', async () => { const data = await api<{ path: string }>('/backup', 'POST', {}); setBackupPath(data.path); }, '本机备份已创建。')}><Save size={15} />创建备份</Btn>{backupPath && <div className="callout success"><code>{backupPath}</code></div>}<div className="notification-setting"><h3>可选桌面提醒</h3><p className="muted-note">仅保持此页面打开时有效。刷新不重复提醒旧通知；应用内未读提醒始终可用。</p><Btn onClick={() => void toggleDesktopAlerts()}><Bell size={15} />{desktopAlerts ? '关闭桌面提醒' : '开启桌面提醒'}</Btn></div><p className="subtle-note">服务检查：{time(health?.checked_at)}</p></section></div>
  </div>;
}
