"""统一的飞书多维表读取器 — probe/sync/知识导入共用。"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Optional

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_LARK_API_BASE = "https://open.feishu.cn/open-apis"

# 权限相关错误码
_PERMISSION_ERROR_CODES = {99991672, 99991668, 99991671}


class BitableRecordError(Exception):
    """飞书 records API 错误，携带 code/msg/page_token 信息"""

    def __init__(
        self,
        msg: str,
        feishu_code: int | None = None,
        feishu_msg: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ):
        super().__init__(msg)
        self.feishu_code = feishu_code
        self.feishu_msg = feishu_msg
        self.page_token = page_token
        self.page_size = page_size


class BitableReader:
    """统一的飞书多维表读取器 — probe/sync/知识导入共用"""

    async def get_token(self, db=None, user=None) -> str:
        """获取 tenant_access_token"""
        from app.services.lark_client import lark_client
        return await lark_client.get_tenant_access_token()

    async def get_token_with_fallback(self, db: Session, user) -> tuple[str, str]:
        """先 tenant → 遇权限错误 fallback user token。
        返回 (token, token_type: "tenant"|"user")
        """
        try:
            token = await self.get_token()
            return token, "tenant"
        except Exception:
            pass

        from app.services.lark_doc_importer import get_valid_user_token
        user_token = await get_valid_user_token(db, user)
        if user_token:
            return user_token, "user"

        raise PermissionError("请先连接飞书账号")

    async def fetch_table_list(self, token: str, app_token: str) -> list[dict]:
        """获取多维表格下的数据表列表。返回 [{"table_id": "tblXXX", "name": "表名"}, ...]"""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_LARK_API_BASE}/bitable/v1/apps/{app_token}/tables",
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                params={"page_size": 100},
            )
            data = r.json()
            code = data.get("code", -1)
            if code != 0:
                if code in _PERMISSION_ERROR_CODES:
                    raise PermissionError(
                        _permission_error_message(code, data.get("msg", ""))
                    )
                raise RuntimeError(f"获取数据表列表失败: {data.get('msg')} (code={code})")
            items = data.get("data", {}).get("items", [])
            return [
                {"table_id": t["table_id"], "name": t.get("name", t["table_id"])}
                for t in items
            ]

    async def fetch_fields(self, token: str, app_token: str, table_id: str) -> list[dict]:
        """读取字段定义"""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_LARK_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                params={"page_size": 100},
            )
            data = r.json()
            code = data.get("code", -1)
            if code != 0:
                if code in _PERMISSION_ERROR_CODES:
                    raise PermissionError(
                        _permission_error_message(code, data.get("msg", ""))
                    )
                raise RuntimeError(f"获取字段失败: {data.get('msg')} (code={code})")
            return data["data"]["items"]

    async def fetch_records_adaptive(
        self,
        token: str,
        app_token: str,
        table_id: str,
        page_sizes: tuple[int, ...] = (500, 100, 20),
        since_ts: Optional[int] = None,
    ) -> tuple[list[dict], dict]:
        """自适应分页拉取记录。
        - 从 page_sizes[0] 开始
        - 遇飞书错误自动降到下一档
        - 所有档位都失败时抛 BitableRecordError，不返回部分数据
        - 返回 (records, stats)
        """
        page_size_idx = 0
        page_token = None
        all_records: list[dict] = []
        errors: list[dict] = []
        degraded = False

        while True:
            try:
                data = await self.fetch_records_page(
                    token, app_token, table_id,
                    page_sizes[page_size_idx], page_token, since_ts,
                )
                items = data.get("items") or []
                all_records.extend(items)
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token")
            except BitableRecordError as e:
                if page_size_idx + 1 < len(page_sizes):
                    old_size = page_sizes[page_size_idx]
                    page_size_idx += 1
                    new_size = page_sizes[page_size_idx]
                    degraded = True
                    errors.append({
                        "from": old_size,
                        "to": new_size,
                        "error": str(e),
                    })
                    logger.warning(
                        f"Bitable fetch 降级: page_size {old_size} → {new_size}, "
                        f"error={e.feishu_msg or e}"
                    )
                    continue
                else:
                    # 所有档位都失败 — 抛异常，丢弃已拉到的部分数据
                    errors.append({
                        "page_size": page_sizes[page_size_idx],
                        "error": str(e),
                        "fatal": True,
                    })
                    raise BitableRecordError(
                        f"所有分页档位均失败，已拉取 {len(all_records)} 条但数据不完整，"
                        f"最后错误: {e.feishu_msg or e}",
                        feishu_code=e.feishu_code,
                        feishu_msg=e.feishu_msg,
                        page_token=page_token,
                        page_size=page_sizes[page_size_idx],
                    )

        stats = {
            "effective_page_size": page_sizes[page_size_idx],
            "pages_fetched": len(all_records) // max(page_sizes[page_size_idx], 1) + 1,
            "degraded": degraded,
            "errors": errors,
            "total_records": len(all_records),
        }
        return all_records, stats

    async def fetch_records_page(
        self,
        token: str,
        app_token: str,
        table_id: str,
        page_size: int,
        page_token: str | None = None,
        since_ts: int | None = None,
    ) -> dict:
        """单页拉取，返回飞书 data 字典，出错抛 BitableRecordError"""
        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
        body: dict = {"page_size": page_size}
        if page_token:
            body["page_token"] = page_token
        if since_ts:
            body["filter"] = {
                "conjunction": "and",
                "conditions": [{
                    "field_name": "最后更新时间",
                    "operator": "isGreater",
                    "value": [str(since_ts * 1000)],
                }],
            }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_LARK_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                headers=headers,
                json=body,
            )
            data = r.json()
            code = data.get("code", -1)
            if code != 0:
                feishu_msg = data.get("msg", "")
                if code in _PERMISSION_ERROR_CODES:
                    raise PermissionError(
                        _permission_error_message(code, feishu_msg)
                    )
                raise BitableRecordError(
                    f"获取记录失败: {feishu_msg} (code={code})",
                    feishu_code=code,
                    feishu_msg=feishu_msg,
                    page_token=page_token,
                    page_size=page_size,
                )
            return data.get("data", {})

    @staticmethod
    def flatten_value(v):
        """将飞书多维表格单元格展平为可存储值。"""
        if v is None:
            return None
        if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
            return "".join(item.get("text", "") for item in v)
        if isinstance(v, dict) and "text" in v:
            return v["text"]
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        return v

    @staticmethod
    def sanitize_col(field_name: str) -> str:
        """字段名清洗为合法列名，空名/纯符号名返回兜底列名"""
        cleaned = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", field_name).strip("_")
        return cleaned if cleaned else f"_unnamed_{abs(hash(field_name)) % 10000}"

    def records_to_html_table(self, fields: list[dict], records: list[dict]) -> str:
        """将字段+记录渲染为 HTML 表格（知识库 fallback 用）"""
        field_names = [f["field_name"] for f in fields]
        rows = []
        # 表头
        header_cells = "".join(f"<th>{fn}</th>" for fn in field_names)
        rows.append(f"<tr>{header_cells}</tr>")
        # 数据行
        for rec in records:
            flds = rec.get("fields", {})
            cells = []
            for fn in field_names:
                val = self.flatten_value(flds.get(fn))
                cell_text = str(val) if val is not None else ""
                cells.append(f"<td>{cell_text}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows)}</table>"

    def records_to_text(self, fields: list[dict], records: list[dict]) -> str:
        """将字段+记录渲染为纯文本摘要"""
        field_names = [f["field_name"] for f in fields]
        lines = []
        lines.append(" | ".join(field_names))
        lines.append("-" * 40)
        for rec in records:
            flds = rec.get("fields", {})
            vals = []
            for fn in field_names:
                val = self.flatten_value(flds.get(fn))
                vals.append(str(val) if val is not None else "")
            lines.append(" | ".join(vals))
        return "\n".join(lines)


def _permission_error_message(code: int, msg: str) -> str:
    """根据飞书错误码返回可行动的错误提示"""
    if code == 99991671:
        return "请在飞书多维表格中添加本系统的应用"
    if code == 99991668:
        return "请先连接飞书账号"
    if code == 99991672:
        return "请联系管理员为应用开通 bitable:app:readonly 权限"
    return f"飞书权限错误: {msg} (code={code})"


bitable_reader = BitableReader()
