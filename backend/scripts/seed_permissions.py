"""权限系统 Seed 数据
- 5个岗位（Position）：商务/策划/财务/HR/管理层
- 6个数据域（DataDomain）：client/project/financial/creative/hr/knowledge
- GlobalDataMask ~15条：全局字段脱敏默认规则
- DataScopePolicy 30条：5角色 × 6域的可见范围
- RoleOutputMask ~150条：5角色 × 6域 × 各字段的输出遮罩
- HandoffTemplate 7个：高频 Agent 组合静态模板

运行方式（从 backend 目录）：
  conda run -n base python scripts/seed_permissions.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.permission import (
    DataDomain,
    DataScopePolicy,
    GlobalDataMask,
    HandoffTemplate,
    HandoffTemplateType,
    MaskAction,
    PolicyResourceType,
    PolicyTargetType,
    Position,
    RoleOutputMask,
    VisibilityScope,
)


# ─── 数据定义 ─────────────────────────────────────────────────────────────────

POSITIONS = [
    {"name": "商务",  "description": "销售/商务开发，负责客户关系与合同管理"},
    {"name": "策划",  "description": "创意/内容策划，负责campaign策划与创意交付"},
    {"name": "财务",  "description": "财务核算，负责合同结算、成本核算、利润分析"},
    {"name": "HR",    "description": "人力资源，负责员工档案、绩效、薪资管理"},
    {"name": "管理层", "description": "高管/总监级，对全域数据有聚合视图权限"},
]

# 6个数据域，含字段定义（name, label, sensitive, type）
DATA_DOMAINS = [
    {
        "name": "client",
        "display_name": "客户信息",
        "description": "客户基础信息、联系人、合作历史",
        "fields": [
            {"name": "client_name",       "label": "客户名称",     "sensitive": False, "type": "string"},
            {"name": "industry",          "label": "所属行业",     "sensitive": False, "type": "string"},
            {"name": "brand",             "label": "品牌名",       "sensitive": False, "type": "string"},
            {"name": "contact_name",      "label": "联系人姓名",   "sensitive": True,  "type": "string"},
            {"name": "contact_phone",     "label": "联系人电话",   "sensitive": True,  "type": "string"},
            {"name": "contact_email",     "label": "联系人邮箱",   "sensitive": True,  "type": "string"},
            {"name": "contract_terms",    "label": "合同条款",     "sensitive": True,  "type": "text"},
            {"name": "history_campaigns", "label": "历史合作项目", "sensitive": False, "type": "json"},
            {"name": "contract_status",   "label": "合同状态",     "sensitive": False, "type": "string"},
        ],
    },
    {
        "name": "project",
        "display_name": "项目信息",
        "description": "项目/campaign、排期、brief、交付物",
        "fields": [
            {"name": "project_name",      "label": "项目名称",   "sensitive": False, "type": "string"},
            {"name": "status",            "label": "项目状态",   "sensitive": False, "type": "string"},
            {"name": "timeline",          "label": "排期",       "sensitive": False, "type": "json"},
            {"name": "department",        "label": "负责部门",   "sensitive": False, "type": "string"},
            {"name": "headcount",         "label": "投入人力",   "sensitive": False, "type": "int"},
            {"name": "brief",             "label": "项目Brief",  "sensitive": True,  "type": "text"},
            {"name": "creative_content",  "label": "创意内容",   "sensitive": True,  "type": "text"},
            {"name": "budget_range",      "label": "预算区间",   "sensitive": False, "type": "string"},
        ],
    },
    {
        "name": "financial",
        "display_name": "财务数据",
        "description": "合同金额、报价、成本、利润率、回款",
        "fields": [
            {"name": "contract_value",    "label": "合同金额",   "sensitive": True,  "type": "number"},
            {"name": "payment_status",    "label": "回款状态",   "sensitive": True,  "type": "string"},
            {"name": "receivable",        "label": "应收账款",   "sensitive": True,  "type": "number"},
            {"name": "cost",              "label": "成本",       "sensitive": True,  "type": "number"},
            {"name": "margin",            "label": "利润率",     "sensitive": True,  "type": "number"},
            {"name": "company_revenue",   "label": "公司总营收", "sensitive": True,  "type": "number"},
            {"name": "budget_exact",      "label": "精确预算",   "sensitive": True,  "type": "number"},
        ],
    },
    {
        "name": "creative",
        "display_name": "创意内容",
        "description": "策划案、创意方案、脚本、素材",
        "fields": [
            {"name": "campaign_title",    "label": "Campaign标题", "sensitive": False, "type": "string"},
            {"name": "campaign_status",   "label": "Campaign状态", "sensitive": False, "type": "string"},
            {"name": "campaign_type",     "label": "Campaign类型", "sensitive": False, "type": "string"},
            {"name": "brief_summary",     "label": "Brief摘要",    "sensitive": False, "type": "text"},
            {"name": "full_content",      "label": "完整创意内容", "sensitive": True,  "type": "text"},
            {"name": "raw_script",        "label": "原始脚本",     "sensitive": True,  "type": "text"},
            {"name": "client_name_masked","label": "脱敏客户名",   "sensitive": False, "type": "string"},
        ],
    },
    {
        "name": "hr",
        "display_name": "HR数据",
        "description": "员工档案、薪资、考勤、绩效评分",
        "fields": [
            {"name": "employee_name",     "label": "员工姓名",   "sensitive": True,  "type": "string"},
            {"name": "department",        "label": "部门",       "sensitive": False, "type": "string"},
            {"name": "position",          "label": "岗位",       "sensitive": False, "type": "string"},
            {"name": "salary_exact",      "label": "精确薪资",   "sensitive": True,  "type": "number"},
            {"name": "salary_band",       "label": "薪资区间",   "sensitive": False, "type": "string"},
            {"name": "headcount",         "label": "人数",       "sensitive": False, "type": "int"},
            {"name": "performance_score", "label": "绩效原始分", "sensitive": True,  "type": "number"},
            {"name": "performance_level", "label": "绩效等级",   "sensitive": False, "type": "string"},
            {"name": "personal_id",       "label": "身份证号",   "sensitive": True,  "type": "string"},
            {"name": "attendance",        "label": "考勤记录",   "sensitive": True,  "type": "json"},
        ],
    },
    {
        "name": "knowledge",
        "display_name": "知识库",
        "description": "行业报告、方法论、培训资料、公司制度（全员公开）",
        "fields": [
            {"name": "title",             "label": "标题",       "sensitive": False, "type": "string"},
            {"name": "content",           "label": "内容",       "sensitive": False, "type": "text"},
            {"name": "category",          "label": "分类",       "sensitive": False, "type": "string"},
            {"name": "author",            "label": "作者",       "sensitive": False, "type": "string"},
        ],
    },
]

# 全局脱敏规则（15条）：默认对敏感字段的全局处理方式
# severity: 1=低 2=中 3=高 4=极高 5=绝对受控
GLOBAL_MASKS = [
    # 联系信息
    {"field_name": "contact_phone",     "mask_action": MaskAction.TRUNCATE,  "mask_params": {"length": 7, "suffix": "****"}, "severity": 3},
    {"field_name": "contact_email",     "mask_action": MaskAction.PARTIAL,   "mask_params": {"prefix_len": 3},               "severity": 3},
    {"field_name": "contact_name",      "mask_action": MaskAction.PARTIAL,   "mask_params": {"prefix_len": 1},               "severity": 2},
    {"field_name": "personal_id",       "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 5},
    # 财务数据
    {"field_name": "contract_value",    "mask_action": MaskAction.RANGE,     "mask_params": {"step": 100000},                "severity": 4},
    {"field_name": "cost",              "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "margin",            "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "company_revenue",   "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 5},
    {"field_name": "budget_exact",      "mask_action": MaskAction.RANGE,     "mask_params": {"step": 50000},                 "severity": 4},
    # HR数据
    {"field_name": "salary_exact",      "mask_action": MaskAction.AGGREGATE, "mask_params": {"aggregate_label": "部门薪资均值范围"}, "severity": 5},
    {"field_name": "performance_score", "mask_action": MaskAction.RANK,      "mask_params": {"rank_label": "绩效等级"},      "severity": 3},
    {"field_name": "attendance",        "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
    # 业务机密
    {"field_name": "contract_terms",    "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "raw_script",        "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
    {"field_name": "full_content",      "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
]

# DataScopePolicy：5角色 × 6域的可见范围
# visibility: own=仅自己关联, dept=部门, all=全部
# output_mask: 该角色在此域中要隐藏的字段列表
DATA_SCOPE_POLICIES = [
    # ── 商务 ──────────────────────────────────────────────────────────────────
    # client: 看自己负责的客户全貌，但合同条款隐藏
    {"position": "商务", "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contract_terms"]},
    # project: 看自己客户的项目，隐藏完整创意防飞单
    {"position": "商务", "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "creative_content"]},
    # financial: 看自己客户的合同/回款，不看成本利润
    {"position": "商务", "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["cost", "margin", "company_revenue"]},
    # creative: 只看标题/状态/brief摘要，不看完整内容
    {"position": "商务", "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script"]},
    # hr: 无权限
    {"position": "商务", "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": ["salary_exact", "salary_band", "performance_score", "performance_level", "personal_id", "attendance", "employee_name"]},
    # knowledge: 全量
    {"position": "商务", "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 策划 ──────────────────────────────────────────────────────────────────
    # client: 只看分配项目的客户，极度受限
    {"position": "策划", "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contact_name", "contact_phone", "contact_email", "contract_terms", "contract_status"]},
    # project: 看分配的项目全量
    {"position": "策划", "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": []},
    # financial: 只看预算区间，不看精确值
    {"position": "策划", "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "cost", "margin", "company_revenue", "receivable", "payment_status", "budget_exact"]},
    # creative: 全量（含借阅机制，通过L3脱敏覆盖）
    {"position": "策划", "domain": "creative",  "visibility": VisibilityScope.ALL, "output_mask": []},
    # hr: 无
    {"position": "策划", "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": ["salary_exact", "salary_band", "performance_score", "performance_level", "personal_id", "attendance", "employee_name"]},
    # knowledge: 全量
    {"position": "策划", "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 财务 ──────────────────────────────────────────────────────────────────
    # client: 看全部客户，但只有合同状态，不碰联系人
    {"position": "财务", "domain": "client",    "visibility": VisibilityScope.ALL, "output_mask": ["contact_name", "contact_phone", "contact_email", "history_campaigns", "contract_terms"]},
    # project: 看全部项目元信息，不碰创意
    {"position": "财务", "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": ["brief", "creative_content", "full_content"]},
    # financial: 全量
    {"position": "财务", "domain": "financial", "visibility": VisibilityScope.ALL, "output_mask": []},
    # creative: 无权限
    {"position": "财务", "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "brief_summary"]},
    # hr: 人力成本粒度，不看个人薪资精确值和绩效
    {"position": "财务", "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": ["salary_exact", "performance_score", "personal_id", "attendance", "employee_name"]},
    # knowledge: 全量
    {"position": "财务", "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── HR ────────────────────────────────────────────────────────────────────
    # client: 无
    {"position": "HR",   "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["client_name", "industry", "brand", "contact_name", "contact_phone", "contact_email", "contract_terms", "history_campaigns", "contract_status"]},
    # project: 只看人力规划元信息
    {"position": "HR",   "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": ["brief", "creative_content", "full_content", "budget_range"]},
    # financial: 无
    {"position": "HR",   "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "payment_status", "receivable", "cost", "margin", "company_revenue", "budget_exact"]},
    # creative: 无
    {"position": "HR",   "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "brief_summary", "campaign_title"]},
    # hr: 全量
    {"position": "HR",   "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": []},
    # knowledge: 全量
    {"position": "HR",   "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 管理层 ────────────────────────────────────────────────────────────────
    # 全域 all，但输出侧仍需遮罩个人敏感信息
    {"position": "管理层", "domain": "client",    "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层", "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层", "domain": "financial", "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层", "domain": "creative",  "visibility": VisibilityScope.ALL, "output_mask": []},
    # hr: 看聚合视图，不看个人精确薪资
    {"position": "管理层", "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": ["salary_exact", "personal_id", "attendance"]},
    {"position": "管理层", "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},
]

# RoleOutputMask：细粒度的字段级输出遮罩（按角色×数据域×字段）
# 基于 output_mask 字段枚举：show/hide/range/aggregate/rank/truncate/label_only
ROLE_OUTPUT_MASKS = [
    # ── 商务 × client ────────────────────────────────────────────────────────
    {"position": "商务", "domain": "client", "field": "contact_phone",     "action": MaskAction.TRUNCATE},
    {"position": "商务", "domain": "client", "field": "contact_email",     "action": MaskAction.PARTIAL},
    {"position": "商务", "domain": "client", "field": "personal_id",       "action": MaskAction.HIDE},
    {"position": "商务", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},

    # ── 商务 × financial ─────────────────────────────────────────────────────
    {"position": "商务", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "contract_value", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "financial", "field": "payment_status", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "financial", "field": "receivable",     "action": MaskAction.SHOW},

    # ── 商务 × creative ──────────────────────────────────────────────────────
    {"position": "商务", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "商务", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "商务", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "商务", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "creative", "field": "brief_summary",   "action": MaskAction.SHOW},

    # ── 商务 × hr ────────────────────────────────────────────────────────────
    {"position": "商务", "domain": "hr", "field": "salary_exact",          "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "salary_band",           "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "performance_score",     "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "performance_level",     "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "personal_id",           "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "attendance",            "action": MaskAction.HIDE},
    {"position": "商务", "domain": "hr", "field": "employee_name",         "action": MaskAction.HIDE},

    # ── 策划 × client ────────────────────────────────────────────────────────
    {"position": "策划", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "策划", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "策划", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "策划", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "策划", "domain": "client", "field": "contract_status",   "action": MaskAction.HIDE},
    {"position": "策划", "domain": "client", "field": "client_name",       "action": MaskAction.LABEL_ONLY},  # 借阅时只显示品牌
    {"position": "策划", "domain": "client", "field": "brand",             "action": MaskAction.SHOW},

    # ── 策划 × financial ─────────────────────────────────────────────────────
    {"position": "策划", "domain": "financial", "field": "contract_value", "action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "receivable",     "action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "payment_status", "action": MaskAction.HIDE},
    {"position": "策划", "domain": "financial", "field": "budget_exact",   "action": MaskAction.RANGE},   # 精确预算→区间
    {"position": "策划", "domain": "financial", "field": "budget_range",   "action": MaskAction.SHOW},

    # ── 策划 × creative (借阅场景，L3脱敏) ───────────────────────────────────
    {"position": "策划", "domain": "creative", "field": "full_content",    "action": MaskAction.SHOW},    # 自己的全量
    {"position": "策划", "domain": "creative", "field": "raw_script",      "action": MaskAction.SHOW},
    {"position": "策划", "domain": "creative", "field": "brief_summary",   "action": MaskAction.SHOW},
    {"position": "策划", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "策划", "domain": "creative", "field": "campaign_type",   "action": MaskAction.SHOW},
    {"position": "策划", "domain": "creative", "field": "client_name_masked","action": MaskAction.SHOW},  # 脱敏后的客户名

    # ── 策划 × hr ────────────────────────────────────────────────────────────
    {"position": "策划", "domain": "hr", "field": "salary_exact",          "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "salary_band",           "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "performance_score",     "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "performance_level",     "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "personal_id",           "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "attendance",            "action": MaskAction.HIDE},
    {"position": "策划", "domain": "hr", "field": "employee_name",         "action": MaskAction.HIDE},

    # ── 财务 × client ────────────────────────────────────────────────────────
    {"position": "财务", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "history_campaigns", "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "client_name",       "action": MaskAction.SHOW},
    {"position": "财务", "domain": "client", "field": "industry",          "action": MaskAction.SHOW},
    {"position": "财务", "domain": "client", "field": "contract_status",   "action": MaskAction.SHOW},

    # ── 财务 × project ───────────────────────────────────────────────────────
    {"position": "财务", "domain": "project", "field": "brief",            "action": MaskAction.HIDE},
    {"position": "财务", "domain": "project", "field": "creative_content", "action": MaskAction.HIDE},
    {"position": "财务", "domain": "project", "field": "project_name",     "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "status",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "timeline",         "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "department",       "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "headcount",        "action": MaskAction.SHOW},

    # ── 财务 × financial ─────────────────────────────────────────────────────
    {"position": "财务", "domain": "financial", "field": "contract_value", "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "cost",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "margin",         "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "company_revenue","action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "payment_status", "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "receivable",     "action": MaskAction.SHOW},

    # ── 财务 × creative ──────────────────────────────────────────────────────
    {"position": "财务", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "brief_summary",   "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "财务", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},

    # ── 财务 × hr ────────────────────────────────────────────────────────────
    {"position": "财务", "domain": "hr", "field": "salary_exact",          "action": MaskAction.AGGREGATE},  # 个人薪资→部门均值
    {"position": "财务", "domain": "hr", "field": "salary_band",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "headcount",             "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "department",            "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "performance_score",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "performance_level",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "personal_id",           "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "attendance",            "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "employee_name",         "action": MaskAction.HIDE},

    # ── HR × client ──────────────────────────────────────────────────────────
    {"position": "HR", "domain": "client", "field": "client_name",         "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "industry",            "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "brand",               "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_name",        "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_phone",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_email",       "action": MaskAction.HIDE},

    # ── HR × project ─────────────────────────────────────────────────────────
    {"position": "HR", "domain": "project", "field": "brief",              "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "creative_content",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "budget_range",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "project_name",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "department",         "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "headcount",          "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "timeline",           "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "status",             "action": MaskAction.SHOW},

    # ── HR × financial ───────────────────────────────────────────────────────
    {"position": "HR", "domain": "financial", "field": "contract_value",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "payment_status",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "receivable",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "cost",             "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "margin",           "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "company_revenue",  "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "budget_exact",     "action": MaskAction.HIDE},

    # ── HR × creative ────────────────────────────────────────────────────────
    {"position": "HR", "domain": "creative", "field": "full_content",      "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "raw_script",        "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "brief_summary",     "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_title",    "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_status",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_type",     "action": MaskAction.HIDE},

    # ── HR × hr ──────────────────────────────────────────────────────────────
    {"position": "HR", "domain": "hr", "field": "employee_name",           "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "department",              "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "position",                "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "salary_exact",            "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "salary_band",             "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "headcount",               "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "performance_score",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "performance_level",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "personal_id",             "action": MaskAction.TRUNCATE},  # 最后4位
    {"position": "HR", "domain": "hr", "field": "attendance",              "action": MaskAction.SHOW},

    # ── 管理层 × client ──────────────────────────────────────────────────────
    {"position": "管理层", "domain": "client", "field": "client_name",     "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "client", "field": "contact_phone",   "action": MaskAction.TRUNCATE},
    {"position": "管理层", "domain": "client", "field": "contact_email",   "action": MaskAction.PARTIAL},
    {"position": "管理层", "domain": "client", "field": "contract_value",  "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "client", "field": "contract_terms",  "action": MaskAction.SHOW},

    # ── 管理层 × financial ───────────────────────────────────────────────────
    {"position": "管理层", "domain": "financial", "field": "contract_value","action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "cost",          "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "margin",        "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "company_revenue","action": MaskAction.SHOW},

    # ── 管理层 × hr ──────────────────────────────────────────────────────────
    {"position": "管理层", "domain": "hr", "field": "salary_exact",         "action": MaskAction.AGGREGATE},  # 投屏时聚合
    {"position": "管理层", "domain": "hr", "field": "salary_band",          "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "performance_score",    "action": MaskAction.RANK},       # →等级
    {"position": "管理层", "domain": "hr", "field": "performance_level",    "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "personal_id",          "action": MaskAction.HIDE},
    {"position": "管理层", "domain": "hr", "field": "attendance",           "action": MaskAction.HIDE},
    {"position": "管理层", "domain": "hr", "field": "employee_name",        "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "headcount",            "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "department",           "action": MaskAction.SHOW},
]

# 7个高频 Agent 组合静态 Handoff 模板
HANDOFF_TEMPLATES = [
    {
        "name": "T1 项目Brief传递（商务→策划）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None,  # 上游Skill名（seed时无具体Skill，留None）
        "dn_name": None,
        "schema_fields": [
            "client_name", "industry", "brand",
            "project_name", "campaign_type", "timeline",
            "budget_range",          # budget_exact → range
            "brief_summary",
            "target_audience",
            "campaign_goals",
        ],
        "excluded_fields": [
            "contract_value", "cost", "margin",  # 精确财务数据不传策划
            "contact_name", "contact_phone", "contact_email",  # 联系人不传
            "full_content", "raw_script",        # 防止已有创意泄露
        ],
    },
    {
        "name": "T2 案例脱敏借阅（策划→策划，L3）",
        "template_type": HandoffTemplateType.L3_MASK,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            "campaign_title", "campaign_type", "industry",
            "brief_summary",
            "budget_range",           # 预算→区间
            "campaign_status",
            "client_name_masked",     # 品牌名保留，公司名脱敏
        ],
        "excluded_fields": [
            "client_name",            # 原始客户名不传
            "contact_name", "contact_phone", "contact_email",
            "contract_value", "budget_exact",
            "raw_script",             # 未发布脚本不借阅
        ],
    },
    {
        "name": "T3 绩效奖金核算（财务→HR）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            "department", "headcount",
            "budget_range",           # 奖金池→区间
            "performance_level",      # 不传raw_score，只传等级
            "period",                 # 核算周期
        ],
        "excluded_fields": [
            "salary_exact", "performance_score",  # 原始数值降级
            "contract_value", "cost", "margin",
            "employee_name", "personal_id",       # 个人标识不传
        ],
    },
    {
        "name": "T4 人力成本核算（HR→财务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            "department", "headcount",
            "salary_band",            # 薪资→区间，不传精确值
            "headcount_change",       # 人员变动（离职/入职人数，不传姓名）
            "period",
        ],
        "excluded_fields": [
            "salary_exact", "employee_name", "personal_id",
            "performance_score", "performance_level",
            "attendance",
        ],
    },
    {
        "name": "T5 合同结算（商务→财务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            "client_name", "contract_status",
            "contract_value",         # T5 结算场景例外，传精确值
            "payment_status", "receivable",
            "project_name", "timeline",
            "invoice_info",
        ],
        "excluded_fields": [
            "cost", "margin",         # 成本利润不传商务→财务方向
            "contact_phone", "contact_email",
            "brief", "creative_content",
        ],
    },
    {
        "name": "T6 经营看板汇总（多源→管理层）",
        "template_type": HandoffTemplateType.MULTI_UPSTREAM,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            # 财务维度（聚合）
            "total_revenue", "total_cost", "overall_margin",
            "client_count", "contract_count",
            # 项目维度
            "project_count_active", "project_count_delivered",
            "avg_campaign_duration",
            # HR维度（聚合）
            "total_headcount", "dept_headcount_breakdown",
            "avg_performance_level",  # 等级聚合，非原始分
            # 时间维度
            "period", "period_comparison",
        ],
        "excluded_fields": [
            "salary_exact", "performance_score",
            "personal_id", "employee_name",
            "contact_phone", "contact_email",
            "raw_script", "full_content",
        ],
    },
    {
        "name": "T7 提案交付（策划→商务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None,
        "dn_name": None,
        "schema_fields": [
            "campaign_title", "campaign_type",
            "brief_summary",
            "creative_concept",       # 对外可见的创意概念
            "deliverables",           # 交付物清单
            "timeline",
            "budget_range",           # 内部成本→区间
        ],
        "excluded_fields": [
            "cost", "margin",         # 内部成本不传商务
            "raw_script",             # 未定稿脚本不传
            "rejected_concepts",      # 毙案不传
            "performance_score",
        ],
    },
]


# ─── Seed 执行 ────────────────────────────────────────────────────────────────

def seed_permissions():
    db = SessionLocal()
    try:
        # 幂等检查
        if db.query(Position).filter(Position.name == "商务").first():
            print("权限系统 seed 数据已存在，跳过。")
            print("如需重跑，请先清空对应表。")
            db.close()
            return

        print("开始写入权限系统 seed 数据...")

        # 1. Positions
        print("  [1/6] 写入岗位（Position）...")
        pos_map: dict[str, Position] = {}
        for p in POSITIONS:
            pos = Position(name=p["name"], description=p["description"])
            db.add(pos)
            db.flush()
            pos_map[p["name"]] = pos
        print(f"        已写入 {len(pos_map)} 个岗位")

        # 2. DataDomains
        print("  [2/6] 写入数据域（DataDomain）...")
        domain_map: dict[str, DataDomain] = {}
        for d in DATA_DOMAINS:
            domain = DataDomain(
                name=d["name"],
                display_name=d["display_name"],
                description=d["description"],
                fields=d["fields"],
            )
            db.add(domain)
            db.flush()
            domain_map[d["name"]] = domain
        print(f"        已写入 {len(domain_map)} 个数据域")

        # 3. GlobalDataMasks
        print("  [3/6] 写入全局脱敏规则（GlobalDataMask）...")
        for m in GLOBAL_MASKS:
            mask = GlobalDataMask(
                field_name=m["field_name"],
                mask_action=m["mask_action"],
                mask_params=m["mask_params"],
                severity=m["severity"],
            )
            db.add(mask)
        db.flush()
        print(f"        已写入 {len(GLOBAL_MASKS)} 条全局脱敏规则")

        # 4. DataScopePolicies
        print("  [4/6] 写入数据范围策略（DataScopePolicy）...")
        scope_count = 0
        for sp in DATA_SCOPE_POLICIES:
            pos = pos_map[sp["position"]]
            domain = domain_map[sp["domain"]]
            policy = DataScopePolicy(
                target_type=PolicyTargetType.POSITION,
                target_position_id=pos.id,
                resource_type=PolicyResourceType.DATA_DOMAIN,
                data_domain_id=domain.id,
                visibility_level=sp["visibility"],
                output_mask=sp["output_mask"],
            )
            db.add(policy)
            scope_count += 1
        db.flush()
        print(f"        已写入 {scope_count} 条数据范围策略（5角色 × 6域）")

        # 5. RoleOutputMasks
        print("  [5/6] 写入角色输出遮罩（RoleOutputMask）...")
        mask_count = 0
        # 用 (position_id, domain_id, field_name) 去重
        seen = set()
        for m in ROLE_OUTPUT_MASKS:
            pos = pos_map[m["position"]]
            domain = domain_map[m["domain"]]
            key = (pos.id, domain.id, m["field"])
            if key in seen:
                continue
            seen.add(key)
            output_mask = RoleOutputMask(
                position_id=pos.id,
                data_domain_id=domain.id,
                field_name=m["field"],
                mask_action=m["action"],
            )
            db.add(output_mask)
            mask_count += 1
        db.flush()
        print(f"        已写入 {mask_count} 条角色输出遮罩")

        # 6. HandoffTemplates
        print("  [6/6] 写入 Handoff 静态模板（HandoffTemplate）...")
        for t in HANDOFF_TEMPLATES:
            tmpl = HandoffTemplate(
                name=t["name"],
                template_type=t["template_type"],
                schema_fields=t["schema_fields"],
                excluded_fields=t["excluded_fields"],
                upstream_skill_id=None,
                downstream_skill_id=None,
            )
            db.add(tmpl)
        db.flush()
        print(f"        已写入 {len(HANDOFF_TEMPLATES)} 个 Handoff 静态模板")

        db.commit()
        print("\nSeed 完成！")
        print(f"  岗位:           {len(pos_map)} 个")
        print(f"  数据域:         {len(domain_map)} 个")
        print(f"  全局脱敏规则:   {len(GLOBAL_MASKS)} 条")
        print(f"  数据范围策略:   {scope_count} 条")
        print(f"  角色输出遮罩:   {mask_count} 条")
        print(f"  Handoff模板:    {len(HANDOFF_TEMPLATES)} 个")

    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] Seed 失败: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_permissions()
