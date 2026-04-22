"""Studio Card Orchestrator — 后端接管 active card 编排。

Phase B7: 前端不注入 studio_orchestration 时，后端仍能完整推进 Card Queue。
输入 run context + card context + contract → 输出 prompt context + patches。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services import studio_card_contract_service
from app.services.studio_patch_bus import build_patch_envelope, build_error_patch

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorInput:
    """编排输入。"""
    public_run_id: str
    run_version: int
    skill_id: int
    conversation_id: int
    user_message: str
    active_card_id: str | None = None
    contract_id: str | None = None
    memory_pack: dict[str, Any] | None = None
    workflow_state: dict[str, Any] | None = None
    validation_result: dict[str, Any] | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    staged_edits: list[dict[str, Any]] = field(default_factory=list)
    completed_card_ids: list[str] = field(default_factory=list)
    stale_card_ids: list[str] = field(default_factory=list)


@dataclass
class OrchestratorOutput:
    """编排输出 — patch 列表 + prompt context。"""
    prompt_context: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    blocked_transition: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


class StudioCardOrchestrator:
    """后端 card 编排器 — 替代前端 proxy orchestration。"""

    def orchestrate(self, inp: OrchestratorInput) -> OrchestratorOutput:
        """执行一轮编排。"""
        output = OrchestratorOutput()
        patch_seq = 0

        # 1. 解析 contract
        contract = None
        if inp.contract_id:
            contract = studio_card_contract_service.get_contract(inp.contract_id)
        if not contract and inp.active_card_id:
            contract = self._infer_contract(inp)

        # 2. 检查 transition blocked
        if contract and inp.active_card_id:
            blocked = self._check_transition_blocked(inp, contract)
            if blocked:
                output.blocked_transition = blocked
                patch_seq += 1
                output.patches.append(build_patch_envelope(
                    run_id=inp.public_run_id,
                    run_version=inp.run_version,
                    patch_seq=patch_seq,
                    patch_type="transition_blocked_patch",
                    target=inp.active_card_id,
                    payload=blocked,
                ))
                return output

        # 3. 构建 prompt context
        output.prompt_context = self._build_prompt_context(inp, contract)
        output.allowed_tools = list(contract.allowed_tools) if contract else []

        # 4. 检查 card exit criteria
        if contract and inp.active_card_id:
            exit_result = self._check_exit_criteria(inp, contract)
            if exit_result.get("should_exit"):
                # 生成 card completion patches
                patch_seq += 1
                output.patches.append(build_patch_envelope(
                    run_id=inp.public_run_id,
                    run_version=inp.run_version,
                    patch_seq=patch_seq,
                    patch_type="card_status_patch",
                    target=inp.active_card_id,
                    payload={
                        "card_id": inp.active_card_id,
                        "status": "completed",
                        "exit_reason": exit_result.get("reason", "criteria_met"),
                    },
                ))

                # 生成 next card
                next_card = self._resolve_next_card(inp, contract)
                if next_card:
                    patch_seq += 1
                    output.patches.append(build_patch_envelope(
                        run_id=inp.public_run_id,
                        run_version=inp.run_version,
                        patch_seq=patch_seq,
                        patch_type="card_patch",
                        target=next_card.get("id", ""),
                        payload=next_card,
                    ))

                # 重算 queue window
                patch_seq += 1
                output.patches.append(build_patch_envelope(
                    run_id=inp.public_run_id,
                    run_version=inp.run_version,
                    patch_seq=patch_seq,
                    patch_type="queue_window_patch",
                    payload=self._recalculate_queue_window(inp, next_card),
                ))

        return output

    def _infer_contract(self, inp: OrchestratorInput) -> Any:
        """从 active card 的属性推断 contract。"""
        if not inp.active_card_id or not inp.cards:
            return None
        for card in inp.cards:
            if not isinstance(card, dict):
                continue
            if card.get("id") == inp.active_card_id:
                cid = card.get("contract_id")
                if cid:
                    return studio_card_contract_service.get_contract(cid)
        return None

    def _check_transition_blocked(
        self,
        inp: OrchestratorInput,
        contract: studio_card_contract_service.StudioCardContract,
    ) -> dict[str, Any] | None:
        """检查跨阶段请求是否被阻断。

        M4 边界: 治理留在 Studio，只有外部实现才 handoff。
        """
        if not inp.active_card_id or not inp.cards:
            return None

        active_card = None
        for card in inp.cards:
            if isinstance(card, dict) and card.get("id") == inp.active_card_id:
                active_card = card
                break

        if not active_card:
            return None

        # 检查 pending staged edits 阻塞
        pending_edits = [
            e for e in inp.staged_edits
            if isinstance(e, dict)
            and e.get("status") == "pending"
            and e.get("origin_card_id") == inp.active_card_id
        ]
        if pending_edits and contract.drawer_policy == "on_pending_edit":
            return {
                "blocked": True,
                "reason": "存在待确认修改，请先处理再继续。",
                "blocked_card_id": inp.active_card_id,
                "prerequisite_card_ids": [e.get("id") for e in pending_edits],
            }

        return None

    def _check_exit_criteria(
        self,
        inp: OrchestratorInput,
        contract: studio_card_contract_service.StudioCardContract,
    ) -> dict[str, Any]:
        """检查当前 card 是否满足退出条件。"""
        if not contract.exit_criteria:
            return {"should_exit": False}

        for criterion in contract.exit_criteria:
            if not isinstance(criterion, dict):
                continue
            ctype = criterion.get("type")
            if ctype == "staged_edit_adopted":
                adopted = any(
                    isinstance(e, dict)
                    and e.get("origin_card_id") == inp.active_card_id
                    and e.get("status") == "adopted"
                    for e in inp.staged_edits
                )
                if adopted:
                    return {"should_exit": True, "reason": "staged_edit_adopted"}
            elif ctype == "sandbox_passed":
                if inp.validation_result and inp.validation_result.get("status") == "pass":
                    return {"should_exit": True, "reason": "sandbox_passed"}

        return {"should_exit": False}

    def _build_prompt_context(
        self,
        inp: OrchestratorInput,
        contract: Any,
    ) -> dict[str, Any]:
        """构建给 LLM 的 prompt context。"""
        ctx: dict[str, Any] = {
            "public_run_id": inp.public_run_id,
            "skill_id": inp.skill_id,
            "conversation_id": inp.conversation_id,
            "user_message": inp.user_message,
        }
        if inp.active_card_id:
            ctx["active_card_id"] = inp.active_card_id
        if contract:
            ctx["contract"] = {
                "contract_id": contract.contract_id,
                "phase": contract.phase,
                "objective": contract.objective,
                "allowed_tools": contract.allowed_tools,
                "forbidden_actions": contract.forbidden_actions,
            }
        if inp.workflow_state:
            ctx["workflow_phase"] = inp.workflow_state.get("phase")
            ctx["session_mode"] = inp.workflow_state.get("session_mode")
        if inp.memory_pack:
            ctx["memory_pack"] = inp.memory_pack
        return ctx

    def _resolve_next_card(
        self,
        inp: OrchestratorInput,
        contract: studio_card_contract_service.StudioCardContract,
    ) -> dict[str, Any] | None:
        """根据 contract.next_cards 确定下一张卡。"""
        if not contract.next_cards:
            return None

        existing_ids = {
            c.get("contract_id") for c in inp.cards
            if isinstance(c, dict) and c.get("contract_id")
        }

        for next_cid in contract.next_cards:
            if next_cid in existing_ids:
                continue
            next_contract = studio_card_contract_service.get_contract(next_cid)
            if next_contract:
                import uuid
                return {
                    "id": f"card_{uuid.uuid4().hex[:10]}",
                    "contract_id": next_cid,
                    "title": next_contract.title,
                    "phase": next_contract.phase,
                    "status": "pending",
                    "source": "orchestrator",
                    "origin": "auto_transition",
                }
        return None

    def _recalculate_queue_window(
        self,
        inp: OrchestratorInput,
        next_card: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """重算 queue window — 保持 3~5 张可见。"""
        actionable_statuses = {"pending", "queued", "active", "reviewing", "drafting", "diff_ready"}
        completed_set = set(inp.completed_card_ids)

        new_active_id = next_card.get("id") if next_card else None
        visible_ids: list[str] = []
        if new_active_id:
            visible_ids.append(new_active_id)

        for card in inp.cards:
            if not isinstance(card, dict):
                continue
            cid = card.get("id")
            if not cid or cid in visible_ids or cid in completed_set:
                continue
            if cid == inp.active_card_id:
                continue
            if card.get("status") in actionable_statuses:
                visible_ids.append(cid)
                if len(visible_ids) >= 5:
                    break

        return {
            "active_card_id": new_active_id or (visible_ids[0] if visible_ids else None),
            "visible_card_ids": visible_ids,
            "phase": (inp.workflow_state or {}).get("phase", "discover"),
        }


# Module-level singleton
studio_card_orchestrator = StudioCardOrchestrator()
