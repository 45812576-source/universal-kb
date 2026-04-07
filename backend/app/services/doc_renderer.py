"""文档转换服务：将上传文件统一转换为 content_html 云文档渲染内容。

复用 file_parser.extract_html()，额外管理 doc_render_status 状态流转。
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

# 文件扩展名 → 渲染模式映射
_EXT_RENDER_MODE = {
    ".md": "native_html",
    ".txt": "native_html",
    ".html": "native_html",
    ".htm": "native_html",
    ".docx": "converted_html",
    ".doc": "converted_html",
    ".pptx": "converted_html",
    ".ppt": "converted_html",
    ".xlsx": "converted_html",
    ".xls": "converted_html",
    ".csv": "converted_html",
    ".pdf": "onlyoffice",  # PDF 转 DOCX 后走 OnlyOffice
}

# OnlyOffice 可处理的扩展名
ONLYOFFICE_EXTS = {
    ".docx", ".doc", ".odt", ".rtf", ".txt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
}


def render_entry(db: Session, entry_id: int) -> dict:
    """对指定知识条目执行云文档转换。

    Returns:
        {"ok": True, "mode": "...", "status": "ready"} 或
        {"ok": False, "error": "...", "status": "failed"}
    """
    entry = db.get(KnowledgeEntry, entry_id)
    if not entry:
        return {"ok": False, "error": "entry not found", "status": "failed"}

    ext = (entry.file_ext or "").lower()

    # 标记处理中
    entry.doc_render_status = "processing"
    entry.doc_render_error = None
    db.commit()

    # 更新 job phase（如果有 running job）
    def _update_phase(phase: str):
        try:
            from app.models.knowledge_job import KnowledgeJob
            job = (
                db.query(KnowledgeJob)
                .filter(
                    KnowledgeJob.knowledge_id == entry_id,
                    KnowledgeJob.job_type == "render",
                    KnowledgeJob.status == "running",
                )
                .first()
            )
            if job:
                job.phase = phase
                db.flush()
        except Exception:
            pass

    try:
        _update_phase("extracting")
        content_html = _do_render(entry, ext, db=db)
        render_mode = _resolve_render_mode(entry, ext)

        _update_phase("rendering")
        if content_html:
            entry.content_html = content_html
            entry.doc_render_status = "ready"
            entry.doc_render_mode = render_mode
        else:
            # 无法生成 HTML 但有 OnlyOffice 支持（含 PDF 已转 DOCX）
            if ext in ONLYOFFICE_EXTS or (ext == ".pdf" and entry.docx_oss_key):
                entry.doc_render_status = "ready"
                entry.doc_render_mode = "onlyoffice"
            else:
                entry.doc_render_status = "ready"
                entry.doc_render_mode = "text_fallback"

        _update_phase("persisting")
        entry.doc_render_error = None
        entry.last_rendered_at = utcnow()

        # 渲染成功后生成 document blocks
        try:
            from app.services.block_splitter import generate_blocks
            generate_blocks(db, entry)
        except Exception as e:
            logger.warning(f"Block generation failed for entry {entry_id}: {e}")

        db.commit()

        return {"ok": True, "mode": entry.doc_render_mode, "status": "ready"}

    except Exception as e:
        logger.warning(f"Doc render failed for entry {entry_id}: {e}")
        entry.doc_render_status = "failed"
        entry.doc_render_error = str(e)[:500]
        entry.last_rendered_at = utcnow()
        db.commit()
        return {"ok": False, "error": str(e), "status": "failed"}


def render_from_path(db: Session, entry: KnowledgeEntry, file_path: str) -> None:
    """直接从本地文件路径执行转换（上传流程中文件还未清理时调用）。"""
    ext = (entry.file_ext or "").lower()

    entry.doc_render_status = "processing"
    db.flush()

    try:
        # PDF 走专门的转换流程
        if ext == ".pdf":
            content_html = _convert_pdf_and_upload(entry, file_path, db=db)
        else:
            from app.utils.file_parser import extract_html
            content_html = extract_html(file_path)

        render_mode = _resolve_render_mode(entry, ext, file_path=file_path)

        if content_html:
            entry.content_html = content_html
            entry.doc_render_status = "ready"
            entry.doc_render_mode = render_mode
        elif ext in ONLYOFFICE_EXTS or ext == ".pdf":
            entry.doc_render_status = "ready"
            entry.doc_render_mode = "onlyoffice"
        else:
            entry.doc_render_status = "ready"
            entry.doc_render_mode = "text_fallback"

        entry.doc_render_error = None
        entry.last_rendered_at = utcnow()

    except Exception as e:
        logger.warning(f"Doc render from path failed for entry {entry.id}: {e}")
        entry.doc_render_status = "failed"
        entry.doc_render_error = str(e)[:500]
        entry.last_rendered_at = utcnow()


def _do_render(entry: KnowledgeEntry, ext: str, db: Optional[Session] = None) -> Optional[str]:
    """下载 OSS 文件到临时目录，调用 extract_html，返回 HTML 或 None。

    PDF 文件会先转换为 DOCX 并上传 OSS（存入 entry.docx_oss_key），
    后续通过 OnlyOffice 预览/编辑该 DOCX。
    """
    if not entry.oss_key:
        # 无 OSS 文件，尝试从 content 生成
        return render_from_content(entry.content or "", ext)

    from app.services.oss_service import download_file

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, f"render{ext}")
    try:
        download_file(entry.oss_key, tmp_path)

        # PDF → DOCX 转换：生成可编辑的 DOCX 并上传 OSS
        if ext == ".pdf":
            return _convert_pdf_and_upload(entry, tmp_path, db)

        from app.utils.file_parser import extract_html
        return extract_html(tmp_path)
    finally:
        # 清理临时目录下所有文件
        import glob as _glob
        for f in _glob.glob(os.path.join(tmp_dir, "*")):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _convert_pdf_and_upload(entry: KnowledgeEntry, pdf_path: str, db: Optional[Session] = None) -> Optional[str]:
    """将 PDF 转为 DOCX，上传 OSS，设置 entry.docx_oss_key。

    扫描件 PDF 兜底：如果转换后的 DOCX 内容为空（< 1KB 或提取文本 < 40 字符），
    则不设置 docx_oss_key，前端自动走 iframe 预览原始 PDF。
    AI 笔记仍然从 entry.content（Vision OCR 文本）生成。

    转换失败（超时、复杂排版等）时返回 None，前端走 iframe 预览原始 PDF。
    """
    from app.utils.file_parser import convert_pdf_to_docx
    from app.services.oss_service import upload_file as oss_upload

    try:
        docx_path = convert_pdf_to_docx(pdf_path)
    except Exception as e:
        logger.warning(f"PDF→DOCX conversion failed for entry {entry.id}: {e}")
        # 转换失败不算致命错误，前端走 iframe 预览原始 PDF
        return None
    try:
        # 检测转换后 DOCX 是否实质为空（扫描件 PDF 场景）
        docx_size = os.path.getsize(docx_path)
        docx_text = ""
        if docx_size >= 1024:
            try:
                from app.utils.file_parser import extract_text as _extract_text
                docx_text = _extract_text(docx_path) or ""
            except Exception:
                docx_text = ""

        is_empty_docx = docx_size < 1024 or len(docx_text.strip()) < 40

        if is_empty_docx:
            logger.info(
                f"PDF→DOCX for entry {entry.id} produced empty DOCX "
                f"(size={docx_size}B, text_len={len(docx_text.strip())}), "
                f"skipping docx_oss_key — will use iframe PDF preview"
            )
            # 不设置 docx_oss_key，前端走 iframe 预览原始 PDF
            return None

        # 生成 DOCX 的 OSS key（在原 PDF key 旁边）
        docx_oss_key = entry.oss_key.rsplit(".", 1)[0] + ".docx"
        oss_upload(docx_path, docx_oss_key)
        entry.docx_oss_key = docx_oss_key
        if db:
            db.flush()
        logger.info(f"PDF→DOCX conversion done for entry {entry.id}, docx_oss_key={docx_oss_key}")

        # 从转换后的 DOCX 提取 HTML 作为 content_html 备用
        from app.utils.file_parser import extract_html
        return extract_html(docx_path)
    finally:
        try:
            os.unlink(docx_path)
        except OSError:
            pass


def render_from_content(content: str, ext: str) -> Optional[str]:
    """无文件时从纯文本 content 生成 HTML（fallback）。"""
    if not content:
        return None
    if ext in (".md",):
        import markdown as md_lib
        return md_lib.markdown(content, extensions=["tables", "fenced_code", "nl2br", "sane_lists"])
    # 其余格式包装为 <p>
    return "\n".join(f"<p>{line or '<br>'}</p>" for line in content.split("\n"))


def _resolve_render_mode(entry: KnowledgeEntry, ext: str, file_path: str | None = None) -> str:
    if ext != ".pdf":
        return _EXT_RENDER_MODE.get(ext, "text_fallback")

    try:
        from app.utils.file_parser import extract_text_result

        if file_path:
            result = extract_text_result(file_path)
            if result.error:
                entry.doc_render_error = result.error
            return result.mode
    except Exception:
        pass
    return _EXT_RENDER_MODE.get(ext, "pdf_fallback")
