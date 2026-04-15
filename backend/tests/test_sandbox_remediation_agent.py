import pytest
from unittest.mock import AsyncMock, patch

from tests.conftest import _make_dept, _make_model_config, _make_skill, _make_user

from app.models.sandbox import SandboxTestReport, SandboxTestSession, SessionStatus, SessionStep
from app.models.skill import SkillVersion


class TestSandboxRemediationAgent:
    @pytest.mark.asyncio
    async def test_generate_remediation_plan_parses_llm_edits(self, db):
        from app.services.sandbox_remediation_agent import generate_remediation_plan

        dept = _make_dept(db, "整改Agent部门")
        user = _make_user(db, "sandbox_remediation_agent_user", dept_id=dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="整改Agent测试Skill")
        version = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.version.desc())
            .first()
        )
        version.system_prompt = "你是测试助手。\n请先分析，再给结论。"
        db.commit()

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
        )
        db.add(session)
        db.flush()

        report = SandboxTestReport(
            session_id=session.id,
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={
                "issues": [{
                    "issue_id": "issue_1",
                    "severity": "major",
                    "reason": "输出缺少明确结论",
                }],
                "fix_plan_structured": [{
                    "id": "fix_1",
                    "title": "补齐结论型输出结构",
                    "priority": "p1",
                    "problem_ids": ["issue_1"],
                    "action_type": "fix_prompt_logic",
                    "target_kind": "skill_prompt",
                    "target_ref": "SKILL.md",
                    "suggested_changes": "增加先结论后依据模板",
                    "acceptance_rule": "首段必须给出结论",
                    "retest_scope": ["all"],
                    "estimated_gain": "提升可行动性",
                }],
            },
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
            report_hash="sandbox-remediation-agent-test",
        )
        db.add(report)
        db.commit()

        llm_response = """
        {
          "tasks": [
            {
              "task_id": "task_1",
              "title": "补齐结论型输出结构",
              "priority": "p0",
              "action_type": "fix_prompt_logic",
              "target_kind": "skill_prompt",
              "target_ref": "SKILL.md",
              "problem_ids": ["issue_1"],
              "suggested_changes": "增加先结论后依据模板",
              "acceptance_rule": "首段必须给出结论",
              "retest_scope": ["all"],
              "estimated_gain": "提升可行动性"
            }
          ],
          "edits": [
            {
              "task_id": "task_1",
              "target_type": "system_prompt",
              "summary": "将输出结构改成先结论后依据",
              "risk_level": "high",
              "diff_ops": [
                {
                  "op": "replace",
                  "old": "请先分析，再给结论。",
                  "new": "请先给结论，再给依据。"
                }
              ]
            }
          ]
        }
        """

        with patch(
            "app.services.sandbox_remediation_agent._read_source_files",
            return_value="## example\n示例内容",
        ), patch(
            "app.services.sandbox_remediation_agent._collect_knowledge_refs",
            return_value=[],
        ), patch(
            "app.services.sandbox_remediation_agent.llm_gateway.chat",
            new=AsyncMock(return_value=(llm_response, {"model_id": "deepseek-chat"})),
        ):
            result = await generate_remediation_plan(db, skill.id, report)

        assert len(result.tasks) == 1
        assert result.tasks[0]["title"] == "补齐结论型输出结构"
        assert len(result.staged_edits) == 1
        assert result.staged_edits[0]["target_type"] == "system_prompt"
        assert result.staged_edits[0]["diff_ops"] == [{
            "op": "replace",
            "old": "请先分析，再给结论。",
            "new": "请先给结论，再给依据。",
        }]
        assert len(result.cards) == 1
