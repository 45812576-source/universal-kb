"""Unified Skill Studio workflow protocol.

Phase 1 目标：
- 统一前后端事件 envelope
- 统一卡片 schema
- 统一 staged edit schema
- 统一动作结果 schema

Phase 2 扩展（统一架构）：
- active_card_id / workspace / test_flow / validation_source / global_constraints
- 标准化事件类型
- Studio session 聚合响应协议
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


# ── 事件类型常量 ─────────────────────────────────────────────────────────────

class StudioEventTypes:
    """统一架构标准化事件类型。"""

    # card 生命周期
    CARD_ACTIVATED = "card_activated"
    CARD_PAUSED = "card_paused"
    CARD_CREATED = "card_created"
    CARD_UPDATED = "card_updated"
    CARD_CONTEXT_APPENDED = "card_context_appended"

    # workspace
    WORKSPACE_CHANGED = "workspace_changed"

    # test flow
    TEST_FLOW_UPDATED = "test_flow_updated"

    # validation
    VALIDATION_CARD_CREATED = "validation_card_created"
    SANDBOX_REPORT_LINKED = "sandbox_report_linked"

    # memo / session
    MEMO_INITIALIZED = "memo_initialized"
    MEMO_UPDATED = "memo_updated"
    PHASE_CHANGED = "phase_changed"
    BLUEPRINT_UPDATED = "blueprint_updated"
    STAGED_CHANGE_CREATED = "staged_change_created"
    STAGED_CHANGE_UPDATED = "staged_change_updated"
    USER_DECISION_RECORDED = "user_decision_recorded"
    VALIDATION_REPORT_READY = "validation_report_ready"
    GLOBAL_CONSTRAINTS_UPDATED = "global_constraints_updated"


# ── 卡片状态常量 ─────────────────────────────────────────────────────────────

class CardStatus:
    """统一卡片状态枚举。"""
    DETECTED = "detected"
    QUEUED = "queued"
    ACTIVE = "active"
    DRAFTING = "drafting"
    DIFF_READY = "diff_ready"
    REVIEWING = "reviewing"
    REVISION_NEEDED = "revision_needed"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    VALIDATED = "validated"
    PAUSED = "paused"
    REJECTED = "rejected"
    # 兼容旧状态
    PENDING = "pending"
    ADOPTED = "adopted"


# ── workspace 模式常量 ───────────────────────────────────────────────────────

class WorkspaceMode:
    """工作区模式枚举。"""
    ANALYSIS = "analysis"
    FILE = "file"
    REPORT = "report"


# ── 基础协议对象 ─────────────────────────────────────────────────────────────

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
    # ── 统一架构扩展字段 ──
    workspace_mode: str | None = None
    target_file: str | None = None
    related_task_ids: list[str] = field(default_factory=list)
    validation_source: dict[str, Any] | None = None
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = data.pop("card_type")
        data["source"] = data.pop("source_type")
        data["content"] = {"summary": self.summary, **(self.content or {})}
        data["actions"] = [asdict(action) for action in self.actions]
        # 统一架构扩展字段：仅在有值时输出，保持旧 API 响应干净
        for _k in ("workspace_mode", "target_file", "validation_source", "origin"):
            if data.get(_k) is None:
                data.pop(_k, None)
        if not data.get("related_task_ids"):
            data.pop("related_task_ids", None)
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
    # ── 统一架构扩展 ──
    card_id: str | None = None
    memo_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for _k in ("card_id", "memo_version"):
            if data.get(_k) is None:
                data.pop(_k, None)
        return data


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
    # ── 统一架构扩展字段 ──
    active_card_id: str | None = None
    workspace_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 统一架构扩展字段：仅在有值时输出
        for _k in ("active_card_id", "workspace_mode"):
            if data.get(_k) is None:
                data.pop(_k, None)
        return data


@dataclass
class WorkflowActionResult:
    action_id: str
    ok: bool
    action: str
    card_id: str | None = None
    staged_edit_id: str | None = None
    target_type: str | None = None
    target_key: str | None = None
    updated_card_status: str | None = None
    updated_staged_edit_status: str | None = None
    workflow_state_patch: dict[str, Any] = field(default_factory=dict)
    memo_refresh_required: bool = False
    editor_refresh_required: bool = False
    recovery_source: str | None = None
    recovery_revision: int | None = None
    recovery_updated_at: str | None = None
    next_cards: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # ── 统一架构扩展 ──
    workspace_patch: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data.get("workspace_patch"):
            data.pop("workspace_patch", None)
        return data


# ── 统一架构新增协议对象 ─────────────────────────────────────────────────────

@dataclass
class WorkspaceTarget:
    """工作区目标引用。"""
    type: str  # source_file / report / analysis
    key: str  # 文件路径、report_id、分析类型

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceData:
    """工作区状态 — 由后端决策，前端直接消费。"""
    mode: str = WorkspaceMode.FILE
    primary_target: dict[str, Any] | None = None
    related_targets: list[dict[str, Any]] = field(default_factory=list)
    report_ref: str | None = None
    governance_drawer_state: str = "closed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestFlowSummary:
    """test flow 概览 — studio session 聚合响应中使用。"""
    phase: str = "idle"
    entry_source: str | None = None
    matched_skill_ids: list[int] = field(default_factory=list)
    blocking_issues: list[dict[str, Any]] = field(default_factory=list)
    current_plan_id: int | None = None
    current_plan_version: int | None = None
    latest_session_id: int | None = None
    latest_report_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationSource:
    """验证来源摘要。"""
    type: str | None = None  # preflight / sandbox / targeted_retest
    session_id: int | None = None
    report_id: int | None = None
    plan_id: int | None = None
    plan_version: int | None = None
    status: str | None = None  # pass / fail / pending
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StudioSessionData:
    """Studio session 聚合响应 — GET /studio/session 返回的顶层结构。"""
    skill_id: int
    workflow_state: dict[str, Any] | None = None
    active_card_id: str | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    staged_edits: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    test_flow: dict[str, Any] = field(default_factory=dict)
    validation_source: dict[str, Any] | None = None
    global_constraints: list[str] = field(default_factory=list)
    recovery_revision: int = 0
    recovery_updated_at: str | None = None
    memo_version: int = 0
    lifecycle_stage: str = "analysis"
    status_summary: str = ""
    context_rollups: list[dict[str, Any]] = field(default_factory=list)
    blueprint: dict[str, Any] | None = None
    card_order: list[str] = field(default_factory=list)
    progress_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 仅在有值时输出
        if not data.get("context_rollups"):
            data.pop("context_rollups", None)
        if data.get("blueprint") is None:
            data.pop("blueprint", None)
        if not data.get("progress_log"):
            data.pop("progress_log", None)
        return data
