"""Studio Session Service — 统一 session 聚合。

职责：
- GET /studio/session 的核心逻辑：一次返回 workflow、cards、staged edits、workspace、test flow 摘要
- POST /studio/session/init 的初始化逻辑
- 向下兼容旧 schema 的 memo recovery
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill_memo import SkillMemo
from app.services.studio_workflow_protocol import (
    StudioSessionData,
    WorkspaceMode,
)
from app.services import studio_workspace_service
from app.services import studio_test_flow_service

logger = logging.getLogger(__name__)


def get_studio_session(
    db: Session,
    skill_id: int,
) -> dict[str, Any] | None:
    """聚合返回完整 studio session 视图。

    从 skill_memos.memo_payload.workflow_recovery 读取，聚合：
    - workflow_state
    - active_card + cards
    - staged_edits
    - workspace（由 studio_workspace_service 计算）
    - test_flow（由 studio_test_flow_service 聚合）
    - validation_source
    - global_constraints
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = memo.memo_payload or {}
    recovery = payload.get("workflow_recovery") or {}
    workflow_state = recovery.get("workflow_state")
    cards = recovery.get("cards") or []
    staged_edits = recovery.get("staged_edits") or []

    # ── cards 排序 ──
    cards = _sort_cards(cards)

    # ── active card 计算 ──
    active_card_id = _resolve_active_card_id(workflow_state, cards, staged_edits)

    # 找到 active card 对象
    active_card = None
    if active_card_id:
        for card in cards:
            if isinstance(card, dict) and card.get("id") == active_card_id:
                active_card = card
                break

    # ── workspace 计算 ──
    workspace = studio_workspace_service.compute_workspace(
        active_card=active_card,
        cards=cards,
        staged_edits=staged_edits,
        workflow_state=workflow_state if isinstance(workflow_state, dict) else None,
    )

    # ── test flow 概览 ──
    test_flow = studio_test_flow_service.get_test_flow_summary(
        db,
        skill_id,
        workflow_state=workflow_state if isinstance(workflow_state, dict) else None,
    )

    # ── validation source ──
    validation_source = None
    if isinstance(workflow_state, dict):
        metadata = workflow_state.get("metadata") or {}
        validation_source = metadata.get("validation_source")

    # ── global constraints ──
    global_constraints: list[str] = []
    if isinstance(workflow_state, dict):
        metadata = workflow_state.get("metadata") or {}
        gc = metadata.get("global_constraints")
        if isinstance(gc, list):
            global_constraints = gc

    # ── context rollups ──
    context_rollups = payload.get("context_rollups") or []

    # ── blueprint ──
    blueprint = payload.get("blueprint")

    # ── card order ──
    card_order = [c["id"] for c in cards if isinstance(c, dict) and c.get("id")]

    # ── progress log ──
    progress_log = payload.get("progress_log") or []

    # ── 动态 status_summary ──
    status_summary = _compute_status_summary(cards, memo.status_summary)

    session_data = StudioSessionData(
        skill_id=skill_id,
        workflow_state=workflow_state if isinstance(workflow_state, dict) else None,
        active_card_id=active_card_id,
        cards=cards,
        staged_edits=staged_edits,
        workspace=workspace,
        test_flow=test_flow,
        validation_source=validation_source,
        global_constraints=global_constraints,
        recovery_revision=_safe_int(recovery.get("revision")),
        recovery_updated_at=recovery.get("updated_at"),
        memo_version=memo.version or 0,
        lifecycle_stage=memo.lifecycle_stage or "analysis",
        status_summary=status_summary,
        context_rollups=context_rollups,
        blueprint=blueprint,
        card_order=card_order,
        progress_log=progress_log,
    )

    return session_data.to_dict()


def init_studio_session(
    db: Session,
    skill_id: int,
    *,
    session_mode: str = "optimize",
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """初始化 studio session（如果 memo 不存在则创建）。

    与 skill_memo_service.init_memo 不冲突：
    - init_memo 负责创建 memo + 初始化任务
    - init_studio_session 在 memo 已存在时补齐 workflow_recovery 结构
    """
    from app.services import skill_memo_service

    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        # 使用 skill_memo_service 创建
        result = skill_memo_service.init_memo(
            db,
            skill_id,
            scenario_type=_session_mode_to_scenario(session_mode),
            goal_summary=None,
            user_id=user_id or 0,
        )
        if not result:
            return None
        db.flush()

    return get_studio_session(db, skill_id)


# ── 内部工具 ──────────────────────────────────────────────────────────────────

def _resolve_active_card_id(
    workflow_state: Any,
    cards: list[dict[str, Any]],
    staged_edits: list[dict[str, Any]] | None = None,
) -> str | None:
    """计算 active card id：优先 workflow_state 显式声明，否则找第一张 active 状态卡片。

    回退优先级：
    1. workflow_state.active_card_id（显式声明，确认卡片存在）
    2. 第一张 status=active 的卡片
    3. 第一张 pending/queued 的卡片
    4. 从 pending staged edit 的 origin_card_id 反查对应卡片
    """
    if isinstance(workflow_state, dict):
        explicit = workflow_state.get("active_card_id")
        if explicit:
            for card in cards:
                if isinstance(card, dict) and card.get("id") == explicit:
                    return explicit

    # fallback: 找第一张 status=active 的卡片
    for card in cards:
        if isinstance(card, dict) and card.get("status") == "active":
            return card.get("id")

    # 再 fallback: 找第一张 pending 卡片
    for card in cards:
        if isinstance(card, dict) and card.get("status") in ("pending", "queued"):
            return card.get("id")

    # 最终 fallback: 从 pending staged edit 的 origin_card_id 反查
    if staged_edits:
        card_ids = {c.get("id") for c in cards if isinstance(c, dict)}
        for edit in (staged_edits or []):
            if not isinstance(edit, dict):
                continue
            if edit.get("status") not in ("pending", "reviewing"):
                continue
            origin = edit.get("origin_card_id")
            if origin and origin in card_ids:
                return origin

    return None


def _session_mode_to_scenario(session_mode: str) -> str:
    mapping = {
        "create": "new_skill_creation",
        "optimize": "published_iteration",
        "audit": "import_remediation",
    }
    return mapping.get(session_mode, "published_iteration")


def _sort_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (priority, created_at) 排序：high > medium > low，同级按创建时间升序。"""
    priority_order = {"high": 0, "p0": 0, "medium": 1, "p1": 1, "low": 2, "p2": 2}
    return sorted(
        cards,
        key=lambda c: (
            priority_order.get(c.get("priority", "medium") if isinstance(c, dict) else "medium", 1),
            (c.get("created_at", "") if isinstance(c, dict) else ""),
        ),
    )


def _compute_status_summary(cards: list[dict[str, Any]], fallback: str | None) -> str:
    """基于卡片状态分布动态生成 status_summary。"""
    if not cards:
        return fallback or ""

    total = len(cards)
    done_statuses = {"accepted", "applied", "validated", "adopted", "rejected"}
    done = sum(1 for c in cards if isinstance(c, dict) and c.get("status") in done_statuses)

    active_card = None
    for c in cards:
        if isinstance(c, dict) and c.get("status") in ("active", "drafting", "reviewing"):
            active_card = c
            break

    if done == total:
        return f"全部 {total} 张卡片已处理完成"

    parts = [f"{done}/{total} 已完成"]
    if active_card:
        title = active_card.get("title", "")
        if len(title) > 30:
            title = title[:30] + "…"
        parts.append(f"当前: {title}")

    return "，".join(parts)


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
