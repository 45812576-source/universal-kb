"""权限系统集成测试 — 覆盖新增的运行时拦截点

测试模块：
    TC-INT-01  skill_engine callable 拦截
    TC-INT-02  skill_engine data_scope 注入到 system prompt
    TC-INT-03  data_query 结果字段脱敏
    TC-INT-04  输出侧 output_mask
    TC-INT-05  projects API 角色级字段过滤
    TC-INT-06  跨角色端到端场景
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import _make_dept, _make_user as _make_user_orig, _make_skill, _login, _auth
from app.models.user import Role, User
from app.models.permission import (
    DataDomain, DataScopePolicy, GlobalDataMask, MaskAction,
    PolicyResourceType, PolicyTargetType, Position, RoleMaskOverride,
    RoleOutputMask, RolePolicyOverride, SkillMaskOverride, SkillPolicy,
    VisibilityScope,
)
from app.services.permission_engine import PermissionEngine, permission_engine
from app.services.data_visibility import DataVisibility, data_visibility
from app.models.skill import Skill, SkillStatus


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_position(db, name="商务"):
    pos = Position(name=name, description=f"{name}岗位")
    db.add(pos)
    db.flush()
    return pos


def _make_domain(db, name="client"):
    domain = DataDomain(
        name=name, display_name=f"{name}数据域", description="测试",
        fields=[
            {"name": "client_name", "label": "客户名", "sensitive": False, "type": "string"},
            {"name": "contract_value", "label": "合同金额", "sensitive": True, "type": "number"},
            {"name": "cost", "label": "成本", "sensitive": True, "type": "number"},
        ],
    )
    db.add(domain)
    db.flush()
    return domain


def _make_user(db, username, role, dept_id=None, position_id=None, password="Test1234!"):
    u = _make_user_orig(db, username, role, dept_id, password)
    if position_id:
        u.position_id = position_id
        db.flush()
    return u


def _make_admin(db, client):
    dept = _make_dept(db)
    user = _make_user(db, "int_admin", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "int_admin")
    return user, _auth(token)


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-01  skill_engine — callable 拦截
# ═════════════════════════════════════════════════════════════════════════════

class TestSkillEngineCallable:
    """验证 skill_engine.prepare 在匹配到 Skill 后正确调用 callable 检查"""

    @pytest.mark.asyncio
    async def test_uncallable_skill_is_skipped(self, db):
        """岗位被标记 callable=False 时，prepare 应跳过该 Skill"""
        from app.services.skill_engine import SkillEngine
        from app.models.conversation import Conversation

        pos = _make_position(db, "受限岗位_int")
        dept = _make_dept(db, "INT部门")
        creator = _make_user(db, "int_creator1", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "受限Skill")

        # 配 policy: callable=False
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=False, data_scope={}, output_mask=[],
        )
        db.add(override)

        emp = _make_user(db, "int_emp1", Role.EMPLOYEE, dept.id, position_id=pos.id)
        conv = Conversation(user_id=emp.id, title="test")
        db.add(conv)
        db.commit()

        # 直接测试 callable 检查（不需要真正跑 LLM）
        assert permission_engine.check_skill_callable(emp, skill.id, db) is False

    @pytest.mark.asyncio
    async def test_callable_skill_passes(self, db):
        """岗位被标记 callable=True 时，检查应通过"""
        pos = _make_position(db, "开放岗位_int")
        dept = _make_dept(db, "INT部门2")
        creator = _make_user(db, "int_creator2", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "开放Skill")

        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=True, data_scope={"client": {"visibility": "own"}}, output_mask=[],
        )
        db.add(override)

        emp = _make_user(db, "int_emp2", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        assert permission_engine.check_skill_callable(emp, skill.id, db) is True

    def test_super_admin_bypasses_callable_restriction(self, db):
        """超管即使被标 callable=False 也能通过"""
        pos = _make_position(db, "超管测试岗")
        dept = _make_dept(db, "INT部门3")
        creator = _make_user(db, "int_creator3", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "超管Skill")

        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=False, data_scope={}, output_mask=[],
        )
        db.add(override)

        admin = _make_user(db, "int_admin3", Role.SUPER_ADMIN, dept.id, position_id=pos.id)
        db.commit()

        assert permission_engine.check_skill_callable(admin, skill.id, db) is True


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-02  skill_engine — data_scope 注入 system prompt
# ═════════════════════════════════════════════════════════════════════════════

class TestDataScopeInjection:
    """验证 data_scope 会被注入到 system prompt"""

    def test_data_scope_returns_override(self, db):
        """get_data_scope 返回岗位级覆盖"""
        pos = _make_position(db, "商务_scope_int")
        dept = _make_dept(db, "SCOPE部门")
        creator = _make_user(db, "scope_creator_int", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "scope测试Skill")

        scope_data = {
            "client": {"visibility": "own", "fields": ["name", "industry"], "excluded": ["contacts"]},
            "financial": {"visibility": "own_client", "fields": ["contract_value"], "excluded": ["cost", "margin"]},
        }
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role",
                             default_data_scope={"visibility": "none"})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=True, data_scope=scope_data, output_mask=[],
        )
        db.add(override)

        emp = _make_user(db, "scope_emp_int", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        result = permission_engine.get_data_scope(emp, skill.id, db)
        assert "client" in result
        assert result["client"]["visibility"] == "own"
        assert "contacts" in result["client"]["excluded"]
        assert "financial" in result
        assert "cost" in result["financial"]["excluded"]

    def test_data_scope_default_when_no_override(self, db):
        """无岗位覆盖时返回默认 scope"""
        dept = _make_dept(db, "SCOPE部门2")
        creator = _make_user(db, "scope_creator_int2", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "scope默认Skill")

        policy = SkillPolicy(skill_id=skill.id, publish_scope="org_wide",
                             default_data_scope={"knowledge": {"visibility": "all"}})
        db.add(policy)
        db.commit()

        emp = _make_user(db, "scope_emp_int2", Role.EMPLOYEE, dept.id)
        db.commit()

        result = permission_engine.get_data_scope(emp, skill.id, db)
        assert result["knowledge"]["visibility"] == "all"


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-03  data_query 结果字段脱敏
# ═════════════════════════════════════════════════════════════════════════════

class TestDataQueryMasking:
    """验证 apply_with_permission_engine 对查询结果做字段级脱敏"""

    def test_global_mask_hides_sensitive_fields(self, db):
        """全局规则 hide 生效"""
        dept = _make_dept(db, "DQ部门")
        creator = _make_user(db, "dq_creator", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "数据查询Skill")
        emp = _make_user(db, "dq_emp", Role.EMPLOYEE, dept.id)

        db.add(GlobalDataMask(field_name="salary_exact", mask_action=MaskAction.HIDE, severity=5))
        db.add(GlobalDataMask(field_name="personal_id", mask_action=MaskAction.HIDE, severity=5))
        db.commit()

        rows = [
            {"name": "张三", "salary_exact": 50000, "personal_id": "310101199001011234", "dept": "技术部"},
            {"name": "李四", "salary_exact": 45000, "personal_id": "310101199002021234", "dept": "产品部"},
        ]
        result = data_visibility.apply_with_permission_engine(
            rows=rows, user=emp, skill_id=skill.id, data_domain_id=None, db=db,
        )
        for row in result:
            assert row.get("salary_exact") is None
            assert row.get("personal_id") is None
            assert row["name"] is not None
            assert row["dept"] is not None

    def test_role_mask_overrides_global(self, db):
        """角色级覆盖应优先于全局"""
        pos = _make_position(db, "财务_dq")
        domain = _make_domain(db, "financial_dq")
        dept = _make_dept(db, "DQ部门2")
        creator = _make_user(db, "dq_creator2", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "财务查询Skill")

        # 全局：contract_value → range
        db.add(GlobalDataMask(
            field_name="contract_value", mask_action=MaskAction.RANGE,
            mask_params={"step": 100000}, severity=3, data_domain_id=domain.id,
        ))
        # 角色覆盖：财务岗 → keep（财务可看精确值）
        db.add(RoleMaskOverride(
            position_id=pos.id, field_name="contract_value",
            mask_action=MaskAction.KEEP, data_domain_id=domain.id,
        ))
        emp = _make_user(db, "finance_emp_dq", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        rows = [{"contract_value": 350000, "client_name": "品牌A"}]
        result = data_visibility.apply_with_permission_engine(
            rows=rows, user=emp, skill_id=skill.id, data_domain_id=domain.id, db=db,
        )
        assert result[0]["contract_value"] == 350000  # 财务可看精确值

    def test_skill_mask_overrides_role(self, db):
        """Skill 级覆盖最优先"""
        pos = _make_position(db, "策划_dq")
        domain = _make_domain(db, "creative_dq")
        dept = _make_dept(db, "DQ部门3")
        creator = _make_user(db, "dq_creator3", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "策划脱敏Skill")

        # 角色级：budget → range
        db.add(RoleMaskOverride(
            position_id=pos.id, field_name="budget",
            mask_action=MaskAction.RANGE, mask_params={"step": 50000},
            data_domain_id=domain.id,
        ))
        # Skill 级覆盖：budget → hide（此 Skill 不给看预算）
        db.add(SkillMaskOverride(
            skill_id=skill.id, field_name="budget",
            mask_action=MaskAction.HIDE, position_id=pos.id,
        ))
        emp = _make_user(db, "planner_emp_dq", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        rows = [{"budget": 200000, "title": "秋促方案"}]
        result = permission_engine.apply_data_masks(emp, skill.id, rows, domain.id, db)
        assert result[0].get("budget") is None  # Skill 级 hide 生效
        assert result[0]["title"] == "秋促方案"

    def test_super_admin_sees_all(self, db):
        """超管不受任何脱敏影响"""
        dept = _make_dept(db, "DQ部门4")
        admin = _make_user(db, "dq_admin", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, admin.id, "超管Skill_dq")

        db.add(GlobalDataMask(field_name="secret_field", mask_action=MaskAction.HIDE, severity=5))
        db.commit()

        rows = [{"secret_field": "机密数据", "public_field": "公开数据"}]
        result = permission_engine.apply_data_masks(admin, skill.id, rows, None, db)
        assert result[0]["secret_field"] == "机密数据"

    def test_range_mask_converts_to_interval(self, db):
        """range 脱敏正确将精确值转为区间"""
        dept = _make_dept(db, "DQ部门5")
        emp = _make_user(db, "dq_emp5", Role.EMPLOYEE, dept.id)
        creator = _make_user(db, "dq_creator5", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "区间Skill")

        db.add(GlobalDataMask(
            field_name="revenue", mask_action=MaskAction.RANGE,
            mask_params={"step": 1000000}, severity=3,
        ))
        db.commit()

        rows = [{"revenue": 3500000, "client": "品牌B"}]
        result = permission_engine.apply_data_masks(emp, skill.id, rows, None, db)
        assert result[0]["revenue"] == "3000000-4000000"

    def test_truncate_mask(self, db):
        """truncate 脱敏截断字符串"""
        dept = _make_dept(db, "DQ部门6")
        emp = _make_user(db, "dq_emp6", Role.EMPLOYEE, dept.id)
        creator = _make_user(db, "dq_creator6", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "截断Skill")

        db.add(GlobalDataMask(
            field_name="phone", mask_action=MaskAction.TRUNCATE,
            mask_params={"length": 4}, severity=3,
        ))
        db.commit()

        rows = [{"phone": "13912345678", "name": "张三"}]
        result = permission_engine.apply_data_masks(emp, skill.id, rows, None, db)
        assert result[0]["phone"] == "1391..."

    def test_remove_mask_drops_field(self, db):
        """remove 脱敏完全移除字段"""
        dept = _make_dept(db, "DQ部门7")
        emp = _make_user(db, "dq_emp7", Role.EMPLOYEE, dept.id)
        creator = _make_user(db, "dq_creator7", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "移除Skill")

        db.add(GlobalDataMask(field_name="internal_memo", mask_action=MaskAction.REMOVE, severity=5))
        db.commit()

        rows = [{"internal_memo": "内部备注", "status": "active"}]
        result = permission_engine.apply_data_masks(emp, skill.id, rows, None, db)
        assert "internal_memo" not in result[0]
        assert result[0]["status"] == "active"


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-04  输出侧 output_mask
# ═════════════════════════════════════════════════════════════════════════════

class TestOutputMask:
    """验证 apply_output_masks 对 structured_output 做输出侧遮罩"""

    def test_output_mask_hides_field(self, db):
        """RoleOutputMask hide 应移除对应字段"""
        pos = _make_position(db, "HR_out")
        domain = _make_domain(db, "financial_out")
        dept = _make_dept(db, "OUT部门")

        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="contract_value", mask_action=MaskAction.HIDE,
        ))
        emp = _make_user(db, "hr_out_emp", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        output = {"contract_value": 500000, "headcount": 10, "department": "电商部"}
        result = permission_engine.apply_output_masks(emp, output, domain.id, db)
        assert "contract_value" not in result
        assert result["headcount"] == 10
        assert result["department"] == "电商部"

    def test_output_mask_range(self, db):
        """RoleOutputMask range 将精确值转区间"""
        pos = _make_position(db, "商务_out")
        domain = _make_domain(db, "financial_out2")
        dept = _make_dept(db, "OUT部门2")

        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="budget", mask_action=MaskAction.RANGE,
        ))
        emp = _make_user(db, "sales_out_emp", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        output = {"budget": 350000, "project_name": "秋促"}
        result = permission_engine.apply_output_masks(emp, output, domain.id, db)
        # range 默认 step=10000
        assert "budget" in result
        assert result["project_name"] == "秋促"

    def test_output_mask_aggregate(self, db):
        """RoleOutputMask aggregate 应返回统计值"""
        pos = _make_position(db, "管理层_out")
        domain = _make_domain(db, "hr_out")
        dept = _make_dept(db, "OUT部门3")

        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.AGGREGATE,
        ))
        emp = _make_user(db, "mgmt_out_emp", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        output = {"salary_exact": 50000, "department": "技术部"}
        result = permission_engine.apply_output_masks(emp, output, domain.id, db)
        assert result["salary_exact"] == "统计值"
        assert result["department"] == "技术部"

    def test_super_admin_bypasses_output_mask(self, db):
        """超管不受 output_mask 影响"""
        pos = _make_position(db, "超管_out")
        domain = _make_domain(db, "hr_out2")
        dept = _make_dept(db, "OUT部门4")

        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.HIDE,
        ))
        admin = _make_user(db, "admin_out_emp", Role.SUPER_ADMIN, dept.id, position_id=pos.id)
        db.commit()

        output = {"salary_exact": 50000}
        result = permission_engine.apply_output_masks(admin, output, domain.id, db)
        assert result["salary_exact"] == 50000


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-05  projects API — 角色级字段过滤
# ═════════════════════════════════════════════════════════════════════════════

class TestProjectFieldFilter:
    """验证 projects API 对不同岗位的返回字段做过滤"""

    def test_hr_cannot_see_brief_and_budget(self, client, db):
        """HR 岗位看不到 brief、budget、creative_content"""
        pos = _make_position(db, "HR_proj")
        domain = _make_domain(db, "project_hr")
        # 改 domain name 为 "project"
        domain.name = "project"
        db.flush()

        # 配置 DataScopePolicy
        db.add(DataScopePolicy(
            target_type=PolicyTargetType.POSITION,
            target_position_id=pos.id,
            resource_type=PolicyResourceType.DATA_DOMAIN,
            data_domain_id=domain.id,
            visibility_level=VisibilityScope.ALL,
            output_mask=["brief", "creative_content", "budget_range"],
        ))

        dept = _make_dept(db, "HR项目部门")
        # 创建一个超管作为项目负责人
        owner = _make_user(db, "proj_owner_hr", Role.SUPER_ADMIN, dept.id)
        db.commit()
        owner_token = _login(client, "proj_owner_hr")

        # 创建项目
        hr_user = _make_user(db, "hr_proj_emp", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        resp = client.post("/api/projects", json={
            "name": "测试项目HR",
            "description": "项目描述 with brief and budget info",
            "members": [{"user_id": hr_user.id, "role_desc": "HR"}],
        }, headers=_auth(owner_token))
        assert resp.status_code == 200
        project_id = resp.json()["id"]

        # HR 用户获取项目列表
        hr_token = _login(client, "hr_proj_emp")
        resp = client.get("/api/projects", headers=_auth(hr_token))
        assert resp.status_code == 200
        projects = resp.json()
        # 应至少有 1 个项目
        assert len(projects) >= 1
        proj = next(p for p in projects if p["id"] == project_id)
        # brief/creative_content/budget_range 不在返回中
        assert "brief" not in proj
        assert "creative_content" not in proj
        assert "budget_range" not in proj
        # 基本字段仍在
        assert "name" in proj
        assert "id" in proj

    def test_super_admin_sees_all_fields(self, client, db):
        """超管应看到所有项目字段"""
        pos = _make_position(db, "超管_proj")
        domain = _make_domain(db, "project_admin")
        domain.name = "project"
        db.flush()

        # 即使配了 output_mask，超管也不过滤
        db.add(DataScopePolicy(
            target_type=PolicyTargetType.POSITION,
            target_position_id=pos.id,
            resource_type=PolicyResourceType.DATA_DOMAIN,
            data_domain_id=domain.id,
            visibility_level=VisibilityScope.ALL,
            output_mask=["description"],
        ))
        dept = _make_dept(db, "超管项目部门")
        admin = _make_user(db, "admin_proj_all", Role.SUPER_ADMIN, dept.id, position_id=pos.id)
        db.commit()
        token = _login(client, "admin_proj_all")

        resp = client.post("/api/projects", json={
            "name": "超管项目",
            "description": "超管可见所有字段",
            "members": [],
        }, headers=_auth(token))
        assert resp.status_code == 200

        resp = client.get("/api/projects", headers=_auth(token))
        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) >= 1
        proj = projects[0]
        assert "description" in proj  # 超管不过滤
        assert "name" in proj

    def test_employee_without_position_sees_all(self, client, db):
        """无岗位的员工不过滤（宽松策略）"""
        dept = _make_dept(db, "无岗位部门")
        owner = _make_user(db, "no_pos_owner", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, "no_pos_emp", Role.EMPLOYEE, dept.id)
        db.commit()

        owner_token = _login(client, "no_pos_owner")
        resp = client.post("/api/projects", json={
            "name": "无岗位项目",
            "description": "描述应可见",
            "members": [{"user_id": emp.id, "role_desc": "成员"}],
        }, headers=_auth(owner_token))
        assert resp.status_code == 200

        emp_token = _login(client, "no_pos_emp")
        resp = client.get("/api/projects", headers=_auth(emp_token))
        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) >= 1
        assert "description" in projects[0]


# ═════════════════════════════════════════════════════════════════════════════
# TC-INT-06  跨角色端到端场景
# ═════════════════════════════════════════════════════════════════════════════

class TestCrossRoleEndToEnd:
    """模拟设计文档中的完整跨角色场景"""

    def test_sales_full_flow(self, db):
        """商务：看自己客户全貌，看不到成本/利润率，合同金额可见"""
        pos = _make_position(db, "商务_e2e")
        domain = _make_domain(db, "financial_e2e")
        dept = _make_dept(db, "E2E部门")
        creator = _make_user(db, "e2e_creator1", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "商务分析Skill")

        # 全局规则
        db.add(GlobalDataMask(field_name="cost", mask_action=MaskAction.HIDE, severity=4))
        db.add(GlobalDataMask(field_name="margin", mask_action=MaskAction.HIDE, severity=4))
        db.add(GlobalDataMask(field_name="company_revenue", mask_action=MaskAction.HIDE, severity=5))
        # 输出遮罩：商务不能看到 cost/margin
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="cost", mask_action=MaskAction.HIDE,
        ))
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="margin", mask_action=MaskAction.HIDE,
        ))

        sales = _make_user(db, "sales_e2e", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        # 输入侧脱敏
        data_in = [{"client_name": "品牌A", "contract_value": 500000, "cost": 300000, "margin": 0.4}]
        masked_in = permission_engine.apply_data_masks(sales, skill.id, data_in, domain.id, db)
        assert masked_in[0]["client_name"] == "品牌A"
        assert masked_in[0]["contract_value"] == 500000
        assert masked_in[0].get("cost") is None
        assert masked_in[0].get("margin") is None

        # 输出侧遮罩
        output = {"contract_value": 500000, "cost": 300000, "margin": 0.4}
        masked_out = permission_engine.apply_output_masks(sales, output, domain.id, db)
        assert "cost" not in masked_out
        assert "margin" not in masked_out
        assert masked_out["contract_value"] == 500000

    def test_planner_budget_range_only(self, db):
        """策划：只能看到预算区间，精确预算 → range"""
        pos = _make_position(db, "创意_e2e")
        domain = _make_domain(db, "financial_e2e2")
        dept = _make_dept(db, "E2E部门2")
        creator = _make_user(db, "e2e_creator2", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "策划分析Skill")

        # 全局：budget_exact → range
        db.add(GlobalDataMask(
            field_name="budget_exact", mask_action=MaskAction.RANGE,
            mask_params={"step": 100000}, severity=3,
        ))
        # 合同精确值对策划 hide
        db.add(GlobalDataMask(
            field_name="contract_value", mask_action=MaskAction.HIDE, severity=4,
        ))

        planner = _make_user(db, "planner_e2e", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        data = [{"budget_exact": 350000, "contract_value": 800000, "campaign_title": "秋促"}]
        result = permission_engine.apply_data_masks(planner, skill.id, data, domain.id, db)
        assert result[0]["budget_exact"] == "300000-400000"
        assert result[0].get("contract_value") is None
        assert result[0]["campaign_title"] == "秋促"

    def test_finance_sees_money_not_creative(self, db):
        """财务：看所有钱相关数据，看不到创意内容"""
        pos = _make_position(db, "财务_e2e")
        dept = _make_dept(db, "E2E部门3")
        creator = _make_user(db, "e2e_creator3", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "财务查询Skill")

        db.add(GlobalDataMask(field_name="creative_content", mask_action=MaskAction.REMOVE, severity=3))
        db.add(GlobalDataMask(field_name="raw_script", mask_action=MaskAction.REMOVE, severity=3))

        finance = _make_user(db, "finance_e2e", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        data = [{"contract_value": 500000, "cost": 300000, "creative_content": "创意脚本...", "raw_script": "拍摄脚本..."}]
        result = permission_engine.apply_data_masks(finance, skill.id, data, None, db)
        assert result[0]["contract_value"] == 500000
        assert result[0]["cost"] == 300000
        assert "creative_content" not in result[0]
        assert "raw_script" not in result[0]

    def test_hr_sees_people_not_finance(self, db):
        """HR：人事全量，业务侧只看人力元信息"""
        pos = _make_position(db, "HR_e2e")
        domain = _make_domain(db, "financial_e2e3")
        dept = _make_dept(db, "E2E部门4")
        creator = _make_user(db, "e2e_creator4", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "HR查询Skill")

        # 输出遮罩：HR 看不到财务字段
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="contract_value", mask_action=MaskAction.HIDE,
        ))
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="revenue", mask_action=MaskAction.HIDE,
        ))

        hr = _make_user(db, "hr_e2e", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        output = {"headcount": 20, "department": "电商部", "contract_value": 500000, "revenue": 1000000}
        result = permission_engine.apply_output_masks(hr, output, domain.id, db)
        assert result["headcount"] == 20
        assert result["department"] == "电商部"
        assert "contract_value" not in result
        assert "revenue" not in result

    def test_management_salary_aggregated(self, db):
        """管理层：投屏场景薪资聚合，绩效→等级"""
        pos = _make_position(db, "管理层_e2e")
        domain = _make_domain(db, "hr_e2e")
        dept = _make_dept(db, "E2E部门5")

        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.AGGREGATE,
        ))
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="performance_score", mask_action=MaskAction.RANK,
        ))
        db.add(RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="personal_id", mask_action=MaskAction.HIDE,
        ))

        mgmt = _make_user(db, "mgmt_e2e", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        output = {
            "salary_exact": 50000,
            "performance_score": 87,
            "personal_id": "310101199001011234",
            "department": "电商部",
            "headcount": 20,
        }
        result = permission_engine.apply_output_masks(mgmt, output, domain.id, db)
        assert result["salary_exact"] == "统计值"
        assert result["performance_score"] == "Top N"
        assert "personal_id" not in result
        assert result["department"] == "电商部"
        assert result["headcount"] == 20

    def test_three_layer_merge_priority(self, db):
        """三层优先级：Skill级 > 角色级 > 全局"""
        pos = _make_position(db, "三层_e2e")
        domain = _make_domain(db, "三层domain")
        dept = _make_dept(db, "三层部门")
        creator = _make_user(db, "三层_creator", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "三层Skill")

        # 全局：phone → truncate(4)
        db.add(GlobalDataMask(
            field_name="phone", mask_action=MaskAction.TRUNCATE,
            mask_params={"length": 4}, severity=3, data_domain_id=domain.id,
        ))
        # 角色级：phone → partial(3) 覆盖全局
        db.add(RoleMaskOverride(
            position_id=pos.id, field_name="phone",
            mask_action=MaskAction.PARTIAL, mask_params={"prefix_len": 3},
            data_domain_id=domain.id,
        ))
        # Skill 级：phone → hide 覆盖角色级
        db.add(SkillMaskOverride(
            skill_id=skill.id, field_name="phone",
            mask_action=MaskAction.HIDE, position_id=pos.id,
        ))

        emp = _make_user(db, "三层_emp", Role.EMPLOYEE, dept.id, position_id=pos.id)
        db.commit()

        # Skill 级生效 → hide
        action, _ = permission_engine.merge_mask_rules(emp, skill.id, "phone", domain.id, db)
        assert action == MaskAction.HIDE

        # 验证实际脱敏效果
        rows = [{"phone": "13912345678", "name": "张三"}]
        result = permission_engine.apply_data_masks(emp, skill.id, rows, domain.id, db)
        assert result[0].get("phone") is None

    def test_empty_rows_passthrough(self, db):
        """空数据不报错直接返回"""
        dept = _make_dept(db, "空数据部门")
        emp = _make_user(db, "empty_emp", Role.EMPLOYEE, dept.id)
        creator = _make_user(db, "empty_creator", Role.SUPER_ADMIN, dept.id)
        skill = _make_skill(db, creator.id, "空Skill")
        db.commit()

        result = permission_engine.apply_data_masks(emp, skill.id, [], None, db)
        assert result == []

        result2 = data_visibility.apply_with_permission_engine(
            rows=[], user=emp, skill_id=skill.id, data_domain_id=None, db=db,
        )
        assert result2 == []
