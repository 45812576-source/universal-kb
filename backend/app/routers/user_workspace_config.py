"""个人工作台配置 API — 管理用户的 Skill/Tool 挂载。"""
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillStatus
from app.models.tool import ToolRegistry
from app.models.user import Role, User
from app.models.workspace import (
    UserWorkspaceConfig,
    Workspace,
    WorkspaceSkill,
    WorkspaceStatus,
    WorkspaceTool,
)

router = APIRouter(prefix="/api/workspace-config", tags=["workspace-config"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class MountItem(BaseModel):
    id: int
    source: str = "own"   # own | dept | market
    mounted: bool = True


class SaveConfigRequest(BaseModel):
    mounted_skills: List[MountItem]
    mounted_tools: List[MountItem]


class PublishRequest(BaseModel):
    scope: str  # department | company
    name: Optional[str] = None
    description: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_config(db: Session, user: User) -> UserWorkspaceConfig:
    cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
    if cfg:
        return cfg

    # 首次创建：预挂载用户自己的 skill/tool + 部门发布的
    own_skills = (
        db.query(Skill)
        .filter(Skill.created_by == user.id, Skill.status != SkillStatus.ARCHIVED)
        .all()
    )
    dept_skills = (
        db.query(Skill)
        .filter(
            Skill.scope == "department",
            Skill.status == SkillStatus.PUBLISHED,
            Skill.department_id == user.department_id,
        )
        .all()
    ) if user.department_id else []

    own_tools = (
        db.query(ToolRegistry)
        .filter(ToolRegistry.created_by == user.id, ToolRegistry.status != "archived")
        .all()
    )
    dept_tools = (
        db.query(ToolRegistry)
        .filter(
            ToolRegistry.scope == "department",
            ToolRegistry.status == "published",
            ToolRegistry.department_id == user.department_id,
        )
        .all()
    ) if user.department_id else []

    mounted_skills = (
        [{"skill_id": s.id, "source": "own", "mounted": True} for s in own_skills]
        + [{"skill_id": s.id, "source": "dept", "mounted": True} for s in dept_skills]
    )
    mounted_tools = (
        [{"tool_id": t.id, "source": "own", "mounted": True} for t in own_tools]
        + [{"tool_id": t.id, "source": "dept", "mounted": True} for t in dept_tools]
    )

    cfg = UserWorkspaceConfig(
        user_id=user.id,
        mounted_skills=mounted_skills,
        mounted_tools=mounted_tools,
        needs_prompt_refresh=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _enrich_skill(s: Skill) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "status": s.status.value if s.status else "draft",
        "scope": s.scope,
    }


def _enrich_tool(t: ToolRegistry) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "display_name": t.display_name,
        "description": t.description,
        "tool_type": t.tool_type,
        "status": t.status,
    }


def _config_response(cfg: UserWorkspaceConfig, db: Session) -> dict:
    """返回带详情的配置。"""
    skill_ids = [item["skill_id"] for item in (cfg.mounted_skills or [])]
    tool_ids = [item["tool_id"] for item in (cfg.mounted_tools or [])]

    skills_map = {}
    if skill_ids:
        for s in db.query(Skill).filter(Skill.id.in_(skill_ids)).all():
            skills_map[s.id] = _enrich_skill(s)

    tools_map = {}
    if tool_ids:
        for t in db.query(ToolRegistry).filter(ToolRegistry.id.in_(tool_ids)).all():
            tools_map[t.id] = _enrich_tool(t)

    enriched_skills = []
    for item in (cfg.mounted_skills or []):
        detail = skills_map.get(item["skill_id"])
        if detail:
            enriched_skills.append({**detail, "source": item["source"], "mounted": item["mounted"]})

    enriched_tools = []
    for item in (cfg.mounted_tools or []):
        detail = tools_map.get(item["tool_id"])
        if detail:
            enriched_tools.append({**detail, "source": item["source"], "mounted": item["mounted"]})

    return {
        "id": cfg.id,
        "user_id": cfg.user_id,
        "mounted_skills": enriched_skills,
        "mounted_tools": enriched_tools,
        "needs_prompt_refresh": cfg.needs_prompt_refresh,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def get_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取当前用户的工作台配置，不存在则自动创建。"""
    cfg = _get_or_create_config(db, user)
    return _config_response(cfg, db)


@router.put("")
def save_config(
    req: SaveConfigRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存挂载配置。"""
    cfg = _get_or_create_config(db, user)

    new_skills = [{"skill_id": m.id, "source": m.source, "mounted": m.mounted} for m in req.mounted_skills]
    new_tools = [{"tool_id": m.id, "source": m.source, "mounted": m.mounted} for m in req.mounted_tools]

    # 检测是否有实质变更
    old_mounted_set = {
        item["skill_id"] for item in (cfg.mounted_skills or []) if item.get("mounted")
    }
    new_mounted_set = {m.id for m in req.mounted_skills if m.mounted}
    old_tool_set = {
        item["tool_id"] for item in (cfg.mounted_tools or []) if item.get("mounted")
    }
    new_tool_set = {m.id for m in req.mounted_tools if m.mounted}

    cfg.mounted_skills = new_skills
    cfg.mounted_tools = new_tools

    if old_mounted_set != new_mounted_set or old_tool_set != new_tool_set:
        cfg.needs_prompt_refresh = True

    db.commit()
    db.refresh(cfg)
    return _config_response(cfg, db)


@router.post("/publish")
def publish_as_workspace(
    req: PublishRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """管理员将当前配置发布为部门/公司标准工作台。"""
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "仅管理员可发布标准工作台")

    cfg = _get_or_create_config(db, user)
    mounted_skill_ids = [
        item["skill_id"] for item in (cfg.mounted_skills or []) if item.get("mounted")
    ]
    mounted_tool_ids = [
        item["tool_id"] for item in (cfg.mounted_tools or []) if item.get("mounted")
    ]

    if not mounted_skill_ids and not mounted_tool_ids:
        raise HTTPException(400, "没有挂载任何 Skill 或 Tool，无法发布")

    # 确定可见范围
    dept_id = user.department_id if req.scope == "department" else None
    visibility = "department" if req.scope == "department" else "all"

    ws_name = req.name or f"{user.display_name}的标准工作台"
    ws_desc = req.description or "管理员推荐的标准工作台配置"

    # 查找该管理员是否已有推荐工作台
    existing = (
        db.query(Workspace)
        .filter(
            Workspace.recommended_by == user.id,
            Workspace.for_department_id == dept_id if dept_id else Workspace.for_department_id.is_(None),
            Workspace.is_active == True,
        )
        .first()
    )

    if existing:
        ws = existing
        ws.name = ws_name
        ws.description = ws_desc
        ws.visibility = visibility
        # 清除旧绑定
        db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == ws.id).delete()
        db.query(WorkspaceTool).filter(WorkspaceTool.workspace_id == ws.id).delete()
    else:
        ws = Workspace(
            name=ws_name,
            description=ws_desc,
            icon="briefcase",
            color="#00A3C4",
            status=WorkspaceStatus.PUBLISHED,
            created_by=user.id,
            department_id=dept_id,
            visibility=visibility,
            recommended_by=user.id,
            for_department_id=dept_id,
        )
        db.add(ws)
        db.flush()

    # 绑定 skill/tool
    for sid in mounted_skill_ids:
        db.add(WorkspaceSkill(workspace_id=ws.id, skill_id=sid))
    for tid in mounted_tool_ids:
        db.add(WorkspaceTool(workspace_id=ws.id, tool_id=tid))

    db.commit()
    db.refresh(ws)
    return {
        "ok": True,
        "workspace_id": ws.id,
        "name": ws.name,
        "scope": req.scope,
        "skill_count": len(mounted_skill_ids),
        "tool_count": len(mounted_tool_ids),
    }
