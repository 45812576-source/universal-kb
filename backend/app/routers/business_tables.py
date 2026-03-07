"""Business tables management API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.business import BusinessTable, DataOwnership, VisibilityLevel
from app.models.user import User, Role
from app.services.llm_gateway import llm_gateway

router = APIRouter(prefix="/api/business-tables", tags=["business-tables"])


class GenerateFromDescRequest(BaseModel):
    description: str
    model_config_id: int = None


class GenerateFromExistingRequest(BaseModel):
    table_name: str
    model_config_id: int = None


class ApplySchemaRequest(BaseModel):
    table_name: str
    display_name: str
    description: str
    ddl_sql: str
    validation_rules: dict = {}
    workflow: dict = {}
    create_skill: bool = True
    skill_def: dict = None


def _table_detail(bt: BusinessTable, columns: list[dict] = None) -> dict:
    return {
        "id": bt.id,
        "table_name": bt.table_name,
        "display_name": bt.display_name,
        "description": bt.description,
        "department_id": bt.department_id,
        "owner_id": bt.owner_id,
        "ddl_sql": bt.ddl_sql,
        "validation_rules": bt.validation_rules or {},
        "workflow": bt.workflow or {},
        "created_at": bt.created_at.isoformat() if bt.created_at else None,
        "columns": columns or [],
    }


@router.get("")
def list_business_tables(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tables = db.query(BusinessTable).order_by(BusinessTable.created_at.desc()).all()
    return [_table_detail(t) for t in tables]


@router.get("/{table_id}")
def get_business_table(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")

    # Fetch columns from INFORMATION_SCHEMA
    try:
        sql = text("""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
        """)
        rows = db.execute(sql, {"table_name": bt.table_name}).fetchall()
        columns = [
            {"name": r[0], "type": r[1], "nullable": r[2] == "YES", "comment": r[4] or ""}
            for r in rows
        ]
    except Exception:
        columns = []

    return _table_detail(bt, columns)


@router.post("/generate")
async def generate_from_description(
    req: GenerateFromDescRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Direction A: natural language → DDL + Skill preview."""
    from app.services.schema_generator import schema_generator
    model_config = llm_gateway.get_config(db, req.model_config_id)
    try:
        preview = await schema_generator.generate_from_description(req.description, model_config)
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/generate-from-existing")
async def generate_from_existing(
    req: GenerateFromExistingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Direction B: existing table → Skill preview."""
    from app.services.schema_generator import schema_generator
    model_config = llm_gateway.get_config(db, req.model_config_id)
    try:
        preview = await schema_generator.generate_from_table(req.table_name, model_config, db)
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/apply")
def apply_schema(
    req: ApplySchemaRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Confirm: execute DDL, register table, optionally create Skill."""
    from app.services.schema_generator import schema_generator
    try:
        # Create table if DDL provided
        if req.ddl_sql and req.ddl_sql.strip():
            schema_generator.apply_schema(req.ddl_sql, db)

        # Register in business_tables
        bt = schema_generator.register_table(
            table_name=req.table_name,
            display_name=req.display_name,
            description=req.description,
            ddl_sql=req.ddl_sql,
            validation_rules=req.validation_rules,
            workflow=req.workflow,
            owner_id=user.id,
            db=db,
        )

        skill_id = None
        if req.create_skill and req.skill_def:
            skill = schema_generator.create_skill_from_def(req.skill_def, req.table_name, user.id, db)
            skill_id = skill.id

        return {"id": bt.id, "table_name": bt.table_name, "skill_id": skill_id}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


class OwnershipRuleRequest(BaseModel):
    owner_field: str
    department_field: str = None
    visibility_level: str = "detail"


@router.get("/{table_id}/ownership")
def get_ownership(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    rule = db.query(DataOwnership).filter(DataOwnership.table_name == bt.table_name).first()
    if not rule:
        return None
    return {
        "id": rule.id,
        "table_name": rule.table_name,
        "owner_field": rule.owner_field,
        "department_field": rule.department_field,
        "visibility_level": rule.visibility_level.value if rule.visibility_level else "detail",
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


@router.put("/{table_id}/ownership")
def set_ownership(
    table_id: int,
    req: OwnershipRuleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if req.visibility_level not in [v.value for v in VisibilityLevel]:
        raise HTTPException(400, f"Invalid visibility_level: {req.visibility_level}")

    rule = db.query(DataOwnership).filter(DataOwnership.table_name == bt.table_name).first()
    if rule:
        rule.owner_field = req.owner_field
        rule.department_field = req.department_field
        rule.visibility_level = req.visibility_level
    else:
        rule = DataOwnership(
            table_name=bt.table_name,
            owner_field=req.owner_field,
            department_field=req.department_field,
            visibility_level=req.visibility_level,
        )
        db.add(rule)
    db.commit()
    return {"ok": True, "table_name": bt.table_name}


@router.delete("/{table_id}")
def delete_business_table(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    db.delete(bt)
    db.commit()
    return {"ok": True}
