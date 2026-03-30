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
)
from app.models.skill import Skill
from app.models.user import Department, Role, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-assets", tags=["data-assets"])


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _folder_tree(db: Session) -> list[dict]:
    """Build nested folder tree."""
    folders = db.query(DataFolder).filter(DataFolder.is_archived == False).order_by(DataFolder.sort_order).all()  # noqa: E712
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
    """返回完整目录树。"""
    return {"items": _folder_tree(db)}


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
