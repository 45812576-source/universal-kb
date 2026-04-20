"""测试流历史 — RunLink CRUD + 装饰 session/history/report。"""
import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.test_flow import TestFlowRunLink


def create_run_link(
    db: Session,
    session_id: int,
    skill_id: int,
    plan_id: int | None,
    plan_version: int | None,
    case_count: int,
    created_by: int,
    report_id: int | None = None,
    entry_source: str | None = None,
    decision_mode: str | None = None,
    conversation_id: int | None = None,
    workflow_id: int | None = None,
) -> TestFlowRunLink:
    """创建 run link 记录。"""
    link = TestFlowRunLink(
        session_id=session_id,
        report_id=report_id,
        skill_id=skill_id,
        plan_id=plan_id,
        plan_version=plan_version,
        case_count=case_count,
        entry_source=entry_source,
        decision_mode=decision_mode,
        conversation_id=conversation_id,
        workflow_id=workflow_id,
        created_by=created_by,
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
    )
    db.add(link)
    db.flush()
    return link


def _get_run_link_by_session(db: Session, session_id: int) -> TestFlowRunLink | None:
    return (
        db.query(TestFlowRunLink)
        .filter(TestFlowRunLink.session_id == session_id)
        .first()
    )


def _get_run_links_by_sessions(db: Session, session_ids: list[int]) -> dict[int, TestFlowRunLink]:
    if not session_ids:
        return {}
    links = (
        db.query(TestFlowRunLink)
        .filter(TestFlowRunLink.session_id.in_(session_ids))
        .all()
    )
    return {link.session_id: link for link in links}


def _link_fields(link: TestFlowRunLink | None) -> dict[str, Any]:
    if not link:
        return {
            "source_case_plan_id": None,
            "source_case_plan_version": None,
            "source_case_count": None,
            "test_entry_source": None,
            "test_decision_mode": None,
            "source_conversation_id": None,
        }
    return {
        "source_case_plan_id": link.plan_id,
        "source_case_plan_version": link.plan_version,
        "source_case_count": link.case_count,
        "test_entry_source": link.entry_source,
        "test_decision_mode": link.decision_mode,
        "source_conversation_id": link.conversation_id,
    }


def decorate_session(db: Session, session_dict: dict[str, Any]) -> dict[str, Any]:
    """用 run link 装饰单个 session 返回。"""
    session_id = session_dict.get("session_id")
    if not session_id:
        return session_dict
    link = _get_run_link_by_session(db, session_id)
    return {**session_dict, **_link_fields(link)}


def decorate_history(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量装饰 history 列表。"""
    session_ids = [
        item["session_id"]
        for item in items
        if isinstance(item.get("session_id"), int)
    ]
    links_map = _get_run_links_by_sessions(db, session_ids)
    return [
        {**item, **_link_fields(links_map.get(item.get("session_id")))}
        for item in items
    ]


def decorate_report(db: Session, report_dict: dict[str, Any]) -> dict[str, Any]:
    """用 run link 装饰 report 返回。"""
    session_id = report_dict.get("session_id")
    if not session_id:
        return report_dict
    link = _get_run_link_by_session(db, session_id)
    return {**report_dict, **_link_fields(link)}
