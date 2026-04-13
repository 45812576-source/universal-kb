"""ToolLoop — 统一工具调用循环，兼容 native FC + 文本 fallback + 多轮。

从 skill_engine._handle_tool_calls_stream 抽出，增加 SecurityPipeline 检查点。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)


@dataclass
class ToolLoopContext:
    """ToolLoop 运行所需的上下文。"""
    db: Session
    llm_messages: list[dict]
    model_config: dict
    user_id: int | None = None
    workspace_id: int | None = None
    skill_id: int | None = None
    tools_schema: list[dict] = field(default_factory=list)
    # 首轮原生 tool_calls（来自 LLM 的第一轮响应）
    initial_native_tool_calls: list[dict] | None = None
    # 首轮 LLM 文本响应（来自 LLM 的第一轮响应，可能含 ```tool_call``` 块）
    initial_response: str = ""
    # thinking 模型的 reasoning_content
    initial_thinking_content: str = ""
    max_rounds: int = 5
    start_block_idx: int = 1
    # SecurityPipeline 实例（可选，Step 3 接入）
    security_pipeline: Any = None


@dataclass
class ToolLoopEvent:
    """ToolLoop 发出的标准化事件。"""
    event: str
    data: dict[str, Any]


# 常量
_AGENT_LOOP_TIMEOUT = 300  # H1: Agent Loop 总超时 (秒)
_TOOL_EXEC_TIMEOUT = 60    # H1: 单次 tool 执行超时 (秒)


class ToolLoop:
    """统一工具调用循环，兼容 native FC + 文本 fallback + 多轮。"""

    async def run(self, context: ToolLoopContext) -> AsyncGenerator[dict | tuple, None]:
        """执行工具调用循环。

        Yields:
            - dict: SSE-ready 事件 {"event": ..., "data": ...}
            - tuple: 最终结果 (response_text, extra_meta)
        """
        db = context.db
        llm_messages = context.llm_messages
        model_config = context.model_config
        user_id = context.user_id
        tools_schema = context.tools_schema
        native_tool_calls = context.initial_native_tool_calls
        response = context.initial_response
        thinking_content = context.initial_thinking_content
        max_rounds = context.max_rounds
        security_pipeline = context.security_pipeline

        _loop_deadline = _time.monotonic() + _AGENT_LOOP_TIMEOUT
        extra_meta: dict = {}
        block_idx = context.start_block_idx
        consecutive_failures = 0

        # 提取原始用户请求用于目标复述
        original_user_request = ""
        for m in reversed(llm_messages):
            if m.get("role") == "user":
                original_user_request = (m.get("content") or "")[:200]
                break

        for round_num in range(max_rounds):
            # H1: 总超时检查
            if _time.monotonic() > _loop_deadline:
                logger.warning("Agent Loop 超时 (%ds)，强制终止", _AGENT_LOOP_TIMEOUT)
                break

            # 解析本轮工具调用列表
            if native_tool_calls:
                calls = native_tool_calls
                use_native = True
            else:
                pattern = r"```tool_call\s*(.*?)\s*```"
                raw_matches = re.findall(pattern, response, re.DOTALL)
                calls = []
                for m in raw_matches:
                    try:
                        parsed = json.loads(m)
                        calls.append(parsed)
                    except json.JSONDecodeError:
                        pass
                use_native = False

            if not calls:
                break

            # ── SecurityPipeline 检查点 ──
            if security_pipeline:
                allowed_calls = []
                for call in calls:
                    tool_name = call.get("tool") or call.get("name", "")
                    decision = await security_pipeline.check_tool_call(
                        db=db,
                        user_id=user_id,
                        tool_name=tool_name,
                        tool_args=call,
                        workspace_id=context.workspace_id,
                        skill_id=context.skill_id,
                    )
                    if decision.status.value in ("deny", "needs_approval"):
                        error_prefix = "安全检查拒绝" if decision.status.value == "deny" else "需要人工审批"
                        yield {"event": "content_block_start", "data": {
                            "index": block_idx, "type": "tool_call",
                            "tool": tool_name, "input": {},
                        }}
                        yield {"event": "content_block_stop", "data": {
                            "index": block_idx, "type": "tool_call",
                            "tool": tool_name,
                            "result": json.dumps({"ok": False, "error": f"{error_prefix}: {decision.reason}"}, ensure_ascii=False),
                            "ok": False,
                            "security_status": decision.status.value,
                        }}
                        block_idx += 1
                        if decision.status.value == "needs_approval":
                            yield {"event": "approval_request", "data": {
                                "tool": tool_name,
                                "reason": decision.reason,
                            }}
                        continue
                    allowed_calls.append(call)
                calls = allowed_calls
                if not calls:
                    yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": False, "reason": "security_blocked"}}
                    break

            yield {"event": "round_start", "data": {"round": round_num + 1, "max_rounds": max_rounds}}

            # 并行触发所有工具
            call_indices: dict[int, dict] = {}
            for call in calls:
                tool_name = call.get("tool") or call.get("name", "")
                raw_args = call.get("params") or call.get("arguments", {})
                params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                yield {"event": "content_block_start", "data": {
                    "index": block_idx, "type": "tool_call",
                    "tool": tool_name, "input": params,
                }}
                yield {"event": "tool_progress", "data": {
                    "index": block_idx, "message": "校验参数...",
                    "phase": "validating",
                }}
                call_indices[block_idx] = call
                block_idx += 1

            # 并行执行
            pairs = await self._execute_tools_parallel(db, calls, user_id)

            # 收集结果并发送 stop 事件
            tool_results = []
            result_block_start = block_idx - len(calls)
            for i, (call, result) in enumerate(pairs):
                tool_name = call.get("tool") or call.get("name", "")
                ok = result.get("ok", False)
                duration_ms = result.get("duration_ms")

                if ok and isinstance(result.get("result"), dict):
                    tool_result_data = result["result"]
                    if "download_url" in tool_result_data:
                        extra_meta["download_url"] = tool_result_data["download_url"]
                    if "filename" in tool_result_data:
                        extra_meta["download_filename"] = tool_result_data["filename"]

                result_str = json.dumps(result, ensure_ascii=False, indent=2)
                yield {"event": "content_block_stop", "data": {
                    "index": result_block_start + i, "type": "tool_call",
                    "tool": tool_name, "result": result_str, "ok": ok,
                    "duration_ms": duration_ms,
                }}

                if ok:
                    tool_results.append(f"工具 `{tool_name}` 执行结果：\n```json\n{result_str}\n```")
                else:
                    raw_args = call.get("params") or call.get("arguments", {})
                    params_used = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    from app.services.tool_executor import tool_executor as _te
                    _tool_obj = None
                    try:
                        from app.models.tool import ToolRegistry as _TR
                        _tool_obj = db.query(_TR).filter(_TR.name == tool_name).first()
                    except Exception:
                        pass
                    try:
                        schema_hint = json.dumps(_tool_obj.input_schema or {}, ensure_ascii=False) if _tool_obj else "{}"
                    except Exception:
                        schema_hint = "{}"
                    error_context = (
                        f"工具 `{tool_name}` 执行失败。\n"
                        f"错误信息：{result.get('error')}\n"
                        f"传入参数：{json.dumps(params_used, ensure_ascii=False)}\n"
                        f"工具期望的参数格式：{schema_hint}\n"
                        f"请检查参数并重试，或换一种方式完成用户的需求。"
                    )
                    tool_results.append(error_context)

            if not tool_results:
                yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": False}}
                break

            # 连续失败早停
            all_failed = all(not r.get("ok", False) for _, r in pairs)
            if all_failed:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= 2:
                logger.warning(f"Agent loop early stop: {consecutive_failures} consecutive all-fail rounds")
                yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": False, "reason": "consecutive_failures"}}
                break

            tool_result_text = "\n\n".join(tool_results)

            # 构建下一轮 messages
            if use_native:
                tool_calls_msg: list[dict] = []
                for call in calls:
                    raw_args = call.get("params") or call.get("arguments", {})
                    tool_calls_msg.append({
                        "id": call.get("id", f"call_{call.get('name', '')}"),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False),
                        },
                    })
                asst_msg: dict = {"role": "assistant", "content": None, "tool_calls": tool_calls_msg}
                if thinking_content:
                    asst_msg["reasoning_content"] = thinking_content
                llm_messages.append(asst_msg)
                thinking_content = ""
                for call, result in pairs:
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", f"call_{call.get('name', '')}"),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                goal_reminder = ""
                if round_num >= 1 and original_user_request:
                    goal_reminder = f"\n\n[提醒] 用户的原始请求是：{original_user_request}"
                llm_messages.append({"role": "assistant", "content": response or "(calling tools)"})
                llm_messages.append({
                    "role": "user",
                    "content": f"[工具执行结果]\n\n{tool_result_text}\n\n请基于以上工具结果，给出最终回复。不需要重复展示JSON，直接告知用户结果即可。{goal_reminder}",
                })

            # 流式下一轮 LLM 响应
            yield {"event": "content_block_start", "data": {"index": block_idx, "type": "text"}}
            new_response = ""
            next_native_calls: list[dict] = []
            next_thinking_content = ""

            async for chunk_type, chunk_data in llm_gateway.chat_stream_typed(
                model_config=model_config,
                messages=llm_messages,
                tools=tools_schema if use_native else None,
            ):
                if chunk_type == "tool_call":
                    next_native_calls.append(chunk_data)
                elif chunk_type == "thinking":
                    next_thinking_content += chunk_data
                elif chunk_type == "content":
                    new_response += chunk_data
                    yield {"event": "content_block_delta", "data": {"index": block_idx, "delta": {"text": chunk_data}}}
                    yield {"event": "delta", "data": {"text": chunk_data}}

            yield {"event": "content_block_stop", "data": {"index": block_idx}}
            block_idx += 1
            response = new_response

            has_next = bool(next_native_calls) or "```tool_call" in response
            yield {"event": "round_end", "data": {"round": round_num + 1, "has_next": has_next}}

            if not has_next:
                break

            native_tool_calls = next_native_calls if next_native_calls else None
            thinking_content = next_thinking_content

        response = re.sub(r"```tool_call\s*.*?\s*```", "", response, flags=re.DOTALL).strip()
        yield (response, extra_meta)

    async def run_sync(self, context: ToolLoopContext) -> tuple[str, dict]:
        """非流式包装 — 收集所有事件，返回最终 (response, meta)。"""
        response = ""
        extra_meta: dict = {}
        async for item in self.run(context):
            if isinstance(item, tuple):
                response, extra_meta = item
        return response, extra_meta

    async def _execute_tools_parallel(
        self,
        db: Session,
        tool_calls: list[dict],
        user_id: int | None,
    ) -> list[tuple[dict, dict]]:
        """并行执行所有工具调用，返回 [(call, result), ...]。"""
        from app.services.tool_executor import tool_executor

        async def _exec_one(call: dict) -> tuple[dict, dict]:
            tool_name = call.get("tool") or call.get("name", "")
            raw_args = call.get("params") or call.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    params = json.loads(raw_args)
                except json.JSONDecodeError:
                    params = {}
            else:
                params = raw_args
            try:
                result = await asyncio.wait_for(
                    tool_executor.execute_tool(db, tool_name, params, user_id),
                    timeout=_TOOL_EXEC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Tool '%s' 执行超时 (%ds)", tool_name, _TOOL_EXEC_TIMEOUT)
                result = {"ok": False, "error": f"工具 {tool_name} 执行超时"}
            return call, result

        return list(await asyncio.gather(*[_exec_one(c) for c in tool_calls]))


tool_loop = ToolLoop()
