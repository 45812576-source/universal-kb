"""Table views CRUD — saved filter/sort/group configs per business table."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.business import BusinessTable, TableField, TableView
from app.models.user import User
from app.services.data_asset_access import require_table_manage_access, require_table_view_access
from app.services.data_asset_codec import hydrate_view_payload, serialize_view

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
    data = serialize_view(v)
    data["created_at"] = v.created_at.isoformat() if v.created_at else None
    return data


@router.get("/{table_id}/views")
def list_views(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    require_table_view_access(db, bt, user)
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
    require_table_manage_access(db, bt, user)
    if not req.name.strip():
        raise HTTPException(400, "视图名称不能为空")
    fields = db.query(TableField).filter(TableField.table_id == table_id).order_by(TableField.sort_order).all()
    hydrated = hydrate_view_payload(req.model_dump(), fields)
    v = TableView(
        table_id=table_id,
        name=hydrated["name"],
        view_type=hydrated["view_type"],
        view_kind=hydrated["view_kind"],
        visibility_scope=hydrated["visibility_scope"],
        view_purpose=hydrated["view_purpose"],
        config=hydrated["config"],
        visible_field_ids=hydrated["visible_field_ids"],
        filter_rule_json=hydrated["filter_rule_json"],
        sort_rule_json=hydrated["sort_rule_json"],
        group_rule_json=hydrated["group_rule_json"],
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
    bt = db.get(BusinessTable, table_id)
    if bt:
        require_table_manage_access(db, bt, user)
    fields = db.query(TableField).filter(TableField.table_id == table_id).order_by(TableField.sort_order).all()
    current = {
        "name": v.name,
        "view_type": v.view_type,
        "view_kind": v.view_kind,
        "visibility_scope": v.visibility_scope,
        "view_purpose": v.view_purpose,
        "visible_field_ids": v.visible_field_ids or [],
        "config": v.config or {},
    }
    if req.name is not None:
        current["name"] = req.name
    if req.config is not None:
        current["config"] = req.config.model_dump()
    hydrated = hydrate_view_payload(current, fields)
    from sqlalchemy.orm.attributes import flag_modified
    v.name = hydrated["name"]
    v.view_type = hydrated["view_type"]
    v.view_kind = hydrated["view_kind"]
    v.visibility_scope = hydrated["visibility_scope"]
    v.view_purpose = hydrated["view_purpose"]
    v.visible_field_ids = hydrated["visible_field_ids"]
    v.config = hydrated["config"]
    v.filter_rule_json = hydrated["filter_rule_json"]
    v.sort_rule_json = hydrated["sort_rule_json"]
    v.group_rule_json = hydrated["group_rule_json"]
    flag_modified(v, "visible_field_ids")
    flag_modified(v, "config")
    flag_modified(v, "filter_rule_json")
    flag_modified(v, "sort_rule_json")
    flag_modified(v, "group_rule_json")
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
    bt = db.get(BusinessTable, table_id)
    if bt:
        require_table_manage_access(db, bt, user)
    db.delete(v)
    db.commit()
    return {"ok": True}
