import json
import os
from typing import AsyncIterator

import httpx
from sqlalchemy.orm import Session

from app.models.skill import ModelConfig


class LLMGateway:
    """统一LLM调用层，所有供应商走 OpenAI-compatible chat completions 接口。"""

    def _build_request(self, model_config: dict, messages: list[dict],
                       temperature: float = None, max_tokens: int = None,
                       stream: bool = False) -> tuple[str, dict, dict]:
        api_base = model_config["api_base"].rstrip("/")
        api_key = model_config.get("api_key") or os.getenv(
            model_config.get("api_key_env", ""), ""
        )
        if not api_key:
            env_var = model_config.get("api_key_env", "API_KEY")
            raise ValueError(f"LLM API key not configured. Please set the '{env_var}' environment variable.")
        temp = temperature if temperature is not None else float(model_config.get("temperature", 0.7))
        tokens = max_tokens or model_config.get("max_tokens", 4096)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_config["model_id"],
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }
        if stream:
            body["stream"] = True

        return f"{api_base}/chat/completions", headers, body

    async def chat(self, model_config: dict, messages: list[dict],
                   temperature: float = None, max_tokens: int = None) -> str:
        url, headers, body = self._build_request(model_config, messages, temperature, max_tokens)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def chat_stream(self, model_config: dict, messages: list[dict],
                          temperature: float = None, max_tokens: int = None) -> AsyncIterator[str]:
        url, headers, body = self._build_request(
            model_config, messages, temperature, max_tokens, stream=True
        )
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            delta = chunk["choices"][0].get("delta", {})
                            if content := delta.get("content"):
                                yield content
                        except (json.JSONDecodeError, KeyError):
                            continue

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
