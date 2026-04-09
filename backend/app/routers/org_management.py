"""组织管理模块 API — 导入/CRUD/变更追踪/基线状态

Prefix: /api/org-management
Auth: require_role(Role.SUPER_ADMIN) 默认，部分接口开放 DEPT_ADMIN
"""

import datetime
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge_governance import GovernanceBaselineSnapshot, GovernanceDepartmentMission, GovernanceStrategyStat
from app.models.org_management import (
    BizProcess,
    BizTerminology,
    DataAssetOwnership,
    DeptCollaborationLink,
    DeptMissionDetail,
    KpiAssignment,
    OkrKeyResult,
    OkrObjective,
    OkrPeriod,
    OrgChangeEvent,
    OrgImportSession,
    PositionAccessRule,
)
from app.models.permission import Position
from app.models.user import Department, Role, User
from app.services.org_change_tracker import model_to_dict, track_change, track_create, track_delete, track_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org-management", tags=["org-management"])


# ══════════════════════════════════════════════════════════════════════════════
# 4.1 导入相关
# ══════════════════════════════════════════════════════════════════════════════

VALID_IMPORT_TYPES = {
    "org_structure", "roster", "okr", "kpi", "dept_mission",
    "biz_process", "terminology", "data_asset", "collab_matrix", "access_matrix",
}


@router.post("/import/upload")
async def import_upload(
    import_type: str = Query(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """上传 CSV/XLSX，创建 import session，触发 AI 解析"""
    if import_type not in VALID_IMPORT_TYPES:
        raise HTTPException(400, f"不支持的导入类型: {import_type}")

    from app.services.org_import_service import create_import_session, parse_upload_file, run_ai_parse

    content = await file.read()
    rows, row_count = parse_upload_file(content, file.filename or "unknown.csv")

    # 保存文件
    upload_dir = os.path.join("./uploads", "org_imports")
    os.makedirs(upload_dir, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    saved_path = os.path.join(upload_dir, f"{ts}_{file.filename}")
    with open(saved_path, "wb") as f:
        f.write(content)

    session = create_import_session(db, import_type, file.filename, saved_path, rows, row_count, user.id)

    # 获取 LLM 配置
    from app.models.skill import ModelConfig
    model_cfg = db.query(ModelConfig).filter(ModelConfig.is_default == True).first()  # noqa: E712
    model_dict = None
    if model_cfg:
        model_dict = {
            "api_base": model_cfg.api_base,
            "api_key": model_cfg.api_key,
            "api_key_env": model_cfg.api_key_env,
            "model_id": model_cfg.model_id,
            "provider": model_cfg.provider,
        }

    await run_ai_parse(db, session, model_dict)
    db.commit()

    return {
        "id": session.id,
        "status": session.status,
        "row_count": session.row_count,
        "parsed_count": session.parsed_count,
        "ai_parse_note": session.ai_parse_note,
    }


@router.get("/import/sessions")
def list_import_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """导入历史列表"""
    sessions = db.query(OrgImportSession).order_by(OrgImportSession.created_at.desc()).limit(100).all()
    return [
        {
            "id": s.id,
            "import_type": s.import_type,
            "file_name": s.file_name,
            "status": s.status,
            "row_count": s.row_count,
            "parsed_count": s.parsed_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "applied_at": s.applied_at.isoformat() if s.applied_at else None,
        }
        for s in sessions
    ]


@router.get("/import/sessions/{session_id}")
def get_import_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """导入详情"""
    s = db.query(OrgImportSession).get(session_id)
    if not s:
        raise HTTPException(404, "导入会话不存在")
    return {
        "id": s.id,
        "import_type": s.import_type,
        "file_name": s.file_name,
        "status": s.status,
        "row_count": s.row_count,
        "parsed_count": s.parsed_count,
        "raw_data": s.raw_data,
        "ai_parsed_data": s.ai_parsed_data,
        "ai_parse_note": s.ai_parse_note,
        "error_rows": s.error_rows,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "applied_at": s.applied_at.isoformat() if s.applied_at else None,
    }


class ParsedDataUpdate(BaseModel):
    ai_parsed_data: list | dict


@router.put("/import/sessions/{session_id}/parsed-data")
def update_parsed_data(
    session_id: int,
    body: ParsedDataUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """用户修正 AI 解析结果"""
    s = db.query(OrgImportSession).get(session_id)
    if not s:
        raise HTTPException(404, "导入会话不存在")
    if s.status not in ("parsed", "confirmed"):
        raise HTTPException(400, f"当前状态 {s.status} 不允许修改")
    s.ai_parsed_data = body.ai_parsed_data
    s.status = "parsed"
    db.commit()
    return {"ok": True}


@router.post("/import/sessions/{session_id}/confirm")
def confirm_import(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """确认解析结果"""
    from app.services.org_import_service import confirm_session
    session = confirm_session(db, session_id)
    db.commit()
    return {"id": session.id, "status": session.status}


@router.post("/import/sessions/{session_id}/apply")
def apply_import(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """写入正式表 + 触发基线联动"""
    from app.services.org_import_service import apply_session
    session = apply_session(db, session_id, user.id)
    db.commit()
    return {"id": session.id, "status": session.status, "applied_at": session.applied_at.isoformat() if session.applied_at else None}


@router.post("/import/sessions/{session_id}/reparse")
async def reparse_import(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """重新 AI 解析"""
    s = db.query(OrgImportSession).get(session_id)
    if not s:
        raise HTTPException(404, "导入会话不存在")

    from app.models.skill import ModelConfig
    from app.services.org_import_service import run_ai_parse
    model_cfg = db.query(ModelConfig).filter(ModelConfig.is_default == True).first()  # noqa: E712
    model_dict = None
    if model_cfg:
        model_dict = {
            "api_base": model_cfg.api_base, "api_key": model_cfg.api_key,
            "api_key_env": model_cfg.api_key_env, "model_id": model_cfg.model_id,
            "provider": model_cfg.provider,
        }
    await run_ai_parse(db, s, model_dict)
    db.commit()
    return {"id": s.id, "status": s.status, "ai_parse_note": s.ai_parse_note}


# ══════════════════════════════════════════════════════════════════════════════
# 4.2 组织架构 CRUD
# ══════════════════════════════════════════════════════════════════════════════

def _dept_dict(d: Department) -> dict:
    return {
        "id": d.id, "name": d.name, "parent_id": d.parent_id,
        "category": d.category, "business_unit": d.business_unit,
        "code": d.code, "level": d.level,
        "headcount_budget": d.headcount_budget,
        "lifecycle_status": d.lifecycle_status,
        "established_at": d.established_at.isoformat() if d.established_at else None,
        "dissolved_at": d.dissolved_at.isoformat() if d.dissolved_at else None,
        "sort_order": d.sort_order,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.get("/departments")
def list_departments(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """部门树"""
    depts = db.query(Department).order_by(Department.sort_order, Department.id).all()
    return [_dept_dict(d) for d in depts]


class DeptCreate(BaseModel):
    name: str
    parent_id: int | None = None
    category: str | None = None
    business_unit: str | None = None
    code: str | None = None
    level: str | None = None
    headcount_budget: int | None = None
    lifecycle_status: str = "active"


@router.post("/departments")
def create_department(
    body: DeptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    dept = Department(**body.model_dump())
    db.add(dept)
    db.flush()
    track_create(db, "department", dept, user.id)
    db.commit()
    return _dept_dict(dept)


class DeptUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    category: str | None = None
    business_unit: str | None = None
    code: str | None = None
    level: str | None = None
    headcount_budget: int | None = None
    lifecycle_status: str | None = None
    sort_order: int | None = None


@router.put("/departments/{dept_id}")
def update_department(
    dept_id: int,
    body: DeptUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    dept = db.query(Department).get(dept_id)
    if not dept:
        raise HTTPException(404, "部门不存在")
    old = model_to_dict(dept)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(dept, k, v)
    track_update(db, "department", dept_id, old, model_to_dict(dept), user.id)
    db.commit()
    return _dept_dict(dept)


@router.delete("/departments/{dept_id}")
def delete_department(
    dept_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    dept = db.query(Department).get(dept_id)
    if not dept:
        raise HTTPException(404, "部门不存在")
    old = model_to_dict(dept)
    dept.lifecycle_status = "dissolved"
    dept.dissolved_at = datetime.date.today()
    track_update(db, "department", dept_id, old, model_to_dict(dept), user.id)
    db.commit()
    return {"ok": True}


@router.get("/departments/{dept_id}/governance-stats")
def dept_governance_stats(
    dept_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """该部门治理统计"""
    stats = db.query(GovernanceStrategyStat).filter(
        GovernanceStrategyStat.department_id == dept_id
    ).all()
    total = sum(s.total_count for s in stats)
    success = sum(s.success_count for s in stats)
    return {
        "department_id": dept_id,
        "total_strategies": len(stats),
        "total_count": total,
        "success_count": success,
        "coverage_rate": round(success / total * 100, 1) if total > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4.3 花名册增强
# ══════════════════════════════════════════════════════════════════════════════

def _user_org_dict(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": u.role.value, "department_id": u.department_id,
        "position_id": u.position_id, "report_to_id": u.report_to_id,
        "is_active": u.is_active,
        "employee_no": u.employee_no, "employee_status": u.employee_status,
        "job_title": u.job_title, "job_level": u.job_level,
        "entry_date": u.entry_date.isoformat() if u.entry_date else None,
        "exit_date": u.exit_date.isoformat() if u.exit_date else None,
        "avatar_url": u.avatar_url,
    }


@router.get("/roster")
def list_roster(
    department_id: int | None = None,
    employee_status: str | None = None,
    position_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(User).filter(User.username != "_system")
    if department_id:
        q = q.filter(User.department_id == department_id)
    if employee_status:
        q = q.filter(User.employee_status == employee_status)
    if position_id:
        q = q.filter(User.position_id == position_id)
    users = q.order_by(User.id).all()
    return [_user_org_dict(u) for u in users]


@router.get("/roster/stats")
def roster_stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    users = db.query(User).filter(User.username != "_system").all()
    depts = db.query(Department).all()
    dept_map = {d.id: d.name for d in depts}

    by_dept: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_level: dict[str, int] = {}
    for u in users:
        dept_name = dept_map.get(u.department_id, "未分配")
        by_dept[dept_name] = by_dept.get(dept_name, 0) + 1
        status = u.employee_status or "active"
        by_status[status] = by_status.get(status, 0) + 1
        level = u.job_level or "未设置"
        by_level[level] = by_level.get(level, 0) + 1

    return {
        "total": len(users),
        "by_department": by_dept,
        "by_status": by_status,
        "by_level": by_level,
    }


@router.get("/roster/{user_id}")
def get_roster_detail(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    result = _user_org_dict(target)

    # 附加 OKR 历史
    okrs = db.query(OkrObjective).filter(
        OkrObjective.owner_type == "user",
        OkrObjective.owner_id == user_id,
    ).order_by(OkrObjective.created_at.desc()).limit(20).all()
    result["okr_history"] = [
        {"id": o.id, "title": o.title, "progress": o.progress, "status": o.status, "period_id": o.period_id}
        for o in okrs
    ]

    # 附加 KPI 历史
    kpis = db.query(KpiAssignment).filter(
        KpiAssignment.user_id == user_id,
    ).order_by(KpiAssignment.created_at.desc()).limit(20).all()
    result["kpi_history"] = [
        {"id": k.id, "period_id": k.period_id, "total_score": k.total_score, "level": k.level, "status": k.status}
        for k in kpis
    ]

    return result


class RosterUpdate(BaseModel):
    employee_no: str | None = None
    employee_status: str | None = None
    job_title: str | None = None
    job_level: str | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    department_id: int | None = None
    position_id: int | None = None


@router.put("/roster/{user_id}")
def update_roster(
    user_id: int,
    body: RosterUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    target = db.query(User).get(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    old = model_to_dict(target)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(target, k, v)
    track_update(db, "user", user_id, old, model_to_dict(target), user.id)
    db.commit()
    return _user_org_dict(target)


# ══════════════════════════════════════════════════════════════════════════════
# 4.4 OKR 管理
# ══════════════════════════════════════════════════════════════════════════════

def _period_dict(p: OkrPeriod) -> dict:
    return {
        "id": p.id, "name": p.name, "period_type": p.period_type,
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "status": p.status,
    }


@router.get("/okr/periods")
def list_okr_periods(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    periods = db.query(OkrPeriod).order_by(OkrPeriod.start_date.desc()).all()
    return [_period_dict(p) for p in periods]


class PeriodCreate(BaseModel):
    name: str
    period_type: str = "quarter"
    start_date: str
    end_date: str


@router.post("/okr/periods")
def create_period(body: PeriodCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    period = OkrPeriod(name=body.name, period_type=body.period_type, start_date=body.start_date, end_date=body.end_date, created_by=user.id)
    db.add(period)
    db.flush()
    track_create(db, "okr_period", period, user.id)
    db.commit()
    return _period_dict(period)


class PeriodUpdate(BaseModel):
    name: str | None = None
    status: str | None = None


@router.put("/okr/periods/{period_id}")
def update_period(period_id: int, body: PeriodUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = db.query(OkrPeriod).get(period_id)
    if not p:
        raise HTTPException(404, "周期不存在")
    old = model_to_dict(p)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    track_update(db, "okr_period", period_id, old, model_to_dict(p), user.id)
    db.commit()
    return _period_dict(p)


def _objective_dict(o: OkrObjective) -> dict:
    return {
        "id": o.id, "period_id": o.period_id, "owner_type": o.owner_type,
        "owner_id": o.owner_id, "parent_objective_id": o.parent_objective_id,
        "title": o.title, "weight": o.weight, "progress": o.progress,
        "status": o.status, "sort_order": o.sort_order,
    }


@router.get("/okr/objectives")
def list_objectives(
    period_id: int | None = None,
    owner_type: str | None = None,
    owner_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(OkrObjective)
    if period_id:
        q = q.filter(OkrObjective.period_id == period_id)
    if owner_type:
        q = q.filter(OkrObjective.owner_type == owner_type)
    if owner_id is not None:
        q = q.filter(OkrObjective.owner_id == owner_id)
    objs = q.order_by(OkrObjective.sort_order, OkrObjective.id).all()
    return [_objective_dict(o) for o in objs]


class ObjectiveCreate(BaseModel):
    period_id: int
    owner_type: str = "company"
    owner_id: int = 0
    parent_objective_id: int | None = None
    title: str
    weight: float = 1.0


@router.post("/okr/objectives")
def create_objective(body: ObjectiveCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    obj = OkrObjective(**body.model_dump(), created_by=user.id)
    db.add(obj)
    db.flush()
    track_create(db, "okr_objective", obj, user.id)
    db.commit()
    return _objective_dict(obj)


class ObjectiveUpdate(BaseModel):
    title: str | None = None
    weight: float | None = None
    progress: float | None = None
    status: str | None = None
    sort_order: int | None = None
    parent_objective_id: int | None = None


@router.put("/okr/objectives/{obj_id}")
def update_objective(obj_id: int, body: ObjectiveUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    obj = db.query(OkrObjective).get(obj_id)
    if not obj:
        raise HTTPException(404, "目标不存在")
    old = model_to_dict(obj)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(obj, k, v)
    track_update(db, "okr_objective", obj_id, old, model_to_dict(obj), user.id)
    db.commit()
    return _objective_dict(obj)


@router.delete("/okr/objectives/{obj_id}")
def delete_objective(obj_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    obj = db.query(OkrObjective).get(obj_id)
    if not obj:
        raise HTTPException(404, "目标不存在")
    track_delete(db, "okr_objective", obj, user.id)
    db.delete(obj)
    db.commit()
    return {"ok": True}


def _kr_dict(kr: OkrKeyResult) -> dict:
    return {
        "id": kr.id, "objective_id": kr.objective_id, "title": kr.title,
        "metric_type": kr.metric_type, "target_value": kr.target_value,
        "current_value": kr.current_value, "unit": kr.unit,
        "weight": kr.weight, "progress": kr.progress, "status": kr.status,
        "owner_user_id": kr.owner_user_id, "sort_order": kr.sort_order,
    }


@router.get("/okr/objectives/{obj_id}/key-results")
def list_key_results(obj_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    krs = db.query(OkrKeyResult).filter(OkrKeyResult.objective_id == obj_id).order_by(OkrKeyResult.sort_order).all()
    return [_kr_dict(kr) for kr in krs]


class KrCreate(BaseModel):
    objective_id: int
    title: str
    metric_type: str = "number"
    target_value: str | None = None
    unit: str | None = None
    weight: float = 1.0
    owner_user_id: int | None = None


@router.post("/okr/key-results")
def create_key_result(body: KrCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    kr = OkrKeyResult(**body.model_dump())
    db.add(kr)
    db.flush()
    track_create(db, "okr_key_result", kr, user.id)
    db.commit()
    return _kr_dict(kr)


class KrUpdate(BaseModel):
    title: str | None = None
    target_value: str | None = None
    current_value: str | None = None
    progress: float | None = None
    status: str | None = None
    weight: float | None = None
    sort_order: int | None = None


@router.put("/okr/key-results/{kr_id}")
def update_key_result(kr_id: int, body: KrUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    kr = db.query(OkrKeyResult).get(kr_id)
    if not kr:
        raise HTTPException(404, "KR 不存在")
    old = model_to_dict(kr)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(kr, k, v)
    track_update(db, "okr_key_result", kr_id, old, model_to_dict(kr), user.id)
    db.commit()
    return _kr_dict(kr)


@router.delete("/okr/key-results/{kr_id}")
def delete_key_result(kr_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    kr = db.query(OkrKeyResult).get(kr_id)
    if not kr:
        raise HTTPException(404, "KR 不存在")
    track_delete(db, "okr_key_result", kr, user.id)
    db.delete(kr)
    db.commit()
    return {"ok": True}


@router.get("/okr/tree")
def okr_tree(
    period_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """OKR 对齐树"""
    objectives = db.query(OkrObjective).filter(OkrObjective.period_id == period_id).order_by(OkrObjective.sort_order).all()
    obj_map = {o.id: {**_objective_dict(o), "key_results": [], "children": []} for o in objectives}

    for o in objectives:
        krs = db.query(OkrKeyResult).filter(OkrKeyResult.objective_id == o.id).order_by(OkrKeyResult.sort_order).all()
        obj_map[o.id]["key_results"] = [_kr_dict(kr) for kr in krs]

    roots = []
    for o in objectives:
        node = obj_map[o.id]
        if o.parent_objective_id and o.parent_objective_id in obj_map:
            obj_map[o.parent_objective_id]["children"].append(node)
        else:
            roots.append(node)

    return roots


@router.post("/okr/recalc-progress")
def recalc_progress(
    period_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """批量重算 O 的完成度"""
    objectives = db.query(OkrObjective).filter(OkrObjective.period_id == period_id).all()
    updated = 0
    for obj in objectives:
        krs = db.query(OkrKeyResult).filter(OkrKeyResult.objective_id == obj.id).all()
        if not krs:
            continue
        total_weight = sum(kr.weight for kr in krs) or 1.0
        weighted_progress = sum(kr.progress * kr.weight for kr in krs) / total_weight
        if abs(obj.progress - weighted_progress) > 0.01:
            obj.progress = round(weighted_progress, 2)
            updated += 1
    db.commit()
    return {"updated": updated}


# ══════════════════════════════════════════════════════════════════════════════
# 4.5 绩效 KPI
# ══════════════════════════════════════════════════════════════════════════════

def _kpi_dict(k: KpiAssignment) -> dict:
    return {
        "id": k.id, "user_id": k.user_id, "period_id": k.period_id,
        "position_id": k.position_id, "department_id": k.department_id,
        "kpi_data": k.kpi_data, "total_score": k.total_score,
        "level": k.level, "evaluator_id": k.evaluator_id, "status": k.status,
    }


@router.get("/kpi/assignments")
def list_kpi_assignments(
    period_id: int | None = None,
    department_id: int | None = None,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(KpiAssignment)
    if period_id:
        q = q.filter(KpiAssignment.period_id == period_id)
    if department_id:
        q = q.filter(KpiAssignment.department_id == department_id)
    if user_id:
        q = q.filter(KpiAssignment.user_id == user_id)
    assignments = q.order_by(KpiAssignment.id).all()
    return [_kpi_dict(k) for k in assignments]


@router.get("/kpi/assignments/{assignment_id}")
def get_kpi_assignment(assignment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    k = db.query(KpiAssignment).get(assignment_id)
    if not k:
        raise HTTPException(404, "KPI 分配不存在")
    return _kpi_dict(k)


class KpiUpdate(BaseModel):
    kpi_data: list | None = None
    total_score: float | None = None
    level: str | None = None
    status: str | None = None
    evaluator_id: int | None = None


@router.put("/kpi/assignments/{assignment_id}")
def update_kpi_assignment(assignment_id: int, body: KpiUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    k = db.query(KpiAssignment).get(assignment_id)
    if not k:
        raise HTTPException(404, "KPI 分配不存在")
    old = model_to_dict(k)
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(k, field, val)
    track_update(db, "kpi_assignment", assignment_id, old, model_to_dict(k), user.id)
    db.commit()
    return _kpi_dict(k)


@router.get("/kpi/summary")
def kpi_summary(
    period_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assignments = db.query(KpiAssignment).filter(KpiAssignment.period_id == period_id).all()
    level_dist: dict[str, int] = {}
    dept_scores: dict[int, list[float]] = {}
    for k in assignments:
        if k.level:
            level_dist[k.level] = level_dist.get(k.level, 0) + 1
        if k.department_id and k.total_score is not None:
            dept_scores.setdefault(k.department_id, []).append(k.total_score)

    dept_avg = {dept_id: round(sum(scores) / len(scores), 1) for dept_id, scores in dept_scores.items()}
    return {"total": len(assignments), "level_distribution": level_dist, "department_avg_scores": dept_avg}


# ══════════════════════════════════════════════════════════════════════════════
# 4.6 部门职责
# ══════════════════════════════════════════════════════════════════════════════

def _mission_dict(m: DeptMissionDetail) -> dict:
    return {
        "id": m.id, "department_id": m.department_id,
        "mission_summary": m.mission_summary,
        "core_functions": m.core_functions,
        "upstream_deps": m.upstream_deps,
        "downstream_deliveries": m.downstream_deliveries,
        "owned_data_types": m.owned_data_types,
    }


@router.get("/dept-missions")
def list_dept_missions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    missions = db.query(DeptMissionDetail).all()
    return [_mission_dict(m) for m in missions]


@router.get("/dept-missions/{dept_id}")
def get_dept_mission(dept_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = db.query(DeptMissionDetail).filter(DeptMissionDetail.department_id == dept_id).first()
    if not m:
        raise HTTPException(404, "部门职责不存在")
    return _mission_dict(m)


class MissionUpdate(BaseModel):
    mission_summary: str | None = None
    core_functions: list | None = None
    upstream_deps: list | None = None
    downstream_deliveries: list | None = None
    owned_data_types: list | None = None


@router.put("/dept-missions/{dept_id}")
def update_dept_mission(dept_id: int, body: MissionUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    m = db.query(DeptMissionDetail).filter(DeptMissionDetail.department_id == dept_id).first()
    if not m:
        m = DeptMissionDetail(department_id=dept_id, created_by=user.id)
        db.add(m)
        db.flush()

    old = model_to_dict(m)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(m, k, v)
    track_update(db, "dept_mission", m.id, old, model_to_dict(m), user.id)

    # 同步到 GovernanceDepartmentMission
    from app.services.org_baseline_sync import sync_to_governance
    evt = OrgChangeEvent(
        entity_type="dept_mission", entity_id=m.id,
        change_type="updated", change_source="manual", created_by=user.id,
    )
    db.add(evt)
    db.flush()
    sync_to_governance(db, evt)

    db.commit()
    return _mission_dict(m)


# ══════════════════════════════════════════════════════════════════════════════
# 4.7 业务流程
# ══════════════════════════════════════════════════════════════════════════════

def _process_dict(p: BizProcess) -> dict:
    return {
        "id": p.id, "name": p.name, "code": p.code,
        "description": p.description, "process_nodes": p.process_nodes,
        "is_active": p.is_active,
    }


@router.get("/biz-processes")
def list_biz_processes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    procs = db.query(BizProcess).order_by(BizProcess.id).all()
    return [_process_dict(p) for p in procs]


class ProcessCreate(BaseModel):
    name: str
    code: str
    description: str | None = None
    process_nodes: list = Field(default_factory=list)


@router.post("/biz-processes")
def create_biz_process(body: ProcessCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = BizProcess(**body.model_dump(), created_by=user.id)
    db.add(p)
    db.flush()
    track_create(db, "biz_process", p, user.id)
    db.commit()
    return _process_dict(p)


class ProcessUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    process_nodes: list | None = None
    is_active: bool | None = None


@router.put("/biz-processes/{process_id}")
def update_biz_process(process_id: int, body: ProcessUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = db.query(BizProcess).get(process_id)
    if not p:
        raise HTTPException(404, "流程不存在")
    old = model_to_dict(p)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    track_update(db, "biz_process", process_id, old, model_to_dict(p), user.id)
    db.commit()
    return _process_dict(p)


@router.delete("/biz-processes/{process_id}")
def delete_biz_process(process_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = db.query(BizProcess).get(process_id)
    if not p:
        raise HTTPException(404, "流程不存在")
    track_delete(db, "biz_process", p, user.id)
    db.delete(p)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 4.8 业务术语
# ══════════════════════════════════════════════════════════════════════════════

def _term_dict(t: BizTerminology) -> dict:
    return {
        "id": t.id, "term": t.term, "aliases": t.aliases,
        "definition": t.definition,
        "resource_library_code": t.resource_library_code,
        "department_id": t.department_id,
    }


@router.get("/terminologies")
def list_terminologies(
    search: str | None = None,
    resource_library_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(BizTerminology)
    if search:
        q = q.filter(BizTerminology.term.contains(search))
    if resource_library_code:
        q = q.filter(BizTerminology.resource_library_code == resource_library_code)
    terms = q.order_by(BizTerminology.id).all()
    return [_term_dict(t) for t in terms]


class TermCreate(BaseModel):
    term: str
    aliases: list = Field(default_factory=list)
    definition: str | None = None
    resource_library_code: str | None = None
    department_id: int | None = None


@router.post("/terminologies")
def create_terminology(body: TermCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    t = BizTerminology(**body.model_dump())
    db.add(t)
    db.flush()
    track_create(db, "terminology", t, user.id)
    db.commit()
    return _term_dict(t)


class TermUpdate(BaseModel):
    term: str | None = None
    aliases: list | None = None
    definition: str | None = None
    resource_library_code: str | None = None
    department_id: int | None = None


@router.put("/terminologies/{term_id}")
def update_terminology(term_id: int, body: TermUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    t = db.query(BizTerminology).get(term_id)
    if not t:
        raise HTTPException(404, "术语不存在")
    old = model_to_dict(t)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    track_update(db, "terminology", term_id, old, model_to_dict(t), user.id)

    # 同步到治理引擎
    from app.services.org_baseline_sync import sync_to_governance
    evt = OrgChangeEvent(entity_type="terminology", entity_id=term_id, change_type="updated", change_source="manual", created_by=user.id)
    db.add(evt)
    db.flush()
    sync_to_governance(db, evt)

    db.commit()
    return _term_dict(t)


@router.delete("/terminologies/{term_id}")
def delete_terminology(term_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    t = db.query(BizTerminology).get(term_id)
    if not t:
        raise HTTPException(404, "术语不存在")
    track_delete(db, "terminology", t, user.id)
    db.delete(t)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 4.9 数据资产归属
# ══════════════════════════════════════════════════════════════════════════════

def _asset_dict(a: DataAssetOwnership) -> dict:
    return {
        "id": a.id, "asset_name": a.asset_name, "asset_code": a.asset_code,
        "owner_department_id": a.owner_department_id,
        "update_frequency": a.update_frequency,
        "consumer_department_ids": a.consumer_department_ids,
        "resource_library_code": a.resource_library_code,
        "description": a.description,
    }


@router.get("/data-assets")
def list_data_assets(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assets = db.query(DataAssetOwnership).order_by(DataAssetOwnership.id).all()
    return [_asset_dict(a) for a in assets]


class AssetCreate(BaseModel):
    asset_name: str
    asset_code: str
    owner_department_id: int
    update_frequency: str = "manual"
    consumer_department_ids: list = Field(default_factory=list)
    resource_library_code: str | None = None
    description: str | None = None


@router.post("/data-assets")
def create_data_asset(body: AssetCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    a = DataAssetOwnership(**body.model_dump())
    db.add(a)
    db.flush()
    track_create(db, "data_asset", a, user.id)
    db.commit()
    return _asset_dict(a)


class AssetUpdate(BaseModel):
    asset_name: str | None = None
    owner_department_id: int | None = None
    update_frequency: str | None = None
    consumer_department_ids: list | None = None
    resource_library_code: str | None = None
    description: str | None = None


@router.put("/data-assets/{asset_id}")
def update_data_asset(asset_id: int, body: AssetUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    a = db.query(DataAssetOwnership).get(asset_id)
    if not a:
        raise HTTPException(404, "数据资产不存在")
    old = model_to_dict(a)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(a, k, v)
    track_update(db, "data_asset", asset_id, old, model_to_dict(a), user.id)

    from app.services.org_baseline_sync import sync_to_governance
    evt = OrgChangeEvent(entity_type="data_asset", entity_id=asset_id, change_type="updated", change_source="manual", created_by=user.id)
    db.add(evt)
    db.flush()
    sync_to_governance(db, evt)

    db.commit()
    return _asset_dict(a)


@router.delete("/data-assets/{asset_id}")
def delete_data_asset(asset_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    a = db.query(DataAssetOwnership).get(asset_id)
    if not a:
        raise HTTPException(404, "数据资产不存在")
    track_delete(db, "data_asset", a, user.id)
    db.delete(a)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 4.10 协作矩阵
# ══════════════════════════════════════════════════════════════════════════════

def _collab_dict(c: DeptCollaborationLink) -> dict:
    return {
        "id": c.id, "dept_a_id": c.dept_a_id, "dept_b_id": c.dept_b_id,
        "frequency": c.frequency, "scenarios": c.scenarios,
    }


@router.get("/collab-links")
def list_collab_links(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    links = db.query(DeptCollaborationLink).order_by(DeptCollaborationLink.id).all()
    return [_collab_dict(c) for c in links]


class CollabCreate(BaseModel):
    dept_a_id: int
    dept_b_id: int
    frequency: str = "medium"
    scenarios: list = Field(default_factory=list)


@router.post("/collab-links")
def create_collab_link(body: CollabCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    c = DeptCollaborationLink(**body.model_dump())
    db.add(c)
    db.flush()
    track_create(db, "collab_link", c, user.id)
    db.commit()
    return _collab_dict(c)


class CollabUpdate(BaseModel):
    frequency: str | None = None
    scenarios: list | None = None


@router.put("/collab-links/{link_id}")
def update_collab_link(link_id: int, body: CollabUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    c = db.query(DeptCollaborationLink).get(link_id)
    if not c:
        raise HTTPException(404, "协作关系不存在")
    old = model_to_dict(c)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    track_update(db, "collab_link", link_id, old, model_to_dict(c), user.id)
    db.commit()
    return _collab_dict(c)


@router.delete("/collab-links/{link_id}")
def delete_collab_link(link_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    c = db.query(DeptCollaborationLink).get(link_id)
    if not c:
        raise HTTPException(404, "协作关系不存在")
    track_delete(db, "collab_link", c, user.id)
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/collab-matrix")
def collab_matrix_view(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """矩阵视图 — 部门×部门 热力图数据"""
    depts = db.query(Department).filter(Department.lifecycle_status != "dissolved").order_by(Department.sort_order).all()
    links = db.query(DeptCollaborationLink).all()

    freq_map = {"high": 3, "medium": 2, "low": 1}
    matrix: dict[str, dict[str, int]] = {}
    for link in links:
        key_ab = f"{link.dept_a_id}-{link.dept_b_id}"
        key_ba = f"{link.dept_b_id}-{link.dept_a_id}"
        val = freq_map.get(link.frequency, 0)
        matrix[key_ab] = {"value": val, "scenarios": link.scenarios}
        matrix[key_ba] = {"value": val, "scenarios": link.scenarios}

    return {
        "departments": [{"id": d.id, "name": d.name} for d in depts],
        "matrix": matrix,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4.11 访问矩阵
# ══════════════════════════════════════════════════════════════════════════════

def _access_dict(r: PositionAccessRule) -> dict:
    return {
        "id": r.id, "position_id": r.position_id,
        "data_domain": r.data_domain, "access_range": r.access_range,
        "excluded_fields": r.excluded_fields,
    }


@router.get("/access-rules")
def list_access_rules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rules = db.query(PositionAccessRule).order_by(PositionAccessRule.position_id).all()
    return [_access_dict(r) for r in rules]


class AccessRuleBatch(BaseModel):
    rules: list[dict]  # [{position_id, data_domain, access_range, excluded_fields}]


@router.put("/access-rules")
def batch_update_access_rules(body: AccessRuleBatch, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    """批量更新 → 同步 DataScopePolicy"""
    from app.services.org_baseline_sync import sync_to_governance

    updated = 0
    for rule_data in body.rules:
        pos_id = rule_data.get("position_id")
        domain = rule_data.get("data_domain")
        if not pos_id or not domain:
            continue

        existing = db.query(PositionAccessRule).filter(
            PositionAccessRule.position_id == pos_id,
            PositionAccessRule.data_domain == domain,
        ).first()

        if existing:
            old = model_to_dict(existing)
            existing.access_range = rule_data.get("access_range", existing.access_range)
            existing.excluded_fields = rule_data.get("excluded_fields", existing.excluded_fields)
            track_update(db, "access_rule", existing.id, old, model_to_dict(existing), user.id)
            evt = OrgChangeEvent(entity_type="access_rule", entity_id=existing.id, change_type="updated", change_source="manual", created_by=user.id)
            db.add(evt)
            db.flush()
            sync_to_governance(db, evt)
        else:
            rule = PositionAccessRule(
                position_id=pos_id,
                data_domain=domain,
                access_range=rule_data.get("access_range", "none"),
                excluded_fields=rule_data.get("excluded_fields", []),
            )
            db.add(rule)
            db.flush()
            track_create(db, "access_rule", rule, user.id)
            evt = OrgChangeEvent(entity_type="access_rule", entity_id=rule.id, change_type="created", change_source="manual", created_by=user.id)
            db.add(evt)
            db.flush()
            sync_to_governance(db, evt)
        updated += 1

    db.commit()
    return {"updated": updated}


@router.get("/access-matrix")
def access_matrix_view(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """矩阵视图 — 岗位×数据域"""
    positions = db.query(Position).order_by(Position.sort_order, Position.id).all()
    rules = db.query(PositionAccessRule).all()

    data_domains = sorted(set(r.data_domain for r in rules))
    matrix: dict[str, dict] = {}
    for r in rules:
        key = f"{r.position_id}-{r.data_domain}"
        matrix[key] = {"access_range": r.access_range, "excluded_fields": r.excluded_fields}

    return {
        "positions": [{"id": p.id, "name": p.name} for p in positions],
        "data_domains": data_domains,
        "matrix": matrix,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4.12 变更追踪
# ══════════════════════════════════════════════════════════════════════════════

def _event_dict(e: OrgChangeEvent) -> dict:
    return {
        "id": e.id, "entity_type": e.entity_type, "entity_id": e.entity_id,
        "change_type": e.change_type, "field_changes": e.field_changes,
        "change_source": e.change_source, "import_session_id": e.import_session_id,
        "baseline_version": e.baseline_version,
        "created_by": e.created_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/change-events")
def list_change_events(
    entity_type: str | None = None,
    change_type: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(OrgChangeEvent)
    if entity_type:
        q = q.filter(OrgChangeEvent.entity_type == entity_type)
    if change_type:
        q = q.filter(OrgChangeEvent.change_type == change_type)
    events = q.order_by(OrgChangeEvent.created_at.desc()).limit(limit).all()
    return [_event_dict(e) for e in events]


@router.get("/change-events/{entity_type}/{entity_id}")
def get_entity_changes(
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    events = db.query(OrgChangeEvent).filter(
        OrgChangeEvent.entity_type == entity_type,
        OrgChangeEvent.entity_id == entity_id,
    ).order_by(OrgChangeEvent.created_at.desc()).all()
    return [_event_dict(e) for e in events]


@router.get("/change-events/timeline")
def change_timeline(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """时间线视图（最近 N 条变更）"""
    events = db.query(OrgChangeEvent).order_by(OrgChangeEvent.created_at.desc()).limit(limit).all()
    return [_event_dict(e) for e in events]


# ══════════════════════════════════════════════════════════════════════════════
# 4.13 基线版本中心（V2）
# ══════════════════════════════════════════════════════════════════════════════

from app.models.org_management import (
    OrgBaseline, PositionCompetencyModel, ResourceLibraryDefinition,
    KrResourceMapping, CollabProtocol, OrgChangeImpact,
)


@router.get("/baseline-status")
def baseline_status(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """基线控制台聚合数据：当前/候选版本 + 治理覆盖率 + 未同步项 + 偏离告警"""
    from app.services.org_baseline_sync import get_governance_sync_status
    return get_governance_sync_status(db)


@router.get("/baselines")
def list_baselines(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """所有基线版本列表"""
    baselines = db.query(OrgBaseline).order_by(OrgBaseline.created_at.desc()).all()
    return [_baseline_dict(b) for b in baselines]


@router.get("/baselines/{baseline_id}")
def get_baseline(baseline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    b = db.query(OrgBaseline).get(baseline_id)
    if not b:
        raise HTTPException(404, "基线版本不存在")
    result = _baseline_dict(b)
    # 附加影响面记录
    impacts = db.query(OrgChangeImpact).filter(OrgChangeImpact.baseline_id == baseline_id).order_by(OrgChangeImpact.created_at.desc()).all()
    result["impacts"] = [
        {"id": i.id, "impact_type": i.impact_type, "target_type": i.impact_target_type,
         "target_id": i.impact_target_id, "target_name": i.impact_target_name,
         "severity": i.severity, "description": i.description, "resolved": i.resolved,
         "created_at": i.created_at.isoformat() if i.created_at else None}
        for i in impacts
    ]
    return result


class BaselineCreate(BaseModel):
    note: str | None = None


@router.post("/baselines/create-candidate")
def create_candidate(body: BaselineCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    """创建候选基线版本"""
    from app.services.org_baseline_sync import create_candidate_baseline
    baseline = create_candidate_baseline(db, user.id, body.note)
    db.commit()
    return _baseline_dict(baseline)


@router.post("/baselines/{baseline_id}/activate")
def activate(baseline_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    """激活候选基线"""
    from app.services.org_baseline_sync import activate_baseline
    baseline = activate_baseline(db, baseline_id, user.id)
    db.commit()
    return _baseline_dict(baseline)


@router.post("/baseline-status/force-snapshot")
def force_snapshot(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """手动触发：创建候选 → 立即激活"""
    from app.services.org_baseline_sync import create_candidate_baseline, activate_baseline, get_active_baseline, create_initial_baseline

    active = get_active_baseline(db)
    if not active:
        baseline = create_initial_baseline(db, user.id, 0)
    else:
        baseline = create_candidate_baseline(db, user.id, "手动触发", "manual")
        baseline = activate_baseline(db, baseline.id, user.id)

    db.commit()
    return {"version": baseline.version, "id": baseline.id}


def _baseline_dict(b: OrgBaseline) -> dict:
    return {
        "id": b.id, "version": b.version, "version_type": b.version_type,
        "status": b.status, "snapshot_summary": b.snapshot_summary,
        "diff_from_previous": b.diff_from_previous,
        "impact_analysis": b.impact_analysis,
        "governance_snapshot_id": b.governance_snapshot_id,
        "trigger_source": b.trigger_source,
        "created_by": b.created_by,
        "activated_at": b.activated_at.isoformat() if b.activated_at else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "note": b.note,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4.14 岗位能力模型
# ══════════════════════════════════════════════════════════════════════════════

def _competency_dict(c: PositionCompetencyModel) -> dict:
    return {
        "id": c.id, "position_id": c.position_id,
        "responsibilities": c.responsibilities, "competencies": c.competencies,
        "output_standards": c.output_standards, "career_path": c.career_path,
    }


@router.get("/competency-models")
def list_competency_models(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    models = db.query(PositionCompetencyModel).all()
    return [_competency_dict(c) for c in models]


@router.get("/competency-models/{position_id}")
def get_competency_model(position_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    c = db.query(PositionCompetencyModel).filter(PositionCompetencyModel.position_id == position_id).first()
    if not c:
        raise HTTPException(404, "岗位能力模型不存在")
    return _competency_dict(c)


class CompetencyUpdate(BaseModel):
    responsibilities: list | None = None
    competencies: list | None = None
    output_standards: list | None = None
    career_path: list | None = None


@router.put("/competency-models/{position_id}")
def upsert_competency_model(position_id: int, body: CompetencyUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    c = db.query(PositionCompetencyModel).filter(PositionCompetencyModel.position_id == position_id).first()
    if not c:
        c = PositionCompetencyModel(position_id=position_id, created_by=user.id)
        db.add(c)
        db.flush()
    old = model_to_dict(c)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    track_update(db, "competency_model", c.id, old, model_to_dict(c), user.id)
    db.commit()
    return _competency_dict(c)


# ══════════════════════════════════════════════════════════════════════════════
# 4.15 资源库定义中心
# ══════════════════════════════════════════════════════════════════════════════

def _lib_def_dict(d: ResourceLibraryDefinition) -> dict:
    return {
        "id": d.id, "library_code": d.library_code, "display_name": d.display_name,
        "owner_department_id": d.owner_department_id, "owner_position_id": d.owner_position_id,
        "required_fields": d.required_fields, "consumption_scenarios": d.consumption_scenarios,
        "read_write_policy": d.read_write_policy, "update_cycle_sla": d.update_cycle_sla,
        "quality_baseline": d.quality_baseline,
    }


@router.get("/resource-library-defs")
def list_resource_library_defs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    defs = db.query(ResourceLibraryDefinition).all()
    return [_lib_def_dict(d) for d in defs]


class LibDefCreate(BaseModel):
    library_code: str
    display_name: str
    owner_department_id: int | None = None
    owner_position_id: int | None = None
    required_fields: list = Field(default_factory=list)
    consumption_scenarios: list = Field(default_factory=list)
    read_write_policy: dict = Field(default_factory=dict)
    update_cycle_sla: str | None = None
    quality_baseline: dict = Field(default_factory=dict)


@router.post("/resource-library-defs")
def create_resource_library_def(body: LibDefCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    d = ResourceLibraryDefinition(**body.model_dump(), created_by=user.id)
    db.add(d)
    db.flush()
    track_create(db, "resource_library_def", d, user.id)
    db.commit()
    return _lib_def_dict(d)


class LibDefUpdate(BaseModel):
    display_name: str | None = None
    owner_department_id: int | None = None
    owner_position_id: int | None = None
    required_fields: list | None = None
    consumption_scenarios: list | None = None
    read_write_policy: dict | None = None
    update_cycle_sla: str | None = None
    quality_baseline: dict | None = None


@router.put("/resource-library-defs/{def_id}")
def update_resource_library_def(def_id: int, body: LibDefUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    d = db.query(ResourceLibraryDefinition).get(def_id)
    if not d:
        raise HTTPException(404, "资源库定义不存在")
    old = model_to_dict(d)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(d, k, v)
    track_update(db, "resource_library_def", def_id, old, model_to_dict(d), user.id)
    db.commit()
    return _lib_def_dict(d)


# ══════════════════════════════════════════════════════════════════════════════
# 4.16 KR → 资源库映射
# ══════════════════════════════════════════════════════════════════════════════

def _kr_mapping_dict(m: KrResourceMapping) -> dict:
    return {
        "id": m.id, "kr_id": m.kr_id, "target_type": m.target_type,
        "target_code": m.target_code, "target_id": m.target_id,
        "relevance": m.relevance, "description": m.description,
    }


@router.get("/kr-mappings")
def list_kr_mappings(kr_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(KrResourceMapping)
    if kr_id:
        q = q.filter(KrResourceMapping.kr_id == kr_id)
    return [_kr_mapping_dict(m) for m in q.all()]


class KrMappingCreate(BaseModel):
    kr_id: int
    target_type: str
    target_code: str
    target_id: int | None = None
    relevance: str = "direct"
    description: str | None = None


@router.post("/kr-mappings")
def create_kr_mapping(body: KrMappingCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    m = KrResourceMapping(**body.model_dump(), created_by=user.id)
    db.add(m)
    db.flush()
    track_create(db, "kr_mapping", m, user.id)
    db.commit()
    return _kr_mapping_dict(m)


@router.delete("/kr-mappings/{mapping_id}")
def delete_kr_mapping(mapping_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    m = db.query(KrResourceMapping).get(mapping_id)
    if not m:
        raise HTTPException(404, "映射不存在")
    track_delete(db, "kr_mapping", m, user.id)
    db.delete(m)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 4.17 协同协议
# ══════════════════════════════════════════════════════════════════════════════

def _protocol_dict(p: CollabProtocol) -> dict:
    return {
        "id": p.id, "provider_department_id": p.provider_department_id,
        "consumer_department_id": p.consumer_department_id,
        "data_object": p.data_object,
        "provider_position_id": p.provider_position_id,
        "consumer_position_id": p.consumer_position_id,
        "trigger_event": p.trigger_event,
        "sync_frequency": p.sync_frequency,
        "latency_tolerance": p.latency_tolerance,
        "sla_description": p.sla_description,
        "is_active": p.is_active,
    }


@router.get("/collab-protocols")
def list_collab_protocols(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    protocols = db.query(CollabProtocol).filter(CollabProtocol.is_active == True).all()  # noqa: E712
    return [_protocol_dict(p) for p in protocols]


class ProtocolCreate(BaseModel):
    provider_department_id: int
    consumer_department_id: int
    data_object: str
    provider_position_id: int | None = None
    consumer_position_id: int | None = None
    trigger_event: str | None = None
    sync_frequency: str = "manual"
    latency_tolerance: str | None = None
    sla_description: str | None = None


@router.post("/collab-protocols")
def create_collab_protocol(body: ProtocolCreate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = CollabProtocol(**body.model_dump(), created_by=user.id)
    db.add(p)
    db.flush()
    track_create(db, "collab_protocol", p, user.id)
    db.commit()
    return _protocol_dict(p)


class ProtocolUpdate(BaseModel):
    data_object: str | None = None
    trigger_event: str | None = None
    sync_frequency: str | None = None
    latency_tolerance: str | None = None
    sla_description: str | None = None
    is_active: bool | None = None


@router.put("/collab-protocols/{protocol_id}")
def update_collab_protocol(protocol_id: int, body: ProtocolUpdate, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = db.query(CollabProtocol).get(protocol_id)
    if not p:
        raise HTTPException(404, "协同协议不存在")
    old = model_to_dict(p)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    track_update(db, "collab_protocol", protocol_id, old, model_to_dict(p), user.id)
    db.commit()
    return _protocol_dict(p)


@router.delete("/collab-protocols/{protocol_id}")
def delete_collab_protocol(protocol_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    p = db.query(CollabProtocol).get(protocol_id)
    if not p:
        raise HTTPException(404, "协同协议不存在")
    track_delete(db, "collab_protocol", p, user.id)
    db.delete(p)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# 4.18 变更影响分析
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/change-impacts")
def list_change_impacts(
    baseline_id: int | None = None,
    resolved: bool | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(OrgChangeImpact)
    if baseline_id:
        q = q.filter(OrgChangeImpact.baseline_id == baseline_id)
    if resolved is not None:
        q = q.filter(OrgChangeImpact.resolved == resolved)
    impacts = q.order_by(OrgChangeImpact.created_at.desc()).limit(200).all()
    return [
        {"id": i.id, "baseline_id": i.baseline_id, "impact_type": i.impact_type,
         "target_type": i.impact_target_type, "target_id": i.impact_target_id,
         "target_name": i.impact_target_name, "severity": i.severity,
         "description": i.description, "resolved": i.resolved,
         "created_at": i.created_at.isoformat() if i.created_at else None}
        for i in impacts
    ]


@router.post("/change-impacts/{impact_id}/resolve")
def resolve_impact(impact_id: int, db: Session = Depends(get_db), user: User = Depends(require_role(Role.SUPER_ADMIN))):
    i = db.query(OrgChangeImpact).get(impact_id)
    if not i:
        raise HTTPException(404, "影响记录不存在")
    i.resolved = True
    i.resolved_at = datetime.datetime.utcnow()
    i.resolved_by = user.id
    db.commit()
    return {"ok": True}
