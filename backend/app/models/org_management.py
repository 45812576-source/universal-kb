"""组织管理模块数据模型 — 11 张新表"""

import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


# ── 导入会话 ─────────────────────────────────────────────────────────────────

class OrgImportSession(Base):
    """每次表格导入为一个 session，AI 整理后产出结构化数据，人 confirm 后写入。"""
    __tablename__ = "org_import_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_type = Column(String(30), nullable=False)  # org_structure/roster/okr/kpi/dept_mission/biz_process/terminology/data_asset/collab_matrix/access_matrix
    file_name = Column(String(500), nullable=True)
    file_path = Column(String(500), nullable=True)
    raw_data = Column(JSON, nullable=True)  # pandas 解析后的原始数据
    ai_parsed_data = Column(JSON, nullable=True)  # AI 整理后的结构化数据
    ai_parse_note = Column(Text, nullable=True)  # AI 整理说明
    status = Column(String(20), default="uploading")  # uploading/parsing/parsed/confirmed/applied/failed
    row_count = Column(Integer, default=0)
    parsed_count = Column(Integer, default=0)
    error_rows = Column(JSON, default=list)  # [{row, reason}]
    applied_at = Column(DateTime, nullable=True)
    baseline_snapshot_id = Column(Integer, ForeignKey("governance_baseline_snapshots.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ── 组织变更事件 ──────────────────────────────────────────────────────────────

class OrgChangeEvent(Base):
    """所有组织数据变更都记录到这张表，支持时序回溯。"""
    __tablename__ = "org_change_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(50), nullable=False)  # department/user/position/okr_period/okr_objective/okr_key_result/kpi_assignment/dept_mission/biz_process/terminology/data_asset/collab_link/access_rule
    entity_id = Column(Integer, nullable=False)
    change_type = Column(String(20), nullable=False)  # created/updated/deleted/imported/confirmed
    field_changes = Column(JSON, default=list)  # [{field, old_value, new_value}]
    change_source = Column(String(20), default="manual")  # import/manual/ai_suggest/baseline_sync
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    baseline_version = Column(String(20), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ── OKR 周期管理 ─────────────────────────────────────────────────────────────

class OkrPeriod(Base):
    """OKR 周期"""
    __tablename__ = "okr_periods"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # 如 "2026 Q2"
    period_type = Column(String(20), nullable=False)  # quarter/half_year/year
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(20), default="draft")  # draft/active/evaluating/archived
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class OkrObjective(Base):
    """O（目标），全员粒度"""
    __tablename__ = "okr_objectives"

    id = Column(Integer, primary_key=True, autoincrement=True)
    period_id = Column(Integer, ForeignKey("okr_periods.id"), nullable=False)
    owner_type = Column(String(20), nullable=False)  # company/department/user
    owner_id = Column(Integer, default=0)  # company=0, department=dept_id, user=user_id
    parent_objective_id = Column(Integer, ForeignKey("okr_objectives.id"), nullable=True)
    title = Column(String(500), nullable=False)
    weight = Column(Float, default=1.0)
    progress = Column(Float, default=0)  # 0-100
    status = Column(String(20), default="draft")  # draft/active/completed/cancelled
    sort_order = Column(Integer, default=0)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    parent = relationship("OkrObjective", remote_side=[id])
    key_results = relationship("OkrKeyResult", back_populates="objective", cascade="all, delete-orphan")


class OkrKeyResult(Base):
    """KR（关键结果），挂在 O 下"""
    __tablename__ = "okr_key_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    objective_id = Column(Integer, ForeignKey("okr_objectives.id"), nullable=False)
    title = Column(String(500), nullable=False)
    metric_type = Column(String(20), default="number")  # number/percentage/boolean/milestone
    target_value = Column(String(100), nullable=True)
    current_value = Column(String(100), nullable=True)
    unit = Column(String(50), nullable=True)  # 万元、%、个
    weight = Column(Float, default=1.0)
    progress = Column(Float, default=0)  # 0-100
    status = Column(String(20), default="on_track")  # on_track/at_risk/behind/completed
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sort_order = Column(Integer, default=0)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    objective = relationship("OkrObjective", back_populates="key_results")


# ── 绩效 KPI ─────────────────────────────────────────────────────────────────

class KpiAssignment(Base):
    """KPI 分配（每人每周期）"""
    __tablename__ = "kpi_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("okr_periods.id"), nullable=False)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)  # 当时的岗位
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)  # 当时的部门
    kpi_data = Column(JSON, default=list)  # [{name, weight, target, actual, score, metric_type, unit}]
    total_score = Column(Float, nullable=True)
    level = Column(String(10), nullable=True)  # S/A/B/C/D
    evaluator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(20), default="draft")  # draft/submitted/evaluated/confirmed
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 部门职责详情 ──────────────────────────────────────────────────────────────

class DeptMissionDetail(Base):
    """部门职责详情（增强 GovernanceDepartmentMission）"""
    __tablename__ = "dept_mission_details"

    id = Column(Integer, primary_key=True, autoincrement=True)
    department_id = Column(Integer, ForeignKey("departments.id"), unique=True, nullable=False)
    mission_summary = Column(Text, nullable=True)  # 部门使命一句话
    core_functions = Column(JSON, default=list)  # [{name, description}]
    upstream_deps = Column(JSON, default=list)  # [{dept_id, what_receive}]
    downstream_deliveries = Column(JSON, default=list)  # [{dept_id, what_deliver}]
    owned_data_types = Column(JSON, default=list)  # 该部门"拥有"的数据类型
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 业务流程 ──────────────────────────────────────────────────────────────────

class BizProcess(Base):
    """业务流程"""
    __tablename__ = "biz_processes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    code = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    process_nodes = Column(JSON, default=list)  # [{order, name, dept_id, position_id, input_data, output_data}]
    is_active = Column(Boolean, default=True)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 业务术语 ──────────────────────────────────────────────────────────────────

class BizTerminology(Base):
    """业务术语"""
    __tablename__ = "biz_terminologies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    term = Column(String(200), nullable=False)
    aliases = Column(JSON, default=list)  # 同义词/变体列表
    definition = Column(Text, nullable=True)
    resource_library_code = Column(String(100), nullable=True)  # 归属资源库 code
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 数据资产归属 ──────────────────────────────────────────────────────────────

class DataAssetOwnership(Base):
    """数据资产归属"""
    __tablename__ = "data_asset_ownerships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_name = Column(String(200), nullable=False)
    asset_code = Column(String(100), unique=True, nullable=False)
    owner_department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    update_frequency = Column(String(20), default="manual")  # realtime/daily/weekly/monthly/manual
    consumer_department_ids = Column(JSON, default=list)  # 消费部门 ID 列表
    resource_library_code = Column(String(100), nullable=True)  # 关联的治理资源库
    description = Column(Text, nullable=True)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 跨部门协作 ────────────────────────────────────────────────────────────────

class DeptCollaborationLink(Base):
    """跨部门协作频率"""
    __tablename__ = "dept_collaboration_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dept_a_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    dept_b_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    frequency = Column(String(10), default="medium")  # high/medium/low
    scenarios = Column(JSON, default=list)  # 典型协作场景描述
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ── 岗位-数据域访问矩阵 ──────────────────────────────────────────────────────

class PositionAccessRule(Base):
    """岗位数据域访问规则"""
    __tablename__ = "position_access_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    data_domain = Column(String(50), nullable=False)  # client/project/financial/creative/hr/knowledge
    access_range = Column(String(20), default="none")  # none/own/own_client/assigned/department/all
    excluded_fields = Column(JSON, default=list)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("position_id", "data_domain", name="uq_position_access_rule"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# V2: 组织基线版本中心 + 5 类底座信息
# ══════════════════════════════════════════════════════════════════════════════

# ── 组织基线版本（独立于 GovernanceBaselineSnapshot） ─────────────────────────

class OrgBaseline(Base):
    """组织基线版本 — 组织管理域的主版本对象。
    状态机：draft → candidate → active → archived
    """
    __tablename__ = "org_baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(20), unique=True, nullable=False)  # v0.1, v0.2, v1.0
    version_type = Column(String(20), nullable=False)  # init / incremental / major
    status = Column(String(20), default="draft")  # draft / candidate / active / archived
    # 快照数据：当前版本生成时的组织全貌统计
    snapshot_summary = Column(JSON, default=dict)  # {dept_count, user_count, position_count, okr_count, ...}
    # 与上一个 active 版本的差异
    diff_from_previous = Column(JSON, default=list)  # [{entity_type, entity_id, change_type, summary}]
    # 影响面分析
    impact_analysis = Column(JSON, default=dict)  # {affected_resource_libraries, affected_policies, affected_rules}
    # 关联的治理引擎 snapshot ID（同步后回填）
    governance_snapshot_id = Column(Integer, ForeignKey("governance_baseline_snapshots.id"), nullable=True)
    # 触发来源
    trigger_source = Column(String(30), default="manual")  # import / manual / auto / schedule
    trigger_import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    # 操作人
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    activated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    activated_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    note = Column(Text, nullable=True)  # 版本说明


# ── 岗位能力模型 ──────────────────────────────────────────────────────────────

class PositionCompetencyModel(Base):
    """岗位胜任力 / 职责拆解 / 输出物标准"""
    __tablename__ = "position_competency_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"), unique=True, nullable=False)
    responsibilities = Column(JSON, default=list)  # [{name, description, priority}]
    competencies = Column(JSON, default=list)  # [{name, level_required, description}] 胜任力项
    output_standards = Column(JSON, default=list)  # [{deliverable, quality_criteria, frequency}] 输出物标准
    career_path = Column(JSON, default=list)  # [{from_level, to_level, typical_duration, requirements}]
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 资源库定义中心 ────────────────────────────────────────────────────────────

class ResourceLibraryDefinition(Base):
    """资源库的组织管理侧定义：字段需求、消费场景、读写属性、更新周期 SLA"""
    __tablename__ = "resource_library_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_code = Column(String(100), unique=True, nullable=False)  # 关联 GovernanceResourceLibrary.code
    display_name = Column(String(200), nullable=False)
    owner_department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    required_fields = Column(JSON, default=list)  # [{field_key, label, type, required, description}]
    consumption_scenarios = Column(JSON, default=list)  # [{scenario, consumer_roles, frequency}]
    read_write_policy = Column(JSON, default=dict)  # {who_writes, who_reads, approval_required}
    update_cycle_sla = Column(String(30), nullable=True)  # realtime / daily / weekly / monthly
    quality_baseline = Column(JSON, default=dict)  # {min_completeness_pct, min_freshness_days, min_accuracy_pct}
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── KR → 资源库 / 资产 / 流程映射 ────────────────────────────────────────────

class KrResourceMapping(Base):
    """KR 到资源库、数据资产、业务流程的挂接关系"""
    __tablename__ = "kr_resource_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kr_id = Column(Integer, ForeignKey("okr_key_results.id"), nullable=False)
    target_type = Column(String(30), nullable=False)  # resource_library / data_asset / biz_process / position
    target_code = Column(String(100), nullable=False)  # 目标的 code
    target_id = Column(Integer, nullable=True)  # 目标实体 ID
    relevance = Column(String(20), default="direct")  # direct / indirect / supporting
    description = Column(Text, nullable=True)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("kr_id", "target_type", "target_code", name="uq_kr_resource_mapping"),
    )


# ── 协同协议基线 ─────────────────────────────────────────────────────────────

class CollabProtocol(Base):
    """部门间协同协议：谁提供、谁消费、事件触发、同步频率、延迟容忍度"""
    __tablename__ = "collab_protocols"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    consumer_department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    data_object = Column(String(200), nullable=False)  # 协同对象：如 "客户台账"、"项目排期"
    provider_position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    consumer_position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    trigger_event = Column(String(200), nullable=True)  # 触发同步的事件
    sync_frequency = Column(String(20), default="manual")  # realtime / daily / weekly / monthly / event_driven / manual
    latency_tolerance = Column(String(50), nullable=True)  # 如 "4h", "1d", "realtime"
    sla_description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    import_session_id = Column(Integer, ForeignKey("org_import_sessions.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ── 组织变更影响分析 ──────────────────────────────────────────────────────────

class OrgChangeImpact(Base):
    """组织变更对治理体系的影响分析记录"""
    __tablename__ = "org_change_impacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baseline_id = Column(Integer, ForeignKey("org_baselines.id"), nullable=False)  # 关联的基线版本
    change_event_id = Column(Integer, ForeignKey("org_change_events.id"), nullable=True)
    impact_type = Column(String(50), nullable=False)  # resource_library / classification_rule / access_policy / collab_protocol / mission_sync
    impact_target_type = Column(String(50), nullable=False)  # 受影响实体类型
    impact_target_id = Column(Integer, nullable=True)
    impact_target_name = Column(String(200), nullable=True)
    severity = Column(String(10), default="medium")  # high / medium / low
    description = Column(Text, nullable=True)
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
