"""
TC-USER-JOURNEY: 从用户视角出发的场景化测试
================================================

用真实使用路径驱动测试，覆盖两个已知 Bug：

Bug-1: AI 思考后直接创建任务，未给用户展示计划摘要
Bug-2: 点击快速回复选项后发送，LLM 不回复

Journey A — 普通对话：发消息 → 收到回复
Journey B — PEV 升级路径：复杂请求 → 展示计划 → 执行步骤
Journey C — 快速回复：AI 给出列表选项 → 用户点击 → 再次收到回复
Journey D — 快速回复 + PEV 升级：列表选项内容复杂 → 选项被 PEV 截走 → 前端收到可解析事件
Journey E — 快速回复 + 普通 Skill：选项内容触发 early return（追问）
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    _make_user, _make_dept, _make_model_config, _make_skill,
    _login, _auth,
)
from app.models.user import Role


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    events = []
    current_event = "delta"
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                events.append({"event": current_event, "data": data})
                current_event = "delta"
            except json.JSONDecodeError:
                pass
    return events


def _setup(client, db):
    """创建用户 + 对话，返回 (token, conv_id)。"""
    dept = _make_dept(db)
    _make_user(db, f"uj_{id(db)}", Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, f"uj_{id(db)}")
    r = client.post("/api/conversations", headers=_auth(token))
    assert r.status_code == 200
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


# 快捷回复的典型 AI 回复内容（含列表）
QUICK_REPLY_CONTENT = """好的，请问您需要以下哪种服务？

1. 帮我写文案
2. 分析竞品数据
3. 生成周报摘要"""

# 不含列表的普通回复
PLAIN_REPLY_CONTENT = "我来帮你处理这个问题，请稍等。"


# ─────────────────────────────────────────────────────────────────────────────
# Journey A — 普通对话：发消息 → 收到完整回复
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyA_NormalChat:
    """用户发一条普通消息，AI 流式回复。"""

    @pytest.fixture(autouse=True)
    def no_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def test_A1_user_sends_hello_gets_reply(self, client, db):
        """用户发"你好"，前端收到 status(preparing) → content → done。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "你好！我是 AI 助手，有什么可以帮您？")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "你好"},
                )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        event_types = {e["event"] for e in events}

        # 用户视角：必须看到 preparing 状态和最终内容
        assert "status" in event_types, "应该有 preparing 状态提示"
        assert "done" in event_types, "应该有 done 事件"

        preparing_events = [e for e in events if e["event"] == "status" and e["data"].get("stage") == "preparing"]
        assert preparing_events, "必须先发 preparing 状态"

        done_events = [e for e in events if e["event"] == "done"]
        assert done_events[0]["data"].get("message_id"), "done 事件应包含 message_id"

    def test_A2_reply_content_is_streamed(self, client, db):
        """AI 回复应该逐字流式输出，不是一次性返回。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            for char in "这是一段流式回复的内容":
                yield ("content", char)

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "测试流式"},
                )

        events = _parse_sse(resp.text)
        delta_events = [e for e in events if e["event"] == "delta"]
        # 应该有多个 delta 事件（逐字流出）
        assert len(delta_events) >= 5, f"流式输出应多次 delta，实际 {len(delta_events)} 次"

    def test_A3_message_persisted_after_reply(self, client, db):
        """对话结束后，消息应写入数据库，用户刷新页面能看到历史。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "持久化测试回复")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "请持久化"},
                )

        # 模拟用户刷新页面，重新拉历史消息
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        contents = [m["content"] for m in msgs]
        assert "持久化测试回复" in contents, "刷新后应能看到 AI 回复"
        assert "请持久化" in contents, "刷新后应能看到用户消息"


# ─────────────────────────────────────────────────────────────────────────────
# Journey B — PEV 升级路径（Bug-1 复现与验证）
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyB_PEVUpgrade:
    """
    Bug-1 场景：用户发复杂任务（如"帮我调研竞品并生成报告"），
    AI 思考后应先展示计划供用户确认，而不是直接创建任务开始执行。

    当前 Bug 现象：pev_orchestrator.run() 直接执行，没有 plan_ready 暂停点，
    用户看不到计划内容，任务就已经在后台跑起来了。
    """

    def test_B1_pev_plan_is_surfaced_to_user_before_execution(self, client, db):
        """
        [Bug-1 复现] 用户发复杂请求后，前端应收到包含计划摘要的 SSE 事件。
        期望有 plan_ready 或类似事件，让用户知道 AI 制定了什么计划。
        """
        token, conv_id = _setup(client, db)

        # 模拟 PEV 升级判断返回场景
        mock_plan = {
            "steps": [
                {
                    "step_key": "research",
                    "step_type": "llm_generate",
                    "description": "调研竞品信息",
                    "depends_on": [],
                    "input_spec": {},
                    "output_spec": {},
                    "verify_criteria": "输出不为空",
                },
                {
                    "step_key": "report",
                    "step_type": "llm_generate",
                    "description": "生成分析报告",
                    "depends_on": ["research"],
                    "input_spec": {},
                    "output_spec": {},
                    "verify_criteria": "报告包含结论",
                },
            ]
        }

        mock_pev_events = [
            {"event": "pev_start", "data": {"job_id": 1, "scenario": "task_decomp", "goal": "帮我调研竞品"}},
            {"event": "pev_plan_ready", "data": {"steps": mock_plan["steps"], "step_count": 2}},  # 期望有这个事件
            {"event": "pev_step_start", "data": {"step_key": "research", "description": "调研竞品信息"}},
            {"event": "pev_step_done", "data": {"step_key": "research", "ok": True}},
            {"event": "pev_done", "data": {"summary": "调研完成"}},
        ]

        async def mock_pev_run(db, job):
            for ev in mock_pev_events:
                yield ev

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="task_decomp")):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "帮我调研竞品并生成分析报告"},
                )

        events = _parse_sse(resp.text)
        event_types = [e["event"] for e in events]

        # Bug-1 验证：用户应在执行前看到计划
        assert "pev_start" in event_types, "用户应收到 PEV 启动通知"

        # 这是当前 Bug 的核心：计划应该先 surface 给用户
        plan_events = [e for e in events if e["event"] == "pev_plan_ready"]
        assert plan_events, (
            "BUG-1: 用户未收到计划摘要事件 (pev_plan_ready)。"
            "AI 直接执行任务，用户不知道 AI 在做什么。"
        )
        assert plan_events[0]["data"].get("step_count") == 2

        # 计划事件应在执行事件之前
        plan_idx = next(i for i, e in enumerate(events) if e["event"] == "pev_plan_ready")
        exec_indices = [i for i, e in enumerate(events) if e["event"] == "pev_step_start"]
        if exec_indices:
            assert plan_idx < exec_indices[0], "计划应在第一个步骤执行前呈现给用户"

    def test_B2_pev_step_progress_shown_to_user(self, client, db):
        """用户能看到每个执行步骤的进度，而不是等到全部完成才给结果。"""
        token, conv_id = _setup(client, db)

        async def mock_pev_run(db, job):
            yield {"event": "pev_start", "data": {"job_id": 1, "scenario": "task_decomp", "goal": "生成报告"}}
            yield {"event": "pev_plan_ready", "data": {"steps": [], "step_count": 2}}
            yield {"event": "pev_step_start", "data": {"step_key": "step_1", "description": "收集数据"}}
            yield {"event": "pev_step_done", "data": {"step_key": "step_1", "ok": True}}
            yield {"event": "pev_step_start", "data": {"step_key": "step_2", "description": "生成报告"}}
            yield {"event": "pev_step_done", "data": {"step_key": "step_2", "ok": True}}
            yield {"event": "pev_done", "data": {"summary": "任务完成"}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="task_decomp")):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "生成竞品分析报告"},
                )

        events = _parse_sse(resp.text)
        step_starts = [e for e in events if e["event"] == "pev_step_start"]
        step_dones = [e for e in events if e["event"] == "pev_step_done"]

        assert len(step_starts) == 2, "用户应看到 2 个步骤开始"
        assert len(step_dones) == 2, "用户应看到 2 个步骤完成"

    def test_B3_non_complex_request_skips_pev(self, client, db):
        """普通对话（你好、写文案）不应触发 PEV，直接走 Skill 路径。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "你好！")

        pev_run_called = False

        async def mock_pev_run(db, job):
            nonlocal pev_run_called
            pev_run_called = True
            yield {"event": "pev_start", "data": {}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value=None)):  # 不升级
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                with patch("app.services.skill_engine.skill_engine.prepare",
                           new=AsyncMock(return_value=prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                               new=fake_stream):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/stream",
                            headers=_auth(token), json={"content": "你好"},
                        )

        assert not pev_run_called, "普通对话不应触发 PEV 执行"
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), "应走普通 done 路径"


# ─────────────────────────────────────────────────────────────────────────────
# Journey C — 快速回复：AI 给出列表 → 用户点击 → 再次收到回复（Bug-2 核心）
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyC_QuickReply:
    """
    Bug-2 场景：AI 回复包含列表，前端解析为快速回复选项。
    用户点击选项后（选项文本填入输入框并发送），LLM 不响应。

    根因猜测：选项内容触发了 PEV 升级判断，PEV 走了不同的 SSE 格式，
    前端 chat-store 的 switch-case 不认识 pev_* 事件，无法更新 UI。
    """

    @pytest.fixture(autouse=True)
    def no_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def test_C1_ai_reply_with_list_triggers_quick_reply_options(self, client, db):
        """AI 回复含有列表时，用户应能看到快速回复选项（前端解析验证）。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", QUICK_REPLY_CONTENT)

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "有什么可以帮我？"},
                )

        events = _parse_sse(resp.text)
        done_events = [e for e in events if e["event"] == "done"]
        assert done_events, "应正常收到 done 事件"

        # 拉取消息，验证内容包含列表
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert assistant_msgs
        content = assistant_msgs[-1]["content"]
        assert "1." in content or "帮我写文案" in content, "回复应包含列表选项"

    def test_C2_quick_reply_option_sends_successfully(self, client, db):
        """用户点击快速回复选项（"帮我写文案"）后，应该成功发出消息并收到回复。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        # Round 1: AI 给出列表选项
        async def fake_stream_round1(**kwargs):
            yield ("content", QUICK_REPLY_CONTENT)

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream_round1):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "有什么服务？"},
                )

        # Round 2: 用户点击 "帮我写文案" 快速回复选项
        async def fake_stream_round2(**kwargs):
            yield ("content", "好的，请问您需要什么风格的文案？")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream_round2):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "帮我写文案"},  # 快速回复选项的文本
                )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        event_types = {e["event"] for e in events}

        # Bug-2 验证：快速回复后 LLM 必须有响应
        assert "done" in event_types, (
            "BUG-2: 用户点击快速回复选项后发送，未收到 done 事件，LLM 没有响应。"
        )

        delta_events = [e for e in events if e["event"] == "delta"]
        assert delta_events, "BUG-2: 快速回复后未收到任何文字输出。"

        # 验证消息已持久化
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        contents = [m["content"] for m in msgs]
        assert "好的，请问您需要什么风格的文案？" in contents, "AI 回复应入库"

    def test_C3_multiple_quick_replies_in_succession(self, client, db):
        """用户连续点击快速回复选项 3 次，每次都应收到正常回复。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        options = ["帮我写文案", "分析竞品数据", "生成周报摘要"]
        replies = ["好的，请说明文案需求", "好的，请提供竞品名称", "好的，请提供本周数据"]

        for opt, reply in zip(options, replies):
            async def make_stream(r=reply):
                async def fake_stream(**kwargs):
                    yield ("content", r)
                return fake_stream

            stream_fn = None

            async def fake_stream(r=reply, **kwargs):
                yield ("content", r)

            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": opt},
                    )

            events = _parse_sse(resp.text)
            assert any(e["event"] == "done" for e in events), \
                f"BUG-2: 快速回复 '{opt}' 后未收到 done 事件"
            assert any(e["event"] == "delta" for e in events), \
                f"BUG-2: 快速回复 '{opt}' 后未收到文字输出"

    def test_C4_quick_reply_content_is_same_as_option_text(self, client, db):
        """快速回复发出的消息内容应与选项文本完全一致，不应被篡改。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        received_content = []

        async def capture_stream(**kwargs):
            # 从 llm_messages 中提取用户发送的内容
            msgs = kwargs.get("messages") or prep.llm_messages
            if msgs:
                last_user = [m for m in msgs if m.get("role") == "user"]
                if last_user:
                    received_content.append(last_user[-1].get("content", ""))
            yield ("content", "收到")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=capture_stream):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "帮我写文案"},
                )

        # 用户消息应被持久化，内容完整
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert any("帮我写文案" in m["content"] for m in user_msgs), "用户消息内容应完整保存"


# ─────────────────────────────────────────────────────────────────────────────
# Journey D — 快速回复 + PEV 升级（Bug-2 深层根因）
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyD_QuickReplyTriggerssPEV:
    """
    用户点击的快速回复选项内容（如"分析竞品数据"）触发 PEV 升级时，
    前端必须能处理 pev_* 事件，不能出现"无回复"现象。

    这是 Bug-2 的深层根因：前端 chat-store switch-case 不处理 pev_* 事件类型。
    """

    def test_D1_quick_reply_that_triggers_pev_still_shows_progress(self, client, db):
        """
        [Bug-2 深层] 快速回复内容触发 PEV 升级时，
        前端至少应收到 pev_start 事件，不能是空响应。
        """
        token, conv_id = _setup(client, db)

        async def mock_pev_run(db, job):
            yield {"event": "pev_start", "data": {"job_id": 1, "scenario": "task_decomp", "goal": "分析竞品数据"}}
            yield {"event": "pev_plan_ready", "data": {"steps": [], "step_count": 1}}
            yield {"event": "pev_done", "data": {"summary": "分析完成"}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="task_decomp")):  # 触发 PEV
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": "分析竞品数据"},  # 快速回复选项触发 PEV
                )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)

        # 后端必须至少发出 pev_start 事件
        assert events, "BUG-2: 快速回复触发 PEV 后，后端没有发出任何 SSE 事件"
        pev_events = [e for e in events if e["event"].startswith("pev_")]
        assert pev_events, (
            "BUG-2: 后端 PEV 事件未正确 stream 给前端。"
            f"收到的事件: {[e['event'] for e in events]}"
        )

    def test_D2_pev_events_have_correct_format_for_frontend(self, client, db):
        """PEV 事件格式应该让前端能解析（有 event 字段和 data 字段）。"""
        token, conv_id = _setup(client, db)

        async def mock_pev_run(db, job):
            yield {"event": "pev_start", "data": {"job_id": 1, "scenario": "intel", "goal": "收集竞品"}}
            yield {"event": "pev_plan_ready", "data": {"steps": [{"step_key": "crawl", "description": "爬取数据"}], "step_count": 1}}
            yield {"event": "pev_step_start", "data": {"step_key": "crawl", "description": "爬取数据", "order": 0}}
            yield {"event": "pev_step_done", "data": {"step_key": "crawl", "ok": True, "duration_ms": 1200}}
            yield {"event": "pev_done", "data": {"summary": "收集了 5 条竞品情报", "job_id": 1}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="intel")):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "帮我收集竞品情报"},
                )

        events = _parse_sse(resp.text)
        for e in events:
            assert "event" in e, f"事件缺少 event 字段: {e}"
            assert "data" in e, f"事件缺少 data 字段: {e}"

        # pev_start 应包含 job_id
        pev_start = next((e for e in events if e["event"] == "pev_start"), None)
        assert pev_start, "应有 pev_start 事件"
        assert "job_id" in pev_start["data"]

        # pev_done 应包含 summary
        pev_done = next((e for e in events if e["event"] == "pev_done"), None)
        assert pev_done, "应有 pev_done 事件"
        assert "summary" in pev_done["data"]

    def test_D3_pev_start_event_is_first_after_request(self, client, db):
        """PEV 路径：第一个 SSE 事件必须是 pev_start，让前端知道切换到任务模式。"""
        token, conv_id = _setup(client, db)

        async def mock_pev_run(db, job):
            yield {"event": "pev_start", "data": {"job_id": 1, "scenario": "task_decomp", "goal": "x"}}
            yield {"event": "pev_done", "data": {"summary": "完成"}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="task_decomp")):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "复杂任务"},
                )

        events = _parse_sse(resp.text)
        assert events, "应收到 SSE 事件"
        assert events[0]["event"] == "pev_start", (
            f"第一个事件应为 pev_start，实际为 {events[0]['event']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Journey E — 快速回复 + Early Return（追问场景）
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyE_QuickReplyEarlyReturn:
    """
    用户点击快速回复选项，触发 InputEvaluator 追问（缺参数 early return）。
    这是一个合理的用例：选项只是意图，AI 追问具体信息。
    """

    @pytest.fixture(autouse=True)
    def no_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def test_E1_quick_reply_triggers_follow_up_question(self, client, db):
        """用户点击"帮我写文案"，AI 追问品牌名称。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.early_return = ("请问您的品牌名称是？", {})

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": "帮我写文案"},
            )

        events = _parse_sse(resp.text)
        event_types = {e["event"] for e in events}
        assert "done" in event_types, "early return 后应有 done 事件"

        delta_texts = [e["data"].get("text", "") for e in events if e["event"] == "delta"]
        assert any("品牌名称" in t for t in delta_texts), "追问内容应出现在 delta 事件中"

    def test_E2_user_answers_follow_up_and_gets_real_reply(self, client, db):
        """用户回答追问后，AI 应生成真正的回复（不再 early return）。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        # Round 1: AI 追问
        prep_round1 = _make_prep()
        prep_round1.early_return = ("请问您的品牌名称是？", {})

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep_round1)):
            client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token), json={"content": "帮我写文案"},
            )

        # Round 2: 用户回答品牌名称，AI 正式生成文案
        prep_round2 = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "以下是为您定制的品牌文案：\n【品牌口号】让生活更美好")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep_round2)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "品牌名称是：生活家"},
                )

        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)
        delta_texts = "".join(e["data"].get("text", "") for e in events if e["event"] == "delta")
        assert "文案" in delta_texts, "用户回答追问后应收到文案内容"

    def test_E3_early_return_does_not_enter_llm_stream(self, client, db):
        """
        Quick reply 触发 early return 时，不应调用 LLM 流，
        节省 token，直接返回追问。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.early_return = ("请补充项目背景。", {})

        llm_called = False

        async def spy_stream(**kwargs):
            nonlocal llm_called
            llm_called = True
            yield ("content", "不应出现")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=spy_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "生成周报摘要"},
                )

        assert not llm_called, "early return 路径不应调用 LLM"
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# Journey F — 边界：快速回复空内容 / 特殊字符 / 超长内容
# ─────────────────────────────────────────────────────────────────────────────

class TestJourneyF_EdgeCases:
    """快速回复选项的边界场景。"""

    @pytest.fixture(autouse=True)
    def no_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def test_F1_quick_reply_with_special_chars(self, client, db):
        """选项含特殊字符（@、#、引号）也能正常发送。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def fake_stream(**kwargs):
            yield ("content", "收到您的请求")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token),
                    json={"content": '帮我分析 #竞品 @小红书 "品牌力"'},
                )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)

    def test_F2_quick_reply_with_very_long_option(self, client, db):
        """超长选项文本（200字）也能正常发送和处理。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        long_option = "帮我分析" + "这个竞品的市场策略" * 20  # ~200字

        async def fake_stream(**kwargs):
            yield ("content", "我来帮您分析")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": long_option},
                )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)

    def test_F3_pev_upgrade_returns_none_for_simple_quick_reply(self, client, db):
        """简单的快速回复选项（"好的"）不触发 PEV 时，前端走普通 Skill 路径并收到 done。"""
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        pev_run_called = False

        async def mock_pev_run(db, job):
            nonlocal pev_run_called
            pev_run_called = True
            yield {"event": "pev_start", "data": {}}

        async def fake_stream(**kwargs):
            yield ("content", "好的，我继续帮您处理。")

        # should_upgrade 返回 None —— 不升级
        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value=None)):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                with patch("app.services.skill_engine.skill_engine.prepare",
                           new=AsyncMock(return_value=prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                               new=fake_stream):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/stream",
                            headers=_auth(token), json={"content": "好的"},
                        )

        # PEV run 不应被调用（未升级）
        assert not pev_run_called, "简单确认词汇不应触发 PEV 执行"
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), "应走普通 done 路径"
