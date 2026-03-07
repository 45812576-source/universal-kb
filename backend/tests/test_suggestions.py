"""TC-SUGGEST: Skill suggestion submission, review, and my-list."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_skill, _login, _auth
from app.models.user import Role


def _submit_suggestion(client, token, skill_id, problem="回答太模糊", direction="请更具体"):
    return client.post(f"/api/skills/{skill_id}/suggestions", headers=_auth(token), json={
        "problem_desc": problem,
        "expected_direction": direction,
        "case_example": "用户问A，回答了B",
    })


# ── Submit ────────────────────────────────────────────────────────────────────

def test_employee_can_submit_suggestion(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin1", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp1", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill1")
    db.commit()

    token = _login(client, "semp1")
    resp = _submit_suggestion(client, token, skill.id)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_suggestion_for_nonexistent_skill(client, db):
    dept = _make_dept(db)
    _make_user(db, "semp2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "semp2")

    resp = _submit_suggestion(client, token, 99999)
    assert resp.status_code == 404


def test_suggestion_requires_auth(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin2", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill2")
    db.commit()

    resp = client.post(f"/api/skills/{skill.id}/suggestions", json={
        "problem_desc": "x", "expected_direction": "y"
    })
    assert resp.status_code in (401, 403)


# ── List (admin view) ─────────────────────────────────────────────────────────

def test_admin_can_list_suggestions(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin3", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp3", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill3")
    db.commit()

    emp_token = _login(client, "semp3")
    _submit_suggestion(client, emp_token, skill.id, "问题1", "方向1")
    _submit_suggestion(client, emp_token, skill.id, "问题2", "方向2")

    admin_token = _login(client, "sadmin3")
    resp = client.get(f"/api/skills/{skill.id}/suggestions", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_suggestions_filter_by_status(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin4", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp4", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill4")
    db.commit()

    emp_token = _login(client, "semp4")
    r = _submit_suggestion(client, emp_token, skill.id)
    suggestion_id = r.json()["id"]

    admin_token = _login(client, "sadmin4")
    # Review one as adopted
    client.patch(f"/api/skill-suggestions/{suggestion_id}/review",
                 headers=_auth(admin_token),
                 json={"status": "adopted", "review_note": "好"})

    resp = client.get(f"/api/skills/{skill.id}/suggestions?status=adopted", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert all(s["status"] == "adopted" for s in resp.json())


# ── Review ────────────────────────────────────────────────────────────────────

def test_admin_review_suggestion(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin5", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp5", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill5")
    db.commit()

    emp_token = _login(client, "semp5")
    r = _submit_suggestion(client, emp_token, skill.id)
    suggestion_id = r.json()["id"]

    admin_token = _login(client, "sadmin5")
    resp = client.patch(f"/api/skill-suggestions/{suggestion_id}/review",
                        headers=_auth(admin_token),
                        json={"status": "partial", "review_note": "部分好"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "partial"


def test_employee_cannot_review(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin6", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp6", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill6")
    db.commit()

    emp_token = _login(client, "semp6")
    r = _submit_suggestion(client, emp_token, skill.id)
    suggestion_id = r.json()["id"]

    resp = client.patch(f"/api/skill-suggestions/{suggestion_id}/review",
                        headers=_auth(emp_token),
                        json={"status": "adopted"})
    assert resp.status_code == 403


def test_review_invalid_status(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin7", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "semp7", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill7")
    db.commit()

    emp_token = _login(client, "semp7")
    r = _submit_suggestion(client, emp_token, skill.id)

    admin_token = _login(client, "sadmin7")
    resp = client.patch(f"/api/skill-suggestions/{r.json()['id']}/review",
                        headers=_auth(admin_token),
                        json={"status": "pending"})  # pending is not a valid review status
    assert resp.status_code == 400


# ── My suggestions ────────────────────────────────────────────────────────────

def test_my_suggestions_only_own(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "sadmin8", Role.SUPER_ADMIN, dept.id)
    emp1 = _make_user(db, "semp8a", Role.EMPLOYEE, dept.id)
    emp2 = _make_user(db, "semp8b", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "SuggestSkill8")
    db.commit()

    t1 = _login(client, "semp8a")
    t2 = _login(client, "semp8b")
    _submit_suggestion(client, t1, skill.id, "emp1 problem", "emp1 direction")
    _submit_suggestion(client, t2, skill.id, "emp2 problem", "emp2 direction")

    resp = client.get("/api/my/suggestions", headers=_auth(t1))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["problem_desc"] == "emp1 problem"
