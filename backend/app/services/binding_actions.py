"""Skill Studio binding action resolution and execution."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.business import BusinessTable, SkillDataQuery, SkillTableBinding
from app.models.skill import Skill
from app.models.tool import SkillTool, ToolRegistry
from app.models.user import Role, User


@dataclass
class BindingCandidate:
    action: str
    target_kind: str
    target_id: int | None
    target_name: str
    display_name: str
    confidence: float
    ambiguous: bool = False
    alternatives: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "display_name": self.display_name,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "alternatives": self.alternatives or [],
        }


def assert_skill_write_access(skill: Skill, user: User) -> None:
    if user.role == Role.SUPER_ADMIN:
        return
    if skill.created_by == user.id:
        return
    if user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id:
        return
    raise HTTPException(403, "无权修改此 Skill 的绑定")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _intent(text: str) -> tuple[str | None, str | None]:
    normalized = _normalize(text)
    if not normalized:
        return None, None
    verb = "unbind" if any(k in normalized for k in ("解绑", "取消绑定", "移除", "删掉", "不要用", "unbound", "unbind")) else None
    if verb is None and any(k in normalized for k in ("绑定", "挂载", "接入", "使用", "加上", "bind")):
        verb = "bind"
    if verb is None:
        return None, None
    target = None
    if any(k in normalized for k in ("数据表", "表格", "业务表", "资产", "table", "data")):
        target = "table"
    if any(k in normalized for k in ("工具", "tool", "api", "函数")):
        target = "tool"
    return verb, target


def _score_asset(query: str, name: str, display_name: str) -> float:
    q = _normalize(query)
    n = _normalize(name)
    d = _normalize(display_name)
    if not q:
        return 0
    if q in (n, d):
        return 1.0
    if n and n in q:
        return 0.92
    if d and d in q:
        return 0.9
    if q in n or q in d:
        return 0.78
    tokens = [part for part in re.split(r"[_\-./:：,，;；\s]+", query) if part]
    if tokens and any(_normalize(part) in n or _normalize(part) in d for part in tokens):
        return 0.62
    return 0


def _bound_tool_ids(db: Session, skill_id: int) -> set[int]:
    return {row.tool_id for row in db.query(SkillTool).filter(SkillTool.skill_id == skill_id).all()}


def _bound_table_names(db: Session, skill: Skill) -> set[str]:
    declared = {row.table_name for row in db.query(SkillDataQuery).filter(SkillDataQuery.skill_id == skill.id).all()}
    quick = {str(q.get("table_name")) for q in (skill.data_queries or []) if q.get("table_name")}
    bound_ids = {row.table_id for row in db.query(SkillTableBinding).filter(SkillTableBinding.skill_id == skill.id).all()}
    if bound_ids:
        declared.update(
            table.table_name
            for table in db.query(BusinessTable).filter(BusinessTable.id.in_(bound_ids)).all()
            if table.table_name
        )
    return declared | quick


def _candidate_from_matches(action: str, target_kind: str, matches: list[tuple[float, Any]]) -> BindingCandidate | None:
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score, best = matches[0]
    if best_score <= 0:
        return None
    alternatives = [
        {
            "id": item.id,
            "name": getattr(item, "name", None) or getattr(item, "table_name", ""),
            "display_name": item.display_name,
            "confidence": round(score, 2),
        }
        for score, item in matches[:5]
        if score >= max(0.45, best_score - 0.12)
    ]
    ambiguous = len(alternatives) > 1 and alternatives[1]["confidence"] >= round(best_score - 0.08, 2)
    return BindingCandidate(
        action=action,
        target_kind=target_kind,
        target_id=best.id,
        target_name=getattr(best, "name", None) or getattr(best, "table_name", ""),
        display_name=best.display_name,
        confidence=round(best_score, 2),
        ambiguous=ambiguous,
        alternatives=alternatives,
    )


def resolve_binding_actions(db: Session, skill_id: int, user: User, text: str) -> list[dict[str, Any]]:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    assert_skill_write_access(skill, user)

    verb, target = _intent(text)
    if not verb or not target:
        return []

    if target == "tool":
        action = "bind_tool" if verb == "bind" else "unbind_tool"
        bound_ids = _bound_tool_ids(db, skill_id)
        query = db.query(ToolRegistry)
        if verb == "bind":
            query = query.filter(ToolRegistry.is_active == True, ToolRegistry.status != "archived")
        elif bound_ids:
            query = query.filter(ToolRegistry.id.in_(bound_ids))
        else:
            return []
        matches = [
            (_score_asset(text, tool.name, tool.display_name), tool)
            for tool in query.limit(200).all()
        ]
        candidate = _candidate_from_matches(action, "tool", matches)
        return [candidate.to_dict()] if candidate else []

    action = "bind_table" if verb == "bind" else "unbind_table"
    bound_names = _bound_table_names(db, skill)
    query = db.query(BusinessTable)
    if verb == "bind":
        query = query.filter(BusinessTable.is_archived == False, BusinessTable.publish_status == "published")
    elif bound_names:
        query = query.filter(BusinessTable.table_name.in_(bound_names))
    else:
        return []
    matches = [
        (_score_asset(text, table.table_name, table.display_name), table)
        for table in query.limit(200).all()
    ]
    candidate = _candidate_from_matches(action, "table", matches)
    return [candidate.to_dict()] if candidate else []


def _bind_tool(db: Session, skill: Skill, target_id: int) -> dict[str, Any]:
    tool = db.get(ToolRegistry, target_id)
    if not tool:
        raise HTTPException(404, "Tool not found")
    existing = db.query(SkillTool).filter(SkillTool.skill_id == skill.id, SkillTool.tool_id == tool.id).first()
    if existing:
        return {"ok": True, "action": "bind_tool", "changed": False, "target": tool.display_name}
    db.add(SkillTool(skill_id=skill.id, tool_id=tool.id))
    db.commit()
    return {"ok": True, "action": "bind_tool", "changed": True, "target": tool.display_name}


def _unbind_tool(db: Session, skill: Skill, target_id: int) -> dict[str, Any]:
    tool = db.get(ToolRegistry, target_id)
    if not tool:
        raise HTTPException(404, "Tool not found")
    row = db.query(SkillTool).filter(SkillTool.skill_id == skill.id, SkillTool.tool_id == tool.id).first()
    if row:
        db.delete(row)
        db.commit()
        return {"ok": True, "action": "unbind_tool", "changed": True, "target": tool.display_name}
    return {"ok": True, "action": "unbind_tool", "changed": False, "target": tool.display_name}


def _sync_quick_queries(skill: Skill, table_name: str, display_name: str, *, add: bool) -> None:
    queries = [q for q in (skill.data_queries or []) if q.get("table_name") != table_name]
    if add:
        queries.append({
            "query_name": f"read_{table_name}",
            "query_type": "read",
            "table_name": table_name,
            "description": display_name or table_name,
        })
    skill.data_queries = queries


def _bind_table(db: Session, skill: Skill, target_id: int, user: User) -> dict[str, Any]:
    table = db.get(BusinessTable, target_id)
    if not table:
        raise HTTPException(404, "Table not found")
    changed = False
    existing_query = db.query(SkillDataQuery).filter(
        SkillDataQuery.skill_id == skill.id,
        SkillDataQuery.table_name == table.table_name,
    ).first()
    if not existing_query:
        db.add(SkillDataQuery(
            skill_id=skill.id,
            query_name=f"read_{table.table_name}",
            query_type="read",
            table_name=table.table_name,
            description=table.display_name or table.table_name,
        ))
        changed = True
    existing_binding = db.query(SkillTableBinding).filter(
        SkillTableBinding.skill_id == skill.id,
        SkillTableBinding.table_id == table.id,
    ).first()
    if not existing_binding:
        db.add(SkillTableBinding(
            skill_id=skill.id,
            table_id=table.id,
            binding_type="runtime_read",
            alias=table.display_name or table.table_name,
            description="来自 Skill Studio 绑定动作",
            created_by=user.id,
        ))
        changed = True
    _sync_quick_queries(skill, table.table_name, table.display_name, add=True)
    db.commit()
    return {"ok": True, "action": "bind_table", "changed": changed, "target": table.display_name or table.table_name}


def _unbind_table(db: Session, skill: Skill, target_id: int) -> dict[str, Any]:
    table = db.get(BusinessTable, target_id)
    if not table:
        raise HTTPException(404, "Table not found")
    changed = False
    deleted = db.query(SkillDataQuery).filter(
        SkillDataQuery.skill_id == skill.id,
        SkillDataQuery.table_name == table.table_name,
    ).delete(synchronize_session=False)
    changed = changed or deleted > 0
    deleted = db.query(SkillTableBinding).filter(
        SkillTableBinding.skill_id == skill.id,
        SkillTableBinding.table_id == table.id,
    ).delete(synchronize_session=False)
    changed = changed or deleted > 0
    _sync_quick_queries(skill, table.table_name, table.display_name, add=False)
    db.commit()
    return {"ok": True, "action": "unbind_table", "changed": changed, "target": table.display_name or table.table_name}


def execute_binding_action(db: Session, skill_id: int, user: User, action: str, target_id: int) -> dict[str, Any]:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    assert_skill_write_access(skill, user)
    if action == "bind_tool":
        return _bind_tool(db, skill, target_id)
    if action == "unbind_tool":
        return _unbind_tool(db, skill, target_id)
    if action == "bind_table":
        return _bind_table(db, skill, target_id, user)
    if action == "unbind_table":
        return _unbind_table(db, skill, target_id)
    raise HTTPException(400, "Unsupported binding action")
