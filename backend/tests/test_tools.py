"""TC-TOOLS: Tool registry CRUD, Skill binding, role enforcement."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_skill, _make_tool, _login, _auth
from app.models.user import Role
from app.models.tool import ToolType


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_list_tools_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin1")
    resp = client.get("/api/tools", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_tool_as_admin(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin2")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "weather_tool",
        "display_name": "天气查询",
        "description": "查询天气信息",
        "tool_type": "builtin",
        "config": {},
        "input_schema": {"city": "string"},
        "output_format": "json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "weather_tool"


def test_create_tool_duplicate_name_fails(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin3", Role.SUPER_ADMIN, dept.id)
    _make_tool(db, admin.id, "dup_tool")
    db.commit()
    token = _login(client, "tadmin3")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "dup_tool",
        "display_name": "重复",
        "tool_type": "builtin",
    })
    assert resp.status_code == 400


def test_create_tool_employee_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "temp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "temp1")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "emp_tool",
        "display_name": "x",
        "tool_type": "builtin",
    })
    assert resp.status_code == 403


def test_get_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin4", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "get_tool")
    db.commit()
    token = _login(client, "tadmin4")
    resp = client.get(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["name"] == "get_tool"


def test_get_tool_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin5")
    resp = client.get("/api/tools/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_update_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin6", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "upd_tool")
    db.commit()
    token = _login(client, "tadmin6")
    resp = client.put(f"/api/tools/{tool.id}", headers=_auth(token), json={
        "display_name": "更新后工具",
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "更新后工具"
    assert resp.json()["is_active"] is False


def test_delete_tool_requires_super_admin(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin7s", Role.SUPER_ADMIN, dept.id)
    _make_user(db, "tdept1", Role.DEPT_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "del_tool")
    db.commit()
    token = _login(client, "tdept1")
    resp = client.delete(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 403


def test_delete_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin8", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "gone_tool")
    db.commit()
    token = _login(client, "tadmin8")
    resp = client.delete(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.get(f"/api/tools/{tool.id}", headers=_auth(token)).status_code == 404


# ── Skill binding ─────────────────────────────────────────────────────────────

def test_bind_tool_to_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin9", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "绑定Skill")
    tool = _make_tool(db, admin.id, "bind_tool")
    db.commit()
    token = _login(client, "tadmin9")
    resp = client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_bind_tool_idempotent(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin10", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "幂等Skill")
    tool = _make_tool(db, admin.id, "idem_tool")
    db.commit()
    token = _login(client, "tadmin10")
    client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    resp = client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200  # idempotent


def test_get_skill_tools(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin11", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "工具列表Skill")
    t1 = _make_tool(db, admin.id, "tool_list_1")
    t2 = _make_tool(db, admin.id, "tool_list_2")
    db.commit()
    token = _login(client, "tadmin11")
    client.post(f"/api/tools/skill/{skill.id}/tools/{t1.id}", headers=_auth(token))
    client.post(f"/api/tools/skill/{skill.id}/tools/{t2.id}", headers=_auth(token))
    resp = client.get(f"/api/tools/skill/{skill.id}/tools", headers=_auth(token))
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "tool_list_1" in names
    assert "tool_list_2" in names


def test_unbind_tool_from_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin12", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "解绑Skill")
    tool = _make_tool(db, admin.id, "unbind_tool")
    db.commit()
    token = _login(client, "tadmin12")
    client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    resp = client.delete(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    resp2 = client.get(f"/api/tools/skill/{skill.id}/tools", headers=_auth(token))
    assert not any(t["name"] == "unbind_tool" for t in resp2.json())
