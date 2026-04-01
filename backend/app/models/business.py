import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class VisibilityLevel(str, enum.Enum):
    DETAIL = "detail"
    DESENSITIZED = "desensitized"
    STATS = "stats"


class DataOwnership(Base):
    __tablename__ = "data_ownership_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(100), nullable=False)
    owner_field = Column(String(100), nullable=False)  # e.g. "sales_rep_id"
    department_field = Column(String(100), nullable=True)  # e.g. "department_id"
    visibility_level = Column(
        Enum(VisibilityLevel, values_callable=lambda obj: [e.value for e in obj]),
        default=VisibilityLevel.DETAIL,
    )
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class BusinessTable(Base):
    __tablename__ = "business_tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ddl_sql = Column(Text)
    validation_rules = Column(JSON, default=dict)
    workflow = Column(JSON, default=dict)
    # ── Phase 1A: 数据资产扩展字段 ──
    folder_id = Column(Integer, ForeignKey("data_folders.id"), nullable=True)
    source_type = Column(String(20), default="blank")  # blank | mysql | lark_bitable | imported
    source_ref = Column(JSON, default=dict)  # e.g. {"app_token":"xxx","table_id":"tblxxx"}
    sync_status = Column(String(20), default="idle")  # idle | syncing | success | partial_success | failed | disabled
    sync_error = Column(Text, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_job_id = Column(Integer, nullable=True)
    field_profile_status = Column(String(20), default="pending")  # pending | ready | failed
    field_profile_error = Column(Text, nullable=True)
    record_count_cache = Column(Integer, nullable=True)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    owner = relationship("User", foreign_keys=[owner_id])
    folder = relationship("DataFolder", foreign_keys=[folder_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    table_name = Column(String(100), nullable=False)
    operation = Column(String(20), nullable=False)  # INSERT/UPDATE/DELETE
    row_id = Column(String(100))
    old_values = Column(JSON)
    new_values = Column(JSON)
    sql_executed = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SkillDataQuery(Base):
    __tablename__ = "skill_data_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    query_name = Column(String(100), nullable=False)
    query_type = Column(String(20), nullable=False)  # read/write/compute
    table_name = Column(String(100), nullable=False)
    description = Column(Text)
    template_sql = Column(Text)

    skill = relationship("Skill", foreign_keys=[skill_id])


class TableView(Base):
    """User-defined views for a business table: saved filter/sort/group/column config."""
    __tablename__ = "table_views"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    view_type = Column(String(20), default="grid")   # "grid" | "kanban" | "gallery"
    config = Column(JSON, default=dict)              # {filters, sorts, group_by, hidden_columns, column_widths}
    # ── Phase 1A: 视图扩展字段 ──
    view_purpose = Column(String(30), nullable=True)  # explore | ops | permission_basis | skill_runtime | review
    visibility_scope = Column(String(20), default="table_inherit")  # private | team | table_inherit | published
    is_default = Column(Boolean, default=False)
    is_system = Column(Boolean, default=False)
    last_used_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # ── Phase v1: 视图能力升级 ──
    visible_field_ids = Column(JSON, default=list)
    field_layout_json = Column(JSON, default=dict)
    filter_rule_json = Column(JSON, default=dict)
    group_rule_json = Column(JSON, default=dict)
    sort_rule_json = Column(JSON, default=dict)
    aggregate_rule_json = Column(JSON, default=dict)
    row_limit = Column(Integer, nullable=True)
    disclosure_ceiling = Column(String(5), nullable=True)     # L0-L4
    allowed_role_group_ids = Column(JSON, default=list)
    allowed_skill_ids = Column(JSON, default=list)
    view_kind = Column(String(20), default="list")            # list | board | metric | pivot | review_queue
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    table = relationship("BusinessTable", foreign_keys=[table_id])


# ── Phase 1A: 数据资产新模型 ─────────────────────────────────────────────────


class DataFolder(Base):
    """持久化目录树，替代前端 VirtualFolder。"""
    __tablename__ = "data_folders"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", "workspace_scope", "owner_id", name="uq_folder_name_scope"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("data_folders.id"), nullable=True)
    workspace_scope = Column(String(20), default="company")  # company | department | personal
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sort_order = Column(Integer, default=0)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    parent = relationship("DataFolder", remote_side="DataFolder.id", foreign_keys=[parent_id])
    children = relationship("DataFolder", foreign_keys=[parent_id])


class TableField(Base):
    """字段元信息持久化——从动态推断变成资产。"""
    __tablename__ = "table_fields"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    field_name = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=True)
    physical_column_name = Column(String(200), nullable=True)
    field_type = Column(String(30), default="text")  # text | number | single_select | multi_select | date | datetime | boolean | person | department | url | email | phone | attachment | relation | json | long_text | currency | percent
    source_field_type = Column(String(50), nullable=True)  # 保存来源原始类型，如飞书 type code
    is_nullable = Column(Boolean, default=True)
    is_system = Column(Boolean, default=False)
    is_hidden_by_default = Column(Boolean, default=False)
    is_filterable = Column(Boolean, default=True)
    is_groupable = Column(Boolean, default=False)
    is_sortable = Column(Boolean, default=True)
    enum_values = Column(JSON, default=list)  # ["待跟进", "已签约"]
    enum_source = Column(String(20), nullable=True)  # source_declared | observed
    sample_values = Column(JSON, default=list)  # max 10 samples
    distinct_count_cache = Column(Integer, nullable=True)
    null_ratio = Column(Float, nullable=True)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0)
    # ── Phase v1: 字段数据字典扩展 ──
    field_role_tags = Column(JSON, default=list)    # ["dimension","metric","identifier","sensitive","derived","system"]
    is_enum = Column(Boolean, default=False)
    is_free_text = Column(Boolean, default=False)
    is_sensitive = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    table = relationship("BusinessTable", foreign_keys=[table_id])


class TableSyncJob(Base):
    """同步任务记录——让同步过程透明可审计。"""
    __tablename__ = "table_sync_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String(20), nullable=True)  # lark_bitable | mysql | ...
    job_type = Column(String(30), nullable=False)  # full_sync | incremental_sync | schema_refresh | field_profile_refresh
    status = Column(String(20), default="queued")  # queued | running | success | partial_success | failed | cancelled
    error_type = Column(String(30), nullable=True)  # auth_error | network_error | schema_error | storage_error | unknown_error
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    triggered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    trigger_source = Column(String(20), default="manual")  # manual | scheduled | webhook | migration
    result_summary = Column(JSON, default=dict)
    error_message = Column(Text, nullable=True)
    stats = Column(JSON, default=dict)  # {"inserted": 10, "updated": 5, "deleted": 0, "field_changes": 2}

    table = relationship("BusinessTable", foreign_keys=[table_id])


class SkillTableBinding(Base):
    """Skill 到视图的绑定——明确 Skill 通过哪个 view 读数据。

    与 SkillDataQuery 共存：
    - SkillDataQuery: 声明层，Skill 声明会读哪张表
    - SkillTableBinding: 执行层，Skill 实际被允许通过哪个 view 读
    - 没有 binding 的旧 Skill 视为 legacy_unbound，仍走老逻辑
    """
    __tablename__ = "skill_table_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    view_id = Column(Integer, ForeignKey("table_views.id", ondelete="SET NULL"), nullable=True)
    binding_type = Column(String(20), default="runtime_read")  # runtime_read | runtime_write | config_reference | example_data
    alias = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    table = relationship("BusinessTable", foreign_keys=[table_id])
    view = relationship("TableView", foreign_keys=[view_id])


# ── Phase v1: 数据资产升级 ─────────────────────────────────────────────────────


class TableRoleGroup(Base):
    """角色组——权限策略的主语。"""
    __tablename__ = "table_role_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    group_type = Column(String(20), default="human_role")  # human_role | skill_role | mixed
    subject_scope = Column(String(20), default="custom")   # custom | all_users | all_skills
    user_ids = Column(JSON, default=list)
    department_ids = Column(JSON, default=list)
    role_keys = Column(JSON, default=list)
    skill_ids = Column(JSON, default=list)
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    table = relationship("BusinessTable", foreign_keys=[table_id])


class TablePermissionPolicy(Base):
    """权限策略——角色组在某表/视图上能做什么。"""
    __tablename__ = "table_permission_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    view_id = Column(Integer, ForeignKey("table_views.id", ondelete="SET NULL"), nullable=True)
    role_group_id = Column(Integer, ForeignKey("table_role_groups.id", ondelete="CASCADE"), nullable=False)
    row_access_mode = Column(String(20), default="none")       # none | all | owner | department | rule
    row_rule_json = Column(JSON, default=dict)                  # v1: 仅支持 owner/department 预设
    field_access_mode = Column(String(20), default="all")       # all | allowlist | blocklist
    allowed_field_ids = Column(JSON, default=list)
    blocked_field_ids = Column(JSON, default=list)
    disclosure_level = Column(String(5), default="L0")          # L0 | L1 | L2 | L3 | L4
    masking_rule_json = Column(JSON, default=dict)              # v1: 6种固定规则
    tool_permission_mode = Column(String(20), default="deny")   # deny | readonly | readwrite
    export_permission = Column(Boolean, default=False)
    reason_template = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    table = relationship("BusinessTable", foreign_keys=[table_id])
    view = relationship("TableView", foreign_keys=[view_id])
    role_group = relationship("TableRoleGroup", foreign_keys=[role_group_id])


class FieldValueDictionary(Base):
    """字段枚举全集——从推断值升级为受控枚举。"""
    __tablename__ = "field_value_dictionary"

    id = Column(Integer, primary_key=True, autoincrement=True)
    field_id = Column(Integer, ForeignKey("table_fields.id", ondelete="CASCADE"), nullable=False)
    value = Column(String(500), nullable=False)
    label = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    source = Column(String(20), default="manual")  # manual | inferred | synced
    sort_order = Column(Integer, default=0)
    hit_count = Column(Integer, default=0)
    last_seen_at = Column(DateTime, nullable=True)

    field = relationship("TableField", foreign_keys=[field_id])


class SkillDataGrant(Base):
    """Skill 数据授权——Skill 通过哪个视图、什么级别读数据。"""
    __tablename__ = "skill_data_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False)
    table_id = Column(Integer, ForeignKey("business_tables.id", ondelete="CASCADE"), nullable=False)
    view_id = Column(Integer, ForeignKey("table_views.id", ondelete="SET NULL"), nullable=True)
    role_group_id = Column(Integer, ForeignKey("table_role_groups.id", ondelete="SET NULL"), nullable=True)
    grant_mode = Column(String(10), default="allow")   # deny | allow
    allowed_actions = Column(JSON, default=list)        # ["read", "aggregate", "export"]
    max_disclosure_level = Column(String(5), default="L2")  # 外部 Skill 默认不超过 L2
    row_rule_override_json = Column(JSON, default=dict)
    field_rule_override_json = Column(JSON, default=dict)
    approval_required = Column(Boolean, default=False)
    audit_level = Column(String(10), default="basic")   # none | basic | full
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    skill = relationship("Skill", foreign_keys=[skill_id])
    table = relationship("BusinessTable", foreign_keys=[table_id])
    view = relationship("TableView", foreign_keys=[view_id])
    role_group = relationship("TableRoleGroup", foreign_keys=[role_group_id])
