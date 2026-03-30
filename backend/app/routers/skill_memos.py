"""Skill Memo API — Skill Studio 状态机路由。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services import skill_memo_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills/{skill_id}/memo", tags=["skill_memo"])


# ── Request / Response models ────────────────────────────────────────────────

class MemoInitRequest(BaseModel):
    scenario_type: str  # import_remediation / new_skill_creation / published_iteration
    goal_summary: Optional[str] = None
    force_rebuild: bool = False


class AnalyzeImportRequest(BaseModel):
    trigger: str = "import_zip"


class TaskStartRequest(BaseModel):
    source: str = "studio_chat"


class CompleteFromSaveRequest(BaseModel):
    filename: str
    file_type: str = "asset"  # asset / prompt
    content_size: int = 0
    content_hash: Optional[str] = None
    version_id: Optional[int] = None


class DirectTestRequest(BaseModel):
    source: str = "persistent_notice"


class TestResultRequest(BaseModel):
    source: str  # preflight / sandbox / manual
    version: int = 0
    status: str  # passed / failed
    summary: str = ""
    details: Optional[dict] = None
    suggested_followups: Optional[list[dict]] = None


class AdoptFeedbackRequest(BaseModel):
    source_type: str  # comment
    source_id: int
    summary: str
    task_blueprint: dict = {}


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def get_memo(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前 memo 视图。"""
    result = skill_memo_service.get_memo(db, skill_id)
    if not result:
        return {"skill_id": skill_id, "memo": None}
    return result


@router.post("/init")
def init_memo(
    skill_id: int,
    req: MemoInitRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新建 Skill 完成需求澄清后，或手动重建 memo。"""
    result = skill_memo_service.init_memo(
        db, skill_id, req.scenario_type, req.goal_summary, user.id, req.force_rebuild
    )
    return {"ok": True, "memo": result.get("memo") if isinstance(result, dict) else result, "current_task": result.get("current_task") if isinstance(result, dict) else None}


@router.post("/analyze-import")
def analyze_import(
    skill_id: int,
    req: AnalyzeImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导入 Skill 后触发结构分析。"""
    result = skill_memo_service.analyze_import(db, skill_id, user.id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "分析失败"))
    return result


@router.post("/tasks/{task_id}/start")
def start_task(
    skill_id: int,
    task_id: str,
    req: TaskStartRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户选择"开始完善"时调用。"""
    result = skill_memo_service.start_task(db, skill_id, task_id, user.id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "操作失败"))
    return result


@router.post("/tasks/{task_id}/complete-from-save")
def complete_from_save(
    skill_id: int,
    task_id: str,
    req: CompleteFromSaveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """前端在文件保存成功后调用。"""
    result = skill_memo_service.complete_from_save(
        db, skill_id, task_id,
        req.filename, req.file_type, req.content_size, req.content_hash, req.version_id,
    )
    return result


@router.post("/direct-test")
def direct_test(
    skill_id: int,
    req: DirectTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户点击"无需完善直接提交测试"。"""
    result = skill_memo_service.direct_test(db, skill_id, user.id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "操作失败"))
    return result


@router.post("/test-result")
def test_result(
    skill_id: int,
    req: TestResultRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """测试流程结束后统一回写 memo。"""
    result = skill_memo_service.record_test_result(
        db, skill_id,
        req.source, req.version, req.status, req.summary,
        req.details, req.suggested_followups, user.id,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "操作失败"))
    return result


@router.post("/adopt-feedback")
def adopt_feedback(
    skill_id: int,
    req: AdoptFeedbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """采纳用户反馈并转化为任务。"""
    result = skill_memo_service.adopt_feedback(
        db, skill_id,
        req.source_type, req.source_id, req.summary, req.task_blueprint, user.id,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "操作失败"))
    return result
