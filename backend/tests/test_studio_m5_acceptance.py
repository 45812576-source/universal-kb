from app.models.skill_memo import SkillMemo
from app.services.studio_session_service import get_studio_session
from tests.conftest import _make_dept, _make_model_config, _make_skill, _make_user


def _seed_m5_recovery_skill(db):
    dept = _make_dept(db, name="Studio M5 QA")
    user = _make_user(db, username="studio_m5_qa", dept_id=dept.id)
    _make_model_config(db)
    skill = _make_skill(db, user.id, name="M5 QA Skill")
    memo = SkillMemo(
        skill_id=skill.id,
        scenario_type="published_iteration",
        lifecycle_stage="editing",
        status_summary="处理中",
        memo_payload={
            "workflow_recovery": {
                "workflow_state": {
                    "phase": "validation",
                    "active_card_id": "confirm-card",
                },
                "cards": [
                    {
                        "id": "tool-card",
                        "type": "governance",
                        "kind": "external_build",
                        "title": "实现搜索工具",
                        "status": "pending",
                        "target_file": "tools/search_tool.py",
                        "file_role": "tool",
                        "handoff_policy": "open_opencode",
                        "route_kind": "external",
                        "destination": "opencode",
                        "return_to": "bind_back",
                        "external_state": "returned_waiting_validation",
                        "content": {"summary": "需要外部实现搜索工具"},
                    },
                    {
                        "id": "confirm-card",
                        "type": "confirm",
                        "kind": "confirm",
                        "origin": "bind_back",
                        "title": "验收外部产物：实现搜索工具",
                        "status": "active",
                        "target_file": "tools/search_tool.py",
                        "file_role": "tool",
                        "handoff_policy": "stay_in_studio_chat",
                        "route_kind": "internal",
                        "destination": "studio_chat",
                        "return_to": "confirm",
                        "content": {
                            "source_card_id": "tool-card",
                            "summary": "外部结果已返回",
                        },
                    },
                ],
                "queue_window": {
                    "active_card_id": "confirm-card",
                    "visible_card_ids": ["confirm-card", "tool-card"],
                    "backlog_count": 0,
                    "phase": "validation",
                    "max_visible": 5,
                    "reveal_policy": "stage_gated",
                    "resume_hint": {
                        "kind": "resume_reprioritized",
                        "message": "由于外部产物待验证，当前优先处理“验收外部产物：实现搜索工具”",
                    },
                    "active_card_explanation": "外部实现已返回，请先验收再进入验证",
                },
            }
        },
        created_by=user.id,
        updated_by=user.id,
        version=3,
    )
    db.add(memo)
    db.commit()
    return skill


def test_m5_session_recovers_persisted_queue_window_and_external_summary(db):
    skill = _seed_m5_recovery_skill(db)

    session = get_studio_session(db, skill.id)

    assert session is not None
    assert session["active_card_id"] == "confirm-card"
    queue_window = session["card_queue_window"]
    assert queue_window["active_card_id"] == "confirm-card"
    assert queue_window["resume_hint"]["kind"] == "resume_reprioritized"
    assert "当前优先处理" in queue_window["resume_hint"]["message"]
    assert queue_window["active_card_explanation"] == "外部实现已返回，请先验收再进入验证"

    external = session["external_route_summary"]
    assert external is not None
    assert external["has_returned_waiting_validation"] is True
    assert external["current_external_card_title"] == "实现搜索工具"
    assert external["current_return_to"] == "bind_back"
