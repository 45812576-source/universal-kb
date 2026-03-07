"""TC-CONTRIB: Contribution stats and leaderboard."""
import pytest
from tests.conftest import (
    _make_user, _make_dept, _make_skill, _login, _auth
)
from app.models.user import Role
from app.models.skill import SkillSuggestion, SkillAttribution, AttributionLevel, SuggestionStatus


def _make_suggestion(db, skill_id, user_id, status=SuggestionStatus.PENDING):
    s = SkillSuggestion(
        skill_id=skill_id,
        submitted_by=user_id,
        problem_desc="问题描述",
        expected_direction="改进方向",
        status=status,
    )
    db.add(s)
    db.flush()
    return s


def _make_attribution(db, skill_id, suggestion_id, level=AttributionLevel.FULL):
    a = SkillAttribution(
        skill_id=skill_id,
        version_from=1,
        version_to=2,
        suggestion_id=suggestion_id,
        attribution_level=level,
    )
    db.add(a)
    db.flush()
    return a


# ── Access Control ────────────────────────────────────────────────────────────

def test_employee_cannot_view_stats(client, db):
    dept = _make_dept(db)
    _make_user(db, "cemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "cemp1")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    assert resp.status_code == 403


def test_super_admin_can_view_stats(client, db):
    dept = _make_dept(db)
    _make_user(db, "cadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "cadmin1")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_dept_admin_can_view_stats(client, db):
    dept = _make_dept(db)
    _make_user(db, "cdept1", Role.DEPT_ADMIN, dept.id)
    db.commit()
    token = _login(client, "cdept1")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    assert resp.status_code == 200


# ── Stats content ─────────────────────────────────────────────────────────────

def test_stats_empty_returns_all_users(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin2", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "cadmin2")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    # Both users should appear
    assert len(data) == 2
    for entry in data:
        assert entry["total_suggestions"] == 0
        assert entry["adopted_count"] == 0
        assert entry["influence_score"] == 0


def test_stats_counts_suggestions(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin3", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp3", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "贡献Skill1")
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.PENDING)
    db.commit()
    token = _login(client, "cadmin3")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    emp_stat = next(s for s in resp.json() if s["user_id"] == emp.id)
    assert emp_stat["total_suggestions"] == 2
    assert emp_stat["adopted_count"] == 1


def test_stats_adoption_rate(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin4", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp4", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "贡献Skill2")
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.PENDING)
    _make_suggestion(db, skill.id, emp.id, SuggestionStatus.PENDING)
    db.commit()
    token = _login(client, "cadmin4")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    emp_stat = next(s for s in resp.json() if s["user_id"] == emp.id)
    assert emp_stat["adoption_rate"] == 0.5


def test_stats_influence_score(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin5", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp5", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "贡献Skill3")
    s1 = _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
    s2 = _make_suggestion(db, skill.id, emp.id, SuggestionStatus.PARTIAL)
    _make_attribution(db, skill.id, s1.id, AttributionLevel.FULL)    # 3 points
    _make_attribution(db, skill.id, s2.id, AttributionLevel.PARTIAL)  # 1 point
    db.commit()
    token = _login(client, "cadmin5")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    emp_stat = next(s for s in resp.json() if s["user_id"] == emp.id)
    assert emp_stat["influence_score"] == 4  # 3+1


def test_stats_impacted_skills_count(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin6", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp6", Role.EMPLOYEE, dept.id)
    skill_a = _make_skill(db, admin.id, "影响Skill-A")
    skill_b = _make_skill(db, admin.id, "影响Skill-B")
    s1 = _make_suggestion(db, skill_a.id, emp.id, SuggestionStatus.ADOPTED)
    s2 = _make_suggestion(db, skill_b.id, emp.id, SuggestionStatus.ADOPTED)
    _make_attribution(db, skill_a.id, s1.id, AttributionLevel.FULL)
    _make_attribution(db, skill_b.id, s2.id, AttributionLevel.FULL)
    db.commit()
    token = _login(client, "cadmin6")

    resp = client.get("/api/contributions/stats", headers=_auth(token))
    emp_stat = next(s for s in resp.json() if s["user_id"] == emp.id)
    assert emp_stat["impacted_skills"] == 2


def test_stats_filter_by_department(client, db):
    dept_a = _make_dept(db, "部门甲")
    dept_b = _make_dept(db, "部门乙")
    admin = _make_user(db, "cadmin7", Role.SUPER_ADMIN, dept_a.id)
    emp_a = _make_user(db, "cemp7a", Role.EMPLOYEE, dept_a.id)
    emp_b = _make_user(db, "cemp7b", Role.EMPLOYEE, dept_b.id)
    db.commit()
    token = _login(client, "cadmin7")

    resp = client.get(f"/api/contributions/stats?department_id={dept_a.id}", headers=_auth(token))
    user_ids = {s["user_id"] for s in resp.json()}
    assert emp_a.id in user_ids
    assert emp_b.id not in user_ids


# ── Leaderboard ───────────────────────────────────────────────────────────────

def test_leaderboard_accessible_to_employee(client, db):
    dept = _make_dept(db)
    _make_user(db, "cemp8", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "cemp8")

    resp = client.get("/api/contributions/leaderboard", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_leaderboard_empty_no_attributions(client, db):
    dept = _make_dept(db)
    _make_user(db, "cemp9", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "cemp9")

    resp = client.get("/api/contributions/leaderboard", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_leaderboard_scores_correctly(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin8", Role.SUPER_ADMIN, dept.id)
    emp1 = _make_user(db, "cemp10a", Role.EMPLOYEE, dept.id)
    emp2 = _make_user(db, "cemp10b", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "排行Skill")
    s1 = _make_suggestion(db, skill.id, emp1.id, SuggestionStatus.ADOPTED)
    s2 = _make_suggestion(db, skill.id, emp2.id, SuggestionStatus.ADOPTED)
    _make_attribution(db, skill.id, s1.id, AttributionLevel.FULL)    # emp1: 3
    _make_attribution(db, skill.id, s2.id, AttributionLevel.PARTIAL)  # emp2: 1
    db.commit()
    token = _login(client, "cemp10a")

    resp = client.get("/api/contributions/leaderboard", headers=_auth(token))
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 2
    # emp1 should be first (higher score)
    assert entries[0]["user_id"] == emp1.id
    assert entries[0]["influence_score"] == 3
    assert entries[1]["user_id"] == emp2.id
    assert entries[1]["influence_score"] == 1


def test_leaderboard_limit_param(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin9", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "限制Skill")
    users = []
    for i in range(5):
        emp = _make_user(db, f"cemp11_{i}", Role.EMPLOYEE, dept.id)
        users.append(emp)
        s = _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
        _make_attribution(db, skill.id, s.id, AttributionLevel.FULL)
    db.commit()
    token = _login(client, "cadmin9")

    resp = client.get("/api/contributions/leaderboard?limit=3", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_leaderboard_fields(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "cadmin10", Role.SUPER_ADMIN, dept.id)
    emp = _make_user(db, "cemp12", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "字段Skill")
    s = _make_suggestion(db, skill.id, emp.id, SuggestionStatus.ADOPTED)
    _make_attribution(db, skill.id, s.id, AttributionLevel.FULL)
    db.commit()
    token = _login(client, "cemp12")

    resp = client.get("/api/contributions/leaderboard", headers=_auth(token))
    entry = resp.json()[0]
    assert "user_id" in entry
    assert "display_name" in entry
    assert "department" in entry
    assert "influence_score" in entry
