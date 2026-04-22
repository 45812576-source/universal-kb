"""Studio Card Transition Service — 卡片阶段过渡验证。

Phase B7: 负责验证卡片是否可以过渡到下一阶段，生成 transition_blocked_patch。
M4 边界: 治理、绑定、权限、索引、文件确认属于 internal route。
只有 Tool / script / API / function / automation 属于真正外部 handoff。
"""
from __future__ import annotations

import logging
from typing import Any

from app.services import studio_card_contract_service

logger = logging.getLogger(__name__)


def validate_transition(
    *,
    from_contract_id: str,
    to_contract_id: str,
    cards: list[dict[str, Any]] | None = None,
    staged_edits: list[dict[str, Any]] | None = None,
    workflow_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """验证从一张卡到另一张卡的过渡是否合法。

    返回 {"allowed": True} 或 {"allowed": False, "reason": ..., "message": ...}。
    """
    from_contract = studio_card_contract_service.get_contract(from_contract_id)
    to_contract = studio_card_contract_service.get_contract(to_contract_id)

    if not from_contract:
        return {"allowed": False, "reason": "unknown_source", "message": f"源 contract {from_contract_id} 不存在"}
    if not to_contract:
        return {"allowed": False, "reason": "unknown_target", "message": f"目标 contract {to_contract_id} 不存在"}

    # 检查 next_cards 白名单
    if to_contract_id not in from_contract.next_cards:
        return {
            "allowed": False,
            "reason": "not_in_next_cards",
            "message": f"「{from_contract.title}」不能直接跳转到「{to_contract.title}」，请按顺序推进。",
        }

    # 检查 pending staged edits 阻塞
    if from_contract.drawer_policy == "on_pending_edit" and staged_edits:
        pending = [
            e for e in (staged_edits or [])
            if isinstance(e, dict) and e.get("status") == "pending"
        ]
        if pending:
            return {
                "allowed": False,
                "reason": "pending_staged_edit",
                "message": f"「{from_contract.title}」还有 {len(pending)} 个待确认修改，请先处理。",
            }

    return {"allowed": True}


def classify_route(
    *,
    card: dict[str, Any],
    contract: studio_card_contract_service.StudioCardContract | None = None,
) -> dict[str, Any]:
    """判断卡片的 route 类型: internal | external。

    M4 最终边界:
    - governance / file workspace / studio chat → internal route
    - tool / dev_studio / opencode → external handoff
    """
    handoff_policy = card.get("handoff_policy", "")
    file_role = card.get("file_role", "")

    EXTERNAL_POLICIES = {"open_development_studio", "open_opencode"}
    INTERNAL_POLICIES = {"open_file_workspace", "open_governance_panel", "stay_in_studio_chat"}

    if handoff_policy in EXTERNAL_POLICIES:
        destinations = {
            "open_development_studio": "dev_studio",
            "open_opencode": "opencode",
        }
        return {
            "route_kind": "external",
            "destination": destinations.get(handoff_policy, "external"),
            "return_to": "bind_back",
            "explanation": f"需要在外部编辑器中完成「{card.get('title', '')}」的实现",
        }

    if handoff_policy in INTERNAL_POLICIES:
        destinations = {
            "open_file_workspace": "file_workspace",
            "open_governance_panel": "governance_panel",
            "stay_in_studio_chat": "studio_chat",
        }
        return {
            "route_kind": "internal",
            "destination": destinations.get(handoff_policy, "studio_chat"),
            "return_to": "current_studio_flow",
            "explanation": f"在 Studio 内完成「{card.get('title', '')}」",
        }

    # 从 file_role 推断
    if file_role == "tool":
        return {
            "route_kind": "external",
            "destination": "dev_studio",
            "return_to": "bind_back",
            "explanation": "工具类文件需要在外部完成实现",
        }

    return {
        "route_kind": "internal",
        "destination": "studio_chat",
        "return_to": "current_studio_flow",
        "explanation": "在 Studio 内继续",
    }
