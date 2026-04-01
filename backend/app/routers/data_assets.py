"""Data Assets API — enriched layer for data table management.

New endpoints under /api/data-assets that provide enriched views over
BusinessTable, DataFolder, TableField, TableSyncJob, SkillTableBinding.
Does NOT replace existing /api/business-tables or /api/data routes.
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.business import (
    BusinessTable, DataFolder, SkillDataQuery, SkillTableBinding,
    TableField, TableSyncJob, TableView,
    TableRoleGroup, TablePermissionPolicy, FieldValueDictionary, SkillDataGrant,
)
from app.models.permission import PermissionAuditLog
from app.models.skill import Skill
from app.models.user import Department, Role, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-assets", tags=["data-assets"])


def ensure_default_view(db: Session, table_id: int) -> TableView | None:
    """确保数据表存在默认系统视图。同步完成/表创建后调用。

    如果已存在 is_system=True & is_default=True 的视图则跳过。
    否则创建一个包含所有非隐藏字段的默认探索视图。
    """
    existing = (
        db.query(TableView)
        .filter(
            TableView.table_id == table_id,
            TableView.is_system == True,  # noqa: E712
            TableView.is_default == True,  # noqa: E712
        )
        .first()
    )
    if existing:
        # 同步后字段可能变化，更新 visible_field_ids
        all_fields = (
            db.query(TableField)
            .filter(TableField.table_id == table_id)
            .order_by(TableField.sort_order)
            .all()
        )
        visible_ids = [f.id for f in all_fields if not f.is_hidden_by_default]
        if set(existing.visible_field_ids or []) != set(visible_ids):
            existing.visible_field_ids = visible_ids
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(existing, "visible_field_ids")
        return existing

    all_fields = (
        db.query(TableField)
        .filter(TableField.table_id == table_id)
        .order_by(TableField.sort_order)
        .all()
    )
    if not all_fields:
        return None

    visible_ids = [f.id for f in all_fields if not f.is_hidden_by_default]
    if not visible_ids:
        visible_ids = [f.id for f in all_fields]

    view = TableView(
        table_id=table_id,
        name="默认视图",
        view_purpose="explore",
        view_kind="list",
        is_system=True,
        is_default=True,
        visible_field_ids=visible_ids,
        disclosure_ceiling=None,  # 继承表级策略
    )
    db.add(view)
    db.flush()
    return view


def _write_audit_log(db: Session, user: User, action: str, target_table: str, target_id: int | None, old_values=None, new_values=None):
    """写入权限审计日志。"""
    log = PermissionAuditLog(
        operator_id=user.id,
        action=action,
        target_table=target_table,
        target_id=target_id,
        old_values=old_values or {},
        new_values=new_values or {},
    )
    db.add(log)



# ─── Helpers ─────────────────────────────────────────────────────────────────


def _folder_tree(db: Session, user: "User | None" = None) -> list[dict]:
    """Build nested folder tree with visibility filtering."""
    q = db.query(DataFolder).filter(DataFolder.is_archived == False)  # noqa: E712
    folders = q.order_by(DataFolder.sort_order).all()

    # 可见性过滤: 非管理员只能看到 company 或匹配自身的 folder
    if user and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        folders = [
            f for f in folders
            if f.workspace_scope == "company"
            or (f.workspace_scope == "department" and user.department_id and f.department_id == user.department_id)
            or (f.workspace_scope == "personal" and f.owner_id == user.id)
        ]
    by_parent: dict[int | None, list] = {}
    for f in folders:
        by_parent.setdefault(f.parent_id, []).append(f)

    def _build(parent_id: int | None) -> list[dict]:
        items = []
        for f in by_parent.get(parent_id, []):
            items.append({
                "id": f.id,
                "name": f.name,
                "parent_id": f.parent_id,
                "workspace_scope": f.workspace_scope,
                "sort_order": f.sort_order,
                "is_archived": f.is_archived,
                "children": _build(f.id),
            })
        return items

    return _build(None)


def _table_risk_warnings(bt: BusinessTable, field_count: int, binding_count: int) -> list[dict]:
    """Generate risk warnings for a table."""
    warnings = []
    rules = bt.validation_rules or {}

    if bt.sync_status == "failed":
        warnings.append({"code": "SYNC_FAILED", "message": "同步失败，请检查错误信息并重试"})
    if bt.field_profile_status == "pending":
        warnings.append({"code": "PROFILE_PENDING", "message": "字段画像待分析，权限配置可能受限"})
    if bt.field_profile_status == "failed":
        warnings.append({"code": "PROFILE_FAILED", "message": "字段画像分析失败"})

    access_scope = rules.get("access_scope", "self")
    if access_scope == "self":
        warnings.append({"code": "NO_ACCESS_POLICY", "message": "当前仅自己可见，未配置共享权限"})

    if binding_count == 0 and bt.source_type != "blank":
        warnings.append({"code": "NO_SKILL_VIEW", "message": "暂无 Skill 绑定视图"})

    return warnings


def _serialize_datetime(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        return dt.isoformat()
    return str(dt)


# ─── Folder endpoints ────────────────────────────────────────────────────────


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: int | None = None
    workspace_scope: str = "company"


class PatchFolderRequest(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    sort_order: int | None = None
    is_archived: bool | None = None


@router.get("/folders")
def list_folders(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回完整目录树（非管理员会按可见性过滤）。"""
    return {"items": _folder_tree(db, user)}


@router.post("/folders")
def create_folder(
    req: CreateFolderRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    if not req.name.strip():
        raise HTTPException(400, "目录名称不能为空")

    if req.parent_id is not None:
        parent = db.get(DataFolder, req.parent_id)
        if not parent:
            raise HTTPException(404, "父目录不存在")

    existing = db.query(DataFolder).filter(
        DataFolder.parent_id == req.parent_id,
        DataFolder.name == req.name.strip(),
        DataFolder.workspace_scope == req.workspace_scope,
    ).first()
    if existing:
        raise HTTPException(400, "同名目录已存在")

    folder = DataFolder(
        name=req.name.strip(),
        parent_id=req.parent_id,
        workspace_scope=req.workspace_scope,
        owner_id=user.id,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return {"id": folder.id, "name": folder.name}


@router.patch("/folders/{folder_id}")
def patch_folder(
    folder_id: int,
    req: PatchFolderRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    folder = db.get(DataFolder, folder_id)
    if not folder:
        raise HTTPException(404, "目录不存在")

    if req.name is not None:
        folder.name = req.name.strip()
    if req.parent_id is not None:
        # 防止循环
        if req.parent_id == folder.id:
            raise HTTPException(400, "不能将目录移动到自身")
        # 检查祖先链是否包含自己
        check_id = req.parent_id
        visited = {folder.id}
        while check_id is not None:
            if check_id in visited:
                raise HTTPException(400, "不能形成循环目录结构")
            visited.add(check_id)
            parent = db.get(DataFolder, check_id)
            check_id = parent.parent_id if parent else None
        folder.parent_id = req.parent_id
    if req.sort_order is not None:
        folder.sort_order = req.sort_order
    if req.is_archived is not None:
        folder.is_archived = req.is_archived

    db.commit()
    return {"ok": True}


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    folder = db.get(DataFolder, folder_id)
    if not folder:
        raise HTTPException(404, "目录不存在")

    # 移出该目录下的表
    db.query(BusinessTable).filter(BusinessTable.folder_id == folder_id).update({"folder_id": None})
    # 移出子目录下的表
    child_ids = [c.id for c in db.query(DataFolder.id).filter(DataFolder.parent_id == folder_id).all()]
    if child_ids:
        db.query(BusinessTable).filter(BusinessTable.folder_id.in_(child_ids)).update({"folder_id": None}, synchronize_session=False)
        db.query(DataFolder).filter(DataFolder.id.in_(child_ids)).delete(synchronize_session=False)

    db.delete(folder)
    db.commit()
    return {"ok": True}


# ─── Table list & detail ─────────────────────────────────────────────────────


@router.get("/tables")
def list_tables(
    folder_id: Optional[int] = Query(None),
    source_type: Optional[str] = Query(None),
    skill_id: Optional[int] = Query(None),
    sync_status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enriched 数据表列表。"""
    query = db.query(BusinessTable).filter(BusinessTable.is_archived == False)  # noqa: E712

    if folder_id is not None:
        query = query.filter(BusinessTable.folder_id == folder_id)
    if source_type:
        query = query.filter(BusinessTable.source_type == source_type)
    if sync_status:
        query = query.filter(BusinessTable.sync_status == sync_status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (BusinessTable.display_name.like(like)) |
            (BusinessTable.table_name.like(like)) |
            (BusinessTable.description.like(like))
        )

    # 权限过滤（复用现有逻辑）
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    tables = query.order_by(BusinessTable.created_at.desc()).all()

    if not is_admin:
        filtered = []
        for t in tables:
            rules = t.validation_rules or {}
            row_scope = rules.get("row_scope", "private")
            if row_scope == "private" and t.owner_id != user.id:
                continue
            if row_scope == "department":
                dept_ids = rules.get("row_department_ids") or []
                if dept_ids and user.department_id not in dept_ids:
                    continue
            filtered.append(t)
        tables = filtered

    # skill_id 过滤
    if skill_id:
        sdq_table_names = {
            r.table_name for r in
            db.query(SkillDataQuery.table_name).filter(SkillDataQuery.skill_id == skill_id).all()
        }
        stb_table_ids = {
            r.table_id for r in
            db.query(SkillTableBinding.table_id).filter(SkillTableBinding.skill_id == skill_id).all()
        }
        tables = [t for t in tables if t.table_name in sdq_table_names or t.id in stb_table_ids]

    # Batch enrichment
    table_ids = [t.id for t in tables]
    table_names = [t.table_name for t in tables]

    # field count per table
    field_counts: dict[int, int] = {}
    if table_ids:
        for tid, cnt in db.query(TableField.table_id, func.count(TableField.id)).filter(
            TableField.table_id.in_(table_ids)
        ).group_by(TableField.table_id).all():
            field_counts[tid] = cnt

    # skill bindings per table (merged: SkillDataQuery + SkillTableBinding)
    binding_map: dict[int, list[dict]] = {tid: [] for tid in table_ids}
    if table_names:
        # SkillDataQuery
        sdq_rows = (
            db.query(SkillDataQuery.table_name, Skill.id, Skill.name)
            .join(Skill, Skill.id == SkillDataQuery.skill_id)
            .filter(SkillDataQuery.table_name.in_(table_names))
            .all()
        )
        name_to_id = {t.table_name: t.id for t in tables}
        for tname, sid, sname in sdq_rows:
            tid = name_to_id.get(tname)
            if tid:
                binding_map[tid].append({"skill_id": sid, "skill_name": sname, "source": "legacy"})

    if table_ids:
        stb_rows = (
            db.query(SkillTableBinding.table_id, Skill.id, Skill.name, TableView.id, TableView.name)
            .join(Skill, Skill.id == SkillTableBinding.skill_id)
            .outerjoin(TableView, TableView.id == SkillTableBinding.view_id)
            .filter(SkillTableBinding.table_id.in_(table_ids))
            .all()
        )
        for tid, sid, sname, vid, vname in stb_rows:
            binding_map[tid].append({
                "skill_id": sid, "skill_name": sname,
                "view_id": vid, "view_name": vname,
                "source": "binding",
            })

    # role_group count + view count per table (for permission summary)
    rg_counts: dict[int, int] = {}
    view_counts: dict[int, int] = {}
    if table_ids:
        for tid, cnt in db.query(TableRoleGroup.table_id, func.count(TableRoleGroup.id)).filter(
            TableRoleGroup.table_id.in_(table_ids)
        ).group_by(TableRoleGroup.table_id).all():
            rg_counts[tid] = cnt
        for tid, cnt in db.query(TableView.table_id, func.count(TableView.id)).filter(
            TableView.table_id.in_(table_ids)
        ).group_by(TableView.table_id).all():
            view_counts[tid] = cnt

    result = []
    for t in tables:
        fc = field_counts.get(t.id, 0)
        bindings = binding_map.get(t.id, [])
        # Deduplicate skills
        seen_skills = set()
        unique_bindings = []
        for b in bindings:
            if b["skill_id"] not in seen_skills:
                seen_skills.add(b["skill_id"])
                unique_bindings.append(b)

        result.append({
            "id": t.id,
            "table_name": t.table_name,
            "display_name": t.display_name,
            "description": t.description,
            "folder_id": t.folder_id,
            "source_type": t.source_type or "blank",
            "sync_status": t.sync_status or "idle",
            "last_synced_at": _serialize_datetime(t.last_synced_at),
            "record_count": t.record_count_cache,
            "field_count": fc,
            "bound_skills": unique_bindings,
            "risk_warnings": _table_risk_warnings(t, fc, len(unique_bindings)),
            "is_archived": t.is_archived or False,
            "created_at": _serialize_datetime(t.created_at),
            "role_group_count": rg_counts.get(t.id, 0),
            "view_count": view_counts.get(t.id, 0),
        })

    return {"items": result, "total": len(result)}


@router.get("/tables/{table_id}")
def get_table_detail(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一次返回完整聚合详情。"""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    rules = bt.validation_rules or {}

    # 字段列表
    fields = db.query(TableField).filter(TableField.table_id == table_id).order_by(TableField.sort_order).all()
    fields_data = [
        {
            "id": f.id,
            "field_name": f.field_name,
            "display_name": f.display_name,
            "physical_column_name": f.physical_column_name,
            "field_type": f.field_type,
            "source_field_type": f.source_field_type,
            "is_nullable": f.is_nullable,
            "is_system": f.is_system,
            "is_filterable": f.is_filterable,
            "is_groupable": f.is_groupable,
            "is_sortable": f.is_sortable,
            "enum_values": f.enum_values or [],
            "enum_source": f.enum_source,
            "sample_values": f.sample_values or [],
            "distinct_count": f.distinct_count_cache,
            "null_ratio": f.null_ratio,
            "description": f.description,
            "field_role_tags": f.field_role_tags or [],
            "is_enum": f.is_enum or False,
            "is_free_text": f.is_free_text or False,
            "is_sensitive": f.is_sensitive or False,
        }
        for f in fields
    ]

    # 如果没有 table_fields 记录，fallback 到 INFORMATION_SCHEMA
    if not fields_data:
        try:
            col_rows = db.execute(
                text("""
                    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
                    ORDER BY ORDINAL_POSITION
                """),
                {"table_name": bt.table_name},
            ).fetchall()
            fields_data = [
                {
                    "id": None,
                    "field_name": r[0],
                    "display_name": r[0],
                    "physical_column_name": r[0],
                    "field_type": r[1],
                    "source_field_type": None,
                    "is_nullable": r[2] == "YES",
                    "is_system": r[0] in ("id", "created_at", "updated_at", "_record_id", "_synced_at"),
                    "is_filterable": True,
                    "is_groupable": False,
                    "is_sortable": True,
                    "enum_values": [],
                    "enum_source": None,
                    "sample_values": [],
                    "distinct_count": None,
                    "null_ratio": None,
                    "description": r[3] or "",
                }
                for r in col_rows
            ]
        except Exception:
            fields_data = []

    # 权限（从 validation_rules 解析，不引入新 policy 对象）
    access_policy = {
        "access_scope": rules.get("access_scope", "self"),
        "access_user_ids": rules.get("access_user_ids", []),
        "access_role_ids": rules.get("access_role_ids", []),
        "access_department_ids": rules.get("access_department_ids", []),
        "access_project_ids": rules.get("access_project_ids", []),
        "row_scope": rules.get("row_scope", "private"),
        "row_department_ids": rules.get("row_department_ids", []),
        "column_scope": rules.get("column_scope", "private"),
        "column_department_ids": rules.get("column_department_ids", []),
        "hidden_fields": rules.get("hidden_fields", []),
    }

    # 视图列表
    views = db.query(TableView).filter(TableView.table_id == table_id).order_by(TableView.created_at).all()
    views_data = [
        {
            "id": v.id,
            "name": v.name,
            "view_type": v.view_type,
            "view_purpose": v.view_purpose,
            "visibility_scope": v.visibility_scope or "table_inherit",
            "is_default": v.is_default or False,
            "is_system": v.is_system or False,
            "config": v.config or {},
            "created_by": v.created_by,
            "visible_field_ids": v.visible_field_ids or [],
            "view_kind": v.view_kind or "list",
            "disclosure_ceiling": v.disclosure_ceiling,
            "allowed_role_group_ids": v.allowed_role_group_ids or [],
            "allowed_skill_ids": v.allowed_skill_ids or [],
            "row_limit": v.row_limit,
        }
        for v in views
    ]

    # Skill 绑定（合并 SkillDataQuery + SkillTableBinding）
    bindings_data = []

    # SkillDataQuery（声明层）
    sdq_rows = (
        db.query(SkillDataQuery, Skill.name)
        .join(Skill, Skill.id == SkillDataQuery.skill_id)
        .filter(SkillDataQuery.table_name == bt.table_name)
        .all()
    )
    bound_skill_ids = set()

    # SkillTableBinding（执行层）
    stb_rows = (
        db.query(SkillTableBinding)
        .filter(SkillTableBinding.table_id == table_id)
        .all()
    )
    stb_by_skill: dict[int, list] = {}
    for stb in stb_rows:
        stb_by_skill.setdefault(stb.skill_id, []).append(stb)
        bound_skill_ids.add(stb.skill_id)

    for sdq, skill_name in sdq_rows:
        stb_list = stb_by_skill.pop(sdq.skill_id, [])
        if stb_list:
            for stb in stb_list:
                view = db.get(TableView, stb.view_id) if stb.view_id else None
                bindings_data.append({
                    "skill_id": sdq.skill_id,
                    "skill_name": skill_name,
                    "binding_id": stb.id,
                    "view_id": stb.view_id,
                    "view_name": view.name if view else None,
                    "binding_type": stb.binding_type,
                    "alias": stb.alias,
                    "status": "healthy",
                })
        else:
            bindings_data.append({
                "skill_id": sdq.skill_id,
                "skill_name": skill_name,
                "binding_id": None,
                "view_id": None,
                "view_name": None,
                "binding_type": None,
                "alias": None,
                "status": "legacy_unbound",
            })

    # Bindings without SkillDataQuery
    for skill_id, stb_list in stb_by_skill.items():
        skill = db.get(Skill, skill_id)
        for stb in stb_list:
            view = db.get(TableView, stb.view_id) if stb.view_id else None
            bindings_data.append({
                "skill_id": skill_id,
                "skill_name": skill.name if skill else f"skill_{skill_id}",
                "binding_id": stb.id,
                "view_id": stb.view_id,
                "view_name": view.name if view else None,
                "binding_type": stb.binding_type,
                "alias": stb.alias,
                "status": "healthy",
            })

    # 最近 5 条同步记录
    sync_jobs = (
        db.query(TableSyncJob)
        .filter(TableSyncJob.table_id == table_id)
        .order_by(TableSyncJob.id.desc())
        .limit(5)
        .all()
    )
    sync_jobs_data = [
        {
            "id": j.id,
            "job_type": j.job_type,
            "status": j.status,
            "error_type": j.error_type,
            "error_message": j.error_message,
            "started_at": _serialize_datetime(j.started_at),
            "finished_at": _serialize_datetime(j.finished_at),
            "trigger_source": j.trigger_source,
            "stats": j.stats or {},
        }
        for j in sync_jobs
    ]

    binding_count = len([b for b in bindings_data if b["status"] != "legacy_unbound"])
    field_count = len(fields_data)

    # v1: 角色组 & 权限策略 & Skill 授权
    role_groups_data = [
        _serialize_role_group(rg)
        for rg in db.query(TableRoleGroup).filter(TableRoleGroup.table_id == table_id).order_by(TableRoleGroup.id).all()
    ]
    policies_data = [
        _serialize_policy(p)
        for p in db.query(TablePermissionPolicy).filter(TablePermissionPolicy.table_id == table_id).order_by(TablePermissionPolicy.id).all()
    ]
    grants = db.query(SkillDataGrant).filter(SkillDataGrant.table_id == table_id).order_by(SkillDataGrant.id).all()
    grants_data = []
    for g in grants:
        gd = _serialize_grant(g)
        skill = db.get(Skill, g.skill_id)
        gd["skill_name"] = skill.name if skill else f"skill_{g.skill_id}"
        view = db.get(TableView, g.view_id) if g.view_id else None
        gd["view_name"] = view.name if view else None
        grants_data.append(gd)

    return {
        "id": bt.id,
        "table_name": bt.table_name,
        "display_name": bt.display_name,
        "description": bt.description,
        "folder_id": bt.folder_id,
        "source_type": bt.source_type or "blank",
        "source_ref": bt.source_ref or {},
        "sync_status": bt.sync_status or "idle",
        "sync_error": bt.sync_error,
        "last_synced_at": _serialize_datetime(bt.last_synced_at),
        "field_profile_status": bt.field_profile_status or "pending",
        "field_profile_error": bt.field_profile_error,
        "record_count": bt.record_count_cache,
        "is_archived": bt.is_archived or False,
        "owner_id": bt.owner_id,
        "department_id": bt.department_id,
        "created_at": _serialize_datetime(bt.created_at),
        "updated_at": _serialize_datetime(bt.updated_at),
        "fields": fields_data,
        "access_policy": access_policy,
        "views": views_data,
        "bindings": bindings_data,
        "recent_sync_jobs": sync_jobs_data,
        "risk_warnings": _table_risk_warnings(bt, field_count, binding_count),
        "role_groups": role_groups_data,
        "permission_policies": policies_data,
        "skill_grants": grants_data,
    }


# ─── Table move ──────────────────────────────────────────────────────────────


class MoveTableRequest(BaseModel):
    folder_id: int | None = None
    sort_order: int | None = None


@router.patch("/tables/{table_id}/move")
def move_table(
    table_id: int,
    req: MoveTableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    if req.folder_id is not None:
        if req.folder_id > 0:
            folder = db.get(DataFolder, req.folder_id)
            if not folder:
                raise HTTPException(404, "目标目录不存在")
        bt.folder_id = req.folder_id if req.folder_id > 0 else None

    if req.sort_order is not None:
        rules = dict(bt.validation_rules or {})
        rules["sort_order"] = req.sort_order
        bt.validation_rules = rules
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(bt, "validation_rules")

    db.commit()
    return {"ok": True}


# ─── Field profile ───────────────────────────────────────────────────────────


@router.get("/tables/{table_id}/profile")
def get_table_profile(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    fields = db.query(TableField).filter(TableField.table_id == table_id).order_by(TableField.sort_order).all()

    return {
        "table_id": table_id,
        "profile_status": bt.field_profile_status or "pending",
        "profile_error": bt.field_profile_error,
        "field_profiles": [
            {
                "field_id": f.id,
                "field_name": f.field_name,
                "display_name": f.display_name,
                "field_type": f.field_type,
                "enum_values": f.enum_values or [],
                "enum_source": f.enum_source,
                "sample_values": f.sample_values or [],
                "distinct_count": f.distinct_count_cache,
                "null_ratio": f.null_ratio,
                "is_filterable": f.is_filterable,
                "is_groupable": f.is_groupable,
                "is_sortable": f.is_sortable,
            }
            for f in fields
        ],
    }


# ─── Preview ─────────────────────────────────────────────────────────────────


@router.get("/tables/{table_id}/preview")
def get_table_preview(
    table_id: int,
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    import decimal as _decimal

    def _serialize(v):
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.isoformat()
        if isinstance(v, _decimal.Decimal):
            return float(v)
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v

    try:
        # Columns
        col_rows = db.execute(
            text("""
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
                ORDER BY ORDINAL_POSITION
            """),
            {"t": bt.table_name},
        ).fetchall()
        columns = [{"name": r[0], "type": r[1]} for r in col_rows]

        # Row count
        record_count = db.execute(text(f"SELECT COUNT(*) FROM `{bt.table_name}`")).scalar()

        # Preview rows
        result = db.execute(text(f"SELECT * FROM `{bt.table_name}` LIMIT :lim"), {"lim": page_size})
        col_names = list(result.keys())
        rows = [{k: _serialize(v) for k, v in zip(col_names, row)} for row in result.fetchall()]

        # Update cache
        if bt.record_count_cache != record_count:
            bt.record_count_cache = record_count
            db.commit()

    except Exception as e:
        return {
            "columns": [],
            "rows": [],
            "summary": {"record_count": 0, "field_count": 0, "last_synced_at": None},
            "warnings": [{"code": "QUERY_ERROR", "message": str(e)}],
        }

    # Warnings
    warnings = []
    rules = bt.validation_rules or {}
    if bt.field_profile_status == "pending":
        warnings.append({"code": "PROFILE_PENDING", "message": "字段画像待分析"})

    # Check for owner field
    has_owner_field = bool(rules.get("owner_field")) or any(
        c["name"] in ("owner_id", "负责人", "sales_rep_id") for c in columns
    )
    if not has_owner_field:
        warnings.append({"code": "NO_OWNER_FIELD", "message": "当前未配置归属字段，后续行级权限只能用固定筛选"})

    return {
        "columns": columns,
        "rows": rows,
        "summary": {
            "record_count": record_count,
            "field_count": len(columns),
            "last_synced_at": _serialize_datetime(bt.last_synced_at),
        },
        "warnings": warnings,
    }


# ─── Skill bindings ──────────────────────────────────────────────────────────


@router.get("/tables/{table_id}/bindings")
def list_bindings(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    # Reuse detail logic for bindings
    detail = get_table_detail(table_id, db, user)
    return {"items": detail["bindings"]}


class CreateBindingRequest(BaseModel):
    skill_id: int
    table_id: int
    view_id: int | None = None
    binding_type: str = "runtime_read"
    alias: str | None = None
    description: str | None = None


@router.post("/bindings")
def create_binding(
    req: CreateBindingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, req.table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    skill = db.get(Skill, req.skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    if req.view_id:
        view = db.get(TableView, req.view_id)
        if not view or view.table_id != req.table_id:
            raise HTTPException(400, "视图不存在或不属于该表")

    binding = SkillTableBinding(
        skill_id=req.skill_id,
        table_id=req.table_id,
        view_id=req.view_id,
        binding_type=req.binding_type,
        alias=req.alias,
        description=req.description,
        created_by=user.id,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)

    view = db.get(TableView, binding.view_id) if binding.view_id else None
    return {
        "id": binding.id,
        "skill_id": binding.skill_id,
        "skill_name": skill.name,
        "table_id": binding.table_id,
        "view_id": binding.view_id,
        "view_name": view.name if view else None,
        "binding_type": binding.binding_type,
    }


@router.delete("/bindings/{binding_id}")
def delete_binding(
    binding_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    binding = db.get(SkillTableBinding, binding_id)
    if not binding:
        raise HTTPException(404, "绑定不存在")
    db.delete(binding)
    db.commit()
    return {"ok": True}


# ─── Sync jobs ───────────────────────────────────────────────────────────────


@router.get("/tables/{table_id}/sync-jobs")
def list_sync_jobs(
    table_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    jobs = (
        db.query(TableSyncJob)
        .filter(TableSyncJob.table_id == table_id)
        .order_by(TableSyncJob.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "status": j.status,
                "error_type": j.error_type,
                "error_message": j.error_message,
                "started_at": _serialize_datetime(j.started_at),
                "finished_at": _serialize_datetime(j.finished_at),
                "trigger_source": j.trigger_source,
                "stats": j.stats or {},
            }
            for j in jobs
        ]
    }


@router.post("/tables/{table_id}/sync")
async def trigger_sync(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """触发同步（包装现有 bitable_sync）。"""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    if bt.source_type != "lark_bitable":
        raise HTTPException(400, "该表不是飞书来源，无法同步")

    from app.services.bitable_sync import bitable_sync
    from app.services.lark_client import LarkConfigError, LarkAuthError

    try:
        result = await bitable_sync.incremental_sync(db, bt)
        return {"ok": True, **result}
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")
    except Exception as e:
        raise HTTPException(500, f"同步失败: {e}")


# ─── Lark probe (enhanced) ───────────────────────────────────────────────────


class LarkProbeRequest(BaseModel):
    app_token: str
    table_id: str
    display_name: str = ""


# Bitable field type code → human-readable type mapping
_BITABLE_TYPE_NAMES = {
    1: "text", 2: "number", 3: "single_select", 4: "multi_select",
    5: "date", 7: "boolean", 11: "person", 13: "phone", 15: "url",
    17: "attachment", 18: "relation", 19: "lookup", 20: "formula",
    22: "created_time", 23: "modified_time", 24: "created_by", 25: "modified_by",
    1001: "auto_number", 1004: "currency", 1005: "rating", 1006: "email",
}

# 可能是归属字段的关键词
_OWNER_KEYWORDS = {"负责人", "销售", "owner", "responsible", "assignee", "经办人", "创建人"}
_DEPT_KEYWORDS = {"部门", "归属部门", "department", "团队", "team"}
_FILTER_TYPES = {3, 4, 7}  # single_select, multi_select, checkbox


@router.post("/lark/probe")
async def enhanced_lark_probe(
    req: LarkProbeRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """增强版飞书预览：返回枚举值 + 建议权限字段。"""
    from app.services.lark_client import lark_client, LarkConfigError, LarkAuthError

    try:
        token = await lark_client.get_tenant_access_token()
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    import httpx
    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

    async with httpx.AsyncClient(timeout=15) as client:
        # Fields
        r = await client.get(
            f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/fields",
            headers=headers,
            params={"page_size": 100},
        )
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(400, f"获取字段失败: {data.get('msg')} (code={data.get('code')})")
        raw_fields = data["data"]["items"]

        # Preview rows
        r2 = await client.post(
            f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/records/search",
            headers=headers,
            json={"page_size": 20},
        )
        data2 = r2.json()
        preview_rows = []
        if data2.get("code") == 0:
            from app.routers.business_tables import _flatten_bitable_cell
            field_names = [f["field_name"] for f in raw_fields]
            for rec in data2["data"].get("items", []):
                row = {fn: rec.get("fields", {}).get(fn) for fn in field_names}
                flat = {k: _flatten_bitable_cell(v) for k, v in row.items()}
                preview_rows.append(flat)

    # Build enriched column info
    columns = []
    owner_fields = []
    department_fields = []
    filterable_fields = []

    for f in raw_fields:
        fname = f["field_name"]
        ftype = f.get("type", 1)
        type_name = _BITABLE_TYPE_NAMES.get(ftype, f"type_{ftype}")

        # Extract enum values from single_select / multi_select
        enum_values = []
        if ftype in (3, 4):  # single_select, multi_select
            options = f.get("property", {}).get("options", [])
            enum_values = [opt.get("name", "") for opt in options if opt.get("name")]

        # Sample values from preview
        sample_vals = list({
            str(row.get(fname, ""))
            for row in preview_rows[:10]
            if row.get(fname) is not None and row.get(fname) != ""
        })[:5]

        columns.append({
            "field_name": fname,
            "field_type": type_name,
            "source_field_type": ftype,
            "enum_values": enum_values,
            "sample_values": sample_vals,
        })

        # Permission candidates
        fname_lower = fname.lower()
        if ftype == 11 or any(kw in fname_lower for kw in _OWNER_KEYWORDS):
            owner_fields.append(fname)
        if any(kw in fname_lower for kw in _DEPT_KEYWORDS):
            department_fields.append(fname)
        if ftype in _FILTER_TYPES or enum_values:
            filterable_fields.append(fname)

    return {
        "app_token": req.app_token,
        "table_id": req.table_id,
        "table_name": req.display_name or req.table_id,
        "source_type": "lark_bitable",
        "columns": columns,
        "preview_rows": preview_rows,
        "permission_candidates": {
            "owner_fields": owner_fields,
            "department_fields": department_fields,
            "filterable_fields": filterable_fields,
        },
    }


# ─── View impact check ──────────────────────────────────────────────────────


@router.get("/views/{view_id}/impact")
def check_view_impact(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除视图前检查影响：哪些 Skill 绑定会受影响。"""
    bindings = db.query(SkillTableBinding).filter(SkillTableBinding.view_id == view_id).all()
    if not bindings:
        return {"affected_skills": [], "can_delete": True}

    affected = []
    for b in bindings:
        skill = db.get(Skill, b.skill_id)
        affected.append({
            "skill_id": b.skill_id,
            "skill_name": skill.name if skill else f"skill_{b.skill_id}",
            "binding_type": b.binding_type,
        })

    return {
        "affected_skills": affected,
        "can_delete": False,
        "message": f"该视图被 {len(affected)} 个 Skill 绑定引用，删除前请先转移绑定",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase v1: 数据资产管理升级 API
# ═══════════════════════════════════════════════════════════════════════════════


# ─── Role Group CRUD ──────────────────────────────────────────────────────────


class RoleGroupCreate(BaseModel):
    name: str
    group_type: str = "human_role"
    subject_scope: str = "custom"
    user_ids: list[int] = []
    department_ids: list[int] = []
    role_keys: list[str] = []
    skill_ids: list[int] = []
    description: str | None = None


class RoleGroupPatch(BaseModel):
    name: str | None = None
    group_type: str | None = None
    subject_scope: str | None = None
    user_ids: list[int] | None = None
    department_ids: list[int] | None = None
    role_keys: list[str] | None = None
    skill_ids: list[int] | None = None
    description: str | None = None


def _serialize_role_group(rg: TableRoleGroup) -> dict:
    return {
        "id": rg.id,
        "table_id": rg.table_id,
        "name": rg.name,
        "group_type": rg.group_type,
        "subject_scope": rg.subject_scope,
        "user_ids": rg.user_ids or [],
        "department_ids": rg.department_ids or [],
        "role_keys": rg.role_keys or [],
        "skill_ids": rg.skill_ids or [],
        "description": rg.description,
        "is_system": rg.is_system or False,
        "created_at": _serialize_datetime(rg.created_at),
        "updated_at": _serialize_datetime(rg.updated_at),
    }


@router.get("/tables/{table_id}/role-groups")
def list_role_groups(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    groups = db.query(TableRoleGroup).filter(TableRoleGroup.table_id == table_id).order_by(TableRoleGroup.id).all()
    return {"items": [_serialize_role_group(g) for g in groups]}


@router.post("/tables/{table_id}/role-groups")
def create_role_group(
    table_id: int,
    req: RoleGroupCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    if not req.name.strip():
        raise HTTPException(400, "角色组名称不能为空")

    rg = TableRoleGroup(
        table_id=table_id,
        name=req.name.strip(),
        group_type=req.group_type,
        subject_scope=req.subject_scope,
        user_ids=req.user_ids,
        department_ids=req.department_ids,
        role_keys=req.role_keys,
        skill_ids=req.skill_ids,
        description=req.description,
    )
    db.add(rg)
    db.flush()
    _write_audit_log(db, user, "create_role_group", "table_role_groups", rg.id,
                     new_values={"name": rg.name, "table_id": table_id, "group_type": rg.group_type})
    db.commit()
    db.refresh(rg)
    return _serialize_role_group(rg)


@router.patch("/role-groups/{group_id}")
def patch_role_group(
    group_id: int,
    req: RoleGroupPatch,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    rg = db.get(TableRoleGroup, group_id)
    if not rg:
        raise HTTPException(404, "角色组不存在")
    if rg.is_system:
        raise HTTPException(400, "系统角色组不可编辑")

    old_values = {f: getattr(rg, f) for f in ("name", "group_type", "subject_scope", "user_ids", "department_ids", "role_keys", "skill_ids")}
    changed = {}
    for field in ("name", "group_type", "subject_scope", "user_ids", "department_ids", "role_keys", "skill_ids", "description"):
        val = getattr(req, field)
        if val is not None:
            changed[field] = val.strip() if isinstance(val, str) else val
            setattr(rg, field, changed[field])

    if changed:
        _write_audit_log(db, user, "update_role_group", "table_role_groups", rg.id,
                         old_values=old_values, new_values=changed)
    db.commit()
    db.refresh(rg)
    return _serialize_role_group(rg)


@router.delete("/role-groups/{group_id}")
def delete_role_group(
    group_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    rg = db.get(TableRoleGroup, group_id)
    if not rg:
        raise HTTPException(404, "角色组不存在")
    if rg.is_system:
        raise HTTPException(400, "系统角色组不可删除")
    _write_audit_log(db, user, "delete_role_group", "table_role_groups", rg.id,
                     old_values={"name": rg.name, "table_id": rg.table_id})
    db.delete(rg)
    db.commit()
    return {"ok": True}


# ─── Permission Policies ──────────────────────────────────────────────────────


class PermissionPolicyItem(BaseModel):
    id: int | None = None
    role_group_id: int
    view_id: int | None = None
    row_access_mode: str = "none"
    row_rule_json: dict = {}
    field_access_mode: str = "all"
    allowed_field_ids: list[int] = []
    blocked_field_ids: list[int] = []
    disclosure_level: str = "L0"
    masking_rule_json: dict = {}
    tool_permission_mode: str = "deny"
    export_permission: bool = False
    reason_template: str | None = None


class PermissionPoliciesBatchSave(BaseModel):
    policies: list[PermissionPolicyItem]


def _serialize_policy(p: TablePermissionPolicy) -> dict:
    return {
        "id": p.id,
        "table_id": p.table_id,
        "view_id": p.view_id,
        "role_group_id": p.role_group_id,
        "row_access_mode": p.row_access_mode,
        "row_rule_json": p.row_rule_json or {},
        "field_access_mode": p.field_access_mode,
        "allowed_field_ids": p.allowed_field_ids or [],
        "blocked_field_ids": p.blocked_field_ids or [],
        "disclosure_level": p.disclosure_level,
        "masking_rule_json": p.masking_rule_json or {},
        "tool_permission_mode": p.tool_permission_mode,
        "export_permission": p.export_permission or False,
        "reason_template": p.reason_template,
        "created_at": _serialize_datetime(p.created_at),
        "updated_at": _serialize_datetime(p.updated_at),
    }


VALID_DISCLOSURE_LEVELS = {"L0", "L1", "L2", "L3", "L4"}


@router.get("/tables/{table_id}/permission-policies")
def list_permission_policies(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    policies = db.query(TablePermissionPolicy).filter(
        TablePermissionPolicy.table_id == table_id
    ).order_by(TablePermissionPolicy.id).all()
    return {"items": [_serialize_policy(p) for p in policies]}


@router.put("/tables/{table_id}/permission-policies")
def batch_save_permission_policies(
    table_id: int,
    req: PermissionPoliciesBatchSave,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    # Validate disclosure levels
    for item in req.policies:
        if item.disclosure_level not in VALID_DISCLOSURE_LEVELS:
            raise HTTPException(400, f"无效的披露级别: {item.disclosure_level}")

    # Delete existing policies for this table (not view-specific)
    db.query(TablePermissionPolicy).filter(
        TablePermissionPolicy.table_id == table_id,
        TablePermissionPolicy.view_id.is_(None),
    ).delete(synchronize_session=False)

    saved = []
    for item in req.policies:
        p = TablePermissionPolicy(
            table_id=table_id,
            view_id=item.view_id,
            role_group_id=item.role_group_id,
            row_access_mode=item.row_access_mode,
            row_rule_json=item.row_rule_json,
            field_access_mode=item.field_access_mode,
            allowed_field_ids=item.allowed_field_ids,
            blocked_field_ids=item.blocked_field_ids,
            disclosure_level=item.disclosure_level,
            masking_rule_json=item.masking_rule_json,
            tool_permission_mode=item.tool_permission_mode,
            export_permission=item.export_permission,
            reason_template=item.reason_template,
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(p)
        saved.append(p)

    _write_audit_log(db, user, "batch_save_permission_policies", "table_permission_policies", table_id,
                     new_values={"count": len(saved), "role_group_ids": [i.role_group_id for i in req.policies]})
    db.commit()
    for p in saved:
        db.refresh(p)
    return {"items": [_serialize_policy(p) for p in saved]}


@router.put("/views/{view_id}/permission-policies")
def save_view_permission_policies(
    view_id: int,
    req: PermissionPoliciesBatchSave,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    view = db.get(TableView, view_id)
    if not view:
        raise HTTPException(404, "视图不存在")

    db.query(TablePermissionPolicy).filter(
        TablePermissionPolicy.view_id == view_id,
    ).delete(synchronize_session=False)

    saved = []
    for item in req.policies:
        if item.disclosure_level not in VALID_DISCLOSURE_LEVELS:
            raise HTTPException(400, f"无效的披露级别: {item.disclosure_level}")
        p = TablePermissionPolicy(
            table_id=view.table_id,
            view_id=view_id,
            role_group_id=item.role_group_id,
            row_access_mode=item.row_access_mode,
            row_rule_json=item.row_rule_json,
            field_access_mode=item.field_access_mode,
            allowed_field_ids=item.allowed_field_ids,
            blocked_field_ids=item.blocked_field_ids,
            disclosure_level=item.disclosure_level,
            masking_rule_json=item.masking_rule_json,
            tool_permission_mode=item.tool_permission_mode,
            export_permission=item.export_permission,
            reason_template=item.reason_template,
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(p)
        saved.append(p)

    _write_audit_log(db, user, "save_view_permission_policies", "table_permission_policies", view_id,
                     new_values={"count": len(saved), "view_id": view_id})
    db.commit()
    for p in saved:
        db.refresh(p)
    return {"items": [_serialize_policy(p) for p in saved]}


# ─── Field Dictionary ─────────────────────────────────────────────────────────


def _serialize_dict_entry(e: FieldValueDictionary) -> dict:
    return {
        "id": e.id,
        "field_id": e.field_id,
        "value": e.value,
        "label": e.label,
        "is_active": e.is_active,
        "source": e.source,
        "sort_order": e.sort_order,
        "hit_count": e.hit_count,
        "last_seen_at": _serialize_datetime(e.last_seen_at),
    }


@router.get("/fields/{field_id}/dictionary")
def get_field_dictionary(
    field_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    field = db.get(TableField, field_id)
    if not field:
        raise HTTPException(404, "字段不存在")
    entries = db.query(FieldValueDictionary).filter(
        FieldValueDictionary.field_id == field_id
    ).order_by(FieldValueDictionary.sort_order).all()
    return {
        "field_id": field_id,
        "field_name": field.field_name,
        "is_enum": field.is_enum or False,
        "is_free_text": field.is_free_text or False,
        "items": [_serialize_dict_entry(e) for e in entries],
    }


class FieldDictionaryBatchSave(BaseModel):
    entries: list[dict]  # [{value, label?, is_active?, source?, sort_order?}]
    is_enum: bool | None = None


@router.put("/fields/{field_id}/dictionary")
def save_field_dictionary(
    field_id: int,
    req: FieldDictionaryBatchSave,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    field = db.get(TableField, field_id)
    if not field:
        raise HTTPException(404, "字段不存在")

    # 合并逻辑: synced 值不被 manual 删除（只标记 is_active=false）
    existing = db.query(FieldValueDictionary).filter(FieldValueDictionary.field_id == field_id).all()
    synced_values = {e.value: e for e in existing if e.source == "synced"}
    new_values = {entry.get("value", ""): entry for entry in req.entries}

    # 删除非 synced 的旧条目
    for e in existing:
        if e.source != "synced":
            db.delete(e)
        elif e.value not in new_values:
            # synced 值不在新列表中 → 标记为不活跃
            e.is_active = False

    saved = []
    for i, entry in enumerate(req.entries):
        val = entry.get("value", "")
        # 如果是 synced 值，更新但保留 source
        if val in synced_values:
            e = synced_values[val]
            e.label = entry.get("label", e.label)
            e.is_active = entry.get("is_active", True)
            e.sort_order = entry.get("sort_order", i)
            saved.append(e)
            continue
        e = FieldValueDictionary(
            field_id=field_id,
            value=entry.get("value", ""),
            label=entry.get("label"),
            is_active=entry.get("is_active", True),
            source=entry.get("source", "manual"),
            sort_order=entry.get("sort_order", i),
        )
        db.add(e)
        saved.append(e)

    if req.is_enum is not None:
        field.is_enum = req.is_enum
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(field, "is_enum")

    db.commit()
    for e in saved:
        db.refresh(e)
    return {"items": [_serialize_dict_entry(e) for e in saved]}


class EnumValueCreate(BaseModel):
    value: str
    label: str | None = None
    source: str = "manual"


@router.post("/fields/{field_id}/enum-values")
def add_enum_value(
    field_id: int,
    req: EnumValueCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    field = db.get(TableField, field_id)
    if not field:
        raise HTTPException(404, "字段不存在")

    max_order = db.query(func.max(FieldValueDictionary.sort_order)).filter(
        FieldValueDictionary.field_id == field_id
    ).scalar() or 0

    e = FieldValueDictionary(
        field_id=field_id,
        value=req.value,
        label=req.label,
        source=req.source,
        sort_order=max_order + 1,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return _serialize_dict_entry(e)


@router.delete("/fields/{field_id}/enum-values/{value_id}")
def delete_enum_value(
    field_id: int,
    value_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    e = db.get(FieldValueDictionary, value_id)
    if not e or e.field_id != field_id:
        raise HTTPException(404, "枚举值不存在")
    db.delete(e)
    db.commit()
    return {"ok": True}


class FieldTagsPatch(BaseModel):
    field_role_tags: list[str] | None = None
    is_sensitive: bool | None = None
    is_enum: bool | None = None
    is_free_text: bool | None = None


@router.patch("/fields/{field_id}/tags")
def patch_field_tags(
    field_id: int,
    req: FieldTagsPatch,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    field = db.get(TableField, field_id)
    if not field:
        raise HTTPException(404, "字段不存在")

    from sqlalchemy.orm.attributes import flag_modified
    if req.field_role_tags is not None:
        field.field_role_tags = req.field_role_tags
        flag_modified(field, "field_role_tags")
    if req.is_sensitive is not None:
        field.is_sensitive = req.is_sensitive
    if req.is_enum is not None:
        field.is_enum = req.is_enum
    if req.is_free_text is not None:
        field.is_free_text = req.is_free_text

    db.commit()
    return {"ok": True}


# ─── Enum upgrade suggestions ────────────────────────────────────────────────


@router.get("/tables/{table_id}/enum-suggestions")
def get_enum_suggestions(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回可能应该升级为枚举的 free text 字段。"""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    from app.services.field_profiler import suggest_enum_upgrade
    return {"suggestions": suggest_enum_upgrade(db, table_id)}


class BatchFieldTagsPatch(BaseModel):
    field_ids: list[int]
    is_sensitive: bool | None = None
    field_role_tags: list[str] | None = None
    is_enum: bool | None = None
    is_free_text: bool | None = None


@router.patch("/fields/batch-tags")
def batch_patch_field_tags(
    req: BatchFieldTagsPatch,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """批量更新字段标签（敏感、角色标签等）。"""
    from sqlalchemy.orm.attributes import flag_modified
    fields = db.query(TableField).filter(TableField.id.in_(req.field_ids)).all()
    if not fields:
        raise HTTPException(404, "未找到指定字段")

    for field in fields:
        if req.is_sensitive is not None:
            field.is_sensitive = req.is_sensitive
        if req.field_role_tags is not None:
            field.field_role_tags = req.field_role_tags
            flag_modified(field, "field_role_tags")
        if req.is_enum is not None:
            field.is_enum = req.is_enum
        if req.is_free_text is not None:
            field.is_free_text = req.is_free_text

    db.commit()
    return {"ok": True, "updated": len(fields)}


# ─── Skill Data Grants ───────────────────────────────────────────────────────


class SkillGrantItem(BaseModel):
    id: int | None = None
    skill_id: int
    view_id: int | None = None
    role_group_id: int | None = None
    grant_mode: str = "allow"
    allowed_actions: list[str] = ["read"]
    max_disclosure_level: str = "L2"
    row_rule_override_json: dict = {}
    field_rule_override_json: dict = {}
    approval_required: bool = False
    audit_level: str = "basic"


class SkillGrantsBatchSave(BaseModel):
    grants: list[SkillGrantItem]


def _serialize_grant(g: SkillDataGrant) -> dict:
    return {
        "id": g.id,
        "skill_id": g.skill_id,
        "table_id": g.table_id,
        "view_id": g.view_id,
        "role_group_id": g.role_group_id,
        "grant_mode": g.grant_mode,
        "allowed_actions": g.allowed_actions or [],
        "max_disclosure_level": g.max_disclosure_level,
        "row_rule_override_json": g.row_rule_override_json or {},
        "field_rule_override_json": g.field_rule_override_json or {},
        "approval_required": g.approval_required or False,
        "audit_level": g.audit_level,
        "created_at": _serialize_datetime(g.created_at),
        "updated_at": _serialize_datetime(g.updated_at),
    }


@router.get("/tables/{table_id}/skill-grants")
def list_skill_grants(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    grants = db.query(SkillDataGrant).filter(SkillDataGrant.table_id == table_id).order_by(SkillDataGrant.id).all()
    # Enrich with skill names
    result = []
    for g in grants:
        data = _serialize_grant(g)
        skill = db.get(Skill, g.skill_id)
        data["skill_name"] = skill.name if skill else f"skill_{g.skill_id}"
        view = db.get(TableView, g.view_id) if g.view_id else None
        data["view_name"] = view.name if view else None
        result.append(data)
    return {"items": result}


@router.put("/tables/{table_id}/skill-grants")
def batch_save_skill_grants(
    table_id: int,
    req: SkillGrantsBatchSave,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    for item in req.grants:
        if item.max_disclosure_level not in VALID_DISCLOSURE_LEVELS:
            raise HTTPException(400, f"无效的披露级别: {item.max_disclosure_level}")
        if item.grant_mode == "allow" and not item.view_id:
            raise HTTPException(400, "授权模式为 allow 时必须指定视图")

    db.query(SkillDataGrant).filter(SkillDataGrant.table_id == table_id).delete(synchronize_session=False)

    saved = []
    for item in req.grants:
        g = SkillDataGrant(
            skill_id=item.skill_id,
            table_id=table_id,
            view_id=item.view_id,
            role_group_id=item.role_group_id,
            grant_mode=item.grant_mode,
            allowed_actions=item.allowed_actions,
            max_disclosure_level=item.max_disclosure_level,
            row_rule_override_json=item.row_rule_override_json,
            field_rule_override_json=item.field_rule_override_json,
            approval_required=item.approval_required,
            audit_level=item.audit_level,
        )
        db.add(g)
        saved.append(g)

    _write_audit_log(db, user, "batch_save_skill_grants", "skill_data_grants", table_id,
                     new_values={"count": len(saved), "skill_ids": [i.skill_id for i in req.grants]})
    db.commit()
    for g in saved:
        db.refresh(g)
    return {"items": [_serialize_grant(g) for g in saved]}


# ─── Permission Explain ──────────────────────────────────────────────────────


@router.get("/tables/{table_id}/permission-explain")
def explain_permissions(
    table_id: int,
    user_id: int | None = None,
    skill_id: int | None = None,
    view_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回某用户/Skill 对某表的生效权限解释。"""
    from app.services.policy_engine import (
        resolve_user_role_groups,
        resolve_effective_policy,
        check_disclosure_capability,
        compute_visible_fields,
    )

    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")

    # 默认解释当前用户的权限；admin 可查看其他用户
    target_user = user
    if user_id and user_id != user.id:
        if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
            raise HTTPException(403, "仅管理员可查看他人权限")
        target_user = db.get(User, user_id)
        if not target_user:
            raise HTTPException(404, "用户不存在")

    groups = resolve_user_role_groups(db, bt.id, target_user, skill_id=skill_id)
    policy = resolve_effective_policy(db, bt.id, [g.id for g in groups], view_id, skill_id)
    caps = check_disclosure_capability(policy.disclosure_level)

    fields = db.query(TableField).filter(TableField.table_id == bt.id).all()
    visible_fields = compute_visible_fields(fields, policy)

    return {
        "table_id": table_id,
        "target_user_id": target_user.id,
        "skill_id": skill_id,
        "view_id": view_id,
        "denied": policy.denied,
        "deny_reasons": policy.deny_reasons,
        "matched_role_groups": [
            {"id": g.id, "name": g.name, "group_type": g.group_type}
            for g in groups
        ],
        "effective_policy": {
            "row_access_mode": policy.row_access_mode,
            "row_rule_json": policy.row_rule_json,
            "field_access_mode": policy.field_access_mode,
            "disclosure_level": policy.disclosure_level,
            "masking_rules": policy.masking_rules,
            "export_permission": policy.export_permission,
            "tool_permission_mode": policy.tool_permission_mode,
            "source": policy.source,
        },
        "disclosure_capabilities": caps,
        "visible_fields": [
            {"id": f.id, "field_name": f.field_name, "display_name": f.display_name}
            for f in visible_fields
        ],
        "effective_grant": policy.effective_grant,
    }


# ─── Unfiled governance ───────────────────────────────────────────────────────


@router.get("/unfiled")
def list_unfiled_tables(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回所有未归档（folder_id 为空）的数据表。"""
    tables = (
        db.query(BusinessTable)
        .filter(BusinessTable.folder_id.is_(None), BusinessTable.is_archived == False)  # noqa: E712
        .order_by(BusinessTable.created_at.desc())
        .all()
    )
    result = []
    for t in tables:
        fc = db.query(func.count(TableField.id)).filter(TableField.table_id == t.id).scalar() or 0
        result.append({
            "id": t.id,
            "table_name": t.table_name,
            "display_name": t.display_name,
            "description": t.description,
            "source_type": t.source_type or "blank",
            "field_count": fc,
            "record_count": t.record_count_cache,
            "created_at": _serialize_datetime(t.created_at),
        })
    return {"items": result, "total": len(result)}


class ClassifyRequest(BaseModel):
    folder_id: int
    reason: str | None = None


@router.post("/tables/{table_id}/classify")
def classify_table(
    table_id: int,
    req: ClassifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    folder = db.get(DataFolder, req.folder_id)
    if not folder:
        raise HTTPException(404, "目标目录不存在")
    old_folder_id = bt.folder_id
    bt.folder_id = req.folder_id
    _write_audit_log(db, user, "classify", "business_tables", table_id,
                     old_values={"folder_id": old_folder_id},
                     new_values={"folder_id": req.folder_id, "reason": req.reason})
    db.commit()
    return {"ok": True}


class BatchClassifyRequest(BaseModel):
    table_ids: list[int]
    folder_id: int
    reason: str | None = None


@router.post("/batch-classify")
def batch_classify_tables(
    req: BatchClassifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    folder = db.get(DataFolder, req.folder_id)
    if not folder:
        raise HTTPException(404, "目标目录不存在")

    # 冲突检测: 检查目标 folder 下已有的 display_name
    existing_names = {
        t.display_name
        for t in db.query(BusinessTable).filter(BusinessTable.folder_id == req.folder_id).all()
    }

    results = []
    for tid in req.table_ids:
        bt = db.get(BusinessTable, tid)
        if not bt:
            results.append({"table_id": tid, "success": False, "error": "表不存在"})
            continue
        if bt.display_name in existing_names:
            results.append({"table_id": tid, "success": False, "error": f"目标目录已有同名表: {bt.display_name}"})
            continue
        old_folder_id = bt.folder_id
        bt.folder_id = req.folder_id
        existing_names.add(bt.display_name)
        _write_audit_log(db, user, "classify", "business_tables", tid,
                         old_values={"folder_id": old_folder_id},
                         new_values={"folder_id": req.folder_id, "reason": req.reason})
        results.append({"table_id": tid, "success": True})

    db.commit()
    return {"results": results}


@router.get("/unfiled/classify-suggestions")
def suggest_classifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """基于表名/字段特征，建议未归档表应归入哪个 folder。"""
    unfiled = (
        db.query(BusinessTable)
        .filter(BusinessTable.folder_id.is_(None), BusinessTable.is_archived == False)  # noqa: E712
        .all()
    )
    folders = db.query(DataFolder).filter(DataFolder.is_archived == False).all()  # noqa: E712

    # 构建 folder 下已有表的表名前缀 → folder 映射
    folder_tables = {}
    for f in folders:
        tables = db.query(BusinessTable).filter(BusinessTable.folder_id == f.id).all()
        prefixes = set()
        for t in tables:
            parts = t.table_name.split("_")
            if len(parts) >= 2:
                prefixes.add(parts[0])
        folder_tables[f.id] = {"name": f.name, "prefixes": prefixes, "source_types": {t.source_type for t in tables}}

    suggestions = []
    for t in unfiled:
        t_prefix = t.table_name.split("_")[0] if "_" in t.table_name else ""
        best_folder = None
        reason = ""

        for fid, info in folder_tables.items():
            # 表名前缀匹配
            if t_prefix and t_prefix in info["prefixes"]:
                best_folder = fid
                reason = f"表名前缀 '{t_prefix}' 匹配已有表"
                break
            # source_type 匹配
            if t.source_type and t.source_type in info["source_types"] and t.source_type != "blank":
                best_folder = fid
                reason = f"来源类型 '{t.source_type}' 匹配"

        if best_folder:
            suggestions.append({
                "table_id": t.id,
                "table_name": t.table_name,
                "display_name": t.display_name,
                "suggested_folder_id": best_folder,
                "suggested_folder_name": folder_tables[best_folder]["name"],
                "reason": reason,
            })

    return {"suggestions": suggestions}


# ─── View CRUD ───────────────────────────────────────────────────────────────


VALID_VIEW_KINDS = {"list", "board", "metric", "pivot", "review_queue"}


class ViewCreateRequest(BaseModel):
    name: str
    view_type: str = "grid"
    view_kind: str = "list"
    visible_field_ids: list[int] = []
    disclosure_ceiling: str | None = None
    allowed_role_group_ids: list[int] = []
    allowed_skill_ids: list[int] = []
    row_limit: int | None = None
    config: dict = {}


class ViewPatchRequest(BaseModel):
    name: str | None = None
    view_type: str | None = None
    view_kind: str | None = None
    visible_field_ids: list[int] | None = None
    disclosure_ceiling: str | None = None
    allowed_role_group_ids: list[int] | None = None
    allowed_skill_ids: list[int] | None = None
    row_limit: int | None = None
    config: dict | None = None


def _serialize_view(v: TableView) -> dict:
    return {
        "id": v.id,
        "table_id": v.table_id,
        "name": v.name,
        "view_type": v.view_type,
        "view_purpose": v.view_purpose,
        "visibility_scope": v.visibility_scope or "table_inherit",
        "is_default": v.is_default or False,
        "is_system": v.is_system or False,
        "config": v.config or {},
        "created_by": v.created_by,
        "visible_field_ids": v.visible_field_ids or [],
        "view_kind": v.view_kind or "list",
        "disclosure_ceiling": v.disclosure_ceiling,
        "allowed_role_group_ids": v.allowed_role_group_ids or [],
        "allowed_skill_ids": v.allowed_skill_ids or [],
        "row_limit": v.row_limit,
        "created_at": _serialize_datetime(v.created_at),
        "updated_at": _serialize_datetime(v.updated_at),
    }


@router.post("/tables/{table_id}/views")
def create_view(
    table_id: int,
    req: ViewCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "数据表不存在")
    if not req.name.strip():
        raise HTTPException(400, "视图名称不能为空")
    if req.view_kind not in VALID_VIEW_KINDS:
        raise HTTPException(400, f"无效的视图类型: {req.view_kind}")
    if req.disclosure_ceiling and req.disclosure_ceiling not in VALID_DISCLOSURE_LEVELS:
        raise HTTPException(400, f"无效的披露上限: {req.disclosure_ceiling}")

    v = TableView(
        table_id=table_id,
        name=req.name.strip(),
        view_type=req.view_type,
        view_kind=req.view_kind,
        visible_field_ids=req.visible_field_ids,
        disclosure_ceiling=req.disclosure_ceiling,
        allowed_role_group_ids=req.allowed_role_group_ids,
        allowed_skill_ids=req.allowed_skill_ids,
        row_limit=req.row_limit,
        config=req.config,
        created_by=user.id,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return _serialize_view(v)


@router.patch("/views/{view_id}")
def patch_view(
    view_id: int,
    req: ViewPatchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    v = db.get(TableView, view_id)
    if not v:
        raise HTTPException(404, "视图不存在")
    if v.is_system:
        raise HTTPException(400, "系统视图不可编辑")

    if req.view_kind is not None and req.view_kind not in VALID_VIEW_KINDS:
        raise HTTPException(400, f"无效的视图类型: {req.view_kind}")
    if req.disclosure_ceiling is not None and req.disclosure_ceiling not in VALID_DISCLOSURE_LEVELS:
        raise HTTPException(400, f"无效的披露上限: {req.disclosure_ceiling}")

    from sqlalchemy.orm.attributes import flag_modified
    for field in ("name", "view_type", "view_kind", "visible_field_ids", "disclosure_ceiling",
                  "allowed_role_group_ids", "allowed_skill_ids", "row_limit", "config"):
        val = getattr(req, field)
        if val is not None:
            setattr(v, field, val.strip() if isinstance(val, str) else val)
            if field in ("visible_field_ids", "allowed_role_group_ids", "allowed_skill_ids", "config"):
                flag_modified(v, field)

    db.commit()
    db.refresh(v)
    return _serialize_view(v)


@router.delete("/views/{view_id}")
def delete_view(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    v = db.get(TableView, view_id)
    if not v:
        raise HTTPException(404, "视图不存在")
    if v.is_system:
        raise HTTPException(400, "系统视图不可删除")

    # 检查绑定影响
    binding_count = db.query(SkillTableBinding).filter(SkillTableBinding.view_id == view_id).count()
    grant_count = db.query(SkillDataGrant).filter(SkillDataGrant.view_id == view_id).count()
    policy_count = db.query(TablePermissionPolicy).filter(TablePermissionPolicy.view_id == view_id).count()

    if binding_count > 0 or grant_count > 0:
        raise HTTPException(
            400,
            f"此视图被 {binding_count} 个 Skill 绑定和 {grant_count} 个数据授权引用，请先解除后再删除"
        )

    # 删除视图级策略
    db.query(TablePermissionPolicy).filter(TablePermissionPolicy.view_id == view_id).delete(synchronize_session=False)
    db.delete(v)
    db.commit()
    return {"ok": True}


@router.get("/views/{view_id}/impact")
def view_impact(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回删除此视图的影响：有多少 binding / grant / policy 引用。"""
    v = db.get(TableView, view_id)
    if not v:
        raise HTTPException(404, "视图不存在")
    return {
        "view_id": view_id,
        "binding_count": db.query(SkillTableBinding).filter(SkillTableBinding.view_id == view_id).count(),
        "grant_count": db.query(SkillDataGrant).filter(SkillDataGrant.view_id == view_id).count(),
        "policy_count": db.query(TablePermissionPolicy).filter(TablePermissionPolicy.view_id == view_id).count(),
    }
