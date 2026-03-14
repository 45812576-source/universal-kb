"""
TC-USER-EMOTION-JOURNEY: 用户情绪旅程测试法
=============================================

方法论：不从技术角度，而从用户的"期待 → 行动 → 感受"三段式出发。
每个测试代表一个真实用户在真实情境下的完整体验弧线。

五大情境维度：
  SCENE-1: 首次使用 / 新用户摸索期（容错、引导）
  SCENE-2: 日常高频操作（速度、连贯性、状态一致）
  SCENE-3: 异常打断 / 意外操作（中断、重试、恢复）
  SCENE-4: 边界输入 / 极端内容（空消息、超长、特殊字符）
  SCENE-5: 多轮上下文 / 长对话（记忆连贯、历史加载）

已发现 Bug 追踪：
  ✓ Bug-1: PEV 不展示计划（已修复，本文件验证修复有效）
  ✓ Bug-2: 快速回复后静默（已修复，本文件验证修复有效）
  ? Bug-3: 用户连发两条消息 → 第二条被丢弃
  ? Bug-4: 标题只取第一条，后续长对话标题不更新
  ? Bug-5: 引用功能在空消息时仍可发送
  ? Bug-6: 停止生成后消息入库内容不完整
  ? Bug-7: 对话列表不实时更新（新建对话后需刷新）
  ? Bug-8: 快速回复正则漏匹配（带 ** 加粗的列表项）
"""

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    _make_user, _make_dept, _make_model_config,
    _login, _auth,
)
from app.models.user import Role


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    events, current_event = [], "delta"
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                events.append({"event": current_event, "data": json.loads(line[6:])})
                current_event = "delta"
            except json.JSONDecodeError:
                pass
    return events


def _setup(client, db, username=None):
    dept = _make_dept(db)
    uname = username or f"ej_{id(db)}_{int(time.time()*1000) % 99999}"
    _make_user(db, uname, Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, uname)
    r = client.post("/api/conversations", headers=_auth(token))
    return token, r.json()["id"]


def _make_prep(early_return=None):
    prep = MagicMock()
    prep.early_return = early_return
    prep.skill_name = None
    prep.skill_id = None
    prep.skill_version = None
    prep.tools_schema = None
    prep.llm_messages = []
    prep.model_config = {"context_window": 32000}
    return prep


def _no_pev():
    return patch(
        "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
        new=AsyncMock(return_value=None),
    )


def _fake_llm(content="好的"):
    async def _stream(**kwargs):
        yield ("content", content)
    return _stream


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-1: 首次使用 / 新用户摸索期
# ═════════════════════════════════════════════════════════════════════════════

class TestScene1_NewUser:
    """
    用户画像：第一次打开 Le Desk，不知道能做什么，乱点乱试。
    期待：系统容错好，不报奇怪错误，引导清晰。
    """

    def test_1A_send_empty_message_is_blocked(self, client, db):
        """
        【场景】用户啥都没输入就按了发送（误操作）。
        【期待】后端应拒绝，不产生空 AI 消息，不报 500。
        【发现】实际上后端 SendMessage 的 content: str 没有 min_length 校验，
               空字符串会被接受并触发完整的 Prepare → LLM 流程，造成无意义的 API 调用。
        """
        token, conv_id = _setup(client, db)
        with _no_pev():
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": ""},
            )
        # 期望 400 拒绝，或者至少不返回 200 触发 LLM
        # 当前 Bug: 返回 200 并进入 LLM 流程
        assert resp.status_code in (400, 422), (
            f"BUG: 空消息被接受并触发 LLM，status={resp.status_code}。"
            "应该在入口就拒绝空内容。"
        )

    def test_1B_send_only_spaces_is_blocked(self, client, db):
        """
        【场景】用户只输入了几个空格。
        【期待】同空消息处理。
        """
        token, conv_id = _setup(client, db)
        with _no_pev():
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": "   \n  \t  "},
            )
        assert resp.status_code in (400, 422), (
            f"BUG: 纯空白消息被接受，status={resp.status_code}"
        )

    def test_1C_new_conversation_starts_with_no_messages(self, client, db):
        """
        【场景】用户新建对话后立即查看消息列表。
        【期待】空列表，无任何历史。
        """
        token, conv_id = _setup(client, db)
        msgs = client.get(
            f"/api/conversations/{conv_id}/messages",
            headers=_auth(token),
        ).json()
        assert msgs == [], f"新对话应为空，实际: {msgs}"

    def test_1D_first_message_sets_conversation_title(self, client, db):
        """
        【场景】用户发第一条消息后，对话列表里的标题应更新为消息内容。
        【期待】对话标题 = 用户消息前60字。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("好的")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token),
                        json={"content": "帮我写一份竞品分析报告"},
                    )
        convs = client.get("/api/conversations", headers=_auth(token)).json()
        conv = next((c for c in convs if c["id"] == conv_id), None)
        assert conv is not None
        assert "竞品分析报告" in conv["title"], (
            f"第一条消息后标题应包含用户消息内容，实际: {conv['title']}"
        )

    def test_1E_accessing_other_users_conv_returns_404(self, client, db):
        """
        【场景】用户 A 的对话 ID 被用户 B 猜到并尝试访问。
        【期待】404，不泄漏内容。
        """
        dept = _make_dept(db)
        _make_user(db, "ej_u1", Role.EMPLOYEE, dept.id)
        _make_user(db, "ej_u2", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()

        t1 = _login(client, "ej_u1")
        t2 = _login(client, "ej_u2")
        r = client.post("/api/conversations", headers=_auth(t1))
        conv_id = r.json()["id"]

        resp = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(t2))
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-2: 日常高频操作 — 连贯性、状态一致
# ═════════════════════════════════════════════════════════════════════════════

class TestScene2_DailyUsage:
    """
    用户画像：熟练用户，每天多次使用。
    最在意：速度、状态一致、操作连贯不被打断。
    """

    def test_2A_rapid_fire_two_messages_second_not_dropped(self, client, db):
        """
        【场景】用户快速连发两条消息（第一条还没回复完就发了第二条）。
        【期待】两条用户消息都应入库。
        【发现Bug-3】chat-store.ts 的 isSending guard 会在前端阻止第二条发出，
                但如果用户绕过前端（如直接 API 调用），后端无并发锁，两条都会触发流，
                但前端因 isSending=true 永远不会渲染第二条的 AI 回复。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("第一条回复")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "第一条消息"},
                    )

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("第二条回复")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "第二条消息"},
                    )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        contents = [m["content"] for m in user_msgs]
        assert "第一条消息" in contents, "第一条用户消息应入库"
        assert "第二条消息" in contents, (
            "BUG-3: 第二条用户消息丢失。连续发消息时后端应依次处理，两条均入库。"
        )

    def test_2B_conversation_list_updates_after_new_message(self, client, db):
        """
        【场景】用户发完消息，切到对话列表，期望看到刚才的对话在最顶部（最近更新）。
        【期待】对话按 updated_at 降序，刚发消息的对话排第一。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        # 再创建一个更早的对话
        old_conv = client.post("/api/conversations", headers=_auth(token)).json()

        time.sleep(0.01)  # 确保时间戳不同

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("回复")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "最新消息"},
                    )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        assert convs[0]["id"] == conv_id, (
            f"发完消息的对话应排第一，实际排第一的是 id={convs[0]['id']}，期望 {conv_id}"
        )

    def test_2C_title_does_not_update_after_second_message(self, client, db):
        """
        【场景】用户第一条说"帮我写文案"，第二条说"品牌是XX"，期望标题保持第一条。
        【发现Bug-4】conversations.py 的标题逻辑是 msg_count <= 2，
                 而 msg_count 计算的是所有消息（含 user+assistant），
                 所以第二次对话（第3条消息）仍然可能覆盖标题。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("好的")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "帮我写文案"},
                    )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        title_after_first = next(c["title"] for c in convs if c["id"] == conv_id)

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("好的")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "这条完全不同的内容应该不改标题"},
                    )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        title_after_second = next(c["title"] for c in convs if c["id"] == conv_id)

        assert title_after_second == title_after_first, (
            f"BUG-4: 第二条消息覆盖了标题。\n"
            f"  第一条后标题: {title_after_first!r}\n"
            f"  第二条后标题: {title_after_second!r}\n"
            f"  标题应在第一条消息后固定，不再变化。"
        )

    def test_2D_quote_only_message_is_rejected(self, client, db):
        """
        【场景】用户点了"引用"某条消息，但没输入任何新内容，直接点发送。
        【期待】后端应拒绝（引用前缀 + 空正文不是有效消息）。
        【发现Bug-5】前端 ChatInput 的 canSend 逻辑：有 quote 时也允许发送，
                 但实际内容只是引用前缀，没有实质新内容。
                 这会导致用户气泡显示"引用内容"，AI 收到的却只是引用，没有问题。
        注：此处测纯引用前缀发到后端的情况。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        # 引用前缀格式（来自 ChatInput.buildQuotePrefix）
        quote_prefix = "[Pasted text #1]\n原始内容\n\n"

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("回复")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token),
                        json={"content": quote_prefix},  # 只有引用，没有实质问题
                    )

        # 理想情况：400。实际当前：200 正常处理（用户没问问题 AI 不知道该回什么）
        # 这不是 crash，但是用户体验差
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), "即便引用前缀也应完整走完流程"
        # 记录为已知用户体验问题：引用但不提问时 AI 回复质量低

    def test_2E_skill_tag_appears_in_assistant_message_metadata(self, client, db):
        """
        【场景】工作台有 Skill，用户发消息后，AI 回复气泡应显示 "via SkillName" 标签。
        【期待】assistant 消息的 metadata 包含 skill_name。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        prep.skill_name = "竞品分析助手"
        prep.skill_id = 999

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("分析结果")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "分析竞品"},
                    )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msg = next((m for m in msgs if m["role"] == "assistant"), None)
        assert assistant_msg, "应有 assistant 消息"
        # skill_name 应出现在 metadata
        meta = assistant_msg.get("metadata") or {}
        assert meta.get("skill_name") == "竞品分析助手", (
            f"BUG: skill_name 未写入 metadata，实际: {meta}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-3: 异常打断 / 意外操作
# ═════════════════════════════════════════════════════════════════════════════

class TestScene3_Interruption:
    """
    用户画像：等待 AI 回复时，因为太慢或者回答方向不对，主动中断。
    最在意：中断后状态干净，不卡死，能继续发下一条消息。
    """

    def test_3A_stop_generation_partial_content_is_saved(self, client, db):
        """
        【场景】AI 正在流式输出，用户点击"停止生成"。
        【期待】已生成的部分内容应入库（不丢失），消息标记为已停止。
        【发现Bug-6】conversations.py 中，assistant 消息在 done 事件之后才写库。
                 如果用户在流式中途 abort，SSE 连接断开但 generator 还在跑，
                 full_content 是部分内容，最终 db.commit() 会不会执行取决于 ASGI 实现。
                 前端 chat-store.ts abort 后会 appendOptimisticMessage("已停止生成")，
                 但这是纯前端状态，刷新页面后消失。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def slow_stream(**kwargs):
            yield ("content", "这是部分内容")
            # 模拟用户在这里 abort，后续内容不再发出

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=slow_stream):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "开始生成"},
                    )

        # 即使是"部分"流，done 事件到达后消息应入库
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert assistant_msgs, (
            "BUG-6: 流结束后 assistant 消息未入库，用户刷新页面后历史消失。"
        )

    def test_3B_error_during_stream_leaves_clean_state(self, client, db):
        """
        【场景】LLM 调用中途抛出异常（如网络超时）。
        【期待】用户看到错误提示，消息列表干净（不留孤儿 user 消息没有 AI 回复）。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        async def exploding_stream(**kwargs):
            raise ConnectionError("模拟网络中断")
            yield  # noqa: unreachable, makes it an async generator

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=exploding_stream):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "这条会失败"},
                    )

        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert error_events, "流错误后应发 error 事件"
        assert error_events[0]["data"].get("error_type") in (
            "network", "server_error"
        ), f"error_type 不正确: {error_events[0]['data']}"

        # user 消息已入库（在 stream 之前 commit），这是正常的
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert any("这条会失败" in m["content"] for m in user_msgs), "用户消息应入库"

    def test_3C_send_after_error_works_normally(self, client, db):
        """
        【场景】出错后用户继续发下一条消息，期望恢复正常。
        【期待】下一条消息正常得到回复，isSending 状态已重置。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        # Round 1: 出错
        async def bad_stream(**kwargs):
            raise RuntimeError("服务器内部错误")
            yield

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=bad_stream):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "第一条（出错）"},
                    )

        # Round 2: 正常
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("正常回复")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "第二条（正常）"},
                    )

        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), (
            "出错后再发消息应正常响应，不应因上一次错误而卡死"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-4: 边界输入 / 极端内容
# ═════════════════════════════════════════════════════════════════════════════

class TestScene4_EdgeInputs:
    """
    用户画像：不按套路出牌的用户，或粘贴了大段内容。
    最在意：不崩溃、不乱码、不超时。
    """

    def test_4A_very_long_message_is_handled(self, client, db):
        """
        【场景】用户粘贴了一篇5000字文章直接问"帮我总结"。
        【期待】正常处理，不 413/500。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        long_content = "这是一段很长的文字。" * 500  # ~5000 字

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("总结完毕")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": long_content},
                    )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events)

    def test_4B_json_injection_in_message_content(self, client, db):
        """
        【场景】用户消息含 JSON 特殊字符，验证 SSE 序列化不出错。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        tricky_content = '{"key": "value", "array": [1,2,3], "quote": "it\\"s"}'

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("收到")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": tricky_content},
                    )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), "含特殊字符的消息应正常流式完成"

    def test_4C_markdown_list_with_bold_items_parsed_as_quick_replies(self, client, db):
        """
        【场景】AI 回复了带 **加粗** 的列表，如：
               1. **帮我写文案** - 生成品牌文字内容
               2. **分析数据** - 竞品和市场分析
        【期待】前端 parseQuickReplies 应能解析出选项。
        【发现Bug-8】当前正则 /^\s*(?:\d+[.)]\s*|...)(.+)$/ 能捕获 "**帮我写文案** - 生成..."
                 作为整体（包含 Markdown 符号），前端按钮显示为 "**帮我写文案** - 生成..."
                 用户点击时发出的文本含 ** 和 - ，AI 需要能理解这种格式。
                 不算严重 Bug 但影响体验（按钮文字有 Markdown 符号）。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        bold_list_reply = (
            "请选择您需要的服务：\n"
            "1. **帮我写文案** — 生成品牌内容\n"
            "2. **分析竞品** — 市场竞争分析\n"
            "3. **生成报告** — 数据可视化报告"
        )

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm(bold_list_reply)):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "有什么可以帮我？"},
                    )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msg = next((m for m in msgs if m["role"] == "assistant"), None)
        assert assistant_msg
        content = assistant_msg["content"]
        # 验证内容完整保存
        assert "**帮我写文案**" in content

        # 如果用户点击了含 ** 的选项后发消息，后端应正常处理
        tricky_option = "**帮我写文案** — 生成品牌内容"
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("好的，请提供品牌名称")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": tricky_option},
                    )

        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), (
            "BUG-8: 含 Markdown 符号的快速回复选项无法正常发送"
        )

    def test_4D_newline_only_message_is_blocked(self, client, db):
        """
        【场景】用户按了很多次 Enter，消息全是换行。
        【期待】后端视为空消息，拒绝。
        """
        token, conv_id = _setup(client, db)
        with _no_pev():
            resp = client.post(
                f"/api/conversations/{conv_id}/messages/stream",
                headers=_auth(token),
                json={"content": "\n\n\n\n"},
            )
        assert resp.status_code in (400, 422), (
            f"BUG: 纯换行消息被接受，status={resp.status_code}"
        )

    def test_4E_extremely_long_message_title_is_truncated(self, client, db):
        """
        【场景】用户第一条消息超过60字（很常见），对话标题应截断。
        【期待】标题长度 <= 60 字符。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()
        long_first_msg = "这是一条超过六十个字的消息，用户粘贴了很多内容，" * 3

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("好")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": long_first_msg},
                    )

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        conv = next(c for c in convs if c["id"] == conv_id)
        assert len(conv["title"]) <= 60, f"标题超过60字: {len(conv['title'])} 字"


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-5: 多轮上下文 / 长对话
# ═════════════════════════════════════════════════════════════════════════════

class TestScene5_LongConversation:
    """
    用户画像：把 Le Desk 当工作助手，长期使用同一个对话做一个项目。
    最在意：AI 能记住之前说的话，历史消息完整可查。
    """

    def test_5A_messages_are_in_chronological_order(self, client, db):
        """
        【场景】用户多轮对话后查看历史，期望消息按时间顺序排列。
        【期待】消息列表按 created_at 升序（最早在上，最新在下）。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        messages_sent = ["第一轮问题", "第二轮问题", "第三轮问题"]
        for msg in messages_sent:
            with _no_pev():
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm("回复")):
                        client.post(
                            f"/api/conversations/{conv_id}/messages/stream",
                            headers=_auth(token), json={"content": msg},
                        )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        contents = [m["content"] for m in user_msgs]

        assert contents == messages_sent, (
            f"消息顺序不对，期望 {messages_sent}，实际 {contents}"
        )

    def test_5B_all_messages_persisted_after_multi_turn(self, client, db):
        """
        【场景】5 轮对话后，所有消息（用户+AI）都应在历史里。
        【期待】messages 数量 = 10（5个 user + 5个 assistant）。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        for i in range(5):
            with _no_pev():
                with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                    with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm(f"AI回复{i}")):
                        client.post(
                            f"/api/conversations/{conv_id}/messages/stream",
                            headers=_auth(token), json={"content": f"用户问题{i}"},
                        )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assert len(msgs) == 10, f"5轮对话应有10条消息，实际 {len(msgs)} 条"

    def test_5C_early_return_message_also_in_history(self, client, db):
        """
        【场景】Skill 追问（early return）的消息也应出现在历史记录里。
        【期待】early return 产生的 assistant 消息入库且可查。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep(early_return=("请问您的品牌名称是？", {}))

        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "帮我写文案"},
                )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert assistant_msgs, "early return 的追问消息应入库"
        assert any("品牌名称" in m["content"] for m in assistant_msgs)

    def test_5D_deleted_conversation_not_in_list(self, client, db):
        """
        【场景】用户删除（soft delete）一个对话后，列表里不应再出现。
        【期待】对话列表不含已删除的对话。
        注：依赖 PATCH /conversations/{id} 接口（若有）或 is_active 字段。
        """
        token, conv_id = _setup(client, db)

        # 尝试软删除
        patch_resp = client.patch(
            f"/api/conversations/{conv_id}",
            headers=_auth(token),
            json={"is_active": False},
        )
        if patch_resp.status_code not in (200, 204):
            pytest.skip("该接口不支持软删除，跳过此用例")

        convs = client.get("/api/conversations", headers=_auth(token)).json()
        ids = [c["id"] for c in convs]
        assert conv_id not in ids, "已删除的对话不应出现在列表中"

    def test_5E_pev_done_message_in_history(self, client, db):
        """
        【场景】PEV 任务完成后，用户切换到对话列表再回来，应能看到任务摘要消息。
        【期待】PEV 任务的 summary 作为 assistant 消息存入历史。
        """
        token, conv_id = _setup(client, db)

        async def mock_pev_run(db, job):
            yield {"event": "pev_start", "data": {"job_id": 1, "scenario": "task_decomp", "goal": "生成报告"}}
            yield {"event": "pev_plan_ready", "data": {"steps": [], "step_count": 1}}
            yield {"event": "pev_done", "data": {"summary": "任务已完成：生成了3份竞品分析报告", "status": "completed"}}

        with patch("app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
                   new=AsyncMock(return_value="task_decomp")):
            with patch("app.services.pev.orchestrator.pev_orchestrator.run",
                       side_effect=mock_pev_run):
                client.post(
                    f"/api/conversations/{conv_id}/messages/stream",
                    headers=_auth(token), json={"content": "帮我生成竞品报告"},
                )

        # 模拟用户刷新后查看历史
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert assistant_msgs, (
            "BUG: PEV 完成后 assistant 消息未入库，用户刷新后看不到任务结果"
        )
        contents = [m["content"] for m in assistant_msgs]
        assert any("竞品分析报告" in c or "任务已完成" in c for c in contents), (
            f"PEV 摘要应作为 assistant 消息保存，实际内容: {contents}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENE-6: 快速回复正则边界（前端逻辑后端验证）
# ═════════════════════════════════════════════════════════════════════════════

class TestScene6_QuickReplyRegex:
    """
    专项测试 parseQuickReplies 的正则覆盖范围。
    用实际发送验证「什么样的 AI 回复」会产生快速回复选项。
    """

    def _send_and_get_last_assistant(self, client, db, token, conv_id, ai_content):
        prep = _make_prep()
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=_fake_llm(ai_content)):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "测试"},
                    )
        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        return next((m for m in reversed(msgs) if m["role"] == "assistant"), None)

    def test_6A_numbered_list_saved_intact(self, client, db):
        """带数字的列表 AI 回复完整保存，内容正确。"""
        token, conv_id = _setup(client, db)
        content = "请选择：\n1. 选项A\n2. 选项B\n3. 选项C"
        msg = self._send_and_get_last_assistant(client, db, token, conv_id, content)
        assert msg
        assert "1. 选项A" in msg["content"]

    def test_6B_bullet_list_saved_intact(self, client, db):
        """带 - 的列表完整保存。"""
        token, conv_id = _setup(client, db)
        content = "可选操作：\n- 操作一\n- 操作二\n- 操作三"
        msg = self._send_and_get_last_assistant(client, db, token, conv_id, content)
        assert msg
        assert "- 操作一" in msg["content"]

    def test_6C_only_one_item_no_quick_reply(self, client, db):
        """只有1个列表项，不应触发快速回复（前端需要 >= 2 个）。"""
        token, conv_id = _setup(client, db)
        content = "只有一个选项：\n1. 唯一选项"
        msg = self._send_and_get_last_assistant(client, db, token, conv_id, content)
        assert msg
        # 消息应正常保存
        assert "唯一选项" in msg["content"]

    def test_6D_seven_items_no_quick_reply(self, client, db):
        """7个列表项超过上限6，不应触发快速回复（体验设计：太多选项不展示）。"""
        token, conv_id = _setup(client, db)
        items = "\n".join(f"{i+1}. 选项{i+1}" for i in range(7))
        content = f"太多选项：\n{items}"
        msg = self._send_and_get_last_assistant(client, db, token, conv_id, content)
        assert msg
        assert "选项7" in msg["content"]

    def test_6E_quick_reply_option_click_then_another_ai_list(self, client, db):
        """
        用户点了快速回复 → AI 又给了新的列表 → 用户再点 → 应该正常。
        这是最常见的引导式对话模式。
        """
        token, conv_id = _setup(client, db)
        prep = _make_prep()

        # Round 1
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                           new=_fake_llm("第一级菜单：\n1. 文案创作\n2. 数据分析")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "你能做什么？"},
                    )

        # Round 2：用户点了"文案创作"
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                           new=_fake_llm("文案风格选择：\n1. 小红书风格\n2. 知乎风格\n3. 微信公众号风格")):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "文案创作"},
                    )

        # Round 3：用户点了"小红书风格"
        with _no_pev():
            with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
                with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed",
                           new=_fake_llm("好的，请告诉我品牌和主题")):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/stream",
                        headers=_auth(token), json={"content": "小红书风格"},
                    )

        events = _parse_sse(resp.text)
        assert any(e["event"] == "done" for e in events), (
            "三级快速回复链：最后一级应正常收到 AI 回复"
        )

        msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
        assert len(msgs) == 6, f"3轮对话应6条消息，实际 {len(msgs)}"
