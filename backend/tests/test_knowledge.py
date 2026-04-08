"""TC-KNOWLEDGE: Knowledge entry CRUD, review workflow, and role visibility."""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role


def _create_entry(client, token, title="测试经验", content="测试内容"):
    return client.post("/api/knowledge", headers=_auth(token), json={
        "title": title,
        "content": content,
        "category": "experience",
        "industry_tags": ["食品"],
        "platform_tags": ["抖音"],
        "topic_tags": ["投放策略"],
    })


# ── Create ────────────────────────────────────────────────────────────────────

def test_employee_can_create_knowledge(client, db):
    dept = _make_dept(db)
    _make_user(db, "kemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "kemp1")

    resp = _create_entry(client, token)
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["status"] == "pending"


def test_create_knowledge_requires_auth(client):
    resp = client.post("/api/knowledge", json={
        "title": "未登录经验",
        "content": "内容",
    })
    assert resp.status_code in (401, 403)


def test_create_knowledge_minimal_fields(client, db):
    dept = _make_dept(db)
    _make_user(db, "kemp2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "kemp2")

    resp = client.post("/api/knowledge", headers=_auth(token), json={
        "title": "最小字段",
        "content": "只有标题和内容",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


# ── List ──────────────────────────────────────────────────────────────────────

def test_employee_sees_own_pending_entries(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin1", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp3", Role.EMPLOYEE, dept.id)
    db.commit()

    token = _login(client, "kemp3")
    _create_entry(client, token, "我的经验", "我的内容")

    # Emp can see their own pending entry
    resp = client.get("/api/knowledge", headers=_auth(token))
    assert resp.status_code == 200
    assert any(e["title"] == "我的经验" for e in resp.json())


def test_employee_cannot_see_other_pending_entries(client, db):
    dept = _make_dept(db)
    _make_user(db, "kadmin2", Role.SUPER_ADMIN, dept.id)
    emp1 = _make_user(db, "kemp4a", Role.EMPLOYEE, dept.id)
    emp2 = _make_user(db, "kemp4b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "kemp4a")
    t2 = _login(client, "kemp4b")
    _create_entry(client, t1, "emp1私有", "内容1")

    resp = client.get("/api/knowledge", headers=_auth(t2))
    titles = [e["title"] for e in resp.json()]
    assert "emp1私有" not in titles


def test_super_admin_sees_own_and_approved_entries(client, db):
    """SUPER_ADMIN 可见全部知识（与 _can_view_entry 语义一致）。"""
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin3", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp5", Role.EMPLOYEE, dept.id)
    db.commit()

    emp_token = _login(client, "kemp5")
    _create_entry(client, emp_token, "待审经验", "内容")

    admin_token = _login(client, "kadmin3")
    _create_entry(client, admin_token, "管理员经验", "内容")

    resp = client.get("/api/knowledge", headers=_auth(admin_token))
    assert resp.status_code == 200
    titles = [e["title"] for e in resp.json()]
    # admin 能看到自己创建的
    assert "管理员经验" in titles
    # super_admin 能看到所有人的所有状态
    assert "待审经验" in titles


def test_list_knowledge_filter_by_category(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin4", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "kadmin4")

    client.post("/api/knowledge", headers=_auth(token), json={
        "title": "经验类", "content": "x", "category": "experience",
    })
    client.post("/api/knowledge", headers=_auth(token), json={
        "title": "情报类", "content": "y", "category": "external_intel",
    })

    resp = client.get("/api/knowledge?category=external_intel", headers=_auth(token))
    assert resp.status_code == 200
    titles = [e["title"] for e in resp.json()]
    assert "情报类" in titles
    assert "经验类" not in titles


# ── Get Detail ────────────────────────────────────────────────────────────────

def test_get_knowledge_detail(client, db):
    dept = _make_dept(db)
    _make_user(db, "kadmin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "kadmin5")

    resp = _create_entry(client, token, "详情测试", "完整内容这里")
    kid = resp.json()["id"]

    resp = client.get(f"/api/knowledge/{kid}", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "详情测试"
    assert data["content"] == "完整内容这里"  # full content in detail


def test_get_knowledge_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "kadmin6", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "kadmin6")

    resp = client.get("/api/knowledge/99999", headers=_auth(token))
    assert resp.status_code == 404


# ── Review ────────────────────────────────────────────────────────────────────

def test_admin_can_approve_knowledge(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin7", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp6", Role.EMPLOYEE, dept.id)
    db.commit()

    emp_token = _login(client, "kemp6")
    r = _create_entry(client, emp_token, "待批准", "内容")
    kid = r.json()["id"]

    admin_token = _login(client, "kadmin7")
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={
        "action": "approve", "note": "质量不错"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_admin_can_reject_knowledge(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin8", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp7", Role.EMPLOYEE, dept.id)
    db.commit()

    emp_token = _login(client, "kemp7")
    r = _create_entry(client, emp_token, "待拒绝", "内容")
    kid = r.json()["id"]

    admin_token = _login(client, "kadmin8")
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={
        "action": "reject", "note": "内容不符合规范"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_employee_cannot_review_knowledge(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin9", Role.SUPER_ADMIN, dept.id)
    emp1 = _make_user(db, "kemp8a", Role.EMPLOYEE, dept.id)
    emp2 = _make_user(db, "kemp8b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "kemp8a")
    r = _create_entry(client, t1, "他人经验", "内容")
    kid = r.json()["id"]

    t2 = _login(client, "kemp8b")
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(t2), json={
        "action": "approve",
    })
    assert resp.status_code == 403


def test_review_invalid_action(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin10", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp9", Role.EMPLOYEE, dept.id)
    db.commit()

    emp_token = _login(client, "kemp9")
    r = _create_entry(client, emp_token, "无效操作", "内容")
    kid = r.json()["id"]

    admin_token = _login(client, "kadmin10")
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={
        "action": "invalid_action",
    })
    assert resp.status_code == 400


def test_review_already_reviewed_entry(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin11", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "kemp10", Role.EMPLOYEE, dept.id)
    db.commit()

    emp_token = _login(client, "kemp10")
    r = _create_entry(client, emp_token, "重复审核", "内容")
    kid = r.json()["id"]

    admin_token = _login(client, "kadmin11")
    client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={"action": "approve"})
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={"action": "reject"})
    assert resp.status_code == 400


def test_approved_entry_visible_to_all_employees(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "kadmin12", Role.SUPER_ADMIN, dept.id)
    emp1 = _make_user(db, "kemp11a", Role.EMPLOYEE, dept.id)
    emp2 = _make_user(db, "kemp11b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "kemp11a")
    r = _create_entry(client, t1, "公开经验", "内容")
    kid = r.json()["id"]

    admin_token = _login(client, "kadmin12")
    client.post(f"/api/knowledge/{kid}/review", headers=_auth(admin_token), json={"action": "approve"})

    t2 = _login(client, "kemp11b")
    resp = client.get("/api/knowledge", headers=_auth(t2))
    titles = [e["title"] for e in resp.json()]
    assert "公开经验" in titles


def test_dept_admin_can_only_review_own_dept(client, db):
    dept_a = _make_dept(db, name="部门A")
    dept_b = _make_dept(db, name="部门B")
    from app.models.user import Department
    dept_a_id = dept_a.id
    dept_b_id = dept_b.id

    dept_admin = _make_user(db, "kdadmin1", Role.DEPT_ADMIN, dept_b_id)
    emp = _make_user(db, "kemp12", Role.EMPLOYEE, dept_a_id)
    db.commit()

    emp_token = _login(client, "kemp12")
    r = _create_entry(client, emp_token, "部门A经验", "内容")
    kid = r.json()["id"]

    dadmin_token = _login(client, "kdadmin1")
    resp = client.post(f"/api/knowledge/{kid}/review", headers=_auth(dadmin_token), json={
        "action": "approve",
    })
    assert resp.status_code == 403
