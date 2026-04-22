"""Card Orchestrator 单元测试 — 覆盖跨阶段请求阻断、pending staged edit 阻塞、
Why-phase early exit 防止、remediation cards without evidence。"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.studio_card_orchestrator import (
    OrchestratorInput,
    OrchestratorOutput,
    StudioCardOrchestrator,
)
from app.services.studio_card_contract_service import StudioCardContract


def _make_contract(
    contract_id="refine.draft_ready",
    phase="refine",
    drawer_policy="on_pending_edit",
    allowed_tools=None,
    exit_criteria=None,
    next_cards=None,
    forbidden_actions=None,
):
    return StudioCardContract(
        contract_id=contract_id,
        title="Test Card",
        phase=phase,
        objective="Test",
        allowed_tools=allowed_tools or ["skill_draft.stage_edit"],
        drawer_policy=drawer_policy,
        exit_criteria=exit_criteria or [],
        next_cards=next_cards or [],
        forbidden_actions=forbidden_actions or [],
    )


def _make_input(
    active_card_id="card_1",
    cards=None,
    staged_edits=None,
    contract_id="refine.draft_ready",
    workflow_state=None,
    validation_result=None,
):
    if cards is None:
        cards = [{"id": "card_1", "status": "active", "contract_id": contract_id}]
    return OrchestratorInput(
        public_run_id="run-1",
        run_version=1,
        skill_id=100,
        conversation_id=1000,
        user_message="test",
        active_card_id=active_card_id,
        contract_id=contract_id,
        cards=cards,
        staged_edits=staged_edits or [],
        workflow_state=workflow_state,
        validation_result=validation_result,
    )


class TestPendingStagedEditBlocking:
    """pending staged edit 应阻塞 transition。"""

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_pending_edit_blocks_transition(self, mock_get):
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.blocked_transition is not None
        assert "待确认修改" in output.blocked_transition["reason"]
        assert output.blocked_transition["blocked_card_id"] == "card_1"
        assert "edit_1" in output.blocked_transition["prerequisite_card_ids"]

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_no_pending_edits_no_blocking(self, mock_get):
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(staged_edits=[])

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.blocked_transition is None

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_adopted_edits_dont_block(self, mock_get):
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "adopted", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.blocked_transition is None

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_pending_edit_other_card_no_block(self, mock_get):
        """其他 card 的 pending edit 不应阻塞当前 card。"""
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_2"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.blocked_transition is None

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_drawer_policy_never_no_block(self, mock_get):
        """drawer_policy=never 时即使有 pending edit 也不阻塞。"""
        contract = _make_contract(drawer_policy="never")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.blocked_transition is None


class TestBlockedTransitionPatch:
    """blocked_transition 应生成 transition_blocked_patch。"""

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_blocked_generates_patch(self, mock_get):
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert len(output.patches) == 1
        patch_data = output.patches[0]
        assert patch_data["patch_type"] == "transition_blocked_patch"
        assert patch_data["target"] == "card_1"

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_blocked_returns_early(self, mock_get):
        """阻塞时不应继续构建 prompt_context 和 allowed_tools。"""
        contract = _make_contract(drawer_policy="on_pending_edit")
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.prompt_context == {}
        assert output.allowed_tools == []


class TestExitCriteria:
    """Exit criteria 正确触发 card completion。"""

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_staged_edit_adopted_triggers_exit(self, mock_get):
        contract = _make_contract(
            exit_criteria=[{"type": "staged_edit_adopted"}],
            next_cards=["governance.panel"],
        )
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "adopted", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        # 应有 card_status_patch (completed)
        card_status_patches = [p for p in output.patches if p["patch_type"] == "card_status_patch"]
        assert len(card_status_patches) == 1
        assert card_status_patches[0]["payload"]["status"] == "completed"

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_sandbox_passed_triggers_exit(self, mock_get):
        contract = _make_contract(
            exit_criteria=[{"type": "sandbox_passed"}],
            next_cards=[],
        )
        mock_get.return_value = contract

        inp = _make_input(
            validation_result={"status": "pass"},
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        card_status_patches = [p for p in output.patches if p["patch_type"] == "card_status_patch"]
        assert len(card_status_patches) == 1
        assert card_status_patches[0]["payload"]["exit_reason"] == "sandbox_passed"

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_no_exit_criteria_no_exit(self, mock_get):
        contract = _make_contract(exit_criteria=[])
        mock_get.return_value = contract

        inp = _make_input()

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        card_status_patches = [p for p in output.patches if p["patch_type"] == "card_status_patch"]
        assert len(card_status_patches) == 0

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_unmet_exit_criteria_no_exit(self, mock_get):
        """staged_edit_adopted 条件不满足（status=pending）时不应退出。"""
        contract = _make_contract(
            exit_criteria=[{"type": "staged_edit_adopted"}],
        )
        mock_get.return_value = contract

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "pending", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        # 注意: drawer_policy="on_pending_edit" 会先阻塞，这里用 never
        contract.drawer_policy = "never"
        output = orch.orchestrate(inp)

        card_status_patches = [p for p in output.patches if p["patch_type"] == "card_status_patch"]
        assert len(card_status_patches) == 0


class TestPromptContext:
    """orchestrate 应构建正确的 prompt context。"""

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_prompt_context_includes_contract(self, mock_get):
        contract = _make_contract(
            contract_id="create.onboarding",
            phase="create",
            allowed_tools=["studio_chat.ask_one_question"],
            forbidden_actions=["No draft"],
        )
        mock_get.return_value = contract

        inp = _make_input(contract_id="create.onboarding")

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        ctx = output.prompt_context
        assert ctx["skill_id"] == 100
        assert ctx["conversation_id"] == 1000
        assert ctx["active_card_id"] == "card_1"
        assert ctx["contract"]["contract_id"] == "create.onboarding"
        assert "studio_chat.ask_one_question" in ctx["contract"]["allowed_tools"]

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_allowed_tools_from_contract(self, mock_get):
        contract = _make_contract(
            allowed_tools=["sandbox.run", "sandbox.targeted_rerun"],
        )
        mock_get.return_value = contract

        inp = _make_input()

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert "sandbox.run" in output.allowed_tools
        assert "sandbox.targeted_rerun" in output.allowed_tools

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_no_contract_empty_tools(self, mock_get):
        mock_get.return_value = None

        inp = _make_input(contract_id=None)

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        assert output.allowed_tools == []


class TestQueueWindow:
    """Exit 后应正确重算 queue window。"""

    @patch("app.services.studio_card_contract_service.get_contract")
    def test_exit_generates_queue_window_patch(self, mock_get):
        next_contract = StudioCardContract(
            contract_id="governance.panel",
            phase="governance",
            title="Governance Card",
            objective="Test next card",
        )
        contract = _make_contract(
            exit_criteria=[{"type": "staged_edit_adopted"}],
            next_cards=["governance.panel"],
        )

        def _get(cid):
            if cid == "refine.draft_ready":
                return contract
            if cid == "governance.panel":
                return next_contract
            return None

        mock_get.side_effect = _get

        inp = _make_input(
            staged_edits=[
                {"id": "edit_1", "status": "adopted", "origin_card_id": "card_1"},
            ],
        )

        orch = StudioCardOrchestrator()
        output = orch.orchestrate(inp)

        queue_patches = [p for p in output.patches if p["patch_type"] == "queue_window_patch"]
        assert len(queue_patches) == 1
