import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.services.auth_service import authenticate, create_token, verify_password, hash_password
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5MB


def _user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role.value,
        "department_id": user.department_id,
        "position_id": user.position_id,
        "report_to_id": user.report_to_id,
        "report_to_name": user.report_to.display_name if user.report_to else None,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "avatar_url": getattr(user, "avatar_url", None),
    }


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate(db, req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.id, user.role.value)
    return {"access_token": token, "user": _user_dict(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _user_dict(user)


@router.post("/change_password")
def change_password(
    req: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(req.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    current_user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"ok": True}


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if file.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="只支持 JPG/PNG/WebP/GIF 格式")
    data = await file.read()
    if len(data) > _MAX_AVATAR_SIZE:
        raise HTTPException(status_code=400, detail="图片不超过 5MB")

    ext = file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "jpg"
    filename = f"avatar_{current_user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    avatar_dir = Path(settings.UPLOAD_DIR) / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    (avatar_dir / filename).write_bytes(data)

    current_user.avatar_url = f"/api/avatars/{filename}"
    db.commit()
    return {"avatar_url": current_user.avatar_url}


@router.get("/model-grants")
def get_model_grants(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回当前用户被授权使用的受限模型 key 列表。"""
    from app.models.opencode import UserModelGrant
    grants = db.query(UserModelGrant).filter(UserModelGrant.user_id == current_user.id).all()
    return {"model_keys": [g.model_key for g in grants]}
