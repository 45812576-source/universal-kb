"""Unified Skill Studio workflow protocol.

Phase 1 目标：
- 统一前后端事件 envelope
- 统一卡片 schema
- 统一 staged edit schema
- 统一动作结果 schema
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class WorkflowAction:
    label: str
    type: str
    payload: dict[str, Any] | None = None


@dataclass
class WorkflowCardData:
    id: str
    workflow_id: str | None
    source_type: str
    card_type: str
    phase: str
    title: str
    summary: str
    status: str = "pending"
    priority: str = "medium"
    target: dict[str, Any] = field(default_factory=dict)
    actions: list[WorkflowAction] = field(default_factory=list)
    content: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = data.pop("card_type")
        data["source"] = data.pop("source_type")
        data["content"] = {"summary": self.summary, **(self.content or {})}
        data["actions"] = [asdict(action) for action in self.actions]
        return data


@dataclass
class WorkflowStagedEditData:
    id: str
    workflow_id: str | None
    origin_card_id: str | None
    source_type: str
    target_type: str
    target_key: str | None
    summary: str
    risk_level: str
    diff_ops: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source_type,
            "workflow_id": self.workflow_id,
            "origin_card_id": self.origin_card_id,
            "target_type": self.target_type,
            "target_key": self.target_key,
            "summary": self.summary,
            "risk_level": self.risk_level,
            "diff_ops": self.diff_ops,
            "status": self.status,
        }


@dataclass
class WorkflowEventEnvelope:
    event_type: str
    workflow_id: str | None
    source_type: str
    phase: str
    payload: dict[str, Any]
    correlation_id: str = field(default_factory=lambda: _new_id("corr"))
    created_at: str = field(default_factory=_now_iso)
    skill_id: int | None = None
    conversation_id: int | None = None
    step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowStateData:
    workflow_id: str | None
    session_mode: str
    workflow_mode: str
    phase: str
    next_action: str
    complexity_level: str = "medium"
    execution_strategy: str = "fast_then_deep"
    fast_status: str = "pending"
    deep_status: str = "pending"
    route_reason: str = ""
    active_assist_skills: list[str] = field(default_factory=list)
    status: str = "active"
    skill_id: int | None = None
    conversation_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowActionResult:
    action_id: str
    ok: bool
    action: str
    card_id: str | None = None
    staged_edit_id: str | None = None
    updated_card_status: str | None = None
    updated_staged_edit_status: str | None = None
    workflow_state_patch: dict[str, Any] = field(default_factory=dict)
    memo_refresh_required: bool = False
    editor_refresh_required: bool = False
    next_cards: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
