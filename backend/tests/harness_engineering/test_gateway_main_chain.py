"""Phase 8: Gateway 主链迁移单元测试。

测试 gateway dispatch 完整 lifecycle、DB 双写、灰度开关、memory pack 注入。
不依赖外部服务，全部用 mock/patch。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.harness.contracts import (
    AgentType,
    HarnessRequest,
    HarnessRun,
    RunStatus,
)
from app.harness.events import EventName, HarnessEvent, emit
from app.harness.gateway import (
    HarnessGateway,
    _executors,
    create_gateway,
    is_gateway_main_chain,
    register_executor,
)
from app.harness.session_store import SessionStore


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_request(
    *,
    agent_type: AgentType = AgentType.SKILL_STUDIO,
    user_id: int = 1,
    workspace_id: int = 10,
    skill_id: int = 100,
    conversation_id: int = 1000,
    user_message: str = "测试消息",
    metadata: dict[str, Any] | None = None,
) -> HarnessRequest:
    from app.harness.adapters import build_skill_studio_request
    return build_skill_studio_request(
        user_id=user_id,
        workspace_id=workspace_id,
        skill_id=skill_id,
        conversation_id=conversation_id,
        user_message=user_message,
        stream=True,
        metadata=metadata or {},
    )


def _fake_db():
    """Create a mock DB session."""
    db = MagicMock()
    db.commit = MagicMock()
    db.flush = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
    db.get = MagicMock(return_value=None)
    return db


async def _collect_events(gen) -> list[HarnessEvent]:
    events = []
    async for evt in gen:
        events.append(evt)
    return events


# ── Test: Gateway dispatch lifecycle ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_lifecycle_no_executor():
    """无注册 executor 时 dispatch 应 yield ERROR。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    # 用一个不存在 executor 的 agent_type
    from app.harness.contracts import HarnessContext, HarnessSessionKey
    req = HarnessRequest(
        session_key=HarnessSessionKey(
            user_id=1,
            agent_type=AgentType.CHAT,
            workspace_id=10,
            conversation_id=100,
        ),
        agent_type=AgentType.CHAT,
        user_id=1,
        input_text="test",
    )

    events = await _collect_events(gateway.dispatch(req, db))
    event_names = [e.event for e in events]
    assert EventName.RUN_CREATED in event_names
    assert EventName.ERROR in event_names


@pytest.mark.asyncio
async def test_dispatch_lifecycle_with_executor():
    """注册 executor 后 dispatch 应 yield 完整 lifecycle。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    # 注册一个简单 executor
    yielded_events = [
        emit(EventName.STATUS, {"stage": "generating"}),
        emit(EventName.DELTA, {"text": "hello "}),
        emit(EventName.DELTA, {"text": "world"}),
        emit(EventName.REPLACE, {"text": "hello world"}),
    ]

    async def _test_executor(request, db, store):
        for evt in yielded_events:
            yield evt

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _test_executor)

        req = _make_request()
        events = await _collect_events(gateway.dispatch(req, db))
        event_names = [e.event for e in events]

        assert EventName.RUN_CREATED in event_names
        assert EventName.RUN_STARTED in event_names
        assert EventName.STATUS in event_names
        assert EventName.DELTA in event_names
        assert EventName.REPLACE in event_names
        assert EventName.RUN_COMPLETED in event_names
    finally:
        _executors.clear()
        _executors.update(old_executors)


@pytest.mark.asyncio
async def test_dispatch_executor_failure():
    """executor 抛异常时 dispatch 应 yield RUN_FAILED。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    async def _failing_executor(request, db, store):
        yield emit(EventName.STATUS, {"stage": "generating"})
        raise RuntimeError("boom")

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _failing_executor)

        req = _make_request()
        events = await _collect_events(gateway.dispatch(req, db))
        event_names = [e.event for e in events]

        assert EventName.RUN_STARTED in event_names
        assert EventName.RUN_FAILED in event_names
        # 不应有 RUN_COMPLETED
        assert EventName.RUN_COMPLETED not in event_names
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: DB 双写 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_writes_events_to_db():
    """有 public_run_id 时 dispatch 应调用 studio_run_event_store.append_event。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    async def _simple_executor(request, db, store):
        yield emit(EventName.DELTA, {"text": "a"})
        yield emit(EventName.REPLACE, {"text": "a"})

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _simple_executor)

        req = _make_request(metadata={"public_run_id": "test-run-001", "run_version": 1})

        with patch("app.services.studio_run_event_store.append_event") as mock_append, \
             patch("app.services.studio_run_event_store.set_harness_run_id") as mock_set:
            events = await _collect_events(gateway.dispatch(req, db))

            # append_event 应被调用（每个 executor 事件一次）
            assert mock_append.call_count == 2
            # set_harness_run_id 应被调用一次
            assert mock_set.call_count == 1
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: Memory pack 注入 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_injects_memory_pack():
    """SKILL_STUDIO + skill_id 时应注入 memory_pack 到 request.metadata。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    captured_metadata = {}

    async def _capture_executor(request, db, store):
        captured_metadata.update(request.metadata)
        yield emit(EventName.DONE, {})

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _capture_executor)

        req = _make_request()

        fake_mp = {"skill_summary": {"name": "test"}, "context_rollups": []}
        with patch("app.services.studio_memory_pack_service.build_memory_pack", return_value=fake_mp):
            events = await _collect_events(gateway.dispatch(req, db))

        assert "memory_pack" in captured_metadata
        assert captured_metadata["memory_pack"]["skill_summary"]["name"] == "test"
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: dispatch_sse ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_sse_returns_text():
    """dispatch_sse 应返回 SSE 格式文本流。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    async def _simple_executor(request, db, store):
        yield emit(EventName.DELTA, {"text": "hello"})

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _simple_executor)

        req = _make_request()
        sse_parts = []
        async for part in gateway.dispatch_sse(req, db):
            sse_parts.append(part)

        # 每个 part 应是 SSE 格式文本
        full_text = "".join(sse_parts)
        assert "event: run_created" in full_text
        assert "event: run_started" in full_text
        assert "event: delta" in full_text
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: dispatch_sync ──────────────────────────────────────────────────────

def test_dispatch_sync_collects_content():
    """dispatch_sync 应收集 content 并返回 HarnessResponse。"""
    store = SessionStore()
    gateway = HarnessGateway(store=store)
    db = _fake_db()

    async def _content_executor(request, db, store):
        yield emit(EventName.DELTA, {"text": "hello "})
        yield emit(EventName.DELTA, {"text": "world"})

    old_executors = dict(_executors)
    try:
        register_executor(AgentType.SKILL_STUDIO, _content_executor)

        req = _make_request()
        resp = gateway.dispatch_sync(req, db)
        assert resp.content == "hello world"
        assert resp.status == RunStatus.COMPLETED
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: 灰度开关 ───────────────────────────────────────────────────────────

def test_gateway_main_chain_default_off():
    """默认灰度开关应为 False。"""
    with patch("app.config.settings") as mock_settings:
        mock_settings.GATEWAY_MAIN_CHAIN_ENABLED = False
        assert is_gateway_main_chain() is False


def test_gateway_main_chain_on():
    """灰度开关设为 True 时应返回 True。"""
    with patch("app.config.settings") as mock_settings:
        mock_settings.GATEWAY_MAIN_CHAIN_ENABLED = True
        assert is_gateway_main_chain() is True


# ── Test: Executor 注册 ──────────────────────────────────────────────────────

def test_register_executor():
    """register_executor 应将 executor 注册到全局表。"""
    old_executors = dict(_executors)
    try:
        async def _noop(req, db, store):
            yield emit(EventName.DONE, {})

        register_executor(AgentType.CHAT, _noop)
        assert AgentType.CHAT in _executors
        assert _executors[AgentType.CHAT] is _noop
    finally:
        _executors.clear()
        _executors.update(old_executors)


# ── Test: SkillStudio executor 桥接注册 ──────────────────────────────────────

def test_skill_studio_executor_registered():
    """导入 skill_studio 模块后 SKILL_STUDIO executor 应已注册。"""
    # 触发模块加载（注册在模块底部）
    import app.harness.profiles.skill_studio  # noqa: F401
    assert AgentType.SKILL_STUDIO in _executors


# ── Test: create_gateway ─────────────────────────────────────────────────────

def test_create_gateway_returns_instance():
    gw = create_gateway()
    assert isinstance(gw, HarnessGateway)


def test_create_gateway_with_custom_store():
    store = SessionStore()
    gw = create_gateway(store=store)
    assert gw.store is store
