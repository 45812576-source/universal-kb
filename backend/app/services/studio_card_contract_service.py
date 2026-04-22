"""Studio Card Contract Service — 后端 canonical card contract 定义。

Phase B6: card contract 从前端静态文件迁到后端。
初期为 Python dict 静态配置，后续可 versioned JSON / DB-backed。
contract 改版后前端无需发布即可生效。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class StudioCardCta:
    action_id: str
    label: str
    tone: str = "primary"  # primary | secondary | danger

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StudioCardContract:
    contract_id: str
    title: str
    phase: str  # create | refine | governance | validation | fixing | confirm | release
    objective: str
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    exit_criteria: list[dict[str, Any]] = field(default_factory=list)
    artifact_schema: dict[str, Any] = field(default_factory=dict)
    next_cards: list[str] = field(default_factory=list)
    stale_policy: str = "supersede"  # supersede | keep | archive
    transition_rules: list[dict[str, Any]] = field(default_factory=list)
    drawer_policy: str = "never"  # never | manual | on_pending_edit
    ctas: list[StudioCardCta] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 移除空值
        for k in ("exit_criteria", "artifact_schema", "transition_rules"):
            if not data.get(k):
                data.pop(k, None)
        return data

    def to_summary(self) -> dict[str, Any]:
        """给前端的轻量摘要 — 不含完整 transition_rules。"""
        return {
            "contract_id": self.contract_id,
            "title": self.title,
            "phase": self.phase,
            "objective": self.objective,
            "allowed_tools": self.allowed_tools,
            "forbidden_actions": self.forbidden_actions,
            "next_cards": self.next_cards,
            "drawer_policy": self.drawer_policy,
            "ctas": [c.to_dict() for c in self.ctas] if isinstance(self.ctas, list) and self.ctas and isinstance(self.ctas[0], StudioCardCta) else self.ctas,
        }


# ── File Role CTA 映射 ───────────────────────────────────────────────────────

FILE_ROLE_CTAS: dict[str, list[dict[str, Any]]] = {
    "main_prompt": [
        {"action_id": "architect.continue", "label": "继续主文件编排"},
        {"action_id": "draft.apply", "label": "查看修改"},
        {"action_id": "validation.open_sandbox", "label": "运行测试"},
    ],
    "example": [
        {"action_id": "file_role.generate_examples", "label": "生成示例"},
        {"action_id": "file_role.calibrate_examples", "label": "校准示例"},
        {"action_id": "file_role.link_main_prompt", "label": "关联主 Prompt"},
    ],
    "reference": [
        {"action_id": "file_role.summarize_reference", "label": "摘要资料"},
        {"action_id": "file_role.extract_rules", "label": "提取引用规则"},
        {"action_id": "file_role.suggest_prompt_update", "label": "生成主 Prompt 建议"},
    ],
    "knowledge_base": [
        {"action_id": "file_role.organize_knowledge", "label": "整理知识"},
        {"action_id": "file_role.bind_knowledge", "label": "绑定知识库"},
        {"action_id": "governance.open_panel", "label": "打开治理面板"},
    ],
    "tool": [
        {"action_id": "file_role.generate_tool_package", "label": "生成工具交接包"},
        {"action_id": "handoff.external_build", "label": "去外部完成实现", "tone": "secondary"},
        {"action_id": "file_role.start_validation", "label": "开始验证"},
    ],
    "template": [{"action_id": "architect.continue", "label": "继续编排"}],
    "unknown_asset": [{"action_id": "architect.continue", "label": "继续编排"}],
}


# ── Contract Registry ─────────────────────────────────────────────────────────

def _cta(action_id: str, label: str, tone: str = "primary") -> StudioCardCta:
    return StudioCardCta(action_id=action_id, label=label, tone=tone)


_CONTRACTS: dict[str, StudioCardContract] = {
    # ── Create 模式 ──
    "create.onboarding": StudioCardContract(
        contract_id="create.onboarding",
        title="创作起步卡",
        phase="create",
        objective="先把用户问题、目标和场景拉进当前 Skill 的创作上下文。",
        allowed_tools=["studio_chat.ask_one_question", "studio_artifact.save"],
        forbidden_actions=["不要直接生成最终草稿。", "不要提前跳到治理或测试。"],
        next_cards=["create.summary_ready", "architect.phase.execute"],
        ctas=[_cta("chat.start_requirement", "开始描述需求")],
    ),
    "create.summary_ready": StudioCardContract(
        contract_id="create.summary_ready",
        title="需求摘要确认卡",
        phase="create",
        objective="确认 AI 对需求的理解，再决定进入目录或草稿生成。",
        allowed_tools=["studio_artifact.update", "skill_draft.generate"],
        forbidden_actions=["不要跳过用户确认直接生成草稿。"],
        next_cards=["architect.phase.execute", "refine.draft_ready"],
        ctas=[_cta("summary.confirm", "确认摘要"), _cta("summary.discard", "放弃", "danger")],
    ),
    "architect.phase.execute": StudioCardContract(
        contract_id="architect.phase.execute",
        title="架构阶段执行卡",
        phase="create",
        objective="围绕当前 Why / What / How 阶段继续追问、拆解和收敛。",
        allowed_tools=["studio_chat.ask_one_question", "studio_artifact.save", "studio_artifact.update"],
        forbidden_actions=["不要在阶段未收敛时提前生成最终草稿。"],
        next_cards=["create.summary_ready", "refine.draft_ready"],
        ctas=[_cta("architect.continue", "继续当前阶段")],
    ),

    # ── Refine 模式 ──
    "refine.draft_ready": StudioCardContract(
        contract_id="refine.draft_ready",
        title="草稿确认卡",
        phase="refine",
        objective="让草稿先以待确认修改的形式进入工作区，再决定采纳与否。",
        allowed_tools=["skill_draft.stage_edit", "staged_edit.adopt", "staged_edit.reject"],
        forbidden_actions=["不要未经确认直接覆盖编辑区。"],
        next_cards=["refine.file_split", "governance.panel", "validation.test_ready"],
        drawer_policy="on_pending_edit",
        ctas=[_cta("draft.apply", "应用草稿"), _cta("draft.discard", "放弃", "danger")],
    ),
    "refine.file_split": StudioCardContract(
        contract_id="refine.file_split",
        title="文件拆分确认卡",
        phase="refine",
        objective="在真正写入前确认拆分结构和主 Prompt 的变更。",
        allowed_tools=["skill_file.stage_edit", "staged_edit.adopt", "staged_edit.reject"],
        forbidden_actions=["不要未确认直接创建拆分文件。"],
        next_cards=["governance.panel", "validation.test_ready"],
        drawer_policy="on_pending_edit",
        ctas=[_cta("split.confirm", "确认拆分"), _cta("split.discard", "放弃", "danger")],
    ),
    "refine.tool_suggestion": StudioCardContract(
        contract_id="refine.tool_suggestion",
        title="工具绑定建议卡",
        phase="refine",
        objective="逐个确认 AI 推荐的外部工具绑定，或跳转外部完成实现。",
        allowed_tools=["studio_chat.ask_one_question", "studio_artifact.update"],
        forbidden_actions=["不要跳过确认直接绑定。"],
        next_cards=["governance.panel", "validation.test_ready"],
        ctas=[_cta("tool.confirm", "在 Studio 中完成绑定"), _cta("handoff.external_build", "去外部完成实现", "secondary")],
    ),
    "refine.knowledge_binding_hint": StudioCardContract(
        contract_id="refine.knowledge_binding_hint",
        title="知识绑定建议卡",
        phase="refine",
        objective="补齐知识标签或引用来源，避免 Skill 失去上下文支撑。",
        allowed_tools=["studio_chat.ask_one_question", "studio_artifact.update"],
        forbidden_actions=["不要把知识绑定当成可跳过的隐形步骤。"],
        next_cards=["governance.panel", "validation.test_ready"],
        drawer_policy="manual",
        ctas=[_cta("knowledge.bind", "绑定知识库")],
    ),

    # ── Governance ──
    "governance.panel": StudioCardContract(
        contract_id="governance.panel",
        title="治理推进卡",
        phase="governance",
        objective="让权限、挂载、测试方案等治理动作回到主线推进。",
        allowed_tools=["skill_governance.open_panel", "studio_artifact.update"],
        forbidden_actions=["不要让治理动作停留在旁路提示里。"],
        next_cards=["validation.test_ready", "fixing.overview"],
        drawer_policy="manual",
        ctas=[_cta("governance.open_panel", "打开治理面板")],
    ),

    # ── Validation ──
    "validation.test_ready": StudioCardContract(
        contract_id="validation.test_ready",
        title="测试就绪卡",
        phase="validation",
        objective="把当前 Skill 推入 Sandbox 或测试流，拿到可回流的结果。",
        allowed_tools=["sandbox.run"],
        forbidden_actions=["不要只在聊天里口头说可以测试。"],
        next_cards=["fixing.overview", "release.test_passed"],
        drawer_policy="manual",
        ctas=[_cta("validation.open_sandbox", "打开 Sandbox")],
    ),

    # ── Fixing ──
    "fixing.overview": StudioCardContract(
        contract_id="fixing.overview",
        title="整改概览卡",
        phase="fixing",
        objective="把 failed 报告转成明确的整改任务队列和下一步动作。",
        allowed_tools=["studio_artifact.update", "sandbox.targeted_rerun"],
        forbidden_actions=["不要只解释报告而不生成整改路径。"],
        next_cards=["fixing.task", "fixing.targeted_retest"],
        drawer_policy="manual",
    ),
    "fixing.task": StudioCardContract(
        contract_id="fixing.task",
        title="整改任务卡",
        phase="fixing",
        objective="聚焦单个问题项，定位目标文件并开始修复。",
        allowed_tools=["skill_file.open", "skill_file.stage_edit", "studio_chat.ask_one_question"],
        forbidden_actions=["不要同时并行修多个未确认问题。"],
        next_cards=["fixing.targeted_retest", "release.test_passed"],
        drawer_policy="manual",
        ctas=[_cta("fixing.start_task", "修复此项")],
    ),
    "fixing.targeted_retest": StudioCardContract(
        contract_id="fixing.targeted_retest",
        title="局部重测卡",
        phase="fixing",
        objective="针对已修复问题做局部验证，避免每次都跑全量。",
        allowed_tools=["sandbox.targeted_rerun"],
        forbidden_actions=["不要遗漏 source report 和 issue 范围。"],
        next_cards=["release.test_passed", "fixing.task"],
        drawer_policy="manual",
        ctas=[_cta("fixing.targeted_retest", "运行局部重测")],
    ),

    # ── Confirm ──
    "confirm.staged_edit_review": StudioCardContract(
        contract_id="confirm.staged_edit_review",
        title="待确认修改卡",
        phase="confirm",
        objective="让文件改动以 staged edit 方式进入确认流，而不是直接落盘。",
        allowed_tools=["skill_file.open", "staged_edit.adopt", "staged_edit.reject"],
        forbidden_actions=["不要跳过确认步骤直接采纳。"],
        next_cards=["validation.test_ready", "fixing.task", "release.submit"],
        drawer_policy="on_pending_edit",
    ),
    "confirm.bind_back": StudioCardContract(
        contract_id="confirm.bind_back",
        title="外部编辑回绑卡",
        phase="confirm",
        objective="外部实现已返回，先确认变更再进入验证。",
        allowed_tools=["skill_file.open", "staged_edit.adopt", "staged_edit.reject", "sandbox.run"],
        forbidden_actions=["不要跳过回绑直接继续。"],
        next_cards=["validation.test_ready", "fixing.overview"],
        drawer_policy="on_pending_edit",
        ctas=[_cta("handoff.bind_back", "确认变更并进入验证"), _cta("draft.discard", "放弃外部变更", "danger")],
    ),

    # ── Release ──
    "release.test_passed": StudioCardContract(
        contract_id="release.test_passed",
        title="测试通过卡",
        phase="release",
        objective="在测试通过后，把用户带到审批或发布前复核。",
        allowed_tools=["studio_chat.ask_one_question"],
        forbidden_actions=["不要测试通过后让用户自己找下一步。"],
        next_cards=["release.submit"],
        drawer_policy="manual",
        ctas=[_cta("release.submit_approval", "提交审批")],
    ),
    "release.submit": StudioCardContract(
        contract_id="release.submit",
        title="提交审批卡",
        phase="release",
        objective="执行提审或发布动作，闭合主流程。",
        allowed_tools=["studio_chat.ask_one_question"],
        forbidden_actions=["不要停留在'可以提交'的口头提示。"],
        next_cards=["release.completed"],
        drawer_policy="manual",
        ctas=[_cta("release.submit_approval", "执行提交")],
    ),

    # ── Optimize 模式 ──
    "optimize.governance.audit_review": StudioCardContract(
        contract_id="optimize.governance.audit_review",
        title="审计结果确认卡",
        phase="governance",
        objective="确认 Skill 审计结果，了解当前状态。",
        allowed_tools=["studio_chat.ask_one_question"],
        next_cards=["optimize.governance.constraint_check"],
        ctas=[_cta("architect.continue", "查看审计结果")],
    ),
    "optimize.governance.constraint_check": StudioCardContract(
        contract_id="optimize.governance.constraint_check",
        title="约束检查卡",
        phase="governance",
        objective="检查 Skill 是否满足优化前置约束条件。",
        allowed_tools=["studio_chat.ask_one_question"],
        next_cards=["optimize.refine.prompt_edit"],
        ctas=[_cta("architect.continue", "查看约束检查")],
    ),
    "optimize.refine.prompt_edit": StudioCardContract(
        contract_id="optimize.refine.prompt_edit",
        title="Prompt 优化卡",
        phase="refine",
        objective="根据审计和约束结果，优化 Prompt 内容。",
        allowed_tools=["skill_file.stage_edit", "staged_edit.adopt", "staged_edit.reject"],
        forbidden_actions=["不要未经确认直接覆盖。"],
        next_cards=["optimize.refine.example_edit"],
        drawer_policy="on_pending_edit",
        ctas=[_cta("draft.apply", "应用修改"), _cta("draft.discard", "放弃", "danger")],
    ),
    "optimize.refine.example_edit": StudioCardContract(
        contract_id="optimize.refine.example_edit",
        title="示例优化卡",
        phase="refine",
        objective="优化或补充 Skill 示例。",
        allowed_tools=["skill_file.stage_edit", "staged_edit.adopt", "staged_edit.reject"],
        forbidden_actions=["不要未经确认直接覆盖。"],
        next_cards=["optimize.refine.tool_binding"],
        drawer_policy="on_pending_edit",
        ctas=[_cta("draft.apply", "应用修改"), _cta("draft.discard", "放弃", "danger")],
    ),
    "optimize.refine.tool_binding": StudioCardContract(
        contract_id="optimize.refine.tool_binding",
        title="工具绑定优化卡",
        phase="refine",
        objective="优化 Skill 的工具绑定配置。",
        allowed_tools=["studio_chat.ask_one_question", "studio_artifact.update"],
        next_cards=["optimize.validation.preflight"],
        ctas=[_cta("tool.confirm", "在 Studio 中完成绑定"), _cta("handoff.external_build", "去外部完成实现", "secondary")],
    ),
    "optimize.validation.preflight": StudioCardContract(
        contract_id="optimize.validation.preflight",
        title="优化预检卡",
        phase="validation",
        objective="在优化后执行预检验证。",
        allowed_tools=["sandbox.run"],
        next_cards=["optimize.validation.sandbox_run"],
        drawer_policy="manual",
        ctas=[_cta("validation.open_sandbox", "运行预检")],
    ),
    "optimize.validation.sandbox_run": StudioCardContract(
        contract_id="optimize.validation.sandbox_run",
        title="优化验证卡",
        phase="validation",
        objective="运行 Sandbox 验证优化后的 Skill。",
        allowed_tools=["sandbox.run"],
        drawer_policy="manual",
        ctas=[_cta("validation.open_sandbox", "打开 Sandbox")],
    ),

    # ── Audit 模式 ──
    "audit.scan.quality": StudioCardContract(
        contract_id="audit.scan.quality",
        title="质量扫描卡",
        phase="governance",
        objective="对导入的 Skill 执行质量扫描，发现潜在问题。",
        allowed_tools=["studio_chat.ask_one_question"],
        next_cards=["audit.scan.security"],
        ctas=[_cta("architect.continue", "查看扫描结果")],
    ),
    "audit.scan.security": StudioCardContract(
        contract_id="audit.scan.security",
        title="安全扫描卡",
        phase="governance",
        objective="对导入的 Skill 执行安全扫描。",
        allowed_tools=["studio_chat.ask_one_question"],
        next_cards=["audit.fixing.critical"],
        ctas=[_cta("architect.continue", "查看扫描结果")],
    ),
    "audit.fixing.critical": StudioCardContract(
        contract_id="audit.fixing.critical",
        title="严重问题整改卡",
        phase="fixing",
        objective="修复扫描发现的严重问题。",
        allowed_tools=["skill_file.open", "skill_file.stage_edit", "studio_chat.ask_one_question"],
        forbidden_actions=["不要跳过严重问题。"],
        next_cards=["audit.fixing.moderate"],
        drawer_policy="manual",
        ctas=[_cta("fixing.start_task", "修复此项")],
    ),
    "audit.fixing.moderate": StudioCardContract(
        contract_id="audit.fixing.moderate",
        title="中等问题整改卡",
        phase="fixing",
        objective="修复扫描发现的中等问题。",
        allowed_tools=["skill_file.open", "skill_file.stage_edit", "studio_chat.ask_one_question"],
        next_cards=["audit.release.preflight_recheck"],
        drawer_policy="manual",
        ctas=[_cta("fixing.start_task", "修复此项")],
    ),
    "audit.release.preflight_recheck": StudioCardContract(
        contract_id="audit.release.preflight_recheck",
        title="整改后复检卡",
        phase="release",
        objective="整改完成后重新执行预检。",
        allowed_tools=["sandbox.run"],
        next_cards=["audit.release.publish_gate"],
        drawer_policy="manual",
        ctas=[_cta("validation.open_sandbox", "运行复检")],
    ),
    "audit.release.publish_gate": StudioCardContract(
        contract_id="audit.release.publish_gate",
        title="发布门禁卡",
        phase="release",
        objective="确认所有问题已修复，可以发布。",
        allowed_tools=["studio_chat.ask_one_question"],
        forbidden_actions=["不要跳过未修复的严重问题。"],
        drawer_policy="manual",
        ctas=[_cta("release.submit_approval", "确认发布")],
    ),
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_contract(contract_id: str) -> StudioCardContract | None:
    """按 contract_id 获取 contract。"""
    return _CONTRACTS.get(contract_id)


def get_contract_summary(contract_id: str) -> dict[str, Any] | None:
    """返回轻量摘要，给前端渲染用。"""
    c = _CONTRACTS.get(contract_id)
    return c.to_summary() if c else None


def get_all_contracts() -> dict[str, StudioCardContract]:
    """返回完整 contract 字典。"""
    return dict(_CONTRACTS)


def get_all_contract_summaries() -> list[dict[str, Any]]:
    """返回所有 contract 的摘要列表。"""
    return [c.to_summary() for c in _CONTRACTS.values()]


def get_allowed_tools(contract_id: str) -> list[str]:
    """按 contract_id 返回 allowed_tools。"""
    c = _CONTRACTS.get(contract_id)
    return list(c.allowed_tools) if c else []


def get_next_cards(contract_id: str) -> list[str]:
    """按 contract_id 返回 next_cards。"""
    c = _CONTRACTS.get(contract_id)
    return list(c.next_cards) if c else []


def is_tool_allowed(contract_id: str, tool_name: str) -> bool:
    """判断指定 tool 是否被 contract 允许。"""
    c = _CONTRACTS.get(contract_id)
    if not c:
        return False
    return tool_name in c.allowed_tools


def get_file_role_ctas(file_role: str) -> list[dict[str, Any]]:
    """按 file_role 获取 CTA 列表。"""
    return FILE_ROLE_CTAS.get(file_role, [])
