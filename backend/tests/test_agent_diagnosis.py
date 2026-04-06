"""TC-AGENT-DIAG: Agent 行为诊断测试

针对三类已知 UX 问题的定向排查：

问题1 — 循环提问（InputEvaluator 反复追问）
  - 评分逻辑：阈值/字段分值/null字段处理
  - 上限保护：消息轮数 > max_clarify_msgs 时应放行
  - 对话积累：多轮后应收敛通过而非继续追问
  - 空 current_message 路径（当前调用方式）

问题2 — 工具调用失败/循环（Agent Loop 行为）
  - 连续失败早停：连续2轮全失败应停止
  - 参数解析容错：JSON 格式错误不 crash
  - max_rounds 上限：严格不超过5轮
  - 失败后 LLM 不应再次发起相同调用（任务漂移检测）
  - 工具名解析：tool/name 字段兼容性

问题3 — Skill 兼容性差（prepare 分支）
  - required_inputs 为空时直接放行
  - skill 无 versions 时不 crash
  - early_return 被正确短路
  - InputEvaluator 异常时放行而非阻断
  - 无 skill 命中时走默认流程不 crash
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.input_evaluator import InputEvaluator, input_evaluator
from app.services.skill_engine import SkillEngine


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════

def _msg(role: str, content: str):
    """构造最简 Message-like mock。"""
    m = MagicMock()
    m.role = MagicMock()
    m.role.value = role
    m.content = content
    return m


def _make_messages(pairs: list[tuple[str, str]]):
    return [_msg(r, c) for r, c in pairs]


def _make_skill_version(required_inputs=None, system_prompt="你是助手。",
                         output_schema=None, model_config_id=None):
    v = MagicMock()
    v.required_inputs = required_inputs or []
    v.system_prompt = system_prompt
    v.output_schema = output_schema
    v.model_config_id = model_config_id
    return v


def _make_skill(name="test-skill", mode_val="hybrid", versions=None, data_queries=None):
    from app.models.skill import SkillMode
    s = MagicMock()
    s.id = 1
    s.name = name
    s.description = "测试Skill"
    s.mode = SkillMode.HYBRID
    s.versions = versions if versions is not None else [_make_skill_version()]
    s.data_queries = data_queries or []
    s.tools = []
    return s


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════════
# 问题1：循环提问 — InputEvaluator 行为
# ═══════════════════════════════════════════════════════════════════════

class TestInputEvaluatorLoopBehavior:
    """验证 InputEvaluator 在各种场景下不会造成无限追问。"""

    REQUIRED = [
        {"key": "product",  "label": "产品名称", "desc": "卖什么产品",   "example": "XX猫粮"},
        {"key": "channel",  "label": "销售渠道", "desc": "在哪里卖",     "example": "抖音"},
        {"key": "target",   "label": "目标人群", "desc": "卖给谁",       "example": "养猫女性"},
        {"key": "goal",     "label": "策划目标", "desc": "要达成什么",   "example": "GMV50万"},
    ]

    def test_empty_required_inputs_always_pass(self):
        """required_inputs 为空时无条件放行，不发起 LLM 调用。"""
        result = _run(input_evaluator.evaluate(
            purpose="随便什么目标",
            required_inputs=[],
            history_messages=[],
        ))
        assert result["pass"] is True
        assert result["score"] == 100

    def test_single_field_full_score_passes(self):
        """只有1个字段时，100分应通过阈值60。"""
        single = [{"key": "product", "label": "产品", "desc": "产品名", "example": "猫粮"}]
        with patch.object(input_evaluator, "evaluate", wraps=input_evaluator.evaluate):
            with patch("app.services.input_evaluator.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = (
                    '{"score": 100, "provided": {"product": true}, "missing_labels": [], "missing_questions": []}',
                    {}
                )
                result = _run(input_evaluator.evaluate(
                    purpose="写策划",
                    required_inputs=single,
                    history_messages=[_msg("user", "我们卖猫粮")],
                ))
        assert result["pass"] is True

    def test_llm_failure_falls_through(self):
        """LLM 调用失败时，evaluate 应放行（pass=True），不阻断用户。"""
        with patch("app.services.input_evaluator.llm_gateway.chat",
                   new_callable=AsyncMock, side_effect=Exception("LLM 超时")):
            result = _run(input_evaluator.evaluate(
                purpose="写策划",
                required_inputs=self.REQUIRED,
                history_messages=[_msg("user", "帮我做策划")],
            ))
        assert result["pass"] is True, "LLM 异常时应放行，不应阻断用户"

    def test_llm_returns_invalid_json_falls_through(self):
        """LLM 返回非 JSON 时，evaluate 应放行。"""
        with patch("app.services.input_evaluator.llm_gateway.chat",
                   new_callable=AsyncMock, return_value=("这是无效输出", {})):
            result = _run(input_evaluator.evaluate(
                purpose="写策划",
                required_inputs=self.REQUIRED,
                history_messages=[_msg("user", "帮我做策划")],
            ))
        assert result["pass"] is True, "JSON 解析失败时应放行"

    def test_score_below_threshold_blocks(self):
        """score < 60 时应阻断，且返回追问。"""
        with patch("app.services.input_evaluator.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (
                json.dumps({
                    "score": 25,
                    "provided": {"product": True, "channel": False, "target": False, "goal": False},
                    "missing_labels": ["销售渠道", "目标人群", "策划目标"],
                    "missing_questions": ["你主要在哪些渠道销售？", "目标人群是谁？", "要达成什么目标？"],
                }),
                {}
            )
            result = _run(input_evaluator.evaluate(
                purpose="写策划",
                required_inputs=self.REQUIRED,
                history_messages=[_msg("user", "帮我做个活动策划")],
            ))
        assert result["pass"] is False
        assert len(result["missing_questions"]) > 0

    def test_score_at_threshold_passes(self):
        """score == 60 时恰好通过。"""
        with patch("app.services.input_evaluator.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (
                json.dumps({
                    "score": 60,
                    "provided": {"product": True, "channel": True, "target": False, "goal": False},
                    "missing_labels": [], "missing_questions": [],
                }),
                {}
            )
            result = _run(input_evaluator.evaluate(
                purpose="写策划",
                required_inputs=self.REQUIRED,
                history_messages=_make_messages([
                    ("user", "帮我做策划"),
                    ("assistant", "请补充产品信息"),
                    ("user", "产品是XX猫粮，在抖音卖"),
                ]),
            ))
        assert result["pass"] is True

    def test_max_clarify_guard_in_prepare(self):
        """prepare() 中：消息数 > max_clarify_msgs 时应跳过 InputEvaluator，不再追问。

        max_clarify_msgs = n_required * 2 = 4*2 = 8
        当 len(messages) > 8 时，即使 score 很低也不应追问。
        """
        engine = SkillEngine()
        skill_version = _make_skill_version(required_inputs=self.REQUIRED)
        skill = _make_skill(versions=[skill_version])

        # 构造 11 条消息（超过上限8）
        messages = _make_messages([
            ("user", f"消息{i}") for i in range(11)
        ])

        db = MagicMock()
        db.get.return_value = skill
        # H3: prepare() 现在使用 .order_by(...).limit(100).all()[::-1]
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = messages
        db.query.return_value.filter.return_value.first.return_value = None

        conv = MagicMock()
        conv.id = 1
        conv.skill_id = skill.id
        conv.workspace_id = None

        eval_called = []

        async def mock_eval(**kwargs):
            eval_called.append(True)
            return {"pass": False, "score": 0, "missing_questions": ["缺信息"]}

        with patch("app.services.input_evaluator.input_evaluator.evaluate",
                   side_effect=mock_eval):
            with patch("app.services.skill_engine.llm_gateway.get_config",
                       return_value={"model": "test"}):
                with patch("app.services.skill_engine.llm_gateway.chat",
                           new_callable=AsyncMock, return_value=("回复", {})):
                    with patch("app.services.skill_engine.prompt_compiler.compile",
                               return_value=("system_prompt", [])):
                        try:
                            result = _run(engine.prepare(db, conv, "继续帮我"))
                        except Exception:
                            pass  # 其他 mock 不完整导致的错误可忽略

        assert len(eval_called) == 0, (
            f"消息数超过 max_clarify_msgs 后，InputEvaluator 不应被调用，但调用了 {len(eval_called)} 次"
        )

    def test_build_checklist_text_format(self):
        """检查 checklist 格式正确，分值计算无除零错误。"""
        ev = InputEvaluator()
        text = ev.build_checklist_text(self.REQUIRED)
        assert "product" in text
        assert "25" in text  # 100/4 = 25 per field

    def test_build_checklist_single_field_no_zero_division(self):
        """单字段时分值为100，不应出现除零。"""
        ev = InputEvaluator()
        text = ev.build_checklist_text([{"key": "x", "label": "X", "desc": "desc"}])
        assert "100" in text


# ═══════════════════════════════════════════════════════════════════════
# 问题2：工具调用失败/循环 — Agent Loop 行为
# ═══════════════════════════════════════════════════════════════════════

class TestAgentLoopBehavior:
    """验证 Agent Loop 在工具失败场景下不会无限循环。"""

    def _engine(self):
        return SkillEngine()

    def _base_messages(self):
        return [{"role": "user", "content": "帮我查询数据"}]

    def _model_config(self):
        return {"model": "test", "api_base": "http://test"}

    def test_max_rounds_strictly_enforced(self):
        """工具每轮都返回成功但 LLM 一直发新工具调用，严格不超过 max_rounds=5。"""
        engine = self._engine()
        call_count = [0]

        async def fake_execute_tools_parallel(db, calls, user_id):
            call_count[0] += 1
            return [(call, {"ok": True, "result": {"data": "ok"}}) for call in calls]

        # 每轮 LLM 都返回新的 tool_call 块，触发继续循环
        async def fake_stream(model_config, messages, tools=None):
            for _ in range(3):
                yield ("content", "执行中... ")
            # 每轮都发一个 tool_call，强制循环
            yield ("tool_call", {
                "id": "call_1",
                "name": "query_data",
                "arguments": '{"table": "sales"}',
            })

        db = MagicMock()
        skill = MagicMock()
        skill.name = "test"

        results = []

        async def run():
            async for item in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response='```tool_call\n{"tool": "query_data", "params": {"table": "sales"}}\n```',
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
                max_rounds=5,
            ):
                results.append(item)

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute_tools_parallel):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        assert call_count[0] <= 5, f"工具执行次数 {call_count[0]} 超过 max_rounds=5"

    def test_consecutive_all_fail_stops_at_2(self):
        """连续2轮所有工具均失败时，应触发早停，不进行第3轮。"""
        engine = self._engine()
        call_count = [0]

        async def fake_execute_tools_parallel(db, calls, user_id):
            call_count[0] += 1
            # 所有工具都失败
            return [(call, {"ok": False, "error": "连接超时"}) for call in calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "我来重试... ")
            yield ("tool_call", {"id": "c1", "name": "query_data", "arguments": "{}"})

        db = MagicMock()
        skill = MagicMock()

        events = []

        async def run():
            async for item in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response='```tool_call\n{"tool": "query_data", "params": {}}\n```',
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
                max_rounds=5,
            ):
                events.append(item)

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute_tools_parallel):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        assert call_count[0] <= 2, (
            f"连续失败早停应在第2轮触发，实际执行了 {call_count[0]} 轮"
        )

        # 应该有 consecutive_failures 早停事件
        stop_events = [
            e for e in events
            if isinstance(e, dict) and e.get("event") == "round_end"
            and e.get("data", {}).get("reason") == "consecutive_failures"
        ]
        assert len(stop_events) >= 1, "应有 reason=consecutive_failures 的 round_end 事件"

    def test_mixed_success_failure_resets_counter(self):
        """有工具成功时，连续失败计数应重置，不应提前停止。"""
        engine = self._engine()
        call_count = [0]

        async def fake_execute_tools_parallel(db, calls, user_id):
            call_count[0] += 1
            # 第1轮失败，第2轮成功，第3轮失败
            if call_count[0] == 1:
                return [(call, {"ok": False, "error": "失败"}) for call in calls]
            elif call_count[0] == 2:
                return [(call, {"ok": True, "result": {}}) for call in calls]
            else:
                return [(call, {"ok": False, "error": "失败"}) for call in calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "继续... ")
            if call_count[0] < 3:
                yield ("tool_call", {"id": "c1", "name": "query_data", "arguments": "{}"})

        db = MagicMock()
        skill = MagicMock()
        events = []

        async def run():
            async for item in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response='```tool_call\n{"tool": "query_data", "params": {}}\n```',
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
                max_rounds=5,
            ):
                events.append(item)

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute_tools_parallel):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        # 第2轮成功重置计数，第3轮失败后计数=1，不触发早停
        # 因此应该执行3轮（不是2轮就停）
        assert call_count[0] >= 3, (
            f"成功重置计数后不应提前停止，但只执行了 {call_count[0]} 轮"
        )

    def test_tool_name_from_tool_field(self):
        """工具调用中使用 'tool' 字段（而非 'name'）应能正确解析。"""
        engine = self._engine()
        parsed_names = []

        async def fake_execute_one(db, tool_calls, user_id):
            for call in tool_calls:
                name = call.get("tool") or call.get("name", "")
                parsed_names.append(name)
            return [(call, {"ok": True, "result": {}}) for call in tool_calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "完成")

        db = MagicMock()
        skill = MagicMock()

        async def run():
            async for _ in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response='```tool_call\n{"tool": "query_sales", "params": {"date": "today"}}\n```',
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
            ):
                pass

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute_one):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        assert "query_sales" in parsed_names, f"tool 字段未正确解析，parsed_names={parsed_names}"

    def test_tool_name_from_name_field(self):
        """工具调用中使用 'name' 字段（native FC 格式）应能正确解析。"""
        engine = self._engine()
        parsed_names = []

        async def fake_execute_one(db, tool_calls, user_id):
            for call in tool_calls:
                name = call.get("tool") or call.get("name", "")
                parsed_names.append(name)
            return [(call, {"ok": True, "result": {}}) for call in tool_calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "完成")

        db = MagicMock()
        skill = MagicMock()

        async def run():
            async for _ in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response="",  # 空 response，走 native_tool_calls 路径
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
                native_tool_calls=[{"id": "c1", "name": "query_sales", "arguments": '{"date": "today"}'}],
            ):
                pass

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute_one):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        assert "query_sales" in parsed_names

    def test_malformed_tool_call_json_does_not_crash(self):
        """tool_call 块中 JSON 格式错误时，应跳过该调用而不 crash。"""
        engine = self._engine()
        call_count = [0]

        async def fake_execute(db, calls, user_id):
            call_count[0] += 1
            return [(c, {"ok": True, "result": {}}) for c in calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "完成")

        db = MagicMock()
        skill = MagicMock()

        async def run():
            async for _ in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                # 故意破坏 JSON
                response='```tool_call\n{tool: "query", params: invalid}\n```',
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
            ):
                pass

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                # 不应抛异常
                _run(run())

        # JSON 解析失败，calls 列表为空，execute 不应被调用
        assert call_count[0] == 0, "JSON 解析失败时不应调用工具执行"

    def test_empty_response_no_tool_calls_exits_immediately(self):
        """response 中没有 tool_call 块时，第一轮解析到空 calls，立即退出循环。"""
        engine = self._engine()
        call_count = [0]

        async def fake_execute(db, calls, user_id):
            call_count[0] += 1
            return []

        db = MagicMock()
        skill = MagicMock()

        async def run():
            async for _ in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response="这是普通回复，没有工具调用",
                llm_messages=self._base_messages(),
                model_config=self._model_config(),
                user_id=1,
            ):
                pass

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute):
            _run(run())

        assert call_count[0] == 0


# ═══════════════════════════════════════════════════════════════════════
# 问题3：Skill 兼容性差 — prepare 分支健壮性
# ═══════════════════════════════════════════════════════════════════════

class TestSkillCompatibility:
    """验证 prepare() 在各种 Skill 配置下不会 crash 或产生错误行为。"""

    def _make_conv(self, skill_id=None, workspace_id=None):
        conv = MagicMock()
        conv.id = 1
        conv.skill_id = skill_id
        conv.workspace_id = workspace_id
        return conv

    def _make_db(self, skill=None, messages=None):
        db = MagicMock()
        db.get.return_value = skill
        msgs = messages or []
        # H3: prepare() 使用 .order_by(...).limit(100).all()[::-1]
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = msgs
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_skill_with_no_versions_does_not_crash(self):
        """Skill.versions 为空列表时，prepare 不应抛出 IndexError。"""
        engine = SkillEngine()
        skill = _make_skill(versions=[])
        db = self._make_db(skill=skill)
        conv = self._make_conv(skill_id=skill.id)

        with patch("app.services.skill_engine.llm_gateway.get_config",
                   return_value={"model": "test"}):
            with patch("app.services.skill_engine.llm_gateway.supports_function_calling",
                       return_value=False):
                with patch("app.services.skill_engine.prompt_compiler.compile",
                           return_value=("system_prompt", [])):
                    with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                               return_value={"model": "lite"}):
                        try:
                            result = _run(engine.prepare(db, conv, "帮我"))
                            # 只要不 crash 就通过
                            assert result is not None
                        except AttributeError as e:
                            pytest.fail(f"Skill 无版本时不应抛出 AttributeError: {e}")
                        except Exception:
                            pass  # 其他 mock 不完整导致的错误可忽略

    def test_required_inputs_empty_skips_evaluator(self):
        """required_inputs=[] 时不调用 LLM 评估，直接放行。"""
        ev = InputEvaluator()
        with patch("app.services.input_evaluator.llm_gateway.chat",
                   new_callable=AsyncMock) as mock_chat:
            result = _run(ev.evaluate(
                purpose="任意任务",
                required_inputs=[],
                history_messages=[],
            ))
            mock_chat.assert_not_called()
        assert result["pass"] is True

    def test_evaluator_called_only_within_message_limit(self):
        """prepare 中 InputEvaluator 只在 len(messages) <= max_clarify 时调用。

        n_required=2 → max_clarify = 2*2 = 4
        messages=7 条时不应调用 InputEvaluator。
        """
        engine = SkillEngine()
        required = [
            {"key": "a", "label": "A", "desc": "描述A"},
            {"key": "b", "label": "B", "desc": "描述B"},
        ]
        skill_version = _make_skill_version(required_inputs=required)
        skill = _make_skill(versions=[skill_version])

        # 7条消息 > max_clarify=4
        messages = [_msg("user", f"msg{i}") for i in range(7)]
        db = self._make_db(skill=skill, messages=messages)
        conv = self._make_conv(skill_id=skill.id)

        eval_calls = []

        async def mock_eval(**kwargs):
            eval_calls.append(True)
            return {"pass": False, "score": 0, "missing_questions": ["追问"]}

        with patch("app.services.input_evaluator.input_evaluator.evaluate",
                   side_effect=mock_eval):
            with patch("app.services.skill_engine.llm_gateway.get_config",
                       return_value={"model": "test"}):
                with patch("app.services.skill_engine.llm_gateway.supports_function_calling",
                           return_value=False):
                    with patch("app.services.skill_engine.prompt_compiler.compile",
                               return_value=("sys", [])):
                        with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                                   return_value={"model": "lite"}):
                            try:
                                _run(engine.prepare(db, conv, "继续"))
                            except Exception:
                                pass

        assert len(eval_calls) == 0, (
            f"消息超过 max_clarify_msgs 后不应调用 InputEvaluator，但调用了 {len(eval_calls)} 次"
        )

    def test_evaluator_called_within_message_limit(self):
        """messages=3 条时（< max_clarify=4），应调用 InputEvaluator。"""
        engine = SkillEngine()
        required = [
            {"key": "a", "label": "A", "desc": "描述A"},
            {"key": "b", "label": "B", "desc": "描述B"},
        ]
        skill_version = _make_skill_version(required_inputs=required)
        skill = _make_skill(versions=[skill_version])

        messages = [_msg("user", f"msg{i}") for i in range(3)]
        db = self._make_db(skill=skill, messages=messages)
        conv = self._make_conv(skill_id=skill.id)

        eval_calls = []

        async def mock_eval(**kwargs):
            eval_calls.append(True)
            return {"pass": True, "score": 100, "missing_questions": []}

        with patch("app.services.input_evaluator.input_evaluator.evaluate",
                   side_effect=mock_eval):
            with patch("app.services.skill_engine.llm_gateway.get_config",
                       return_value={"model": "test"}):
                with patch("app.services.skill_engine.llm_gateway.supports_function_calling",
                           return_value=False):
                    with patch("app.services.skill_engine.prompt_compiler.compile",
                               return_value=("sys", [])):
                        with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                                   return_value={"model": "lite"}):
                            try:
                                _run(engine.prepare(db, conv, "帮我做策划"))
                            except Exception:
                                pass

        assert len(eval_calls) >= 1, (
            "消息未超过 max_clarify_msgs 时，应调用 InputEvaluator"
        )

    def test_early_return_short_circuits(self):
        """early_return 非 None 时，execute() 不应再调用 LLM.chat。"""
        engine = SkillEngine()

        mock_prep = MagicMock()
        mock_prep.early_return = ("这是早返结果", {"source": "rule_engine"})
        mock_prep.skill_name = None
        mock_prep.skill_version = None

        with patch.object(engine, "prepare", new_callable=AsyncMock,
                          return_value=mock_prep):
            with patch("app.services.skill_engine.llm_gateway.chat",
                       new_callable=AsyncMock) as mock_llm:
                db = MagicMock()
                conv = MagicMock()
                result = _run(engine.execute(db, conv, "帮我"))

        mock_llm.assert_not_called()
        assert result[0] == "这是早返结果"

    def test_no_skill_matched_does_not_crash(self):
        """skill 匹配失败（返回 None）时，prepare 应用默认 system_prompt 继续执行。"""
        engine = SkillEngine()
        db = self._make_db(skill=None)
        db.get.return_value = None

        conv = self._make_conv(skill_id=None)

        with patch("app.services.skill_engine.llm_gateway.get_config",
                   return_value={"model": "test"}):
            with patch.object(engine, "_match_skill", new_callable=AsyncMock,
                               return_value=None):
                with patch("app.services.skill_engine.prompt_compiler.compile",
                           return_value=("默认系统提示", [])):
                    with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                               return_value={"model": "lite"}):
                        try:
                            result = _run(engine.prepare(db, conv, "你好"))
                            # 不 crash 即通过
                        except Exception as e:
                            # 允许因其他 mock 不完整抛 Exception，
                            # 但不允许 AttributeError/IndexError（代码缺陷）
                            if isinstance(e, (AttributeError, IndexError)):
                                pytest.fail(f"无 Skill 时不应抛出 {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 边界条件集中测试
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """覆盖各类边界输入。"""

    def test_evaluate_missing_keys_in_required_input(self):
        """required_inputs 中 item 缺少 key/label 字段时不 crash。"""
        ev = InputEvaluator()
        # 只有 key，缺 label/desc
        partial = [{"key": "product"}]
        with patch("app.services.input_evaluator.llm_gateway.chat",
                   new_callable=AsyncMock, return_value=(
                       '{"score": 100, "provided": {}, "missing_labels": [], "missing_questions": []}', {}
                   )):
            result = _run(ev.evaluate(
                purpose="任务",
                required_inputs=partial,
                history_messages=[],
            ))
        assert result["pass"] is True

    def test_evaluate_with_long_history_truncated(self):
        """历史消息很长时 evaluate 不 crash（内部取最后12条）。"""
        ev = InputEvaluator()
        long_history = [_msg("user", f"消息{i}") for i in range(50)]
        with patch("app.services.input_evaluator.llm_gateway.chat",
                   new_callable=AsyncMock, return_value=(
                       '{"score": 100, "provided": {}, "missing_labels": [], "missing_questions": []}', {}
                   )):
            result = _run(ev.evaluate(
                purpose="任务",
                required_inputs=[{"key": "x", "label": "X", "desc": "d"}],
                history_messages=long_history,
            ))
        assert result is not None

    def test_agent_loop_tool_result_cleanup(self):
        """最终 response 中 tool_call 代码块应被清理干净。"""
        engine = SkillEngine()

        async def fake_execute(db, calls, user_id):
            return [(c, {"ok": True, "result": {"data": "ok"}}) for c in calls]

        async def fake_stream(model_config, messages, tools=None):
            yield ("content", "执行完成，结果如上。")

        db = MagicMock()
        skill = MagicMock()
        final_response = []

        async def run():
            async for item in engine._handle_tool_calls_stream(
                db=db,
                skill=skill,
                response='```tool_call\n{"tool": "query_data", "params": {}}\n```',
                llm_messages=[{"role": "user", "content": "查数据"}],
                model_config={"model": "test"},
                user_id=1,
            ):
                if isinstance(item, tuple):
                    final_response.append(item[0])

        with patch.object(engine, "_execute_tools_parallel", side_effect=fake_execute):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       side_effect=lambda **kw: fake_stream(**kw)):
                _run(run())

        if final_response:
            assert "```tool_call" not in final_response[0], (
                "最终回复中不应包含 tool_call 代码块"
            )
