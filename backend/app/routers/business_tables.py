"""Business tables management API."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.business import BusinessTable, DataOwnership, VisibilityLevel, SkillDataQuery
from app.models.user import User, Role, Department
from app.models.skill import Skill
from app.services.llm_gateway import llm_gateway
from app.services.lark_client import LarkConfigError, LarkAuthError

router = APIRouter(prefix="/api/business-tables", tags=["business-tables"])


def _flatten_bitable_cell(v):
    """将飞书多维表格单元格的原始值展平为可读字符串或基础类型。

    覆盖类型：
      type=1  多行文本    -> list of {text, type}
      type=2  数字        -> float
      type=3  单选        -> str
      type=4  多选        -> list of str
      type=5  日期        -> ms timestamp -> 北京时间字符串
      type=11 人员        -> list of {name, ...}
      type=17 附件        -> list of {name, ...}
      type=18 单向关联    -> {link_record_ids: [...]}
      type=19 查找引用    -> {type, value: [...]}  value 内容与对应字段类型一致
      type=20 公式/查找   -> {type, value: [...]}  同上
    """
    import datetime

    if v is None:
        return None

    # ── 查找引用 / 公式（type=19/20）：外层是 {type, value:[...]} ──
    if isinstance(v, dict) and "type" in v and "value" in v:
        inner_type = v["type"]
        inner_vals = v["value"]
        if not isinstance(inner_vals, list) or not inner_vals:
            return None
        # 对 value 数组里每一项递归解析，再拼接
        parts = []
        for item in inner_vals:
            parts.append(_flatten_bitable_cell_inner(item, inner_type))
        result = "、".join(str(p) for p in parts if p not in (None, ""))
        return result if result else None

    # ── 单向/双向关联 {link_record_ids: [...]} ──
    if isinstance(v, dict) and "link_record_ids" in v:
        ids = v["link_record_ids"]
        if not ids:
            return None
        return "、".join(str(i) for i in ids)

    # ── 普通 list（多行文本 type=1、附件 type=17 等）──
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "name" in item:
                    parts.append(str(item["name"]))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "".join(parts) if parts else None

    # ── 毫秒时间戳（type=5 日期直接返回数字）──
    if isinstance(v, (int, float)) and v > 1e12:
        dt = datetime.datetime.fromtimestamp(v / 1000, tz=datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # ── 基础类型 str/int/float/bool ──
    return v


def _flatten_bitable_cell_inner(item, field_type: int):
    """处理 value 数组内的单个元素（用于查找引用/公式字段）。"""
    import datetime

    if item is None:
        return None

    # type=5 日期：ms 时间戳
    if field_type == 5 and isinstance(item, (int, float)):
        dt = datetime.datetime.fromtimestamp(item / 1000, tz=datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d")

    # type=11 人员：{name, ...}
    if field_type == 11 and isinstance(item, dict):
        return item.get("name") or item.get("display_name") or item.get("id", "")

    # type=1 多行文本：{text, type}
    if isinstance(item, dict) and "text" in item:
        return str(item["text"])

    # type=17 附件：{name, ...}
    if isinstance(item, dict) and "name" in item:
        return str(item["name"])

    # 数字/字符串/布尔直接返回
    if isinstance(item, (int, float)):
        return str(item) if field_type not in (2, 20) else item
    if isinstance(item, str):
        return item

    return str(item)



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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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


@router.post("/resolve-wiki")
async def resolve_wiki(
    req: ResolveWikiRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Preview a Feishu Bitable table: fetch fields + first 20 records. Does NOT persist."""
    from app.services.lark_client import lark_client
    try:
        token = await lark_client.get_tenant_access_token()
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

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
            flat = {k: _flatten_bitable_cell(v) for k, v in row.items()}
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
    except LarkConfigError:
        raise HTTPException(503, "飞书集成尚未配置，请联系管理员")
    except LarkAuthError:
        raise HTTPException(502, "飞书认证失败，请检查应用配置")

    base = "https://open.feishu.cn/open-apis"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

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
        raise HTTPException(500, f"本地建表失败，请联系管理员检查数据库: {e}")

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
        existing.owner_id = user.id
        rules = dict(existing.validation_rules or {})
        rules.update({"bitable_app_token": req.app_token, "bitable_table_id": req.table_id, "last_synced_at": int(time.time())})
        rules.setdefault("row_scope", "private")
        rules.setdefault("column_scope", "private")
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(existing, "validation_rules")
        existing.validation_rules = rules
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
        f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n"
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
    return {"id": bt.id, "table_name": bt.table_name, "display_name": bt.display_name}


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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
    alter_sql = f"ALTER TABLE `{bt.table_name}` ADD COLUMN `{req.name}` {mysql_type} {null_clause}{comment_clause}"

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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
        f"ALTER TABLE `{bt.table_name}` "
        f"CHANGE COLUMN `{col_name}` `{new_name}` {data_type} {null_clause}{comment_clause}"
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Drop a column from a business table. Protected columns (id, created_at, updated_at) cannot be dropped."""
    PROTECTED = {"id", "created_at", "updated_at", "_record_id", "_synced_at"}
    bt = db.get(BusinessTable, table_id)
    if not bt:
        raise HTTPException(404, "Business table not found")
    if col_name in PROTECTED:
        raise HTTPException(400, f"列 '{col_name}' 是系统保留列，不允许删除")

    try:
        db.execute(text(f"ALTER TABLE `{bt.table_name}` DROP COLUMN `{col_name}`"))
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
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
