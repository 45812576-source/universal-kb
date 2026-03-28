"""Table views CRUD — saved filter/sort/group configs per business table."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.business import BusinessTable, TableView
from app.models.user import User

router = APIRouter(prefix="/api/business-tables", tags=["table-views"])


class ViewConfig(BaseModel):
    filters: list[dict] = []        # [{field, op, value}]
    sorts: list[dict] = []          # [{field, dir}]
    group_by: str = ""
    hidden_columns: list[str] = []
    column_widths: dict = {}


class CreateViewRequest(BaseModel):
    name: str
    view_type: str = "grid"
    config: ViewConfig = ViewConfig()


class PatchViewRequest(BaseModel):
    name: str = None
    config: ViewConfig = None


def _view_out(v: TableView) -> dict:
    return {
        "id": v.id,
        "table_id": v.table_id,
        "name": v.name,
        "view_type": v.view_type,
        "config": v.config or {},
        "created_by": v.created_by,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.get("/{table_id}/views")
def list_views(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    views = db.query(TableView).filter(TableView.table_id == table_id).order_by(TableView.created_at).all()
    return [_view_out(v) for v in views]


@router.post("/{table_id}/views")
def create_view(
    table_id: int,
    req: CreateViewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if not req.name.strip():
        raise HTTPException(400, "视图名称不能为空")
    v = TableView(
        table_id=table_id,
        name=req.name.strip(),
        view_type=req.view_type,
        config=req.config.model_dump(),
        created_by=user.id,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return _view_out(v)


@router.patch("/{table_id}/views/{view_id}")
def update_view(
    table_id: int,
    view_id: int,
    req: PatchViewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    v = db.get(TableView, view_id)
    if not v or v.table_id != table_id:
        raise HTTPException(404, "View not found")
    if req.name is not None:
        v.name = req.name.strip()
    if req.config is not None:
        v.config = req.config.model_dump()
    db.commit()
    return _view_out(v)


@router.delete("/{table_id}/views/{view_id}")
def delete_view(
    table_id: int,
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    v = db.get(TableView, view_id)
    if not v or v.table_id != table_id:
        raise HTTPException(404, "View not found")
    db.delete(v)
    db.commit()
    return {"ok": True}
