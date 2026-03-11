"""Business tables management API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.business import BusinessTable, DataOwnership, VisibilityLevel, SkillDataQuery
from app.models.user import User, Role, Department
from app.models.skill import Skill
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
    if not tables:
        return []

    # Batch-fetch enrichment data to avoid N+1
    table_names = [t.table_name for t in tables]
    owner_ids = list({t.owner_id for t in tables if t.owner_id})
    dept_ids = list({t.department_id for t in tables if t.department_id})

    # owner display_name map
    owner_map: dict[int, str] = {}
    if owner_ids:
        for u in db.query(User.id, User.display_name).filter(User.id.in_(owner_ids)):
            owner_map[u.id] = u.display_name

    # department name map
    dept_map: dict[int, str] = {}
    if dept_ids:
        for d in db.query(Department.id, Department.name).filter(Department.id.in_(dept_ids)):
            dept_map[d.id] = d.name

    # data_ownership_rules: table_name → rule
    ownership_map: dict[str, dict] = {}
    for rule in db.query(DataOwnership).filter(DataOwnership.table_name.in_(table_names)):
        ownership_map[rule.table_name] = {
            "owner_field": rule.owner_field,
            "department_field": rule.department_field,
            "visibility_level": rule.visibility_level.value if rule.visibility_level else "detail",
        }

    # skill_data_queries → skill names per table
    skill_map: dict[str, list[str]] = {n: [] for n in table_names}
    sdq_rows = (
        db.query(SkillDataQuery.table_name, Skill.name)
        .join(Skill, Skill.id == SkillDataQuery.skill_id)
        .filter(SkillDataQuery.table_name.in_(table_names))
        .all()
    )
    for tname, sname in sdq_rows:
        skill_map[tname].append(sname)

    result = []
    for t in tables:
        d = _table_detail(t)
        d["owner_name"] = owner_map.get(t.owner_id) if t.owner_id else None
        d["department_name"] = dept_map.get(t.department_id) if t.department_id else None
        d["ownership"] = ownership_map.get(t.table_name)
        d["referenced_skills"] = skill_map.get(t.table_name, [])
        result.append(d)
    return result


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


class ProbeBitableRequest(BaseModel):
    app_token: str      # from bitable URL: /base/{app_token}
    table_id: str       # from bitable URL: ?table={table_id}
    display_name: str = ""


class SyncBitableRequest(BaseModel):
    app_token: str
    table_id: str
    display_name: str = ""
    sync_table_name: str = ""   # target MySQL table name; auto-generated if empty


class ProbeTableRequest(BaseModel):
    db_url: str            # e.g. "mysql+pymysql://user:pass@host:3306/dbname"
    table_name: str


@router.post("/probe-bitable")
async def probe_bitable(
    req: ProbeBitableRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Preview a Feishu Bitable table: fetch fields + first 20 records. Does NOT persist."""
    from app.services.lark_client import lark_client
    try:
        token = await lark_client.get_tenant_access_token()
    except Exception as e:
        raise HTTPException(400, f"飞书认证失败: {e}")

    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}"}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Get fields
        r = await client.get(
            f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/fields",
            headers=headers,
            params={"page_size": 100},
        )
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(400, f"获取字段失败: {data.get('msg')} (code={data.get('code')})")
        fields = data["data"]["items"]
        columns = [
            {"name": f["field_name"], "type": f.get("type", 1), "nullable": True, "comment": ""}
            for f in fields
        ]

        # 2. Get first 20 records via search
        r2 = await client.post(
            f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/records/search",
            headers=headers,
            json={"page_size": 20},
        )
        data2 = r2.json()
        if data2.get("code") != 0:
            raise HTTPException(400, f"获取记录失败: {data2.get('msg')} (code={data2.get('code')})")
        records = data2["data"]["items"]
        field_names = [c["name"] for c in columns]
        preview_rows = []
        for rec in records:
            row = {fn: rec.get("fields", {}).get(fn) for fn in field_names}
            # flatten cell values (bitable returns rich objects for some types)
            flat = {}
            for k, v in row.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
                    flat[k] = "".join(item.get("text", "") for item in v)
                elif isinstance(v, dict) and "text" in v:
                    flat[k] = v["text"]
                else:
                    flat[k] = v
            preview_rows.append(flat)

    return {
        "app_token": req.app_token,
        "table_id": req.table_id,
        "columns": columns,
        "preview_rows": preview_rows,
    }


# Bitable field type → MySQL type mapping
_BITABLE_TYPE_MAP = {
    1: "TEXT",        # 多行文本
    2: "DOUBLE",      # 数字
    3: "VARCHAR(50)", # 单选
    4: "TEXT",        # 多选 (JSON array)
    5: "DATETIME",    # 日期
    7: "TINYINT(1)",  # 复选框
    11: "TEXT",       # 人员
    13: "TEXT",       # 电话
    15: "TEXT",       # URL
    17: "TEXT",       # 附件
    18: "TEXT",       # 单向关联
    19: "BIGINT",     # 查找引用
    20: "DOUBLE",     # 公式
    21: "DOUBLE",     # 双向关联
    22: "BIGINT",     # 创建时间
    23: "BIGINT",     # 最后更新时间
    24: "TEXT",       # 创建人
    25: "TEXT",       # 修改人
    1001: "TEXT",     # 自动编号
    1002: "TEXT",     # 条码
    1003: "TEXT",     # 进度
    1004: "DOUBLE",   # 货币
    1005: "DOUBLE",   # 评分
    1006: "TEXT",     # 邮件
    1007: "TEXT",     # 地理位置
    1008: "TEXT",     # 群组
}


@router.post("/sync-bitable")
async def sync_bitable(
    req: SyncBitableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Full sync: fetch ALL records from Feishu Bitable → create/replace local MySQL table → register."""
    from app.services.lark_client import lark_client
    import re, time

    try:
        token = await lark_client.get_tenant_access_token()
    except Exception as e:
        raise HTTPException(400, f"飞书认证失败: {e}")

    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}"}

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Fields
        r = await client.get(
            f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/fields",
            headers=headers,
            params={"page_size": 100},
        )
        fdata = r.json()
        if fdata.get("code") != 0:
            raise HTTPException(400, f"获取字段失败: {fdata.get('msg')}")
        fields = fdata["data"]["items"]

        # 2. All records (paginated)
        all_records = []
        page_token = None
        while True:
            body = {"page_size": 500}
            if page_token:
                body["page_token"] = page_token
            r2 = await client.post(
                f"{base}/bitable/v1/apps/{req.app_token}/tables/{req.table_id}/records/search",
                headers=headers,
                json=body,
            )
            rdata = r2.json()
            if rdata.get("code") != 0:
                raise HTTPException(400, f"获取记录失败: {rdata.get('msg')}")
            all_records.extend(rdata["data"]["items"])
            if not rdata["data"].get("has_more"):
                break
            page_token = rdata["data"].get("page_token")

    # 3. Derive target table name
    safe_name = req.sync_table_name.strip() if req.sync_table_name.strip() else ""
    if not safe_name:
        safe_name = f"bitable_{re.sub(r'[^a-z0-9_]', '_', req.table_id.lower()[:30])}"

    display = req.display_name.strip() or safe_name

    # 4. Build DDL
    col_defs = ["  `_record_id` VARCHAR(100) PRIMARY KEY COMMENT '飞书记录ID'"]
    field_names = []
    for f in fields:
        fn = f["field_name"]
        field_names.append(fn)
        # sanitize column name for MySQL
        col = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", fn)
        mysql_type = _BITABLE_TYPE_MAP.get(f.get("type", 1), "TEXT")
        col_defs.append(f"  `{col}` {mysql_type} COMMENT '{fn}'")
    col_defs.append("  `_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    ddl = f"CREATE TABLE IF NOT EXISTS `{safe_name}` (\n" + ",\n".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    # col name mapping: field_name → mysql col name
    col_map = {}
    for f in fields:
        fn = f["field_name"]
        col_map[fn] = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", fn)

    # 5. Create/reset table
    try:
        db.execute(text(f"DROP TABLE IF EXISTS `{safe_name}`"))
        db.execute(text(ddl))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"建表失败: {e}")

    # 6. Insert records
    import json as _json
    inserted = 0
    for rec in all_records:
        record_id = rec.get("record_id", "")
        flds = rec.get("fields", {})
        row_data = {"_record_id": record_id}
        for fn in field_names:
            col = col_map[fn]
            v = flds.get(fn)
            # flatten
            if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
                v = "".join(item.get("text", "") for item in v)
            elif isinstance(v, dict) and "text" in v:
                v = v["text"]
            elif isinstance(v, (list, dict)):
                v = _json.dumps(v, ensure_ascii=False)
            row_data[col] = v
        cols_sql = ", ".join(f"`{k}`" for k in row_data)
        placeholders = ", ".join(f":{k}" for k in row_data)
        try:
            db.execute(text(f"INSERT INTO `{safe_name}` ({cols_sql}) VALUES ({placeholders})"), row_data)
            inserted += 1
        except Exception:
            pass
    db.commit()

    # 7. Register (upsert)
    existing = db.query(BusinessTable).filter(BusinessTable.table_name == safe_name).first()
    if existing:
        existing.display_name = display
        existing.description = f"飞书多维表格同步 | app_token={req.app_token} | table_id={req.table_id}"
        rules = dict(existing.validation_rules or {})
        rules.update({"bitable_app_token": req.app_token, "bitable_table_id": req.table_id, "last_synced_at": int(time.time())})
        existing.validation_rules = rules
        bt = existing
    else:
        bt = BusinessTable(
            table_name=safe_name,
            display_name=display,
            description=f"飞书多维表格同步 | app_token={req.app_token} | table_id={req.table_id}",
            ddl_sql=ddl,
            validation_rules={"bitable_app_token": req.app_token, "bitable_table_id": req.table_id, "last_synced_at": int(time.time())},
            owner_id=user.id,
        )
        db.add(bt)
    db.commit()
    db.refresh(bt)

    return {"ok": True, "table_name": safe_name, "id": bt.id, "inserted": inserted, "total_fields": len(fields)}


@router.post("/probe")
def probe_external_table(
    req: ProbeTableRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Connect to an external DB, fetch schema + first 20 rows. Does NOT persist anything."""
    from sqlalchemy import create_engine, inspect, text as sa_text
    try:
        engine = create_engine(req.db_url, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            insp = inspect(engine)
            cols = insp.get_columns(req.table_name)
            columns = [
                {
                    "name": c["name"],
                    "type": str(c["type"]),
                    "nullable": c.get("nullable", True),
                    "comment": c.get("comment") or "",
                }
                for c in cols
            ]
            result = conn.execute(sa_text(f"SELECT * FROM `{req.table_name}` LIMIT 20"))
            col_names = list(result.keys())
            rows = [dict(zip(col_names, row)) for row in result.fetchall()]
            # serialize non-JSON-safe types
            import datetime, decimal
            def _s(v):
                if isinstance(v, (datetime.datetime, datetime.date)): return v.isoformat()
                if isinstance(v, decimal.Decimal): return float(v)
                if isinstance(v, bytes): return v.decode("utf-8", errors="replace")
                return v
            rows = [{k: _s(v) for k, v in r.items()} for r in rows]
        return {"table_name": req.table_name, "columns": columns, "preview_rows": rows}
    except Exception as e:
        raise HTTPException(400, f"连接或查询失败: {e}")


class PatchTableRequest(BaseModel):
    display_name: str = None
    hidden_fields: list[str] = None       # stored in validation_rules["hidden_fields"]
    folder_id: int = None                 # stored in validation_rules["folder_id"]
    sort_order: int = None                # stored in validation_rules["sort_order"]
    # Column-level scope
    column_scope: str = None              # "all" | "department" | "private"
    column_department_ids: list[int] = None  # when column_scope="department"
    # Row-level scope
    row_scope: str = None                 # "all" | "department" | "private"
    row_department_ids: list[int] = None  # when row_scope="department"


@router.patch("/{table_id}")
def patch_business_table(
    table_id: int,
    req: PatchTableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Partial update: rename display_name, hide fields, set folder/sort/scope metadata."""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if req.display_name is not None:
        bt.display_name = req.display_name
    rules = dict(bt.validation_rules or {})
    if req.hidden_fields is not None:
        rules["hidden_fields"] = req.hidden_fields
    if req.folder_id is not None:
        rules["folder_id"] = req.folder_id
    if req.sort_order is not None:
        rules["sort_order"] = req.sort_order
    if req.column_scope is not None:
        rules["column_scope"] = req.column_scope
    if req.column_department_ids is not None:
        rules["column_department_ids"] = req.column_department_ids
    if req.row_scope is not None:
        rules["row_scope"] = req.row_scope
    if req.row_department_ids is not None:
        rules["row_department_ids"] = req.row_department_ids
    bt.validation_rules = rules
    db.commit()
    return {"ok": True}
