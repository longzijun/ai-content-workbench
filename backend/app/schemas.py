"""Request validation for the local workbench API."""
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .platform_registry import VIDEO_SCRIPT_SCHEMA_VERSION

Platform = Literal["wechat", "toutiao", "xiaohongshu", "weixin_channels", "kuaishou", "douyin"]
ContentLine = Literal["A", "B", "C"]
VideoShotRole = Literal["opening", "body", "closing"]
VideoVisualType = Literal["talking_head", "screen_recording", "material", "mixed"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(RequestModel):
    url: str = Field(max_length=4000)
    title: str = Field(default="", max_length=300)
    note: str = Field(default="", max_length=6000)
    platform: Literal['wechat', 'toutiao', 'xiaohongshu', 'xhs', 'external', 'unknown'] = 'unknown'

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("来源须为 http/https 链接")
        return value


class TopicCreate(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    brief: str = Field(default="", max_length=30000)
    platforms: list[Platform] = Field(min_length=1, max_length=6)
    sources: list[Source] = Field(default_factory=list, max_length=100)
    content_line: ContentLine | None = None
    stance_ids: list[str] = Field(default_factory=list, max_length=19)
    video_target_duration_seconds: int = Field(default=60, ge=10, le=600)

    @field_validator("stance_ids")
    @classmethod
    def unique_stances(cls, value):
        if len(set(value)) != len(value) or any(not item.strip() for item in value):
            raise ValueError("观点 ID 不能为空或重复")
        return value

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("选题标题不能为空")
        return value.strip()

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("目标平台不能重复")
        return value


class TopicPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    brief: str | None = Field(default=None, max_length=30000)
    platforms: list[Platform] | None = Field(default=None, min_length=1, max_length=6)
    sources: list[Source] | None = Field(default=None, max_length=100)
    content_line: ContentLine | None = None
    stance_ids: list[str] | None = Field(default=None, max_length=19)
    video_target_duration_seconds: int | None = Field(default=None, ge=10, le=600)
    expected_revision: int = Field(ge=1)

    @field_validator("stance_ids")
    @classmethod
    def unique_stances(cls, value):
        if value is not None and (len(set(value)) != len(value) or any(not item.strip() for item in value)):
            raise ValueError("观点 ID 不能为空或重复")
        return value


class Decision(RequestModel):
    decision: Literal["approved", "rejected", "parked"]
    expected_revision: int = Field(ge=1)


class Generate(RequestModel):
    expected_revision: int = Field(ge=1)


class CreateDrafts(RequestModel):
    expected_revision: int = Field(ge=1)
    platforms: list[Platform] = Field(min_length=1, max_length=6)
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class CandidateCommit(RequestModel):
    expected_version_id: str | None = None
    candidate_ids: dict[str, str] = Field(min_length=1, max_length=6)
    expected_revisions: dict[str, int] = Field(min_length=1, max_length=6)


class RiskConfirmation(RequestModel):
    expected_revision: int = Field(ge=1)
    risks: list[Literal["finance", "current_affairs"]] = Field(min_length=1, max_length=2)

    @field_validator("risks")
    @classmethod
    def unique_risks(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("风险类型不能重复")
        return value


class ProductionSlot(RequestModel):
    """给一个已批准的选题排期。人工动作，不是自动触发。"""
    batch_date: str = Field(min_length=8, max_length=10)
    expires_at: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=200)
    expected_revision: int | None = Field(default=None, ge=1)


class BulkQueue(RequestModel):
    """批量把草稿排入交付队列。只排队，不发布。"""
    draft_ids: list[str] = Field(min_length=1, max_length=50)


class FormatDecision(RequestModel):
    """采纳一个叙事形式。adopted 为空串表示撤销该平台的决策。"""
    adopted: str = Field(default="", max_length=40)
    by: str = Field(default="manual", pattern="^(manual|recommendation)$")


ImageRole = Literal["cover", "body", "card", "concept", "diagram"]
ImageAspect = Literal["2.35:1", "16:9", "3:4", "9:16", "1:1"]


class ImageTask(RequestModel):
    """结构化图片任务：每张图有独立用途、提示词与画幅，替代扁平字符串提示词。"""
    prompt: str = Field(min_length=1, max_length=2000)
    page_role: ImageRole = "body"
    aspect: ImageAspect | None = None
    visual_type: str = Field(default="", max_length=60)
    style_id: str = Field(default="", max_length=60)

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value):
        if not value.strip():
            raise ValueError("图片提示词不能为空")
        return value.strip()


class Work(RequestModel):
    platform: Platform
    format: str = Field(min_length=1, max_length=100)
    # B 轴（叙事形式）可选字段。空串 = 未指定，不是非法值。
    # 用 "" 而非 None：已冻结的旧 payload 是存量 JSON 字符串，本就不含此键，
    # digest() 对整个 dict 做 sha256；若默认 None，model_dump() 会给每件作品
    # 补出 "content_form": null 字面量，同样改变 hash。空串与
    # migrations.py 迁移 2 的 layout_templates.content_form TEXT NOT NULL
    # DEFAULT '' 语义一致。
    # 刻意不加 Literal 约束：native 生成路径不强制 JSON schema
    # （local_generation.py:263 只把 schema 拼进 prompt 文本），
    # 模型可能幻觉出该键；硬枚举会把一次外观漂移变成致命 422。
    # 取值合法性由 formats.content_form_warnings() 软处理，只出 warning。
    content_form: str = Field(default="", max_length=100)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=150000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    image_prompts: list[str] = Field(default_factory=list, max_length=50)
    image_tasks: list[ImageTask] = Field(default_factory=list, max_length=50)
    source_urls: list[str] = Field(default_factory=list, max_length=100)


class VideoShot(RequestModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    role: VideoShotRole
    spoken_text: str = Field(min_length=1, max_length=6000)
    visual_type: VideoVisualType
    visual_notes: str = Field(min_length=1, max_length=6000)
    subtitle_points: list[str] = Field(default_factory=list, max_length=12)
    source_refs: list[str] = Field(default_factory=list, max_length=30)


class StoredVideoShot(VideoShot):
    order: int = Field(ge=1)
    duration_estimate_seconds: int = Field(ge=1)


class VideoScriptWork(RequestModel):
    """Authoring input; narration and duration estimates are server-derived."""
    platform: Platform
    format: Literal["video_script"] = "video_script"
    content_type: Literal["video_script"] = "video_script"
    script_schema_version: Literal[VIDEO_SCRIPT_SCHEMA_VERSION] = VIDEO_SCRIPT_SCHEMA_VERSION
    content_form: str = Field(default="", max_length=100)
    title: str = Field(min_length=1, max_length=300)
    cover_text: str = Field(min_length=1, max_length=100)
    shots: list[VideoShot] = Field(min_length=2, max_length=40)
    target_duration_seconds: int = Field(ge=10, le=600)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source_urls: list[str] = Field(default_factory=list, max_length=100)


class StoredVideoScriptWork(VideoScriptWork):
    shots: list[StoredVideoShot] = Field(min_length=2, max_length=40)
    body: str
    opening_hook: str
    closing_conclusion: str
    estimated_duration_seconds: int = Field(ge=2)
    duration_estimate_basis: dict
    duration_delta_seconds: int
    duration_warning: str = ""
    image_prompts: list[str] = Field(default_factory=list)
    image_tasks: list[dict] = Field(default_factory=list)


class Payload(RequestModel):
    strategy: str = Field(default="", max_length=30000)
    works: list[VideoScriptWork | Work] = Field(min_length=1, max_length=20)


class StoredPayload(RequestModel):
    """Database/freeze projection after server-side shot derivation."""
    strategy: str = Field(default="", max_length=30000)
    works: list[StoredVideoScriptWork | Work] = Field(min_length=1, max_length=20)


class VersionCreate(RequestModel):
    payload: Payload
    base_version_id: str


class Approval(RequestModel):
    platform: Platform
    account_label: str = Field(min_length=1, max_length=100)
    ai_disclosure: str = Field(min_length=1, max_length=3000)
    scheduled_at: str
    facts_confirmed: Literal[True]


class DraftCreate(RequestModel):
    platform: Platform
    account_label: str = Field(default="", max_length=100)


class DraftReview(RequestModel):
    facts_confirmed: Literal[True]
    ai_disclosure: str = Field(min_length=1, max_length=3000)
    account_label: str = Field(min_length=1, max_length=100)


class PublishRecord(RequestModel):
    url: str = Field(max_length=4000)
    published_at: str
    confirmation: Literal[True]

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ValueError("发布记录须为 http/https 链接")
        return value


class Sync(RequestModel):
    version_id: str


class Import(RequestModel):
    base_version_id: str


class SettingsUpdate(RequestModel):
    backup_copy_dir: str | None = Field(default=None, max_length=2000)
    codex_path: str | None = Field(default=None, max_length=2000)
    local_base_url: str | None = Field(default=None, max_length=2000)
    local_model: str | None = Field(default=None, max_length=300)
    task_timeout_seconds: int | None = Field(default=None, ge=30, le=1800)
    vault_path: str | None = Field(default=None, max_length=2000)
    comfy_base_url: str | None = Field(default=None, max_length=2000)
    image_checkpoint_name: str | None = Field(default=None, max_length=200)
    image_max_assets: int | None = Field(default=None, ge=1, le=10)


class WechatCredentials(RequestModel):
    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(min_length=1, max_length=1000)
    thumb_media_id: str = Field(default="", max_length=1000)


class BrowserProbe(RequestModel):
    action: Literal["inspect", "open_editor", "open_drafts", "open_text_image", "open_long_text"] = "inspect"
    capture_screenshot: bool = False


class TrendSettingsUpdate(RequestModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=1440, strict=True)
    scope: Literal['broad', 'ai'] | None = None
    keywords: list[str] | None = Field(default=None, max_length=20)
    enabled_sources: list[str] | None = Field(default=None, min_length=1, max_length=20)
    window_days: int | None = Field(default=None, ge=1, le=30, strict=True)

    @field_validator('keywords')
    @classmethod
    def keyword_lengths(cls, words):
        if words is not None and any(not word.strip() or len(word.strip()) > 60 for word in words):
            raise ValueError('每个关键词需为 1 到 60 字')
        return words


class TrendSelect(RequestModel):
    platforms: list[Platform] = Field(min_length=1, max_length=6)


class VisualPlanRequest(RequestModel):
    """半自动视觉策划请求：只出提案，不生成。"""
    platform: Platform


class AssetSelect(RequestModel):
    """候选图切换请求：把指定位切到指定候选资产。"""
    asset_id: str = Field(min_length=24, max_length=24, pattern=r'^[0-9a-f]{24}$')


# ---------------------------------------------------------------------------
# 内容管理层（V2-18）
# ---------------------------------------------------------------------------

class ContentMeta(RequestModel):
    """选题运营元数据。全部可选，未传即不改。"""
    priority: int | None = Field(default=None, ge=1, le=5)
    favorite: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class BatchAction(RequestModel):
    entity_type: Literal["topic", "draft"]
    ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["archive", "delete", "tag", "priority", "favorite"]
    payload: dict = Field(default_factory=dict)


class BatchArchive(RequestModel):
    archived: bool = True


class BatchTag(RequestModel):
    add: list[str] = Field(default_factory=list, max_length=20)
    remove: list[str] = Field(default_factory=list, max_length=20)


class BatchPriority(RequestModel):
    priority: int = Field(ge=1, le=5)


class BatchFavorite(RequestModel):
    favorite: bool = True


class SourceInspect(RequestModel):
    """来源体检请求。只判 URL，不联网、不写库。"""
    sources: list[dict] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------- V3 本地内容模式

class FactBaseSave(RequestModel):
    """共享事实底稿。字段全部可选：缺项由 fact_base.gaps() 逐条点名。"""
    audience: str = Field(default="", max_length=20000)
    core_view: str = Field(default="", max_length=20000)
    sources: list[dict] = Field(default_factory=list, max_length=100)
    facts: list[dict] = Field(default_factory=list, max_length=200)
    open_questions: list[dict] = Field(default_factory=list, max_length=100)
    author_experience: str = Field(default="", max_length=20000)
    private_info: str = Field(default="", max_length=20000)
    asset_list: list[dict] = Field(default_factory=list, max_length=200)
    platform_angles: dict = Field(default_factory=dict)
    material_type: Literal["original", "research", "adaptation"] | None = None
    raw_text: str = Field(default="", max_length=30000)


class QuickDraftExtract(RequestModel):
    """底稿快速生产：本地模型辅助提炼粘贴底稿。"""
    draft_text: str = Field(min_length=1, max_length=30000)
    material_type: Literal["original", "research", "adaptation"] = "original"


class FactBasePrefill(RequestModel):
    """本篇资料 AI 辅助整理：基于选题标题/说明/已登记来源提炼结构化草稿。
    只返回草稿不写库，作者审阅后仍走 fact-base 保存。"""
    material_type: Literal["original", "research", "adaptation"] = "original"


class QuickDraftProduce(RequestModel):
    """底稿快速生产：粘贴底稿 → 提炼要点 → 一次确认并开始生产。"""
    title: str = Field(min_length=1, max_length=300)
    material_type: Literal["original", "research", "adaptation"]
    platforms: list[Platform] = Field(min_length=1, max_length=6)
    content_line: ContentLine
    video_target_duration_seconds: int = Field(default=60, ge=10, le=600)
    sources: list[Source] = Field(default_factory=list, max_length=100)
    candidate_mode: bool = False
    # 提炼结果（可编辑）。与 FactBaseSave 同构，raw_text 单独承载粘贴原文。
    audience: str = Field(default="", max_length=20000)
    core_view: str = Field(default="", max_length=20000)
    facts: list[dict] = Field(default_factory=list, max_length=200)
    open_questions: list[dict] = Field(default_factory=list, max_length=100)
    author_experience: str = Field(default="", max_length=20000)
    private_info: str = Field(default="", max_length=20000)
    asset_list: list[dict] = Field(default_factory=list, max_length=200)
    platform_angles: dict = Field(default_factory=dict)
    raw_text: str = Field(min_length=1, max_length=30000)

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("目标平台不能重复")
        return value


class Produce(RequestModel):
    """一次发起三平台生产。平台必须与已批准选题一致。"""
    platforms: list[Platform] = Field(min_length=1, max_length=6)


class PlatformReview(RequestModel):
    """集中审阅：一次可确认多份，但逐平台记录。

    2026-11-04 起账号标签与 AI 标识可留空：留空时由后端按平台名与
    「AI 辅助创作」兜底（仍进制品指纹），实际账号与标识在平台后台管理。"""
    result: Literal["approved", "returned", "pending_material"]
    comment: str = Field(default="", max_length=4000)
    account_label: str = Field(default="", max_length=200)
    ai_disclosure: str = Field(default="", max_length=500)
    facts_confirmed: bool
    expected_version_id: str | None = None
    expected_fact_revision: int | None = Field(default=None, ge=0)


class PlatformAccessUpdate(RequestModel):
    """逐平台/账号草稿保存许可。小红书一律拒绝。"""
    platform: Platform
    enabled: bool
    account_label: str = Field(default="", max_length=200)


class PublishRegister(RequestModel):
    """人工发布登记。发布状态只有「未登记／用户确认已发布」。"""
    platform: Platform
    account_label: str = Field(default="", max_length=200)
    version_id: str = Field(default="", max_length=64)
    published_at: str = Field(min_length=10, max_length=40)
    url: str = Field(default="", max_length=2000)
    platform_edited: bool = False
    final_text: str = Field(default="", max_length=200000)
    note: str = Field(default="", max_length=4000)


class PublishBackfill(RequestModel):
    """链接与最终稿回填：两个独立待办，可同时存在，不覆盖已发布事实。"""
    url: str | None = Field(default=None, max_length=2000)
    final_text: str | None = Field(default=None, max_length=200000)


class FeedbackCreate(RequestModel):
    """匿名读者反馈录入。AI 只整理，不发送。"""
    text: str = Field(min_length=1, max_length=20000)
    platform: str = Field(default="", max_length=20)
    topic_id: str = Field(default="", max_length=64)


class ObservationCreate(RequestModel):
    """手工登记一条运营观测。缺失值记未知（不传即未知，不记 0）。"""
    platform: Platform
    metric_name: str = Field(min_length=1, max_length=100)
    metric_value: float | None = None
    scope: Literal["work", "account"] = "work"
    work_ref: str = Field(default="", max_length=300)
    window_start: str = Field(default="", max_length=40)
    window_end: str = Field(default="", max_length=40)
    source: str = Field(default="manual", max_length=60)
    evidence_path: str = Field(default="", max_length=400)
    note: str = Field(default="", max_length=2000)
    observed_at: str = Field(default="", max_length=40)


class CommitImport(RequestModel):
    """提交导入预览：行数据来自预览响应，重复行在提交时判定。"""
    rows: list[dict] = Field(min_length=0, max_length=5000)
