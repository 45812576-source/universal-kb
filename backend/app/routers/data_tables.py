"""Business data CRUD API — row-level read/write with audit logging."""
import datetime
import decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.business import AuditLog, BusinessTable, DataOwnership
from app.models.user import User, Role
from app.services.data_visibility import data_visibility


def _check_write_permission(bt: BusinessTable, user: User):
    """写操作行级权限校验：non-admin 只能向 row_scope=all/department(自身部门) 的表写入。"""
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if is_admin:
        return
    rules = bt.validation_rules or {}
    row_scope = rules.get("row_scope", "private")
    if row_scope == "private":
        raise HTTPException(403, "该表为私有表，无写入权限")
    if row_scope == "department":
        dept_ids = rules.get("row_department_ids") or []
        if dept_ids and user.department_id not in dept_ids:
            raise HTTPException(403, "您不在该表的授权部门内，无写入权限")


def _check_row_owner(bt: BusinessTable, row_values: dict, user: User):
    """更新/删除时校验行所有权：若配置了 owner_field，非管理员只能修改自己的行。"""
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if is_admin:
        return
    rules = bt.validation_rules or {}
    row_scope = rules.get("row_scope", "private")
    if row_scope != "all":
        # private 已在 _check_write_permission 拦截；department 级别仍需校验行所有权
        owner_field = None
        from app.models.business import DataOwnership as DO
        # row_values 是从 DB 读出的字典，key 是列名
        # 尝试从 validation_rules 或 DataOwnership 找 owner_field
        if "owner_field" in rules:
            owner_field = rules["owner_field"]
        if owner_field and str(row_values.get(owner_field, "")) != str(user.id):
            raise HTTPException(403, "无权修改他人数据")

router = APIRouter(prefix="/api/data", tags=["data-tables"])


def _serialize_value(v):
    """Convert non-JSON-serializable types to string."""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


def _get_registered_table(db: Session, table_name: str) -> BusinessTable:
    bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
    if not bt:
        raise HTTPException(404, f"业务表 '{table_name}' 未注册")
    return bt


def _get_columns(db: Session, table_name: str) -> list[dict]:
    sql = text("""
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
        ORDER BY ORDINAL_POSITION
    """)
    rows = db.execute(sql, {"table_name": table_name}).fetchall()
    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": r[2] == "YES",
            "default": r[3],
            "comment": r[4] or "",
        }
        for r in rows
    ]


@router.get("/{table_name}/schema")
def get_table_schema(
    table_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    columns = _get_columns(db, table_name)
    return {
        "table_name": table_name,
        "display_name": bt.display_name,
        "description": bt.description,
        "columns": columns,
        "validation_rules": bt.validation_rules or {},
        "workflow": bt.workflow or {},
    }


@router.get("/{table_name}/rows")
def list_rows(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    offset = (page - 1) * page_size
    rules = bt.validation_rules or {}

    from app.models.user import Role
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)

    # ── Row scope: check validation_rules["row_scope"] ──
    row_scope = rules.get("row_scope", "private")
    if not is_admin and row_scope == "private":
        # Private: only admins can see
        return {"total": 0, "page": page, "page_size": page_size, "columns": [], "rows": []}

    base_sql = f"SELECT * FROM `{table_name}`"

    if not is_admin and row_scope == "department":
        dept_ids = rules.get("row_department_ids") or []
        if dept_ids and user.department_id not in dept_ids:
            return {"total": 0, "page": page, "page_size": page_size, "columns": [], "rows": []}

    # Legacy DataOwnership row-filter (owner/department field matching)
    ownership = db.query(DataOwnership).filter(DataOwnership.table_name == table_name).first()
    if ownership and not is_admin:
        conditions = []
        if ownership.owner_field:
            conditions.append(f"`{ownership.owner_field}` = {user.id}")
        if ownership.department_field and user.department_id:
            conditions.append(f"`{ownership.department_field}` = {user.department_id}")
        if conditions:
            base_sql += " WHERE (" + " OR ".join(conditions) + ")"

    count_result = db.execute(text(f"SELECT COUNT(*) FROM ({base_sql}) AS _t")).scalar()

    rows_result = db.execute(
        text(base_sql + " LIMIT :limit OFFSET :offset"),
        {"limit": page_size, "offset": offset},
    )
    all_columns = list(rows_result.keys())
    rows = [_serialize_row(dict(zip(all_columns, row))) for row in rows_result.fetchall()]

    # ── Column scope + hidden_fields ──
    hidden_fields: list[str] = rules.get("hidden_fields") or []
    col_scope = rules.get("column_scope", "all")
    if not is_admin:
        if col_scope == "private":
            # No columns visible for non-admins
            rows = [{} for _ in rows]
            all_columns = []
        elif col_scope == "department":
            dept_ids = rules.get("column_department_ids") or []
            if dept_ids and user.department_id not in dept_ids:
                rows = [{} for _ in rows]
                all_columns = []

    # Remove hidden fields
    if hidden_fields and all_columns:
        all_columns = [c for c in all_columns if c not in hidden_fields]
        rows = [{k: v for k, v in row.items() if k not in hidden_fields} for row in rows]

    # Apply legacy field-level visibility (desensitize)
    if ownership:
        desensitize_config = rules.get("desensitize_fields", {})
        rows = data_visibility.apply_visibility(rows, user, ownership, desensitize_config)

    return {
        "total": count_result,
        "page": page,
        "page_size": page_size,
        "columns": all_columns,
        "rows": rows,
    }


class RowCreate(BaseModel):
    data: dict


class RowUpdate(BaseModel):
    data: dict


@router.post("/{table_name}/rows")
def create_row(
    table_name: str,
    req: RowCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    _check_write_permission(bt, user)

    # Validate against validation_rules
    validation_rules = bt.validation_rules or {}
    for field, rules in validation_rules.items():
        val = req.data.get(field)
        if val is not None:
            if "max" in rules and float(val) > float(rules["max"]):
                raise HTTPException(400, f"{field} 不能超过 {rules['max']}")
            if "min" in rules and float(val) < float(rules["min"]):
                raise HTTPException(400, f"{field} 不能低于 {rules['min']}")
            if "enum" in rules and str(val) not in [str(e) for e in rules["enum"]]:
                raise HTTPException(400, f"{field} 只能是 {rules['enum']}")

    data = {k: v for k, v in req.data.items()}
    cols = ", ".join(f"`{k}`" for k in data.keys())
    placeholders = ", ".join(f":{k}" for k in data.keys())
    sql = text(f"INSERT INTO `{table_name}` ({cols}) VALUES ({placeholders})")

    try:
        result = db.execute(sql, data)
        db.commit()
        new_id = result.lastrowid

        # Audit log
        log = AuditLog(
            user_id=user.id,
            table_name=table_name,
            operation="INSERT",
            row_id=str(new_id),
            new_values=_serialize_row(data),
            sql_executed=str(sql),
        )
        db.add(log)
        db.commit()

        return {"id": new_id, "ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.put("/{table_name}/rows/{row_id}")
def update_row(
    table_name: str,
    row_id: int,
    req: RowUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    _check_write_permission(bt, user)

    # Get old values for audit
    old_row = db.execute(
        text(f"SELECT * FROM `{table_name}` WHERE id = :id"),
        {"id": row_id},
    ).fetchone()
    if not old_row:
        raise HTTPException(404, "Row not found")

    old_cols = db.execute(
        text(f"SELECT * FROM `{table_name}` WHERE id = :id"),
        {"id": row_id},
    )
    col_names = list(old_cols.keys())
    old_values = dict(zip(col_names, old_row))
    _check_row_owner(bt, old_values, user)

    # Validate
    validation_rules = bt.validation_rules or {}
    for field, rules in validation_rules.items():
        val = req.data.get(field)
        if val is not None:
            if "max" in rules and float(val) > float(rules["max"]):
                raise HTTPException(400, f"{field} 不能超过 {rules['max']}")
            if "min" in rules and float(val) < float(rules["min"]):
                raise HTTPException(400, f"{field} 不能低于 {rules['min']}")
            if "enum" in rules and str(val) not in [str(e) for e in rules["enum"]]:
                raise HTTPException(400, f"{field} 只能是 {rules['enum']}")

    data = {k: v for k, v in req.data.items() if k != "id"}
    set_clause = ", ".join(f"`{k}` = :{k}" for k in data.keys())
    sql = text(f"UPDATE `{table_name}` SET {set_clause} WHERE id = :__id")
    data["__id"] = row_id

    try:
        db.execute(sql, data)
        db.commit()

        # Audit log — serialize old_values properly
        log = AuditLog(
            user_id=user.id,
            table_name=table_name,
            operation="UPDATE",
            row_id=str(row_id),
            old_values=_serialize_row(old_values),
            new_values=_serialize_row(req.data),
            sql_executed=str(sql),
        )
        db.add(log)
        db.commit()

        return {"ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.delete("/{table_name}/rows/{row_id}")
def delete_row(
    table_name: str,
    row_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    _check_write_permission(bt, user)

    old_row = db.execute(
        text(f"SELECT * FROM `{table_name}` WHERE id = :id"),
        {"id": row_id},
    ).fetchone()
    if not old_row:
        raise HTTPException(404, "Row not found")

    old_cols = db.execute(
        text(f"SELECT * FROM `{table_name}` WHERE id = :id"),
        {"id": row_id},
    )
    col_names = list(old_cols.keys())
    old_values = dict(zip(col_names, old_row))
    _check_row_owner(bt, old_values, user)

    try:
        db.execute(text(f"DELETE FROM `{table_name}` WHERE id = :id"), {"id": row_id})
        db.commit()

        log = AuditLog(
            user_id=user.id,
            table_name=table_name,
            operation="DELETE",
            row_id=str(row_id),
            sql_executed=f"DELETE FROM {table_name} WHERE id = {row_id}",
        )
        db.add(log)
        db.commit()

        return {"ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(400, str(e))
