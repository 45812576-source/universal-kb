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
        "origin": origin,
        "target": {},
        "actions": [],
        "content": {"summary": summary},
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

    return {
        "ok": True,
        "card_id": card_id,
        "decision": decision,
        "new_card_status": new_status,
        "workflow_state_patch": state_patch,
        "workspace": workspace,
        "recovery_revision": recovery.get("revision", 0),
        "memo_refresh_required": decision in ("accept", "reject"),
    }


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

    return {
        "ok": True,
        "staged_edit_id": staged_edit_id,
        "decision": decision,
        "new_status": target_status,
        "workspace": workspace,
        "recovery_revision": recovery.get("revision", 0),
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
