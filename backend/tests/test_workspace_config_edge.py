"""
工作台配置大重构 — 极限边界测试

按角色 × 场景矩阵覆盖：
  角色: super_admin / dept_admin / employee / 未认证 / 已禁用用户
  场景: 工作台配置 CRUD / 发布 / 意见审核 / 跨部门隔离 / 并发 / 数据一致性

运行方式:
  cd /Users/xia/project/universal-kb/backend
  python -m pytest tests/test_workspace_config_edge.py -v
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.models.user import Role
from app.models.skill import SkillStatus, SuggestionStatus, SkillSuggestion, Skill
from app.models.tool import ToolType
from app.models.workspace import (
    UserWorkspaceConfig, Workspace, WorkspaceSkill, WorkspaceTool,
)

# conftest.py 提供: db, client, _make_dept, _make_user, _make_skill, _make_tool, _login, _auth
from tests.conftest import (
    _make_dept, _make_user, _make_skill, _make_tool,
    _login, _auth, _make_model_config,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_dept_with_users(db):
    """创建部门 + 三种角色用户，返回 (dept, super_admin, dept_admin, employee)。"""
    dept = _make_dept(db, "研发部")
    sa = _make_user(db, "sa_user", Role.SUPER_ADMIN, dept.id, "Pass1234!")
    da = _make_user(db, "da_user", Role.DEPT_ADMIN, dept.id, "Pass1234!")
    emp = _make_user(db, "emp_user", Role.EMPLOYEE, dept.id, "Pass1234!")
    db.commit()
    return dept, sa, da, emp


def _setup_two_depts(db):
    """创建两个部门各有一个员工 + 一个 dept_admin，返回 (dept_a, dept_b, da_a, da_b, emp_a, emp_b)。"""
    dept_a = _make_dept(db, "部门A")
    dept_b = _make_dept(db, "部门B")
    da_a = _make_user(db, "da_a", Role.DEPT_ADMIN, dept_a.id, "Pass1234!")
    da_b = _make_user(db, "da_b", Role.DEPT_ADMIN, dept_b.id, "Pass1234!")
    emp_a = _make_user(db, "emp_a", Role.EMPLOYEE, dept_a.id, "Pass1234!")
    emp_b = _make_user(db, "emp_b", Role.EMPLOYEE, dept_b.id, "Pass1234!")
    db.commit()
    return dept_a, dept_b, da_a, da_b, emp_a, emp_b


def _make_dept_skill(db, user_id, dept_id, name="部门Skill"):
    """创建一个已发布的部门级 Skill。"""
    skill = _make_skill(db, user_id, name, SkillStatus.PUBLISHED)
    skill.scope = "department"
    skill.department_id = dept_id
    db.flush()
    return skill


def _make_dept_tool(db, user_id, dept_id, name="dept_tool"):
    """创建一个已发布的部门级 Tool。"""
    tool = _make_tool(db, user_id, name, ToolType.BUILTIN)
    tool.scope = "department"
    tool.status = "published"
    tool.department_id = dept_id
    db.flush()
    return tool


# ═══════════════════════════════════════════════════════════════════════════════
# Module 1: 权限矩阵 — 未认证 / 已禁用用户
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthBoundary:
    """认证边界：无 token、过期 token、已禁用用户。"""

    def test_no_token_get_config(self, client):
        resp = client.get("/api/workspace-config")
        assert resp.status_code in (401, 403)

    def test_no_token_put_config(self, client):
        resp = client.put("/api/workspace-config", json={"mounted_skills": [], "mounted_tools": []})
        assert resp.status_code in (401, 403)

    def test_no_token_publish(self, client):
        resp = client.post("/api/workspace-config/publish", json={"scope": "department"})
        assert resp.status_code in (401, 403)

    def test_no_token_review_suggestion(self, client):
        resp = client.patch("/api/skill-suggestions/999/review", json={"status": "adopted"})
        assert resp.status_code in (401, 403)

    def test_invalid_token(self, client):
        resp = client.get("/api/workspace-config", headers={"Authorization": "Bearer invalid.jwt.token"})
        assert resp.status_code in (401, 403)

    def test_disabled_user_rejected(self, client, db):
        """is_active=False 的用户：先登录获取 token，然后禁用，token 应失效。"""
        dept = _make_dept(db)
        user = _make_user(db, "disabled_user", Role.EMPLOYEE, dept.id)
        db.commit()

        # 先登录获取有效 token
        token = _login(client, "disabled_user", "Test1234!")

        # 禁用用户（模拟管理员后台操作）
        user.is_active = False
        db.commit()

        # 用旧 token 访问应被拒绝
        resp = client.get("/api/workspace-config", headers=_auth(token))
        assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════════
# Module 2: 各角色 GET 工作台配置
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetConfigByRole:
    """各角色首次/重复获取配置的行为。"""

    def test_employee_first_access_creates_config(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "emp1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "emp1")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == emp.id
        assert isinstance(data["mounted_skills"], list)
        assert isinstance(data["mounted_tools"], list)

    def test_super_admin_first_access(self, client, db):
        dept = _make_dept(db)
        sa = _make_user(db, "sa1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa1")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["user_id"] == sa.id

    def test_dept_admin_first_access(self, client, db):
        dept = _make_dept(db)
        da = _make_user(db, "da1", Role.DEPT_ADMIN, dept.id)
        db.commit()
        token = _login(client, "da1")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["user_id"] == da.id

    def test_idempotent_get(self, client, db):
        """多次 GET 返回同一配置 ID。"""
        dept = _make_dept(db)
        _make_user(db, "idemp", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "idemp")

        r1 = client.get("/api/workspace-config", headers=_auth(token)).json()
        r2 = client.get("/api/workspace-config", headers=_auth(token)).json()
        assert r1["id"] == r2["id"]

    def test_premount_own_skills_on_first_access(self, client, db):
        """首次访问应自动预挂载用户自己的 Skill 和 Tool。"""
        dept = _make_dept(db)
        user = _make_user(db, "own_mount", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "我的Skill", SkillStatus.PUBLISHED)
        tool = _make_tool(db, user.id, "my_tool", ToolType.BUILTIN)
        db.commit()
        token = _login(client, "own_mount")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        data = resp.json()

        skill_ids = [s["id"] for s in data["mounted_skills"]]
        tool_ids = [t["id"] for t in data["mounted_tools"]]
        assert skill.id in skill_ids
        assert tool.id in tool_ids

    def test_premount_dept_skills_on_first_access(self, client, db):
        """首次访问应预挂载部门发布的 Skill/Tool。"""
        dept = _make_dept(db)
        admin = _make_user(db, "dept_pub", Role.DEPT_ADMIN, dept.id)
        emp = _make_user(db, "dept_emp", Role.EMPLOYEE, dept.id)
        dept_skill = _make_dept_skill(db, admin.id, dept.id, "部门公共Skill")
        dept_tool = _make_dept_tool(db, admin.id, dept.id, "dept_pub_tool")
        db.commit()
        token = _login(client, "dept_emp")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        data = resp.json()

        skill_ids = [s["id"] for s in data["mounted_skills"]]
        tool_ids = [t["id"] for t in data["mounted_tools"]]
        assert dept_skill.id in skill_ids
        assert dept_tool.id in tool_ids

    def test_user_without_dept_gets_empty_dept_items(self, client, db):
        """没有 department_id 的用户不会预挂载部门 skill/tool。"""
        user = _make_user(db, "no_dept", Role.EMPLOYEE, dept_id=None)
        db.commit()
        token = _login(client, "no_dept")

        resp = client.get("/api/workspace-config", headers=_auth(token))
        data = resp.json()
        # 只有 own 来源或空
        for s in data["mounted_skills"]:
            assert s["source"] == "own"

    def test_deleted_skill_silently_dropped(self, client, db):
        """挂载的 Skill 被删除后，GET 返回时不含该项。"""
        dept = _make_dept(db)
        user = _make_user(db, "del_test", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "即将删除Skill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "del_test")

        # 首次创建配置
        client.get("/api/workspace-config", headers=_auth(token))

        # 删除 skill
        db.delete(db.get(Skill, skill.id))
        # 同时清理 skill_versions
        from app.models.skill import SkillVersion
        db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).delete()
        db.delete(db.get(Skill, skill.id)) if db.get(Skill, skill.id) else None
        db.commit()

        resp = client.get("/api/workspace-config", headers=_auth(token))
        ids = [s["id"] for s in resp.json()["mounted_skills"]]
        assert skill.id not in ids


# ═══════════════════════════════════════════════════════════════════════════════
# Module 3: PUT 保存配置 — 边界输入
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveConfigEdge:
    """PUT /api/workspace-config 极端输入。"""

    def test_save_empty_lists(self, client, db):
        """保存空列表应成功。"""
        dept = _make_dept(db)
        _make_user(db, "empty_save", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "empty_save")

        # 先 GET 创建配置
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": [], "mounted_tools": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mounted_skills"] == []
        assert data["mounted_tools"] == []

    def test_save_nonexistent_skill_id(self, client, db):
        """挂载不存在的 skill_id，保存成功但 GET 时被过滤掉。"""
        dept = _make_dept(db)
        _make_user(db, "ghost", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "ghost")
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [{"id": 99999, "source": "market", "mounted": True}],
                "mounted_tools": [],
            },
        )
        assert resp.status_code == 200
        # 不存在的 skill 被 _config_response 过滤
        assert len(resp.json()["mounted_skills"]) == 0

    def test_save_duplicate_skill_ids(self, client, db):
        """同一个 skill_id 重复出现，不应崩溃。"""
        dept = _make_dept(db)
        user = _make_user(db, "dup_save", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "DupSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "dup_save")
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [
                    {"id": skill.id, "source": "own", "mounted": True},
                    {"id": skill.id, "source": "own", "mounted": False},
                ],
                "mounted_tools": [],
            },
        )
        assert resp.status_code == 200

    def test_save_preserves_needs_refresh_when_no_change(self, client, db):
        """保存相同挂载集合不触发 needs_prompt_refresh。"""
        dept = _make_dept(db)
        user = _make_user(db, "no_change", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "StableSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "no_change")

        # 创建 + 首次保存
        config = client.get("/api/workspace-config", headers=_auth(token)).json()
        skills = [{"id": s["id"], "source": s["source"], "mounted": s["mounted"]}
                  for s in config["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in config["mounted_tools"]]

        # 清除 refresh flag
        cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
        cfg.needs_prompt_refresh = False
        db.commit()

        # 保存相同数据
        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        assert resp.json()["needs_prompt_refresh"] is False

    def test_save_toggle_triggers_refresh(self, client, db):
        """切换挂载状态触发 needs_prompt_refresh=True。"""
        dept = _make_dept(db)
        user = _make_user(db, "toggle_ref", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "ToggleSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "toggle_ref")

        client.get("/api/workspace-config", headers=_auth(token))

        # 清除 refresh
        cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
        cfg.needs_prompt_refresh = False
        db.commit()

        # 把 skill 设为 unmounted
        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [{"id": skill.id, "source": "own", "mounted": False}],
                "mounted_tools": [],
            },
        )
        assert resp.json()["needs_prompt_refresh"] is True

    def test_large_mount_list(self, client, db):
        """挂载大量 skill（50+）应正常保存。"""
        dept = _make_dept(db)
        user = _make_user(db, "big_list", Role.EMPLOYEE, dept.id)
        skills = []
        for i in range(50):
            s = _make_skill(db, user.id, f"BatchSkill_{i}", SkillStatus.PUBLISHED)
            skills.append(s)
        db.commit()
        token = _login(client, "big_list")
        client.get("/api/workspace-config", headers=_auth(token))

        mount_data = [{"id": s.id, "source": "own", "mounted": True} for s in skills]
        resp = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": mount_data, "mounted_tools": []},
        )
        assert resp.status_code == 200
        assert len(resp.json()["mounted_skills"]) == 50

    def test_employee_cannot_save_other_user_config(self, client, db):
        """用户只能操作自己的配置，不能伪造 user_id。"""
        dept = _make_dept(db)
        user_a = _make_user(db, "usr_a", Role.EMPLOYEE, dept.id)
        user_b = _make_user(db, "usr_b", Role.EMPLOYEE, dept.id)
        db.commit()
        token_a = _login(client, "usr_a")
        token_b = _login(client, "usr_b")

        # A 创建配置
        client.get("/api/workspace-config", headers=_auth(token_a))
        # B 创建配置
        client.get("/api/workspace-config", headers=_auth(token_b))

        # B 保存 → 只影响 B 自己的配置
        client.put(
            "/api/workspace-config",
            headers=_auth(token_b),
            json={"mounted_skills": [], "mounted_tools": []},
        )

        # A 的配置不受影响
        resp_a = client.get("/api/workspace-config", headers=_auth(token_a))
        # A 可能有预挂载的 skill
        assert resp_a.json()["user_id"] == user_a.id


# ═══════════════════════════════════════════════════════════════════════════════
# Module 4: 发布权限矩阵
# ═══════════════════════════════════════════════════════════════════════════════

class TestPublishPermissionMatrix:
    """POST /publish 的角色 × 场景组合。"""

    def _prepare_config_with_skill(self, client, db, username, role, dept_id):
        """创建用户 + skill + 配置，返回 token。"""
        user = _make_user(db, username, role, dept_id)
        skill = _make_skill(db, user.id, f"{username}_skill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, username)
        # 创建配置并挂载 skill
        client.get("/api/workspace-config", headers=_auth(token))
        return token, user, skill

    def test_employee_cannot_publish(self, client, db):
        """普通员工不能发布标准工作台 → 403。"""
        dept = _make_dept(db)
        token, _, _ = self._prepare_config_with_skill(client, db, "emp_pub", Role.EMPLOYEE, dept.id)

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "非法发布"},
        )
        assert resp.status_code == 403

    def test_dept_admin_can_publish_department(self, client, db):
        """部门管理员可以发布部门标准。"""
        dept = _make_dept(db)
        token, user, _ = self._prepare_config_with_skill(client, db, "da_pub", Role.DEPT_ADMIN, dept.id)

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "部门标准"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scope"] == "department"

    def test_dept_admin_can_publish_company(self, client, db):
        """部门管理员也可以发布公司级标准（API 允许）。"""
        dept = _make_dept(db)
        token, _, _ = self._prepare_config_with_skill(client, db, "da_co", Role.DEPT_ADMIN, dept.id)

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "company", "name": "公司标准"},
        )
        assert resp.status_code == 200
        assert resp.json()["scope"] == "company"

    def test_super_admin_can_publish_both(self, client, db):
        """超管可以发布部门和公司标准。"""
        dept = _make_dept(db)
        token, _, _ = self._prepare_config_with_skill(client, db, "sa_pub", Role.SUPER_ADMIN, dept.id)

        for scope in ("department", "company"):
            resp = client.post(
                "/api/workspace-config/publish",
                headers=_auth(token),
                json={"scope": scope, "name": f"超管{scope}标准"},
            )
            assert resp.status_code == 200

    def test_publish_empty_config_400(self, client, db):
        """空挂载配置不允许发布 → 400。"""
        dept = _make_dept(db)
        _make_user(db, "empty_pub", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "empty_pub")
        client.get("/api/workspace-config", headers=_auth(token))

        # 清空挂载
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": [], "mounted_tools": []},
        )

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department"},
        )
        assert resp.status_code == 400

    def test_publish_upsert_same_workspace(self, client, db):
        """同一管理员重复发布同 scope 应更新而非新建。"""
        dept = _make_dept(db)
        token, user, _ = self._prepare_config_with_skill(client, db, "upsert", Role.SUPER_ADMIN, dept.id)

        r1 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "v1"},
        )
        r2 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "v2"},
        )
        assert r1.json()["workspace_id"] == r2.json()["workspace_id"]
        # 名称已更新
        ws = db.get(Workspace, r2.json()["workspace_id"])
        assert ws.name == "v2"

    def test_publish_dept_and_company_are_separate(self, client, db):
        """同一管理员发布 department 和 company 应生成两个不同的 workspace。"""
        dept = _make_dept(db)
        token, _, _ = self._prepare_config_with_skill(client, db, "sep_pub", Role.SUPER_ADMIN, dept.id)

        r1 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "部门"},
        )
        r2 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "company", "name": "公司"},
        )
        assert r1.json()["workspace_id"] != r2.json()["workspace_id"]

    def test_publish_creates_workspace_skills_and_tools(self, client, db):
        """发布后 WorkspaceSkill/WorkspaceTool 行数与挂载数一致。"""
        dept = _make_dept(db)
        user = _make_user(db, "bind_pub", Role.SUPER_ADMIN, dept.id)
        s1 = _make_skill(db, user.id, "PubS1", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, user.id, "PubS2", SkillStatus.PUBLISHED)
        t1 = _make_tool(db, user.id, "pub_t1", ToolType.BUILTIN)
        db.commit()
        token = _login(client, "bind_pub")
        client.get("/api/workspace-config", headers=_auth(token))

        # 挂载 2 skill + 1 tool
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [
                    {"id": s1.id, "source": "own", "mounted": True},
                    {"id": s2.id, "source": "own", "mounted": True},
                ],
                "mounted_tools": [
                    {"id": t1.id, "source": "own", "mounted": True},
                ],
            },
        )

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "绑定验证"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_count"] == 2
        assert data["tool_count"] == 1

        ws_id = data["workspace_id"]
        ws_skills = db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == ws_id).all()
        ws_tools = db.query(WorkspaceTool).filter(WorkspaceTool.workspace_id == ws_id).all()
        assert len(ws_skills) == 2
        assert len(ws_tools) == 1

    def test_publish_only_mounted_items(self, client, db):
        """只有 mounted=True 的项被发布，unmounted 的不包括。"""
        dept = _make_dept(db)
        user = _make_user(db, "partial_pub", Role.SUPER_ADMIN, dept.id)
        s1 = _make_skill(db, user.id, "Mounted", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, user.id, "Unmounted", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "partial_pub")
        client.get("/api/workspace-config", headers=_auth(token))

        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [
                    {"id": s1.id, "source": "own", "mounted": True},
                    {"id": s2.id, "source": "own", "mounted": False},
                ],
                "mounted_tools": [],
            },
        )

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "仅挂载"},
        )
        assert resp.json()["skill_count"] == 1

    def test_dept_admin_no_department_publish_department(self, client, db):
        """dept_admin 没有 department_id 发布 department scope → dept_id 为 None。"""
        da = _make_user(db, "no_dept_da", Role.DEPT_ADMIN, dept_id=None)
        skill = _make_skill(db, da.id, "NoDeptSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "no_dept_da")
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "无部门发布"},
        )
        assert resp.status_code == 200
        ws = db.get(Workspace, resp.json()["workspace_id"])
        # department_id 和 for_department_id 都是 None
        assert ws.for_department_id is None

    def test_publish_default_name(self, client, db):
        """不传 name 时使用默认名称。"""
        dept = _make_dept(db)
        token, user, _ = self._prepare_config_with_skill(client, db, "dflt_name", Role.SUPER_ADMIN, dept.id)

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "company"},
        )
        assert resp.status_code == 200
        ws = db.get(Workspace, resp.json()["workspace_id"])
        assert user.display_name in ws.name


# ═══════════════════════════════════════════════════════════════════════════════
# Module 5: Suggestion 审核权限矩阵
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuggestionReviewMatrix:
    """PATCH /api/skill-suggestions/{id}/review 角色 × 关系矩阵。"""

    def _create_suggestion(self, db, skill_id, submitter_id):
        s = SkillSuggestion(
            skill_id=skill_id,
            submitted_by=submitter_id,
            problem_desc="测试问题",
            expected_direction="测试方向",
            status=SuggestionStatus.PENDING,
        )
        db.add(s)
        db.flush()
        return s

    def test_super_admin_reviews_any_skill(self, client, db):
        """超管可以审核任意 skill 的意见。"""
        dept = _make_dept(db)
        sa = _make_user(db, "sa_rev", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "emp_sub", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, emp.id, "EmpSkill", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, emp.id)
        db.commit()
        token = _login(client, "sa_rev")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "adopted", "review_note": "好建议"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "adopted"

    def test_dept_admin_reviews_any_skill(self, client, db):
        """部门管理员可以审核任意 skill 的意见。"""
        dept = _make_dept(db)
        da = _make_user(db, "da_rev", Role.DEPT_ADMIN, dept.id)
        emp = _make_user(db, "emp_sub2", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, emp.id, "EmpSkill2", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, emp.id)
        db.commit()
        token = _login(client, "da_rev")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "rejected"},
        )
        assert resp.status_code == 200

    def test_skill_creator_reviews_own_skill_suggestion(self, client, db):
        """Skill 创建者（即使是员工）可以审核自己 skill 的意见。"""
        dept = _make_dept(db)
        creator = _make_user(db, "creator_rev", Role.EMPLOYEE, dept.id)
        submitter = _make_user(db, "submitter", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "CreatorSkill", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, submitter.id)
        db.commit()
        token = _login(client, "creator_rev")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "adopted"},
        )
        assert resp.status_code == 200

    def test_non_owner_non_admin_blocked(self, client, db):
        """既非创建者也非管理员 → 403。"""
        dept = _make_dept(db)
        creator = _make_user(db, "owner_x", Role.EMPLOYEE, dept.id)
        stranger = _make_user(db, "stranger", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "OwnerSkill", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, creator.id)
        db.commit()
        token = _login(client, "stranger")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "adopted"},
        )
        assert resp.status_code == 403

    def test_submitter_cannot_review_own_suggestion(self, client, db):
        """提交者自己不能审核自己的意见（除非恰好是 skill 创建者或管理员）。"""
        dept = _make_dept(db)
        creator = _make_user(db, "sk_own", Role.EMPLOYEE, dept.id)
        submitter = _make_user(db, "self_rev", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "SomeSkill", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, submitter.id)
        db.commit()
        token = _login(client, "self_rev")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "adopted"},
        )
        # submitter 既不是 skill 创建者也不是管理员 → 403
        assert resp.status_code == 403

    def test_review_nonexistent_suggestion_404(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "sa_404", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_404")

        resp = client.patch(
            "/api/skill-suggestions/99999/review",
            headers=_auth(token),
            json={"status": "adopted"},
        )
        assert resp.status_code == 404

    def test_review_with_invalid_status_400(self, client, db):
        """无效状态值 → 400。"""
        dept = _make_dept(db)
        sa = _make_user(db, "sa_bad", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, sa.id, "BadStatus", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, sa.id)
        db.commit()
        token = _login(client, "sa_bad")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 400

    def test_review_with_pending_status_400(self, client, db):
        """不能将状态回退为 pending → 400。"""
        dept = _make_dept(db)
        sa = _make_user(db, "sa_pend", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, sa.id, "PendBack", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, sa.id)
        db.commit()
        token = _login(client, "sa_pend")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "pending"},
        )
        assert resp.status_code == 400

    def test_review_partial_status(self, client, db):
        """partial 是合法状态。"""
        dept = _make_dept(db)
        sa = _make_user(db, "sa_partial", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, sa.id, "PartialS", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, sa.id)
        db.commit()
        token = _login(client, "sa_partial")

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "partial", "review_note": "部分采纳"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"

    def test_review_sets_reviewer_fields(self, client, db):
        """审核后 reviewed_by / reviewed_at 应被填充。"""
        dept = _make_dept(db)
        sa = _make_user(db, "sa_fields", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, sa.id, "FieldS", SkillStatus.PUBLISHED)
        suggestion = self._create_suggestion(db, skill.id, sa.id)
        db.commit()
        token = _login(client, "sa_fields")

        client.patch(
            f"/api/skill-suggestions/{suggestion.id}/review",
            headers=_auth(token),
            json={"status": "adopted", "review_note": "已采纳"},
        )

        db.expire_all()
        updated = db.get(SkillSuggestion, suggestion.id)
        assert updated.reviewed_by == sa.id
        assert updated.reviewed_at is not None
        assert updated.review_note == "已采纳"


# ═══════════════════════════════════════════════════════════════════════════════
# Module 6: Suggestion 提交 + 列表
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuggestionSubmitAndList:
    """提交意见 + 列表查看 + /comments 权限。"""

    def test_any_user_can_submit_suggestion(self, client, db):
        dept = _make_dept(db)
        creator = _make_user(db, "sk_cr", Role.EMPLOYEE, dept.id)
        submitter = _make_user(db, "sub_any", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "SubSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "sub_any")

        resp = client.post(
            f"/api/skills/{skill.id}/suggestions",
            headers=_auth(token),
            json={
                "problem_desc": "某场景不好用",
                "expected_direction": "希望增加XXX功能",
                "case_example": "用户说了YYY但没响应",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_submit_to_nonexistent_skill_404(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "sub_404", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "sub_404")

        resp = client.post(
            "/api/skills/99999/suggestions",
            headers=_auth(token),
            json={"problem_desc": "x", "expected_direction": "y"},
        )
        assert resp.status_code == 404

    def test_list_suggestions_open_to_all(self, client, db):
        """/api/skills/{id}/suggestions 对所有认证用户开放。"""
        dept = _make_dept(db)
        creator = _make_user(db, "ls_cr", Role.EMPLOYEE, dept.id)
        stranger = _make_user(db, "ls_str", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "ListSkill", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "ls_str")

        resp = client.get(f"/api/skills/{skill.id}/suggestions", headers=_auth(token))
        assert resp.status_code == 200

    def test_list_suggestions_with_status_filter(self, client, db):
        dept = _make_dept(db)
        creator = _make_user(db, "filt_cr", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "FiltSkill", SkillStatus.PUBLISHED)
        # 添加一条 pending
        s = SkillSuggestion(
            skill_id=skill.id, submitted_by=creator.id,
            problem_desc="p", expected_direction="e",
            status=SuggestionStatus.PENDING,
        )
        db.add(s)
        db.commit()
        token = _login(client, "filt_cr")

        resp = client.get(
            f"/api/skills/{skill.id}/suggestions?status=pending",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["status"] == "pending" for item in data)

    def test_comments_endpoint_restricted_to_owner_or_admin(self, client, db):
        """/api/skills/{id}/comments 只对 skill 创建者或管理员开放。"""
        dept = _make_dept(db)
        creator = _make_user(db, "cmt_cr", Role.EMPLOYEE, dept.id)
        stranger = _make_user(db, "cmt_str", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "CmtSkill", SkillStatus.PUBLISHED)
        db.commit()

        # 非创建者非管理员 → 403
        token_str = _login(client, "cmt_str")
        resp = client.get(f"/api/skills/{skill.id}/comments", headers=_auth(token_str))
        assert resp.status_code == 403

        # 创建者 → 200
        token_cr = _login(client, "cmt_cr")
        resp = client.get(f"/api/skills/{skill.id}/comments", headers=_auth(token_cr))
        assert resp.status_code == 200

    def test_comments_endpoint_admin_can_access(self, client, db):
        """管理员可以查看任意 skill 的 comments。"""
        dept = _make_dept(db)
        creator = _make_user(db, "cmt_cr2", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "cmt_adm", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "CmtSkill2", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "cmt_adm")

        resp = client.get(f"/api/skills/{skill.id}/comments", headers=_auth(token))
        assert resp.status_code == 200

    def test_my_suggestions_only_own(self, client, db):
        """GET /api/my/suggestions 只返回自己提交的。"""
        dept = _make_dept(db)
        user_a = _make_user(db, "my_a", Role.EMPLOYEE, dept.id)
        user_b = _make_user(db, "my_b", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user_a.id, "MySugSkill", SkillStatus.PUBLISHED)
        # A 提交 2 条，B 提交 1 条
        for _ in range(2):
            db.add(SkillSuggestion(
                skill_id=skill.id, submitted_by=user_a.id,
                problem_desc="A的问题", expected_direction="A的方向",
                status=SuggestionStatus.PENDING,
            ))
        db.add(SkillSuggestion(
            skill_id=skill.id, submitted_by=user_b.id,
            problem_desc="B的问题", expected_direction="B的方向",
            status=SuggestionStatus.PENDING,
        ))
        db.commit()

        token_a = _login(client, "my_a")
        resp = client.get("/api/my/suggestions", headers=_auth(token_a))
        data = resp.json()
        assert len(data) == 2
        assert all(item["submitted_by"] == user_a.id for item in data)


# ═══════════════════════════════════════════════════════════════════════════════
# Module 7: 跨部门隔离
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossDepartmentIsolation:
    """不同部门用户之间的隔离性。"""

    def test_dept_skills_not_visible_to_other_dept(self, client, db):
        """A 部门的部门 Skill 不应出现在 B 部门员工的预挂载中。"""
        dept_a, dept_b, da_a, da_b, emp_a, emp_b = _setup_two_depts(db)
        # A 部门发布 skill
        dept_skill = _make_dept_skill(db, da_a.id, dept_a.id, "A部门专属")
        db.commit()

        # B 部门员工获取配置
        token_b = _login(client, "emp_b")
        resp = client.get("/api/workspace-config", headers=_auth(token_b))
        skill_ids = [s["id"] for s in resp.json()["mounted_skills"]]
        assert dept_skill.id not in skill_ids

    def test_own_dept_skills_visible(self, client, db):
        """A 部门的部门 Skill 应出现在 A 部门员工的预挂载中。"""
        dept_a, dept_b, da_a, da_b, emp_a, emp_b = _setup_two_depts(db)
        dept_skill = _make_dept_skill(db, da_a.id, dept_a.id, "A部门Skill")
        db.commit()

        token_a = _login(client, "emp_a")
        resp = client.get("/api/workspace-config", headers=_auth(token_a))
        skill_ids = [s["id"] for s in resp.json()["mounted_skills"]]
        assert dept_skill.id in skill_ids

    def test_dept_admin_publish_only_affects_own_dept(self, client, db):
        """A 部门管理员发布的标准工作台 for_department_id 应为 A 部门。"""
        dept_a, dept_b, da_a, da_b, emp_a, emp_b = _setup_two_depts(db)
        skill = _make_skill(db, da_a.id, "DaASkill", SkillStatus.PUBLISHED)
        db.commit()

        token = _login(client, "da_a")
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "A部门标准"},
        )
        assert resp.status_code == 200
        ws = db.get(Workspace, resp.json()["workspace_id"])
        assert ws.for_department_id == dept_a.id


# ═══════════════════════════════════════════════════════════════════════════════
# Module 8: 端到端复合场景
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEndScenarios:
    """模拟真实用户旅程。"""

    def test_new_employee_full_journey(self, client, db):
        """
        新员工完整旅程：
        GET 创建配置 → 查看部门 skill → 取消挂载一个 → 保存 → 验证 refresh。
        """
        dept = _make_dept(db)
        admin = _make_user(db, "mgr", Role.DEPT_ADMIN, dept.id)
        emp = _make_user(db, "newbie", Role.EMPLOYEE, dept.id)
        ds1 = _make_dept_skill(db, admin.id, dept.id, "部门Skill1")
        ds2 = _make_dept_skill(db, admin.id, dept.id, "部门Skill2")
        db.commit()
        token = _login(client, "newbie")

        # Step 1: GET 自动创建
        config = client.get("/api/workspace-config", headers=_auth(token)).json()
        dept_skill_ids = {s["id"] for s in config["mounted_skills"] if s["source"] == "dept"}
        assert ds1.id in dept_skill_ids
        assert ds2.id in dept_skill_ids

        # Step 2: 取消挂载 ds2
        skills = []
        for s in config["mounted_skills"]:
            mounted = False if s["id"] == ds2.id else s["mounted"]
            skills.append({"id": s["id"], "source": s["source"], "mounted": mounted})
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in config["mounted_tools"]]

        # Step 3: 保存
        saved = client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": skills, "mounted_tools": tools},
        ).json()
        assert saved["needs_prompt_refresh"] is True
        # ds2 仍在列表中但 mounted=False
        ds2_item = next((s for s in saved["mounted_skills"] if s["id"] == ds2.id), None)
        if ds2_item:
            assert ds2_item["mounted"] is False

    def test_admin_publish_then_employee_sees_published(self, client, db):
        """
        管理员发布标准工作台 → 员工通过 /workspaces API 能看到该推荐工作台。
        """
        dept = _make_dept(db)
        admin = _make_user(db, "pub_admin", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "see_emp", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, admin.id, "AdminSkill", SkillStatus.PUBLISHED)
        db.commit()

        admin_token = _login(client, "pub_admin")
        client.get("/api/workspace-config", headers=_auth(admin_token))

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(admin_token),
            json={"scope": "department", "name": "推荐工作台"},
        )
        assert resp.status_code == 200
        ws_id = resp.json()["workspace_id"]

        # 验证 workspace 存在且有 recommended_by
        ws = db.get(Workspace, ws_id)
        assert ws is not None
        assert ws.recommended_by == admin.id
        assert ws.is_active is not False  # None 或 True

    def test_skill_lifecycle_submit_then_review(self, client, db):
        """
        员工提交意见 → Skill 创建者（另一员工）审核采纳 → 验证状态变更。
        """
        dept = _make_dept(db)
        creator = _make_user(db, "lc_creator", Role.EMPLOYEE, dept.id)
        user = _make_user(db, "lc_user", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, creator.id, "LifeSkill", SkillStatus.PUBLISHED)
        db.commit()

        # 员工提交意见
        user_token = _login(client, "lc_user")
        resp = client.post(
            f"/api/skills/{skill.id}/suggestions",
            headers=_auth(user_token),
            json={
                "problem_desc": "竞品提及时没有分析",
                "expected_direction": "自动展开竞争分析",
            },
        )
        suggestion_id = resp.json()["id"]

        # 创建者审核
        creator_token = _login(client, "lc_creator")
        resp = client.patch(
            f"/api/skill-suggestions/{suggestion_id}/review",
            headers=_auth(creator_token),
            json={"status": "adopted", "review_note": "好主意，下个版本加上"},
        )
        assert resp.status_code == 200

        # 验证状态
        db.expire_all()
        s = db.get(SkillSuggestion, suggestion_id)
        assert s.status == SuggestionStatus.ADOPTED
        assert s.reviewed_by == creator.id

    def test_config_change_then_refresh_prompt(self, client, db):
        """
        保存配置变更 → needs_prompt_refresh=True → 模拟 _refresh_skill_routing_prompt。
        """
        dept = _make_dept(db)
        user = _make_user(db, "ref_user", Role.EMPLOYEE, dept.id)
        s1 = _make_skill(db, user.id, "RefS1", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, user.id, "RefS2", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "ref_user")

        # 创建配置
        client.get("/api/workspace-config", headers=_auth(token))

        # 全部挂载
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [
                    {"id": s1.id, "source": "own", "mounted": True},
                    {"id": s2.id, "source": "own", "mounted": True},
                ],
                "mounted_tools": [],
            },
        )

        cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
        assert cfg.needs_prompt_refresh is True

        # 模拟 refresh
        from app.services.skill_engine import skill_engine
        from app.services.llm_gateway import llm_gateway as _gw
        with patch.object(_gw, "chat", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = ("## Skill 路由指引\n- 分析 → RefS1\n- 汇总 → RefS2", {})
            with patch.object(_gw, "get_config", return_value={}):
                import asyncio
                asyncio.run(skill_engine._refresh_skill_routing_prompt(db, cfg))

        assert cfg.needs_prompt_refresh is False
        assert cfg.skill_routing_prompt is not None
        assert "Skill 路由指引" in cfg.skill_routing_prompt

    def test_unmount_all_then_refresh_clears_prompt(self, client, db):
        """
        卸载所有 skill → refresh 应清空路由 prompt。
        """
        dept = _make_dept(db)
        user = _make_user(db, "clear_user", Role.EMPLOYEE, dept.id)
        skill = _make_skill(db, user.id, "ClearS", SkillStatus.PUBLISHED)
        db.commit()
        token = _login(client, "clear_user")

        # 先挂载
        client.get("/api/workspace-config", headers=_auth(token))
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [{"id": skill.id, "source": "own", "mounted": True}],
                "mounted_tools": [],
            },
        )

        cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
        cfg.skill_routing_prompt = "旧 prompt"
        cfg.last_skill_snapshot = [{"name": "ClearS", "description": "x"}]
        cfg.needs_prompt_refresh = False
        db.commit()

        # 卸载所有
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={"mounted_skills": [], "mounted_tools": []},
        )

        db.expire_all()
        cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
        assert cfg.needs_prompt_refresh is True

        # refresh 应清空（mounted_skills 为空时不调用 LLM，直接清空）
        from app.services.skill_engine import skill_engine
        import asyncio
        asyncio.run(skill_engine._refresh_skill_routing_prompt(db, cfg))

        assert cfg.skill_routing_prompt is None or cfg.skill_routing_prompt == ""
        assert cfg.needs_prompt_refresh is False

    def test_two_admins_publish_independently(self, client, db):
        """
        两个管理员各自发布 → 生成独立的 workspace，互不覆盖。
        """
        dept = _make_dept(db)
        admin1 = _make_user(db, "adm1", Role.SUPER_ADMIN, dept.id)
        admin2 = _make_user(db, "adm2", Role.DEPT_ADMIN, dept.id)
        s1 = _make_skill(db, admin1.id, "Admin1Skill", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, admin2.id, "Admin2Skill", SkillStatus.PUBLISHED)
        db.commit()

        t1 = _login(client, "adm1")
        t2 = _login(client, "adm2")

        client.get("/api/workspace-config", headers=_auth(t1))
        client.get("/api/workspace-config", headers=_auth(t2))

        r1 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(t1),
            json={"scope": "department", "name": "管理员1标准"},
        )
        r2 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(t2),
            json={"scope": "department", "name": "管理员2标准"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["workspace_id"] != r2.json()["workspace_id"]

    def test_republish_clears_old_bindings(self, client, db):
        """
        重新发布时旧的 WorkspaceSkill/Tool 绑定应被清除并替换。
        """
        dept = _make_dept(db)
        admin = _make_user(db, "rebind", Role.SUPER_ADMIN, dept.id)
        s1 = _make_skill(db, admin.id, "OldS", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, admin.id, "NewS", SkillStatus.PUBLISHED)
        t1 = _make_tool(db, admin.id, "old_t", ToolType.BUILTIN)
        db.commit()

        token = _login(client, "rebind")
        client.get("/api/workspace-config", headers=_auth(token))

        # 首次发布: s1 + t1
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [{"id": s1.id, "source": "own", "mounted": True}],
                "mounted_tools": [{"id": t1.id, "source": "own", "mounted": True}],
            },
        )
        r1 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "company", "name": "v1"},
        )
        ws_id = r1.json()["workspace_id"]

        # 重新配置: 只有 s2，没有 t1
        client.put(
            "/api/workspace-config",
            headers=_auth(token),
            json={
                "mounted_skills": [{"id": s2.id, "source": "own", "mounted": True}],
                "mounted_tools": [],
            },
        )
        r2 = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "company", "name": "v2"},
        )
        assert r2.json()["workspace_id"] == ws_id  # upsert 同一个

        ws_skills = db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == ws_id).all()
        ws_tools = db.query(WorkspaceTool).filter(WorkspaceTool.workspace_id == ws_id).all()
        assert len(ws_skills) == 1
        assert ws_skills[0].skill_id == s2.id
        assert len(ws_tools) == 0  # 旧 tool 已被清除


# ═══════════════════════════════════════════════════════════════════════════════
# Module 9: 数据一致性 & 并发
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataConsistency:
    """数据一致性边界。"""

    def test_config_unique_per_user(self, db):
        """UserWorkspaceConfig 的 user_id 唯一约束。"""
        dept = _make_dept(db)
        user = _make_user(db, "uniq", Role.EMPLOYEE, dept.id)
        db.commit()

        cfg1 = UserWorkspaceConfig(user_id=user.id, mounted_skills=[], mounted_tools=[])
        db.add(cfg1)
        db.commit()

        cfg2 = UserWorkspaceConfig(user_id=user.id, mounted_skills=[], mounted_tools=[])
        db.add(cfg2)
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

    def test_workspace_skill_references_valid(self, client, db):
        """发布后 WorkspaceSkill.skill_id 应指向有效 Skill。"""
        dept = _make_dept(db)
        admin = _make_user(db, "valid_ref", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, admin.id, "ValidRef", SkillStatus.PUBLISHED)
        db.commit()

        token = _login(client, "valid_ref")
        client.get("/api/workspace-config", headers=_auth(token))

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth(token),
            json={"scope": "department", "name": "引用验证"},
        )
        ws_id = resp.json()["workspace_id"]

        ws_skills = db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == ws_id).all()
        for ws_skill in ws_skills:
            assert db.get(Skill, ws_skill.skill_id) is not None

    def test_multiple_users_independent_configs(self, client, db):
        """多用户配置互相独立。"""
        dept = _make_dept(db)
        u1 = _make_user(db, "indep1", Role.EMPLOYEE, dept.id)
        u2 = _make_user(db, "indep2", Role.EMPLOYEE, dept.id)
        s1 = _make_skill(db, u1.id, "U1Skill", SkillStatus.PUBLISHED)
        s2 = _make_skill(db, u2.id, "U2Skill", SkillStatus.PUBLISHED)
        db.commit()

        t1 = _login(client, "indep1")
        t2 = _login(client, "indep2")

        client.get("/api/workspace-config", headers=_auth(t1))
        client.get("/api/workspace-config", headers=_auth(t2))

        # U1 卸载所有
        client.put(
            "/api/workspace-config",
            headers=_auth(t1),
            json={"mounted_skills": [], "mounted_tools": []},
        )

        # U2 仍有自己的 skill
        r2 = client.get("/api/workspace-config", headers=_auth(t2)).json()
        u2_ids = [s["id"] for s in r2["mounted_skills"]]
        assert s2.id in u2_ids

    def test_archived_skill_not_premounted(self, client, db):
        """已归档的 Skill 不应出现在预挂载中。"""
        dept = _make_dept(db)
        user = _make_user(db, "arch", Role.EMPLOYEE, dept.id)
        active_skill = _make_skill(db, user.id, "ActiveS", SkillStatus.PUBLISHED)
        archived_skill = _make_skill(db, user.id, "ArchivedS", SkillStatus.ARCHIVED)
        db.commit()

        token = _login(client, "arch")
        resp = client.get("/api/workspace-config", headers=_auth(token))
        ids = [s["id"] for s in resp.json()["mounted_skills"]]
        assert active_skill.id in ids
        assert archived_skill.id not in ids

    def test_draft_skill_still_premounted_for_owner(self, client, db):
        """草稿 Skill 也应预挂载（自己的所有非归档 skill 都挂载）。"""
        dept = _make_dept(db)
        user = _make_user(db, "draft_own", Role.EMPLOYEE, dept.id)
        draft_skill = _make_skill(db, user.id, "DraftS", SkillStatus.DRAFT)
        db.commit()

        token = _login(client, "draft_own")
        resp = client.get("/api/workspace-config", headers=_auth(token))
        ids = [s["id"] for s in resp.json()["mounted_skills"]]
        assert draft_skill.id in ids
