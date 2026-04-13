"""Hermes Harness Gateway — 统一入口调度。

所有 Agent 入口最终都应构造 HarnessRequest 并调用 HarnessGateway.dispatch()。
Phase 1: Gateway 仅负责 run 创建、状态管理、事件记录。
实际 Agent 执行仍由各自的 engine/service 完成 — Gateway 通过 adapter 桥接。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Callable, Optional

from sqlalchemy.orm import Session as DBSession

from app.harness.contracts import (
    AgentType,
    HarnessRequest,
    HarnessResponse,
    HarnessRun,
    RunStatus,
)
from app.harness.events import EventName, HarnessEvent, emit
from app.harness.session_store import SessionStore

logger = logging.getLogger(__name__)

# 全局单例 SessionStore — 进程内共享
_store = SessionStore()


def get_session_store() -> SessionStore:
    """获取全局 SessionStore 实例。"""
    return _store


# Agent 执行器类型：接收 request + db + store，返回 async generator of HarnessEvent
AgentExecutor = Callable[
    [HarnessRequest, DBSession, SessionStore],
    AsyncGenerator[HarnessEvent, None],
]

# agent_type -> executor 注册表
_executors: dict[AgentType, AgentExecutor] = {}


def register_executor(agent_type: AgentType, executor: AgentExecutor) -> None:
    """注册 Agent 执行器。各 AgentProfile 在启动时注册。"""
    _executors[agent_type] = executor
    logger.info("Registered harness executor for %s", agent_type.value)


class HarnessGateway:
    """统一入口调度器。

    职责:
    1. 创建 HarnessRun
    2. 查找并调用对应 AgentExecutor
    3. 管理 run 状态（running -> completed/failed）
    4. 产出 HarnessEvent 流
    """

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self.store = store or _store

    async def dispatch(
        self,
        request: HarnessRequest,
        db: DBSession,
    ) -> AsyncGenerator[HarnessEvent, None]:
        """调度一次请求。返回 HarnessEvent 异步生成器。"""
        # 1. 创建 run
        session = self.store.create_or_get_session(
            request.session_key,
            agent_type=request.agent_type,
            db=db,
        )
        run = HarnessRun(
            request_id=request.request_id,
            session_id=session.session_id,
            session_key=request.session_key,
            agent_type=request.agent_type,
        )
        self.store.create_run(run, db=db)
        yield emit(
            EventName.RUN_CREATED,
            {"run_id": run.run_id, "agent_type": request.agent_type.value},
            run_id=run.run_id,
            session_id=session.session_id,
        )

        # 2. 查找 executor
        executor = _executors.get(request.agent_type)
        if not executor:
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=f"No executor for {request.agent_type.value}", db=db)
            yield emit(EventName.ERROR, {"message": f"No executor registered for agent_type={request.agent_type.value}"}, run_id=run.run_id, session_id=session.session_id)
            return

        # 3. 执行
        self.store.update_run_status(run.run_id, RunStatus.RUNNING, db=db)
        yield emit(EventName.RUN_STARTED, {"run_id": run.run_id}, run_id=run.run_id, session_id=session.session_id)

        try:
            async for event in executor(request, db, self.store):
                yield event
        except Exception as exc:
            logger.exception("Executor failed for run %s", run.run_id)
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=str(exc), db=db)
            yield emit(EventName.RUN_FAILED, {"run_id": run.run_id, "error": str(exc)}, run_id=run.run_id, session_id=session.session_id)
            return

        # 4. 完成
        final_status = self.store.get_run(run.run_id)
        if final_status and final_status.status == RunStatus.RUNNING:
            self.store.update_run_status(run.run_id, RunStatus.COMPLETED, db=db)
            yield emit(EventName.RUN_COMPLETED, {"run_id": run.run_id}, run_id=run.run_id, session_id=session.session_id)

    def dispatch_sync(self, request: HarnessRequest, db: DBSession) -> HarnessResponse:
        """同步调度 — 用于非流式场景。收集所有事件后返回最终 Response。"""
        import asyncio
        events: list[HarnessEvent] = []

        async def _collect():
            async for evt in self.dispatch(request, db):
                events.append(evt)

        asyncio.run(_collect())

        run_id = events[0].data.get("run_id", "") if events else ""
        last_run = self.store.get_run(run_id)

        return HarnessResponse(
            request_id=request.request_id,
            run_id=run_id,
            status=last_run.status if last_run else RunStatus.FAILED,
            error=last_run.error if last_run else "No events produced",
        )


# 模块级便捷函数
def create_gateway(store: Optional[SessionStore] = None) -> HarnessGateway:
    return HarnessGateway(store)
