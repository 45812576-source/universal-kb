"""TC-AGENT: Tests for Agent Loop improvements, SSE streaming, error classification,
Thinking Block support, and upload-stream endpoint.

Covers:
- _classify_error() error type detection
- _handle_tool_calls_stream() round_start / round_end / tool_progress events
- chat_stream_typed() thinking vs content chunks
- stream endpoint: content_block_start with type=thinking
- upload-stream endpoint: SSE response format
- done event: token_usage field present
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tests.conftest import _make_user, _make_dept, _login, _auth, _make_model_config
from app.models.user import Role


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    """Parse SSE text into list of {event, data} dicts."""
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


# ─────────────────────────────────────────────────────────────────────────────
# 1. _classify_error
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyError:
    def setup_method(self):
        from app.routers.conversations import _classify_error
        self.classify = _classify_error

    def test_rate_limit_429(self):
        assert self.classify(Exception("HTTP 429 Too Many Requests")) == "rate_limit"

    def test_rate_limit_quota(self):
        assert self.classify(Exception("quota exceeded for this model")) == "rate_limit"

    def test_rate_limit_rate(self):
        assert self.classify(Exception("rate limit reached")) == "rate_limit"

    def test_context_overflow_token(self):
        assert self.classify(Exception("maximum token length exceeded")) == "context_overflow"

    def test_context_overflow_length(self):
        assert self.classify(Exception("input length too large")) == "context_overflow"

    def test_context_overflow_too_long(self):
        assert self.classify(Exception("prompt is too long")) == "context_overflow"

    def test_network_connect(self):
        assert self.classify(Exception("connection refused")) == "network"

    def test_network_timeout(self):
        assert self.classify(Exception("timeout connecting to LLM")) == "network"

    def test_server_error_default(self):
        assert self.classify(Exception("internal server error")) == "server_error"

    def test_server_error_unknown(self):
        assert self.classify(Exception("something weird happened")) == "server_error"

    def test_retryable_types(self):
        retryable = {"rate_limit", "network"}
        for err_type in retryable:
            assert err_type in retryable

    def test_non_retryable_types(self):
        non_retryable = {"context_overflow", "server_error"}
        retryable = {"rate_limit", "network"}
        for err_type in non_retryable:
            assert err_type not in retryable


# ─────────────────────────────────────────────────────────────────────────────
# 2. _handle_tool_calls_stream — round events
# ─────────────────────────────────────────────────────────────────────────────

class TestHandleToolCallsStream:
    """Tests for the agent loop streaming generator."""

    def _make_engine(self):
        from app.services.skill_engine import SkillEngine
        return SkillEngine()

    @pytest.mark.asyncio
    async def test_no_tool_calls_yields_only_final(self):
        engine = self._make_engine()
        response = "这是一个没有工具调用的普通回复"
        events = []
        async for item in engine._handle_tool_calls_stream(
            db=MagicMock(), skill=None,
            response=response, llm_messages=[],
            model_config={}, user_id=None,
        ):
            events.append(item)

        # Should yield only the final (response, meta) tuple
        assert len(events) == 1
        assert isinstance(events[0], tuple)
        final_response, meta = events[0]
        assert final_response == response
        assert meta == {}

    def _fake_stream_factory(self, chunks):
        """Return a function that, when called, returns an async generator yielding chunks."""
        async def _gen(*args, **kwargs):
            for chunk in chunks:
                yield chunk
        return _gen

    @pytest.mark.asyncio
    async def test_round_start_emitted(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "test_tool", "params": {}}\n```'
        mock_result = {"ok": True, "result": {"data": "ok"}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "工具执行完毕")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        round_start_events = [e for e in events if isinstance(e, dict) and e.get("event") == "round_start"]
        assert len(round_start_events) >= 1
        first_round = round_start_events[0]["data"]
        assert first_round["round"] == 1
        assert first_round["max_rounds"] == 5

    @pytest.mark.asyncio
    async def test_round_end_emitted(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "test_tool", "params": {}}\n```'
        mock_result = {"ok": True, "result": {}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "完成")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        round_end_events = [e for e in events if isinstance(e, dict) and e.get("event") == "round_end"]
        assert len(round_end_events) >= 1
        last_round_end = round_end_events[-1]["data"]
        assert "round" in last_round_end
        assert "has_next" in last_round_end
        assert last_round_end["has_next"] is False

    @pytest.mark.asyncio
    async def test_tool_progress_emitted(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "my_tool", "params": {"x": 1}}\n```'
        mock_result = {"ok": True, "result": {}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "done")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        progress_events = [e for e in events if isinstance(e, dict) and e.get("event") == "tool_progress"]
        assert len(progress_events) >= 1
        assert "message" in progress_events[0]["data"]
        assert "phase" in progress_events[0]["data"]

    @pytest.mark.asyncio
    async def test_content_block_start_for_tool_call(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "calc", "params": {"a": 2}}\n```'
        mock_result = {"ok": True, "result": {"value": 4}}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "结果是4")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        block_starts = [e for e in events if isinstance(e, dict) and e.get("event") == "content_block_start"]
        tool_starts = [e for e in block_starts if e["data"].get("type") == "tool_call"]
        assert len(tool_starts) >= 1
        assert tool_starts[0]["data"]["tool"] == "calc"

    @pytest.mark.asyncio
    async def test_tool_failure_emits_block_stop_with_ok_false(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "bad_tool", "params": {}}\n```'
        mock_result = {"ok": False, "error": "tool not found"}

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "工具失败")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        block_stops = [e for e in events if isinstance(e, dict) and e.get("event") == "content_block_stop"]
        tool_stops = [e for e in block_stops if e["data"].get("type") == "tool_call"]
        assert any(s["data"]["ok"] is False for s in tool_stops)

    @pytest.mark.asyncio
    async def test_download_url_extracted_from_tool_result(self):
        engine = self._make_engine()
        tool_response = '```tool_call\n{"tool": "ppt_gen", "params": {}}\n```'
        mock_result = {"ok": True, "result": {"download_url": "/api/files/abc.pptx", "filename": "deck.pptx"}}

        final_meta = {}
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=self._fake_stream_factory([("content", "PPT已生成")])):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=tool_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    if isinstance(item, tuple):
                        _, final_meta = item

        assert final_meta.get("download_url") == "/api/files/abc.pptx"
        assert final_meta.get("download_filename") == "deck.pptx"

    @pytest.mark.asyncio
    async def test_multi_round_increments_round_number(self):
        """Two rounds of tool calls → round_start events with round=1,2."""
        engine = self._make_engine()
        first_response = '```tool_call\n{"tool": "step1", "params": {}}\n```'
        second_llm_response = '```tool_call\n{"tool": "step2", "params": {}}\n```'
        third_llm_response = "最终回复，无工具"

        mock_result = {"ok": True, "result": {}}
        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield ("content", second_llm_response)
            else:
                yield ("content", third_llm_response)

        events = []
        with patch("app.services.tool_executor.tool_executor.execute_tool",
                   new=AsyncMock(return_value=mock_result)):
            with patch("app.services.skill_engine.llm_gateway.chat_stream_typed",
                       new=fake_stream):
                async for item in engine._handle_tool_calls_stream(
                    db=MagicMock(), skill=None,
                    response=first_response, llm_messages=[],
                    model_config={}, user_id=None,
                ):
                    events.append(item)

        round_starts = [e for e in events if isinstance(e, dict) and e.get("event") == "round_start"]
        rounds = [e["data"]["round"] for e in round_starts]
        assert 1 in rounds
        assert 2 in rounds


# ─────────────────────────────────────────────────────────────────────────────
# 3. chat_stream_typed — thinking vs content
# ─────────────────────────────────────────────────────────────────────────────

class TestChatStreamTyped:
    def _make_gateway(self):
        from app.services.llm_gateway import LLMGateway
        return LLMGateway()

    @pytest.mark.asyncio
    async def test_content_only(self):
        gw = self._make_gateway()
        fake_lines = [
            'data: {"choices":[{"delta":{"content":"hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]

        def fake_stream_get(*args, **kwargs):
            class FakeResp:
                async def aiter_lines(self):
                    for line in fake_lines:
                        yield line
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
            return FakeResp()

        chunks = []
        with patch("httpx.AsyncClient.stream", side_effect=fake_stream_get):
            async for ctype, text in gw.chat_stream_typed(
                model_config={"api_base": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 100, "temperature": "0.7"},
                messages=[],
            ):
                chunks.append((ctype, text))

        assert chunks == [("content", "hello"), ("content", " world")]

    @pytest.mark.asyncio
    async def test_reasoning_content_yields_thinking(self):
        gw = self._make_gateway()
        fake_lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"思考中..."}}]}',
            'data: {"choices":[{"delta":{"content":"最终回答"}}]}',
            "data: [DONE]",
        ]

        def fake_stream_get(*args, **kwargs):
            class FakeResp:
                async def aiter_lines(self):
                    for line in fake_lines:
                        yield line
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
            return FakeResp()

        chunks = []
        with patch("httpx.AsyncClient.stream", side_effect=fake_stream_get):
            async for ctype, text in gw.chat_stream_typed(
                model_config={"api_base": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 100, "temperature": "0.7"},
                messages=[],
            ):
                chunks.append((ctype, text))

        assert ("thinking", "思考中...") in chunks
        assert ("content", "最终回答") in chunks

    @pytest.mark.asyncio
    async def test_both_reasoning_and_content_in_same_chunk(self):
        gw = self._make_gateway()
        fake_lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"r","content":"c"}}]}',
            "data: [DONE]",
        ]

        def fake_stream_get(*args, **kwargs):
            class FakeResp:
                async def aiter_lines(self):
                    for line in fake_lines:
                        yield line
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
            return FakeResp()

        chunks = []
        with patch("httpx.AsyncClient.stream", side_effect=fake_stream_get):
            async for ctype, text in gw.chat_stream_typed(
                model_config={"api_base": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 100, "temperature": "0.7"},
                messages=[],
            ):
                chunks.append((ctype, text))

        types = [c[0] for c in chunks]
        assert "thinking" in types
        assert "content" in types

    @pytest.mark.asyncio
    async def test_chat_stream_backward_compat_yields_only_content(self):
        """chat_stream() (old API) should only yield content text, not thinking."""
        gw = self._make_gateway()
        fake_lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}',
            'data: {"choices":[{"delta":{"content":"回答"}}]}',
            "data: [DONE]",
        ]

        def fake_stream_get(*args, **kwargs):
            class FakeResp:
                async def aiter_lines(self):
                    for line in fake_lines:
                        yield line
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
            return FakeResp()

        chunks = []
        with patch("httpx.AsyncClient.stream", side_effect=fake_stream_get):
            async for text in gw.chat_stream(
                model_config={"api_base": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 100, "temperature": "0.7"},
                messages=[],
            ):
                chunks.append(text)

        assert chunks == ["回答"]  # thinking NOT yielded


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stream endpoint — thinking block events in SSE output
# ─────────────────────────────────────────────────────────────────────────────

def _setup_conv(client, db):
    dept = _make_dept(db)
    user = _make_user(db, f"stream_{id(client)}", Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, f"stream_{id(client)}")
    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]
    return token, conv_id


class TestStreamEndpointSSE:

    def test_stream_returns_sse_content_type(self, client, db):
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "你好")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "你好"},
                )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_stream_emits_status_preparing(self, client, db):
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "回复")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "测试"},
                )

        events = _parse_sse(resp.text)
        status_events = [e for e in events if e["event"] == "status"]
        stages = [e["data"].get("stage") for e in status_events]
        assert "preparing" in stages

    def test_stream_emits_done_with_message_id(self, client, db):
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "完整回复")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "测试"},
                )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert "message_id" in done_events[0]["data"]
        assert done_events[0]["data"]["message_id"] is not None

    def test_stream_done_includes_token_usage(self, client, db):
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "回复内容")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = [{"role": "user", "content": "你好"}]
        mock_prep.model_config = {"context_window": 8000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "你好"},
                )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        usage = done_events[0]["data"].get("token_usage")
        assert usage is not None
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "estimated_context_used" in usage
        assert "context_limit" in usage
        assert usage["context_limit"] == 8000

    def test_stream_thinking_block_events_emitted(self, client, db):
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("thinking", "让我想一想...")
            yield ("content", "答案是42。")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "宇宙的意义"},
                )

        events = _parse_sse(resp.text)
        block_starts = [e for e in events if e["event"] == "content_block_start"]
        thinking_starts = [e for e in block_starts if e["data"].get("type") == "thinking"]
        text_starts = [e for e in block_starts if e["data"].get("type") == "text"]

        assert len(thinking_starts) >= 1, "Should emit thinking block_start"
        assert len(text_starts) >= 1, "Should emit text block_start after thinking"

    def test_stream_thinking_block_index_ordering(self, client, db):
        """Thinking block should be index 0, text block should be index 1."""
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("thinking", "思考...")
            yield ("content", "回答")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "test"},
                )

        events = _parse_sse(resp.text)
        block_starts = [e for e in events if e["event"] == "content_block_start"]

        thinking_start = next((e for e in block_starts if e["data"].get("type") == "thinking"), None)
        text_start = next((e for e in block_starts if e["data"].get("type") == "text"), None)

        assert thinking_start is not None
        assert text_start is not None
        assert thinking_start["data"]["index"] < text_start["data"]["index"]

    def test_stream_error_includes_error_type_and_retryable(self, client, db):
        token, conv_id = _setup_conv(client, db)

        with patch("app.services.skill_engine.skill_engine.prepare",
                   new=AsyncMock(side_effect=Exception("HTTP 429 rate limit"))):
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": "触发限流"},
            )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) == 1
        err = error_events[0]["data"]
        assert "error_type" in err
        assert "retryable" in err
        assert err["error_type"] == "rate_limit"
        assert err["retryable"] is True

    def test_stream_server_error_not_retryable(self, client, db):
        token, conv_id = _setup_conv(client, db)

        with patch("app.services.skill_engine.skill_engine.prepare",
                   new=AsyncMock(side_effect=Exception("internal server error"))):
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": "触发服务端错误"},
            )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) == 1
        assert error_events[0]["data"]["retryable"] is False

    def test_stream_content_only_no_thinking_block(self, client, db):
        """Normal models without reasoning don't emit thinking blocks."""
        token, conv_id = _setup_conv(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "直接回答，无思考过程。")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "普通问题"},
                )

        events = _parse_sse(resp.text)
        thinking_starts = [e for e in events if e["event"] == "content_block_start"
                           and e["data"].get("type") == "thinking"]
        assert len(thinking_starts) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. upload-stream endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestUploadStreamEndpoint:

    def _setup(self, client, db):
        dept = _make_dept(db)
        _make_user(db, f"ups_{id(db)}", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()
        token = _login(client, f"ups_{id(db)}")
        r = client.post("/api/conversations", headers=_auth(token))
        return token, r.json()["id"]

    def test_upload_stream_returns_sse(self, client, db):
        token, conv_id = self._setup(client, db)

        fake_file_content = b"This is a short test document."
        fake_text = "This is a short test document."

        async def fake_stream_typed(**kwargs):
            yield ("content", "文件分析完成。")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.utils.file_parser.extract_text", return_value=fake_text):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("test.txt", fake_file_content, "text/plain")},
                        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_upload_stream_emits_uploading_status(self, client, db):
        token, conv_id = self._setup(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "ok")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.utils.file_parser.extract_text", return_value="文件内容"):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("doc.txt", b"hello", "text/plain")},
                        )

        events = _parse_sse(resp.text)
        stages = [e["data"].get("stage") for e in events if e["event"] == "status"]
        assert "uploading" in stages
        assert "parsing" in stages

    def test_upload_stream_emits_done(self, client, db):
        token, conv_id = self._setup(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "分析完毕")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.utils.file_parser.extract_text", return_value="内容"):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("report.txt", b"data", "text/plain")},
                        )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert "message_id" in done_events[0]["data"]

    def test_upload_stream_done_includes_token_usage(self, client, db):
        token, conv_id = self._setup(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("content", "结果")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = [{"role": "user", "content": "x" * 100}]
        mock_prep.model_config = {"context_window": 16000}

        with patch("app.utils.file_parser.extract_text", return_value="内容"):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("f.txt", b"x", "text/plain")},
                        )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        usage = done_events[0]["data"].get("token_usage")
        assert usage is not None
        assert usage["context_limit"] == 16000
        assert usage["estimated_context_used"] > 0

    def test_upload_stream_other_user_forbidden(self, client, db):
        token, conv_id = self._setup(client, db)
        dept = _make_dept(db)
        _make_user(db, "other_user_upstr", Role.EMPLOYEE, dept.id)
        db.commit()
        other_token = _login(client, "other_user_upstr")

        resp = client.post(
            f"/api/conversations/{conv_id}/messages/upload-stream",
            headers=_auth(other_token),
            files={"file": ("f.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 404

    def test_upload_stream_thinking_blocks_forwarded(self, client, db):
        token, conv_id = self._setup(client, db)

        async def fake_stream_typed(**kwargs):
            yield ("thinking", "分析文件内容...")
            yield ("content", "文件摘要如下。")

        mock_prep = MagicMock()
        mock_prep.early_return = None
        mock_prep.skill_name = None
        mock_prep.skill_version = None
        mock_prep.skill_id = None
        mock_prep.llm_messages = []
        mock_prep.model_config = {"context_window": 32000}

        with patch("app.utils.file_parser.extract_text", return_value="文件内容"):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=mock_prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", side_effect=fake_stream_typed):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("doc.txt", b"content", "text/plain")},
                        )

        events = _parse_sse(resp.text)
        thinking_starts = [e for e in events if e["event"] == "content_block_start"
                           and e["data"].get("type") == "thinking"]
        assert len(thinking_starts) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. parse_sse helper self-test
# ─────────────────────────────────────────────────────────────────────────────

class TestParseSSEHelper:
    def test_parse_simple(self):
        text = "event: done\ndata: {\"x\": 1}\n\n"
        events = _parse_sse(text)
        assert events == [{"event": "done", "data": {"x": 1}}]

    def test_parse_multiple_events(self):
        text = (
            "event: status\ndata: {\"stage\": \"preparing\"}\n\n"
            "event: delta\ndata: {\"text\": \"hello\"}\n\n"
            "event: done\ndata: {\"message_id\": 42}\n\n"
        )
        events = _parse_sse(text)
        assert len(events) == 3
        assert events[0]["event"] == "status"
        assert events[1]["data"]["text"] == "hello"
        assert events[2]["data"]["message_id"] == 42

    def test_parse_skips_malformed_json(self):
        text = "event: bad\ndata: not-json\n\nevent: ok\ndata: {\"v\": 1}\n\n"
        events = _parse_sse(text)
        assert len(events) == 1
        assert events[0]["data"]["v"] == 1
