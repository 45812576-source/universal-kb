"""Studio Card Service — 统一卡片生命周期管理。

职责：
- active card 切换（activate / pause）
- card context append
- card decision（accept / reject / revise / pause）
- global constraints 更新
- staged edit 与 card 关联
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill_memo import SkillMemo
from app.services.studio_workflow_protocol import (
    CardStatus,
    StudioEventTypes,
    WorkspaceMode,
    _new_id,
    _now_iso,
)
from app.services import studio_workspace_service

logger = logging.getLogger(__name__)


def create_card(
    db: Session,
    skill_id: int,
    *,
    card_type: str = "governance",
    title: str,
    summary: str = "",
    phase: str | None = None,
    priority: str = "medium",
    target_file: str | None = None,
    origin: str = "user_request",
    activate: bool = False,
    user_id: int | None = None,
) -> dict[str, Any]:
    """创建新卡片并追加到 recovery.cards。

    如果 activate=True，同时激活新卡；否则放入队列（queued）。
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    # 推断 phase
    if not phase:
        phase = workflow_state.get("phase") or "governance_execution"

    # 推断 workspace_mode
    workspace_mode = _infer_workspace_mode({"type": card_type, "card_type": card_type})
    file_role = _infer_file_role(target_file, None, None)
    handoff_policy = _infer_handoff_policy(file_role, workspace_mode, target_file)

    card_id = _new_id("card")
    new_card: dict[str, Any] = {
        "id": card_id,
        "workflow_id": workflow_state.get("workflow_id"),
        "source": "studio",
        "type": card_type,
        "card_type": card_type,
        "phase": phase,
        "title": title,
        "summary": summary,
        "status": CardStatus.ACTIVE if activate else CardStatus.QUEUED,
        "priority": priority,
        "workspace_mode": workspace_mode,
        "target_file": target_file,
        "file_role": file_role,
        "handoff_policy": handoff_policy,
        "origin": origin,
        "target": {},
        "actions": [],
        "content": {
            "summary": summary,
            **({"file_role": file_role} if file_role else {}),
            **({"handoff_policy": handoff_policy} if handoff_policy else {}),
        },
        "created_at": _now_iso(),
    }

    if activate:
        # 暂停当前 active card
        old_active_id = workflow_state.get("active_card_id")
        if old_active_id:
            for card in cards:
                if isinstance(card, dict) and card.get("id") == old_active_id:
                    if card.get("status") in (CardStatus.ACTIVE, CardStatus.DRAFTING):
                        card["status"] = CardStatus.PAUSED
        workflow_state["active_card_id"] = card_id
        workflow_state["workspace_mode"] = workspace_mode
        _ensure_unified_flag(workflow_state)

    cards.append(new_card)
    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "card_id": card_id,
        "card": new_card,
        "activated": activate,
        "recovery_revision": recovery.get("revision", 0),
    }


def pause_card(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """挂起指定卡片。如果它是 active card 则清除 active_card_id。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    target_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            target_card = card
            break

    if not target_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    if target_card.get("status") not in (CardStatus.ACTIVE, CardStatus.DRAFTING, CardStatus.REVIEWING):
        return _err("invalid_status", f"当前状态 {target_card.get('status')} 不可挂起")

    target_card["status"] = CardStatus.PAUSED

    # 如果是 active card 则清除
    if workflow_state.get("active_card_id") == card_id:
        workflow_state["active_card_id"] = None

    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "card_id": card_id,
        "new_status": CardStatus.PAUSED,
        "recovery_revision": recovery.get("revision", 0),
    }


def activate_card(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """切换 active card，更新 workspace，回写 recovery。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    # 查找目标卡片
    target_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            target_card = card
            break

    if not target_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    # 暂停当前 active card
    old_active_id = workflow_state.get("active_card_id")
    if old_active_id and old_active_id != card_id:
        for card in cards:
            if isinstance(card, dict) and card.get("id") == old_active_id:
                if card.get("status") in (CardStatus.ACTIVE, CardStatus.DRAFTING):
                    card["status"] = CardStatus.PAUSED

    # 激活目标卡片
    if target_card.get("status") in (CardStatus.PENDING, CardStatus.QUEUED, CardStatus.PAUSED):
        target_card["status"] = CardStatus.ACTIVE

    # 更新 workflow state
    workflow_state["active_card_id"] = card_id
    workspace_mode = target_card.get("workspace_mode") or _infer_workspace_mode(target_card)
    workflow_state["workspace_mode"] = workspace_mode
    # 标记统一架构模式 — 后续 _refresh / sync 检测用
    _ensure_unified_flag(workflow_state)

    # 计算新 workspace
    workspace = studio_workspace_service.compute_workspace(
        active_card=target_card,
        cards=cards,
        staged_edits=recovery.get("staged_edits", []),
        workflow_state=workflow_state,
    )

    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "card_id": card_id,
        "active_card": target_card,
        "workspace": workspace,
        "workflow_state_patch": {
            "active_card_id": card_id,
            "workspace_mode": workspace_mode,
        },
        "recovery_revision": recovery.get("revision", 0),
    }


def append_card_context(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    context_entry: dict[str, Any],
    user_id: int | None = None,
) -> dict[str, Any]:
    """向卡片追加上下文条目（用户补充意见、跨上下文迁移等）。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []

    target_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            target_card = card
            break

    if not target_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    # 追加 context
    content = target_card.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    card_context = content.get("card_context") or []
    card_context.append({
        "type": context_entry.get("type", "user_comment"),
        "content": context_entry.get("content", ""),
        "source": context_entry.get("source", "user"),
        "created_at": _now_iso(),
    })
    content["card_context"] = card_context
    target_card["content"] = content

    recovery["cards"] = cards
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "card_id": card_id,
        "context_count": len(card_context),
        "recovery_revision": recovery.get("revision", 0),
    }


def card_decision(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    decision: str,
    reason: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """记录用户对卡片的决策：accept / reject / revise / pause。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}
    staged_edits = recovery.get("staged_edits") or []

    target_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            target_card = card
            break

    if not target_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    # 决策映射
    decision_status_map = {
        "accept": CardStatus.ACCEPTED,
        "reject": CardStatus.REJECTED,
        "revise": CardStatus.REVISION_NEEDED,
        "pause": CardStatus.PAUSED,
    }
    new_status = decision_status_map.get(decision)
    if not new_status:
        return _err("invalid_decision", f"不支持的决策类型: {decision}")

    target_card["status"] = new_status
    target_card["content"] = target_card.get("content") or {}
    target_card["content"]["user_decision"] = {
        "decision": decision,
        "reason": reason,
        "user_id": user_id,
        "decided_at": _now_iso(),
    }

    # 维持统一架构标志 — 即使所有卡片处理完 active_card_id=None 也不清除
    _ensure_unified_flag(workflow_state)

    # accept/reject 时通过既有域服务落盘 + 同步 memo tasks
    state_patch: dict[str, Any] = {"card_status": new_status}
    db_side_effect_ids: list[str] = []  # 实际执行了 DB adopt/reject 的 staged_edit_id

    if decision in ("accept", "reject"):
        from app.services.skill_memo_service import patch_workflow_recovery_action

        for edit in staged_edits:
            if not isinstance(edit, dict) or edit.get("origin_card_id") != card_id:
                continue
            if edit.get("status") not in ("pending", "reviewing"):
                continue

            edit_id = edit.get("id")
            db_edit_id = _extract_db_edit_id(edit_id)

            if db_edit_id and decision == "accept":
                try:
                    from app.services.studio_governance import adopt_staged_edit
                    adopt_staged_edit(db, db_edit_id, user_id or 0)
                except Exception:
                    logger.warning("DB adopt_staged_edit(%s) failed, JSON-only fallback", db_edit_id, exc_info=True)

            if db_edit_id and decision == "reject":
                try:
                    from app.services.studio_governance import reject_staged_edit
                    reject_staged_edit(db, db_edit_id, user_id or 0)
                except Exception:
                    logger.warning("DB reject_staged_edit(%s) failed, JSON-only fallback", db_edit_id, exc_info=True)

            target_status = "accepted" if decision == "accept" else "rejected"
            edit["status"] = target_status
            db_side_effect_ids.append(str(edit_id))

    if decision == "accept":
        # 自动推进到下一张卡
        next_card = _find_next_pending_card(cards, card_id)
        if next_card:
            next_card["status"] = CardStatus.ACTIVE
            workflow_state["active_card_id"] = next_card["id"]
            workspace_mode = next_card.get("workspace_mode") or _infer_workspace_mode(next_card)
            workflow_state["workspace_mode"] = workspace_mode
            state_patch["active_card_id"] = next_card["id"]
            state_patch["workspace_mode"] = workspace_mode
        else:
            # 所有卡片已处理完
            workflow_state["active_card_id"] = None
            workflow_state["next_action"] = "recommend_test"
            state_patch["active_card_id"] = None
            state_patch["next_action"] = "recommend_test"

    # 记录到 progress_log
    progress_log = payload.get("progress_log") or []
    progress_log.append({
        "type": "card_decision",
        "card_id": card_id,
        "decision": decision,
        "reason": reason,
        "timestamp": _now_iso(),
    })
    payload["progress_log"] = progress_log

    # M4 fix: 写入 card_exit_log — 完整记录用户决策事件
    if decision in ("accept", "reject"):
        exit_log = recovery.get("card_exit_log") or []
        card_kind = target_card.get("kind", "")
        event_type = "user_decision"
        if card_kind == "validation":
            event_type = "validation_linked"
        elif target_card.get("handoff_policy") == "open_development_studio":
            event_type = "external_edit_opened"
        exit_log.append({
            "event": event_type,
            "card_id": card_id,
            "decision": decision,
            "new_status": new_status,
            "reason": reason,
            "timestamp": _now_iso(),
        })
        recovery["card_exit_log"] = exit_log

    # M5 B12: confirm 卡 decision 后处理 — 闭合外部 handoff 回路
    confirm_card_status_events: list[dict[str, Any]] = []
    card_kind = target_card.get("kind", "")
    card_origin = target_card.get("origin", "")

    if card_kind == "confirm" and card_origin == "bind_back":
        source_card_id = (target_card.get("content") or {}).get("source_card_id")

        if decision == "accept":
            target_card["status"] = CardStatus.ACCEPTED

            if source_card_id:
                for card in cards:
                    if isinstance(card, dict) and card.get("id") == source_card_id:
                        card["external_state"] = "completed"
                        card["status"] = CardStatus.VALIDATED
                        confirm_card_status_events.append({
                            "card_id": source_card_id,
                            "new_status": "validated",
                            "external_state": "completed",
                            "reason": "confirm_accepted",
                        })
                        break

            validate_card_id = (target_card.get("content") or {}).get("validate_card_id")
            if validate_card_id:
                for card in cards:
                    if isinstance(card, dict) and card.get("id") == validate_card_id:
                        card["status"] = CardStatus.ACTIVE
                        confirm_card_status_events.append({
                            "card_id": validate_card_id,
                            "new_status": "active",
                            "reason": "confirm_accepted_activate_validation",
                        })
                        break
            else:
                _complete_confirm_card(recovery, card_id, source_card_id)

        elif decision == "reject":
            target_card["status"] = CardStatus.REJECTED

            if source_card_id:
                for card in cards:
                    if isinstance(card, dict) and card.get("id") == source_card_id:
                        card["external_state"] = None
                        card["status"] = CardStatus.ACTIVE
                        confirm_card_status_events.append({
                            "card_id": source_card_id,
                            "new_status": "active",
                            "external_state": None,
                            "reason": "confirm_rejected_restored",
                        })
                        break

    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    recovery["staged_edits"] = staged_edits
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery

    # 同步 memo tasks 双向联动（card adopt/reject → task done/skipped）
    if decision in ("accept", "reject") and db_side_effect_ids:
        from app.services.skill_memo_service import sync_tasks_from_workflow_action

        target_status = "adopted" if decision == "accept" else "rejected"
        sync_tasks_from_workflow_action(
            payload,
            card_id=card_id,
            staged_edit_id=db_side_effect_ids[0] if db_side_effect_ids else None,
            updated_card_status=target_status,
            updated_staged_edit_status=target_status,
            user_id=user_id,
        )

    _save(db, memo, payload, user_id)

    # 计算新 workspace
    active_card_id = workflow_state.get("active_card_id")
    active_card = None
    if active_card_id:
        for card in cards:
            if isinstance(card, dict) and card.get("id") == active_card_id:
                active_card = card
                break

    workspace = studio_workspace_service.compute_workspace(
        active_card=active_card,
        cards=cards,
        staged_edits=staged_edits,
        workflow_state=workflow_state,
    )

    # M4 B6: 返回 card_status_events 供调用方 emit SSE
    card_status_events: list[dict[str, Any]] = [{
        "card_id": card_id,
        "new_status": new_status,
        "reason": f"user_decision:{decision}",
    }]
    if decision == "accept" and state_patch.get("active_card_id"):
        card_status_events.append({
            "card_id": state_patch["active_card_id"],
            "new_status": CardStatus.ACTIVE,
            "reason": "auto_activated_after_accept",
        })

    # M5 B12: 合并 confirm 卡产生的额外状态事件
    card_status_events.extend(confirm_card_status_events)

    return {
        "ok": True,
        "card_id": card_id,
        "decision": decision,
        "new_card_status": new_status,
        "workflow_state_patch": state_patch,
        "workspace": workspace,
        "recovery_revision": recovery.get("revision", 0),
        "memo_refresh_required": decision in ("accept", "reject"),
        "card_status_events": card_status_events,
    }


def _complete_confirm_card(
    recovery: dict[str, Any],
    confirm_card_id: str | None,
    source_card_id: str | None,
) -> None:
    """M5 B12: confirm 卡完成后清理 — 将 confirm 和 source 加入 completed_card_ids。"""
    completed = recovery.setdefault("completed_card_ids", [])
    if confirm_card_id and confirm_card_id not in completed:
        completed.append(confirm_card_id)
    if source_card_id and source_card_id not in completed:
        completed.append(source_card_id)


def update_global_constraints(
    db: Session,
    skill_id: int,
    *,
    constraints: list[str],
    mode: str = "replace",
    user_id: int | None = None,
) -> dict[str, Any]:
    """更新全局约束条件。mode: replace / append。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    workflow_state = recovery.get("workflow_state") or {}

    metadata = workflow_state.get("metadata") or {}
    existing = metadata.get("global_constraints") or []

    if mode == "append":
        updated = existing + constraints
    else:
        updated = constraints

    metadata["global_constraints"] = updated
    workflow_state["metadata"] = metadata
    recovery["workflow_state"] = workflow_state
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "global_constraints": updated,
        "recovery_revision": recovery.get("revision", 0),
    }


def staged_change_decision(
    db: Session,
    skill_id: int,
    staged_edit_id: str,
    *,
    decision: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    """对单个 staged change 做 accept / reject。

    允许前端在卡片之外直接操作单个 staged change（部分接受场景）。
    """
    if decision not in ("accept", "reject"):
        return _err("invalid_decision", f"不支持: {decision}")

    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    staged_edits = recovery.get("staged_edits") or []

    target_edit = None
    for edit in staged_edits:
        if isinstance(edit, dict) and str(edit.get("id")) == str(staged_edit_id):
            target_edit = edit
            break

    if not target_edit:
        return _err("edit_not_found", f"Staged edit {staged_edit_id} 不存在")

    # DB 层操作
    db_edit_id = _extract_db_edit_id(staged_edit_id)
    if db_edit_id:
        try:
            if decision == "accept":
                from app.services.studio_governance import adopt_staged_edit
                adopt_staged_edit(db, db_edit_id, user_id or 0)
            else:
                from app.services.studio_governance import reject_staged_edit
                reject_staged_edit(db, db_edit_id, user_id or 0)
        except Exception:
            logger.warning("DB %s staged_edit(%s) failed", decision, db_edit_id, exc_info=True)

    target_status = "accepted" if decision == "accept" else "rejected"
    target_edit["status"] = target_status

    recovery["staged_edits"] = staged_edits
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    # 计算 workspace — 让前端同步
    workflow_state = recovery.get("workflow_state") or {}
    active_card_id = workflow_state.get("active_card_id")
    active_card = None
    if active_card_id:
        cards = recovery.get("cards") or []
        for card in cards:
            if isinstance(card, dict) and card.get("id") == active_card_id:
                active_card = card
                break

    workspace = studio_workspace_service.compute_workspace(
        active_card=active_card,
        cards=recovery.get("cards") or [],
        staged_edits=staged_edits,
        workflow_state=workflow_state,
    )

    # M4 B6: 检查 origin card 的所有 staged_edit 是否全部 resolved
    card_status_events: list[dict[str, Any]] = []
    origin_card_id = target_edit.get("origin_card_id")
    if origin_card_id:
        completion = check_card_completion_after_edit(
            recovery.get("cards") or [],
            staged_edits,
            origin_card_id,
        )
        if completion:
            card_status_events.append(completion)

    return {
        "ok": True,
        "staged_edit_id": staged_edit_id,
        "decision": decision,
        "new_status": target_status,
        "workspace": workspace,
        "recovery_revision": recovery.get("revision", 0),
        "card_status_events": card_status_events,
    }


def check_card_completion_after_edit(
    cards: list[dict[str, Any]],
    staged_edits: list[dict[str, Any]],
    origin_card_id: str,
) -> dict[str, Any] | None:
    """检查卡片关联的所有 staged_edit 是否都已 resolved。

    如果全部 resolved，返回建议的 card_status_patch 事件 payload。
    """
    card_edits = [
        e for e in staged_edits
        if isinstance(e, dict) and e.get("origin_card_id") == origin_card_id
    ]
    if not card_edits:
        return None

    resolved_statuses = {"accepted", "adopted", "rejected", "skipped"}
    all_resolved = all(e.get("status") in resolved_statuses for e in card_edits)
    if not all_resolved:
        return None

    adopted_count = sum(1 for e in card_edits if e.get("status") in ("accepted", "adopted"))
    if adopted_count > 0:
        return {
            "card_id": origin_card_id,
            "new_status": CardStatus.ACCEPTED,
            "reason": f"all_{len(card_edits)}_edits_resolved",
        }
    else:
        return {
            "card_id": origin_card_id,
            "new_status": CardStatus.REJECTED,
            "reason": "all_edits_rejected_or_skipped",
        }


# ── Handoff / Bind-back ──────────────────────────────────────────────────

def handoff_card(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    target_role: str,
    target_file: str | None = None,
    handoff_policy: str = "open_development_studio",
    summary: str = "",
    handoff_summary: str | None = None,
    acceptance_criteria: list[str] | None = None,
    activate_target: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    """外部 handoff — 仅用于需要 DevStudio/OpenCode 等外部实现的场景。

    Guard: handoff_policy 必须是 external 类型，否则拒绝。
    内部路由（open_file_workspace / open_governance_panel / stay_in_studio_chat）
    不走 handoff，由前端直接处理。

    流程：
    1. 验证 handoff_policy 属于外部类型
    2. 设置源卡片 external_state = waiting_external_build
    3. 创建衍生卡（继承 workflow_id、phase、关联 source_card_id）
    4. 记录 handoff_created 到 card_exit_log
    5. 返回 route_kind / destination / return_to / explanation
    """
    # guard: 只允许外部 handoff
    route_kind = _classify_route_kind(handoff_policy)
    if route_kind != "external":
        return _err(
            "invalid_handoff_policy",
            f"handoff_card 仅用于外部交接 (open_development_studio / open_opencode)，"
            f"'{handoff_policy}' 属于内部路由，前端直接处理即可",
        )

    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    # 找源卡片
    source_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            source_card = card
            break
    if not source_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    # M4: 设置源卡片 external_state，不 pause — 保持 active 直到外部开始
    source_card["external_state"] = "waiting_external_build"

    # 创建衍生卡
    derived_id = _new_id("handoff")
    destination = _handoff_destination_id(handoff_policy)
    destination_label = _handoff_destination_label(handoff_policy)
    resolved_summary = handoff_summary or summary
    derived_card = {
        "id": derived_id,
        "contract_id": f"handoff.{source_card.get('contract_id', card_id)}",
        "workflow_id": source_card.get("workflow_id") or workflow_state.get("workflow_id"),
        "source": "handoff",
        "type": source_card.get("type", "governance"),
        "card_type": source_card.get("card_type", "governance"),
        "phase": source_card.get("phase", ""),
        "title": resolved_summary or f"外部实现：{source_card.get('title', '')}",
        "summary": resolved_summary or f"需在 {destination_label} 中实现",
        "status": CardStatus.QUEUED,
        "priority": source_card.get("priority", "medium"),
        "workspace_mode": "file" if target_file else "analysis",
        "target_file": target_file,
        "file_role": target_role,
        "handoff_policy": handoff_policy,
        "route_kind": "external",
        "destination": destination,
        "return_to": "bind_back",
        "external_state": "waiting_external_build",
        "origin": "handoff",
        "kind": "external_build",
        "target": {"type": target_role, "key": target_file} if target_file else {},
        "actions": [],
        "content": {
            "summary": resolved_summary,
            "handoff_summary": resolved_summary,
            "acceptance_criteria": acceptance_criteria or [],
            "source_card_id": card_id,
            "file_role": target_role,
            "handoff_policy": handoff_policy,
            "destination": destination,
            "destination_label": destination_label,
            "return_to": "bind_back",
            "external_state": "waiting_external_build",
        },
        "related_task_ids": [card_id],
    }
    cards.append(derived_card)

    # 激活衍生卡
    if activate_target:
        derived_card["status"] = CardStatus.ACTIVE
        workflow_state["active_card_id"] = derived_id
        _ensure_unified_flag(workflow_state)

    # 记录 handoff_created 到 card_exit_log
    exit_log = recovery.get("card_exit_log") or []
    exit_log.append({
        "event": "handoff_created",
        "source_card_id": card_id,
        "target_card_id": derived_id,
        "target_role": target_role,
        "target_file": target_file,
        "handoff_policy": handoff_policy,
        "route_kind": "external",
        "destination": destination,
        "summary": resolved_summary,
        "handoff_summary": resolved_summary,
        "acceptance_criteria": acceptance_criteria or [],
        "timestamp": _now_iso(),
    })
    recovery["card_exit_log"] = exit_log

    recovery["cards"] = cards
    recovery["workflow_state"] = workflow_state
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    source_title = source_card.get("title", "")
    return {
        "ok": True,
        "source_card_id": card_id,
        "derived_card_id": derived_id,
        "handoff_policy": handoff_policy,
        "activated": activate_target,
        "recovery_revision": recovery.get("revision", 0),
        # M4: 前端需要的路由字段
        "route_kind": "external",
        "destination": destination,
        "return_to": "bind_back",
        "handoff_summary": resolved_summary,
        "acceptance_criteria": acceptance_criteria or [],
        "explanation": f"「{source_title}」需要在 {destination_label} 中完成外部实现，完成后回到 Studio 回绑验收",
    }


def _handoff_destination_id(handoff_policy: str) -> str:
    """handoff_policy → 前端稳定消费的 destination 枚举。"""
    ids = {
        "open_development_studio": "dev_studio",
        "open_opencode": "opencode",
    }
    return ids.get(handoff_policy, "dev_studio")


def _handoff_destination_label(handoff_policy: str) -> str:
    """handoff_policy → 人可读的目标名称。"""
    labels = {
        "open_development_studio": "Development Studio",
        "open_opencode": "OpenCode",
    }
    return labels.get(handoff_policy, handoff_policy)


def bind_back_card(
    db: Session,
    skill_id: int,
    card_id: str,
    *,
    source: str = "external_edit_returned",
    summary: str = "",
    required_checks: list[str] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """外部编辑回绑 — 作为"回程路由器"创建 confirm/validate 后续卡。

    M4 重构：不再简单恢复原卡状态，而是：
    1. 更新源卡 external_state → returned_waiting_bindback
    2. 创建 confirm_external_result 确认卡（让用户验收外部产物）
    3. 如果有 required_checks，额外创建 validate_external 验证卡
    4. 激活确认卡
    5. 记录 external_edit_returned 到 card_exit_log
    6. 返回 route_kind / destination / return_to / explanation
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    target_card = None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            target_card = card
            break
    if not target_card:
        return _err("card_not_found", f"卡片 {card_id} 不存在")

    # M4: 更新 external_state
    target_card["external_state"] = "returned_waiting_bindback"

    # 追加回绑上下文到源卡
    context = target_card.get("content") or {}
    bind_back_entry = {
        "type": "bind_back",
        "source": source,
        "summary": summary,
        "required_checks": required_checks or [],
        "timestamp": _now_iso(),
    }
    context_history = context.get("context_history") or []
    context_history.append(bind_back_entry)
    context["context_history"] = context_history
    context["last_bind_back"] = bind_back_entry
    target_card["content"] = context

    # ── 创建 confirm_external_result 确认卡 ──
    confirm_id = _new_id("confirm")
    source_title = target_card.get("title", "")
    confirm_card = {
        "id": confirm_id,
        "contract_id": f"confirm.{target_card.get('contract_id', card_id)}",
        "workflow_id": target_card.get("workflow_id") or workflow_state.get("workflow_id"),
        "source": "bind_back",
        "type": "confirm",
        "card_type": "confirm",
        "phase": target_card.get("phase", ""),
        "title": f"验收外部产物：{source_title}",
        "summary": summary or f"外部实现已返回，请确认是否符合预期",
        "status": CardStatus.ACTIVE,
        "priority": "high",
        "workspace_mode": "analysis",
        "target_file": target_card.get("target_file"),
        "file_role": target_card.get("file_role"),
        "handoff_policy": "stay_in_studio_chat",
        "route_kind": "internal",
        "destination": "studio_chat",
        "return_to": "confirm",
        "origin": "bind_back",
        "kind": "confirm",
        "target": target_card.get("target") or {},
        "actions": ["accept", "reject", "revise"],
        "content": {
            "summary": summary,
            "source_card_id": card_id,
            "bind_back_source": source,
            "required_checks": required_checks or [],
            "route_kind": "internal",
            "destination": "studio_chat",
            "return_to": "confirm",
        },
        "related_task_ids": [card_id],
    }
    cards.append(confirm_card)

    # ── 如有 required_checks，创建 validate_external 验证卡（blocked by confirm） ──
    validate_id: str | None = None
    if required_checks:
        validate_id = _new_id("validate")
        validate_card = {
            "id": validate_id,
            "contract_id": f"validate.{target_card.get('contract_id', card_id)}",
            "workflow_id": target_card.get("workflow_id") or workflow_state.get("workflow_id"),
            "source": "bind_back",
            "type": "validation",
            "card_type": "validation",
            "phase": target_card.get("phase", ""),
            "title": f"验证外部产物：{source_title}",
            "summary": "、".join(required_checks),
            "status": CardStatus.QUEUED,
            "priority": "high",
            "workspace_mode": "report",
            "target_file": target_card.get("target_file"),
            "file_role": target_card.get("file_role"),
            "handoff_policy": "open_governance_panel",
            "route_kind": "internal",
            "destination": "governance_panel",
            "return_to": "validate",
            "origin": "bind_back_validation",
            "kind": "validation",
            "target": target_card.get("target") or {},
            "actions": [],
            "content": {
                "required_checks": required_checks,
                "source_card_id": card_id,
                "confirm_card_id": confirm_id,
                "route_kind": "internal",
                "destination": "governance_panel",
                "return_to": "validate",
            },
            "related_task_ids": [card_id, confirm_id],
        }
        cards.append(validate_card)
        target_card["external_state"] = "returned_waiting_validation"

    # 激活确认卡
    workflow_state["active_card_id"] = confirm_id
    _ensure_unified_flag(workflow_state)

    # 记录 external_edit_returned 到 card_exit_log
    exit_log = recovery.get("card_exit_log") or []
    exit_log.append({
        "event": "external_edit_returned",
        "card_id": card_id,
        "confirm_card_id": confirm_id,
        "validate_card_id": validate_id,
        "source": source,
        "summary": summary,
        "required_checks": required_checks or [],
        "timestamp": _now_iso(),
    })
    recovery["card_exit_log"] = exit_log

    recovery["cards"] = cards
    recovery["workflow_state"] = workflow_state
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    next_card_kind = "confirm"
    return {
        "ok": True,
        "card_id": card_id,
        "confirm_card_id": confirm_id,
        "validate_card_id": validate_id,
        "next_card_id": confirm_id,
        "next_card_kind": next_card_kind,
        "new_status": target_card.get("external_state", "returned_waiting_bindback"),
        "bind_back_source": source,
        "required_checks": required_checks or [],
        "recovery_revision": recovery.get("revision", 0),
        # M4: 前端需要的路由字段
        "route_kind": "internal",
        "destination": "studio_chat",
        "return_to": next_card_kind,
        "explanation": f"外部实现已返回，请在确认卡中验收「{source_title}」的产物",
    }


# ── 内部工具函数 ─────────────────────────────────────────────────────────────

def _infer_workspace_mode(card: dict[str, Any]) -> str:
    """从 card_type 推断 workspace mode。"""
    card_type = card.get("type") or card.get("card_type") or ""
    mapping = {
        "architect": WorkspaceMode.ANALYSIS,
        "governance": WorkspaceMode.FILE,
        "validation": WorkspaceMode.REPORT,
        "audit_issue": WorkspaceMode.FILE,
        "quality_issue": WorkspaceMode.FILE,
        "remediation": WorkspaceMode.FILE,
    }
    return mapping.get(card_type, WorkspaceMode.FILE)


def _infer_file_role(target_file: str | None, target_type: str | None, target_kind: str | None) -> str | None:
    file_path = (target_file or "").strip()
    lower_name = file_path.rsplit("/", 1)[-1].lower() if file_path else ""
    normalized_type = (target_type or "").strip().lower()
    normalized_kind = (target_kind or "").strip().lower()
    if normalized_kind == "skill_prompt" or normalized_type in {"prompt", "system_prompt"} or lower_name == "skill.md":
        return "main_prompt"
    if normalized_kind == "example" or "example" in lower_name:
        return "example"
    if normalized_kind == "reference" or "reference" in lower_name:
        return "reference"
    if normalized_kind in {"knowledge_base", "knowledge"}:
        return "knowledge_base"
    if normalized_kind == "template" or "template" in lower_name:
        return "template"
    if normalized_kind == "tool" or normalized_type == "tool_binding" or "tool" in lower_name:
        return "tool"
    if file_path:
        return "unknown_asset"
    return None


_EXTERNAL_HANDOFF_POLICIES = frozenset({
    "open_development_studio",
    "open_opencode",
})

_INTERNAL_ROUTE_POLICIES = frozenset({
    "open_file_workspace",
    "open_governance_panel",
    "stay_in_studio_chat",
})


def _classify_route_kind(handoff_policy: str | None) -> str:
    """internal | external | none."""
    if not handoff_policy:
        return "none"
    if handoff_policy in _EXTERNAL_HANDOFF_POLICIES:
        return "external"
    if handoff_policy in _INTERNAL_ROUTE_POLICIES:
        return "internal"
    return "none"


def _infer_handoff_policy(file_role: str | None, workspace_mode: str | None, target_file: str | None) -> str | None:
    """推断 handoff_policy — 仅返回策略值，route_kind 由 _classify_route_kind 判定。"""
    if file_role == "tool":
        return "open_development_studio"
    if workspace_mode == WorkspaceMode.REPORT:
        return "open_governance_panel"
    if workspace_mode == WorkspaceMode.ANALYSIS:
        return "stay_in_studio_chat"
    if target_file:
        return "open_file_workspace"
    return None


def _find_next_pending_card(
    cards: list[dict[str, Any]],
    exclude_card_id: str,
) -> dict[str, Any] | None:
    """找到下一张待处理卡片（优先级高的先）。"""
    priority_order = {"high": 0, "p0": 0, "medium": 1, "p1": 1, "low": 2, "p2": 2}
    pending = [
        c for c in cards
        if isinstance(c, dict)
        and c.get("id") != exclude_card_id
        and c.get("status") in (CardStatus.PENDING, CardStatus.QUEUED)
    ]
    pending.sort(key=lambda c: priority_order.get(c.get("priority", "medium"), 1))
    return pending[0] if pending else None


def _mark_recovery_updated(recovery: dict[str, Any]) -> None:
    """递增 recovery revision 并更新时间戳。"""
    rev = recovery.get("revision") or 0
    try:
        rev = int(rev)
    except (TypeError, ValueError):
        rev = 0
    schema_version = recovery.get("schema_version") or 0
    try:
        schema_version = int(schema_version)
    except (TypeError, ValueError):
        schema_version = 0
    recovery["schema_version"] = max(schema_version, 3)
    recovery["revision"] = rev + 1
    recovery["updated_at"] = _now_iso()


def _save(db: Session, memo: SkillMemo, payload: dict, user_id: int | None) -> None:
    """保存 memo payload — 使用 optimistic lock 原子写入，自动重试一次。"""
    from app.services.skill_memo_service import save_memo_payload_atomic, OptimisticLockError

    if user_id:
        memo.updated_by = user_id
    try:
        save_memo_payload_atomic(db, memo, payload)
    except OptimisticLockError:
        # 并发写入冲突：刷新 memo 对象后重试一次
        logger.warning("OptimisticLockError on memo %s, retrying once", memo.id)
        db.expire(memo)
        db.refresh(memo)
        # 重新读取 version 后再试
        save_memo_payload_atomic(db, memo, payload)
    db.flush()


def _extract_db_edit_id(edit_id: Any) -> int | None:
    """尝试从 staged edit id 提取 DB 层整数 id。

    workflow recovery 中的 edit id 可能是:
    - 整数 (DB 直接写入)
    - 纯数字字符串 "123"
    - 前缀格式 "db_123" / "se_123" (protocol 层生成，只有数字后缀部分是 DB id)
    只有整数 id 能用于调用 studio_governance.adopt/reject。
    """
    if edit_id is None:
        return None
    if isinstance(edit_id, int):
        return edit_id
    s = str(edit_id).strip()
    if s.isdigit():
        return int(s)
    # 尝试 "db_123" 格式
    if s.startswith("db_"):
        suffix = s[3:]
        if suffix.isdigit():
            return int(suffix)
    # "se_xxxx" 是 protocol 层自生成的 hex id，不对应 DB 记录
    if s.startswith("se_"):
        return None
    # 其他未知格式 — 记日志但不报错
    logger.debug("_extract_db_edit_id: unrecognized format %r, returning None", edit_id)
    return None


def _ensure_unified_flag(workflow_state: dict[str, Any]) -> None:
    """在 metadata 中设置 unified_architecture=True 标志。

    该标志是统一架构模式的持久标识，不随 active_card_id 清空而消失，
    防止旧系统的 _refresh / sync 逻辑误判模式。
    """
    metadata = workflow_state.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        workflow_state["metadata"] = metadata
    metadata["unified_architecture"] = True


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


# ── Artifact 持久化 ────────────────────────────────────────────────────────

def save_card_artifact(
    db: Session,
    skill_id: int,
    *,
    card_id: str,
    contract_id: str,
    artifact_key: str,
    artifact_data: Any,
    user_id: int | None = None,
) -> dict[str, Any]:
    """将卡片产物保存到 recovery.card_artifacts[contract_id][artifact_key]。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}

    card_artifacts = recovery.get("card_artifacts") or {}
    if contract_id not in card_artifacts:
        card_artifacts[contract_id] = {}
    card_artifacts[contract_id][artifact_key] = artifact_data

    recovery["card_artifacts"] = card_artifacts
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "card_id": card_id,
        "contract_id": contract_id,
        "artifact_key": artifact_key,
        "recovery_revision": recovery.get("revision", 0),
    }


def complete_card(
    db: Session,
    skill_id: int,
    *,
    card_id: str,
    contract_id: str,
    exit_reason: str = "adopted",
    next_card_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """标记卡片完成：设为 adopted，记录到 completed_card_ids 和 card_exit_log。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []

    # 设置卡片状态为 adopted
    for card in cards:
        if isinstance(card, dict) and card.get("id") == card_id:
            card["status"] = CardStatus.ADOPTED
            break

    # 追加到 completed_card_ids
    completed = recovery.get("completed_card_ids") or []
    if card_id not in completed:
        completed.append(card_id)
    recovery["completed_card_ids"] = completed

    # 追加到 card_exit_log
    exit_log = recovery.get("card_exit_log") or []
    exit_log.append({
        "card_id": card_id,
        "contract_id": contract_id,
        "exit_reason": exit_reason,
        "next_card_id": next_card_id,
        "completed_at": _now_iso(),
    })
    recovery["card_exit_log"] = exit_log

    recovery["cards"] = cards
    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    # 如果有 next_card_id，激活下一张
    result: dict[str, Any] = {
        "ok": True,
        "card_id": card_id,
        "exit_reason": exit_reason,
        "recovery_revision": recovery.get("revision", 0),
    }
    if next_card_id:
        activate_result = activate_card(db, skill_id, next_card_id, user_id=user_id)
        result["next_card_activated"] = activate_result.get("ok", False)
        result["next_card_id"] = next_card_id

    return result


def mark_cards_stale(
    db: Session,
    skill_id: int,
    *,
    card_ids: list[str],
    reason: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """将指定卡片标记为 stale（上游修改导致下游 artifact 过期）。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}

    stale = recovery.get("stale_card_ids") or []
    for cid in card_ids:
        if cid not in stale:
            stale.append(cid)
    recovery["stale_card_ids"] = stale

    _mark_recovery_updated(recovery)
    payload["workflow_recovery"] = recovery
    _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "stale_card_ids": stale,
        "reason": reason,
        "recovery_revision": recovery.get("revision", 0),
    }


def get_card_artifacts(
    db: Session,
    skill_id: int,
) -> dict[str, Any]:
    """读取 recovery.card_artifacts → {contract_id: {artifact_key: data}}。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {}

    payload = memo.memo_payload or {}
    recovery = payload.get("workflow_recovery") or {}
    return recovery.get("card_artifacts") or {}


# ── AI 动态卡片提案 ──────────────────────────────────────────────────────

# 不允许跳过的卡片 kind
_SKIP_PROTECTED_KINDS = {"confirm", "governance"}
# 每轮最多处理的 proposal 数
_MAX_PROPOSALS_PER_ROUND = 3


def apply_card_proposals(
    db: Session,
    skill_id: int,
    *,
    proposals: list[dict[str, Any]],
    user_id: int | None = None,
) -> dict[str, Any]:
    """应用 AI 提出的卡片变更提案。

    支持三种 action:
    - skip: 标记 queued 卡为 rejected（不在 completed 中且非 protected kind）
    - add: 创建新卡追加到 cards（contract_id 不冲突，phase 合法）
    - merge: 将多张 queued 卡合并为一张新卡

    返回 {ok, applied: [...], rejected: [...], recovery_revision}
    """
    if not proposals:
        return {"ok": True, "applied": [], "rejected": []}

    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return _err("memo_not_found", "Memo 不存在")

    payload = copy.deepcopy(memo.memo_payload or {})
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    completed_set = set(recovery.get("completed_card_ids") or [])
    workflow_state = recovery.get("workflow_state") or {}

    card_map = {c.get("id"): c for c in cards if isinstance(c, dict) and c.get("id")}
    contract_ids = {c.get("contract_id") for c in cards if isinstance(c, dict)}

    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    # 找最大 priority（add 操作的 priority 天花板）
    max_priority = max(
        (c.get("priority", 0) for c in cards if isinstance(c, dict) and isinstance(c.get("priority"), (int, float))),
        default=100,
    )

    for proposal in proposals[:_MAX_PROPOSALS_PER_ROUND]:
        action = proposal.get("action", "")
        if action == "skip":
            result = _apply_skip(proposal, card_map, completed_set)
        elif action == "add":
            result = _apply_add(proposal, contract_ids, max_priority, workflow_state, cards)
        elif action == "merge":
            result = _apply_merge(proposal, card_map, completed_set, workflow_state, cards)
        else:
            result = {"ok": False, "reason": f"unknown action: {action}", "proposal": proposal}

        if result.get("ok"):
            applied.append(result)
        else:
            rejected.append(result)

    if applied:
        recovery["cards"] = cards
        _mark_recovery_updated(recovery)
        payload["workflow_recovery"] = recovery
        _save(db, memo, payload, user_id)

    return {
        "ok": True,
        "applied": applied,
        "rejected": rejected,
        "recovery_revision": recovery.get("revision", 0),
    }


def _apply_skip(
    proposal: dict[str, Any],
    card_map: dict[str, dict[str, Any]],
    completed_set: set[str],
) -> dict[str, Any]:
    """跳过一张 queued 卡。"""
    card_id = proposal.get("card_id", "")
    card = card_map.get(card_id)
    if not card:
        return {"ok": False, "reason": f"card {card_id} not found", "proposal": proposal}
    if card_id in completed_set:
        return {"ok": False, "reason": f"card {card_id} already completed", "proposal": proposal}
    if card.get("status") != "queued":
        return {"ok": False, "reason": f"card {card_id} status is {card.get('status')}, not queued", "proposal": proposal}
    if card.get("kind") in _SKIP_PROTECTED_KINDS:
        return {"ok": False, "reason": f"cannot skip {card.get('kind')} card", "proposal": proposal}

    card["status"] = CardStatus.REJECTED
    card["content"] = card.get("content") or {}
    card["content"]["skip_reason"] = proposal.get("reason", "AI 建议跳过")
    return {
        "ok": True,
        "action": "skip",
        "card_id": card_id,
        "reason": proposal.get("reason", ""),
    }


def _apply_add(
    proposal: dict[str, Any],
    contract_ids: set[str | None],
    max_priority: int | float,
    workflow_state: dict[str, Any],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """追加一张新卡。"""
    contract_id = proposal.get("contract_id", "")
    title = proposal.get("title", "")
    phase = proposal.get("phase", "")

    if not contract_id or not title:
        return {"ok": False, "reason": "missing contract_id or title", "proposal": proposal}
    if contract_id in contract_ids:
        return {"ok": False, "reason": f"contract_id {contract_id} already exists", "proposal": proposal}
    if not phase:
        return {"ok": False, "reason": "missing phase", "proposal": proposal}

    # priority 天花板：不超过注册表最大 priority
    proposed_priority = proposal.get("priority", 50)
    if isinstance(proposed_priority, (int, float)):
        proposed_priority = min(proposed_priority, max_priority)
    else:
        proposed_priority = 50

    new_card = {
        "id": f"ai_proposal:{contract_id}",
        "contract_id": contract_id,
        "workflow_id": workflow_state.get("workflow_id"),
        "source": "ai_proposal",
        "type": "dynamic",
        "card_type": "dynamic",
        "phase": phase,
        "title": title,
        "summary": title,
        "status": "queued",
        "priority": proposed_priority,
        "workspace_mode": proposal.get("mode", "analysis"),
        "target_file": None,
        "file_role": None,
        "handoff_policy": "stay_in_studio_chat",
        "origin": "ai_proposal",
        "kind": proposal.get("kind", "create"),
        "target": {},
        "actions": [],
        "content": {
            "summary": title,
            "contract_id": contract_id,
            "proposal_reason": proposal.get("reason", ""),
        },
    }
    cards.append(new_card)
    contract_ids.add(contract_id)
    return {
        "ok": True,
        "action": "add",
        "card_id": new_card["id"],
        "contract_id": contract_id,
        "title": title,
    }


def _apply_merge(
    proposal: dict[str, Any],
    card_map: dict[str, dict[str, Any]],
    completed_set: set[str],
    workflow_state: dict[str, Any],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """合并多张 queued 卡为一张。"""
    source_ids = proposal.get("source_ids") or []
    title = proposal.get("title", "")

    if len(source_ids) < 2:
        return {"ok": False, "reason": "merge needs at least 2 source_ids", "proposal": proposal}
    if not title:
        return {"ok": False, "reason": "missing title", "proposal": proposal}

    # 验证所有 source 都存在且 queued
    source_cards = []
    for sid in source_ids:
        sc = card_map.get(sid)
        if not sc:
            return {"ok": False, "reason": f"source card {sid} not found", "proposal": proposal}
        if sid in completed_set:
            return {"ok": False, "reason": f"source card {sid} already completed", "proposal": proposal}
        if sc.get("status") != "queued":
            return {"ok": False, "reason": f"source card {sid} status is {sc.get('status')}", "proposal": proposal}
        source_cards.append(sc)

    # 继承最高 priority 和第一个的 phase
    max_pri = max(sc.get("priority", 0) for sc in source_cards)
    phase = source_cards[0].get("phase", "")

    # 标记 source 为 rejected
    for sc in source_cards:
        sc["status"] = CardStatus.REJECTED
        sc["content"] = sc.get("content") or {}
        sc["content"]["merged_into"] = f"merged:{title}"

    # 创建合并卡
    merged_contract = proposal.get("contract_id") or f"merged.{source_ids[0]}"
    merged_card = {
        "id": f"merged:{merged_contract}",
        "contract_id": merged_contract,
        "workflow_id": workflow_state.get("workflow_id"),
        "source": "ai_proposal",
        "type": "dynamic",
        "card_type": "dynamic",
        "phase": phase,
        "title": title,
        "summary": title,
        "status": "queued",
        "priority": max_pri,
        "workspace_mode": "analysis",
        "target_file": None,
        "file_role": None,
        "handoff_policy": "stay_in_studio_chat",
        "origin": "ai_merge",
        "kind": "create",
        "target": {},
        "actions": [],
        "content": {
            "summary": title,
            "contract_id": merged_contract,
            "merged_from": source_ids,
        },
    }
    cards.append(merged_card)
    return {
        "ok": True,
        "action": "merge",
        "card_id": merged_card["id"],
        "merged_from": source_ids,
        "title": title,
    }
