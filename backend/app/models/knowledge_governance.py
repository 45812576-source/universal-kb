import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class GovernanceObjective(Base):
    __tablename__ = "governance_objectives"
    __table_args__ = (
        UniqueConstraint("parent_id", "code", name="uq_governance_objective_parent_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    code = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    level = Column(String(30), default="company")  # company | function | department | kr
    parent_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    business_line = Column(String(100), nullable=True)
    objective_role = Column(String(50), nullable=True)  # strategy | kr | enablement | execution
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    parent = relationship("GovernanceObjective", remote_side=[id], back_populates="children")
    children = relationship("GovernanceObjective", back_populates="parent")


class GovernanceDepartmentMission(Base):
    __tablename__ = "governance_department_missions"
    __table_args__ = (
        UniqueConstraint("department_id", "code", name="uq_governance_department_mission_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    name = Column(String(200), nullable=False)
    code = Column(String(100), nullable=False)
    core_role = Column(Text, nullable=True)
    mission_statement = Column(Text, nullable=True)
    upstream_dependencies = Column(JSON, default=list)
    downstream_deliverables = Column(JSON, default=list)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceKR(Base):
    __tablename__ = "governance_krs"
    __table_args__ = (
        UniqueConstraint("mission_id", "code", name="uq_governance_kr_mission_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mission_id = Column(Integer, ForeignKey("governance_department_missions.id"), nullable=False)
    objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    name = Column(String(200), nullable=False)
    code = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    metric_definition = Column(Text, nullable=True)
    target_value = Column(String(100), nullable=True)
    time_horizon = Column(String(50), nullable=True)
    owner_role = Column(String(100), nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceRequiredElement(Base):
    __tablename__ = "governance_required_elements"
    __table_args__ = (
        UniqueConstraint("kr_id", "code", name="uq_governance_required_element_kr_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kr_id = Column(Integer, ForeignKey("governance_krs.id"), nullable=False)
    name = Column(String(200), nullable=False)
    code = Column(String(100), nullable=False)
    element_type = Column(String(50), default="resource")  # resource | role | capability | external_signal | process
    description = Column(Text, nullable=True)
    required_library_codes = Column(JSON, default=list)
    required_object_types = Column(JSON, default=list)
    suggested_update_cycle = Column(String(30), nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceResourceLibrary(Base):
    __tablename__ = "governance_resource_libraries"
    __table_args__ = (
        UniqueConstraint("objective_id", "code", name="uq_governance_library_objective_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=False)
    name = Column(String(200), nullable=False)
    code = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    library_type = Column(String(50), default="resource_library")
    object_type = Column(String(50), nullable=False)  # customer | sop_ticket | case | external_intel | skill_material
    governance_mode = Column(String(20), default="ab_fusion")  # rules | ai | ab_fusion
    default_visibility = Column(String(20), default="read")
    default_update_cycle = Column(String(30), nullable=True)  # realtime | daily | weekly | manual
    field_schema = Column(JSON, default=list)
    consumption_scenarios = Column(JSON, default=list)
    collaboration_baseline = Column(JSON, default=dict)
    consumer_departments = Column(JSON, default=list)  # 消费该资源库的部门 ID 列表
    dependency_library_codes = Column(JSON, default=list)  # 上下游依赖的资源库 code 列表
    classification_hints = Column(JSON, default=dict)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    objective = relationship("GovernanceObjective", foreign_keys=[objective_id])


class GovernanceObjectType(Base):
    __tablename__ = "governance_object_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(100), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    dimension_schema = Column(JSON, default=list)
    baseline_fields = Column(JSON, default=list)
    default_consumption_modes = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceFieldTemplate(Base):
    __tablename__ = "governance_field_templates"
    __table_args__ = (
        UniqueConstraint("object_type_id", "field_key", name="uq_governance_field_template_object_field"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    object_type_id = Column(Integer, ForeignKey("governance_object_types.id"), nullable=False)
    field_key = Column(String(100), nullable=False)
    field_label = Column(String(200), nullable=False)
    field_type = Column(String(50), default="text")
    is_required = Column(Boolean, default=False)
    is_editable = Column(Boolean, default=True)
    visibility_mode = Column(String(20), default="read")  # read | edit | restricted
    update_cycle = Column(String(30), nullable=True)  # realtime | daily | weekly | manual
    consumer_modes = Column(JSON, default=list)
    description = Column(Text, nullable=True)
    example_values = Column(JSON, default=list)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    object_type = relationship("GovernanceObjectType", foreign_keys=[object_type_id])


class GovernanceSuggestionTask(Base):
    __tablename__ = "governance_suggestion_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_type = Column(String(50), nullable=False)  # knowledge | business_table | project | task
    subject_id = Column(Integer, nullable=False)
    task_type = Column(String(50), nullable=False)  # classify | align_library | fix_fields | sync_baseline
    status = Column(String(20), default="pending")  # pending | accepted | rejected | applied
    objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    resource_library_id = Column(Integer, ForeignKey("governance_resource_libraries.id"), nullable=True)
    object_type_id = Column(Integer, ForeignKey("governance_object_types.id"), nullable=True)
    suggested_payload = Column(JSON, default=dict)
    reason = Column(Text, nullable=True)
    confidence = Column(Integer, default=0)  # 0-100
    auto_applied = Column(Boolean, default=False)  # 是否由引擎自动生效
    candidates_payload = Column(JSON, nullable=True)  # top-2 候选 + 证据（供人审时展示）
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    objective = relationship("GovernanceObjective", foreign_keys=[objective_id])
    resource_library = relationship("GovernanceResourceLibrary", foreign_keys=[resource_library_id])
    object_type = relationship("GovernanceObjectType", foreign_keys=[object_type_id])


class GovernanceFeedbackEvent(Base):
    __tablename__ = "governance_feedback_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    suggestion_id = Column(Integer, ForeignKey("governance_suggestion_tasks.id"), nullable=True)
    subject_type = Column(String(50), nullable=False)
    subject_id = Column(Integer, nullable=False)
    strategy_key = Column(String(200), nullable=False)
    event_type = Column(String(50), nullable=False)  # applied | rejected | corrected | reverted
    reward_score = Column(Integer, default=0)  # scaled by 100
    from_objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    from_resource_library_id = Column(Integer, ForeignKey("governance_resource_libraries.id"), nullable=True)
    to_objective_id = Column(Integer, ForeignKey("governance_objectives.id"), nullable=True)
    to_resource_library_id = Column(Integer, ForeignKey("governance_resource_libraries.id"), nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class GovernanceStrategyStat(Base):
    __tablename__ = "governance_strategy_stats"
    __table_args__ = (
        UniqueConstraint("strategy_key", name="uq_governance_strategy_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_key = Column(String(200), nullable=False)
    strategy_group = Column(String(100), nullable=False)
    subject_type = Column(String(50), nullable=True)
    objective_code = Column(String(100), nullable=True)
    library_code = Column(String(100), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    business_line = Column(String(100), nullable=True)
    is_frozen = Column(Boolean, default=False)
    manual_bias = Column(Integer, default=0)
    total_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    reject_count = Column(Integer, default=0)
    cumulative_reward = Column(Integer, default=0)
    last_reward = Column(Integer, default=0)
    last_event_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceObject(Base):
    __tablename__ = "governance_objects"
    __table_args__ = (
        UniqueConstraint("object_type_id", "canonical_key", name="uq_governance_object_type_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    object_type_id = Column(Integer, ForeignKey("governance_object_types.id"), nullable=False)
    canonical_key = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=False)
    business_line = Column(String(100), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    lifecycle_status = Column(String(30), default="active")  # active | draft | deprecated | archived
    object_payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class GovernanceBaselineSnapshot(Base):
    """基线版本快照：记录治理体系的全量骨架 + 统计指标。

    每次重大变更（初始化、治理轮次完成、增量迭代）都会创建一个版本。
    """
    __tablename__ = "governance_baseline_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_id = Column(Integer, ForeignKey("governance_resource_libraries.id"), nullable=True)  # 兼容旧用法，新版本可为 NULL
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    change_type = Column(String(50), nullable=False)  # field_update | consumer_change | dependency_change | cycle_change | init | governance_round | steady_state | incremental | gap_fill

    # ── Phase 3 新增字段 ──────────────────────────────────────────────────────
    version = Column(String(20), nullable=True)  # 语义化版本号：v0.1, v0.2, v1.0, v1.1
    version_type = Column(String(30), nullable=True)  # init | governance_round | steady_state | incremental | gap_fill
    snapshot_data = Column(JSON, nullable=True)  # 全量骨架快照（objectives, libraries, object_types, strategies）
    stats_data = Column(JSON, nullable=True)  # 统计指标（分类覆盖率、置信度分布、缺口数）
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=False)  # 当前激活的基线版本

    old_value = Column(JSON, default=dict)
    new_value = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class GovernanceExperiment(Base):
    """灰度实验：在指定部门以候选阈值运行 N 天，对比自动通过率/人审量/误判率。"""
    __tablename__ = "governance_experiments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    department_ids = Column(JSON, default=list)  # 灰度覆盖的部门 ID 列表
    threshold = Column(Integer, nullable=False)  # 候选阈值
    baseline_threshold = Column(Integer, nullable=False)  # 对照组阈值（当前全局值）
    duration_days = Column(Integer, default=7)
    status = Column(String(20), default="running")  # running | completed | applied | cancelled
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    result_payload = Column(JSON, nullable=True)  # 实验结论指标
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class GovernanceObjectFacet(Base):
    __tablename__ = "governance_object_facets"
    __table_args__ = (
        UniqueConstraint("governance_object_id", "resource_library_id", "facet_key", name="uq_governance_object_facet"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    governance_object_id = Column(Integer, ForeignKey("governance_objects.id"), nullable=False)
    resource_library_id = Column(Integer, ForeignKey("governance_resource_libraries.id"), nullable=False)
    facet_key = Column(String(100), nullable=False)
    facet_name = Column(String(200), nullable=False)
    field_values = Column(JSON, default=dict)
    consumer_scenarios = Column(JSON, default=list)
    visibility_mode = Column(String(20), default="read")
    is_editable = Column(Boolean, default=False)
    update_cycle = Column(String(30), nullable=True)
    source_subjects = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
