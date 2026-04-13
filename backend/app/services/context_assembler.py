"""ContextAssembler — 消息历史组装、上下文压缩，从 skill_engine 抽出。"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageRole
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)


class ContextAssembler:
    """消息历史组装与上下文压缩。"""

    def load_messages(self, db: Session, conversation: Conversation, limit: int = 100) -> list[Message]:
        """H3: 分页加载，最多取最近 limit 条消息，正序返回。"""
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
            .all()
        )[::-1]
        return messages

    def build_llm_messages(
        self,
        messages: list[Message],
        system_content: str,
        user_message: str,
    ) -> list[dict]:
        """从历史消息构建 LLM 消息列表。

        跳过空 assistant 消息和孤立 user 消息，避免 API 错误。
        """
        raw_pairs: list[tuple] = []
        pending_user = None
        for m in messages:
            if m.role == MessageRole.USER:
                if pending_user is not None:
                    raw_pairs.append((pending_user, None))
                pending_user = m
            elif m.role == MessageRole.ASSISTANT:
                asst_content = (m.content or "").strip()
                if pending_user is not None and asst_content:
                    raw_pairs.append((pending_user, m))
                    pending_user = None

        llm_messages = [{"role": "system", "content": system_content}]
        for user_m, asst_m in raw_pairs:
            if asst_m is None:
                continue
            _ac = (asst_m.content or "").strip()
            if not _ac:
                continue
            llm_messages.append({"role": "user", "content": user_m.content or "(empty)"})
            llm_messages.append({"role": "assistant", "content": _ac})
        llm_messages.append({"role": "user", "content": user_message})
        return llm_messages

    async def compact_if_needed(
        self,
        db: Session,
        llm_messages: list[dict],
        model_config: dict,
        threshold: float = 0.85,
        summarize_fn=None,
    ) -> list[dict]:
        """If estimated token usage exceeds threshold * context_window, summarize early history."""
        total_text = "".join(m.get("content") or "" for m in llm_messages)
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model("gpt-4")
            estimated_tokens = len(enc.encode(total_text))
        except Exception:
            estimated_tokens = len(total_text) // 2

        context_window = model_config.get("context_window", 32000)
        if estimated_tokens <= context_window * threshold:
            return llm_messages

        system_msgs = [m for m in llm_messages if m["role"] == "system"]
        non_system = [m for m in llm_messages if m["role"] != "system"]

        keep_recent = 6
        early = non_system[:-keep_recent] if len(non_system) > keep_recent else []
        recent = non_system[-keep_recent:]

        if not early:
            return llm_messages

        try:
            summarizer = summarize_fn or self._summarize_history
            summary = await summarizer(db, early, model_config)
            compacted = [
                *system_msgs,
                {"role": "user", "content": f"[系统消息：前期对话摘要，非用户发言]\n{summary}"},
                *recent,
            ]
            new_chars = sum(len(m.get("content") or "") for m in compacted)
            logger.info(
                f"Context compacted: {len(total_text)} → {new_chars} chars "
                f"(~{estimated_tokens} → {new_chars // 2} tokens)"
            )
            return compacted
        except Exception as e:
            logger.warning(f"Context compaction failed, using original: {e}")
            return llm_messages

    async def _summarize_history(self, db: Session, messages: list[dict], model_config: dict) -> str:
        """Summarize a list of messages into a concise paragraph."""
        history_text = "\n".join(
            f"{m['role']}: {m.get('content', '')[:500]}" for m in messages
        )
        prompt = (
            "请用简洁的中文（200字以内）总结以下对话的主要内容和结论。\n"
            "要求：\n"
            "1. 必须保留用户的原始请求/目标（第一条 user 消息的核心意图）\n"
            "2. 保留关键信息、重要结果和已完成的步骤\n"
            "3. 如有工具调用，保留工具名称和关键结果\n\n"
            f"{history_text}"
        )
        try:
            lite_config = llm_gateway.resolve_config(db, "skill.compress_history")
        except Exception:
            lite_config = model_config
        result, _ = await asyncio.wait_for(
            llm_gateway.chat(
                model_config=lite_config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=300,
            ),
            timeout=30,
        )
        return result.strip()


context_assembler = ContextAssembler()
