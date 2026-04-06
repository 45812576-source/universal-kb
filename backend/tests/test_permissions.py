"""权限系统集成测试 — 覆盖跨角色边界场景

测试模块：
    TC-PERM-01  岗位（Position）CRUD
    TC-PERM-02  数据域（DataDomain）CRUD
    TC-PERM-03  数据范围策略（DataScopePolicy）CRUD
    TC-PERM-04  全局脱敏规则（GlobalDataMask）CRUD
    TC-PERM-05  角色脱敏覆盖（RoleMaskOverride）CRUD
    TC-PERM-06  角色输出遮罩（RoleOutputMask）CRUD
    TC-PERM-07  脱敏预览（MaskPreview）
    TC-PERM-08  Skill Policy CRUD
    TC-PERM-09  角色 Policy 覆盖（RolePolicyOverride）
    TC-PERM-10  Skill 脱敏覆盖（SkillMaskOverride）
    TC-PERM-11  Agent 连接白名单（AgentConnection）
    TC-PERM-12  审批流（ApprovalRequest / ApprovalAction）
    TC-PERM-13  Output Schema 生成与审批
    TC-PERM-14  Handoff 模板 CRUD 与缓存提升
    TC-PERM-15  PermissionEngine — check_skill_callable
    TC-PERM-16  PermissionEngine — get_data_scope
    TC-PERM-17  PermissionEngine — 三层脱敏合并（merge_mask_rules）
    TC-PERM-18  PermissionEngine — apply_data_masks 字段级替换
    TC-PERM-19  PermissionEngine — apply_output_masks 输出侧遮罩
    TC-PERM-20  HandoffEngine — 白名单校验
    TC-PERM-21  HandoffEngine — Schema 解析（模板→缓存→动态）
    TC-PERM-22  HandoffEngine — Payload 提取与合规校验
    TC-PERM-23  跨角色边界场景：商务无法看到成本字段
    TC-PERM-24  跨角色边界场景：策划只能看到预算区间
    TC-PERM-25  跨角色边界场景：HR 无法看到财务字段
    TC-PERM-26  跨角色边界场景：管理层 salary_exact→aggregate
    TC-PERM-27  用户岗位绑定 API
    TC-PERM-28  非管理员无法访问权限 API
"""
import datetime
import pytest

from tests.conftest import _make_dept, _make_user, _make_skill, _login, _auth
from app.models.user import Role
from app.models.permission import (
    ApprovalRequest,
    DataDomain, DataScopePolicy, GlobalDataMask, HandoffSchemaCache,
    HandoffTemplate, HandoffTemplateType, MaskAction, PolicyResourceType,
    PolicyTargetType, Position, RoleMaskOverride, RoleOutputMask,
    SkillAgentConnection, SkillMaskOverride, SkillPolicy, VisibilityScope,
    RolePolicyOverride,
)
from app.services.permission_engine import PermissionEngine
from app.services.handoff_engine import HandoffEngine


def _attach_sandbox_report(db, approval_request_id, tester_id, target_type="skill", target_id=1):
    """为审批请求创建沙盒测试报告并关联。"""
    from app.models.sandbox import SandboxTestSession, SandboxTestReport
    session = SandboxTestSession(
        target_type=target_type,
        target_id=target_id,
        tester_id=tester_id,
    )
    db.add(session)
    db.flush()
    report = SandboxTestReport(
        session_id=session.id,
        target_type=target_type,
        target_id=target_id,
        tester_id=tester_id,
        approval_eligible=True,
        report_hash="testhash123",
    )
    db.add(report)
    db.flush()
    req = db.get(ApprovalRequest, approval_request_id)
    req.security_scan_result = {
        "sandbox_test_report_id": report.id,
        "report_hash": report.report_hash,
    }
    db.commit()
    return report


# ─── 共用 Fixture ─────────────────────────────────────────────────────────────

def _make_admin(db, client):
    dept = _make_dept(db)
    user = _make_user(db, "admin_user", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "admin_user")
    return user, _auth(token)


def _make_employee(db, client, username="emp_user"):
    dept = _make_dept(db, "员工部门")
    user = _make_user(db, username, Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, username)
    return user, _auth(token)


def _make_position(db, name="商务"):
    pos = Position(name=name, description=f"{name}岗位")
    db.add(pos)
    db.flush()
    return pos


def _make_domain(db, name="client"):
    domain = DataDomain(
        name=name,
        display_name=f"{name}数据域",
        description="测试",
        fields=[
            {"name": "client_name", "label": "客户名", "sensitive": False, "type": "string"},
            {"name": "contract_value", "label": "合同金额", "sensitive": True, "type": "number"},
            {"name": "cost", "label": "成本", "sensitive": True, "type": "number"},
        ],
    )
    db.add(domain)
    db.flush()
    return domain


# ─── TC-PERM-01  Position CRUD ────────────────────────────────────────────────

class TestPositionCRUD:
    def test_create_position(self, client, db):
        _, headers = _make_admin(db, client)
        resp = client.post("/api/admin/permissions/positions", json={
            "name": "商务", "description": "销售岗位"
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "商务"
        assert data["id"] > 0

    def test_list_positions(self, client, db):
        _, headers = _make_admin(db, client)
        client.post("/api/admin/permissions/positions", json={"name": "策划"}, headers=headers)
        client.post("/api/admin/permissions/positions", json={"name": "财务"}, headers=headers)
        resp = client.get("/api/admin/permissions/positions", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_update_position(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/permissions/positions", json={"name": "旧名"}, headers=headers)
        pid = r.json()["id"]
        resp = client.put(f"/api/admin/permissions/positions/{pid}", json={"name": "新名"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "新名"

    def test_delete_position(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/permissions/positions", json={"name": "临时岗"}, headers=headers)
        pid = r.json()["id"]
        resp = client.delete(f"/api/admin/permissions/positions/{pid}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ─── TC-PERM-02  DataDomain CRUD ─────────────────────────────────────────────

class TestDataDomainCRUD:
    def test_create_domain(self, client, db):
        _, headers = _make_admin(db, client)
        resp = client.post("/api/admin/permissions/data-domains", json={
            "name": "client",
            "display_name": "客户信息",
            "description": "客户数据",
            "fields": [{"name": "client_name", "label": "客户名", "sensitive": False, "type": "string"}],
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "client"
        assert len(data["fields"]) == 1

    def test_list_domains(self, client, db):
        _, headers = _make_admin(db, client)
        client.post("/api/admin/permissions/data-domains", json={"name": "d1", "display_name": "D1", "fields": []}, headers=headers)
        client.post("/api/admin/permissions/data-domains", json={"name": "d2", "display_name": "D2", "fields": []}, headers=headers)
        resp = client.get("/api/admin/permissions/data-domains", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_update_domain_fields(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/permissions/data-domains", json={
            "name": "fin", "display_name": "财务", "fields": []
        }, headers=headers)
        did = r.json()["id"]
        resp = client.put(f"/api/admin/permissions/data-domains/{did}", json={
            "name": "fin", "display_name": "财务数据",
            "fields": [{"name": "revenue", "label": "收入", "sensitive": True, "type": "number"}],
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "财务数据"
        assert len(resp.json()["fields"]) == 1


# ─── TC-PERM-03  DataScopePolicy CRUD ────────────────────────────────────────

class TestDataScopePolicyCRUD:
    def test_create_policy(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db)
        domain = _make_domain(db)
        db.commit()
        resp = client.post("/api/admin/permissions/policies", json={
            "target_type": "position",
            "target_position_id": pos.id,
            "resource_type": "data_domain",
            "data_domain_id": domain.id,
            "visibility_level": "own",
            "output_mask": ["cost"],
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["visibility_level"] == "own"
        assert "cost" in data["output_mask"]

    def test_filter_policies_by_position(self, client, db):
        _, headers = _make_admin(db, client)
        pos1 = _make_position(db, "A")
        pos2 = _make_position(db, "B")
        domain = _make_domain(db)
        db.commit()
        for pid in [pos1.id, pos2.id]:
            client.post("/api/admin/permissions/policies", json={
                "target_type": "position", "target_position_id": pid,
                "resource_type": "data_domain", "data_domain_id": domain.id,
                "visibility_level": "own", "output_mask": [],
            }, headers=headers)
        resp = client.get(f"/api/admin/permissions/policies?target_position_id={pos1.id}", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ─── TC-PERM-04  GlobalDataMask CRUD ─────────────────────────────────────────

class TestGlobalDataMaskCRUD:
    def test_create_global_mask(self, client, db):
        _, headers = _make_admin(db, client)
        resp = client.post("/api/admin/permissions/global-masks", json={
            "field_name": "salary_exact",
            "mask_action": "aggregate",
            "mask_params": {"aggregate_label": "部门均值"},
            "severity": 5,
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["field_name"] == "salary_exact"
        assert data["mask_action"] == "aggregate"

    def test_list_global_masks(self, client, db):
        _, headers = _make_admin(db, client)
        for fname in ["cost", "margin", "personal_id"]:
            client.post("/api/admin/permissions/global-masks", json={
                "field_name": fname, "mask_action": "hide", "mask_params": {}, "severity": 4
            }, headers=headers)
        resp = client.get("/api/admin/permissions/global-masks", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_update_global_mask(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/permissions/global-masks", json={
            "field_name": "contact_phone", "mask_action": "hide", "mask_params": {}, "severity": 3
        }, headers=headers)
        mid = r.json()["id"]
        resp = client.put(f"/api/admin/permissions/global-masks/{mid}", json={
            "field_name": "contact_phone", "mask_action": "truncate",
            "mask_params": {"length": 7}, "severity": 3,
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["mask_action"] == "truncate"


# ─── TC-PERM-05  RoleMaskOverride CRUD ───────────────────────────────────────

class TestRoleMaskOverrideCRUD:
    def test_create_role_mask(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db, "财务")
        domain = _make_domain(db)
        db.commit()
        resp = client.post("/api/admin/permissions/role-masks", json={
            "position_id": pos.id,
            "field_name": "cost",
            "data_domain_id": domain.id,
            "mask_action": "show",
            "mask_params": {},
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["mask_action"] == "show"

    def test_list_role_masks_by_position(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db)
        domain = _make_domain(db)
        db.commit()
        for fname in ["cost", "margin"]:
            client.post("/api/admin/permissions/role-masks", json={
                "position_id": pos.id, "field_name": fname,
                "mask_action": "show", "mask_params": {},
            }, headers=headers)
        resp = client.get(f"/api/admin/permissions/role-masks?position_id={pos.id}", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_delete_role_mask(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db)
        db.commit()
        r = client.post("/api/admin/permissions/role-masks", json={
            "position_id": pos.id, "field_name": "x", "mask_action": "hide", "mask_params": {}
        }, headers=headers)
        mid = r.json()["id"]
        resp = client.delete(f"/api/admin/permissions/role-masks/{mid}", headers=headers)
        assert resp.status_code == 200


# ─── TC-PERM-06  RoleOutputMask CRUD ─────────────────────────────────────────

class TestRoleOutputMaskCRUD:
    def test_create_output_mask(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db, "管理层")
        domain = _make_domain(db, "hr")
        db.commit()
        resp = client.post("/api/admin/permissions/output-masks", json={
            "position_id": pos.id,
            "data_domain_id": domain.id,
            "field_name": "salary_exact",
            "mask_action": "aggregate",
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["mask_action"] == "aggregate"

    def test_list_output_masks_by_position(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db)
        domain = _make_domain(db)
        db.commit()
        for fname in ["cost", "margin", "company_revenue"]:
            client.post("/api/admin/permissions/output-masks", json={
                "position_id": pos.id, "data_domain_id": domain.id,
                "field_name": fname, "mask_action": "hide",
            }, headers=headers)
        resp = client.get(f"/api/admin/permissions/output-masks?position_id={pos.id}", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_update_output_mask(self, client, db):
        _, headers = _make_admin(db, client)
        pos = _make_position(db)
        domain = _make_domain(db)
        db.commit()
        r = client.post("/api/admin/permissions/output-masks", json={
            "position_id": pos.id, "data_domain_id": domain.id,
            "field_name": "salary_exact", "mask_action": "hide",
        }, headers=headers)
        mid = r.json()["id"]
        resp = client.put(f"/api/admin/permissions/output-masks/{mid}", json={
            "position_id": pos.id, "data_domain_id": domain.id,
            "field_name": "salary_exact", "mask_action": "aggregate",
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["mask_action"] == "aggregate"


# ─── TC-PERM-07  MaskPreview ─────────────────────────────────────────────────

class TestMaskPreview:
    def test_preview_with_global_mask(self, client, db):
        """全局脱敏规则能在预览中生效"""
        _, headers = _make_admin(db, client)
        # 全部走 API，避免双 session 锁冲突
        pr = client.post("/api/admin/permissions/positions", json={"name": "商务_preview"}, headers=headers)
        pos_id = pr.json()["id"]
        client.post("/api/admin/permissions/global-masks", json={
            "field_name": "cost", "mask_action": "hide", "mask_params": {}, "severity": 4
        }, headers=headers)
        resp = client.post("/api/admin/permissions/mask-preview", json={
            "position_id": pos_id,
            "sample_data": [{"cost": 100000, "client_name": "测试客户"}],
        }, headers=headers)
        assert resp.status_code == 200
        masked = resp.json()["masked"]
        assert len(masked) == 1
        # cost 应该被 hide（返回 None 或从结果中移除）
        assert masked[0].get("cost") is None
        # client_name 无规则，保持原值
        assert masked[0]["client_name"] == "测试客户"

    def test_preview_with_skill_mask_override(self, client, db):
        """Skill 级脱敏覆盖优先于全局规则"""
        _, headers = _make_admin(db, client)
        # 全部走 API
        pr_pos = client.post("/api/admin/permissions/positions", json={"name": "财务_preview"}, headers=headers)
        pos_id = pr_pos.json()["id"]
        skill_user = _make_user_orig(db, "skill_creator_prev", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        # 全局：cost → hide
        client.post("/api/admin/permissions/global-masks", json={
            "field_name": "cost", "mask_action": "hide", "mask_params": {}, "severity": 4
        }, headers=headers)
        # Skill policy → Skill mask override: cost → show（财务可见）
        pr = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "org_wide", "default_data_scope": {}
        }, headers=headers)
        policy_id = pr.json()["id"]
        client.post(f"/api/admin/skill-policies/{policy_id}/masks", json=[{
            "position_id": pos_id, "field_name": "cost", "mask_action": "show", "mask_params": {}
        }], headers=headers)
        resp = client.post("/api/admin/permissions/mask-preview", json={
            "position_id": pos_id,
            "skill_id": skill.id,
            "sample_data": [{"cost": 50000}],
        }, headers=headers)
        assert resp.status_code == 200
        masked = resp.json()["masked"]
        # Skill 级 show 覆盖全局 hide，财务可见 cost
        assert masked[0]["cost"] == 50000


# ─── TC-PERM-08  Skill Policy CRUD ───────────────────────────────────────────

class TestSkillPolicyCRUD:
    def test_create_skill_policy(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        resp = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id,
            "publish_scope": "same_role",
            "default_data_scope": {"visibility": "own"},
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_id"] == skill.id
        assert data["publish_scope"] == "same_role"

    def test_duplicate_policy_rejected(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        resp = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "org_wide", "default_data_scope": {}
        }, headers=headers)
        assert resp.status_code == 400

    def test_update_skill_policy(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        pid = r.json()["id"]
        resp = client.put(f"/api/admin/skill-policies/{pid}", json={
            "publish_scope": "org_wide"
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["publish_scope"] == "org_wide"


# ─── TC-PERM-09  RolePolicyOverride ─────────────────────────────────────────

class TestRolePolicyOverride:
    def test_create_override(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator4", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        pos = _make_position(db, "管理层")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        resp = client.post(f"/api/admin/skill-policies/{policy_id}/overrides", json={
            "position_id": pos.id,
            "callable": True,
            "data_scope": {"visibility": "all"},
            "output_mask": [],
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["callable"] is True
        assert data["data_scope"]["visibility"] == "all"

    def test_override_upsert(self, client, db):
        """同一 policy × position 再 POST 应 upsert 而非报错"""
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator5", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        pos = _make_position(db, "策划")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        client.post(f"/api/admin/skill-policies/{policy_id}/overrides", json={
            "position_id": pos.id, "callable": True, "data_scope": {}, "output_mask": []
        }, headers=headers)
        resp = client.post(f"/api/admin/skill-policies/{policy_id}/overrides", json={
            "position_id": pos.id, "callable": False, "data_scope": {}, "output_mask": []
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["callable"] is False

    def test_delete_override(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "creator6", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        pos = _make_position(db, "HR")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        o = client.post(f"/api/admin/skill-policies/{policy_id}/overrides", json={
            "position_id": pos.id, "callable": True, "data_scope": {}, "output_mask": []
        }, headers=headers)
        oid = o.json()["id"]
        resp = client.delete(f"/api/admin/skill-policies/{policy_id}/overrides/{oid}", headers=headers)
        assert resp.status_code == 200


# ─── TC-PERM-10  SkillMaskOverride ───────────────────────────────────────────

class TestSkillMaskOverride:
    def _setup(self, client, db, headers):
        skill_user = _make_user(db, "smask_creator", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        return skill, r.json()["id"]

    def test_set_skill_masks(self, client, db):
        _, headers = _make_admin(db, client)
        skill, policy_id = self._setup(client, db, headers)
        pos = _make_position(db, "财务")
        db.commit()
        resp = client.post(f"/api/admin/skill-policies/{policy_id}/masks", json=[
            {"position_id": pos.id, "field_name": "cost", "mask_action": "show", "mask_params": {}},
            {"position_id": pos.id, "field_name": "margin", "mask_action": "show", "mask_params": {}},
        ], headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_skill_mask_upsert(self, client, db):
        _, headers = _make_admin(db, client)
        skill, policy_id = self._setup(client, db, headers)
        pos = _make_position(db, "财务2")
        db.commit()
        client.post(f"/api/admin/skill-policies/{policy_id}/masks", json=[
            {"position_id": pos.id, "field_name": "cost", "mask_action": "hide", "mask_params": {}}
        ], headers=headers)
        resp = client.post(f"/api/admin/skill-policies/{policy_id}/masks", json=[
            {"position_id": pos.id, "field_name": "cost", "mask_action": "show", "mask_params": {}}
        ], headers=headers)
        assert resp.status_code == 200
        assert resp.json()[0]["mask_action"] == "show"


# ─── TC-PERM-11  AgentConnection ─────────────────────────────────────────────

class TestAgentConnection:
    def test_add_downstream_connection(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "conn_creator", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游Skill")
        skill_b = _make_skill(db, skill_user.id, name="下游Skill")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill_a.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        resp = client.post(f"/api/admin/skill-policies/{policy_id}/connections", json={
            "direction": "downstream",
            "connected_skill_id": skill_b.id,
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["direction"] == "downstream"

    def test_duplicate_connection_idempotent(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "conn_creator2", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游2")
        skill_b = _make_skill(db, skill_user.id, name="下游2")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill_a.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        c1 = client.post(f"/api/admin/skill-policies/{policy_id}/connections", json={
            "direction": "downstream", "connected_skill_id": skill_b.id
        }, headers=headers)
        c2 = client.post(f"/api/admin/skill-policies/{policy_id}/connections", json={
            "direction": "downstream", "connected_skill_id": skill_b.id
        }, headers=headers)
        assert c1.json()["id"] == c2.json()["id"]

    def test_delete_connection(self, client, db):
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "conn_creator3", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游3")
        skill_b = _make_skill(db, skill_user.id, name="下游3")
        db.commit()
        r = client.post("/api/admin/skill-policies", json={
            "skill_id": skill_a.id, "publish_scope": "same_role", "default_data_scope": {}
        }, headers=headers)
        policy_id = r.json()["id"]
        c = client.post(f"/api/admin/skill-policies/{policy_id}/connections", json={
            "direction": "downstream", "connected_skill_id": skill_b.id
        }, headers=headers)
        cid = c.json()["id"]
        resp = client.delete(f"/api/admin/skill-policies/{policy_id}/connections/{cid}", headers=headers)
        assert resp.status_code == 200


# ─── TC-PERM-12  审批流 ───────────────────────────────────────────────────────

class TestApprovalFlow:
    def test_create_approval_request(self, client, db):
        emp, emp_headers = _make_employee(db, client)
        resp = client.post("/api/approvals", json={
            "request_type": "skill_publish",
            "target_id": 1,
            "target_type": "skill",
        }, headers=emp_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["request_type"] == "skill_publish"

    def test_admin_approve(self, client, db):
        emp, emp_headers = _make_employee(db, client, "emp2")
        admin, admin_headers = _make_admin(db, client)
        r = client.post("/api/approvals", json={
            "request_type": "skill_publish", "target_id": 1, "target_type": "skill"
        }, headers=emp_headers)
        rid = r.json()["id"]
        _attach_sandbox_report(db, rid, admin.id, target_type="skill", target_id=1)
        resp = client.post(f"/api/approvals/{rid}/actions", json={
            "action": "approve", "comment": "通过"
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_admin_reject(self, client, db):
        emp, emp_headers = _make_employee(db, client, "emp3")
        admin, admin_headers = _make_admin(db, client)
        r = client.post("/api/approvals", json={
            "request_type": "scope_change", "target_id": 2, "target_type": "policy"
        }, headers=emp_headers)
        rid = r.json()["id"]
        resp = client.post(f"/api/approvals/{rid}/actions", json={
            "action": "reject", "comment": "不符合规定"
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_add_conditions(self, client, db):
        emp, emp_headers = _make_employee(db, client, "emp4")
        admin, admin_headers = _make_admin(db, client)
        r = client.post("/api/approvals", json={
            "request_type": "skill_publish", "target_id": 3, "target_type": "skill"
        }, headers=emp_headers)
        rid = r.json()["id"]
        resp = client.post(f"/api/approvals/{rid}/actions", json={
            "action": "add_conditions",
            "comment": "需脱敏",
            "conditions": ["不得包含合同金额"],
        }, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "conditions"
        assert "不得包含合同金额" in data["conditions"]

    def test_employee_sees_only_own_approvals(self, client, db):
        emp1, emp1_h = _make_employee(db, client, "emp5")
        emp2, emp2_h = _make_employee(db, client, "emp6")
        client.post("/api/approvals", json={"request_type": "skill_publish"}, headers=emp1_h)
        client.post("/api/approvals", json={"request_type": "skill_publish"}, headers=emp1_h)
        # emp2 只发了1个
        client.post("/api/approvals", json={"request_type": "skill_publish"}, headers=emp2_h)
        resp = client.get("/api/approvals", headers=emp1_h)
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_cannot_act_on_closed_approval(self, client, db):
        emp, emp_headers = _make_employee(db, client, "emp7")
        admin, admin_headers = _make_admin(db, client)
        r = client.post("/api/approvals", json={"request_type": "skill_publish"}, headers=emp_headers)
        rid = r.json()["id"]
        _attach_sandbox_report(db, rid, admin.id, target_type="skill", target_id=1)
        client.post(f"/api/approvals/{rid}/actions", json={"action": "approve"}, headers=admin_headers)
        resp = client.post(f"/api/approvals/{rid}/actions", json={"action": "reject"}, headers=admin_headers)
        assert resp.status_code == 400


# ─── TC-PERM-13  Output Schema ────────────────────────────────────────────────

class TestOutputSchema:
    def test_list_schemas_empty(self, client, db):
        _, headers = _make_admin(db, client)
        resp = client.get("/api/admin/output-schemas", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_approve_schema(self, client, db):
        from app.models.permission import SkillOutputSchema, SchemaStatus
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "schema_creator", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 直接写入一个 pending_review schema
        schema = SkillOutputSchema(
            skill_id=skill.id,
            version=1,
            status=SchemaStatus.PENDING_REVIEW,
            schema_json={"fields": ["result", "summary"]},
            created_by=skill_user.id,
        )
        db.add(schema)
        db.commit()

        resp = client.post(f"/api/admin/output-schemas/{schema.id}/approve", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_update_schema_before_approval(self, client, db):
        from app.models.permission import SkillOutputSchema, SchemaStatus
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "schema_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        schema = SkillOutputSchema(
            skill_id=skill.id, version=1, status=SchemaStatus.DRAFT,
            schema_json={"fields": ["a"]}, created_by=skill_user.id,
        )
        db.add(schema)
        db.commit()
        resp = client.put(f"/api/admin/output-schemas/{schema.id}", json={
            "schema_json": {"fields": ["a", "b"]}
        }, headers=headers)
        assert resp.status_code == 200
        assert "b" in resp.json()["schema_json"]["fields"]

    def test_cannot_edit_approved_schema(self, client, db):
        from app.models.permission import SkillOutputSchema, SchemaStatus
        _, headers = _make_admin(db, client)
        skill_user = _make_user(db, "schema_creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        schema = SkillOutputSchema(
            skill_id=skill.id, version=1, status=SchemaStatus.APPROVED,
            schema_json={"fields": ["x"]}, created_by=skill_user.id,
        )
        db.add(schema)
        db.commit()
        resp = client.put(f"/api/admin/output-schemas/{schema.id}", json={
            "schema_json": {"fields": ["x", "y"]}
        }, headers=headers)
        assert resp.status_code == 400


# ─── TC-PERM-14  Handoff 模板与缓存 ──────────────────────────────────────────

class TestHandoffTemplates:
    def test_create_template(self, client, db):
        _, headers = _make_admin(db, client)
        resp = client.post("/api/admin/handoff/templates", json={
            "name": "T1 测试模板",
            "template_type": "standard",
            "schema_fields": ["client_name", "budget_range"],
            "excluded_fields": ["contract_value"],
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "client_name" in data["schema_fields"]
        assert "contract_value" in data["excluded_fields"]

    def test_list_templates(self, client, db):
        _, headers = _make_admin(db, client)
        for i in range(3):
            client.post("/api/admin/handoff/templates", json={
                "name": f"模板{i}", "template_type": "standard",
                "schema_fields": [], "excluded_fields": [],
            }, headers=headers)
        resp = client.get("/api/admin/handoff/templates", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_update_template(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/handoff/templates", json={
            "name": "旧模板", "template_type": "standard",
            "schema_fields": ["a"], "excluded_fields": [],
        }, headers=headers)
        tid = r.json()["id"]
        resp = client.put(f"/api/admin/handoff/templates/{tid}", json={
            "name": "新模板", "template_type": "l3_mask",
            "schema_fields": ["a", "b"], "excluded_fields": ["c"],
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "新模板"
        assert resp.json()["template_type"] == "l3_mask"

    def test_delete_template(self, client, db):
        _, headers = _make_admin(db, client)
        r = client.post("/api/admin/handoff/templates", json={
            "name": "删除模板", "template_type": "standard",
            "schema_fields": [], "excluded_fields": [],
        }, headers=headers)
        tid = r.json()["id"]
        resp = client.delete(f"/api/admin/handoff/templates/{tid}", headers=headers)
        assert resp.status_code == 200

    def test_promote_cache_to_template(self, client, db):
        _, headers = _make_admin(db, client)
        cache = HandoffSchemaCache(
            cache_key="test_key_promote",
            schema_json={"fields": ["client_name", "budget_range"]},
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=7),
        )
        db.add(cache)
        db.commit()
        resp = client.post(f"/api/admin/handoff/caches/{cache.id}/promote?name=提升模板", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        tmpl = resp.json()["template"]
        assert tmpl["name"] == "提升模板"


# ─── TC-PERM-15  PermissionEngine — check_skill_callable ─────────────────────

class TestPermissionEngineCallable:
    def test_super_admin_always_callable(self, db):
        engine = PermissionEngine()
        pos = _make_position(db)
        skill_user = _make_user(db, "pe_creator1", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 创建 policy 并明确标记该岗位 callable=False
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=False, data_scope={}, output_mask=[],
        )
        db.add(override)
        db.commit()

        admin_user = _make_user(db, "pe_admin1", Role.SUPER_ADMIN, position_id=pos.id)
        db.commit()
        assert engine.check_skill_callable(admin_user, skill.id, db) is True

    def test_position_callable_false(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "受限岗位")
        skill_user = _make_user(db, "pe_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=False, data_scope={}, output_mask=[],
        )
        db.add(override)
        db.commit()

        emp = _make_user(db, "pe_emp1", Role.EMPLOYEE, position_id=pos.id)
        db.commit()
        assert engine.check_skill_callable(emp, skill.id, db) is False

    def test_no_policy_defaults_callable(self, db):
        engine = PermissionEngine()
        skill_user = _make_user(db, "pe_creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        emp = _make_user(db, "pe_emp2", Role.EMPLOYEE)
        db.commit()
        # 没有 policy → 宽松策略 → 允许
        assert engine.check_skill_callable(emp, skill.id, db) is True


def _make_user(db, username, role, dept_id=None, position_id=None, password="Test1234!"):
    from app.services.auth_service import hash_password as _hp
    u = _make_user_orig(db, username, role, dept_id, password)
    if position_id:
        u.position_id = position_id
        db.flush()
    return u


# 保留原始引用
from tests.conftest import _make_user as _make_user_orig


# ─── TC-PERM-16  PermissionEngine — get_data_scope ───────────────────────────

class TestPermissionEngineDataScope:
    def test_returns_override_scope_for_position(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "商务_scope")
        skill_user = _make_user_orig(db, "scope_creator", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role",
                             default_data_scope={"visibility": "own"})
        db.add(policy)
        db.flush()
        override = RolePolicyOverride(
            skill_policy_id=policy.id, position_id=pos.id,
            callable=True,
            data_scope={"visibility": "all", "fields": ["client_name"]},
            output_mask=[],
        )
        db.add(override)
        db.commit()

        emp = _make_user_orig(db, "scope_emp", Role.EMPLOYEE)
        emp.position_id = pos.id
        db.commit()
        scope = engine.get_data_scope(emp, skill.id, db)
        assert scope["visibility"] == "all"

    def test_returns_default_scope_when_no_override(self, db):
        engine = PermissionEngine()
        skill_user = _make_user_orig(db, "scope_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        policy = SkillPolicy(skill_id=skill.id, publish_scope="same_role",
                             default_data_scope={"visibility": "dept"})
        db.add(policy)
        db.commit()

        emp = _make_user_orig(db, "scope_emp2", Role.EMPLOYEE)
        db.commit()
        scope = engine.get_data_scope(emp, skill.id, db)
        assert scope["visibility"] == "dept"


# ─── TC-PERM-17  PermissionEngine — merge_mask_rules ─────────────────────────

class TestMergeMaskRules:
    def test_global_rule_applied(self, db):
        engine = PermissionEngine()
        emp = _make_user_orig(db, "merge_emp1", Role.EMPLOYEE)
        skill_user = _make_user_orig(db, "merge_creator1", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 写全局规则
        gmask = GlobalDataMask(field_name="personal_id", mask_action=MaskAction.HIDE, severity=5)
        db.add(gmask)
        db.commit()

        action, params = engine.merge_mask_rules(emp, skill.id, "personal_id", None, db)
        assert action == MaskAction.HIDE

    def test_role_overrides_global(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "财务_merge")
        emp = _make_user_orig(db, "merge_emp2", Role.EMPLOYEE)
        emp.position_id = pos.id
        skill_user = _make_user_orig(db, "merge_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 全局：cost → hide
        gmask = GlobalDataMask(field_name="cost", mask_action=MaskAction.HIDE, severity=4)
        db.add(gmask)
        # 角色覆盖：财务 cost → show
        rmask = RoleMaskOverride(position_id=pos.id, field_name="cost", mask_action=MaskAction.SHOW)
        db.add(rmask)
        db.commit()

        action, _ = engine.merge_mask_rules(emp, skill.id, "cost", None, db)
        assert action == MaskAction.SHOW

    def test_skill_overrides_role_and_global(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "策划_merge")
        emp = _make_user_orig(db, "merge_emp3", Role.EMPLOYEE)
        emp.position_id = pos.id
        skill_user = _make_user_orig(db, "merge_creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 全局：margin → hide
        gmask = GlobalDataMask(field_name="margin", mask_action=MaskAction.HIDE, severity=4)
        db.add(gmask)
        # 角色：margin → range
        rmask = RoleMaskOverride(position_id=pos.id, field_name="margin", mask_action=MaskAction.RANGE)
        db.add(rmask)
        # Skill级：margin → show（最高优先）
        smask = SkillMaskOverride(skill_id=skill.id, position_id=pos.id,
                                  field_name="margin", mask_action=MaskAction.SHOW)
        db.add(smask)
        db.commit()

        action, _ = engine.merge_mask_rules(emp, skill.id, "margin", None, db)
        assert action == MaskAction.SHOW

    def test_no_rule_returns_keep(self, db):
        engine = PermissionEngine()
        emp = _make_user_orig(db, "merge_emp4", Role.EMPLOYEE)
        skill_user = _make_user_orig(db, "merge_creator4", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.commit()
        action, _ = engine.merge_mask_rules(emp, skill.id, "unknown_field", None, db)
        assert action == MaskAction.KEEP


# ─── TC-PERM-18  PermissionEngine — apply_data_masks ─────────────────────────

class TestApplyDataMasks:
    def test_hide_action_removes_value(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "商务_mask")
        emp = _make_user_orig(db, "mask_emp1", Role.EMPLOYEE)
        emp.position_id = pos.id
        skill_user = _make_user_orig(db, "mask_creator1", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        gmask = GlobalDataMask(field_name="cost", mask_action=MaskAction.HIDE, severity=4)
        db.add(gmask)
        db.commit()

        data = [{"cost": 500000, "client_name": "测试客户"}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert result[0]["client_name"] == "测试客户"
        assert result[0].get("cost") is None

    def test_range_action_converts_to_interval(self, db):
        engine = PermissionEngine()
        emp = _make_user_orig(db, "mask_emp2", Role.EMPLOYEE)
        skill_user = _make_user_orig(db, "mask_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        gmask = GlobalDataMask(
            field_name="budget_exact", mask_action=MaskAction.RANGE,
            mask_params={"step": 100000}, severity=3,
        )
        db.add(gmask)
        db.commit()

        data = [{"budget_exact": 350000}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert result[0]["budget_exact"] == "300000-400000"

    def test_truncate_action(self, db):
        engine = PermissionEngine()
        emp = _make_user_orig(db, "mask_emp3", Role.EMPLOYEE)
        skill_user = _make_user_orig(db, "mask_creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        gmask = GlobalDataMask(
            field_name="contact_phone", mask_action=MaskAction.TRUNCATE,
            mask_params={"length": 7}, severity=3,
        )
        db.add(gmask)
        db.commit()

        data = [{"contact_phone": "13800001234"}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert result[0]["contact_phone"].startswith("1380000")
        assert "..." in result[0]["contact_phone"]

    def test_super_admin_bypasses_masks(self, db):
        engine = PermissionEngine()
        admin = _make_user_orig(db, "mask_admin1", Role.SUPER_ADMIN)
        skill_user = _make_user_orig(db, "mask_creator4", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        gmask = GlobalDataMask(field_name="cost", mask_action=MaskAction.HIDE, severity=4)
        db.add(gmask)
        db.commit()

        data = [{"cost": 999999}]
        result = engine.apply_data_masks(admin, skill.id, data, None, db)
        assert result[0]["cost"] == 999999

    def test_remove_action_drops_field(self, db):
        engine = PermissionEngine()
        emp = _make_user_orig(db, "mask_emp4", Role.EMPLOYEE)
        skill_user = _make_user_orig(db, "mask_creator5", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        gmask = GlobalDataMask(field_name="personal_id", mask_action=MaskAction.REMOVE, severity=5)
        db.add(gmask)
        db.commit()

        data = [{"personal_id": "310000199001011234", "name": "张三"}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert "personal_id" not in result[0]
        assert result[0]["name"] == "张三"


# ─── TC-PERM-19  PermissionEngine — apply_output_masks ───────────────────────

class TestApplyOutputMasks:
    def test_output_mask_hides_field(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "商务_output")
        emp = _make_user_orig(db, "out_emp1", Role.EMPLOYEE)
        emp.position_id = pos.id
        domain = _make_domain(db, "financial_out")
        mask = RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="cost", mask_action=MaskAction.HIDE,
        )
        db.add(mask)
        db.commit()

        data = {"cost": 100000, "contract_value": 500000}
        result = engine.apply_output_masks(emp, data, domain.id, db)
        assert "cost" not in result
        assert result["contract_value"] == 500000

    def test_output_mask_aggregate(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "管理层_output")
        emp = _make_user_orig(db, "out_emp2", Role.EMPLOYEE)
        emp.position_id = pos.id
        domain = _make_domain(db, "hr_out")
        mask = RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.AGGREGATE,
        )
        db.add(mask)
        db.commit()

        data = {"salary_exact": 30000, "department": "电商部"}
        result = engine.apply_output_masks(emp, data, domain.id, db)
        assert result["salary_exact"] == "统计值"
        assert result["department"] == "电商部"

    def test_super_admin_bypasses_output_masks(self, db):
        engine = PermissionEngine()
        pos = _make_position(db, "admin_output")
        admin = _make_user_orig(db, "out_admin1", Role.SUPER_ADMIN)
        admin.position_id = pos.id
        domain = _make_domain(db, "admin_domain")
        mask = RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.HIDE,
        )
        db.add(mask)
        db.commit()

        data = {"salary_exact": 50000}
        result = engine.apply_output_masks(admin, data, domain.id, db)
        assert result["salary_exact"] == 50000


# ─── TC-PERM-20  HandoffEngine — 白名单校验 ──────────────────────────────────

class TestHandoffValidation:
    def test_connection_in_whitelist_allowed(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator1", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游A")
        skill_b = _make_skill(db, skill_user.id, name="下游B")
        policy = SkillPolicy(skill_id=skill_a.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.flush()
        conn = SkillAgentConnection(
            skill_policy_id=policy.id, direction="downstream", connected_skill_id=skill_b.id
        )
        db.add(conn)
        db.commit()
        assert engine.validate_connection(skill_a.id, skill_b.id, db) is True

    def test_connection_not_in_whitelist_blocked(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator2", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游C")
        skill_c = _make_skill(db, skill_user.id, name="未授权下游")
        policy = SkillPolicy(skill_id=skill_a.id, publish_scope="same_role", default_data_scope={})
        db.add(policy)
        db.commit()
        # 没有连接记录
        assert engine.validate_connection(skill_a.id, skill_c.id, db) is False

    def test_no_policy_defaults_denied(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator3", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="无policy上游")
        skill_b = _make_skill(db, skill_user.id, name="任意下游")
        db.commit()
        # H10: deny-by-default — 上游无 policy → 禁止 handoff
        assert engine.validate_connection(skill_a.id, skill_b.id, db) is False


# ─── TC-PERM-21  HandoffEngine — Schema 解析 ─────────────────────────────────

class TestHandoffSchemaResolution:
    def test_static_template_takes_priority(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator4", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="上游模板A")
        skill_b = _make_skill(db, skill_user.id, name="下游模板B")
        tmpl = HandoffTemplate(
            name="测试模板",
            upstream_skill_id=skill_a.id,
            downstream_skill_id=skill_b.id,
            template_type=HandoffTemplateType.STANDARD,
            schema_fields=["client_name", "budget_range"],
            excluded_fields=["cost"],
        )
        db.add(tmpl)
        db.commit()

        result = engine.resolve_schema(skill_a.id, skill_b.id, None, db)
        assert result["source"] == "template"
        assert result["template_id"] == tmpl.id
        assert "client_name" in result["schema_fields"]

    def test_cache_used_when_no_template(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator5", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="缓存上游")
        skill_b = _make_skill(db, skill_user.id, name="缓存下游")
        cache_key = engine._make_cache_key(skill_a.id, skill_b.id, None)
        cache = HandoffSchemaCache(
            cache_key=cache_key,
            upstream_skill_id=skill_a.id,
            downstream_skill_id=skill_b.id,
            schema_json={"fields": ["project_name"]},
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=7),
        )
        db.add(cache)
        db.commit()

        result = engine.resolve_schema(skill_a.id, skill_b.id, None, db)
        assert result["source"] == "cache"
        assert result["cache_id"] == cache.id

    def test_dynamic_schema_returned_when_nothing_found(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator6", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="动态上游")
        skill_b = _make_skill(db, skill_user.id, name="动态下游")
        db.commit()

        result = engine.resolve_schema(skill_a.id, skill_b.id, None, db)
        assert result["source"] == "dynamic"
        assert result["schema"] == {}

    def test_cache_hit_count_increments(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator7", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="命中上游")
        skill_b = _make_skill(db, skill_user.id, name="命中下游")
        cache_key = engine._make_cache_key(skill_a.id, skill_b.id, None)
        cache = HandoffSchemaCache(
            cache_key=cache_key,
            schema_json={"fields": ["x"]},
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=7),
            hit_count=2,
        )
        db.add(cache)
        db.commit()

        engine.resolve_schema(skill_a.id, skill_b.id, None, db)
        db.refresh(cache)
        assert cache.hit_count == 3

    def test_expired_cache_ignored(self, db):
        engine = HandoffEngine()
        skill_user = _make_user_orig(db, "hoff_creator8", Role.SUPER_ADMIN)
        skill_a = _make_skill(db, skill_user.id, name="过期上游")
        skill_b = _make_skill(db, skill_user.id, name="过期下游")
        cache_key = engine._make_cache_key(skill_a.id, skill_b.id, None)
        expired_cache = HandoffSchemaCache(
            cache_key=cache_key,
            schema_json={"fields": ["stale"]},
            expires_at=datetime.datetime.utcnow() - datetime.timedelta(days=1),
        )
        db.add(expired_cache)
        db.commit()

        result = engine.resolve_schema(skill_a.id, skill_b.id, None, db)
        assert result["source"] == "dynamic"


# ─── TC-PERM-22  HandoffEngine — Payload 提取与合规校验 ──────────────────────

class TestHandoffPayload:
    def test_extract_payload_by_schema_fields(self):
        engine = HandoffEngine()
        agent_output = {
            "client_name": "测试客户",
            "budget_range": "100-200万",
            "cost": 80000,       # 不在 schema_fields 中，应被排除
            "analysis": "详细分析文本",  # analysis 不传
        }
        schema = {
            "schema_fields": ["client_name", "budget_range"],
            "excluded_fields": [],
        }
        payload = engine.extract_payload(agent_output, schema)
        assert payload["client_name"] == "测试客户"
        assert payload["budget_range"] == "100-200万"
        assert "cost" not in payload
        assert "analysis" not in payload

    def test_extract_payload_excluded_fields(self):
        engine = HandoffEngine()
        agent_output = {
            "client_name": "A客户",
            "contract_value": 500000,
            "budget_range": "200-300万",
        }
        schema = {
            "schema_fields": ["client_name", "contract_value", "budget_range"],
            "excluded_fields": ["contract_value"],
        }
        payload = engine.extract_payload(agent_output, schema)
        assert "contract_value" not in payload
        assert payload["client_name"] == "A客户"

    def test_extract_payload_with_field_mapping(self):
        engine = HandoffEngine()
        agent_output = {"rev": 100000, "name": "品牌A"}
        schema = {
            "schema_fields": ["revenue", "name"],
            "excluded_fields": [],
            "mapping": {"rev": "revenue"},
        }
        payload = engine.extract_payload(agent_output, schema)
        assert payload["revenue"] == 100000
        assert "rev" not in payload

    def test_validate_payload_required_fields(self):
        engine = HandoffEngine()
        payload = {"client_name": "A", "budget_range": "100-200万"}
        policy = {"required_fields": ["client_name", "budget_range"], "forbidden_fields": []}
        passed, diagnostics = engine.validate_payload(payload, policy)
        assert passed is True
        assert diagnostics == []

    def test_validate_payload_missing_required(self):
        engine = HandoffEngine()
        payload = {"client_name": "A"}
        policy = {"required_fields": ["client_name", "budget_range"], "forbidden_fields": []}
        passed, diagnostics = engine.validate_payload(payload, policy)
        assert passed is False
        assert any("budget_range" in d for d in diagnostics)

    def test_validate_payload_forbidden_field(self):
        engine = HandoffEngine()
        payload = {"client_name": "A", "cost": 80000}  # cost 是禁止字段
        policy = {"required_fields": [], "forbidden_fields": ["cost"]}
        passed, diagnostics = engine.validate_payload(payload, policy)
        assert passed is False
        assert any("cost" in d for d in diagnostics)


# ─── TC-PERM-23~26  跨角色边界场景 ───────────────────────────────────────────

class TestCrossRoleBoundaries:
    """模拟设计文档中定义的真实跨角色数据隔离场景"""

    def test_sales_cannot_see_cost_and_margin(self, db):
        """TC-PERM-23：商务无法看到成本和利润率"""
        engine = PermissionEngine()
        pos = _make_position(db, "商务_boundary")
        emp = _make_user_orig(db, "sales_boundary", Role.EMPLOYEE)
        emp.position_id = pos.id
        skill_user = _make_user_orig(db, "boundary_creator1", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 全局规则：cost/margin → hide
        db.add(GlobalDataMask(field_name="cost", mask_action=MaskAction.HIDE, severity=4))
        db.add(GlobalDataMask(field_name="margin", mask_action=MaskAction.HIDE, severity=4))
        db.commit()

        data = [{"client_name": "品牌A", "contract_value": 500000, "cost": 300000, "margin": 0.4}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert result[0]["client_name"] == "品牌A"
        assert result[0].get("cost") is None
        assert result[0].get("margin") is None
        assert result[0]["contract_value"] == 500000  # 商务可见合同金额

    def test_planner_sees_only_budget_range(self, db):
        """TC-PERM-24：策划只能看到预算区间，不能看精确预算"""
        engine = PermissionEngine()
        pos = _make_position(db, "策划_boundary")
        emp = _make_user_orig(db, "planner_boundary", Role.EMPLOYEE)
        emp.position_id = pos.id
        skill_user = _make_user_orig(db, "boundary_creator2", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        db.add(GlobalDataMask(
            field_name="budget_exact", mask_action=MaskAction.RANGE,
            mask_params={"step": 100000}, severity=4,
        ))
        db.commit()

        data = [{"budget_exact": 350000, "budget_range": "300-400万", "campaign_title": "秋促"}]
        result = engine.apply_data_masks(emp, skill.id, data, None, db)
        assert result[0]["budget_exact"] == "300000-400000"
        assert result[0]["budget_range"] == "300-400万"

    def test_hr_cannot_see_financial_data(self, db):
        """TC-PERM-25：HR 无法看到财务字段"""
        engine = PermissionEngine()
        pos = _make_position(db, "HR_boundary")
        emp = _make_user_orig(db, "hr_boundary", Role.EMPLOYEE)
        emp.position_id = pos.id
        domain = _make_domain(db, "financial_hr")
        skill_user = _make_user_orig(db, "boundary_creator3", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        # 输出遮罩：HR 看不到 contract_value
        mask = RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="contract_value", mask_action=MaskAction.HIDE,
        )
        db.add(mask)
        db.commit()

        data = {"contract_value": 500000, "headcount": 10}
        result = engine.apply_output_masks(emp, data, domain.id, db)
        assert "contract_value" not in result
        assert result["headcount"] == 10

    def test_management_salary_aggregated(self, db):
        """TC-PERM-26：管理层看薪资时应聚合而非精确值"""
        engine = PermissionEngine()
        pos = _make_position(db, "管理层_boundary")
        emp = _make_user_orig(db, "mgmt_boundary", Role.EMPLOYEE)
        emp.position_id = pos.id
        domain = _make_domain(db, "hr_mgmt")
        mask = RoleOutputMask(
            position_id=pos.id, data_domain_id=domain.id,
            field_name="salary_exact", mask_action=MaskAction.AGGREGATE,
        )
        db.add(mask)
        db.commit()

        data = {"salary_exact": 50000, "department": "电商部", "headcount": 20}
        result = engine.apply_output_masks(emp, data, domain.id, db)
        assert result["salary_exact"] == "统计值"
        assert result["department"] == "电商部"
        assert result["headcount"] == 20


# ─── TC-PERM-27  用户岗位绑定 API ────────────────────────────────────────────

class TestUserPositionBinding:
    def test_bind_position_to_user(self, client, db):
        _, headers = _make_admin(db, client)
        dept = _make_dept(db, "业务部门")
        emp_user = _make_user_orig(db, "bind_emp", Role.EMPLOYEE, dept.id)
        pos = _make_position(db, "商务_bind")
        db.commit()

        resp = client.put(f"/api/admin/permissions/users/{emp_user.id}", json={
            "position_id": pos.id
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # 验证绑定生效
        db.expire(emp_user)
        db.refresh(emp_user)
        assert emp_user.position_id == pos.id

    def test_unbind_position(self, client, db):
        _, headers = _make_admin(db, client)
        dept = _make_dept(db, "业务部门2")
        pos = _make_position(db, "策划_bind")
        emp_user = _make_user_orig(db, "bind_emp2", Role.EMPLOYEE, dept.id)
        emp_user.position_id = pos.id
        db.commit()

        resp = client.put(f"/api/admin/permissions/users/{emp_user.id}", json={
            "position_id": None
        }, headers=headers)
        assert resp.status_code == 200
        db.expire(emp_user)
        db.refresh(emp_user)
        assert emp_user.position_id is None


# ─── TC-PERM-28  非管理员无法访问权限 API ────────────────────────────────────

class TestPermissionAPIAccessControl:
    def test_employee_cannot_access_positions(self, client, db):
        _, emp_headers = _make_employee(db, client, "access_emp1")
        resp = client.get("/api/admin/permissions/positions", headers=emp_headers)
        assert resp.status_code in (403, 401)

    def test_employee_cannot_create_global_mask(self, client, db):
        _, emp_headers = _make_employee(db, client, "access_emp2")
        resp = client.post("/api/admin/permissions/global-masks", json={
            "field_name": "hack", "mask_action": "show", "mask_params": {}, "severity": 1
        }, headers=emp_headers)
        assert resp.status_code in (403, 401)

    def test_employee_cannot_approve_schema(self, client, db):
        from app.models.permission import SkillOutputSchema, SchemaStatus
        _, admin_headers = _make_admin(db, client)
        _, emp_headers = _make_employee(db, client, "access_emp3")
        skill_user = _make_user_orig(db, "access_creator", Role.SUPER_ADMIN)
        skill = _make_skill(db, skill_user.id)
        schema = SkillOutputSchema(
            skill_id=skill.id, version=1, status=SchemaStatus.PENDING_REVIEW,
            schema_json={}, created_by=skill_user.id,
        )
        db.add(schema)
        db.commit()
        resp = client.post(f"/api/admin/output-schemas/{schema.id}/approve", headers=emp_headers)
        assert resp.status_code in (403, 401)

    def test_unauthenticated_blocked(self, client, db):
        resp = client.get("/api/admin/permissions/positions")
        assert resp.status_code in (403, 401)
