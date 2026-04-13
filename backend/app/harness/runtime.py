"""AgentRuntime — 统一 Agent 运行时，Chat 作为首个接入的 Agent Profile。

执行流程：
1. SecurityPipeline.check(auth + model_grant)
2. SkillRouter.route(message)
3. ContextAssembler.build(conversation)
4. PromptBuilder.compile(skill, context)
5. KnowledgeInjector.inject(query)
6. LLM call (stream)
7. ToolLoop.run(if tool_calls)
8. OutputFilter.apply(response)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time_mod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from sqlalchemy.orm import Session

from app.harness.contracts import (
    AgentType,
    HarnessContext,
    HarnessRequest,
    HarnessResponse,
    HarnessRun,
    RunStatus,
    SecurityDecisionStatus,
)
from app.harness.security import SecurityContext, SecurityPipeline, security_pipeline
from app.harness.tool_loop import ToolLoop, ToolLoopContext, tool_loop
from app.models.conversation import Conversation, Message, MessageRole
from app.models.skill import Skill, SkillMode, SkillStatus
from app.services.context_assembler import context_assembler
from app.services.knowledge_injector import knowledge_injector
from app.services.llm_gateway import llm_gateway
from app.services.prompt_builder import prompt_builder
from app.services.skill_router import skill_router
from app.services.skill_engine import (
    PrepareResult,
    SkillEngine,
    _check_model_grant,
    _read_source_files,
    skill_engine,
)
from app.services import prompt_compiler

logger = logging.getLogger(__name__)


class AgentRuntime:
    """统一 Agent 运行时 — Chat 作为首个接入的 Agent Profile。"""

    def __init__(
        self,
        security: SecurityPipeline | None = None,
    ):
        self.security = security or security_pipeline
        self.tool_loop = tool_loop

    async def run(
        self,
        request: HarnessRequest,
        db: Session,
        conversation: Conversation,
        *,
        active_skill_ids: list[int] | None = None,
        force_skill_id: int | None = None,
        on_status=None,
    ) -> AsyncGenerator[dict | tuple, None]:
        """流式执行 Chat 请求。

        Yields:
            - dict: SSE-ready 事件 {"event": ..., "data": ...}
            - 最终不 yield done 事件（由调用方组装 done 并 yield）

        最终结果通过 `result` 属性获取。
        """
        user_id = request.user_id
        user_message = request.input_text

        # ── 1. SecurityPipeline: auth + model_grant ──
        auth_ctx = SecurityContext(db=db, user_id=user_id)
        auth_decision = await self.security.check(auth_ctx)
        if auth_decision.status == SecurityDecisionStatus.DENY:
            yield {"event": "error", "data": {
                "message": auth_decision.reason,
                "error_type": "permission_denied",
                "retryable": False,
            }}
            return

        # ── 2. Prepare (委托给 skill_engine.prepare，复用全部逻辑) ──
        # 在 runtime 层调用 skill_engine.prepare，这保证了：
        # - 所有 skill 匹配逻辑（含 workspace 边界修复）
        # - 变量提取、知识注入、prompt 编译
        # - early_return 处理
        prep = await skill_engine.prepare(
            db, conversation, user_message,
            user_id=user_id,
            active_skill_ids=active_skill_ids,
            force_skill_id=force_skill_id,
            on_status=on_status,
        )

        # Early return
        if prep.early_return is not None:
            response_text, early_meta = prep.early_return
            yield {"event": "early_return", "data": {
                "response": response_text,
                "metadata": early_meta,
            }}
            return

        # ── 3. Model grant check (via SecurityPipeline) ──
        model_ctx = SecurityContext(
            db=db, user_id=user_id,
            model_config=prep.model_config,
            skill_id=prep.skill_id,
        )
        model_decision = await self.security.check(model_ctx)
        if model_decision.status == SecurityDecisionStatus.DENY:
            yield {"event": "error", "data": {
                "message": model_decision.reason,
                "error_type": "permission_denied",
                "retryable": False,
            }}
            return

        # ── 4. Streaming LLM call ──
        yield {"event": "status", "data": {"stage": "generating", "skill_name": prep.skill_name}}

        full_content = ""
        full_thinking = ""
        block_idx = 0
        current_block_type = None
        native_tool_calls: list[dict] = []

        _llm_stream = llm_gateway.chat_stream_typed(
            model_config=prep.model_config,
            messages=prep.llm_messages,
            tools=prep.tools_schema or None,
        )
        async for chunk_type, chunk_data in _llm_stream:
            if chunk_type == "thinking":
                full_thinking += chunk_data
                if current_block_type != "thinking":
                    if current_block_type is not None:
                        yield {"event": "content_block_stop", "data": {"index": block_idx}}
                        block_idx += 1
                    yield {"event": "content_block_start", "data": {"index": block_idx, "type": "thinking"}}
                    current_block_type = "thinking"
                yield {"event": "content_block_delta", "data": {"index": block_idx, "delta": {"text": chunk_data}}}
            elif chunk_type == "tool_call":
                native_tool_calls.append(chunk_data)
            else:  # content
                if current_block_type != "text":
                    if current_block_type is not None:
                        yield {"event": "content_block_stop", "data": {"index": block_idx}}
                        block_idx += 1
                    yield {"event": "content_block_start", "data": {"index": block_idx, "type": "text"}}
                    current_block_type = "text"
                full_content += chunk_data
                yield {"event": "content_block_delta", "data": {"index": block_idx, "delta": {"text": chunk_data}}}
                yield {"event": "delta", "data": {"text": chunk_data}}

        # Close last block
        if current_block_type is not None:
            yield {"event": "content_block_stop", "data": {"index": block_idx}}

        # ── 5. Post-processing ──
        response = full_content
        tool_meta: dict = {}

        # Structured output
        skill_version = prep.skill_version
        structured_output = None
        if skill_version and skill_version.output_schema:
            parsed = SkillEngine._try_parse_structured_output(response)
            if parsed is not None:
                structured_output = parsed
                response = prompt_compiler.render_structured_as_markdown(
                    skill_version.output_schema, parsed
                )
                yield {"event": "replace", "data": {"text": response}}

        # ── 6. Agent Loop (ToolLoop) ──
        if native_tool_calls or "```tool_call" in response:
            skill_obj = db.get(Skill, prep.skill_id) if prep.skill_id else None
            yield {"event": "status", "data": {"stage": "tool_calling"}}
            _next_block_idx = block_idx + (1 if current_block_type is not None else 0)

            tool_ctx = ToolLoopContext(
                db=db,
                llm_messages=prep.llm_messages,
                model_config=prep.model_config,
                user_id=user_id,
                workspace_id=request.context.workspace_id,
                skill_id=prep.skill_id,
                tools_schema=prep.tools_schema or [],
                initial_native_tool_calls=native_tool_calls or None,
                initial_response=response,
                initial_thinking_content=full_thinking,
                start_block_idx=_next_block_idx,
                security_pipeline=self.security,
            )
            async for item in self.tool_loop.run(tool_ctx):
                if isinstance(item, tuple):
                    response, tool_meta = item
                else:
                    yield item
            yield {"event": "replace", "data": {"text": response}}

        # PPT auto-execution
        if prep.skill_name == "pptx-generation" and "```python" in response:
            tool_meta = skill_engine._execute_pptx_code(response)
        if prep.skill_name == "pptx-generation" and not tool_meta and "```html" in response:
            tool_meta = skill_engine._execute_html_ppt(response)

        if structured_output is not None:
            tool_meta["structured_output"] = structured_output

        # ── 7. OutputFilter ──
        if structured_output:
            filtered = self.security.filter_output(
                db, user_id, prep.skill_id, structured_output,
            )
            if filtered is not structured_output:
                tool_meta["structured_output"] = filtered

        # ── 8. 返回最终结果 ──
        yield ("__result__", {
            "response": response,
            "tool_meta": tool_meta,
            "prep": prep,
        })

    async def run_sync(
        self,
        request: HarnessRequest,
        db: Session,
        conversation: Conversation,
        *,
        active_skill_ids: list[int] | None = None,
        force_skill_id: int | None = None,
    ) -> HarnessResponse:
        """非流式 Chat 执行。"""
        response = ""
        tool_meta: dict = {}
        prep = None
        error = None

        async for item in self.run(
            request, db, conversation,
            active_skill_ids=active_skill_ids,
            force_skill_id=force_skill_id,
        ):
            if isinstance(item, tuple):
                key, data = item
                if key == "__result__":
                    response = data["response"]
                    tool_meta = data["tool_meta"]
                    prep = data["prep"]
            elif isinstance(item, dict) and item.get("event") == "error":
                error = item["data"].get("message", "Unknown error")
            elif isinstance(item, dict) and item.get("event") == "early_return":
                response = item["data"]["response"]
                tool_meta = item["data"].get("metadata", {})

        run_id = ""
        if error:
            return HarnessResponse(
                request_id=request.request_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                error=error,
            )

        return HarnessResponse(
            request_id=request.request_id,
            run_id=run_id,
            status=RunStatus.COMPLETED,
            content=response,
            metadata=tool_meta,
        )


# 全局实例
agent_runtime = AgentRuntime()
