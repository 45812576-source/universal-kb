"""Low-friction input API: raw_inputs + drafts lifecycle."""
import datetime
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.draft import Draft, DraftStatus, LearningSample
from app.models.feedback_item import FeedbackItem
from app.models.knowledge import KnowledgeEntry
from app.models.opportunity import Opportunity
from app.models.raw_input import DetectedObjectType, RawInput, RawInputSourceType
from app.models.user import User
from app.services.input_processor import process_raw_input

router = APIRouter(prefix="/api", tags=["drafts"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _draft_dict(d: Draft) -> dict:
    return {
        "id": d.id,
        "object_type": d.object_type.value if d.object_type else "unknown",
        "title": d.title,
        "summary": d.summary,
        "fields": d.fields_json or {},
        "tags": d.tags_json or {},
        "pending_questions": d.pending_questions or [],
        "confirmed_fields": d.confirmed_fields or {},
        "suggested_actions": d.suggested_actions or [],
        "status": d.status.value if d.status else "draft",
        "formal_object_id": d.formal_object_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _save_upload(file: UploadFile) -> str:
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "file")[1]
    path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    content = file.file.read()
    with open(path, "wb") as f:
        f.write(content)
    return path


# ── raw-inputs ────────────────────────────────────────────────────────────────

@router.post("/raw-inputs")
async def create_raw_input(
    text: Optional[str] = Form(None),
    source_type: str = Form("text"),
    source_channel: str = Form("web"),
    workspace_id: Optional[int] = Form(None),
    conversation_id: Optional[int] = Form(None),
    url: Optional[str] = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接收多模态原始输入，触发 AI 处理，返回生成的 draft。"""
    attachment_urls = []
    for f in files:
        if f.filename:
            path = _save_upload(f)
            attachment_urls.append(path)

    ri = RawInput(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        created_by_id=user.id,
        source_type=source_type,
        source_channel=source_channel,
        raw_text=text,
        attachment_urls=attachment_urls,
        context_json={"url": url} if url else {},
    )
    db.add(ri)
    db.flush()

    draft = await process_raw_input(ri.id, db)
    return {"raw_input_id": ri.id, "draft": _draft_dict(draft)}


# ── drafts ────────────────────────────────────────────────────────────────────

@router.get("/drafts")
def list_drafts(
    status: Optional[str] = None,
    object_type: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Draft).filter(Draft.created_by_id == user.id)
    if status:
        q = q.filter(Draft.status == status)
    if object_type:
        q = q.filter(Draft.object_type == object_type)
    drafts = q.order_by(Draft.created_at.desc()).limit(50).all()
    return [_draft_dict(d) for d in drafts]


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    return _draft_dict(draft)


class ConfirmRequest(BaseModel):
    confirmed_fields: dict = {}
    corrections: dict = {}


@router.patch("/drafts/{draft_id}/confirm")
def confirm_draft_fields(
    draft_id: int,
    req: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户确认字段或纠错，记录 learning sample。"""
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")

    # 合并已确认字段
    confirmed = dict(draft.confirmed_fields or {})
    confirmed.update(req.confirmed_fields)
    draft.confirmed_fields = confirmed

    # 记录纠错
    if req.corrections:
        corrections = list(draft.user_corrections or [])
        for field, new_val in req.corrections.items():
            ai_val = (draft.fields_json or {}).get(field)
            corrections.append({
                "field": field,
                "ai_value": ai_val,
                "user_value": new_val,
                "ts": datetime.datetime.utcnow().isoformat(),
            })
            # 更新 fields_json
            fields = dict(draft.fields_json or {})
            fields[field] = new_val
            draft.fields_json = fields

            # 写 learning sample
            sample = LearningSample(
                draft_id=draft.id,
                raw_input_id=draft.source_raw_input_id,
                object_type=draft.object_type.value if draft.object_type else "unknown",
                task_type="field_correction",
                model_output_json={"field": field, "value": ai_val},
                user_correction_json={"field": field, "value": new_val},
                final_answer_json={"field": field, "value": new_val},
                created_by_id=user.id,
            )
            db.add(sample)
        draft.user_corrections = corrections

    # 移除已确认的 pending_questions
    answered = set(req.confirmed_fields.keys()) | set(req.corrections.keys())
    draft.pending_questions = [
        q for q in (draft.pending_questions or [])
        if q.get("field") not in answered
    ]

    if not draft.pending_questions:
        draft.status = DraftStatus.CONFIRMED

    db.commit()
    return _draft_dict(draft)


@router.post("/drafts/{draft_id}/convert")
def convert_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将草稿转为正式对象（knowledge_entry / opportunity / feedback_item）。"""
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    if draft.status == DraftStatus.CONVERTED:
        raise HTTPException(400, "Draft already converted")
    if draft.status == DraftStatus.DISCARDED:
        raise HTTPException(400, "Draft is discarded")

    # 合并 AI 字段 + 用户确认字段（用户确认优先）
    fields = {**(draft.fields_json or {}), **(draft.confirmed_fields or {})}

    formal_id = None

    if draft.object_type == DetectedObjectType.KNOWLEDGE:
        # 判断确认程度决定 capture_mode
        pending_qs = draft.pending_questions or []
        if draft.status == DraftStatus.CONFIRMED or not pending_qs:
            capture_mode = "chat_delegate_confirmed"
        else:
            capture_mode = "chat_delegate_partial"

        entry = KnowledgeEntry(
            title=draft.title or fields.get("title", "未命名"),
            content=fields.get("content_summary", draft.summary or ""),
            category=fields.get("knowledge_type", "experience"),
            industry_tags=fields.get("industry_tags", []),
            platform_tags=fields.get("platform_tags", []),
            topic_tags=fields.get("topic_tags", []),
            created_by=user.id,
            department_id=user.department_id,
            source_type="ai_draft",
            source_draft_id=draft.id,
            raw_input_id=draft.source_raw_input_id,
            capture_mode=capture_mode,
        )

        # 从 draft.fields_json 读取已有分类结果（由 input_processor 写入）
        cls_data = (draft.fields_json or {}).get("_taxonomy_classification")
        if cls_data:
            entry.taxonomy_code = cls_data.get("taxonomy_code")
            entry.taxonomy_board = cls_data.get("taxonomy_board")
            entry.taxonomy_path = cls_data.get("taxonomy_path")
            entry.storage_layer = cls_data.get("storage_layer")
            entry.target_kb_ids = cls_data.get("target_kb_ids")
            entry.serving_skill_codes = cls_data.get("serving_skill_codes")
            entry.ai_classification_note = cls_data.get("reasoning")
            entry.classification_confidence = cls_data.get("confidence")

        db.add(entry)
        db.flush()
        formal_id = entry.id

        # 通过策略引擎决定是否自动通过
        from app.services.knowledge_service import submit_knowledge
        submit_knowledge(db, entry)

    elif draft.object_type == DetectedObjectType.OPPORTUNITY:
        opp = Opportunity(
            title=draft.title or fields.get("title", "未命名商机"),
            customer_name=fields.get("customer_name"),
            industry=fields.get("industry"),
            stage=fields.get("stage", "lead"),
            priority=fields.get("priority", "normal"),
            needs_summary=fields.get("needs_summary"),
            decision_map=fields.get("decision_map", []),
            risk_points=fields.get("risk_points", []),
            next_actions=fields.get("next_actions", []),
            source_draft_id=draft.id,
            created_by_id=user.id,
            department_id=user.department_id,
        )
        db.add(opp)
        db.flush()
        formal_id = opp.id

    elif draft.object_type == DetectedObjectType.FEEDBACK:
        fb = FeedbackItem(
            title=draft.title or fields.get("title", "未命名反馈"),
            customer_name=fields.get("customer_name"),
            feedback_type=fields.get("feedback_type"),
            severity=fields.get("severity", "medium"),
            description=fields.get("description", draft.summary or ""),
            affected_module=fields.get("affected_module"),
            renewal_risk_level=fields.get("renewal_risk_level", "low"),
            routed_team=fields.get("routed_team"),
            knowledgeworthy=1 if fields.get("knowledgeworthy") else 0,
            source_draft_id=draft.id,
            created_by_id=user.id,
        )
        db.add(fb)
        db.flush()
        formal_id = fb.id

    else:
        raise HTTPException(400, f"Cannot convert object_type: {draft.object_type}")

    draft.formal_object_id = formal_id
    draft.status = DraftStatus.CONVERTED
    db.commit()

    return {
        "draft_id": draft.id,
        "object_type": draft.object_type.value,
        "formal_object_id": formal_id,
    }


@router.post("/drafts/{draft_id}/discard")
def discard_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    draft.status = DraftStatus.DISCARDED
    db.commit()
    return {"ok": True}


# ── Confirmations feed ────────────────────────────────────────────────────────

@router.get("/confirmations")
def get_pending_confirmations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户所有 waiting_confirmation 草稿的待确认问题（信息流）。"""
    drafts = (
        db.query(Draft)
        .filter(
            Draft.created_by_id == user.id,
            Draft.status == DraftStatus.WAITING_CONFIRMATION,
        )
        .order_by(Draft.created_at.desc())
        .limit(20)
        .all()
    )

    items = []
    for d in drafts:
        for q in (d.pending_questions or []):
            items.append({
                "draft_id": d.id,
                "draft_title": d.title,
                "object_type": d.object_type.value if d.object_type else "unknown",
                **q,
            })
    return items
