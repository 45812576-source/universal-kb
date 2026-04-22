"""Adapters between legacy Studio payloads and unified workflow protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.services.studio_workflow_protocol import (
    WorkflowAction,
    WorkflowActionResult,
    WorkflowCardData,
    WorkflowEventEnvelope,
    WorkflowStagedEditData,
)

def _infer_file_role(*, target_file: str | None, target: dict[str, Any] | None, content: dict[str, Any] | None) -> str | None:
    target_kind = str((content or {}).get("target_kind") or "").strip().lower()
    target_type = str((target or {}).get("type") or (target or {}).get("target_type") or "").strip().lower()
    file_path = (target_file or "").strip()
    file_name = file_path.rsplit("/", 1)[-1] if file_path else ""
    lower_name = file_name.lower()

    if target_kind == "skill_prompt" or lower_name == "skill.md" or target_type in {"prompt", "system_prompt"}:
        return "main_prompt"
    if "example" in lower_name or target_kind == "example":
        return "example"
    if "reference" in lower_name or target_kind == "reference":
        return "reference"
    if target_kind in {"knowledge_base", "knowledge"}:
        return "knowledge_base"
    if target_kind == "template" or "template" in lower_name:
        return "template"
    if target_kind == "tool" or target_type == "tool_binding" or "tool" in lower_name:
        return "tool"
    if file_path:
        return "unknown_asset"
    return None


def _infer_handoff_policy(*, file_role: str | None, workspace_mode: str | None, target_file: str | None) -> str | None:
    if file_role == "tool":
        return "open_development_studio"
    if workspace_mode == "report":
        return "open_governance_panel"
    if workspace_mode == "analysis":
        return "stay_in_studio_chat"
    if target_file:
        return "open_file_workspace"
    return None


def _route_kind_for_policy(handoff_policy: str | None) -> str | None:
    """B8: 委托给 studio_card_transition_service.classify_route()。"""
    if not handoff_policy:
        return None
    from app.services import studio_card_transition_service
    route_info = studio_card_transition_service.classify_route(
        card={"handoff_policy": handoff_policy},
    )
    kind = route_info.get("route_kind")
    return kind if kind in ("external", "internal") else None


def _destination_for_policy(handoff_policy: str | None) -> str | None:
    destinations = {
        "open_development_studio": "dev_studio",
        "open_opencode": "opencode",
        "open_file_workspace": "file_workspace",
        "open_governance_panel": "governance_panel",
        "stay_in_studio_chat": "studio_chat",
    }
    return destinations.get(handoff_policy or "")


def _recovery_meta(memo: dict[str, Any] | None) -> dict[str, Any]:
    recovery = memo.get("workflow_recovery") if isinstance(memo, dict) else None
    if not isinstance(recovery, dict):
        return {
            "recovery_source": None,
            "recovery_revision": None,
            "recovery_updated_at": None,
        }
    return {
        "recovery_source": recovery.get("source") if isinstance(recovery.get("source"), str) else None,
        "recovery_revision": int(recovery.get("revision")) if isinstance(recovery.get("revision"), int) else None,
        "recovery_updated_at": recovery.get("updated_at") if isinstance(recovery.get("updated_at"), str) else None,
    }


def normalize_workflow_card(
    raw: dict[str, Any],
    *,
    source_type: str,
    phase: str = "review",
    workflow_id: str | None = None,
) -> dict[str, Any]:
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    target_file = raw.get("target_file")
    if target_file is None:
        target_file = raw.get("target_ref") or content.get("target_ref") or content.get("file_path")
    target_file_str = str(target_file) if isinstance(target_file, str) and target_file else None
    workspace_mode = str(raw.get("workspace_mode") or "") or None
    file_role = str(raw.get("file_role") or content.get("file_role") or "").strip() or None
    if file_role is None:
        file_role = _infer_file_role(target_file=target_file_str, target=target, content=content)
    handoff_policy = str(raw.get("handoff_policy") or content.get("handoff_policy") or "").strip() or None
    if handoff_policy is None:
        handoff_policy = _infer_handoff_policy(
            file_role=file_role,
            workspace_mode=workspace_mode,
            target_file=target_file_str,
        )
    route_kind = str(raw.get("route_kind") or content.get("route_kind") or "").strip() or _route_kind_for_policy(handoff_policy)
    destination = str(raw.get("destination") or content.get("destination") or "").strip() or _destination_for_policy(handoff_policy)
    return_to = str(raw.get("return_to") or content.get("return_to") or "").strip() or ("bind_back" if route_kind == "external" else "none")
    summary = str(
        raw.get("summary")
        or content.get("summary")
        or raw.get("description")
        or raw.get("reason")
        or ""
    )[:300]
    actions: list[WorkflowAction] = []
    for action in raw.get("actions") or []:
        if not isinstance(action, dict):
            continue
        actions.append(WorkflowAction(
            label=str(action.get("label") or ""),
            type=str(action.get("type") or "adopt"),
            payload=action.get("payload") if isinstance(action.get("payload"), dict) else None,
        ))
    if not actions:
        actions = [
            WorkflowAction(label="查看修改", type="view_diff"),
            WorkflowAction(label="采纳", type="adopt"),
            WorkflowAction(label="不采纳", type="reject"),
        ]

    card = WorkflowCardData(
        id=str(raw.get("id") or ""),
        workflow_id=workflow_id,
        source_type=source_type,
        card_type=str(raw.get("type") or "staged_edit"),
        phase=str(raw.get("phase") or phase),
        title=str(raw.get("title") or "治理建议")[:120],
        summary=summary,
        status=str(raw.get("status") or "pending"),
        priority=str(raw.get("priority") or "medium"),
        target=target,
        actions=actions,
        content=content,
        workspace_mode=workspace_mode,
        target_file=target_file_str,
        file_role=file_role,
        handoff_policy=handoff_policy,
        route_kind=route_kind,
        destination=destination,
        return_to=return_to,
        external_state=str(raw.get("external_state") or content.get("external_state") or "").strip() or None,
    )
    result = card.to_dict()
    if raw.get("severity") is not None:
        result["severity"] = raw.get("severity")
    if raw.get("category") is not None:
        result["category"] = raw.get("category")
    if raw.get("suggested_action") is not None:
        result["suggested_action"] = raw.get("suggested_action")
    return result


def normalize_workflow_staged_edit(
    raw: dict[str, Any],
    *,
    source_type: str,
    workflow_id: str | None = None,
    origin_card_id: str | None = None,
) -> dict[str, Any]:
    target_key = str(raw.get("target_key")) if raw.get("target_key") is not None else None
    target_type = str(raw.get("target_type") or "system_prompt")
    file_role = str(raw.get("file_role") or "").strip() or _infer_file_role(
        target_file=target_key,
        target={"target_type": target_type},
        content=None,
    )
    handoff_policy = str(raw.get("handoff_policy") or "").strip() or _infer_handoff_policy(
        file_role=file_role,
        workspace_mode="file",
        target_file=target_key,
    )
    route_kind = str(raw.get("route_kind") or "").strip() or _route_kind_for_policy(handoff_policy)
    destination = str(raw.get("destination") or "").strip() or _destination_for_policy(handoff_policy)
    return_to = str(raw.get("return_to") or "").strip() or ("bind_back" if route_kind == "external" else "none")
    edit = WorkflowStagedEditData(
        id=str(raw.get("id") or ""),
        workflow_id=workflow_id,
        origin_card_id=origin_card_id,
        source_type=source_type,
        target_type=target_type,
        target_key=target_key,
        summary=str(raw.get("summary") or "治理修改")[:200],
        risk_level=str(raw.get("risk_level") or "medium"),
        diff_ops=list(raw.get("diff_ops") or []),
        status=str(raw.get("status") or "pending"),
        file_role=file_role,
        handoff_policy=handoff_policy,
        route_kind=route_kind,
        destination=destination,
        return_to=return_to,
    )
    return edit.to_dict()


def build_workflow_event_envelope(
    *,
    event_type: str,
    payload: dict[str, Any],
    source_type: str,
    phase: str,
    workflow_id: str | None = None,
    skill_id: int | None = None,
    conversation_id: int | None = None,
    step: str | None = None,
) -> dict[str, Any]:
    return WorkflowEventEnvelope(
        event_type=event_type,
        workflow_id=workflow_id,
        source_type=source_type,
        phase=phase,
        payload=payload,
        skill_id=skill_id,
        conversation_id=conversation_id,
        step=step,
    ).to_dict()


def dispatch_workflow_action(
    db: Session,
    *,
    skill_id: int,
    action: str,
    staged_edit_id: int | None,
    user_id: int,
    card_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}

    if action == "prepare_next_step":
        from app.services.skill_memo_service import get_memo

        memo = get_memo(db, skill_id)
        recovery = memo.get("workflow_recovery") if isinstance(memo, dict) else None
        workflow_state = recovery.get("workflow_state") if isinstance(recovery, dict) and isinstance(recovery.get("workflow_state"), dict) else {}
        metadata = workflow_state.get("metadata") if isinstance(workflow_state.get("metadata"), dict) else {}
        test_recommendation = metadata.get("test_recommendation") if isinstance(metadata.get("test_recommendation"), dict) else None
        return WorkflowActionResult(
            action_id=f"wf_action_{skill_id}_prepare_next_step",
            ok=bool(workflow_state),
            action=action,
            card_id=card_id,
            workflow_state_patch=dict(workflow_state),
            memo_refresh_required=False,
            editor_refresh_required=False,
            **_recovery_meta(memo if isinstance(memo, dict) else None),
            result={
                "next_action": workflow_state.get("next_action"),
                "phase": workflow_state.get("phase"),
                "test_recommendation": test_recommendation,
            },
            error=None if workflow_state else "workflow_state_not_found",
        ).to_dict()

    if action == "adopt_staged_edit":
        from app.services.studio_governance import adopt_staged_edit
        from app.services.skill_memo_service import patch_workflow_recovery_action

        if not staged_edit_id:
            return WorkflowActionResult(
                action_id="",
                ok=False,
                action=action,
                card_id=card_id,
                error="missing_staged_edit_id",
            ).to_dict()
        adopted = adopt_staged_edit(db, staged_edit_id, user_id)
        workflow_state_patch: dict[str, Any] = {}
        memo: dict[str, Any] | None = None
        if adopted.get("ok"):
            memo = patch_workflow_recovery_action(
                db,
                skill_id,
                card_id=card_id,
                staged_edit_id=str(staged_edit_id),
                updated_card_status="adopted",
                updated_staged_edit_status="adopted",
                user_id=user_id,
                commit=True,
            )
            if isinstance(memo, dict):
                recovery = memo.get("workflow_recovery")
                if isinstance(recovery, dict) and isinstance(recovery.get("workflow_state"), dict):
                    workflow_state_patch = dict(recovery["workflow_state"])
        return WorkflowActionResult(
            action_id=f"wf_action_{staged_edit_id}_adopt",
            ok=bool(adopted.get("ok")),
            action=action,
            card_id=card_id,
            staged_edit_id=str(staged_edit_id),
            target_type=str(adopted.get("target_type")) if adopted.get("target_type") is not None else None,
            target_key=str(adopted.get("target_key")) if adopted.get("target_key") is not None else None,
            updated_card_status="adopted" if adopted.get("ok") else None,
            updated_staged_edit_status="adopted" if adopted.get("ok") else None,
            workflow_state_patch=workflow_state_patch,
            memo_refresh_required=True,
            editor_refresh_required=True,
            **_recovery_meta(memo),
            result=adopted,
            error=None if adopted.get("ok") else str(adopted.get("error") or "adopt_failed"),
        ).to_dict()

    if action == "reject_staged_edit":
        from app.services.studio_governance import reject_staged_edit
        from app.services.skill_memo_service import patch_workflow_recovery_action

        if not staged_edit_id:
            return WorkflowActionResult(
                action_id="",
                ok=False,
                action=action,
                card_id=card_id,
                error="missing_staged_edit_id",
            ).to_dict()
        rejected = reject_staged_edit(db, staged_edit_id, user_id)
        workflow_state_patch: dict[str, Any] = {}
        memo: dict[str, Any] | None = None
        if rejected.get("ok"):
            memo = patch_workflow_recovery_action(
                db,
                skill_id,
                card_id=card_id,
                staged_edit_id=str(staged_edit_id),
                updated_card_status="rejected",
                updated_staged_edit_status="rejected",
                user_id=user_id,
                commit=True,
            )
            if isinstance(memo, dict):
                recovery = memo.get("workflow_recovery")
                if isinstance(recovery, dict) and isinstance(recovery.get("workflow_state"), dict):
                    workflow_state_patch = dict(recovery["workflow_state"])
        return WorkflowActionResult(
            action_id=f"wf_action_{staged_edit_id}_reject",
            ok=bool(rejected.get("ok")),
            action=action,
            card_id=card_id,
            staged_edit_id=str(staged_edit_id),
            target_type=str(rejected.get("target_type")) if rejected.get("target_type") is not None else None,
            target_key=str(rejected.get("target_key")) if rejected.get("target_key") is not None else None,
            updated_card_status="rejected" if rejected.get("ok") else None,
            updated_staged_edit_status="rejected" if rejected.get("ok") else None,
            workflow_state_patch=workflow_state_patch,
            memo_refresh_required=True,
            editor_refresh_required=False,
            **_recovery_meta(memo),
            result=rejected,
            error=None if rejected.get("ok") else str(rejected.get("error") or "reject_failed"),
        ).to_dict()

    if action in {
        "confirm_archive",
        "reindex_knowledge",
        "navigate_tools",
        "navigate_data_assets",
        "bind_sandbox_tools",
        "bind_knowledge_references",
        "bind_permission_tables",
        "binding_action",
    }:
        from app.models.user import User
        from app.services.skill_memo_service import patch_workflow_recovery_action
        from app.services.studio_followup_actions import (
            apply_sandbox_report_action,
            confirm_knowledge_archive,
            reindex_skill_knowledge,
        )

        user = db.get(User, user_id)
        if not user:
            return WorkflowActionResult(
                action_id="",
                ok=False,
                action=action,
                card_id=card_id,
                error="user_not_found",
            ).to_dict()

        try:
            if action == "confirm_archive":
                result = confirm_knowledge_archive(
                    db,
                    skill_id=skill_id,
                    user=user,
                    confirmations=list(payload.get("confirmations") or []),
                )
            elif action == "reindex_knowledge":
                knowledge_ids = [int(item) for item in (payload.get("knowledge_ids") or []) if str(item).isdigit()]
                result = reindex_skill_knowledge(
                    db,
                    skill_id=skill_id,
                    knowledge_ids=knowledge_ids,
                    user=user,
                )
            elif action in {"bind_sandbox_tools", "bind_knowledge_references", "bind_permission_tables"}:
                report_id = int(payload.get("source_report_id") or 0)
                if report_id <= 0:
                    raise ValueError("missing_source_report_id")
                result = apply_sandbox_report_action(
                    db,
                    report_id=report_id,
                    action=action,
                    payload=payload,
                    user=user,
                )
            elif action == "binding_action":
                from app.services.binding_actions import execute_binding_action

                binding_action = str(payload.get("action") or "")
                target_id = int(payload.get("target_id") or 0)
                if not binding_action or target_id <= 0:
                    raise ValueError("missing_binding_target")
                result = execute_binding_action(db, skill_id, user, binding_action, target_id)
            else:
                target_url = str(payload.get("target_url") or ("/data" if action == "navigate_data_assets" else "/skills"))
                result = {"ok": True, "action": action, "target_url": target_url}
        except HTTPException as exc:
            return WorkflowActionResult(
                action_id="",
                ok=False,
                action=action,
                card_id=card_id,
                error=str(exc.detail),
            ).to_dict()
        except ValueError as exc:
            return WorkflowActionResult(
                action_id="",
                ok=False,
                action=action,
                card_id=card_id,
                error=str(exc),
            ).to_dict()

        workflow_state_patch: dict[str, Any] = {}
        memo_refresh_required = False
        memo: dict[str, Any] | None = None
        if result.get("ok") and card_id:
            memo = patch_workflow_recovery_action(
                db,
                skill_id,
                card_id=card_id,
                updated_card_status="adopted",
                user_id=user_id,
                commit=True,
            )
            memo_refresh_required = True
            if isinstance(memo, dict):
                recovery = memo.get("workflow_recovery")
                if isinstance(recovery, dict) and isinstance(recovery.get("workflow_state"), dict):
                    workflow_state_patch = dict(recovery["workflow_state"])

        return WorkflowActionResult(
            action_id=f"wf_action_{skill_id}_{action}",
            ok=bool(result.get("ok")),
            action=action,
            card_id=card_id,
            updated_card_status="adopted" if result.get("ok") and card_id else None,
            workflow_state_patch=workflow_state_patch,
            memo_refresh_required=memo_refresh_required,
            editor_refresh_required=False,
            **_recovery_meta(memo),
            result=result,
            error=None if result.get("ok") else str(result.get("error") or "followup_action_failed"),
        ).to_dict()

    return WorkflowActionResult(
        action_id="",
        ok=False,
        action=action,
        card_id=card_id,
        error="unsupported_action",
    ).to_dict()
