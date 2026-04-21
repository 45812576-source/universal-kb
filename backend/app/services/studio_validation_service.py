"""Studio Validation Service — 统一 validation 结果回写到 studio session。

职责：
- 明确 validation card 结构
- 把 sandbox session/report 来源写入 workflow_recovery.test_flow
- test pass/fail 后推进 phase/next_action/active_card_id/workspace.mode
- 失败时生成 remediation governance card
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.studio_workflow_protocol import (
    CardStatus,
    StudioEventTypes,
    ValidationSource,
    WorkspaceMode,
    _new_id,
    _now_iso,
)

logger = logging.getLogger(__name__)


def build_validation_card(
    *,
    workflow_id: str | None,
    validation_type: str,
    title: str,
    summary: str,
    source_session_id: int | None = None,
    source_report_id: int | None = None,
    source_plan_id: int | None = None,
    source_plan_version: int | None = None,
    phase: str = "validation",
) -> dict[str, Any]:
    """构建标准 validation card。"""
    card_id = _new_id("vcard")
    return {
        "id": card_id,
        "workflow_id": workflow_id,
        "source": "validation",
        "type": "validation",
        "card_type": "validation",
        "phase": phase,
        "title": title,
        "summary": summary,
        "status": CardStatus.ACTIVE,
        "priority": "high",
        "workspace_mode": WorkspaceMode.REPORT,
        "validation_source": {
            "type": validation_type,
            "session_id": source_session_id,
            "report_id": source_report_id,
            "plan_id": source_plan_id,
            "plan_version": source_plan_version,
        },
        "origin": f"validation_{validation_type}",
        "target": {},
        "actions": [],
        "content": {
            "summary": summary,
            "validation_type": validation_type,
            "report_id": source_report_id,
            "session_id": source_session_id,
        },
        "created_at": _now_iso(),
    }


def build_validation_source(
    *,
    validation_type: str,
    session_id: int | None = None,
    report_id: int | None = None,
    plan_id: int | None = None,
    plan_version: int | None = None,
    status: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """构建 validation_source 摘要。"""
    return ValidationSource(
        type=validation_type,
        session_id=session_id,
        report_id=report_id,
        plan_id=plan_id,
        plan_version=plan_version,
        status=status,
        summary=summary,
    ).to_dict()


def apply_test_result_to_recovery(
    recovery: dict[str, Any],
    *,
    test_status: str,
    test_summary: str,
    validation_type: str = "sandbox",
    session_id: int | None = None,
    report_id: int | None = None,
    plan_id: int | None = None,
    plan_version: int | None = None,
) -> dict[str, Any]:
    """将测试结果写入 workflow_recovery，推进 phase/workspace/active_card。

    返回 state_patch 字典，描述本次变更。
    """
    workflow_state = recovery.get("workflow_state") or {}
    cards = recovery.get("cards") or []
    state_patch: dict[str, Any] = {}

    # 1. 写入 test_flow 摘要
    test_flow = workflow_state.get("metadata", {}).get("test_flow") or {}
    test_flow.update({
        "latest_session_id": session_id,
        "latest_report_id": report_id,
        "latest_status": test_status,
        "latest_summary": test_summary,
        "validation_type": validation_type,
    })
    if "metadata" not in workflow_state:
        workflow_state["metadata"] = {}
    workflow_state["metadata"]["test_flow"] = test_flow

    # 2. 写入 validation_source
    workflow_state["metadata"]["validation_source"] = build_validation_source(
        validation_type=validation_type,
        session_id=session_id,
        report_id=report_id,
        plan_id=plan_id,
        plan_version=plan_version,
        status=test_status,
        summary=test_summary,
    )

    # 3. 维持统一架构标志
    if "metadata" not in workflow_state:
        workflow_state["metadata"] = {}
    workflow_state["metadata"]["unified_architecture"] = True

    # 4. 根据 pass/fail 推进状态
    if test_status == "pass":
        workflow_state["phase"] = "ready_for_publish"
        workflow_state["next_action"] = "confirm_publish"
        state_patch["phase"] = "ready_for_publish"
        state_patch["workspace_mode"] = WorkspaceMode.REPORT
    elif test_status == "fail":
        # 生成 remediation card
        remediation_card = _build_remediation_card_from_failure(
            workflow_id=workflow_state.get("workflow_id"),
            validation_type=validation_type,
            report_id=report_id,
            session_id=session_id,
            summary=test_summary,
        )
        cards.append(remediation_card)

        workflow_state["phase"] = "governance_execution"
        workflow_state["next_action"] = "review_remediation"
        workflow_state["active_card_id"] = remediation_card["id"]
        workflow_state["workspace_mode"] = WorkspaceMode.FILE

        state_patch["phase"] = "governance_execution"
        state_patch["active_card_id"] = remediation_card["id"]
        state_patch["workspace_mode"] = WorkspaceMode.FILE
        state_patch["new_remediation_card_id"] = remediation_card["id"]

    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    return state_patch


def _build_remediation_card_from_failure(
    *,
    workflow_id: str | None,
    validation_type: str,
    report_id: int | None,
    session_id: int | None,
    summary: str,
) -> dict[str, Any]:
    """测试失败后自动生成 remediation governance card。"""
    card_id = _new_id("rcard")
    return {
        "id": card_id,
        "workflow_id": workflow_id,
        "source": "validation_remediation",
        "type": "governance",
        "card_type": "governance",
        "phase": "governance_execution",
        "title": f"整改：{validation_type} 测试失败",
        "summary": summary or "测试未通过，需要根据报告进行整改",
        "status": CardStatus.ACTIVE,
        "priority": "high",
        "workspace_mode": WorkspaceMode.FILE,
        "origin": f"validation_result_{validation_type}",
        "validation_source": {
            "type": validation_type,
            "session_id": session_id,
            "report_id": report_id,
        },
        "target": {},
        "actions": [],
        "content": {
            "summary": summary,
            "source_report_id": report_id,
            "source_session_id": session_id,
            "validation_type": validation_type,
        },
        "created_at": _now_iso(),
    }
