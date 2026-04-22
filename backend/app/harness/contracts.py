"""Hermes Harness 统一契约层。

冻结枚举、请求/响应模型、运行/步骤/制品/审批/记忆引用。
所有 Agent 入口最终都应构造 HarnessRequest 并通过 HarnessGateway.dispatch() 执行。
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 1: 冻结枚举
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AgentType(str, enum.Enum):
    """系统支持的 Agent 类型。新增 Agent 必须在此注册。"""
    CHAT = "chat"
    SKILL_STUDIO = "skill_studio"
    SANDBOX = "sandbox"
    DEV_STUDIO = "dev_studio"
    PROJECT = "project"


class WorkspaceType(str, enum.Enum):
    """工作台类型。与 Workspace.workspace_type 列对齐。"""
    CHAT = "chat"
    OPENCODE = "opencode"
    SANDBOX = "sandbox"
    SKILL_STUDIO = "skill_studio"
    PROJECT = "project"


class RunStatus(str, enum.Enum):
    """一次运行的生命周期状态。"""
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_TOOL = "waiting_tool"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class StepType(str, enum.Enum):
    """运行中单个步骤的类型。"""
    REQUEST_RECEIVED = "request_received"
    CONTEXT_ASSEMBLED = "context_assembled"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ARTIFACT_WRITTEN = "artifact_written"
    FALLBACK_APPLIED = "fallback_applied"
    OUTPUT_EMITTED = "output_emitted"
    CONTEXT_COMPRESSION = "context_compression"
    SECURITY_CHECK = "security_check"
    OUTPUT_FILTER = "output_filter"


class SecurityDecisionStatus(str, enum.Enum):
    """安全管线对单次操作的判定结果。"""
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


class ArtifactType(str, enum.Enum):
    """运行产出物类型。"""
    FILE = "file"
    REPORT = "report"
    CODE_DIFF = "code_diff"
    SANDBOX_EVIDENCE = "sandbox_evidence"
    HANDOFF = "handoff"


class ApprovalStatus(str, enum.Enum):
    """审批请求状态。"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 3: HarnessSessionKey
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class HarnessSessionKey:
    """会话隔离键 — 唯一标识一个 Agent 会话上下文。

    组合策略:
    - chat:         user_id + agent_type + workspace_id + conversation_id
    - skill_studio: user_id + agent_type + workspace_id + target_type + target_id
    - sandbox:      user_id + agent_type + target_type + target_id
    - dev_studio:   user_id + agent_type + workspace_id + project_id
    - project:      user_id + agent_type + project_id
    """
    user_id: int
    agent_type: AgentType
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None
    target_type: Optional[str] = None    # "skill" | "tool"
    target_id: Optional[int] = None
    conversation_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.agent_type == AgentType.SKILL_STUDIO:
            if self.target_type != "skill" or self.target_id is None:
                raise ValueError("skill_studio 要求 target_type='skill' 且 target_id 必填")
        if self.agent_type == AgentType.DEV_STUDIO:
            if self.workspace_id is None and self.project_id is None:
                raise ValueError("dev_studio 要求 workspace_id 或 project_id 至少一个存在")
        if self.agent_type == AgentType.PROJECT and self.project_id is None:
            raise ValueError("project 要求 project_id 必填")

    @property
    def compound_key(self) -> str:
        """返回用于索引的字符串键。"""
        parts = [str(self.user_id), self.agent_type.value]
        if self.workspace_id is not None:
            parts.append(f"ws:{self.workspace_id}")
        if self.project_id is not None:
            parts.append(f"proj:{self.project_id}")
        if self.target_type is not None:
            parts.append(f"t:{self.target_type}")
        if self.target_id is not None:
            parts.append(f"tid:{self.target_id}")
        if self.conversation_id is not None:
            parts.append(f"conv:{self.conversation_id}")
        return ":".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 2: 请求/响应/上下文 契约
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _new_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class HarnessContext:
    """请求携带的上下文信息 — 由入口适配器填充。"""
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None
    conversation_id: Optional[int] = None
    skill_id: Optional[int] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    # 额外上下文：工具列表、知识绑定、权限快照等
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessRequest:
    """统一入口请求 — 所有 Agent 入口最终都应转换为此对象。"""
    session_key: HarnessSessionKey
    agent_type: AgentType
    user_id: int
    input_text: str
    input_files: list[dict[str, Any]] = field(default_factory=list)
    context: HarnessContext = field(default_factory=HarnessContext)
    # 流式/同步模式
    stream: bool = True
    # 沙盒模式
    sandbox_mode: bool = False
    # 请求级元数据（模型偏好、附件等）
    metadata: dict[str, Any] = field(default_factory=dict)
    # 唯一请求 ID
    request_id: str = field(default_factory=_new_id)

    @property
    def user_message(self) -> str:
        """兼容旧调用方命名。"""
        return self.input_text


@dataclass
class HarnessResponse:
    """统一响应 — 一次运行完成后的最终结果。"""
    request_id: str
    run_id: str
    status: RunStatus
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 运行/步骤/制品/审批/记忆引用
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class HarnessSession:
    """统一逻辑会话记录。"""
    session_id: str = field(default_factory=_new_id)
    session_key: Optional[HarnessSessionKey] = None
    agent_type: Optional[AgentType] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessRun:
    """一次用户请求触发的运行。"""
    run_id: str = field(default_factory=_new_id)
    request_id: str = ""
    session_id: Optional[str] = None
    session_key: Optional[HarnessSessionKey] = None
    agent_type: Optional[AgentType] = None
    status: RunStatus = RunStatus.CREATED
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessStep:
    """运行中的单个步骤。"""
    step_id: str = field(default_factory=_new_id)
    run_id: str = ""
    step_type: StepType = StepType.MODEL_CALL
    seq: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    input_summary: str = ""
    output_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class HarnessArtifact:
    """运行产出物。"""
    artifact_id: str = field(default_factory=_new_id)
    run_id: str = ""
    artifact_type: ArtifactType = ArtifactType.FILE
    name: str = ""
    content_ref: str = ""   # 文件路径、OSS key、或内联内容
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class HarnessApproval:
    """审批请求与结果。"""
    approval_id: str = field(default_factory=_new_id)
    run_id: str = ""
    step_id: str = ""
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: Optional[int] = None  # user_id
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessMemoryRef:
    """运行引用的记忆/知识/上下文。"""
    ref_id: str = field(default_factory=_new_id)
    run_id: str = ""
    ref_type: str = ""  # "conversation_history" | "knowledge_entry" | "project_context" | "user_memory" | "studio_state"
    ref_source_id: Optional[int] = None  # 具体记录 ID
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
