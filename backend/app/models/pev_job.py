"""PEV (Plan-Execute-Verify) Job 和 Step 数据模型。"""
import datetime
import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class PEVJobStatus(str, enum.Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PEVStepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PEVJob(Base):
    __tablename__ = "pev_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(
        Enum(PEVJobStatus, values_callable=lambda x: [e.value for e in x]),
        default=PEVJobStatus.PLANNING,
        nullable=False,
    )
    scenario = Column(String(50), nullable=False)  # "intel" / "skill_chain" / "task_decomp"
    goal = Column(Text, nullable=False)
    plan = Column(JSON, default=dict)          # 结构化计划（steps 数组）
    context = Column(JSON, default=dict)       # 跨步骤累积的结果上下文
    config = Column(JSON, default=dict)        # skip_verify, max_retries 等

    # 关联
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    intel_task_id = Column(Integer, ForeignKey("intel_tasks.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # 进度
    total_steps = Column(Integer, default=0)
    completed_steps = Column(Integer, default=0)
    current_step_index = Column(Integer, default=0)

    # 时间
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # 关系
    steps = relationship("PEVStep", back_populates="job", order_by="PEVStep.order_index")
    user = relationship("User", foreign_keys=[user_id])


class PEVStep(Base):
    __tablename__ = "pev_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("pev_jobs.id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    step_key = Column(String(100), nullable=False)
    step_type = Column(String(50), nullable=False)
    # "llm_generate" / "tool_call" / "crawl" / "sub_task" / "skill_execute"
    description = Column(Text, nullable=True)
    depends_on = Column(JSON, default=list)    # 依赖的 step_key 列表
    input_spec = Column(JSON, default=dict)    # 输入规格，支持 $step_key.field 引用
    output_spec = Column(JSON, default=dict)   # 期望输出 schema（供 verify 用）
    verify_criteria = Column(Text, nullable=True)  # 自然语言验证标准
    status = Column(
        Enum(PEVStepStatus, values_callable=lambda x: [e.value for e in x]),
        default=PEVStepStatus.PENDING,
        nullable=False,
    )
    result = Column(JSON, nullable=True)
    verify_result = Column(JSON, nullable=True)
    retry_count = Column(Integer, default=0)

    # 关系
    job = relationship("PEVJob", back_populates="steps")
