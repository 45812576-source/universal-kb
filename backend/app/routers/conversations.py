import asyncio
import json
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.conversation import Conversation, Message, MessageRole
from app.models.knowledge import KnowledgeEntry
from app.models.user import User
from app.services.skill_engine import skill_engine
from app.services.llm_gateway import llm_gateway
from app.utils.file_parser import extract_text

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _classify_error(e: Exception) -> str:
    """Classify exception into error type for frontend."""
    msg = str(e).lower()
    if "rate" in msg or "429" in msg or "quota" in msg:
        return "rate_limit"
    if "context" in msg or "token" in msg or "length" in msg or "too long" in msg:
        return "context_overflow"
    if "connect" in msg or "timeout" in msg or "network" in msg:
        return "network"
    return "server_error"


class SendMessage(BaseModel):
    content: str
    active_skill_ids: list[int] | None = None


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
        result = await skill_engine.execute(db, conv, req.content, user_id=user.id, active_skill_ids=req.active_skill_ids)
    except ValueError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(503, f"AI 服务暂时不可用，请稍后重试")

    # skill_engine always returns (content, meta_dict)
    response, guide_meta = result

    # Resolve skill name
    skill_name = None
    if conv.skill_id:
        from app.models.skill import Skill as SkillModel
        sk = db.get(SkillModel, conv.skill_id)
        skill_name = sk.name if sk else None

    # Extract token usage from guide_meta
    llm_usage = guide_meta.pop("llm_usage", {})
    msg_metadata = {
        "skill_id": conv.skill_id,
        "skill_name": skill_name,
        "model_id": llm_usage.get("model_id"),
        "input_tokens": llm_usage.get("input_tokens", 0),
        "output_tokens": llm_usage.get("output_tokens", 0),
        **guide_meta,
    }

    # Persist assistant response
    assistant_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        content=response,
        metadata_=msg_metadata,
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
        "skill_name": skill_name,
        "metadata": msg_metadata,
    }


@router.post("/{conv_id}/messages/stream")
async def stream_message(
    conv_id: int,
    req: SendMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming endpoint: same logic as send_message but streams LLM output."""
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

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def event_generator():
        try:
            # --- Prepare phase (skill matching, knowledge, prompt) ---
            yield _sse("status", {"stage": "preparing"})

            prep = await skill_engine.prepare(
                db, conv, req.content,
                user_id=user.id,
                active_skill_ids=req.active_skill_ids,
            )

            # Resolve skill name for metadata
            skill_name = prep.skill_name

            # Early return: prepare produced a non-streaming result
            if prep.early_return is not None:
                response_text, early_meta = prep.early_return
                llm_usage = early_meta.pop("llm_usage", {})
                msg_metadata = {
                    "skill_id": conv.skill_id,
                    "skill_name": skill_name,
                    "model_id": llm_usage.get("model_id"),
                    "input_tokens": llm_usage.get("input_tokens", 0),
                    "output_tokens": llm_usage.get("output_tokens", 0),
                    **early_meta,
                }
                assistant_msg = Message(
                    conversation_id=conv_id,
                    role=MessageRole.ASSISTANT,
                    content=response_text,
                    metadata_=msg_metadata,
                )
                db.add(assistant_msg)
                msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
                if msg_count <= 2:
                    conv.title = req.content[:60]
                db.commit()

                yield _sse("delta", {"text": response_text})
                yield _sse("done", {
                    "message_id": assistant_msg.id,
                    "metadata": msg_metadata,
                })
                return

            # --- Streaming LLM call ---
            yield _sse("status", {"stage": "generating", "skill_name": skill_name})

            full_content = ""
            block_idx = 0
            current_block_type = None  # "text" | "thinking" | None
            native_tool_calls: list[dict] = []

            async for chunk_type, chunk_data in llm_gateway.chat_stream_typed(
                model_config=prep.model_config,
                messages=prep.llm_messages,
                tools=prep.tools_schema or None,
            ):
                if chunk_type == "thinking":
                    if current_block_type != "thinking":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "thinking"})
                        current_block_type = "thinking"
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                elif chunk_type == "tool_call":
                    # 原生 function calling：收集，等第一轮 LLM 结束后统一处理
                    native_tool_calls.append(chunk_data)
                else:  # content
                    if current_block_type != "text":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "text"})
                        current_block_type = "text"
                    full_content += chunk_data
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                    yield _sse("delta", {"text": chunk_data})  # backward compat

            # Close last block
            if current_block_type is not None:
                yield _sse("content_block_stop", {"index": block_idx})

            # --- Post-processing (tool calls, structured output) ---
            response = full_content
            tool_meta: dict = {}

            # Structured output
            skill_version = prep.skill_version
            structured_output = None
            if skill_version and skill_version.output_schema:
                from app.services.skill_engine import SkillEngine
                parsed = SkillEngine._try_parse_structured_output(response)
                if parsed is not None:
                    structured_output = parsed
                    from app.services import prompt_compiler
                    response = prompt_compiler.render_structured_as_markdown(
                        skill_version.output_schema, parsed
                    )
                    yield _sse("replace", {"text": response})

            # Agent Loop: 原生 function calling 或文本 fallback
            if native_tool_calls or "```tool_call" in response:
                from app.models.skill import Skill as SkillModel
                skill_obj = db.get(SkillModel, prep.skill_id) if prep.skill_id else None
                yield _sse("status", {"stage": "tool_calling"})
                async for item in skill_engine._handle_tool_calls_stream(
                    db, skill_obj, response, prep.llm_messages, prep.model_config, user.id,
                    tools_schema=prep.tools_schema or None,
                    native_tool_calls=native_tool_calls or None,
                ):
                    if isinstance(item, tuple):
                        response, tool_meta = item
                    else:
                        yield _sse(item["event"], item["data"])
                yield _sse("replace", {"text": response})

            # PPT auto-execution
            if prep.skill_name == "pptx-generation" and "```python" in response:
                tool_meta = skill_engine._execute_pptx_code(response)
            if prep.skill_name == "pptx-generation" and not tool_meta and "```html" in response:
                tool_meta = skill_engine._execute_html_ppt(response)

            if structured_output is not None:
                tool_meta["structured_output"] = structured_output

            # Build metadata
            msg_metadata = {
                "skill_id": conv.skill_id,
                "skill_name": skill_name,
                **tool_meta,
            }

            # Persist assistant message
            assistant_msg = Message(
                conversation_id=conv_id,
                role=MessageRole.ASSISTANT,
                content=response,
                metadata_=msg_metadata,
            )
            db.add(assistant_msg)

            msg_count = db.query(Message).filter(Message.conversation_id == conv_id).count()
            if msg_count <= 2:
                conv.title = req.content[:60]

            db.commit()

            # Estimate token usage for context warning
            total_input_chars = sum(len(m.get("content", "")) for m in prep.llm_messages)
            estimated_input_tokens = total_input_chars // 2  # rough char-to-token ratio for CJK
            estimated_output_tokens = len(response) // 2
            model_context_limit = prep.model_config.get("context_window", 32000)

            yield _sse("done", {
                "message_id": assistant_msg.id,
                "metadata": msg_metadata,
                "token_usage": {
                    "input_tokens": estimated_input_tokens,
                    "output_tokens": estimated_output_tokens,
                    "estimated_context_used": estimated_input_tokens + estimated_output_tokens,
                    "context_limit": model_context_limit,
                },
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            error_type = _classify_error(e)
            yield _sse("error", {
                "message": str(e),
                "error_type": error_type,
                "retryable": error_type in ("network", "rate_limit"),
            })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.post("/{conv_id}/messages/{msg_id}/save-as-knowledge")
def save_message_as_knowledge(
    conv_id: int,
    msg_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将 assistant 消息内容沉淀为知识条目（Skill 产出通道）。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    msg = db.get(Message, msg_id)
    if not msg or msg.conversation_id != conv_id:
        raise HTTPException(404, "Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise HTTPException(400, "Only assistant messages can be saved as knowledge")

    # 构建标题：取消息前60个字符
    title = msg.content[:60].strip()
    if len(msg.content) > 60:
        title += "..."

    entry = KnowledgeEntry(
        title=title,
        content=msg.content,
        category="experience",
        industry_tags=[],
        platform_tags=[],
        topic_tags=[],
        created_by=user.id,
        department_id=user.department_id,
        source_type="skill_output",
        capture_mode="skill_output",
    )
    db.add(entry)
    db.flush()

    from app.services.knowledge_service import submit_knowledge
    entry = submit_knowledge(db, entry)

    return {
        "knowledge_id": entry.id,
        "status": entry.status.value,
        "review_level": entry.review_level,
        "auto_approved": entry.status.value == "approved",
        "taxonomy_code": entry.taxonomy_code,
        "taxonomy_board": entry.taxonomy_board,
    }


@router.post("/{conv_id}/messages/upload")
async def upload_and_chat(
    conv_id: int,
    message: Optional[str] = Form(None),
    file: UploadFile = File(...),
    active_skill_ids: Optional[str] = Form(None),  # JSON array string e.g. "[1,2,3]"
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """在 chat 中上传文件，自动分类并生成 AI 回复（带分类建议）。"""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    # 保存文件
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1]
    saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    content_bytes = await file.read()
    with open(saved_path, "wb") as f:
        f.write(content_bytes)

    # 提取文本
    try:
        file_text = extract_text(saved_path)
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    # 运行自动分类
    from app.services.knowledge_classifier import classify
    cls_result = None
    try:
        cls_result = await classify(file_text, db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Classification failed: {e}")

    # 超长文本：用 FOE MapReduce 生成结构化摘要用于聊天上下文；原文完整保存到知识库
    _FOE_THRESHOLD = 2000
    chat_content = file_text  # 默认直接用原文
    foe_summary = None
    if len(file_text) > _FOE_THRESHOLD:
        try:
            from app.services.llm_gateway import llm_gateway
            from app.utils.file_parser import foe_summarize
            _cfg = llm_gateway.get_lite_config()
            foe_summary = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: foe_summarize(raw_text=file_text, llm_cfg=_cfg),
            )
            chat_content = foe_summary
        except Exception as _e:
            import logging as _log2
            _log2.getLogger(__name__).warning(f"FOE summarize failed, using raw text: {_e}")

    # 构建发送给 skill_engine 的消息（带分类 context）
    cls_context = ""
    if cls_result:
        cls_context = (
            f"\n\n[系统自动分类] 该文件已被识别为「{cls_result.taxonomy_path[-1] if cls_result.taxonomy_path else cls_result.taxonomy_code}」"
            f"（板块 {cls_result.taxonomy_board}，置信度 {cls_result.confidence:.0%}）"
            f"，建议归入知识库：{', '.join(cls_result.target_kb_ids)}"
        )

    # chat_content: FOE摘要（长文）或原文（短文），用于对话上下文
    # file_text: 原始完整文本，用于知识库存储，数据不丢失
    file_label = f"[文件: {file.filename}]" + (" [FOE摘要]" if foe_summary else "")
    user_text = f"{file_label}\n{chat_content}"
    if message:
        user_text = f"{message}\n\n{user_text}"
    user_text += cls_context

    # 并行：存 KB + 调 skill_engine
    import asyncio as _asyncio
    import logging as _logging

    # 提前取出 user 字段，避免 async task 中访问 detached ORM 对象
    _user_id = user.id
    _dept_id = user.department_id
    _foe_summary = foe_summary  # 捕获到闭包，避免外层变量被覆盖

    async def _save_to_kb():
        # 独立 session，失败不污染主事务
        from app.database import SessionLocal as _SessionLocal
        kb_db = _SessionLocal()
        try:
            kb_entry = KnowledgeEntry(
                title=file.filename or "上传文件",
                content=file_text,
                summary=_foe_summary,
                category=cls_result.taxonomy_board if cls_result else "experience",
                industry_tags=[],
                platform_tags=[],
                topic_tags=[],
                created_by=_user_id,
                department_id=_dept_id,
                source_type="chat_upload",
                capture_mode="chat_upload",
            )
            kb_db.add(kb_entry)
            kb_db.flush()
            from app.services.knowledge_service import submit_knowledge
            kb_entry = submit_knowledge(kb_db, kb_entry)
            kb_db.commit()
            return kb_entry.id
        except Exception as e:
            kb_db.rollback()
            _logging.getLogger(__name__).warning(f"KB auto-save failed: {e}")
            return None
        finally:
            kb_db.close()

    # 先把用户消息存入 DB（此时 skill_engine 从历史读到它，current_message 不重复传）
    user_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.USER,
        content=user_text,
        metadata_={"file_upload": True, "filename": file.filename},
    )
    db.add(user_msg)
    db.flush()

    # 并行执行 KB 存储和 skill_engine（current_message="" 避免重复评估）
    kb_task = _asyncio.create_task(_save_to_kb())
    try:
        parsed_skill_ids = None
        if active_skill_ids:
            import json as _j
            try:
                parsed_skill_ids = _j.loads(active_skill_ids)
            except Exception:
                parsed_skill_ids = None
        result = await skill_engine.execute(db, conv, user_text, user_id=user.id, active_skill_ids=parsed_skill_ids)
    except ValueError as e:
        raise HTTPException(503, str(e))
    kb_entry_id = await kb_task

    response, guide_meta = result

    # Extract token usage from guide_meta
    llm_usage = guide_meta.pop("llm_usage", {})

    # 保存 assistant 回复
    assistant_msg = Message(
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        content=response,
        metadata_={
            "skill_id": conv.skill_id,
            "file_upload": True,
            "filename": file.filename,
            "kb_entry_id": kb_entry_id,
            "classification": cls_result.to_dict() if cls_result else None,
            "model_id": llm_usage.get("model_id"),
            "input_tokens": llm_usage.get("input_tokens", 0),
            "output_tokens": llm_usage.get("output_tokens", 0),
            **guide_meta,
        },
    )
    db.add(assistant_msg)

    if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
        conv.title = f"[文件] {file.filename}"[:60]

    db.commit()

    skill_name = None
    if conv.skill_id:
        from app.models.skill import Skill as SkillModel
        sk = db.get(SkillModel, conv.skill_id)
        skill_name = sk.name if sk else None

    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": response,
        "skill_id": conv.skill_id,
        "skill_name": skill_name,
        "classification": cls_result.to_dict() if cls_result else None,
        "filename": file.filename,
    }


@router.post("/{conv_id}/messages/upload-stream")
async def upload_stream_message(
    conv_id: int,
    message: Optional[str] = Form(None),
    file: UploadFile = File(...),
    active_skill_ids: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming endpoint for file upload: upload + parse + stream LLM response."""
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def event_generator():
        try:
            # 1. Upload phase
            yield _sse("status", {"stage": "uploading"})

            os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
            ext = os.path.splitext(file.filename or "")[1]
            saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
            content_bytes = await file.read()
            with open(saved_path, "wb") as f:
                f.write(content_bytes)

            # 2. Parse phase
            yield _sse("status", {"stage": "parsing"})

            try:
                file_text = extract_text(saved_path)
            except ValueError as e:
                os.unlink(saved_path)
                yield _sse("error", {"message": str(e), "error_type": "server_error", "retryable": False})
                return

            # Classification (best effort)
            from app.services.knowledge_classifier import classify
            cls_result = None
            try:
                cls_result = await classify(file_text, db)
            except Exception:
                pass

            # FOE summarize for long texts
            _FOE_THRESHOLD = 2000
            chat_content = file_text
            foe_summary = None
            if len(file_text) > _FOE_THRESHOLD:
                yield _sse("status", {"stage": "summarizing"})
                try:
                    from app.utils.file_parser import foe_summarize
                    _cfg = llm_gateway.get_lite_config()
                    foe_summary = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: foe_summarize(raw_text=file_text, llm_cfg=_cfg),
                    )
                    chat_content = foe_summary
                except Exception:
                    pass

            # Build user text
            cls_context = ""
            if cls_result:
                cls_context = (
                    f"\n\n[系统自动分类] 该文件已被识别为「{cls_result.taxonomy_path[-1] if cls_result.taxonomy_path else cls_result.taxonomy_code}」"
                    f"（板块 {cls_result.taxonomy_board}，置信度 {cls_result.confidence:.0%}）"
                    f"，建议归入知识库：{', '.join(cls_result.target_kb_ids)}"
                )

            file_label = f"[文件: {file.filename}]" + (" [FOE摘要]" if foe_summary else "")
            user_text = f"{file_label}\n{chat_content}"
            if message:
                user_text = f"{message}\n\n{user_text}"
            user_text += cls_context

            # Save user message
            user_msg = Message(
                conversation_id=conv_id,
                role=MessageRole.USER,
                content=user_text,
                metadata_={"file_upload": True, "filename": file.filename},
            )
            db.add(user_msg)
            db.flush()

            # KB save in background
            _user_id = user.id
            _dept_id = user.department_id
            _foe_summary = foe_summary

            async def _save_to_kb():
                from app.database import SessionLocal as _SessionLocal
                kb_db = _SessionLocal()
                try:
                    kb_entry = KnowledgeEntry(
                        title=file.filename or "上传文件",
                        content=file_text,
                        summary=_foe_summary,
                        category=cls_result.taxonomy_board if cls_result else "experience",
                        industry_tags=[], platform_tags=[], topic_tags=[],
                        created_by=_user_id, department_id=_dept_id,
                        source_type="chat_upload", capture_mode="chat_upload",
                    )
                    kb_db.add(kb_entry)
                    kb_db.flush()
                    from app.services.knowledge_service import submit_knowledge
                    kb_entry = submit_knowledge(kb_db, kb_entry)
                    kb_db.commit()
                    return kb_entry.id
                except Exception:
                    kb_db.rollback()
                    return None
                finally:
                    kb_db.close()

            kb_task = asyncio.create_task(_save_to_kb())

            # 3. Prepare + stream LLM
            yield _sse("status", {"stage": "preparing"})

            parsed_skill_ids = None
            if active_skill_ids:
                try:
                    parsed_skill_ids = json.loads(active_skill_ids)
                except Exception:
                    pass

            prep = await skill_engine.prepare(
                db, conv, user_text,
                user_id=user.id,
                active_skill_ids=parsed_skill_ids,
            )

            skill_name = prep.skill_name

            if prep.early_return is not None:
                response_text, early_meta = prep.early_return
                llm_usage = early_meta.pop("llm_usage", {})
                msg_metadata = {
                    "skill_id": conv.skill_id,
                    "skill_name": skill_name,
                    "file_upload": True, "filename": file.filename,
                    "model_id": llm_usage.get("model_id"),
                    "input_tokens": llm_usage.get("input_tokens", 0),
                    "output_tokens": llm_usage.get("output_tokens", 0),
                    **early_meta,
                }
                assistant_msg = Message(
                    conversation_id=conv_id, role=MessageRole.ASSISTANT,
                    content=response_text, metadata_=msg_metadata,
                )
                db.add(assistant_msg)
                if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
                    conv.title = f"[文件] {file.filename}"[:60]
                db.commit()
                await kb_task
                yield _sse("delta", {"text": response_text})
                yield _sse("done", {"message_id": assistant_msg.id, "metadata": msg_metadata})
                return

            # Stream LLM
            yield _sse("status", {"stage": "generating", "skill_name": skill_name})

            full_content = ""
            block_idx = 0
            current_block_type = None
            native_tool_calls: list[dict] = []

            async for chunk_type, chunk_data in llm_gateway.chat_stream_typed(
                model_config=prep.model_config,
                messages=prep.llm_messages,
                tools=prep.tools_schema or None,
            ):
                if chunk_type == "thinking":
                    if current_block_type != "thinking":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "thinking"})
                        current_block_type = "thinking"
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                elif chunk_type == "tool_call":
                    native_tool_calls.append(chunk_data)
                else:
                    if current_block_type != "text":
                        if current_block_type is not None:
                            yield _sse("content_block_stop", {"index": block_idx})
                            block_idx += 1
                        yield _sse("content_block_start", {"index": block_idx, "type": "text"})
                        current_block_type = "text"
                    full_content += chunk_data
                    yield _sse("content_block_delta", {"index": block_idx, "delta": {"text": chunk_data}})
                    yield _sse("delta", {"text": chunk_data})

            if current_block_type is not None:
                yield _sse("content_block_stop", {"index": block_idx})

            response = full_content
            tool_meta: dict = {}

            # Agent Loop: 原生 function calling 或文本 fallback
            if native_tool_calls or "```tool_call" in response:
                from app.models.skill import Skill as SkillModel
                skill_obj = db.get(SkillModel, prep.skill_id) if prep.skill_id else None
                yield _sse("status", {"stage": "tool_calling"})
                async for item in skill_engine._handle_tool_calls_stream(
                    db, skill_obj, response, prep.llm_messages, prep.model_config, user.id,
                    tools_schema=prep.tools_schema or None,
                    native_tool_calls=native_tool_calls or None,
                ):
                    if isinstance(item, tuple):
                        response, tool_meta = item
                    else:
                        yield _sse(item["event"], item["data"])
                yield _sse("replace", {"text": response})

            kb_entry_id = await kb_task

            msg_metadata = {
                "skill_id": conv.skill_id,
                "skill_name": skill_name,
                "file_upload": True,
                "filename": file.filename,
                "kb_entry_id": kb_entry_id,
                **tool_meta,
            }

            assistant_msg = Message(
                conversation_id=conv_id, role=MessageRole.ASSISTANT,
                content=response, metadata_=msg_metadata,
            )
            db.add(assistant_msg)
            if db.query(Message).filter(Message.conversation_id == conv_id).count() <= 2:
                conv.title = f"[文件] {file.filename}"[:60]
            db.commit()

            total_input_chars = sum(len(m.get("content", "")) for m in prep.llm_messages)
            estimated_input_tokens = total_input_chars // 2
            estimated_output_tokens = len(response) // 2
            model_context_limit = prep.model_config.get("context_window", 32000)

            yield _sse("done", {
                "message_id": assistant_msg.id,
                "metadata": msg_metadata,
                "token_usage": {
                    "input_tokens": estimated_input_tokens,
                    "output_tokens": estimated_output_tokens,
                    "estimated_context_used": estimated_input_tokens + estimated_output_tokens,
                    "context_limit": model_context_limit,
                },
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            error_type = _classify_error(e)
            yield _sse("error", {
                "message": str(e),
                "error_type": error_type,
                "retryable": error_type in ("network", "rate_limit"),
            })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConversationPatch(BaseModel):
    title: Optional[str] = None
    skill_id: Optional[int] = None  # None = keep current; -1 = clear


@router.patch("/{conv_id}")
def patch_conversation(
    conv_id: int,
    req: ConversationPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    if req.title is not None:
        conv.title = req.title[:60]
    if req.skill_id is not None:
        conv.skill_id = None if req.skill_id == -1 else req.skill_id
    db.commit()
    return {"ok": True, "title": conv.title, "skill_id": conv.skill_id}


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
