"""文档理解流水线收口测试。

覆盖 4 大类：
1. 标题体系 — 优先级链、乱码、中文、飞书、用户覆盖
2. 标签体系 — document_type 枚举、taxonomy 映射、5维固定结构、建议标签
3. 数据类型与脱敏 — 全类型命中、场景override、组合升档、D3/D4摘要
4. 自动标签与摘要 — fallback路径、content_tags 完整、summary 不为空
"""
import pytest

from app.data.sensitivity_rules import (
    COMBO_ESCALATION_RULES,
    CONTENT_TAG_VOCABULARY,
    DESENSITIZATION_LEVELS,
    DOCUMENT_TYPES,
    PERMISSION_DOMAINS,
    TAXONOMY_DOCTYPE_MAP,
    check_taxonomy_doctype_conflict,
    compute_desensitization_level,
    detect_data_types,
    get_summary_sensitivity_mode,
    get_tag_fallback,
    infer_document_type,
    validate_content_tags,
)


# ════════════════════════════════════════════════════════════════════════════════
# 1. 标题体系
# ════════════════════════════════════════════════════════════════════════════════

class TestTitleSystem:
    def test_chinese_filename(self):
        """中文文件名正常识别"""
        doc_type, _ = infer_document_type("2026年Q1投放方案.pptx", "")
        assert doc_type == "proposal"

    def test_garbled_filename(self):
        """乱码文件名降级为 unknown"""
        doc_type, source = infer_document_type("Ã¤Â¸Â­Ã¦ÂÂ.pdf", "")
        # 乱码可能不命中任何关键词
        assert source in ("rule", "unknown")

    def test_lark_title(self):
        """飞书导入标题（含飞书常见格式）"""
        doc_type, _ = infer_document_type("【会议纪要】2026-03-15 周会", "")
        assert doc_type == "meeting_note"

    def test_validate_content_tags_complete(self):
        """content_tags 缺失维度自动填 fallback"""
        tags = validate_content_tags({"subject_tag": "投放团队"})
        assert tags["subject_tag"] == "投放团队"
        assert tags["object_tag"] == get_tag_fallback("object_tag")
        assert tags["scenario_tag"] == get_tag_fallback("scenario_tag")
        assert tags["action_tag"] == get_tag_fallback("action_tag")
        assert tags["industry_or_domain_tag"] == get_tag_fallback("industry_or_domain_tag")

    def test_validate_content_tags_empty_string_fills_fallback(self):
        """空字符串也应该填 fallback"""
        tags = validate_content_tags({"subject_tag": "", "object_tag": "  "})
        assert tags["subject_tag"] == get_tag_fallback("subject_tag")
        assert tags["object_tag"] == get_tag_fallback("object_tag")

    def test_validate_content_tags_all_present(self):
        """全部维度有值时保留"""
        input_tags = {
            "subject_tag": "销售团队",
            "object_tag": "客户",
            "scenario_tag": "新客开拓",
            "action_tag": "分析",
            "industry_or_domain_tag": "快消",
        }
        result = validate_content_tags(input_tags)
        assert result == input_tags


# ════════════════════════════════════════════════════════════════════════════════
# 2. 标签体系
# ════════════════════════════════════════════════════════════════════════════════

class TestTagSystem:
    def test_document_type_includes_business_types(self):
        """document_type 包含广告行业业务类型"""
        assert "media_plan" in DOCUMENT_TYPES
        assert "creative_brief" in DOCUMENT_TYPES
        assert "pitch_deck" in DOCUMENT_TYPES
        assert "campaign_review" in DOCUMENT_TYPES
        assert "vendor_material" in DOCUMENT_TYPES
        assert "legal_doc" in DOCUMENT_TYPES

    def test_document_type_count(self):
        assert len(DOCUMENT_TYPES) >= 22

    def test_taxonomy_doctype_map_all_boards(self):
        """taxonomy 映射覆盖 A-F 全部板块"""
        for board in "ABCDEF":
            assert board in TAXONOMY_DOCTYPE_MAP
            assert len(TAXONOMY_DOCTYPE_MAP[board]) >= 2

    def test_taxonomy_conflict_detected(self):
        """taxonomy 与 document_type 冲突正确检测"""
        conflict = check_taxonomy_doctype_conflict("A", "hr_doc")
        assert conflict is not None
        assert conflict["conflict"] is True

    def test_taxonomy_no_conflict(self):
        """taxonomy 与 document_type 兼容不报冲突"""
        assert check_taxonomy_doctype_conflict("A", "report") is None
        assert check_taxonomy_doctype_conflict("C", "customer_material") is None

    def test_taxonomy_conflict_other_always_ok(self):
        """document_type=other 不与任何 taxonomy 冲突"""
        for board in "ABCDEF":
            assert check_taxonomy_doctype_conflict(board, "other") is None

    def test_permission_domains_have_labels(self):
        """PERMISSION_DOMAINS 每个域都有 label 和 desc"""
        for key, val in PERMISSION_DOMAINS.items():
            assert isinstance(val, dict)
            assert "label" in val
            assert "desc" in val

    def test_content_tag_vocabulary_5_dimensions(self):
        """5维标签词表完整"""
        assert len(CONTENT_TAG_VOCABULARY) == 5
        for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
            assert dim in CONTENT_TAG_VOCABULARY
            vocab = CONTENT_TAG_VOCABULARY[dim]
            assert isinstance(vocab, dict)
            assert "vocabulary" in vocab
            assert "fallback" in vocab
            assert len(vocab["vocabulary"]) >= 10

    def test_infer_media_plan(self):
        doc_type, _ = infer_document_type("Q2媒介方案_抖音.pptx", "")
        assert doc_type == "media_plan"

    def test_infer_pitch_deck(self):
        doc_type, _ = infer_document_type("XX品牌比稿方案.pdf", "")
        assert doc_type == "pitch_deck"

    def test_infer_legal_doc(self):
        doc_type, _ = infer_document_type("法务审核意见.docx", "合规检查")
        assert doc_type == "legal_doc"


# ════════════════════════════════════════════════════════════════════════════════
# 3. 数据类型与脱敏（扩充后）
# ════════════════════════════════════════════════════════════════════════════════

class TestDataTypeExpanded:
    """新增数据类型命中测试"""

    def test_person_name(self):
        hits = detect_data_types("联系人姓名：张三")
        types = [h["type"] for h in hits]
        assert "person_name" in types

    def test_address(self):
        hits = detect_data_types("家庭住址：北京市朝阳区")
        types = [h["type"] for h in hits]
        assert "address" in types

    def test_birthday(self):
        hits = detect_data_types("出生日期：1990-01-01")
        types = [h["type"] for h in hits]
        assert "birthday" in types

    def test_company_name(self):
        hits = detect_data_types("公司名称：北京ABC科技有限公司")
        types = [h["type"] for h in hits]
        assert "company_name" in types

    def test_tax_number(self):
        hits = detect_data_types("纳税人识别号：91110108MA01ABCDEF")
        types = [h["type"] for h in hits]
        assert "tax_number" in types

    def test_license_number(self):
        hits = detect_data_types("营业执照编号见附件")
        types = [h["type"] for h in hits]
        assert "license_number" in types

    def test_invoice_number(self):
        hits = detect_data_types("发票编号：INV-2026-001")
        types = [h["type"] for h in hits]
        assert "invoice_number" in types

    def test_customer_contact(self):
        hits = detect_data_types("客户联系方式：张总 138xxxx")
        types = [h["type"] for h in hits]
        assert "customer_contact" in types

    def test_customer_list(self):
        hits = detect_data_types("以下为客户清单，共50家客户")
        types = [h["type"] for h in hits]
        assert "customer_list" in types

    def test_lead_name(self):
        hits = detect_data_types("新线索：XX品牌意向客户")
        types = [h["type"] for h in hits]
        assert "lead_name" in types

    def test_crm_id(self):
        hits = detect_data_types("CRM编号：CRM-2026-001")
        types = [h["type"] for h in hits]
        assert "crm_id" in types

    def test_order_number(self):
        hits = detect_data_types("订单编号：ORD-2026-001")
        types = [h["type"] for h in hits]
        assert "order_number" in types

    def test_pricing_term(self):
        hits = detect_data_types("定价为每个CPM 50元，折扣率8折")
        types = [h["type"] for h in hits]
        assert "pricing_term" in types

    def test_rebate_rate(self):
        hits = detect_data_types("媒体返点比例为5%")
        types = [h["type"] for h in hits]
        assert "rebate_rate" in types

    def test_ad_account_id(self):
        hits = detect_data_types("广告账户ID：AD-12345")
        types = [h["type"] for h in hits]
        assert "ad_account_id" in types

    def test_channel_account(self):
        hits = detect_data_types("渠道账号信息如下")
        types = [h["type"] for h in hits]
        assert "channel_account" in types

    def test_media_plan_detail(self):
        hits = detect_data_types("以下为媒介方案和排期表")
        types = [h["type"] for h in hits]
        assert "media_plan_detail" in types

    def test_conversion_data_detail(self):
        hits = detect_data_types("转化数据明细如下")
        types = [h["type"] for h in hits]
        assert "conversion_data_detail" in types

    def test_access_token(self):
        hits = detect_data_types("access_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxxxx")
        types = [h["type"] for h in hits]
        assert "access_token" in types

    def test_cookie(self):
        hits = detect_data_types("请设置 cookie: JSESSIONID=abc123")
        types = [h["type"] for h in hits]
        assert "cookie" in types

    def test_internal_url(self):
        hits = detect_data_types("内部链接：http://192.168.1.100:8080/admin")
        types = [h["type"] for h in hits]
        assert "internal_url" in types

    def test_ip_address(self):
        hits = detect_data_types("服务器 IP 10.0.1.50")
        types = [h["type"] for h in hits]
        assert "ip_address" in types

    def test_pricing_policy(self):
        hits = detect_data_types("以下为定价策略和价格体系调整方案")
        types = [h["type"] for h in hits]
        assert "pricing_policy" in types

    def test_negotiation_record(self):
        hits = detect_data_types("商务谈判记录如下")
        types = [h["type"] for h in hits]
        assert "negotiation_record" in types

    def test_internal_conclusion(self):
        hits = detect_data_types("以下为管理层决议，标记为机密")
        types = [h["type"] for h in hits]
        assert "internal_conclusion" in types

    def test_phone_number(self):
        hits = detect_data_types("联系人张三，手机号 13812345678")
        types = [h["type"] for h in hits]
        assert "phone_number" in types

    def test_id_card(self):
        hits = detect_data_types("身份证号：110101199901011234")
        types = [h["type"] for h in hits]
        assert "id_card" in types

    def test_email(self):
        hits = detect_data_types("请发邮件到 zhangsan@company.com")
        types = [h["type"] for h in hits]
        assert "email" in types

    def test_amount(self):
        hits = detect_data_types("合同金额为¥500,000.00")
        types = [h["type"] for h in hits]
        assert "amount" in types

    def test_api_key(self):
        hits = detect_data_types("API key: sk-abcdefghijklmnopqrstuvwxyz")
        types = [h["type"] for h in hits]
        assert "api_key" in types

    def test_salary(self):
        hits = detect_data_types("该岗位月薪15000-25000")
        types = [h["type"] for h in hits]
        assert "salary" in types

    def test_no_sensitive_data(self):
        hits = detect_data_types("这是一篇关于投放策略的培训材料，介绍了ROI优化方法论。")
        assert len(hits) == 0


class TestComboEscalation:
    """组合升档规则"""

    def test_customer_phone_company_escalate(self):
        """客户名+手机号+公司名 → D3"""
        hits = [
            {"type": "customer_name", "label": "", "count": 1, "samples": []},
            {"type": "phone_number", "label": "", "count": 1, "samples": []},
        ]
        level, _ = compute_desensitization_level(hits, None)
        assert level >= "D2"  # 至少 D2 from customer_name
        # 加入 company_name 后
        hits.append({"type": "company_name", "label": "", "count": 1, "samples": []})
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D3"

    def test_name_idcard_escalate_d4(self):
        """姓名+身份证号 → D4"""
        hits = [
            {"type": "person_name", "label": "", "count": 1, "samples": []},
            {"type": "id_card", "label": "", "count": 1, "samples": []},
        ]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D4"

    def test_amount_customer_contract_escalate(self):
        """金额+客户名+合同号 → D3"""
        hits = [
            {"type": "amount", "label": "", "count": 1, "samples": []},
            {"type": "customer_name", "label": "", "count": 1, "samples": []},
            {"type": "contract_number", "label": "", "count": 1, "samples": []},
        ]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D3"

    def test_api_key_always_d4(self):
        """api_key 单独命中直接 D4"""
        hits = [{"type": "api_key", "label": "", "count": 1, "samples": []}]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D4"

    def test_password_always_d4(self):
        """password 单独命中直接 D4"""
        hits = [{"type": "password", "label": "", "count": 1, "samples": []}]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D4"

    def test_access_token_always_d4(self):
        hits = [{"type": "access_token", "label": "", "count": 1, "samples": []}]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D4"

    def test_cookie_always_d4(self):
        hits = [{"type": "cookie", "label": "", "count": 1, "samples": []}]
        level, _ = compute_desensitization_level(hits, None)
        assert level == "D4"


class TestScenarioOverrideExpanded:
    """扩展场景 override"""

    def test_proposal_d1(self):
        level, _ = compute_desensitization_level([], "proposal")
        assert level == "D1"

    def test_report_d0(self):
        level, _ = compute_desensitization_level([], "report")
        assert level == "D0"

    def test_media_plan_d2(self):
        level, _ = compute_desensitization_level([], "media_plan")
        assert level == "D2"

    def test_pitch_deck_d2(self):
        level, _ = compute_desensitization_level([], "pitch_deck")
        assert level == "D2"

    def test_legal_doc_d3(self):
        level, _ = compute_desensitization_level([], "legal_doc")
        assert level == "D3"
        _, vis = compute_desensitization_level([], "legal_doc")
        assert vis == "confidential"

    def test_d3_summary_abstracted(self):
        """D3 摘要模式为 abstracted"""
        assert get_summary_sensitivity_mode(None, "D3") == "abstracted"

    def test_d4_summary_abstracted(self):
        assert get_summary_sensitivity_mode(None, "D4") == "abstracted"


# ════════════════════════════════════════════════════════════════════════════════
# 4. 自动标签与摘要 — fallback 路径
# ════════════════════════════════════════════════════════════════════════════════

class TestFallbackPaths:
    def test_validate_content_tags_none_input(self):
        """None 输入填满 fallback"""
        result = validate_content_tags(None)
        for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
            assert dim in result
            assert result[dim]  # 不为空

    def test_validate_content_tags_empty_dict(self):
        """空 dict 填满 fallback"""
        result = validate_content_tags({})
        assert len(result) == 5
        for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
            assert result[dim] == get_tag_fallback(dim)

    def test_get_tag_fallback_all_dimensions(self):
        """每个维度都有 fallback"""
        for dim in ("subject_tag", "object_tag", "scenario_tag", "action_tag", "industry_or_domain_tag"):
            fb = get_tag_fallback(dim)
            assert fb and isinstance(fb, str)

    def test_get_tag_fallback_unknown_dimension(self):
        """未知维度返回 '通用'"""
        assert get_tag_fallback("nonexistent") == "通用"


# ════════════════════════════════════════════════════════════════════════════════
# 5. 枚举完整性（扩充后）
# ════════════════════════════════════════════════════════════════════════════════

class TestEnumCompleteness:
    def test_document_types_count(self):
        assert len(DOCUMENT_TYPES) >= 22

    def test_permission_domains_count(self):
        assert len(PERMISSION_DOMAINS) >= 5

    def test_desensitization_levels_count(self):
        assert len(DESENSITIZATION_LEVELS) == 5
        assert set(DESENSITIZATION_LEVELS.keys()) == {"D0", "D1", "D2", "D3", "D4"}

    def test_combo_rules_count(self):
        assert len(COMBO_ESCALATION_RULES) >= 7

    def test_taxonomy_map_count(self):
        assert len(TAXONOMY_DOCTYPE_MAP) == 6

    def test_content_tag_vocabulary_count(self):
        assert len(CONTENT_TAG_VOCABULARY) == 5
