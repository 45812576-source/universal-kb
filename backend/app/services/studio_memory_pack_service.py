"""Studio MemoryPack Service — 给 LLM 的上下文记忆包。

Phase B10: 从 SkillMemo + workflow_state + 最近 cards + staged_edits 中提取
memory_pack，注入到 OrchestratorInput → prompt context，让模型在每轮对话中
拥有完整的项目记忆。

MemoryPack 结构:
- skill_summary: Skill 的核心摘要（名称、描述、状态）
- workflow_phase: 当前 workflow 阶段
- session_mode: 当前 session 模式
- recent_cards: 最近 N 张卡片的摘要（含状态、contract、决策）
- active_card_context: 当前 active card 的完整上下文
- pending_staged_edits: 待确认修改摘要
- global_constraints: 全局约束
- decision_history: 最近用户决策记录
- context_rollups: 已收敛的上下文摘要
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill_memo import SkillMemo

logger = logging.getLogger(__name__)

# 最多保留最近 N 张卡片在 memory_pack 中
_MAX_RECENT_CARDS = 8
# 最多保留最近 N 条决策记录
_MAX_DECISION_HISTORY = 10


def build_memory_pack(
    db: Session,
    skill_id: int,
    *,
    include_context_rollups: bool = True,
    max_recent_cards: int = _MAX_RECENT_CARDS,
) -> dict[str, Any] | None:
    """从 SkillMemo 构建 memory_pack。

    返回 None 表示 memo 不存在。
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = memo.memo_payload or {}
    recovery = payload.get("workflow_recovery") or {}
    workflow_state = recovery.get("workflow_state") or {}
    cards = recovery.get("cards") or []
    staged_edits = recovery.get("staged_edits") or []

    pack: dict[str, Any] = {}

    # 1. skill_summary
    pack["skill_summary"] = _build_skill_summary(memo, payload)

    # 2. workflow_phase + session_mode
    pack["workflow_phase"] = workflow_state.get("phase", "")
    pack["session_mode"] = workflow_state.get("session_mode", "")

    # 3. active_card_context
    active_card_id = workflow_state.get("active_card_id")
    if active_card_id:
        for card in cards:
            if isinstance(card, dict) and card.get("id") == active_card_id:
                pack["active_card_context"] = _build_card_context(card)
                break

    # 4. recent_cards (排除 active card，最多 N 张)
    recent = []
    for card in reversed(cards):
        if not isinstance(card, dict):
            continue
        cid = card.get("id")
        if cid == active_card_id:
            continue
        recent.append(_build_card_summary(card))
        if len(recent) >= max_recent_cards:
            break
    pack["recent_cards"] = recent

    # 5. pending_staged_edits
    pending = [
        _build_staged_edit_summary(e)
        for e in staged_edits
        if isinstance(e, dict) and e.get("status") == "pending"
    ]
    if pending:
        pack["pending_staged_edits"] = pending

    # 6. global_constraints
    metadata = workflow_state.get("metadata") or {}
    gc = metadata.get("global_constraints")
    if isinstance(gc, list) and gc:
        pack["global_constraints"] = gc

    # 7. decision_history
    exit_log = recovery.get("card_exit_log") or []
    decisions = [
        entry for entry in exit_log
        if isinstance(entry, dict) and entry.get("event") in (
            "decision_recorded", "handoff_created", "external_edit_returned",
            "card_completed", "card_accepted", "card_rejected",
        )
    ]
    if decisions:
        pack["decision_history"] = decisions[-_MAX_DECISION_HISTORY:]

    # 8. context_rollups
    if include_context_rollups:
        rollups = payload.get("context_rollups") or []
        if rollups:
            pack["context_rollups"] = rollups

    return pack


def build_memory_pack_for_orchestrator(
    db: Session,
    skill_id: int,
) -> dict[str, Any] | None:
    """Orchestrator 专用 — 返回精简版 memory_pack。"""
    return build_memory_pack(db, skill_id, include_context_rollups=False, max_recent_cards=5)


# ── 内部构建函数 ─────────────────────────────────────────────────────────────

def _build_skill_summary(memo: SkillMemo, payload: dict[str, Any]) -> dict[str, Any]:
    """Skill 核心摘要。"""
    return {
        "skill_id": memo.skill_id,
        "lifecycle_stage": memo.lifecycle_stage or "analysis",
        "title": payload.get("title") or "",
        "description": payload.get("description") or "",
        "version": payload.get("version") or 0,
    }


def _build_card_context(card: dict[str, Any]) -> dict[str, Any]:
    """Active card 的完整上下文 — 包含 content、context_history。"""
    content = card.get("content") if isinstance(card.get("content"), dict) else {}
    return {
        "card_id": card.get("id"),
        "contract_id": card.get("contract_id"),
        "title": card.get("title"),
        "phase": card.get("phase"),
        "status": card.get("status"),
        "card_type": card.get("card_type") or card.get("type"),
        "target_file": card.get("target_file"),
        "file_role": card.get("file_role"),
        "handoff_policy": card.get("handoff_policy"),
        "route_kind": card.get("route_kind"),
        "summary": content.get("summary") or card.get("summary"),
        "context_history": content.get("context_history") or [],
        "actions": card.get("actions") or [],
    }


def _build_card_summary(card: dict[str, Any]) -> dict[str, Any]:
    """卡片轻量摘要 — 用于 recent_cards。"""
    return {
        "card_id": card.get("id"),
        "contract_id": card.get("contract_id"),
        "title": card.get("title"),
        "status": card.get("status"),
        "phase": card.get("phase"),
        "card_type": card.get("card_type") or card.get("type"),
    }


def _build_staged_edit_summary(edit: dict[str, Any]) -> dict[str, Any]:
    """Staged edit 摘要。"""
    return {
        "id": edit.get("id"),
        "origin_card_id": edit.get("origin_card_id"),
        "target_file": edit.get("target_file"),
        "status": edit.get("status"),
        "summary": edit.get("summary") or edit.get("description") or "",
    }
