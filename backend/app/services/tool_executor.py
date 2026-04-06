"""Tool execution engine: MCP / builtin / HTTP."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import time
from typing import Any

import httpx
import jsonschema
from sqlalchemy.orm import Session

from app.models.tool import ToolRegistry, ToolType

logger = logging.getLogger(__name__)

# ── 安全：Builtin 工具模块白名单前缀 ────────────────────────────────────────
_ALLOWED_MODULE_PREFIXES = ("app.tools.",)

def _validate_module_path(module_path: str) -> None:
    """校验模块路径在白名单内，防止任意代码执行。"""
    if not any(module_path.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
        raise PermissionError(
            f"模块 '{module_path}' 不在允许列表中，仅允许 {_ALLOWED_MODULE_PREFIXES} 前缀"
        )


async def _check_manifest_preconditions(
    tool: ToolRegistry,
    params: dict,
    db: Session | None,
) -> str | None:
    """检查 manifest 声明的前置条件。返回错误描述字符串，或 None 表示通过。"""
    config = tool.config or {}
    manifest = config.get("manifest")
    if not manifest:
        return None

    errors: list[str] = []

    for ds in manifest.get("data_sources", []):
        key = ds.get("key", "")
        ds_type = ds.get("type", "")
        required = ds.get("required", False)
        description = ds.get("description", key)

        value = params.get(key)

        # ── registered_table ──────────────────────────────────────────
        if ds_type == "registered_table":
            if not value:
                if required:
                    errors.append(
                        f"缺少必填参数 '{key}'（{description}）：需提供已注册的业务表名"
                    )
                continue
            if db is not None:
                from app.models.business import BusinessTable
                exists = db.query(BusinessTable).filter(
                    BusinessTable.table_name == value
                ).first()
                if not exists:
                    errors.append(
                        f"参数 '{key}' 指定的业务表 '{value}' 未在系统中注册，"
                        f"请先在【数据管理】中注册该表，或检查表名是否正确"
                    )

        # ── uploaded_file ──────────────────────────────────────────────
        elif ds_type == "uploaded_file":
            if not value:
                if required:
                    accept = ds.get("accept", [])
                    accept_str = "、".join(accept) if accept else "文件"
                    errors.append(
                        f"缺少必填参数 '{key}'（{description}）：请先上传 {accept_str} 文件，"
                        f"再调用此工具"
                    )
                continue
            # 检查扩展名
            accept = ds.get("accept", [])
            if accept and isinstance(value, str):
                ext = "." + value.rsplit(".", 1)[-1].lower() if "." in value else ""
                if ext and ext not in [a.lower() for a in accept]:
                    errors.append(
                        f"参数 '{key}' 文件类型不符，工具仅接受：{' '.join(accept)}"
                    )

        # ── chat_context ───────────────────────────────────────────────
        elif ds_type == "chat_context":
            if not value and required:
                errors.append(
                    f"缺少必填参数 '{key}'（{description}）：请在对话中提供相关信息"
                )

    if errors:
        # 同时把 preconditions 说明附在错误里，帮助用户理解
        preconditions = manifest.get("preconditions", [])
        msg = "工具前置条件未满足：\n" + "\n".join(f"• {e}" for e in errors)
        if preconditions:
            msg += "\n\n工具说明的运行要求：\n" + "\n".join(f"• {p}" for p in preconditions)
        return msg

    return None


def _validate_params(tool: ToolRegistry, params: dict) -> str | None:
    """Validate params against tool's input_schema. Returns error string or None."""
    schema = tool.input_schema
    if not schema:
        return None
    try:
        jsonschema.validate(instance=params, schema=schema)
        return None
    except jsonschema.ValidationError as e:
        # Build a friendly error message
        path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "根字段"
        return f"参数校验失败（{path}）：{e.message}"
    except jsonschema.SchemaError as e:
        logger.warning(f"Tool '{tool.name}' has invalid schema: {e}")
        return None  # schema itself is broken, skip validation


def _validate_params_with_schema(tool: ToolRegistry, params: dict, schema: dict | None = None) -> str | None:
    """Validate params against a specific schema (for version-pinned tools)."""
    schema = schema or tool.input_schema
    if not schema:
        return None
    try:
        jsonschema.validate(instance=params, schema=schema)
        return None
    except jsonschema.ValidationError as e:
        path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "根字段"
        return f"参数校验失败（{path}）：{e.message}"
    except jsonschema.SchemaError as e:
        logger.warning(f"Tool '{tool.name}' has invalid schema: {e}")
        return None


class ToolExecutor:

    async def execute_tool(
        self,
        db: Session,
        tool_name: str,
        params: dict,
        user_id: int | None = None,
        skill_id: int | None = None,
    ) -> dict:
        """Unified entry point. Returns {"ok": bool, "result": Any, "error": str, "duration_ms": int, "phases": list}."""
        tool = db.query(ToolRegistry).filter(
            ToolRegistry.name == tool_name,
            ToolRegistry.is_active == True,
        ).first()

        if not tool:
            return {"ok": False, "error": f"工具 '{tool_name}' 不存在或已停用", "phases": []}

        # ── 权限检查：用户必须有权调用该工具 ────────────────────────────────
        if user_id is not None:
            from app.models.user import User
            user = db.get(User, user_id)
            if user:
                from app.models.tool import SkillTool
                # 工具必须绑定到当前 Skill（如有），否则仅 SUPER_ADMIN 可直接调用
                if skill_id is not None:
                    bound = db.query(SkillTool).filter(
                        SkillTool.skill_id == skill_id,
                        SkillTool.tool_id == tool.id,
                    ).first()
                    if not bound:
                        from app.models.user import Role
                        if user.role != Role.SUPER_ADMIN:
                            return {
                                "ok": False,
                                "error": f"工具 '{tool_name}' 未绑定到当前 Skill，无权调用",
                                "phases": ["permission_denied"],
                            }

        # Gap 2: 版本解析 — 如果 Skill 绑定了 pinned_version，使用该版本的快照
        _effective_config = tool.config
        _effective_schema = tool.input_schema
        if skill_id is not None:
            from app.models.tool import SkillTool, ToolVersion
            st = db.query(SkillTool).filter(
                SkillTool.skill_id == skill_id, SkillTool.tool_id == tool.id,
            ).first()
            if st and st.pinned_version is not None:
                tv = db.query(ToolVersion).filter(
                    ToolVersion.tool_id == tool.id,
                    ToolVersion.version == st.pinned_version,
                ).first()
                if tv:
                    _effective_config = tv.config_snapshot or tool.config
                    _effective_schema = tv.input_schema_snapshot or tool.input_schema
                    logger.debug(f"Tool '{tool_name}' using pinned version {st.pinned_version}")

        phases = []

        # Schema validation (use effective schema for version-pinned tools)
        validation_error = _validate_params_with_schema(tool, params, _effective_schema)
        if validation_error:
            schema_str = json.dumps(tool.input_schema or {}, ensure_ascii=False)
            return {
                "ok": False,
                "error": (
                    f"{validation_error}\n"
                    f"工具期望的参数格式：{schema_str}"
                ),
                "phases": ["validation_failed"],
            }
        phases.append("validated")

        # Manifest precondition check
        manifest_error = await _check_manifest_preconditions(tool, params, db)
        if manifest_error:
            return {
                "ok": False,
                "error": manifest_error,
                "phases": phases + ["precondition_failed"],
            }
        phases.append("preconditions_ok")

        # Get per-tool timeout from config (default 60s)
        config = tool.config or {}
        timeout_s = config.get("timeout", 60)

        start_ms = int(time.monotonic() * 1000)
        try:
            if tool.tool_type == ToolType.BUILTIN:
                result = await self._execute_builtin(tool, params, db=db, user_id=user_id, timeout_s=timeout_s)
            elif tool.tool_type == ToolType.HTTP:
                result = await self._execute_http(tool, params, timeout_s=timeout_s)
            elif tool.tool_type == ToolType.MCP:
                result = await self._execute_mcp(tool, params, timeout_s=timeout_s)
            else:
                return {"ok": False, "error": f"未知工具类型: {tool.tool_type}", "phases": phases}

            phases.append("executed")
            duration_ms = int(time.monotonic() * 1000) - start_ms
            # L2: 结构化审计日志
            logger.info(
                "tool_audit ok=true tool=%s type=%s user_id=%s skill_id=%s duration_ms=%d",
                tool_name, tool.tool_type.value if tool.tool_type else "?",
                user_id, skill_id, duration_ms,
            )
            return {"ok": True, "result": result, "duration_ms": duration_ms, "phases": phases}
        except Exception as e:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            logger.error(
                "tool_audit ok=false tool=%s type=%s user_id=%s skill_id=%s duration_ms=%d error=%s",
                tool_name, tool.tool_type.value if tool.tool_type else "?",
                user_id, skill_id, duration_ms, str(e)[:200],
            )
            return {"ok": False, "error": str(e), "duration_ms": duration_ms, "phases": phases}

    async def _execute_builtin(
        self,
        tool: ToolRegistry,
        params: dict,
        db: Session | None = None,
        user_id: int | None = None,
        timeout_s: int = 60,
    ) -> Any:
        """Dynamically import and call a Python builtin tool.

        安全措施：
        - 模块路径白名单校验（仅允许 app.tools.* 前缀）
        - 不再 reload 已加载模块（线程安全 + 防磁盘恶意文件）
        - 统一 timeout 包裹
        """
        config = tool.config or {}
        module_path = config.get("module", f"app.tools.{tool.name}")
        func_name = config.get("function", "execute")

        # 安全校验：模块路径必须在白名单内
        _validate_module_path(module_path)

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
            return await asyncio.wait_for(func(**kwargs), timeout=timeout_s)
        else:
            # 同步函数放到线程池，避免阻塞事件循环，同时支持超时
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: func(**kwargs)),
                timeout=timeout_s,
            )

    async def _execute_http(self, tool: ToolRegistry, params: dict, timeout_s: int = 60) -> Any:
        """Call an external HTTP API."""
        config = tool.config or {}
        url = config.get("url", "")
        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            if method == "GET":
                resp = await client.get(url, params=params, headers=headers)
            else:
                resp = await client.request(method, url, json=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _execute_mcp(self, tool: ToolRegistry, params: dict, timeout_s: int = 60) -> Any:
        """Call MCP server via HTTP（服务由 mcp_installer 在审批通过时启动）。"""
        config = tool.config or {}
        url = config.get("url", "").rstrip("/")
        if not url:
            raise ValueError("MCP 服务尚未安装或 URL 未配置，请联系管理员重新审批")

        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool.name, "arguments": params},
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{url}/rpc", json=rpc_request)
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

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

        lines = [
            "## 可用工具",
            "",
            "你可以调用以下工具来完成用户的需求。",
            "",
        ]

        for t in tools:
            schema = t.input_schema or {}
            schema_str = json.dumps(schema, ensure_ascii=False, indent=2)

            # 提取 required 字段列表
            required_fields = schema.get("required", [])
            properties = schema.get("properties", {})

            lines.append(f"### 工具：{t.name}（{t.display_name}）")

            if t.description:
                lines.append(f"**功能说明**：{t.description}")

            # 从 config 提取 usage_hint
            config = t.config or {}
            usage_hint = config.get("usage_hint", "")
            if usage_hint:
                lines.append(f"**使用场景**：{usage_hint}")

            # 参数说明（human-friendly）
            if properties:
                lines.append("**参数说明**：")
                for field_name, field_def in properties.items():
                    field_desc = field_def.get("description", "")
                    field_type = field_def.get("type", "any")
                    required_mark = "（必填）" if field_name in required_fields else "（选填）"
                    lines.append(f"- `{field_name}` [{field_type}]{required_mark}：{field_desc}")

            lines.append(f"**参数 Schema**：\n```json\n{schema_str}\n```")
            lines.append("")

        lines += [
            "## 工具调用规范",
            "",
            "### 何时调用",
            "- 当用户需求明确匹配某个工具时，**必须主动调用**，不要仅用文字描述",
            "- 不确定时宁可调用（工具失败可重试），不要遗漏用户的工具需求",
            "",
            "### 调用格式",
            "在回复中包含 JSON 块（用 ```tool_call 和 ``` 包裹）：",
            "```tool_call",
            '{"tool": "tool_name", "params": {"key": "value"}}',
            "```",
            "",
            "### 正确示例",
            '用户说「帮我生成一份Excel」→ 调用工具，参数从对话上下文提取',
            "",
            "### 错误示例（禁止）",
            '- ❌ 只说「我可以帮你生成Excel」但不调用工具',
            '- ❌ 参数不符合 Schema（如必填字段缺失）',
            '- ❌ 在 tool_call 块外写 JSON',
            "",
            "### 多工具并行",
            "如果需要多个工具，可在同一条回复中包含多个 tool_call 块，它们会并行执行。",
            "",
            "### 处理结果",
            "- 工具成功：基于结果给出清晰回复，**不要重复展示原始 JSON**",
            "- 工具失败：检查错误信息中的参数要求，**修正参数后重试**，最多重试2次",
            "- 多次失败：换一种方式满足用户需求，并说明原因",
        ]

        return "\n".join(lines)


tool_executor = ToolExecutor()
