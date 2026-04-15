"""Skill Studio rollout and feature flag helpers for P4."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import Role, User
from app.services.studio_latency_policy import initial_lane_statuses

_ROLLOUT_FLAG_KEYS = (
    "dual_lane_enabled",
    "fast_lane_enabled",
    "deep_lane_enabled",
    "sla_degrade_enabled",
    "patch_protocol_enabled",
    "frontend_run_protocol_enabled",
)

_USER_FEATURE_FLAG_MAP = {
    "dual_lane_enabled": "skill_studio_dual_lane_enabled",
    "fast_lane_enabled": "skill_studio_fast_lane_enabled",
    "deep_lane_enabled": "skill_studio_deep_lane_enabled",
    "sla_degrade_enabled": "skill_studio_sla_degrade_enabled",
    "patch_protocol_enabled": "skill_studio_patch_protocol_enabled",
    "frontend_run_protocol_enabled": "skill_studio_frontend_run_protocol_enabled",
}


@dataclass(frozen=True)
class StudioRolloutFlags:
    dual_lane_enabled: bool = True
    fast_lane_enabled: bool = True
    deep_lane_enabled: bool = True
    sla_degrade_enabled: bool = True
    patch_protocol_enabled: bool = True
    frontend_run_protocol_enabled: bool = True

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class StudioRolloutDecision:
    eligible: bool
    scope: str
    reason: str
    flags: StudioRolloutFlags
    user_id: int | None = None
    department_id: int | None = None
    session_mode: str | None = None
    workflow_mode: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "scope": self.scope,
            "reason": self.reason,
            "flags": self.flags.to_dict(),
            "user_id": self.user_id,
            "department_id": self.department_id,
            "session_mode": self.session_mode,
            "workflow_mode": self.workflow_mode,
        }


def _parse_int_list(raw: str | None) -> set[int]:
    result: set[int] = set()
    for token in (raw or "").replace(";", ",").split(","):
        token = token.strip()
        if token.isdigit():
            result.add(int(token))
    return result


def _parse_str_list(raw: str | None) -> set[str]:
    return {token.strip() for token in (raw or "").replace(";", ",").split(",") if token.strip()}


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _normalize_flags(flags: StudioRolloutFlags) -> StudioRolloutFlags:
    dual_lane_enabled = bool(flags.dual_lane_enabled)
    fast_lane_enabled = bool(flags.fast_lane_enabled)
    deep_lane_enabled = bool(flags.deep_lane_enabled and dual_lane_enabled)
    if not fast_lane_enabled and not deep_lane_enabled:
        fast_lane_enabled = True
    return StudioRolloutFlags(
        dual_lane_enabled=dual_lane_enabled,
        fast_lane_enabled=fast_lane_enabled,
        deep_lane_enabled=deep_lane_enabled,
        sla_degrade_enabled=bool(flags.sla_degrade_enabled and fast_lane_enabled),
        patch_protocol_enabled=bool(flags.patch_protocol_enabled),
        frontend_run_protocol_enabled=bool(flags.frontend_run_protocol_enabled),
    )


def _feature_override(user: User | None, flag_key: str) -> bool | None:
    if user is None or not isinstance(user.feature_flags, dict):
        return None
    return _coerce_bool(user.feature_flags.get(_USER_FEATURE_FLAG_MAP[flag_key]))


def resolve_rollout_decision(
    db: Session,
    *,
    user_id: int | None,
    session_mode: str | None = None,
    workflow_mode: str | None = None,
) -> StudioRolloutDecision:
    user = db.get(User, user_id) if user_id else None
    configured_scopes: list[str] = []
    matched_scopes: list[str] = []

    if settings.SKILL_STUDIO_ROLLOUT_INTERNAL_ONLY:
        configured_scopes.append("internal")
        if user and user.role in {Role.SUPER_ADMIN, Role.DEPT_ADMIN}:
            matched_scopes.append("internal")

    rollout_user_ids = _parse_int_list(settings.SKILL_STUDIO_ROLLOUT_USER_IDS)
    if rollout_user_ids:
        configured_scopes.append("users")
        if user_id in rollout_user_ids:
            matched_scopes.append("users")

    rollout_department_ids = _parse_int_list(settings.SKILL_STUDIO_ROLLOUT_DEPARTMENT_IDS)
    if rollout_department_ids:
        configured_scopes.append("departments")
        if user and user.department_id in rollout_department_ids:
            matched_scopes.append("departments")

    rollout_session_modes = _parse_str_list(settings.SKILL_STUDIO_ROLLOUT_SESSION_MODES)
    if rollout_session_modes:
        configured_scopes.append("session_modes")
        if session_mode and session_mode in rollout_session_modes:
            matched_scopes.append("session_modes")

    eligible = not configured_scopes or bool(matched_scopes)
    scope = "global_default" if not configured_scopes else "+".join(matched_scopes) if matched_scopes else "blocked"
    reason = "global_default" if not configured_scopes else "matched_rollout_scope" if matched_scopes else "rollout_scope_miss"

    effective_flags = {
        "dual_lane_enabled": settings.SKILL_STUDIO_DUAL_LANE_ENABLED and eligible,
        "fast_lane_enabled": settings.SKILL_STUDIO_FAST_LANE_ENABLED and eligible,
        "deep_lane_enabled": settings.SKILL_STUDIO_DEEP_LANE_ENABLED and eligible,
        "sla_degrade_enabled": settings.SKILL_STUDIO_SLA_DEGRADE_ENABLED and eligible,
        "patch_protocol_enabled": settings.SKILL_STUDIO_PATCH_PROTOCOL_ENABLED and eligible,
        "frontend_run_protocol_enabled": settings.SKILL_STUDIO_FRONTEND_RUN_PROTOCOL_ENABLED and eligible,
    }
    for flag_key in _ROLLOUT_FLAG_KEYS:
        override = _feature_override(user, flag_key)
        if override is not None:
            effective_flags[flag_key] = override

    return StudioRolloutDecision(
        eligible=eligible,
        scope=scope,
        reason=reason,
        flags=_normalize_flags(StudioRolloutFlags(**effective_flags)),
        user_id=user.id if user else user_id,
        department_id=user.department_id if user else None,
        session_mode=session_mode,
        workflow_mode=workflow_mode,
    )


def merge_rollout_metadata(
    metadata: dict[str, Any] | None,
    decision: StudioRolloutDecision,
) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    next_metadata["rollout"] = decision.to_metadata()
    return next_metadata


def apply_rollout_to_execution_strategy(
    execution_strategy: str,
    *,
    flags: StudioRolloutFlags,
) -> str:
    if not flags.deep_lane_enabled:
        return "fast_only"
    if not flags.fast_lane_enabled:
        return "deep_resume"
    return execution_strategy


def lane_statuses_for_rollout(
    execution_strategy: str,
    *,
    flags: StudioRolloutFlags,
) -> dict[str, str]:
    lane_statuses = initial_lane_statuses(execution_strategy)
    if not flags.fast_lane_enabled:
        lane_statuses["fast_status"] = "not_requested"
    if not flags.deep_lane_enabled:
        lane_statuses["deep_status"] = "not_requested"
    return lane_statuses


def rollout_flag_from_workflow_state(
    workflow_state: dict[str, Any] | None,
    *,
    flag_key: str,
    default: bool = True,
) -> bool:
    if flag_key not in _ROLLOUT_FLAG_KEYS:
        return default
    if not isinstance(workflow_state, dict):
        return default
    metadata = workflow_state.get("metadata")
    if not isinstance(metadata, dict):
        return default
    rollout = metadata.get("rollout")
    if not isinstance(rollout, dict):
        return default
    flags = rollout.get("flags")
    if not isinstance(flags, dict):
        return default
    value = _coerce_bool(flags.get(flag_key))
    return default if value is None else value
