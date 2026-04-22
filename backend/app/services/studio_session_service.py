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
    cards = [_enrich_card_for_session(card) for card in cards if isinstance(card, dict)]
    staged_edits = [_enrich_staged_edit_for_session(edit) for edit in staged_edits if isinstance(edit, dict)]

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

    # ── M4: CardResolver 集成 — 多模式卡片补充 ──
    lifecycle_stage = memo.lifecycle_stage or "analysis"
    session_mode = ""
    resolve_phase = ""
    if isinstance(workflow_state, dict):
        session_mode = workflow_state.get("session_mode", "")
        metadata = workflow_state.get("metadata") or {}
        # create_new_skill: architect_phase 来自 metadata 或 workflow_state
        # optimize_existing_skill: 从 lifecycle_stage 推断
        # audit_imported_skill: 从 metadata.audit_phase 或 lifecycle_stage 推断
        if session_mode == "create_new_skill":
            resolve_phase = metadata.get("architect_phase") or workflow_state.get("architect_phase", "")
        elif session_mode == "optimize_existing_skill":
            resolve_phase = _lifecycle_to_optimize_phase(lifecycle_stage)
        elif session_mode == "audit_imported_skill":
            resolve_phase = metadata.get("audit_phase") or _lifecycle_to_audit_phase(lifecycle_stage)

    # 所有已知模式 + 有 phase 时调用 CardResolver
    if session_mode and resolve_phase and resolve_phase != "ready_for_draft":
        try:
            from app.services.studio_card_resolver import resolve_cards
            resolver_result = resolve_cards(
                db, skill_id,
                session_mode=session_mode,
                architect_phase=resolve_phase,
                workflow_state=workflow_state,
                cards=cards,
                staged_edits=staged_edits,
                memo=memo,
            )
            cards = resolver_result.cards
            if resolver_result.active_card_id:
                active_card_id = resolver_result.active_card_id
        except Exception:
            logger.warning("CardResolver failed for skill %s, falling back", skill_id, exc_info=True)

    # ── M3: 从 recovery 读取 artifact/completed/stale ──
    completed_card_ids = recovery.get("completed_card_ids") or []
    card_artifacts = recovery.get("card_artifacts") or {}
    stale_card_ids = recovery.get("stale_card_ids") or []

    # ── card order ──
    persisted_card_order = recovery.get("card_order") if isinstance(recovery.get("card_order"), list) else []
    card_ids = [c["id"] for c in cards if isinstance(c, dict) and c.get("id")]
    card_order = [str(card_id) for card_id in persisted_card_order if card_id in card_ids]
    card_order.extend(card_id for card_id in card_ids if card_id not in card_order)
    if card_order:
        card_map = {c["id"]: c for c in cards if isinstance(c, dict) and c.get("id")}
        cards = [card_map[card_id] for card_id in card_order if card_id in card_map]

    active_card = None
    if active_card_id:
        for card in cards:
            if isinstance(card, dict) and card.get("id") == active_card_id:
                active_card = card
                break

    # ── M4: external_route_summary ──
    external_route_summary = _build_external_route_summary(cards)

    persisted_queue_window = recovery.get("queue_window") if isinstance(recovery.get("queue_window"), dict) else None
    card_queue_window = persisted_queue_window or _build_card_queue_window(
        cards, active_card_id, workflow_state,
        completed_card_ids=completed_card_ids,
        staged_edits=staged_edits,
    )

    # ── M3: card_queue_ledger ──
    card_exit_log = recovery.get("card_exit_log") or []
    persisted_card_queue_ledger = recovery.get("card_queue_ledger") if isinstance(recovery.get("card_queue_ledger"), dict) else None
    card_queue_ledger = persisted_card_queue_ledger or _build_card_queue_ledger(
        completed_card_ids=completed_card_ids,
        card_artifacts=card_artifacts,
        stale_card_ids=stale_card_ids,
        card_exit_log=card_exit_log,
        cards=cards,
    )

    # ── workspace 计算 ──
    workspace = studio_workspace_service.compute_workspace(
        active_card=active_card,
        cards=cards,
        staged_edits=staged_edits,
        workflow_state=workflow_state if isinstance(workflow_state, dict) else None,
    )

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
        lifecycle_stage=lifecycle_stage,
        status_summary=status_summary,
        context_rollups=context_rollups,
        blueprint=blueprint,
        card_order=card_order,
        progress_log=progress_log,
        workflow_cards=cards,
        card_queue_window=card_queue_window,
        completed_card_ids=completed_card_ids,
        card_artifacts=card_artifacts,
        stale_card_ids=stale_card_ids,
        card_queue_ledger=card_queue_ledger,
        external_route_summary=external_route_summary,
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


def _lifecycle_to_optimize_phase(lifecycle_stage: str) -> str:
    """从 lifecycle_stage 推断 optimize 模式的 resolve phase。"""
    mapping = {
        "analysis": "governance",
        "governance": "governance",
        "refine": "refine",
        "editing": "refine",
        "draft": "refine",
        "validation": "validation",
        "testing": "validation",
        "review": "validation",
        "published": "validation",
    }
    return mapping.get(lifecycle_stage, "governance")


def _lifecycle_to_audit_phase(lifecycle_stage: str) -> str:
    """从 lifecycle_stage 推断 audit 模式的 resolve phase。"""
    mapping = {
        "analysis": "audit",
        "audit": "audit",
        "governance": "audit",
        "fixing": "fixing",
        "editing": "fixing",
        "refine": "fixing",
        "draft": "fixing",
        "release": "release",
        "validation": "release",
        "testing": "release",
        "review": "release",
        "published": "release",
    }
    return mapping.get(lifecycle_stage, "audit")


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


def _infer_file_role(target_file: str | None, target: dict[str, Any] | None, content: dict[str, Any] | None) -> str | None:
    target_kind = str((content or {}).get("target_kind") or "").strip().lower()
    target_type = str((target or {}).get("type") or (target or {}).get("target_type") or "").strip().lower()
    file_path = (target_file or "").strip()
    file_name = file_path.rsplit("/", 1)[-1] if file_path else ""
    lower_name = file_name.lower()

    if target_kind == "skill_prompt" or lower_name == "skill.md" or target_type in {"prompt", "system_prompt"}:
        return "main_prompt"
    if "example" in lower_name or target_kind == "example":
        return "example"
    if "reference" in lower_name or target_kind == "reference":
        return "reference"
    if target_kind in {"knowledge_base", "knowledge"}:
        return "knowledge_base"
    if target_kind == "template" or "template" in lower_name:
        return "template"
    if target_kind == "tool" or target_type == "tool_binding" or "tool" in lower_name:
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


def _destination_for_policy(handoff_policy: str | None) -> str | None:
    destinations = {
        "open_development_studio": "dev_studio",
        "open_opencode": "opencode",
        "open_file_workspace": "file_workspace",
        "open_governance_panel": "governance_panel",
        "stay_in_studio_chat": "studio_chat",
    }
    return destinations.get(handoff_policy or "")


def _infer_handoff_policy(file_role: str | None, workspace_mode: str | None, target_file: str | None) -> str | None:
    """推断 handoff_policy — 仅返回策略值，route_kind 由 _classify_route_kind 判定。"""
    if file_role == "tool":
        return "open_development_studio"
    if workspace_mode == "report":
        return "open_governance_panel"
    if workspace_mode == "analysis":
        return "stay_in_studio_chat"
    if target_file:
        return "open_file_workspace"
    return None


def _target_file_from_card(card: dict[str, Any]) -> str | None:
    content = card.get("content") if isinstance(card.get("content"), dict) else {}
    for value in (
        card.get("target_file"),
        card.get("target_ref"),
        content.get("target_ref"),
        content.get("file_path"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    target = card.get("target") if isinstance(card.get("target"), dict) else {}
    for key in ("key", "target_key"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _enrich_card_for_session(card: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(card)
    content = dict(enriched.get("content")) if isinstance(enriched.get("content"), dict) else {}
    target = dict(enriched.get("target")) if isinstance(enriched.get("target"), dict) else {}
    target_file = _target_file_from_card(enriched)
    workspace_mode = str(enriched.get("workspace_mode") or "").strip() or None
    file_role = str(enriched.get("file_role") or content.get("file_role") or "").strip() or None
    if file_role is None:
        file_role = _infer_file_role(target_file, target, content)
    handoff_policy = str(enriched.get("handoff_policy") or content.get("handoff_policy") or "").strip() or None
    if handoff_policy is None:
        handoff_policy = _infer_handoff_policy(file_role, workspace_mode, target_file)

    if target_file and not enriched.get("target_file"):
        enriched["target_file"] = target_file
    if file_role:
        enriched["file_role"] = file_role
        content.setdefault("file_role", file_role)
    if handoff_policy:
        enriched["handoff_policy"] = handoff_policy
        content.setdefault("handoff_policy", handoff_policy)
    # M4: route_kind — 区分 internal 和 external
    route_kind = _classify_route_kind(handoff_policy)
    enriched["route_kind"] = route_kind
    destination = str(enriched.get("destination") or content.get("destination") or "").strip() or _destination_for_policy(handoff_policy)
    return_to = str(enriched.get("return_to") or content.get("return_to") or "").strip() or ("bind_back" if route_kind == "external" else "none")
    if destination:
        enriched["destination"] = destination
        content.setdefault("destination", destination)
    if return_to:
        enriched["return_to"] = return_to
        content.setdefault("return_to", return_to)
    if enriched.get("external_state") and "external_state" not in content:
        content["external_state"] = enriched["external_state"]
    if content:
        enriched["content"] = content
    return enriched


def _enrich_staged_edit_for_session(edit: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(edit)
    target_key = str(enriched.get("target_key") or "").strip() or None
    target_type = str(enriched.get("target_type") or "").strip() or None
    file_role = str(enriched.get("file_role") or "").strip() or _infer_file_role(
        target_key,
        {"target_type": target_type},
        None,
    )
    handoff_policy = str(enriched.get("handoff_policy") or "").strip() or _infer_handoff_policy(
        file_role,
        "file",
        target_key,
    )
    if file_role:
        enriched["file_role"] = file_role
    if handoff_policy:
        enriched["handoff_policy"] = handoff_policy
    route_kind = _classify_route_kind(handoff_policy)
    enriched["route_kind"] = route_kind
    destination = _destination_for_policy(handoff_policy)
    if destination:
        enriched["destination"] = destination
    enriched["return_to"] = "bind_back" if route_kind == "external" else "none"
    return enriched


def _build_card_queue_window(
    cards: list[dict[str, Any]],
    active_card_id: str | None,
    workflow_state: Any,
    *,
    completed_card_ids: list[str] | None = None,
    staged_edits: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """构建卡片队列窗口。

    M3 升级：
    - blocked hidden: 状态为 blocked / revision_needed / paused 的卡片不占 visible 位
    - next phase preview: active phase 之外的下一 phase 首卡作为 preview 出现
    - active 完成后窗口推进: 如果 active card 已在 completed_card_ids 中，自动滑动到下一张
    """
    if not cards:
        return None

    completed_set = set(completed_card_ids or [])
    active_id = active_card_id or next(
        (c.get("id") for c in cards if c.get("status") == "active"), None
    )

    # 如果 active card 已完成，推进到下一张未完成的 actionable 卡
    actionable_statuses = {"pending", "queued", "active", "reviewing", "drafting", "diff_ready"}
    if active_id and active_id in completed_set:
        active_id = None
        for card in cards:
            cid = card.get("id")
            if not cid or cid in completed_set:
                continue
            if card.get("status") in actionable_statuses:
                active_id = cid
                break

    # blocked / hidden 状态：不占 visible 位
    hidden_statuses = {"blocked", "revision_needed", "paused"}
    # 当前 phase
    phase = "discover"
    if isinstance(workflow_state, dict) and isinstance(workflow_state.get("phase"), str):
        phase = workflow_state["phase"]

    # 确定 active card 的 phase 用于 next phase preview
    active_phase = None
    if active_id:
        for card in cards:
            if card.get("id") == active_id:
                active_phase = card.get("phase")
                break

    visible_ids: list[str] = []
    hidden_ids: list[str] = []
    preview_id: str | None = None  # next phase 预览卡

    # 先放 active card
    if active_id:
        visible_ids.append(active_id)

    # 填充同 phase 的 actionable 卡
    for card in cards:
        card_id = card.get("id")
        if not card_id or card_id in visible_ids or card_id in completed_set:
            continue
        status = card.get("status", "")

        # blocked/hidden 不占位
        if status in hidden_statuses:
            hidden_ids.append(card_id)
            continue

        if status not in actionable_statuses:
            continue

        # next phase preview: 不同 phase 且尚无 preview → 标记为 preview，不占 visible 位
        card_phase = card.get("phase")
        if active_phase and card_phase and card_phase != active_phase and not preview_id:
            preview_id = card_id
            continue

        visible_ids.append(card_id)
        if len(visible_ids) >= 5:
            break

    total_actionable = sum(
        1 for card in cards
        if card.get("status") in actionable_statuses and card.get("id") not in completed_set
    )

    # ── M3 增补: pending_artifacts ──
    edits = staged_edits or []
    has_pending_staged_edit = any(
        isinstance(e, dict) and e.get("status") == "pending"
        for e in edits
    )
    has_external_edit_waiting_bindback = any(
        isinstance(c, dict)
        and c.get("external_state") in ("returned_waiting_bindback", "returned_waiting_validation")
        for c in cards
    )
    has_failed_validation = any(
        isinstance(c, dict)
        and c.get("kind") == "fixing"
        and c.get("status") in ("active", "pending")
        for c in cards
    )
    pending_artifacts = {
        "has_pending_staged_edit": has_pending_staged_edit,
        "has_external_edit_waiting_bindback": has_external_edit_waiting_bindback,
        "has_failed_validation": has_failed_validation,
    }

    # ── M3 增补: blocking_signal ──
    blocking_signal: dict[str, Any] | None = None
    if has_pending_staged_edit:
        # 找到第一张有 pending staged edit 的卡
        pending_edit_card_id = None
        for e in edits:
            if isinstance(e, dict) and e.get("status") == "pending" and e.get("origin_card_id"):
                pending_edit_card_id = e["origin_card_id"]
                break
        blocking_signal = {
            "kind": "pending_confirmation",
            "card_id": pending_edit_card_id or active_id or "",
            "reason": "存在待确认修改",
        }
    elif has_failed_validation:
        fixing_card_id = next(
            (c.get("id") for c in cards if isinstance(c, dict) and c.get("kind") == "fixing" and c.get("status") in ("active", "pending")),
            None,
        )
        blocking_signal = {
            "kind": "failed_validation",
            "card_id": fixing_card_id or active_id or "",
            "reason": "测试失败待整改",
        }
    elif has_external_edit_waiting_bindback:
        # M4: 找到处于外部实现状态的卡片
        ext_card = None
        for c in cards:
            if not isinstance(c, dict):
                continue
            ext_st = c.get("external_state", "")
            if ext_st in ("waiting_external_build", "external_in_progress", "returned_waiting_bindback", "returned_waiting_validation"):
                ext_card = c
                break
        ext_state = (ext_card or {}).get("external_state", "waiting_external_build")
        ext_reasons = {
            "waiting_external_build": "等待外部实现启动",
            "external_in_progress": "外部实现进行中",
            "returned_waiting_bindback": "外部产物已返回，待回绑验收",
            "returned_waiting_validation": "外部产物待验证",
        }
        blocking_signal = {
            "kind": "waiting_external",
            "card_id": (ext_card or {}).get("id", "") or active_id or "",
            "reason": ext_reasons.get(ext_state, "外部编辑待回绑"),
            "external_state": ext_state,
        }
    else:
        # 阶段门禁：当前阶段有未完成的 blocking 卡
        if active_phase:
            blocking_in_phase = [
                c for c in cards
                if isinstance(c, dict)
                and c.get("phase") == active_phase
                and c.get("status") in hidden_statuses
            ]
            if blocking_in_phase:
                bc = blocking_in_phase[0]
                blocking_signal = {
                    "kind": "phase_gate",
                    "card_id": bc.get("id", ""),
                    "reason": f"阶段 {active_phase} 存在阻塞卡片",
                }

    # ── M3 增补: resume_hint ──
    resume_hint: dict[str, str] | None = None
    prev_active_card_id: str | None = None
    if isinstance(workflow_state, dict):
        prev_active_card_id = workflow_state.get("active_card_id")
    if prev_active_card_id and active_id:
        if prev_active_card_id == active_id:
            # 找 active card title
            active_title = ""
            for c in cards:
                if isinstance(c, dict) and c.get("id") == active_id:
                    active_title = c.get("title", "")
                    break
            resume_hint = {
                "kind": "resume_same_card",
                "message": f"上次停在\"{active_title}\"，可继续",
            }
        else:
            # 优先级变更
            active_title = ""
            for c in cards:
                if isinstance(c, dict) and c.get("id") == active_id:
                    active_title = c.get("title", "")
                    break
            reason = blocking_signal["reason"] if blocking_signal else "优先级变更"
            resume_hint = {
                "kind": "resume_reprioritized",
                "message": f"由于{reason}，当前优先处理\"{active_title}\"",
            }

    # ── M3/M4 增补: active_card_explanation ──
    active_card_explanation: str | None = None
    if active_id:
        active_card_obj: dict[str, Any] | None = None
        active_title = ""
        for c in cards:
            if isinstance(c, dict) and c.get("id") == active_id:
                active_card_obj = c
                active_title = c.get("title", "")
                break
        # M4: 外部 handoff 相关的解释
        active_kind = (active_card_obj or {}).get("kind", "")
        active_origin = (active_card_obj or {}).get("origin", "")
        if active_kind == "confirm" and active_origin == "bind_back":
            source_card_id = ((active_card_obj or {}).get("content") or {}).get("source_card_id", "")
            source_title = ""
            for c in cards:
                if isinstance(c, dict) and c.get("id") == source_card_id:
                    source_title = c.get("title", "")
                    break
            active_card_explanation = f"外部实现已返回，请验收「{source_title or active_title}」的产物"
        elif active_kind == "external_build":
            dest = ((active_card_obj or {}).get("content") or {}).get("destination", "外部编辑器")
            active_card_explanation = f"「{active_title}」需要在 {dest} 中完成，完成后自动回绑"
        elif blocking_signal:
            bs_kind = blocking_signal.get("kind", "")
            if bs_kind == "waiting_external":
                active_card_explanation = f"{blocking_signal['reason']}，当前可继续其他任务"
            else:
                active_card_explanation = f"因为{blocking_signal['reason']}，当前先处理确认卡"
        elif prev_active_card_id and prev_active_card_id != active_id:
            prev_title = ""
            for c in cards:
                if isinstance(c, dict) and c.get("id") == prev_active_card_id:
                    prev_title = c.get("title", "")
                    break
            if prev_title:
                active_card_explanation = f"因为\"{prev_title}\"已确认，下一步\"{active_title}\""
            else:
                active_card_explanation = f"当前任务：{active_title}"
        else:
            active_card_explanation = f"当前任务：{active_title}"

    return {
        "active_card_id": active_id,
        "visible_card_ids": visible_ids,
        "hidden_card_ids": hidden_ids,
        "preview_card_id": preview_id,
        "backlog_count": max(total_actionable - len(visible_ids), 0),
        "phase": phase,
        "max_visible": 5,
        "reveal_policy": "stage_gated",
        "pending_artifacts": pending_artifacts,
        "blocking_signal": blocking_signal,
        "resume_hint": resume_hint,
        "active_card_explanation": active_card_explanation,
    }


def _build_external_route_summary(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """构建外部 handoff 状态汇总 — 前端用于显示外部实现进度。"""
    external_cards = [
        c for c in cards
        if isinstance(c, dict) and c.get("external_state")
    ]
    if not external_cards:
        return None

    state_counts: dict[str, int] = {}
    active_externals: list[dict[str, Any]] = []
    for c in external_cards:
        ext_state = c.get("external_state", "")
        state_counts[ext_state] = state_counts.get(ext_state, 0) + 1
        if ext_state in ("waiting_external_build", "external_in_progress", "returned_waiting_bindback", "returned_waiting_validation"):
            active_externals.append({
                "card_id": c.get("id", ""),
                "title": c.get("title", ""),
                "external_state": ext_state,
                "destination": (c.get("content") or {}).get("destination", ""),
                "return_to": c.get("return_to") or (c.get("content") or {}).get("return_to", ""),
            })

    current_external = active_externals[0] if active_externals else None
    return {
        "total": len(external_cards),
        "state_counts": state_counts,
        "active_externals": active_externals[:5],
        "has_pending": bool(active_externals),
        "has_external_in_progress": bool(state_counts.get("external_in_progress")),
        "has_returned_waiting_bindback": bool(state_counts.get("returned_waiting_bindback")),
        "has_returned_waiting_validation": bool(state_counts.get("returned_waiting_validation")),
        "current_external_card_title": (current_external or {}).get("title", ""),
        "current_return_to": (current_external or {}).get("return_to", ""),
    }


def _build_card_queue_ledger(
    *,
    completed_card_ids: list[str],
    card_artifacts: dict[str, Any],
    stale_card_ids: list[str],
    card_exit_log: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建 card_queue_ledger — 汇总卡片流转的完整账本。

    ledger 结构：
    - completed: 已完成卡片 id 列表
    - stale: 已过期卡片 id 列表
    - artifacts_by_contract: {contract_id: [artifact_key, ...]}
    - exit_log: 退出记录（最近 20 条）
    - stats: 统计信息
    """
    done_statuses = {"accepted", "applied", "validated", "adopted", "rejected"}
    total = len(cards)
    completed_count = len(completed_card_ids)
    stale_count = len(stale_card_ids)
    active_count = sum(
        1 for c in cards
        if isinstance(c, dict) and c.get("status") in ("active", "drafting", "reviewing")
    )
    pending_count = sum(
        1 for c in cards
        if isinstance(c, dict) and c.get("status") in ("pending", "queued")
    )
    done_count = sum(
        1 for c in cards
        if isinstance(c, dict) and c.get("status") in done_statuses
    )

    # 按 contract_id 聚合 artifact keys
    artifacts_summary: dict[str, list[str]] = {}
    for contract_id, artifact_dict in card_artifacts.items():
        if isinstance(artifact_dict, dict):
            artifacts_summary[contract_id] = list(artifact_dict.keys())

    return {
        "completed": completed_card_ids,
        "stale": stale_card_ids,
        "artifacts_by_contract": artifacts_summary,
        "exit_log": card_exit_log[-20:],
        "stats": {
            "total": total,
            "completed": completed_count,
            "stale": stale_count,
            "active": active_count,
            "pending": pending_count,
            "done": done_count,
        },
    }
