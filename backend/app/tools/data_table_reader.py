"""Data table read builtin tool — 视图优先模式。

v4 §1.1: 普通用户只认视图资产，禁止裸 table_name 入口。
v4 §8.1: Agent 必须携带 view_id，否则后端拒绝。
v4 §8.2: view_id 无效直接报错，不回退整表。

Input params:
{
  "view_id": 5,                    # 必须（非admin用户）
  "view_name": "风控汇总",          # 配合 table_id/table_name 查视图
  "table_name": "creative_topics", # 仅 super_admin 可裸表读取且需 admin_raw_table=true
  "table_id": 1,
  "filters": [{"field": "status", "op": "eq", "value": "pending"}],
  "columns": ["topic", "status"],
  "limit": 50,
  "admin_raw_table": false         # 高级开关：仅 super_admin 可用
}

Output: {"ok": true, "rows": [...], "columns": [...], "total": 10, "table_id": 1, "view_id": 5}
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import BusinessTable, TableView
from app.services.data_engine import data_engine

logger = logging.getLogger(__name__)

_OP_MAP = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "contains": "LIKE", "starts": "LIKE", "ends": "LIKE",
}


async def execute(params: dict, db: Session, user_id: int | None = None) -> dict:
    """Read rows from a registered business table — view-first with permission enforcement.

    v4: 普通用户只能通过视图读取。整表直读仅 super_admin + 显式高级开关。
    """
    view_id = params.get("view_id")
    view_name = params.get("view_name", "")
    table_name = params.get("table_name", "")
    table_id = params.get("table_id")
    filters = params.get("filters") or []
    columns = params.get("columns") or []
    limit = min(int(params.get("limit", 50)), 500)
    admin_raw_table = params.get("admin_raw_table", False)

    # 获取用户对象
    user = None
    if user_id:
        from app.models.user import User
        user = db.get(User, user_id)

    from app.models.user import Role
    is_admin = user and user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)

    # ── 视图查找链路 ──
    view = None
    bt = None

    # 1. view_id 直接查
    if view_id:
        view = db.get(TableView, view_id)
        if not view:
            return {"ok": False, "error": f"视图 {view_id} 不存在", "error_code": "view_invalid"}
        # v4 §7.1: 检查 view_state
        view_state = getattr(view, "view_state", None) or "available"
        if view_state != "available":
            error_code = f"view_{view_state}"
            reason = getattr(view, "view_invalid_reason", None) or f"视图状态异常: {view_state}"
            return {"ok": False, "error": reason, "error_code": error_code}
        bt = db.get(BusinessTable, view.table_id)

    # 2. table_id + view_name
    if not view and table_id and view_name:
        bt = db.get(BusinessTable, table_id)
        if bt:
            view = db.query(TableView).filter(
                TableView.table_id == bt.id,
                TableView.name == view_name,
            ).first()

    # 3. table_name + view_name
    if not view and table_name and view_name:
        bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
        if not bt:
            bt = db.query(BusinessTable).filter(BusinessTable.display_name == table_name).first()
        if bt:
            view = db.query(TableView).filter(
                TableView.table_id == bt.id,
                TableView.name == view_name,
            ).first()
            if not view:
                avail_views = db.query(TableView.name).filter(
                    TableView.table_id == bt.id,
                    ).all()
                hints = "、".join(v.name for v in avail_views) if avail_views else "暂无视图"
                return {"ok": False, "error": f"表 '{bt.display_name}' 下未找到视图 '{view_name}'。可用视图：{hints}"}

    # 4. table_name（无 view_name）→ 尝试找默认视图
    if not view and (table_name or table_id):
        if not bt:
            if table_id:
                bt = db.get(BusinessTable, table_id)
            else:
                bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
                if not bt:
                    bt = db.query(BusinessTable).filter(BusinessTable.display_name == table_name).first()

        if bt:
            # 尝试找默认视图
            view = db.query(TableView).filter(
                TableView.table_id == bt.id,
                TableView.is_default == True,  # noqa: E712
            ).first()

            if not view:
                # 尝试找任一 explore 视图
                view = db.query(TableView).filter(
                    TableView.table_id == bt.id,
                    TableView.view_purpose == "explore",
                    ).first()

    # ── 有视图 → 走视图执行层 ──
    if view and bt:
        if not user:
            return {"ok": False, "error": "需要用户身份才能通过视图读取数据"}

        # v4 §7.3: 后端二次校验 view_state
        view_state = getattr(view, "view_state", None) or "available"
        if view_state != "available":
            error_code = f"view_{view_state}"
            reason = getattr(view, "view_invalid_reason", None) or f"视图状态异常: {view_state}"
            return {"ok": False, "error": reason, "error_code": error_code}

        from app.services.data_view_runtime import execute_view_read
        result = execute_view_read(
            db=db,
            view_id=view.id,
            user=user,
            filters=filters,
            columns=columns,
            limit=limit,
        )
        out = result.to_dict()
        out["table_id"] = bt.id
        out["view_id"] = view.id
        out["columns"] = [f["field_name"] for f in result.fields] if result.fields else []
        return out

    # ── 无视图 ──

    # 没找到表
    if not bt:
        if not table_name and not table_id:
            return {"ok": False, "error": "请提供 view_id 来读取数据。OpenCode 不接受无视图的数据读取请求。"}

        name_hint = table_name or str(table_id)
        # 检查是否是工作区文件
        from pathlib import Path
        workspace_files = list(Path("workspace").glob(f"**/{name_hint}*")) if Path("workspace").exists() else []
        if workspace_files:
            return {
                "ok": False,
                "error": f"文件 '{name_hint}' 已上传到工作区，但尚未导入为业务数据表。请先在数据管理中将文件导入为数据表后再读取。",
            }
        available = db.query(BusinessTable.table_name, BusinessTable.display_name).limit(20).all()
        hint = "、".join(f"{t.display_name}({t.table_name})" for t in available) if available else "暂无已注册表"
        return {"ok": False, "error": f"表 '{name_hint}' 未在业务表注册表中。可用的表：{hint}"}

    # v4 §1.1: 有表但无视图 — 非 admin 直接拒绝，不允许隐式 fallback
    if not is_admin:
        avail_views = db.query(TableView.name).filter(
            TableView.table_id == bt.id,
        ).all()
        if avail_views:
            hints = "、".join(v.name for v in avail_views)
            return {"ok": False, "error": f"请指定视图读取数据。表 '{bt.display_name}' 下可用视图：{hints}"}
        return {"ok": False, "error": f"表 '{bt.display_name}' 暂无可用视图，请联系管理员配置。"}

    # v4 §1.1: Admin 整表直读 — 必须显式 admin_raw_table=true 高级开关
    if not admin_raw_table:
        avail_views = db.query(TableView.name).filter(
            TableView.table_id == bt.id,
        ).all()
        if avail_views:
            hints = "、".join(v.name for v in avail_views)
            return {"ok": False, "error": f"请通过视图读取数据。表 '{bt.display_name}' 下可用视图：{hints}。如需整表直读请设置 admin_raw_table=true。"}
        return {"ok": False, "error": f"表 '{bt.display_name}' 暂无视图。如需管理员整表直读请设置 admin_raw_table=true。"}

    # Admin fallback with explicit switch: 裸表读取
    table_name = bt.table_name
    columns_info = data_engine._get_columns(db, table_name)
    allowed_cols = {c["name"] for c in columns_info}

    if columns:
        invalid = [c for c in columns if c not in allowed_cols]
        if invalid:
            return {"ok": False, "error": f"不存在的列: {invalid}"}
        select_clause = ", ".join(f"`{c}`" for c in columns)
    else:
        rules = bt.validation_rules or {}
        hidden = set(rules.get("hidden_fields") or [])
        select_clause = ", ".join(f"`{c}`" for c in [c["name"] for c in columns_info] if c not in hidden)
        columns = [c["name"] for c in columns_info if c["name"] not in hidden]

    sql = f"SELECT {select_clause} FROM `{table_name}`"

    where_parts = []
    for f in filters:
        field = f.get("field", "").strip()
        op = f.get("op", "eq")
        val = f.get("value", "")
        if not field or op not in _OP_MAP:
            continue
        if not re.match(r'^[\w\u4e00-\u9fff]+$', field):
            continue
        if field not in allowed_cols:
            continue
        sql_op = _OP_MAP[op]
        if op == "contains":
            val_escaped = str(val).replace("'", "''")
            where_parts.append(f"`{field}` LIKE '%{val_escaped}%'")
        elif op == "starts":
            val_escaped = str(val).replace("'", "''")
            where_parts.append(f"`{field}` LIKE '{val_escaped}%'")
        elif op == "ends":
            val_escaped = str(val).replace("'", "''")
            where_parts.append(f"`{field}` LIKE '%{val_escaped}'")
        elif isinstance(val, str):
            val_escaped = val.replace("'", "''")
            where_parts.append(f"`{field}` {sql_op} '{val_escaped}'")
        else:
            where_parts.append(f"`{field}` {sql_op} {val}")

    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    sql += f" LIMIT {limit}"

    ok, reason = data_engine.validate_sql(sql, "read", [table_name])
    if not ok:
        return {"ok": False, "error": f"SQL 校验失败: {reason}"}

    try:
        import datetime, decimal
        result = db.execute(text(sql))
        col_names = list(result.keys())
        raw_rows = result.fetchall()

        def _serialize(v):
            if isinstance(v, (datetime.datetime, datetime.date)):
                return v.isoformat()
            if isinstance(v, decimal.Decimal):
                return float(v)
            if isinstance(v, bytes):
                return v.decode("utf-8", errors="replace")
            return v

        rows = [dict(zip(col_names, [_serialize(c) for c in row])) for row in raw_rows]
        return {
            "ok": True,
            "rows": rows,
            "columns": col_names,
            "total": len(rows),
            "table_name": table_name,
            "table_id": bt.id,
            "warnings": ["管理员整表直读模式，非视图结果"],
        }
    except Exception as e:
        logger.error(f"data_table_reader failed: {e}")
        return {"ok": False, "error": str(e)}
