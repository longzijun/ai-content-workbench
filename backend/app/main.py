"""Loopback-only API and the built workbench frontend."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import access, adapters, db, service
from .content_studio import WorkingCopySave, SuggestionRequest, CommitCopies
from .schemas import CreateDrafts, CandidateCommit
from .config import PROJECT_ROOT, get_settings, settings_to_dict, update_settings
from .schemas import Approval, BulkQueue, Decision, DraftCreate, DraftReview, FactBaseSave, FeedbackCreate, Generate, Import, PublishBackfill, PublishRecord, PublishRegister, Produce, RiskConfirmation, PlatformReview, PlatformAccessUpdate, SettingsUpdate, Sync, TopicCreate, TopicPatch, VersionCreate, WechatCredentials, Platform, BrowserProbe, TrendSettingsUpdate, TrendSelect, VisualPlanRequest, AssetSelect, ProductionSlot, FormatDecision, ContentMeta, BatchAction, BatchArchive, SourceInspect, ObservationCreate, CommitImport, QuickDraftExtract, QuickDraftProduce, FactBasePrefill

_BOOT_TIME = datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("WORKBENCH_PROCESS_ROLE") == "web":
        from .execution_runtime import check_schema
        if not check_schema():
            raise RuntimeError("运行数据库尚未升级，请先执行 scripts/migrate_runtime.py")
    else:
        db.init_db()  # In-process isolated tests; deployed Web never runs migrations.
    from . import v3_migration
    with db.connect() as connection:
        marker = v3_migration.migration_marker(connection)
    if marker:
        try:
            # 迁移标记时间晚于进程启动时间，说明本次启动刚刚执行了首次迁移。
            if datetime.fromisoformat(marker) >= _BOOT_TIME:
                with db.transaction() as connection:
                    service._activity(connection, None, "V3 本地内容模式迁移完成：旧交付任务已隔离、旧排期与未完成生成已暂停；平台自动操作保持关闭")
        except ValueError:
            pass
    try:
        yield
    finally:
        from .browser_bridge import stop_browser
        stop_browser()


app = FastAPI(title="自媒体自动化流程工作台", version="0.1.0", lifespan=lifespan)
def allowed_hosts():
    hosts = ["127.0.0.1", "localhost", "testserver"]
    external = os.environ.get("WORKBENCH_EXTERNAL_ORIGIN", "").strip().rstrip("/")
    if not external:
        network = get_settings().runtime_dir / "private-network.json"
        if network.is_file():
            external = str(json.loads(network.read_text("utf-8")).get("origin", "")).rstrip("/")
    if external:
        parsed = urlparse(external)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("WORKBENCH_EXTERNAL_ORIGIN 必须是完整且不带路径的 HTTPS 站点地址")
        hosts.append(parsed.hostname)
    return hosts


app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())


@app.middleware("http")
async def private_responses(request: Request, call_next):
    network = get_settings().runtime_dir / "private-network.json"
    if network.is_file():
        try:
            policy = json.loads(network.read_text("utf-8"))
            owner = str(policy.get("owner_login", ""))
            hostname = urlparse(str(policy.get("origin", ""))).hostname
            external = (request.url.hostname == hostname or bool(request.headers.get("tailscale-user-login"))
                        or (request.client and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient")))
            if external and (not owner or not hmac.compare_digest(owner.encode("utf-8"), request.headers.get("tailscale-user-login", "").encode("utf-8"))):
                return JSONResponse(status_code=403, content={"detail": "此私人入口仅允许本人网络设备访问"}, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, TypeError):
            return JSONResponse(status_code=503, content={"detail": "私人访问配置需要检查"}, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        expected = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        if parsed.scheme not in ("http", "https") or origin.rstrip("/") != expected:
            raise HTTPException(status_code=403, detail="请求来源与工作台地址不一致")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="拒绝跨站访问工作台")


def session(request: Request):
    check_origin(request)
    active = access.current(request.cookies.get(access.COOKIE))
    if active is None:
        raise HTTPException(status_code=401, detail="请登录工作台，或重新验证已过期的会话")
    return active


def mutation(request: Request, active_session=Depends(session)):
    provided = request.headers.get("x-workbench-token", "")
    if not hmac.compare_digest(provided, active_session["token"]):
        raise HTTPException(status_code=403, detail="缺少有效的工作台请求令牌，请刷新页面")


WRITE = [Depends(mutation)]
READ = [Depends(session)]


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    details = "; ".join(f"{'.'.join(str(piece) for piece in item['loc'][1:])}: {item['msg']}" for item in error.errors())
    return JSONResponse(status_code=422, content={"detail": details})


@app.exception_handler(ValueError)
async def value_error(request, error):
    return JSONResponse(status_code=422, content={"detail": str(error)})


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256)
    device_name: str = Field(default="浏览器", max_length=100)


@app.get("/api/auth/status")
def auth_status(request: Request, response: Response):
    check_origin(request)
    response.headers["Cache-Control"] = "no-store"
    return {"configured": access.configured(), "authenticated": access.current(request.cookies.get(access.COOKIE)) is not None}


@app.post("/api/auth/login")
def login(request: Request, response: Response, data: LoginRequest):
    check_origin(request)
    active = access.login(data.password, data.device_name, "owner-login")
    response.set_cookie(access.COOKIE, active["id"], max_age=access.LIFETIME_SECONDS,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@app.post("/api/auth/logout", dependencies=WRITE)
def logout(request: Request, response: Response):
    access.revoke(request.cookies.get(access.COOKIE))
    response.delete_cookie(access.COOKIE, path="/")
    return {"authenticated": False}


@app.get("/api/auth/sessions", dependencies=READ)
def device_sessions(request: Request):
    return {"sessions": access.list_sessions(request.cookies.get(access.COOKIE))}


@app.delete("/api/auth/sessions/{session_id}", dependencies=WRITE)
def revoke_device(session_id: str):
    access.revoke(session_id)
    return {"revoked": True}


@app.get("/api/bootstrap")
def bootstrap(request: Request, response: Response, active_session=Depends(session)):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    return {"token": active_session["token"], "settings": settings_to_dict(settings), "overview": service.state()["overview"], "health": adapters.get_health(settings)}


@app.get("/api/ready")
def ready(request: Request):
    check_origin(request)
    return {"app_id": "self-media-workbench", "version": "0.2.0", "api_layout": "web-worker",
            "release_id": os.environ.get("WORKBENCH_RELEASE_ID", "development")}


@app.post("/api/lms/reload")
def lms_reload(request: Request):
    """受凭据保护的本机模型变更通知。"""
    check_origin(request)
    machine_token = os.environ.get("WORKBENCH_MACHINE_TOKEN", "")
    if not machine_token or not hmac.compare_digest(machine_token, request.headers.get("x-workbench-machine-token", "")):
        raise HTTPException(status_code=401, detail="需要独立的本机组件授权")
    note = "统一模型服务通知：LM Studio 模型/上下文已切换"
    try:
        with db.connect() as connection:
            service._activity(connection, "", note)
    except Exception:
        note = note + "（activity 写入失败，已忽略）"
    models: list[str] = []
    try:
        import httpx
        with httpx.Client(timeout=3) as client:
            response = client.get("http://127.0.0.1:1234/v1/models")
            models = [m.get("id") for m in response.json().get("data", [])]
    except Exception:
        pass
    return {"ok": True, "notified": note, "models": models}


@app.get("/api/state", dependencies=READ)
def get_state():
    return service.state()


@app.get("/api/platform-registry", dependencies=READ)
def platform_registry():
    from .platform_registry import public_registry
    return public_registry()


@app.get("/api/runtime-status", dependencies=READ)
def runtime_status():
    from . import backup_scheduler, execution_runtime
    return {**execution_runtime.runtime_status(), "backup": backup_scheduler.status()}


@app.get("/api/style-library", dependencies=READ)
def get_style_library():
    from . import style_library
    return style_library.load_library()


@app.get("/api/health", dependencies=READ)
def get_health():
    return adapters.get_health(get_settings())


@app.get("/api/settings", dependencies=READ)
def get_config():
    return settings_to_dict(get_settings())


@app.post("/api/settings", dependencies=WRITE)
def set_config(data: SettingsUpdate):
    settings = update_settings(data.model_dump(exclude_none=True))
    return settings_to_dict(settings if settings is not None else get_settings())


@app.post("/api/integrations/wechat", dependencies=WRITE)
def save_wechat(data: WechatCredentials):
    from .config import save_wechat_credentials
    save_wechat_credentials(data.app_id.strip(), data.app_secret, data.thumb_media_id.strip())
    return settings_to_dict(get_settings())


@app.post("/api/integrations/wechat/check", dependencies=WRITE)
def check_wechat():
    from . import runtime_mode
    runtime_mode.require_platform_operations("公众号接口连通性检查")
    return adapters.check_wechat_connection(get_settings())


@app.get("/api/integrations/browser/status", dependencies=READ)
def browser_status():
    from . import browser_bridge
    from importlib.util import find_spec
    return {"platforms": browser_bridge.get_status(get_settings()), "available": find_spec("playwright") is not None}


@app.get("/api/capabilities", dependencies=READ)
def platform_capabilities():
    from .capabilities import inventory
    return inventory(get_settings())


@app.get("/api/production", dependencies=READ)
def production_overview():
    """排期、批次与定时器概览。只读。"""
    from . import production, scheduler
    return {"production": production.status(), "scheduler": scheduler.status(),
            "slots": production.list_slots()}


@app.post("/api/topics/{topic_id}/schedule", dependencies=WRITE)
def schedule_topic(topic_id: str, data: ProductionSlot):
    """为已批准的选题安排生产日期。人工动作。"""
    from . import production
    return production.schedule_slot(topic_id, data.batch_date,
                                     expires_at=data.expires_at, note=data.note,
                                     expected_revision=data.expected_revision)


@app.delete("/api/production/slots/{slot_id}", dependencies=WRITE)
def cancel_production_slot(slot_id: str):
    from . import production
    return production.cancel_slot(slot_id)


@app.post("/api/production/slots/{slot_id}/resume", dependencies=WRITE)
def resume_production_slot(slot_id: str):
    """恢复被 V3 迁移暂停的排期。恢复后也只生成本地内容，不补做停服期间遗漏的批次。"""
    from . import production
    return production.resume_slot(slot_id)


@app.get("/api/runtime-mode", dependencies=READ)
def runtime_mode_view():
    from . import runtime_mode
    return runtime_mode.mode_view()


@app.get("/api/platform-access", dependencies=READ)
def platform_access_list():
    from . import runtime_mode
    return runtime_mode.platform_access_view()


@app.post("/api/platform-access", dependencies=WRITE)
def platform_access_update(data: PlatformAccessUpdate):
    from . import runtime_mode
    return runtime_mode.set_platform_access(data.platform, data.enabled, data.account_label)


@app.get("/api/quarantine", dependencies=READ)
def quarantine_list():
    """旧平台交付任务隔离清单。只读；隔离态不可重排、不可批量重试。"""
    from . import v3_migration
    with db.connect() as connection:
        return {"items": v3_migration.quarantined_tasks(connection),
                "note": "隔离任务保留原始状态与原因，不会自动重试；结果未知的保存请人工核对平台草稿箱。"}


@app.post("/api/production/run", dependencies=WRITE)
def run_production_now():
    """立刻跑一次批次。与定时器走完全相同的代码路径。本地内容模式只生成本地作品。"""
    from . import production
    return production.tick()


@app.get("/api/topics/{topic_id}/formats", dependencies=READ)
def topic_formats(topic_id: str):
    """三个平台的叙事形式推荐 + 已采纳的决策。只读，不调模型。"""
    from . import format_decisions
    try:
        return format_decisions.recommend_all(topic_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.get("/api/versions/{version_id}/layout-audit", dependencies=READ)
def version_layout_audit(version_id: str):
    """读回该版本已落库的排版审计结果。纯读。"""
    from . import layout_flow
    return layout_flow.results_for_version(version_id)


@app.post("/api/versions/{version_id}/layout-audit", dependencies=WRITE)
def run_version_layout_audit(version_id: str):
    """重跑排版审计。审核时会自动跑一次，这里是手动重跑入口。"""
    from . import layout_flow
    try:
        return layout_flow.run_for_version(version_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.put("/api/topics/{topic_id}/formats/{platform}", dependencies=WRITE)
def save_topic_format(topic_id: str, platform: Platform, data: FormatDecision):
    """采纳或覆盖某平台的叙事形式。下一次建任务时随快照带进生成。"""
    from . import format_decisions
    try:
        return format_decisions.save(topic_id, platform, data.adopted, by=data.by)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))


@app.delete("/api/topics/{topic_id}/formats/{platform}", dependencies=WRITE)
def clear_topic_format(topic_id: str, platform: Platform):
    """撤销某平台的形式决策，回到「不注入」的默认路径。"""
    from . import format_decisions
    return format_decisions.clear(topic_id, platform)


@app.post("/api/drafts/queue", dependencies=WRITE)
def queue_drafts(data: BulkQueue):
    """把若干草稿排入交付队列。只排队 —— 工作台不会代你发布任何内容。"""
    from . import delivery, runtime_mode, service
    from . import db as database
    runtime_mode.require_platform_operations("草稿批量排入交付队列")
    queued, skipped = [], []
    with database.transaction() as connection:
        for draft_id in dict.fromkeys(data.draft_ids):
            draft = service._row(connection, 'drafts', draft_id)
            if draft['state'] in ('blocked', 'stale', 'published', 'reviewed'):
                skipped.append({'draft_id': draft_id, 'state': draft['state'],
                                'reason': '该草稿当前状态不适合排队'})
                continue
            if draft['state'] == 'unknown':
                skipped.append({'draft_id': draft_id, 'state': draft['state'],
                                'reason': '上次保存结果未确认，请先在平台核对，不能自动重试'})
                continue
            delivery.enqueue_in(connection, [draft_id])
            queued.append(draft_id)
    if queued:
        with database.transaction() as connection:
            for draft_id in queued:
                draft = service._row(connection, 'drafts', draft_id)
                service._notice(connection, draft['topic_id'], '草稿已排入交付队列',
                                '已排入交付队列，请在平台草稿箱核对正文、图片、账号和 AI 标识；'
                                '工作台不会代你发布，发布由你在平台完成。')
    return {"queued": queued, "skipped": skipped,
            "note": "排队只是把草稿送进交付流程；工作台不会代你发布任何内容。"}


@app.post("/api/integrations/browser/{platform}/login", dependencies=WRITE)
def browser_login(platform: Platform):
    from . import browser_bridge, runtime_mode
    runtime_mode.require_platform_operations("打开平台登录页")
    return browser_bridge.open_login(platform, get_settings())


@app.post("/api/integrations/browser/{platform}/check", dependencies=WRITE)
def browser_check(platform: Platform):
    from . import browser_bridge, runtime_mode
    runtime_mode.require_platform_operations("平台连接检查")
    return browser_bridge.check_connection(platform, get_settings())


@app.post("/api/integrations/browser/{platform}/inspect", dependencies=WRITE)
def browser_inspect(platform: Platform):
    from . import browser_bridge, runtime_mode
    runtime_mode.require_platform_operations("平台页面巡检")
    return browser_bridge.inspect_platform(platform, get_settings())


@app.post("/api/integrations/browser/{platform}/probe", dependencies=WRITE)
def browser_probe(platform: Platform, data: BrowserProbe):
    from . import browser_drafts, runtime_mode
    runtime_mode.require_platform_operations("平台浏览器探测")
    return browser_drafts.probe_platform(platform, data.action, get_settings(), data.capture_screenshot)


@app.post("/api/integrations/browser/{platform}/verify", dependencies=WRITE)
def browser_verify(platform: Platform):
    from . import browser_drafts, runtime_mode
    runtime_mode.require_platform_operations("平台草稿连接核验")
    return browser_drafts.verify_saved_connection_draft(platform, get_settings())


@app.post("/api/topics/{topic_id}/research", dependencies=WRITE)
def topic_research(topic_id: str):
    from .research import collect
    return collect(service.get_topic(topic_id))


@app.post("/api/sources/inspect", dependencies=READ)
def sources_inspect(data: SourceInspect):
    """来源体检：批准前就能知道哪一条不合格、该换成什么。

    不写库、不联网，纯判定。批准按钮据此在点击前变灰，而不是跑完整条决策链
    之后才被拦截。
    """
    from .research import inspect_sources
    return inspect_sources(data.sources)


@app.get("/api/topics/{topic_id}/source-check", dependencies=READ)
def topic_source_check(topic_id: str):
    """已存选题的来源体检。"""
    from .research import inspect_sources
    return inspect_sources(service.get_topic(topic_id).get("sources"))


@app.post("/api/topics", dependencies=WRITE)
def new_topic(data: TopicCreate):
    return service.create_topic(data.model_dump())


@app.get('/api/trends', dependencies=READ)
def trend_state(direction: str = "", content_line: str = "", classification: str = "", publishable: str = "", cursor: str = "", limit: int = 50):
    from . import trends
    filters = {}
    if direction:
        filters['direction'] = direction
    if content_line:
        filters['content_line'] = content_line
    if classification:
        filters['classification'] = classification
    if publishable in ('true', 'false'):
        filters['publishable'] = publishable == 'true'
    return trends.state(filters, cursor=cursor or None, limit=limit)


@app.post('/api/trends/refresh', dependencies=WRITE)
def trend_refresh():
    from . import trends
    return trends.request_refresh()


@app.post('/api/trends/settings', dependencies=WRITE)
def trend_settings(data: TrendSettingsUpdate):
    from . import trends
    return trends.update_settings(data.model_dump(exclude_none=True))


@app.post('/api/trends/{candidate_id}/select', dependencies=WRITE)
def trend_select(candidate_id: str, data: TrendSelect):
    from . import trends
    result = trends.select(candidate_id, data.platforms)
    # Retrying selection repairs a missed enqueue without fetching in the API.
    # The durable request is idempotent and never approves or produces content.
    from . import research_queue
    research_queue.enqueue(result['topic'])
    return result


@app.post('/api/trends/{candidate_id}/ignore', dependencies=WRITE)
def trend_ignore(candidate_id: str):
    from . import trends
    return trends.ignore(candidate_id)


@app.get("/api/topics/{topic_id}", dependencies=READ)
def get_topic(topic_id: str):
    return service.get_topic(topic_id)


@app.patch("/api/topics/{topic_id}", dependencies=WRITE)
def edit_topic(topic_id: str, data: TopicPatch):
    return service.patch_topic(topic_id, data.model_dump(exclude_unset=True))


@app.post("/api/topics/{topic_id}/risk-confirmation", dependencies=WRITE)
def confirm_risks(topic_id: str, data: RiskConfirmation):
    return service.confirm_topic_risks(topic_id, data.model_dump())


@app.post("/api/topics/{topic_id}/decision", dependencies=WRITE)
def decision(topic_id: str, data: Decision):
    return service.decide_topic(topic_id, data.model_dump())


@app.post("/api/topics/{topic_id}/create-drafts", dependencies=WRITE)
def create_editable_drafts(topic_id: str, data: CreateDrafts):
    from . import draft_workflow
    return draft_workflow.create(topic_id, data.model_dump())


@app.get("/api/topics/{topic_id}/candidate-drafts", dependencies=READ)
def list_candidate_drafts(topic_id: str):
    from . import candidate_drafts
    return candidate_drafts.list_for_topic(topic_id)


@app.get("/api/candidate-drafts/{candidate_id}", dependencies=READ)
def read_candidate_draft(candidate_id: str):
    from . import candidate_drafts, draft_workflow
    return candidate_drafts.read(draft_workflow.candidate_topic(candidate_id), candidate_id)


@app.put("/api/candidate-drafts/{candidate_id}", dependencies=WRITE)
def save_candidate_draft(candidate_id: str, data: WorkingCopySave):
    from . import candidate_drafts, draft_workflow
    return candidate_drafts.save(draft_workflow.candidate_topic(candidate_id), candidate_id, data)


@app.post("/api/topics/{topic_id}/candidate-commit", dependencies=WRITE)
def commit_candidate_drafts(topic_id: str, data: CandidateCommit):
    from . import candidate_drafts
    return candidate_drafts.commit(topic_id, data.model_dump())


@app.post("/api/topics/{topic_id}/generate", dependencies=WRITE)
def generate(topic_id: str, data: Generate):
    return service.generate(topic_id, data.expected_revision)


@app.post("/api/topics/{topic_id}/workbuddy-handoff", dependencies=WRITE)
def export_workbuddy_handoff(topic_id: str, data: Generate):
    raise HTTPException(status_code=410, detail="人工生成交接已停用，请使用本地模型生成；历史交接仅保留溯源")


@app.post("/api/workbuddy-handoffs/{task_id}/import", dependencies=WRITE)
def import_workbuddy_handoff(task_id: str, data: dict):
    raise HTTPException(status_code=410, detail="WorkBuddy JSON 导入已停用，历史版本和交接记录保留")


@app.post("/api/jobs/{job_id}/cancel", dependencies=WRITE)
def cancel(job_id: str):
    return service.cancel_job(job_id)


@app.post("/api/jobs/{job_id}/resume", dependencies=WRITE)
def resume_job(job_id: str):
    """恢复被 V3 迁移暂停的未完成生成任务。只生成本地内容。"""
    return service.resume_paused_job(job_id)


@app.post("/api/jobs/{job_id}/retry", dependencies=WRITE)
def retry(job_id: str):
    return service.retry_job(job_id)


@app.post("/api/topics/{topic_id}/versions", dependencies=WRITE)
def edit_version(topic_id: str, data: VersionCreate):
    return service.create_version(topic_id, data.payload.model_dump(), data.base_version_id, defer_delivery=True)


@app.post("/api/versions/{version_id}/review", dependencies=WRITE)
def review(version_id: str):
    return service.review_version(version_id)


@app.get('/api/versions/{version_id}/assets/{platform}/{asset_id}', dependencies=READ)
def version_asset(version_id: str, platform: Platform, asset_id: str):
    from .image_assets import get_asset_file, ImageAssetError
    version = service.get_version(version_id)
    try:
        path = get_asset_file(get_settings(), version['topic_id'], version_id, platform, asset_id)
    except ImageAssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return FileResponse(path, headers={'Cache-Control':'private, no-cache'})


@app.post("/api/versions/{version_id}/visual-plan", dependencies=WRITE)
def visual_plan(version_id: str, data: VisualPlanRequest):
    raise HTTPException(status_code=409, detail="AI 生图策略已停用（2026-10-20 决策）：配图仅使用热点抓取留存的源生图；请在选题详情重新执行「检索并读取原文」补充源图后建立新版本")


@app.post("/api/versions/{version_id}/assets/{platform}/source-images", dependencies=WRITE)
def version_source_images(version_id: str, platform: Platform):
    """手动重注册源生图：候选池更新或注册中断后由用户触发，失败如实返回。"""
    from .image_assets import ImageAssetError
    from .source_images import register_source_images
    version = service.get_version(version_id)
    if platform not in {work['platform'] for work in version['payload']['works']}:
        raise HTTPException(status_code=404, detail="当前版本没有该平台作品")
    try:
        manifest = register_source_images(get_settings(), version['topic_id'], version_id, platform)
    except ImageAssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return manifest or {'status': 'no_source_images', 'assets': [],
                        'error': '该选题没有已抓取的源生图，请先在选题详情执行检索并读取原文'}


@app.post("/api/versions/{version_id}/assets/{platform}/regenerate/{index}", dependencies=WRITE)
def asset_regenerate(version_id: str, platform: Platform, index: int):
    raise HTTPException(status_code=409, detail="AI 生图策略已停用（2026-10-20 决策）：仅使用源生图；请在选题详情重新执行「检索并读取原文」抓取源图后建立新版本")


@app.post("/api/versions/{version_id}/assets/{platform}/select/{index}", dependencies=WRITE)
def asset_select(version_id: str, platform: Platform, index: int, data: AssetSelect):
    return service.select_version_asset(version_id, platform, index, data.asset_id)


@app.get("/api/versions/{version_id}/assets/{platform}/{asset_id}/typeset", dependencies=READ)
def asset_typeset(version_id: str, platform: Platform, asset_id: str,
                  title: str = "", account_label: str = "", page_label: str = ""):
    from . import typeset as typesetting
    from .image_assets import ImageAssetError
    version = service.get_version(version_id)
    title = (title or "").strip()[:120]
    if not title:
        raise HTTPException(status_code=422, detail="排版标题不能为空")
    try:
        path = typesetting.typeset_asset(get_settings(), version['topic_id'], version_id, platform, asset_id,
                                         title=title, account_label=(account_label or "").strip()[:60],
                                         page_label=(page_label or "").strip()[:20])
    except (ImageAssetError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, ImageAssetError) else 422, detail=str(exc)) from None
    return FileResponse(path, media_type="image/png", headers={'Cache-Control':'private, no-cache'})


@app.post("/api/versions/{version_id}/approve", dependencies=WRITE)
def approve(version_id: str, data: Approval):
    return service.approve_version(version_id, data.model_dump())


@app.post("/api/versions/{version_id}/drafts", dependencies=WRITE)
def draft(version_id: str, data: DraftCreate):
    return service.create_draft(version_id, data.model_dump())


@app.post("/api/drafts/{draft_id}/sync", dependencies=WRITE)
def draft_sync(draft_id: str):
    from . import db, runtime_mode, service
    runtime_mode.require_platform_operations("平台草稿同步")
    with db.connect() as connection:
        draft = service._row(connection, "drafts", draft_id)
        runtime_mode.require_platform_save(draft["platform"], "平台草稿同步")
        if not draft.get("remote_id"):
            from . import review
            status = review.current(dict(draft))
            service.require(status.get("approved") is True, "；".join(status.get("blocking") or ["请先逐平台审阅通过"]))
    return service.sync_draft(draft_id, verify_media=True)


@app.post("/api/drafts/{draft_id}/queue", dependencies=WRITE)
def draft_queue(draft_id: str):
    from . import runtime_mode
    runtime_mode.require_platform_operations("草稿排入交付队列")
    from .delivery import enqueue
    return enqueue(draft_id)


@app.post("/api/drafts/{draft_id}/cancel-delivery", dependencies=WRITE)
def draft_cancel_delivery(draft_id: str):
    from .delivery import cancel
    return cancel(draft_id)


@app.post("/api/drafts/{draft_id}/review", dependencies=WRITE)
def draft_review(draft_id: str, data: DraftReview):
    return service.review_draft(draft_id, data.model_dump())


@app.post("/api/drafts/{draft_id}/open", dependencies=WRITE)
def draft_open(draft_id: str):
    from . import runtime_mode
    runtime_mode.require_platform_operations("打开平台已存草稿")
    result = service.open_draft(draft_id)
    if result.get("state") != "opened":
        raise HTTPException(status_code=409, detail=result.get("error") or "原草稿尚未确认打开，请在平台后台核对")
    return result


@app.post("/api/drafts/{draft_id}/record", dependencies=WRITE)
def draft_record(draft_id: str, data: PublishRecord):
    return service.record_draft(draft_id, data.model_dump())


@app.get("/api/drafts/{draft_id}/content", dependencies=READ)
def draft_content(draft_id: str):
    return service.draft_content(draft_id)


# ---------------------------------------------------------------- V3 事实底稿与三平台生产

@app.get("/api/topics/{topic_id}/fact-base", dependencies=READ)
def get_fact_base(topic_id: str):
    from . import fact_base
    fact = fact_base.get(topic_id)
    return {"fact_base": fact, "gaps": fact_base.gaps(fact) if fact else ["尚未建立共享事实底稿"]}


@app.get("/api/topics/{topic_id}/studio-draft/{platform}", dependencies=READ)
def studio_draft(topic_id: str, platform: Platform, base_version_id: str):
    from . import content_studio
    return content_studio.read_copy(topic_id, base_version_id, platform)


@app.put("/api/topics/{topic_id}/studio-draft/{platform}", dependencies=WRITE)
def save_studio_draft(topic_id: str, platform: Platform, base_version_id: str, data: WorkingCopySave):
    from . import content_studio
    return content_studio.save_copy(topic_id, base_version_id, platform, data)


@app.post("/api/topics/{topic_id}/studio-draft/{platform}/suggestions", dependencies=WRITE)
def studio_suggestion(topic_id: str, platform: Platform, base_version_id: str, data: SuggestionRequest):
    from . import content_studio
    return content_studio.suggest(topic_id, base_version_id, platform, data)


@app.get("/api/topics/{topic_id}/studio-draft/{platform}/suggestions/{suggestion_id}", dependencies=READ)
def check_studio_suggestion(topic_id: str, platform: Platform, base_version_id: str, suggestion_id: str):
    from . import content_studio
    return content_studio.validate_suggestion(topic_id, base_version_id, platform, suggestion_id)


@app.post("/api/topics/{topic_id}/studio-commit", dependencies=WRITE)
def commit_studio_drafts(topic_id: str, data: CommitCopies):
    from . import content_studio
    return content_studio.commit_copies(topic_id, data)


@app.post("/api/topics/{topic_id}/fact-base", dependencies=WRITE)
def save_fact_base(topic_id: str, data: FactBaseSave):
    from . import fact_base
    fact = fact_base.save(topic_id, data.model_dump())
    return {"fact_base": fact, "gaps": fact_base.gaps(fact)}


@app.post("/api/topics/{topic_id}/fact-base/ready", dependencies=WRITE)
def ready_fact_base(topic_id: str):
    from . import fact_base
    fact = fact_base.mark_ready(topic_id)
    return {"fact_base": fact, "gaps": fact_base.gaps(fact)}


@app.post("/api/topics/{topic_id}/fact-base/prefill", dependencies=WRITE)
def prefill_fact_base(topic_id: str, data: FactBasePrefill):
    """本篇资料 AI 辅助整理：返回结构化草稿，不写库。作者审阅后走保存/确认。"""
    from . import quick_draft
    return quick_draft.prefill(topic_id, data.material_type)


@app.post("/api/topics/{topic_id}/produce", dependencies=WRITE)
def produce(topic_id: str, data: Produce):
    """一次发起三平台生产：底稿锁定 → 本地生成，不排平台交付。"""
    return service.produce_topic(topic_id, data.platforms)


# ---------------------------------------------------------------- 底稿快速生产（批次B）

@app.post("/api/quick-draft/extract", dependencies=WRITE)
def quick_draft_extract(data: QuickDraftExtract):
    """本地模型辅助提炼粘贴底稿：返回可编辑的受众/观点/事实/待核/平台角度。"""
    from . import quick_draft
    return quick_draft.extract(data.model_dump())


@app.post("/api/quick-draft/produce", dependencies=WRITE)
def quick_draft_produce(data: QuickDraftProduce):
    """粘贴底稿 → 提炼要点 → 一次确认并开始生产：建选题、建事实底稿、批准、生产。"""
    from . import quick_draft
    return quick_draft.produce(data.model_dump())


@app.get("/api/topics/{topic_id}/review", dependencies=READ)
def review_console(topic_id: str):
    from . import review
    return review.console(topic_id)


@app.post("/api/drafts/{draft_id}/review-platform", dependencies=WRITE)
def review_platform(draft_id: str, data: PlatformReview):
    from . import review
    return review.record(draft_id, data.result, data.comment, data.account_label, data.ai_disclosure, data.facts_confirmed,
                         data.expected_version_id, data.expected_fact_revision)


# ---------------------------------------------------------------- V3 材料包

@app.post("/api/topics/{topic_id}/material-packages", dependencies=WRITE)
def build_material_package(topic_id: str):
    raise HTTPException(status_code=410, detail="日常 ZIP 材料包已停用，请打开发布准备页；历史导出仍可下载")


@app.get("/api/topics/{topic_id}/publish-preparation", dependencies=READ)
def publish_preparation(topic_id: str):
    from . import publish_preparation as preparation
    return preparation.get(topic_id)


@app.get("/api/topics/{topic_id}/material-packages", dependencies=READ)
def list_material_packages(topic_id: str):
    from . import material_package
    return {"items": material_package.list_packages(topic_id)}


@app.get("/api/material-packages/{package_id}", dependencies=READ)
def get_material_package(package_id: str):
    from . import material_package
    return material_package.get(package_id)


@app.get("/api/material-packages/{package_id}/download", dependencies=READ)
def download_material_package(package_id: str):
    from . import material_package
    package = material_package.get(package_id)
    path = package.get("zip_path") or ""
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="材料包 ZIP 不存在，请重新构建")
    return FileResponse(path, media_type="application/zip", filename=Path(path).name)


# ---------------------------------------------------------------- V3 人工发布台账

@app.post("/api/topics/{topic_id}/publish-records", dependencies=WRITE)
def register_publish(topic_id: str, data: PublishRegister):
    from . import publish_ledger
    return publish_ledger.register(topic_id, data.model_dump())


@app.get("/api/topics/{topic_id}/publish-records", dependencies=READ)
def topic_publish_records(topic_id: str):
    from . import publish_ledger
    return {"items": publish_ledger.list_records(topic_id)}


@app.get("/api/publish-records", dependencies=READ)
def all_publish_records():
    from . import publish_ledger
    return {"items": publish_ledger.list_records()}


@app.post("/api/publish-records/{record_id}/backfill", dependencies=WRITE)
def backfill_publish(record_id: str, data: PublishBackfill):
    from . import publish_ledger
    return publish_ledger.backfill(record_id, data.model_dump(exclude_none=True))


# ---------------------------------------------------------------- V3 数据导入与周复盘

@app.post("/api/data-imports", dependencies=WRITE)
async def preview_data_import(request: Request):
    """CSV 预览：body 为原始 CSV 文本（Content-Type: text/csv; charset=utf-8），
    文件名走 X-Filename 头。不做 multipart，避免额外依赖。"""
    from . import data_import
    raw = await request.body()
    service.require(len(raw) <= 5 * 1024 * 1024, "CSV 不能超过 5MB", 422)
    service.require(bool(raw), "请提供 CSV 内容", 422)
    filename = request.headers.get("x-filename") or "import.csv"
    try:
        return data_import.preview(filename[:300], raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@app.post("/api/data-imports/{batch_id}/commit", dependencies=WRITE)
def commit_data_import(batch_id: str, data: CommitImport):
    from . import data_import
    return data_import.commit_rows(batch_id, data.rows)


@app.get("/api/data-imports", dependencies=READ)
def list_data_imports():
    from . import data_import
    return {"items": data_import.list_batches()}


@app.post("/api/observations", dependencies=WRITE)
def create_observation(data: ObservationCreate):
    from . import data_import
    return data_import.record_manual(data.model_dump())


@app.get("/api/observations", dependencies=READ)
def list_observations(platform: str | None = None, work_ref: str | None = None):
    from . import data_import
    return {"items": data_import.list_observations(platform, work_ref)}


@app.post("/api/feedback", dependencies=WRITE)
def create_feedback(data: FeedbackCreate):
    from . import feedback
    return feedback.add(data.model_dump())


@app.get("/api/feedback", dependencies=READ)
def list_feedback(topic_id: str | None = None):
    from . import feedback
    return {"items": feedback.list_items(topic_id)}


@app.post("/api/feedback/{feedback_id}/organize", dependencies=WRITE)
def organize_feedback(feedback_id: str):
    from . import feedback
    return feedback.organize(feedback_id)


@app.get("/api/weekly-review", dependencies=READ)
def get_weekly_review(week: str | None = None, refresh: bool = False):
    from . import weekly_review
    if refresh:
        service.require(week, "生成复盘需要 week=YYYY-MM-DD", 422)
        return weekly_review.generate(week)
    return {"review": weekly_review.latest(week)}


@app.post("/api/notifications/{notification_id}/read", dependencies=WRITE)
def notification_read(notification_id: str):
    return service.read_notification(notification_id)


@app.post("/api/publications/{publication_id}/revoke", dependencies=WRITE)
def revoke(publication_id: str):
    return service.revoke_publication(publication_id)


@app.get("/api/publications/{publication_id}/export", dependencies=WRITE)
def export(publication_id: str):
    path = service.export_publication(publication_id)
    return FileResponse(path, media_type="application/zip", filename=Path(path).name)


@app.post("/api/publications/{publication_id}/record", dependencies=WRITE)
def record(publication_id: str, data: PublishRecord):
    return service.record_publication(publication_id, data.model_dump())


@app.post("/api/topics/{topic_id}/sync", dependencies=WRITE)
def sync(topic_id: str, data: Sync):
    return service.sync_topic(topic_id, data.version_id)


@app.post("/api/topics/{topic_id}/import", dependencies=WRITE)
def import_work(topic_id: str, data: Import):
    return service.import_topic(topic_id, data.base_version_id)


@app.post("/api/backup", dependencies=WRITE)
def backup():
    return service.backup()


# ---------------------------------------------------------------------------
# 内容管理层（V2-18）
# ---------------------------------------------------------------------------

@app.get("/api/dashboard", dependencies=READ)
def dashboard():
    """运营总览指标。全部来自真实表，无数据时前端显示「暂无数据」。"""
    from . import content
    return content.dashboard()


@app.get("/api/content", dependencies=READ)
def content_list(kind: str = "all", q: str = "", platform: str = "", state: str = "",
                 date_from: str = "", date_to: str = "", sort: str = "updated_at",
                 order: str = "desc", page: int = 1, page_size: int = 20,
                 include_deleted: bool = False, priority: str = "", tag: str = ""):
    """服务端检索：筛选、排序、分页全部下推到 SQL。"""
    from . import content
    return content.list_content(kind=kind, q=q, platform=platform, state=state,
                                date_from=date_from, date_to=date_to, sort=sort, order=order,
                                page=page, page_size=page_size,
                                include_deleted=include_deleted, priority=priority, tag=tag)


@app.get("/api/content/facets", dependencies=READ)
def content_facets():
    """筛选字典与排序选项的真实取值。"""
    from . import content
    return content.content_facets_all()


@app.get("/api/content/{entity_type}/{entity_id}", dependencies=READ)
def content_detail(entity_type: str, entity_id: str):
    """内容详情 + 该对象的操作记录。"""
    from . import content
    return content.get_detail(entity_type, entity_id)


@app.patch("/api/content/topic/{topic_id}/meta", dependencies=WRITE)
def content_topic_meta(topic_id: str, data: ContentMeta):
    """设置选题的优先级、收藏、标签。"""
    from . import content
    return content.update_topic_meta(topic_id, data.model_dump(exclude_none=True))


@app.post("/api/content/topic/{topic_id}/duplicate", dependencies=WRITE)
def content_duplicate(topic_id: str):
    """复制选题。新记录为 pending，不继承批准状态。"""
    from . import content
    return content.duplicate_topic(topic_id)


@app.post("/api/content/topic/{topic_id}/archive", dependencies=WRITE)
def content_archive(topic_id: str, data: BatchArchive):
    from . import content
    return content.set_archived(topic_id, data.archived)


@app.post("/api/content/{entity_type}/{entity_id}/delete", dependencies=WRITE)
def content_delete(entity_type: str, entity_id: str):
    """软删除。移入回收站，可恢复。"""
    from . import content
    return content.soft_delete(entity_type, entity_id)


@app.post("/api/content/{entity_type}/{entity_id}/restore", dependencies=WRITE)
def content_restore(entity_type: str, entity_id: str):
    from . import content
    return content.restore(entity_type, entity_id)


@app.delete("/api/content/{entity_type}/{entity_id}/purge", dependencies=WRITE)
def content_purge(entity_type: str, entity_id: str, confirm: bool = False):
    """永久删除。需 confirm=true 且引用检查通过。"""
    from . import content
    return content.purge(entity_type, entity_id, confirm=confirm)


@app.get("/api/content/{entity_type}/{entity_id}/references", dependencies=READ)
def content_references(entity_type: str, entity_id: str):
    """删除前的引用检查，供前端展示影响范围。"""
    from . import content, db
    with db.connect() as connection:
        blockers = content.reference_blockers(connection, entity_type, entity_id)
    return {"entity_type": entity_type, "entity_id": entity_id,
            "can_purge": not blockers, "blockers": blockers}


@app.post("/api/content/batch", dependencies=WRITE)
def content_batch(data: BatchAction):
    """批量操作。逐条结算，返回每条真实结果。"""
    from . import content
    return content.batch(data.entity_type, data.ids, data.action, data.payload)


@app.get("/api/trash", dependencies=READ)
def trash_list(q: str = "", entity_type: str = "", page: int = 1, page_size: int = 20):
    from . import content
    return content.list_trash(q=q, entity_type=entity_type, page=page, page_size=page_size)


@app.get("/api/audit-logs", dependencies=READ)
def audit_logs(entity_type: str = "", entity_id: str = "", action: str = "",
               result: str = "", limit: int = 100, offset: int = 0):
    from . import content
    return content.list_audit(entity_type=entity_type or None, entity_id=entity_id or None,
                              action=action or None, result=result or None,
                              limit=limit, offset=offset)


@app.get("/{path:path}")
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    root = (PROJECT_ROOT / "frontend" / "dist").resolve()
    candidate = (root / path).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return FileResponse(candidate)
    index = root / "index.html"
    if index.is_file():
        # no-store：升级后浏览器不得继续使用旧 bundle（JS/CSS 带内容哈希，可长缓存）。
        return FileResponse(index, headers={'Cache-Control': 'no-store'})
    return HTMLResponse("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>自动化流程工作台</title><body><h1>自动化流程工作台</h1><p>界面正在准备中，请稍后刷新。</p></body></html>", status_code=503)
