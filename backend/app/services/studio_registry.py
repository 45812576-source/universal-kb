"""统一实例注册表 — workspace 是持久身份对象，进程只是可回收壳。"""
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models.opencode import StudioRegistration, OpenCodeWorkspaceMapping
from app.models.conversation import Conversation
from app.models.workspace import Workspace
from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class StudioEntryResolution:
    registration_id: int
    conversation_id: int
    workspace_root: str
    project_dir: str
    runtime_status: str  # stopped|starting|running|unhealthy
    runtime_port: Optional[int]
    generation: int
    needs_recover: bool


def resolve_entry(
    db: Session,
    user: User,
    workspace_type: str,
) -> StudioEntryResolution:
    """查或创建注册表记录 + 确保 conversation 存在，返回稳定入口。

    此函数是前端入口页的唯一后端依赖，保证：
    - 同一 (user, workspace_type) 永远返回同一 registration
    - primary_conversation_id 始终有效
    - workspace_root/project_dir 始终存在于磁盘
    - skill_studio 与 opencode 共用同一用户工程文件区
    """
    from app.routers.dev_studio import (
        _workspace_root_for_user,
        _workspace_project_dir,
        ensure_workspace_layout,
    )

    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user.id,
            StudioRegistration.workspace_type == workspace_type,
        )
        .first()
    )

    # 首次：创建注册记录
    if reg is None:
        # skill_studio 与 opencode 共用同一 workspace_root/project_dir
        workspace_root = _workspace_root_for_user(user.id, user.display_name or "")
        project_dir = _workspace_project_dir(workspace_root)

        # skill_studio 没有独立 runtime，标记 n/a
        reg = StudioRegistration(
            user_id=user.id,
            workspace_type=workspace_type,
            workspace_root=workspace_root,
            project_dir=project_dir,
            runtime_status="stopped" if workspace_type == "opencode" else "n/a",
            generation=0,
        )
        db.add(reg)
        db.flush()

    # 确保磁盘目录存在
    if reg.workspace_root:
        ensure_workspace_layout(reg.workspace_root, display_name=user.display_name or "")

    # 确保 primary_conversation_id 有效
    conv_valid = False
    if reg.primary_conversation_id:
        conv = db.get(Conversation, reg.primary_conversation_id)
        if conv and conv.is_active and conv.user_id == user.id:
            conv_valid = True

    if not conv_valid:
        # 尝试找已有的同类型 conversation
        conv = _find_or_create_conversation(db, user, workspace_type)
        reg.primary_conversation_id = conv.id

    reg.last_active_at = datetime.datetime.utcnow()
    db.commit()

    needs_recover = reg.runtime_status in ("stopped", "unhealthy")

    return StudioEntryResolution(
        registration_id=reg.id,
        conversation_id=reg.primary_conversation_id,
        workspace_root=reg.workspace_root,
        project_dir=reg.project_dir,
        runtime_status=reg.runtime_status,
        runtime_port=reg.runtime_port,
        generation=reg.generation,
        needs_recover=needs_recover,
    )


def _find_or_create_conversation(
    db: Session, user: User, workspace_type: str
) -> Conversation:
    """查找或创建 workspace_type 对应的 conversation。"""
    # 找到对应 workspace
    ws = (
        db.query(Workspace)
        .filter(Workspace.workspace_type == workspace_type)
        .first()
    )

    if ws:
        # 找已有 conversation
        existing = (
            db.query(Conversation)
            .filter(
                Conversation.user_id == user.id,
                Conversation.workspace_id == ws.id,
                Conversation.is_active == True,
            )
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if existing:
            return existing

        # 创建新 conversation
        title_map = {
            "opencode": "OpenCode 开发",
            "skill_studio": "Skill Studio",
        }
        conv = Conversation(
            user_id=user.id,
            workspace_id=ws.id,
            title=title_map.get(workspace_type, ws.name),
        )
        db.add(conv)
        db.flush()
        return conv

    # 无 workspace 记录时创建独立 conversation
    conv = Conversation(
        user_id=user.id,
        title=f"{workspace_type} 会话",
    )
    db.add(conv)
    db.flush()
    return conv


def update_runtime_status(
    db: Session,
    user_id: int,
    workspace_type: str,
    status: str,
    port: Optional[int] = None,
    bump_generation: bool = False,
) -> Optional[StudioRegistration]:
    """更新运行时状态。"""
    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
        )
        .first()
    )
    if not reg:
        return None

    old_status = reg.runtime_status

    # 先判断恢复（用旧状态），再写入新状态
    if status == "running" and old_status in ("stopped", "unhealthy"):
        reg.last_recovered_at = datetime.datetime.utcnow()

    reg.runtime_status = status
    if port is not None:
        reg.runtime_port = port
    if bump_generation:
        reg.generation = (reg.generation or 0) + 1
    if status == "running":
        reg.last_active_at = datetime.datetime.utcnow()

    db.commit()
    return reg


def resolve_studio_project_dir(db: Session, user_id: int, workspace_type: str) -> Optional[str]:
    """返回该用户工作台的 project_dir。skill_studio 与 opencode 共用同一目录。"""
    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
        )
        .first()
    )
    if reg and reg.project_dir:
        return reg.project_dir
    # fallback: 如果 skill_studio 未注册但 opencode 已注册，共用
    if workspace_type == "skill_studio":
        oc_reg = (
            db.query(StudioRegistration)
            .filter(
                StudioRegistration.user_id == user_id,
                StudioRegistration.workspace_type == "opencode",
            )
            .first()
        )
        if oc_reg and oc_reg.project_dir:
            return oc_reg.project_dir
    return None


def get_registration(
    db: Session, user_id: int, workspace_type: str
) -> Optional[StudioRegistration]:
    """只读查询。"""
    return (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
        )
        .first()
    )


def migrate_existing_users(db: Session) -> dict:
    """迁移现有用户数据到注册表。返回 {migrated: int, errors: [...]}。"""
    from app.routers.dev_studio import _workspace_root_for_user, _workspace_project_dir

    migrated = 0
    errors = []

    # 1. 从 OpenCodeWorkspaceMapping 补建 opencode registration
    mappings = db.query(OpenCodeWorkspaceMapping).all()
    for m in mappings:
        try:
            existing = (
                db.query(StudioRegistration)
                .filter(
                    StudioRegistration.user_id == m.user_id,
                    StudioRegistration.workspace_type == "opencode",
                )
                .first()
            )
            if existing:
                continue

            workspace_root = m.directory or _workspace_root_for_user(m.user_id)
            project_dir = _workspace_project_dir(workspace_root)

            # 找最近活跃的 opencode conversation
            ws = db.query(Workspace).filter(Workspace.workspace_type == "opencode").first()
            conv_id = None
            if ws:
                conv = (
                    db.query(Conversation)
                    .filter(
                        Conversation.user_id == m.user_id,
                        Conversation.workspace_id == ws.id,
                        Conversation.is_active == True,
                    )
                    .order_by(Conversation.updated_at.desc())
                    .first()
                )
                if conv:
                    conv_id = conv.id

            reg = StudioRegistration(
                user_id=m.user_id,
                workspace_type="opencode",
                workspace_root=workspace_root,
                project_dir=project_dir,
                primary_conversation_id=conv_id,
                runtime_status="stopped",
                generation=0,
            )
            db.add(reg)
            migrated += 1
        except Exception as e:
            errors.append(f"opencode user={m.user_id}: {e}")

    # 2. 补建 skill_studio registration
    skill_ws = db.query(Workspace).filter(Workspace.workspace_type == "skill_studio").first()
    if skill_ws:
        convs = (
            db.query(Conversation)
            .filter(
                Conversation.workspace_id == skill_ws.id,
                Conversation.is_active == True,
            )
            .all()
        )
        seen_users = set()
        for c in convs:
            if c.user_id in seen_users:
                continue
            seen_users.add(c.user_id)
            try:
                existing = (
                    db.query(StudioRegistration)
                    .filter(
                        StudioRegistration.user_id == c.user_id,
                        StudioRegistration.workspace_type == "skill_studio",
                    )
                    .first()
                )
                if existing:
                    continue

                # skill_studio 与 opencode 共用同一工程文件区
                workspace_root = _workspace_root_for_user(c.user_id)
                project_dir = _workspace_project_dir(workspace_root)
                reg = StudioRegistration(
                    user_id=c.user_id,
                    workspace_type="skill_studio",
                    workspace_root=workspace_root,
                    project_dir=project_dir,
                    primary_conversation_id=c.id,
                    runtime_status="n/a",
                    generation=0,
                )
                db.add(reg)
                migrated += 1
            except Exception as e:
                errors.append(f"skill_studio user={c.user_id}: {e}")

    db.commit()
    logger.info(f"[StudioRegistry] 迁移完成: migrated={migrated}, errors={len(errors)}")
    return {"migrated": migrated, "errors": errors}
