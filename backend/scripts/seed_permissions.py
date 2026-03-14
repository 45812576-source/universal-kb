"""权限系统 Seed 数据（v2 - 9岗位版本）
- 9个岗位（Position）：商务/媒介/运营/创意/产研/客户成功/财务/HR/管理层
- 6个数据域（DataDomain）：client/project/financial/creative/hr/knowledge
- GlobalDataMask ~15条：全局字段脱敏默认规则
- DataScopePolicy 54条：9角色 × 6域的可见范围
- RoleOutputMask ~300条：9角色 × 6域 × 各字段的输出遮罩
- HandoffTemplate 7个：高频 Agent 组合静态模板

运行方式（从 backend 目录）：
  conda run -n base python scripts/seed_permissions.py

增量模式：已有岗位不会重建，只补建新增岗位和关联规则。
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
    # 前台业务
    {"name": "商务",     "description": "客户开发、商机跟进、合同签约、回款管理、客户关系维护续约"},
    {"name": "媒介",     "description": "代理商管理、客户开户/充值/风控、平台结算对账、返点核算、资源管理"},
    {"name": "运营",     "description": "广告投放策略与执行、ROI分析优化、产品运营、用户增长、数据分析"},
    {"name": "创意",     "description": "短视频/图文创意策划、素材拍摄剪辑制作、素材效果跟踪与迭代优化"},
    {"name": "客户成功",  "description": "客户交付对接、使用培训、问题响应、需求收集反馈、续费运营"},
    # 中台产研
    {"name": "产研",     "description": "产品需求/设计/迭代、前后端开发、测试质量保障、技术架构"},
    # 后台职能
    {"name": "财务",     "description": "账务处理、费用审核、预算监控、税务申报、成本核算、各事业部盈利分析"},
    {"name": "HR",      "description": "招聘、绩效、培训、薪酬福利、员工关系、行政事务"},
    {"name": "管理层",   "description": "高管/总监级，战略决策，全域数据聚合视图权限"},
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

# 全局脱敏规则（15条）
GLOBAL_MASKS = [
    {"field_name": "contact_phone",     "mask_action": MaskAction.TRUNCATE,  "mask_params": {"length": 7, "suffix": "****"}, "severity": 3},
    {"field_name": "contact_email",     "mask_action": MaskAction.PARTIAL,   "mask_params": {"prefix_len": 3},               "severity": 3},
    {"field_name": "contact_name",      "mask_action": MaskAction.PARTIAL,   "mask_params": {"prefix_len": 1},               "severity": 2},
    {"field_name": "personal_id",       "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 5},
    {"field_name": "contract_value",    "mask_action": MaskAction.RANGE,     "mask_params": {"step": 100000},                "severity": 4},
    {"field_name": "cost",              "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "margin",            "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "company_revenue",   "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 5},
    {"field_name": "budget_exact",      "mask_action": MaskAction.RANGE,     "mask_params": {"step": 50000},                 "severity": 4},
    {"field_name": "salary_exact",      "mask_action": MaskAction.AGGREGATE, "mask_params": {"aggregate_label": "部门薪资均值范围"}, "severity": 5},
    {"field_name": "performance_score", "mask_action": MaskAction.RANK,      "mask_params": {"rank_label": "绩效等级"},      "severity": 3},
    {"field_name": "attendance",        "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
    {"field_name": "contract_terms",    "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 4},
    {"field_name": "raw_script",        "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
    {"field_name": "full_content",      "mask_action": MaskAction.HIDE,      "mask_params": {},                              "severity": 3},
]

# ─── 无 HR 权限的公共 output_mask ──────────────────────────────────────────────
_HR_HIDE_ALL = ["salary_exact", "salary_band", "performance_score", "performance_level", "personal_id", "attendance", "employee_name"]

# DataScopePolicy：9角色 × 6域 = 54条
DATA_SCOPE_POLICIES = [
    # ── 商务 ─────────────────────────────────────────────────────────────────
    # 看自己客户全貌，合同条款隐藏；看自己客户财务（不含成本利润）；不碰创意原文；无HR
    {"position": "商务",    "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contract_terms"]},
    {"position": "商务",    "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "creative_content"]},
    {"position": "商务",    "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["cost", "margin", "company_revenue"]},
    {"position": "商务",    "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script"]},
    {"position": "商务",    "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "商务",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 媒介 ─────────────────────────────────────────────────────────────────
    # 看全部客户开户/账户信息，看合同状态和回款（结算对账需要），不看成本利润
    # 不碰创意/HR
    {"position": "媒介",    "domain": "client",    "visibility": VisibilityScope.ALL, "output_mask": ["contract_terms"]},
    {"position": "媒介",    "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": ["brief", "creative_content", "full_content"]},
    {"position": "媒介",    "domain": "financial", "visibility": VisibilityScope.ALL, "output_mask": ["cost", "margin", "company_revenue"]},
    {"position": "媒介",    "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "brief_summary"]},
    {"position": "媒介",    "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "媒介",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 运营 ─────────────────────────────────────────────────────────────────
    # 看分配客户/项目，看投放预算区间，看创意素材效果数据；不看合同/成本/HR
    {"position": "运营",    "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contact_name", "contact_phone", "contact_email", "contract_terms"]},
    {"position": "运营",    "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": []},
    {"position": "运营",    "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "cost", "margin", "company_revenue", "receivable", "payment_status", "budget_exact"]},
    {"position": "运营",    "domain": "creative",  "visibility": VisibilityScope.DEPT, "output_mask": ["raw_script"]},
    {"position": "运营",    "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "运营",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 创意 ─────────────────────────────────────────────────────────────────
    # 创意全量（含L3借阅脱敏），看项目Brief，不看合同/财务/HR
    {"position": "创意",    "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contact_name", "contact_phone", "contact_email", "contract_terms", "contract_status"]},
    {"position": "创意",    "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": []},
    {"position": "创意",    "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "cost", "margin", "company_revenue", "receivable", "payment_status", "budget_exact"]},
    {"position": "创意",    "domain": "creative",  "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "创意",    "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "创意",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 产研 ─────────────────────────────────────────────────────────────────
    # 看分配项目技术需求/排期，不碰客户联系人/合同/财务/创意原文/HR
    {"position": "产研",    "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contact_name", "contact_phone", "contact_email", "contract_terms", "contract_status", "history_campaigns"]},
    {"position": "产研",    "domain": "project",   "visibility": VisibilityScope.DEPT, "output_mask": ["creative_content"]},
    {"position": "产研",    "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "cost", "margin", "company_revenue", "receivable", "payment_status", "budget_exact"]},
    {"position": "产研",    "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script"]},
    {"position": "产研",    "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "产研",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 客户成功 ──────────────────────────────────────────────────────────────
    # 看分配客户信息（交付对接），看项目进度，看预算区间，不看完整创意/HR
    {"position": "客户成功", "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["contract_terms"]},
    {"position": "客户成功", "domain": "project",   "visibility": VisibilityScope.OWN, "output_mask": ["creative_content", "full_content"]},
    {"position": "客户成功", "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["cost", "margin", "company_revenue", "budget_exact"]},
    {"position": "客户成功", "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script"]},
    {"position": "客户成功", "domain": "hr",        "visibility": VisibilityScope.OWN, "output_mask": _HR_HIDE_ALL},
    {"position": "客户成功", "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 财务 ─────────────────────────────────────────────────────────────────
    # 看全部客户合同状态，全量财务，项目元信息，HR成本粒度
    {"position": "财务",    "domain": "client",    "visibility": VisibilityScope.ALL, "output_mask": ["contact_name", "contact_phone", "contact_email", "history_campaigns", "contract_terms"]},
    {"position": "财务",    "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": ["brief", "creative_content", "full_content"]},
    {"position": "财务",    "domain": "financial", "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "财务",    "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "brief_summary"]},
    {"position": "财务",    "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": ["salary_exact", "performance_score", "personal_id", "attendance", "employee_name"]},
    {"position": "财务",    "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── HR ────────────────────────────────────────────────────────────────────
    # HR全量，项目只看人力元信息，不碰客户/财务/创意
    {"position": "HR",     "domain": "client",    "visibility": VisibilityScope.OWN, "output_mask": ["client_name", "industry", "brand", "contact_name", "contact_phone", "contact_email", "contract_terms", "history_campaigns", "contract_status"]},
    {"position": "HR",     "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": ["brief", "creative_content", "full_content", "budget_range"]},
    {"position": "HR",     "domain": "financial", "visibility": VisibilityScope.OWN, "output_mask": ["contract_value", "payment_status", "receivable", "cost", "margin", "company_revenue", "budget_exact"]},
    {"position": "HR",     "domain": "creative",  "visibility": VisibilityScope.OWN, "output_mask": ["full_content", "raw_script", "brief_summary", "campaign_title"]},
    {"position": "HR",     "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "HR",     "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},

    # ── 管理层 ────────────────────────────────────────────────────────────────
    # 全域all，输出侧遮罩个人敏感信息
    {"position": "管理层",  "domain": "client",    "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层",  "domain": "project",   "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层",  "domain": "financial", "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层",  "domain": "creative",  "visibility": VisibilityScope.ALL, "output_mask": []},
    {"position": "管理层",  "domain": "hr",        "visibility": VisibilityScope.ALL, "output_mask": ["salary_exact", "personal_id", "attendance"]},
    {"position": "管理层",  "domain": "knowledge", "visibility": VisibilityScope.ALL, "output_mask": []},
]

# ─── RoleOutputMask：细粒度字段级遮罩 ──────────────────────────────────────────
# 公共模板：无HR权限的角色（商务/媒介/运营/创意/产研/客户成功）
def _hr_hide_masks(pos: str):
    return [
        {"position": pos, "domain": "hr", "field": f, "action": MaskAction.HIDE}
        for f in _HR_HIDE_ALL
    ]

ROLE_OUTPUT_MASKS = [
    # ══════════════════════════════════════════════════════════════════════════
    # 商务
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "商务", "domain": "client", "field": "contact_phone",     "action": MaskAction.TRUNCATE},
    {"position": "商务", "domain": "client", "field": "contact_email",     "action": MaskAction.PARTIAL},
    {"position": "商务", "domain": "client", "field": "personal_id",       "action": MaskAction.HIDE},
    {"position": "商务", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    # financial
    {"position": "商务", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "商务", "domain": "financial", "field": "contract_value", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "financial", "field": "payment_status", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "financial", "field": "receivable",     "action": MaskAction.SHOW},
    # creative
    {"position": "商务", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "商务", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "商务", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "商务", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},
    {"position": "商务", "domain": "creative", "field": "brief_summary",   "action": MaskAction.SHOW},
    *_hr_hide_masks("商务"),

    # ══════════════════════════════════════════════════════════════════════════
    # 媒介 — 结算对账需要看合同金额/回款，不看成本利润
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "媒介", "domain": "client", "field": "contact_phone",     "action": MaskAction.TRUNCATE},
    {"position": "媒介", "domain": "client", "field": "contact_email",     "action": MaskAction.PARTIAL},
    {"position": "媒介", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "client", "field": "client_name",       "action": MaskAction.SHOW},
    {"position": "媒介", "domain": "client", "field": "contract_status",   "action": MaskAction.SHOW},
    # financial
    {"position": "媒介", "domain": "financial", "field": "contract_value", "action": MaskAction.SHOW},
    {"position": "媒介", "domain": "financial", "field": "payment_status", "action": MaskAction.SHOW},
    {"position": "媒介", "domain": "financial", "field": "receivable",     "action": MaskAction.SHOW},
    {"position": "媒介", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    # project
    {"position": "媒介", "domain": "project", "field": "brief",            "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "project", "field": "creative_content", "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "project", "field": "project_name",     "action": MaskAction.SHOW},
    {"position": "媒介", "domain": "project", "field": "status",           "action": MaskAction.SHOW},
    # creative
    {"position": "媒介", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "媒介", "domain": "creative", "field": "brief_summary",   "action": MaskAction.HIDE},
    *_hr_hide_masks("媒介"),

    # ══════════════════════════════════════════════════════════════════════════
    # 运营 — 投放数据/ROI/预算区间，不看合同精确值和成本
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "运营", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "运营", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "运营", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "运营", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "运营", "domain": "client", "field": "client_name",       "action": MaskAction.SHOW},
    {"position": "运营", "domain": "client", "field": "brand",             "action": MaskAction.SHOW},
    {"position": "运营", "domain": "client", "field": "industry",          "action": MaskAction.SHOW},
    # financial — 只看预算区间
    {"position": "运营", "domain": "financial", "field": "contract_value", "action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "receivable",     "action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "payment_status", "action": MaskAction.HIDE},
    {"position": "运营", "domain": "financial", "field": "budget_exact",   "action": MaskAction.RANGE},
    {"position": "运营", "domain": "financial", "field": "budget_range",   "action": MaskAction.SHOW},
    # creative — 看素材效果，隐藏原始脚本
    {"position": "运营", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "运营", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},
    {"position": "运营", "domain": "creative", "field": "brief_summary",   "action": MaskAction.SHOW},
    {"position": "运营", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "运营", "domain": "creative", "field": "full_content",    "action": MaskAction.SHOW},
    *_hr_hide_masks("运营"),

    # ══════════════════════════════════════════════════════════════════════════
    # 创意 — 创意全量（含L3借阅），客户极度受限
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "创意", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "创意", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "创意", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "创意", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "创意", "domain": "client", "field": "contract_status",   "action": MaskAction.HIDE},
    {"position": "创意", "domain": "client", "field": "client_name",       "action": MaskAction.LABEL_ONLY},
    {"position": "创意", "domain": "client", "field": "brand",             "action": MaskAction.SHOW},
    # financial — 只看预算区间
    {"position": "创意", "domain": "financial", "field": "contract_value", "action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "receivable",     "action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "payment_status", "action": MaskAction.HIDE},
    {"position": "创意", "domain": "financial", "field": "budget_exact",   "action": MaskAction.RANGE},
    {"position": "创意", "domain": "financial", "field": "budget_range",   "action": MaskAction.SHOW},
    # creative — 全量
    {"position": "创意", "domain": "creative", "field": "full_content",    "action": MaskAction.SHOW},
    {"position": "创意", "domain": "creative", "field": "raw_script",      "action": MaskAction.SHOW},
    {"position": "创意", "domain": "creative", "field": "brief_summary",   "action": MaskAction.SHOW},
    {"position": "创意", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "创意", "domain": "creative", "field": "campaign_type",   "action": MaskAction.SHOW},
    {"position": "创意", "domain": "creative", "field": "client_name_masked","action": MaskAction.SHOW},
    *_hr_hide_masks("创意"),

    # ══════════════════════════════════════════════════════════════════════════
    # 产研 — 技术需求/排期，不碰商业数据
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "产研", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "contract_status",   "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "history_campaigns", "action": MaskAction.HIDE},
    {"position": "产研", "domain": "client", "field": "client_name",       "action": MaskAction.SHOW},
    {"position": "产研", "domain": "client", "field": "brand",             "action": MaskAction.SHOW},
    {"position": "产研", "domain": "client", "field": "industry",          "action": MaskAction.SHOW},
    # project
    {"position": "产研", "domain": "project", "field": "project_name",     "action": MaskAction.SHOW},
    {"position": "产研", "domain": "project", "field": "status",           "action": MaskAction.SHOW},
    {"position": "产研", "domain": "project", "field": "timeline",         "action": MaskAction.SHOW},
    {"position": "产研", "domain": "project", "field": "brief",            "action": MaskAction.SHOW},
    {"position": "产研", "domain": "project", "field": "creative_content", "action": MaskAction.HIDE},
    # financial — 全隐藏
    {"position": "产研", "domain": "financial", "field": "contract_value", "action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "cost",           "action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "margin",         "action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "receivable",     "action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "payment_status", "action": MaskAction.HIDE},
    {"position": "产研", "domain": "financial", "field": "budget_exact",   "action": MaskAction.HIDE},
    # creative
    {"position": "产研", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "产研", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "产研", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "产研", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},
    *_hr_hide_masks("产研"),

    # ══════════════════════════════════════════════════════════════════════════
    # 客户成功 — 交付对接，看客户/项目进度/预算区间，不看创意原文
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "客户成功", "domain": "client", "field": "contact_phone",  "action": MaskAction.TRUNCATE},
    {"position": "客户成功", "domain": "client", "field": "contact_email",  "action": MaskAction.PARTIAL},
    {"position": "客户成功", "domain": "client", "field": "contract_terms", "action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "client", "field": "client_name",    "action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "client", "field": "contact_name",   "action": MaskAction.SHOW},
    # project
    {"position": "客户成功", "domain": "project", "field": "project_name",  "action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "project", "field": "status",        "action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "project", "field": "timeline",      "action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "project", "field": "creative_content","action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "project", "field": "full_content",  "action": MaskAction.HIDE},
    # financial
    {"position": "客户成功", "domain": "financial", "field": "contract_value","action": MaskAction.RANGE},
    {"position": "客户成功", "domain": "financial", "field": "payment_status","action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "financial", "field": "cost",         "action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "financial", "field": "margin",       "action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "financial", "field": "company_revenue","action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "financial", "field": "budget_exact", "action": MaskAction.HIDE},
    # creative
    {"position": "客户成功", "domain": "creative", "field": "full_content",  "action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "creative", "field": "raw_script",    "action": MaskAction.HIDE},
    {"position": "客户成功", "domain": "creative", "field": "campaign_title","action": MaskAction.SHOW},
    {"position": "客户成功", "domain": "creative", "field": "campaign_status","action": MaskAction.SHOW},
    *_hr_hide_masks("客户成功"),

    # ══════════════════════════════════════════════════════════════════════════
    # 财务 — 全量财务，客户合同状态，HR成本粒度
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "财务", "domain": "client", "field": "contact_name",      "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contact_phone",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contact_email",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "history_campaigns", "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "contract_terms",    "action": MaskAction.HIDE},
    {"position": "财务", "domain": "client", "field": "client_name",       "action": MaskAction.SHOW},
    {"position": "财务", "domain": "client", "field": "industry",          "action": MaskAction.SHOW},
    {"position": "财务", "domain": "client", "field": "contract_status",   "action": MaskAction.SHOW},
    # project
    {"position": "财务", "domain": "project", "field": "brief",            "action": MaskAction.HIDE},
    {"position": "财务", "domain": "project", "field": "creative_content", "action": MaskAction.HIDE},
    {"position": "财务", "domain": "project", "field": "project_name",     "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "status",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "timeline",         "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "department",       "action": MaskAction.SHOW},
    {"position": "财务", "domain": "project", "field": "headcount",        "action": MaskAction.SHOW},
    # financial — 全量
    {"position": "财务", "domain": "financial", "field": "contract_value", "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "cost",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "margin",         "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "company_revenue","action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "payment_status", "action": MaskAction.SHOW},
    {"position": "财务", "domain": "financial", "field": "receivable",     "action": MaskAction.SHOW},
    # creative
    {"position": "财务", "domain": "creative", "field": "full_content",    "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "raw_script",      "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "brief_summary",   "action": MaskAction.HIDE},
    {"position": "财务", "domain": "creative", "field": "campaign_title",  "action": MaskAction.SHOW},
    {"position": "财务", "domain": "creative", "field": "campaign_status", "action": MaskAction.SHOW},
    # hr — 成本粒度
    {"position": "财务", "domain": "hr", "field": "salary_exact",          "action": MaskAction.AGGREGATE},
    {"position": "财务", "domain": "hr", "field": "salary_band",           "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "headcount",             "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "department",            "action": MaskAction.SHOW},
    {"position": "财务", "domain": "hr", "field": "performance_score",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "performance_level",     "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "personal_id",           "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "attendance",            "action": MaskAction.HIDE},
    {"position": "财务", "domain": "hr", "field": "employee_name",         "action": MaskAction.HIDE},

    # ══════════════════════════════════════════════════════════════════════════
    # HR — 人事全量，业务侧只看人力元信息
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "HR", "domain": "client", "field": "client_name",         "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "industry",            "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "brand",               "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_name",        "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_phone",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "client", "field": "contact_email",       "action": MaskAction.HIDE},
    # project
    {"position": "HR", "domain": "project", "field": "brief",              "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "creative_content",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "budget_range",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "project", "field": "project_name",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "department",         "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "headcount",          "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "timeline",           "action": MaskAction.SHOW},
    {"position": "HR", "domain": "project", "field": "status",             "action": MaskAction.SHOW},
    # financial
    {"position": "HR", "domain": "financial", "field": "contract_value",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "payment_status",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "receivable",       "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "cost",             "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "margin",           "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "company_revenue",  "action": MaskAction.HIDE},
    {"position": "HR", "domain": "financial", "field": "budget_exact",     "action": MaskAction.HIDE},
    # creative
    {"position": "HR", "domain": "creative", "field": "full_content",      "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "raw_script",        "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "brief_summary",     "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_title",    "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_status",   "action": MaskAction.HIDE},
    {"position": "HR", "domain": "creative", "field": "campaign_type",     "action": MaskAction.HIDE},
    # hr — 全量
    {"position": "HR", "domain": "hr", "field": "employee_name",           "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "department",              "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "position",                "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "salary_exact",            "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "salary_band",             "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "headcount",               "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "performance_score",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "performance_level",       "action": MaskAction.SHOW},
    {"position": "HR", "domain": "hr", "field": "personal_id",             "action": MaskAction.TRUNCATE},
    {"position": "HR", "domain": "hr", "field": "attendance",              "action": MaskAction.SHOW},

    # ══════════════════════════════════════════════════════════════════════════
    # 管理层 — 全域，输出侧遮罩个人敏感
    # ══════════════════════════════════════════════════════════════════════════
    {"position": "管理层", "domain": "client", "field": "client_name",     "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "client", "field": "contact_phone",   "action": MaskAction.TRUNCATE},
    {"position": "管理层", "domain": "client", "field": "contact_email",   "action": MaskAction.PARTIAL},
    {"position": "管理层", "domain": "client", "field": "contract_value",  "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "client", "field": "contract_terms",  "action": MaskAction.SHOW},
    # financial
    {"position": "管理层", "domain": "financial", "field": "contract_value","action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "cost",          "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "margin",        "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "financial", "field": "company_revenue","action": MaskAction.SHOW},
    # hr
    {"position": "管理层", "domain": "hr", "field": "salary_exact",         "action": MaskAction.AGGREGATE},
    {"position": "管理层", "domain": "hr", "field": "salary_band",          "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "performance_score",    "action": MaskAction.RANK},
    {"position": "管理层", "domain": "hr", "field": "performance_level",    "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "personal_id",          "action": MaskAction.HIDE},
    {"position": "管理层", "domain": "hr", "field": "attendance",           "action": MaskAction.HIDE},
    {"position": "管理层", "domain": "hr", "field": "employee_name",        "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "headcount",            "action": MaskAction.SHOW},
    {"position": "管理层", "domain": "hr", "field": "department",           "action": MaskAction.SHOW},
]

# 7个高频 Agent 组合静态 Handoff 模板（保持不变）
HANDOFF_TEMPLATES = [
    {
        "name": "T1 项目Brief传递（商务→创意）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None, "dn_name": None,
        "schema_fields": ["client_name", "industry", "brand", "project_name", "campaign_type", "timeline", "budget_range", "brief_summary", "target_audience", "campaign_goals"],
        "excluded_fields": ["contract_value", "cost", "margin", "contact_name", "contact_phone", "contact_email", "full_content", "raw_script"],
    },
    {
        "name": "T2 案例脱敏借阅（创意→创意，L3）",
        "template_type": HandoffTemplateType.L3_MASK,
        "up_name": None, "dn_name": None,
        "schema_fields": ["campaign_title", "campaign_type", "industry", "brief_summary", "budget_range", "campaign_status", "client_name_masked"],
        "excluded_fields": ["client_name", "contact_name", "contact_phone", "contact_email", "contract_value", "budget_exact", "raw_script"],
    },
    {
        "name": "T3 绩效奖金核算（财务→HR）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None, "dn_name": None,
        "schema_fields": ["department", "headcount", "budget_range", "performance_level", "period"],
        "excluded_fields": ["salary_exact", "performance_score", "contract_value", "cost", "margin", "employee_name", "personal_id"],
    },
    {
        "name": "T4 人力成本核算（HR→财务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None, "dn_name": None,
        "schema_fields": ["department", "headcount", "salary_band", "headcount_change", "period"],
        "excluded_fields": ["salary_exact", "employee_name", "personal_id", "performance_score", "performance_level", "attendance"],
    },
    {
        "name": "T5 合同结算（商务→财务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None, "dn_name": None,
        "schema_fields": ["client_name", "contract_status", "contract_value", "payment_status", "receivable", "project_name", "timeline", "invoice_info"],
        "excluded_fields": ["cost", "margin", "contact_phone", "contact_email", "brief", "creative_content"],
    },
    {
        "name": "T6 经营看板汇总（多源→管理层）",
        "template_type": HandoffTemplateType.MULTI_UPSTREAM,
        "up_name": None, "dn_name": None,
        "schema_fields": ["total_revenue", "total_cost", "overall_margin", "client_count", "contract_count", "project_count_active", "project_count_delivered", "avg_campaign_duration", "total_headcount", "dept_headcount_breakdown", "avg_performance_level", "period", "period_comparison"],
        "excluded_fields": ["salary_exact", "performance_score", "personal_id", "employee_name", "contact_phone", "contact_email", "raw_script", "full_content"],
    },
    {
        "name": "T7 投放数据同步（运营→商务）",
        "template_type": HandoffTemplateType.STANDARD,
        "up_name": None, "dn_name": None,
        "schema_fields": ["campaign_title", "campaign_type", "brief_summary", "roi_summary", "spend_summary", "timeline", "budget_range"],
        "excluded_fields": ["cost", "margin", "raw_script", "full_content", "performance_score"],
    },
]


# ─── Seed 执行（增量模式）──────────────────────────────────────────────────────

def seed_permissions():
    db = SessionLocal()
    try:
        # 加载已有岗位
        existing_pos = {p.name: p for p in db.query(Position).all()}
        existing_domains = {d.name: d for d in db.query(DataDomain).all()}

        new_positions = False
        print("开始写入权限系统 seed 数据（增量模式）...")

        # 1. Positions — 补建新增
        print("  [1/6] 检查/补建岗位（Position）...")
        pos_map = dict(existing_pos)
        for p in POSITIONS:
            if p["name"] not in pos_map:
                pos = Position(name=p["name"], description=p["description"])
                db.add(pos)
                db.flush()
                pos_map[p["name"]] = pos
                new_positions = True
                print(f"        新增岗位: {p['name']}")
            else:
                # 更新描述
                existing = pos_map[p["name"]]
                if existing.description != p["description"]:
                    existing.description = p["description"]
        db.flush()
        print(f"        共 {len(pos_map)} 个岗位")

        # 2. DataDomains — 补建
        print("  [2/6] 检查/补建数据域（DataDomain）...")
        domain_map = dict(existing_domains)
        for d in DATA_DOMAINS:
            if d["name"] not in domain_map:
                domain = DataDomain(name=d["name"], display_name=d["display_name"], description=d["description"], fields=d["fields"])
                db.add(domain)
                db.flush()
                domain_map[d["name"]] = domain
                print(f"        新增数据域: {d['name']}")
        print(f"        共 {len(domain_map)} 个数据域")

        # 3. GlobalDataMasks — 幂等（按field_name判断）
        print("  [3/6] 检查/补建全局脱敏规则...")
        existing_masks = {m.field_name for m in db.query(GlobalDataMask).all()}
        added_masks = 0
        for m in GLOBAL_MASKS:
            if m["field_name"] not in existing_masks:
                db.add(GlobalDataMask(field_name=m["field_name"], mask_action=m["mask_action"], mask_params=m["mask_params"], severity=m["severity"]))
                added_masks += 1
        db.flush()
        print(f"        新增 {added_masks} 条（总计 {len(existing_masks) + added_masks} 条）")

        # 4. DataScopePolicies — 清除旧的再全量写入
        print("  [4/6] 写入数据范围策略...")
        db.query(DataScopePolicy).delete()
        db.flush()
        scope_count = 0
        for sp in DATA_SCOPE_POLICIES:
            pos = pos_map[sp["position"]]
            domain = domain_map[sp["domain"]]
            db.add(DataScopePolicy(
                target_type=PolicyTargetType.POSITION,
                target_position_id=pos.id,
                resource_type=PolicyResourceType.DATA_DOMAIN,
                data_domain_id=domain.id,
                visibility_level=sp["visibility"],
                output_mask=sp["output_mask"],
            ))
            scope_count += 1
        db.flush()
        print(f"        已写入 {scope_count} 条（9角色 × 6域）")

        # 5. RoleOutputMasks — 清除旧的再全量写入
        print("  [5/6] 写入角色输出遮罩...")
        db.query(RoleOutputMask).delete()
        db.flush()
        mask_count = 0
        seen = set()
        for m in ROLE_OUTPUT_MASKS:
            pos = pos_map[m["position"]]
            domain = domain_map[m["domain"]]
            key = (pos.id, domain.id, m["field"])
            if key in seen:
                continue
            seen.add(key)
            db.add(RoleOutputMask(position_id=pos.id, data_domain_id=domain.id, field_name=m["field"], mask_action=m["action"]))
            mask_count += 1
        db.flush()
        print(f"        已写入 {mask_count} 条")

        # 6. HandoffTemplates — 清除旧的再全量写入
        print("  [6/6] 写入 Handoff 模板...")
        db.query(HandoffTemplate).delete()
        db.flush()
        for t in HANDOFF_TEMPLATES:
            db.add(HandoffTemplate(
                name=t["name"], template_type=t["template_type"],
                schema_fields=t["schema_fields"], excluded_fields=t["excluded_fields"],
                upstream_skill_id=None, downstream_skill_id=None,
            ))
        db.flush()
        print(f"        已写入 {len(HANDOFF_TEMPLATES)} 个模板")

        db.commit()
        print(f"\nSeed 完成！")
        print(f"  岗位:           {len(pos_map)} 个")
        print(f"  数据域:         {len(domain_map)} 个")
        print(f"  数据范围策略:   {scope_count} 条")
        print(f"  角色输出遮罩:   {mask_count} 条")
        print(f"  Handoff模板:    {len(HANDOFF_TEMPLATES)} 个")

    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] Seed 失败: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_permissions()
