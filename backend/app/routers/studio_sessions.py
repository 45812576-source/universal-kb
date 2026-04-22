"""Studio Sessions API — Skill Studio 统一架构 facade 路由。

提供：
- GET  /api/skills/{skill_id}/studio/session — 聚合 session 视图
- POST /api/skills/{skill_id}/studio/session/init — 初始化 session

- POST /api/skills/{skill_id}/studio/cards/{card_id}/activate — 激活卡片
- POST /api/skills/{skill_id}/studio/cards/{card_id}/append-context — 追加上下文
- POST /api/skills/{skill_id}/studio/cards/{card_id}/decision — 用户决策
- POST /api/skills/{skill_id}/studio/cards/{card_id}/handoff — 卡片交接（M4）
- POST /api/skills/{skill_id}/studio/cards/{card_id}/bind-back — 外部编辑回绑（M4）
- POST /api/skills/{skill_id}/studio/global-constraints — 更新全局约束

- POST /api/skills/{skill_id}/studio/test-flow/resolve-entry — test flow 入口解析
- GET  /api/test-flow/run-links/{sandbox_session_id} — 查询 run link
- GET  /api/skills/{skill_id}/studio/test-flow/run-links — 查询 skill run links
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services import studio_session_service
from app.services import studio_card_service
from app.services import studio_test_flow_service
from app.services.skill_memo_service import OptimisticLockError
from app.services.studio_workflow_protocol import StudioEventTypes

logger = logging.getLogger(__name__)

router = APIRouter(tags=["studio_sessions"])


def _emit(db: Session, event_type: str, skill_id: int, user_id: int, payload: dict[str, Any] | None = None) -> None:
    """发射 studio 事件 — 必须在主事务 commit 之后调用。"""
    try:
        from app.services import event_bus
        event_bus.emit(
            db,
            event_type=event_type,
            source_type="studio",
            source_id=skill_id,
            payload=payload or {},
            user_id=user_id,
        )
    except Exception:
        logger.warning("Studio event emit failed: %s", event_type, exc_info=True)


# ── Request Models ───────────────────────────────────────────────────────────

class SessionInitRequest(BaseModel):
    session_mode: str = "optimize"  # create / optimize / audit


class AppendContextRequest(BaseModel):
    type: str = "user_comment"
    content: str
    source: str = "user"


class CardDecisionRequest(BaseModel):
    decision: str  # accept / reject / revise / pause
    reason: Optional[str] = None


class CreateCardRequest(BaseModel):
    card_type: str = "governance"  # architect / governance / validation
    title: str
    summary: str = ""
    phase: Optional[str] = None
    priority: str = "medium"
    target_file: Optional[str] = None
    origin: str = "user_request"
    activate: bool = False


class HandoffRequest(BaseModel):
    target_role: str  # tool / external_build / etc.
    target_file: Optional[str] = None
    handoff_policy: str = "open_development_studio"  # 仅限外部 handoff 策略
    route_kind: str = "external"
    destination: Optional[str] = None
    return_to: str = "bind_back"
    summary: str = ""
    handoff_summary: Optional[str] = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    activate_target: bool = True


class BindBackRequest(BaseModel):
    source: str = "external_edit_returned"
    summary: str = ""
    required_checks: list[str] = Field(default_factory=list)


class GlobalConstraintsRequest(BaseModel):
    constraints: list[str]
    mode: str = "replace"  # replace / append


# ── Session Endpoints ────────────────────────────────────────────────────────

@router.get("/api/skills/{skill_id}/studio/session")
def get_session(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """聚合返回完整 studio session 视图。"""
    result = studio_session_service.get_studio_session(db, skill_id)
    if not result:
        raise HTTPException(status_code=404, detail="Skill memo 不存在")
    return result


@router.post("/api/skills/{skill_id}/studio/session/init")
def init_session(
    skill_id: int,
    req: SessionInitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """初始化或恢复 studio session。"""
    result = studio_session_service.init_studio_session(
        db,
        skill_id,
        session_mode=req.session_mode,
        user_id=user.id,
    )
    if not result:
        raise HTTPException(status_code=500, detail="Session 初始化失败")
    db.commit()
    _emit(db, StudioEventTypes.MEMO_INITIALIZED, skill_id, user.id, {"session_mode": req.session_mode})
    return result


# ── Card Endpoints ───────────────────────────────────────────────────────────

@router.post("/api/skills/{skill_id}/studio/cards")
def create_card(
    skill_id: int,
    req: CreateCardRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建新卡片（chat 路由层 / 用户手动创建）。"""
    try:
        result = studio_card_service.create_card(
            db,
            skill_id,
            card_type=req.card_type,
            title=req.title,
            summary=req.summary,
            phase=req.phase,
            priority=req.priority,
            target_file=req.target_file,
            origin=req.origin,
            activate=req.activate,
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.CARD_CREATED, skill_id, user.id, {
        "card_id": result.get("card_id"), "card_type": req.card_type, "activated": req.activate,
    })
    return result


@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/pause")
def pause_card(
    skill_id: int,
    card_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """挂起卡片。"""
    try:
        result = studio_card_service.pause_card(db, skill_id, card_id, user_id=user.id)
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.CARD_PAUSED, skill_id, user.id, {"card_id": card_id})
    return result


@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/activate")
def activate_card(
    skill_id: int,
    card_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """切换 active card。"""
    try:
        result = studio_card_service.activate_card(db, skill_id, card_id, user_id=user.id)
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.CARD_ACTIVATED, skill_id, user.id, {"card_id": card_id})
    return result


@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/append-context")
def append_context(
    skill_id: int,
    card_id: str,
    req: AppendContextRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """向卡片追加上下文条目。"""
    try:
        result = studio_card_service.append_card_context(
            db,
            skill_id,
            card_id,
            context_entry=req.model_dump(),
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.CARD_CONTEXT_APPENDED, skill_id, user.id, {"card_id": card_id})
    return result


@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/decision")
def card_decision(
    skill_id: int,
    card_id: str,
    req: CardDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录用户对卡片的决策。"""
    try:
        result = studio_card_service.card_decision(
            db,
            skill_id,
            card_id,
            decision=req.decision,
            reason=req.reason,
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.USER_DECISION_RECORDED, skill_id, user.id, {
        "card_id": card_id, "decision": req.decision,
    })

    # M5 B12: 传播 confirm 卡产生的 card_status_events
    for cse in (result.get("card_status_events") or []):
        _emit(db, StudioEventTypes.CARD_UPDATED, skill_id, user.id, cse)

    return result


# ── Handoff / Bind-back ──────────────────────────────────────────────────────

@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/handoff")
def handoff_card(
    skill_id: int,
    card_id: str,
    req: HandoffRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从当前卡片派生衍生卡并执行交接。"""
    try:
        result = studio_card_service.handoff_card(
            db, skill_id, card_id,
            target_role=req.target_role,
            target_file=req.target_file,
            handoff_policy=req.handoff_policy,
            summary=req.summary,
            handoff_summary=req.handoff_summary,
            acceptance_criteria=req.acceptance_criteria,
            activate_target=req.activate_target,
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, "handoff_created", skill_id, user.id, {
        "card_id": card_id,
        "derived_card_id": result.get("derived_card_id"),
        "handoff_policy": req.handoff_policy,
    })
    return result


@router.post("/api/skills/{skill_id}/studio/cards/{card_id}/bind-back")
def bind_back_card(
    skill_id: int,
    card_id: str,
    req: BindBackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """外部编辑完成后回绑到卡片。"""
    try:
        result = studio_card_service.bind_back_card(
            db, skill_id, card_id,
            source=req.source,
            summary=req.summary,
            required_checks=req.required_checks,
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, "external_edit_returned", skill_id, user.id, {
        "card_id": card_id,
        "source": req.source,
    })
    return result


# ── Global Constraints ───────────────────────────────────────────────────────

@router.post("/api/skills/{skill_id}/studio/global-constraints")
def update_global_constraints(
    skill_id: int,
    req: GlobalConstraintsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新全局约束条件。"""
    try:
        result = studio_card_service.update_global_constraints(
            db,
            skill_id,
            constraints=req.constraints,
            mode=req.mode,
            user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.GLOBAL_CONSTRAINTS_UPDATED, skill_id, user.id, {
        "constraints": req.constraints, "mode": req.mode,
    })
    return result


# ── Workspace & Staged Changes ───────────────────────────────────────────────

@router.get("/api/skills/{skill_id}/studio/workspace")
def get_workspace(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取当前 workspace 状态（后端决策结果）。"""
    session = studio_session_service.get_studio_session(db, skill_id)
    if not session:
        raise HTTPException(status_code=404, detail="Skill memo 不存在")
    return session.get("workspace") or {}


class StagedChangeDecisionRequest(BaseModel):
    decision: str  # accept / reject


@router.post("/api/skills/{skill_id}/studio/staged-changes/{staged_edit_id}/accept")
def accept_staged_change(
    skill_id: int,
    staged_edit_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接受单个 staged change。"""
    try:
        result = studio_card_service.staged_change_decision(
            db, skill_id, staged_edit_id, decision="accept", user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.STAGED_CHANGE_UPDATED, skill_id, user.id, {
        "staged_edit_id": staged_edit_id, "decision": "accept",
    })
    return result


@router.post("/api/skills/{skill_id}/studio/staged-changes/{staged_edit_id}/reject")
def reject_staged_change(
    skill_id: int,
    staged_edit_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """拒绝单个 staged change。"""
    try:
        result = studio_card_service.staged_change_decision(
            db, skill_id, staged_edit_id, decision="reject", user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.STAGED_CHANGE_UPDATED, skill_id, user.id, {
        "staged_edit_id": staged_edit_id, "decision": "reject",
    })
    return result


# ── Blueprint ────────────────────────────────────────────────────────────────

@router.get("/api/skills/{skill_id}/studio/blueprint")
def get_blueprint(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取 Skill Blueprint。"""
    from app.services import studio_blueprint_service
    result = studio_blueprint_service.get_blueprint(db, skill_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Blueprint 不存在")
    return result


class SaveBlueprintRequest(BaseModel):
    blueprint: dict[str, Any]


@router.post("/api/skills/{skill_id}/studio/blueprint")
def save_blueprint(
    skill_id: int,
    req: SaveBlueprintRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存 / 更新 Blueprint。"""
    from app.services import studio_blueprint_service
    try:
        result = studio_blueprint_service.save_blueprint(
            db, skill_id, blueprint=req.blueprint, user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.BLUEPRINT_UPDATED, skill_id, user.id, {})
    return result


@router.post("/api/skills/{skill_id}/studio/blueprint/compile-governance-cards")
def compile_governance_cards(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从 Blueprint 自动编译治理执行卡片。"""
    from app.services import studio_blueprint_service
    try:
        result = studio_blueprint_service.compile_governance_cards(
            db, skill_id, user_id=user.id,
        )
    except OptimisticLockError:
        raise HTTPException(status_code=409, detail="并发写入冲突，请重试")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    db.commit()
    _emit(db, StudioEventTypes.PHASE_CHANGED, skill_id, user.id, {
        "phase": "governance_execution", "generated_count": result.get("generated_count"),
    })
    return result


# ── Test Flow Resolve Entry ──────────────────────────────────────────────────

class ResolveEntryRequest(BaseModel):
    content: str
    mentioned_skill_ids: list[int] = Field(default_factory=list)
    candidate_skills: list[dict[str, Any]] = Field(default_factory=list)
    entry_source: str = "studio"
    conversation_id: Optional[int] = None
    auto_create_card: bool = False


@router.post("/api/skills/{skill_id}/studio/test-flow/resolve-entry")
def resolve_test_flow_entry(
    skill_id: int,
    req: ResolveEntryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """统一 test flow 入口解析（后端化）。"""
    result = studio_test_flow_service.resolve_entry(
        db,
        skill_id,
        content=req.content,
        mentioned_skill_ids=req.mentioned_skill_ids or None,
        candidate_skills=req.candidate_skills or None,
        entry_source=req.entry_source,
        conversation_id=req.conversation_id,
        auto_create_card=req.auto_create_card,
    )
    if req.auto_create_card and result.get("auto_created_card_id"):
        db.commit()
    return result


# ── Test Flow Run Links ──────────────────────────────────────────────────────

@router.get("/api/test-flow/run-links/{sandbox_session_id}")
def get_run_link(
    sandbox_session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """根据 sandbox session id 查询 run link。"""
    result = studio_test_flow_service.get_run_links_by_session(db, sandbox_session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Run link 不存在")
    return result


@router.get("/api/skills/{skill_id}/studio/test-flow/run-links")
def get_skill_run_links(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取 skill 的所有 run links。"""
    return studio_test_flow_service.get_run_links_by_skill(db, skill_id)


# ── Card Contract API (Phase B6) ─────────────────────────────────────────────

@router.get("/api/studio/card-contracts")
def list_card_contracts(
    user: User = Depends(get_current_user),
):
    """获取所有 card contract 摘要 — 后端 canonical owner。"""
    from app.services import studio_card_contract_service
    return {"contracts": studio_card_contract_service.get_all_contract_summaries()}


@router.get("/api/studio/card-contracts/{contract_id}")
def get_card_contract(
    contract_id: str,
    user: User = Depends(get_current_user),
):
    """获取单个 card contract 详情。"""
    from app.services import studio_card_contract_service
    contract = studio_card_contract_service.get_contract(contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id} 不存在")
    return {"contract": contract.to_dict()}


# ── Timeline API (Phase B11) ────────────────────────────────────────────────

@router.get("/api/skills/{skill_id}/studio/timeline")
def get_timeline(
    skill_id: int,
    mode: str = "fast",
    after_sequence: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取 studio 时间线 — fast 或 deep 模式。"""
    from app.services import studio_timeline_service
    if mode == "deep":
        return studio_timeline_service.get_deep_timeline(db, skill_id)
    return studio_timeline_service.get_fast_timeline(
        db, skill_id, after_sequence=after_sequence,
    )


@router.get("/api/studio/runs/{run_id}/timeline")
def get_run_timeline(
    run_id: str,
    after_sequence: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取单个 run 的完整事件时间线。"""
    from app.services import studio_timeline_service
    return studio_timeline_service.get_run_timeline(
        db, run_id, after_sequence=after_sequence,
    )
