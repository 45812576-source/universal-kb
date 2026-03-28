import datetime
import enum

from sqlalchemy import BigInteger, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import relationship

from app.database import Base


class KnowledgeFolder(Base):
    """用户自定义文件夹，支持无限层级树形结构。"""
    __tablename__ = "knowledge_folders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    children = relationship(
        "KnowledgeFolder",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="KnowledgeFolder.sort_order",
    )
    parent = relationship("KnowledgeFolder", back_populates="children", remote_side=[id])


class KnowledgeStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class ReviewStage(str, enum.Enum):
    AUTO_APPROVED = "auto_approved"           # L0/L1: 直接通过，无需人工
    PENDING_DEPT = "pending_dept"             # L2/L3: 等待部门管理员审核
    DEPT_APPROVED_PENDING_SUPER = "dept_approved_pending_super"  # L3: 部门已过，等超管
    APPROVED = "approved"                     # 最终通过
    REJECTED = "rejected"                     # 拒绝


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    content_html = Column(Text, nullable=True)    # 富文本 HTML（供前端云文档编辑器）
    category = Column(String(50), default="experience")  # experience / external_intel
    status = Column(Enum(KnowledgeStatus), default=KnowledgeStatus.PENDING)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    folder_id = Column(Integer, ForeignKey("knowledge_folders.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    review_note = Column(Text, nullable=True)

    # Metadata for hard filtering in Milvus
    industry_tags = Column(JSON, default=list)   # ["食品", "美妆"]
    platform_tags = Column(JSON, default=list)   # ["抖音", "小红书"]
    topic_tags = Column(JSON, default=list)      # ["投放策略", "客户拓展"]

    # Source info
    source_type = Column(String(50), default="manual")  # manual / upload / auto_collected
    source_file = Column(String(255), nullable=True)

    # ── OSS 文件存储 ─────────────────────────────────────────────────────────
    oss_key = Column(String(500), nullable=True)       # OSS 对象路径
    file_type = Column(String(50), nullable=True)      # MIME 类型 (application/pdf 等)
    file_ext = Column(String(20), nullable=True)       # 文件扩展名 (.pdf, .docx 等)
    file_size = Column(BigInteger, nullable=True)      # 文件大小 (bytes)

    # ── AI 智能命名 ──────────────────────────────────────────────────────────
    ai_title = Column(String(500), nullable=True)      # AI 生成的标题
    ai_summary = Column(Text, nullable=True)           # AI 生成的摘要
    ai_tags = Column(JSON, nullable=True)              # AI 生成的标签 {"industry":[], "platform":[], "topic":[]}
    quality_score = Column(Float, nullable=True)        # AI 内容质量评分 0-1

    # Milvus chunk IDs
    milvus_ids = Column(JSON, default=list)

    source_draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    raw_input_id    = Column(Integer, nullable=True)  # raw_inputs table not in use
    # capture_mode values:
    #   manual_form             手动录入表单
    #   chat_delegate_confirmed 对话转换+全字段用户确认
    #   chat_delegate_partial   对话转换+部分未确认
    #   skill_output            Skill 执行产出沉淀
    #   upload_ai_clean         文件上传+AI清洗无敏感
    #   upload                  文件上传原始
    capture_mode    = Column(String(50), default="manual_form")
    visibility_scope = Column(String(50), nullable=True)
    linked_skill_codes = Column(JSON, default=list)
    applicable_departments = Column(JSON, default=list)
    applicable_roles = Column(JSON, default=list)

    # ── 分级审核字段 ──────────────────────────────────────────────────────────
    # review_level: 0=直接收录 1=AI自动收录 2=部门确认 3=超管确认
    review_level = Column(Integer, default=2)
    review_stage = Column(
        Enum(ReviewStage, values_callable=lambda x: [e.value for e in x]), default=ReviewStage.PENDING_DEPT, nullable=True
    )
    # FOE LangChain 摘要（上传时生成或按需生成，用于 @ 引用注入对话上下文）
    summary = Column(Text, nullable=True)

    # AI检测到的敏感词标记列表，如 ["customer_name", "amount"]
    sensitivity_flags = Column(JSON, default=list)
    # AI自动审核说明
    auto_review_note = Column(Text, nullable=True)

    # ── 知识分类树字段 ──────────────────────────────────────────────────────────
    # taxonomy_code: 分类树编码，如 "A1.1"
    taxonomy_code = Column(String(50), nullable=True)
    # taxonomy_board: 大板块 A-F
    taxonomy_board = Column(String(10), nullable=True)
    # taxonomy_path: 完整路径数组，如 ["A.渠道与平台", "A1.国内付费渠道", ...]
    taxonomy_path = Column(JSON, nullable=True)
    # storage_layer: L1/L2/L3/L4
    storage_layer = Column(String(10), nullable=True)
    # target_kb_ids: 应归入的知识库 ID 列表，如 ["KT-01", "DB-08"]
    target_kb_ids = Column(JSON, nullable=True)
    # serving_skill_codes: 可服务的 Skill 编码列表
    serving_skill_codes = Column(JSON, nullable=True)
    # ai_classification_note: AI 分类推理说明
    ai_classification_note = Column(Text, nullable=True)
    # classification_confidence: AI 分类置信度 0-1
    classification_confidence = Column(Float, nullable=True)

    # ── 飞书文档同步 ──────────────────────────────────────────────────────────
    lark_doc_token = Column(String(200), nullable=True)       # 飞书文档 token
    lark_doc_type = Column(String(50), nullable=True)         # docx / wiki / sheet / file
    lark_doc_url = Column(String(500), nullable=True)         # 原始飞书链接
    lark_sync_interval = Column(Integer, default=0)           # 同步间隔（分钟），0=不同步
    lark_last_synced_at = Column(Integer, default=0)          # 上次同步时间戳

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    creator = relationship("User", foreign_keys=[created_by])
    reviewer = relationship("User", foreign_keys=[reviewed_by])
    revisions = relationship(
        "KnowledgeRevision",
        back_populates="knowledge",
        cascade="all, delete-orphan",
        order_by="KnowledgeRevision.version",
    )


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id = Column(Integer, ForeignKey("knowledge_entries.id"), nullable=False)
    user_request = Column(Text, nullable=True)   # 用户每次的修改要求
    diff_content = Column(Text, nullable=True)   # 与上一版本的 diff
    version = Column(Integer, nullable=False, default=1)
    visibility = Column(String(50), default="super_admin_only")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    knowledge = relationship("KnowledgeEntry", back_populates="revisions")
