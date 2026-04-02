"""数据类型字典、脱敏规则、文档类型枚举、受控内容标签词表。

供文档理解流水线使用：
  - DATA_TYPE_REGISTRY: 通用数据类型 → 默认脱敏动作/级别/摘要规则
  - DOCUMENT_TYPES: 受控文档类型枚举
  - PERMISSION_DOMAINS: 权限域枚举（含中文标签和权限文案）
  - DESENSITIZATION_LEVELS: 脱敏级别枚举 D0~D4
  - SCENARIO_OVERRIDES: 业务场景特例
  - COMBO_ESCALATION_RULES: 组合升档规则
  - TAXONOMY_DOCTYPE_MAP: taxonomy board → 默认 document_type 映射
  - CONTENT_TAG_VOCABULARY: 5维受控标签词表 + fallback
"""
from __future__ import annotations

import re
from typing import Any

_LEVEL_ORDER = {"D0": 0, "D1": 1, "D2": 2, "D3": 3, "D4": 4}

# ════════════════════════════════════════════════════════════════════════════════
# 1. 通用数据类型字典（7大类 40+ 类型）
# ════════════════════════════════════════════════════════════════════════════════

DATA_TYPE_REGISTRY: dict[str, dict[str, Any]] = {
    # ── 个人身份类 ──────────────────────────────────────────────────────────
    "person_name": {
        "label": "人名",
        "keywords": ["姓名", "联系人", "负责人"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D1",
        "display_rule": "替换为\"某某\"",
        "summary_rule": "不出现在摘要中",
        "share_rule": "脱敏后可分享",
    },
    "phone_number": {
        "label": "手机号码",
        "pattern": re.compile(r"1[3-9]\d{9}"),
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D1",
        "display_rule": "中间4位掩码",
        "summary_rule": "不出现在摘要中",
        "share_rule": "脱敏后可分享",
    },
    "email": {
        "label": "邮箱地址",
        "pattern": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D1",
        "display_rule": "用户名部分掩码",
        "summary_rule": "不出现在摘要中",
        "share_rule": "脱敏后可分享",
    },
    "id_card": {
        "label": "身份证号",
        "pattern": re.compile(r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"),
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D3",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止外部分享",
    },
    "passport_number": {
        "label": "护照号",
        "pattern": re.compile(r"(?:护照[号码]*|passport\s*(?:no|number)?)\s*[:：]?\s*([A-Z][A-Z0-9]{5,9})"),
        "keywords": ["护照号", "passport"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D3",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止外部分享",
    },
    "address": {
        "label": "地址",
        "keywords": ["家庭住址", "居住地址", "联系地址", "通讯地址"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D2",
        "display_rule": "仅显示省市",
        "summary_rule": "不出现在摘要中",
        "share_rule": "脱敏后可分享",
    },
    "birthday": {
        "label": "出生日期",
        "keywords": ["出生日期", "生日", "出生年月"],
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D2",
        "display_rule": "仅显示年份",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "personal_info": {
        "label": "个人隐私",
        "keywords": ["个人信息", "隐私", "户籍"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D3",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止外部分享",
    },

    # ── 公司与账户类 ────────────────────────────────────────────────────────
    "company_name": {
        "label": "公司名称",
        "keywords": ["公司名称", "企业名称", "公司全称"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D1",
        "display_rule": "替换为\"某公司\"",
        "summary_rule": "用行业代称替代",
        "share_rule": "脱敏后可分享",
    },
    "bank_account": {
        "label": "银行卡号",
        "pattern": re.compile(r"\b\d{16,19}\b"),
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D3",
        "display_rule": "仅显示后4位",
        "summary_rule": "禁止出现",
        "share_rule": "禁止外部分享",
    },
    "tax_number": {
        "label": "税号",
        "keywords": ["税号", "纳税人识别号", "统一社会信用代码"],
        "pattern": re.compile(r"[0-9A-Z]{15,20}"),
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "license_number": {
        "label": "营业执照号",
        "keywords": ["营业执照", "执照编号", "工商注册号"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "invoice_number": {
        "label": "发票号",
        "keywords": ["发票号", "发票编号", "发票代码"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },

    # ── 客户经营类 ──────────────────────────────────────────────────────────
    "customer_name": {
        "label": "客户名称",
        "keywords": ["客户名", "客户名称", "甲方", "委托方", "品牌方"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D2",
        "display_rule": "替换为\"某客户\"",
        "summary_rule": "用行业代称替代",
        "share_rule": "脱敏后可分享",
    },
    "customer_contact": {
        "label": "客户联系方式",
        "keywords": ["客户联系方式", "客户电话", "客户邮箱", "对接人"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "customer_list": {
        "label": "客户清单",
        "keywords": ["客户清单", "客户列表", "客户名单", "客户台账"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D2",
        "display_rule": "替换为\"N个客户\"",
        "summary_rule": "仅提及数量",
        "share_rule": "禁止外部分享",
    },
    "lead_name": {
        "label": "线索名称",
        "keywords": ["线索", "潜客", "意向客户"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D1",
        "display_rule": "替换为\"某线索\"",
        "summary_rule": "不出现在摘要中",
        "share_rule": "脱敏后可分享",
    },
    "lead_contact": {
        "label": "线索联系方式",
        "keywords": ["线索电话", "线索联系方式", "线索手机"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "crm_id": {
        "label": "CRM编号",
        "keywords": ["CRM编号", "客户编号", "CRM ID"],
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D1",
        "display_rule": "部分掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },

    # ── 合同交易类 ──────────────────────────────────────────────────────────
    "contract_number": {
        "label": "合同编号",
        "keywords": ["合同编号", "合同号", "协议编号"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "禁止外部分享",
    },
    "order_number": {
        "label": "订单号",
        "keywords": ["订单号", "订单编号", "采购单号"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D1",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "pricing_term": {
        "label": "定价条款",
        "keywords": ["定价", "单价", "报价", "折扣率", "返点"],
        "default_mask_action": "range_mask",
        "default_desensitization_level": "D2",
        "display_rule": "显示范围区间",
        "summary_rule": "用范围替代",
        "share_rule": "禁止外部分享",
    },
    "rebate_rate": {
        "label": "返点比例",
        "keywords": ["返点", "返佣", "返利", "rebate"],
        "default_mask_action": "range_mask",
        "default_desensitization_level": "D3",
        "display_rule": "显示范围区间",
        "summary_rule": "仅提及存在",
        "share_rule": "禁止外部分享",
    },
    "amount": {
        "label": "金额数据",
        "pattern": re.compile(r"(?:[\¥\$€]|(?:人民币|美元|USD|RMB|CNY))\s*[\d,]+(?:\.\d{1,2})?|[\d,]+(?:\.\d{1,2})?\s*(?:万元|亿元|元|美金)"),
        "default_mask_action": "range_mask",
        "default_desensitization_level": "D1",
        "display_rule": "显示数量级范围",
        "summary_rule": "用范围替代精确值",
        "share_rule": "脱敏后可分享",
    },
    "salary": {
        "label": "薪资数据",
        "keywords": ["薪资", "工资", "年薪", "月薪", "底薪", "绩效奖金", "薪酬"],
        "default_mask_action": "range_mask",
        "default_desensitization_level": "D3",
        "display_rule": "显示范围区间",
        "summary_rule": "用级别替代",
        "share_rule": "禁止外部分享",
    },

    # ── 投放与平台类 ────────────────────────────────────────────────────────
    "ad_account_id": {
        "label": "广告账户ID",
        "keywords": ["广告账户", "账户ID", "投放账户", "广告主ID"],
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D1",
        "display_rule": "部分掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "channel_account": {
        "label": "渠道账号",
        "keywords": ["渠道账号", "代理商账号", "媒介账号"],
        "default_mask_action": "partial_mask",
        "default_desensitization_level": "D1",
        "display_rule": "部分掩码",
        "summary_rule": "不出现",
        "share_rule": "脱敏后可分享",
    },
    "media_plan_detail": {
        "label": "媒介计划明细",
        "keywords": ["媒介计划", "排期表", "投放计划明细", "媒介排期"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D2",
        "display_rule": "抽象化表述",
        "summary_rule": "仅提及存在",
        "share_rule": "禁止外部分享",
    },
    "conversion_data_detail": {
        "label": "转化数据明细",
        "keywords": ["转化数据", "转化明细", "成本明细", "ROI明细", "ROAS明细"],
        "default_mask_action": "range_mask",
        "default_desensitization_level": "D2",
        "display_rule": "显示范围",
        "summary_rule": "用趋势替代精确值",
        "share_rule": "禁止外部分享",
    },

    # ── 技术安全类 ──────────────────────────────────────────────────────────
    "api_key": {
        "label": "API密钥",
        "pattern": re.compile(r"(?:sk-|ak-|AKIA)[a-zA-Z0-9]{20,}"),
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D4",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止分享",
    },
    "access_token": {
        "label": "访问令牌",
        "pattern": re.compile(r"(?:Bearer\s+|token[=:]\s*)[a-zA-Z0-9._-]{20,}"),
        "keywords": ["access_token", "refresh_token", "bearer token"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D4",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止分享",
    },
    "cookie": {
        "label": "Cookie",
        "keywords": ["cookie", "session_id", "JSESSIONID"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D4",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止分享",
    },
    "password": {
        "label": "密码",
        "keywords": ["密码", "password", "口令", "PIN", "密钥"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D4",
        "display_rule": "完全掩码",
        "summary_rule": "禁止出现",
        "share_rule": "禁止分享",
    },
    "internal_url": {
        "label": "内部URL",
        "pattern": re.compile(r"https?://(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)[^\s]+"),
        "keywords": ["内网地址", "内部链接"],
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "禁止外部分享",
    },
    "ip_address": {
        "label": "IP地址",
        "pattern": re.compile(r"\b(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b"),
        "default_mask_action": "full_mask",
        "default_desensitization_level": "D2",
        "display_rule": "完全掩码",
        "summary_rule": "不出现",
        "share_rule": "禁止外部分享",
    },

    # ── 战略经营类 ──────────────────────────────────────────────────────────
    "strategic_info": {
        "label": "战略信息",
        "keywords": ["竞品分析", "竞争对手", "公司战略", "商业计划",
                     "股权", "融资", "估值", "并购", "核心技术", "专利"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D3",
        "display_rule": "抽象化表述",
        "summary_rule": "仅提及存在，不展开",
        "share_rule": "禁止外部分享",
    },
    "pricing_policy": {
        "label": "定价策略",
        "keywords": ["定价策略", "价格体系", "价格政策", "调价方案"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D3",
        "display_rule": "抽象化表述",
        "summary_rule": "仅提及存在",
        "share_rule": "禁止外部分享",
    },
    "negotiation_record": {
        "label": "谈判记录",
        "keywords": ["谈判记录", "商务谈判", "议价记录", "报价沟通"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D2",
        "display_rule": "抽象化表述",
        "summary_rule": "仅提及存在",
        "share_rule": "禁止外部分享",
    },
    "internal_conclusion": {
        "label": "内部结论",
        "keywords": ["内部结论", "决策记录", "管理层决议", "保密", "机密"],
        "default_mask_action": "abstract",
        "default_desensitization_level": "D3",
        "display_rule": "抽象化表述",
        "summary_rule": "仅提及存在",
        "share_rule": "禁止外部分享",
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# 2. 文档类型枚举（含中文标签）
# ════════════════════════════════════════════════════════════════════════════════

DOCUMENT_TYPES: dict[str, str] = {
    "policy": "制度/政策",
    "sop": "标准操作流程",
    "contract": "合同/协议",
    "proposal": "方案/提案",
    "report": "报告",
    "meeting_note": "会议纪要",
    "customer_material": "客户材料",
    "product_doc": "产品文档",
    "finance_doc": "财务文档",
    "hr_doc": "人事文档",
    "case_study": "案例/复盘",
    "training_material": "培训材料",
    "external_intel": "外部情报",
    "data_export": "数据导出",
    "form_template": "表单/模板",
    "media_plan": "媒介方案",
    "creative_brief": "创意简报",
    "pitch_deck": "比稿方案",
    "campaign_review": "项目复盘",
    "vendor_material": "供应商材料",
    "legal_doc": "法务文档",
    "other": "其他",
}

# 文件名/关键词 → document_type 映射（规则层）
_DOCTYPE_KEYWORD_MAP: dict[str, list[str]] = {
    "contract": ["合同", "协议", "contract", "agreement", "签约"],
    "proposal": ["方案", "提案", "proposal", "plan", "策划"],
    "report": ["报告", "report", "周报", "月报", "年报", "季报", "analysis"],
    "meeting_note": ["会议", "纪要", "meeting", "minutes"],
    "sop": ["SOP", "操作流程", "操作手册", "指南"],
    "policy": ["制度", "规范", "政策", "policy"],
    "case_study": ["案例", "复盘", "case", "review"],
    "training_material": ["培训", "training", "教程", "课件"],
    "customer_material": ["客户", "甲方", "品牌方"],
    "product_doc": ["产品", "PRD", "需求文档"],
    "finance_doc": ["财务", "预算", "成本", "利润", "finance", "budget"],
    "hr_doc": ["人事", "招聘", "绩效", "考勤", "HR"],
    "data_export": ["导出", "export", "数据表"],
    "form_template": ["模板", "template", "表单"],
    "external_intel": ["情报", "竞品", "行业报告", "趋势"],
    "media_plan": ["媒介方案", "媒介排期", "排期表", "media plan", "媒介计划"],
    "creative_brief": ["创意简报", "brief", "需求单"],
    "pitch_deck": ["比稿", "pitch", "竞标", "比稿方案"],
    "campaign_review": ["项目复盘", "campaign review", "效果总结"],
    "vendor_material": ["供应商", "外包", "服务商"],
    "legal_doc": ["法务", "法律", "合规", "legal"],
}


# ════════════════════════════════════════════════════════════════════════════════
# 3. 权限域枚举（含中文标签和权限文案）
# ════════════════════════════════════════════════════════════════════════════════

PERMISSION_DOMAINS: dict[str, dict[str, str]] = {
    "public": {
        "label": "全员可见",
        "desc": "全公司员工均可查看",
        "share_policy": "可自由分享",
    },
    "department": {
        "label": "部门内可见",
        "desc": "仅文档所属部门成员可查看",
        "share_policy": "部门内可分享，跨部门需审批",
    },
    "team": {
        "label": "团队可见",
        "desc": "仅指定团队/项目组成员可查看",
        "share_policy": "团队内可分享，对外需审批",
    },
    "owner_only": {
        "label": "仅创建者",
        "desc": "仅文档创建者可查看",
        "share_policy": "不可分享，需先提升可见范围",
    },
    "confidential": {
        "label": "机密-需审批",
        "desc": "含高敏感信息，查看和分享均需审批",
        "share_policy": "任何分享需超管审批",
    },
}

# 向后兼容：允许 `perm in PERMISSION_DOMAINS` 检查
# （之前 PERMISSION_DOMAINS 的 value 是 str，现在改为 dict）


# ════════════════════════════════════════════════════════════════════════════════
# 4. 脱敏级别枚举
# ════════════════════════════════════════════════════════════════════════════════

DESENSITIZATION_LEVELS: dict[str, dict[str, str]] = {
    "D0": {"label": "公开", "desc": "无敏感数据，可自由分享"},
    "D1": {"label": "内部", "desc": "含一般业务数据（金额/邮箱），脱敏后可分享"},
    "D2": {"label": "敏感", "desc": "含客户名/合同号等，需审批后分享"},
    "D3": {"label": "机密", "desc": "含战略/薪资/身份证等，禁止外部分享"},
    "D4": {"label": "绝密", "desc": "含密钥/密码等，禁止任何形式分享"},
}


# ════════════════════════════════════════════════════════════════════════════════
# 5. 业务场景特例规则（完整化）
# ════════════════════════════════════════════════════════════════════════════════

SCENARIO_OVERRIDES: dict[str, dict[str, Any]] = {
    "contract": {
        "min_level": "D2",
        "force_flags": ["customer_name", "amount", "contract_number"],
        "summary_rule": "abstracted",
        "visibility": "confidential",
    },
    "customer_material": {
        "min_level": "D2",
        "force_flags": ["customer_name"],
        "summary_rule": "abstracted",
        "visibility": "department",
    },
    "finance_doc": {
        "min_level": "D2",
        "force_flags": ["amount"],
        "summary_rule": "masked",
        "visibility": "confidential",
    },
    "hr_doc": {
        "min_level": "D3",
        "force_flags": ["salary", "personal_info"],
        "summary_rule": "abstracted",
        "visibility": "confidential",
    },
    "data_export": {
        "min_level": "D1",
        "summary_rule": "masked",
        "visibility": "department",
    },
    "external_intel": {
        "min_level": "D0",
        "summary_rule": "raw",
        "visibility": "public",
    },
    "proposal": {
        "min_level": "D1",
        "force_flags": ["amount", "pricing_term"],
        "summary_rule": "masked",
        "visibility": "department",
    },
    "report": {
        "min_level": "D0",
        "summary_rule": "raw",
        "visibility": "department",
    },
    "media_plan": {
        "min_level": "D2",
        "force_flags": ["amount", "media_plan_detail"],
        "summary_rule": "masked",
        "visibility": "department",
    },
    "pitch_deck": {
        "min_level": "D2",
        "force_flags": ["pricing_term", "strategic_info"],
        "summary_rule": "abstracted",
        "visibility": "confidential",
    },
    "legal_doc": {
        "min_level": "D3",
        "force_flags": ["contract_number"],
        "summary_rule": "abstracted",
        "visibility": "confidential",
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# 5.1 组合升档规则
# ════════════════════════════════════════════════════════════════════════════════

COMBO_ESCALATION_RULES: list[dict[str, Any]] = [
    {
        "combo": {"customer_name", "phone_number", "company_name"},
        "min_match": 2,
        "escalate_to": "D3",
        "reason": "客户名+联系方式+公司名组合构成客户画像",
    },
    {
        "combo": {"person_name", "id_card"},
        "min_match": 2,
        "escalate_to": "D4",
        "reason": "姓名+身份证号构成完整个人身份信息",
    },
    {
        "combo": {"amount", "customer_name", "contract_number"},
        "min_match": 2,
        "escalate_to": "D3",
        "reason": "金额+客户+合同号构成合同敏感信息",
    },
    {
        "combo": {"api_key"},
        "min_match": 1,
        "escalate_to": "D4",
        "reason": "API密钥任一命中直接D4",
    },
    {
        "combo": {"password"},
        "min_match": 1,
        "escalate_to": "D4",
        "reason": "密码任一命中直接D4",
    },
    {
        "combo": {"access_token"},
        "min_match": 1,
        "escalate_to": "D4",
        "reason": "访问令牌任一命中直接D4",
    },
    {
        "combo": {"cookie"},
        "min_match": 1,
        "escalate_to": "D4",
        "reason": "Cookie任一命中直接D4",
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# 6. taxonomy → document_type 映射
# ════════════════════════════════════════════════════════════════════════════════

TAXONOMY_DOCTYPE_MAP: dict[str, list[str]] = {
    "A": ["report", "training_material", "case_study", "media_plan"],       # 渠道与平台
    "B": ["proposal", "report", "case_study", "sop", "media_plan"],          # 投放策略与方法论
    "C": ["customer_material", "case_study", "external_intel", "report"],    # 行业与客户知识
    "D": ["creative_brief", "training_material", "case_study"],              # 素材与创意
    "E": ["report", "data_export", "training_material"],                     # 数据与分析
    "F": ["product_doc", "sop", "training_material", "policy"],              # 产品与运营
}


def check_taxonomy_doctype_conflict(
    taxonomy_board: str | None,
    document_type: str | None,
) -> dict | None:
    """检查 taxonomy board 与 document_type 是否冲突。

    Returns: None=无冲突，dict=冲突详情
    """
    if not taxonomy_board or not document_type:
        return None
    expected = TAXONOMY_DOCTYPE_MAP.get(taxonomy_board, [])
    if not expected:
        return None
    if document_type in expected or document_type == "other":
        return None
    return {
        "taxonomy_board": taxonomy_board,
        "document_type": document_type,
        "expected_types": expected,
        "conflict": True,
        "message": f"文档类型 '{document_type}' 不在 taxonomy '{taxonomy_board}' 的常见类型 {expected} 中",
    }


# ════════════════════════════════════════════════════════════════════════════════
# 7. 受控内容标签词表（5维 + fallback）
# ════════════════════════════════════════════════════════════════════════════════

CONTENT_TAG_VOCABULARY: dict[str, dict[str, Any]] = {
    "subject_tag": {
        "label": "主体（谁产出/使用）",
        "vocabulary": [
            "投放团队", "运营团队", "销售团队", "产品团队", "技术团队",
            "财务部", "人事部", "市场部", "客户成功", "管理层",
            "创意团队", "数据团队", "策略团队", "媒介团队", "法务部",
        ],
        "fallback": "通用",
    },
    "object_tag": {
        "label": "对象（涉及什么）",
        "vocabulary": [
            "客户", "竞品", "渠道", "产品", "用户",
            "供应商", "合作伙伴", "行业", "市场", "项目",
            "品牌", "媒体", "KOL", "消费者", "代理商",
        ],
        "fallback": "通用",
    },
    "scenario_tag": {
        "label": "场景（什么场景下使用）",
        "vocabulary": [
            "新客开拓", "老客维护", "投放优化", "预算规划", "复盘总结",
            "培训赋能", "内部汇报", "外部协作", "应急响应", "日常运营",
            "比稿竞标", "季度规划", "年度总结", "入职培训", "项目交接",
        ],
        "fallback": "日常运营",
    },
    "action_tag": {
        "label": "动作（做什么）",
        "vocabulary": [
            "分析", "决策", "执行", "监控", "优化",
            "汇报", "审批", "沟通", "学习", "创新",
            "规划", "复盘", "协调", "测试", "评估",
        ],
        "fallback": "参考",
    },
    "industry_or_domain_tag": {
        "label": "行业/领域",
        "vocabulary": [
            "快消", "美妆", "电商", "游戏", "教育",
            "金融", "医疗", "汽车", "旅游", "房产",
            "零售", "3C数码", "母婴", "食品饮料", "服装",
            "本地生活", "B2B", "SaaS", "内容平台", "跨境电商",
        ],
        "fallback": "综合",
    },
}


def get_tag_fallback(dimension: str) -> str:
    """获取指定标签维度的 fallback 值。"""
    vocab = CONTENT_TAG_VOCABULARY.get(dimension, {})
    return vocab.get("fallback", "通用") if isinstance(vocab, dict) else "通用"


def validate_content_tags(tags: dict) -> dict:
    """确保 content_tags 5维完整，缺失维度填 fallback。"""
    result = {}
    for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
        val = tags.get(dim) if isinstance(tags, dict) else None
        if val and isinstance(val, str) and val.strip():
            result[dim] = val.strip()
        else:
            result[dim] = get_tag_fallback(dim)
    return result


# ════════════════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════════════════

def detect_data_types(content: str) -> list[dict]:
    """扫描内容中的数据类型命中，返回 [{type, label, count, samples}]。"""
    hits: list[dict] = []
    for dtype, spec in DATA_TYPE_REGISTRY.items():
        found_count = 0
        samples: list[str] = []
        # 正则匹配
        if "pattern" in spec:
            matches = spec["pattern"].findall(content)
            found_count += len(matches)
            samples.extend(matches[:3])
        # 关键词匹配
        if "keywords" in spec:
            for kw in spec["keywords"]:
                cnt = content.count(kw)
                if cnt > 0:
                    found_count += cnt
                    if kw not in samples:
                        samples.append(kw)
        if found_count > 0:
            hits.append({
                "type": dtype,
                "label": spec["label"],
                "count": found_count,
                "samples": samples[:3],
            })
    return hits


def infer_document_type(filename: str, content: str) -> tuple[str | None, str]:
    """通过文件名和内容关键词推断文档类型。

    得分规则：每个命中关键词按字符长度加权（更长的关键词更精确）。
    Returns: (document_type, source)  source='rule' 或 'unknown'
    """
    text = (filename + " " + content[:2000]).lower()
    best_type = None
    best_score = 0.0
    for dtype, keywords in _DOCTYPE_KEYWORD_MAP.items():
        score = sum(len(kw) for kw in keywords if kw.lower() in text)
        if score > best_score:
            best_score = score
            best_type = dtype
    if best_score >= 1:
        return best_type, "rule"
    return None, "unknown"


def compute_desensitization_level(
    data_type_hits: list[dict],
    document_type: str | None,
) -> tuple[str, str]:
    """基于数据类型命中 + 文档类型 + 组合升档，计算脱敏级别。

    Returns: (level, visibility_recommendation)
    """
    max_level = "D0"

    # 数据类型驱动
    for hit in data_type_hits:
        spec = DATA_TYPE_REGISTRY.get(hit["type"], {})
        level = spec.get("default_desensitization_level", "D0")
        if _LEVEL_ORDER.get(level, 0) > _LEVEL_ORDER.get(max_level, 0):
            max_level = level

    # 组合升档规则
    hit_types = {h["type"] for h in data_type_hits}
    for rule in COMBO_ESCALATION_RULES:
        overlap = rule["combo"] & hit_types
        if len(overlap) >= rule["min_match"]:
            escalate = rule["escalate_to"]
            if _LEVEL_ORDER.get(escalate, 0) > _LEVEL_ORDER.get(max_level, 0):
                max_level = escalate

    # 场景特例升级
    if document_type and document_type in SCENARIO_OVERRIDES:
        override = SCENARIO_OVERRIDES[document_type]
        min_level = override.get("min_level", "D0")
        if _LEVEL_ORDER.get(min_level, 0) > _LEVEL_ORDER.get(max_level, 0):
            max_level = min_level

    # 推算可见性建议
    visibility_map = {
        "D0": "public",
        "D1": "department",
        "D2": "department",
        "D3": "confidential",
        "D4": "confidential",
    }
    if document_type and document_type in SCENARIO_OVERRIDES:
        visibility = SCENARIO_OVERRIDES[document_type].get(
            "visibility", visibility_map.get(max_level, "department")
        )
    else:
        visibility = visibility_map.get(max_level, "department")

    return max_level, visibility


def get_summary_sensitivity_mode(
    document_type: str | None,
    desensitization_level: str,
) -> str:
    """决定摘要生成时的脱敏模式。"""
    if document_type and document_type in SCENARIO_OVERRIDES:
        return SCENARIO_OVERRIDES[document_type].get("summary_rule", "raw")

    level_num = _LEVEL_ORDER.get(desensitization_level, 0)
    if level_num >= 3:
        return "abstracted"
    elif level_num >= 1:
        return "masked"
    return "raw"


# ════════════════════════════════════════════════════════════════════════════════
# 8. 文档类型缩写码 + 系统编号生成
# ════════════════════════════════════════════════════════════════════════════════

DOCTYPE_CODES: dict[str, str] = {
    "contract": "CTR",
    "proposal": "PRP",
    "report": "RPT",
    "meeting_note": "MTG",
    "sop": "SOP",
    "policy": "POL",
    "case_study": "CSE",
    "customer_material": "CUS",
    "finance_doc": "FIN",
    "hr_doc": "HRD",
    "training_material": "TRN",
    "product_doc": "PRD",
    "data_export": "DAT",
    "form_template": "FRM",
    "external_intel": "INT",
    "media_plan": "MDP",
    "creative_brief": "BRF",
    "pitch_deck": "PIT",
    "campaign_review": "CRV",
    "vendor_material": "VND",
    "legal_doc": "LGL",
    "other": "OTH",
}


def generate_system_id(document_type: str, knowledge_id: int) -> str:
    """生成语义化系统编号: {YYMMDD}_{DOCTYPE_CODE}_{hash6}"""
    import datetime as _dt
    import hashlib as _hl

    date_part = _dt.datetime.utcnow().strftime("%y%m%d")
    code = DOCTYPE_CODES.get(document_type, "OTH")
    hash_part = _hl.md5(str(knowledge_id).encode()).hexdigest()[:6]
    return f"{date_part}_{code}_{hash_part}"
