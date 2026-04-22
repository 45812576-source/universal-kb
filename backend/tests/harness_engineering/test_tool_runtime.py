"""Tool Runtime 单元测试 — 覆盖 read/stage/execute/publish tiers、sandbox.run 前置检查、
publish/delete/approval human-in-the-loop、audit 记录、tool_error_patch 合法性。"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.studio_tool_runtime import (
    ToolCategory,
    ToolCheckResult,
    ToolCheckResponse,
    ToolExecutionRecord,
    ToolIntent,
    build_tool_error_patch,
    build_tool_confirmation_patch,
    check_tool_intent,
    complete_execution_record,
    create_execution_record,
    execution_record_to_harness_step,
    get_tool_category,
    is_tool_registered,
    TOOL_CATEGORIES,
    TOOLS_REQUIRING_CONFIRMATION,
)


# ── Tool Category Tiers ──────────────────────────────────────────────────────

class TestToolCategoryTiers:

    def test_read_tier_tools(self):
        read_tools = [k for k, v in TOOL_CATEGORIES.items() if v == ToolCategory.READ]
        assert "studio_chat.ask_one_question" in read_tools
        assert "skill_file.open" in read_tools

    def test_stage_tier_tools(self):
        stage_tools = [k for k, v in TOOL_CATEGORIES.items() if v == ToolCategory.STAGE]
        assert "skill_draft.stage_edit" in stage_tools
        assert "studio_artifact.save" in stage_tools

    def test_execute_tier_tools(self):
        execute_tools = [k for k, v in TOOL_CATEGORIES.items() if v == ToolCategory.EXECUTE]
        assert "sandbox.run" in execute_tools
        assert "sandbox.targeted_rerun" in execute_tools

    def test_publish_tier_tools(self):
        publish_tools = [k for k, v in TOOL_CATEGORIES.items() if v == ToolCategory.PUBLISH]
        assert "staged_edit.adopt" in publish_tools
        assert "staged_edit.reject" in publish_tools

    def test_get_tool_category_returns_correct_type(self):
        assert get_tool_category("sandbox.run") == ToolCategory.EXECUTE
        assert get_tool_category("staged_edit.adopt") == ToolCategory.PUBLISH
        assert get_tool_category("nonexistent") is None

    def test_is_tool_registered(self):
        assert is_tool_registered("sandbox.run") is True
        assert is_tool_registered("nonexistent") is False


# ── check_tool_intent: sandbox.run pre-checks ────────────────────────────────

class TestCheckToolIntentSandbox:

    def _make_active_card(self, status="active", contract_id="validation.test_ready"):
        return {
            "id": "card_1",
            "status": status,
            "contract_id": contract_id,
        }

    @patch("app.services.studio_card_contract_service.is_tool_allowed", return_value=True)
    @patch("app.services.studio_card_contract_service.get_contract")
    def test_sandbox_run_needs_confirmation(self, mock_get, mock_allowed):
        mock_get.return_value = MagicMock(
            allowed_tools=["sandbox.run"],
            forbidden_actions=[],
        )
        intent = ToolIntent(tool_name="sandbox.run", card_id="card_1")
        resp = check_tool_intent(intent, active_card=self._make_active_card())
        assert resp.result == ToolCheckResult.NEEDS_CONFIRMATION
        assert resp.confirmation_prompt is not None

    @patch("app.services.studio_card_contract_service.is_tool_allowed", return_value=True)
    @patch("app.services.studio_card_contract_service.get_contract")
    def test_sandbox_run_skip_confirmation(self, mock_get, mock_allowed):
        mock_get.return_value = MagicMock(
            allowed_tools=["sandbox.run"],
            forbidden_actions=[],
        )
        intent = ToolIntent(tool_name="sandbox.run", card_id="card_1")
        resp = check_tool_intent(intent, active_card=self._make_active_card(), skip_confirmation=True)
        assert resp.result == ToolCheckResult.ALLOWED

    def test_sandbox_run_no_active_card(self):
        intent = ToolIntent(tool_name="sandbox.run")
        resp = check_tool_intent(intent, active_card=None)
        assert resp.result == ToolCheckResult.DENIED_NO_CARD

    def test_sandbox_run_card_not_active(self):
        intent = ToolIntent(tool_name="sandbox.run")
        card = self._make_active_card(status="completed")
        resp = check_tool_intent(intent, active_card=card)
        assert resp.result == ToolCheckResult.DENIED_CARD_NOT_ACTIVE


# ── check_tool_intent: publish tier human-in-the-loop ─────────────────────────

class TestCheckToolIntentPublish:

    @patch("app.services.studio_card_contract_service.is_tool_allowed", return_value=True)
    @patch("app.services.studio_card_contract_service.get_contract")
    def test_staged_edit_adopt_needs_confirmation(self, mock_get, mock_allowed):
        mock_get.return_value = MagicMock(
            allowed_tools=["staged_edit.adopt"],
            forbidden_actions=[],
        )
        intent = ToolIntent(tool_name="staged_edit.adopt", card_id="card_1")
        card = {"id": "card_1", "status": "diff_ready", "contract_id": "refine.draft_ready"}
        resp = check_tool_intent(intent, active_card=card)
        assert resp.result == ToolCheckResult.NEEDS_CONFIRMATION

    @patch("app.services.studio_card_contract_service.is_tool_allowed", return_value=True)
    @patch("app.services.studio_card_contract_service.get_contract")
    def test_staged_edit_reject_no_confirmation_needed(self, mock_get, mock_allowed):
        mock_get.return_value = MagicMock(
            allowed_tools=["staged_edit.reject"],
            forbidden_actions=[],
        )
        intent = ToolIntent(tool_name="staged_edit.reject", card_id="card_1")
        card = {"id": "card_1", "status": "diff_ready", "contract_id": "refine.draft_ready"}
        resp = check_tool_intent(intent, active_card=card)
        # staged_edit.reject is NOT in TOOLS_REQUIRING_CONFIRMATION
        assert resp.result == ToolCheckResult.ALLOWED


# ── check_tool_intent: unregistered tool ──────────────────────────────────────

class TestCheckToolIntentUnregistered:

    def test_unregistered_tool_denied(self):
        intent = ToolIntent(tool_name="malicious.exploit")
        resp = check_tool_intent(intent, active_card={"id": "c1", "status": "active"})
        assert resp.result == ToolCheckResult.DENIED_NOT_IN_CONTRACT
        assert resp.reason == "unregistered_tool"

    @patch("app.services.studio_card_contract_service.is_tool_allowed", return_value=False)
    @patch("app.services.studio_card_contract_service.get_contract")
    def test_tool_not_in_contract_whitelist(self, mock_get, mock_allowed):
        mock_get.return_value = MagicMock(
            allowed_tools=["studio_chat.ask_one_question"],
            forbidden_actions=[],
        )
        intent = ToolIntent(
            tool_name="sandbox.run",
            contract_id="create.onboarding",
            card_id="card_1",
        )
        card = {"id": "card_1", "status": "active", "contract_id": "create.onboarding"}
        resp = check_tool_intent(intent, active_card=card)
        assert resp.result == ToolCheckResult.DENIED_NOT_IN_CONTRACT
        assert resp.reason == "not_in_allowed_tools"


# ── Execution Records (Audit) ────────────────────────────────────────────────

class TestExecutionRecord:

    def test_create_record_allowed(self):
        intent = ToolIntent(
            tool_name="studio_chat.ask_one_question",
            run_id="run-1",
            step_seq=3,
            arguments={"question": "test?"},
        )
        check = ToolCheckResponse(
            result=ToolCheckResult.ALLOWED,
            tool_name="studio_chat.ask_one_question",
            category=ToolCategory.READ,
        )
        record = create_execution_record(intent, check_response=check)
        assert record.status == "running"
        assert record.run_id == "run-1"
        assert record.tool_name == "studio_chat.ask_one_question"
        assert record.category == "read"
        assert record.step_seq == 3
        assert record.input_summary.startswith("studio_chat.ask_one_question(")

    def test_create_record_denied(self):
        intent = ToolIntent(tool_name="sandbox.run", run_id="run-1", step_seq=1)
        check = ToolCheckResponse(
            result=ToolCheckResult.DENIED_NO_CARD,
            tool_name="sandbox.run",
            category=ToolCategory.EXECUTE,
            reason="no_active_card",
            message="No card",
        )
        record = create_execution_record(intent, check_response=check)
        assert record.status == "denied"
        assert record.error == "No card"
        assert record.finished_at is not None

    def test_complete_record_success(self):
        intent = ToolIntent(tool_name="skill_file.open", run_id="run-1")
        check = ToolCheckResponse(
            result=ToolCheckResult.ALLOWED,
            tool_name="skill_file.open",
            category=ToolCategory.READ,
        )
        record = create_execution_record(intent, check_response=check)
        completed = complete_execution_record(record, output_summary="File opened")
        assert completed.status == "completed"
        assert completed.output_summary == "File opened"
        assert completed.finished_at is not None

    def test_complete_record_failure(self):
        intent = ToolIntent(tool_name="sandbox.run", run_id="run-1")
        check = ToolCheckResponse(
            result=ToolCheckResult.ALLOWED,
            tool_name="sandbox.run",
            category=ToolCategory.EXECUTE,
        )
        record = create_execution_record(intent, check_response=check)
        completed = complete_execution_record(record, error="Sandbox crashed")
        assert completed.status == "failed"
        assert completed.error == "Sandbox crashed"

    def test_record_to_harness_step(self):
        record = ToolExecutionRecord(
            step_id="step_abc",
            run_id="run-1",
            tool_name="sandbox.run",
            category="execute",
            step_seq=5,
            status="completed",
            input_summary="sandbox.run()",
            output_summary="Passed",
        )
        step = execution_record_to_harness_step(record)
        assert step["step_id"] == "step_abc"
        assert step["run_id"] == "run-1"
        assert step["step_type"] == "tool_call"
        assert step["seq"] == 5
        assert step["metadata"]["tool_name"] == "sandbox.run"
        assert step["metadata"]["category"] == "execute"
        assert step["metadata"]["status"] == "completed"

    def test_input_summary_truncates(self):
        intent = ToolIntent(
            tool_name="test",
            arguments={"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
        )
        from app.services.studio_tool_runtime import _summarize_input
        summary = _summarize_input(intent)
        assert "(+" in summary  # should indicate truncation


# ── build_tool_error_patch — 拒绝工具返回合法 patch ───────────────────────────

class TestBuildToolErrorPatch:

    def test_denied_tool_returns_tool_error_patch(self):
        check = ToolCheckResponse(
            result=ToolCheckResult.DENIED_NOT_IN_CONTRACT,
            tool_name="sandbox.run",
            category=ToolCategory.EXECUTE,
            reason="not_in_allowed_tools",
            message="Tool not in whitelist",
        )
        patch = build_tool_error_patch(
            run_id="run-1",
            run_version=1,
            patch_seq=5,
            check_response=check,
            card_id="card_abc",
        )
        # patch_type 必须是 tool_error_patch（不是 error_patch）
        assert patch["patch_type"] == "tool_error_patch"
        # 信封完整性
        assert patch["public_run_id"] == "run-1"
        assert patch["run_version"] == 1
        assert patch["patch_seq"] == 5
        assert "patch_id" in patch
        assert "idempotency_key" in patch
        assert patch["target"] == "card_abc"
        # payload 字段名正确 — 前端从顶层读 tool_name/card_id
        payload = patch["payload"]
        assert payload["error_type"] == "not_in_allowed_tools"
        assert payload["message"] == "Tool not in whitelist"
        assert payload["retryable"] is False
        assert payload["tool_name"] == "sandbox.run"
        assert payload["category"] == "execute"
        assert payload["card_id"] == "card_abc"

    def test_denied_no_card_error_patch(self):
        check = ToolCheckResponse(
            result=ToolCheckResult.DENIED_NO_CARD,
            tool_name="skill_draft.stage_edit",
            category=ToolCategory.STAGE,
            reason="no_active_card",
            message="No active card",
        )
        patch = build_tool_error_patch(
            run_id="run-2",
            run_version=2,
            patch_seq=1,
            check_response=check,
        )
        assert patch["patch_type"] == "tool_error_patch"
        assert patch["payload"]["error_type"] == "no_active_card"
        assert patch["payload"]["tool_name"] == "skill_draft.stage_edit"


# ── build_tool_confirmation_patch ─────────────────────────────────────────────

class TestBuildToolConfirmationPatch:

    def test_confirmation_patch_structure(self):
        check = ToolCheckResponse(
            result=ToolCheckResult.NEEDS_CONFIRMATION,
            tool_name="staged_edit.adopt",
            category=ToolCategory.PUBLISH,
            reason="needs_human_confirmation",
            message="Needs confirmation",
            confirmation_prompt="Are you sure?",
        )
        patch = build_tool_confirmation_patch(
            run_id="run-1",
            run_version=1,
            patch_seq=3,
            check_response=check,
            card_id="card_1",
        )
        assert patch["patch_type"] == "tool_confirmation_patch"
        assert patch["target"] == "card_1"
        payload = patch["payload"]
        assert payload["tool_name"] == "staged_edit.adopt"
        assert payload["confirmation_prompt"] == "Are you sure?"
