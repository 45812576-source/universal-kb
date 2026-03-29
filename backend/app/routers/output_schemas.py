"""Skill Output Schema 管理 API"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.permission import SchemaStatus, SkillOutputSchema
from app.models.skill import Skill, SkillVersion
from app.models.user import Role, User

router = APIRouter(prefix="/api/admin/output-schemas", tags=["output-schemas"])

_admin = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN))


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class SchemaUpdate(BaseModel):
    schema_json: dict
    status: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _schema(s: SkillOutputSchema) -> dict:
    return {
        "id": s.id,
        "skill_id": s.skill_id,
        "version": s.version,
        "status": s.status,
        "schema_json": s.schema_json or {},
        "created_by": s.created_by,
        "approved_by": s.approved_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_schemas(
    skill_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    q = db.query(SkillOutputSchema)
    if skill_id is not None:
        q = q.filter(SkillOutputSchema.skill_id == skill_id)
    if status:
        q = q.filter(SkillOutputSchema.status == status)
    return [_schema(s) for s in q.order_by(SkillOutputSchema.skill_id, SkillOutputSchema.version.desc()).all()]


@router.post("/generate")
async def generate_schema(
    skill_id: int,
    model_config_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    """触发 LLM 自动推导 output schema（基于 Skill 最新版本的 system_prompt）"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    latest_version: SkillVersion | None = skill.versions[0] if skill.versions else None
    if not latest_version:
        raise HTTPException(400, "Skill 没有版本，无法推导 Schema")

    # 如果最新版本已有 output_schema，直接转存
    if latest_version.output_schema:
        existing_schema = latest_version.output_schema
    else:
        # 调用 LLM 推导
        try:
            from app.services.llm_gateway import llm_gateway
            from app.services.schema_generator import schema_generator
            model_config = llm_gateway.resolve_config(db, "output_schema.gen", model_config_id)
            existing_schema = await schema_generator.generate_output_schema(
                system_prompt=latest_version.system_prompt,
                model_config=model_config,
            )
        except Exception as e:
            raise HTTPException(500, f"Schema 推导失败：{e}")

    # 获取当前最大版本号
    max_ver = (
        db.query(SkillOutputSchema)
        .filter(SkillOutputSchema.skill_id == skill_id)
        .count()
    )

    s = SkillOutputSchema(
        skill_id=skill_id,
        version=max_ver + 1,
        status=SchemaStatus.PENDING_REVIEW,
        schema_json=existing_schema,
        created_by=user.id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _schema(s)


@router.post("/{schema_id}/approve")
def approve_schema(
    schema_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """审批通过 Schema（仅超管）"""
    s = db.get(SkillOutputSchema, schema_id)
    if not s:
        raise HTTPException(404, "Schema 不存在")
    if s.status == SchemaStatus.APPROVED:
        raise HTTPException(400, "已经是 approved 状态")
    s.status = SchemaStatus.APPROVED
    s.approved_by = user.id
    db.commit()
    db.refresh(s)
    return _schema(s)


@router.put("/{schema_id}")
def update_schema(
    schema_id: int,
    req: SchemaUpdate,
    db: Session = Depends(get_db),
    user: User = _admin,
):
    """手动编辑 Schema"""
    s = db.get(SkillOutputSchema, schema_id)
    if not s:
        raise HTTPException(404, "Schema 不存在")
    if s.status == SchemaStatus.APPROVED:
        raise HTTPException(400, "已审批通过的 Schema 不可编辑，请创建新版本")
    s.schema_json = req.schema_json
    if req.status:
        s.status = req.status
    db.commit()
    db.refresh(s)
    return _schema(s)
