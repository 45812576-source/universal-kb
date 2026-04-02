from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage
from app.models.user import Role
from tests.conftest import _auth, _login, _make_dept, _make_user


def _titles(resp):
    return [item["title"] for item in resp.json()]


def test_employee_can_see_own_pending_only(client, db):
    dept = _make_dept(db, "矩阵部门A")
    owner = _make_user(db, "matrix_emp_owner", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "matrix_emp_other", Role.EMPLOYEE, dept.id)
    db.add_all([
        KnowledgeEntry(title="自己的待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=owner.id, department_id=dept.id),
        KnowledgeEntry(title="别人的待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=other.id, department_id=dept.id),
        KnowledgeEntry(title="别人的已通过", content="x", category="experience", status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED, created_by=other.id, department_id=dept.id),
    ])
    db.commit()

    token = _login(client, "matrix_emp_owner")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "自己的待审" in titles
    assert "别人的待审" not in titles
    assert "别人的已通过" in titles


def test_dept_admin_can_see_same_dept_pending_but_not_other_dept_pending(client, db):
    dept_a = _make_dept(db, "矩阵部门B")
    dept_b = _make_dept(db, "矩阵部门C")
    admin = _make_user(db, "matrix_admin", Role.DEPT_ADMIN, dept_a.id)
    same_dept = _make_user(db, "matrix_same", Role.EMPLOYEE, dept_a.id)
    other_dept = _make_user(db, "matrix_other", Role.EMPLOYEE, dept_b.id)
    db.add_all([
        KnowledgeEntry(title="同部门待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=same_dept.id, department_id=dept_a.id),
        KnowledgeEntry(title="外部门待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=other_dept.id, department_id=dept_b.id),
    ])
    db.commit()

    token = _login(client, "matrix_admin")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "同部门待审" in titles
    assert "外部门待审" not in titles


def test_super_admin_can_see_all_statuses(client, db):
    dept = _make_dept(db, "矩阵部门D")
    owner = _make_user(db, "matrix_owner", Role.EMPLOYEE, dept.id)
    sa = _make_user(db, "matrix_sa", Role.SUPER_ADMIN, dept.id)
    db.add_all([
        KnowledgeEntry(title="超管看待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=owner.id, department_id=dept.id),
        KnowledgeEntry(title="超管看通过", content="x", category="experience", status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED, created_by=owner.id, department_id=dept.id),
    ])
    db.commit()

    token = _login(client, "matrix_sa")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "超管看待审" in titles
    assert "超管看通过" in titles
