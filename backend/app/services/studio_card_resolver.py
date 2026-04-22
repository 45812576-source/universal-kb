"""Studio Card Resolver — 根据 skill 状态决定需要哪些卡片。

职责：
- 根据 session_mode 路由到对应的卡片注册表（Architect / Optimize / Audit）
- 根据当前 phase 确定可见卡片集
- 过滤已完成的卡片
- 为每张卡附加 contract_id
- 按生命周期阻塞优先级排序
- 返回 active_card 建议 + cards 列表 + workflow_state_patch
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Architect 卡片注册表 ──────────────────────────────────────────────────────

ARCHITECT_CARDS: list[dict[str, Any]] = [
    # ── Phase 1: 问题定义 (Why) ──
    {
        "id": "create:architect:5whys",
        "contract_id": "architect.why.5whys",
        "title": "5 Whys 根因卡",
        "phase": "phase_1_why",
        "kind": "create",
        "mode": "analysis",
        "priority": 120,
    },
    {
        "id": "create:architect:first-principles",
        "contract_id": "architect.why.first_principles",
        "title": "第一性原理卡",
        "phase": "phase_1_why",
        "kind": "create",
        "mode": "analysis",
        "priority": 119,
    },
    {
        "id": "create:architect:jtbd",
        "contract_id": "architect.why.jtbd",
        "title": "JTBD 场景卡",
        "phase": "phase_1_why",
        "kind": "create",
        "mode": "analysis",
        "priority": 118,
    },
    {
        "id": "create:architect:cynefin",
        "contract_id": "architect.why.cynefin",
        "title": "Cynefin 复杂度卡",
        "phase": "phase_1_why",
        "kind": "create",
        "mode": "analysis",
        "priority": 117,
    },
    # ── Phase 2: 要素拆解 (What) ──
    {
        "id": "create:architect:mece",
        "contract_id": "architect.what.mece",
        "title": "MECE 维度卡",
        "phase": "phase_2_what",
        "kind": "create",
        "mode": "analysis",
        "priority": 116,
    },
    {
        "id": "create:architect:issue-tree",
        "contract_id": "architect.what.issue_tree",
        "title": "Issue Tree 卡",
        "phase": "phase_2_what",
        "kind": "create",
        "mode": "analysis",
        "priority": 115,
    },
    {
        "id": "create:architect:value-chain",
        "contract_id": "architect.what.value_chain",
        "title": "价值链分析卡",
        "phase": "phase_2_what",
        "kind": "create",
        "mode": "analysis",
        "priority": 114,
    },
    {
        "id": "create:architect:scenario-planning",
        "contract_id": "architect.what.scenario_planning",
        "title": "场景规划卡",
        "phase": "phase_2_what",
        "kind": "create",
        "mode": "analysis",
        "priority": 113,
    },
    # ── Phase 3: 验证收敛 (How) ──
    {
        "id": "create:architect:pyramid",
        "contract_id": "architect.how.pyramid",
        "title": "金字塔结构卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 112,
    },
    {
        "id": "create:architect:pre-mortem",
        "contract_id": "architect.how.pre_mortem",
        "title": "Pre-Mortem 卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 111,
    },
    {
        "id": "create:architect:red-team",
        "contract_id": "architect.how.red_team",
        "title": "Red Team 卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 110,
    },
    {
        "id": "create:architect:sensitivity",
        "contract_id": "architect.how.sensitivity",
        "title": "敏感度分析卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 109,
    },
    {
        "id": "create:architect:zero-based",
        "contract_id": "architect.how.zero_based",
        "title": "零基思考卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 108,
    },
    {
        "id": "create:architect:ooda",
        "contract_id": "architect.how.ooda",
        "title": "OODA Loop 卡",
        "phase": "phase_3_how",
        "kind": "create",
        "mode": "analysis",
        "priority": 107,
    },
]

# ── Optimize 卡片注册表 ─────────────────────────────────────────────────────

OPTIMIZE_CARDS: list[dict[str, Any]] = [
    # ── 治理卡 ──
    {
        "id": "governance:audit-review",
        "contract_id": "optimize.governance.audit_review",
        "title": "审计结果确认卡",
        "phase": "governance",
        "kind": "confirm",
        "mode": "report",
        "priority": 200,
    },
    {
        "id": "governance:constraint-check",
        "contract_id": "optimize.governance.constraint_check",
        "title": "全局约束检查卡",
        "phase": "governance",
        "kind": "governance",
        "mode": "report",
        "priority": 199,
    },
    # ── 修改卡 ──
    {
        "id": "refine:prompt-edit",
        "contract_id": "optimize.refine.prompt_edit",
        "title": "Prompt 修改卡",
        "phase": "refine",
        "kind": "refine",
        "mode": "file",
        "priority": 150,
    },
    {
        "id": "refine:example-edit",
        "contract_id": "optimize.refine.example_edit",
        "title": "示例修改卡",
        "phase": "refine",
        "kind": "refine",
        "mode": "file",
        "priority": 149,
    },
    {
        "id": "refine:tool-binding",
        "contract_id": "optimize.refine.tool_binding",
        "title": "工具绑定修改卡",
        "phase": "refine",
        "kind": "refine",
        "mode": "file",
        "priority": 148,
    },
    # ── 验证卡 ──
    {
        "id": "validation:preflight",
        "contract_id": "optimize.validation.preflight",
        "title": "Preflight 预检卡",
        "phase": "validation",
        "kind": "validation",
        "mode": "report",
        "priority": 100,
    },
    {
        "id": "validation:sandbox-run",
        "contract_id": "optimize.validation.sandbox_run",
        "title": "沙盒执行验证卡",
        "phase": "validation",
        "kind": "validation",
        "mode": "report",
        "priority": 99,
    },
]

# ── Audit 卡片注册表 ──────────────────────────────────────────────────────

AUDIT_CARDS: list[dict[str, Any]] = [
    # ── 审计卡 ──
    {
        "id": "audit:quality-scan",
        "contract_id": "audit.scan.quality",
        "title": "质量审计卡",
        "phase": "audit",
        "kind": "governance",
        "mode": "report",
        "priority": 200,
    },
    {
        "id": "audit:security-scan",
        "contract_id": "audit.scan.security",
        "title": "安全审计卡",
        "phase": "audit",
        "kind": "governance",
        "mode": "report",
        "priority": 199,
    },
    # ── 整改卡 ──
    {
        "id": "fixing:critical-issues",
        "contract_id": "audit.fixing.critical",
        "title": "严重问题整改卡",
        "phase": "fixing",
        "kind": "fixing",
        "mode": "file",
        "priority": 180,
    },
    {
        "id": "fixing:moderate-issues",
        "contract_id": "audit.fixing.moderate",
        "title": "一般问题整改卡",
        "phase": "fixing",
        "kind": "fixing",
        "mode": "file",
        "priority": 170,
    },
    # ── 发布前验证 ──
    {
        "id": "release:preflight-recheck",
        "contract_id": "audit.release.preflight_recheck",
        "title": "整改后 Preflight 复查卡",
        "phase": "release",
        "kind": "validation",
        "mode": "report",
        "priority": 100,
    },
    {
        "id": "release:publish-gate",
        "contract_id": "audit.release.publish_gate",
        "title": "发布门禁卡",
        "phase": "release",
        "kind": "release",
        "mode": "report",
        "priority": 90,
    },
]

# ── Phase Groups — 按模式分组 ────────────────────────────────────────────

# Architect (create_new_skill)
# phase -> 对应阶段范围
_ARCHITECT_PHASE_GROUPS: dict[str, list[str]] = {
    "phase_1_why": ["phase_1_why"],
    "phase_2_what": ["phase_1_why", "phase_2_what"],
    "phase_3_how": ["phase_1_why", "phase_2_what", "phase_3_how"],
    "ooda_iteration": ["phase_1_why", "phase_2_what", "phase_3_how"],
    "ready_for_draft": ["phase_1_why", "phase_2_what", "phase_3_how"],
}

# Optimize (optimize_existing_skill)
_OPTIMIZE_PHASE_GROUPS: dict[str, list[str]] = {
    "governance": ["governance"],
    "refine": ["governance", "refine"],
    "validation": ["governance", "refine", "validation"],
}

# Audit (audit_imported_skill)
_AUDIT_PHASE_GROUPS: dict[str, list[str]] = {
    "audit": ["audit"],
    "fixing": ["audit", "fixing"],
    "release": ["audit", "fixing", "release"],
}

# 兼容旧代码 — M3 使用的名称
_PHASE_GROUPS = _ARCHITECT_PHASE_GROUPS

# 模式 → (注册表, phase_groups, card_type) 映射
_MODE_REGISTRY: dict[str, tuple[list[dict[str, Any]], dict[str, list[str]], str]] = {
    "create_new_skill": (ARCHITECT_CARDS, _ARCHITECT_PHASE_GROUPS, "architect"),
    "optimize_existing_skill": (OPTIMIZE_CARDS, _OPTIMIZE_PHASE_GROUPS, "optimize"),
    "audit_imported_skill": (AUDIT_CARDS, _AUDIT_PHASE_GROUPS, "audit"),
}

# 生命周期阻塞优先级（数字越小越优先）
_LIFECYCLE_PRIORITY: dict[str, int] = {
    "confirm": 0,
    "fixing": 1,
    "governance": 2,
    "validation": 3,
    "create": 4,
    "refine": 5,
    "release": 6,
}


@dataclass
class CardResolverResult:
    """CardResolver 返回结构。"""
    cards: list[dict[str, Any]] = field(default_factory=list)
    active_card_id: str | None = None
    workflow_state_patch: dict[str, Any] = field(default_factory=dict)


def resolve_cards(
    db: Any,
    skill_id: int,
    *,
    session_mode: str,
    architect_phase: str,
    workflow_state: dict[str, Any] | None = None,
    cards: list[dict[str, Any]] | None = None,
    staged_edits: list[dict[str, Any]] | None = None,
    memo: Any = None,
) -> CardResolverResult:
    """根据 session_mode + phase 解析需要哪些卡片。

    支持三种模式：
    - create_new_skill → ARCHITECT_CARDS
    - optimize_existing_skill → OPTIMIZE_CARDS
    - audit_imported_skill → AUDIT_CARDS

    核心职责：
    1. 按 session_mode 选择注册表
    2. 根据 phase 确定可见卡片范围
    3. 过滤已完成和已存在的卡片
    4. 按生命周期阻塞优先级排序
    5. 返回 active_card 建议 + cards 列表 + workflow_state_patch
    """
    cards = list(cards or [])
    workflow_state = dict(workflow_state or {})

    # 按 session_mode 路由到注册表
    mode_entry = _MODE_REGISTRY.get(session_mode)
    if not mode_entry:
        return CardResolverResult(cards=cards)
    registry, phase_groups, card_type = mode_entry

    # 从 memo 读取 completed_card_ids
    recovery = {}
    if memo:
        payload = getattr(memo, "memo_payload", None) or (memo if isinstance(memo, dict) else {})
        recovery = payload.get("workflow_recovery") or {}

    completed_card_ids = set(recovery.get("completed_card_ids") or [])
    existing_card_ids = {c.get("id") for c in cards if isinstance(c, dict)}

    # 确定当前阶段应生成哪些卡
    active_phases = phase_groups.get(architect_phase, [])
    cards_to_add: list[dict[str, Any]] = []

    for card_def in registry:
        card_id = card_def["id"]
        if card_id in completed_card_ids:
            continue
        if card_id in existing_card_ids:
            continue
        if card_def["phase"] not in active_phases:
            continue
        cards_to_add.append(_build_card_from_registry(card_def, workflow_state, card_type))

    # 合并：已有卡 + 新生成的卡
    merged_cards = cards + cards_to_add

    # 排序：按生命周期优先级
    merged_cards = _sort_by_lifecycle_priority(merged_cards)

    # 确定 active_card 建议
    active_card_id = _suggest_active_card(
        merged_cards,
        workflow_state.get("active_card_id"),
        completed_card_ids,
    )

    # workflow_state_patch
    state_patch: dict[str, Any] = {}
    if cards_to_add:
        state_patch["cards_resolved"] = True
        state_patch["resolved_card_count"] = len(cards_to_add)
        state_patch["resolved_mode"] = session_mode

    return CardResolverResult(
        cards=merged_cards,
        active_card_id=active_card_id,
        workflow_state_patch=state_patch,
    )


def _build_card_from_registry(
    card_def: dict[str, Any],
    workflow_state: dict[str, Any],
    card_type: str = "architect",
) -> dict[str, Any]:
    """从注册表定义构建卡片 dict。"""
    mode = card_def.get("mode", "analysis")
    # 根据 mode 推断默认 handoff_policy
    handoff_map = {
        "analysis": "stay_in_studio_chat",
        "file": "open_file_workspace",
        "report": "open_governance_panel",
    }
    return {
        "id": card_def["id"],
        "contract_id": card_def["contract_id"],
        "workflow_id": workflow_state.get("workflow_id"),
        "source": "card_resolver",
        "type": card_type,
        "card_type": card_type,
        "phase": card_def["phase"],
        "title": card_def["title"],
        "summary": card_def["title"],
        "status": "queued",
        "priority": card_def["priority"],
        "workspace_mode": mode,
        "target_file": None,
        "file_role": None,
        "handoff_policy": handoff_map.get(mode, "stay_in_studio_chat"),
        "origin": "card_resolver",
        "kind": card_def["kind"],
        "target": {},
        "actions": [],
        "content": {
            "summary": card_def["title"],
            "contract_id": card_def["contract_id"],
        },
    }


def _sort_by_lifecycle_priority(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按生命周期阻塞优先级排序。"""
    def sort_key(card: dict[str, Any]) -> tuple[int, int]:
        kind = card.get("kind", "create")
        lifecycle_order = _LIFECYCLE_PRIORITY.get(kind, 4)
        # 数字 priority 值越高越优先（降序）
        card_priority = card.get("priority", 0)
        if isinstance(card_priority, str):
            card_priority = {"high": 100, "p0": 100, "medium": 50, "p1": 50, "low": 10, "p2": 10}.get(card_priority, 50)
        return (lifecycle_order, -card_priority)

    return sorted(cards, key=sort_key)


def _suggest_active_card(
    cards: list[dict[str, Any]],
    current_active_id: str | None,
    completed_card_ids: set[str],
) -> str | None:
    """建议 active card：保持当前 active，否则选第一张 active/queued 且未完成的卡。"""
    # 如果当前 active card 仍有效，保持
    if current_active_id:
        for card in cards:
            if card.get("id") == current_active_id and card.get("id") not in completed_card_ids:
                status = card.get("status", "")
                if status in ("active", "drafting", "reviewing", "queued", "pending"):
                    return current_active_id

    # 选第一张可激活的卡
    for card in cards:
        card_id = card.get("id")
        if not card_id or card_id in completed_card_ids:
            continue
        status = card.get("status", "")
        if status in ("active", "queued", "pending"):
            return card_id

    return None
