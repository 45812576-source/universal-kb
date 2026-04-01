"""Data table read builtin tool.

Input params:
{
  "table_name": "creative_topics",
  "filters": [{"field": "status", "op": "eq", "value": "pending"}],
  "columns": ["topic", "status"],   # optional; omit for all visible columns
  "limit": 50
}

Output: {"ok": true, "rows": [...], "columns": [...], "total": 10}
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.services.data_engine import data_engine

logger = logging.getLogger(__name__)

_OP_MAP = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "contains": "LIKE", "starts": "LIKE", "ends": "LIKE",
}


async def execute(params: dict, db: Session, user_id: int | None = None) -> dict:
    """Read rows from a registered business table with optional filters."""
    table_name = params.get("table_name", "")
    filters = params.get("filters") or []
    columns = params.get("columns") or []
    limit = min(int(params.get("limit", 50)), 500)

    if not table_name:
        return {"ok": False, "error": "table_name 不能为空"}

    # Validate table is registered — support table_name and display_name lookup
    bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
    if not bt:
        # Fallback: try matching by display_name
        bt = db.query(BusinessTable).filter(BusinessTable.display_name == table_name).first()
    if not bt:
        # Check if it might be an uploaded file that hasn't been imported as a business table
        from pathlib import Path
        workspace_files = list(Path("workspace").glob(f"**/{table_name}*")) if Path("workspace").exists() else []
        if workspace_files:
            return {
                "ok": False,
                "error": f"文件 '{table_name}' 已上传到工作区，但尚未导入为业务数据表。请先在数据管理中将文件导入为数据表后再读取。",
            }
        # List available tables to help the user
        available = db.query(BusinessTable.table_name, BusinessTable.display_name).limit(20).all()
        hint = "、".join(f"{t.display_name}({t.table_name})" for t in available) if available else "暂无已注册表"
        return {"ok": False, "error": f"表 '{table_name}' 未在业务表注册表中。可用的表：{hint}"}

    # Use the actual table_name (in case we matched by display_name)
    table_name = bt.table_name

    # Get allowed column names
    columns_info = data_engine._get_columns(db, table_name)
    allowed_cols = {c["name"] for c in columns_info}

    # Validate requested columns
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

    # Build WHERE
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

    # Validate SQL safety
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
        }
    except Exception as e:
        logger.error(f"data_table_reader failed: {e}")
        return {"ok": False, "error": str(e)}
