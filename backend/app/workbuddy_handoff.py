"""WorkBuddy 正文人工交接包：导出、过期校验、共用检查与版本导入。

Workbench 没有可虚构的 WorkBuddy 自动 API。本模块明确记录 manual_handoff，
由用户在 WorkBuddy 交互会话中生成 JSON，再导入。任务、平台、材料、规则、
叙事决策和提示词指纹全部核对，过期结果不会冒充本次生成。

两条通道（本地模型 / 人工交接）必须拿到同一份冻结材料，否则同一选题会产出
两种事实基础。人工交接执行端不读文件、不访问网页，所以材料只能在导出时装好：
共享事实底稿与 research.collect 已缓存的原文研究结果都装进快照（与本地通道
同形）；两者都没有时明确拒绝导出，不留一个「只有链接」的包去猜。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException

from . import adapters, db, fact_base, format_decisions, format_plan, material_context, research, service, style_library

REQUEST_KIND = "workbuddy-generation-handoff"
RESULT_KIND = "workbuddy-generation-result"
SCHEMA_VERSION = 1
PROVIDER = "workbuddy:manual-handoff"
#: 导出时既无共享事实底稿、也无已缓存原文研究结果的拒绝文案。人工交接不会
#: 在生成时读原文，静默放行等于让两条通道拿到不同材料。
MATERIAL_GATE_MESSAGE = ("交接包只带冻结材料：本选题既没有共享事实底稿，也没有已缓存的原文研究结果。"
                         "本地自动通道会在生成时读取原文，人工交接不会。请先在选题中心读取原文，"
                         "或建立共享事实底稿后再导出。")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def _task_dir(task_id: str) -> Path:
    service.require(isinstance(task_id, str) and bool(task_id) and "/" not in task_id and "\\" not in task_id,
                    "WorkBuddy 任务标识无效", 422)
    return service.get_settings().runtime_dir / "exports" / "workbuddy-handoffs" / task_id


def _request_path(task_id: str) -> Path:
    return _task_dir(task_id) / "request.json"


def _state_path(task_id: str) -> Path:
    return _task_dir(task_id) / "import.json"


def _result_fingerprint(result: dict) -> str:
    """同一份结果的稳定指纹，用于识别「同一份结果粘贴了两次」。"""
    return hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _load_state(task_id: str) -> dict | None:
    """已导入记录。没有导入过返回 None。"""
    try:
        state = json.loads(_state_path(task_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _studied_material(topic: dict) -> dict | None:
    return material_context.studied_material(topic)


def _snapshot(connection, topic, fact_text: str | None) -> dict:
    """Legacy read-only handoff compatibility wrapper around the public snapshot."""
    return material_context.snapshot(connection, topic, fact_text)


def material_source(snapshot: dict) -> str:
    return material_context.material_source(snapshot)


def build_request(snapshot: dict, task_id: str, created_at: str) -> dict:
    """从冻结快照构造逐平台请求；未选平台样本不会进入任何请求。"""
    requests = []
    for platform in snapshot["platforms"]:
        part = {**snapshot, "platforms": [platform]}
        hint = format_plan.hint_for(snapshot, platform)
        if hint:
            part["_format_hint"] = hint
        prompt = adapters._prompt(part) + adapters.LOCAL_GENERATION_INSTRUCTION
        requests.append({
            "platform": platform,
            "prompt_fingerprint": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt": prompt,
            "response_schema": adapters._schema(),
        })
    return {
        "kind": REQUEST_KIND,
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "topic_id": snapshot["id"],
        "topic_revision": snapshot["revision"],
        "platforms": snapshot["platforms"],
        "provider": PROVIDER,
        "execution": "manual_handoff",
        "created_at": created_at,
        "style_version": style_library.load_library()["version"],
        "style_fingerprint": style_library.fingerprint(),
        "material_fingerprint": adapters.material_fingerprint(snapshot),
        "topic_snapshot": snapshot,
        "requests": requests,
        "response_contract": {
            "kind": RESULT_KIND,
            "required_fields": ["kind", "task_id", "topic_id", "topic_revision",
                                "style_fingerprint", "material_fingerprint",
                                "prompt_fingerprints", "payload"],
            "note": "逐平台生成后合并为一个 payload；每个选定平台恰好一件作品。",
        },
    }


def export_topic(topic_id: str, expected_revision: int) -> dict:
    with db.transaction() as connection:
        topic = service._row(connection, "topics", topic_id)
        service.require(topic["revision"] == expected_revision, "选题范围已变化，请重新批准")
        service.require(topic["status"] == "approved", "请先人工批准选题范围")
        service._require_generation_ready(topic)
        service._require_writable_sources(topic)
        service.require(not connection.execute(
            "SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic_id,)).fetchone(),
            "该选题已有正在排队或运行的任务", 409)
        existing = fact_base.get(topic_id)
        locked = fact_base.lock(connection, topic_id) if existing else None
        snapshot = _snapshot(connection, topic, fact_base.prompt_text(locked) if locked else None)
        base_version_id = topic["current_version_id"]
    # 材料门槛：没有底稿也没有已研究原文时拒绝导出，而不是交出一个只有链接的包。
    service.require(material_source(snapshot) != "none", MATERIAL_GATE_MESSAGE, 422)
    task_id, stamp = service.new_id(), service.now()
    package = build_request(snapshot, task_id, stamp)
    # 导出基线版本：导入时若当前版本已不是它，说明中间可能产生了人工修改，
    # 直接落库会把那份修改悄悄变成历史版本。
    package["base_version_id"] = base_version_id
    package["material_source"] = material_source(snapshot)
    path = _request_path(task_id)
    _atomic_json(path, package)
    return {"package": package, "request_path": str(path),
            "material_source": material_source(snapshot),
            "instructions": "把 request.json 交给 WorkBuddy 交互会话逐平台生成；返回符合 response_contract 的 JSON 后导入。人工交接不是自动 API 调用。"}


def _load_request(task_id: str) -> dict:
    path = _request_path(task_id)
    try:
        package = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("WorkBuddy 交接包不存在或无法读取，请重新导出") from error
    if package.get("kind") != REQUEST_KIND or package.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("WorkBuddy 交接包类型或版本不受支持")
    return package


def validate_result(result: dict, package: dict) -> tuple[dict, list[str]]:
    """只校验，不写库。所有版本和平台不匹配都在导入前明确失败。"""
    if not isinstance(result, dict) or result.get("kind") != RESULT_KIND:
        raise ValueError("WorkBuddy 结果类型不正确")
    for key in ("task_id", "topic_id", "topic_revision", "style_fingerprint", "material_fingerprint"):
        if result.get(key) != package.get(key):
            raise ValueError(f"WorkBuddy 结果的 {key} 与交接包不一致，不能导入过期或其他任务结果")
    if result.get("style_fingerprint") != style_library.fingerprint():
        raise ValueError("文风样本或作者规则已变化，请重新导出 WorkBuddy 任务包")
    snapshot = package["topic_snapshot"]
    if result.get("material_fingerprint") != adapters.material_fingerprint(snapshot):
        raise ValueError("事实材料已变化，请重新导出 WorkBuddy 任务包")
    expected_prompts = {item["platform"]: item["prompt_fingerprint"] for item in package["requests"]}
    current_package = build_request(snapshot, package["task_id"], package["created_at"])
    current_prompts = {item["platform"]: item["prompt_fingerprint"] for item in current_package["requests"]}
    if expected_prompts != current_prompts:
        raise ValueError("工作台生成规则已变化，请重新导出 WorkBuddy 任务包")
    if result.get("prompt_fingerprints") != expected_prompts:
        raise ValueError("生成规则或平台请求已变化，请按最新任务包重新生成")
    payload, advisories = adapters.validate_generated_payload(result.get("payload"), snapshot)
    return payload, advisories


def _require_current_requests(package: dict, current_snapshot: dict, result: dict) -> None:
    """用**当前**材料重算提示词指纹，再和结果比对。

    只看导出包里的旧快照是自证的：那样只能发现代码规则变化，发现不了叙事
    决策、事实底稿或已研究原文的变化 —— material_fingerprint 的投影白名单
    里没有叙事决策，决策改动后材料指纹不变，旧结构稿就能冒充本次生成。
    """
    expected = {item["platform"]: item["prompt_fingerprint"] for item in package["requests"]}
    current = {item["platform"]: item["prompt_fingerprint"]
               for item in build_request(current_snapshot, package["task_id"], package["created_at"])["requests"]}
    service.require(current == expected,
                    "叙事形式决策、事实材料或工作台生成规则已变化，WorkBuddy 结果已过期，请重新导出任务包", 409)
    service.require(result.get("prompt_fingerprints") == current,
                    "WorkBuddy 结果与当前生成请求不一致，请按最新任务包重新生成", 409)


def current_decision_mismatch(connection, topic, snapshot: dict) -> bool:
    """导出后叙事形式决策被改动或撤销。material_fingerprint 看不见这个变化。"""
    return format_decisions.snapshot(topic["id"], connection) != snapshot.get(format_decisions.DECISION_KEY, {})


def import_result(result: dict) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("task_id"), str):
        raise ValueError("WorkBuddy 结果缺少 task_id")
    package = _load_request(result["task_id"])
    payload, advisories = validate_result(result, package)
    snapshot = package["topic_snapshot"]
    task_id = package["task_id"]
    # 叙事形式回填必须在算指纹之前做：它是派生字段，先算会把「同一份结果第二次
    # 提交」判成两份不同结果。这里按导出快照回填，事务内再按当前快照补一次，
    # 两次结果一致（决策若已变化，后面会明确拒绝）。
    format_plan.apply_content_forms(snapshot, payload)
    fingerprint = _result_fingerprint({"task_id": task_id,
                                       "prompt_fingerprints": result.get("prompt_fingerprints"),
                                       "payload": payload})

    # 幂等：同一任务包 + 同一份结果（响应丢失后重试、误粘贴两次）只返回原版本。
    state = _load_state(task_id)
    if state and state.get("version_id"):
        if state.get("result_fingerprint") == fingerprint:
            try:
                version = service.get_version(state["version_id"])
            except HTTPException:  # 版本已被删除进回收站：如实报冲突，不冒充成功
                version = None
            if version:
                return {"version": version, "provider": PROVIDER, "task_id": task_id,
                        "advisories": advisories, "automatic_api": False, "execution": "manual_handoff",
                        "reused": True, "imported_at": state.get("imported_at"),
                        "note": "该任务包已导入过同一份结果，直接返回原版本，未重复建版本。"}
        service.require(False, "该任务包已导入过一份不同的结果；同一任务只能导入一次。"
                               "需要重新生成请重新导出任务包。", 409)

    with db.transaction() as connection:
        topic = service._row(connection, "topics", snapshot["id"])
        service.require(topic["status"] == "approved", "选题当前尚未批准", 409)
        service.require(topic["revision"] == snapshot["revision"], "选题范围已变化，WorkBuddy 结果已过期", 409)
        if "base_version_id" in package and topic["current_version_id"] != package["base_version_id"]:
            service.require(False, "导出后该选题已产生新版本，可能包含人工修改；"
                                   "导入会把它变成历史版本。请重新导出任务包。", 409)
        current_snapshot = _snapshot(connection, topic, None)
        if topic["title"] != snapshot["title"] or topic["brief"] != snapshot["brief"] or current_snapshot["sources"] != snapshot["sources"]:
            raise ValueError("选题事实材料已变化，WorkBuddy 结果已过期")
        current_fact = fact_base.get(topic["id"])
        current_fact_text = fact_base.prompt_text(current_fact) if current_fact else None
        if current_fact_text != snapshot.get("_fact_base"):
            raise ValueError("共享事实底稿已变化，WorkBuddy 结果已过期")
        if current_fact_text:
            current_snapshot["_fact_base"] = current_fact_text
        if current_decision_mismatch(connection, topic, snapshot):
            raise ValueError("叙事形式决策已变化，WorkBuddy 结果已过期，请重新导出任务包")
        if adapters.material_fingerprint(current_snapshot) != adapters.material_fingerprint(snapshot):
            raise ValueError("有效材料已变化，WorkBuddy 结果已过期")
        _require_current_requests(package, current_snapshot, result)
        service.require(not connection.execute(
            "SELECT 1 FROM jobs WHERE topic_id=? AND state IN ('queued','running')", (topic["id"],)).fetchone(),
            "该选题已有正在排队或运行的任务，不能同时导入 WorkBuddy 结果", 409)
        # 与本地通道一致：叙事形式由工作台写入，不靠模型自填。
        format_plan.apply_content_forms(current_snapshot, payload)
        version_id, _ = service._insert_version(connection, topic, payload, PROVIDER)
        service._activity(connection, topic["id"], f"导入 WorkBuddy 人工交接结果；任务 {task_id}，规则与材料指纹已核对")
    _atomic_json(_state_path(task_id), {"task_id": task_id, "topic_id": snapshot["id"],
                                        "result_fingerprint": fingerprint, "version_id": version_id,
                                        "imported_at": service.now()})
    service._register_source_images(topic, version_id, payload)
    service.update_delivery_summary(version_id)
    return {"version": service.get_version(version_id), "provider": PROVIDER,
            "task_id": task_id, "advisories": advisories,
            "automatic_api": False, "execution": "manual_handoff", "reused": False}
