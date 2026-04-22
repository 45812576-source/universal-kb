"""Hermes Harness 统一事件协议。

所有 SSE 事件和日志事件都应通过 HarnessEvent 发出，
确保前端、日志系统、replay 都消费同一语义。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventCategory(str, Enum):
    """事件大类 — 决定事件的消费方和处理优先级。"""
    LIFECYCLE = "lifecycle"     # run 生命周期: created, running, completed, failed
    STREAM = "stream"           # 流式输出: delta, content_block_start/stop, replace
    STATUS = "status"           # 阶段状态: preparing, generating, tool_calling
    SECURITY = "security"       # 安全管线: approval_request, security_decision
    ARTIFACT = "artifact"       # 产出物: file, report, code_diff
    STUDIO = "studio"           # Skill Studio 专属: architect_phase, audit, governance
    ERROR = "error"             # 错误


# 将现有散落的 SSE event name 收敛为枚举
class EventName(str, Enum):
    """全系统 SSE 事件名枚举。新增事件必须在此注册。"""
    # lifecycle
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"

    # stream (兼容现有前端)
    DELTA = "delta"
    REPLACE = "replace"
    DONE = "done"
    CONTENT_BLOCK_START = "content_block_start"
    CONTENT_BLOCK_DELTA = "content_block_delta"
    CONTENT_BLOCK_STOP = "content_block_stop"
    FALLBACK_TEXT = "fallback_text"

    # status
    STATUS = "status"
    ROUTE_STATUS = "route_status"
    ASSIST_SKILLS_STATUS = "assist_skills_status"

    # security
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECIDED = "approval_decided"
    SECURITY_DECISION = "security_decision"

    # studio architect
    ARCHITECT_PHASE_STATUS = "architect_phase_status"
    ARCHITECT_QUESTION = "architect_question"
    ARCHITECT_PHASE_SUMMARY = "architect_phase_summary"
    ARCHITECT_STRUCTURE = "architect_structure"
    ARCHITECT_PRIORITY_MATRIX = "architect_priority_matrix"
    ARCHITECT_OODA_DECISION = "architect_ooda_decision"
    ARCHITECT_READY_FOR_DRAFT = "architect_ready_for_draft"

    # studio governance
    AUDIT_SUMMARY = "audit_summary"
    GOVERNANCE_CARD = "governance_card"
    STAGED_EDIT_NOTICE = "staged_edit_notice"

    # studio card orchestration
    CARD_PATCH = "card_patch"
    CARD_STATUS_PATCH = "card_status_patch"
    ARTIFACT_PATCH = "artifact_patch"
    STALE_PATCH = "stale_patch"
    QUEUE_WINDOW_PATCH = "queue_window_patch"

    # pev
    PEV_ERROR = "pev_error"

    # error
    ERROR = "error"


# event name -> category 映射
_CATEGORY_MAP: dict[EventName, EventCategory] = {
    EventName.RUN_CREATED: EventCategory.LIFECYCLE,
    EventName.RUN_STARTED: EventCategory.LIFECYCLE,
    EventName.RUN_COMPLETED: EventCategory.LIFECYCLE,
    EventName.RUN_FAILED: EventCategory.LIFECYCLE,
    EventName.RUN_CANCELLED: EventCategory.LIFECYCLE,
    EventName.DELTA: EventCategory.STREAM,
    EventName.REPLACE: EventCategory.STREAM,
    EventName.DONE: EventCategory.STREAM,
    EventName.CONTENT_BLOCK_START: EventCategory.STREAM,
    EventName.CONTENT_BLOCK_DELTA: EventCategory.STREAM,
    EventName.CONTENT_BLOCK_STOP: EventCategory.STREAM,
    EventName.FALLBACK_TEXT: EventCategory.STREAM,
    EventName.STATUS: EventCategory.STATUS,
    EventName.ROUTE_STATUS: EventCategory.STATUS,
    EventName.ASSIST_SKILLS_STATUS: EventCategory.STATUS,
    EventName.APPROVAL_REQUEST: EventCategory.SECURITY,
    EventName.APPROVAL_DECIDED: EventCategory.SECURITY,
    EventName.SECURITY_DECISION: EventCategory.SECURITY,
    EventName.ARCHITECT_PHASE_STATUS: EventCategory.STUDIO,
    EventName.ARCHITECT_QUESTION: EventCategory.STUDIO,
    EventName.ARCHITECT_PHASE_SUMMARY: EventCategory.STUDIO,
    EventName.ARCHITECT_STRUCTURE: EventCategory.STUDIO,
    EventName.ARCHITECT_PRIORITY_MATRIX: EventCategory.STUDIO,
    EventName.ARCHITECT_OODA_DECISION: EventCategory.STUDIO,
    EventName.ARCHITECT_READY_FOR_DRAFT: EventCategory.STUDIO,
    EventName.AUDIT_SUMMARY: EventCategory.STUDIO,
    EventName.GOVERNANCE_CARD: EventCategory.STUDIO,
    EventName.STAGED_EDIT_NOTICE: EventCategory.STUDIO,
    EventName.CARD_PATCH: EventCategory.STUDIO,
    EventName.CARD_STATUS_PATCH: EventCategory.STUDIO,
    EventName.ARTIFACT_PATCH: EventCategory.STUDIO,
    EventName.STALE_PATCH: EventCategory.STUDIO,
    EventName.QUEUE_WINDOW_PATCH: EventCategory.STUDIO,
    EventName.PEV_ERROR: EventCategory.ERROR,
    EventName.ERROR: EventCategory.ERROR,
}


@dataclass
class HarnessEvent:
    """统一事件对象。可序列化为 SSE 文本或写入日志/replay。"""
    event: EventName
    data: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    step_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def category(self) -> EventCategory:
        return _CATEGORY_MAP.get(self.event, EventCategory.ERROR)

    @property
    def event_type(self) -> str:
        return self.event.value

    def to_sse(self) -> str:
        """序列化为 SSE 文本格式 — 与现有 _sse() 兼容。"""
        payload = {**self.data}
        if self.run_id:
            payload.setdefault("_run_id", self.run_id)
        if self.session_id:
            payload.setdefault("_session_id", self.session_id)
        if self.step_id:
            payload.setdefault("_step_id", self.step_id)
        return f"event: {self.event.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict — 用于日志和 replay 存储。"""
        return {
            "event_id": self.event_id,
            "event": self.event.value,
            "event_type": self.event.value,
            "category": self.category.value,
            "data": self.data,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "step_id": self.step_id,
            "timestamp": self.timestamp,
        }


def emit(event: EventName, data: dict[str, Any], *,
         run_id: Optional[str] = None,
         session_id: Optional[str] = None,
         step_id: Optional[str] = None) -> HarnessEvent:
    """快捷构造 HarnessEvent。"""
    return HarnessEvent(event=event, data=data, run_id=run_id, session_id=session_id, step_id=step_id)


def emit_sse(event: EventName, data: dict[str, Any], *,
             run_id: Optional[str] = None,
             session_id: Optional[str] = None,
             step_id: Optional[str] = None) -> str:
    """快捷构造并直接返回 SSE 文本 — 可直接替换现有 _sse() 调用。"""
    return emit(event, data, run_id=run_id, session_id=session_id, step_id=step_id).to_sse()
