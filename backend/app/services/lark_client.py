"""Feishu (Lark) API client."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_LARK_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: dict = {}  # Simple in-memory cache


class LarkClient:

    def __init__(self):
        self._app_id: str = ""
        self._app_secret: str = ""

    def _load_config(self):
        from app.config import settings
        self._app_id = getattr(settings, "LARK_APP_ID", "")
        self._app_secret = getattr(settings, "LARK_APP_SECRET", "")

    async def get_tenant_access_token(self) -> str:
        """Get or refresh tenant access token."""
        self._load_config()
        import time

        cache_key = self._app_id
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached["expires_at"] > time.time() + 60:
            return cached["token"]

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Lark auth failed: {data.get('msg')}")

            token = data["tenant_access_token"]
            expires_in = data.get("expire", 7200)
            import time
            _TOKEN_CACHE[cache_key] = {
                "token": token,
                "expires_at": time.time() + expires_in,
            }
            return token

    async def send_message(
        self,
        receive_id: str,
        content: str,
        receive_id_type: str = "open_id",
        msg_type: str = "text",
    ) -> bool:
        """Send a message to a Lark user or chat."""
        try:
            token = await self.get_tenant_access_token()
        except Exception as e:
            logger.error(f"Failed to get Lark token: {e}")
            return False

        if msg_type == "text":
            import json
            msg_content = json.dumps({"text": content})
        else:
            msg_content = content

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": msg_content,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"Lark send_message failed: {data.get('msg')}")
                return False
            return True

    async def send_rich_message(
        self,
        receive_id: str,
        title: str,
        content: str,
        receive_id_type: str = "open_id",
    ) -> bool:
        """Send a rich text / markdown-like message via Lark card."""
        import json

        card_content = json.dumps({
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content[:2000],  # Lark card content limit
                }
            ],
        })
        return await self.send_message(
            receive_id, card_content, receive_id_type, msg_type="interactive"
        )

    async def get_user_info(self, open_id: str) -> Optional[dict]:
        """Get Lark user info by open_id."""
        try:
            token = await self.get_tenant_access_token()
        except Exception:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("user")
            return None


lark_client = LarkClient()
