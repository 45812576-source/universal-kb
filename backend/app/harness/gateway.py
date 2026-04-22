"""Hermes Harness Gateway — 统一入口调度。

所有 Agent 入口最终都应构造 HarnessRequest 并调用 HarnessGateway.dispatch()。
Phase 8: Gateway 成为 Skill Studio 真正主链入口 — 集成 DB 双写、memory pack、event append。
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 灰度开关
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_GATEWAY_MAIN_CHAIN_ENABLED = False


def is_gateway_main_chain() -> bool:
    """检查是否走 Gateway 主链。优先读 settings，回退到模块常量。"""
    try:
        from app.config import settings
        return getattr(settings, "GATEWAY_MAIN_CHAIN_ENABLED", _GATEWAY_MAIN_CHAIN_ENABLED)
    except Exception:
        return _GATEWAY_MAIN_CHAIN_ENABLED


class HarnessGateway:
    """统一入口调度器。

    职责:
    1. 创建 HarnessRun + DB agent_runs 记录
    2. 注入 memory pack（SKILL_STUDIO）
    3. 查找并调用对应 AgentExecutor
    4. 每个事件写 agent_run_events DB
    5. 管理 run 状态（running -> completed/failed）
    6. 产出 HarnessEvent 流 + SSE 桥接
    """

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self.store = store or _store

    async def dispatch(
        self,
        request: HarnessRequest,
        db: DBSession,
    ) -> AsyncGenerator[HarnessEvent, None]:
        """调度一次请求。返回 HarnessEvent 异步生成器。"""
        # 1. Session + Run（内存态）
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

        # 1b. 同步写 agent_runs DB（仅 SKILL_STUDIO）
        public_run_id = request.metadata.get("public_run_id")
        if request.agent_type == AgentType.SKILL_STUDIO and public_run_id:
            try:
                from app.services import studio_run_event_store
                studio_run_event_store.set_harness_run_id(db, public_run_id, run.run_id)
                db.commit()
            except Exception:
                logger.debug("Gateway: failed to link harness_run_id for %s", public_run_id)

        yield emit(
            EventName.RUN_CREATED,
            {"run_id": run.run_id, "agent_type": request.agent_type.value},
            run_id=run.run_id,
            session_id=session.session_id,
        )

        # 2. Memory pack 注入（仅 SKILL_STUDIO）
        if request.agent_type == AgentType.SKILL_STUDIO and request.context.skill_id:
            try:
                from app.services.studio_memory_pack_service import build_memory_pack
                mp = build_memory_pack(db, request.context.skill_id)
                if mp:
                    request.metadata["memory_pack"] = mp
            except Exception:
                logger.debug("Gateway: memory pack build failed for skill %s", request.context.skill_id)

        # 3. 查找 executor
        executor = _executors.get(request.agent_type)
        if not executor:
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=f"No executor for {request.agent_type.value}", db=db)
            yield emit(EventName.ERROR, {"message": f"No executor registered for agent_type={request.agent_type.value}"}, run_id=run.run_id, session_id=session.session_id)
            return

        # 4. 执行
        self.store.update_run_status(run.run_id, RunStatus.RUNNING, db=db)
        yield emit(EventName.RUN_STARTED, {"run_id": run.run_id}, run_id=run.run_id, session_id=session.session_id)

        seq = 0
        try:
            async for event in executor(request, db, self.store):
                # 4b. 每个事件写 agent_run_events DB（仅 SKILL_STUDIO + 有 public_run_id）
                seq += 1
                if request.agent_type == AgentType.SKILL_STUDIO and public_run_id:
                    try:
                        from app.services import studio_run_event_store
                        studio_run_event_store.append_event(
                            db,
                            public_run_id=public_run_id,
                            run_version=request.metadata.get("run_version", 1),
                            sequence=seq,
                            event_type=event.event.value,
                            payload=event.data,
                            harness_run_id=run.run_id,
                        )
                        db.commit()
                    except Exception:
                        logger.debug("Gateway: event append failed seq=%d run=%s", seq, run.run_id)
                yield event
        except Exception as exc:
            logger.exception("Executor failed for run %s", run.run_id)
            self.store.update_run_status(run.run_id, RunStatus.FAILED, error=str(exc), db=db)
            yield emit(EventName.RUN_FAILED, {"run_id": run.run_id, "error": str(exc)}, run_id=run.run_id, session_id=session.session_id)
            return

        # 5. 完成
        final_status = self.store.get_run(run.run_id)
        if final_status and final_status.status == RunStatus.RUNNING:
            self.store.update_run_status(run.run_id, RunStatus.COMPLETED, db=db)
            yield emit(EventName.RUN_COMPLETED, {"run_id": run.run_id}, run_id=run.run_id, session_id=session.session_id)

    async def dispatch_sse(
        self,
        request: HarnessRequest,
        db: DBSession,
    ) -> AsyncGenerator[str, None]:
        """Gateway → SSE 文本流，可直接作为 StreamingResponse body。"""
        async for event in self.dispatch(request, db):
            yield event.to_sse()

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

        # 收集 content
        content = ""
        error = None
        for evt in events:
            if evt.event == EventName.REPLACE:
                content = evt.data.get("text", content)
            elif evt.event == EventName.DELTA:
                content += evt.data.get("text", "")
            elif evt.event == EventName.ERROR:
                error = evt.data.get("message", "Unknown error")

        return HarnessResponse(
            request_id=request.request_id,
            run_id=run_id,
            status=last_run.status if last_run else RunStatus.FAILED,
            content=content,
            error=error or (last_run.error if last_run else "No events produced"),
        )


# 确保所有 executor 已注册
_executors_loaded = False


def _ensure_executors() -> None:
    """延迟加载 executor 注册 — 避免循环导入。"""
    global _executors_loaded
    if _executors_loaded:
        return
    _executors_loaded = True
    try:
        import app.harness.profiles.skill_studio  # noqa: F401 — 触发 register_executor
    except Exception:
        logger.debug("Failed to auto-load skill_studio executor")


# 模块级便捷函数
def create_gateway(store: Optional[SessionStore] = None) -> HarnessGateway:
    _ensure_executors()
    return HarnessGateway(store)
