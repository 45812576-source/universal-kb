"""TC-WORKSPACE: 工作台 CRUD、权限矩阵、状态机、Skill/Tool 绑定。"""
import pytest
from tests.conftest import (
    _make_user, _make_dept, _make_skill, _make_model_config, _make_tool,
    _login, _auth,
)
from app.models.user import Role
from app.models.skill import SkillStatus
from app.models.workspace import Workspace, WorkspaceStatus


# ─── helpers ──────────────────────────────────────────────────────────────────

def _create_ws(client, token, name="测试工作台", **kwargs):
    body = {"name": name, **kwargs}
    return client.post("/api/workspaces", headers=_auth(token), json=body)


def _make_ws(db, user_id, dept_id, name="工作台", status=WorkspaceStatus.DRAFT,
             visibility="all", is_active=True):
    ws = Workspace(
        name=name, description="", icon="chat", color="#00D1FF",
        category="通用", visibility=visibility,
        welcome_message="你好", status=status,
        created_by=user_id, department_id=dept_id, is_active=is_active,
    )
    db.add(ws)
    db.flush()
    return ws


# ─── 创建工作台 ────────────────────────────────────────────────────────────────

class TestCreateWorkspace:
    def test_super_admin_creates_published(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ws_admin1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "ws_admin1")

        resp = _create_ws(client, token)
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_dept_admin_creates_published(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ws_dadmin1", Role.DEPT_ADMIN, dept.id)
        db.commit()
        token = _login(client, "ws_dadmin1")

        resp = _create_ws(client, token)
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_employee_creates_draft(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ws_emp1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "ws_emp1")

        resp = _create_ws(client, token)
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"

    def test_employee_draft_quota(self, client, db):
        dept = _make_dept(db)
        user = _make_user(db, "ws_emp2", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "ws_emp2")

        # 创建 3 个
        for i in range(3):
            r = _create_ws(client, token, name=f"草稿{i}")
            assert r.status_code == 200

        # 第 4 个应该失败
        r = _create_ws(client, token, name="第4个")
        assert r.status_code == 400
        assert "草稿" in r.json()["detail"]

    def test_system_context_only_super_admin(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ws_dadmin2", Role.DEPT_ADMIN, dept.id)
        _make_user(db, "ws_admin2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        # DEPT_ADMIN 设置 system_context 被忽略
        t_da = _login(client, "ws_dadmin2")
        r = _create_ws(client, t_da, name="ws_da", system_context="机密指令")
        assert r.status_code == 200
        ws_id = r.json()["id"]
        # 用 SUPER_ADMIN 读取确认 system_context 为 None
        t_sa = _login(client, "ws_admin2")
        detail = client.get(f"/api/workspaces/{ws_id}", headers=_auth(t_sa)).json()
        assert detail.get("system_context") is None

        # SUPER_ADMIN 设置 system_context 生效
        r2 = _create_ws(client, t_sa, name="ws_sa", system_context="SUPER指令")
        ws_id2 = r2.json()["id"]
        detail2 = client.get(f"/api/workspaces/{ws_id2}", headers=_auth(t_sa)).json()
        assert detail2["system_context"] == "SUPER指令"

    def test_invalid_model_config_id(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "ws_admin3", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "ws_admin3")

        resp = _create_ws(client, token, model_config_id=99999)
        assert resp.status_code == 400


# ─── 列表可见性 ────────────────────────────────────────────────────────────────

class TestWorkspaceVisibility:
    def test_published_all_visible_to_employee(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wv_admin1", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wv_emp1", Role.EMPLOYEE, dept.id)
        _make_ws(db, admin.id, dept.id, name="公开台", status=WorkspaceStatus.PUBLISHED, visibility="all")
        db.commit()

        token = _login(client, "wv_emp1")
        resp = client.get("/api/workspaces", headers=_auth(token))
        names = [w["name"] for w in resp.json()]
        assert "公开台" in names

    def test_published_department_only_own_dept(self, client, db):
        dept_a = _make_dept(db, "部门A")
        dept_b = _make_dept(db, "部门B")
        admin = _make_user(db, "wv_admin2", Role.SUPER_ADMIN, dept_a.id)
        emp_b = _make_user(db, "wv_emp2", Role.EMPLOYEE, dept_b.id)
        _make_ws(db, admin.id, dept_a.id, name="部门台", status=WorkspaceStatus.PUBLISHED, visibility="department")
        db.commit()

        token = _login(client, "wv_emp2")
        resp = client.get("/api/workspaces", headers=_auth(token))
        names = [w["name"] for w in resp.json()]
        assert "部门台" not in names

    def test_draft_only_visible_to_creator(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "wv_emp3", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "wv_emp4", Role.EMPLOYEE, dept.id)
        _make_ws(db, emp1.id, dept.id, name="私人草稿", status=WorkspaceStatus.DRAFT)
        db.commit()

        token2 = _login(client, "wv_emp4")
        resp = client.get("/api/workspaces", headers=_auth(token2))
        names = [w["name"] for w in resp.json()]
        assert "私人草稿" not in names

    def test_admin_sees_all(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wv_emp5", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wv_admin3", Role.SUPER_ADMIN, dept.id)
        _make_ws(db, emp.id, dept.id, name="员工草稿", status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wv_admin3")
        resp = client.get("/api/workspaces", headers=_auth(token))
        names = [w["name"] for w in resp.json()]
        assert "员工草稿" in names

    def test_inactive_workspace_hidden(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wv_admin4", Role.SUPER_ADMIN, dept.id)
        _make_ws(db, admin.id, dept.id, name="已删除台", status=WorkspaceStatus.PUBLISHED, is_active=False)
        db.commit()

        token = _login(client, "wv_admin4")
        resp = client.get("/api/workspaces", headers=_auth(token))
        names = [w["name"] for w in resp.json()]
        assert "已删除台" not in names


# ─── 更新工作台 ────────────────────────────────────────────────────────────────

class TestUpdateWorkspace:
    def test_super_admin_can_update_any(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wu_admin1", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wu_emp1", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, name="原名", status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wu_admin1")
        r = client.put(f"/api/workspaces/{ws.id}", headers=_auth(token), json={"name": "改名后"})
        assert r.status_code == 200
        assert r.json()["name"] == "改名后"

    def test_employee_can_update_own_draft(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wu_emp2", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, name="草稿", status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wu_emp2")
        r = client.put(f"/api/workspaces/{ws.id}", headers=_auth(token), json={"name": "修改草稿"})
        assert r.status_code == 200

    def test_employee_cannot_update_others_draft(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "wu_emp3", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "wu_emp4", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp1.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wu_emp4")
        r = client.put(f"/api/workspaces/{ws.id}", headers=_auth(token), json={"name": "入侵"})
        assert r.status_code == 403

    def test_dept_admin_can_update_own_dept_published(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wu_da1", Role.DEPT_ADMIN, dept.id)
        ws = _make_ws(db, admin.id, dept.id, name="本部门台", status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wu_da1")
        r = client.put(f"/api/workspaces/{ws.id}", headers=_auth(token), json={"name": "已修改"})
        assert r.status_code == 200

    def test_dept_admin_cannot_update_other_dept_published(self, client, db):
        dept_a = _make_dept(db, "A")
        dept_b = _make_dept(db, "B")
        admin_a = _make_user(db, "wu_da2", Role.DEPT_ADMIN, dept_a.id)
        admin_b = _make_user(db, "wu_da3", Role.DEPT_ADMIN, dept_b.id)
        ws = _make_ws(db, admin_b.id, dept_b.id, name="B部门台", status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wu_da2")
        r = client.put(f"/api/workspaces/{ws.id}", headers=_auth(token), json={"name": "越权"})
        assert r.status_code == 403

    def test_employee_cannot_set_system_context(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wu_admin2", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wu_emp5", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        t_emp = _login(client, "wu_emp5")
        client.put(f"/api/workspaces/{ws.id}", headers=_auth(t_emp), json={"system_context": "黑客"})

        t_sa = _login(client, "wu_admin2")
        detail = client.get(f"/api/workspaces/{ws.id}", headers=_auth(t_sa)).json()
        assert detail.get("system_context") is None


# ─── 删除工作台 ────────────────────────────────────────────────────────────────

class TestDeleteWorkspace:
    def test_super_admin_can_delete_any(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wd_admin1", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wd_emp1", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wd_admin1")
        r = client.delete(f"/api/workspaces/{ws.id}", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_employee_can_delete_own_draft(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wd_emp2", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wd_emp2")
        r = client.delete(f"/api/workspaces/{ws.id}", headers=_auth(token))
        assert r.status_code == 200

    def test_employee_cannot_delete_published(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wd_admin2", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wd_emp3", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wd_emp3")
        r = client.delete(f"/api/workspaces/{ws.id}", headers=_auth(token))
        assert r.status_code == 403

    def test_delete_is_soft(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wd_admin3", Role.SUPER_ADMIN, dept.id)
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()
        ws_id = ws.id

        token = _login(client, "wd_admin3")
        client.delete(f"/api/workspaces/{ws_id}", headers=_auth(token))

        db.expire_all()
        ws_after = db.get(Workspace, ws_id)
        assert ws_after is not None
        assert ws_after.is_active is False


# ─── 状态机：提交审核 ──────────────────────────────────────────────────────────

class TestSubmitWorkspace:
    def test_employee_submit_draft_to_reviewing(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "ws_sub1", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "ws_sub1")
        r = client.patch(f"/api/workspaces/{ws.id}/submit", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["status"] == "reviewing"

    def test_cannot_submit_already_reviewing(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "ws_sub2", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "ws_sub2")
        r = client.patch(f"/api/workspaces/{ws.id}/submit", headers=_auth(token))
        assert r.status_code == 400

    def test_cannot_submit_others_workspace(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "ws_sub3", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "ws_sub4", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp1.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "ws_sub4")
        r = client.patch(f"/api/workspaces/{ws.id}/submit", headers=_auth(token))
        assert r.status_code == 404


# ─── 状态机：审核 ─────────────────────────────────────────────────────────────

class TestReviewWorkspace:
    def test_admin_approve_reviewing(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wr_emp1", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wr_admin1", Role.SUPER_ADMIN, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "wr_admin1")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "approve"})
        assert r.status_code == 200
        assert r.json()["status"] == "published"

    def test_admin_reject_reviewing_back_to_draft(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wr_emp2", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wr_admin2", Role.SUPER_ADMIN, dept.id)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "wr_admin2")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "reject"})
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

    def test_review_invalid_action(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wr_admin3", Role.SUPER_ADMIN, dept.id)
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "wr_admin3")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "invalid"})
        assert r.status_code == 400

    def test_cannot_review_non_reviewing(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wr_admin4", Role.SUPER_ADMIN, dept.id)
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wr_admin4")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "approve"})
        assert r.status_code == 400

    def test_dept_admin_cannot_review_other_dept(self, client, db):
        dept_a = _make_dept(db, "A")
        dept_b = _make_dept(db, "B")
        admin_a = _make_user(db, "wr_da1", Role.DEPT_ADMIN, dept_a.id)
        emp_b = _make_user(db, "wr_emp3", Role.EMPLOYEE, dept_b.id)
        ws = _make_ws(db, emp_b.id, dept_b.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "wr_da1")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "approve"})
        assert r.status_code == 403

    def test_employee_cannot_review(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "wr_emp4", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "wr_emp5", Role.EMPLOYEE, dept.id)
        ws = _make_ws(db, emp1.id, dept.id, status=WorkspaceStatus.REVIEWING)
        db.commit()

        token = _login(client, "wr_emp5")
        r = client.patch(f"/api/workspaces/{ws.id}/review", headers=_auth(token),
                         json={"action": "approve"})
        assert r.status_code in (403, 401)


# ─── Skill 绑定 ───────────────────────────────────────────────────────────────

class TestBindSkill:
    def test_admin_bind_unbind_skill(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wb_admin1", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, admin.id, name="绑定Skill", status=SkillStatus.PUBLISHED)
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wb_admin1")
        r = client.post(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 重复绑定幂等
        r2 = client.post(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        assert r2.status_code == 200

        # 解绑
        r3 = client.delete(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        assert r3.status_code == 200

    def test_employee_can_bind_published_skill_to_own_draft(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wb_admin2", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "wb_emp1", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, admin.id, name="公开Skill", status=SkillStatus.PUBLISHED)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wb_emp1")
        r = client.post(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        assert r.status_code == 200

    def test_employee_cannot_bind_draft_skill(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wb_emp2", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, emp.id, name="草稿Skill", status=SkillStatus.DRAFT)
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wb_emp2")
        r = client.post(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        assert r.status_code == 400

    def test_employee_cannot_bind_to_others_workspace(self, client, db):
        dept = _make_dept(db)
        emp1 = _make_user(db, "wb_emp3", Role.EMPLOYEE, dept.id)
        emp2 = _make_user(db, "wb_emp4", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wb_admin3", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, admin.id, name="Skill2", status=SkillStatus.PUBLISHED)
        ws = _make_ws(db, emp1.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wb_emp4")
        r = client.post(f"/api/workspaces/{ws.id}/skills/{skill.id}", headers=_auth(token))
        # 他人草稿对外不可见 → 404（等同于隐式 403，符合安全设计）
        assert r.status_code in (403, 404)

    def test_batch_set_skills(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wb_admin4", Role.SUPER_ADMIN, dept.id)
        skill1 = _make_skill(db, admin.id, name="批量Skill1")
        skill2 = _make_skill(db, admin.id, name="批量Skill2")
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wb_admin4")
        r = client.put(f"/api/workspaces/{ws.id}/skills", headers=_auth(token),
                       json={"ids": [skill1.id, skill2.id]})
        assert r.status_code == 200
        skill_ids = [s["id"] for s in r.json()["skills"]]
        assert skill1.id in skill_ids
        assert skill2.id in skill_ids

        # 替换为空
        r2 = client.put(f"/api/workspaces/{ws.id}/skills", headers=_auth(token), json={"ids": []})
        assert r2.status_code == 200
        assert r2.json()["skills"] == []

    def test_batch_set_skills_ignores_invalid_ids(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wb_admin5", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, admin.id, name="有效Skill")
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wb_admin5")
        r = client.put(f"/api/workspaces/{ws.id}/skills", headers=_auth(token),
                       json={"ids": [skill.id, 99999]})
        assert r.status_code == 200
        skill_ids = [s["id"] for s in r.json()["skills"]]
        assert skill.id in skill_ids
        assert 99999 not in skill_ids


# ─── Tool 绑定 ────────────────────────────────────────────────────────────────

class TestBindTool:
    def test_admin_bind_tool(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wt_admin1", Role.SUPER_ADMIN, dept.id)
        tool = _make_tool(db, admin.id, name="ws_tool1")
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wt_admin1")
        r = client.post(f"/api/workspaces/{ws.id}/tools/{tool.id}", headers=_auth(token))
        assert r.status_code == 200

    def test_employee_cannot_bind_inactive_tool(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "wt_emp1", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wt_admin2", Role.SUPER_ADMIN, dept.id)
        tool = _make_tool(db, admin.id, name="ws_tool2")
        tool.is_active = False
        ws = _make_ws(db, emp.id, dept.id, status=WorkspaceStatus.DRAFT)
        db.commit()

        token = _login(client, "wt_emp1")
        r = client.post(f"/api/workspaces/{ws.id}/tools/{tool.id}", headers=_auth(token))
        assert r.status_code == 400

    def test_batch_set_tools(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "wt_admin3", Role.SUPER_ADMIN, dept.id)
        tool1 = _make_tool(db, admin.id, name="batch_tool1")
        tool2 = _make_tool(db, admin.id, name="batch_tool2")
        ws = _make_ws(db, admin.id, dept.id, status=WorkspaceStatus.PUBLISHED)
        db.commit()

        token = _login(client, "wt_admin3")
        r = client.put(f"/api/workspaces/{ws.id}/tools", headers=_auth(token),
                       json={"ids": [tool1.id, tool2.id]})
        assert r.status_code == 200
        tool_ids = [t["id"] for t in r.json()["tools"]]
        assert tool1.id in tool_ids
        assert tool2.id in tool_ids


# ─── 完整流程 ─────────────────────────────────────────────────────────────────

class TestWorkspaceFullFlow:
    def test_employee_full_lifecycle(self, client, db):
        """员工：创建草稿 → 提交审核 → 管理员批准 → 发布可见。"""
        dept = _make_dept(db)
        emp = _make_user(db, "wf_emp1", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "wf_admin1", Role.SUPER_ADMIN, dept.id)
        another_emp = _make_user(db, "wf_emp2", Role.EMPLOYEE, dept.id)
        db.commit()

        t_emp = _login(client, "wf_emp1")
        t_admin = _login(client, "wf_admin1")
        t_emp2 = _login(client, "wf_emp2")

        # 创建草稿
        r = _create_ws(client, t_emp, name="员工工作台")
        ws_id = r.json()["id"]
        assert r.json()["status"] == "draft"

        # 另一个员工看不到
        resp = client.get("/api/workspaces", headers=_auth(t_emp2))
        assert ws_id not in [w["id"] for w in resp.json()]

        # 提交审核
        client.patch(f"/api/workspaces/{ws_id}/submit", headers=_auth(t_emp))

        # 管理员批准
        client.patch(f"/api/workspaces/{ws_id}/review", headers=_auth(t_admin),
                     json={"action": "approve"})

        # 现在另一员工也能看到
        resp2 = client.get("/api/workspaces", headers=_auth(t_emp2))
        assert ws_id in [w["id"] for w in resp2.json()]

    def test_employee_draft_deleted_after_quota_freed(self, client, db):
        """员工删除草稿后可以继续创建新工作台。"""
        dept = _make_dept(db)
        emp = _make_user(db, "wf_emp3", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "wf_emp3")

        ws_ids = []
        for i in range(3):
            r = _create_ws(client, token, name=f"草稿{i}")
            ws_ids.append(r.json()["id"])

        # 第 4 个失败
        assert _create_ws(client, token, name="第4个").status_code == 400

        # 删一个
        client.delete(f"/api/workspaces/{ws_ids[0]}", headers=_auth(token))

        # 现在可以再建
        assert _create_ws(client, token, name="新草稿").status_code == 200
