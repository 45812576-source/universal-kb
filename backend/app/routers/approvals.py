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


# ─── Policy 权限分层工具 ──────────────────────────────────────────────────────

def _split_policy_by_dept(suggested_policy: dict, dept_admin: "User", db: Session) -> tuple[dict, dict]:
    """将 suggested_policy 按 dept_admin 权限边界分割为两部分：
    - dept_portion：dept_admin 有权确认的（自己部门的 role_overrides + 所有 mask_overrides + same_role 内的 scope）
    - super_portion：超出部分（跨部门 role_overrides + scope 升级超出 same_role）

    规则：
    - publish_scope: dept_admin 最多可确认 same_role；更宽留给 super_admin
    - role_overrides: dept_admin 只能确认本部门职位（position.department_id == dept_admin.department_id）
    - mask_overrides: 全部由 dept_admin 确认（mask 是收严方向，无越权风险）
    """
    from app.models.permission import Position

    # 获取本部门职位 id 集合
    dept_positions: set[int] = set()
    if dept_admin.department_id:
        for p in db.query(Position).filter(Position.department_id == dept_admin.department_id).all():
            dept_positions.add(p.id)

    scope_order = ["self_only", "same_role", "cross_role", "org_wide"]
    suggested_scope = suggested_policy.get("publish_scope", "same_role")
    dept_max_scope = "same_role"

    # dept 能确认的 publish_scope
    if scope_order.index(suggested_scope) <= scope_order.index(dept_max_scope):
        dept_scope = suggested_scope
        super_scope = None   # 不需要升级
    else:
        dept_scope = dept_max_scope
        super_scope = suggested_scope  # super_admin 负责升到目标 scope

    all_overrides = suggested_policy.get("role_overrides", [])
    dept_overrides = [o for o in all_overrides if o.get("position_id") in dept_positions]
    super_overrides = [o for o in all_overrides if o.get("position_id") not in dept_positions]

    all_masks = suggested_policy.get("mask_overrides", [])
    # mask 全部归 dept_admin
    dept_masks = all_masks
    super_masks: list = []

    dept_portion = {
        "publish_scope": dept_scope,
        "default_data_scope": suggested_policy.get("default_data_scope", {}),
        "role_overrides": dept_overrides,
        "mask_overrides": dept_masks,
    }
    super_portion = {
        "publish_scope": super_scope,          # None 表示不需要升级
        "role_overrides": super_overrides,
        "mask_overrides": super_masks,
    }
    return dept_portion, super_portion


def _merge_policy_portions(dept_portion: dict, super_portion: dict) -> dict:
    """合并 dept + super 两阶段确认结果为完整 Policy。"""
    # publish_scope 取两者中更宽的
    scope_order = ["self_only", "same_role", "cross_role", "org_wide"]
    dept_scope = dept_portion.get("publish_scope", "same_role")
    super_scope = super_portion.get("publish_scope") or dept_scope
    final_scope_idx = max(scope_order.index(dept_scope), scope_order.index(super_scope))
    final_scope = scope_order[final_scope_idx]

    return {
        "publish_scope": final_scope,
        "default_data_scope": dept_portion.get("default_data_scope", {}),
        "role_overrides": (dept_portion.get("role_overrides") or []) + (super_portion.get("role_overrides") or []),
        "mask_overrides": (dept_portion.get("mask_overrides") or []) + (super_portion.get("mask_overrides") or []),
    }


# ─── Policy 初稿应用 ───────────────────────────────────────────────────────────

def _write_policy_to_db(skill_id: int, final_policy: dict, user, db: Session) -> None:
    """将合并后的完整 Policy 写入数据库（SkillPolicy + RolePolicyOverride + SkillMaskOverride）。
    幂等：已存在则跳过。
    """
    from app.models.permission import (
        PublishScope, SkillPolicy, RolePolicyOverride, SkillMaskOverride, MaskAction,
    )
    from app.routers.skills import _ensure_skill_policy

    existing = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
    if existing:
        return

    if not final_policy:
        _ensure_skill_policy(skill_id, user, db)
        return

    scope_map = {
        "self_only": PublishScope.SELF_ONLY,
        "same_role": PublishScope.SAME_ROLE,
        "cross_role": PublishScope.CROSS_ROLE,
        "org_wide": PublishScope.ORG_WIDE,
    }
    view_scope_map = {
        PublishScope.SELF_ONLY: PublishScope.SAME_ROLE,
        PublishScope.SAME_ROLE: PublishScope.SAME_ROLE,
        PublishScope.CROSS_ROLE: PublishScope.ORG_WIDE,
        PublishScope.ORG_WIDE: PublishScope.ORG_WIDE,
    }
    publish_scope = scope_map.get(final_policy.get("publish_scope", "same_role"), PublishScope.SAME_ROLE)

    sp = SkillPolicy(
        skill_id=skill_id,
        publish_scope=publish_scope,
        view_scope=view_scope_map.get(publish_scope, PublishScope.ORG_WIDE),
        default_data_scope=final_policy.get("default_data_scope", {}),
    )
    db.add(sp)
    db.flush()

    for override in final_policy.get("role_overrides", []):
        pos_id = override.get("position_id")
        if pos_id is None:
            continue
        db.add(RolePolicyOverride(
            skill_policy_id=sp.id,
            position_id=pos_id,
            callable=override.get("callable", True),
            data_scope=override.get("data_scope", {}),
            output_mask=override.get("output_mask", []),
        ))

    mask_action_map = {a.value: a for a in MaskAction}
    for mask in final_policy.get("mask_overrides", []):
        field = mask.get("field")
        if not field:
            continue
        action = mask_action_map.get(mask.get("action", "keep"), MaskAction.KEEP)
        db.add(SkillMaskOverride(
            skill_id=skill_id,
            position_id=mask.get("position_id"),
            field_name=field,
            mask_action=action,
            mask_params=mask.get("params", {}),
        ))

    db.flush()


def _apply_scan_policy(skill_id: int, approval_request, user, db: Session) -> None:
    """审批通过时应用最终 Policy：合并 dept_approved_policy + super 确认部分。"""
    scan = getattr(approval_request, "security_scan_result", None) or {}
    suggested = scan.get("suggested_policy") if scan and not scan.get("fallback") else None
    dept_portion = getattr(approval_request, "dept_approved_policy", None) or {}

    if not suggested and not dept_portion:
        from app.routers.skills import _ensure_skill_policy
        _ensure_skill_policy(skill_id, user, db)
        return

    if dept_portion:
        # 有 dept 阶段确认结果 → super 阶段补充剩余部分
        # super_portion = suggested 中去掉 dept 已覆盖的部分
        dept_pos_ids = {o.get("position_id") for o in dept_portion.get("role_overrides", [])}
        all_overrides = suggested.get("role_overrides", []) if suggested else []
        super_overrides = [o for o in all_overrides if o.get("position_id") not in dept_pos_ids]

        scope_order = ["self_only", "same_role", "cross_role", "org_wide"]
        dept_scope = dept_portion.get("publish_scope", "same_role")
        suggested_scope = suggested.get("publish_scope", "same_role") if suggested else dept_scope
        super_scope = suggested_scope if scope_order.index(suggested_scope) > scope_order.index(dept_scope) else dept_scope

        super_portion = {
            "publish_scope": super_scope,
            "role_overrides": super_overrides,
            "mask_overrides": [],  # mask 已在 dept 阶段全量处理
        }
        final_policy = _merge_policy_portions(dept_portion, super_portion)
    else:
        final_policy = suggested

    _write_policy_to_db(skill_id, final_policy, user, db)


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
                "version": latest_ver.version if latest_ver else None,
                "system_prompt": latest_ver.system_prompt if latest_ver else "",
                "change_note": latest_ver.change_note if latest_ver else "",
                "source_files": skill.source_files or [],
                "knowledge_tags": skill.knowledge_tags or [],
                "data_queries": skill.data_queries or [],
                "bound_tools": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "display_name": t.display_name,
                        "tool_type": t.tool_type.value if t.tool_type else "",
                    }
                    for t in list(skill.bound_tools)
                ],
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
    elif r.target_type == "webapp" and r.target_id:
        from app.models.web_app import WebApp
        webapp = db.get(WebApp, r.target_id)
        if webapp:
            target_detail = {
                "name": webapp.name,
                "description": webapp.description or "",
                "is_public": webapp.is_public,
                "preview_url": f"/api/web-apps/{webapp.id}/preview",
            }
    elif r.target_type == "knowledge" and r.target_id:
        from app.models.knowledge import KnowledgeEntry
        entry = db.get(KnowledgeEntry, r.target_id)
        if entry:
            target_detail = {
                "name": entry.ai_title or entry.title,
                "title": entry.title,
                "content": (entry.content or "")[:500],
                "category": entry.category,
                "file_ext": entry.file_ext,
                "source_file": entry.source_file,
                "created_by": entry.created_by,
                "creator_name": entry.creator.display_name if entry.creator else None,
                "review_level": entry.review_level,
                "review_stage": entry.review_stage.value if entry.review_stage else None,
                "sensitivity_flags": entry.sensitivity_flags or [],
                "auto_review_note": entry.auto_review_note,
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
        "security_scan_result": getattr(r, "security_scan_result", None),
        "dept_approved_policy": getattr(r, "dept_approved_policy", None),
        "sandbox_report_id": getattr(r, "sandbox_report_id", None),
        "sandbox_report_hash": getattr(r, "sandbox_report_hash", None),
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
        if "," in request_type:
            types = [t.strip() for t in request_type.split(",")]
            q = q.filter(ApprovalRequest.request_type.in_(types))
        else:
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
    from sqlalchemy import or_
    from app.models.knowledge import KnowledgeEntry

    # 知识编辑权限：所有用户都可能收到（作为文档创建者）
    my_entry_ids = [
        e.id for e in db.query(KnowledgeEntry.id).filter(KnowledgeEntry.created_by == user.id).all()
    ]
    ke_count = 0
    if my_entry_ids:
        ke_count = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_EDIT,
                ApprovalRequest.target_id.in_(my_entry_ids),
            )
            .count()
        )

    # 其他审批类型的 count（原逻辑）
    other_count = 0
    if user.role == Role.SUPER_ADMIN:
        other_count = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.stage == "super_pending",
                ApprovalRequest.request_type != ApprovalRequestType.KNOWLEDGE_EDIT,
            )
            .count()
        )
    elif user.role == Role.DEPT_ADMIN:
        from app.models.user import User as UserModel
        dept_user_ids = [
            u.id for u in db.query(UserModel).filter(UserModel.department_id == user.department_id).all()
        ]
        other_count = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.stage == "dept_pending",
                ApprovalRequest.request_type != ApprovalRequestType.KNOWLEDGE_EDIT,
                ApprovalRequest.requester_id.in_(dept_user_ids),
            )
            .count()
        )

    return {"count": ke_count + other_count}


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


@router.get("/incoming")
def incoming_approvals(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """需要我审批的请求（包括知识编辑权限申请——我是文档创建者的那些）。"""
    from sqlalchemy import or_

    # 知识编辑权限：文档创建者审批
    from app.models.knowledge import KnowledgeEntry
    my_entry_ids = [
        e.id for e in db.query(KnowledgeEntry.id).filter(KnowledgeEntry.created_by == user.id).all()
    ]

    conditions = []
    # 知识编辑权限：我创建的文档的申请
    if my_entry_ids:
        conditions.append(
            (ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_EDIT)
            & (ApprovalRequest.target_id.in_(my_entry_ids))
        )
    # 管理员还能看其他类型的审批
    if user.role == Role.SUPER_ADMIN:
        conditions.append(
            (ApprovalRequest.request_type != ApprovalRequestType.KNOWLEDGE_EDIT)
        )
    elif user.role == Role.DEPT_ADMIN:
        from app.models.user import User as UserModel
        dept_user_ids = [
            u.id for u in db.query(UserModel).filter(UserModel.department_id == user.department_id).all()
        ]
        conditions.append(
            (ApprovalRequest.request_type != ApprovalRequestType.KNOWLEDGE_EDIT)
            & (ApprovalRequest.stage == "dept_pending")
            & (ApprovalRequest.requester_id.in_(dept_user_ids))
        )

    if not conditions:
        return []

    items = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.status == ApprovalStatus.PENDING)
        .filter(or_(*conditions))
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
    user: User = Depends(get_current_user),
):
    """审批动作：approve / reject / add_conditions"""
    r = db.get(ApprovalRequest, request_id)
    if not r:
        raise HTTPException(404, "审批申请不存在")
    if r.status != ApprovalStatus.PENDING:
        raise HTTPException(400, f"审批已完结（状态：{r.status}），不可重复操作")

    # 权限检查：knowledge_edit 由文档创建者或 super_admin 审批，其他类型由 admin 角色审批
    is_knowledge_edit = r.request_type == ApprovalRequestType.KNOWLEDGE_EDIT and r.target_type == "knowledge"
    if is_knowledge_edit:
        from app.models.knowledge import KnowledgeEntry
        entry = db.get(KnowledgeEntry, r.target_id) if r.target_id else None
        if not entry:
            raise HTTPException(404, "关联文档不存在")
        if entry.created_by != user.id and user.role != Role.SUPER_ADMIN:
            raise HTTPException(403, "只有文档创建者或超级管理员可以审批编辑权限")
    elif user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "仅管理员可执行审批")

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

    is_webapp_publish = (
        r.request_type == ApprovalRequestType.WEBAPP_PUBLISH
        and r.target_type == "webapp"
        and r.target_id
    )

    # ── 审批前校验：沙盒测试报告消费 ──
    # Skill/Tool 发布审批必须关联有效的沙盒测试报告
    if req.action == "approve" and r.request_type in (
        ApprovalRequestType.SKILL_PUBLISH,
    ):
        # 优先使用 FK 列，fallback 到 security_scan_result JSON
        report_id = getattr(r, "sandbox_report_id", None)
        report_hash = getattr(r, "sandbox_report_hash", None)
        if not report_id:
            scan = getattr(r, "security_scan_result", None) or {}
            report_id = scan.get("sandbox_test_report_id")
            report_hash = report_hash or scan.get("report_hash")
        if not report_id:
            raise HTTPException(
                400,
                "审批请求未关联沙盒测试报告，无法审批通过。请先完成交互式沙盒测试。"
            )
        from app.models.sandbox import SandboxTestReport
        report = db.get(SandboxTestReport, report_id)
        if not report:
            raise HTTPException(400, f"沙盒测试报告 #{report_id} 不存在")
        if report_hash and report.report_hash != report_hash:
            raise HTTPException(
                400,
                "沙盒测试报告哈希不匹配，报告可能已被篡改，请重新测试"
            )
        if not report.approval_eligible:
            raise HTTPException(
                400,
                "沙盒测试报告显示未通过全部三项评价，无法审批通过"
            )

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
            # 部门管理员：确认自己权限内的部分并流转到超管；超管可直接终审
            if user.role == Role.SUPER_ADMIN:
                # 超管直接终审通过（跳过 dept 阶段）
                r.status = ApprovalStatus.APPROVED
                from app.models.skill import Skill, SkillStatus
                skill = db.get(Skill, r.target_id)
                if skill:
                    skill.status = SkillStatus.PUBLISHED
                    skill.scope = "company"
                    _apply_scan_policy(r.target_id, r, user, db)
                    from app.routers.skills import _cascade_tool_status_on_publish
                    _cascade_tool_status_on_publish(r.target_id, db)
            elif user.role == Role.DEPT_ADMIN:
                # 分割 Policy：dept 只确认本部门权限内的部分
                scan = getattr(r, "security_scan_result", None) or {}
                suggested = scan.get("suggested_policy") if scan and not scan.get("fallback") else None
                if suggested:
                    dept_portion, _super_portion = _split_policy_by_dept(suggested, user, db)
                    r.dept_approved_policy = dept_portion
                # 推进到超管审批阶段（无论有无 super 部分，都需要超管最终发布）
                r.stage = "super_pending"
            else:
                raise HTTPException(403, "仅部门管理员或超级管理员可执行审批")
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
                _apply_scan_policy(r.target_id, r, user, db)
                from app.routers.skills import _cascade_tool_status_on_publish
                _cascade_tool_status_on_publish(r.target_id, db)
        elif is_tool_publish and stage == "dept_pending":
            # 超管可在第一阶段直接终审通过；部门管理员则推进到超管审批阶段
            if user.role == Role.SUPER_ADMIN:
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
            elif user.role == Role.DEPT_ADMIN:
                r.stage = "super_pending"
            else:
                raise HTTPException(403, "仅部门管理员或超级管理员可执行审批")
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
        elif is_webapp_publish and stage == "dept_pending":
            if user.role == Role.SUPER_ADMIN:
                r.status = ApprovalStatus.APPROVED
                from app.models.web_app import WebApp
                webapp = db.get(WebApp, r.target_id)
                if webapp:
                    webapp.status = "published"
            elif user.role == Role.DEPT_ADMIN:
                r.stage = "super_pending"
            else:
                raise HTTPException(403, "仅部门管理员或超级管理员可执行审批")
        elif is_webapp_publish and stage == "super_pending":
            if user.role != Role.SUPER_ADMIN:
                raise HTTPException(403, "仅超级管理员可执行最终审批")
            r.status = ApprovalStatus.APPROVED
            from app.models.web_app import WebApp
            webapp = db.get(WebApp, r.target_id)
            if webapp:
                webapp.status = "published"
        elif is_knowledge_edit:
            # 知识编辑权限审批：通过 → 写入 KnowledgeEditGrant
            r.status = ApprovalStatus.APPROVED
            from app.models.knowledge import KnowledgeEditGrant
            existing_grant = db.query(KnowledgeEditGrant).filter_by(
                entry_id=r.target_id, user_id=r.requester_id
            ).first()
            if not existing_grant:
                db.add(KnowledgeEditGrant(
                    entry_id=r.target_id,
                    user_id=r.requester_id,
                    granted_by=user.id,
                ))
        elif r.request_type == ApprovalRequestType.KNOWLEDGE_REVIEW:
            # 知识内容审核：通过审批流处理时，实际审核已在 knowledge.py 完成
            # 此处仅作为后补状态同步
            r.status = ApprovalStatus.APPROVED
            from app.models.knowledge import KnowledgeEntry
            from app.services.knowledge_service import approve_knowledge, super_approve_knowledge
            entry = db.get(KnowledgeEntry, r.target_id) if r.target_id else None
            if entry and entry.status.value == "pending":
                if stage == "super_pending" and user.role == Role.SUPER_ADMIN:
                    try:
                        super_approve_knowledge(db, r.target_id, user.id, req.comment or "")
                    except ValueError:
                        pass
                else:
                    approve_knowledge(db, r.target_id, user.id, req.comment or "")
                    # L3 → 推给超管
                    if entry.review_stage and entry.review_stage.value == "dept_approved_pending_super":
                        r.status = ApprovalStatus.PENDING
                        r.stage = "super_pending"
        elif r.request_type == ApprovalRequestType.SKILL_VERSION_CHANGE:
            # Skill 版本变更审批：通过 → 无需额外操作（版本已创建）
            r.status = ApprovalStatus.APPROVED
        elif r.request_type == ApprovalRequestType.SKILL_OWNERSHIP_TRANSFER:
            # Skill 所有权转让：通过 → 更新 created_by
            r.status = ApprovalStatus.APPROVED
            if r.target_id and r.conditions:
                from app.models.skill import Skill
                skill = db.get(Skill, r.target_id)
                new_owner_id = None
                for cond in (r.conditions or []):
                    if isinstance(cond, dict):
                        new_owner_id = cond.get("new_owner_id")
                if skill and new_owner_id:
                    skill.created_by = new_owner_id
        else:
            # 其他审批类型，保持原逻辑
            r.status = ApprovalStatus.APPROVED
            if is_skill_publish:
                from app.models.skill import Skill, SkillStatus
                skill = db.get(Skill, r.target_id)
                if skill:
                    skill.status = SkillStatus.PUBLISHED
                    skill.scope = "company"
                    _apply_scan_policy(r.target_id, r, user, db)
                    from app.routers.skills import _cascade_tool_status_on_publish
                    _cascade_tool_status_on_publish(r.target_id, db)
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
        # 驳回时把 skill/tool/webapp 状态回退到 draft
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
        elif r.target_type == "webapp" and r.target_id:
            from app.models.web_app import WebApp
            webapp = db.get(WebApp, r.target_id)
            if webapp and webapp.status == "reviewing":
                webapp.status = "draft"
        # knowledge_review 拒绝 → reject_knowledge
        if r.request_type == ApprovalRequestType.KNOWLEDGE_REVIEW and r.target_id:
            from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
            entry = db.get(KnowledgeEntry, r.target_id)
            if entry and entry.status == KnowledgeStatus.PENDING:
                from app.services.knowledge_service import reject_knowledge
                reject_knowledge(db, r.target_id, user.id, req.comment or "审批拒绝")
    elif req.action == "add_conditions":
        r.status = ApprovalStatus.CONDITIONS
        if req.conditions:
            r.conditions = req.conditions

    db.commit()
    db.refresh(r)

    # Gap 6: 发射审批事件
    try:
        from app.services import event_bus
        event_type = "approval_resolved" if r.status != ApprovalStatus.PENDING else "approval_requested"
        event_bus.emit(
            db, event_type=event_type, source_type="approval", source_id=r.id,
            payload={"request_type": str(r.request_type), "status": str(r.status), "action": req.action},
            user_id=user.id,
        )
    except Exception:
        pass

    return _req(r, db)
