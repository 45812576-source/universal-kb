"""TC-BIZ: BusinessTable registration, ownership rules, and role enforcement.

Note: generate/generate-from-existing endpoints require LLM, skipped here.
      data_tables (row CRUD) uses INFORMATION_SCHEMA and dynamic DDL, skipped here.
      apply_schema with DDL execution also skipped (SQLite doesn't support IF NOT EXISTS well).
"""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.models.business import BusinessTable, DataFolder, DataOwnership


def _make_business_table(db, table_name="test_bt", display_name="测试业务表", owner_id=1, **kwargs):
    bt = BusinessTable(
        table_name=table_name,
        display_name=display_name,
        description="测试用",
        ddl_sql="CREATE TABLE test_bt (id INT PRIMARY KEY);",
        validation_rules={},
        workflow={},
        owner_id=owner_id,
        **kwargs,
    )
    db.add(bt)
    db.flush()
    return bt


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_business_tables_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "badmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "badmin1")

    resp = client.get("/api/business-tables", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_business_tables_returns_registered(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin2", Role.SUPER_ADMIN, dept.id)
    _make_business_table(db, "sales_data", "销售数据", admin.id)
    _make_business_table(db, "customer_data", "客户数据", admin.id)
    db.commit()
    token = _login(client, "badmin2")

    resp = client.get("/api/business-tables", headers=_auth(token))
    assert resp.status_code == 200
    names = [t["table_name"] for t in resp.json()]
    assert "sales_data" in names
    assert "customer_data" in names


def test_list_requires_auth(client):
    resp = client.get("/api/business-tables")
    assert resp.status_code in (401, 403)


def test_list_hides_unpublished_company_table_from_non_owner(client, db):
    dept = _make_dept(db)
    owner = _make_user(db, "biz_owner_private", Role.EMPLOYEE, dept.id)
    peer = _make_user(db, "biz_peer_private", Role.EMPLOYEE, dept.id)
    folder = DataFolder(name="旧接口公司目录", workspace_scope="company")
    db.add(folder)
    db.flush()
    _make_business_table(db, "legacy_hidden_company", "旧接口未发布表", owner.id, folder_id=folder.id)
    db.commit()

    token = _login(client, "biz_peer_private")
    resp = client.get("/api/business-tables", headers=_auth(token))
    assert resp.status_code == 200
    assert all(t["table_name"] != "legacy_hidden_company" for t in resp.json())


# ── Get Detail ────────────────────────────────────────────────────────────────

def test_get_business_table_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "badmin3", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "badmin3")

    resp = client.get("/api/business-tables/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_get_business_table_detail(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin4", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "detail_table", "详情表", admin.id)
    db.commit()
    token = _login(client, "badmin4")

    resp = client.get(f"/api/business-tables/{bt.id}", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["table_name"] == "detail_table"
    assert data["display_name"] == "详情表"


def test_get_business_table_detail_forbids_unpublished_non_owner(client, db):
    dept = _make_dept(db)
    owner = _make_user(db, "biz_detail_owner", Role.EMPLOYEE, dept.id)
    peer = _make_user(db, "biz_detail_peer", Role.EMPLOYEE, dept.id)
    folder = DataFolder(name="旧详情公司目录", workspace_scope="company")
    db.add(folder)
    db.flush()
    bt = _make_business_table(db, "legacy_detail_hidden", "旧详情未发布表", owner.id, folder_id=folder.id)
    db.commit()

    token = _login(client, "biz_detail_peer")
    resp = client.get(f"/api/business-tables/{bt.id}", headers=_auth(token))
    assert resp.status_code == 403


# ── Apply (register without DDL execution) ────────────────────────────────────

def test_apply_registers_table(client, db):
    dept = _make_dept(db)
    _make_user(db, "badmin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "badmin5")

    resp = client.post("/api/business-tables/apply", headers=_auth(token), json={
        "table_name": "new_biz_table",
        "display_name": "新业务表",
        "description": "描述",
        "ddl_sql": "",  # no DDL execution
        "validation_rules": {},
        "workflow": {},
        "create_skill": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["table_name"] == "new_biz_table"


def test_apply_duplicate_table_fails(client, db):
    dept = _make_dept(db)
    _make_user(db, "badmin6", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "badmin6")

    payload = {
        "table_name": "dup_table",
        "display_name": "重复表",
        "description": "x",
        "ddl_sql": "",
        "create_skill": False,
    }
    client.post("/api/business-tables/apply", headers=_auth(token), json=payload)
    resp = client.post("/api/business-tables/apply", headers=_auth(token), json=payload)
    assert resp.status_code == 400


def test_apply_requires_admin(client, db):
    dept = _make_dept(db)
    _make_user(db, "bemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "bemp1")

    resp = client.post("/api/business-tables/apply", headers=_auth(token), json={
        "table_name": "forbidden_table",
        "display_name": "x",
        "description": "x",
        "ddl_sql": "",
        "create_skill": False,
    })
    assert resp.status_code == 403


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_business_table(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin7", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "to_delete", "删除表", admin.id)
    db.commit()
    token = _login(client, "badmin7")

    resp = client.delete(f"/api/business-tables/{bt.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get(f"/api/business-tables/{bt.id}", headers=_auth(token))
    assert resp.status_code == 404


def test_delete_requires_super_admin(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin8x", Role.SUPER_ADMIN, dept.id)
    dept_admin = _make_user(db, "bdept1", Role.DEPT_ADMIN, dept.id)
    bt = _make_business_table(db, "protected_table", "受保护表", admin.id)
    db.commit()
    token = _login(client, "bdept1")

    resp = client.delete(f"/api/business-tables/{bt.id}", headers=_auth(token))
    assert resp.status_code == 403


def test_delete_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "badmin9", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "badmin9")

    resp = client.delete("/api/business-tables/99999", headers=_auth(token))
    assert resp.status_code == 404


# ── Ownership Rules ───────────────────────────────────────────────────────────

def test_get_ownership_no_rule(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin10", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "no_rule_table", "无规则表", admin.id)
    db.commit()
    token = _login(client, "badmin10")

    resp = client.get(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_ownership_forbids_unpublished_non_owner(client, db):
    dept = _make_dept(db)
    owner = _make_user(db, "biz_owner_rule_owner", Role.EMPLOYEE, dept.id)
    peer = _make_user(db, "biz_owner_rule_peer", Role.EMPLOYEE, dept.id)
    folder = DataFolder(name="旧归属公司目录", workspace_scope="company")
    db.add(folder)
    db.flush()
    bt = _make_business_table(db, "legacy_rule_hidden", "旧归属未发布表", owner.id, folder_id=folder.id)
    db.add(DataOwnership(table_name=bt.table_name, owner_field="owner_id", visibility_level="detail"))
    db.commit()

    token = _login(client, "biz_owner_rule_peer")
    resp = client.get(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token))
    assert resp.status_code == 403


def test_patch_business_table_forbids_non_owner(client, db):
    dept = _make_dept(db)
    owner = _make_user(db, "biz_patch_owner", Role.EMPLOYEE, dept.id)
    peer = _make_user(db, "biz_patch_peer", Role.EMPLOYEE, dept.id)
    folder = DataFolder(name="旧编辑公司目录", workspace_scope="company")
    db.add(folder)
    db.flush()
    bt = _make_business_table(
        db,
        "legacy_patch_published",
        "旧编辑已发布表",
        owner.id,
        folder_id=folder.id,
        publish_status="published",
    )
    db.commit()

    token = _login(client, "biz_patch_peer")
    resp = client.patch(
        f"/api/business-tables/{bt.id}",
        headers=_auth(token),
        json={"display_name": "被别人改名"},
    )
    assert resp.status_code == 403


def test_set_and_get_ownership(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin11", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "owned_table", "有归属表", admin.id)
    db.commit()
    token = _login(client, "badmin11")

    resp = client.put(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token), json={
        "owner_field": "user_id",
        "department_field": "dept_id",
        "visibility_level": "detail",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner_field"] == "user_id"
    assert data["department_field"] == "dept_id"
    assert data["visibility_level"] == "detail"


def test_update_existing_ownership(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin12", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "update_rule_table", "更新规则表", admin.id)
    db.commit()
    token = _login(client, "badmin12")

    client.put(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token), json={
        "owner_field": "rep_id",
        "visibility_level": "detail",
    })
    # Update
    resp = client.put(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token), json={
        "owner_field": "sales_id",
        "visibility_level": "desensitized",
    })
    assert resp.status_code == 200

    rule = client.get(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token)).json()
    assert rule["owner_field"] == "sales_id"
    assert rule["visibility_level"] == "desensitized"


def test_set_ownership_invalid_visibility(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin13", Role.SUPER_ADMIN, dept.id)
    bt = _make_business_table(db, "bad_vis_table", "错误可见性表", admin.id)
    db.commit()
    token = _login(client, "badmin13")

    resp = client.put(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token), json={
        "owner_field": "user_id",
        "visibility_level": "invalid_level",
    })
    assert resp.status_code == 400


def test_set_ownership_employee_forbidden(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "badmin14", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "bemp2", Role.EMPLOYEE, dept.id)
    bt = _make_business_table(db, "emp_forbidden_table", "员工禁止表", admin.id)
    db.commit()
    token = _login(client, "bemp2")

    resp = client.put(f"/api/business-tables/{bt.id}/ownership", headers=_auth(token), json={
        "owner_field": "user_id",
        "visibility_level": "detail",
    })
    assert resp.status_code == 403
