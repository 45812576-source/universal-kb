"""Web apps CRUD + preview + public share."""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.web_app import WebApp
from app.models.user import User

router = APIRouter(tags=["web-apps"])


class WebAppCreate(BaseModel):
    name: str
    description: Optional[str] = None
    html_content: str
    is_public: bool = False


class WebAppUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    html_content: Optional[str] = None
    is_public: Optional[bool] = None


def _app_dict(app: WebApp, include_html: bool = False) -> dict:
    d = {
        "id": app.id,
        "name": app.name,
        "description": app.description,
        "created_by": app.created_by,
        "is_public": app.is_public,
        "share_token": app.share_token,
        "preview_url": f"/api/web-apps/{app.id}/preview",
        "share_url": f"/share/{app.share_token}" if app.share_token else None,
        "created_at": app.created_at.isoformat() if app.created_at else None,
    }
    if include_html:
        d["html_content"] = app.html_content
    return d


@router.get("/api/web-apps")
def list_web_apps(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    apps = (
        db.query(WebApp)
        .filter(WebApp.created_by == user.id)
        .order_by(WebApp.created_at.desc())
        .all()
    )
    return [_app_dict(a) for a in apps]


@router.post("/api/web-apps")
def create_web_app(
    body: WebAppCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    share_token = secrets.token_urlsafe(16) if body.is_public else secrets.token_urlsafe(16)
    app = WebApp(
        name=body.name,
        description=body.description,
        html_content=body.html_content,
        created_by=user.id,
        is_public=body.is_public,
        share_token=share_token,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return _app_dict(app, include_html=True)


@router.get("/api/web-apps/{app_id}")
def get_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    return _app_dict(app, include_html=True)


@router.put("/api/web-apps/{app_id}")
def update_web_app(
    app_id: int,
    body: WebAppUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    if app.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(app, field, value)
    db.commit()
    db.refresh(app)
    return _app_dict(app, include_html=True)


@router.delete("/api/web-apps/{app_id}")
def delete_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    if app.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    db.delete(app)
    db.commit()
    return {"ok": True}


@router.get("/api/web-apps/{app_id}/preview", response_class=HTMLResponse)
def preview_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    return HTMLResponse(content=app.html_content or "<html><body>空内容</body></html>")


@router.get("/share/{share_token}", response_class=HTMLResponse)
def public_share(share_token: str, db: Session = Depends(get_db)):
    """Public access via share token — no login required."""
    app = db.query(WebApp).filter(WebApp.share_token == share_token).first()
    if not app:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(content=app.html_content or "<html><body>空内容</body></html>")
