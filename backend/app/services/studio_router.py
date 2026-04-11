"""Studio Session Router — 首轮用户消息 + skill 状态联合判断 session 模式。

支持 architect_mode：当需求模糊/框架层问题/导入重构时，进入分阶段咨询式引导。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillVersion

logger = logging.getLogger(__name__)


ARCHITECT_PHASES = (
    "phase_1_why", "phase_2_what", "phase_3_how",
    "ooda_iteration", "ready_for_draft",
)


@dataclass
class RouteResult:
    session_mode: str  # "create_new_skill" | "optimize_existing_skill" | "audit_imported_skill"
    active_assist_skills: list[str] = field(default_factory=list)
    route_reason: str = ""
    next_action: str = ""  # "collect_requirements" | "run_audit" | "start_editing"
    workflow_mode: str = "none"  # "architect_mode" | "none"
    initial_phase: str = ""  # 仅 architect_mode 时有值


# ── Intent 关键词规则 ──────────────────────────────────────────────────────────

_AUDIT_KEYWORDS = re.compile(
    r"审计|检查质量|质量评估|诊断|review|audit|检测|分析问题", re.IGNORECASE
)
_CREATE_KEYWORDS = re.compile(
    r"新建|创建|从零|从头|新 skill|新技能|build.*new|create.*new|start.*fresh", re.IGNORECASE
)
_OPTIMIZE_KEYWORDS = re.compile(
    r"优化|改进|提升|修改|改一?改|改[下掉]|调整|重构|improve|optimize|refine|enhance|tweak", re.IGNORECASE
)
# 已有完整 spec 的信号
_SPEC_READY_KEYWORDS = re.compile(
    r"spec.*已[经有]|已有.*spec|文档.*齐了|需求.*完整|按.*这个.*来|直接.*生成|have.*spec|spec.*ready",
    re.IGNORECASE,
)
# 低复杂度 — 不启用 architect
_SIMPLE_PATCH_KEYWORDS = re.compile(
    r"润色|微调|小改|换个词|wording|typo|错别字|只改.*一[点处]|patch|小修",
    re.IGNORECASE,
)


def _classify_intent(user_message: str) -> str | None:
    """从首轮用户消息中提取意图倾向。"""
    if not user_message:
        return None
    if _AUDIT_KEYWORDS.search(user_message):
        return "audit"
    if _CREATE_KEYWORDS.search(user_message):
        return "create"
    if _OPTIMIZE_KEYWORDS.search(user_message):
        return "optimize"
    return None


def _should_use_architect(user_message: str, skill: Skill | None, latest_prompt: str) -> tuple[bool, str]:
    """判断是否启用 architect_mode，返回 (should_use, initial_phase)。

    启用条件（§3.1）：
    - 新建 + 需求模糊 → phase_1_why
    - 优化 + 框架层问题（prompt 结构差） → phase_1_why
    - 导入 + 审计发现框架问题 → phase_1_why（由 audit 后升级）
    - 已有完整 spec → phase_3_how

    不启用条件（§3.2）：
    - 纯润色/小 patch
    """
    # 小 patch 不启用
    if _SIMPLE_PATCH_KEYWORDS.search(user_message or ""):
        return False, ""

    # 已有完整 spec → 直接 phase_3
    if _SPEC_READY_KEYWORDS.search(user_message or ""):
        return True, "phase_3_how"

    # 无 skill 或极短 prompt → 需求模糊，phase_1
    if not skill or not latest_prompt or len(latest_prompt) < 50:
        return True, "phase_1_why"

    # 有 skill 且 prompt 长度合理 → 看 prompt 质量信号
    # 简单启发式：如果 prompt 无明确角色定义/无结构化输出/无分步骤 → 框架层问题
    _has_role = bool(re.search(r"你是|你的角色|You are|Act as|Role:", latest_prompt, re.IGNORECASE))
    _has_structure = bool(re.search(r"##|步骤|Step|输出格式|Output|JSON|markdown", latest_prompt, re.IGNORECASE))
    if not _has_role and not _has_structure:
        return True, "phase_1_why"

    return False, ""


def route_session(
    db: Session,
    skill_id: int | None = None,
    user_message: str = "",
) -> RouteResult:
    """根据首轮用户消息 + skill 属性联合判断 Studio session 模式。

    路由规则（优先级从高到低）：
    1. 用户消息显式意图（audit/create/optimize 关键词）
    2. skill 属性：无 skill_id → create；source_type == imported → audit
    3. 默认有 skill → optimize

    辅助 Skill 分配：
    - create_new_skill: brainstorming + mckinsey
    - audit_imported_skill: mckinsey + quality_audit
    - optimize_existing_skill: prompt_optimizer + mckinsey

    architect_mode 叠加：符合条件时 workflow_mode=architect_mode + initial_phase。
    """
    intent = _classify_intent(user_message)

    # 预加载 skill + latest prompt 用于 architect 判断
    skill = db.get(Skill, skill_id) if skill_id else None
    latest_prompt = ""
    if skill:
        latest = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version.desc())
            .first()
        )
        latest_prompt = latest.system_prompt if latest else ""

    # ── 判断 architect_mode ──
    use_architect, arch_phase = _should_use_architect(user_message, skill, latest_prompt)

    def _with_architect(r: RouteResult) -> RouteResult:
        """如果 architect 判断启用，叠加 workflow_mode + initial_phase + skill。"""
        if use_architect:
            r.workflow_mode = "architect_mode"
            r.initial_phase = arch_phase
            if "skill-architect-master" not in r.active_assist_skills:
                r.active_assist_skills.append("skill-architect-master")
        return r

    # ── 无 skill_id ──
    if not skill_id or not skill:
        return _with_architect(RouteResult(
            session_mode="create_new_skill",
            active_assist_skills=["brainstorming", "mckinsey"],
            route_reason="no_skill_id" if not skill_id else "skill_not_found",
            next_action="collect_requirements",
        ))

    # ── 用户显式意图优先 ──
    if intent == "create":
        return _with_architect(RouteResult(
            session_mode="create_new_skill",
            active_assist_skills=["brainstorming", "mckinsey"],
            route_reason="user_intent_create",
            next_action="collect_requirements",
        ))

    if intent == "audit":
        r = RouteResult(
            session_mode="audit_imported_skill",
            active_assist_skills=["mckinsey", "quality_audit"],
            route_reason="user_intent_audit",
            next_action="run_audit",
        )
        # audit 后由 audit_summary 判断是否升级 architect_mode（不在 route 时直接设）
        return r

    if intent == "optimize":
        return _with_architect(RouteResult(
            session_mode="optimize_existing_skill",
            active_assist_skills=["prompt_optimizer", "mckinsey"],
            route_reason="user_intent_optimize",
            next_action="start_editing",
        ))

    # ── skill 属性兜底 ──
    if skill.source_type == "imported":
        return RouteResult(
            session_mode="audit_imported_skill",
            active_assist_skills=["mckinsey", "quality_audit"],
            route_reason="imported_skill",
            next_action="run_audit",
        )

    if not latest_prompt or len(latest_prompt) < 50:
        return _with_architect(RouteResult(
            session_mode="create_new_skill",
            active_assist_skills=["brainstorming", "mckinsey"],
            route_reason="empty_or_minimal_skill",
            next_action="collect_requirements",
        ))

    return _with_architect(RouteResult(
        session_mode="optimize_existing_skill",
        active_assist_skills=["prompt_optimizer", "mckinsey"],
        route_reason="existing_skill",
        next_action="start_editing",
    ))
