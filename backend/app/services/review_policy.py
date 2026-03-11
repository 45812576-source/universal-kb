"""知识入库分级审核策略引擎。

四级审核策略：
  L0 - 直接收录（跳过审核）
  L1 - AI自动收录（无敏感内容时直接 APPROVED + 向量化）
  L2 - 部门确认（现有流程，部门管理员审核）
  L3 - 超管确认（部门通过后再由超管二次确认）
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.knowledge import KnowledgeEntry

# ── 敏感词配置（可后续迁移至 DB 配置表）────────────────────────────────────────

# L2 触发词：含客户/金额/合同等业务敏感信息
SENSITIVE_KEYWORDS: list[str] = [
    "客户名", "客户联系方式", "联系电话", "手机号", "邮箱地址",
    "合同编号", "合同金额", "具体金额", "报价", "单价", "总价",
    "付款", "账号", "银行卡", "身份证",
    "个人信息", "隐私",
]

# L3 触发词：含公司战略/竞品/核心机密
STRATEGIC_KEYWORDS: list[str] = [
    "定价策略", "竞品分析", "竞争对手", "公司战略", "战略规划",
    "股权", "融资", "估值", "并购", "商业计划",
    "核心技术", "专利", "保密", "机密",
    "跨部门", "组织架构调整", "裁员", "薪资体系",
]

# capture_mode → 基础审核级别
_CAPTURE_MODE_LEVEL: dict[str, int] = {
    "chat_delegate_confirmed": 1,
    "chat_delegate_partial":   2,
    "skill_output":            1,
    "upload_ai_clean":         1,
    "chat_upload":             1,
    "upload":                  2,
    "manual_form":             2,
}


class ReviewPolicy:
    """计算知识条目的审核级别并执行自动审核。"""

    def detect_sensitive(self, content: str) -> list[str]:
        """检测内容中命中的敏感词，返回标记列表（去重）。"""
        content_lower = content.lower()
        flags: list[str] = []
        for kw in SENSITIVE_KEYWORDS:
            if kw in content_lower or kw in content:
                flags.append(kw)
        return list(dict.fromkeys(flags))  # preserve order, deduplicate

    def detect_strategic(self, content: str) -> list[str]:
        """检测内容中命中的战略级关键词，返回标记列表。"""
        flags: list[str] = []
        for kw in STRATEGIC_KEYWORDS:
            if kw in content:
                flags.append(kw)
        return list(dict.fromkeys(flags))

    def compute_level(
        self,
        capture_mode: str,
        content: str,
        tags: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        """计算审核级别。

        Returns:
            (level, sensitivity_flags)
            level: 1=AI自动收录, 2=部门确认, 3=超管确认
        """
        base = _CAPTURE_MODE_LEVEL.get(capture_mode, 2)

        sensitive_flags = self.detect_sensitive(content)
        strategic_flags = self.detect_strategic(content)

        all_flags = list(dict.fromkeys(sensitive_flags + strategic_flags))

        if strategic_flags:
            level = max(base, 3)
        elif sensitive_flags:
            level = max(base, 2)
        else:
            level = base

        return level, all_flags

    def auto_review(
        self,
        capture_mode: str,
        content: str,
        tags: list[str] | None = None,
    ) -> tuple[bool, int, list[str], str]:
        """判断是否可以自动通过，返回审核结果。

        Returns:
            (auto_pass, level, flags, note)
            auto_pass: True=直接APPROVED, False=需人工审核
        """
        level, flags = self.compute_level(capture_mode, content, tags)

        if level <= 1:
            note = f"自动审核通过 (capture_mode={capture_mode}, level=L{level})"
            return True, level, flags, note

        if flags:
            note = f"检测到敏感词: {', '.join(flags[:5])}，需人工审核 (L{level})"
        else:
            note = f"来源类型 {capture_mode} 需人工审核 (L{level})"
        return False, level, flags, note


review_policy = ReviewPolicy()
