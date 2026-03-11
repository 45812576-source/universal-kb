"""Unit tests for the review policy engine."""
import pytest
from app.services.review_policy import ReviewPolicy, SENSITIVE_KEYWORDS, STRATEGIC_KEYWORDS


@pytest.fixture
def policy():
    return ReviewPolicy()


# ── detect_sensitive ───────────────────────────────────────────────────────────

class TestDetectSensitive:
    def test_no_sensitive(self, policy):
        flags = policy.detect_sensitive("这是一段普通的经验总结，关于投放策略的心得。")
        assert flags == []

    def test_customer_name(self, policy):
        flags = policy.detect_sensitive("客户名是张三，联系电话是138xxxxxx。")
        assert "客户名" in flags or "联系电话" in flags

    def test_amount(self, policy):
        flags = policy.detect_sensitive("合同金额为50万，合同编号是ABC-2024-001。")
        assert "合同金额" in flags or "合同编号" in flags

    def test_dedup(self, policy):
        # 同一关键词出现多次，返回结果不重复
        flags = policy.detect_sensitive("客户名客户名客户名")
        assert flags.count("客户名") == 1


class TestDetectStrategic:
    def test_no_strategic(self, policy):
        flags = policy.detect_strategic("日常工作总结与方法论提炼。")
        assert flags == []

    def test_pricing_strategy(self, policy):
        flags = policy.detect_strategic("公司的定价策略调整方案如下：")
        assert "定价策略" in flags

    def test_competitor(self, policy):
        flags = policy.detect_strategic("竞品分析显示，竞争对手已下调价格。")
        assert "竞品分析" in flags or "竞争对手" in flags

    def test_confidential(self, policy):
        flags = policy.detect_strategic("此内容属于核心技术机密，严禁外传。")
        assert "核心技术" in flags or "机密" in flags


# ── compute_level ──────────────────────────────────────────────────────────────

class TestComputeLevel:
    def test_manual_no_sensitive_is_l2(self, policy):
        level, flags = policy.compute_level("manual_form", "普通经验总结内容。")
        assert level == 2
        assert flags == []

    def test_chat_confirmed_no_sensitive_is_l1(self, policy):
        level, flags = policy.compute_level("chat_delegate_confirmed", "总结了一些推广技巧。")
        assert level == 1
        assert flags == []

    def test_skill_output_no_sensitive_is_l1(self, policy):
        level, flags = policy.compute_level("skill_output", "本次分析结论如下。")
        assert level == 1
        assert flags == []

    def test_upload_ai_clean_no_sensitive_is_l1(self, policy):
        level, flags = policy.compute_level("upload_ai_clean", "文件内容摘要。")
        assert level == 1
        assert flags == []

    def test_upload_no_sensitive_is_l2(self, policy):
        level, flags = policy.compute_level("upload", "普通上传内容。")
        assert level == 2

    def test_chat_partial_no_sensitive_is_l2(self, policy):
        level, flags = policy.compute_level("chat_delegate_partial", "部分确认内容。")
        assert level == 2

    def test_sensitive_content_upgrades_l1_to_l2(self, policy):
        # skill_output 基础是 L1，但有敏感词应升到 L2
        level, flags = policy.compute_level(
            "skill_output", "此客户名为某公司，合同金额100万。"
        )
        assert level == 2
        assert len(flags) > 0

    def test_strategic_content_upgrades_to_l3(self, policy):
        # chat_confirmed 基础是 L1，有战略词应升到 L3
        level, flags = policy.compute_level(
            "chat_delegate_confirmed", "公司战略规划显示定价策略将调整。"
        )
        assert level == 3
        assert len(flags) > 0

    def test_manual_strategic_is_l3(self, policy):
        level, flags = policy.compute_level(
            "manual_form", "竞品分析结果：竞争对手已融资完成。"
        )
        assert level == 3

    def test_unknown_capture_mode_defaults_to_l2(self, policy):
        level, flags = policy.compute_level("unknown_mode", "内容。")
        assert level == 2


# ── auto_review ────────────────────────────────────────────────────────────────

class TestAutoReview:
    def test_l1_auto_pass(self, policy):
        auto_pass, level, flags, note = policy.auto_review(
            "chat_delegate_confirmed", "普通投放经验分享。"
        )
        assert auto_pass is True
        assert level == 1
        assert "自动审核通过" in note

    def test_l2_no_auto_pass(self, policy):
        auto_pass, level, flags, note = policy.auto_review(
            "manual_form", "手动录入的内容。"
        )
        assert auto_pass is False
        assert level == 2

    def test_l3_no_auto_pass(self, policy):
        auto_pass, level, flags, note = policy.auto_review(
            "skill_output", "定价策略分析：建议调整竞品定价。"
        )
        assert auto_pass is False
        assert level == 3

    def test_l1_with_sensitive_no_auto_pass(self, policy):
        # 即便 L1 来源，有敏感词也不自动通过
        auto_pass, level, flags, note = policy.auto_review(
            "upload_ai_clean", "客户名：张三，联系电话：138xxxxxx"
        )
        assert auto_pass is False
        assert level >= 2

    def test_note_contains_flags(self, policy):
        auto_pass, level, flags, note = policy.auto_review(
            "manual_form", "合同金额为100万"
        )
        assert not auto_pass
        # 敏感词应出现在 note 中
        assert any(f in note for f in flags)
