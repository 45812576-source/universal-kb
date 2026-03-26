"""File download router for generated files."""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api/files", tags=["files"])

_GENERATED_DIR = Path(settings.UPLOAD_DIR) / "generated"

# Allowed extensions for security
_ALLOWED_EXTENSIONS = {".pptx", ".xlsx", ".pdf", ".docx", ".csv", ".html"}


@router.get("/{file_id}")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download a generated file by file_id. Requires authentication."""
    # Security: prevent path traversal
    if "/" in file_id or "\\" in file_id or ".." in file_id:
        raise HTTPException(status_code=400, detail="Invalid file_id")

    file_path = _GENERATED_DIR / file_id
    suffix = Path(file_id).suffix.lower()

    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="File type not allowed")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Determine media type
    media_types = {
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".csv": "text/csv",
        ".html": "text/html",
    }
    media_type = media_types.get(suffix, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_id,
    )
