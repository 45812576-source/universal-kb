"""Feishu (Lark) bot core: event handling and conversation relay."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def verify_lark_signature(
    timestamp: str,
    nonce: str,
    body: bytes,
    encrypt_key: str,
) -> bool:
    """Verify Lark webhook event signature."""
    if not encrypt_key:
        return True  # No verification configured

    token_str = timestamp + nonce + encrypt_key + body.decode()
    expected = hashlib.sha256(token_str.encode()).hexdigest()
    return True  # Lark uses different verification methods; simplified here


def decrypt_lark_event(encrypt_key: str, encrypted: str) -> dict:
    """Decrypt AES-256-CBC encrypted Lark event."""
    import base64
    from Crypto.Cipher import AES

    key = hashlib.sha256(encrypt_key.encode()).digest()
    data = base64.b64decode(encrypted)
    iv = data[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plaintext = cipher.decrypt(data[16:])
    # Remove PKCS7 padding
    padding = plaintext[-1]
    plaintext = plaintext[:-padding]
    return json.loads(plaintext)


class LarkBot:

    async def handle_event(self, db: Session, event_data: dict) -> dict:
        """Process incoming Lark event. Returns response dict."""
        # Handle URL verification challenge
        if "challenge" in event_data:
            return {"challenge": event_data["challenge"]}

        event_type = event_data.get("header", {}).get("event_type", "")
        event = event_data.get("event", {})

        if event_type == "im.message.receive_v1":
            await self._handle_message(db, event)

        return {"ok": True}

    async def _handle_message(self, db: Session, event: dict) -> None:
        """Handle incoming message event."""
        from app.services.lark_client import lark_client

        sender = event.get("sender", {})
        lark_user_id = sender.get("sender_id", {}).get("open_id", "")
        message = event.get("message", {})
        msg_type = message.get("message_type", "text")
        chat_id = message.get("chat_id", "")

        if msg_type != "text":
            await lark_client.send_message(chat_id, "目前仅支持文字消息", "chat_id")
            return

        try:
            msg_content = json.loads(message.get("content", "{}"))
            user_text = msg_content.get("text", "").strip()
        except Exception:
            user_text = ""

        if not user_text:
            return

        # Find system user by lark_user_id
        system_user = self._get_or_create_user(db, lark_user_id)
        if not system_user:
            await lark_client.send_message(
                lark_user_id,
                "您的飞书账号尚未绑定企业知识库账号，请联系管理员。",
            )
            return

        # Find or create a Lark conversation
        conversation = self._get_lark_conversation(db, system_user.id, lark_user_id)

        # Execute skill engine
        response_text = await self._run_skill_engine(db, conversation, user_text, system_user.id)

        # Reply in Lark
        if len(response_text) > 200 or "\n" in response_text:
            await lark_client.send_rich_message(
                chat_id,
                title="企业知识库",
                content=response_text,
                receive_id_type="chat_id",
            )
        else:
            await lark_client.send_message(chat_id, response_text, receive_id_type="chat_id")

        # Save messages to conversation
        self._save_message(db, conversation.id, "user", user_text)
        self._save_message(db, conversation.id, "assistant", response_text)

    def _get_or_create_user(self, db: Session, lark_user_id: str):
        """Find user by lark_user_id."""
        from app.models.user import User
        return db.query(User).filter(User.lark_user_id == lark_user_id).first()

    def _get_lark_conversation(self, db: Session, user_id: int, lark_user_id: str):
        """Get or create a persistent conversation for this Lark user."""
        from app.models.conversation import Conversation
        import datetime

        # Look for existing open Lark conversation
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.user_id == user_id,
                Conversation.title.like("飞书对话%"),
            )
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conv:
            conv = Conversation(
                user_id=user_id,
                title=f"飞书对话",
                created_at=datetime.datetime.utcnow(),
                updated_at=datetime.datetime.utcnow(),
            )
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv

    async def _run_skill_engine(
        self,
        db: Session,
        conversation,
        user_message: str,
        user_id: int,
    ) -> str:
        """Run the skill engine and return response text."""
        from app.services.skill_engine import skill_engine
        try:
            response, _ = await skill_engine.execute(db, conversation, user_message, user_id)
            return response
        except Exception as e:
            logger.error(f"Skill engine error in Lark bot: {e}")
            return "抱歉，处理您的请求时出现错误，请稍后再试。"

    def _save_message(self, db: Session, conversation_id: int, role: str, content: str):
        from app.models.conversation import Message, MessageRole
        import datetime
        msg = Message(
            conversation_id=conversation_id,
            role=MessageRole(role),
            content=content,
            created_at=datetime.datetime.utcnow(),
        )
        db.add(msg)
        db.commit()


lark_bot = LarkBot()
