"""审批流 API"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.permission import (
    ApprovalAction,
    ApprovalActionType,
    ApprovalRequest,
    ApprovalRequestType,
    ApprovalStatus,
)
from app.models.user import Role, User

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class ApprovalRequestCreate(BaseModel):
    request_type: str
    target_id: Optional[int] = None
    target_type: Optional[str] = None


class ApprovalActionCreate(BaseModel):
    action: str  # "approve" | "reject" | "add_conditions"
    comment: Optional[str] = None
    conditions: Optional[list] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _req(r: ApprovalRequest, db: Session) -> dict:
    # 关联查出目标详情
    target_detail: dict = {}
    if r.target_type == "skill" and r.target_id:
        from app.models.skill import Skill, SkillVersion
        skill = db.get(Skill, r.target_id)
        if skill:
            latest_ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            target_detail = {
                "name": skill.name,
                "description": skill.description or "",
                "scope": skill.scope,
                "mode": skill.mode,
                "system_prompt": latest_ver.system_prompt if latest_ver else "",
                "change_note": latest_ver.change_note if latest_ver else "",
            }
    elif r.target_type == "tool" and r.target_id:
        from app.models.tool import ToolRegistry
        tool = db.get(ToolRegistry, r.target_id)
        if tool:
            config = tool.config or {}
            manifest = config.get("manifest", {})
            deploy_info = config.get("deploy_info", {})
            target_detail = {
                "name": tool.display_name,
                "tool_name": tool.name,
                "description": tool.description or "",
                "tool_type": tool.tool_type.value if tool.tool_type else "",
                "scope": tool.scope or "personal",
                "input_schema": tool.input_schema or {},
                "invocation_mode": manifest.get("invocation_mode", ""),
                "data_sources": manifest.get("data_sources", []),
                "permissions": manifest.get("permissions", deploy_info.get("permissions", [])),
                "preconditions": manifest.get("preconditions", []),
                "deploy_info": deploy_info,
            }

    return {
        "id": r.id,
        "request_type": r.request_type,
        "target_id": r.target_id,
        "target_type": r.target_type,
        "target_detail": target_detail,
        "requester_id": r.requester_id,
        "requester_name": r.requester.display_name if r.requester else None,
        "status": r.status,
        "stage": getattr(r, "stage", None),
        "conditions": r.conditions or [],
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "actions": [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "actor_name": a.actor.display_name if a.actor else None,
                "action": a.action,
                "comment": a.comment,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in (r.actions or [])
        ],
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_approvals(
    status: Optional[str] = Query(None),
    request_type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待审批列表。管理员看全部，部门负责人可看本部门请求，员工看自己的。"""
    q = db.query(ApprovalRequest)

    # 权限过滤
    if user.role == Role.EMPLOYEE:
        q = q.filter(ApprovalRequest.requester_id == user.id)
    elif user.role == Role.DEPT_ADMIN:
        # 部门负责人看：自己发起的 + 本部门用户发起的
        from app.models.user import User as UserModel
        dept_user_ids = [
            u.id for u in db.query(UserModel).filter(UserModel.department_id == user.department_id).all()
        ]
        from sqlalchemy import or_
        q = q.filter(
            or_(
                ApprovalRequest.requester_id == user.id,
                ApprovalRequest.requester_id.in_(dept_user_ids),
            )
        )
    # super_admin 看全部，不加过滤

    if status:
        q = q.filter(ApprovalRequest.status == status)
    if request_type:
        q = q.filter(ApprovalRequest.request_type == request_type)

    total = q.count()
    items = (
        q.order_by(ApprovalRequest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_req(r, db) for r in items],
    }


@router.get("/pending-count")
def pending_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户需要处理的待审批数量（用于侧边栏红标）。"""
    if user.role == Role.SUPER_ADMIN:
        # 超管：stage=super_pending 的所有待审批
        count = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.status == ApprovalStatus.PENDING, ApprovalRequest.stage == "super_pending")
            .count()
        )
    elif user.role == Role.DEPT_ADMIN:
        # 部门管理员：stage=dept_pending 且 requester 在本部门的
        from app.models.user import User as UserModel
        from sqlalchemy import or_
        dept_user_ids = [
            u.id for u in db.query(UserModel).filter(UserModel.department_id == user.department_id).all()
        ]
        count = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.stage == "dept_pending",
                ApprovalRequest.requester_id.in_(dept_user_ids),
            )
            .count()
        )
    else:
        count = 0
    return {"count": count}


@router.get("/my")
def my_approvals(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """我发起的审批"""
    items = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.requester_id == user.id)
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    return [_req(r, db) for r in items]


@router.get("/{request_id}")
def get_approval(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    r = db.get(ApprovalRequest, request_id)
    if not r:
        raise HTTPException(404, "审批申请不存在")
    # 员工只能查自己的
    if user.role == Role.EMPLOYEE and r.requester_id != user.id:
        raise HTTPException(403, "无权查看")
    return _req(r, db)


@router.post("")
def create_approval(
    req: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发起审批申请"""
    r = ApprovalRequest(
        request_type=req.request_type,
        target_id=req.target_id,
        target_type=req.target_type,
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return _req(r, db)


@router.post("/{request_id}/actions")
async def act_on_approval(
    request_id: int,
    req: ApprovalActionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """审批动作：approve / reject / add_conditions"""
    r = db.get(ApprovalRequest, request_id)
    if not r:
        raise HTTPException(404, "审批申请不存在")
    if r.status != ApprovalStatus.PENDING:
        raise HTTPException(400, f"审批已完结（状态：{r.status}），不可重复操作")

    action_map = {
        "approve": ApprovalActionType.APPROVE,
        "reject": ApprovalActionType.REJECT,
        "add_conditions": ApprovalActionType.ADD_CONDITIONS,
    }
    action_enum = action_map.get(req.action)
    if not action_enum:
        raise HTTPException(400, f"未知操作：{req.action}")

    a = ApprovalAction(
        request_id=request_id,
        actor_id=user.id,
        action=action_enum,
        comment=req.comment,
    )
    db.add(a)

    # 更新申请状态
    if req.action == "approve":
        is_skill_publish = (
            r.request_type == ApprovalRequestType.SKILL_PUBLISH
            and r.target_type == "skill"
            and r.target_id
        )
        is_tool_publish = (
            r.request_type == ApprovalRequestType.TOOL_PUBLISH
            and r.target_type == "tool"
            and r.target_id
        )
        stage = getattr(r, "stage", "dept_pending") or "dept_pending"

        if is_skill_publish and stage == "dept_pending":
            # 部门管理员通过第一步 → 进入超管审批阶段，skill 仍不发布
            if user.role not in (Role.DEPT_ADMIN, Role.SUPER_ADMIN):
                raise HTTPException(403, "仅部门管理员可执行第一阶段审批")
            r.stage = "super_pending"
        elif is_skill_publish and stage == "super_pending":
            # 超管通过第二步 → 正式发布
            if user.role != Role.SUPER_ADMIN:
                raise HTTPException(403, "仅超级管理员可执行最终审批")
            r.status = ApprovalStatus.APPROVED
            from app.models.skill import Skill, SkillStatus
            skill = db.get(Skill, r.target_id)
            if skill:
                skill.status = SkillStatus.PUBLISHED
                skill.scope = "company"
                from app.routers.skills import _ensure_skill_policy
                _ensure_skill_policy(r.target_id, user, db)
        elif is_tool_publish and stage == "dept_pending":
            # 部门管理员通过第一步 → 进入超管审批，tool 仍不发布
            if user.role not in (Role.DEPT_ADMIN, Role.SUPER_ADMIN):
                raise HTTPException(403, "仅部门管理员可执行第一阶段审批")
            r.stage = "super_pending"
        elif is_tool_publish and stage == "super_pending":
            # 超管通过第二步 → 安装并启动 MCP 服务，正式发布
            if user.role != Role.SUPER_ADMIN:
                raise HTTPException(403, "仅超级管理员可执行最终审批")
            r.status = ApprovalStatus.APPROVED
            from app.models.tool import ToolRegistry, ToolType
            tool = db.get(ToolRegistry, r.target_id)
            if tool:
                tool.status = "published"
                import datetime as _dt
                tool.updated_at = _dt.datetime.utcnow()
                db.commit()
                # MCP 工具：自动安装依赖并启动服务
                if tool.tool_type == ToolType.MCP:
                    from app.services.mcp_installer import install_and_start
                    install_result = await install_and_start(db, tool)
                    if not install_result["ok"]:
                        a.comment = (a.comment or "") + f"\n[MCP 安装失败] {install_result['error']}"
                        tool.is_active = False
                else:
                    tool.is_active = True
        else:
            # 其他审批类型，保持原逻辑
            r.status = ApprovalStatus.APPROVED
            if is_skill_publish:
                from app.models.skill import Skill, SkillStatus
                skill = db.get(Skill, r.target_id)
                if skill:
                    skill.status = SkillStatus.PUBLISHED
                    skill.scope = "company"
                    from app.routers.skills import _ensure_skill_policy
                    _ensure_skill_policy(r.target_id, user, db)
            elif is_tool_publish:
                from app.models.tool import ToolRegistry, ToolType
                tool = db.get(ToolRegistry, r.target_id)
                if tool:
                    tool.status = "published"
                    import datetime as _dt
                    tool.updated_at = _dt.datetime.utcnow()
                    db.commit()
                    if tool.tool_type == ToolType.MCP:
                        from app.services.mcp_installer import install_and_start
                        install_result = await install_and_start(db, tool)
                        if not install_result["ok"]:
                            a.comment = (a.comment or "") + f"\n[MCP 安装失败] {install_result['error']}"
                            tool.is_active = False
                    else:
                        tool.is_active = True
    elif req.action == "reject":
        r.status = ApprovalStatus.REJECTED
        # 驳回时把 skill/tool 状态回退到 draft
        if r.target_type == "skill" and r.target_id:
            from app.models.skill import Skill, SkillStatus
            skill = db.get(Skill, r.target_id)
            if skill and skill.status.value == "reviewing":
                skill.status = SkillStatus.DRAFT
        elif r.target_type == "tool" and r.target_id:
            from app.models.tool import ToolRegistry
            tool = db.get(ToolRegistry, r.target_id)
            if tool and tool.status == "reviewing":
                tool.status = "draft"
                tool.is_active = False
                import datetime as _dt
                tool.updated_at = _dt.datetime.utcnow()
    elif req.action == "add_conditions":
        r.status = ApprovalStatus.CONDITIONS
        if req.conditions:
            r.conditions = req.conditions

    db.commit()
    db.refresh(r)
    return _req(r, db)
