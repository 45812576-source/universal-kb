import json
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder, KnowledgeStatus, ReviewStage
from app.models.user import Role, User
from app.services.knowledge_service import (
    approve_knowledge,
    reject_knowledge,
    submit_knowledge,
    super_approve_knowledge,
    super_reject_knowledge,
)
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


class SuperReviewAction(BaseModel):
    action: str  # "approve" or "reject"
    note: str = ""


_REVIEW_LEVEL_LABEL = {1: "L1-自动", 2: "L2-部门", 3: "L3-超管"}
_REVIEW_STAGE_LABEL = {
    ReviewStage.AUTO_APPROVED: "自动通过",
    ReviewStage.PENDING_DEPT: "待部门审核",
    ReviewStage.DEPT_APPROVED_PENDING_SUPER: "待超管确认",
    ReviewStage.APPROVED: "已通过",
    ReviewStage.REJECTED: "已拒绝",
}


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
        "capture_mode": e.capture_mode,
        "review_level": e.review_level or 2,
        "review_level_label": _REVIEW_LEVEL_LABEL.get(e.review_level or 2, "L2-部门"),
        "review_stage": e.review_stage.value if e.review_stage else "pending_dept",
        "review_stage_label": _REVIEW_STAGE_LABEL.get(
            e.review_stage, "待部门审核"
        ),
        "sensitivity_flags": e.sensitivity_flags or [],
        "auto_review_note": e.auto_review_note,
        "folder_id": e.folder_id,
        "taxonomy_board": e.taxonomy_board,
        "taxonomy_code": e.taxonomy_code,
        "taxonomy_path": e.taxonomy_path or [],
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
        capture_mode="manual_form",
    )
    db.add(entry)
    db.flush()
    entry = submit_knowledge(db, entry)
    return {"id": entry.id, "status": entry.status.value, "review_level": entry.review_level}


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

    # 检测敏感词决定 capture_mode
    from app.services.review_policy import review_policy
    sensitive_flags = review_policy.detect_sensitive(content)
    strategic_flags = review_policy.detect_strategic(content)
    if sensitive_flags or strategic_flags:
        capture_mode = "upload"
    else:
        capture_mode = "upload_ai_clean"

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
        capture_mode=capture_mode,
    )
    db.add(entry)
    db.flush()

    # 自动分类（异步，不阻塞入库）
    from app.services.knowledge_classifier import classify, apply_classification_to_entry
    try:
        cls_result = await classify(content, db)
        if cls_result:
            apply_classification_to_entry(entry, cls_result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Auto-classification failed: {e}")

    entry = submit_knowledge(db, entry)

    # 向量索引（后台，不阻塞响应）
    try:
        from app.services import vector_service
        vector_service.index_knowledge(entry.id, content, created_by=user.id)
    except Exception as _ve:
        import logging
        logging.getLogger(__name__).warning(f"Vector indexing failed for entry {entry.id}: {_ve}")

    return {
        "id": entry.id,
        "status": entry.status.value,
        "content_length": len(content),
        "review_level": entry.review_level,
        "capture_mode": entry.capture_mode,
        "taxonomy_code": entry.taxonomy_code,
        "taxonomy_board": entry.taxonomy_board,
        "classification_confidence": entry.classification_confidence,
    }


@router.get("")
def list_knowledge(
    status: str = None,
    category: str = None,
    source_type: str = None,
    review_stage: str = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(KnowledgeEntry)

    # 员工可见：自己创建的 + 已审批通过的
    if user.role == Role.EMPLOYEE:
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
                KnowledgeEntry.created_by == user.id,
                KnowledgeEntry.department_id == user.department_id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )
    # SUPER_ADMIN sees all

    if status:
        q = q.filter(KnowledgeEntry.status == status)
    if category:
        q = q.filter(KnowledgeEntry.category == category)
    if source_type:
        q = q.filter(KnowledgeEntry.source_type == source_type)
    if review_stage:
        q = q.filter(KnowledgeEntry.review_stage == review_stage)

    entries = q.order_by(KnowledgeEntry.created_at.desc()).all()
    return [_entry_dict(e) for e in entries]


@router.get("/chunks/search")
def search_chunks(
    q: str = None,
    taxonomy_board: str = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """向量语义搜索知识切片；Milvus 不可用时退化为 SQL LIKE 搜索。
    taxonomy_board: A/B/C/D/E/F 对应知识大类，空=不限。
    """
    from sqlalchemy import or_

    # 先按权限过滤
    eq = db.query(KnowledgeEntry)
    if user.role.value == "employee":
        eq = eq.filter(
            or_(
                KnowledgeEntry.created_by == user.id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )
    elif user.role.value == "dept_admin":
        eq = eq.filter(
            or_(
                KnowledgeEntry.department_id == user.department_id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )

    # 按知识大类过滤（taxonomy_board 字段，如 "A"/"B"/"C"...）
    if taxonomy_board:
        eq = eq.filter(KnowledgeEntry.taxonomy_board == taxonomy_board)

    entries = eq.all()
    entry_map = {e.id: e for e in entries}
    kid_list = list(entry_map.keys())

    if not kid_list:
        return []

    results = []

    if q:
        # 尝试 Milvus 向量搜索
        try:
            from app.services import vector_service
            hits = vector_service.search_knowledge(q, top_k=limit * 5, knowledge_id_filter=kid_list)
            # 每个文件只保留分数最高的 chunk
            best: dict[int, dict] = {}
            for hit in hits:
                if hit["score"] < 0.3:
                    continue
                kid = hit["knowledge_id"]
                if kid not in best or hit["score"] > best[kid]["score"]:
                    e = entry_map.get(kid)
                    if e:
                        best[kid] = {
                            "knowledge_id": kid,
                            "chunk_index": hit["chunk_index"],
                            "text": hit["text"],
                            "score": hit["score"],
                            "source_file": e.source_file,
                            "taxonomy_board": e.taxonomy_board,
                            "category": e.category,
                            "title": e.title,
                        }
            results = sorted(best.values(), key=lambda x: x["score"], reverse=True)
            return results[:limit]
        except Exception:
            pass

        # 退化：SQL LIKE 匹配 content
        matched = [e for e in entries if q.lower() in (e.content or "").lower() or q.lower() in (e.title or "").lower()]
        for e in matched[:limit]:
            content = e.content or ""
            idx = content.lower().find(q.lower())
            start = max(0, idx - 50)
            snippet = content[start:start + 300]
            results.append({
                "knowledge_id": e.id,
                "chunk_index": 0,
                "text": snippet,
                "score": 1.0,
                "source_file": e.source_file,
                "taxonomy_board": e.taxonomy_board,
                "category": e.category,
                "title": e.title,
            })
        return results

    # 无关键词：直接返回最近的条目摘要
    for e in entries[:limit]:
        content = e.content or ""
        results.append({
            "knowledge_id": e.id,
            "chunk_index": 0,
            "text": content[:300],
            "score": 1.0,
            "source_file": e.source_file,
            "taxonomy_board": e.taxonomy_board,
            "category": e.category,
            "title": e.title,
        })
    return results


@router.get("/{kid}/chunks")
def get_knowledge_chunks(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回某条知识的所有 chunks（用于预览完整内容）。"""
    from sqlalchemy import or_

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    # 权限检查
    if user.role.value == "employee":
        if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")
    elif user.role.value == "dept_admin":
        if entry.department_id != user.department_id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")

    # 尝试从 Milvus 拉取 chunks
    chunks = []
    try:
        from app.services import vector_service
        col = vector_service.get_collection()
        res = col.query(
            expr=f"knowledge_id == {kid}",
            output_fields=["chunk_index", "text"],
            limit=200,
        )
        res_sorted = sorted(res, key=lambda x: x["chunk_index"])
        chunks = [{"index": r["chunk_index"], "text": r["text"]} for r in res_sorted]
    except Exception:
        pass

    if not chunks:
        # 退化：将 content 按 500 字切片
        content = entry.content or ""
        size = 500
        chunks = [
            {"index": i, "text": content[i * size: (i + 1) * size]}
            for i in range(max(1, (len(content) + size - 1) // size))
            if content[i * size: (i + 1) * size]
        ]

    return {
        "id": entry.id,
        "title": entry.title,
        "content": entry.content,
        "source_type": entry.source_type,
        "source_file": entry.source_file,
        "chunks": chunks,
    }


# ─── Folder CRUD ──────────────────────────────────────────────────────────────

class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None


class FolderRename(BaseModel):
    name: str


class FolderMove(BaseModel):
    parent_id: Optional[int] = None


def _folder_dict(f: KnowledgeFolder) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "parent_id": f.parent_id,
        "sort_order": f.sort_order,
        "created_by": f.created_by,
        "created_at": f.created_at.isoformat(),
    }


@router.get("/folders")
def list_folders(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户自己创建的文件夹（扁平列表，前端自行构建树）。
    "我的整理"文件夹属于个人空间，任何角色都只看自己的；
    超管/部门管理员的跨用户权限体现在系统归档视图里。
    """
    q = db.query(KnowledgeFolder).filter(KnowledgeFolder.created_by == user.id)
    folders = q.order_by(KnowledgeFolder.sort_order, KnowledgeFolder.id).all()
    return [_folder_dict(f) for f in folders]


@router.post("/folders")
def create_folder(
    req: FolderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = KnowledgeFolder(
        name=req.name,
        parent_id=req.parent_id,
        created_by=user.id,
        department_id=user.department_id,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _folder_dict(folder)


@router.patch("/folders/{fid}/rename")
def rename_folder(
    fid: int,
    req: FolderRename,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = db.get(KnowledgeFolder, fid)
    if not folder:
        raise HTTPException(404, "Folder not found")
    if folder.created_by != user.id and user.role == Role.EMPLOYEE:
        raise HTTPException(403, "Cannot rename others' folders")
    folder.name = req.name
    db.commit()
    return _folder_dict(folder)


@router.patch("/folders/{fid}/move")
def move_folder(
    fid: int,
    req: FolderMove,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = db.get(KnowledgeFolder, fid)
    if not folder:
        raise HTTPException(404, "Folder not found")
    if folder.created_by != user.id and user.role == Role.EMPLOYEE:
        raise HTTPException(403, "Cannot move others' folders")
    if req.parent_id is not None:
        def _is_descendant(check_id: int) -> bool:
            node = db.get(KnowledgeFolder, check_id)
            while node:
                if node.id == fid:
                    return True
                if node.parent_id is None:
                    return False
                node = db.get(KnowledgeFolder, node.parent_id)
            return False
        if _is_descendant(req.parent_id):
            raise HTTPException(400, "Cannot move folder into its own descendant")
    folder.parent_id = req.parent_id
    db.commit()
    return _folder_dict(folder)


@router.delete("/folders/{fid}")
def delete_folder(
    fid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = db.get(KnowledgeFolder, fid)
    if not folder:
        raise HTTPException(404, "Folder not found")
    if folder.created_by != user.id and user.role == Role.EMPLOYEE:
        raise HTTPException(403, "Cannot delete others' folders")
    for child in db.query(KnowledgeFolder).filter(KnowledgeFolder.parent_id == fid).all():
        child.parent_id = folder.parent_id
    for entry in db.query(KnowledgeEntry).filter(KnowledgeEntry.folder_id == fid).all():
        entry.folder_id = folder.parent_id
    db.delete(folder)
    db.commit()
    return {"ok": True}


@router.get("/{kid}")
def get_knowledge(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    # 只能查看自己的，或已审批的（供 RAG / chat 引用）
    if user.role != Role.SUPER_ADMIN:
        if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")
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

    # 只能审核 PENDING 状态 且 review_stage 为 pending_dept
    if entry.status != KnowledgeStatus.PENDING:
        raise HTTPException(400, "Only pending entries can be reviewed")
    if entry.review_stage not in (ReviewStage.PENDING_DEPT, None):
        raise HTTPException(
            400,
            f"Entry is in stage '{entry.review_stage}', not eligible for dept review",
        )

    # 部门管理员只能审核本部门条目
    if user.role == Role.DEPT_ADMIN and entry.department_id != user.department_id:
        raise HTTPException(403, "Can only review your department's entries")

    if req.action == "approve":
        entry = approve_knowledge(db, kid, user.id, req.note)
    elif req.action == "reject":
        entry = reject_knowledge(db, kid, user.id, req.note)
    else:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    return _entry_dict(entry)


@router.post("/{kid}/super-review")
def super_review_knowledge(
    kid: int,
    req: SuperReviewAction,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管二次确认，仅用于 L3 流程（dept_approved_pending_super 状态）。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.review_stage != ReviewStage.DEPT_APPROVED_PENDING_SUPER:
        raise HTTPException(
            400,
            f"Entry is not in dept_approved_pending_super stage (current: {entry.review_stage})",
        )

    if req.action == "approve":
        try:
            entry = super_approve_knowledge(db, kid, user.id, req.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
    elif req.action == "reject":
        entry = super_reject_knowledge(db, kid, user.id, req.note)
    else:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    return _entry_dict(entry)


class EntryUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


@router.patch("/{kid}")
def update_knowledge(
    kid: int,
    req: EntryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role == Role.EMPLOYEE and entry.created_by != user.id:
        raise HTTPException(403, "Cannot edit others' entries")
    if user.role == Role.DEPT_ADMIN and entry.created_by != user.id and entry.department_id != user.department_id:
        raise HTTPException(403, "只能编辑本部门的知识条目")
    if req.title is not None:
        entry.title = req.title
    if req.content is not None:
        entry.content = req.content
    db.commit()
    return _entry_dict(entry)


@router.delete("/{kid}")
def delete_knowledge(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role == Role.EMPLOYEE and entry.created_by != user.id:
        raise HTTPException(403, "Cannot delete others' entries")
    if user.role == Role.DEPT_ADMIN and entry.created_by != user.id and entry.department_id != user.department_id:
        raise HTTPException(403, "只能删除本部门的知识条目")
    db.delete(entry)
    db.commit()
    return {"ok": True}


@router.post("/{kid}/summarize")
async def summarize_knowledge(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回知识条目的 FOE 摘要。已有则直接返回；否则现场生成并持久化。"""
    from sqlalchemy import or_
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    # 权限：自己创建的 或 已审批的
    if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
        raise HTTPException(403, "No access to this entry")

    if entry.summary:
        return {"summary": entry.summary}

    # 现场生成
    try:
        import asyncio as _asyncio
        from app.services.llm_gateway import llm_gateway
        from app.utils.file_parser import foe_summarize
        _cfg = llm_gateway.get_lite_config()
        _content = entry.content
        summary = await _asyncio.get_event_loop().run_in_executor(
            None,
            lambda: foe_summarize(raw_text=_content, llm_cfg=_cfg),
        )
        entry.summary = summary
        db.commit()
        return {"summary": summary}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"summarize failed for kid={kid}: {e}")
        # 降级：返回前 800 字
        return {"summary": (entry.content or "")[:800]}


@router.patch("/{kid}/folder")
def move_entry_to_folder(
    kid: int,
    folder_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把知识条目移动到指定文件夹（folder_id=None 表示移到根）。
    超管可移动任何条目；其他角色只能移动自己创建的条目。
    """
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
        raise HTTPException(403, "只有超级管理员可以移动他人的知识条目")
    entry.folder_id = folder_id
    db.commit()
    return {"ok": True, "folder_id": folder_id}


@router.post("/image-upload")
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload an inline image for the rich text editor. Returns a public URL."""
    ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"不支持的图片格式: {ext}")

    image_dir = os.path.join(settings.UPLOAD_DIR, "images")
    os.makedirs(image_dir, exist_ok=True)

    filename = f"{uuid.uuid4()}{ext}"
    saved_path = os.path.join(image_dir, filename)
    with open(saved_path, "wb") as f:
        f.write(await file.read())

    url = f"/api/knowledge/images/{filename}"
    return {"url": url}


@router.get("/images/{filename}")
def serve_image(filename: str):
    """Serve an uploaded inline image."""
    from fastapi.responses import FileResponse
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    image_dir = os.path.join(settings.UPLOAD_DIR, "images")
    path = os.path.join(image_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Image not found")
    return FileResponse(path)
