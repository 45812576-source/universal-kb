"""知识库管理后台 API V1.5

目录树管理、子树委派、Rerun 作业。
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_admin import (
    FolderAuditAction,
    KnowledgeFolderAuditLog,
    KnowledgeFolderGrant,
    KnowledgeRerunJob,
    RerunStatus,
    RerunTargetScope,
    RerunTriggerType,
)
from app.models.user import Role, User
from app.services.knowledge_rerun_service import _get_subtree_ids, execute_rerun

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge/admin", tags=["knowledge-admin"])


# ── 权限检查 ─────────────────────────────────────────────────────────────────

def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="需要超管权限")


def _require_folder_grant(db: Session, user: User, folder_id: int) -> None:
    """检查用户是否有子树委派权限（超管直接通过）。"""
    if user.role == Role.SUPER_ADMIN:
        return
    # 检查 folder_id 本身及其所有祖先是否有 grant
    current_id: Optional[int] = folder_id
    visited = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        grant = db.query(KnowledgeFolderGrant).filter(
            KnowledgeFolderGrant.folder_id == current_id,
            KnowledgeFolderGrant.grantee_user_id == user.id,
        ).first()
        if grant:
            return
        folder = db.get(KnowledgeFolder, current_id)
        if not folder:
            break
        current_id = folder.parent_id
    raise HTTPException(status_code=403, detail="无此目录的管理权限")


def _audit_log(
    db: Session,
    folder_id: int,
    action: FolderAuditAction,
    user_id: int,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    log = KnowledgeFolderAuditLog(
        folder_id=folder_id,
        action=action,
        old_value=old_value,
        new_value=new_value,
        performed_by=user_id,
    )
    db.add(log)


# ── Pydantic Schemas ─────────────────────────────────────────────────────────

class FolderCreate(BaseModel):
    name: str = Field(..., max_length=100)
    parent_id: Optional[int] = None
    taxonomy_board: Optional[str] = None
    taxonomy_code: Optional[str] = None
    business_unit: Optional[str] = None  # 显式指定或从父节点继承
    sort_order: int = 0


class FolderUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    sort_order: Optional[int] = None


class FolderMove(BaseModel):
    new_parent_id: Optional[int] = None


class GrantCreate(BaseModel):
    folder_id: int
    grantee_user_id: int
    can_manage_children: bool = True
    can_delete_descendants: bool = True


# ── 目录树管理 ───────────────────────────────────────────────────────────────

def _folder_to_dict(folder: KnowledgeFolder, entry_counts: dict[int, int], grant_counts: dict[int, int]) -> dict:
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "sort_order": folder.sort_order,
        "is_system": folder.is_system,
        "taxonomy_board": folder.taxonomy_board,
        "taxonomy_code": folder.taxonomy_code,
        "entry_count": entry_counts.get(folder.id, 0),
        "manager_count": grant_counts.get(folder.id, 0),
        "created_at": folder.created_at.isoformat() if folder.created_at else None,
        "children": [],
    }


def _build_tree(
    folders: list[KnowledgeFolder],
    entry_counts: dict[int, int],
    grant_counts: dict[int, int],
) -> list[dict]:
    """构建嵌套树结构。"""
    nodes = {f.id: _folder_to_dict(f, entry_counts, grant_counts) for f in folders}
    roots = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id and f.parent_id in nodes:
            nodes[f.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/tree")
def get_system_folder_tree(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取完整系统目录树（带节点元信息）。"""
    _require_folder_grant(db, user, 0)  # 基础权限校验 — 超管直接通过

    folders = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.is_system == 1
    ).order_by(KnowledgeFolder.sort_order, KnowledgeFolder.id).all()

    # 统计每个 folder 下的文档数
    from sqlalchemy import func
    entry_stats = db.query(
        KnowledgeEntry.folder_id, func.count(KnowledgeEntry.id)
    ).filter(
        KnowledgeEntry.folder_id.isnot(None)
    ).group_by(KnowledgeEntry.folder_id).all()
    entry_counts = dict(entry_stats)

    # 统计每个 folder 的委派管理员数
    grant_stats = db.query(
        KnowledgeFolderGrant.folder_id, func.count(KnowledgeFolderGrant.id)
    ).group_by(KnowledgeFolderGrant.folder_id).all()
    grant_counts = dict(grant_stats)

    tree = _build_tree(folders, entry_counts, grant_counts)
    return {"tree": tree, "total_folders": len(folders)}


@router.post("/folders")
def create_folder(
    body: FolderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新增系统目录节点。business_unit 未指定时从父节点继承。"""
    parent = None
    if body.parent_id:
        _require_folder_grant(db, user, body.parent_id)
        parent = db.get(KnowledgeFolder, body.parent_id)
        if not parent or parent.is_system != 1:
            raise HTTPException(status_code=404, detail="父目录不存在或非系统目录")
    else:
        _require_super_admin(user)

    # business_unit: 显式指定 > 从父节点继承
    business_unit = body.business_unit
    if not business_unit and parent:
        business_unit = parent.business_unit

    folder = KnowledgeFolder(
        name=body.name,
        parent_id=body.parent_id,
        created_by=user.id,
        is_system=1,
        taxonomy_board=body.taxonomy_board or (parent.taxonomy_board if parent else None),
        taxonomy_code=body.taxonomy_code,
        business_unit=business_unit,
        sort_order=body.sort_order,
    )
    db.add(folder)
    db.flush()

    _audit_log(db, folder.id, FolderAuditAction.CREATE, user.id,
               new_value={"name": body.name, "parent_id": body.parent_id})
    db.commit()
    db.refresh(folder)
    return {"id": folder.id, "name": folder.name}


@router.patch("/folders/{folder_id}")
def update_folder(
    folder_id: int,
    body: FolderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """修改节点（rename/sort）。"""
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")
    _require_folder_grant(db, user, folder_id)

    old_values = {}
    new_values = {}
    trigger_rerun = False

    if body.name is not None and body.name != folder.name:
        old_values["name"] = folder.name
        new_values["name"] = body.name
        folder.name = body.name
        trigger_rerun = True

    if body.sort_order is not None and body.sort_order != folder.sort_order:
        old_values["sort_order"] = folder.sort_order
        new_values["sort_order"] = body.sort_order
        folder.sort_order = body.sort_order

    if old_values:
        action = FolderAuditAction.RENAME if "name" in old_values else FolderAuditAction.SORT
        _audit_log(db, folder_id, action, user.id, old_value=old_values, new_value=new_values)

    db.commit()

    # 重命名触发 rerun
    if trigger_rerun:
        _trigger_rerun(db, folder_id, RerunTriggerType.FOLDER_RENAME, user.id)

    return {"ok": True}


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除节点（含影响校验 + 先重绑后删除）。

    流程：
    1. 权限校验（含委派边界保护）
    2. 收集受影响文档的 taxonomy_code，用于重绑
    3. 先把文档的 folder_id 清空，但保留 taxonomy_code
    4. 删除目录树 + grants
    5. 触发 rerun（类型 folder_delete），rerun 会按 taxonomy_code 全局重绑
    6. rerun 结束后仍无法绑定的才标 needs_review
    """
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")

    # 检查是否是被委派的根节点 — 子树管理员不能删自己的授权根
    grant = db.query(KnowledgeFolderGrant).filter(
        KnowledgeFolderGrant.folder_id == folder_id,
        KnowledgeFolderGrant.grantee_user_id == user.id,
    ).first()
    if grant and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="不能删除自己的授权根目录")

    _require_folder_grant(db, user, folder_id)

    # P1-2: 检查子树内是否有授予给其他用户的 grant root
    subtree_ids = _get_subtree_ids(db, folder_id)
    if user.role != Role.SUPER_ADMIN:
        other_grants = db.query(KnowledgeFolderGrant).filter(
            KnowledgeFolderGrant.folder_id.in_(subtree_ids),
            KnowledgeFolderGrant.grantee_user_id != user.id,
        ).count()
        if other_grants > 0:
            raise HTTPException(
                status_code=403,
                detail=f"子树内有 {other_grants} 个其他用户的授权根，不能越级删除",
            )

    affected_entries = db.query(KnowledgeEntry).filter(
        KnowledgeEntry.folder_id.in_(subtree_ids)
    ).count()

    # 收集受影响文档 ID（用于后续 rerun）
    orphan_entry_ids = [
        eid for (eid,) in db.query(KnowledgeEntry.id).filter(
            KnowledgeEntry.folder_id.in_(subtree_ids)
        ).all()
    ]

    # 清空 folder_id 但保留 taxonomy_code（rerun 靠 taxonomy_code 重绑）
    db.query(KnowledgeEntry).filter(
        KnowledgeEntry.folder_id.in_(subtree_ids)
    ).update({"folder_id": None}, synchronize_session="fetch")

    # 先清理 FK 依赖，再删除 folder
    db.query(KnowledgeFolderGrant).filter(
        KnowledgeFolderGrant.folder_id.in_(subtree_ids)
    ).delete(synchronize_session="fetch")

    db.query(KnowledgeFolderAuditLog).filter(
        KnowledgeFolderAuditLog.folder_id.in_(subtree_ids)
    ).delete(synchronize_session="fetch")

    db.query(KnowledgeRerunJob).filter(
        KnowledgeRerunJob.target_folder_id.in_(subtree_ids)
    ).delete(synchronize_session="fetch")

    # 删除 folder（cascade 会删除子节点）
    folder_name = folder.name
    db.delete(folder)
    db.flush()

    # 审计日志写到父目录（folder 已删，不能写自身）
    if folder.parent_id:
        _audit_log(db, folder.parent_id, FolderAuditAction.DELETE, user.id,
                   old_value={"deleted_folder_id": folder_id, "name": folder_name, "affected_entries": affected_entries})

    db.commit()

    # 触发 orphan rerun（按 taxonomy_code 全局重绑）
    rerun_result = None
    if orphan_entry_ids:
        from app.services.knowledge_rerun_service import execute_orphan_rerun
        job = KnowledgeRerunJob(
            trigger_type=RerunTriggerType.FOLDER_DELETE,
            target_folder_id=folder_id,  # 记录被删的 folder（此时已不存在）
            target_scope=RerunTargetScope.SUBTREE,
            status=RerunStatus.PENDING,
            created_by=user.id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        try:
            execute_orphan_rerun(db, job, orphan_entry_ids)
            db.refresh(job)
            rerun_result = {
                "job_id": job.id,
                "rebound": job.reclassified_count,
                "needs_review": job.skipped_count,
            }
        except Exception as e:
            logger.error(f"Orphan rerun failed: {e}")

    return {
        "ok": True,
        "affected_entries": affected_entries,
        "rerun": rerun_result,
    }


@router.post("/folders/{folder_id}/move")
def move_folder(
    folder_id: int,
    body: FolderMove,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移动节点。"""
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")
    _require_folder_grant(db, user, folder_id)

    if body.new_parent_id:
        # 不能移到自己的子树下
        subtree = _get_subtree_ids(db, folder_id)
        if body.new_parent_id in subtree:
            raise HTTPException(status_code=400, detail="不能移动到自身子树下")
        new_parent = db.get(KnowledgeFolder, body.new_parent_id)
        if not new_parent or new_parent.is_system != 1:
            raise HTTPException(status_code=404, detail="目标父目录不存在或非系统目录")
        _require_folder_grant(db, user, body.new_parent_id)

    old_parent_id = folder.parent_id
    folder.parent_id = body.new_parent_id

    _audit_log(db, folder_id, FolderAuditAction.MOVE, user.id,
               old_value={"parent_id": old_parent_id},
               new_value={"parent_id": body.new_parent_id})
    db.commit()

    # 移动触发 rerun
    _trigger_rerun(db, folder_id, RerunTriggerType.FOLDER_MOVE, user.id)

    return {"ok": True}


@router.get("/folders/{folder_id}/impact")
def folder_impact_preview(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除/移动前的影响预览。"""
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")
    _require_folder_grant(db, user, folder_id)

    subtree_ids = _get_subtree_ids(db, folder_id)
    entry_count = db.query(KnowledgeEntry).filter(
        KnowledgeEntry.folder_id.in_(subtree_ids)
    ).count()
    child_folder_count = len(subtree_ids) - 1  # 不含自身
    grant_count = db.query(KnowledgeFolderGrant).filter(
        KnowledgeFolderGrant.folder_id.in_(subtree_ids)
    ).count()

    return {
        "folder_id": folder_id,
        "folder_name": folder.name,
        "child_folder_count": child_folder_count,
        "entry_count": entry_count,
        "grant_count": grant_count,
    }


# ── 子树委派 ─────────────────────────────────────────────────────────────────

@router.get("/folder-grants")
def list_grants(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """授权列表。"""
    _require_super_admin(user)
    grants = db.query(KnowledgeFolderGrant).all()
    result = []
    for g in grants:
        folder = db.get(KnowledgeFolder, g.folder_id)
        grantee = db.get(User, g.grantee_user_id)
        result.append({
            "id": g.id,
            "folder_id": g.folder_id,
            "folder_name": folder.name if folder else None,
            "grantee_user_id": g.grantee_user_id,
            "grantee_name": grantee.display_name if grantee else None,
            "scope": g.scope.value if g.scope else "subtree",
            "can_manage_children": g.can_manage_children,
            "can_delete_descendants": g.can_delete_descendants,
            "created_by": g.created_by,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        })
    return result


@router.post("/folder-grants")
def create_grant(
    body: GrantCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新增授权（仅超管）。"""
    _require_super_admin(user)

    folder = db.get(KnowledgeFolder, body.folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")
    grantee = db.get(User, body.grantee_user_id)
    if not grantee:
        raise HTTPException(status_code=404, detail="用户不存在")

    existing = db.query(KnowledgeFolderGrant).filter(
        KnowledgeFolderGrant.folder_id == body.folder_id,
        KnowledgeFolderGrant.grantee_user_id == body.grantee_user_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="该用户已有此目录的授权")

    grant = KnowledgeFolderGrant(
        folder_id=body.folder_id,
        grantee_user_id=body.grantee_user_id,
        can_manage_children=body.can_manage_children,
        can_delete_descendants=body.can_delete_descendants,
        created_by=user.id,
    )
    db.add(grant)

    _audit_log(db, body.folder_id, FolderAuditAction.GRANT, user.id,
               new_value={"grantee_user_id": body.grantee_user_id})
    db.commit()
    db.refresh(grant)
    return {"id": grant.id}


@router.delete("/folder-grants/{grant_id}")
def delete_grant(
    grant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销授权（仅超管）。"""
    _require_super_admin(user)

    grant = db.get(KnowledgeFolderGrant, grant_id)
    if not grant:
        raise HTTPException(status_code=404, detail="授权记录不存在")

    _audit_log(db, grant.folder_id, FolderAuditAction.REVOKE, user.id,
               old_value={"grantee_user_id": grant.grantee_user_id})

    db.delete(grant)
    db.commit()
    return {"ok": True}


@router.get("/folders/{folder_id}/managers")
def list_folder_managers(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """某节点的管理员列表。"""
    _require_folder_grant(db, user, folder_id)

    grants = db.query(KnowledgeFolderGrant).filter(
        KnowledgeFolderGrant.folder_id == folder_id
    ).all()
    result = []
    for g in grants:
        grantee = db.get(User, g.grantee_user_id)
        result.append({
            "id": g.id,
            "user_id": g.grantee_user_id,
            "display_name": grantee.display_name if grantee else None,
            "can_manage_children": g.can_manage_children,
            "can_delete_descendants": g.can_delete_descendants,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        })
    return result


# ── Rerun 作业 ───────────────────────────────────────────────────────────────

def _trigger_rerun(
    db: Session, folder_id: int, trigger_type: RerunTriggerType, user_id: int,
) -> KnowledgeRerunJob:
    """创建并异步执行 rerun 作业。"""
    job = KnowledgeRerunJob(
        trigger_type=trigger_type,
        target_folder_id=folder_id,
        target_scope=RerunTargetScope.SUBTREE,
        status=RerunStatus.PENDING,
        created_by=user_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # 同步执行（简单场景，未来可改为后台任务队列）
    try:
        execute_rerun(db, job)
    except Exception as e:
        logger.error(f"Rerun job {job.id} execution error: {e}")

    return job


@router.post("/folders/{folder_id}/rerun")
def manual_rerun(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发 rerun。"""
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder or folder.is_system != 1:
        raise HTTPException(status_code=404, detail="系统目录不存在")
    _require_folder_grant(db, user, folder_id)

    _audit_log(db, folder_id, FolderAuditAction.RERUN_TRIGGER, user.id)
    db.commit()

    job = _trigger_rerun(db, folder_id, RerunTriggerType.MANUAL, user.id)
    db.refresh(job)

    return {
        "job_id": job.id,
        "status": job.status.value if job.status else "pending",
        "affected_count": job.affected_count,
    }


@router.get("/rerun-jobs")
def list_rerun_jobs(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """作业列表。"""
    _require_folder_grant(db, user, 0)

    total = db.query(KnowledgeRerunJob).count()
    jobs = db.query(KnowledgeRerunJob).order_by(
        KnowledgeRerunJob.created_at.desc()
    ).offset(offset).limit(limit).all()

    result = []
    for j in jobs:
        folder = db.get(KnowledgeFolder, j.target_folder_id)
        result.append({
            "id": j.id,
            "trigger_type": j.trigger_type.value if j.trigger_type else None,
            "target_folder_id": j.target_folder_id,
            "target_folder_name": folder.name if folder else "(已删除)",
            "status": j.status.value if j.status else "pending",
            "affected_count": j.affected_count,
            "reclassified_count": j.reclassified_count,
            "renamed_count": j.renamed_count,
            "failed_count": j.failed_count,
            "skipped_count": j.skipped_count,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        })
    return {"total": total, "items": result}


@router.get("/rerun-jobs/{job_id}")
def get_rerun_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """作业详情。"""
    job = db.get(KnowledgeRerunJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="作业不存在")

    folder = db.get(KnowledgeFolder, job.target_folder_id)
    return {
        "id": job.id,
        "trigger_type": job.trigger_type.value if job.trigger_type else None,
        "target_folder_id": job.target_folder_id,
        "target_folder_name": folder.name if folder else "(已删除)",
        "target_scope": job.target_scope.value if job.target_scope else "subtree",
        "status": job.status.value if job.status else "pending",
        "affected_count": job.affected_count,
        "reclassified_count": job.reclassified_count,
        "renamed_count": job.renamed_count,
        "failed_count": job.failed_count,
        "skipped_count": job.skipped_count,
        "error_log": job.error_log,
        "created_by": job.created_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


# ── 审计日志查询 ─────────────────────────────────────────────────────────────

@router.get("/audit-logs")
def list_audit_logs(
    folder_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """目录变更审计日志。"""
    _require_folder_grant(db, user, 0)

    query = db.query(KnowledgeFolderAuditLog)
    if folder_id:
        query = query.filter(KnowledgeFolderAuditLog.folder_id == folder_id)
    total = query.count()
    logs = query.order_by(
        KnowledgeFolderAuditLog.created_at.desc()
    ).offset(offset).limit(limit).all()

    result = []
    for log in logs:
        performer = db.get(User, log.performed_by)
        folder = db.get(KnowledgeFolder, log.folder_id)
        result.append({
            "id": log.id,
            "folder_id": log.folder_id,
            "folder_name": folder.name if folder else "(已删除)",
            "action": log.action.value if log.action else None,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "performed_by": log.performed_by,
            "performer_name": performer.display_name if performer else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        })
    return {"total": total, "items": result}
