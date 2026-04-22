"""Studio Blueprint Service — Skill Blueprint 存储与治理卡片编译。

职责：
- 读写 memo_payload.blueprint
- compile-governance-cards：从 blueprint.package_plan 自动派生治理卡片
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill_memo import SkillMemo
from app.services.studio_workflow_protocol import (
    CardStatus,
    WorkspaceMode,
    _new_id,
    _now_iso,
)

logger = logging.getLogger(__name__)


def get_blueprint(
    db: Session,
    skill_id: int,
) -> dict[str, Any] | None:
    """读取当前 skill 的 blueprint。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = memo.memo_payload or {}
    return payload.get("blueprint")


def save_blueprint(
    db: Session,
    skill_id: int,
    *,
    blueprint: dict[str, Any],
    user_id: int | None = None,
) -> dict[str, Any]:
    """保存 / 更新 blueprint。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "memo_not_found"}

    from app.services.skill_memo_service import save_memo_payload_atomic

    payload = copy.deepcopy(memo.memo_payload or {})
    blueprint["updated_at"] = _now_iso()
    payload["blueprint"] = blueprint

    if user_id:
        memo.updated_by = user_id
    save_memo_payload_atomic(db, memo, payload)
    db.flush()

    return {"ok": True, "blueprint": blueprint}


def compile_governance_cards(
    db: Session,
    skill_id: int,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """从 blueprint.package_plan 自动派生治理执行卡片。

    对应设计文档 §10.4：Ready for Draft 确认后，
    根据 package_plan.required_files / suggested_files / planned_new_files
    自动生成 governance card 列表。
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "memo_not_found"}

    from app.services.skill_memo_service import save_memo_payload_atomic

    payload = copy.deepcopy(memo.memo_payload or {})
    blueprint = payload.get("blueprint")
    if not isinstance(blueprint, dict):
        return {"ok": False, "error": "blueprint_not_found"}

    package_plan = blueprint.get("package_plan") or {}
    recovery = payload.get("workflow_recovery") or {}
    cards = recovery.get("cards") or []
    workflow_state = recovery.get("workflow_state") or {}

    generated_cards: list[dict[str, Any]] = []

    # 从 required_files 生成卡片
    for filepath in package_plan.get("required_files") or []:
        card = _file_card(workflow_state, filepath, priority="high", origin="blueprint_compile")
        generated_cards.append(card)

    # 从 suggested_files 生成卡片
    for filepath in package_plan.get("suggested_files") or []:
        card = _file_card(workflow_state, filepath, priority="medium", origin="blueprint_compile")
        generated_cards.append(card)

    # 从 planned_new_files 生成卡片
    for filepath in package_plan.get("planned_new_files") or []:
        card = _file_card(workflow_state, filepath, priority="medium", origin="blueprint_compile")
        card["content"]["is_new_file"] = True
        generated_cards.append(card)

    # 从 linkage_rules 生成联动卡
    for rule in package_plan.get("linkage_rules") or []:
        if isinstance(rule, dict) and rule.get("description"):
            card_id = _new_id("gcard")
            generated_cards.append({
                "id": card_id,
                "workflow_id": workflow_state.get("workflow_id"),
                "source": "studio",
                "type": "governance",
                "card_type": "governance",
                "phase": "governance_execution",
                "title": rule.get("description", "联动规则")[:120],
                "summary": rule.get("description", ""),
                "status": CardStatus.QUEUED,
                "priority": "low",
                "workspace_mode": WorkspaceMode.FILE,
                "origin": "blueprint_compile",
                "target": {},
                "actions": [],
                "content": {"summary": rule.get("description", ""), "linkage_rule": rule},
                "created_at": _now_iso(),
            })

    if not generated_cards:
        return {"ok": True, "generated_count": 0, "cards": []}

    # 追加到现有 cards
    cards.extend(generated_cards)

    # 切换到 governance_execution phase
    workflow_state["phase"] = "governance_execution"
    workflow_state["workflow_mode"] = "governance"
    if not workflow_state.get("active_card_id") and generated_cards:
        # 激活第一张高优先级卡
        first_card = generated_cards[0]
        first_card["status"] = CardStatus.ACTIVE
        workflow_state["active_card_id"] = first_card["id"]
        workflow_state["workspace_mode"] = first_card.get("workspace_mode", WorkspaceMode.FILE)
    # 确保标志
    metadata = workflow_state.get("metadata") or {}
    metadata["unified_architecture"] = True
    workflow_state["metadata"] = metadata

    recovery["workflow_state"] = workflow_state
    recovery["cards"] = cards
    # bump revision
    rev = recovery.get("revision") or 0
    try:
        rev = int(rev)
    except (TypeError, ValueError):
        rev = 0
    recovery["revision"] = rev + 1
    recovery["updated_at"] = _now_iso()

    payload["workflow_recovery"] = recovery

    if user_id:
        memo.updated_by = user_id
    save_memo_payload_atomic(db, memo, payload)
    db.flush()

    return {
        "ok": True,
        "generated_count": len(generated_cards),
        "cards": generated_cards,
        "active_card_id": workflow_state.get("active_card_id"),
        "recovery_revision": recovery.get("revision", 0),
    }


def _file_card(
    workflow_state: dict[str, Any],
    filepath: str,
    *,
    priority: str = "medium",
    origin: str = "blueprint_compile",
) -> dict[str, Any]:
    """从文件路径生成一张 governance card。"""
    card_id = _new_id("gcard")
    filename = filepath.rsplit("/", 1)[-1] if "/" in filepath else filepath
    file_role = _infer_file_role(filepath)
    handoff_policy = "open_development_studio" if file_role == "tool" else "open_file_workspace"
    return {
        "id": card_id,
        "workflow_id": workflow_state.get("workflow_id"),
        "source": "studio",
        "type": "governance",
        "card_type": "governance",
        "phase": "governance_execution",
        "title": f"处理文件: {filename}",
        "summary": f"根据 Blueprint 生成：处理 {filepath}",
        "status": CardStatus.QUEUED,
        "priority": priority,
        "workspace_mode": WorkspaceMode.FILE,
        "target_file": filepath,
        "file_role": file_role,
        "handoff_policy": handoff_policy,
        "origin": origin,
        "target": {"target_type": "source_file", "target_key": filepath},
        "actions": [],
        "content": {
            "summary": f"处理 {filepath}",
            "file_path": filepath,
            "file_role": file_role,
            "handoff_policy": handoff_policy,
        },
        "created_at": _now_iso(),
    }


def _infer_file_role(filepath: str) -> str:
    lower_name = (filepath.rsplit("/", 1)[-1] if "/" in filepath else filepath).lower()
    if lower_name == "skill.md":
        return "main_prompt"
    if "example" in lower_name:
        return "example"
    if "reference" in lower_name:
        return "reference"
    if "template" in lower_name:
        return "template"
    if "tool" in lower_name:
        return "tool"
    if "knowledge" in lower_name or lower_name.endswith((".md", ".txt", ".jsonl")):
        return "knowledge_base"
    return "unknown_asset"
