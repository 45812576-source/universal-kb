"""Feishu (Lark) API client."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_LARK_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: dict = {}  # Simple in-memory cache


class LarkConfigError(Exception):
    """飞书应用配置缺失（LARK_APP_ID / LARK_APP_SECRET 未配置）"""


class LarkAuthError(Exception):
    """飞书租户 token 获取失败（应用配置错误、secret 错误等）"""


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
        if not self._app_id or not self._app_secret:
            raise LarkConfigError("飞书集成尚未配置，请联系管理员")
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
                raise LarkAuthError(f"飞书认证失败: {data.get('msg')}")

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

    # ── 审批 API ─────────────────────────────────────────────────────────

    async def create_approval_instance(
        self,
        approval_code: str,
        open_id: str,
        form: list[dict],
        node_approver_open_ids: list[str] | None = None,
        urgency: str = "normal",
    ) -> dict:
        """创建飞书审批实例。返回 {"instance_code": "..."}。

        注意：飞书 API 要求 form 是 JSON 字符串，不是 object。
        """
        import json
        token = await self.get_tenant_access_token()

        body: dict = {
            "approval_code": approval_code,
            "open_id": open_id,
            "form": json.dumps(form, ensure_ascii=False),
        }
        if node_approver_open_ids:
            body["node_approver_open_id_list"] = [
                {"key": "default", "value": node_approver_open_ids}
            ]
        if urgency == "urgent":
            body["urgency"] = "urgent"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/approval/v4/instances",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"创建审批失败: {data.get('msg')} (code={data.get('code')})")
            return {"instance_code": data["data"]["instance_code"]}

    async def get_approval_instance(self, instance_code: str) -> dict:
        """查询飞书审批实例详情。"""
        token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/approval/v4/instances/{instance_code}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"查询审批失败: {data.get('msg')}")
            return data.get("data", {})

    async def list_approval_definitions(self) -> list[dict]:
        """获取企业可用的审批定义列表。"""
        token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/approval/v4/approvals",
                headers={"Authorization": f"Bearer {token}"},
                params={"page_size": 100},
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning(f"List approval definitions failed: {data.get('msg')}")
                return []
            items = data.get("data", {}).get("items", [])
            return [
                {
                    "approval_code": it.get("approval_code", ""),
                    "approval_name": it.get("approval_name", ""),
                    "status": it.get("status", ""),
                }
                for it in items
            ]

    # ── 文档导出 API ────────────────────────────────────────────────────

    async def get_wiki_node(self, token: str) -> dict:
        """解析 wiki token 为实际文档 obj_token + obj_type。"""
        access_token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/wiki/v2/spaces/get_node",
                params={"token": token},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                msg = data.get("msg", "")
                if "permission" in msg.lower() or "denied" in msg.lower() or data.get("code") == 99991672:
                    raise PermissionError(
                        "知识库节点权限不足，请在飞书管理后台为应用开通该知识空间的阅读权限。"
                        "操作路径：飞书管理后台 → 知识库 → 空间设置 → 添加应用为成员"
                    )
                raise RuntimeError(f"解析 wiki 节点失败: {msg}")
            node = data["data"]["node"]
            return {
                "obj_token": node["obj_token"],
                "obj_type": node["obj_type"],
                "title": node.get("title", ""),
            }

    async def download_file(self, file_token: str) -> tuple[bytes, str]:
        """直接下载飞书云空间文件。返回 (file_bytes, filename)。"""
        access_token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/drive/v1/files/{file_token}/download",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                # 可能是媒体文件，尝试 medias 接口
                resp = await client.get(
                    f"{_LARK_API_BASE}/drive/v1/medias/{file_token}/download",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"下载飞书文件失败: HTTP {resp.status_code}")

            # 从 Content-Disposition 提取文件名
            filename = ""
            cd = resp.headers.get("content-disposition", "")
            if "filename" in cd:
                import re
                # 优先匹配 filename*=UTF-8''xxx
                m = re.search(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", cd)
                if m:
                    from urllib.parse import unquote
                    filename = unquote(m.group(1).strip())
                else:
                    # 匹配 filename="xxx"
                    m = re.search(r'filename="?([^";]+)"?', cd)
                    if m:
                        filename = m.group(1).strip()

            return resp.content, filename

    async def create_export_task(
        self, token: str, doc_type: str, file_extension: str = "docx"
    ) -> str:
        """创建文档导出任务，返回 ticket。"""
        access_token = await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/drive/v1/export_tasks",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "token": token,
                    "type": doc_type,
                    "file_extension": file_extension,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"创建导出任务失败: {data.get('msg')} (code={data.get('code')})")
            return data["data"]["ticket"]

    async def poll_and_download_export(self, ticket: str, doc_token: str = "", max_wait: int = 60) -> bytes:
        """轮询导出任务直到完成，然后下载文件内容。"""
        import asyncio
        access_token = await self.get_tenant_access_token()

        file_token = None
        for _ in range(max_wait // 2):
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_LARK_API_BASE}/drive/v1/export_tasks/{ticket}",
                    params={"token": doc_token} if doc_token else None,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"查询导出任务失败: {data.get('msg')}")
                result = data.get("data", {}).get("result", {})
                job_status = result.get("job_status", -1)
                if job_status == 0:  # 完成
                    file_token = result.get("file_token")
                    break
                elif job_status == 1 or job_status == 2:  # 处理中
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"导出任务失败, job_status={job_status}")

        if not file_token:
            raise RuntimeError(f"导出任务超时 (waited {max_wait}s)")

        # 下载文件
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_LARK_API_BASE}/drive/v1/export_tasks/file/{file_token}/download",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"下载导出文件失败: HTTP {resp.status_code}")
            return resp.content

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
