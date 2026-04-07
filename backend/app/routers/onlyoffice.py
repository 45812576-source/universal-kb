"""OnlyOffice Document Server 集成：编辑器配置 + 保存回调。"""
import logging
import os
import tempfile
import time

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.user import Role, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/onlyoffice", tags=["onlyoffice"])

# OnlyOffice 支持的文件类型
_EDITABLE_EXTS = {
    # 文档
    ".docx", ".doc", ".odt", ".rtf", ".txt",
    # 表格
    ".xlsx", ".xls", ".ods", ".csv",
    # 演示
    ".pptx", ".ppt", ".odp",
}

_EXT_TO_DOCTYPE = {
    ".docx": "word", ".doc": "word", ".odt": "word", ".rtf": "word", ".txt": "word",
    ".xlsx": "cell", ".xls": "cell", ".ods": "cell", ".csv": "cell",
    ".pptx": "slide", ".ppt": "slide", ".odp": "slide",
}


def _sign_jwt(payload: dict) -> str:
    """使用 OnlyOffice JWT secret 签名。"""
    return jwt.encode(payload, settings.ONLYOFFICE_JWT_SECRET, algorithm="HS256")


@router.get("/config/{kid}")
def get_editor_config(
    kid: int,
    mode: str = "edit",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """生成 OnlyOffice 编辑器配置 JSON。

    mode: "edit" (编辑) 或 "view" (只读预览)
    """
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    # 权限检查
    if user.role != Role.SUPER_ADMIN:
        if entry.created_by != user.id and entry.status != KnowledgeStatus.APPROVED:
            raise HTTPException(403, "Access denied")

    if not entry.oss_key:
        raise HTTPException(400, "此知识条目没有关联的原始文件")

    ext = (entry.file_ext or "").lower()

    # PDF 通过转换后的 DOCX 文件走 OnlyOffice
    is_pdf = ext == ".pdf"
    if is_pdf:
        if not entry.docx_oss_key:
            raise HTTPException(400, "PDF 尚未完成转换，请稍后重试")
        oss_key_for_office = entry.docx_oss_key
        ext_for_office = ".docx"
    elif ext not in _EDITABLE_EXTS:
        raise HTTPException(400, f"OnlyOffice 不支持编辑此文件类型: {ext}")
    else:
        oss_key_for_office = entry.oss_key
        ext_for_office = ext

    # 编辑权限：只有创建者和超管可以编辑
    can_edit = (user.role == Role.SUPER_ADMIN or entry.created_by == user.id)
    actual_mode = "edit" if (mode == "edit" and can_edit) else "view"

    # 生成文件下载 URL（OnlyOffice 服务器需要能访问到）
    from app.services.oss_service import generate_signed_url
    file_url = generate_signed_url(oss_key_for_office, expires=7200)

    # 回调 URL（OnlyOffice 编辑完成后调用）
    backend_base = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000")
    callback_url = f"{backend_base}/api/onlyoffice/callback?kid={kid}"

    doc_key = f"{kid}_{int(entry.updated_at.timestamp() if entry.updated_at else time.time())}"

    config = {
        "document": {
            "fileType": ext_for_office.lstrip("."),
            "key": doc_key,
            "title": entry.source_file or entry.title,
            "url": file_url,
        },
        "documentType": _EXT_TO_DOCTYPE.get(ext_for_office, "word"),
        "editorConfig": {
            "callbackUrl": callback_url,
            "lang": "zh-CN",
            "mode": actual_mode,
            "user": {
                "id": str(user.id),
                "name": user.username if hasattr(user, "username") else f"User-{user.id}",
            },
        },
    }

    # JWT 签名
    token = _sign_jwt(config)
    config["token"] = token

    return {
        "config": config,
        "onlyoffice_url": settings.ONLYOFFICE_URL,
        "can_edit": can_edit,
    }


@router.post("/callback")
async def editor_callback(
    request: Request,
    kid: int = None,
    db: Session = Depends(get_db),
):
    """OnlyOffice 保存回调。

    status:
      0 - 未找到错误
      1 - 正在编辑
      2 - 准备保存（文件已就绪）
      3 - 保存错误
      4 - 关闭且无修改
      6 - 正在编辑但已保存
      7 - 强制保存错误
    """
    body = await request.json()
    status = body.get("status", 0)

    logger.info(f"OnlyOffice callback: kid={kid}, status={status}")

    # 只在 status=2（准备保存）或 status=6（编辑中保存）时处理
    if status not in (2, 6):
        return {"error": 0}

    if not kid:
        logger.warning("OnlyOffice callback missing kid parameter")
        return {"error": 0}

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        logger.warning(f"OnlyOffice callback: knowledge {kid} not found")
        return {"error": 0}

    download_url = body.get("url")
    if not download_url:
        logger.warning("OnlyOffice callback missing download URL")
        return {"error": 0}

    try:
        # 下载编辑后的文件
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            file_data = resp.content

        # PDF 条目编辑的是转换后的 DOCX
        ext = entry.file_ext or ".docx"
        is_pdf = ext.lower() == ".pdf"
        save_oss_key = entry.docx_oss_key if is_pdf and entry.docx_oss_key else entry.oss_key
        save_ext = ".docx" if is_pdf else ext

        with tempfile.NamedTemporaryFile(suffix=save_ext, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = tmp.name

        try:
            # 上传新版本到 OSS（覆盖原文件 / 覆盖转换后的 DOCX）
            from app.services.oss_service import upload_file as oss_upload
            oss_upload(tmp_path, save_oss_key)

            # 重新提取文本
            from app.utils.file_parser import extract_text
            try:
                new_content = extract_text(tmp_path)
                entry.content = new_content
            except Exception as e:
                logger.warning(f"Text re-extraction failed after OnlyOffice edit: {e}")

            # 更新文件大小
            entry.file_size = len(file_data)

            # 重新向量化（先删旧的再建新的）
            from app.services.vector_service import delete_knowledge_vectors, index_knowledge
            try:
                if entry.milvus_ids:
                    delete_knowledge_vectors(entry.id)
                milvus_ids = index_knowledge(
                    entry.id,
                    entry.content,
                    created_by=entry.created_by or 0,
                    taxonomy_board=entry.taxonomy_board or "",
                    taxonomy_code=entry.taxonomy_code or "",
                    file_type=entry.file_type or "",
                    quality_score=entry.quality_score or 0.5,
                )
                entry.milvus_ids = milvus_ids
            except Exception as e:
                logger.warning(f"Re-vectorization failed: {e}")

            db.commit()
            logger.info(f"OnlyOffice save successful for knowledge {kid}")

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"OnlyOffice callback processing failed: {e}")

    return {"error": 0}
