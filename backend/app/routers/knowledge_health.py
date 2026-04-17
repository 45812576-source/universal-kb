from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.user import User

router = APIRouter(prefix="/api/knowledge-health", tags=["knowledge-health"])


@router.get("")
def knowledge_health(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    upload_dir = settings.UPLOAD_DIR
    return {
        "ok": True,
        "checks": {
            "knowledge_query": db.query(KnowledgeEntry.id).limit(1).count() >= 0,
            "folder_query": db.query(KnowledgeFolder.id).limit(1).count() >= 0,
            "upload_dir_exists": os.path.isdir(upload_dir),
            "upload_dir_writable": os.access(upload_dir, os.W_OK) if os.path.isdir(upload_dir) else False,
            "lark_configured": bool(
                settings.LARK_APP_ID and settings.LARK_APP_SECRET
            ),
            "lark_import_auth_mode": settings.LARK_IMPORT_AUTH_MODE,
            "lark_oauth_enabled": settings.LARK_OAUTH_ENABLED,
        },
    }
