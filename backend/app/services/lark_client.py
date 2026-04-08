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


class LarkPermissionError(Exception):
    """飞书 API 权限不足（文档无访问权限等）"""


class LarkClient:

    def __init__(self):
        self._app_id: str = ""
        self._app_secret: str = ""

    def _load_config(self):
        from app.config import settings
        self._app_id = getattr(settings, "LARK_APP_ID", "")
        self._app_secret = getattr(settings, "LARK_APP_SECRET", "")

    async def _get_token(self, access_token: str | None = None) -> str:
        """获取有效 token：优先使用传入的 user_access_token，否则走 tenant_access_token。"""
        return access_token or await self.get_tenant_access_token()

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

    # ── OAuth 方法 ─────────────────────────────────────────────────────

    def get_oauth_url(self, state: str) -> str:
        """生成飞书 OAuth 授权页面 URL。"""
        self._load_config()
        from app.config import settings
        redirect_uri = getattr(settings, "LARK_OAUTH_REDIRECT_URI", "")
        if not self._app_id:
            raise LarkConfigError("飞书集成尚未配置，请联系管理员")
        from urllib.parse import quote
        return (
            f"https://open.feishu.cn/open-apis/authen/v1/authorize"
            f"?app_id={self._app_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&state={state}"
        )

    async def exchange_code_for_token(self, code: str) -> dict:
        """用授权 code 换取 user_access_token + refresh_token。

        Returns: {"access_token": ..., "refresh_token": ..., "expires_in": ...,
                  "open_id": ..., "union_id": ..., "name": ...}
        """
        self._load_config()
        if not self._app_id or not self._app_secret:
            raise LarkConfigError("飞书集成尚未配置，请联系管理员")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/authen/v2/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "app_id": self._app_id,
                    "app_secret": self._app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise LarkAuthError(f"飞书 OAuth 换取 token 失败: {data.get('msg')}")
            return data.get("data", {})

    async def refresh_user_token(self, refresh_token: str) -> dict:
        """刷新 user_access_token。

        Returns: {"access_token": ..., "refresh_token": ..., "expires_in": ...}
        """
        self._load_config()
        if not self._app_id or not self._app_secret:
            raise LarkConfigError("飞书集成尚未配置，请联系管理员")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/authen/v2/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "app_id": self._app_id,
                    "app_secret": self._app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise LarkAuthError(f"飞书 OAuth 刷新 token 失败: {data.get('msg')}")
            return data.get("data", {})

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

    async def get_wiki_node(self, token: str, access_token: str | None = None) -> dict:
        """解析 wiki token 为实际文档 obj_token + obj_type。"""
        access_token = await self._get_token(access_token)
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

    async def get_doc_meta(self, doc_token: str, doc_type: str, access_token: str | None = None) -> dict:
        """获取飞书文档元数据（标题、创建者、修改时间等）。

        通过 POST /drive/v1/metas/batch_query 批量查询接口。
        Returns: {"title": "...", "create_time": ..., "latest_modify_time": ...,
                  "owner_id": "...", "url": "..."}
        """
        access_token = await self._get_token(access_token)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_LARK_API_BASE}/drive/v1/metas/batch_query",
                params={"user_id_type": "open_id"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "request_docs": [
                        {"doc_token": doc_token, "doc_type": doc_type}
                    ],
                    "with_url": True,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning(f"get_doc_meta failed: {data.get('msg')}")
                return {}
            metas = data.get("data", {}).get("metas", [])
            if not metas:
                return {}
            meta = metas[0]
            return {
                "title": meta.get("title", ""),
                "create_time": meta.get("create_time", ""),
                "latest_modify_time": meta.get("latest_modify_time", ""),
                "owner_id": meta.get("owner_id", ""),
                "url": meta.get("url", ""),
                "type": meta.get("type", ""),
            }

    async def download_file(self, file_token: str, access_token: str | None = None) -> tuple[bytes, str]:
        """直接下载飞书云空间文件。返回 (file_bytes, filename)。"""
        access_token = await self._get_token(access_token)
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
        self, token: str, doc_type: str, file_extension: str = "docx",
        access_token: str | None = None,
    ) -> str:
        """创建文档导出任务，返回 ticket。"""
        access_token = await self._get_token(access_token)
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
                msg = data.get("msg", "")
                code = data.get("code", 0)
                if code in (99991672, 99991668) or "permission" in msg.lower() or "denied" in msg.lower():
                    raise LarkPermissionError(f"文档权限不足: {msg} (code={code})")
                raise RuntimeError(f"创建导出任务失败: {msg} (code={code})")
            return data["data"]["ticket"]

    async def poll_and_download_export(
        self, ticket: str, doc_token: str = "", max_wait: int = 60,
        access_token: str | None = None,
    ) -> bytes:
        """轮询导出任务直到完成，然后下载文件内容。"""
        import asyncio
        access_token = await self._get_token(access_token)

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
