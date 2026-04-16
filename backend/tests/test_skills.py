"""TC-SKILL: Skill CRUD, versioning, status, role enforcement."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_skill, _make_model_config, _login, _auth
from app.models.permission import ApprovalRequest
from app.models.user import Role
from app.models.skill import SkillStatus, SkillExecutionLog, SkillAuditResult, SkillFolderAlias, ArchitectWorkflowState, StagedEdit
from app.models.skill_knowledge_ref import SkillKnowledgeReference


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_skill_as_admin(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "admin1")

    resp = client.post("/api/skills", headers=_auth(token), json={
        "name": "营销分析",
        "description": "营销数据分析助手",
        "mode": "hybrid",
        "system_prompt": "你是营销分析助手。",
        "variables": [],
        "knowledge_tags": ["营销"],
        "auto_inject": True,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "营销分析"


def test_create_skill_as_employee(client, db):
    _make_dept(db)
    _make_user(db, "emp1", Role.EMPLOYEE)
    db.commit()
    token = _login(client, "emp1")

    resp = client.post("/api/skills", headers=_auth(token), json={
        "name": "违规Skill",
        "system_prompt": "x",
    })
    assert resp.status_code == 200


def test_create_skill_duplicate_name(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "admin2")

    body = {"name": "重复Skill", "system_prompt": "x", "variables": []}
    client.post("/api/skills", headers=_auth(token), json=body)
    resp = client.post("/api/skills", headers=_auth(token), json=body)
    assert resp.status_code == 400


# ── Read ──────────────────────────────────────────────────────────────────────

def test_list_skills(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin3", Role.SUPER_ADMIN, dept.id)
    _make_skill(db, admin.id, "Skill-A")
    _make_skill(db, admin.id, "Skill-B")
    db.commit()
    token = _login(client, "admin3")

    resp = client.get("/api/skills", headers=_auth(token))
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()]
    assert "Skill-A" in names
    assert "Skill-B" in names


def test_get_skill_detail(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin4", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "Detail-Skill")
    db.commit()
    token = _login(client, "admin4")

    resp = client.get(f"/api/skills/{skill.id}", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Detail-Skill"
    assert len(data["versions"]) == 1
    assert data["versions"][0]["version"] == 1


def test_get_skill_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "admin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "admin5")

    resp = client.get("/api/skills/99999", headers=_auth(token))
    assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────

def test_update_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin6", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "OldName")
    db.commit()
    token = _login(client, "admin6")

    resp = client.put(f"/api/skills/{skill.id}", headers=_auth(token), json={
        "name": "NewName",
        "description": "updated",
        "mode": "structured",
        "system_prompt": "new prompt",
        "knowledge_tags": [],
        "auto_inject": False,
        "variables": [],
    })
    assert resp.status_code == 200

    detail = client.get(f"/api/skills/{skill.id}", headers=_auth(token)).json()
    assert detail["name"] == "NewName"


# ── Versioning ────────────────────────────────────────────────────────────────

def test_add_version(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin7", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "VersionSkill")
    db.commit()
    token = _login(client, "admin7")

    resp = client.post(f"/api/skills/{skill.id}/versions", headers=_auth(token), json={
        "system_prompt": "新版本 prompt",
        "variables": ["行业"],
        "change_note": "加了行业变量",
    })
    assert resp.status_code == 200
    assert resp.json()["version"] == 2


def test_version_increments_correctly(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin8", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "IncrSkill")
    db.commit()
    token = _login(client, "admin8")

    for i in range(3):
        client.post(f"/api/skills/{skill.id}/versions", headers=_auth(token), json={
            "system_prompt": f"v{i+2} prompt", "variables": [],
        })

    detail = client.get(f"/api/skills/{skill.id}", headers=_auth(token)).json()
    versions = [v["version"] for v in detail["versions"]]
    assert sorted(versions, reverse=True) == versions  # descending order
    assert max(versions) == 4


# ── Status ────────────────────────────────────────────────────────────────────

def test_update_status(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin9", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "StatusSkill", status=SkillStatus.DRAFT)
    # 无绑定工具时，system_prompt 需 ≥200 行才能发布
    from app.models.skill import SkillVersion
    ver = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).first()
    ver.system_prompt = "\n".join([f"指令行 {i+1}" for i in range(200)])
    db.commit()
    token = _login(client, "admin9")

    resp = client.patch(f"/api/skills/{skill.id}/status?status=published", headers=_auth(token))
    assert resp.status_code == 200

    detail = client.get(f"/api/skills/{skill.id}", headers=_auth(token)).json()
    assert detail["status"] == "published"


def test_dept_admin_publish_creates_super_pending_approval(client, db):
    from app.models.sandbox import SandboxTestReport, SandboxTestSession
    from app.models.skill import SkillVersion

    dept = _make_dept(db)
    admin = _make_user(db, "dept_admin_publish", Role.DEPT_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "DeptAdminStatusSkill", status=SkillStatus.DRAFT)
    ver = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).first()
    ver.system_prompt = "\n".join([f"指令行 {i+1}" for i in range(200)])
    session = SandboxTestSession(target_type="skill", target_id=skill.id, tester_id=admin.id)
    db.add(session)
    db.flush()
    report = SandboxTestReport(
        session_id=session.id,
        target_type="skill",
        target_id=skill.id,
        tester_id=admin.id,
        approval_eligible=True,
        report_hash="deptadminhash",
    )
    db.add(report)
    db.commit()

    token = _login(client, "dept_admin_publish")
    resp = client.patch(f"/api/skills/{skill.id}/status?status=published", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "reviewing"
    assert resp.json()["approval_stage"] == "super_pending"

    db.refresh(skill)
    assert skill.status == SkillStatus.REVIEWING
    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.target_id == skill.id,
            ApprovalRequest.target_type == "skill",
        )
        .first()
    )
    assert approval is not None
    assert approval.stage == "super_pending"

    detail = client.get(f"/api/skills/{skill.id}", headers=_auth(token)).json()
    assert detail["approval_stage"] == "super_pending"


def test_update_status_invalid(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin10", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "StatusSkill2")
    db.commit()
    token = _login(client, "admin10")

    resp = client.patch(f"/api/skills/{skill.id}/status?status=invalid_value", headers=_auth(token))
    assert resp.status_code == 400


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin11", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "DeleteMe")
    db.commit()
    token = _login(client, "admin11")

    resp = client.delete(f"/api/skills/{skill.id}", headers=_auth(token))
    assert resp.status_code == 200

    resp = client.get(f"/api/skills/{skill.id}", headers=_auth(token))
    assert resp.status_code == 404


def test_delete_skill_cleans_referencing_records(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "admin11_refs", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "DeleteMeWithRefs")
    db.flush()
    db.add(SkillExecutionLog(skill_id=skill.id, user_id=admin.id, success=False, error_type="fk"))
    db.add(SkillAuditResult(skill_id=skill.id, quality_verdict="needs_work"))
    db.add(SkillFolderAlias(skill_id=skill.id, old_folder_key="delete-me-with-refs-old"))
    db.add(StagedEdit(skill_id=skill.id, target_type="system_prompt", diff_ops=[], summary="pending", risk_level="low"))
    db.add(ArchitectWorkflowState(conversation_id=900001, skill_id=skill.id))
    db.add(SkillKnowledgeReference(skill_id=skill.id, knowledge_id=1, publish_version=1))
    db.commit()
    token = _login(client, "admin11_refs")

    resp = client.delete(f"/api/skills/{skill.id}", headers=_auth(token))
    assert resp.status_code == 200

    assert db.query(SkillExecutionLog).filter(SkillExecutionLog.skill_id == skill.id).count() == 0
    assert db.query(SkillAuditResult).filter(SkillAuditResult.skill_id == skill.id).count() == 0
    assert db.query(SkillFolderAlias).filter(SkillFolderAlias.skill_id == skill.id).count() == 0
    assert db.query(StagedEdit).filter(StagedEdit.skill_id == skill.id).count() == 0
    assert db.query(SkillKnowledgeReference).filter(SkillKnowledgeReference.skill_id == skill.id).count() == 0

    workflow = db.query(ArchitectWorkflowState).filter(ArchitectWorkflowState.conversation_id == 900001).first()
    assert workflow is not None
    assert workflow.skill_id is None


def test_delete_skill_non_superadmin_forbidden(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "dadmin1", Role.DEPT_ADMIN, dept.id)
    _make_user(db, "duser1", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, admin.id, "NoDeleteSkill")
    db.commit()
    token = _login(client, "duser1")

    resp = client.delete(f"/api/skills/{skill.id}", headers=_auth(token))
    assert resp.status_code == 403
