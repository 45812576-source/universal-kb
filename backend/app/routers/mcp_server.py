"""MCP Server: expose company Skills as MCP tools to authorized external clients."""
import datetime
import hashlib
import logging

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, Any

from app.database import get_db
from app.models.mcp import McpToken, McpTokenScope
from app.models.skill import Skill, SkillStatus
from app.models.workspace import WorkspaceSkill

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-server"])


class McpRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Any = 1
    method: str
    params: dict = {}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_mcp_token(authorization: Optional[str], db: Session) -> McpToken:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    raw = authorization[7:]
    token_hash = _hash_token(raw)
    token = db.query(McpToken).filter(McpToken.token_hash == token_hash).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid token")
    if token.expires_at and token.expires_at < datetime.datetime.utcnow():
        raise HTTPException(status_code=401, detail="Token expired")
    token.last_used_at = datetime.datetime.utcnow()
    db.commit()
    return token


def _get_accessible_skills(token: McpToken, db: Session) -> list[Skill]:
    if token.scope == McpTokenScope.ADMIN:
        return db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()

    if token.scope == McpTokenScope.WORKSPACE and token.workspace_id:
        ws_skill_ids = [
            ws.skill_id for ws in
            db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == token.workspace_id).all()
        ]
        return db.query(Skill).filter(
            Skill.id.in_(ws_skill_ids),
            Skill.status == SkillStatus.PUBLISHED,
        ).all()

    return db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()


@router.post("/mcp")
async def mcp_endpoint(
    req: McpRequest,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    token = _get_mcp_token(authorization, db)
    skills = _get_accessible_skills(token, db)

    if req.method == "tools/list":
        tools = [
            {
                "name": s.name,
                "description": s.description or "",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "用户消息"},
                    },
                    "required": ["message"],
                },
            }
            for s in skills
        ]
        return {"jsonrpc": "2.0", "id": req.id, "result": {"tools": tools}}

    if req.method == "tools/call":
        tool_name = req.params.get("name", "")
        args = req.params.get("arguments", {})
        user_message = args.get("message", "")

        skill = next((s for s in skills if s.name == tool_name), None)
        if not skill:
            return {
                "jsonrpc": "2.0", "id": req.id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"},
            }

        try:
            from app.models.conversation import Conversation
            from app.services.skill_engine import skill_engine

            conv = Conversation(user_id=token.user_id, title="MCP Call", skill_id=skill.id)
            db.add(conv)
            db.flush()

            response = await skill_engine.execute(db, conv, user_message, user_id=token.user_id)
            db.rollback()

            return {
                "jsonrpc": "2.0", "id": req.id,
                "result": {"content": [{"type": "text", "text": response}]},
            }
        except Exception as e:
            logger.error(f"MCP tool call error: {e}")
            return {
                "jsonrpc": "2.0", "id": req.id,
                "error": {"code": -32603, "message": str(e)},
            }

    return {
        "jsonrpc": "2.0", "id": req.id,
        "error": {"code": -32601, "message": f"Method '{req.method}' not supported"},
    }
