from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conversation import Conversation, Message, MessageRole
from app.models.user import User
from app.services.skill_engine import skill_engine

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class SendMessage(BaseModel):
    content: str


class ConversationCreate(BaseModel):
    workspace_id: Optional[int] = None


@router.post("")
def create_conversation(
    req: ConversationCreate = ConversationCreate(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = Conversation(user_id=user.id, workspace_id=req.workspace_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"id": conv.id, "title": conv.title, "workspace_id": conv.workspace_id}


@router.get("")
def list_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    convs = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id, Conversation.is_active == True)
        .order_by(Conversation.updated_at.desc())
        .limit(50)
        .all()
    )
    def _conv_dict(c: Conversation) -> dict:
        from app.models.workspace import Workspace
        ws = db.get(Workspace, c.workspace_id) if c.workspace_id else None
        return {
            "id": c.id,
            "title": c.title,
            "skill_id": c.skill_id,
            "workspace_id": c.workspace_id,
            "workspace": {"name": ws.name, "icon": ws.icon, "color": ws.color} if ws else None,
            "updated_at": c.updated_at.isoformat(),
        }

    return [_conv_dict(c) for c in convs]


@router.get("/{conv_id}/messages")
def get_messages(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    return [
        {
            "id": m.id,
            "role": m.role.value,
            "content": m.content,
            "metadata": m.metadata_,
            "created_at": m.created_at.isoformat(),
        }
        for m in conv.messages
    ]


@router.post("/{conv_id}/messages")
async def send_message(
    conv_id: int,
    req: SendMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # Persist user message
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=req.content,
    )
    db.add(user_msg)
    db.flush()

    # Execute skill engine
    try:
        response = await skill_engine.execute(db, conv, req.content, user_id=user.id)
    except ValueError as e:
        raise HTTPException(503, str(e))

    # Persist assistant response
    assistant_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        content=response,
        metadata_={"skill_id": conv.skill_id},
    )
    db.add(assistant_msg)

    # Update conversation title on first exchange
    msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
    if msg_count <= 2:
        conv.title = req.content[:60]

    db.commit()
    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": response,
        "skill_id": conv.skill_id,
    }


@router.delete("/{conv_id}")
def delete_conversation(
    conv_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    conv.is_active = False
    db.commit()
    return {"ok": True}
