"""TC-PEV-VERIFY: VerifyAgent 单元测试。

覆盖：
1. Schema 校验（pass / fail 两种）
2. LLM 语义校验（mock LLM）
3. verify_step 综合校验
4. verify_final 全局验证
5. 无 verify_criteria 时的默认行为
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pev.verify_agent import VerifyAgent


def _mock_db():
    return MagicMock()


def _make_step(verify_criteria="", output_spec=None):
    return {
        "step_key": "test_step",
        "step_type": "llm_generate",
        "description": "测试步骤",
        "output_spec": output_spec or {},
        "verify_criteria": verify_criteria,
    }


def _make_result(ok=True, data=None, error=None):
    return {"ok": ok, "data": data, "error": error}


# ─────────────────────────────────────────────────────────────────────────────
# Schema 校验
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaVerify:

    def test_no_schema_always_passes(self):
        agent = VerifyAgent()
        errors = agent._schema_verify({"ok": True, "data": "any"}, None)
        assert errors == []

    def test_empty_schema_always_passes(self):
        agent = VerifyAgent()
        errors = agent._schema_verify({"ok": True, "data": "any"}, {})
        assert errors == []

    def test_valid_data_passes_schema(self):
        agent = VerifyAgent()
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        result = {"ok": True, "data": {"name": "test"}}
        errors = agent._schema_verify(result, schema)
        assert errors == []

    def test_invalid_data_fails_schema(self):
        agent = VerifyAgent()
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        result = {"ok": True, "data": {"name": "missing_count"}}
        errors = agent._schema_verify(result, schema)
        assert len(errors) > 0
        assert "Schema 校验失败" in errors[0]

    def test_none_result_fails(self):
        agent = VerifyAgent()
        schema = {"type": "object"}
        errors = agent._schema_verify(None, schema)
        assert len(errors) > 0


# ─────────────────────────────────────────────────────────────────────────────
# verify_step
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyStep:

    @pytest.mark.asyncio
    async def test_no_criteria_exec_success(self):
        """无 verify_criteria 且执行成功 → pass"""
        agent = VerifyAgent()
        step = _make_step(verify_criteria="")
        result = _make_result(ok=True, data={"content": "some text"})
        vr = await agent.verify_step(step, result, _mock_db())
        assert vr["pass"] is True

    @pytest.mark.asyncio
    async def test_no_criteria_exec_fail(self):
        """无 verify_criteria 但执行失败 → fail"""
        agent = VerifyAgent()
        step = _make_step(verify_criteria="")
        result = _make_result(ok=False, error="爬取超时")
        vr = await agent.verify_step(step, result, _mock_db())
        assert vr["pass"] is False

    @pytest.mark.asyncio
    async def test_schema_fail_short_circuits(self):
        """Schema 校验失败时，不调用 LLM，直接返回 fail。"""
        agent = VerifyAgent()
        schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
        step = _make_step(verify_criteria="新增数量 >= 1", output_spec=schema)
        result = _make_result(ok=True, data={"wrong_field": "x"})

        with patch.object(agent, "_llm_verify", new_callable=AsyncMock) as mock_llm:
            vr = await agent.verify_step(step, result, _mock_db())
            mock_llm.assert_not_called()

        assert vr["pass"] is False

    @pytest.mark.asyncio
    async def test_llm_verify_called_when_criteria(self):
        """有 verify_criteria 且 schema 通过时调用 LLM 校验。"""
        agent = VerifyAgent()
        step = _make_step(verify_criteria="内容长度 >= 100 字")
        result = _make_result(ok=True, data={"content": "a" * 150})

        with patch.object(agent, "_llm_verify", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"pass": True, "score": 90, "issues": [], "suggestion": ""}
            vr = await agent.verify_step(step, result, _mock_db())
            mock_llm.assert_called_once()

        assert vr["pass"] is True
        assert vr["score"] == 90

    @pytest.mark.asyncio
    async def test_llm_verify_fail_returns_suggestion(self):
        agent = VerifyAgent()
        step = _make_step(verify_criteria="必须包含竞品名称")
        result = _make_result(ok=True, data={"content": "短内容"})

        with patch.object(agent, "_llm_verify", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "pass": False, "score": 30,
                "issues": ["未提及竞品名称"],
                "suggestion": "请在内容中明确列出竞品名称",
            }
            vr = await agent.verify_step(step, result, _mock_db())

        assert vr["pass"] is False
        assert "未提及竞品名称" in vr["issues"]
        assert "竞品名称" in vr["suggestion"]


# ─────────────────────────────────────────────────────────────────────────────
# _llm_verify
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMVerify:

    @pytest.mark.asyncio
    async def test_llm_verify_parses_json(self):
        agent = VerifyAgent()
        llm_response = json.dumps({
            "pass": True, "score": 85, "issues": [], "suggestion": ""
        })
        with patch("app.services.pev.verify_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(llm_response, {}))
            result = await agent._llm_verify("描述", "标准", {"ok": True, "data": "x"}, _mock_db())

        assert result["pass"] is True
        assert result["score"] == 85

    @pytest.mark.asyncio
    async def test_llm_verify_fails_closed_on_error(self):
        """H7: LLM 调用失败时 fail-closed（默认不通过），防止 LLM 宕机时所有验证自动通过。"""
        agent = VerifyAgent()
        with patch("app.services.pev.verify_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(side_effect=Exception("API error"))
            result = await agent._llm_verify("描述", "标准", None, _mock_db())

        assert result["pass"] is False  # H7: fail-closed
        assert result["score"] == 0

    @pytest.mark.asyncio
    async def test_no_criteria_skips_llm(self):
        agent = VerifyAgent()
        with patch("app.services.pev.verify_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock()
            result = await agent._llm_verify("描述", "", {"ok": True, "data": "x"}, _mock_db())
            mock_gw.chat.assert_not_called()

        assert result["pass"] is True


# ─────────────────────────────────────────────────────────────────────────────
# verify_final
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyFinal:

    @pytest.mark.asyncio
    async def test_verify_final_passes(self):
        agent = VerifyAgent()
        job = MagicMock()
        job.goal = "采集并分析竞品动态"
        job.scenario = "intel"

        llm_response = json.dumps({
            "pass": True, "score": 92, "issues": [], "summary": "任务圆满完成"
        })
        with patch("app.services.pev.verify_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(return_value=(llm_response, {}))

            result = await agent.verify_final(
                job,
                {"step1": {"ok": True, "data": "content"}, "step2": {"ok": True, "data": "report"}},
                _mock_db(),
            )

        assert result["pass"] is True
        assert result["score"] == 92
        assert "圆满" in result["summary"]

    @pytest.mark.asyncio
    async def test_verify_final_fails_closed_on_error(self):
        """H7: 最终验证异常时 fail-closed。"""
        agent = VerifyAgent()
        job = MagicMock()
        job.goal = "目标"
        job.scenario = "intel"

        with patch("app.services.pev.verify_agent.llm_gateway") as mock_gw:
            mock_gw.get_lite_config.return_value = {"model_id": "deepseek-chat", "max_tokens": 512}
            mock_gw.chat = AsyncMock(side_effect=Exception("timeout"))

            result = await agent.verify_final(job, {}, _mock_db())

        assert result["pass"] is False  # H7: fail-closed
        assert result["score"] == 0
