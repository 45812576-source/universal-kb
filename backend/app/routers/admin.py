from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import require_role
from app.models.user import Department, Role, User
from app.models.skill import ModelConfig

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─── Model Config CRUD ───────────────────────────────────────────────────────

class ModelConfigCreate(BaseModel):
    name: str
    provider: str
    model_id: str
    api_base: str
    api_key_env: str = ""
    max_tokens: int = 4096
    temperature: str = "0.7"
    is_default: bool = False


@router.get("/models")
def list_models(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    return [
        {
            "id": m.id,
            "name": m.name,
            "provider": m.provider,
            "model_id": m.model_id,
            "api_base": m.api_base,
            "api_key_env": m.api_key_env,
            "max_tokens": m.max_tokens,
            "temperature": m.temperature,
            "is_default": m.is_default,
        }
        for m in db.query(ModelConfig).all()
    ]


@router.post("/models")
def create_model(
    req: ModelConfigCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    if req.is_default:
        db.query(ModelConfig).update({ModelConfig.is_default: False})
    mc = ModelConfig(**req.model_dump())
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return {"id": mc.id, "name": mc.name}


@router.put("/models/{model_id}")
def update_model(
    model_id: int,
    req: ModelConfigCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mc = db.get(ModelConfig, model_id)
    if not mc:
        raise HTTPException(404, "Model config not found")
    if req.is_default:
        db.query(ModelConfig).update({ModelConfig.is_default: False})
    for k, v in req.model_dump().items():
        setattr(mc, k, v)
    db.commit()
    db.refresh(mc)
    return {"id": mc.id, "name": mc.name}


@router.delete("/models/{model_id}")
def delete_model(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mc = db.get(ModelConfig, model_id)
    if not mc:
        raise HTTPException(404, "Model config not found")
    db.delete(mc)
    db.commit()
    return {"ok": True}


# ─── Department listing (for user management UI) ─────────────────────────────

@router.get("/departments")
def list_departments(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    return [
        {
            "id": d.id,
            "name": d.name,
            "parent_id": d.parent_id,
            "category": d.category,
            "business_unit": d.business_unit,
        }
        for d in db.query(Department).all()
    ]
