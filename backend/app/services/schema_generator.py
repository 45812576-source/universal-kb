"""Schema Generator: bidirectional natural language ↔ DDL+Skill generation."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.models.skill import Skill, SkillStatus, SkillVersion, SkillMode
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_FROM_DESC_SYSTEM = """你是数据库架构设计助手。根据业务场景描述，生成 MySQL 表结构和对应的 Skill 定义。

## 业务描述
{description}

## 要求
- 生成合理的 MySQL DDL（CREATE TABLE 语句）
- 表名使用小写下划线格式
- 必须包含 id（INT AUTO_INCREMENT PRIMARY KEY）和 created_at（DATETIME）字段
- 根据业务字段推断合理的数据类型
- 生成对应的 Skill 定义，包含查询/写入能力
- 输出严格 JSON，不要 markdown 代码块：
{{
  "table_name": "xxx_yyy",
  "display_name": "业务显示名",
  "description": "表的用途说明",
  "ddl_sql": "CREATE TABLE xxx_yyy (...);",
  "validation_rules": {{"字段名": {{"max": 100}}}},
  "workflow": {{"stages": [], "field": "status"}},
  "skill": {{
    "name": "Skill名称",
    "description": "Skill描述",
    "system_prompt": "完整的 system prompt",
    "variables": [],
    "knowledge_tags": [],
    "data_queries": [
      {{"query_name": "查询所有记录", "query_type": "read", "table_name": "xxx_yyy", "description": "..."}}
    ]
  }}
}}"""

_FROM_TABLE_SYSTEM = """你是数据库分析助手。根据提供的表结构，生成对应的 Skill 定义。

## 表名
{table_name}

## 表结构
{columns}

## 要求
- 分析字段语义，生成合适的 Skill
- 输出严格 JSON，不要 markdown 代码块：
{{
  "skill": {{
    "name": "Skill名称",
    "description": "Skill描述",
    "system_prompt": "完整的 system prompt",
    "variables": [],
    "knowledge_tags": [],
    "data_queries": [
      {{"query_name": "查询所有记录", "query_type": "read", "table_name": "{table_name}", "description": "..."}}
    ]
  }}
}}"""


class SchemaGenerator:

    def _strip_code_block(self, text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        return cleaned

    async def generate_from_description(
        self,
        description: str,
        model_config: dict,
    ) -> dict:
        """Direction A: natural language → DDL + Skill definition (preview)."""
        system = _FROM_DESC_SYSTEM.format(description=description)
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": description},
            ],
            temperature=0.2,
            max_tokens=3000,
        )
        return json.loads(self._strip_code_block(result))

    async def generate_from_table(
        self,
        table_name: str,
        model_config: dict,
        db: Session,
    ) -> dict:
        """Direction B: existing table → Skill definition (preview)."""
        sql = text("""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
        """)
        rows = db.execute(sql, {"table_name": table_name}).fetchall()
        columns_str = "\n".join(
            f"- {r[0]} ({r[1]}, {'nullable' if r[2]=='YES' else 'not null'})"
            + (f" -- {r[4]}" if r[4] else "")
            for r in rows
        )
        system = _FROM_TABLE_SYSTEM.format(
            table_name=table_name,
            columns=columns_str,
        )
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"请为 {table_name} 表生成 Skill 定义"},
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        return json.loads(self._strip_code_block(result))

    def apply_schema(self, ddl_sql: str, db: Session) -> None:
        """Execute DDL to create table."""
        # Safety: only allow CREATE TABLE
        if not re.match(r"^\s*CREATE\s+TABLE", ddl_sql, re.IGNORECASE):
            raise ValueError("只允许执行 CREATE TABLE 语句")
        db.execute(text(ddl_sql))
        db.commit()

    def register_table(
        self,
        table_name: str,
        display_name: str,
        description: str,
        ddl_sql: str,
        validation_rules: dict,
        workflow: dict,
        owner_id: int,
        db: Session,
    ) -> BusinessTable:
        """Register a table in business_tables."""
        existing = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
        if existing:
            raise ValueError(f"表 '{table_name}' 已注册")
        bt = BusinessTable(
            table_name=table_name,
            display_name=display_name,
            description=description,
            ddl_sql=ddl_sql,
            validation_rules=validation_rules or {},
            workflow=workflow or {},
            owner_id=owner_id,
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        return bt

    def create_skill_from_def(
        self,
        skill_def: dict,
        table_name: str,
        user_id: int,
        db: Session,
    ) -> Skill:
        """Create a Skill from the generated skill definition."""
        skill = Skill(
            name=skill_def["name"],
            description=skill_def.get("description", ""),
            mode=SkillMode.STRUCTURED,
            status=SkillStatus.DRAFT,
            knowledge_tags=skill_def.get("knowledge_tags", []),
            auto_inject=False,
            data_queries=skill_def.get("data_queries", []),
            created_by=user_id,
        )
        db.add(skill)
        db.flush()

        version = SkillVersion(
            skill_id=skill.id,
            version=1,
            system_prompt=skill_def.get("system_prompt", ""),
            variables=skill_def.get("variables", []),
            created_by=user_id,
            change_note="由 Schema 自动生成",
        )
        db.add(version)
        db.commit()
        db.refresh(skill)
        return skill


schema_generator = SchemaGenerator()
