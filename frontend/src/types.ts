export type ContentLine = 'A' | 'B' | 'C';
export interface RiskConfirmation { revision: number; risks: ('finance' | 'current_affairs')[]; confirmed_at: string }
export interface StyleStance { id: string; statement: string; reason: string; confirmed: boolean }
export interface StyleLibrary { version: string; rules_version: string; content_lines: Record<ContentLine, { name: string; editorial_share: number; prompt: string }>; stances: StyleStance[] }
export type Platform = 'wechat' | 'toutiao' | 'xiaohongshu' | 'weixin_channels' | 'kuaishou' | 'douyin';
export interface VideoShot { id: string; order: number; role: 'opening' | 'body' | 'closing'; spoken_text: string; visual_type: 'talking_head' | 'screen_recording' | 'material' | 'mixed'; visual_notes: string; subtitle_points: string[]; source_refs: string[]; duration_estimate_seconds?: number }
export interface Source { url: string; title: string; note: string; platform?: TrendSourcePlatform | 'unknown' }
export interface ImageTask { prompt: string; page_role: 'cover' | 'body' | 'card' | 'concept' | 'diagram'; aspect?: '2.35:1' | '16:9' | '3:4' | '9:16' | '1:1' | null; visual_type?: string; style_id?: string }
export interface VisualPlan { created_at: string; platform: Platform; tasks: ImageTask[]; source: 'local_model' | 'heuristic'; model: string | null; warnings: string[] }
export interface EmbeddedImage { id: string; caption: string; data_uri: string }
export interface Work { platform: Platform; format: string; content_type?: string; script_schema_version?: string; title: string; body: string; tags: string[]; image_prompts: string[]; image_tasks?: ImageTask[]; source_urls: string[]; html?: string | null; text?: string; images_embedded?: EmbeddedImage[]; export_warnings?: string[]; cover_text?: string; shots?: VideoShot[]; target_duration_seconds?: number; estimated_duration_seconds?: number; opening_hook?: string; closing_conclusion?: string; duration_warning?: string }
export interface Review { status: 'passed' | 'blocked'; findings: { severity: 'blocker' | 'warning'; message: string; platform?: string }[]; created_at: string; version_id: string }
export interface AssetSource { source_url?: string; source_title?: string; publisher?: string; rights_holder?: string; caption?: string; kind?: string; license?: string; authorization_id?: string; rights_confirmed?: boolean; url?: string; title?: string; source_name?: string; [key: string]: unknown }
export interface ImageAsset { id: string; url?: string; filename?: string; sha256?: string; mime_type?: string; bytes?: number; width?: number; height?: number; role?: string; prompt?: string; provider?: string; is_ai_generated?: boolean; depicts_verified_event?: boolean; caption?: string; license?: string; license_url?: string; rights_holder?: string; rights_status?: string; review_required?: boolean; credit?: string; credit_required?: boolean; source?: AssetSource; prompt_index?: number }
export interface MaterialCandidate { id: string; title?: string; description_url?: string; license?: string; artist?: string; date?: string; eligible?: boolean; asset_id?: string; review_reasons?: string[]; download_error?: string; relevance?: { status?: string; basis?: string } }
export interface ImageAssetManifest { status: 'not_started' | 'ready' | 'generating' | 'failed' | 'unknown' | 'cancelled' | 'blocked' | 'corrupt' | 'needs_review'; assets: ImageAsset[]; candidates?: Partial<Record<string, ImageAsset[]>>; selected?: Record<string, string>; error?: string | null; warnings?: string[]; provider?: string; requested_count?: number; produced_count?: number; remaining_count?: number; unprocessed_prompt_count?: number; material_search?: { checked_at?: string; candidates?: MaterialCandidate[]; error?: string | null } }
export interface SourcePolicy { requires_review: boolean; message?: string; review_items?: unknown[]; reasons?: unknown[]; findings?: unknown[]; issues?: unknown[]; [key: string]: unknown }
export interface DraftMedia { status: string; expected_count: number; verified_count: number; cover_verified: boolean; error?: string | null }
export interface Version { id: string; topic_id: string; number: number; payload: { strategy: string; works: Work[] }; hash: string; created_at: string; provider: string; review: Review | null; sync: Record<string, unknown> | null; image_assets?: Partial<Record<Platform, ImageAssetManifest>>; source_policy?: Partial<Record<Platform, SourcePolicy>> }
export interface Job { candidate_mode?: boolean; id: string; topic_id: string; kind: string; state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; stage: string; provider: string; error: string | null; created_at: string; updated_at: string; cancel_requested: boolean; events: { time: string; message: string }[] }
export interface Topic { id: string; title: string; brief: string; platforms: Platform[]; video_target_duration_seconds?: number; sources: Source[]; revision: number; status: 'pending' | 'approved' | 'rejected' | 'parked'; content_line: ContentLine | null; stance_ids: string[]; risk_confirmation: RiskConfirmation | null; required_risk_confirmations: ('finance' | 'current_affairs')[]; created_at: string; updated_at: string; current_version: Version | null; versions: Version[]; jobs: Job[] }
export interface DeliveryTask { state: string; stage: string; message: string; error?: string | null; cancel_requested: number; attempts: number }
export interface Draft { delivery?: DeliveryTask | null; id: string; topic_id: string; version_id: string; platform: Platform; title: string; account_label: string; state: 'local' | 'needs_connection' | 'syncing' | 'unknown' | 'synced' | 'browser_saved' | 'reviewed' | 'published' | 'stale' | 'blocked' | 'failed'; destination: 'workbench' | 'platform' | 'browser'; remote_id: string | null; remote_url: string | null; error: string | null; created_at: string; updated_at: string; media?: DraftMedia; images?: ImageAssetManifest; source_policy?: SourcePolicy }
export interface Notice { id: string; topic_id: string | null; title: string; body: string; read: boolean; created_at: string }
export interface Settings { codex_path: string; local_base_url: string; local_model: string; task_timeout_seconds: number; vault_path: string; backup_copy_dir?: string; draft_destination?: string; [key: string]: unknown }
export interface Health { codex: { available: boolean; authenticated: boolean; auth_mode?: string; version?: string; path?: string; error?: string | null; inference_verified?: boolean }; workbuddy?: { available: boolean; mode: 'manual_handoff'; automatic_api: boolean; note?: string }; local: { available: boolean; models: string[]; loaded_model?: string; error?: string | null }; comfy?: { available: boolean; version?: string; error?: string | null }; cloud: { enabled: false }; checked_at: string; platforms?: Record<string, { configured?: boolean; verified?: boolean; available?: boolean; state?: string; message?: string }> }
export interface State { topics: Topic[]; jobs: Job[]; publications: unknown[]; drafts: Draft[]; notifications: Notice[]; activity: unknown[]; overview: Record<string, unknown> }
export interface Bootstrap { token: string; settings: Settings; overview: Record<string, unknown>; health: Health }
export interface BrowserStatus { platform: Platform; state: 'disconnected' | 'login_required' | 'connected' | 'needs_verification' | 'error' | 'disabled'; account_label: string | null; login_url: string; message: string; browser_open: boolean; last_checked: string | null; draft_capability_verified: boolean; verified_draft_capability?: boolean }
export interface BrowserBridge { platforms: Record<Platform, BrowserStatus>; available: boolean; error?: string }
export type TrendClassification = 'hot' | 'rising' | 'watch' | 'context';
export type TrendSourcePlatform = 'external' | Platform | 'xhs';
export interface TrendSettings { enabled: boolean; interval_minutes: number; scope: 'broad' | 'ai'; keywords: string[]; enabled_sources: string[]; window_days: number }
export interface TrendRefresh { state: 'idle' | 'running' | 'succeeded' | 'partial' | 'failed'; run_id: string | null; last_started_at: string | null; last_finished_at: string | null; next_refresh_at: string | null; error: string | null; source_counts?: { ok: number; error: number; not_due: number; pending: number; attempted: number } }
export interface TrendSource { id: string; name: string; url: string; kind: 'ranking' | 'rss' | 'trendradar' | 'json' | string; category: string; platform?: TrendSourcePlatform; status: 'pending' | 'ok' | 'error' | 'not_due'; last_fetched_at: string | null; item_count: number; error: string | null; tier: 'official' | 'interpretation' | 'signal'; min_interval_minutes: number; next_at?: string | null; health?: { consecutive_failures: number; last_success_at: string | null; last_error: string | null; error_category?: string | null; degraded: boolean }; last_run?: { status: string | null; item_count: number; kept: number; duplicates: number; dropped: number; error: string | null; error_category: string | null; finished_at: string | null } }
export interface TrendEvidence { source_id: string; source_name: string; platform?: TrendSourcePlatform; url: string; rank: number | null; heat_value: number | null; heat_label: string | null; fetched_at: string }
export type TrendDirection = 'ai' | 'tech' | 'society' | 'life';
export interface TrendCandidate { id: string; title: string; url: string; summary: string; published_at: string | null; first_seen_at: string; last_seen_at: string; status: 'new' | 'saved' | 'ignored'; topic_id: string | null; classification: TrendClassification; priority: number; priority_label: string; direction: TrendDirection; source_tags: TrendDirection[]; article_tags: TrendDirection[]; score: { total: number; dimensions?: Record<string, unknown>; content_lines?: { line: string; name: string; score: number }[]; publishable?: boolean; publishable_reason?: string; [key: string]: unknown }; reasons: string[]; uncertainties: string[]; suggested_title: string; angle: string; brief: string; recommended_platforms: Platform[]; signal_evidence: Record<string, unknown>; sources: TrendEvidence[] }
export interface PlatformTrendSource { platform: Platform | 'xhs'; available: boolean; source_ids: string[]; reason: string; checked_at: string | null; reference_url?: string }
export interface TrendRadarState { settings: TrendSettings; refresh: TrendRefresh; sources: TrendSource[]; candidates: TrendCandidate[]; platform_sources?: PlatformTrendSource[]; tier_labels?: Record<string, string>; tier_notes?: Record<string, string>; candidate_total?: number; candidate_shown?: number; next_cursor?: string | null; funnel?: { run_id: string; collected: number; kept: number; duplicates: number; dropped: number; sources_attempted: number; sources_ok: number; sources_error: number; per_source: Record<string, { status: string | null; item_count: number; kept: number; duplicates: number; dropped: number; error: string | null; error_category: string | null; finished_at: string | null }> } | null }
export interface ProductionSlotRow { id: string; topic_id: string; topic_revision: number; batch_date: string; expires_at: string | null; state: 'scheduled' | 'queued' | 'produced' | 'skipped' | 'expired' | 'missed' | 'cancelled'; note: string; created_at: string; updated_at: string }
export interface ProductionRunRow { batch_date: string; state: 'running' | 'succeeded' | 'failed' | 'stopped'; started_at: string; finished_at: string | null }
export interface ProductionStatus {
  slots: Record<string, number>; items: Record<string, number>; recent_runs: ProductionRunRow[];
  today: { batch_date: string; quota_used: number; quota_limit: number; quota_remaining: number };
  limits: { max_topics_per_day: number; stagger_minutes: number; stagger_enforced: boolean; timezone: string };
  note: string;
}
export interface SchedulerStatus { running: boolean; tick_seconds: number; production_hour_local: number; timezone: string; note: string }
export interface ProductionOverview { production: ProductionStatus; scheduler: SchedulerStatus; slots: ProductionSlotRow[] }

// ---- V2-18 运营总览 ----
export interface DashboardStats {
  today_trends: number; pending_topics: number; producing: number; awaiting_review: number;
  platform_drafts: number; published: number; failed_jobs: number; trash: number; unread: number;
}
export interface DashboardPlatform {
  platform: Platform; label: string; channel: string; auth_state: string;
  checked_at: string | null; implemented: boolean; drafts: number; note?: string;
}
export interface DashboardPayload {
  stats: DashboardStats;
  activity: { id: string; topic_id: string | null; message: string; created_at: string }[];
  platforms: DashboardPlatform[];
  alerts: { level: string; message: string }[];
  production: ProductionStatus;
  scheduler: SchedulerStatus;
  last_production_run: ProductionRunRow | null;
  capability_attention: { available: boolean; unknown_count: number | null; waiting_count: number | null };
  notice: string;
}
