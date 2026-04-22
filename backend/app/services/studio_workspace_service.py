"""Studio Workspace Service — 根据 active card 和 workflow state 决策工作区模式。

职责：
- 根据 active_card_id 与 workflow_state.phase 输出 workspace 状态
- 决定 mode / primary_target / related_targets / report_ref / governance_drawer_state
- 前端直接消费，不再需要自行猜测工作区模式
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.studio_workflow_protocol import (
    WorkspaceData,
    WorkspaceMode,
)

logger = logging.getLogger(__name__)


def compute_workspace(
    *,
    active_card: dict[str, Any] | None,
    cards: list[dict[str, Any]],
    staged_edits: list[dict[str, Any]],
    workflow_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据当前 active card 与 workflow state 计算工作区状态。

    返回 WorkspaceData.to_dict() 格式。
    """
    if not active_card:
        return WorkspaceData(
            mode=_mode_from_phase(workflow_state),
        ).to_dict()

    card_type = active_card.get("type") or active_card.get("card_type") or ""
    workspace_mode_override = active_card.get("workspace_mode")

    if workspace_mode_override:
        mode = workspace_mode_override
    else:
        mode = _mode_from_card_type(card_type)

    primary_target = _resolve_primary_target(active_card, mode)
    related_targets = _resolve_related_targets(active_card, staged_edits)
    report_ref = _resolve_report_ref(active_card)
    governance_drawer_state = _resolve_governance_drawer(active_card, workflow_state)

    return WorkspaceData(
        mode=mode,
        primary_target=primary_target,
        related_targets=related_targets,
        report_ref=report_ref,
        governance_drawer_state=governance_drawer_state,
    ).to_dict()


def _mode_from_card_type(card_type: str) -> str:
    """card_type -> workspace mode 映射。"""
    mapping = {
        "architect": WorkspaceMode.ANALYSIS,
        "governance": WorkspaceMode.FILE,
        "validation": WorkspaceMode.REPORT,
        # 兼容旧 card_type
        "audit_issue": WorkspaceMode.FILE,
        "quality_issue": WorkspaceMode.FILE,
        "remediation": WorkspaceMode.FILE,
        "preflight": WorkspaceMode.REPORT,
        "sandbox_report": WorkspaceMode.REPORT,
        # M5: 新增卡片类型映射
        "confirm": WorkspaceMode.REPORT,
        "external_build": WorkspaceMode.FILE,
        "fixing": WorkspaceMode.FILE,
        "release": WorkspaceMode.REPORT,
        "refine": WorkspaceMode.FILE,
        "optimize": WorkspaceMode.FILE,
        "audit": WorkspaceMode.REPORT,
    }
    return mapping.get(card_type, WorkspaceMode.FILE)


def _mode_from_phase(workflow_state: dict[str, Any] | None) -> str:
    """当没有 active card 时，从 phase 推断默认 mode。"""
    if not workflow_state:
        return WorkspaceMode.FILE

    phase = workflow_state.get("phase", "")
    if phase in ("phase_1_why", "phase_2_what", "phase_3_how", "ooda_iteration"):
        return WorkspaceMode.ANALYSIS
    if phase in ("validation",):
        return WorkspaceMode.REPORT
    return WorkspaceMode.FILE


def _resolve_primary_target(card: dict[str, Any], mode: str) -> dict[str, Any] | None:
    """从卡片信息解析主工作目标。"""
    # 优先取卡片显式声明的 target_file
    target_file = card.get("target_file")
    if target_file:
        return {"type": "source_file", "key": target_file}

    # 从 target 字段提取
    target = card.get("target") or {}
    if isinstance(target, dict):
        target_key = target.get("target_key") or target.get("file_path") or target.get("key")
        if target_key:
            target_type = target.get("target_type", "source_file")
            return {"type": target_type, "key": target_key}

    # 从 content 中的 staged_edit 关联提取
    content = card.get("content") or {}
    staged_edit_id = content.get("staged_edit_id")
    if staged_edit_id:
        return {"type": "staged_edit", "key": staged_edit_id}

    # validation 卡用 report
    validation_source = card.get("validation_source") or {}
    report_id = validation_source.get("report_id")
    if report_id and mode == WorkspaceMode.REPORT:
        return {"type": "report", "key": str(report_id)}

    return None


def _resolve_related_targets(
    card: dict[str, Any],
    staged_edits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """收集当前卡片关联的其他文件/目标。"""
    result: list[dict[str, Any]] = []
    card_id = card.get("id")

    # 从 staged_edits 中找当前卡片关联的编辑
    for edit in staged_edits:
        if not isinstance(edit, dict):
            continue
        if edit.get("origin_card_id") == card_id:
            target_key = edit.get("target_key")
            if target_key:
                result.append({
                    "type": edit.get("target_type", "source_file"),
                    "key": target_key,
                    "staged_edit_id": edit.get("id"),
                })

    return result


def _resolve_report_ref(card: dict[str, Any]) -> str | None:
    """如果卡片是 validation 类型，提取 report 引用。"""
    validation_source = card.get("validation_source") or {}
    report_id = validation_source.get("report_id")
    if report_id:
        return str(report_id)

    # 兼容 content 中的 report_id
    content = card.get("content") or {}
    report_id = content.get("report_id") or content.get("source_report_id")
    if report_id:
        return str(report_id)

    return None


def _resolve_governance_drawer(
    card: dict[str, Any],
    workflow_state: dict[str, Any] | None,
) -> str:
    """决定治理抽屉状态。"""
    card_type = card.get("type") or card.get("card_type") or ""

    # governance 卡片执行期间自动展开抽屉
    if card_type in ("governance", "audit_issue", "quality_issue", "remediation", "confirm", "fixing"):
        status = card.get("status", "")
        if status in ("active", "drafting", "diff_ready", "reviewing"):
            return "expanded"

    return "closed"
