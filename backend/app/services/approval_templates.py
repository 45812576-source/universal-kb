"""审批模板配置 — 每种 request_type 对应一个审查模板

模板驱动审批决策：
- required_evidence: 必须提交的证据清单
- review_checklist: 审批人逐条确认清单
- approval_criteria / rejection_criteria: 通过/驳回标准
"""

from __future__ import annotations

from typing import Any


# ─── 模板定义 ─────────────────────────────────────────────────────────────────

APPROVAL_TEMPLATES: dict[str, dict[str, Any]] = {
    # ── Skill 类 ──────────────────────────────────────────────────────────────
    "skill_publish": {
        "decision_focus": "Skill 是否可安全发布，输入输出边界是否明确",
        "required_evidence": [
            {"key": "change_note", "label": "发布说明", "required": True, "auto": True},
            {"key": "test_result", "label": "沙盒测试结果", "required": True, "auto": True},
            {"key": "resource_refs", "label": "引用资源清单", "required": True, "auto": True},
            {"key": "permission_config", "label": "权限与脱敏设置", "required": True, "auto": True},
            {"key": "rollback_plan", "label": "回滚方案", "required": True, "auto": False},
            {"key": "owner_info", "label": "负责人信息", "required": True, "auto": True},
        ],
        "review_checklist": [
            "功能范围明确，不存在越界行为",
            "沙盒测试通过，三项评价达标",
            "权限/脱敏设置合规",
            "输出边界清楚，无误导用户风险",
            "引用资源无高风险敏感数据泄露",
            "有可执行的回滚路径",
        ],
        "approval_criteria": "功能范围明确、测试通过、权限合规、风险可接受、有回滚路径",
        "rejection_criteria": "无测试证据 / 权限未说明 / 无回滚方案 / 高风险引用",
        "post_approve": "更新 Skill 状态为 published，应用 Policy",
        "post_reject": "回退 Skill 状态为 draft",
    },
    "skill_version_change": {
        "decision_focus": "版本变更是否安全，对现有用户的影响是否可控",
        "required_evidence": [
            {"key": "change_note", "label": "变更说明", "required": True, "auto": True},
            {"key": "version_diff", "label": "版本差异", "required": True, "auto": True},
            {"key": "test_result", "label": "测试结果", "required": True, "auto": True},
            {"key": "impact_analysis", "label": "影响分析", "required": True, "auto": False},
        ],
        "review_checklist": [
            "变更范围明确，不超出预期",
            "向后兼容或有迁移说明",
            "测试覆盖变更内容",
            "对现有用户无破坏性影响",
        ],
        "approval_criteria": "变更合理、测试通过、影响可控",
        "rejection_criteria": "无测试证据 / 破坏性变更未说明 / 影响范围不清",
        "post_approve": "版本已创建，无需额外操作",
        "post_reject": "保持当前版本",
    },
    "skill_ownership_transfer": {
        "decision_focus": "所有权转让是否合理，新所有者是否具备维护能力",
        "required_evidence": [
            {"key": "transfer_reason", "label": "转让原因", "required": True, "auto": False},
            {"key": "new_owner_info", "label": "新所有者信息", "required": True, "auto": True},
            {"key": "handover_plan", "label": "交接计划", "required": False, "auto": False},
        ],
        "review_checklist": [
            "转让原因合理",
            "新所有者具备维护能力",
            "交接计划完整",
        ],
        "approval_criteria": "原因合理、新所有者具备能力",
        "rejection_criteria": "新所有者不合适 / 转让原因不充分",
        "post_approve": "更新 Skill created_by 为新所有者",
        "post_reject": "保持原所有者",
    },
    "tool_publish": {
        "decision_focus": "工具是否安全可发布，依赖和权限是否声明清楚",
        "required_evidence": [
            {"key": "tool_manifest", "label": "工具 Manifest", "required": True, "auto": True},
            {"key": "deploy_info", "label": "部署信息", "required": True, "auto": True},
            {"key": "test_result", "label": "测试结果", "required": True, "auto": True},
            {"key": "permission_declaration", "label": "权限声明", "required": True, "auto": True},
            {"key": "rollback_plan", "label": "回滚方案", "required": True, "auto": False},
        ],
        "review_checklist": [
            "工具功能描述准确",
            "权限声明完整，无隐式权限",
            "依赖环境已说明",
            "测试结果达标",
            "有可执行的回滚路径",
        ],
        "approval_criteria": "Manifest 完整、权限合规、测试通过、有回滚路径",
        "rejection_criteria": "权限未声明 / 无测试 / 依赖不清",
        "post_approve": "更新工具状态为 published",
        "post_reject": "回退工具状态为 draft",
    },

    # ── 知识类 ────────────────────────────────────────────────────────────────
    "knowledge_review": {
        "decision_focus": "知识内容是否准确、合规，敏感信息是否处理",
        "required_evidence": [
            {"key": "content_preview", "label": "内容预览", "required": True, "auto": True},
            {"key": "source_info", "label": "来源信息", "required": True, "auto": True},
            {"key": "sensitivity_check", "label": "敏感检查结果", "required": True, "auto": True},
            {"key": "ai_review_note", "label": "AI 审核意见", "required": False, "auto": True},
        ],
        "review_checklist": [
            "内容准确，无明显错误",
            "敏感信息已脱敏或标注",
            "分类归属正确",
            "符合知识管理规范",
        ],
        "approval_criteria": "内容准确、敏感信息处理得当、分类正确",
        "rejection_criteria": "内容不准确 / 敏感信息未处理 / 分类错误",
        "post_approve": "知识状态更新为 approved",
        "post_reject": "知识状态更新为 rejected",
    },
    "knowledge_edit": {
        "decision_focus": "申请者是否有合理的编辑需求和能力",
        "required_evidence": [
            {"key": "edit_reason", "label": "编辑理由", "required": True, "auto": False},
            {"key": "document_info", "label": "文档信息", "required": True, "auto": True},
        ],
        "review_checklist": [
            "编辑理由合理",
            "申请者具备相关知识背景",
        ],
        "approval_criteria": "理由合理、申请者具备背景",
        "rejection_criteria": "理由不充分 / 非必要编辑",
        "post_approve": "写入 KnowledgeEditGrant",
        "post_reject": "拒绝授权",
    },

    # ── Web App ───────────────────────────────────────────────────────────────
    "webapp_publish": {
        "decision_focus": "Web 应用是否安全、功能是否符合预期",
        "required_evidence": [
            {"key": "app_info", "label": "应用基本信息", "required": True, "auto": True},
            {"key": "code_preview", "label": "代码预览", "required": True, "auto": True},
            {"key": "creator_info", "label": "创建者信息", "required": True, "auto": True},
        ],
        "review_checklist": [
            "应用功能描述准确",
            "无安全风险（XSS、数据泄露等）",
            "界面可用，无明显 Bug",
        ],
        "approval_criteria": "功能合规、无安全风险",
        "rejection_criteria": "存在安全风险 / 功能不符",
        "post_approve": "更新 WebApp 状态为 published",
        "post_reject": "回退 WebApp 状态为 draft",
    },

    # ── 权限 & 脱敏 ──────────────────────────────────────────────────────────
    "scope_change": {
        "decision_focus": "权限范围变更是否必要，对数据安全的影响",
        "required_evidence": [
            {"key": "current_scope", "label": "当前权限范围", "required": True, "auto": True},
            {"key": "target_scope", "label": "目标权限范围", "required": True, "auto": False},
            {"key": "change_reason", "label": "变更原因", "required": True, "auto": False},
            {"key": "impact_analysis", "label": "影响分析", "required": True, "auto": False},
        ],
        "review_checklist": [
            "变更原因合理",
            "目标范围符合最小权限原则",
            "影响范围已评估",
            "不会造成数据泄露风险",
        ],
        "approval_criteria": "原因合理、最小权限、影响可控",
        "rejection_criteria": "权限过大 / 原因不充分 / 影响不清",
        "post_approve": "应用新的权限范围",
        "post_reject": "保持原权限",
    },
    "mask_override": {
        "decision_focus": "脱敏覆盖是否必要，数据安全风险是否可控",
        "required_evidence": [
            {"key": "current_mask", "label": "当前脱敏规则", "required": True, "auto": True},
            {"key": "target_mask", "label": "目标脱敏规则", "required": True, "auto": False},
            {"key": "override_reason", "label": "覆盖原因", "required": True, "auto": False},
            {"key": "data_usage_scenario", "label": "数据使用场景", "required": True, "auto": False},
        ],
        "review_checklist": [
            "覆盖原因合理且有业务必要性",
            "覆盖范围最小化",
            "数据使用场景明确",
            "不违反合规要求",
        ],
        "approval_criteria": "业务必要、范围最小、合规",
        "rejection_criteria": "无业务必要 / 范围过大 / 违反合规",
        "post_approve": "应用脱敏覆盖规则",
        "post_reject": "保持原脱敏规则",
    },
    "schema_approval": {
        "decision_focus": "输出 Schema 变更是否合理，对下游的影响是否可控",
        "required_evidence": [
            {"key": "schema_diff", "label": "Schema 差异", "required": True, "auto": True},
            {"key": "downstream_impact", "label": "下游影响", "required": True, "auto": False},
            {"key": "change_reason", "label": "变更原因", "required": True, "auto": False},
        ],
        "review_checklist": [
            "Schema 变更合理",
            "下游影响已评估",
            "向后兼容或有迁移计划",
        ],
        "approval_criteria": "变更合理、影响可控、有迁移计划",
        "rejection_criteria": "破坏性变更 / 影响不清",
        "post_approve": "更新 Schema 状态为 approved",
        "post_reject": "保持 Schema 为 draft",
    },

    # ── 数据安全 6 类 ─────────────────────────────────────────────────────────
    "export_sensitive": {
        "decision_focus": "敏感数据导出是否必要，导出后数据安全如何保障",
        "required_evidence": [
            {"key": "export_scope", "label": "导出范围", "required": True, "auto": False},
            {"key": "export_purpose", "label": "导出目的", "required": True, "auto": False},
            {"key": "data_protection_plan", "label": "数据保护方案", "required": True, "auto": False},
            {"key": "retention_period", "label": "保留期限", "required": True, "auto": False},
        ],
        "review_checklist": [
            "导出目的合理且有业务必要性",
            "导出范围最小化",
            "数据保护方案完整（加密、权限控制等）",
            "保留期限明确且合理",
            "符合数据合规要求",
        ],
        "approval_criteria": "业务必要、范围最小、保护到位、合规",
        "rejection_criteria": "无业务必要 / 范围过大 / 无保护方案 / 违反合规",
        "post_approve": "授权导出，记录审计日志",
        "post_reject": "拒绝导出",
    },
    "elevate_disclosure": {
        "decision_focus": "提升披露等级是否必要，对数据安全的影响",
        "required_evidence": [
            {"key": "current_level", "label": "当前披露等级", "required": True, "auto": True},
            {"key": "target_level", "label": "目标披露等级", "required": True, "auto": False},
            {"key": "elevation_reason", "label": "提升原因", "required": True, "auto": False},
            {"key": "impact_assessment", "label": "影响评估", "required": True, "auto": False},
        ],
        "review_checklist": [
            "提升原因合理",
            "目标等级适当",
            "影响评估完整",
            "不会导致敏感数据过度暴露",
        ],
        "approval_criteria": "原因合理、等级适当、影响可控",
        "rejection_criteria": "等级过高 / 原因不充分 / 影响评估缺失",
        "post_approve": "更新披露等级",
        "post_reject": "保持当前等级",
    },
    "grant_access": {
        "decision_focus": "访问权限授予是否必要，是否符合最小权限原则",
        "required_evidence": [
            {"key": "access_scope", "label": "访问范围", "required": True, "auto": False},
            {"key": "access_reason", "label": "访问原因", "required": True, "auto": False},
            {"key": "grantee_info", "label": "被授权人信息", "required": True, "auto": True},
            {"key": "duration", "label": "授权期限", "required": True, "auto": False},
        ],
        "review_checklist": [
            "访问原因合理",
            "访问范围最小化",
            "被授权人身份确认",
            "授权期限合理",
        ],
        "approval_criteria": "原因合理、范围最小、期限明确",
        "rejection_criteria": "范围过大 / 原因不充分 / 期限不合理",
        "post_approve": "授予访问权限",
        "post_reject": "拒绝授权",
    },
    "policy_change": {
        "decision_focus": "策略变更是否合理，对系统安全的影响",
        "required_evidence": [
            {"key": "current_policy", "label": "当前策略", "required": True, "auto": True},
            {"key": "target_policy", "label": "目标策略", "required": True, "auto": False},
            {"key": "change_reason", "label": "变更原因", "required": True, "auto": False},
            {"key": "impact_analysis", "label": "影响分析", "required": True, "auto": False},
            {"key": "rollback_plan", "label": "回滚方案", "required": True, "auto": False},
        ],
        "review_checklist": [
            "变更原因合理",
            "目标策略符合安全要求",
            "影响分析完整",
            "有可执行的回滚方案",
            "不违反上级策略约束",
        ],
        "approval_criteria": "原因合理、策略合规、影响可控、有回滚方案",
        "rejection_criteria": "违反安全要求 / 影响不清 / 无回滚方案",
        "post_approve": "应用新策略",
        "post_reject": "保持原策略",
    },
    "field_sensitivity_change": {
        "decision_focus": "字段敏感级别变更是否合理，对脱敏策略的连锁影响",
        "required_evidence": [
            {"key": "field_info", "label": "字段信息", "required": True, "auto": True},
            {"key": "current_sensitivity", "label": "当前敏感级别", "required": True, "auto": True},
            {"key": "target_sensitivity", "label": "目标敏感级别", "required": True, "auto": False},
            {"key": "change_reason", "label": "变更原因", "required": True, "auto": False},
            {"key": "cascade_impact", "label": "连锁影响评估", "required": True, "auto": False},
        ],
        "review_checklist": [
            "变更原因合理",
            "目标级别与数据实际敏感度匹配",
            "连锁影响（脱敏规则、权限策略）已评估",
            "不会导致敏感数据暴露",
        ],
        "approval_criteria": "原因合理、级别匹配、连锁影响可控",
        "rejection_criteria": "级别不匹配 / 连锁影响不清 / 可能导致数据暴露",
        "post_approve": "更新字段敏感级别，触发脱敏策略重算",
        "post_reject": "保持当前敏感级别",
    },
    "small_sample_change": {
        "decision_focus": "小样本保护变更是否合理，对数据隐私的影响",
        "required_evidence": [
            {"key": "current_config", "label": "当前保护配置", "required": True, "auto": True},
            {"key": "target_config", "label": "目标保护配置", "required": True, "auto": False},
            {"key": "change_reason", "label": "变更原因", "required": True, "auto": False},
            {"key": "privacy_impact", "label": "隐私影响评估", "required": True, "auto": False},
        ],
        "review_checklist": [
            "变更原因合理",
            "新配置不低于最低保护要求",
            "隐私影响已评估",
            "不会导致个体可识别",
        ],
        "approval_criteria": "原因合理、保护级别充分、隐私安全",
        "rejection_criteria": "保护级别不足 / 隐私风险 / 原因不充分",
        "post_approve": "应用新保护配置",
        "post_reject": "保持当前配置",
    },
}


# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

def get_template(request_type: str) -> dict[str, Any] | None:
    """获取审批模板"""
    return APPROVAL_TEMPLATES.get(request_type)


def check_evidence_completeness(request_type: str, evidence_pack: dict | None) -> list[str]:
    """校验证据包完整性，返回缺失的必填项 key 列表"""
    tpl = get_template(request_type)
    if not tpl:
        return []

    evidence = evidence_pack or {}
    missing = []
    for item in tpl.get("required_evidence", []):
        if item["required"] and evidence.get(item["key"]) is None:
            missing.append(item["key"])
    return missing


def get_auto_evidence(request_type: str, target_type: str | None, target_id: int | None, db) -> dict[str, Any]:
    """自动采集可自动生成的证据项"""
    tpl = get_template(request_type)
    if not tpl or not target_id:
        return {}

    auto_keys = {item["key"] for item in tpl.get("required_evidence", []) if item.get("auto")}
    if not auto_keys:
        return {}

    evidence: dict[str, Any] = {}

    # ── Skill 类 ──
    if target_type == "skill" and request_type in ("skill_publish", "skill_version_change", "skill_ownership_transfer"):
        from app.models.skill import Skill, SkillVersion
        skill = db.get(Skill, target_id)
        if skill:
            latest_ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            if "change_note" in auto_keys and latest_ver:
                evidence["change_note"] = latest_ver.change_note or ""
            if "owner_info" in auto_keys:
                owner = db.get(db.bind.dialect.dbapi.module if False else None, None)  # placeholder
                from app.models.user import User
                owner = db.get(User, skill.created_by)
                evidence["owner_info"] = {
                    "user_id": skill.created_by,
                    "name": owner.display_name if owner else str(skill.created_by),
                }
            if "resource_refs" in auto_keys:
                evidence["resource_refs"] = {
                    "source_files": skill.source_files or [],
                    "knowledge_tags": skill.knowledge_tags or [],
                    "data_queries": skill.data_queries or [],
                }
            if "test_result" in auto_keys:
                # 尝试获取沙盒测试报告
                from app.models.sandbox import SandboxTestReport
                report = (
                    db.query(SandboxTestReport)
                    .filter(SandboxTestReport.target_type == "skill", SandboxTestReport.target_id == target_id)
                    .order_by(SandboxTestReport.created_at.desc())
                    .first()
                )
                if report:
                    evidence["test_result"] = {
                        "report_id": report.id,
                        "quality_passed": report.quality_passed,
                        "usability_passed": report.usability_passed,
                        "anti_hallucination_passed": report.anti_hallucination_passed,
                        "approval_eligible": report.approval_eligible,
                    }
            if "permission_config" in auto_keys:
                from app.models.permission import SkillPolicy
                policy = db.query(SkillPolicy).filter(SkillPolicy.skill_id == target_id).first()
                if policy:
                    evidence["permission_config"] = {
                        "publish_scope": policy.publish_scope.value if policy.publish_scope else None,
                        "view_scope": policy.view_scope.value if policy.view_scope else None,
                    }
            if "version_diff" in auto_keys and latest_ver and latest_ver.version > 1:
                prev_ver = (
                    db.query(SkillVersion)
                    .filter(SkillVersion.skill_id == skill.id, SkillVersion.version == latest_ver.version - 1)
                    .first()
                )
                if prev_ver:
                    evidence["version_diff"] = {
                        "prev_version": prev_ver.version,
                        "current_version": latest_ver.version,
                        "prev_prompt_len": len(prev_ver.system_prompt or ""),
                        "current_prompt_len": len(latest_ver.system_prompt or ""),
                    }
            if "new_owner_info" in auto_keys:
                # 从 conditions 获取 new_owner_id
                pass  # 在 API 层面处理

    # ── Tool 类 ──
    elif target_type == "tool" and request_type == "tool_publish":
        from app.models.tool import ToolRegistry
        tool = db.get(ToolRegistry, target_id)
        if tool:
            config = tool.config or {}
            manifest = config.get("manifest", {})
            deploy_info = config.get("deploy_info", {})
            if "tool_manifest" in auto_keys:
                evidence["tool_manifest"] = manifest
            if "deploy_info" in auto_keys:
                evidence["deploy_info"] = deploy_info
            if "permission_declaration" in auto_keys:
                evidence["permission_declaration"] = manifest.get("permissions", deploy_info.get("permissions", []))
            if "test_result" in auto_keys:
                evidence["test_result"] = {"tested": deploy_info.get("tested", False), "test_note": deploy_info.get("test_note", "")}

    # ── Knowledge 类 ──
    elif target_type == "knowledge":
        from app.models.knowledge import KnowledgeEntry
        entry = db.get(KnowledgeEntry, target_id)
        if entry:
            if "content_preview" in auto_keys:
                content = entry.content or ""
                evidence["content_preview"] = content[:2000] if len(content) > 2000 else content
            if "source_info" in auto_keys:
                evidence["source_info"] = {
                    "source_file": entry.source_file,
                    "category": entry.category,
                    "file_ext": entry.file_ext,
                }
            if "sensitivity_check" in auto_keys:
                evidence["sensitivity_check"] = {
                    "sensitivity_flags": entry.sensitivity_flags or [],
                    "review_level": entry.review_level,
                }
            if "ai_review_note" in auto_keys and entry.auto_review_note:
                evidence["ai_review_note"] = entry.auto_review_note
            if "document_info" in auto_keys:
                evidence["document_info"] = {
                    "title": entry.ai_title or entry.title,
                    "category": entry.category,
                    "creator_id": entry.created_by,
                }

    # ── WebApp 类 ──
    elif target_type == "webapp" and request_type == "webapp_publish":
        from app.models.web_app import WebApp
        webapp = db.get(WebApp, target_id)
        if webapp:
            if "app_info" in auto_keys:
                evidence["app_info"] = {"name": webapp.name, "description": webapp.description or ""}
            if "code_preview" in auto_keys:
                code = webapp.html_code or ""
                evidence["code_preview"] = code[:3000] if len(code) > 3000 else code
            if "creator_info" in auto_keys:
                from app.models.user import User
                creator = db.get(User, webapp.created_by) if webapp.created_by else None
                evidence["creator_info"] = {
                    "user_id": webapp.created_by,
                    "name": creator.display_name if creator else None,
                }

    # ── 权限 & 脱敏 / 数据安全类 ──
    # auto 证据主要来自当前快照，大部分需要后端数据接口对接
    # 此处提供占位，标注"待系统对接"
    elif request_type in ("scope_change", "mask_override", "schema_approval",
                          "export_sensitive", "elevate_disclosure", "grant_access",
                          "policy_change", "field_sensitivity_change", "small_sample_change"):
        for item in tpl.get("required_evidence", []):
            if item.get("auto") and item["key"] not in evidence:
                evidence[item["key"]] = {"_status": "pending_integration", "_label": "待系统对接"}

    return evidence
