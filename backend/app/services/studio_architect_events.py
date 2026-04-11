"""Studio Architect Events — 结构化事件构造函数。

供 studio_agent 在对话流程中 yield SSE 事件。
每个函数返回 {"event": str, "data": dict}，与 conversations.py 的 _sse() 兼容。

事件类型（§6.1）：
- architect_phase_status  — 阶段状态变更（首轮 route 已发，后续阶段推进时复用）
- architect_question      — 单问题引导卡
- architect_phase_summary — 阶段总结确认卡
- architect_structure     — 结构化分析卡（JTBD/Issue Tree/金字塔/MECE/场景规划）
- architect_priority_matrix — P0/P1/P2 优先级矩阵
- architect_ooda_decision — OODA 收敛决策
- architect_ready_for_draft — 收敛完成，准备进入草稿/治理
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.skill import ArchitectWorkflowState


# ── 事件构造函数 ─────────────────────────────────────────────────────────────

def make_phase_status(phase: str, mode_source: str = "", ooda_round: int = 0) -> dict:
    """阶段状态变更事件。"""
    return {
        "event": "architect_phase_status",
        "data": {
            "phase": phase,
            "mode_source": mode_source,
            "ooda_round": ooda_round,
        },
    }


def make_question(
    question: str,
    phase: str,
    options: list[str] | None = None,
    framework: str | None = None,
) -> dict:
    """单问题引导卡 — 一次只问一个问题，多选题优先。"""
    data: dict[str, Any] = {
        "question": question,
        "phase": phase,
    }
    if options:
        data["options"] = options
    if framework:
        data["framework"] = framework
    return {"event": "architect_question", "data": data}


def make_phase_summary(
    phase: str,
    outputs: dict[str, Any],
    confirmed: bool = False,
) -> dict:
    """阶段总结确认卡 — 展示当前阶段产出 + 确认按钮。"""
    return {
        "event": "architect_phase_summary",
        "data": {
            "phase": phase,
            "outputs": outputs,
            "confirmed": confirmed,
        },
    }


def make_structure(
    structure_type: str,
    title: str,
    data: Any,
) -> dict:
    """结构化分析卡 — JTBD / Issue Tree / 金字塔 / MECE / Scenario Planning。"""
    return {
        "event": "architect_structure",
        "data": {
            "type": structure_type,
            "title": title,
            "data": data,
        },
    }


def make_priority_matrix(
    items: list[dict],
) -> dict:
    """P0/P1/P2 优先级矩阵。每项: {"label": str, "priority": "P0"|"P1"|"P2", "reason": str}"""
    return {
        "event": "architect_priority_matrix",
        "data": {"items": items},
    }


def make_ooda_decision(
    round_: int,
    action: str,
    reason: str,
    rollback_to: str | None = None,
) -> dict:
    """OODA 收敛决策 — action: "continue" | "rollback" | "converged"。"""
    data: dict[str, Any] = {
        "round": round_,
        "action": action,
        "reason": reason,
    }
    if rollback_to:
        data["rollback_to"] = rollback_to
    return {"event": "architect_ooda_decision", "data": data}


def make_ready_for_draft(
    summary: dict[str, Any],
    exit_to: str = "generate_draft",
) -> dict:
    """收敛完成，准备退出 architect → 进入草稿/治理/局部优化。"""
    return {
        "event": "architect_ready_for_draft",
        "data": {
            "summary": summary,
            "exit_to": exit_to,
        },
    }


# ── 状态推进辅助函数 ─────────────────────────────────────────────────────────

_PHASE_ORDER = [
    "phase_1_why",
    "phase_2_what",
    "phase_3_how",
    "ooda_iteration",
    "ready_for_draft",
]


def advance_phase(
    db: Session,
    conversation_id: int,
    phase_outputs: dict | None = None,
) -> ArchitectWorkflowState | None:
    """确认当前阶段并推进到下一阶段，返回更新后的状态。

    自动处理：
    - 标记当前 phase 为 confirmed
    - 合并 phase_outputs
    - 推进到下一 phase（ooda_iteration 时 ooda_round+1）
    """
    state = db.query(ArchitectWorkflowState).filter(
        ArchitectWorkflowState.conversation_id == conversation_id
    ).first()
    if not state:
        return None

    current_phase = state.workflow_phase

    # 确认当前阶段
    confirmed = dict(state.phase_confirmed or {})
    confirmed[current_phase] = True
    state.phase_confirmed = confirmed
    flag_modified(state, "phase_confirmed")

    # 合并 outputs
    if phase_outputs:
        outputs = dict(state.phase_outputs or {})
        outputs[current_phase] = phase_outputs
        state.phase_outputs = outputs
        flag_modified(state, "phase_outputs")

    # 推进
    if current_phase in _PHASE_ORDER:
        idx = _PHASE_ORDER.index(current_phase)
        if current_phase == "ooda_iteration":
            state.ooda_round = (state.ooda_round or 0) + 1
            # ooda 默认留在 ooda_iteration，由外部判断是否进入 ready_for_draft
        elif idx + 1 < len(_PHASE_ORDER):
            state.workflow_phase = _PHASE_ORDER[idx + 1]

    db.commit()
    db.refresh(state)
    return state


def rollback_phase(
    db: Session,
    conversation_id: int,
    target_phase: str,
) -> ArchitectWorkflowState | None:
    """OODA 回调到指定阶段。"""
    state = db.query(ArchitectWorkflowState).filter(
        ArchitectWorkflowState.conversation_id == conversation_id
    ).first()
    if not state:
        return None

    if target_phase in _PHASE_ORDER:
        state.workflow_phase = target_phase
        # 取消回调目标及后续阶段的 confirmed 标记
        confirmed = dict(state.phase_confirmed or {})
        idx = _PHASE_ORDER.index(target_phase)
        for p in _PHASE_ORDER[idx:]:
            confirmed.pop(p, None)
        state.phase_confirmed = confirmed
        flag_modified(state, "phase_confirmed")

    db.commit()
    db.refresh(state)
    return state
