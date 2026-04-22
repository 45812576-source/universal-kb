"""Skill Studio patch protocol helpers.

P2 先把 patch envelope、事件分类和 run-aware 判定规则固定下来。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


PATCH_TYPE_BY_EVENT: dict[str, str] = {
    "workflow_state": "workflow_patch",
    "status": "workflow_patch",
    "route_status": "workflow_patch",
    "assist_skills_status": "workflow_patch",
    "architect_phase_status": "workflow_patch",
    "audit_summary": "audit_patch",
    "governance_card": "card_patch",
    "card_patch": "card_patch",
    "staged_edit_notice": "staged_edit_patch",
    "staged_edit_patch": "staged_edit_patch",
    "card_status_patch": "card_status_patch",
    "artifact_patch": "artifact_patch",
    "stale_patch": "stale_patch",
    "queue_window_patch": "queue_window_patch",
    "deep_summary": "deep_summary_patch",
    "deep_evidence": "evidence_patch",
}


@dataclass
class StudioPatchEnvelope:
    run_id: str
    run_version: int
    patch_seq: int
    patch_type: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def patch_type_for_event(event_name: str) -> str | None:
    return PATCH_TYPE_BY_EVENT.get(event_name)


def attach_run_context(
    payload: dict[str, Any] | None,
    *,
    run_id: str,
    run_version: int,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    base = dict(payload or {})
    base.setdefault("run_id", run_id)
    base.setdefault("run_version", run_version)
    if workflow_id:
        base.setdefault("workflow_id", workflow_id)

    metadata = base.get("metadata")
    next_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    run_metadata = next_metadata.get("run")
    merged_run_metadata = dict(run_metadata) if isinstance(run_metadata, dict) else {}
    merged_run_metadata.setdefault("run_id", run_id)
    merged_run_metadata.setdefault("run_version", run_version)
    next_metadata["run"] = merged_run_metadata
    if next_metadata:
        base["metadata"] = next_metadata
    return base


def build_patch_envelope(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    patch_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return StudioPatchEnvelope(
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type=patch_type,
        payload=attach_run_context(
            payload,
            run_id=run_id,
            run_version=run_version,
            workflow_id=run_id,
        ),
    ).to_dict()


def should_apply_patch(
    patch: dict[str, Any],
    *,
    active_run_id: str | None,
    active_run_version: int | None,
    applied_patch_seqs: set[int] | None = None,
) -> bool:
    if not isinstance(patch, dict):
        return False
    if active_run_id and str(patch.get("run_id") or "") != active_run_id:
        return False
    if active_run_version is not None and int(patch.get("run_version") or 0) != active_run_version:
        return False
    patch_seq = int(patch.get("patch_seq") or 0)
    if applied_patch_seqs and patch_seq in applied_patch_seqs:
        return False
    return patch_seq > 0
