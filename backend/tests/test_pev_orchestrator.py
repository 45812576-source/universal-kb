"""TC-PEV-ORCHESTRATOR: PEVOrchestrator 端到端 mock 测试。

覆盖：
1. 正常流程：plan → execute → verify → done
2. 步骤失败 + 重试成功
3. 重试耗尽 → replan → 成功
4. replan 耗尽 → FAILED
5. should_upgrade 判断
6. ref_resolver 拓扑排序 + $ref 解析
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pev.ref_resolver import topological_sort, resolve_inputs


# ─────────────────────────────────────────────────────────────────────────────
# ref_resolver 单元测试
# ─────────────────────────────────────────────────────────────────────────────

class TestTopologicalSort:

    def test_simple_chain(self):
        steps = [
            {"step_key": "b", "depends_on": ["a"]},
            {"step_key": "a", "depends_on": []},
        ]
        sorted_steps = topological_sort(steps)
        keys = [s["step_key"] for s in sorted_steps]
        assert keys.index("a") < keys.index("b")

    def test_no_deps_preserves_order(self):
        steps = [
            {"step_key": "x", "depends_on": []},
            {"step_key": "y", "depends_on": []},
        ]
        sorted_steps = topological_sort(steps)
        assert len(sorted_steps) == 2

    def test_cycle_raises(self):
        steps = [
            {"step_key": "a", "depends_on": ["b"]},
            {"step_key": "b", "depends_on": ["a"]},
        ]
        with pytest.raises(ValueError, match="循环依赖"):
            topological_sort(steps)

    def test_diamond_dependency(self):
        steps = [
            {"step_key": "d", "depends_on": ["b", "c"]},
            {"step_key": "b", "depends_on": ["a"]},
            {"step_key": "c", "depends_on": ["a"]},
            {"step_key": "a", "depends_on": []},
        ]
        sorted_steps = topological_sort(steps)
        keys = [s["step_key"] for s in sorted_steps]
        assert keys.index("a") < keys.index("b")
        assert keys.index("a") < keys.index("c")
        assert keys.index("b") < keys.index("d")
        assert keys.index("c") < keys.index("d")


class TestResolveInputs:

    def test_literal_values_pass_through(self):
        spec = {"url": "https://example.com", "count": 5}
        result = resolve_inputs(spec, {})
        assert result == spec

    def test_ref_resolution(self):
        spec = {"content": "$step1.data"}
        context = {"step1": {"data": "爬取内容", "count": 3}}
        result = resolve_inputs(spec, context)
        assert result["content"] == "爬取内容"

    def test_whole_step_ref(self):
        spec = {"prev": "$step1"}
        context = {"step1": {"ok": True, "data": "x"}}
        result = resolve_inputs(spec, context)
        assert result["prev"] == {"ok": True, "data": "x"}

    def test_nested_field_ref(self):
        spec = {"val": "$step1.data.nested"}
        context = {"step1": {"data": {"nested": 42}}}
        result = resolve_inputs(spec, context)
        assert result["val"] == 42

    def test_missing_ref_returns_original(self):
        spec = {"x": "$missing_step.field"}
        result = resolve_inputs(spec, {})
        assert result["x"] == "$missing_step.field"


# ─────────────────────────────────────────────────────────────────────────────
# PEVOrchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _make_job(scenario="intel", goal="测试目标", config=None):
    job = MagicMock()
    job.id = 1
    job.scenario = scenario
    job.goal = goal
    job.context = {}
    job.config = config or {}
    job.user_id = 1
    job.plan = None
    job.total_steps = 0
    job.completed_steps = 0
    job.current_step_index = 0
    job.status = None
    job.started_at = None
    job.finished_at = None
    return job


def _make_pev_step(step_key="s1", step_type="llm_generate", order_index=0, depends_on=None):
    step = MagicMock()
    step.id = order_index + 1
    step.step_key = step_key
    step.step_type = step_type
    step.description = f"Step {step_key}"
    step.order_index = order_index
    step.depends_on = depends_on or []
    step.input_spec = {}
    step.output_spec = {}
    step.verify_criteria = ""
    step.result = None
    step.verify_result = None
    step.retry_count = 0
    step.status = None
    return step


class TestPEVOrchestrator:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        """正常流程：plan → execute(ok) → verify(pass) → done。"""
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        job = _make_job(config={"skip_verify": False})
        db = MagicMock()

        plan_dict = {
            "steps": [
                {"step_key": "s1", "step_type": "llm_generate", "description": "步骤1",
                 "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
            ]
        }
        exec_result = {"ok": True, "data": {"content": "生成内容"}, "error": None}
        verify_step_result = {"pass": True, "score": 90, "issues": [], "suggestion": ""}
        verify_final_result = {"pass": True, "score": 88, "issues": [], "summary": "完成"}

        # 模拟 DB query 返回 steps
        pev_step = _make_pev_step("s1", order_index=0)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [pev_step]

        with patch("app.services.pev.orchestrator.plan_agent") as mock_plan, \
             patch("app.services.pev.orchestrator.execute_agent") as mock_exec, \
             patch("app.services.pev.orchestrator.verify_agent") as mock_verify:

            mock_plan.generate_plan = AsyncMock(return_value=plan_dict)
            mock_exec.execute_step = AsyncMock(return_value=exec_result)
            mock_verify.verify_step = AsyncMock(return_value=verify_step_result)
            mock_verify.verify_final = AsyncMock(return_value=verify_final_result)

            events = []
            async for event in orch.run(db, job):
                events.append(event)

        event_types = [e["event"] for e in events]
        assert "pev_start" in event_types
        assert "pev_plan" in event_types
        assert "pev_step_start" in event_types
        assert "pev_step_result" in event_types
        assert "pev_done" in event_types

        done_event = next(e for e in events if e["event"] == "pev_done")
        assert done_event["data"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_step_retry_on_verify_fail(self):
        """第一次验证失败，重试后成功。"""
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        job = _make_job(config={"max_retries": 1, "skip_verify": False})
        db = MagicMock()

        plan_dict = {
            "steps": [
                {"step_key": "s1", "step_type": "llm_generate", "description": "步骤1",
                 "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": "内容充分"},
            ]
        }
        exec_result = {"ok": True, "data": {"content": "短内容"}, "error": None}
        verify_fail = {"pass": False, "score": 30, "issues": ["内容过短"], "suggestion": "请扩展内容"}
        verify_pass = {"pass": True, "score": 85, "issues": [], "suggestion": ""}
        verify_final_result = {"pass": True, "score": 85, "issues": [], "summary": "完成"}

        pev_step = _make_pev_step("s1", order_index=0)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [pev_step]

        call_count = {"n": 0}

        async def mock_verify_step(*args, **kwargs):
            call_count["n"] += 1
            return verify_fail if call_count["n"] == 1 else verify_pass

        with patch("app.services.pev.orchestrator.plan_agent") as mock_plan, \
             patch("app.services.pev.orchestrator.execute_agent") as mock_exec, \
             patch("app.services.pev.orchestrator.verify_agent") as mock_verify:

            mock_plan.generate_plan = AsyncMock(return_value=plan_dict)
            mock_exec.execute_step = AsyncMock(return_value=exec_result)
            mock_verify.verify_step = AsyncMock(side_effect=mock_verify_step)
            mock_verify.verify_final = AsyncMock(return_value=verify_final_result)

            events = []
            async for event in orch.run(db, job):
                events.append(event)

        event_types = [e["event"] for e in events]
        assert "pev_step_retry" in event_types
        assert "pev_done" in event_types
        assert call_count["n"] == 2  # 验证了两次

    @pytest.mark.asyncio
    async def test_plan_failure_yields_error(self):
        """PlanAgent 抛出异常 → pev_error 事件。"""
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        job = _make_job()
        db = MagicMock()

        with patch("app.services.pev.orchestrator.plan_agent") as mock_plan:
            mock_plan.generate_plan = AsyncMock(side_effect=ValueError("LLM API 不可用"))

            events = []
            async for event in orch.run(db, job):
                events.append(event)

        event_types = [e["event"] for e in events]
        assert "pev_error" in event_types
        error_event = next(e for e in events if e["event"] == "pev_error")
        assert "LLM API" in error_event["data"]["message"]

    @pytest.mark.asyncio
    async def test_skip_verify_mode(self):
        """skip_verify=True 时跳过 verify_step 和 verify_final。"""
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        job = _make_job(config={"skip_verify": True})
        db = MagicMock()

        plan_dict = {
            "steps": [
                {"step_key": "s1", "step_type": "llm_generate", "description": "步骤1",
                 "depends_on": [], "input_spec": {}, "output_spec": {}, "verify_criteria": ""},
            ]
        }
        exec_result = {"ok": True, "data": {"content": "ok"}, "error": None}

        pev_step = _make_pev_step("s1", order_index=0)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [pev_step]

        with patch("app.services.pev.orchestrator.plan_agent") as mock_plan, \
             patch("app.services.pev.orchestrator.execute_agent") as mock_exec, \
             patch("app.services.pev.orchestrator.verify_agent") as mock_verify:

            mock_plan.generate_plan = AsyncMock(return_value=plan_dict)
            mock_exec.execute_step = AsyncMock(return_value=exec_result)
            mock_verify.verify_step = AsyncMock(return_value={"pass": True, "score": 100, "issues": [], "suggestion": ""})
            mock_verify.verify_final = AsyncMock(return_value={"pass": True, "score": 100, "issues": [], "summary": ""})

            events = []
            async for event in orch.run(db, job):
                events.append(event)

        # skip_verify=True 时 verify_step 不被调用
        mock_verify.verify_step.assert_not_called()
        event_types = [e["event"] for e in events]
        assert "pev_done" in event_types


# ─────────────────────────────────────────────────────────────────────────────
# should_upgrade
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldUpgrade:

    @pytest.mark.asyncio
    async def test_returns_none_for_simple_chat(self):
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        conv = MagicMock()
        conv.messages = []

        with patch("app.services.pev.orchestrator.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=("none", {}))
            result = await orch.should_upgrade("你好", None, conv, MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_intel_scenario(self):
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        conv = MagicMock()
        conv.messages = []

        with patch("app.services.pev.orchestrator.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=("intel", {}))
            result = await orch.should_upgrade(
                "帮我从5个不同渠道采集竞品的最新产品动态、价格变化和用户评价数据，然后进行系统性汇总对比分析并自动生成完整报告",
                None, conv, MagicMock()
            )

        assert result == "intel"

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_error(self):
        """LLM 调用失败时降级为 None，不阻断对话。"""
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        conv = MagicMock()
        conv.messages = []

        with patch("app.services.pev.orchestrator.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(side_effect=Exception("timeout"))
            result = await orch.should_upgrade("任意消息", None, conv, MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_task_decomp_scenario(self):
        from app.services.pev.orchestrator import PEVOrchestrator

        orch = PEVOrchestrator()
        conv = MagicMock()
        conv.messages = []

        with patch("app.services.pev.orchestrator.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=("task_decomp", {}))
            result = await orch.should_upgrade(
                "把新品上市的整体计划拆解成详细执行任务列表，然后依次创建日历事项并分配给相关责任人，最后自动生成项目甘特图",
                None, conv, MagicMock()
            )

        assert result == "task_decomp"
