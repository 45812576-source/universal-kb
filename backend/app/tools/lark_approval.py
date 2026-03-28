"""飞书审批工具 — AI 对话中发起飞书审批流程。

Input params:
{
  "approval_code": "审批定义 code（飞书后台模板）",
  "title": "审批标题",
  "form_data": [{"id": "widget1", "type": "input", "value": "3天"}],
  "urgency": "normal"  // 可选: "normal" | "urgent"
}

Output: {"instance_code": "...", "status": "PENDING", "message": "审批已发起"}
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def execute(params: dict, db=None, user_id: int | None = None) -> Any:
    if db is None or user_id is None:
        raise ValueError("lark_approval 需要 db 和 user_id 上下文")

    from app.models.user import User
    from app.models.lark_approval import LarkApprovalInstance
    from app.services.lark_client import lark_client

    # 1. 获取用户飞书 ID
    user = db.get(User, user_id)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    if not user.lark_user_id:
        return {
            "ok": False,
            "error": "您的账号尚未绑定飞书，请联系管理员在用户管理中绑定飞书 ID",
        }

    approval_code = params.get("approval_code", "").strip()
    title = params.get("title", "").strip()
    form_data = params.get("form_data", [])
    urgency = params.get("urgency", "normal")

    if not approval_code:
        return {"ok": False, "error": "缺少审批定义 code（approval_code）"}
    if not title:
        return {"ok": False, "error": "缺少审批标题（title）"}

    # 2. 调飞书 API 创建审批实例
    try:
        result = await lark_client.create_approval_instance(
            approval_code=approval_code,
            open_id=user.lark_user_id,
            form=form_data,
            urgency=urgency,
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Lark approval creation failed: {e}")
        return {"ok": False, "error": f"飞书审批发起失败: {e}"}

    instance_code = result["instance_code"]

    # 3. 本地记录
    record = LarkApprovalInstance(
        instance_code=instance_code,
        approval_code=approval_code,
        title=title,
        status="PENDING",
        form_data=form_data,
        user_id=user_id,
    )
    db.add(record)
    db.commit()

    return {
        "ok": True,
        "instance_code": instance_code,
        "status": "PENDING",
        "message": f"审批「{title}」已发起，等待审批人在飞书处理",
    }
