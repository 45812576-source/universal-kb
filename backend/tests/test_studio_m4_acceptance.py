from app.models.skill_memo import SkillMemo
from app.services import studio_card_service
from app.services.studio_session_service import get_studio_session
from tests.conftest import _make_dept, _make_model_config, _make_skill, _make_user


def _seed_external_tool_card(db):
    dept = _make_dept(db, name="Studio M4 QA")
    user = _make_user(db, username="studio_m4_qa", dept_id=dept.id)
    _make_model_config(db)
    skill = _make_skill(db, user.id, name="M4 QA Skill")
    memo = SkillMemo(
        skill_id=skill.id,
        scenario_type="published_iteration",
        lifecycle_stage="editing",
        status_summary="处理中",
        memo_payload={
            "workflow_recovery": {
                "workflow_state": {
                    "phase": "governance_execution",
                    "active_card_id": "tool-card",
                },
                "cards": [
                    {
                        "id": "tool-card",
                        "type": "governance",
                        "title": "实现搜索工具",
                        "status": "active",
                        "target_file": "tools/search_tool.py",
                        "file_role": "tool",
                        "handoff_policy": "open_opencode",
                        "content": {"summary": "需要外部实现搜索工具"},
                    }
                ],
            }
        },
        created_by=user.id,
        updated_by=user.id,
        version=3,
    )
    db.add(memo)
    db.commit()
    return skill, user


def test_m4_handoff_returns_frontend_contract_and_blocks_internal_route(db):
    skill, user = _seed_external_tool_card(db)

    internal = studio_card_service.handoff_card(
        db,
        skill.id,
        "tool-card",
        target_role="knowledge_base",
        target_file="kb.md",
        handoff_policy="open_governance_panel",
        user_id=user.id,
    )
    assert internal["ok"] is False
    assert internal["error_code"] == "invalid_handoff_policy"

    result = studio_card_service.handoff_card(
        db,
        skill.id,
        "tool-card",
        target_role="tool",
        target_file="tools/search_tool.py",
        handoff_policy="open_opencode",
        handoff_summary="实现搜索工具",
        acceptance_criteria=["能返回搜索结果"],
        user_id=user.id,
    )

    assert result["ok"] is True
    assert result["route_kind"] == "external"
    assert result["destination"] == "opencode"
    assert result["return_to"] == "bind_back"
    assert result["acceptance_criteria"] == ["能返回搜索结果"]

    session = get_studio_session(db, skill.id)
    cards = {card["id"]: card for card in session["cards"]}
    assert cards["tool-card"]["external_state"] == "waiting_external_build"
    derived = cards[result["derived_card_id"]]
    assert derived["route_kind"] == "external"
    assert derived["destination"] == "opencode"
    assert derived["return_to"] == "bind_back"
    assert derived["external_state"] == "waiting_external_build"
    assert session["external_route_summary"]["has_pending"] is True


def test_m4_bind_back_converges_to_confirm_and_validate(db):
    skill, user = _seed_external_tool_card(db)
    handoff = studio_card_service.handoff_card(
        db,
        skill.id,
        "tool-card",
        target_role="tool",
        target_file="tools/search_tool.py",
        handoff_policy="open_development_studio",
        user_id=user.id,
    )

    result = studio_card_service.bind_back_card(
        db,
        skill.id,
        handoff["derived_card_id"],
        summary="外部实现已提交",
        required_checks=["运行 sandbox"],
        user_id=user.id,
    )

    assert result["ok"] is True
    assert result["route_kind"] == "internal"
    assert result["destination"] == "studio_chat"
    assert result["return_to"] == "confirm"
    assert result["next_card_id"] == result["confirm_card_id"]
    assert result["next_card_kind"] == "confirm"
    assert result["validate_card_id"]

    session = get_studio_session(db, skill.id)
    cards = {card["id"]: card for card in session["cards"]}
    confirm = cards[result["confirm_card_id"]]
    validate = cards[result["validate_card_id"]]
    assert confirm["route_kind"] == "internal"
    assert confirm["destination"] == "studio_chat"
    assert confirm["return_to"] == "confirm"
    assert validate["route_kind"] == "internal"
    assert validate["destination"] == "governance_panel"
    assert validate["return_to"] == "validate"
    assert session["external_route_summary"]["has_returned_waiting_validation"] is True
