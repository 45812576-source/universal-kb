import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator, Any
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.models.skill import ModelConfig, ModelAssignment

logger = logging.getLogger(__name__)

# ── 重试配置 ─────────────────────────────────────────────────────────────────
_RETRYABLE_STATUS_CODES = {429, 502, 503}
_RETRY_DELAYS = [1.0, 2.0, 4.0]  # 指数退避（最多 3 次重试）

# ── 调用点注册表 ──────────────────────────────────────────────────────────────
# 每个 slot 对应系统中一个独立的 AI 调用场景，可在 admin 前端绑定不同模型。
# fallback: 未绑定时的默认策略 ("default"=DB默认模型, "lite"=轻量模型, "preflight_exec", "preflight_score")
SLOT_REGISTRY: dict[str, dict] = {
    # ── 对话 ──
    "conversation.main":          {"name": "主对话", "category": "对话", "desc": "用户发消息后的主 LLM 回复（流式），Workspace 模式下直接对话", "fallback": "default"},
    "conversation.title":         {"name": "对话标题生成", "category": "对话", "desc": "超长文件上传后用 LLM 生成摘要作为对话上下文", "fallback": "lite"},
    # ── Skill ──
    "skill.match":                {"name": "Skill 匹配", "category": "Skill", "desc": "根据用户消息从候选 Skill 列表中选出最匹配的一个", "fallback": "lite"},
    "skill.switch_detect":        {"name": "Skill 切换检测", "category": "Skill", "desc": "判断用户是否想切换到另一个 Skill", "fallback": "lite"},
    "skill.extract_params":       {"name": "Skill 参数提取", "category": "Skill", "desc": "从对话中提取 Skill 所需的变量值", "fallback": "default"},
    "skill.rerank":               {"name": "知识片段重排序", "category": "Skill", "desc": "从向量搜索返回的候选 chunks 中筛选最相关的 top_k 条", "fallback": "lite"},
    "skill.execute":              {"name": "Skill 执行", "category": "Skill", "desc": "编译完 prompt 后调用 LLM 生成最终回复（含流式多轮工具调用）", "fallback": "default"},
    "skill.compress_history":     {"name": "历史压缩", "category": "Skill", "desc": "对话轮数过多时用 LLM 压缩历史上下文保留关键信息", "fallback": "lite"},
    "skill.tool_match":           {"name": "工具匹配", "category": "Skill", "desc": "判断用户消息是否想调用某个工具", "fallback": "lite"},
    "skill.tool_param_extract":   {"name": "工具参数提取", "category": "Skill", "desc": "从对话中提取调用工具所需的参数 JSON", "fallback": "lite"},
    "skill.tool_select":          {"name": "工具候选筛选", "category": "Skill", "desc": "从所有可用工具中选出与用户消息最相关的 3-5 个", "fallback": "lite"},
    "skill.tool_output_map":      {"name": "工具输出映射", "category": "Skill", "desc": "将 Skill 的 structured_output 映射为工具的 input_schema", "fallback": "lite"},
    "skill.edit":                 {"name": "Skill 编辑辅助", "category": "Skill", "desc": "AI 辅助生成/优化 Skill 的 system_prompt", "fallback": "default"},
    "skill.security_scan":        {"name": "Skill 安全扫描", "category": "Skill", "desc": "扫描 Skill prompt 是否有注入/越权等安全风险", "fallback": "default"},
    "skill.classify":             {"name": "Skill 分类", "category": "Skill", "desc": "对 Skill 进行智能分类", "fallback": "lite"},
    "skill.run_in_router":        {"name": "Skill 路由执行", "category": "Skill", "desc": "skills router 中直接调用 LLM 执行 Skill", "fallback": "default"},
    # ── 知识 ──
    "knowledge.classify":         {"name": "知识分类", "category": "知识", "desc": "上传知识后用 LLM 自动分类到知识体系", "fallback": "default"},
    "knowledge.name":             {"name": "知识命名", "category": "知识", "desc": "自动为上传的知识文档生成标题", "fallback": "lite"},
    "knowledge.search":           {"name": "知识搜索增强", "category": "知识", "desc": "在知识搜索中用 LLM 增强查询", "fallback": "lite"},
    # ── 项目 ──
    "project.engine":             {"name": "项目引擎", "category": "项目", "desc": "项目中的 AI 调用：生成项目计划/总结/提醒/分析等", "fallback": "default"},
    # ── PEV ──
    "pev.plan":                   {"name": "PEV 规划", "category": "PEV", "desc": "Plan-Execute-Verify 规划阶段：拆解任务步骤", "fallback": "lite"},
    "pev.execute":                {"name": "PEV 执行", "category": "PEV", "desc": "PEV 执行阶段：按步骤逐一执行", "fallback": "default"},
    "pev.verify":                 {"name": "PEV 验证", "category": "PEV", "desc": "PEV 验证阶段：检查执行结果质量", "fallback": "lite"},
    "pev.orchestrate":            {"name": "PEV 编排", "category": "PEV", "desc": "PEV 编排器：判断用户意图是否需要走 PEV 流程", "fallback": "lite"},
    # ── 沙箱 ──
    "sandbox.mock_input":         {"name": "沙箱测试输入生成", "category": "沙箱", "desc": "根据 Skill 的 system_prompt 自动生成模拟测试输入", "fallback": "lite"},
    "sandbox.tool_mock":          {"name": "沙箱工具模拟", "category": "沙箱", "desc": "为 Skill 关联的工具生成模拟输入数据", "fallback": "lite"},
    "sandbox.evaluate":           {"name": "沙箱结果评估", "category": "沙箱", "desc": "评估 Skill 回复质量（通过/不通过 + 评语）", "fallback": "lite"},
    "sandbox.execute":            {"name": "沙箱 Skill 执行", "category": "沙箱", "desc": "在沙箱中实际调用 LLM 执行 Skill", "fallback": "default"},
    "sandbox.fe_detect":          {"name": "沙箱前端检测", "category": "沙箱", "desc": "判断 Skill 的 prompt 是否要求生成前端 UI 代码", "fallback": "lite"},
    "sandbox.preflight_gen":      {"name": "Preflight 用例生成", "category": "沙箱", "desc": "为 Preflight 测试自动生成测试用例", "fallback": "lite"},
    "sandbox.preflight_exec":     {"name": "Preflight 执行", "category": "沙箱", "desc": "Preflight 测试中实际执行 Skill 的 LLM 调用", "fallback": "preflight_exec"},
    "sandbox.preflight_score":    {"name": "Preflight 评分", "category": "沙箱", "desc": "Preflight 测试中对每条回复进行质量评分", "fallback": "preflight_score"},
    # ── 其他 ──
    # ── 治理 ──
    "governance.classify":        {"name": "治理分类", "category": "治理", "desc": "LLM fallback：关键词规则置信度不足时用 LLM 做治理分类", "fallback": "lite"},
    "governance.suggest":         {"name": "治理建议生成", "category": "治理", "desc": "基于补充资料生成增量治理骨架/策略", "fallback": "lite"},
    # ── 其他 ──
    "input.evaluate":             {"name": "输入评估", "category": "其他", "desc": "评估用户输入是否有效/是否需要澄清", "fallback": "lite"},
    "input.process":              {"name": "输入处理", "category": "其他", "desc": "预处理用户输入（意图识别、实体提取等）", "fallback": "default"},
    "schema.generate":            {"name": "Schema 生成", "category": "其他", "desc": "根据 Skill 描述自动生成 output_schema JSON", "fallback": "default"},
    "attribution":                {"name": "归因分析", "category": "其他", "desc": "分析 AI 回复中哪些内容来自哪些知识源", "fallback": "default"},
    "intel.collect":              {"name": "情报收集", "category": "其他", "desc": "收集和分析竞品/市场情报", "fallback": "default"},
    "rule.engine":                {"name": "规则引擎", "category": "其他", "desc": "用 LLM 执行自然语言规则判断", "fallback": "default"},
    "studio.agent":               {"name": "Studio Agent", "category": "其他", "desc": "DevStudio 中的 AI 对话（流式+非流式）", "fallback": "default"},
    "task.engine":                {"name": "任务引擎", "category": "其他", "desc": "项目任务中的 AI 辅助（总结/分解任务等）", "fallback": "default"},
    "business_table.generate":    {"name": "业务表格生成", "category": "其他", "desc": "用 LLM 解析业务数据生成结构化表格", "fallback": "default"},
    "output_schema.gen":          {"name": "输出模式生成", "category": "其他", "desc": "根据描述自动生成结构化输出 Schema", "fallback": "default"},
    "file.parse":                 {"name": "文件解析", "category": "其他", "desc": "解析上传文件内容时用 LLM 辅助理解", "fallback": "lite"},
    "tool.web_builder":           {"name": "网页生成", "category": "工具", "desc": "根据用户描述生成 HTML 网页代码", "fallback": "default"},
    "tool.brainstorming":         {"name": "头脑风暴", "category": "工具", "desc": "AI 辅助头脑风暴，生成创意发散", "fallback": "lite"},
    "tool.data_engine":           {"name": "数据引擎", "category": "工具", "desc": "用 LLM 解析和转换数据", "fallback": "default"},
    "tool.input_evaluator":       {"name": "工具输入评估", "category": "工具", "desc": "评估工具调用的输入参数是否合理", "fallback": "lite"},
}

# Load .env from backend root (in case env vars not injected by process)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent.parent / ".env"
    load_dotenv(_env_path, override=False)
except ImportError:
    pass


class LLMGateway:
    """统一LLM调用层，所有供应商走 OpenAI-compatible chat completions 接口。"""

    # 已知不支持 function calling 的模型（文本 fallback）
    _NO_FUNCTION_CALLING = {"moonshot-v1-8k-thinking", "moonshot-v1-32k-thinking"}

    def __init__(self):
        # 全局连接池：复用 TCP/TLS 连接，避免每次 LLM 调用都新建客户端
        # keepalive_expiry=30s：服务端通常在 60-90s 后关闭空闲连接，设为 30s 可避免复用已死连接
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20,
                                keepalive_expiry=30),
        )

    def supports_function_calling(self, model_config: dict) -> bool:
        return model_config.get("model_id", "") not in self._NO_FUNCTION_CALLING

    def _build_request(self, model_config: dict, messages: list[dict],
                       temperature: float = None, max_tokens: int = None,
                       stream: bool = False,
                       tools: list[dict] | None = None) -> tuple[str, dict, dict]:
        api_base = model_config["api_base"].rstrip("/")
        api_key = model_config.get("api_key") or os.getenv(
            model_config.get("api_key_env", ""), ""
        )
        if not api_key:
            env_var = model_config.get("api_key_env", "API_KEY")
            raise ValueError(f"LLM API key not configured. Please set the '{env_var}' environment variable.")
        temp = temperature if temperature is not None else float(model_config.get("temperature", 0.7))
        tokens = max_tokens or model_config.get("max_tokens", 4096)
        provider = model_config.get("provider", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": model_config["model_id"],
            "messages": messages,
            "max_tokens": tokens,
        }
        # moonshot thinking models only accept temperature=1
        if provider == "moonshot":
            body["temperature"] = 1
        else:
            body["temperature"] = temp
        if stream:
            body["stream"] = True
        if tools and self.supports_function_calling(model_config):
            body["tools"] = tools
            body["tool_choice"] = "auto"

        return f"{api_base}/chat/completions", headers, body

    async def chat(self, model_config: dict, messages: list[dict],
                   temperature: float = None, max_tokens: int = None,
                   tools: list[dict] | None = None) -> tuple[str, dict]:
        """Returns (content, usage) where usage = {input_tokens, output_tokens, model_id}.

        当 tools 非空且模型支持 function calling 时，native tool_calls 会被序列化后追加到
        content 尾部（```tool_call 格式），与文本 fallback 保持兼容。

        重试策略：ConnectTimeout/ReadTimeout/429/502/503 → 指数退避最多 3 次。
        """
        url, headers, body = self._build_request(model_config, messages, temperature, max_tokens, tools=tools)
        t0 = time.monotonic()
        last_exc: Exception | None = None
        resp = None
        for _attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                resp = await self._client.post(url, headers=headers, json=body)
                if resp.status_code in _RETRYABLE_STATUS_CODES and _attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[_attempt]
                    logger.warning(
                        f"LLM HTTP {resp.status_code} on attempt {_attempt+1}, "
                        f"retrying in {delay}s... [{model_config.get('model_id')}]"
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                if _attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[_attempt]
                    logger.warning(
                        f"LLM {type(e).__name__} on attempt {_attempt+1}, "
                        f"retrying in {delay}s... [{model_config.get('model_id')}]"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        if resp is None:
            raise last_exc or RuntimeError("LLM call failed after retries")
        elapsed = time.monotonic() - t0
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or ""

        # 处理 native function calling 响应：将 tool_calls 转为文本 fallback 格式
        native_tool_calls = msg.get("tool_calls") or []
        if native_tool_calls:
            import json as _json
            for tc in native_tool_calls:
                fn = tc.get("function", {})
                try:
                    args = _json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                content += f"\n```tool_call\n{_json.dumps({'id': tc.get('id', ''), 'name': fn.get('name', ''), 'arguments': args}, ensure_ascii=False)}\n```"

        raw_usage = data.get("usage") or {}
        usage = {
            "input_tokens": raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0,
            "output_tokens": raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0,
            "model_id": model_config.get("model_id", ""),
        }
        # L2: 结构化 LLM 审计日志（含 token 用量）
        logger.info(
            "llm_audit model=%s elapsed=%.1fs in_tokens=%d out_tokens=%d tool_calls=%d",
            model_config.get("model_id", "?"), elapsed,
            usage["input_tokens"], usage["output_tokens"], len(native_tool_calls),
        )
        return content, usage

    async def chat_stream(self, model_config: dict, messages: list[dict],
                          temperature: float = None, max_tokens: int = None) -> AsyncIterator[str]:
        """Yields content text chunks (backward compatible)."""
        async for chunk_type, text in self.chat_stream_typed(model_config, messages, temperature, max_tokens):
            if chunk_type == "content":
                yield text

    async def chat_stream_typed(
        self,
        model_config: dict,
        messages: list[dict],
        temperature: float = None,
        max_tokens: int = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yields (chunk_type, data):
          - ("thinking", str)   — 推理链文本
          - ("content",  str)   — 普通回复文本
          - ("tool_call", dict) — 原生工具调用 {"id", "name", "arguments": str}
                                   仅当传入 tools 且模型支持 function calling 时出现
        """
        url, headers, body = self._build_request(
            model_config, messages, temperature, max_tokens, stream=True, tools=tools
        )
        # 若模型不支持 function calling，tools 已被 _build_request 忽略
        use_native_tools = bool(tools and self.supports_function_calling(model_config))

        # Streaming 连接级重试：仅在连接阶段重试，一旦开始接收 chunk 则不重试
        last_exc: Exception | None = None
        for _attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                async with self._client.stream("POST", url, headers=headers, json=body) as resp:
                    status_code = getattr(resp, "status_code", 200)
                    if status_code in _RETRYABLE_STATUS_CODES and _attempt < len(_RETRY_DELAYS):
                        error_body = await resp.aread()
                        delay = _RETRY_DELAYS[_attempt]
                        logger.warning(
                            f"LLM stream HTTP {status_code} on attempt {_attempt+1}, "
                            f"retrying in {delay}s... [{model_config.get('model_id')}] "
                            f"body={error_body.decode()[:200]}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    if status_code >= 400:
                        error_body = await resp.aread()
                        raise ValueError(f"LLM API error {status_code}: {error_body.decode()[:300]}")

                    tool_calls_buf: dict[int, dict] = {}  # index → {id, name, arguments}

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        try:
                            chunk = json.loads(line[6:])
                            choice = chunk["choices"][0]
                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason")

                            # thinking block
                            if reasoning := delta.get("reasoning_content"):
                                yield ("thinking", reasoning)

                            # normal content
                            if content := delta.get("content"):
                                yield ("content", content)

                            # native tool_calls delta accumulation
                            if use_native_tools:
                                for tc in delta.get("tool_calls") or []:
                                    idx = tc.get("index", 0)
                                    buf = tool_calls_buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                    if tc.get("id"):
                                        buf["id"] = tc["id"]
                                    if fn := tc.get("function"):
                                        buf["name"] += fn.get("name", "")
                                        buf["arguments"] += fn.get("arguments", "")

                                if finish_reason == "tool_calls" and tool_calls_buf:
                                    for buf in tool_calls_buf.values():
                                        yield ("tool_call", buf)
                                    tool_calls_buf.clear()

                        except json.JSONDecodeError as e:
                            logger.warning(f"LLM stream chunk parse error: {e}, raw={line[:200]}")
                            continue
                        except KeyError as e:
                            logger.warning(f"LLM stream chunk missing key: {e}, raw={line[:200]}")
                            continue
                    return  # 正常完成，退出重试循环

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                if _attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[_attempt]
                    logger.warning(
                        f"LLM stream {type(e).__name__} on attempt {_attempt+1}, "
                        f"retrying in {delay}s... [{model_config.get('model_id')}]"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        if last_exc:
            raise last_exc

    def get_lite_config(self) -> dict:
        """Lightweight LLM config for intent/input checks (skill matching, rerank, etc.).
        使用 Ark deepseek-v3.2（非 thinking 模型，RTT 低且稳定）。
        """
        ark_key = os.getenv("ARK_API_KEY", "")
        if not ark_key:
            raise ValueError("No lite LLM API key found. Set ARK_API_KEY.")
        return {
            "provider": "ark",
            "model_id": "deepseek-v3.2",
            "api_base": "https://ark.cn-beijing.volces.com/api/coding/v3",
            "api_key_env": "ARK_API_KEY",
            "api_key": ark_key,
            "max_tokens": 512,
            "temperature": 0.1,
        }

    def get_preflight_exec_config(self) -> dict:
        """Preflight 执行 Skill 测试用 doubao-seed-2.0-pro (ARK)。"""
        ark_key = os.getenv("ARK_API_KEY", "")
        if not ark_key:
            raise ValueError("No ARK API key found. Set ARK_API_KEY.")
        return {
            "provider": "ark",
            "model_id": "doubao-seed-2.0-pro",
            "api_base": "https://ark.cn-beijing.volces.com/api/coding/v3",
            "api_key_env": "ARK_API_KEY",
            "api_key": ark_key,
            "max_tokens": 4096,
            "temperature": 0.7,
        }

    def get_preflight_score_config(self) -> dict:
        """Preflight 质量评分用 kimi-k2.5 (百炼 Coding Plan)。"""
        bailian_key = os.getenv("BAILIAN_API_KEY", "")
        if not bailian_key:
            raise ValueError("No BAILIAN API key found. Set BAILIAN_API_KEY.")
        return {
            "provider": "bailian",
            "model_id": "kimi-k2.5",
            "api_base": "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1",
            "api_key_env": "BAILIAN_API_KEY",
            "api_key": bailian_key,
            "max_tokens": 2048,
            "temperature": 0.0,
        }

    def get_config(self, db: Session, model_config_id: int = None) -> dict:
        """Get model config dict from DB. Falls back to default if id not given."""
        if model_config_id:
            mc = db.get(ModelConfig, model_config_id)
        else:
            mc = db.query(ModelConfig).filter(ModelConfig.is_default == True).first()

        if not mc:
            raise ValueError("No model config found. Please configure a model in admin settings.")

        return {
            "provider": mc.provider,
            "model_id": mc.model_id,
            "api_base": mc.api_base,
            "api_key_env": mc.api_key_env,
            "max_tokens": mc.max_tokens,
            "temperature": mc.temperature,
        }

    async def close(self):
        """关闭底层 httpx 连接池，用于优雅关机。"""
        await self._client.aclose()

    def resolve_config(self, db: Session, slot_key: str, model_config_id: int = None) -> dict:
        """按调用点解析模型配置。

        优先级：
        1. 调用方显式传入的 model_config_id（如 Skill 绑定的模型）
        2. model_assignments 表中的绑定
        3. SLOT_REGISTRY 中的 fallback 策略
        """
        if model_config_id:
            return self.get_config(db, model_config_id)

        assignment = db.query(ModelAssignment).filter_by(slot_key=slot_key).first()
        if assignment:
            return self.get_config(db, assignment.model_config_id)

        slot = SLOT_REGISTRY.get(slot_key, {})
        fb = slot.get("fallback", "default")
        if fb == "lite":
            return self.get_lite_config()
        elif fb == "preflight_exec":
            return self.get_preflight_exec_config()
        elif fb == "preflight_score":
            return self.get_preflight_score_config()
        else:
            return self.get_config(db)


llm_gateway = LLMGateway()
