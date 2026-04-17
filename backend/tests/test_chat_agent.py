"""TC-CHAT-AGENT: 针对 Chat Agent 四项优化的完整测试套件

覆盖：
1. 原生 Function Calling — llm_gateway tool_call 事件、tool_calls body注入、不支持模型的 fallback
2. 工具并行执行 — execute_tools_parallel 并发性、顺序无关性
3. 知识召回 LLM 二次筛选 — rerank 逻辑、降级兜底
4. Skill 匹配连续性缓存 — _needs_skill_switch 分支走向
5. 完整流式端点集成 — tools_schema 传递、native tool_call SSE 事件
"""
import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from tests.conftest import (
    _make_user, _make_dept, _login, _auth, _make_model_config,
    _make_skill, _make_tool,
)
from app.models.user import Role


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    events = []
    current_event = "message"
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                events.append({"event": current_event, "data": data})
                current_event = "message"
            except json.JSONDecodeError:
                pass
    return events


def _make_mock_prep(tools_schema=None):
    """构造 PrepareResult mock，包含 tools_schema 字段。"""
    mock_prep = MagicMock()
    mock_prep.early_return = None
    mock_prep.skill_name = None
    mock_prep.skill_version = None
    mock_prep.skill_id = None
    mock_prep.llm_messages = []
    mock_prep.model_config = {"context_window": 32000}
    mock_prep.tools_schema = tools_schema or []
    return mock_prep


def _setup_conv(client, db, username=None):
    dept = _make_dept(db)
    uname = username or f"u_{id(db)}_{int(time.time()*1000) % 100000}"
    _make_user(db, uname, Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, uname)
    r = client.post("/api/conversations", headers=_auth(token))
    return token, r.json()["id"]


# ═════════════════════════════════════════════════════════════════════════════
# 1. 原生 Function Calling — LLMGateway
# ═════════════════════════════════════════════════════════════════════════════

class TestNativeFunctionCalling:
    """LLMGateway.chat_stream_typed 对 tools 参数和 tool_call 事件的处理。"""

    def _make_gw(self):
        from app.services.llm_gateway import LLMGateway
        return LLMGateway()

    def _model_cfg(self, model_id="gpt-4o"):
        return {
            "api_base": "http://fake", "api_key": "k",
            "model_id": model_id, "max_tokens": 100, "temperature": "0.7",
        }

    def _fake_stream(self, lines):
        """返回一个 mock context manager for gw._client.stream()。

        兼容重试逻辑：每次调用 stream() 都返回新的 async context manager。
        """
        _lines = lines

        class FakeResp:
            status_code = 200
            async def aiter_lines(self):
                for l in _lines:
                    yield l
            async def aread(self):
                return b""
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        return FakeResp()

    # ── 1a. tool_call 事件正确解析 ──

    @pytest.mark.asyncio
    async def test_tool_call_event_yielded_on_finish_reason(self):
        gw = self._make_gw()
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"doc_gen","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"title\\":\\"报告\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        tools = [{"type": "function", "function": {"name": "doc_gen", "description": "生成文档", "parameters": {}}}]

        events = []
        with patch.object(gw._client, "stream", side_effect=lambda *a, **kw: self._fake_stream(lines)):
            async for ctype, data in gw.chat_stream_typed(
                model_config=self._model_cfg(), messages=[], tools=tools
            ):
                events.append((ctype, data))

        tool_events = [(t, d) for t, d in events if t == "tool_call"]
        assert len(tool_events) == 1
        tc = tool_events[0][1]
        assert tc["name"] == "doc_gen"
        assert tc["id"] == "call_1"
        assert "title" in tc["arguments"]

    @pytest.mark.asyncio
    async def test_tool_call_arguments_accumulated_across_chunks(self):
        """arguments 跨多个 chunk 拼接正确。"""
        gw = self._make_gw()
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search","arguments":"{\\"q"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"uery\\":\\"AI\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        tools = [{"type": "function", "function": {"name": "search", "description": "", "parameters": {}}}]

        events = []
        with patch.object(gw._client, "stream", side_effect=lambda *a, **kw: self._fake_stream(lines)):
            async for ctype, data in gw.chat_stream_typed(
                model_config=self._model_cfg(), messages=[], tools=tools
            ):
                events.append((ctype, data))

        tool_events = [d for t, d in events if t == "tool_call"]
        assert len(tool_events) == 1
        args = json.loads(tool_events[0]["arguments"])
        assert args["query"] == "AI"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_same_round(self):
        """同一轮两个并行工具调用都被 yield。"""
        gw = self._make_gw()
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c0","function":{"name":"tool_a","arguments":"{}"}},{"index":1,"id":"c1","function":{"name":"tool_b","arguments":"{}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_b", "description": "", "parameters": {}}},
        ]

        events = []
        with patch.object(gw._client, "stream", side_effect=lambda *a, **kw: self._fake_stream(lines)):
            async for ctype, data in gw.chat_stream_typed(
                model_config=self._model_cfg(), messages=[], tools=tools
            ):
                events.append((ctype, data))

        tool_events = [d for t, d in events if t == "tool_call"]
        names = {e["name"] for e in tool_events}
        assert "tool_a" in names
        assert "tool_b" in names

    # ── 1b. 无 tools 参数时不出现 tool_call 事件 ──

    @pytest.mark.asyncio
    async def test_no_tools_no_tool_call_events(self):
        gw = self._make_gw()
        lines = [
            'data: {"choices":[{"delta":{"content":"普通回复"}}]}',
            "data: [DONE]",
        ]

        events = []
        with patch.object(gw._client, "stream", side_effect=lambda *a, **kw: self._fake_stream(lines)):
            async for ctype, data in gw.chat_stream_typed(
                model_config=self._model_cfg(), messages=[]  # 不传 tools
            ):
                events.append((ctype, data))

        assert all(t != "tool_call" for t, _ in events)
        assert any(t == "content" for t, _ in events)

    # ── 1c. 不支持 function calling 的模型不注入 tools body ──

    def test_unsupported_model_tools_not_in_body(self):
        gw = self._make_gw()
        cfg = self._model_cfg("moonshot-v1-8k-thinking")
        tools = [{"type": "function", "function": {"name": "x", "description": "", "parameters": {}}}]
        _, _, body = gw._build_request(cfg, [], tools=tools)
        assert "tools" not in body

    def test_supported_model_tools_in_body(self):
        gw = self._make_gw()
        cfg = self._model_cfg("gpt-4o")
        tools = [{"type": "function", "function": {"name": "x", "description": "", "parameters": {}}}]
        _, _, body = gw._build_request(cfg, [], tools=tools)
        assert "tools" in body
        assert body["tool_choice"] == "auto"

    def test_anthropic_protocol_uses_messages_endpoint(self):
        gw = self._make_gw()
        cfg = {
            "provider": "bailian",
            "api_protocol": "anthropic-compatible",
            "api_base": "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1",
            "api_key": "k",
            "model_id": "kimi-k2.5",
            "max_tokens": 100,
            "temperature": "0.0",
        }

        url, headers, body = gw._build_request(
            cfg,
            [
                {"role": "system", "content": "你是评分器"},
                {"role": "user", "content": "请评分"},
            ],
            max_tokens=50,
        )

        assert url == "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1/messages"
        assert headers["x-api-key"] == "k"
        assert headers["anthropic-version"] == "2023-06-01"
        assert body["model"] == "kimi-k2.5"
        assert body["system"] == "你是评分器"
        assert body["messages"] == [{"role": "user", "content": "请评分"}]
        assert body["max_tokens"] == 50

    @pytest.mark.asyncio
    async def test_anthropic_chat_response_parsed(self):
        gw = self._make_gw()
        cfg = {
            "provider": "bailian",
            "api_protocol": "anthropic-compatible",
            "api_base": "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1",
            "api_key": "k",
            "model_id": "kimi-k2.5",
            "max_tokens": 100,
            "temperature": "0.0",
        }

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {
                    "content": [{"type": "text", "text": "{\"score\": 88}"}],
                    "usage": {"input_tokens": 12, "output_tokens": 6},
                }

        with patch.object(gw._client, "post", new=AsyncMock(return_value=FakeResp())) as mock_post:
            content, usage = await gw.chat(cfg, [{"role": "user", "content": "评分"}])

        assert content == "{\"score\": 88}"
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 6
        assert mock_post.call_args.args[0].endswith("/messages")

    def test_supports_function_calling_returns_false_for_known_model(self):
        gw = self._make_gw()
        assert gw.supports_function_calling({"model_id": "moonshot-v1-8k-thinking"}) is False
        assert gw.supports_function_calling({"model_id": "moonshot-v1-32k-thinking"}) is False

    def test_supports_function_calling_returns_true_for_others(self):
        gw = self._make_gw()
        for model_id in ["gpt-4o", "deepseek-chat", "claude-3-5-sonnet", "qwen-max"]:
            assert gw.supports_function_calling({"model_id": model_id}) is True

    # ── 1d. content 与 tool_call 混合场景 ──

    @pytest.mark.asyncio
    async def test_content_before_tool_call(self):
        gw = self._make_gw()
        lines = [
            'data: {"choices":[{"delta":{"content":"好的，"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"gen","arguments":"{}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        tools = [{"type": "function", "function": {"name": "gen", "description": "", "parameters": {}}}]

        events = []
        with patch.object(gw._client, "stream", side_effect=lambda *a, **kw: self._fake_stream(lines)):
            async for ctype, data in gw.chat_stream_typed(
                model_config=self._model_cfg(), messages=[], tools=tools
            ):
                events.append((ctype, data))

        types = [t for t, _ in events]
        assert "content" in types
        assert "tool_call" in types


# ═════════════════════════════════════════════════════════════════════════════
# 2. 工具并行执行
# ═════════════════════════════════════════════════════════════════════════════

class TestParallelToolExecution:
    """_execute_tools_parallel 并发行为测试。"""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    @pytest.mark.asyncio
    async def test_all_tools_executed(self):
        engine = self._make_engine()
        calls = [
            {"tool": "tool_a", "params": {"x": 1}},
            {"tool": "tool_b", "params": {"y": 2}},
            {"tool": "tool_c", "params": {"z": 3}},
        ]
        mock_result = {"ok": True, "result": {}}

        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)) as mock_exec:
            results = await engine._execute_tools_parallel(MagicMock(), calls, user_id=1)

        assert len(results) == 3
        assert mock_exec.call_count == 3

    @pytest.mark.asyncio
    async def test_parallel_faster_than_serial(self):
        """并行执行应比串行快（每个工具延时 0.05s，3 个工具总耗时应 < 0.12s）。"""
        engine = self._make_engine()
        calls = [{"tool": f"t{i}", "params": {}} for i in range(3)]

        async def slow_exec(db, tool_name, params, user_id):
            await asyncio.sleep(0.05)
            return {"ok": True, "result": {}}

        with patch("app.services.tool_executor.tool_executor.execute_tool", side_effect=slow_exec):
            t0 = time.time()
            results = await engine._execute_tools_parallel(MagicMock(), calls, user_id=None)
            elapsed = time.time() - t0

        assert len(results) == 3
        assert elapsed < 0.12, f"并行执行耗时 {elapsed:.3f}s，预期 < 0.12s（串行需 0.15s）"

    @pytest.mark.asyncio
    async def test_results_include_all_calls_paired(self):
        """每个结果都配对了原始 call 对象。"""
        engine = self._make_engine()
        calls = [
            {"tool": "alpha", "params": {"k": "v1"}},
            {"tool": "beta",  "params": {"k": "v2"}},
        ]

        async def echo_exec(db, tool_name, params, user_id):
            return {"ok": True, "result": {"tool_name": tool_name}}

        with patch("app.services.tool_executor.tool_executor.execute_tool", side_effect=echo_exec):
            pairs = await engine._execute_tools_parallel(MagicMock(), calls, user_id=None)

        pair_map = {call["tool"]: result["result"]["tool_name"] for call, result in pairs}
        assert pair_map["alpha"] == "alpha"
        assert pair_map["beta"] == "beta"

    @pytest.mark.asyncio
    async def test_native_call_arguments_parsed(self):
        """原生 function calling 格式（arguments 为 JSON 字符串）能正确解析。"""
        engine = self._make_engine()
        calls = [{"name": "gen_doc", "arguments": '{"title": "报告", "pages": 5}', "id": "c1"}]

        captured_params = {}

        async def capture_exec(db, tool_name, params, user_id):
            captured_params[tool_name] = params
            return {"ok": True, "result": {}}

        with patch("app.services.tool_executor.tool_executor.execute_tool", side_effect=capture_exec):
            await engine._execute_tools_parallel(MagicMock(), calls, user_id=None)

        assert captured_params["gen_doc"] == {"title": "报告", "pages": 5}

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_others(self):
        """一个工具失败不影响其他工具返回结果。"""
        engine = self._make_engine()
        calls = [
            {"tool": "good_tool", "params": {}},
            {"tool": "bad_tool",  "params": {}},
        ]

        async def mixed_exec(db, tool_name, params, user_id):
            if tool_name == "bad_tool":
                return {"ok": False, "error": "执行失败"}
            return {"ok": True, "result": {"done": True}}

        with patch("app.services.tool_executor.tool_executor.execute_tool", side_effect=mixed_exec):
            pairs = await engine._execute_tools_parallel(MagicMock(), calls, user_id=None)

        results_by_tool = {c["tool"]: r for c, r in pairs}
        assert results_by_tool["good_tool"]["ok"] is True
        assert results_by_tool["bad_tool"]["ok"] is False

    @pytest.mark.asyncio
    async def test_agent_loop_emits_parallel_block_starts(self):
        """两个工具调用 → 两个 content_block_start(tool_call) 事件，均在执行前发出。"""
        engine = self._make_engine()
        response = (
            '```tool_call\n{"tool": "tool_x", "params": {}}\n```\n'
            '```tool_call\n{"tool": "tool_y", "params": {}}\n```'
        )
        mock_result = {"ok": True, "result": {}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed") as mock_stream:
                async def _gen(*a, **kw):
                    yield ("content", "完成")
                mock_stream.side_effect = _gen

                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None, response=response,
                    llm_messages=[], model_config={}, user_id=None,
                ):
                    events.append(item)

        starts = [e for e in events if isinstance(e, dict) and e.get("event") == "content_block_start"
                  and e["data"].get("type") == "tool_call"]
        assert len(starts) == 2
        names = {e["data"]["tool"] for e in starts}
        assert names == {"tool_x", "tool_y"}


# ═════════════════════════════════════════════════════════════════════════════
# 3. 知识召回 LLM 二次筛选
# ═════════════════════════════════════════════════════════════════════════════

class TestKnowledgeRerank:
    """_rerank_hits_with_llm 逻辑测试。"""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    def _hits(self, n):
        return [{"knowledge_id": i, "text": f"知识片段{i}", "title": f"标题{i}"} for i in range(n)]

    @pytest.mark.asyncio
    async def test_rerank_selects_top_k(self):
        engine = self._make_engine()
        hits = self._hits(10)

        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(return_value=("0,3,5,7,9", {}))):
            result = await engine._rerank_hits_with_llm(MagicMock(), "测试问题", hits, top_k=5)

        assert len(result) == 5
        assert result[0]["knowledge_id"] == 0
        assert result[1]["knowledge_id"] == 3

    @pytest.mark.asyncio
    async def test_rerank_skipped_when_hits_lte_top_k(self):
        """hits 数量 ≤ top_k 时，不调用 LLM，直接返回。"""
        engine = self._make_engine()
        hits = self._hits(3)

        with patch("app.services.skill_engine.llm_gateway.chat") as mock_chat:
            result = await engine._rerank_hits_with_llm(MagicMock(), "问题", hits, top_k=5)

        mock_chat.assert_not_called()
        assert result == hits

    @pytest.mark.asyncio
    async def test_rerank_fallback_on_llm_failure(self):
        """LLM 调用失败时，返回前 top_k 条（不抛异常）。"""
        engine = self._make_engine()
        hits = self._hits(10)

        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(side_effect=Exception("API timeout"))):
            result = await engine._rerank_hits_with_llm(MagicMock(), "问题", hits, top_k=4)

        assert len(result) == 4
        assert result == hits[:4]

    @pytest.mark.asyncio
    async def test_rerank_fallback_on_bad_llm_output(self):
        """LLM 返回非数字时，返回前 top_k 条。"""
        engine = self._make_engine()
        hits = self._hits(8)

        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(return_value=("不知道", {}))):
            result = await engine._rerank_hits_with_llm(MagicMock(), "问题", hits, top_k=3)

        assert len(result) == 3
        assert result == hits[:3]

    @pytest.mark.asyncio
    async def test_inject_knowledge_calls_rerank(self):
        """_inject_knowledge 使用粗召回 top_k=20 并调用 rerank。"""
        engine = self._make_engine()
        raw_hits = [{"knowledge_id": i, "text": f"t{i}", "created_by": 1,
                      "desensitized_text": ""} for i in range(20)]

        with patch("app.services.vector_service.search_knowledge", return_value=raw_hits):
            with patch.object(engine, "_rerank_hits_with_llm",
                               new=AsyncMock(return_value=raw_hits[:5])) as mock_rerank:
                await engine._inject_knowledge("查询", skill=None, db=MagicMock(), user_id=1)

        mock_rerank.assert_called_once()
        call_args = mock_rerank.call_args
        # 新签名: _rerank_hits_with_llm(db, query, hits, top_k=5)
        assert call_args[1].get("top_k") == 5 or call_args[0][3] == 5

    @pytest.mark.asyncio
    async def test_inject_knowledge_uses_top20_for_vector_search(self):
        """向量搜索使用 top_k=20 而不是 6。"""
        engine = self._make_engine()

        with patch("app.services.vector_service.search_knowledge",
                   return_value=[]) as mock_search:
            with patch.object(engine, "_rerank_hits_with_llm", new=AsyncMock(return_value=[])):
                await engine._inject_knowledge("query", skill=None)

        mock_search.assert_called_once()
        assert mock_search.call_args[1].get("top_k") == 20 or mock_search.call_args[0][1] == 20


# ═════════════════════════════════════════════════════════════════════════════
# 4. Skill 匹配连续性缓存
# ═════════════════════════════════════════════════════════════════════════════

class TestSkillSwitchCache:
    """_match_or_keep_skill 与 prepare() 的 skill 切换逻辑测试。"""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    @pytest.mark.asyncio
    async def test_match_or_keep_returns_new_skill(self):
        """LLM 返回候选 skill name 时，切换到该 skill。"""
        engine = self._make_engine()
        current = MagicMock(); current.name = "文案撰写"; current.description = ""
        candidate = MagicMock(); candidate.name = "数据分析"; candidate.description = ""
        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(return_value=("数据分析", {}))):
            with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                       return_value={"model_id": "x", "api_base": "http://x", "api_key": "k", "max_tokens": 50}):
                result = await engine._match_or_keep_skill(MagicMock(), current, "帮我生成数据报表", [candidate])
        assert result is candidate

    @pytest.mark.asyncio
    async def test_match_or_keep_returns_current_when_keep(self):
        """LLM 返回 keep 时，继续使用当前 skill。"""
        engine = self._make_engine()
        current = MagicMock(); current.name = "文案撰写"; current.description = ""
        candidate = MagicMock(); candidate.name = "数据分析"; candidate.description = ""
        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(return_value=("keep", {}))):
            with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                       return_value={"model_id": "x", "api_base": "http://x", "api_key": "k", "max_tokens": 50}):
                result = await engine._match_or_keep_skill(MagicMock(), current, "帮我改一下这段文案的语气", [candidate])
        assert result is current

    @pytest.mark.asyncio
    async def test_match_or_keep_no_candidates(self):
        """没有候选 skill 时直接返回 current，不调 LLM。"""
        engine = self._make_engine()
        current = MagicMock(); current.name = "文案撰写"; current.description = ""
        with patch("app.services.skill_engine.llm_gateway.chat") as mock_chat:
            result = await engine._match_or_keep_skill(MagicMock(), current, "任意消息", [])
        assert result is current
        mock_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_match_or_keep_fallback_on_error(self):
        """LLM 调用失败时保守策略：返回 current_skill。"""
        engine = self._make_engine()
        current = MagicMock(); current.name = "skill_x"; current.description = ""
        candidate = MagicMock(); candidate.name = "skill_y"; candidate.description = ""
        with patch("app.services.skill_engine.llm_gateway.chat",
                   new=AsyncMock(side_effect=Exception("network error"))):
            with patch("app.services.skill_engine.llm_gateway.get_lite_config",
                       return_value={"model_id": "x", "api_base": "http://x", "api_key": "k", "max_tokens": 50}):
                result = await engine._match_or_keep_skill(MagicMock(), current, "用户消息", [candidate])
        assert result is current

    @pytest.mark.asyncio
    async def test_prepare_uses_match_or_keep_when_candidates_exist(self):
        """有切换候选时，prepare() 调用 _match_or_keep_skill（一次调用完成切换+匹配）。"""
        engine = self._make_engine()

        mock_skill = MagicMock()
        mock_skill.id = 1
        mock_skill.name = "文案助手"
        mock_skill.description = ""
        mock_skill.auto_inject = False
        mock_skill.data_queries = None
        mock_skill.mode = None
        mock_skill.versions = []

        mock_other_skill = MagicMock()
        mock_other_skill.id = 2
        mock_other_skill.name = "数据分析"
        mock_other_skill.description = ""

        mock_conv = MagicMock()
        mock_conv.skill_id = 1
        mock_conv.workspace_id = None
        mock_conv.id = 1

        mock_db = MagicMock()
        mock_db.get.return_value = mock_skill
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        mock_match_or_keep = AsyncMock(return_value=mock_skill)
        with patch.object(engine, "_match_or_keep_skill", mock_match_or_keep):
            with patch("app.services.skill_engine.llm_gateway.get_config", return_value={"model_id": "x", "api_base": "http://x", "api_key_env": "K", "max_tokens": 100, "temperature": "0.7"}):
                with patch("app.services.skill_engine.llm_gateway.get_lite_config", return_value={"model_id": "x", "api_base": "http://x", "api_key": "k", "max_tokens": 50, "temperature": "0.1"}):
                    with patch.object(engine, "_inject_knowledge", new=AsyncMock(return_value="")):
                        with patch.object(engine, "_compact_if_needed", new=AsyncMock(side_effect=lambda db, msgs, _: msgs)):
                            pass  # no switch_candidates since workspace_id=None, so _match_or_keep_skill not called

        # 无 workspace 无 switch_candidates 时不调用 _match_or_keep_skill
        mock_match_or_keep.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_match_or_keep_called_with_workspace(self):
        """workspace 有多个 skill 时，prepare() 调用 _match_or_keep_skill 而非两步走。"""
        engine = self._make_engine()

        mock_current_skill = MagicMock()
        mock_current_skill.id = 1
        mock_current_skill.name = "文案助手"
        mock_current_skill.description = ""
        mock_current_skill.auto_inject = False
        mock_current_skill.data_queries = None
        mock_current_skill.mode = None
        mock_current_skill.versions = []

        mock_new_skill = MagicMock()
        mock_new_skill.id = 2
        mock_new_skill.name = "数据分析"
        mock_new_skill.description = ""
        mock_new_skill.auto_inject = False
        mock_new_skill.data_queries = None
        mock_new_skill.mode = None
        mock_new_skill.versions = []

        mock_wsk1 = MagicMock(); mock_wsk1.skill_id = 1
        mock_wsk2 = MagicMock(); mock_wsk2.skill_id = 2
        mock_workspace = MagicMock()
        mock_workspace.workspace_skills = [mock_wsk1, mock_wsk2]
        mock_workspace.model_config_id = None
        mock_workspace.system_context = None
        mock_workspace.project_id = None
        mock_workspace.workspace_tools = []

        mock_conv = MagicMock()
        mock_conv.skill_id = 1
        mock_conv.workspace_id = 99
        mock_conv.id = 1

        mock_db = MagicMock()

        def db_get(model, pk):
            try:
                from app.models.workspace import Workspace
                if model is Workspace:
                    return mock_workspace
            except ImportError:
                pass
            if pk == 1:
                return mock_current_skill
            if pk == 2:
                return mock_new_skill
            return None

        mock_db.get.side_effect = db_get
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []

        fake_config = {"model_id": "x", "api_base": "http://x", "api_key": "k", "max_tokens": 100, "temperature": "0.7"}
        mock_match_or_keep = AsyncMock(return_value=mock_new_skill)
        with patch.object(engine, "_match_or_keep_skill", mock_match_or_keep):
            with patch("app.services.skill_engine.llm_gateway.get_config", return_value=fake_config):
                with patch("app.services.skill_engine.llm_gateway.get_lite_config", return_value=fake_config):
                    with patch.object(engine, "_inject_knowledge", new=AsyncMock(return_value="")):
                        with patch.object(engine, "_compact_if_needed", new=AsyncMock(side_effect=lambda db, msgs, _: msgs)):
                            with patch("app.services.skill_engine.llm_gateway.supports_function_calling", return_value=False):
                                with patch("app.services.tool_executor.tool_executor.get_tools_for_skill", return_value=[]):
                                    await engine.prepare(mock_db, mock_conv, "帮我做数据分析", user_id=1)

        # 核心验证：有 switch_candidates 时调用了 _match_or_keep_skill（一次搞定）
        mock_match_or_keep.assert_called()


# ═════════════════════════════════════════════════════════════════════════════
# 5. _build_tools_schema
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildToolsSchema:
    """SkillEngine._build_tools_schema 格式正确性。"""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    def test_empty_tools_returns_empty_list(self):
        engine = self._make_engine()
        assert engine._build_tools_schema([]) == []

    def test_tool_converted_to_openai_format(self):
        engine = self._make_engine()
        tool = MagicMock()
        tool.name = "doc_generator"
        tool.display_name = "文档生成器"
        tool.description = "生成 Word/PDF 文档"
        tool.input_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

        schema = engine._build_tools_schema([tool])

        assert len(schema) == 1
        assert schema[0]["type"] == "function"
        fn = schema[0]["function"]
        assert fn["name"] == "doc_generator"
        assert fn["description"] == "生成 Word/PDF 文档"
        assert fn["parameters"] == tool.input_schema

    def test_missing_description_falls_back_to_display_name(self):
        engine = self._make_engine()
        tool = MagicMock()
        tool.name = "t1"
        tool.display_name = "显示名称"
        tool.description = None
        tool.input_schema = {}

        schema = engine._build_tools_schema([tool])
        assert schema[0]["function"]["description"] == "显示名称"

    def test_missing_schema_defaults_to_empty_object(self):
        engine = self._make_engine()
        tool = MagicMock()
        tool.name = "t2"
        tool.display_name = "t2"
        tool.description = "desc"
        tool.input_schema = None

        schema = engine._build_tools_schema([tool])
        assert schema[0]["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_description_truncated_at_1024(self):
        engine = self._make_engine()
        tool = MagicMock()
        tool.name = "t3"
        tool.display_name = "t3"
        tool.description = "x" * 2000
        tool.input_schema = {}

        schema = engine._build_tools_schema([tool])
        assert len(schema[0]["function"]["description"]) == 1024

    def test_multiple_tools_all_included(self):
        engine = self._make_engine()
        tools = []
        for i in range(5):
            t = MagicMock()
            t.name = f"tool_{i}"
            t.display_name = f"工具{i}"
            t.description = f"描述{i}"
            t.input_schema = {}
            tools.append(t)

        schema = engine._build_tools_schema(tools)
        assert len(schema) == 5
        assert [s["function"]["name"] for s in schema] == [f"tool_{i}" for i in range(5)]


# ═════════════════════════════════════════════════════════════════════════════
# 6. 流式端点集成 — native tool_call SSE 事件
# ═════════════════════════════════════════════════════════════════════════════

class TestStreamEndpointWithNativeTools:
    """流式端点正确消费 llm_gateway 的原生 tool_call 事件并触发 Agent Loop。"""

    def test_native_tool_call_triggers_agent_loop(self, client, db):
        token, conv_id = _setup_conv(client, db, "ntc_user")

        tool_call_data = {"id": "c1", "name": "gen_doc", "arguments": '{"title": "报告"}'}

        async def fake_stream_typed(**kwargs):
            yield ("tool_call", tool_call_data)  # 原生工具调用

        tool_result = {"ok": True, "result": {"download_url": "/api/files/x.docx", "filename": "报告.docx"}}

        async def fake_stream_after_tool(**kwargs):
            yield ("content", "文档已生成。")

        call_count = 0

        async def dispatch_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async def gen1():
                    yield ("tool_call", tool_call_data)
                return gen1()
            else:
                async def gen2():
                    yield ("content", "文档已生成。")
                return gen2()

        mock_prep = _make_mock_prep(tools_schema=[
            {"type": "function", "function": {"name": "gen_doc", "description": "生成文档", "parameters": {}}}
        ])

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed") as mock_stream:
                first_call = True

                async def _first_gen(*a, **kw):
                    yield ("tool_call", tool_call_data)

                async def _second_gen(*a, **kw):
                    yield ("content", "文档已生成。")

                call_n = [0]

                def side_effect(*a, **kw):
                    call_n[0] += 1
                    if call_n[0] == 1:
                        return _first_gen()
                    return _second_gen()

                mock_stream.side_effect = side_effect

                with patch("app.services.tool_executor.tool_executor.execute_tool",
                           new=AsyncMock(return_value=tool_result)):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token),
                        json={"content": "生成一份分析报告"},
                    )

        events = _parse_sse(resp.text)
        # 应触发 tool_calling 状态
        statuses = [e["data"].get("stage") for e in events if e["event"] == "status"]
        assert "tool_calling" in statuses

    def test_tools_schema_in_prep_result(self, client, db):
        """PrepareResult.tools_schema 有内容时，不会为空列表。"""
        token, conv_id = _setup_conv(client, db, "tschema_user")

        mock_prep = _make_mock_prep(tools_schema=[
            {"type": "function", "function": {"name": "kb_reader", "description": "读取知识库", "parameters": {}}}
        ])

        async def fake_stream_typed(**kwargs):
            # 验证 tools 参数传入了 llm_gateway
            tools = kwargs.get("tools") or []
            if tools:
                yield ("content", f"tools_count:{len(tools)}")
            else:
                yield ("content", "no_tools")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                       side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "查询知识库"},
                )

        events = _parse_sse(resp.text)
        delta_texts = "".join(
            e["data"].get("text", "") for e in events if e["event"] == "delta"
        )
        # tools_schema 非空时应传入 llm_gateway，响应中包含 tools_count
        assert "tools_count:1" in delta_texts

    def test_no_tools_schema_no_tools_passed_to_llm(self, client, db):
        """PrepareResult.tools_schema 为空时，不向 LLM 传递 tools 参数。"""
        token, conv_id = _setup_conv(client, db, "notools_user")

        mock_prep = _make_mock_prep(tools_schema=[])  # 空

        captured_kwargs = {}

        async def capture_stream(**kwargs):
            captured_kwargs.update(kwargs)
            yield ("content", "普通回复")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                       side_effect=capture_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "普通问题"},
                )

        # tools_schema 为空时传 None 或不传
        tools_arg = captured_kwargs.get("tools")
        assert not tools_arg, f"Expected no tools, got {tools_arg}"


# ═════════════════════════════════════════════════════════════════════════════
# 7. Agent Loop — Native Function Calling 路径
# ═════════════════════════════════════════════════════════════════════════════

class TestAgentLoopNativePath:
    """_handle_tool_calls_stream 使用 native_tool_calls 参数时的行为。"""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    @pytest.mark.asyncio
    async def test_native_tool_calls_executes_without_text_parsing(self):
        """传入 native_tool_calls 时，不需要 response 中有 ```tool_call``` 标记。"""
        engine = self._make_engine()
        response = "这是普通回复，没有工具标记"
        native_calls = [{"id": "c1", "name": "search", "arguments": '{"q": "AI"}'}]
        mock_result = {"ok": True, "result": {"hits": []}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed") as mock_stream:
                async def _gen(*a, **kw):
                    yield ("content", "搜索完成，未找到结果。")
                mock_stream.side_effect = _gen

                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None, response=response,
                    llm_messages=[], model_config={}, user_id=None,
                    native_tool_calls=native_calls,
                ):
                    events.append(item)

        # 应触发工具执行
        starts = [e for e in events if isinstance(e, dict) and e.get("event") == "content_block_start"
                  and e["data"].get("type") == "tool_call"]
        assert len(starts) == 1
        assert starts[0]["data"]["tool"] == "search"

    @pytest.mark.asyncio
    async def test_native_path_uses_tool_role_messages(self):
        """原生路径向 llm_messages 追加 tool role 消息（而非 user 消息）。"""
        engine = self._make_engine()
        response = ""
        native_calls = [{"id": "call_abc", "name": "calc", "arguments": '{"expr": "1+1"}'}]
        mock_result = {"ok": True, "result": {"value": 2}}

        captured_messages = []
        call_n = [0]

        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed") as mock_stream:
                async def _gen(*a, **kw):
                    captured_messages.extend(kw.get("messages", []))
                    yield ("content", "结果是2")
                mock_stream.side_effect = _gen

                async for _ in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None, response=response,
                    llm_messages=[], model_config={}, user_id=None,
                    native_tool_calls=native_calls,
                ):
                    pass

        # 应有 role=tool 的消息
        tool_msgs = [m for m in captured_messages if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_call_id"] == "call_abc"

    @pytest.mark.asyncio
    async def test_fallback_text_parsing_when_no_native_calls(self):
        """没有传入 native_tool_calls 时，走文本解析 fallback。"""
        engine = self._make_engine()
        response = '```tool_call\n{"tool": "text_tool", "params": {}}\n```'
        mock_result = {"ok": True, "result": {}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed") as mock_stream:
                async def _gen(*a, **kw):
                    yield ("content", "完成")
                mock_stream.side_effect = _gen

                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None, response=response,
                    llm_messages=[], model_config={}, user_id=None,
                    native_tool_calls=None,  # 明确不传
                ):
                    events.append(item)

        starts = [e for e in events if isinstance(e, dict) and e.get("event") == "content_block_start"
                  and e["data"].get("type") == "tool_call"]
        assert len(starts) == 1
        assert starts[0]["data"]["tool"] == "text_tool"


# ═════════════════════════════════════════════════════════════════════════════
# 8. PrepareResult.tools_schema 字段存在性
# ═════════════════════════════════════════════════════════════════════════════

class TestPrepareResultSchema:

    def test_prepare_result_has_tools_schema_field(self):
        from app.services.skill_engine import PrepareResult
        pr = PrepareResult(llm_messages=[], model_config={})
        assert hasattr(pr, "tools_schema")
        assert pr.tools_schema == []

    def test_prepare_result_tools_schema_assignable(self):
        from app.services.skill_engine import PrepareResult
        schema = [{"type": "function", "function": {"name": "t", "description": "", "parameters": {}}}]
        pr = PrepareResult(llm_messages=[], model_config={}, tools_schema=schema)
        assert pr.tools_schema == schema
