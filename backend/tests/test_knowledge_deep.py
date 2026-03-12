"""TC-KNOWLEDGE-DEEP: 知识审核状态机（L2/L3）、chunks 搜索权限、folder 操作、get 权限、serve_image 安全。"""
import pytest
from unittest.mock import patch, AsyncMock
from tests.conftest import (
    _make_user, _make_dept, _make_model_config, _login, _auth,
)
from app.models.user import Role
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage


def _create_entry(client, token, title="测试知识", content="内容", capture_mode="manual_form"):
    return client.post("/api/knowledge", headers=_auth(token), json={
        "title": title,
        "content": content,
        "category": "experience",
        "capture_mode": capture_mode,
        "industry_tags": [],
        "platform_tags": [],
        "topic_tags": [],
    })


def _make_entry(db, user_id, dept_id, title="知识", status=KnowledgeStatus.PENDING,
                review_stage=ReviewStage.PENDING_DEPT, capture_mode="manual_form", content="内容"):
    e = KnowledgeEntry(
        title=title, content=content, category="experience",
        capture_mode=capture_mode, review_level=2, review_stage=review_stage,
        status=status, created_by=user_id, department_id=dept_id,
        industry_tags=[], platform_tags=[], topic_tags=[],
    )
    db.add(e)
    db.flush()
    return e


# ─── 审核状态机 L2 ─────────────────────────────────────────────────────────────

class TestKnowledgeReviewL2:
    def test_dept_admin_approve_pending(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl2_emp1", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "krl2_da1", Role.DEPT_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="L2审核")
        db.commit()

        token = _login(client, "krl2_da1")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_dept_admin_reject_pending(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl2_emp2", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "krl2_da2", Role.DEPT_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="拒绝知识")
        db.commit()

        token = _login(client, "krl2_da2")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "reject", "note": "内容不完整"})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_cannot_review_already_approved(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl2_emp3", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "krl2_da3", Role.DEPT_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, status=KnowledgeStatus.APPROVED,
                            review_stage=ReviewStage.APPROVED)
        db.commit()

        token = _login(client, "krl2_da3")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code == 400

    def test_dept_admin_cannot_review_other_dept(self, client, db):
        dept_a = _make_dept(db, "A")
        dept_b = _make_dept(db, "B")
        emp = _make_user(db, "krl2_emp4", Role.EMPLOYEE, dept_a.id)
        admin_b = _make_user(db, "krl2_da4", Role.DEPT_ADMIN, dept_b.id)
        entry = _make_entry(db, emp.id, dept_a.id, title="A部门知识")
        db.commit()

        token = _login(client, "krl2_da4")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code == 403

    def test_cannot_review_with_invalid_action(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl2_emp5", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "krl2_da5", Role.DEPT_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id)
        db.commit()

        token = _login(client, "krl2_da5")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "unknown"})
        assert r.status_code == 400

    def test_employee_cannot_review(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "krl2_emp6", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "krl2_emp7", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp1.id, dept.id)
        db.commit()

        token = _login(client, "krl2_emp7")
        r = client.post(f"/api/knowledge/{entry.id}/review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code in (401, 403)


# ─── 审核状态机 L3 ─────────────────────────────────────────────────────────────

class TestKnowledgeReviewL3:
    def test_super_admin_super_approve(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl3_emp1", Role.EMPLOYEE, dept.id)
        sa = _make_user(db, "krl3_sa1", Role.SUPER_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="L3知识",
                            status=KnowledgeStatus.PENDING,
                            review_stage=ReviewStage.DEPT_APPROVED_PENDING_SUPER)
        db.commit()

        token = _login(client, "krl3_sa1")
        r = client.post(f"/api/knowledge/{entry.id}/super-review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_super_admin_super_reject(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl3_emp2", Role.EMPLOYEE, dept.id)
        sa = _make_user(db, "krl3_sa2", Role.SUPER_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="L3拒绝",
                            status=KnowledgeStatus.PENDING,
                            review_stage=ReviewStage.DEPT_APPROVED_PENDING_SUPER)
        db.commit()

        token = _login(client, "krl3_sa2")
        r = client.post(f"/api/knowledge/{entry.id}/super-review", headers=_auth(token),
                        json={"action": "reject"})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_super_review_only_for_dept_approved_pending_super(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl3_emp3", Role.EMPLOYEE, dept.id)
        sa = _make_user(db, "krl3_sa3", Role.SUPER_ADMIN, dept.id)
        # 普通 PENDING 状态（还没经过部门审核）
        entry = _make_entry(db, emp.id, dept.id, status=KnowledgeStatus.PENDING,
                            review_stage=ReviewStage.PENDING_DEPT)
        db.commit()

        token = _login(client, "krl3_sa3")
        r = client.post(f"/api/knowledge/{entry.id}/super-review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code == 400

    def test_dept_admin_cannot_super_review(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "krl3_emp4", Role.EMPLOYEE, dept.id)
        da = _make_user(db, "krl3_da1", Role.DEPT_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id,
                            review_stage=ReviewStage.DEPT_APPROVED_PENDING_SUPER)
        db.commit()

        token = _login(client, "krl3_da1")
        r = client.post(f"/api/knowledge/{entry.id}/super-review", headers=_auth(token),
                        json={"action": "approve"})
        assert r.status_code in (401, 403)


# ─── 权限：get 单条 ────────────────────────────────────────────────────────────

class TestKnowledgeGetPermission:
    def test_employee_can_get_own_pending(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "kgp_emp1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="自己的待审")
        db.commit()

        token = _login(client, "kgp_emp1")
        r = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert r.status_code == 200

    def test_employee_cannot_get_others_pending(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "kgp_emp2", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "kgp_emp3", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp1.id, dept.id, title="他人待审")
        db.commit()

        token = _login(client, "kgp_emp3")
        r = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert r.status_code == 403

    def test_employee_can_get_others_approved(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "kgp_emp4", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "kgp_emp5", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp1.id, dept.id, title="他人已审批",
                            status=KnowledgeStatus.APPROVED,
                            review_stage=ReviewStage.APPROVED)
        db.commit()

        token = _login(client, "kgp_emp5")
        r = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert r.status_code == 200

    def test_super_admin_can_get_any(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "kgp_emp6", Role.EMPLOYEE, dept.id)
        sa = _make_user(db, "kgp_sa1", Role.SUPER_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="任意条目")
        db.commit()

        token = _login(client, "kgp_sa1")
        r = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert r.status_code == 200


# ─── 列表可见性 ────────────────────────────────────────────────────────────────

class TestKnowledgeListVisibility:
    def test_approved_visible_to_other_employees(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "klv_emp1", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "klv_emp2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp1.id, dept.id, title="公开知识",
                            status=KnowledgeStatus.APPROVED,
                            review_stage=ReviewStage.APPROVED)
        db.commit()

        token = _login(client, "klv_emp2")
        r = client.get("/api/knowledge", headers=_auth(token))
        titles = [e["title"] for e in r.json()]
        assert "公开知识" in titles

    def test_pending_not_visible_to_other_employees(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "klv_emp3", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "klv_emp4", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, emp1.id, dept.id, title="待审知识",
                            status=KnowledgeStatus.PENDING)
        db.commit()

        token = _login(client, "klv_emp4")
        r = client.get("/api/knowledge", headers=_auth(token))
        titles = [e["title"] for e in r.json()]
        assert "待审知识" not in titles

    def test_super_admin_sees_all(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "klv_emp5", Role.EMPLOYEE, dept.id)
        sa = _make_user(db, "klv_sa1", Role.SUPER_ADMIN, dept.id)
        entry = _make_entry(db, emp.id, dept.id, title="任意状态知识")
        db.commit()

        token = _login(client, "klv_sa1")
        r = client.get("/api/knowledge", headers=_auth(token))
        titles = [e["title"] for e in r.json()]
        assert "任意状态知识" in titles


# ─── serve_image 路径安全 ──────────────────────────────────────────────────────

class TestServeImageSecurity:
    def test_path_traversal_rejected(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ki_emp1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "ki_emp1")

        # 尝试路径穿越
        for malicious in ["../etc/passwd", "../../secrets", "foo/bar.jpg"]:
            r = client.get(f"/api/knowledge/images/{malicious}", headers=_auth(token))
            assert r.status_code in (400, 404), f"应拦截路径: {malicious}"


# ─── 完整审核流程 ──────────────────────────────────────────────────────────────

class TestKnowledgeReviewFullFlow:
    def test_l2_full_flow(self, client, db):
        """员工创建 → 部门审核 → 审批 → 其他员工可见。"""
        dept = _make_dept(db)
        emp1 = _make_user(db, "kff_emp1", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "kff_emp2", Role.EMPLOYEE, dept.id)
        da = _make_user(db, "kff_da1", Role.DEPT_ADMIN, dept.id)
        db.commit()

        t_emp1 = _login(client, "kff_emp1")
        t_emp2 = _login(client, "kff_emp2")
        t_da = _login(client, "kff_da1")

        # 员工创建
        r = _create_entry(client, t_emp1, title="流程测试知识", capture_mode="manual_form")
        assert r.status_code == 200
        kid = r.json()["id"]

        # 员工2 看不到（待审）
        resp = client.get("/api/knowledge", headers=_auth(t_emp2))
        assert kid not in [e["id"] for e in resp.json()]

        # 部门管理员审批
        r2 = client.post(f"/api/knowledge/{kid}/review", headers=_auth(t_da),
                         json={"action": "approve"})
        assert r2.status_code == 200

        # 员工2 现在能看到
        resp2 = client.get("/api/knowledge", headers=_auth(t_emp2))
        assert kid in [e["id"] for e in resp2.json()]
