"""Tool execution engine: MCP / builtin / HTTP."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import subprocess
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.tool import ToolRegistry, ToolType

logger = logging.getLogger(__name__)


class ToolExecutor:

    async def execute_tool(
        self,
        db: Session,
        tool_name: str,
        params: dict,
        user_id: int | None = None,
    ) -> dict:
        """Unified entry point. Returns {"ok": bool, "result": Any, "error": str}."""
        tool = db.query(ToolRegistry).filter(
            ToolRegistry.name == tool_name,
            ToolRegistry.is_active == True,
        ).first()

        if not tool:
            return {"ok": False, "error": f"Tool '{tool_name}' not found or inactive"}

        try:
            if tool.tool_type == ToolType.BUILTIN:
                result = await self._execute_builtin(tool, params, db=db, user_id=user_id)
            elif tool.tool_type == ToolType.HTTP:
                result = await self._execute_http(tool, params)
            elif tool.tool_type == ToolType.MCP:
                result = await self._execute_mcp(tool, params)
            else:
                return {"ok": False, "error": f"Unknown tool type: {tool.tool_type}"}

            return {"ok": True, "result": result}
        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution failed: {e}")
            return {"ok": False, "error": str(e)}

    async def _execute_builtin(
        self,
        tool: ToolRegistry,
        params: dict,
        db: Session | None = None,
        user_id: int | None = None,
    ) -> Any:
        """Dynamically import and call a Python builtin tool.

        Injects db/user_id when the tool function accepts them,
        so existing tools (ppt_generator, excel_generator) are unaffected.
        """
        config = tool.config or {}
        module_path = config.get("module", f"app.tools.{tool.name}")
        func_name = config.get("function", "execute")

        module = importlib.import_module(module_path)
        func = getattr(module, func_name)

        # Build kwargs — inject db/user_id only if the function declares them
        sig = inspect.signature(func)
        kwargs: dict[str, Any] = {"params": params}
        if "db" in sig.parameters:
            kwargs["db"] = db
        if "user_id" in sig.parameters:
            kwargs["user_id"] = user_id

        if asyncio.iscoroutinefunction(func):
            return await func(**kwargs)
        else:
            return func(**kwargs)

    async def _execute_http(self, tool: ToolRegistry, params: dict) -> Any:
        """Call an external HTTP API."""
        config = tool.config or {}
        url = config.get("url", "")
        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})

        async with httpx.AsyncClient(timeout=60) as client:
            if method == "GET":
                resp = await client.get(url, params=params, headers=headers)
            else:
                resp = await client.request(method, url, json=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _execute_mcp(self, tool: ToolRegistry, params: dict) -> Any:
        """Start MCP server subprocess and call via stdio JSON-RPC."""
        config = tool.config or {}
        command = config.get("command", "")
        args = config.get("args", [])
        env = config.get("env", {})

        if not command:
            raise ValueError("MCP tool missing 'command' in config")

        import os
        proc_env = {**os.environ, **env}

        # Build JSON-RPC request
        rpc_request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool.name,
                "arguments": params,
            },
        }) + "\n"

        proc = subprocess.run(
            [command] + args,
            input=rpc_request.encode(),
            capture_output=True,
            timeout=60,
            env=proc_env,
        )

        if proc.returncode != 0:
            raise RuntimeError(f"MCP process exited {proc.returncode}: {proc.stderr.decode()}")

        response = json.loads(proc.stdout.decode())
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")

        return response.get("result", {})

    def get_tools_for_skill(self, db: Session, skill_id: int) -> list[ToolRegistry]:
        """Return active tools bound to a skill."""
        from app.models.tool import SkillTool
        rows = (
            db.query(ToolRegistry)
            .join(SkillTool, SkillTool.tool_id == ToolRegistry.id)
            .filter(SkillTool.skill_id == skill_id, ToolRegistry.is_active == True)
            .all()
        )
        return rows

    def build_tool_list_prompt(self, tools: list[ToolRegistry]) -> str:
        """Format tool list for injection into system prompt."""
        if not tools:
            return ""
        lines = ["## 可用工具", ""]
        for t in tools:
            schema_str = json.dumps(t.input_schema or {}, ensure_ascii=False, indent=2)
            lines.append(f"### {t.name} ({t.display_name})")
            if t.description:
                lines.append(t.description)
            lines.append(f"参数Schema:\n```json\n{schema_str}\n```")
            lines.append("")
        lines.append(
            "当需要调用工具时，请在回复中包含如下JSON块（用```tool_call和```包裹）：\n"
            "```tool_call\n"
            '{"tool": "tool_name", "params": {...}}\n'
            "```"
        )
        return "\n".join(lines)


tool_executor = ToolExecutor()
