"""Business tables management API."""
from typing import Optional
from urllib.parse import quote_plus, urlparse, urlunparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.utils.sql_safe import qi
from app.models.business import BusinessTable, DataOwnership, VisibilityLevel, SkillDataQuery
from app.models.user import User, Role, Department
from app.models.skill import Skill
from app.services.llm_gateway import llm_gateway
from app.services.lark_client import LarkConfigError, LarkAuthError

router = APIRouter(prefix="/api/business-tables", tags=["business-tables"])


def _safe_db_url(raw_url: str) -> str:
    """对数据库连接字符串中的密码做 URL 编码，防止密码含 @ 等特殊字符时解析错误。"""
    from sqlalchemy.engine.url import make_url
    try:
        u = make_url(raw_url)
        if u.password:
            # 重建 URL，密码部分 quote_plus 编码
            userinfo = f"{quote_plus(u.username or '')}:{quote_plus(u.password)}"
            port_str = f":{u.port}" if u.port else ""
            query_str = "&".join(f"{k}={v}" for k, v in (u.query or {}).items())
            rebuilt = f"{u.drivername}://{userinfo}@{u.host or ''}{port_str}/{u.database or ''}"
            if query_str:
                rebuilt += f"?{query_str}"
            return rebuilt
        return raw_url
    except Exception:
        return raw_url


from app.services.bitable_reader import bitable_reader, BitableReader



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
    owner_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(BusinessTable)
    if owner_id is not None:
        # 仅超管可按 owner_id 过滤其他用户的表
        is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
        if not is_admin and owner_id != user.id:
            return []
        q = q.filter(BusinessTable.owner_id == owner_id)
    tables = q.order_by(BusinessTable.created_at.desc()).all()
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

    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)

    result = []
    for t in tables:
        rules = t.validation_rules or {}
        row_scope = rules.get("row_scope", "private")

        if not is_admin:
            if row_scope == "private":
                # 仅自己能看到
                if t.owner_id != user.id:
                    continue
            elif row_scope == "department":
                dept_ids = rules.get("row_department_ids") or []
                if dept_ids and user.department_id not in dept_ids:
                    continue

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
    user: User = Depends(get_current_user),
):
    """Direction A: natural language → DDL + Skill preview."""
    from app.services.schema_generator import schema_generator
    model_config = llm_gateway.resolve_config(db, "business_table.generate", req.model_config_id)
    try:
        preview = await schema_generator.generate_from_description(req.description, model_config)
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/generate-from-existing")
async def generate_from_existing(
    req: GenerateFromExistingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Direction B: existing table → Skill preview."""
    from app.services.schema_generator import schema_generator
    model_config = llm_gateway.resolve_config(db, "business_table.generate", req.model_config_id)
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
        rules = dict(req.validation_rules or {})
        rules.setdefault("row_scope", "private")
        rules.setdefault("column_scope", "private")
        bt = schema_generator.register_table(
            table_name=req.table_name,
            display_name=req.display_name,
            description=req.description,
            ddl_sql=req.ddl_sql,
            validation_rules=rules,
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


class ResolveWikiRequest(BaseModel):
    wiki_token: str     # from wiki URL: /wiki/{wiki_token}


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


class ImportExternalRequest(BaseModel):
    db_url: str
    table_name: str
    display_name: str = ""


@router.post("/import-external")
def import_external_table(
    req: ImportExternalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Connect to external DB, copy schema + all data to local, register in business_tables."""
    from sqlalchemy import create_engine, inspect as sa_inspect, text as sa_text
    import re, datetime, decimal

    display = req.display_name.strip() or req.table_name
    # Sanitize local table name
    safe_name = re.sub(r"[^a-z0-9_]", "_", req.table_name.strip().lower())[:60]
    if not safe_name or safe_name[0].isdigit():
        safe_name = "ext_" + safe_name

    # Check if already registered
    existing = db.query(BusinessTable).filter(BusinessTable.table_name == safe_name).first()
    if existing:
        raise HTTPException(400, f"表 '{safe_name}' 已存在")

    try:
        ext_engine = create_engine(_safe_db_url(req.db_url), connect_args={"connect_timeout": 10})
    except Exception as e:
        raise HTTPException(400, f"数据库连接字符串无效: {e}")

    try:
        with ext_engine.connect() as ext_conn:
            insp = sa_inspect(ext_engine)
            columns = insp.get_columns(req.table_name)
            if not columns:
                raise HTTPException(400, f"表 '{req.table_name}' 无列信息")

            # Build DDL from external schema
            _TYPE_MAP = {
                "INTEGER": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
                "FLOAT": "FLOAT", "DOUBLE": "DOUBLE", "DECIMAL": "DECIMAL(20,4)",
                "NUMERIC": "DECIMAL(20,4)", "VARCHAR": "VARCHAR(500)", "CHAR": "CHAR(50)",
                "TEXT": "TEXT", "LONGTEXT": "LONGTEXT", "MEDIUMTEXT": "MEDIUMTEXT",
                "DATE": "DATE", "DATETIME": "DATETIME", "TIMESTAMP": "DATETIME",
                "BOOLEAN": "TINYINT(1)", "TINYINT": "TINYINT", "BLOB": "BLOB",
                "JSON": "JSON",
            }
            col_defs = []
            col_names = []
            for c in columns:
                cname = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", c["name"])
                col_names.append((c["name"], cname))
                type_str = str(c["type"]).upper().split("(")[0].strip()
                mysql_type = _TYPE_MAP.get(type_str, "TEXT")
                # Preserve length for VARCHAR
                raw = str(c["type"]).upper()
                if "VARCHAR" in raw and "(" in raw:
                    mysql_type = raw
                col_defs.append(f"  `{cname}` {mysql_type}")

            col_defs.append("  `_imported_at` DATETIME DEFAULT CURRENT_TIMESTAMP")
            ddl = f"CREATE TABLE IF NOT EXISTS {qi(safe_name, '表名')} (\n" + ",\n".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

            # Create local table
            db.execute(sa_text(f"DROP TABLE IF EXISTS {qi(safe_name, '表名')}"))
            db.execute(sa_text(ddl))
            db.commit()

            # Fetch all data from external
            result = ext_conn.execute(sa_text(f"SELECT * FROM {qi(req.table_name, '表名')}"))
            rows = result.fetchall()
            ext_col_keys = list(result.keys())

            # Batch insert
            if rows:
                local_cols = [cname for _, cname in col_names]
                placeholders = ", ".join([f":{cname}" for cname in local_cols])
                insert_sql = f"INSERT INTO {qi(safe_name, '表名')} ({', '.join([qi(c, '列名') for c in local_cols])}) VALUES ({placeholders})"

                def _serialize(v):
                    if v is None:
                        return None
                    if isinstance(v, (datetime.datetime, datetime.date)):
                        return v.isoformat()
                    if isinstance(v, decimal.Decimal):
                        return float(v)
                    if isinstance(v, bytes):
                        return v.decode("utf-8", errors="replace")
                    return v

                batch = []
                for row in rows:
                    row_dict = {}
                    for i, (orig_name, local_name) in enumerate(col_names):
                        idx = ext_col_keys.index(orig_name) if orig_name in ext_col_keys else i
                        row_dict[local_name] = _serialize(row[idx])
                    batch.append(row_dict)

                # Insert in chunks of 500
                for i in range(0, len(batch), 500):
                    db.execute(sa_text(insert_sql), batch[i:i+500])
                db.commit()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"导入失败: {e}")

    # Register in business_tables
    masked_url = re.sub(r":[^@]*@", ":***@", req.db_url)
    bt = BusinessTable(
        table_name=safe_name,
        display_name=display,
        description=f"从外部数据库导入: {masked_url}/{req.table_name}",
        ddl_sql=ddl,
        source_type="external_db",
        source_ref={"db_url_masked": masked_url, "remote_table": req.table_name},
        owner_id=user.id,
        record_count_cache=len(rows) if rows else 0,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    return {"id": bt.id, "table_name": safe_name, "display_name": display, "rows_imported": len(rows) if rows else 0}


@router.post("/resolve-wiki")
async def resolve_wiki(
    req: ResolveWikiRequest,
    user: User = Depends(get_current_user),
):
    """Resolve a Feishu Wiki node token → bitable app_token + table list."""
    from app.services.lark_client import lark_client
    try:
        token = await lark_client.get_tenant_access_token()
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    import httpx
    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{base}/wiki/v2/spaces/get_node",
            headers=headers,
            params={"token": req.wiki_token, "obj_type": "wiki"},
        )
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(400, f"Wiki 节点解析失败: {data.get('msg')} (code={data.get('code')})")

        node = data.get("data", {}).get("node", {})
        obj_type = node.get("obj_type", "")
        obj_token = node.get("obj_token", "")
        title = node.get("title", "")

        if obj_type != "bitable":
            raise HTTPException(400, f"该 Wiki 页面不是多维表格（类型: {obj_type}），无法导入")

        # Fetch table list for this bitable
        r2 = await client.get(
            f"{base}/bitable/v1/apps/{obj_token}/tables",
            headers=headers,
            params={"page_size": 100},
        )
        data2 = r2.json()
        tables = []
        if data2.get("code") == 0:
            tables = [
                {"table_id": t["table_id"], "name": t.get("name", t["table_id"])}
                for t in data2.get("data", {}).get("items", [])
            ]

    return {
        "app_token": obj_token,
        "title": title,
        "tables": tables,
    }


@router.post("/probe-bitable")
async def probe_bitable(
    req: ProbeBitableRequest,
    user: User = Depends(get_current_user),
):
    """Preview a Feishu Bitable table: fetch fields + first 20 records. Does NOT persist."""
    try:
        token = await bitable_reader.get_token()
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    try:
        fields = await bitable_reader.fetch_fields(token, req.app_token, req.table_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    columns = [
        {"name": f["field_name"], "type": f.get("type", 1), "nullable": True, "comment": ""}
        for f in fields
    ]

    try:
        data = await bitable_reader.fetch_records_page(token, req.app_token, req.table_id, page_size=20)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(400, f"获取记录失败: {e}")

    records = data.get("items") or []
    field_names = [c["name"] for c in columns]
    preview_rows = []
    for rec in records:
        row = {fn: rec.get("fields", {}).get(fn) for fn in field_names}
        flat = {k: BitableReader.flatten_value(v) for k, v in row.items()}
        preview_rows.append(flat)

    return {
        "app_token": req.app_token,
        "table_id": req.table_id,
        "columns": columns,
        "preview_rows": preview_rows,
    }


@router.post("/sync-bitable")
async def sync_bitable(
    req: SyncBitableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full sync: fetch ALL records from Feishu Bitable → create/replace local MySQL table → register."""
    import json as _json
    import re, time
    from app.services.bitable_sync import bitable_sync, _BITABLE_TYPE_MAP

    try:
        token = await bitable_reader.get_token()
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    # 1. Fields
    try:
        fields = await bitable_reader.fetch_fields(token, req.app_token, req.table_id)
    except PermissionError as e:
        raise HTTPException(403, _json.dumps(
            {"error": str(e), "stage": "fetch_fields", "suggestion": str(e)},
            ensure_ascii=False,
        ))
    except RuntimeError as e:
        raise HTTPException(400, _json.dumps(
            {"error": str(e), "stage": "fetch_fields", "suggestion": "请检查多维表格权限或链接是否正确"},
            ensure_ascii=False,
        ))

    # 2. All records (adaptive page size)
    try:
        all_records, fetch_stats = await bitable_reader.fetch_records_adaptive(
            token, req.app_token, req.table_id,
        )
    except PermissionError as e:
        raise HTTPException(403, _json.dumps(
            {"error": str(e), "stage": "fetch_records", "suggestion": str(e)},
            ensure_ascii=False,
        ))
    except Exception as e:
        raise HTTPException(400, _json.dumps(
            {"error": f"获取记录失败: {e}", "stage": "fetch_records",
             "suggestion": "请检查多维表格权限或缩小字段范围"},
            ensure_ascii=False,
        ))

    # 3. Derive target table name
    safe_name = req.sync_table_name.strip() if req.sync_table_name.strip() else ""
    if not safe_name:
        safe_name = f"bitable_{re.sub(r'[^a-z0-9_]', '_', req.table_id.lower()[:30])}"

    display = req.display_name.strip() or safe_name

    # 4. Build DDL
    col_defs = ["  `_record_id` VARCHAR(100) PRIMARY KEY COMMENT '飞书记录ID'"]
    field_names = []
    col_map = {}
    for f in fields:
        fn = f["field_name"]
        field_names.append(fn)
        col = BitableReader.sanitize_col(fn)
        col_map[fn] = col
        mysql_type = _BITABLE_TYPE_MAP.get(f.get("type", 1), "TEXT")
        col_defs.append(f"  `{col}` {mysql_type} COMMENT '{fn}'")
    col_defs.append("  `_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    ddl = f"CREATE TABLE IF NOT EXISTS {qi(safe_name, '表名')} (\n" + ",\n".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    # 5. Create/reset table
    try:
        db.execute(text(f"DROP TABLE IF EXISTS {qi(safe_name, '表名')}"))
        db.execute(text(ddl))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"本地建表失败，请联系管理员检查数据库: {e}")

    # 6. Insert records
    inserted = 0
    for rec in all_records:
        record_id = rec.get("record_id", "")
        flds = rec.get("fields", {})
        row_data = {"_record_id": record_id}
        for fn in field_names:
            col = col_map[fn]
            row_data[col] = BitableReader.flatten_value(flds.get(fn))
        cols_sql = ", ".join(f"`{k}`" for k in row_data)
        placeholders = ", ".join(f":{k}" for k in row_data)
        try:
            db.execute(text(f"INSERT INTO {qi(safe_name, '表名')} ({cols_sql}) VALUES ({placeholders})"), row_data)
            inserted += 1
        except Exception:
            pass
    db.commit()

    # 7. Register (upsert) with source_type + source_ref
    existing = db.query(BusinessTable).filter(BusinessTable.table_name == safe_name).first()
    if existing:
        existing.display_name = display
        existing.description = f"飞书多维表格同步 | app_token={req.app_token} | table_id={req.table_id}"
        existing.owner_id = user.id
        rules = dict(existing.validation_rules or {})
        rules.update({"bitable_app_token": req.app_token, "bitable_table_id": req.table_id, "last_synced_at": int(time.time())})
        rules.setdefault("row_scope", "private")
        rules.setdefault("column_scope", "private")
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(existing, "validation_rules")
        existing.validation_rules = rules
        existing.source_type = "lark_bitable"
        existing.source_ref = {"app_token": req.app_token, "table_id": req.table_id}
        bt = existing
    else:
        bt = BusinessTable(
            table_name=safe_name,
            display_name=display,
            description=f"飞书多维表格同步 | app_token={req.app_token} | table_id={req.table_id}",
            ddl_sql=ddl,
            validation_rules={
                "bitable_app_token": req.app_token,
                "bitable_table_id": req.table_id,
                "last_synced_at": int(time.time()),
                "row_scope": "private",
                "column_scope": "private",
            },
            owner_id=user.id,
            source_type="lark_bitable",
            source_ref={"app_token": req.app_token, "table_id": req.table_id},
        )
        db.add(bt)
    db.commit()
    db.refresh(bt)

    # 触发治理分类 job
    from app.models.knowledge_job import KnowledgeJob
    db.add(KnowledgeJob(
        subject_type="business_table", subject_id=bt.id,
        job_type="governance_classify", trigger_source="upload",
    ))
    db.commit()

    degraded_msg = f"（分页已降级到 {fetch_stats['effective_page_size']}）" if fetch_stats.get("degraded") else ""
    return {
        "ok": True,
        "table_name": safe_name,
        "id": bt.id,
        "inserted": inserted,
        "total_fields": len(fields),
        "effective_page_size": fetch_stats.get("effective_page_size"),
        "degraded": fetch_stats.get("degraded", False),
        "sync_stats": fetch_stats,
    }


@router.post("/probe")
def probe_external_table(
    req: ProbeTableRequest,
    user: User = Depends(get_current_user),
):
    """Connect to an external DB, fetch schema + first 20 rows. Does NOT persist anything."""
    from sqlalchemy import create_engine, inspect, text as sa_text
    try:
        engine = create_engine(_safe_db_url(req.db_url), connect_args={"connect_timeout": 5})
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
            result = conn.execute(sa_text(f"SELECT * FROM {qi(req.table_name, '表名')} LIMIT 20"))
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


# ─── field_type → MySQL type mapping ─────────────────────────────────────────
_FIELD_TYPE_MAP = {
    "text":         "TEXT",
    "number":       "DOUBLE",
    "select":       "VARCHAR(100)",
    "multi_select": "TEXT",
    "date":         "DATETIME",
    "person":       "INT",
    "url":          "TEXT",
    "checkbox":     "TINYINT(1)",
    "email":        "VARCHAR(255)",
    "phone":        "VARCHAR(50)",
}


class FieldDef(BaseModel):
    name: str
    field_type: str = "text"        # see _FIELD_TYPE_MAP
    options: list[str] = []         # for select / multi_select
    nullable: bool = True
    comment: str = ""


class CreateBlankTableRequest(BaseModel):
    display_name: str
    description: str = ""
    fields: list[FieldDef] = []     # extra user-defined fields (id/created_at/updated_at auto-added)
    row_scope: str = "private"      # "all" | "department" | "private"
    column_scope: str = "private"


@router.post("/create-blank")
def create_blank_table(
    req: CreateBlankTableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new blank table with user-defined fields."""
    import re, time

    if not req.display_name.strip():
        raise HTTPException(400, "display_name 不能为空")

    # Auto-generate a unique table name from display_name
    base = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "_", req.display_name.strip().lower())
    base = re.sub(r"_+", "_", base).strip("_")[:30] or "table"
    table_name = f"usr_{base}_{int(time.time()) % 100000}"

    # Validate field types
    for f in req.fields:
        if f.field_type not in _FIELD_TYPE_MAP:
            raise HTTPException(400, f"不支持的字段类型 '{f.field_type}'，可选：{list(_FIELD_TYPE_MAP.keys())}")
        if not re.match(r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$', f.name):
            raise HTTPException(400, f"字段名 '{f.name}' 格式不合法")

    # Build DDL
    col_defs = [
        "  `id` INT AUTO_INCREMENT PRIMARY KEY",
        "  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP",
        "  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    ]
    field_meta = []
    for f in req.fields:
        mysql_type = _FIELD_TYPE_MAP[f.field_type]
        null_clause = "NULL" if f.nullable else "NOT NULL"
        comment_clause = f" COMMENT '{f.comment}'" if f.comment else ""
        col_defs.append(f"  `{f.name}` {mysql_type} {null_clause}{comment_clause}")
        field_meta.append({
            "name": f.name,
            "field_type": f.field_type,
            "options": f.options,
            "nullable": f.nullable,
            "comment": f.comment,
        })

    ddl = (
        f"CREATE TABLE IF NOT EXISTS {qi(table_name, '表名')} (\n"
        + ",\n".join(col_defs)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    # Execute DDL
    try:
        db.execute(text(ddl))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"本地建表失败，请联系管理员检查数据库: {e}")

    # Register
    rules = {
        "row_scope": req.row_scope,
        "column_scope": req.column_scope,
        "field_meta": field_meta,
    }
    bt = BusinessTable(
        table_name=table_name,
        display_name=req.display_name.strip(),
        description=req.description,
        ddl_sql=ddl,
        validation_rules=rules,
        owner_id=user.id,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    # 触发治理分类 job
    from app.models.knowledge_job import KnowledgeJob
    db.add(KnowledgeJob(
        subject_type="business_table", subject_id=bt.id,
        job_type="governance_classify", trigger_source="upload",
    ))
    db.commit()

    return {"id": bt.id, "table_name": bt.table_name, "display_name": bt.display_name}


# ─── Upload CSV/Excel ─────────────────────────────────────────────────────────

@router.post("/upload-file")
async def upload_file_as_table(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a CSV or Excel file to create a new data table with data."""
    import re, time, io

    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(400, "仅支持 .csv / .xlsx / .xls 格式")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过 50MB")

    # Parse file into DataFrame
    import pandas as pd
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(400, f"文件解析失败: {e}")

    if df.empty or len(df.columns) == 0:
        raise HTTPException(400, "文件为空或无有效列")

    # Sanitize column names
    clean_cols = []
    for col in df.columns:
        c = str(col).strip()
        if not c:
            c = f"col_{len(clean_cols)}"
        clean_cols.append(c)
    df.columns = clean_cols

    # Generate table name
    display_name = filename.rsplit(".", 1)[0]
    base = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "_", display_name.strip().lower())
    base = re.sub(r"_+", "_", base).strip("_")[:30] or "table"
    table_name = f"usr_{base}_{int(time.time()) % 100000}"

    # Infer MySQL types from pandas dtypes
    col_defs = [
        "  `id` INT AUTO_INCREMENT PRIMARY KEY",
        "  `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP",
        "  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    ]
    field_meta = []
    for col in clean_cols:
        dtype = str(df[col].dtype)
        if "int" in dtype:
            mysql_type, field_type = "BIGINT", "number"
        elif "float" in dtype:
            mysql_type, field_type = "DOUBLE", "number"
        elif "datetime" in dtype:
            mysql_type, field_type = "DATETIME", "date"
        elif "bool" in dtype:
            mysql_type, field_type = "TINYINT(1)", "checkbox"
        else:
            mysql_type, field_type = "TEXT", "text"
        safe_col = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", col)
        col_defs.append(f"  `{safe_col}` {mysql_type} NULL")
        field_meta.append({"name": col, "field_type": field_type, "options": [], "nullable": True, "comment": ""})

    ddl = (
        f"CREATE TABLE IF NOT EXISTS {qi(table_name, '表名')} (\n"
        + ",\n".join(col_defs)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    try:
        db.execute(text(ddl))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"建表失败: {e}")

    # Insert data in batches
    inserted = 0
    safe_cols = [re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", c) for c in clean_cols]
    col_list = ", ".join(f"`{c}`" for c in safe_cols)
    batch_size = 500
    import math
    df = df.where(pd.notnull(df), None)

    for start in range(0, len(df), batch_size):
        batch = df.iloc[start:start + batch_size]
        placeholders = []
        params = {}
        for row_idx, (_, row) in enumerate(batch.iterrows()):
            row_ph = []
            for col_idx, col in enumerate(clean_cols):
                key = f"v_{start + row_idx}_{col_idx}"
                val = row[col]
                if val is None:
                    params[key] = None
                elif isinstance(val, float) and math.isnan(val):
                    params[key] = None
                else:
                    params[key] = val
                row_ph.append(f":{key}")
            placeholders.append(f"({', '.join(row_ph)})")
        insert_sql = f"INSERT INTO {qi(table_name, '表名')} ({col_list}) VALUES {', '.join(placeholders)}"
        try:
            db.execute(text(insert_sql), params)
            db.commit()
            inserted += len(batch)
        except Exception:
            db.rollback()

    # Register in BusinessTable
    rules = {"row_scope": "private", "column_scope": "private", "field_meta": field_meta}
    bt = BusinessTable(
        table_name=table_name,
        display_name=display_name,
        description=f"从 {filename} 导入，共 {inserted} 行",
        ddl_sql=ddl,
        validation_rules=rules,
        owner_id=user.id,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    # 触发治理分类 job
    from app.models.knowledge_job import KnowledgeJob
    db.add(KnowledgeJob(
        subject_type="business_table", subject_id=bt.id,
        job_type="governance_classify", trigger_source="upload",
    ))
    db.commit()

    return {
        "id": bt.id,
        "table_name": bt.table_name,
        "display_name": bt.display_name,
        "rows_inserted": inserted,
        "columns": len(clean_cols),
    }


# ─── Column management APIs ───────────────────────────────────────────────────

class AddColumnRequest(BaseModel):
    name: str
    field_type: str = "text"
    options: list[str] = []
    nullable: bool = True
    comment: str = ""


class RenameColumnRequest(BaseModel):
    new_name: str
    comment: str = None


@router.post("/{table_id}/columns")
def add_column(
    table_id: int,
    req: AddColumnRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a new column to an existing business table."""
    import re
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if req.field_type not in _FIELD_TYPE_MAP:
        raise HTTPException(400, f"不支持的字段类型 '{req.field_type}'")
    if not re.match(r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$', req.name):
        raise HTTPException(400, f"字段名 '{req.name}' 格式不合法")

    mysql_type = _FIELD_TYPE_MAP[req.field_type]
    null_clause = "NULL" if req.nullable else "NOT NULL"
    comment_clause = f" COMMENT '{req.comment}'" if req.comment else ""
    alter_sql = f"ALTER TABLE {qi(bt.table_name, '表名')} ADD COLUMN {qi(req.name, '列名')} {mysql_type} {null_clause}{comment_clause}"

    try:
        db.execute(text(alter_sql))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"新增列失败: {e}")

    # Update field_meta in validation_rules
    rules = dict(bt.validation_rules or {})
    meta = list(rules.get("field_meta") or [])
    meta.append({"name": req.name, "field_type": req.field_type, "options": req.options,
                  "nullable": req.nullable, "comment": req.comment})
    rules["field_meta"] = meta
    bt.validation_rules = rules
    db.commit()
    return {"ok": True, "column": req.name}


@router.patch("/{table_id}/columns/{col_name}")
def rename_column(
    table_id: int,
    col_name: str,
    req: RenameColumnRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rename a column (or update its comment)."""
    import re
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")

    # Get current column info
    col_rows = db.execute(
        text("SELECT DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"),
        {"t": bt.table_name, "c": col_name},
    ).fetchone()
    if not col_rows:
        raise HTTPException(404, f"列 '{col_name}' 不存在")

    new_name = req.new_name.strip()
    if not re.match(r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$', new_name):
        raise HTTPException(400, f"新列名 '{new_name}' 格式不合法")

    data_type = col_rows[0].upper()
    null_clause = "NULL" if col_rows[1] == "YES" else "NOT NULL"
    comment = req.comment if req.comment is not None else (col_rows[2] or "")
    comment_clause = f" COMMENT '{comment}'" if comment else ""

    alter_sql = (
        f"ALTER TABLE {qi(bt.table_name, '表名')} "
        f"CHANGE COLUMN {qi(col_name, '列名')} {qi(new_name, '列名')} {data_type} {null_clause}{comment_clause}"
    )
    try:
        db.execute(text(alter_sql))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"重命名列失败: {e}")

    # Update field_meta
    rules = dict(bt.validation_rules or {})
    meta = list(rules.get("field_meta") or [])
    for m in meta:
        if m["name"] == col_name:
            m["name"] = new_name
            if req.comment is not None:
                m["comment"] = req.comment
            break
    rules["field_meta"] = meta
    bt.validation_rules = rules
    db.commit()
    return {"ok": True, "old_name": col_name, "new_name": new_name}


@router.delete("/{table_id}/columns/{col_name}")
def drop_column(
    table_id: int,
    col_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Drop a column from a business table. Protected columns (id, created_at, updated_at) cannot be dropped."""
    PROTECTED = {"id", "created_at", "updated_at", "_record_id", "_synced_at"}
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if col_name in PROTECTED:
        raise HTTPException(400, f"列 '{col_name}' 是系统保留列，不允许删除")

    try:
        db.execute(text(f"ALTER TABLE {qi(bt.table_name, '表名')} DROP COLUMN {qi(col_name, '列名')}"))
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"删除列失败: {e}")

    # Update field_meta
    rules = dict(bt.validation_rules or {})
    meta = [m for m in (rules.get("field_meta") or []) if m["name"] != col_name]
    rules["field_meta"] = meta
    bt.validation_rules = rules
    db.commit()
    return {"ok": True, "dropped": col_name}


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


# ── 飞书多维表格同步管理 ─────────────────────────────────────────────────────


class SyncConfigRequest(BaseModel):
    sync_interval: int = 0         # 同步间隔（分钟），0=关闭定时同步
    sync_mode: str = "incremental"  # "full" | "incremental"


@router.patch("/{table_id}/sync-config")
def set_sync_config(
    table_id: int,
    req: SyncConfigRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """设置飞书多维表格定时同步配置。"""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    rules = dict(bt.validation_rules or {})
    if not rules.get("bitable_app_token"):
        raise HTTPException(400, "该表未关联飞书多维表格，无法配置同步")

    rules["sync_interval"] = req.sync_interval
    rules["sync_mode"] = req.sync_mode
    bt.validation_rules = rules
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(bt, "validation_rules")
    db.commit()
    return {"ok": True, "sync_interval": req.sync_interval, "sync_mode": req.sync_mode}


@router.post("/{table_id}/sync-now")
async def sync_now(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发飞书多维表格增量同步。"""
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")

    from app.services.bitable_sync import bitable_sync
    try:
        result = await bitable_sync.incremental_sync(db, bt)
        return {"ok": True, **result}
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")
    except Exception as e:
        raise HTTPException(500, f"同步失败: {e}")
