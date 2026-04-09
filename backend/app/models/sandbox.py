"""交互式沙盒测试持久化模型。

四类核心对象：
- SandboxTestSession   — 测试会话（目标、版本、状态、步骤）
- SandboxTestEvidence  — Q1/Q2/Q3 结构化证据
- SandboxTestCase      — 单条测试用例（权限矩阵 × 输入）
- SandboxTestReport    — 不可变 snapshot + 知识库引用
"""
import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


# ─── 枚举 ────────────────────────────────────────────────────────────────────

class SessionStatus(str, enum.Enum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    READY_TO_RUN = "ready_to_run"
    RUNNING = "running"
    COMPLETED = "completed"
    CANNOT_TEST = "cannot_test"


class SessionStep(str, enum.Enum):
    START = "start"
    INPUT_SLOT_REVIEW = "input_slot_review"
    TOOL_REVIEW = "tool_review"
    PERMISSION_REVIEW = "permission_review"
    CASE_GENERATION = "case_generation"
    EXECUTION = "execution"
    EVALUATION = "evaluation"
    DONE = "done"


class SlotSourceKind(str, enum.Enum):
    CHAT_TEXT = "chat_text"
    KNOWLEDGE = "knowledge"
    DATA_TABLE = "data_table"
    SYSTEM_RUNTIME = "system_runtime"


class EvidenceType(str, enum.Enum):
    INPUT_SLOT = "input_slot"
    KNOWLEDGE_BINDING = "knowledge_binding"
    RAG_SAMPLE = "rag_sample"
    TOOL_PROVENANCE = "tool_provenance"
    PERMISSION_SNAPSHOT = "permission_snapshot"


class CaseVerdict(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


# ─── SandboxTestSession ─────────────────────────────────────────────────────

class SandboxTestSession(Base):
    """交互式沙盒测试会话。"""
    __tablename__ = "sandbox_test_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_type = Column(String(20), nullable=False)      # "skill" | "tool"
    target_id = Column(Integer, nullable=False)
    target_version = Column(Integer, nullable=True)        # 锁定的版本号
    target_name = Column(String(200), nullable=True)

    tester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(
        Enum(SessionStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=SessionStatus.DRAFT,
        nullable=False,
    )
    current_step = Column(
        Enum(SessionStep, values_callable=lambda obj: [e.value for e in obj]),
        default=SessionStep.START,
        nullable=False,
    )
    blocked_reason = Column(Text, nullable=True)

    # Q1 输入槽位来源确认（结构化）
    detected_slots = Column(JSON, default=list)
    """
    [{
        "slot_key": str,
        "label": str,
        "structured": bool,
        "required": bool,
        "allowed_sources": [str],
        "chosen_source": str | None,
        "evidence_status": "pending" | "verified" | "failed" | "not_applicable",
        "evidence_ref": str | None,
        "chat_example": str | None,
        "knowledge_entry_id": int | None,
        "table_name": str | None,
        "field_name": str | None,
    }]
    """

    # Q2 Tool 确认（结构化）
    tool_review = Column(JSON, default=list)
    """
    [{
        "tool_id": int,
        "tool_name": str,
        "description": str,
        "confirmed": bool,
        "input_provenance": [{
            "field_name": str,
            "source_kind": str,
            "source_ref": str,
            "resolved_value_preview": str | None,
            "verified": bool,
        }],
    }]
    """

    # Q3 权限快照确认（结构化）
    permission_snapshot = Column(JSON, default=list)
    """
    [{
        "table_name": str,
        "display_name": str,
        "row_visibility": str,      # own / dept / all / blocked
        "ownership_rules": dict,
        "field_masks": [{
            "field_name": str,
            "mask_action": str,
            "mask_params": dict,
        }],
        "groupable_fields": [str],
        "confirmed": bool,
        "included_in_test": bool,
    }]
    """

    # 测试矩阵统计
    theoretical_combo_count = Column(Integer, nullable=True)
    semantic_combo_count = Column(Integer, nullable=True)
    executed_case_count = Column(Integer, nullable=True)

    # 评价结论
    quality_passed = Column(Boolean, nullable=True)
    usability_passed = Column(Boolean, nullable=True)
    anti_hallucination_passed = Column(Boolean, nullable=True)
    approval_eligible = Column(Boolean, nullable=True)

    # 分段执行状态
    step_statuses = Column(JSON, default=dict)
    """
    {
      "case_generation": {"status": "completed", "started_at": "...", "finished_at": "...", "error": null},
      "case_execution": {"status": "failed", "started_at": "...", "error_code": "llm_timeout", "error_message": "...", "retryable": true},
      "evaluation": {...},
      "report_generation": {...},
      "memo_sync": {...},
    }
    """

    # targeted rerun 支持
    parent_session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id"), nullable=True)
    rerun_scope = Column(JSON, nullable=True)  # {"issue_ids": [...], "case_indices": [...]}

    # 报告引用
    report_id = Column(Integer, ForeignKey("sandbox_test_reports.id", use_alter=True), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # relationships
    tester = relationship("User", foreign_keys=[tester_id])
    evidences = relationship("SandboxTestEvidence", back_populates="session", cascade="all, delete-orphan")
    cases = relationship("SandboxTestCase", back_populates="session", cascade="all, delete-orphan")
    report = relationship("SandboxTestReport", foreign_keys=[report_id], post_update=True)


# ─── SandboxTestEvidence ─────────────────────────────────────────────────────

class SandboxTestEvidence(Base):
    """Q1/Q2/Q3 各步骤的结构化证据记录。"""
    __tablename__ = "sandbox_test_evidences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id"), nullable=False)

    evidence_type = Column(
        Enum(EvidenceType, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    step = Column(String(30), nullable=False)  # input_slot_review / tool_review / permission_review

    # 输入槽位证据
    slot_key = Column(String(100), nullable=True)
    source_kind = Column(
        Enum(SlotSourceKind, values_callable=lambda obj: [e.value for e in obj]),
        nullable=True,
    )
    source_ref = Column(Text, nullable=True)           # knowledge_entry_id / table.field / chat 示例文本
    resolved_value_preview = Column(Text, nullable=True)

    # 知识绑定证据
    knowledge_entry_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=True)
    rag_query = Column(Text, nullable=True)
    rag_expected_ids = Column(JSON, default=list)      # [knowledge_entry_id, ...]
    rag_actual_hits = Column(JSON, default=list)       # [{id, score, chunk_preview}, ...]
    rag_hit = Column(Boolean, nullable=True)

    # Tool provenance 证据
    tool_id = Column(Integer, ForeignKey("tool_registry.id"), nullable=True)
    field_name = Column(String(100), nullable=True)
    verified = Column(Boolean, nullable=True)

    # 权限快照证据
    table_name = Column(String(100), nullable=True)
    snapshot_data = Column(JSON, default=dict)         # 完整权限快照

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    session = relationship("SandboxTestSession", back_populates="evidences")


# ─── SandboxTestCase ─────────────────────────────────────────────────────────

class SandboxTestCase(Base):
    """单条测试用例（权限语义矩阵 × 真实输入）。"""
    __tablename__ = "sandbox_test_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id"), nullable=False)
    case_index = Column(Integer, nullable=False)

    # 权限维度
    row_visibility = Column(String(20), nullable=True)      # own / dept / all / blocked
    field_output_semantic = Column(String(50), nullable=True)  # keep / hide / partial / aggregate / range / remove
    group_semantic = Column(String(50), nullable=True)        # none / single_field / multi_field
    tool_precondition = Column(String(50), nullable=True)     # callable / precondition_failed

    # 输入 provenance
    input_provenance = Column(JSON, default=dict)
    """
    {
        "slot_key": "source_kind:source_ref",
        ...
    }
    """

    # 执行
    test_input = Column(Text, nullable=True)           # 实际发给 LLM 的 user message
    system_prompt_used = Column(Text, nullable=True)   # 含权限注入后的完整 prompt
    llm_response = Column(Text, nullable=True)
    execution_duration_ms = Column(Integer, nullable=True)

    # 判定
    verdict = Column(
        Enum(CaseVerdict, values_callable=lambda obj: [e.value for e in obj]),
        nullable=True,
    )
    verdict_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    session = relationship("SandboxTestSession", back_populates="cases")


# ─── SandboxTestReport ───────────────────────────────────────────────────────

class SandboxTestReport(Base):
    """不可变测试报告 snapshot + 知识库引用。"""
    __tablename__ = "sandbox_test_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sandbox_test_sessions.id"), nullable=False)

    target_type = Column(String(20), nullable=False)
    target_id = Column(Integer, nullable=False)
    target_version = Column(Integer, nullable=True)
    target_name = Column(String(200), nullable=True)
    tester_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # 报告三部分
    part1_evidence_check = Column(JSON, default=dict)   # Q1/Q2/Q3 检测结果摘要
    part2_test_matrix = Column(JSON, default=dict)      # 用例矩阵及执行结果
    part3_evaluation = Column(JSON, default=dict)       # 三项评价

    # 统计
    theoretical_combo_count = Column(Integer, nullable=True)
    semantic_combo_count = Column(Integer, nullable=True)
    executed_case_count = Column(Integer, nullable=True)

    # 通过状态
    quality_passed = Column(Boolean, nullable=True)
    usability_passed = Column(Boolean, nullable=True)
    anti_hallucination_passed = Column(Boolean, nullable=True)
    approval_eligible = Column(Boolean, nullable=True)

    # 不可变 hash
    report_hash = Column(String(64), nullable=True)

    # 知识库存证
    knowledge_entry_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    tester = relationship("User", foreign_keys=[tester_id])
    session_ref = relationship("SandboxTestSession", foreign_keys=[session_id])
