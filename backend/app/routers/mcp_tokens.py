"""MCP Token management: create/list/delete personal API tokens for MCP Server access."""
import datetime
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.models.mcp import McpToken, McpTokenScope
from app.models.user import User, Role

router = APIRouter(prefix="/api/mcp-tokens", tags=["mcp-tokens"])


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TokenCreate(BaseModel):
    scope: str = "user"
    workspace_id: Optional[int] = None
    expires_days: Optional[int] = None


@router.post("")
def create_token(
    req: TokenCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if req.scope == "admin" and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "Only super_admin can create admin-scoped tokens")

    raw = secrets.token_urlsafe(32)
    prefix = f"ukb_{raw[:8]}"

    expires_at = None
    if req.expires_days:
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=req.expires_days)

    token = McpToken(
        user_id=user.id,
        workspace_id=req.workspace_id,
        token_hash=_hash_token(raw),
        token_prefix=prefix,
        scope=req.scope,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    return {
        "id": token.id,
        "token": raw,
        "prefix": prefix,
        "scope": req.scope,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.get("")
def list_tokens(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tokens = db.query(McpToken).filter(McpToken.user_id == user.id).all()
    return [
        {
            "id": t.id,
            "prefix": t.token_prefix,
            "scope": t.scope.value,
            "workspace_id": t.workspace_id,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "created_at": t.created_at.isoformat(),
        }
        for t in tokens
    ]


@router.delete("/{token_id}")
def delete_token(
    token_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    token = db.get(McpToken, token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(404, "Token not found")
    db.delete(token)
    db.commit()
    return {"ok": True}
