import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


# ─── 枚举 ────────────────────────────────────────────────────────────────────

class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # ── 组织管理增强字段 ──
    code = Column(String(50), unique=True, nullable=True)  # 岗位编码
    kpi_template = Column(JSON, default=list)  # KPI 指标模板
    evaluation_cycle = Column(String(20), nullable=True)  # month/quarter/half_year/year
    required_data_domains = Column(JSON, default=list)  # 该岗位需要的数据域列表
    deliverables = Column(JSON, default=list)  # 岗位标准交付物
    sort_order = Column(Integer, default=0)

    department = relationship("Department")
    users = relationship("User", back_populates="position")


class DataDomain(Base):
    __tablename__ = "data_domains"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    fields = Column(JSON, default=list)  # [{"name": "revenue", "label": "收入", "sensitive": true}]
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class PolicyTargetType(str, enum.Enum):
    POSITION = "position"
    ROLE = "role"


class PolicyResourceType(str, enum.Enum):
    BUSINESS_TABLE = "business_table"
    DATA_DOMAIN = "data_domain"


class VisibilityScope(str, enum.Enum):
    OWN = "own"
    DEPT = "dept"
    ALL = "all"


class DataScopePolicy(Base):
    __tablename__ = "data_scope_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_type = Column(
        Enum(PolicyTargetType, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    target_position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    target_role = Column(String(50), nullable=True)
    resource_type = Column(
        Enum(PolicyResourceType, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    business_table_id = Column(Integer, ForeignKey("business_tables.id"), nullable=True)
    data_domain_id = Column(Integer, ForeignKey("data_domains.id"), nullable=True)
    visibility_level = Column(
        Enum(VisibilityScope, values_callable=lambda obj: [e.value for e in obj]),
        default=VisibilityScope.OWN,
        nullable=False,
    )
    output_mask = Column(JSON, default=list)  # ["revenue", "contract_value"]
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    position = relationship("Position")


# ─── 脱敏动作枚举 ─────────────────────────────────────────────────────────────

class MaskAction(str, enum.Enum):
    KEEP = "keep"
    HIDE = "hide"
    REMOVE = "remove"
    RANGE = "range"
    TRUNCATE = "truncate"
    PARTIAL = "partial"
    RANK = "rank"
    AGGREGATE = "aggregate"
    REPLACE = "replace"
    NOISE = "noise"
    SHOW = "show"
    LABEL_ONLY = "label_only"


# ─── 发布范围枚举 ─────────────────────────────────────────────────────────────

class PublishScope(str, enum.Enum):
    SELF_ONLY = "self_only"
    SAME_ROLE = "same_role"
    CROSS_ROLE = "cross_role"
    ORG_WIDE = "org_wide"


# ─── 全局脱敏规则 ─────────────────────────────────────────────────────────────

class GlobalDataMask(Base):
    """全局默认脱敏规则（~15 条 seed）"""
    __tablename__ = "global_data_masks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    field_name = Column(String(100), nullable=False)
    data_domain_id = Column(Integer, ForeignKey("data_domains.id"), nullable=True)
    mask_action = Column(
        Enum(MaskAction, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=MaskAction.HIDE,
    )
    mask_params = Column(JSON, default=dict)   # e.g. {"prefix_len": 3} for PARTIAL
    severity = Column(Integer, default=1)       # 敏感级别 1-5
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    data_domain = relationship("DataDomain")


# ─── 角色级脱敏覆盖 ───────────────────────────────────────────────────────────

class RoleMaskOverride(Base):
    """角色级脱敏规则（覆盖全局默认）"""
    __tablename__ = "role_mask_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    field_name = Column(String(100), nullable=False)
    data_domain_id = Column(Integer, ForeignKey("data_domains.id"), nullable=True)
    mask_action = Column(
        Enum(MaskAction, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    mask_params = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    position = relationship("Position")
    data_domain = relationship("DataDomain")


# ─── Skill 级脱敏覆盖 ─────────────────────────────────────────────────────────

class SkillMaskOverride(Base):
    """Skill 级脱敏规则（最严格，覆盖角色级）"""
    __tablename__ = "skill_mask_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)  # None = 所有角色
    field_name = Column(String(100), nullable=False)
    mask_action = Column(
        Enum(MaskAction, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    mask_params = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    position = relationship("Position")


# ─── Skill Policy ─────────────────────────────────────────────────────────────

class SkillPolicy(Base):
    """Skill 权限策略（一对一绑定 Skill，发布时自动生成）"""
    __tablename__ = "skill_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), unique=True, nullable=False)
    publish_scope = Column(
        Enum(PublishScope, values_callable=lambda obj: [e.value for e in obj]),
        default=PublishScope.SAME_ROLE,
        nullable=False,
    )
    view_scope = Column(
        Enum(PublishScope, values_callable=lambda obj: [e.value for e in obj]),
        default=PublishScope.ORG_WIDE,
        nullable=False,
    )
    default_data_scope = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    overrides = relationship("RolePolicyOverride", back_populates="skill_policy", cascade="all, delete-orphan")
    agent_connections = relationship("SkillAgentConnection", back_populates="skill_policy", cascade="all, delete-orphan")


# ─── 角色级 Policy 覆盖 ───────────────────────────────────────────────────────

class RolePolicyOverride(Base):
    """按角色覆盖 Skill 策略"""
    __tablename__ = "role_policy_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_policy_id = Column(Integer, ForeignKey("skill_policies.id"), nullable=False)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    callable = Column(Boolean, default=True)
    data_scope = Column(JSON, default=dict)
    output_mask = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill_policy = relationship("SkillPolicy", back_populates="overrides")
    position = relationship("Position")

    __table_args__ = (
        UniqueConstraint("skill_policy_id", "position_id", name="uq_role_policy_override"),
    )


# ─── 角色输出遮罩 ─────────────────────────────────────────────────────────────

class RoleOutputMask(Base):
    """角色×字段输出遮罩规则（~150 条 seed）"""
    __tablename__ = "role_output_masks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    data_domain_id = Column(Integer, ForeignKey("data_domains.id"), nullable=False)
    field_name = Column(String(100), nullable=False)
    mask_action = Column(
        Enum(MaskAction, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=MaskAction.SHOW,
    )
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    position = relationship("Position")
    data_domain = relationship("DataDomain")

    __table_args__ = (
        UniqueConstraint("position_id", "data_domain_id", "field_name", name="uq_role_output_mask"),
    )


# ─── Skill Output Schema ──────────────────────────────────────────────────────

class SchemaStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"


class SkillOutputSchema(Base):
    """Skill 输出结构定义（版本化）"""
    __tablename__ = "skill_output_schemas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    status = Column(
        Enum(SchemaStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=SchemaStatus.DRAFT,
        nullable=False,
    )
    schema_json = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Agent 连接（上下游白名单） ───────────────────────────────────────────────

class ConnectionDirection(str, enum.Enum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


class SkillAgentConnection(Base):
    """Skill 上下游白名单"""
    __tablename__ = "skill_agent_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_policy_id = Column(Integer, ForeignKey("skill_policies.id"), nullable=False)
    direction = Column(
        Enum(ConnectionDirection, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    connected_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill_policy = relationship("SkillPolicy", back_populates="agent_connections")

    __table_args__ = (
        UniqueConstraint("skill_policy_id", "direction", "connected_skill_id", name="uq_skill_agent_connection"),
    )


# ─── Handoff 模板 ─────────────────────────────────────────────────────────────

class HandoffTemplateType(str, enum.Enum):
    STANDARD = "standard"
    L3_MASK = "l3_mask"
    MULTI_UPSTREAM = "multi_upstream"


class HandoffTemplate(Base):
    """静态 Handoff 模板（7 个初始 seed）"""
    __tablename__ = "handoff_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    upstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    downstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    template_type = Column(
        Enum(HandoffTemplateType, values_callable=lambda obj: [e.value for e in obj]),
        default=HandoffTemplateType.STANDARD,
        nullable=False,
    )
    schema_fields = Column(JSON, default=list)    # 包含哪些字段
    excluded_fields = Column(JSON, default=list)  # 排除哪些字段
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Handoff Schema 缓存 ──────────────────────────────────────────────────────

class HandoffSchemaCache(Base):
    """动态 Schema 缓存（TTL 7天）"""
    __tablename__ = "handoff_schema_caches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(255), unique=True, nullable=False)
    upstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    downstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    task_type_hash = Column(String(64), nullable=True)
    schema_json = Column(JSON, nullable=False, default=dict)
    hit_count = Column(Integer, default=0)
    incomplete_count = Column(Integer, default=0)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── 审批流 ───────────────────────────────────────────────────────────────────

class ApprovalRequestType(str, enum.Enum):
    SKILL_PUBLISH = "skill_publish"
    SKILL_VERSION_CHANGE = "skill_version_change"
    SKILL_OWNERSHIP_TRANSFER = "skill_ownership_transfer"
    TOOL_PUBLISH = "tool_publish"
    WEBAPP_PUBLISH = "webapp_publish"
    SCOPE_CHANGE = "scope_change"
    MASK_OVERRIDE = "mask_override"
    SCHEMA_APPROVAL = "schema_approval"
    KNOWLEDGE_EDIT = "knowledge_edit"
    KNOWLEDGE_REVIEW = "knowledge_review"
    # 数据安全 6 类
    EXPORT_SENSITIVE = "export_sensitive"
    ELEVATE_DISCLOSURE = "elevate_disclosure"
    GRANT_ACCESS = "grant_access"
    POLICY_CHANGE = "policy_change"
    FIELD_SENSITIVITY_CHANGE = "field_sensitivity_change"
    SMALL_SAMPLE_CHANGE = "small_sample_change"
    PERMISSION_CHANGE = "permission_change"
    ORG_MEMORY_PROPOSAL = "org_memory_proposal"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONDITIONS = "conditions"
    WITHDRAWN = "withdrawn"


class ApprovalRequest(Base):
    """审批申请"""
    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_type = Column(
        Enum(ApprovalRequestType, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    target_id = Column(Integer, nullable=True)
    target_type = Column(String(100), nullable=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(
        Enum(ApprovalStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=ApprovalStatus.PENDING,
        nullable=False,
    )
    conditions = Column(JSON, default=list)   # 附条件时的条件列表
    stage = Column(String(20), default="dept_pending", nullable=False)  # dept_pending / super_pending
    security_scan_result = Column(JSON, default=None, nullable=True)   # 安全扫描结果（含风险报告 + Policy 草案）
    dept_approved_policy = Column(JSON, default=None, nullable=True)   # dept_admin 已确认的 Policy 分量（scope/overrides/masks 均在自己权限内）
    # Gap 4: 沙盒-审批强绑定
    sandbox_report_id = Column(Integer, ForeignKey("sandbox_test_reports.id"), nullable=True)
    sandbox_report_hash = Column(String(64), nullable=True)
    # V2: 证据包 + 风险评估
    evidence_pack = Column(JSON, default=None, nullable=True)
    risk_level = Column(String(20), nullable=True)        # high / medium / low
    impact_summary = Column(Text, nullable=True)
    # 显式审批路由
    assigned_approver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    requester = relationship("User", foreign_keys=[requester_id])
    assigned_approver = relationship("User", foreign_keys=[assigned_approver_id])
    actions = relationship("ApprovalAction", back_populates="request", cascade="all, delete-orphan")


class ApprovalActionType(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ADD_CONDITIONS = "add_conditions"
    REQUEST_MORE_INFO = "request_more_info"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    SUPPLEMENT = "supplement"
    WITHDRAW = "withdraw"


class ApprovalAction(Base):
    """审批动作"""
    __tablename__ = "approval_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(Integer, ForeignKey("approval_requests.id"), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(
        Enum(ApprovalActionType, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    comment = Column(Text, nullable=True)
    # V2: 结构化审批结论
    decision_payload = Column(JSON, default=None, nullable=True)
    checklist_result = Column(JSON, default=None, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    request = relationship("ApprovalRequest", back_populates="actions")
    actor = relationship("User", foreign_keys=[actor_id])


# ─── 权限审计日志 ─────────────────────────────────────────────────────────────

class PermissionAuditLog(Base):
    """权限变更专用审计日志"""
    __tablename__ = "permission_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False)
    target_table = Column(String(100), nullable=False)
    target_id = Column(Integer, nullable=True)
    old_values = Column(JSON, default=dict)
    new_values = Column(JSON, default=dict)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    operator = relationship("User", foreign_keys=[operator_id])


# ─── Handoff 执行记录 ─────────────────────────────────────────────────────────

class HandoffExecutionStatus(str, enum.Enum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class HandoffExecution(Base):
    """Handoff 运行记录"""
    __tablename__ = "handoff_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    upstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    downstream_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("handoff_templates.id"), nullable=True)
    cache_id = Column(Integer, ForeignKey("handoff_schema_caches.id"), nullable=True)
    payload_json = Column(JSON, default=dict)
    status = Column(
        Enum(HandoffExecutionStatus, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=HandoffExecutionStatus.SUCCESS,
    )
    error_msg = Column(Text, nullable=True)
    executed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
