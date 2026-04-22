from app.services.studio_patch_bus import patch_type_for_event
from app.services.studio_workflow_adapter import normalize_workflow_card, normalize_workflow_staged_edit
from app.services.studio_workflow_protocol import StudioSessionData


def test_normalize_workflow_card_infers_file_role_and_handoff_policy():
    card = normalize_workflow_card(
        {
            "id": "card_1",
            "type": "governance",
            "title": "更新主 Prompt",
            "target": {"type": "prompt", "key": "SKILL.md"},
            "target_file": "SKILL.md",
            "content": {"summary": "处理 SKILL.md"},
        },
        source_type="studio_governance",
    )

    assert card["file_role"] == "main_prompt"
    assert card["handoff_policy"] == "open_file_workspace"
    assert card["route_kind"] == "internal"
    assert card["destination"] == "file_workspace"
    assert card["target_file"] == "SKILL.md"


def test_normalize_workflow_staged_edit_infers_tool_handoff():
    edit = normalize_workflow_staged_edit(
        {
            "id": "edit_1",
            "target_type": "source_file",
            "target_key": "tools/search_tool.py",
            "summary": "补工具实现",
        },
        source_type="studio_governance",
    )

    assert edit["file_role"] == "tool"
    assert edit["handoff_policy"] == "open_development_studio"
    assert edit["route_kind"] == "external"
    assert edit["destination"] == "dev_studio"
    assert edit["return_to"] == "bind_back"


def test_studio_session_data_exposes_workflow_cards_alias():
    window = {
        "active_card_id": "card_1",
        "visible_card_ids": ["card_1"],
        "backlog_count": 0,
        "phase": "review",
        "max_visible": 5,
        "reveal_policy": "stage_gated",
    }
    payload = StudioSessionData(
        skill_id=1,
        cards=[{"id": "card_1"}],
        staged_edits=[{"id": "edit_1"}],
        card_queue_window=window,
    ).to_dict()

    assert payload["cards"] == [{"id": "card_1"}]
    assert payload["workflow_cards"] == [{"id": "card_1"}]
    assert payload["card_queue_window"]["active_card_id"] == "card_1"


def test_patch_type_for_event_uses_session5_card_aliases():
    assert patch_type_for_event("governance_card") == "card_patch"
    assert patch_type_for_event("staged_edit_notice") == "staged_edit_patch"
    assert patch_type_for_event("card_status_patch") == "card_status_patch"
