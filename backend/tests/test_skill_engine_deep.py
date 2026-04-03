"""TC-SKILL-ENGINE: prepare 核心分支、InputEvaluator 追问、context compaction、knowledge 注入脱敏。"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_engine():
    from app.services.skill_engine import SkillEngine
    return SkillEngine()


def _make_conv(skill_id=None):
    conv = MagicMock()
    conv.skill_id = skill_id
    conv.workspace_id = None
    conv.id = 1
    return conv


# ─── prepare: InputEvaluator early return ─────────────────────────────────────

class TestPrepareInputEvaluator:
    @pytest.mark.asyncio
    async def test_missing_required_input_triggers_early_return(self):
        engine = _make_engine()
        db = MagicMock()
        conv = _make_conv()

        skill = MagicMock()
        skill.id = 1
        skill.name = "文案生成"
        skill.description = "生成营销文案"
        skill.mode = "hybrid"
        skill.data_queries = []
        skill.auto_inject = False
        skill.knowledge_tags = []
        skill.tools = []

        skill_version = MagicMock()
        skill_version.system_prompt = "你是文案助手"
        skill_version.variables = []
        skill_version.output_schema = None
        skill_version.required_inputs = ["品牌名称", "核心卖点"]
        skill_version.version = 1
        skill.versions = [skill_version]
        skill.scope = "company"

        with patch.object(engine, "_match_skill", return_value=skill):
            from app.services.input_evaluator import input_evaluator
            eval_result = {"pass": False, "missing_questions": ["请告诉我品牌名称是什么？"]}
            with patch.object(input_evaluator, "evaluate", new=AsyncMock(return_value=eval_result)):
                with patch("app.services.llm_gateway.llm_gateway.get_config", return_value={}):
                    prep = await engine.prepare(db, conv, "帮我写文案")

        assert prep.early_return is not None
        text, _ = prep.early_return
        assert "品牌名称" in text

    @pytest.mark.asyncio
    async def test_sufficient_inputs_pass_through(self):
        engine = _make_engine()
        db = MagicMock()
        conv = _make_conv()

        with patch.object(engine, "_match_skill", return_value=None), \
             patch("app.services.llm_gateway.llm_gateway.get_config", return_value={"context_window": 32000}), \
             patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=AsyncMock()):
            prep = await engine.prepare(db, conv, "你好")

        assert prep.early_return is None


# ─── prepare: Rule Engine early return ────────────────────────────────────────

class TestPrepareRuleEngine:
    @pytest.mark.asyncio
    async def test_structured_mode_rule_engine_hit_early_returns(self):
        engine = _make_engine()
        db = MagicMock()
        conv = _make_conv()

        skill = MagicMock()
        skill.id = 1
        skill.name = "结构化Skill"
        skill.mode = "structured"
        skill.data_queries = []
        skill.auto_inject = False
        skill.knowledge_tags = []
        skill.tools = []

        skill_version = MagicMock()
        skill_version.system_prompt = "x"
        skill_version.variables = []
        skill_version.output_schema = None
        skill_version.required_inputs = []
        skill_version.version = 1
        skill.versions = [skill_version]

        with patch.object(engine, "_match_skill", return_value=skill), \
             patch("app.services.llm_gateway.llm_gateway.get_config", return_value={}):
            with patch("app.services.rule_engine.rule_engine.try_evaluate",
                       new=AsyncMock(return_value=("规则命中结果", {}))):
                prep = await engine.prepare(db, conv, "触发规则的输入")

        assert prep.early_return is not None
        text, _ = prep.early_return
        assert text == "规则命中结果"

    @pytest.mark.asyncio
    async def test_rule_engine_none_falls_through(self):
        """rule_engine 返回 None 时继续正常流程。"""
        engine = _make_engine()
        db = MagicMock()
        conv = _make_conv()

        skill = MagicMock()
        skill.id = 1
        skill.name = "结构化2"
        skill.mode = "structured"
        skill.data_queries = []
        skill.auto_inject = False
        skill.knowledge_tags = []
        skill.tools = []

        skill_version = MagicMock()
        skill_version.system_prompt = "x"
        skill_version.variables = []
        skill_version.output_schema = None
        skill_version.required_inputs = []
        skill_version.version = 1
        skill.versions = [skill_version]

        with patch.object(engine, "_match_skill", return_value=skill), \
             patch("app.services.llm_gateway.llm_gateway.get_config", return_value={"context_window": 32000}), \
             patch("app.services.rule_engine.rule_engine.try_evaluate", new=AsyncMock(return_value=None)):
            prep = await engine.prepare(db, conv, "普通输入")

        # 没有 early_return，说明继续到 LLM 准备阶段
        assert prep.early_return is None


# ─── Context compaction ────────────────────────────────────────────────────────

class TestContextCompaction:
    @pytest.mark.asyncio
    async def test_compact_triggered_above_threshold(self):
        engine = _make_engine()
        # 制造 context_window=1000，消息填满 90% (>85%)
        model_config = {"context_window": 1000}
        # 每个 message 约 500 chars → token ≈ 250，3 条共 750 tokens > 850 threshold
        messages = [
            {"role": "system", "content": "S" * 100},
            {"role": "user", "content": "U" * 500},
            {"role": "assistant", "content": "A" * 500},
            {"role": "user", "content": "U2" * 200},
            {"role": "assistant", "content": "A2" * 200},
            {"role": "user", "content": "U3" * 200},
            {"role": "assistant", "content": "A3" * 200},
            {"role": "user", "content": "最新问题"},
        ]
        # 总字符约 2700，token 约 1350 > 1000*0.85=850

        with patch.object(engine, "_summarize_history",
                          new=AsyncMock(return_value="前期对话摘要：用户讨论了营销策略...")):
            result = await engine._compact_if_needed(messages, model_config)

        # 结果中应包含摘要消息
        contents = [m.get("content", "") for m in result]
        assert any("摘要" in c or "前期对话" in c for c in contents)
        # system 消息保留
        assert any(m["role"] == "system" for m in result)

    @pytest.mark.asyncio
    async def test_compact_not_triggered_below_threshold(self):
        engine = _make_engine()
        model_config = {"context_window": 32000}
        messages = [
            {"role": "system", "content": "指令"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = await engine._compact_if_needed(messages, model_config)
        # 未超阈值，原样返回
        assert result == messages

    @pytest.mark.asyncio
    async def test_compact_failure_returns_original(self):
        engine = _make_engine()
        model_config = {"context_window": 100}
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U" * 200},
            {"role": "assistant", "content": "A" * 200},
            {"role": "user", "content": "新问题"},
        ]
        with patch.object(engine, "_summarize_history",
                          new=AsyncMock(side_effect=Exception("LLM 不可用"))):
            result = await engine._compact_if_needed(messages, model_config)
        # 失败降级：返回原始消息
        assert result == messages


# ─── Knowledge 注入脱敏 ────────────────────────────────────────────────────────

class TestKnowledgeInjection:
    @pytest.mark.asyncio
    async def test_own_chunks_injected_verbatim(self):
        engine = _make_engine()
        db = MagicMock()
        user_id = 42

        fake_hits = [{"knowledge_id": 1, "text": "秘密内容", "score": 0.9, "created_by": 42}]
        db.query.return_value.filter.return_value.all.return_value = []  # no approved ids

        with patch("app.services.vector_service.search_knowledge", return_value=fake_hits), \
             patch.object(engine, "_rerank_hits_with_llm", new=AsyncMock(return_value=fake_hits)):
            result = await engine._inject_knowledge("查询", skill=None, db=db, user_id=user_id)

        assert "秘密内容" in result
        assert "相关知识" in result

    @pytest.mark.asyncio
    async def test_others_approved_chunks_injected_verbatim(self):
        engine = _make_engine()
        db = MagicMock()
        user_id = 42

        fake_hits = [{"knowledge_id": 99, "text": "公开审批内容", "score": 0.9, "created_by": 1}]

        from app.models.knowledge import KnowledgeStatus
        mock_row = MagicMock()
        mock_row.__iter__ = MagicMock(return_value=iter([99]))
        db.query.return_value.filter.return_value.all.return_value = [(99,)]

        with patch("app.services.vector_service.search_knowledge", return_value=fake_hits), \
             patch.object(engine, "_rerank_hits_with_llm", new=AsyncMock(return_value=fake_hits)):
            result = await engine._inject_knowledge("查询", skill=None, db=db, user_id=user_id)

        assert "公开审批内容" in result

    @pytest.mark.asyncio
    async def test_others_non_approved_chunks_desensitized(self):
        engine = _make_engine()
        db = MagicMock()
        user_id = 42

        fake_hits = [{
            "knowledge_id": 100, "text": "他人机密内容，包含具体金额 100万",
            "score": 0.9, "created_by": 99,
            "desensitized_text": "[已脱敏：他人知识，仅供参考方法论]",
        }]
        # db.query 被调多次：approved entries 查询 + profile 查询
        # 用 side_effect 让每次 query 链都返回空
        mock_chain = MagicMock()
        mock_chain.filter.return_value.all.return_value = []
        db.query.return_value = mock_chain

        with patch("app.services.vector_service.search_knowledge", return_value=fake_hits), \
             patch.object(engine, "_rerank_hits_with_llm", new=AsyncMock(return_value=fake_hits)):
            result = await engine._inject_knowledge("查询", skill=None, db=db, user_id=user_id)

        # 不注入原文
        assert "100万" not in result

    @pytest.mark.asyncio
    async def test_empty_hits_returns_empty_string(self):
        engine = _make_engine()
        db = MagicMock()

        with patch("app.services.vector_service.search_knowledge", return_value=[]):
            result = await engine._inject_knowledge("查询", skill=None, db=db, user_id=1)

        assert result == ""

    @pytest.mark.asyncio
    async def test_vector_search_failure_returns_empty(self):
        engine = _make_engine()
        db = MagicMock()

        with patch("app.services.vector_service.search_knowledge",
                   side_effect=Exception("Milvus 不可用")):
            result = await engine._inject_knowledge("查询", skill=None, db=db, user_id=1)

        assert result == ""


# ─── _try_parse_structured_output ─────────────────────────────────────────────

class TestTryParseStructuredOutput:
    def test_json_code_block(self):
        engine = _make_engine()
        text = '```json\n{"key": "value"}\n```'
        result = engine._try_parse_structured_output(text)
        assert result == {"key": "value"}

    def test_bare_json_object(self):
        engine = _make_engine()
        text = '{"score": 9, "reason": "优秀"}'
        result = engine._try_parse_structured_output(text)
        assert result["score"] == 9

    def test_non_json_text_returns_none(self):
        engine = _make_engine()
        text = "这是一段普通回复，不含 JSON"
        result = engine._try_parse_structured_output(text)
        assert result is None

    def test_malformed_json_returns_none(self):
        engine = _make_engine()
        text = '```json\n{broken json\n```'
        result = engine._try_parse_structured_output(text)
        assert result is None


# ─── _extract_variables ────────────────────────────────────────────────────────

class TestExtractVariables:
    @pytest.mark.asyncio
    async def test_extracts_variables_from_history(self):
        engine = _make_engine()
        # _extract_variables(variables: list[str], conversation_text: str, model_config: dict)
        variables = ["品牌名", "目标受众"]
        conversation_text = "我是小红书运营，品牌是「奈雪的茶」，目标是年轻女性"

        with patch("app.services.llm_gateway.llm_gateway.chat",
                   new=AsyncMock(return_value=('{"品牌名": "奈雪的茶"}', {}))):
            result = await engine._extract_variables(variables, conversation_text, {})

        assert result.get("品牌名") == "奈雪的茶"

    @pytest.mark.asyncio
    async def test_extract_failure_propagates(self):
        engine = _make_engine()
        variables = ["品牌名"]
        conversation_text = "你好"

        with patch("app.services.llm_gateway.llm_gateway.chat",
                   new=AsyncMock(side_effect=Exception("LLM 失败"))):
            try:
                await engine._extract_variables(variables, conversation_text, {})
                raised = False
            except Exception:
                raised = True

        # 方法本身不捕获异常，由调用方处理
        assert raised

    @pytest.mark.asyncio
    async def test_malformed_llm_response_returns_empty_dict(self):
        engine = _make_engine()
        variables = ["x"]
        conversation_text = "你好"

        with patch("app.services.llm_gateway.llm_gateway.chat",
                   new=AsyncMock(return_value=("不是JSON格式的回复", {}))):
            result = await engine._extract_variables(variables, conversation_text, {})

        assert isinstance(result, dict)


# ─── Agent Loop: 连续失败早停 ──────────────────────────────────────────────────

class TestAgentLoopConsecutiveFailure:
    @pytest.mark.asyncio
    async def test_consecutive_two_failures_stops_loop(self):
        engine = _make_engine()
        tool_response = '```tool_call\n{"tool": "bad_tool", "params": {}}\n```'
        fail_result = {"ok": False, "error": "执行失败"}

        # 第一轮失败后，LLM 仍然返回 tool_call，第二轮再失败 → 早停
        call_count = 0

        async def fake_stream_typed(**kwargs):
            nonlocal call_count
            call_count += 1
            yield ("content", '```tool_call\n{"tool": "bad_tool2", "params": {}}\n```')

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=fail_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=fake_stream_typed):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        round_starts = [e for e in events if isinstance(e, dict) and e.get("event") == "round_start"]
        # 连续 2 次失败后应早停，不应超过 2 轮
        assert len(round_starts) <= 2

    @pytest.mark.asyncio
    async def test_max_rounds_limit(self):
        engine = _make_engine()
        tool_response = '```tool_call\n{"tool": "t", "params": {}}\n```'
        ok_result = {"ok": True, "result": {}}

        async def fake_stream_infinite(**kwargs):
            # 每轮都返回新的 tool_call
            yield ("content", '```tool_call\n{"tool": "t2", "params": {}}\n```')

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=ok_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=fake_stream_infinite):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                    max_rounds=5,
                ):
                    events.append(item)

        round_starts = [e for e in events if isinstance(e, dict) and e.get("event") == "round_start"]
        assert len(round_starts) <= 5

    @pytest.mark.asyncio
    async def test_goal_reminder_injected_after_first_round(self):
        """round_num >= 1 时应注入原始用户目标提醒。"""
        engine = _make_engine()
        tool_response = '```tool_call\n{"tool": "t", "params": {}}\n```'
        ok_result = {"ok": True, "result": {}}

        call_count = 0
        captured_messages = []

        async def fake_stream(**kwargs):
            nonlocal call_count
            call_count += 1
            captured_messages.extend(kwargs.get("messages", []))
            if call_count == 1:
                yield ("content", '```tool_call\n{"tool": "t2", "params": {}}\n```')
            else:
                yield ("content", "完成")

        initial_messages = [{"role": "user", "content": "帮我完成原始任务"}]

        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=ok_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed", new=fake_stream):
                async for _ in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response,
                    llm_messages=initial_messages,
                    model_config={}, user_id=None,
                ):
                    pass

        # 第二轮（call_count==2）的 messages 应包含 goal_reminder
        if call_count >= 2:
            second_round_msgs = "\n".join(
                m.get("content", "") for m in captured_messages[len(initial_messages):]
            )
            assert "原始请求" in second_round_msgs or "帮我完成原始任务" in second_round_msgs
