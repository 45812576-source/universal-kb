from app.models.skill_memo import SkillMemo
from app.services.studio_session_service import (
    _build_card_queue_window,
    get_studio_session,
)
from app.services.studio_workflow_adapter import (
    normalize_workflow_card,
    normalize_workflow_staged_edit,
)
from tests.conftest import _make_dept, _make_model_config, _make_skill, _make_user


def _seed_skill_memo(db):
    dept = _make_dept(db, name="Studio QA")
    user = _make_user(db, username="studio_qa", dept_id=dept.id)
    _make_model_config(db)
    skill = _make_skill(db, user.id, name="M1 QA Skill")
    memo = SkillMemo(
        skill_id=skill.id,
        scenario_type="published_iteration",
        lifecycle_stage="editing",
        status_summary="处理中",
        memo_payload={
            "workflow_recovery": {
                "workflow_state": {
                    "phase": "governance_execution",
                    "active_card_id": "card-main",
                },
                "cards": [
                    {
                        "id": "card-main",
                        "type": "governance",
                        "title": "编辑主 Prompt",
                        "status": "active",
                        "target": {"type": "prompt", "key": "SKILL.md"},
                        "content": {"summary": "处理主文件"},
                    },
                    {
                        "id": "card-tool",
                        "type": "governance",
                        "title": "工具交接卡",
                        "status": "pending",
                        "target": {"type": "source_file", "key": "tools/search_tool.py"},
                        "content": {"summary": "外部工具实现"},
                    },
                ],
                "staged_edits": [
                    {
                        "id": "edit-tool",
                        "target_type": "source_file",
                        "target_key": "tools/search_tool.py",
                        "summary": "补工具实现",
                        "status": "pending",
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
    return skill


def test_m1_session_backfills_file_role_and_handoff_policy(db):
    skill = _seed_skill_memo(db)

    session = get_studio_session(db, skill.id)

    assert session is not None
    assert session["active_card_id"] == "card-main"
    cards = {card["id"]: card for card in session["cards"]}
    assert cards["card-main"]["file_role"] == "main_prompt"
    assert cards["card-main"]["handoff_policy"] == "open_file_workspace"
    assert cards["card-main"]["route_kind"] == "internal"
    assert cards["card-tool"]["file_role"] == "tool"
    assert cards["card-tool"]["handoff_policy"] == "open_development_studio"
    assert cards["card-tool"]["route_kind"] == "external"

    staged_edit = session["staged_edits"][0]
    assert staged_edit["file_role"] == "tool"
    assert staged_edit["handoff_policy"] == "open_development_studio"
    assert staged_edit["route_kind"] == "external"

    queue_window = session["card_queue_window"]
    assert queue_window["active_card_id"] == "card-main"
    assert len(queue_window["visible_card_ids"]) <= 5
    assert session["workflow_cards"] == session["cards"]


def test_m1_queue_window_caps_visible_cards_to_five():
    cards = [
        {"id": "card-1", "status": "active"},
        {"id": "card-2", "status": "pending"},
        {"id": "card-3", "status": "pending"},
        {"id": "card-4", "status": "queued"},
        {"id": "card-5", "status": "reviewing"},
        {"id": "card-6", "status": "drafting"},
        {"id": "card-7", "status": "diff_ready"},
        {"id": "card-8", "status": "adopted"},
    ]

    queue_window = _build_card_queue_window(
        cards,
        active_card_id="card-1",
        workflow_state={"phase": "phase_2_what"},
    )

    assert queue_window is not None
    assert queue_window["visible_card_ids"] == ["card-1", "card-2", "card-3", "card-4", "card-5"]
    assert queue_window["backlog_count"] == 2
    assert queue_window["phase"] == "phase_2_what"
    assert queue_window["reveal_policy"] == "stage_gated"


def test_m1_protocol_normalizers_infer_roles_for_example_and_tool():
    card = normalize_workflow_card(
        {
            "id": "card-example",
            "type": "governance",
            "title": "完善 Example",
            "target": {"type": "source_file", "key": "example-demo.md"},
            "target_file": "example-demo.md",
            "content": {"summary": "补示例"},
        },
        source_type="studio_governance",
    )
    edit = normalize_workflow_staged_edit(
        {
            "id": "edit-tool",
            "target_type": "source_file",
            "target_key": "tools/new_tool.py",
            "summary": "补工具实现",
        },
        source_type="studio_governance",
    )

    assert card["file_role"] == "example"
    assert card["handoff_policy"] == "open_file_workspace"
    assert card["route_kind"] == "internal"
    assert edit["file_role"] == "tool"
    assert edit["handoff_policy"] == "open_development_studio"
    assert edit["route_kind"] == "external"
