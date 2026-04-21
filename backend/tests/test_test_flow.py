"""测试流后端主链路单元测试。

覆盖：
- resolve_test_flow_entry 各决策分支
- fork_case_plan 版本递增 + case 复制
- confirm_case_plan 状态流转
- decorate_session / decorate_history / decorate_report 装饰字段
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

# ── resolve_test_flow_entry ──────────────────────────────────────────────────


class TestResolveTestFlowEntry:
    """resolve_test_flow_entry 各分支测试。"""

    def _call(self, payload):
        from app.services.test_flow_trigger import resolve_test_flow_entry
        db = MagicMock()
        return resolve_test_flow_entry(db, payload)

    def test_no_intent_returns_chat_default(self):
        result = self._call({"content": "你好，今天天气怎么样"})
        assert result["action"] == "chat_default"
        assert result["reason"] == "missing_generate_case_intent"

    def test_no_skill_mention_returns_chat_default(self):
        result = self._call({
            "content": "帮我生成测试用例",
            "mentioned_skill_ids": [],
        })
        assert result["action"] == "chat_default"
        assert result["reason"] == "missing_skill_target"

    def test_multiple_skills_returns_pick_skill(self):
        result = self._call({
            "content": "帮我生成测试用例",
            "mentioned_skill_ids": [1, 2],
            "candidate_skills": [
                {"id": 1, "name": "Skill A"},
                {"id": 2, "name": "Skill B"},
            ],
        })
        assert result["action"] == "pick_skill"
        assert result["reason"] == "multiple_skill_targets"
        assert len(result["candidates"]) == 2

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_not_ready_returns_mount_blocked(self, mock_latest, mock_readiness):
        mock_readiness.return_value = {
            "ready": False,
            "blocking_issues": ["missing_confirmed_declaration"],
            "mount_cta": "complete_permission_declaration",
        }
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "mount_blocked"
        assert result["skill"]["id"] == 7
        assert "missing_confirmed_declaration" in result["blocking_issues"]
        assert result["mount_cta"] == "complete_permission_declaration"

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    @patch("app.services.test_flow_trigger.serialize_case_plan")
    def test_ready_with_plan_returns_choose_existing(self, mock_serialize, mock_latest, mock_readiness):
        mock_readiness.return_value = {"ready": True}
        plan = MagicMock()
        plan.id = 90
        mock_latest.return_value = plan
        mock_serialize.return_value = {
            "id": 90, "skill_id": 7, "plan_version": 3,
            "status": "generated", "case_count": 5,
            "focus_mode": "permission_minimal", "materialization": None,
        }

        result = self._call({
            "content": "@销售助手 帮我生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "choose_existing_plan"
        assert result["latest_plan"]["id"] == 90
        assert result["latest_plan"]["plan_version"] == 3

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_ready_without_plan_returns_generate(self, mock_latest, mock_readiness):
        mock_readiness.return_value = {"ready": True}
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "generate_cases"
        assert result["reason"] == "ready_without_existing_plan"

    def test_selected_skill_id_not_merged(self):
        """selected_skill_id 不应隐式合并到候选列表。"""
        result = self._call({
            "content": "帮我生成测试用例",
            "selected_skill_id": 5,
            "mentioned_skill_ids": [],
        })
        assert result["action"] == "chat_default"
        assert result["reason"] == "missing_skill_target"

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_not_ready_returns_case_generation_gate_blocked(self, mock_latest, mock_readiness):
        """missing_bound_assets 时返回门禁语义字段。"""
        mock_readiness.return_value = {
            "ready": False,
            "blocking_issues": ["missing_bound_assets"],
            "mount_cta": "mount_data_assets",
            "blocked_stage": "case_generation_gate",
            "blocked_before": "case_generation",
            "case_generation_allowed": False,
            "quality_evaluation_started": False,
            "verdict_label": "尚未开始质量检测",
            "verdict_reason": "前置条件未完成：未绑定可测试数据资产",
            "gate_summary": "需要先完成：未绑定可测试数据资产",
            "gate_reasons": [{"code": "missing_bound_assets", "title": "未绑定可测试数据资产", "detail": "Skill 未绑定任何数据表，无法生成测试用例。请先在治理面板绑定数据资产。", "severity": "critical", "step_id": "bind_assets", "action": "go_bound_assets"}],
            "guided_steps": [],
            "primary_action": "go_bound_assets",
        }
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "mount_blocked"
        assert result["reason"] == "case_generation_gate_blocked"
        assert result["blocked_stage"] == "case_generation_gate"
        assert result["blocked_before"] == "case_generation"
        assert result["case_generation_allowed"] is False
        assert result["quality_evaluation_started"] is False
        assert result["verdict_label"] == "尚未开始质量检测"
        assert result["primary_action"] == "go_bound_assets"
        assert len(result["gate_reasons"]) == 1
        assert result["gate_reasons"][0]["code"] == "missing_bound_assets"

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_missing_declaration_guides_to_declaration_step(self, mock_latest, mock_readiness):
        """missing_confirmed_declaration 时 primary_action 为 generate_declaration。"""
        mock_readiness.return_value = {
            "ready": False,
            "blocking_issues": ["missing_confirmed_declaration"],
            "mount_cta": "complete_permission_declaration",
            "blocked_stage": "case_generation_gate",
            "blocked_before": "case_generation",
            "case_generation_allowed": False,
            "quality_evaluation_started": False,
            "verdict_label": "尚未开始质量检测",
            "verdict_reason": "前置条件未完成：未确认权限声明",
            "gate_summary": "需要先完成：未确认权限声明",
            "gate_reasons": [{"code": "missing_confirmed_declaration", "title": "未确认权限声明", "detail": "权限声明尚未生成或确认。请先生成并采纳权限声明。", "severity": "critical", "step_id": "confirm_declaration", "action": "generate_declaration"}],
            "guided_steps": [],
            "primary_action": "generate_declaration",
        }
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "mount_blocked"
        assert result["primary_action"] == "generate_declaration"
        assert result["gate_reasons"][0]["step_id"] == "confirm_declaration"

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_table_governance_issues_grouped(self, mock_latest, mock_readiness):
        """多个表治理阻断原因正确分组。"""
        mock_readiness.return_value = {
            "ready": False,
            "blocking_issues": ["missing_skill_data_grant", "missing_table_permission_policy"],
            "mount_cta": "resolve_blocking_issues",
            "blocked_stage": "case_generation_gate",
            "blocked_before": "case_generation",
            "case_generation_allowed": False,
            "quality_evaluation_started": False,
            "verdict_label": "尚未开始质量检测",
            "verdict_reason": "前置条件未完成：数据表授权未配置、表权限策略未配置",
            "gate_summary": "需要先完成：数据表授权未配置、表权限策略未配置",
            "gate_reasons": [
                {"code": "missing_skill_data_grant", "title": "数据表授权未配置", "detail": "Skill 的数据表授权尚未配置，请在治理面板补齐。", "severity": "critical", "step_id": "complete_table_governance", "action": "go_readiness"},
                {"code": "missing_table_permission_policy", "title": "表权限策略未配置", "detail": "数据表的权限策略尚未配置，请在治理面板补齐。", "severity": "critical", "step_id": "complete_table_governance", "action": "go_readiness"},
            ],
            "guided_steps": [],
            "primary_action": "go_readiness",
        }
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "mount_blocked"
        assert len(result["gate_reasons"]) == 2
        assert result["gate_reasons"][0]["step_id"] == "complete_table_governance"
        assert result["gate_reasons"][1]["step_id"] == "complete_table_governance"
        assert result["primary_action"] == "go_readiness"

    @patch("app.services.test_flow_trigger.check_readiness")
    @patch("app.services.test_flow_trigger.latest_case_plan")
    def test_ready_allows_case_generation(self, mock_latest, mock_readiness):
        """readiness ready 时 case_generation_allowed=True, quality_evaluation_started=False。"""
        mock_readiness.return_value = {
            "ready": True,
            "blocking_issues": [],
            "blocked_stage": None,
            "blocked_before": None,
            "case_generation_allowed": True,
            "quality_evaluation_started": False,
            "verdict_label": "可生成测试用例",
            "verdict_reason": None,
            "gate_summary": None,
            "gate_reasons": [],
            "guided_steps": [],
            "primary_action": None,
        }
        mock_latest.return_value = None

        result = self._call({
            "content": "@销售助手 生成测试用例",
            "mentioned_skill_ids": [7],
            "candidate_skills": [{"id": 7, "name": "销售助手"}],
        })
        assert result["action"] == "generate_cases"
        assert "blocked_stage" not in result  # ready 路径不带门禁字段
        assert "case_generation_allowed" not in result  # ready 路径走 generate_cases，不带这些字段


# ── _build_case_generation_gate_details 单元测试 ─────────────────────────────


class TestBuildCaseGenerationGateDetails:
    """直接测试 _build_case_generation_gate_details 的 guided_steps 状态逻辑。"""

    def _call(self, blocking_issues):
        from app.services.test_flow_readiness import _build_case_generation_gate_details
        readiness = {"ready": False, "blocking_issues": blocking_issues}
        return _build_case_generation_gate_details(readiness)

    def test_missing_bound_assets_steps_blocked_then_todo(self):
        """第 1 步 blocked，后续步骤全部 todo。"""
        result = self._call(["missing_bound_assets"])
        steps = result["guided_steps"]
        assert len(steps) == 4
        assert steps[0]["id"] == "bind_assets"
        assert steps[0]["status"] == "blocked"
        assert steps[1]["status"] == "todo"
        assert steps[2]["status"] == "todo"
        assert steps[3]["status"] == "todo"

    def test_missing_declaration_first_step_done(self):
        """bind_assets 不阻断（done），confirm_declaration 阻断（blocked），后续 todo。"""
        result = self._call(["missing_confirmed_declaration"])
        steps = result["guided_steps"]
        assert steps[0]["id"] == "bind_assets"
        assert steps[0]["status"] == "done"
        assert steps[1]["id"] == "confirm_declaration"
        assert steps[1]["status"] == "blocked"
        assert steps[2]["status"] == "todo"
        assert steps[3]["status"] == "todo"

    def test_table_governance_blocked_middle(self):
        """只有 complete_table_governance 阻断时，前两步 done，第 3 步 blocked，第 4 步 todo。"""
        result = self._call(["missing_skill_data_grant"])
        steps = result["guided_steps"]
        assert steps[0]["status"] == "done"
        assert steps[1]["status"] == "done"
        assert steps[2]["id"] == "complete_table_governance"
        assert steps[2]["status"] == "blocked"
        assert steps[3]["status"] == "todo"

    def test_multiple_blocks_only_first_highlighted(self):
        """多个阻断只高亮第一个，后续即使 blocked 也标 todo。"""
        result = self._call(["missing_bound_assets", "missing_confirmed_declaration", "missing_skill_data_grant"])
        steps = result["guided_steps"]
        assert steps[0]["status"] == "blocked"  # bind_assets
        assert steps[1]["status"] == "todo"     # confirm_declaration (本身也 blocked 但排在后面)
        assert steps[2]["status"] == "todo"     # complete_table_governance
        assert steps[3]["status"] == "todo"     # refresh_governance

    def test_gate_reasons_sorted_by_priority(self):
        """gate_reasons 按优先级排序。"""
        result = self._call(["missing_skill_data_grant", "missing_bound_assets"])
        reasons = result["gate_reasons"]
        assert reasons[0]["code"] == "missing_bound_assets"
        assert reasons[1]["code"] == "missing_skill_data_grant"

    def test_primary_action_from_first_reason(self):
        """primary_action 取第一个 gate_reason 的 action。"""
        result = self._call(["missing_confirmed_declaration", "missing_skill_data_grant"])
        assert result["primary_action"] == "generate_declaration"

    def test_unknown_code_handled(self):
        """未知阻断 code 不崩溃，fallback 到默认值。"""
        result = self._call(["some_unknown_issue"])
        assert len(result["gate_reasons"]) == 1
        assert result["gate_reasons"][0]["code"] == "some_unknown_issue"
        assert result["gate_reasons"][0]["step_id"] == "unknown"

    def test_verdict_fields(self):
        """基本字段正确。"""
        result = self._call(["missing_bound_assets"])
        assert result["blocked_stage"] == "case_generation_gate"
        assert result["blocked_before"] == "case_generation"
        assert result["case_generation_allowed"] is False
        assert result["quality_evaluation_started"] is False
        assert result["verdict_label"] == "尚未开始质量检测"


# ── fork_case_plan ───────────────────────────────────────────────────────────


class TestForkCasePlan:

    def _make_plan(self, plan_id=10, skill_id=7, plan_version=2, case_count=3):
        from app.models.skill_governance import TestCasePlanDraft
        plan = TestCasePlanDraft()
        plan.id = plan_id
        plan.skill_id = skill_id
        plan.workspace_id = 1
        plan.bundle_id = 1
        plan.declaration_id = 1
        plan.plan_version = plan_version
        plan.skill_content_version = 5
        plan.governance_version = 6
        plan.permission_declaration_version = 2
        plan.status = "generated"
        plan.focus_mode = "permission_minimal"
        plan.max_cases = 12
        plan.case_count = case_count
        plan.blocking_issues_json = []
        plan.source_plan_id = None
        plan.generation_mode = None
        plan.entry_source = None
        plan.conversation_id = None
        plan.summary_json = None
        plan.confirmed_at = None
        plan.latest_materialized_session_id = None
        plan.last_used_at = None
        plan.created_by = 1
        return plan

    def _make_case(self, case_id, plan_id, status="generated"):
        from app.models.skill_governance import TestCaseDraft
        case = TestCaseDraft()
        case.id = case_id
        case.plan_id = plan_id
        case.skill_id = 7
        case.target_role_ref = "role_1"
        case.role_label = "销售"
        case.asset_ref = "asset_1"
        case.asset_name = "客户表"
        case.asset_type = "table"
        case.case_type = "positive"
        case.risk_tags_json = []
        case.prompt = "测试 prompt"
        case.expected_behavior = "应该通过"
        case.source_refs_json = []
        case.source_verification_status = None
        case.data_source_policy = None
        case.status = status
        case.granular_refs_json = []
        case.controlled_fields_json = []
        return case

    def test_fork_creates_new_version(self):
        from app.services.test_flow_cases import fork_case_plan
        db = MagicMock()
        source = self._make_plan(plan_id=10, plan_version=2)
        db.get.return_value = source

        # db.query() 会被调两次：
        # 1) 查 max plan_version → 返回 chain .filter.order_by.first → (3,)
        # 2) 查 cases → 返回 chain .filter.filter.order_by.all → cases list
        version_chain = MagicMock()
        version_chain.filter.return_value = version_chain
        version_chain.order_by.return_value = version_chain
        version_chain.first.return_value = (3,)

        cases_chain = MagicMock()
        cases_chain.filter.return_value = cases_chain
        cases_chain.order_by.return_value = cases_chain
        # mock 返回已过滤的 cases（真实 DB 中 discarded 会被 SQLAlchemy filter 排除）
        cases_chain.all.return_value = [
            self._make_case(101, 10),
            self._make_case(102, 10),
        ]

        db.query.side_effect = [version_chain, cases_chain]

        added_objects = []
        db.add.side_effect = lambda obj: added_objects.append(obj)
        db.flush.return_value = None

        new_plan = fork_case_plan(db, plan_id=10, mode="revise", user_id=1)

        assert new_plan.plan_version == 4  # max 3 + 1
        assert new_plan.source_plan_id == 10
        assert new_plan.generation_mode == "revise"
        assert new_plan.case_count == 2

    def test_fork_nonexistent_plan_raises(self):
        from app.services.test_flow_cases import fork_case_plan
        from app.api_envelope import ApiEnvelopeException
        db = MagicMock()
        db.get.return_value = None

        with pytest.raises(ApiEnvelopeException):
            fork_case_plan(db, plan_id=999, mode="revise", user_id=1)


# ── confirm_case_plan ────────────────────────────────────────────────────────


class TestConfirmCasePlan:

    def test_confirm_sets_confirmed_at(self):
        from app.services.test_flow_cases import confirm_case_plan
        from app.models.skill_governance import TestCasePlanDraft

        db = MagicMock()
        plan = TestCasePlanDraft()
        plan.id = 10
        plan.status = "generated"
        plan.confirmed_at = None
        plan.summary_json = None
        plan.skill_id = 7
        plan.plan_version = 2
        plan.case_count = 3
        plan.focus_mode = "permission_minimal"
        plan.source_plan_id = None
        plan.generation_mode = None
        db.get.return_value = plan
        db.flush.return_value = None

        result = confirm_case_plan(db, plan_id=10)
        assert result["status"] == "confirmed"
        assert result["confirmed_at"] is not None
        assert plan.confirmed_at is not None
        assert plan.status == "confirmed"

    def test_confirm_nonexistent_raises(self):
        from app.services.test_flow_cases import confirm_case_plan
        from app.api_envelope import ApiEnvelopeException
        db = MagicMock()
        db.get.return_value = None

        with pytest.raises(ApiEnvelopeException):
            confirm_case_plan(db, plan_id=999)


# ── decorate_* ───────────────────────────────────────────────────────────────


class TestDecorate:

    def _make_link(self, session_id, plan_id=10, plan_version=2, case_count=3):
        from app.models.test_flow import TestFlowRunLink
        link = TestFlowRunLink()
        link.session_id = session_id
        link.plan_id = plan_id
        link.plan_version = plan_version
        link.case_count = case_count
        link.entry_source = "sandbox_chat"
        link.decision_mode = "revise"
        link.conversation_id = 42
        return link

    def test_decorate_session_adds_fields(self):
        from app.services.test_flow_history import decorate_session
        db = MagicMock()
        link = self._make_link(501)
        db.query.return_value.filter.return_value.first.return_value = link

        result = decorate_session(db, {"session_id": 501, "target_id": 7})
        assert result["source_case_plan_id"] == 10
        assert result["source_case_plan_version"] == 2
        assert result["source_case_count"] == 3
        assert result["test_entry_source"] == "sandbox_chat"
        assert result["test_decision_mode"] == "revise"
        assert result["source_conversation_id"] == 42
        # 原始字段保留
        assert result["target_id"] == 7

    def test_decorate_session_no_link(self):
        from app.services.test_flow_history import decorate_session
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = decorate_session(db, {"session_id": 501})
        assert result["source_case_plan_id"] is None
        assert result["test_entry_source"] is None

    def test_decorate_history_batch(self):
        from app.services.test_flow_history import decorate_history
        db = MagicMock()
        link501 = self._make_link(501)
        db.query.return_value.filter.return_value.all.return_value = [link501]

        items = [
            {"session_id": 501, "name": "test1"},
            {"session_id": 502, "name": "test2"},
        ]
        result = decorate_history(db, items)
        assert result[0]["source_case_plan_id"] == 10
        assert result[1]["source_case_plan_id"] is None
        # 原始字段保留
        assert result[0]["name"] == "test1"
        assert result[1]["name"] == "test2"

    def test_decorate_report_adds_fields(self):
        from app.services.test_flow_history import decorate_report
        db = MagicMock()
        link = self._make_link(501)
        db.query.return_value.filter.return_value.first.return_value = link

        result = decorate_report(db, {"session_id": 501, "score": 85})
        assert result["source_case_plan_id"] == 10
        assert result["score"] == 85


# ── has_generate_case_intent ─────────────────────────────────────────────────


class TestIntentDetection:

    def test_chinese_intent(self):
        from app.services.test_flow_trigger import has_generate_case_intent
        assert has_generate_case_intent("帮我生成测试用例")
        assert has_generate_case_intent("给我生成 case")
        assert has_generate_case_intent("产出测试集")
        assert has_generate_case_intent("输出测试用例")

    def test_no_intent(self):
        from app.services.test_flow_trigger import has_generate_case_intent
        assert not has_generate_case_intent("你好")
        assert not has_generate_case_intent("查看测试结果")
        assert not has_generate_case_intent("修改测试用例")

    def test_english_case_insensitive(self):
        from app.services.test_flow_trigger import has_generate_case_intent
        assert has_generate_case_intent("帮我生成 Cases")
        assert has_generate_case_intent("给我出 CASES 吧")
