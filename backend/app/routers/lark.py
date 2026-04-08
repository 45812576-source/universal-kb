"""Feishu (Lark) webhook router + 审批查询 API + OAuth 授权。"""
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.lark_bot import lark_bot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lark", tags=["lark"])

# 简易 state → user_id 映射，防 CSRF（内存缓存，生产环境建议用 Redis）
_oauth_state_map: dict[str, tuple[int, float]] = {}


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


# ── OAuth 授权 ─────────────────────────────────────────────────────────────


@router.get("/oauth/authorize")
async def lark_oauth_authorize(user: User = Depends(get_current_user)):
    """返回飞书 OAuth 授权页面 URL。"""
    import time
    from app.services.lark_client import lark_client

    state = secrets.token_urlsafe(32)
    _oauth_state_map[state] = (user.id, time.time())

    # 清理过期 state（>10min）
    now = time.time()
    expired = [k for k, (_, ts) in _oauth_state_map.items() if now - ts > 600]
    for k in expired:
        _oauth_state_map.pop(k, None)

    try:
        authorize_url = lark_client.get_oauth_url(state)
    except Exception as e:
        raise HTTPException(400, str(e))

    return {"authorize_url": authorize_url}


@router.get("/oauth/callback")
async def lark_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """飞书 OAuth 回调：用 code 换 token 并存入用户记录，重定向回前端。"""
    import time
    from app.services.lark_client import lark_client

    # 验证 state
    entry = _oauth_state_map.pop(state, None)
    if not entry:
        raise HTTPException(400, "无效的 state 参数，请重新发起授权")
    user_id, created_at = entry
    if time.time() - created_at > 600:
        raise HTTPException(400, "授权已过期，请重新发起")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")

    # 换取 token
    try:
        token_data = await lark_client.exchange_code_for_token(code)
    except Exception as e:
        logger.error(f"飞书 OAuth 换取 token 失败: {e}")
        raise HTTPException(400, f"飞书授权失败: {e}")

    # 存储到用户记录
    user.lark_access_token = token_data.get("access_token", "")
    user.lark_refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 7200)
    user.lark_token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    db.commit()

    logger.info(f"用户 {user.id} 飞书 OAuth 授权成功")

    # 重定向回前端知识库页面
    return RedirectResponse(url="/knowledge/my?lark_auth=success")


@router.get("/oauth/status")
async def lark_oauth_status(user: User = Depends(get_current_user)):
    """查询当前用户飞书 OAuth 授权状态。"""
    has_token = bool(user.lark_access_token)
    expired = False
    if has_token and user.lark_token_expires_at:
        expired = datetime.utcnow() > user.lark_token_expires_at
    return {
        "connected": has_token,
        "expired": expired,
        "expires_at": user.lark_token_expires_at.isoformat() if user.lark_token_expires_at else None,
    }


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
