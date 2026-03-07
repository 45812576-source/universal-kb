import json
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.user import Role, User
from app.services.knowledge_service import approve_knowledge, reject_knowledge
from app.utils.file_parser import extract_text

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class KnowledgeCreate(BaseModel):
    title: str
    content: str
    category: str = "experience"
    industry_tags: list[str] = []
    platform_tags: list[str] = []
    topic_tags: list[str] = []


class ReviewAction(BaseModel):
    action: str  # "approve" or "reject"
    note: str = ""


def _entry_dict(e: KnowledgeEntry) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "content": e.content[:300] + ("..." if len(e.content) > 300 else ""),
        "category": e.category,
        "status": e.status.value,
        "department_id": e.department_id,
        "created_by": e.created_by,
        "reviewed_by": e.reviewed_by,
        "review_note": e.review_note,
        "industry_tags": e.industry_tags or [],
        "platform_tags": e.platform_tags or [],
        "topic_tags": e.topic_tags or [],
        "source_type": e.source_type,
        "source_file": e.source_file,
        "created_at": e.created_at.isoformat(),
    }


@router.post("")
def create_knowledge(
    req: KnowledgeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = KnowledgeEntry(
        title=req.title,
        content=req.content,
        category=req.category,
        industry_tags=req.industry_tags,
        platform_tags=req.platform_tags,
        topic_tags=req.topic_tags,
        created_by=user.id,
        department_id=user.department_id,
        source_type="manual",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "status": entry.status.value}


@router.post("/upload")
async def upload_knowledge(
    title: str = Form(...),
    category: str = Form("experience"),
    industry_tags: str = Form("[]"),
    platform_tags: str = Form("[]"),
    topic_tags: str = Form("[]"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1]
    saved_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")

    with open(saved_path, "wb") as f:
        f.write(await file.read())

    try:
        content = extract_text(saved_path)
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    entry = KnowledgeEntry(
        title=title,
        content=content,
        category=category,
        industry_tags=json.loads(industry_tags),
        platform_tags=json.loads(platform_tags),
        topic_tags=json.loads(topic_tags),
        created_by=user.id,
        department_id=user.department_id,
        source_type="upload",
        source_file=file.filename,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {
        "id": entry.id,
        "status": entry.status.value,
        "content_length": len(content),
    }


@router.get("")
def list_knowledge(
    status: str = None,
    category: str = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(KnowledgeEntry)

    if user.role == Role.EMPLOYEE:
        # Employees see their own + all approved
        from sqlalchemy import or_
        q = q.filter(
            or_(
                KnowledgeEntry.created_by == user.id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )
    elif user.role == Role.DEPT_ADMIN:
        from sqlalchemy import or_
        q = q.filter(
            or_(
                KnowledgeEntry.department_id == user.department_id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )
    # SUPER_ADMIN sees all

    if status:
        q = q.filter(KnowledgeEntry.status == status)
    if category:
        q = q.filter(KnowledgeEntry.category == category)

    entries = q.order_by(KnowledgeEntry.created_at.desc()).limit(100).all()
    return [_entry_dict(e) for e in entries]


@router.get("/{kid}")
def get_knowledge(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    result = _entry_dict(entry)
    result["content"] = entry.content  # full content for detail view
    return result


@router.post("/{kid}/review")
def review_knowledge(
    kid: int,
    req: ReviewAction,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.status != KnowledgeStatus.PENDING:
        raise HTTPException(400, "Only pending entries can be reviewed")

    # Dept admin can only review their department's entries
    if user.role == Role.DEPT_ADMIN and entry.department_id != user.department_id:
        raise HTTPException(403, "Can only review your department's entries")

    if req.action == "approve":
        entry = approve_knowledge(db, kid, user.id, req.note)
    elif req.action == "reject":
        entry = reject_knowledge(db, kid, user.id, req.note)
    else:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    return _entry_dict(entry)
