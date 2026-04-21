"""Studio Test Flow Service — test flow 入口后端化。

职责：
- resolve-entry 统一入口（多 skill 分流、mount blocked 判断）
- run link 查询
- test flow 概览聚合
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.test_flow import TestFlowRunLink
from app.services.studio_workflow_protocol import TestFlowSummary

logger = logging.getLogger(__name__)


def get_test_flow_summary(
    db: Session,
    skill_id: int,
    *,
    workflow_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从现有数据聚合 test flow 概览（用于 studio session 响应）。"""
    summary = TestFlowSummary()

    # 从 workflow_state.metadata.test_flow 读取已有信息
    if workflow_state:
        metadata = workflow_state.get("metadata") or {}
        test_flow_meta = metadata.get("test_flow") or {}
        summary.phase = test_flow_meta.get("phase_status") or test_flow_meta.get("phase") or "idle"
        summary.entry_source = test_flow_meta.get("entry_source")
        matched = test_flow_meta.get("matched_skill_ids")
        if isinstance(matched, list):
            summary.matched_skill_ids = matched
        summary.blocking_issues = test_flow_meta.get("blocking_issues") or []
        # plan info
        plan_summary = test_flow_meta.get("plan_summary") or {}
        if isinstance(plan_summary, dict):
            summary.current_plan_id = plan_summary.get("plan_id")
            summary.current_plan_version = plan_summary.get("plan_version")

    # 从 run link 查最新执行记录
    latest_link = (
        db.query(TestFlowRunLink)
        .filter(TestFlowRunLink.skill_id == skill_id)
        .order_by(TestFlowRunLink.created_at.desc())
        .first()
    )
    if latest_link:
        summary.latest_session_id = latest_link.session_id
        summary.latest_report_id = latest_link.report_id
        if not summary.current_plan_id and latest_link.plan_id:
            summary.current_plan_id = latest_link.plan_id
            summary.current_plan_version = latest_link.plan_version

    return summary.to_dict()


def resolve_entry(
    db: Session,
    skill_id: int,
    *,
    content: str,
    mentioned_skill_ids: list[int] | None = None,
    candidate_skills: list[dict[str, Any]] | None = None,
    entry_source: str = "studio",
    conversation_id: int | None = None,
    auto_create_card: bool = False,
) -> dict[str, Any]:
    """统一 test flow 入口 — 包装 test_flow_trigger.resolve_test_flow_entry。

    与原始 trigger 的区别：
    - 如果调用方已确定 skill_id 且 mentioned_skill_ids 为空，
      自动补充 mentioned_skill_ids = [skill_id]，避免 "missing_skill_target"。
    - auto_create_card=True 时，mount_blocked 会自动在 recovery 中创建阻断卡片。
    """
    from app.services.test_flow_trigger import resolve_test_flow_entry

    effective_mentioned = list(mentioned_skill_ids or [])
    if not effective_mentioned:
        effective_mentioned = [skill_id]

    payload = {
        "content": content,
        "selected_skill_id": skill_id,
        "mentioned_skill_ids": effective_mentioned,
        "candidate_skills": candidate_skills or [{"id": skill_id, "name": f"Skill #{skill_id}"}],
        "entry_source": entry_source,
        "conversation_id": conversation_id,
    }

    result = resolve_test_flow_entry(db, payload)

    # auto_create_card: mount_blocked 时自动创建阻断卡片
    if auto_create_card and result.get("action") == "mount_blocked":
        try:
            from app.services import studio_card_service
            blocking_issues = result.get("blocking_issues") or []
            summary = result.get("gate_summary") or "测试流被权限挂载阻断"
            card_result = studio_card_service.create_card(
                db,
                skill_id,
                card_type="validation",
                title="挂载阻断: " + (blocking_issues[0] if blocking_issues else "权限未就绪"),
                summary=summary,
                phase="validation",
                priority="high",
                origin="test_flow_mount_blocked",
                activate=True,
            )
            if card_result.get("ok"):
                result["auto_created_card_id"] = card_result["card_id"]
                result["auto_created_card"] = card_result.get("card")
        except Exception:
            logger.warning("auto_create_card for mount_blocked failed", exc_info=True)

    return result


def get_run_links_by_session(
    db: Session,
    sandbox_session_id: int,
) -> dict[str, Any] | None:
    """根据 sandbox_session_id 查询 run link。"""
    link = (
        db.query(TestFlowRunLink)
        .filter(TestFlowRunLink.session_id == sandbox_session_id)
        .first()
    )
    if not link:
        return None

    return {
        "id": link.id,
        "session_id": link.session_id,
        "report_id": link.report_id,
        "skill_id": link.skill_id,
        "plan_id": link.plan_id,
        "plan_version": link.plan_version,
        "case_count": link.case_count,
        "entry_source": link.entry_source,
        "decision_mode": link.decision_mode,
        "conversation_id": link.conversation_id,
        "workflow_id": link.workflow_id,
        "created_at": link.created_at.isoformat() if link.created_at else None,
    }


def get_run_links_by_skill(
    db: Session,
    skill_id: int,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """获取某 skill 的所有 run links（按时间倒序）。"""
    links = (
        db.query(TestFlowRunLink)
        .filter(TestFlowRunLink.skill_id == skill_id)
        .order_by(TestFlowRunLink.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": link.id,
            "session_id": link.session_id,
            "report_id": link.report_id,
            "plan_id": link.plan_id,
            "plan_version": link.plan_version,
            "case_count": link.case_count,
            "entry_source": link.entry_source,
            "decision_mode": link.decision_mode,
            "conversation_id": link.conversation_id,
            "created_at": link.created_at.isoformat() if link.created_at else None,
        }
        for link in links
    ]
