"""测试数据表智能采样接口 /data/{table_name}/sample。"""
import pytest
from sqlalchemy import text
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.models.business import BusinessTable, TableField


def _setup(db, client, table_name="test_sample_tbl"):
    """创建管理员、注册 BusinessTable、建物理表并灌测试数据。"""
    dept = _make_dept(db)
    admin = _make_user(db, "sample_admin", Role.SUPER_ADMIN, dept.id)

    bt = BusinessTable(
        table_name=table_name,
        display_name="采样测试表",
        description="",
        ddl_sql="",
        validation_rules={"row_scope": "all"},
        workflow={},
        owner_id=admin.id,
    )
    db.add(bt)
    db.flush()

    # 注册字段元信息
    db.add(TableField(
        table_id=bt.id, field_name="status", display_name="状态",
        field_type="single_select", is_enum=True,
        enum_values=["待处理", "进行中", "已完成"],
    ))
    db.add(TableField(
        table_id=bt.id, field_name="category", display_name="分类",
        field_type="single_select", is_enum=True,
        enum_values=["A", "B", "C"],
    ))
    db.add(TableField(
        table_id=bt.id, field_name="content", display_name="内容",
        field_type="text", is_enum=False,
    ))
    db.flush()

    # 创建物理表
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT,
            category TEXT,
            content TEXT
        )
    """))

    # 灌数据：3 个 status x 3 个 category = 9 种组合，每种 5 条 = 45 条
    rows = []
    for s in ["待处理", "进行中", "已完成"]:
        for c in ["A", "B", "C"]:
            for i in range(5):
                rows.append({"status": s, "category": c, "content": f"内容_{s}_{c}_{i}"})
    for r in rows:
        db.execute(text(f"INSERT INTO {table_name} (status, category, content) VALUES (:status, :category, :content)"), r)
    db.commit()

    token = _login(client, "sample_admin")
    return bt, token


def test_sample_returns_total_and_rows(client, db):
    """sample 接口应返回 total(全量) 和 rows(采样)。"""
    bt, token = _setup(db, client, table_name="test_sample_basic")

    resp = client.get(f"/api/data/{bt.table_name}/sample?max_rows=200", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()

    assert data["total"] == 45
    assert len(data["rows"]) <= 200
    assert len(data["rows"]) > 0
    assert "columns" in data
    assert "sample_strategy" in data


def test_sample_covers_all_enum_values(client, db):
    """每个枚举字段的每种值至少出现在一行中。"""
    bt, token = _setup(db, client, table_name="test_sample_enum")

    resp = client.get(f"/api/data/{bt.table_name}/sample?max_rows=200", headers=_auth(token))
    data = resp.json()

    rows = data["rows"]
    strategy = data["sample_strategy"]

    # 检查 strategy 报告的枚举字段
    enum_fields = {ef["field"] for ef in strategy["enum_fields"]}
    assert "status" in enum_fields
    assert "category" in enum_fields

    # 检查实际 rows 中每种枚举值都有覆盖
    status_values = {r["status"] for r in rows if r.get("status")}
    assert status_values >= {"待处理", "进行中", "已完成"}

    category_values = {r["category"] for r in rows if r.get("category")}
    assert category_values >= {"A", "B", "C"}


def test_sample_respects_max_rows(client, db):
    """max_rows 限制应生效。"""
    bt, token = _setup(db, client, table_name="test_sample_maxrows")

    # 只要 5 行
    resp = client.get(f"/api/data/{bt.table_name}/sample?max_rows=5", headers=_auth(token))
    data = resp.json()

    assert data["total"] == 45  # total 仍是全量
    assert len(data["rows"]) <= 5


def test_sample_small_table_returns_all(client, db):
    """当表行数 < max_rows 时，应返回全部数据。"""
    dept = _make_dept(db, "小表部门")
    admin = _make_user(db, "sample_admin2", Role.SUPER_ADMIN, dept.id)
    table_name = "test_small_tbl"

    bt = BusinessTable(
        table_name=table_name, display_name="小表", description="",
        ddl_sql="", validation_rules={"row_scope": "all"}, workflow={},
        owner_id=admin.id,
    )
    db.add(bt)
    db.flush()
    db.execute(text(f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"))
    for i in range(3):
        db.execute(text(f"INSERT INTO {table_name} (name) VALUES (:n)"), {"n": f"item_{i}"})
    db.commit()

    token = _login(client, "sample_admin2")
    resp = client.get(f"/api/data/{table_name}/sample?max_rows=200", headers=_auth(token))
    data = resp.json()

    assert data["total"] == 3
    assert len(data["rows"]) == 3
