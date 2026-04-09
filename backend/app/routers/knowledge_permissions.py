"""知识资产细粒度权限 CRUD

前端调用:
  GET    /api/admin/users/{uid}/knowledge-permissions
  POST   /api/admin/users/{uid}/knowledge-permissions  (batch grant)
  DELETE /api/admin/users/{uid}/knowledge-permissions/{grant_id}
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge import KnowledgeFolder
from app.models.knowledge_permission import (
    KnowledgePermissionGrant,
    PermissionResourceType,
    PermissionScope,
    PermissionSource,
)
from app.models.user import Role, User

router = APIRouter(prefix="/api/admin", tags=["knowledge-permissions"])


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class GrantItem(BaseModel):
    resource_type: str = "folder"       # "folder" | "approval_capability"
    resource_id: Optional[int] = None
    action: str                         # e.g. "knowledge.folder.view"
    scope: str = "exact"                # "exact" | "subtree"
    source: str = "direct"              # "direct" | "approval" | "role_default"


class BatchGrantRequest(BaseModel):
    grants: list[GrantItem]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_grant(g: KnowledgePermissionGrant, db: Session) -> dict:
    """序列化单条授权记录，附带 resource_name。"""
    resource_name = None
    if g.resource_type == PermissionResourceType.FOLDER and g.resource_id:
        folder = db.get(KnowledgeFolder, g.resource_id)
        if folder:
            resource_name = folder.name

    # action_category 推断
    action = g.action or ""
    if action.startswith("knowledge.folder."):
        action_category = "folder_mgmt"
    elif action.startswith("knowledge.review.") or action.startswith("knowledge.edit."):
        action_category = "content_review"
    elif ".publish." in action:
        action_category = "publish_approval"
    else:
        action_category = "data_security"

    return {
        "id": g.id,
        "grantee_user_id": g.grantee_user_id,
        "resource_type": g.resource_type.value if g.resource_type else g.resource_type,
        "resource_id": g.resource_id,
        "action": g.action,
        "scope": g.scope.value if g.scope else g.scope,
        "granted_by": g.granted_by,
        "granted_at": g.granted_at.isoformat() if g.granted_at else None,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "source": g.source.value if g.source else g.source,
        "resource_name": resource_name,
        "action_category": action_category,
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/users/{uid}/knowledge-permissions")
def list_knowledge_permissions(
    uid: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """获取用户的所有知识资产权限。"""
    grants = (
        db.query(KnowledgePermissionGrant)
        .filter(KnowledgePermissionGrant.grantee_user_id == uid)
        .order_by(KnowledgePermissionGrant.id)
        .all()
    )
    return [_serialize_grant(g, db) for g in grants]


@router.post("/users/{uid}/knowledge-permissions")
def batch_grant_permissions(
    uid: int,
    body: BatchGrantRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """批量授予知识资产权限。跳过已存在的相同 (resource_type, resource_id, action) 组合。"""
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404, "用户不存在")

    created = []
    for item in body.grants:
        # 检查是否已存在
        existing = (
            db.query(KnowledgePermissionGrant)
            .filter(
                KnowledgePermissionGrant.grantee_user_id == uid,
                KnowledgePermissionGrant.resource_type == item.resource_type,
                KnowledgePermissionGrant.resource_id == item.resource_id,
                KnowledgePermissionGrant.action == item.action,
            )
            .first()
        )
        if existing:
            continue

        grant = KnowledgePermissionGrant(
            grantee_user_id=uid,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            action=item.action,
            scope=item.scope,
            source=item.source,
            granted_by=current.id,
        )
        db.add(grant)
        created.append(grant)

    db.commit()
    for g in created:
        db.refresh(g)

    return {"ok": True, "created_count": len(created)}


@router.delete("/users/{uid}/knowledge-permissions/{grant_id}")
def revoke_permission(
    uid: int,
    grant_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """回收单条知识资产权限。"""
    grant = (
        db.query(KnowledgePermissionGrant)
        .filter(
            KnowledgePermissionGrant.id == grant_id,
            KnowledgePermissionGrant.grantee_user_id == uid,
        )
        .first()
    )
    if not grant:
        raise HTTPException(404, "授权记录不存在")
    db.delete(grant)
    db.commit()
    return {"ok": True}
