"""Handoff 引擎 — Agent 上下游 Schema 解析与 Payload 提取"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.permission import (
    HandoffSchemaCache,
    HandoffTemplate,
    SkillAgentConnection,
    SkillPolicy,
    ConnectionDirection,
)


class HandoffEngine:

    # ── 白名单校验 ────────────────────────────────────────────────────────────

    def validate_connection(
        self,
        upstream_skill_id: int,
        downstream_skill_id: int,
        db: Session,
    ) -> bool:
        """检查 upstream → downstream 连接是否在白名单中。
        若上游 Skill 没有 policy，则默认允许（宽松策略）。
        """
        policy = (
            db.query(SkillPolicy)
            .filter(SkillPolicy.skill_id == upstream_skill_id)
            .first()
        )
        if not policy:
            return True

        connection = (
            db.query(SkillAgentConnection)
            .filter(
                SkillAgentConnection.skill_policy_id == policy.id,
                SkillAgentConnection.direction == ConnectionDirection.DOWNSTREAM,
                SkillAgentConnection.connected_skill_id == downstream_skill_id,
            )
            .first()
        )
        return connection is not None

    # ── Schema 解析 ───────────────────────────────────────────────────────────

    def _make_cache_key(
        self,
        upstream_skill_id: int,
        downstream_skill_id: int,
        task_type_hash: str | None,
    ) -> str:
        parts = f"{upstream_skill_id}:{downstream_skill_id}:{task_type_hash or ''}"
        return hashlib.md5(parts.encode()).hexdigest()

    def resolve_schema(
        self,
        upstream_skill_id: int,
        downstream_skill_id: int,
        task_type: str | None,
        db: Session,
    ) -> dict:
        """解析 Handoff Schema：模板 → 缓存 → 返回空骨架
        优先级：静态模板 > 动态缓存 > 空骨架（需调用方动态生成）
        """
        # 1. 查静态模板
        template = (
            db.query(HandoffTemplate)
            .filter(
                HandoffTemplate.upstream_skill_id == upstream_skill_id,
                HandoffTemplate.downstream_skill_id == downstream_skill_id,
            )
            .first()
        )
        if template:
            return {
                "source": "template",
                "template_id": template.id,
                "schema_fields": template.schema_fields or [],
                "excluded_fields": template.excluded_fields or [],
            }

        # 2. 查动态缓存
        task_type_hash = (
            hashlib.md5(task_type.encode()).hexdigest()[:8] if task_type else None
        )
        cache_key = self._make_cache_key(upstream_skill_id, downstream_skill_id, task_type_hash)
        cache = (
            db.query(HandoffSchemaCache)
            .filter(
                HandoffSchemaCache.cache_key == cache_key,
                HandoffSchemaCache.expires_at > datetime.datetime.utcnow(),
            )
            .first()
        )
        if cache:
            cache.hit_count += 1
            db.commit()
            return {
                "source": "cache",
                "cache_id": cache.id,
                "schema": cache.schema_json,
            }

        # 3. 返回空骨架（调用方负责动态生成并写入缓存）
        return {
            "source": "dynamic",
            "cache_key": cache_key,
            "task_type_hash": task_type_hash,
            "schema": {},
        }

    def save_schema_cache(
        self,
        cache_key: str,
        upstream_skill_id: int,
        downstream_skill_id: int,
        task_type_hash: str | None,
        schema_json: dict,
        db: Session,
        ttl_days: int = 7,
    ) -> HandoffSchemaCache:
        """将动态生成的 Schema 写入缓存"""
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=ttl_days)
        existing = (
            db.query(HandoffSchemaCache)
            .filter(HandoffSchemaCache.cache_key == cache_key)
            .first()
        )
        if existing:
            existing.schema_json = schema_json
            existing.expires_at = expires_at
            existing.hit_count += 1
            db.commit()
            db.refresh(existing)
            return existing

        cache = HandoffSchemaCache(
            cache_key=cache_key,
            upstream_skill_id=upstream_skill_id,
            downstream_skill_id=downstream_skill_id,
            task_type_hash=task_type_hash,
            schema_json=schema_json,
            expires_at=expires_at,
        )
        db.add(cache)
        db.commit()
        db.refresh(cache)
        return cache

    # ── Payload 提取（代码层映射，不过 LLM） ──────────────────────────────────

    def extract_payload(self, agent_output: dict, schema: dict) -> dict:
        """按 schema 定义从 agent 输出中提取 Handoff payload。
        schema 格式示例：
          {"fields": ["revenue", "company_name"], "mapping": {"rev": "revenue"}}
        """
        fields: list[str] = schema.get("schema_fields") or schema.get("fields") or []
        excluded: list[str] = schema.get("excluded_fields") or []
        mapping: dict[str, str] = schema.get("mapping") or {}

        payload: dict[str, Any] = {}

        # 应用字段映射
        normalized = dict(agent_output)
        for src, dst in mapping.items():
            if src in normalized:
                normalized[dst] = normalized.pop(src)

        if fields:
            for f in fields:
                if f in excluded:
                    continue
                if f in normalized:
                    payload[f] = normalized[f]
        else:
            # 未指定字段时取全部（排除 excluded）
            for k, v in normalized.items():
                if k not in excluded:
                    payload[k] = v

        return payload

    # ── Payload 合规校验 ──────────────────────────────────────────────────────

    def validate_payload(self, payload: dict, policy: dict | None) -> bool:
        """合规校验：检查必要字段存在、禁止字段不存在。
        policy 格式示例：
          {"required_fields": ["company_name"], "forbidden_fields": ["password"]}
        """
        if not policy:
            return True

        required = policy.get("required_fields") or []
        forbidden = policy.get("forbidden_fields") or []

        for field in required:
            if field not in payload:
                return False

        for field in forbidden:
            if field in payload:
                return False

        return True


handoff_engine = HandoffEngine()
