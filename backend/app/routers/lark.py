"""Feishu (Lark) webhook router."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
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
