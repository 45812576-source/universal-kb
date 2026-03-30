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
from app.models.knowledge import KnowledgeEntry, KnowledgeEditGrant, KnowledgeFolder, KnowledgeStatus, ReviewStage
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


_ONLYOFFICE_EXTS = {
    ".docx", ".doc", ".odt", ".rtf", ".txt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
}


def _entry_dict(e: KnowledgeEntry) -> dict:
    ext = (e.file_ext or "").lower()
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
        # OSS 文件信息
        "oss_key": e.oss_key,
        "file_type": e.file_type,
        "file_ext": e.file_ext,
        "file_size": e.file_size,
        # AI 命名
        "ai_title": e.ai_title,
        "ai_summary": e.ai_summary,
        "ai_tags": e.ai_tags,
        "quality_score": e.quality_score,
        # 云文档渲染状态
        "doc_render_status": e.doc_render_status,
        "doc_render_error": e.doc_render_error,
        "doc_render_mode": e.doc_render_mode,
        # 来源与同步
        "source_uri": e.source_uri,
        "sync_status": e.sync_status,
        "sync_error": e.sync_error,
        "lark_doc_url": e.lark_doc_url,
        "lark_doc_token": e.lark_doc_token,
        "lark_sync_interval": e.lark_sync_interval,
        "lark_last_synced_at": e.lark_last_synced_at,
        # 分类状态
        "classification_status": e.classification_status,
        "classification_error": e.classification_error,
        "classification_confidence": e.classification_confidence,
        "classification_source": e.classification_source,
        "classified_at": e.classified_at.isoformat() if e.classified_at else None,
        # 能力标志
        "can_open_onlyoffice": bool(e.oss_key and ext in _ONLYOFFICE_EXTS),
        "can_retry_render": e.doc_render_status in ("failed", "pending", None),
        "can_retry_classification": e.classification_status in ("failed", "pending", "needs_review", None),
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
    import mimetypes

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower()
    local_filename = f"{uuid.uuid4()}{ext}"
    saved_path = os.path.join(settings.UPLOAD_DIR, local_filename)

    file_data = await file.read()
    with open(saved_path, "wb") as f:
        f.write(file_data)

    # 提取文本内容（供 AI/向量化）
    try:
        content = extract_text(saved_path)
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    # 上传原件到 OSS
    oss_key = None
    file_size = len(file_data)
    file_type = mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    try:
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        oss_key = generate_oss_key(ext)
        oss_upload(saved_path, oss_key)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OSS upload failed, file kept locally: {e}")

    # 注意：暂不清理本地文件，后续 doc_renderer 需要读取
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
        # OSS 文件信息
        oss_key=oss_key,
        file_type=file_type,
        file_ext=ext,
        file_size=file_size,
        # 云文档渲染状态
        doc_render_status="pending",
    )
    db.add(entry)
    db.flush()

    # 云文档转换 —— 改为入队，不在请求内同步完成
    from app.services.doc_renderer import render_from_path
    try:
        render_from_path(db, entry, saved_path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Doc render failed (will retry via job): {e}")

    # 清理本地临时文件
    try:
        os.unlink(saved_path)
    except OSError:
        pass

    # AI 智能命名（异步，不阻塞入库）
    from app.services.knowledge_namer import auto_name
    try:
        naming_result = await auto_name(content, file.filename or "", file_type, db=db)
        entry.ai_title = naming_result["title"]
        entry.ai_summary = naming_result["summary"]
        entry.ai_tags = naming_result["tags"]
        entry.quality_score = naming_result["quality_score"]
        # AI 生成的标签也同步到筛选用标签字段
        if naming_result["tags"].get("industry"):
            entry.industry_tags = naming_result["tags"]["industry"]
        if naming_result["tags"].get("platform"):
            entry.platform_tags = naming_result["tags"]["platform"]
        if naming_result["tags"].get("topic"):
            entry.topic_tags = naming_result["tags"]["topic"]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"AI naming failed: {e}")

    entry = submit_knowledge(db, entry)

    # 创建异步 Job：render 补偿（如果同步渲染失败了）+ classify
    from app.models.knowledge_job import KnowledgeJob
    if entry.doc_render_status in ("failed", "pending"):
        render_job = KnowledgeJob(
            knowledge_id=entry.id,
            job_type="render",
            trigger_source="upload",
        )
        db.add(render_job)

    classify_job = KnowledgeJob(
        knowledge_id=entry.id,
        job_type="classify",
        trigger_source="upload",
    )
    db.add(classify_job)
    entry.classification_status = "pending"
    db.commit()

    return {
        "id": entry.id,
        "status": entry.status.value,
        "content_length": len(content),
        "review_level": entry.review_level,
        "capture_mode": entry.capture_mode,
        "taxonomy_code": entry.taxonomy_code,
        "taxonomy_board": entry.taxonomy_board,
        "classification_confidence": entry.classification_confidence,
        "oss_key": entry.oss_key,
        "file_type": entry.file_type,
        "file_ext": entry.file_ext,
        "doc_render_status": entry.doc_render_status,
        "doc_render_mode": entry.doc_render_mode,
    }


@router.get("")
def list_knowledge(
    status: str = None,
    category: str = None,
    source_type: str = None,
    review_stage: str = None,
    doc_render_status: str = None,
    classification_status: str = None,
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
    if doc_render_status:
        q = q.filter(KnowledgeEntry.doc_render_status == doc_render_status)
    if classification_status:
        q = q.filter(KnowledgeEntry.classification_status == classification_status)

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


# ─── 文件下载/预览 URL ────────────────────────────────────────────────────────

@router.get("/{kid}/file-url")
def get_file_url(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回 OSS 签名下载 URL（1小时有效）。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN:
        if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")
    if not entry.oss_key:
        raise HTTPException(404, "此知识条目没有关联的原始文件")

    from app.services.oss_service import generate_signed_url
    # 图片/PDF/音视频等浏览器可内联预览的格式用 inline，其余用 attachment
    INLINE_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".mp4", ".webm", ".mp3", ".wav", ".m4a"}
    file_ext = (entry.file_ext or "").lower()
    url = generate_signed_url(entry.oss_key, expires=3600, inline=file_ext in INLINE_EXTS)
    return {
        "url": url,
        "filename": entry.source_file,
        "file_type": entry.file_type,
        "file_ext": entry.file_ext,
        "file_size": entry.file_size,
    }


@router.get("/{kid}/download")
def download_file(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重定向到 OSS 签名下载 URL。"""
    from fastapi.responses import RedirectResponse

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN:
        if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")
    if not entry.oss_key:
        raise HTTPException(404, "此知识条目没有关联的原始文件")

    from app.services.oss_service import generate_signed_url
    url = generate_signed_url(entry.oss_key, expires=3600)
    return RedirectResponse(url=url)


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
    result["content_html"] = entry.content_html  # HTML for cloud doc editor
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

    # 找到或创建对应的审批记录（统一审批流）
    from app.models.permission import (
        ApprovalAction as ApprovalActionModel,
        ApprovalActionType,
        ApprovalRequest,
        ApprovalRequestType,
        ApprovalStatus,
    )
    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.target_id == kid,
            ApprovalRequest.target_type == "knowledge",
            ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_REVIEW,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .first()
    )
    if not approval:
        approval = ApprovalRequest(
            request_type=ApprovalRequestType.KNOWLEDGE_REVIEW,
            target_id=kid,
            target_type="knowledge",
            requester_id=entry.created_by,
            status=ApprovalStatus.PENDING,
            stage="dept_pending",
        )
        db.add(approval)
        db.flush()

    if req.action == "approve":
        entry = approve_knowledge(db, kid, user.id, req.note)
        if entry.review_stage == ReviewStage.APPROVED:
            approval.status = ApprovalStatus.APPROVED
        else:
            approval.stage = "super_pending"
        db.add(ApprovalActionModel(
            request_id=approval.id, actor_id=user.id,
            action=ApprovalActionType.APPROVE, comment=req.note or None,
        ))
    elif req.action == "reject":
        entry = reject_knowledge(db, kid, user.id, req.note)
        approval.status = ApprovalStatus.REJECTED
        db.add(ApprovalActionModel(
            request_id=approval.id, actor_id=user.id,
            action=ApprovalActionType.REJECT, comment=req.note or None,
        ))
    else:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    db.commit()
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

    from app.models.permission import (
        ApprovalAction as ApprovalActionModel,
        ApprovalActionType,
        ApprovalRequest,
        ApprovalRequestType,
        ApprovalStatus,
    )
    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.target_id == kid,
            ApprovalRequest.target_type == "knowledge",
            ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_REVIEW,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .first()
    )

    if req.action == "approve":
        try:
            entry = super_approve_knowledge(db, kid, user.id, req.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if approval:
            approval.status = ApprovalStatus.APPROVED
            db.add(ApprovalActionModel(
                request_id=approval.id, actor_id=user.id,
                action=ApprovalActionType.APPROVE, comment=req.note or None,
            ))
    elif req.action == "reject":
        entry = super_reject_knowledge(db, kid, user.id, req.note)
        if approval:
            approval.status = ApprovalStatus.REJECTED
            db.add(ApprovalActionModel(
                request_id=approval.id, actor_id=user.id,
                action=ApprovalActionType.REJECT, comment=req.note or None,
            ))
    else:
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    db.commit()
    return _entry_dict(entry)


def can_edit_entry(entry: KnowledgeEntry, user: User, db: Session) -> bool:
    """检查用户是否有编辑权限：创建者/super_admin/被授权者。"""
    if entry.created_by == user.id:
        return True
    if user.role == Role.SUPER_ADMIN:
        return True
    grant = db.query(KnowledgeEditGrant).filter_by(
        entry_id=entry.id, user_id=user.id
    ).first()
    return grant is not None


class EntryUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    content_html: Optional[str] = None


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
    if not can_edit_entry(entry, user, db):
        raise HTTPException(403, "无编辑权限，请先向文档创建者申请")
    if req.title is not None:
        entry.title = req.title
    if req.content is not None:
        entry.content = req.content
    if req.content_html is not None:
        entry.content_html = req.content_html
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

    # 清理 OSS 文件
    if entry.oss_key:
        try:
            from app.services.oss_service import delete_file as oss_delete
            oss_delete(entry.oss_key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to delete OSS file {entry.oss_key}: {e}")

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
        _cfg = llm_gateway.resolve_config(db, "knowledge.search")
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


# ── 云文档转换 & 同步 ─────────────────────────────────────────────────


@router.post("/{kid}/render")
def retry_render(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动重试云文档转换（创建 render retry job）。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
        raise HTTPException(403, "无权操作")

    from app.models.knowledge_job import KnowledgeJob
    job = KnowledgeJob(
        knowledge_id=kid,
        job_type="render",
        trigger_source="retry",
    )
    db.add(job)
    entry.doc_render_status = "pending"
    entry.doc_render_error = None
    db.commit()
    return {"ok": True, "job_id": job.id, "status": "queued"}


@router.post("/{kid}/classify")
def retry_classify(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动重试自动分类（创建 classify retry job）。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
        raise HTTPException(403, "无权操作")

    from app.models.knowledge_job import KnowledgeJob
    job = KnowledgeJob(
        knowledge_id=kid,
        job_type="classify",
        trigger_source="retry",
    )
    db.add(job)
    entry.classification_status = "pending"
    entry.classification_error = None
    db.commit()
    return {"ok": True, "job_id": job.id, "status": "queued"}


@router.post("/{kid}/sync")
async def manual_sync(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发飞书文档同步，仅对 source_type=lark_doc 有效。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.source_type != "lark_doc":
        raise HTTPException(400, "此知识条目不是飞书文档，无法同步")
    if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
        raise HTTPException(403, "无权操作")

    from app.services.lark_doc_importer import lark_doc_importer

    # 标记同步中
    entry.sync_status = "syncing"
    entry.sync_error = None
    db.commit()

    try:
        result = await lark_doc_importer.sync_doc(db, entry)
        entry.sync_status = "ok"
        entry.sync_error = None
        db.commit()
        return result
    except Exception as e:
        entry.sync_status = "error"
        entry.sync_error = str(e)[:500]
        db.commit()
        raise HTTPException(502, f"飞书同步失败: {e}")


# ── 飞书文档导入 ──────────────────────────────────────────────────────


class LarkImportRequest(BaseModel):
    url: str
    title: Optional[str] = None
    folder_id: Optional[int] = None
    sync_interval: int = 0
    category: str = "experience"


class LarkBatchImportRequest(BaseModel):
    urls: list[str]
    folder_id: Optional[int] = None
    sync_interval: int = 0
    category: str = "experience"


@router.post("/import-from-lark")
async def import_from_lark(
    req: LarkImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从飞书文档链接导入为知识库云文档。"""
    from app.services.lark_doc_importer import lark_doc_importer

    try:
        entry = await lark_doc_importer.import_doc(
            db=db,
            user=user,
            url=req.url,
            title=req.title,
            folder_id=req.folder_id,
            category=req.category,
            sync_interval=req.sync_interval,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, f"飞书 API 调用失败: {e}")

    return {
        "id": entry.id,
        "title": entry.title,
        "ai_title": entry.ai_title,
        "ai_summary": entry.ai_summary,
        "status": entry.status.value if entry.status else "pending",
        "oss_key": entry.oss_key,
        "lark_doc_token": entry.lark_doc_token,
        "sync_interval": entry.lark_sync_interval,
        "source_type": entry.source_type,
    }


@router.post("/import-from-lark/batch")
async def batch_import_from_lark(
    req: LarkBatchImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量从飞书文档链接导入。逐个导入，返回每个的结果。"""
    from app.services.lark_doc_importer import lark_doc_importer

    results = []
    for url in req.urls:
        try:
            entry = await lark_doc_importer.import_doc(
                db=db,
                user=user,
                url=url,
                folder_id=req.folder_id,
                category=req.category,
                sync_interval=req.sync_interval,
            )
            results.append({
                "url": url,
                "ok": True,
                "id": entry.id,
                "title": entry.title,
            })
        except Exception as e:
            results.append({
                "url": url,
                "ok": False,
                "error": str(e),
            })

    return {"total": len(req.urls), "results": results}


# ── 文档编辑权限 ──────────────────────────────────────────────────────


@router.get("/{kid}/edit-permission")
def check_edit_permission(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """检查当前用户是否有编辑权限，以及是否已有待审批的申请。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    has_permission = can_edit_entry(entry, user, db)

    # 检查是否有 pending 的编辑权限申请
    pending_request = None
    if not has_permission:
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        pending = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_EDIT,
                ApprovalRequest.target_id == kid,
                ApprovalRequest.target_type == "knowledge",
                ApprovalRequest.requester_id == user.id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .first()
        )
        if pending:
            pending_request = {"id": pending.id, "created_at": pending.created_at.isoformat() if pending.created_at else None}

    return {
        "can_edit": has_permission,
        "is_owner": entry.created_by == user.id,
        "pending_request": pending_request,
    }


@router.post("/{kid}/request-edit")
def request_edit_permission(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """申请某文档的编辑权限。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    if can_edit_entry(entry, user, db):
        raise HTTPException(400, "您已有编辑权限")

    # 检查是否已有 pending 申请
    from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
    existing = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.request_type == ApprovalRequestType.KNOWLEDGE_EDIT,
            ApprovalRequest.target_id == kid,
            ApprovalRequest.target_type == "knowledge",
            ApprovalRequest.requester_id == user.id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "已有待审批的申请")

    r = ApprovalRequest(
        request_type=ApprovalRequestType.KNOWLEDGE_EDIT,
        target_id=kid,
        target_type="knowledge",
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
        stage="owner_pending",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "status": "pending"}


@router.get("/{kid}/edit-grants")
def list_edit_grants(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出文档的所有编辑权限授权。只有创建者和 super_admin 可查看。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.created_by != user.id and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "只有文档创建者可以管理编辑权限")

    grants = db.query(KnowledgeEditGrant).filter_by(entry_id=kid).all()
    return [
        {
            "id": g.id,
            "user_id": g.user_id,
            "user_name": g.user.display_name if g.user else None,
            "granted_by": g.granted_by,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in grants
    ]


@router.delete("/{kid}/edit-grants/{uid}")
def revoke_edit_grant(
    kid: int,
    uid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销某用户的编辑权限。只有创建者和 super_admin 可操作。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.created_by != user.id and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "只有文档创建者可以撤销编辑权限")

    grant = db.query(KnowledgeEditGrant).filter_by(entry_id=kid, user_id=uid).first()
    if not grant:
        raise HTTPException(404, "该用户没有编辑权限")

    db.delete(grant)
    db.commit()
    return {"ok": True}


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
def serve_image(filename: str, user: User = Depends(get_current_user)):
    """Serve an uploaded inline image."""
    from fastapi.responses import FileResponse
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    image_dir = os.path.join(settings.UPLOAD_DIR, "images")
    path = os.path.join(image_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Image not found")
    return FileResponse(path)
