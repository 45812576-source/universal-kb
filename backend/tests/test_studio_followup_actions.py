from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.sandbox import SandboxTestReport, SandboxTestSession
from app.models.skill_knowledge_ref import SkillKnowledgeReference
from app.models.user import Role
from app.services.studio_followup_actions import apply_sandbox_report_action
from tests.conftest import _make_dept, _make_skill, _make_user


def test_bind_knowledge_references_uses_session_tester_access(db):
    dept = _make_dept(db)
    user = _make_user(db, "skill_owner_bind_refs", Role.EMPLOYEE, dept.id)
    skill = _make_skill(db, user.id, "BindKnowledgeRefsSkill")
    knowledge = KnowledgeEntry(
        title="Knowledge Ref A",
        content="知识正文",
        status=KnowledgeStatus.APPROVED,
        created_by=user.id,
    )
    db.add(knowledge)
    db.flush()

    session = SandboxTestSession(
        target_type="skill",
        target_id=skill.id,
        tester_id=user.id,
    )
    db.add(session)
    db.flush()

    report = SandboxTestReport(
        session_id=session.id,
        target_type="skill",
        target_id=skill.id,
        tester_id=user.id,
        report_hash="bind-knowledge-refs-report",
    )
    db.add(report)
    db.commit()

    result = apply_sandbox_report_action(
        db,
        report_id=report.id,
        action="bind_knowledge_references",
        payload={"knowledge_ids": [knowledge.id]},
        user=user,
    )

    assert result["ok"] is True
    assert result["bound"] == 1
    refs = db.query(SkillKnowledgeReference).filter(
        SkillKnowledgeReference.skill_id == skill.id,
        SkillKnowledgeReference.knowledge_id == knowledge.id,
    ).all()
    assert len(refs) == 1
