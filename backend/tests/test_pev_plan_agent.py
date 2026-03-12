"""TC-PEV-PLAN: PlanAgent 单元测试。

覆盖：
1. 三个场景（intel / skill_chain / task_decomp）的计划生成
2. replan 调用
3. JSON 解析容错（markdown 代码块包裹）
4. 无效 JSON 抛出 ValueError
"""
import json
import pytest
from unittest.mock import AsyncMock, patch

from app.services.pev.plan_agent import PlanAgent, _parse_plan_json


# ─────────────────────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────────────────────

def _mock_db():
    from unittest.mock import MagicMock
    return MagicMock()


def _plan_response(steps: list[dict]) -> str:
    return json.dumps({"steps": steps})


# ─────────────────────────────────────────────────────────────────────────────
# _parse_plan_json
# ─────────────────────────────────────────────────────────────────────────────

class TestParsePlanJson:

    def test_plain_json(self):
        raw = json.dumps({"steps": [{"step_key": "a"}]})
        result = _parse_plan_json(raw)
        assert result["steps"][0]["step_key"] == "a"

    def test_markdown_json_block(self):
        raw = "```json\n{\"steps\": []}\n```"
        result = _parse_plan_json(raw)
        assert result["steps"] == []

    def test_markdown_plain_block(self):
        raw = "```\n{\"steps\": [{\"step_key\": \"x\"}]}\n```"
        result = _parse_plan_json(raw)
        assert result["steps"][0]["step_key"] == "x"

    def test_invalid_json_raises(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_plan_json("not json")


# ─────────────────────────────────────────────────────────────────────────────
# PlanAgent.generate_plan
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_STEPS = [
    {
        "step_key": "crawl_source",
        "step_type": "crawl",
        "description": "爬取目标页面",
        "depends_on": [],
        "input_spec": {"url": "https://example.com"},
        "output_spec": {},
        "verify_criteria": "新增情报条目数 >= 1",
    },
    {
        "step_key": "analyze_content",
        "step_type": "llm_generate",
        "description": "分析爬取内容",
        "depends_on": ["crawl_source"],
        "input_spec": {"prompt": "分析以下内容"},
        "output_spec": {},
        "verify_criteria": "输出不为空",
    },
]


class TestGeneratePlan:

    @pytest.mark.asyncio
    async def test_intel_scenario(self):
        agent = PlanAgent()
        mock_response = _plan_response(_SAMPLE_STEPS)

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(mock_response, {}))

            plan = await agent.generate_plan(
                goal="采集竞品最新动态",
                scenario="intel",
                context={},
                db=_mock_db(),
            )

        assert "steps" in plan
        assert len(plan["steps"]) == 2
        assert plan["steps"][0]["step_key"] == "crawl_source"

    @pytest.mark.asyncio
    async def test_skill_chain_scenario(self):
        agent = PlanAgent()
        steps = [
            {"step_key": "generate_report", "step_type": "skill_execute", "description": "生成报告",
             "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
            {"step_key": "make_ppt", "step_type": "tool_call", "description": "制作PPT",
             "depends_on": ["generate_report"], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
        ]
        mock_response = _plan_response(steps)

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(mock_response, {}))

            plan = await agent.generate_plan(
                goal="先生成市场分析报告再制作PPT",
                scenario="skill_chain",
                context={},
                db=_mock_db(),
            )

        assert plan["steps"][1]["step_key"] == "make_ppt"

    @pytest.mark.asyncio
    async def test_task_decomp_scenario(self):
        agent = PlanAgent()
        steps = [
            {"step_key": "create_design_task", "step_type": "sub_task", "description": "创建设计子任务",
             "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
        ]
        mock_response = _plan_response(steps)

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(mock_response, {}))

            plan = await agent.generate_plan(
                goal="将新品上市策划分解为执行任务",
                scenario="task_decomp",
                context={},
                db=_mock_db(),
            )

        assert plan["steps"][0]["step_type"] == "sub_task"

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self):
        agent = PlanAgent()

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=("not json at all", {}))

            with pytest.raises(ValueError, match="格式无效"):
                await agent.generate_plan("目标", "intel", {}, _mock_db())

    @pytest.mark.asyncio
    async def test_missing_steps_key_raises(self):
        agent = PlanAgent()

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(json.dumps({"result": "ok"}), {}))

            with pytest.raises(ValueError, match="缺少 steps"):
                await agent.generate_plan("目标", "intel", {}, _mock_db())


# ─────────────────────────────────────────────────────────────────────────────
# PlanAgent.replan
# ─────────────────────────────────────────────────────────────────────────────

class TestReplan:

    @pytest.mark.asyncio
    async def test_replan_generates_adjusted_plan(self):
        agent = PlanAgent()
        new_steps = [
            {"step_key": "retry_crawl", "step_type": "crawl", "description": "重试爬取（换策略）",
             "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
        ]
        mock_response = _plan_response(new_steps)

        original_plan = {
            "scenario": "intel",
            "goal": "采集竞品动态",
            "steps": _SAMPLE_STEPS,
        }
        failed_step = {"step_key": "crawl_source", "description": "爬取目标页面"}

        with patch("app.services.pev.plan_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(mock_response, {}))

            new_plan = await agent.replan(
                original_plan=original_plan,
                failed_step=failed_step,
                verify_feedback="爬取返回空内容",
                context={"analyze_content": {"ok": True}},
                db=_mock_db(),
            )

        assert new_plan["steps"][0]["step_key"] == "retry_crawl"
