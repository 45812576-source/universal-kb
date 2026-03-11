"""Text-to-SQL engine: natural language → SQL → execute → format results."""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import AuditLog, BusinessTable, DataOwnership
from app.models.user import User
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

# SQL keywords that are never allowed
_BLOCKED_PATTERNS = re.compile(
    r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|REPLACE\s+INTO)\b",
    re.IGNORECASE,
)

_GENERATE_SQL_SYSTEM = """你是企业数据库查询助手。根据用户的自然语言请求，生成可执行的 MySQL SQL 语句。

## 可用表结构
{table_context}

## 约束规则
{validation_rules}

## 要求
- 只生成 SELECT / INSERT / UPDATE，不允许 DROP / TRUNCATE / ALTER / DELETE
- 只操作上面列出的业务表
- 输出严格 JSON，不要 markdown 代码块，格式：
{{
  "sql": "SELECT ... FROM ...",
  "operation": "read",
  "explanation": "查询说明"
}}
- operation 取值：read（查询）/ write（写入）
- INSERT/UPDATE 语句需要包含完整字段值"""

_INTENT_CLASSIFY_SYSTEM = """判断用户消息的操作意图。只返回 JSON，格式：
{"type": "data_query"} 表示查询/读取数据
{"type": "data_mutation"} 表示写入/修改数据
{"type": "computation"} 表示数学计算/公式计算/返点计算等精确计算需求
{"type": "ai_generate"} 表示要AI生成内容/分析"""


class DataEngine:

    def describe_tables(self, db: Session) -> list[dict]:
        """Get all registered business tables with their column info."""
        biz_tables = db.query(BusinessTable).all()
        result = []
        for bt in biz_tables:
            cols = self._get_columns(db, bt.table_name)
            result.append({
                "table_name": bt.table_name,
                "display_name": bt.display_name,
                "description": bt.description or "",
                "columns": cols,
                "validation_rules": bt.validation_rules or {},
                "workflow": bt.workflow or {},
            })
        return result

    def _get_columns(self, db: Session, table_name: str) -> list[dict]:
        """Query INFORMATION_SCHEMA for table columns."""
        # Extract database name from connection URL
        try:
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
        except Exception as e:
            logger.warning(f"Failed to get columns for {table_name}: {e}")
            return []

    def _build_table_context(self, tables: list[dict]) -> str:
        lines = []
        for t in tables:
            lines.append(f"### {t['table_name']} ({t['display_name']})")
            if t["description"]:
                lines.append(f"说明: {t['description']}")
            if t["columns"]:
                col_descs = ", ".join(
                    f"{c['name']} {c['type']}" + (f"({c['comment']})" if c["comment"] else "")
                    for c in t["columns"]
                )
                lines.append(f"字段: {col_descs}")
            lines.append("")
        return "\n".join(lines)

    def _build_validation_context(self, tables: list[dict]) -> str:
        rules = []
        for t in tables:
            for field, rule in (t.get("validation_rules") or {}).items():
                if "max" in rule:
                    rules.append(f"- {t['table_name']}.{field} 不能超过 {rule['max']}")
                if "min" in rule:
                    rules.append(f"- {t['table_name']}.{field} 不能低于 {rule['min']}")
                if "enum" in rule:
                    rules.append(f"- {t['table_name']}.{field} 只能是 {rule['enum']}")
        return "\n".join(rules) if rules else "无特殊约束"

    async def generate_sql(
        self,
        user_request: str,
        tables: list[dict],
        model_config: dict,
    ) -> dict:
        """Use LLM to generate SQL from natural language."""
        table_context = self._build_table_context(tables)
        validation_rules = self._build_validation_context(tables)

        system = _GENERATE_SQL_SYSTEM.format(
            table_context=table_context,
            validation_rules=validation_rules,
        )
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_request},
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        # Strip markdown code blocks if present
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        return json.loads(cleaned)

    def validate_sql(
        self,
        sql: str,
        operation: str,
        allowed_tables: list[str],
    ) -> tuple[bool, str]:
        """Validate SQL for safety. Returns (ok, reason)."""
        if _BLOCKED_PATTERNS.search(sql):
            return False, "SQL 包含禁止操作（DROP/TRUNCATE/ALTER 等）"

        if "DELETE" in sql.upper():
            return False, "DELETE 操作不被允许"

        # Check that only registered tables are referenced
        sql_upper = sql.upper()
        if allowed_tables:
            # Simple check: FROM/JOIN should reference known tables
            referenced = re.findall(r'(?:FROM|JOIN)\s+`?(\w+)`?', sql_upper)
            for tbl in referenced:
                if tbl.lower() not in [t.lower() for t in allowed_tables]:
                    return False, f"表 '{tbl}' 未在业务表注册表中"

        return True, ""

    def get_ownership_rule(self, db: Session, table_name: str) -> DataOwnership | None:
        """Fetch ownership rule for a table if configured."""
        return db.query(DataOwnership).filter(DataOwnership.table_name == table_name).first()

    def inject_read_permission(
        self,
        sql: str,
        ownership: DataOwnership,
        user: "User",
    ) -> str:
        """Inject WHERE clause for row-level read permission."""
        from app.models.user import Role
        if user.role in (Role.SUPER_ADMIN,):
            return sql  # super admin sees everything
        conditions = []
        if ownership.owner_field:
            conditions.append(f"`{ownership.owner_field}` = {user.id}")
        if ownership.department_field and user.department_id:
            conditions.append(f"`{ownership.department_field}` = {user.department_id}")
        if not conditions:
            return sql
        where_clause = " OR ".join(conditions)
        sql_upper = sql.upper().strip()
        if "WHERE" in sql_upper:
            return sql + f" AND ({where_clause})"
        else:
            return sql + f" WHERE ({where_clause})"

    async def execute_sql(
        self,
        db: Session,
        sql: str,
        operation: str,
        user_id: int | None,
        table_name: str = "",
        user: "User | None" = None,
    ) -> dict:
        """Execute SQL and record audit log for write operations."""
        try:
            if operation == "read":
                # Inject row-level permission if ownership rule exists
                if user and table_name:
                    ownership = self.get_ownership_rule(db, table_name)
                    if ownership:
                        sql = self.inject_read_permission(sql, ownership, user)
                result = db.execute(text(sql))
                columns = list(result.keys())
                rows = result.fetchall()
                return {
                    "ok": True,
                    "rows": [dict(zip(columns, row)) for row in rows],
                    "columns": columns,
                    "count": len(rows),
                }
            else:
                # Write operation: capture row count
                result = db.execute(text(sql))
                db.commit()

                # Record audit log
                log = AuditLog(
                    user_id=user_id,
                    table_name=table_name,
                    operation="INSERT" if "INSERT" in sql.upper() else "UPDATE",
                    sql_executed=sql,
                    new_values={"sql": sql},
                    created_at=datetime.datetime.utcnow(),
                )
                db.add(log)
                db.commit()

                return {
                    "ok": True,
                    "affected_rows": result.rowcount,
                }
        except Exception as e:
            db.rollback()
            logger.error(f"SQL execution failed: {e}")
            return {"ok": False, "error": str(e)}

    def format_results(self, rows: list[dict], columns: list[str]) -> str:
        """Format query results as readable text table."""
        if not rows:
            return "查询结果为空"

        # Build markdown table
        header = " | ".join(columns)
        separator = " | ".join(["---"] * len(columns))
        data_rows = []
        for row in rows[:50]:  # cap at 50 rows in text output
            vals = [str(row.get(c, "")) for c in columns]
            data_rows.append(" | ".join(vals))

        table = f"| {header} |\n| {separator} |\n"
        table += "\n".join(f"| {r} |" for r in data_rows)

        if len(rows) > 50:
            table += f"\n\n（共 {len(rows)} 条，仅显示前 50 条）"
        else:
            table += f"\n\n共 {len(rows)} 条记录"

        return table

    async def classify_intent(
        self,
        user_message: str,
        model_config: dict,
    ) -> dict:
        """Classify whether user wants data query, mutation, or AI generation."""
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": _INTENT_CLASSIFY_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=50,
        )
        try:
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            return json.loads(cleaned)
        except Exception:
            return {"type": "ai_generate"}


data_engine = DataEngine()
