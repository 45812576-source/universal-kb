"""Data table write builtin tool.

Input params:
{
  "table_name": "creative_topics",
  "rows": [
    {"topic": "美白精华成分科普", "angle": "功效", "status": "pending", "assigned_to": "小王"},
    {"topic": "早C晚A搭配推荐", "angle": "场景", "status": "pending", "assigned_to": "小李"}
  ]
}

Output: {"ok": true, "inserted_count": 2, "table_name": "creative_topics"}
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.services.data_engine import data_engine

logger = logging.getLogger(__name__)


async def execute(params: dict, db: Session, user_id: int | None = None) -> dict:
    """Write rows into a registered business table."""
    table_name = params.get("table_name", "")
    rows = params.get("rows", [])

    if not table_name:
        return {"ok": False, "error": "table_name 不能为空"}
    if not rows:
        return {"ok": False, "error": "rows 列表为空"}

    # Validate table is registered
    bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
    if not bt:
        return {"ok": False, "error": f"表 '{table_name}' 未在业务表注册表中"}

    # Get allowed columns via INFORMATION_SCHEMA
    columns_info = data_engine._get_columns(db, table_name)
    allowed_cols = {c["name"] for c in columns_info}

    inserted_count = 0
    errors = []

    for i, row in enumerate(rows):
        # Validate fields
        invalid_fields = [k for k in row if k not in allowed_cols]
        if invalid_fields:
            errors.append(f"第{i+1}行包含非法字段: {invalid_fields}")
            continue

        if not row:
            continue

        # Build INSERT SQL
        cols = list(row.keys())
        col_list = ", ".join(f"`{c}`" for c in cols)
        val_list = ", ".join(_escape_value(row[c]) for c in cols)
        sql = f"INSERT INTO `{table_name}` ({col_list}) VALUES ({val_list})"

        # Safety check
        ok, reason = data_engine.validate_sql(sql, "write", [table_name])
        if not ok:
            errors.append(f"第{i+1}行SQL校验失败: {reason}")
            continue

        result = await data_engine.execute_sql(
            db=db,
            sql=sql,
            operation="write",
            user_id=user_id,
            table_name=table_name,
        )
        if result.get("ok"):
            inserted_count += 1
        else:
            errors.append(f"第{i+1}行写入失败: {result.get('error', '')}")

    response: dict = {
        "ok": inserted_count > 0 or not errors,
        "inserted_count": inserted_count,
        "table_name": table_name,
    }
    if errors:
        response["errors"] = errors
    return response


def _escape_value(val) -> str:
    """Escape a Python value for safe SQL insertion (basic escaping)."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    # String: escape single quotes
    escaped = str(val).replace("'", "''").replace("\\", "\\\\")
    return f"'{escaped}'"
