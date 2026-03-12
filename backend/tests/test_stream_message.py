"""TC-STREAM: stream_message SSE 事件序列、early_return、thinking block、done 事件。"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tests.conftest import _make_user, _make_dept, _make_model_config, _login, _auth
from app.models.user import Role


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


def _setup(client, db):
    dept = _make_dept(db)
    _make_user(db, f"strm_{id(db)}", Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, f"strm_{id(db)}")
    r = client.post("/api/conversations", headers=_auth(token))
    return token, r.json()["id"]


def _make_prep(extra=None):
    prep = MagicMock()
    prep.early_return = None
    prep.skill_name = None
    prep.skill_id = None
    prep.skill_version = None
    prep.tools_schema = None
    prep.llm_messages = []
    prep.model_config = {"context_window": 32000}
    if extra:
        for k, v in extra.items():
            setattr(prep, k, v)
    return prep


# ─── 普通文本流 ────────────────────────────────────────────────────────────────

class TestStreamMessageText:
    def test_sse_content_type(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "你好")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "测试"},
                )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_standard_sse_event_sequence(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "你好")
            yield ("content", "世界")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "测试"},
                )

        events = _parse_sse(resp.text)
        event_types = [e["event"] for e in events]

        # 必须包含关键事件
        assert "status" in event_types
        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types
        assert "content_block_stop" in event_types
        assert "done" in event_types

        # 顺序：preparing → generating → blocks → done
        statuses = [e["data"]["stage"] for e in events if e["event"] == "status"]
        assert statuses[0] == "preparing"
        assert "generating" in statuses

    def test_delta_backward_compat_emitted(self, client, db):
        """每个 content chunk 同时发 delta 事件（向前兼容）。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "文字")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "hi"},
                )

        events = _parse_sse(resp.text)
        delta_events = [e for e in events if e["event"] == "delta"]
        assert len(delta_events) >= 1
        assert delta_events[0]["data"]["text"] == "文字"

    def test_done_event_has_message_id_and_token_usage(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.llm_messages = [{"role": "user", "content": "x" * 200}]

        async def fake_stream(**kwargs):
            yield ("content", "回复")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "问题"},
                )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        done = done_events[0]["data"]
        assert "message_id" in done
        assert "token_usage" in done
        assert "input_tokens" in done["token_usage"]
        assert "output_tokens" in done["token_usage"]
        assert "context_limit" in done["token_usage"]

    def test_message_persisted_after_stream(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "持久化内容")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "测试持久化"},
                )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        contents = [m["content"] for m in msgs]
        assert "持久化内容" in contents


# ─── Thinking block ───────────────────────────────────────────────────────────

class TestStreamThinkingBlock:
    def test_thinking_block_events(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("thinking", "正在思考...")
            yield ("content", "最终答案")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "复杂问题"},
                )

        events = _parse_sse(resp.text)
        block_starts = [e for e in events if e["event"] == "content_block_start"]
        types = [e["data"]["type"] for e in block_starts]
        assert "thinking" in types
        assert "text" in types

        # thinking block 在 text block 之前
        thinking_idx = types.index("thinking")
        text_idx = types.index("text")
        assert thinking_idx < text_idx

    def test_block_idx_increments(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("thinking", "思")
            yield ("content", "答")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "q"},
                )

        events = _parse_sse(resp.text)
        block_starts = [e for e in events if e["event"] == "content_block_start"]
        indices = [e["data"]["index"] for e in block_starts]
        assert len(set(indices)) == len(indices), "block index 应唯一"
        assert indices == sorted(indices), "block index 应递增"


# ─── Early return 路径 ─────────────────────────────────────────────────────────

class TestStreamEarlyReturn:
    def test_early_return_yields_delta_and_done_only(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.early_return = ("追问：请提供更多信息。", {})

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token), json={"content": "帮我分析"},
            )

        events = _parse_sse(resp.text)
        event_types = [e["event"] for e in events]

        assert "delta" in event_types
        assert "done" in event_types
        # early return 不经过 LLM，没有 content_block_start
        assert "content_block_start" not in event_types

        delta_texts = [e["data"]["text"] for e in events if e["event"] == "delta"]
        assert "追问：请提供更多信息。" in delta_texts

    def test_early_return_message_persisted(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.early_return = ("请补充品牌名称。", {})

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token), json={"content": "帮我"},
            )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        contents = [m["content"] for m in msgs]
        assert "请补充品牌名称。" in contents


# ─── 错误处理 ──────────────────────────────────────────────────────────────────

class TestStreamErrors:
    def test_llm_error_yields_error_event(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            raise Exception("HTTP 429 Too Many Requests")
            yield  # make it an async generator

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "触发限流"},
                )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) >= 1
        err = error_events[0]["data"]
        assert err["error_type"] == "rate_limit"
        assert err["retryable"] is True

    def test_network_error_retryable(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            raise Exception("connection timeout")
            yield

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "超时测试"},
                )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert error_events[0]["data"]["error_type"] == "network"
        assert error_events[0]["data"]["retryable"] is True

    def test_context_overflow_not_retryable(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            raise Exception("input length too large for model")
            yield

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "过长输入"},
                )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert error_events[0]["data"]["error_type"] == "context_overflow"
        assert error_events[0]["data"]["retryable"] is False

    def test_stream_unauthorized(self, client, db):
        _make_dept(db)
        db.commit()
        resp = client.post("/api/conversations/1/messages/stream",
                           json={"content": "未登录"})
        assert resp.status_code in (401, 403)

    def test_stream_other_users_conv(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "se_u1", Role.EMPLOYEE, dept.id)
        _make_user(db, "se_u2", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()

        t1 = _login(client, "se_u1")
        t2 = _login(client, "se_u2")
        r = client.post("/api/conversations", headers=_auth(t1))
        conv_id = r.json()["id"]

        resp = client.post(
            f"/api/conversations/{conv_id}/messages/stream",
            headers=_auth(t2), json={"content": "入侵"},
        )
        assert resp.status_code == 404


# ─── 会话标题更新 ──────────────────────────────────────────────────────────────

class TestStreamConvTitle:
    def test_title_set_on_first_message(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "回复")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "这是我的第一个问题"},
                )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        conv = next(c for c in convs if c["id"] == conv_id)
        assert "这是我的第一个问题" in conv["title"]

    def test_title_truncated_at_60(self, client, db):
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "ok")

        long_content = "A" * 100
        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": long_content},
                )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        conv = next(c for c in convs if c["id"] == conv_id)
        assert len(conv["title"]) <= 60
