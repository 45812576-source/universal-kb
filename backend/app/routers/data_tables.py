"""Business data CRUD API — row-level read/write with audit logging."""
import csv
import datetime
import decimal
import io
import json as _json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.utils.sql_safe import qi
from app.models.business import AuditLog, BusinessTable, DataOwnership, TableRoleGroup, TableField
from app.models.user import User, Role
from app.services.data_asset_access import (
    require_table_manage_access,
    require_table_view_access,
    should_use_asset_safe_default,
)
from app.services.data_asset_codec import normalize_row_for_response, normalize_row_payload
from app.services.data_visibility import data_visibility
from app.services.policy_engine import (
    resolve_user_role_groups,
    resolve_effective_policy,
    check_disclosure_capability,
    compute_visible_columns,
    apply_field_masking,
    build_row_filter_sql,
)


def _check_write_permission(db: Session, bt: BusinessTable, user: User):
    """写操作按数据资产管理权限判定。"""
    require_table_manage_access(db, bt, user)


def _is_admin(user: User) -> bool:
    return user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)


def _has_new_policy(db: Session, bt: BusinessTable) -> bool:
    return (
        db.query(TableRoleGroup)
        .filter(TableRoleGroup.table_id == bt.id)
        .first()
        is not None
    )


def _empty_rows_result(page: int, page_size: int) -> dict:
    return {"total": 0, "page": page, "page_size": page_size, "columns": [], "rows": []}


def _empty_sample_result() -> dict:
    return {
        "total": 0,
        "columns": [],
        "rows": [],
        "sample_strategy": {"enum_fields": [], "covered": 0, "filled": 0},
    }


def _build_legacy_access_context(
    db: Session,
    bt: BusinessTable,
    user: User,
    is_admin: bool,
):
    table_name = bt.table_name
    rules = bt.validation_rules or {}
    row_scope = rules.get("row_scope", "private")

    # 已发布的表视为公开可读（发布 = 管理员明确授权共享数据）
    is_published = (bt.publish_status or "draft") == "published"
    if not is_admin and row_scope == "private" and bt.owner_id != user.id and not is_published:
        return None

    base_sql = f"SELECT * FROM {qi(table_name, '表名')}"

    if not is_admin and row_scope == "department":
        dept_ids = rules.get("row_department_ids") or []
        if dept_ids and user.department_id not in dept_ids:
            return None

    ownership = db.query(DataOwnership).filter(DataOwnership.table_name == table_name).first()
    if ownership and not is_admin:
        conditions = []
        if ownership.owner_field:
            conditions.append(f"`{ownership.owner_field}` = {user.id}")
        if ownership.department_field and user.department_id:
            conditions.append(f"`{ownership.department_field}` = {user.department_id}")
        if conditions:
            base_sql += " WHERE (" + " OR ".join(conditions) + ")"

    return {
        "base_sql": base_sql,
        "rules": rules,
        "ownership": ownership,
    }


def _apply_legacy_field_visibility(
    rows: list[dict],
    all_columns: list[str],
    rules: dict,
    ownership: DataOwnership | None,
    user: User,
    is_admin: bool,
) -> tuple[list[str], list[dict]]:
    hidden_fields: list[str] = rules.get("hidden_fields") or []
    col_scope = rules.get("column_scope", "all")
    visible_columns = list(all_columns)
    visible_rows = list(rows)

    if not is_admin:
        if col_scope == "private":
            visible_rows = [{} for _ in visible_rows]
            visible_columns = []
        elif col_scope == "department":
            dept_ids = rules.get("column_department_ids") or []
            if dept_ids and user.department_id not in dept_ids:
                visible_rows = [{} for _ in visible_rows]
                visible_columns = []

    if hidden_fields and visible_columns:
        visible_columns = [c for c in visible_columns if c not in hidden_fields]
        visible_rows = [{k: v for k, v in row.items() if k not in hidden_fields} for row in visible_rows]

    if ownership:
        desensitize_config = rules.get("desensitize_fields", {})
        visible_rows = data_visibility.apply_visibility(visible_rows, user, ownership, desensitize_config)

    return visible_columns, visible_rows


def _read_rows_for_access(
    db: Session,
    bt: BusinessTable,
    user: User,
    page: int,
    page_size: int,
    offset: int,
    view_id: int | None,
):
    is_admin = _is_admin(user)
    has_new_policy = _has_new_policy(db, bt)
    if has_new_policy and not is_admin:
        return _list_rows_new_policy(db, bt, user, page, page_size, offset, view_id)
    if should_use_asset_safe_default(user, bt, has_new_policy=has_new_policy):
        return _empty_rows_result(page, page_size)
    return _list_rows_legacy(db, bt, user, page, page_size, offset, view_id, is_admin)


def _check_row_owner(bt: BusinessTable, row_values: dict, user: User):
    """更新/删除时校验行所有权：若配置了 owner_field，非管理员只能修改自己的行。"""
    is_admin = _is_admin(user)
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


def _get_table_field_map(db: Session, bt: BusinessTable) -> dict[str, TableField]:
    fields = db.query(TableField).filter(TableField.table_id == bt.id).all()
    field_map: dict[str, TableField] = {}
    for field in fields:
        field_map[field.field_name] = field
        if field.physical_column_name:
            field_map[field.physical_column_name] = field
    return field_map


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
    require_table_view_access(db, bt, user)
    columns = _get_columns(db, table_name)
    return {
        "table_name": table_name,
        "display_name": bt.display_name,
        "description": bt.description,
        "columns": columns,
        "validation_rules": bt.validation_rules or {},
        "workflow": bt.workflow or {},
    }


_OP_MAP = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "contains": "LIKE", "starts": "LIKE", "ends": "LIKE",
}


def _apply_view_config(base_sql: str, config: dict) -> tuple[str, str]:
    """Apply view filters and sorts to a base SELECT SQL.
    Returns (filtered_sql, sorted_sql)."""
    import re

    filters = config.get("filters") or []
    sorts = config.get("sorts") or []

    where_parts = []
    for f in filters:
        field = f.get("field", "").strip()
        op = f.get("op", "eq")
        val = f.get("value", "")
        if not field or op not in _OP_MAP:
            continue
        # Sanitize field name
        if not re.match(r'^[\w\u4e00-\u9fff]+$', field):
            continue
        sql_op = _OP_MAP[op]
        if op == "contains":
            where_parts.append(f"`{field}` LIKE '%{val}%'")
        elif op == "starts":
            where_parts.append(f"`{field}` LIKE '{val}%'")
        elif op == "ends":
            where_parts.append(f"`{field}` LIKE '%{val}'")
        else:
            if isinstance(val, str):
                val_escaped = val.replace("'", "''")
                where_parts.append(f"`{field}` {sql_op} '{val_escaped}'")
            else:
                where_parts.append(f"`{field}` {sql_op} {val}")

    if where_parts:
        if "WHERE" in base_sql.upper():
            base_sql += " AND (" + " AND ".join(where_parts) + ")"
        else:
            base_sql += " WHERE " + " AND ".join(where_parts)

    if sorts:
        order_parts = []
        for s in sorts:
            field = s.get("field", "").strip()
            direction = "DESC" if s.get("dir", "asc").lower() == "desc" else "ASC"
            if field and re.match(r'^[\w\u4e00-\u9fff]+$', field):
                order_parts.append(f"`{field}` {direction}")
        if order_parts:
            base_sql += " ORDER BY " + ", ".join(order_parts)

    return base_sql


@router.get("/{table_name}/rows")
def list_rows(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    view_id: int = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    require_table_view_access(db, bt, user)
    offset = (page - 1) * page_size
    return _read_rows_for_access(db, bt, user, page, page_size, offset, view_id)


def _list_rows_new_policy(
    db: Session,
    bt: BusinessTable,
    user: User,
    page: int,
    page_size: int,
    offset: int,
    view_id: int | None,
):
    """新权限引擎路径。"""
    table_name = bt.table_name
    empty = {"total": 0, "page": page, "page_size": page_size, "columns": [], "rows": []}

    groups = resolve_user_role_groups(db, bt.id, user, skill_id=None)
    policy = resolve_effective_policy(db, bt.id, [g.id for g in groups], view_id)

    if policy.denied:
        return empty

    caps = check_disclosure_capability(policy.disclosure_level)
    if not caps["can_see_rows"]:
        return empty

    # 构建 SQL
    base_sql = f"SELECT * FROM {qi(table_name, '表名')}"
    sql_params: dict = {}

    # 行过滤（参数化）
    row_filter, row_params = build_row_filter_sql(policy, user, table_name)
    if row_filter:
        base_sql += f" WHERE ({row_filter})"
        sql_params.update(row_params)

    # 视图 config（filters + sorts）
    if view_id:
        from app.models.business import TableView
        view = db.get(TableView, view_id)
        if view and view.table_id == bt.id:
            base_sql = _apply_view_config(base_sql, view.config or {})

    count_result = db.execute(text(f"SELECT COUNT(*) FROM ({base_sql}) AS _t"), sql_params).scalar()
    rows_result = db.execute(
        text(base_sql + " LIMIT :limit OFFSET :offset"),
        {**sql_params, "limit": page_size, "offset": offset},
    )
    all_columns = list(rows_result.keys())
    field_map = _get_table_field_map(db, bt)
    rows = [
        normalize_row_for_response(_serialize_row(dict(zip(all_columns, row))), field_map)
        for row in rows_result.fetchall()
    ]

    # 字段过滤
    fields = db.query(TableField).filter(TableField.table_id == bt.id).all()
    if fields and policy.field_access_mode != "all":
        visible_cols = compute_visible_columns(all_columns, fields, policy)
        rows = [{k: v for k, v in row.items() if k in visible_cols} for row in rows]
        all_columns = visible_cols

    # 脱敏（L3 走脱敏，L4 不脱敏）
    if policy.masking_rules and not caps["can_see_raw"]:
        rows = apply_field_masking(rows, policy.masking_rules, fields)

    return {
        "total": count_result,
        "page": page,
        "page_size": page_size,
        "columns": all_columns,
        "rows": rows,
    }


def _list_rows_legacy(
    db: Session,
    bt: BusinessTable,
    user: User,
    page: int,
    page_size: int,
    offset: int,
    view_id: int | None,
    is_admin: bool,
):
    """旧权限逻辑（向后兼容）。"""
    context = _build_legacy_access_context(db, bt, user, is_admin)
    if context is None:
        return _empty_rows_result(page, page_size)
    base_sql = context["base_sql"]
    rules = context["rules"]
    ownership = context["ownership"]

    # ── Apply view config (filters + sorts) if view_id provided ──
    if view_id:
        from app.models.business import TableView
        view = db.get(TableView, view_id)
        if view and view.table_id == bt.id:
            base_sql = _apply_view_config(base_sql, view.config or {})

    count_result = db.execute(text(f"SELECT COUNT(*) FROM ({base_sql}) AS _t")).scalar()

    rows_result = db.execute(
        text(base_sql + " LIMIT :limit OFFSET :offset"),
        {"limit": page_size, "offset": offset},
    )
    all_columns = list(rows_result.keys())
    field_map = _get_table_field_map(db, bt)
    rows = [
        normalize_row_for_response(_serialize_row(dict(zip(all_columns, row))), field_map)
        for row in rows_result.fetchall()
    ]
    all_columns, rows = _apply_legacy_field_visibility(rows, all_columns, rules, ownership, user, is_admin)

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


# ── 智能采样接口 ────────────────────────────────────────────────────────────────

_ENUM_FIELD_TYPES = {"single_select", "multi_select", "boolean"}


def _is_enum_field(f: TableField) -> bool:
    """字段是否为结构化枚举类型，需要按值取样。"""
    if getattr(f, "is_enum", False):
        return True
    if (f.field_type or "").lower() in _ENUM_FIELD_TYPES:
        return True
    return False


@router.get("/{table_name}/sample")
def sample_rows(
    table_name: str,
    max_rows: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """智能采样：对结构化枚举字段，每种枚举值至少返回一行；其余空位用最新数据补足。

    - total: 表的总行数（不受采样影响，便于前端展示数据量）
    - rows: 采样后的行
    - sample_strategy: 描述采样逻辑的元数据
    """
    bt = _get_registered_table(db, table_name)
    require_table_view_access(db, bt, user)
    is_admin = _is_admin(user)
    has_new_policy = _has_new_policy(db, bt)
    if should_use_asset_safe_default(user, bt, has_new_policy=has_new_policy):
        return _empty_sample_result()

    base_sql = f"SELECT * FROM {qi(table_name, '表名')}"
    sql_params: dict = {}
    masking_rules = None
    fields_for_mask: list[TableField] = []
    legacy_rules: dict = {}
    legacy_ownership: DataOwnership | None = None

    if has_new_policy and not is_admin:
        groups = resolve_user_role_groups(db, bt.id, user, skill_id=None)
        policy = resolve_effective_policy(db, bt.id, [g.id for g in groups], None)
        if policy.denied:
            return _empty_sample_result()
        caps = check_disclosure_capability(policy.disclosure_level)
        if not caps["can_see_rows"]:
            return _empty_sample_result()
        row_filter, row_params = build_row_filter_sql(policy, user, table_name)
        if row_filter:
            base_sql += f" WHERE ({row_filter})"
            sql_params.update(row_params)
        fields_for_mask = db.query(TableField).filter(TableField.table_id == bt.id).all()
        _policy_for_visible = policy
        if policy.masking_rules and not caps["can_see_raw"]:
            masking_rules = policy.masking_rules
    else:
        context = _build_legacy_access_context(db, bt, user, is_admin)
        if context is None:
            return _empty_sample_result()
        base_sql = context["base_sql"]
        legacy_rules = context["rules"]
        legacy_ownership = context["ownership"]
        _policy_for_visible = None

    # ── 1. 总行数 ──
    total = db.execute(text(f"SELECT COUNT(*) FROM ({base_sql}) AS _t"), sql_params).scalar() or 0

    # ── 2. 取一行用于发现列名 ──
    probe = db.execute(text(base_sql + " LIMIT 1"), sql_params)
    all_columns = list(probe.keys())
    probe.fetchall()  # drain

    # ── 3. 找出枚举字段（必须是物理列） ──
    fields = db.query(TableField).filter(TableField.table_id == bt.id).all()
    field_by_col: dict[str, TableField] = {}
    for f in fields:
        col = f.physical_column_name or f.field_name
        if col in all_columns:
            field_by_col[col] = f
    enum_field_cols = [col for col, f in field_by_col.items() if _is_enum_field(f)]

    sampled_row_ids: set = set()
    sampled_rows: list[dict] = []
    enum_strategy: list[dict] = []
    field_map = _get_table_field_map(db, bt)

    def _add_row(row_dict: dict):
        rid = row_dict.get("id")
        key = rid if rid is not None else id(row_dict)
        if key in sampled_row_ids:
            return False
        sampled_row_ids.add(key)
        sampled_rows.append(row_dict)
        return True

    # ── 4. 对每个枚举字段，取每种值的代表行 ──
    for col in enum_field_cols:
        if len(sampled_rows) >= max_rows:
            break
        # 获取该列的所有 distinct 值（排除 NULL，用样本表读）
        try:
            distinct_vals = db.execute(
                text(f"SELECT DISTINCT {qi(col, '列名')} AS v FROM ({base_sql}) AS _t WHERE {qi(col, '列名')} IS NOT NULL LIMIT 200"),
                sql_params,
            ).fetchall()
        except Exception:
            continue
        covered_vals = []
        for (val,) in distinct_vals:
            if len(sampled_rows) >= max_rows:
                break
            row_q = db.execute(
                text(f"SELECT * FROM ({base_sql}) AS _t WHERE {qi(col, '列名')} = :v LIMIT 1"),
                {**sql_params, "v": val},
            )
            row = row_q.fetchone()
            if row is None:
                continue
            row_dict = normalize_row_for_response(_serialize_row(dict(zip(all_columns, row))), field_map)
            if _add_row(row_dict):
                covered_vals.append(_serialize_value(val))
        enum_strategy.append({"field": col, "covered_values": covered_vals})

    # ── 5. 用最新数据补足到 max_rows ──
    if len(sampled_rows) < max_rows:
        remaining = max_rows - len(sampled_rows)
        # 优先按 id DESC（如有 id 列），否则不排序
        order_clause = " ORDER BY id DESC" if "id" in all_columns else ""
        fill_q = db.execute(
            text(base_sql + order_clause + " LIMIT :limit"),
            {**sql_params, "limit": remaining + len(sampled_rows)},  # 多取一些以便去重
        )
        for row in fill_q.fetchall():
            if len(sampled_rows) >= max_rows:
                break
            row_dict = normalize_row_for_response(_serialize_row(dict(zip(all_columns, row))), field_map)
            _add_row(row_dict)

    # ── 6. 字段过滤 + 脱敏（新策略路径） ──
    if has_new_policy and not is_admin and _policy_for_visible is not None and fields_for_mask:
        if _policy_for_visible.field_access_mode != "all":
            visible_cols = compute_visible_columns(all_columns, fields_for_mask, _policy_for_visible)
            sampled_rows = [{k: v for k, v in r.items() if k in visible_cols} for r in sampled_rows]
            all_columns = visible_cols
        if masking_rules:
            sampled_rows = apply_field_masking(sampled_rows, masking_rules, fields_for_mask)
    else:
        all_columns, sampled_rows = _apply_legacy_field_visibility(
            sampled_rows,
            all_columns,
            legacy_rules,
            legacy_ownership,
            user,
            is_admin,
        )

    return {
        "total": total,
        "columns": all_columns,
        "rows": sampled_rows,
        "sample_strategy": {
            "enum_fields": enum_strategy,
            "sampled": len(sampled_rows),
            "max_rows": max_rows,
        },
    }


@router.post("/{table_name}/rows")
def create_row(
    table_name: str,
    req: RowCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = _get_registered_table(db, table_name)
    _check_write_permission(db, bt, user)
    field_map = _get_table_field_map(db, bt)

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

    data = normalize_row_payload({k: v for k, v in req.data.items()}, field_map)
    cols = ", ".join(f"`{k}`" for k in data.keys())
    placeholders = ", ".join(f":{k}" for k in data.keys())
    sql = text(f"INSERT INTO {qi(table_name, '表名')} ({cols}) VALUES ({placeholders})")

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
    _check_write_permission(db, bt, user)
    field_map = _get_table_field_map(db, bt)

    # Get old values for audit
    old_row_result = db.execute(
        text(f"SELECT * FROM {qi(table_name, '表名')} WHERE id = :id"),
        {"id": row_id},
    )
    old_row = old_row_result.mappings().first()
    if not old_row:
        raise HTTPException(404, "Row not found")

    old_values = dict(old_row)
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

    data = normalize_row_payload({k: v for k, v in req.data.items() if k != "id"}, field_map)
    set_clause = ", ".join(f"`{k}` = :{k}" for k in data.keys())
    sql = text(f"UPDATE {qi(table_name, '表名')} SET {set_clause} WHERE id = :__id")
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
    _check_write_permission(db, bt, user)

    old_row_result = db.execute(
        text(f"SELECT * FROM {qi(table_name, '表名')} WHERE id = :id"),
        {"id": row_id},
    )
    old_row = old_row_result.mappings().first()
    if not old_row:
        raise HTTPException(404, "Row not found")

    old_values = dict(old_row)
    _check_row_owner(bt, old_values, user)

    try:
        db.execute(text(f"DELETE FROM {qi(table_name, '表名')} WHERE id = :id"), {"id": row_id})
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


# ── 导出接口 ────────────────────────────────────────────────────────────────────

@router.get("/{table_name}/export")
def export_rows(
    table_name: str,
    format: str = Query("csv", pattern="^(csv|excel|json)$"),
    max_rows: int = Query(10000, ge=1, le=100000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导出表数据为 CSV / Excel / JSON 格式。复用 list_rows 的权限逻辑。"""
    bt = _get_registered_table(db, table_name)
    require_table_view_access(db, bt, user)
    result = _read_rows_for_access(db, bt, user, 1, max_rows, 0, None)

    columns = result["columns"]
    rows = result["rows"]

    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) if v is not None else "" for k, v in row.items()})
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{table_name}.csv"'},
        )

    if format == "json":
        content = _json.dumps(rows, ensure_ascii=False, default=str, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{table_name}.json"'},
        )

    # Excel
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(500, "服务端缺少 openpyxl 依赖，无法导出 Excel")

    wb = Workbook()
    ws = wb.active
    ws.title = table_name[:31]
    ws.append(columns)
    for row in rows:
        ws.append([row.get(c) for c in columns])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{table_name}.xlsx"'},
    )
