"""文档转换服务：将上传文件统一转换为 content_html 云文档渲染内容。

复用 file_parser.extract_html()，额外管理 doc_render_status 状态流转。
"""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry

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
    ".pdf": "pdf_fallback",
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
        content_html = _do_render(entry, ext)
        render_mode = _resolve_render_mode(entry, ext)

        _update_phase("rendering")
        if content_html:
            entry.content_html = content_html
            entry.doc_render_status = "ready"
            entry.doc_render_mode = render_mode
        else:
            # 无法生成 HTML 但有 OnlyOffice 支持
            if ext in ONLYOFFICE_EXTS:
                entry.doc_render_status = "ready"
                entry.doc_render_mode = "onlyoffice"
            else:
                entry.doc_render_status = "ready"
                entry.doc_render_mode = "text_fallback"

        _update_phase("persisting")
        entry.doc_render_error = None
        entry.last_rendered_at = datetime.datetime.utcnow()

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
        entry.last_rendered_at = datetime.datetime.utcnow()
        db.commit()
        return {"ok": False, "error": str(e), "status": "failed"}


def render_from_path(db: Session, entry: KnowledgeEntry, file_path: str) -> None:
    """直接从本地文件路径执行转换（上传流程中文件还未清理时调用）。"""
    ext = (entry.file_ext or "").lower()

    entry.doc_render_status = "processing"
    db.flush()

    try:
        from app.utils.file_parser import extract_html
        content_html = extract_html(file_path)
        render_mode = _resolve_render_mode(entry, ext, file_path=file_path)

        if content_html:
            entry.content_html = content_html
            entry.doc_render_status = "ready"
            entry.doc_render_mode = render_mode
        elif ext in ONLYOFFICE_EXTS:
            entry.doc_render_status = "ready"
            entry.doc_render_mode = "onlyoffice"
        else:
            entry.doc_render_status = "ready"
            entry.doc_render_mode = "text_fallback"

        entry.doc_render_error = None
        entry.last_rendered_at = datetime.datetime.utcnow()

    except Exception as e:
        logger.warning(f"Doc render from path failed for entry {entry.id}: {e}")
        entry.doc_render_status = "failed"
        entry.doc_render_error = str(e)[:500]
        entry.last_rendered_at = datetime.datetime.utcnow()


def _do_render(entry: KnowledgeEntry, ext: str) -> Optional[str]:
    """下载 OSS 文件到临时目录，调用 extract_html，返回 HTML 或 None。"""
    if not entry.oss_key:
        # 无 OSS 文件，尝试从 content 生成
        return render_from_content(entry.content or "", ext)

    from app.services.oss_service import download_file

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, f"render{ext}")
    try:
        download_file(entry.oss_key, tmp_path)
        from app.utils.file_parser import extract_html
        return extract_html(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(tmp_dir)
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
