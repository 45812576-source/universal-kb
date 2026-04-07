import asyncio
import json
import datetime
import os
import re
import secrets
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.knowledge import KnowledgeEntry, KnowledgeEditGrant, KnowledgeFolder, KnowledgeStatus, ReviewStage
from app.models.knowledge_share import KnowledgeShareLink
from app.models.user import Role, User
from app.services.knowledge_service import (
    approve_knowledge,
    reject_knowledge,
    submit_knowledge,
    super_approve_knowledge,
    super_reject_knowledge,
)
from app.utils.time_utils import utcnow
from app.utils.file_parser import extract_text

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _has_table(db: Session, table_name: str) -> bool:
    try:
        return inspect(db.bind).has_table(table_name)
    except Exception:
        return False


# ── 标题清洗 ─────────────────────────────────────────────────────────────────

def _repair_mojibake(raw: str) -> str:
    """尽量修复常见 UTF-8/latin1/gbk 链路导致的中文乱码。"""
    if not raw:
        return raw

    candidates = [raw]

    cur = raw
    for _ in range(4):
        try:
            nxt = cur.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            break
        if not nxt or nxt == cur:
            break
        candidates.append(nxt)
        cur = nxt

    try:
        cp = raw.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore")
        if cp:
            candidates.append(cp)
    except Exception:
        pass

    def _score(text: str) -> tuple[int, int, int]:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        bad = sum(text.count(mark) for mark in ("Ã", "Â", "å", "æ", "ä", "�", "盲", "潞", "聥", "聳", "莽", "禄", "脙", "陇"))
        controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
        return (bad + controls, -cjk, len(text))

    return min(candidates, key=_score)


def _sanitize_title(raw: str) -> str:
    """清洗文件名 / 标题：修复编码、去控制字符、去扩展名。"""
    if not raw:
        return "未命名文档"
    raw = _repair_mojibake(raw)
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    name, ext = os.path.splitext(raw)
    if ext and len(ext) <= 6:
        raw = name
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or "未命名文档"


def _display_title(entry: KnowledgeEntry, understanding: dict | None = None) -> str:
    """统一前端/接口展示标题——唯一主口径。

    优先级：
    1. 用户显式标题（entry.title 且非文件名原值）
    2. understanding profile 的 display_title
    3. AI 标题（entry.ai_title）
    4. 清洗后文件名
    5. "未命名文档"
    """
    # 1. 用户显式标题
    user_title = _sanitize_title(entry.title or "")
    source_file_cleaned = _sanitize_title(entry.source_file or "")
    if user_title and user_title != "未命名文档" and user_title != source_file_cleaned:
        return user_title

    # 2. understanding profile display_title
    if understanding:
        ud_title = understanding.get("understanding_display_title") or ""
        if ud_title and ud_title != "未命名文档":
            return ud_title

    # 3. AI 标题
    ai = _sanitize_title(entry.ai_title or "")
    if ai and ai != "未命名文档":
        return ai

    # 4. 清洗后文件名
    if source_file_cleaned and source_file_cleaned != "未命名文档":
        return source_file_cleaned

    # 5. fallback
    return "未命名文档"


# ── "我的知识" 个人根目录 ────────────────────────────────────────────────────

def _ensure_personal_root(db: Session, user: "User") -> KnowledgeFolder:
    """确保用户存在名为 '我的知识' 的个人根目录，返回该 folder。幂等。"""
    existing = (
        db.query(KnowledgeFolder)
        .filter(
            KnowledgeFolder.created_by == user.id,
            KnowledgeFolder.name == "我的知识",
            KnowledgeFolder.parent_id.is_(None),
            KnowledgeFolder.is_system == 0,
        )
        .first()
    )
    if existing:
        return existing
    folder = KnowledgeFolder(
        name="我的知识",
        parent_id=None,
        created_by=user.id,
        department_id=user.department_id,
        sort_order=-1,  # 排在最前
    )
    db.add(folder)
    db.flush()
    return folder


class KnowledgeCreate(BaseModel):
    title: str = "未命名文档"
    content: str = ""
    category: str = "experience"
    industry_tags: list[str] = []
    platform_tags: list[str] = []
    topic_tags: list[str] = []
    folder_id: int | None = None


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


def _get_understanding_profile(db: Session, knowledge_id: int) -> dict:
    """获取文档理解 profile 字段（用于 API 返回）。"""
    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
    try:
        profile = (
            db.query(KnowledgeUnderstandingProfile)
            .filter(KnowledgeUnderstandingProfile.knowledge_id == knowledge_id)
            .first()
        )
    except Exception:
        return {}
    if not profile:
        return {}
    return {
        "understanding_display_title": profile.display_title,
        "understanding_document_type": profile.document_type,
        "understanding_permission_domain": profile.permission_domain,
        "understanding_desensitization_level": profile.desensitization_level,
        "understanding_contains_sensitive_data": profile.contains_sensitive_data,
        "understanding_content_tags": profile.content_tags,
        "understanding_summary_short": profile.summary_short,
        "understanding_summary_search": profile.summary_search,
        "understanding_status": profile.understanding_status,
        "understanding_data_type_hits": profile.data_type_hits,
        "understanding_visibility_recommendation": profile.visibility_recommendation,
        "understanding_suggested_tags": profile.suggested_tags,
        "understanding_title_confidence": profile.title_confidence,
        "understanding_title_source": profile.title_source,
        "understanding_summary_sensitivity_mode": profile.summary_sensitivity_mode,
    }


def _entry_dict(e: KnowledgeEntry, folder_name_map: dict[int, str] | None = None, db: Session | None = None) -> dict:
    ext = (e.file_ext or "").lower()
    creator_department = getattr(getattr(e, "creator", None), "department", None)
    folder_obj = getattr(e, "folder", None)
    _folder_name = None
    _folder_missing = False
    _is_in_my_knowledge = False
    if e.folder_id and folder_name_map:
        _folder_name = folder_name_map.get(e.folder_id)
        _folder_missing = _folder_name is None
        if _folder_name == "我的知识":
            _is_in_my_knowledge = True
    elif e.folder_id:
        _folder_missing = True

    # 文档理解 profile
    understanding = _get_understanding_profile(db, e.id) if db else {}

    result = {
        "id": e.id,
        "title": _display_title(e, understanding),
        "raw_title": e.title,
        "content": e.content[:300] + ("..." if len(e.content) > 300 else ""),
        "category": e.category,
        "status": e.status.value,
        "department_id": e.department_id,
        "business_unit": getattr(creator_department, "business_unit", None),
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
        "folder_name": _folder_name or ("系统待整理" if _folder_missing else None),
        "folder_missing": _folder_missing,
        "is_in_my_knowledge": _is_in_my_knowledge,
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
        # 云文档渲染状态（无文件的手动文档不需要渲染，修正历史 pending 数据）
        "doc_render_status": None if (e.doc_render_status == "pending" and not e.oss_key) else e.doc_render_status,
        "doc_render_error": e.doc_render_error,
        "doc_render_mode": e.doc_render_mode,
        # AI 结构化笔记
        "ai_notes_status": e.ai_notes_status,
        "ai_notes_error": e.ai_notes_error,
        # 来源与同步
        "source_uri": e.source_uri,
        "sync_status": e.sync_status,
        "sync_error": e.sync_error,
        "lark_doc_url": e.lark_doc_url,
        "lark_doc_token": e.lark_doc_token,
        "lark_sync_interval": e.lark_sync_interval,
        "lark_last_synced_at": e.lark_last_synced_at,
        "external_edit_mode": e.external_edit_mode,
        "source_origin_label": "飞书" if e.source_type == "lark_doc" else None,
        "can_refresh_from_source": bool(e.source_type == "lark_doc" and e.lark_doc_url),
        # 分类状态
        "classification_status": e.classification_status,
        "classification_error": e.classification_error,
        "classification_confidence": e.classification_confidence,
        "classification_source": e.classification_source,
        "classified_at": e.classified_at.isoformat() if e.classified_at else None,
        "visibility_scope": _knowledge_visibility_scope(e),
        "folder_business_unit": getattr(folder_obj, "business_unit", None),
        # 能力标志
        "can_open_onlyoffice": bool(e.oss_key and (ext in _ONLYOFFICE_EXTS or (ext == ".pdf" and e.docx_oss_key))),
        "can_retry_render": e.doc_render_status in ("failed", "pending", None),
        "can_retry_classification": e.classification_status in ("failed", "pending", "needs_review", None),
        "created_at": e.created_at.isoformat(),
    }
    result.update(understanding)
    return result


def _knowledge_visibility_scope(e: KnowledgeEntry) -> dict:
    if e.status == KnowledgeStatus.APPROVED:
        return {
            "scope": "approved_global",
            "reason": "approved",
        }
    return {
        "scope": "owner_or_dept_only",
        "reason": "pending_or_unapproved",
        "owner_id": e.created_by,
        "department_id": e.department_id,
    }


def _apply_knowledge_visibility(query, user: User):
    """整理视图可见性：每个人只管自己的文件。

    所有角色（包括 SUPER_ADMIN）都只看到自己创建的 + 已审批通过的文档。
    管理员查看全局数据请走 knowledge_admin / knowledge_governance 路由。
    """
    if user.role == Role.DEPT_ADMIN:
        return query.filter(
            or_(
                KnowledgeEntry.created_by == user.id,
                KnowledgeEntry.department_id == user.department_id,
                KnowledgeEntry.status == KnowledgeStatus.APPROVED,
            )
        )
    # SUPER_ADMIN / EMPLOYEE 统一：只看自己的 + 已审批的
    return query.filter(
        or_(
            KnowledgeEntry.created_by == user.id,
            KnowledgeEntry.status == KnowledgeStatus.APPROVED,
        )
    )


def _can_view_entry(entry: KnowledgeEntry, user: User) -> bool:
    if user.role == Role.SUPER_ADMIN:
        return True
    if user.role == Role.EMPLOYEE:
        return entry.created_by == user.id or entry.status == KnowledgeStatus.APPROVED
    if user.role == Role.DEPT_ADMIN:
        return (
            entry.created_by == user.id
            or entry.department_id == user.department_id
            or entry.status == KnowledgeStatus.APPROVED
        )
    return False


def _can_manage_share(entry: KnowledgeEntry, user: User) -> bool:
    if user.role == Role.SUPER_ADMIN:
        return True
    if entry.created_by == user.id:
        return True
    if user.role == Role.DEPT_ADMIN and user.department_id and entry.department_id == user.department_id:
        return True
    return False


def _share_url(token: str) -> str:
    public_base = (os.getenv("PUBLIC_WEB_BASE_URL") or os.getenv("FRONTEND_ORIGIN") or "").strip().rstrip("/")
    if public_base:
        return f"{public_base}/s/knowledge/{token}"
    return f"/s/knowledge/{token}"


def _share_dict(share: KnowledgeShareLink) -> dict:
    return {
        "id": share.id,
        "share_token": share.share_token,
        "share_url": _share_url(share.share_token),
        "is_active": bool(share.is_active),
        "access_scope": share.access_scope,
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "last_accessed_at": share.last_accessed_at.isoformat() if share.last_accessed_at else None,
        "access_count": share.access_count or 0,
    }


@router.post("")
def create_knowledge(
    req: KnowledgeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 自动归入"我的知识"
    folder_id = req.folder_id
    if folder_id is None:
        personal_root = _ensure_personal_root(db, user)
        folder_id = personal_root.id

    entry = KnowledgeEntry(
        title=_sanitize_title(req.title) if req.title else "未命名文档",
        content=req.content,
        category=req.category,
        industry_tags=req.industry_tags,
        platform_tags=req.platform_tags,
        topic_tags=req.topic_tags,
        created_by=user.id,
        department_id=user.department_id,
        source_type="manual",
        capture_mode="manual_form",
        folder_id=folder_id,
        doc_render_status=None,
    )
    db.add(entry)
    db.flush()
    entry = submit_knowledge(db, entry)
    db.commit()

    folder_name = None
    if entry.folder_id:
        f = db.get(KnowledgeFolder, entry.folder_id)
        if f:
            folder_name = f.name

    return {
        "id": entry.id,
        "title": entry.title,
        "status": entry.status.value,
        "review_level": entry.review_level,
        "folder_id": entry.folder_id,
        "folder_name": folder_name,
        "doc_render_status": entry.doc_render_status,
    }


async def _bg_post_upload(
    entry_id: int,
    content: str,
    filename: str,
    file_type: str,
    saved_path: str,
    session_factory=None,
    skip_pipeline: bool = False,
):
    """后台执行文档理解流水线 + 文档渲染 + 清理，不阻塞上传响应。"""
    import logging
    _logger = logging.getLogger(__name__)
    if skip_pipeline:
        try:
            os.unlink(saved_path)
        except OSError:
            pass
        return
    if session_factory is None:
        bind = getattr(getattr(saved_path, "bind", None), "bind", None)
        from app.database import SessionLocal
        session_factory = SessionLocal
    bg_db = session_factory()
    try:
        entry = bg_db.get(KnowledgeEntry, entry_id)
        if not entry:
            return

        # 文档渲染
        try:
            from app.services.doc_renderer import render_from_path
            render_from_path(bg_db, entry, saved_path)
        except Exception as e:
            _logger.warning(f"Doc render failed (will retry via job): {e}")
            entry.doc_render_error = str(e)[:500]

        # ── 统一文档理解流水线（替代原 AI 命名）──────────────────────────
        try:
            from app.services.knowledge_understanding import understand_document
            profile = await understand_document(
                knowledge_id=entry.id,
                content=content,
                filename=filename,
                file_type=file_type,
                db=bg_db,
            )
            # 向后兼容：同步更新主表的 ai_title/ai_summary/ai_tags/quality_score
            if profile.display_title:
                entry.ai_title = profile.display_title
            if profile.summary_short:
                entry.ai_summary = profile.summary_short
            if profile.content_tags:
                tags = profile.content_tags
                entry.ai_tags = {
                    "industry": [tags.get("industry_or_domain_tag", "")] if tags.get("industry_or_domain_tag") else [],
                    "platform": [],
                    "topic": [tags.get("scenario_tag", "")] if tags.get("scenario_tag") else [],
                }
                if tags.get("industry_or_domain_tag"):
                    entry.industry_tags = list(set((entry.industry_tags or []) + [tags["industry_or_domain_tag"]]))
                if tags.get("scenario_tag"):
                    entry.topic_tags = list(set((entry.topic_tags or []) + [tags["scenario_tag"]]))
        except Exception as e:
            _logger.warning(f"Document understanding failed, falling back to AI naming: {e}")
            # 降级到原 AI 命名
            try:
                from app.services.knowledge_namer import auto_name
                naming_result = await auto_name(content, filename, file_type, db=bg_db)
                entry.ai_title = naming_result["title"]
                entry.ai_summary = naming_result["summary"]
                entry.ai_tags = naming_result["tags"]
                entry.quality_score = naming_result["quality_score"]
                if naming_result["tags"].get("industry"):
                    entry.industry_tags = naming_result["tags"]["industry"]
                if naming_result["tags"].get("platform"):
                    entry.platform_tags = naming_result["tags"]["platform"]
                if naming_result["tags"].get("topic"):
                    entry.topic_tags = naming_result["tags"]["topic"]
            except Exception as e2:
                _logger.warning(f"AI naming fallback also failed: {e2}")

        bg_db.commit()
    except Exception as e:
        bg_db.rollback()
        _logger.warning(f"bg_post_upload failed: {e}")
    finally:
        bg_db.close()
        # 清理本地临时文件
        try:
            os.unlink(saved_path)
        except OSError:
            pass


def _create_entry_from_file(
    db: Session, saved_path: str, filename: str, file_data: bytes,
    category: str, industry_tags: str, platform_tags: str, topic_tags: str,
    user: "User", folder_id: int | None, explicit_title: str | None = None,
) -> tuple["KnowledgeEntry", str, str]:
    """从单个文件创建 KnowledgeEntry，返回 (entry, content, file_type)。
    explicit_title: 前端显式传入的标题，优先级最高。
    """
    import mimetypes, logging as _logging
    _log = _logging.getLogger(__name__)
    ext = os.path.splitext(filename)[1].lower()

    try:
        content = extract_text(saved_path)
    except Exception as _e:
        _log.warning(f"extract_text failed for {filename}: {_e}")
        content = ""  # 文本抽取失败不阻断上传，entry 仍创建

    # OSS
    oss_key = None
    file_size = len(file_data)
    file_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        oss_key = generate_oss_key(ext)
        oss_upload(saved_path, oss_key)
    except Exception:
        pass

    from app.services.review_policy import review_policy
    sensitive_flags = review_policy.detect_sensitive(content)
    strategic_flags = review_policy.detect_strategic(content)
    capture_mode = "upload" if (sensitive_flags or strategic_flags) else "upload_ai_clean"

    # 标题优先级：显式传入 > 清洗后文件名 > 原始文件名
    display_title = explicit_title or _sanitize_title(filename) or filename

    # 自动归入"我的知识"
    effective_folder = folder_id
    if effective_folder is None:
        personal_root = _ensure_personal_root(db, user)
        effective_folder = personal_root.id

    # 生成 fallback HTML，确保 entry 创建时即有可渲染内容
    fallback_html = None
    if content:
        from app.services.doc_renderer import render_from_content
        fallback_html = render_from_content(content, ext)

    entry = KnowledgeEntry(
        title=display_title,
        content=content,
        content_html=fallback_html,
        category=category,
        industry_tags=json.loads(industry_tags),
        platform_tags=json.loads(platform_tags),
        topic_tags=json.loads(topic_tags),
        created_by=user.id,
        department_id=user.department_id,
        source_type="upload",
        source_file=filename,
        capture_mode=capture_mode,
        oss_key=oss_key, file_type=file_type, file_ext=ext, file_size=file_size,
        doc_render_status="pending",
        folder_id=effective_folder,
    )
    db.add(entry)
    db.flush()

    entry = submit_knowledge(db, entry)

    if _has_table(db, "knowledge_jobs"):
        from app.models.knowledge_job import KnowledgeJob
        if entry.doc_render_status in ("failed", "pending"):
            db.add(KnowledgeJob(knowledge_id=entry.id, job_type="render", trigger_source="upload"))
        db.add(KnowledgeJob(knowledge_id=entry.id, job_type="classify", trigger_source="upload"))
        db.add(KnowledgeJob(knowledge_id=entry.id, job_type="understand", trigger_source="upload"))
        db.add(KnowledgeJob(knowledge_id=entry.id, job_type="ai_notes", trigger_source="upload"))
        db.add(KnowledgeJob(knowledge_id=entry.id, job_type="governance_classify", trigger_source="upload"))
    entry.classification_status = "pending"
    entry.ai_notes_status = "pending"

    return entry, content, file_type


@router.post("/upload")
async def upload_knowledge(
    title: str = Form(...),
    category: str = Form("experience"),
    industry_tags: str = Form("[]"),
    platform_tags: str = Form("[]"),
    topic_tags: str = Form("[]"),
    folder_id: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower()

    file_data = await file.read()

    # ── ZIP 解压：逐文件入库 ──
    if ext == ".zip":
        import zipfile, io, tempfile
        results = []
        try:
            zf = zipfile.ZipFile(io.BytesIO(file_data))
        except zipfile.BadZipFile:
            raise HTTPException(400, "无效的 ZIP 文件")

        for info in zf.infolist():
            if info.is_dir():
                continue
            inner_name = os.path.basename(info.filename)
            if not inner_name or inner_name.startswith("."):
                continue
            inner_ext = os.path.splitext(inner_name)[1].lower()
            if inner_ext not in (".txt", ".pdf", ".docx", ".pptx", ".md", ".xlsx", ".xls", ".csv",
                                  ".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                continue

            inner_data = zf.read(info.filename)
            inner_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{inner_ext}")
            with open(inner_path, "wb") as f:
                f.write(inner_data)

            try:
                entry, content, file_type = _create_entry_from_file(
                    db, inner_path, inner_name, inner_data,
                    category, industry_tags, platform_tags, topic_tags, user, folder_id,
                    explicit_title=_sanitize_title(inner_name),
                )
                db.commit()
                # 后台 AI naming + 渲染
                bind = db.get_bind()
                session_factory = sessionmaker(autocommit=False, autoflush=False, bind=bind)
                asyncio.create_task(
                    _bg_post_upload(
                        entry.id,
                        content,
                        inner_name,
                        file_type,
                        inner_path,
                        session_factory=session_factory,
                        skip_pipeline=bind.dialect.name == "sqlite",
                    )
                )
                results.append({"id": entry.id, "name": inner_name, "title": entry.title, "folder_id": entry.folder_id})
            except ValueError as e:
                try:
                    os.unlink(inner_path)
                except OSError:
                    pass
                results.append({"id": None, "name": inner_name, "error": str(e)})

        zf.close()
        return {"zip": True, "results": results}

    # ── 普通单文件上传 ──
    local_filename = f"{uuid.uuid4()}{ext}"
    saved_path = os.path.join(settings.UPLOAD_DIR, local_filename)
    with open(saved_path, "wb") as f:
        f.write(file_data)

    # title 显式传入时优先使用；否则 _create_entry_from_file 内部会从文件名清洗
    raw_filename = file.filename or "unknown"
    explicit_title = _sanitize_title(title) if title and title != raw_filename else None

    try:
        entry, content, file_type = _create_entry_from_file(
            db, saved_path, raw_filename, file_data,
            category, industry_tags, platform_tags, topic_tags, user, folder_id,
            explicit_title=explicit_title,
        )
    except ValueError as e:
        os.unlink(saved_path)
        raise HTTPException(400, str(e))

    # 尽量在请求内完成一次同步转换，避免用户长时间看到 pending。
    try:
        from app.services.doc_renderer import render_from_path
        render_from_path(db, entry, saved_path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Sync render failed for entry {entry.id}: {e}")
        entry.doc_render_error = str(e)[:500]

    db.commit()

    # 后台执行 AI naming + 文档渲染（不阻塞响应）
    bind = db.get_bind()
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=bind)
    asyncio.create_task(
        _bg_post_upload(
            entry.id,
            content,
            raw_filename,
            file_type,
            saved_path,
            session_factory=session_factory,
            skip_pipeline=bind.dialect.name == "sqlite",
        )
    )

    # 查询 folder_name
    folder_name = None
    if entry.folder_id:
        _f = db.get(KnowledgeFolder, entry.folder_id)
        if _f:
            folder_name = _f.name

    return {
        "id": entry.id,
        "title": entry.title,
        "source_file": entry.source_file,
        "status": entry.status.value,
        "content_length": len(content),
        "review_level": entry.review_level,
        "capture_mode": entry.capture_mode,
        "folder_id": entry.folder_id,
        "folder_name": folder_name,
        "taxonomy_code": entry.taxonomy_code,
        "taxonomy_board": entry.taxonomy_board,
        "classification_confidence": entry.classification_confidence,
        "oss_key": entry.oss_key,
        "file_type": entry.file_type,
        "file_ext": entry.file_ext,
        "doc_render_status": entry.doc_render_status,
        "doc_render_error": entry.doc_render_error,
        "doc_render_mode": entry.doc_render_mode,
        "has_editable_fallback": bool(entry.content or entry.content_html),
        "render_pending": entry.doc_render_status in ("pending", "processing"),
        "content_available": bool(entry.content),
    }




@router.get("")
def list_knowledge(
    status: str = None,
    category: str = None,
    source_type: str = None,
    review_stage: str = None,
    doc_render_status: str = None,
    classification_status: str = None,
    unfiled: bool = False,
    owner_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if owner_only:
        q = db.query(KnowledgeEntry).filter(KnowledgeEntry.created_by == user.id)
    else:
        q = _apply_knowledge_visibility(db.query(KnowledgeEntry), user)

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
    if unfiled:
        q = q.filter(KnowledgeEntry.folder_id.is_(None))

    entries = q.order_by(KnowledgeEntry.created_at.desc()).all()

    # 构建 folder_id -> name 映射，让 _entry_dict 返回 folder_name
    folder_ids = {e.folder_id for e in entries if e.folder_id}
    folder_name_map: dict[int, str] = {}
    if folder_ids:
        folder_rows = db.query(KnowledgeFolder.id, KnowledgeFolder.name).filter(
            KnowledgeFolder.id.in_(folder_ids)
        ).all()
        folder_name_map = {r.id: r.name for r in folder_rows}

    return [_entry_dict(e, folder_name_map, db=db) for e in entries]


def _enrich_search_results_with_blocks(db: Session, best: dict) -> None:
    """为搜索命中结果补充 block 映射信息。"""
    try:
        from app.models.knowledge_block import KnowledgeChunkMapping
        kid_chunk_pairs = [(v["knowledge_id"], v["chunk_index"]) for v in best.values()]
        if not kid_chunk_pairs:
            return
        all_kids = list({p[0] for p in kid_chunk_pairs})
        mappings = (
            db.query(KnowledgeChunkMapping)
            .filter(KnowledgeChunkMapping.knowledge_id.in_(all_kids))
            .all()
        )
        mapping_index = {}
        for m in mappings:
            mapping_index[(m.knowledge_id, m.chunk_index)] = m
        for kid, result in best.items():
            m = mapping_index.get((kid, result["chunk_index"]))
            if m:
                result["block_id"] = m.block_id
                result["block_key"] = m.block_key
                result["heading_path"] = None
                result["char_range"] = [m.char_start_in_block, m.char_end_in_block]
                if m.block:
                    result["heading_path"] = m.block.heading_path
    except Exception:
        pass  # 降级：无 block 信息也不影响搜索


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
                            # block 映射（向后兼容：无映射时为 None）
                            "block_id": None,
                            "block_key": None,
                            "heading_path": None,
                            "char_range": None,
                        }
            # 补充 block 映射信息
            _enrich_search_results_with_blocks(db, best)
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
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not _can_view_entry(entry, user):
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
    if not _can_view_entry(entry, user):
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
    if not _can_view_entry(entry, user):
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
        "is_system": bool(f.is_system),
        "taxonomy_board": f.taxonomy_board,
        "taxonomy_code": f.taxonomy_code,
        "created_at": f.created_at.isoformat(),
    }


@router.get("/folders")
def list_folders(
    owner_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回文件夹列表（扁平列表，前端自行构建树）。

    规则：
    - owner_only=true：只返回当前用户自己创建的文件夹（"我的知识"视图）
    - owner_only=false（默认）：用户自建 + 系统归档树 + 可见文档所在文件夹
    """
    if owner_only:
        folders = (
            db.query(KnowledgeFolder)
            .filter(KnowledgeFolder.created_by == user.id)
            .order_by(KnowledgeFolder.sort_order, KnowledgeFolder.id)
            .all()
        )
        return [_folder_dict(f) for f in folders]

    visible_folder_ids = {
        folder_id
        for (folder_id,) in _apply_knowledge_visibility(
            db.query(KnowledgeEntry.folder_id).filter(KnowledgeEntry.folder_id.isnot(None)),
            user,
        ).all()
        if folder_id is not None
    }

    folder_filter = (KnowledgeFolder.created_by == user.id) | (KnowledgeFolder.is_system == 1)
    if visible_folder_ids:
        folder_filter = folder_filter | (KnowledgeFolder.id.in_(visible_folder_ids))

    folders = (
        db.query(KnowledgeFolder)
        .filter(folder_filter)
        .order_by(
            KnowledgeFolder.is_system.desc(),
            KnowledgeFolder.sort_order,
            KnowledgeFolder.id,
        )
        .all()
    )
    return [_folder_dict(f) for f in folders]


@router.post("/ensure-my-folder")
def ensure_my_folder(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """确保当前用户存在"我的知识"个人根目录，返回 folder 信息。幂等。"""
    folder = _ensure_personal_root(db, user)
    db.commit()
    return _folder_dict(folder)


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
    if not _can_view_entry(entry, user):
        raise HTTPException(403, "Access denied")
    # 查 folder_name
    _fmap: dict[int, str] = {}
    if entry.folder_id:
        _f = db.get(KnowledgeFolder, entry.folder_id)
        if _f:
            _fmap[entry.folder_id] = _f.name
    result = _entry_dict(entry, _fmap, db=db)
    result["content"] = entry.content  # full content for detail view
    result["content_html"] = entry.content_html  # HTML for cloud doc editor
    result["ai_notes_html"] = entry.ai_notes_html  # AI 结构化笔记 HTML
    return result


@router.put("/{kid}/ai-notes")
def save_ai_notes(
    kid: int,
    ai_notes_html: str = Body(..., embed=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户编辑 AI 笔记后保存。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    entry.ai_notes_html = ai_notes_html
    db.commit()
    return {"ok": True}


@router.post("/{kid}/retry-ai-notes")
def retry_ai_notes(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重新生成 AI 笔记。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    entry.ai_notes_status = "pending"
    entry.ai_notes_error = None
    entry.ai_notes_html = None

    if _has_table(db, "knowledge_jobs"):
        from app.models.knowledge_job import KnowledgeJob
        db.add(KnowledgeJob(knowledge_id=entry.id, job_type="ai_notes", trigger_source="retry"))
    db.commit()
    return {"ok": True, "ai_notes_status": "pending"}


@router.post("/{kid}/share-links")
def create_share_link(
    kid: int,
    access_scope: str = Body("public_readonly", embed=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if access_scope not in ("public_readonly", "public_editable"):
        raise HTTPException(400, "access_scope 仅支持 public_readonly 或 public_editable")
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not _can_manage_share(entry, user):
        raise HTTPException(403, "无权分享该文档")

    existing = (
        db.query(KnowledgeShareLink)
        .filter(
            KnowledgeShareLink.knowledge_id == entry.id,
            KnowledgeShareLink.is_active.is_(True),
        )
        .order_by(KnowledgeShareLink.created_at.desc())
        .first()
    )
    if existing:
        if existing.access_scope != access_scope:
            existing.access_scope = access_scope
            db.commit()
            db.refresh(existing)
        return _share_dict(existing)

    share = KnowledgeShareLink(
        knowledge_id=entry.id,
        share_token=secrets.token_urlsafe(24),
        created_by=user.id,
        is_active=True,
        access_scope=access_scope,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return _share_dict(share)


@router.get("/{kid}/share-links")
def list_share_links(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not _can_manage_share(entry, user):
        raise HTTPException(403, "无权查看分享状态")

    shares = (
        db.query(KnowledgeShareLink)
        .filter(KnowledgeShareLink.knowledge_id == entry.id)
        .order_by(KnowledgeShareLink.created_at.desc())
        .all()
    )
    return [_share_dict(share) for share in shares]


@router.delete("/share-links/{share_id}")
def disable_share_link(
    share_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    share = db.get(KnowledgeShareLink, share_id)
    if not share:
        raise HTTPException(404, "Share link not found")
    entry = db.get(KnowledgeEntry, share.knowledge_id)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not _can_manage_share(entry, user):
        raise HTTPException(403, "无权关闭分享")

    share.is_active = False
    db.commit()
    return {"ok": True}


@router.post("/{kid}/share-links/regenerate")
def regenerate_share_link(
    kid: int,
    access_scope: str = Body("public_readonly", embed=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if access_scope not in ("public_readonly", "public_editable"):
        raise HTTPException(400, "access_scope 仅支持 public_readonly 或 public_editable")
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not _can_manage_share(entry, user):
        raise HTTPException(403, "无权重置分享")

    db.query(KnowledgeShareLink).filter(
        KnowledgeShareLink.knowledge_id == entry.id,
        KnowledgeShareLink.is_active.is_(True),
    ).update({"is_active": False})

    share = KnowledgeShareLink(
        knowledge_id=entry.id,
        share_token=secrets.token_urlsafe(24),
        created_by=user.id,
        is_active=True,
        access_scope=access_scope,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return _share_dict(share)


@router.get("/public/share/{share_token}")
def get_public_share(
    share_token: str,
    db: Session = Depends(get_db),
):
    share = db.query(KnowledgeShareLink).filter(KnowledgeShareLink.share_token == share_token).first()
    if not share or not share.is_active:
        raise HTTPException(404, "链接已失效")
    if share.expires_at and share.expires_at <= utcnow():
        raise HTTPException(410, "链接已过期")

    entry = db.get(KnowledgeEntry, share.knowledge_id)
    if not entry:
        raise HTTPException(404, "文档不存在")

    share.access_count = (share.access_count or 0) + 1
    share.last_accessed_at = utcnow()
    db.commit()

    return {
        "title": _display_title(entry),
        "content": entry.content,
        "content_html": entry.content_html,
        "source_type": entry.source_type,
        "source_origin_label": "飞书" if entry.source_type == "lark_doc" else "工作台",
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "doc_render_status": entry.doc_render_status,
        "share_meta": {
            "access_scope": share.access_scope,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        },
    }


class PublicShareSaveRequest(BaseModel):
    content_html: str


@router.put("/public/share/{share_token}")
def save_public_share(
    share_token: str,
    req: PublicShareSaveRequest,
    db: Session = Depends(get_db),
):
    share = db.query(KnowledgeShareLink).filter(KnowledgeShareLink.share_token == share_token).first()
    if not share or not share.is_active:
        raise HTTPException(404, "链接已失效")
    if share.expires_at and share.expires_at <= utcnow():
        raise HTTPException(410, "链接已过期")
    if share.access_scope != "public_editable":
        raise HTTPException(403, "该链接无编辑权限")

    entry = db.get(KnowledgeEntry, share.knowledge_id)
    if not entry:
        raise HTTPException(404, "文档不存在")

    entry.content_html = req.content_html
    # 从 HTML 中提取纯文本作为 content
    import re as _re
    entry.content = _re.sub(r"<[^>]+>", "", req.content_html).strip()
    entry.updated_at = utcnow()
    db.commit()

    return {"ok": True}


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
    return _entry_dict(entry, db=db)


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
    return _entry_dict(entry, db=db)


_SYSTEM_READONLY_SOURCE_TYPES = {"sandbox_test"}


def can_edit_entry(entry: KnowledgeEntry, user: User, db: Session) -> bool:
    """检查用户是否有编辑权限：创建者/super_admin/被授权者。
    系统自动生成的文档（如沙盒测试报告）始终只读。"""
    if entry.source_type in _SYSTEM_READONLY_SOURCE_TYPES:
        return False
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
    return _entry_dict(entry, db=db)


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


@router.post("/{kid}/understand")
def retry_understand(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动重跑文档理解流水线。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
        raise HTTPException(403, "无权操作")

    from app.models.knowledge_job import KnowledgeJob
    job = KnowledgeJob(
        knowledge_id=kid,
        job_type="understand",
        trigger_source="retry",
    )
    db.add(job)
    db.commit()
    return {"ok": True, "job_id": job.id, "status": "queued"}


class UnderstandingProfileUpdate(BaseModel):
    display_title: str | None = None
    document_type: str | None = None
    permission_domain: str | None = None
    desensitization_level: str | None = None
    content_tags: dict | None = None


@router.patch("/{kid}/understanding")
def patch_understanding_profile(
    kid: int,
    req: UnderstandingProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """人工修正文档理解 profile（修正后标记 source=manual，不被后续自动覆盖）。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if not can_edit_entry(entry, user, db):
        raise HTTPException(403, "无编辑权限")

    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
    profile = (
        db.query(KnowledgeUnderstandingProfile)
        .filter(KnowledgeUnderstandingProfile.knowledge_id == kid)
        .first()
    )
    if not profile:
        raise HTTPException(404, "该文档尚无理解 profile，请先运行理解流水线")

    import datetime
    if req.display_title is not None:
        profile.display_title = req.display_title
        profile.title_source = "user"
    if req.document_type is not None:
        profile.document_type = req.document_type
        profile.classification_source = "manual"
    if req.permission_domain is not None:
        profile.permission_domain = req.permission_domain
    if req.desensitization_level is not None:
        profile.desensitization_level = req.desensitization_level
        profile.masking_source = "manual"
    if req.content_tags is not None:
        from app.data.sensitivity_rules import validate_content_tags
        profile.content_tags = validate_content_tags(req.content_tags)
        profile.tagging_source = "manual"
    profile.updated_at = datetime.datetime.utcnow()
    db.commit()

    # 同步主表展示标题
    if req.display_title is not None:
        entry.ai_title = req.display_title
        db.commit()

    return _get_understanding_profile(db, kid)


@router.post("/{kid}/refresh-from-lark")
async def refresh_from_lark(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动用飞书最新内容覆盖工作台副本，仅对 source_type=lark_doc 有效。"""
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    if entry.source_type != "lark_doc":
        raise HTTPException(400, "此知识条目不是飞书导入副本，无法刷新")
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
        raise HTTPException(502, f"飞书刷新失败: {e}")


@router.post("/{kid}/sync")
async def manual_sync(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await refresh_from_lark(kid, db, user)


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
        "folder_id": entry.folder_id,
        "folder_name": db.get(KnowledgeFolder, entry.folder_id).name if entry.folder_id else None,
        "doc_render_status": entry.doc_render_status,
        "doc_render_mode": entry.doc_render_mode,
        "external_edit_mode": entry.external_edit_mode,
        "lark_doc_url": entry.lark_doc_url,
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
                "doc_render_status": entry.doc_render_status,
                "doc_render_mode": entry.doc_render_mode,
                "external_edit_mode": entry.external_edit_mode,
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


# ── Job 查询 ─────────────────────────────────────────────────────────


@router.get("/{kid}/jobs")
def list_jobs(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回知识条目的 render/classify job 列表（最近 20 条）。"""
    from app.models.knowledge_job import KnowledgeJob

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    jobs = (
        db.query(KnowledgeJob)
        .filter(KnowledgeJob.knowledge_id == kid)
        .order_by(KnowledgeJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": j.id,
            "job_type": j.job_type,
            "status": j.status,
            "phase": j.phase,
            "attempt_count": j.attempt_count,
            "max_attempts": j.max_attempts,
            "error_type": j.error_type,
            "error_message": j.error_message,
            "trigger_source": j.trigger_source,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]


# ── Block 信息 ───────────────────────────────────────────────────────


@router.get("/{kid}/blocks")
def list_blocks(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回知识条目的 document blocks（前端锚点定位用）。"""
    from app.models.knowledge_block import KnowledgeDocumentBlock

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    blocks = (
        db.query(KnowledgeDocumentBlock)
        .filter(KnowledgeDocumentBlock.knowledge_id == kid)
        .order_by(KnowledgeDocumentBlock.block_order)
        .all()
    )
    return [
        {
            "id": b.id,
            "block_key": b.block_key,
            "block_type": b.block_type,
            "block_order": b.block_order,
            "plain_text": b.plain_text,
            "heading_path": b.heading_path,
            "start_offset": b.start_offset,
            "end_offset": b.end_offset,
            "source_anchor": b.source_anchor,
        }
        for b in blocks
    ]


# ── 批量归档 ─────────────────────────────────────────────────────────


class BatchMoveRequest(BaseModel):
    entry_ids: list[int]
    folder_id: int


@router.post("/batch/move")
def batch_move(
    req: BatchMoveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """批量移动知识条目到指定文件夹。"""
    moved = 0
    for eid in req.entry_ids:
        entry = db.get(KnowledgeEntry, eid)
        if not entry:
            continue
        if user.role != Role.SUPER_ADMIN and entry.created_by != user.id:
            continue
        entry.folder_id = req.folder_id
        moved += 1
    db.commit()
    return {"ok": True, "moved": moved, "total": len(req.entry_ids)}


class BatchSuggestRequest(BaseModel):
    entry_ids: list[int]


@router.post("/batch/suggest-folders")
async def batch_suggest_folders(
    req: BatchSuggestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量生成归档建议。"""
    from app.services.filing_suggester import suggest_folders_batch

    suggestions = await suggest_folders_batch(db, req.entry_ids, user.id)
    return {"total": len(req.entry_ids), "suggestions": suggestions}


@router.get("/{kid}/filing-suggestion")
def get_filing_suggestion(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取单篇文档的归档建议。"""
    from app.models.knowledge_filing import KnowledgeFilingSuggestion

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    suggestion = (
        db.query(KnowledgeFilingSuggestion)
        .filter(
            KnowledgeFilingSuggestion.knowledge_id == kid,
            KnowledgeFilingSuggestion.status == "pending",
        )
        .order_by(KnowledgeFilingSuggestion.created_at.desc())
        .first()
    )

    if not suggestion:
        return {"suggestion": None}

    return {
        "suggestion": {
            "id": suggestion.id,
            "suggested_folder_id": suggestion.suggested_folder_id,
            "suggested_folder_path": suggestion.suggested_folder_path,
            "confidence": suggestion.confidence,
            "reason": suggestion.reason,
            "status": suggestion.status,
        }
    }


class AcceptSuggestionRequest(BaseModel):
    suggestion_id: int


@router.post("/{kid}/filing-suggestion/accept")
def accept_filing_suggestion(
    kid: int,
    req: AcceptSuggestionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接受归档建议，将文档移到建议的文件夹。"""
    from app.models.knowledge_filing import KnowledgeFilingSuggestion

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    suggestion = db.get(KnowledgeFilingSuggestion, req.suggestion_id)
    if not suggestion or suggestion.knowledge_id != kid:
        raise HTTPException(404, "Suggestion not found")

    entry.folder_id = suggestion.suggested_folder_id
    suggestion.status = "accepted"
    db.commit()
    return {"ok": True, "folder_id": entry.folder_id}


@router.post("/{kid}/filing-suggestion/reject")
def reject_filing_suggestion(
    kid: int,
    req: AcceptSuggestionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """拒绝归档建议。"""
    from app.models.knowledge_filing import KnowledgeFilingSuggestion

    suggestion = db.get(KnowledgeFilingSuggestion, req.suggestion_id)
    if not suggestion or suggestion.knowledge_id != kid:
        raise HTTPException(404, "Suggestion not found")

    suggestion.status = "rejected"
    db.commit()
    return {"ok": True}


# ── 自动归档治理 ─────────────────────────────────────────────────────


@router.post("/filing/auto-run")
def filing_auto_run(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """一键自动归档所有未归档文档。仅超管可操作。"""
    from app.services.auto_filer import auto_file_batch
    stats = auto_file_batch(db, user_id=user.id)
    return stats


@router.get("/filing/unfiled")
def filing_unfiled(
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取未归档文档列表。"""
    from app.services.auto_filer import get_unfiled_entries
    return get_unfiled_entries(db, limit=limit)


class UndoBatchRequest(BaseModel):
    batch_id: str


@router.post("/filing/undo")
def filing_undo(
    req: UndoBatchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """撤销一批自动归档。"""
    from app.services.auto_filer import undo_batch
    count = undo_batch(db, req.batch_id)
    return {"ok": True, "undone": count}


@router.post("/filing/undo-single/{action_id}")
def filing_undo_single(
    action_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销单条自动归档。"""
    from app.services.auto_filer import undo_single
    ok = undo_single(db, action_id)
    if not ok:
        raise HTTPException(400, "无法撤销此操作")
    return {"ok": True}


@router.get("/filing/actions")
def filing_actions(
    batch_id: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看归档操作记录。"""
    from app.services.auto_filer import get_filing_actions
    return get_filing_actions(db, batch_id=batch_id, limit=limit)


@router.get("/filing/suggestions")
def filing_suggestions(
    status: str = "pending",
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出归档建议（支持 status 过滤：pending/accepted/rejected）。"""
    from app.models.knowledge_filing import KnowledgeFilingSuggestion

    q = db.query(KnowledgeFilingSuggestion)
    if status:
        q = q.filter(KnowledgeFilingSuggestion.status == status)
    suggestions = q.order_by(KnowledgeFilingSuggestion.created_at.desc()).limit(limit).all()

    results = []
    for s in suggestions:
        entry = db.get(KnowledgeEntry, s.knowledge_id)
        results.append({
            "id": s.id,
            "knowledge_id": s.knowledge_id,
            "title": (entry.ai_title or entry.title) if entry else "",
            "suggested_folder_id": s.suggested_folder_id,
            "suggested_folder_path": s.suggested_folder_path,
            "confidence": s.confidence,
            "reason": s.reason,
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return results


@router.post("/filing/ensure-system-tree")
def filing_ensure_system_tree(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """初始化/刷新系统归档树。"""
    from app.services.system_folder_service import ensure_system_folders
    mapping = ensure_system_folders(db, owner_id=user.id)
    return {"ok": True, "nodes": len(mapping)}


# ═══════════════════════════════════════════════════════════════════════════════
# 文档理解确认（用户必须处理完才能继续使用知识库）
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/understanding/pending-count")
def understanding_pending_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户有多少条未确认的文档理解结果。前端用此接口决定是否弹出拦截。"""
    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile

    count = (
        db.query(KnowledgeUnderstandingProfile)
        .join(KnowledgeEntry, KnowledgeUnderstandingProfile.knowledge_id == KnowledgeEntry.id)
        .filter(
            KnowledgeEntry.created_by == user.id,
            KnowledgeUnderstandingProfile.understanding_status.in_(["success", "partial"]),
            KnowledgeUnderstandingProfile.confirmed_at.is_(None),
        )
        .count()
    )
    return {"pending_count": count}


@router.get("/understanding/pending")
def understanding_pending_list(
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回未确认的文档理解结果列表，供用户逐条审阅确认。"""
    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile

    profiles = (
        db.query(KnowledgeUnderstandingProfile)
        .join(KnowledgeEntry, KnowledgeUnderstandingProfile.knowledge_id == KnowledgeEntry.id)
        .filter(
            KnowledgeEntry.created_by == user.id,
            KnowledgeUnderstandingProfile.understanding_status.in_(["success", "partial"]),
            KnowledgeUnderstandingProfile.confirmed_at.is_(None),
        )
        .order_by(KnowledgeUnderstandingProfile.created_at.desc())
        .limit(limit)
        .all()
    )

    results = []
    for p in profiles:
        entry = db.get(KnowledgeEntry, p.knowledge_id)
        results.append({
            "profile_id": p.id,
            "knowledge_id": p.knowledge_id,
            "source_file": entry.source_file if entry else None,
            "raw_title": p.raw_title,
            "display_title": p.display_title,
            "title_confidence": p.title_confidence,
            "document_type": p.document_type,
            "permission_domain": p.permission_domain,
            "desensitization_level": p.desensitization_level,
            "content_tags": p.content_tags,
            "content_tag_confidences": p.content_tag_confidences,
            "suggested_tags": p.suggested_tags,
            "summary_short": p.summary_short,
            "summary_search": p.summary_search,
            "system_id": p.system_id,
            "quality_score": entry.quality_score if entry else None,
            "understanding_status": p.understanding_status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return results


class UnderstandingConfirmRequest(BaseModel):
    """用户确认/修正文档理解结果。只需传想修改的字段，未传的保持 AI 结果。"""
    title: str | None = None
    document_type: str | None = None
    summary_short: str | None = None
    content_tags: dict | None = None


@router.post("/understanding/{profile_id}/confirm")
def understanding_confirm(
    profile_id: int,
    body: UnderstandingConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户确认一条文档理解结果，可同时修正字段。"""
    import datetime as _dt
    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile

    profile = db.get(KnowledgeUnderstandingProfile, profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")

    entry = db.get(KnowledgeEntry, profile.knowledge_id)
    if not entry or entry.created_by != user.id:
        raise HTTPException(403, "只能确认自己上传的文档")

    if profile.confirmed_at:
        return {"ok": True, "message": "已确认过"}

    # 记录用户修正
    corrections = {}
    if body.title is not None and body.title != profile.display_title:
        corrections["title"] = {"from": profile.display_title, "to": body.title}
        profile.display_title = body.title
        profile.title_source = "user"
        entry.ai_title = body.title
    if body.document_type is not None and body.document_type != profile.document_type:
        corrections["document_type"] = {"from": profile.document_type, "to": body.document_type}
        profile.document_type = body.document_type
        profile.classification_source = "manual"
    if body.summary_short is not None and body.summary_short != profile.summary_short:
        corrections["summary_short"] = {"from": profile.summary_short, "to": body.summary_short}
        profile.summary_short = body.summary_short
        entry.ai_summary = body.summary_short
    if body.content_tags is not None and body.content_tags != profile.content_tags:
        corrections["content_tags"] = {"from": profile.content_tags, "to": body.content_tags}
        from app.data.sensitivity_rules import validate_content_tags
        profile.content_tags = validate_content_tags(body.content_tags)
        profile.tagging_source = "manual"

    profile.confirmed_at = _dt.datetime.utcnow()
    profile.confirmed_by = user.id
    profile.user_corrections = corrections if corrections else None

    db.commit()
    return {"ok": True, "corrections": len(corrections)}


@router.post("/understanding/confirm-batch")
def understanding_confirm_batch(
    profile_ids: list[int],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量确认：用户审阅后全部接受 AI 结果（无修正）。"""
    import datetime as _dt
    from app.models.knowledge_understanding import KnowledgeUnderstandingProfile

    confirmed = 0
    for pid in profile_ids:
        profile = db.get(KnowledgeUnderstandingProfile, pid)
        if not profile or profile.confirmed_at:
            continue
        entry = db.get(KnowledgeEntry, profile.knowledge_id)
        if not entry or entry.created_by != user.id:
            continue
        profile.confirmed_at = _dt.datetime.utcnow()
        profile.confirmed_by = user.id
        confirmed += 1

    db.commit()
    return {"ok": True, "confirmed": confirmed}
