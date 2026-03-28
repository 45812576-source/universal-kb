"""Feishu (Lark) webhook router + 审批查询 API."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.lark_bot import lark_bot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lark", tags=["lark"])


@router.get("/event")
async def lark_verify(request: Request):
    """Lark URL verification (GET method for some older integrations)."""
    challenge = request.query_params.get("challenge", "")
    if challenge:
        return {"challenge": challenge}
    return {"status": "ok"}


@router.post("/event")
async def lark_event(request: Request, db: Session = Depends(get_db)):
    """Receive Lark event callbacks."""
    from app.config import settings

    body = await request.body()

    # Parse event data
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Handle encrypted events
    encrypt_key = getattr(settings, "LARK_ENCRYPT_KEY", "")
    if encrypt_key and "encrypt" in raw:
        try:
            from app.services.lark_bot import decrypt_lark_event
            raw = decrypt_lark_event(encrypt_key, raw["encrypt"])
        except Exception as e:
            logger.error(f"Lark event decryption failed: {e}")
            raise HTTPException(status_code=400, detail="Decryption failed")

    # Handle URL verification challenge
    if raw.get("type") == "url_verification":
        return {"challenge": raw.get("challenge", "")}

    # Verify token (v1 style)
    verification_token = getattr(settings, "LARK_VERIFICATION_TOKEN", "")
    if verification_token:
        event_token = raw.get("token", "") or raw.get("header", {}).get("token", "")
        if event_token and event_token != verification_token:
            raise HTTPException(status_code=401, detail="Invalid verification token")

    # Process event asynchronously
    try:
        result = await lark_bot.handle_event(db, raw)
        return result
    except Exception as e:
        logger.error(f"Lark event handling error: {e}")
        return {"ok": True}  # Always return 200 to Lark


# ── 审批查询 API ────────────────────────────────────────────────────────────


@router.get("/approval-definitions")
async def list_approval_definitions(user: User = Depends(get_current_user)):
    """获取企业可用的飞书审批模板列表。"""
    from app.services.lark_client import lark_client
    try:
        definitions = await lark_client.list_approval_definitions()
        return definitions
    except Exception as e:
        raise HTTPException(400, f"获取审批定义列表失败: {e}")


@router.get("/approval-instances")
def list_approval_instances(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看当前用户的审批记录。"""
    from app.models.lark_approval import LarkApprovalInstance
    q = db.query(LarkApprovalInstance).filter(LarkApprovalInstance.user_id == user.id)
    if status:
        q = q.filter(LarkApprovalInstance.status == status)
    rows = q.order_by(LarkApprovalInstance.created_at.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "instance_code": r.instance_code,
            "approval_code": r.approval_code,
            "title": r.title,
            "status": r.status,
            "form_data": r.form_data,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.get("/approval-instances/{instance_code}")
async def get_approval_instance(
    instance_code: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询单个审批实例详情（先查本地，再查飞书最新状态）。"""
    from app.models.lark_approval import LarkApprovalInstance
    from app.services.lark_client import lark_client

    record = (
        db.query(LarkApprovalInstance)
        .filter(
            LarkApprovalInstance.instance_code == instance_code,
            LarkApprovalInstance.user_id == user.id,
        )
        .first()
    )
    if not record:
        raise HTTPException(404, "审批记录不存在")

    # 同步飞书最新状态
    try:
        remote = await lark_client.get_approval_instance(instance_code)
        remote_status = remote.get("status", record.status)
        if remote_status != record.status:
            record.status = remote_status
            record.result_data = remote
            db.commit()
    except Exception as e:
        logger.warning(f"Failed to sync approval status from Lark: {e}")

    return {
        "id": record.id,
        "instance_code": record.instance_code,
        "approval_code": record.approval_code,
        "title": record.title,
        "status": record.status,
        "form_data": record.form_data,
        "result_data": record.result_data,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }
