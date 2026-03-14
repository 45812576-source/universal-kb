import json
import os
from typing import AsyncIterator, Any
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.models.skill import ModelConfig

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
        """
        url, headers, body = self._build_request(model_config, messages, temperature, max_tokens, tools=tools)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=body)
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

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    raise ValueError(f"LLM API error {resp.status_code}: {error_body.decode()[:300]}")

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

                    except (json.JSONDecodeError, KeyError):
                        continue

    def get_lite_config(self) -> dict:
        """DeepSeek config for lightweight calls (intent/input checks). No DB needed."""
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")
        return {
            "provider": "deepseek",
            "model_id": "deepseek-chat",
            "api_base": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_key": api_key,
            "max_tokens": 512,
            "temperature": "0.1",
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


llm_gateway = LLMGateway()
