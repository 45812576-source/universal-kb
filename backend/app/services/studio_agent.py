"""Studio Agent — 专为 Skill Studio 设计的 orchestrator (V2)。

V2 核心变化：
- StudioSessionState 作为单一事实来源，不再依赖原始聊天历史主导回复
- Fact Reconciliation 层：每轮调用前对账用户输入，分类为 new_fact/correction/rejection 等
- 场景化编排：6 种场景各有独立推进模板，首轮追问内容因场景而异
- 防重复机制：问题去重 + 回复去重 + 强制退避
- 文件工作流修正：默认 not_needed，forbidden 状态 5 轮内禁止追问
- Draft Readiness Score：满足 3 项即直接出草稿
- 上下文污染治理：错误方向降权 + 周期性 rollup

使用方：conversations.py 的 skill_studio 快速路径。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway
from app.services.studio_latency_policy import (
    choose_execution_strategy,
    estimate_complexity_level,
)
from app.services.studio_rollout import (
    apply_rollout_to_execution_strategy,
    lane_statuses_for_rollout,
    resolve_rollout_decision,
)

logger = logging.getLogger(__name__)


def _next_action_for_session_mode(session_mode: str) -> str:
    if session_mode == "create_new_skill":
        return "collect_requirements"
    if session_mode == "audit_imported_skill":
        return "run_audit"
    if session_mode == "optimize_existing_skill":
        return "start_editing"
    return "continue_chat"


# ══════════════════════════════════════════════════════════════════════════════
# 1. StudioSessionState — 单一事实来源
# ══════════════════════════════════════════════════════════════════════════════

SCENARIO_TYPES = (
    "unknown", "workflow_approval", "knowledge_qa", "data_analysis",
    "tool_executor", "writing_generation", "classification_extraction",
    "multi_step_agent",
)

MODE_TYPES = (
    "discover", "refine", "draft", "revise", "file_grounding", "test_ready",
)

SESSION_MODES = ("create_new_skill", "optimize_existing_skill", "audit_imported_skill")

FILE_NEED_TYPES = (
    "forbidden", "not_needed", "optional", "required", "user_requested",
)


@dataclass
class StudioSessionState:
    """每个 Skill Studio 会话的结构化状态。"""
    # ── 核心 ──
    goal_summary: str = ""
    scenario_type: str = "unknown"
    current_mode: str = "discover"
    session_mode: str = "create_new_skill"  # create_new_skill | optimize_existing_skill | audit_imported_skill
    architect_phase: str = ""  # phase_1_why | phase_2_what | phase_3_how | ooda_iteration | ready_for_draft
    ooda_round: int = 0
    phase_confirmed: dict = field(default_factory=dict)  # {phase_1_why: True, ...}

    # ── 事实 ──
    confirmed_facts: list[str] = field(default_factory=list)
    active_constraints: list[str] = field(default_factory=list)
    pending_unknowns: list[str] = field(default_factory=list)
    rejected_assumptions: list[str] = field(default_factory=list)
    user_corrections: list[str] = field(default_factory=list)

    # ── 文件 ──
    file_need_status: str = "not_needed"
    file_forbidden_countdown: int = 0  # >0 时禁止主动问文件
    selected_file_context: str = ""

    # ── 推进 ──
    draft_readiness_score: int = 0  # 满 3 即可出草稿
    has_outputted_summary: bool = False
    has_outputted_draft: bool = False
    rounds_since_draft: int = 0

    # ── 去重 ──
    last_assistant_question_fingerprint: str = ""
    asked_question_categories: list[str] = field(default_factory=list)
    last_assistant_direction: str = ""

    # ── 上下文 ──
    context_rollup: str = ""
    total_user_rounds: int = 0
    has_existing_prompt: bool = False

    # ── 本轮 reconciliation 结果 ──
    current_reconciled_facts: list[dict] = field(default_factory=list)
    current_scenario_shift: str | None = None
    current_repeat_blocked: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# 1b. 任务路由 + 辅助 Skill 注入 + 审计 / 治理 prompt
# ══════════════════════════════════════════════════════════════════════════════

# ── Skill Architect 三阶段辅助 prompt ──
# 源自 skill-architect-master.md 的管理咨询框架
# Phase 1: 问题定义（Why）→ Phase 2: 要素拆解（What）→ Phase 3: 验证收敛（How）

ASSIST_SKILL_PROMPTS: dict[str, str] = {
    # ── Phase 1: 问题定义 ──
    "phase1_problem_definition": (
        "## Skill Architect · Phase 1：问题定义（Why）\n"
        "你正在帮助用户定义 Skill 要解决的根本问题。按以下框架引导，**一次只问一个问题**：\n\n"
        "### 1.1 丰田 5 Whys\n"
        "连续追问「为什么需要这个 Skill」，至少深入 3-5 层，每层确认用户的回答后再追问下一层。\n"
        "根因往往在第 3-5 层才浮现。不要自己脑补，让用户说。\n\n"
        "### 1.2 第一性原理\n"
        "找到根因后：「如果这个 Skill 不存在，从零开始你会怎么解决？」\n"
        "区分真约束（物理/逻辑/资源）和惯性假设（大家都这么做）。\n\n"
        "### 1.3 JTBD（Jobs to Be Done）\n"
        "定义真实使用场景——场景、焦虑、期望结果、现有替代方案。\n\n"
        "### 1.4 Cynefin 复杂度判断\n"
        "判断问题属于 Simple（刚性流程）/ Complicated（框架+判断）/ Complex（探索迭代）/ Chaotic（快速原型）。\n\n"
        "**Phase 1 完成标准：** 根因清晰 + 真实使用场景明确 + 复杂度分类确定。\n"
        "完成后输出 `studio_phase_progress` 事件标记 Phase 1 完成。"
    ),

    # ── Phase 2: 要素拆解 ──
    "phase2_element_decomposition": (
        "## Skill Architect · Phase 2：要素拆解（What）\n"
        "从根因出发，穷举所有影响结论质量的输入维度。\n\n"
        "### 2.1 MECE 原则\n"
        "对影响结果的所有 input 维度做穷尽且不重叠的分组。\n"
        "检验：「去掉任何一个维度，结论会有盲区吗？」「有没有两个维度在说同一件事？」\n\n"
        "### 2.2 Issue Tree\n"
        "将核心问题分解为子问题树，每层 MECE，直到叶节点可验证、可量化。\n"
        "用缩进或表格呈现树形结构。\n\n"
        "### 2.3 Value Chain\n"
        "拆解 [Input 采集] → [信息结构化] → [分析推理] → [结论输出]，\n"
        "对每个环节问：对结果质量影响多大？是瓶颈吗？\n\n"
        "### 2.4 Scenario Planning\n"
        "最佳场景（信息充分）、最差场景（信息稀缺）、边缘场景（非典型用法）。\n"
        "每个场景下走一遍 Issue Tree，暴露隐藏维度。\n\n"
        "**Phase 2 完成标准：** MECE 维度清单 + Issue Tree + 场景验证通过。\n"
        "完成后输出 `studio_phase_progress` 事件标记 Phase 2 完成。"
    ),

    # ── Phase 3: 验证收敛 ──
    "phase3_validation": (
        "## Skill Architect · Phase 3：验证收敛（How）\n"
        "从全面维度中筛选对结论确定性影响最大的关键要素。\n\n"
        "### 3.1 金字塔原理（Minto）\n"
        "反向验证：要得出高确定性结论，需要哪些论据？每个论据需要哪些证据？论据之间是否 MECE？\n\n"
        "### 3.2 Pre-Mortem（Gary Klein）\n"
        "**假设这个 Skill 已上线并失败了。** 它为什么失败？用户抱怨什么？哪些输入遗漏导致偏差？\n"
        "至少列 3 个失败原因，越不舒服的越要写。\n\n"
        "### 3.3 Red Team / Devil's Advocate\n"
        "- 反例测试：有没有案例，某维度值很高但结论应该相反？\n"
        "- 矛盾检测：两个维度会互相矛盾吗？如何处理冲突？\n"
        "- 区分度测试：某维度在所有场景下值都差不多？→ 去掉它。\n\n"
        "### 3.4 Sensitivity Analysis\n"
        "对每个维度：「如果这个维度变化 50%，结论会改变吗？」\n"
        "高敏感 → P0 关键要素 / 中敏感 → P1 重要参考 / 低敏感 → P2 可降级\n\n"
        "### 3.5 归零思维\n"
        "对每个保留维度：「从零开始，我还会保留它吗？」剔除惯性遗留。\n\n"
        "### 3.6 OODA Loop\n"
        "至少 2 轮 OODA（Observe→Orient→Decide→Act），直到两轮变化趋于收敛。\n\n"
        "**Phase 3 完成标准：** P0/P1/P2 优先级排序的关键要素清单 + 失败预防清单。"
    ),

    # ── 全流程审计（已有 Skill 用）──
    "skill_audit": (
        "## Skill Architect · 质量审计模式\n"
        "你正在审计一个已有 Skill。使用 Phase 3 的验证框架反向评估：\n\n"
        "**审计维度（每项 0-100 分）：**\n"
        "1. **根因清晰度** — Skill 解决的根本问题是否一句话能说清？还是在解决表面需求？（5 Whys 视角）\n"
        "2. **要素完备性** — 输入维度是否 MECE？有无遗漏的关键决策要素？（Issue Tree 视角）\n"
        "3. **场景鲁棒性** — 最差场景和边缘场景下 Skill 还能工作吗？（Scenario Planning 视角）\n"
        "4. **结论确定性** — 论据链是否完整？证据是否充分？（金字塔原理视角）\n"
        "5. **失败预防** — 用 Pre-Mortem 能想到几个失败场景？当前 Prompt 有防范吗？（Pre-Mortem 视角）\n"
        "6. **维度精准度** — 有没有低区分度的冗余维度？有没有惯性遗留？（Sensitivity + 归零视角）\n\n"
        "**注意：** 用户已有完整清晰 spec 时不要跳过——直接用 Phase 3 质疑：「清晰」不等于「正确」。"
    ),
}

# ── 注入规则：哪个 session_mode 加载哪些辅助策略 ──
ASSIST_SKILL_RULES: dict[str, list[str]] = {
    "create_new_skill": ["phase1_problem_definition", "phase2_element_decomposition", "phase3_validation"],
    "optimize_existing_skill": ["skill_audit", "phase3_validation"],
    "audit_imported_skill": ["skill_audit", "phase3_validation"],
}

# ── 审计 prompt 附加段（optimize/audit 首轮注入）──
_AUDIT_SYSTEM_ADDON = """
## Skill 审计输出规则（本轮必须遵守）
你正在审计一个已有 Skill。使用 Skill Architect 的六维审计框架。在本轮回复中，你**必须**在正常文本回复之后附加一个结构化审计结论块：

```studio_audit
{"quality_score": <0-100整数>, "severity": "<low|medium|high|critical>", "issues": [{"dimension": "<审计维度>", "score": <0-100>, "detail": "<具体问题描述>", "framework": "<所用分析框架>"}], "recommended_path": "<optimize|restructure>", "phase_entry": "<phase1|phase2|phase3>", "assist_skills_to_enable": []}
```

审计维度必须覆盖以下六项（对应 Skill Architect 框架）：
1. **根因清晰度** (framework: "5_whys") — Skill 解决的根本问题是否清晰？
2. **要素完备性** (framework: "mece_issue_tree") — 输入维度是否 MECE？有无遗漏？
3. **场景鲁棒性** (framework: "scenario_planning") — 最差/边缘场景下能否工作？
4. **结论确定性** (framework: "pyramid_principle") — 论据链是否完整？
5. **失败预防** (framework: "pre_mortem") — 有哪些可预见的失败路径？
6. **维度精准度** (framework: "sensitivity_analysis") — 有无冗余/低区分度维度？

评判规则：
- quality_score < 40 或 severity == "critical" → recommended_path = "restructure"，phase_entry = "phase1"（需从根因重新定义）
- 40 <= quality_score < 70 → recommended_path = "optimize"，phase_entry = "phase2"（要素需补充）
- quality_score >= 70 → recommended_path = "optimize"，phase_entry = "phase3"（进入验证收敛）
"""

# ── 治理动作输出规则段 ──
_GOVERNANCE_SYSTEM_ADDON = """
## 治理动作输出规则
当你发现 Skill 需要具体修改时，除了文字说明，还应附加治理动作卡片。每张卡片对应一个具体的修改建议：

```studio_governance_action
{"card_id": "<唯一ID如gov_001>", "title": "<简短标题>", "summary": "<问题描述>", "target": "<system_prompt|source_file|tool_binding>", "reason": "<为什么要改>", "risk_level": "<low|medium|high>", "framework": "<对应的分析框架，如 5_whys / mece_issue_tree / pre_mortem / sensitivity_analysis 等>", "phase": "<phase1|phase2|phase3>", "staged_edit": {"ops": [{"type": "append", "content": "<要追加的内容>"}]}}
```

原则：
- 一轮最多输出 2 个治理动作卡片
- 每个卡片必须有可执行的 staged_edit
- staged_edit.ops 的格式与 studio_diff 的 ops 一致
- framework 字段标注该建议基于哪个分析框架（5_whys / first_principles / jtbd / mece_issue_tree / scenario_planning / pyramid_principle / pre_mortem / red_team / sensitivity_analysis / zero_based）
- phase 字段标注该建议属于哪个 Skill Architect 阶段
- 治理动作卡片可以与 studio_audit 同时出现
"""


# ── Architect 工作流输出规则（architect_mode 时注入）──
_ARCHITECT_OUTPUT_RULES = """当前处于 **Skill Architect 工作流**，阶段：**{current_phase}**，OODA 轮次：{ooda_round}。
已确认阶段：{confirmed_phases}。

在进入 `ready_for_draft` 之前：
- 禁止输出 `studio_draft` / `studio_diff`
- 禁止口头声称“草稿已生成”或“下方有治理卡片”，除非你本轮真的输出了对应结构化块
- 即使用户要求“直接出草稿”，也只能继续推进当前阶段，或在满足条件时先输出 `architect_ready_for_draft`

你必须按当前阶段引导用户，使用以下结构化事件输出：

### architect_question — 向用户提问
在需要用户回答时输出，**一次只问一个问题**：
```architect_question
{{"phase": "{current_phase}", "framework": "<当前使用的框架>", "question": "<问题>", "options": ["<选项1>", "<选项2>"], "why": "<为什么问这个>"}}
```
- options 可选，有助于降低用户认知负担
- framework 标注当前在用哪个分析框架（5_whys / first_principles / jtbd / cynefin / mece / issue_tree / scenario_planning / pyramid / pre_mortem / red_team / sensitivity / zero_based / ooda）

### architect_phase_summary — 阶段完成确认
当你判断当前阶段信息已充分时，输出阶段总结供用户确认：
```architect_phase_summary
{{"phase": "{current_phase}", "summary": "<该阶段核心结论>", "deliverables": ["<产出1>", "<产出2>"], "confidence": <0.0-1.0>, "ready_for_next": true}}
```
- 用户确认后（回复"确认"/"继续"等），系统推进到下一阶段
- confidence < 0.7 时应建议用户补充信息而非推进

### architect_structure — Issue Tree / 维度结构
当拆解维度或构建 Issue Tree 时输出：
```architect_structure
{{"type": "<issue_tree|dimension_map|value_chain>", "root": "<根节点>", "nodes": [{{"id": "n1", "label": "<节点>", "parent": null, "children": ["n2", "n3"]}}, ...]}}
```

### architect_priority_matrix — 维度优先级排序
Phase 3 的 Sensitivity Analysis 完成后输出：
```architect_priority_matrix
{{"dimensions": [{{"name": "<维度>", "priority": "P0", "sensitivity": "high", "reason": "<为什么是P0>"}}, ...]}}
```

### architect_ooda_decision — OODA Loop 决策
每轮 OODA 结束时输出：
```architect_ooda_decision
{{"ooda_round": {ooda_round}, "observation": "<本轮发现>", "orientation": "<如何改变理解>", "decision": "<continue_to_draft|回调到phase_X>", "delta_from_last": "<与上轮的差异>"}}
```
- decision = "continue_to_draft" 表示收敛，准备出草稿
- decision 包含 "phase_" 表示需要回调到某阶段补充

### architect_ready_for_draft — 全流程收敛
当 Phase 3 完成 + OODA 至少 2 轮且收敛时输出：
```architect_ready_for_draft
{{"key_elements": [{{"name": "<要素>", "priority": "P0", "source_phase": "phase_X"}}], "failure_prevention": ["<失败预防项1>"], "draft_approach": "<生成草稿的策略说明>"}}
```
输出此块后，下一步直接生成 studio_draft。
"""

_NO_ARCHITECT_RULES = "当前未启用 Skill Architect 工作流。"


def _resolve_session_mode(
    selected_skill_id: int | None,
    editor_prompt: str | None,
    user_message: str,
    skill_metadata: dict | None,
    total_user_rounds: int,
) -> tuple[str, str, list[str]]:
    """返回 (session_mode, route_reason, active_assist_skills)。

    路由信号：
    - editor_prompt 为空 + selected_skill_id 为空 → create_new_skill
    - editor_prompt 非空 + skill source_type == "imported" → audit_imported_skill
    - editor_prompt 非空 + 非导入 → optimize_existing_skill
    """
    has_editor = bool(editor_prompt and editor_prompt.strip())
    is_imported = bool(skill_metadata and skill_metadata.get("source_type") == "imported")

    if not has_editor and not selected_skill_id:
        mode = "create_new_skill"
        reason = "编辑器为空且未选中 Skill，进入新建模式"
    elif is_imported:
        mode = "audit_imported_skill"
        reason = "导入的 Skill，进入审计模式"
    elif has_editor or selected_skill_id:
        mode = "optimize_existing_skill"
        reason = "已有 Skill 内容，进入优化模式"
    else:
        mode = "create_new_skill"
        reason = "默认进入新建模式"

    active_assists = list(ASSIST_SKILL_RULES.get(mode, []))
    return mode, reason, active_assists


# ══════════════════════════════════════════════════════════════════════════════
# 2. Fact Reconciliation — 用户输入分类
# ══════════════════════════════════════════════════════════════════════════════

# ── 意图匹配模式 ──

_P_CORRECTION = re.compile(
    r"不是这个意思|你理解错了|你搞错了|不是这样|理解偏了|方向不对|方向错了|搞反了|弄错了",
)
_P_REJECTION = re.compile(
    r"不要|不需要|别问|别再问|我已经说了|已经说过|说过了|不用再问|别提了|不用问",
)
_P_SCENARIO_SHIFT = re.compile(
    r"改成|转为|重点放在|换成|聚焦到|改为|其实是|本质上是|不是.*而是",
)
_P_FILE_DENIAL = re.compile(
    r"不要文件|不需要上传|先别管.{0,4}文件|不用文件|不需要文件|先不[要用]文件|不要.*md|别问.*文件",
)
_P_FILE_REQUEST = re.compile(
    r"读.{0,6}文件|按.{0,10}md|看.{0,6}文件|导入|读取.*\.md|按.*文件|参考.*文件|基于.*文件",
)
_P_DIRECT_ACTION = re.compile(
    r"直接给|直接做|直接写|先写出来|给我草稿|出一版|直接出|直接生成|生成草稿|先出方案|先出一版|给我方案",
)
_P_CONFIRM = re.compile(
    r"^(确认|好的|可以|继续|没问题|OK|ok|对的|行|同意|就这样|嗯|是的|对)[\s。！!,.，]*$",
)
_P_CONSTRAINT = re.compile(
    r"不能|不允许|禁止|必须|一定要|只能|不可以|不得|务必|限制|约束",
)

# ── 场景识别关键词 ──
_SCENARIO_KEYWORDS: dict[str, list[str]] = {
    "workflow_approval": ["审批", "审核", "流程", "校验", "权限", "证据", "判定", "规则判断", "合规"],
    "knowledge_qa": ["知识库", "问答", "FAQ", "制度文档", "文档问答", "知识检索"],
    "data_analysis": ["数据表", "数据分析", "指标", "筛选", "聚合", "报表", "数据看板", "统计"],
    "tool_executor": ["工具", "API", "脚本", "自动化", "调用接口", "执行", "函数"],
    "writing_generation": ["写作", "文案", "内容生成", "文章", "报告", "文稿", "宣传", "营销"],
    "classification_extraction": ["提取", "分类", "结构化", "标注", "实体", "抽取", "解析"],
    "multi_step_agent": ["多步", "agent", "编排", "工作流", "pipeline", "链式"],
}

# ── 问题类别指纹 ──
_QUESTION_CATEGORIES: dict[str, re.Pattern] = {
    "file_need": re.compile(r"文件|上传|md|附件|素材"),
    "target_user": re.compile(r"目标用户|谁.*用|给谁|受众|面向"),
    "input_source": re.compile(r"输入.*来|数据.*来|来源|输入.*是"),
    "output_format": re.compile(r"输出.*格式|输出.*形式|返回.*什么|产出|交付物"),
    "constraint": re.compile(r"约束|限制|不能|禁止|边界"),
    "tool_need": re.compile(r"工具|接口|API|调用"),
}


def _classify_user_input(message: str) -> list[dict]:
    """将用户输入分类为结构化事实类型。返回 [{type, content, category?}]。"""
    results: list[dict] = []
    text = message.strip()

    if _P_CORRECTION.search(text):
        results.append({"type": "correction", "content": text[:200]})

    if _P_REJECTION.search(text):
        results.append({"type": "rejection", "content": text[:200]})

    if _P_SCENARIO_SHIFT.search(text):
        # 尝试识别目标场景
        target_scenario = _detect_scenario_from_text(text)
        results.append({"type": "scenario_shift", "content": text[:200], "scenario": target_scenario})

    if _P_FILE_DENIAL.search(text):
        results.append({"type": "file_rejection", "content": text[:100]})

    if _P_FILE_REQUEST.search(text):
        results.append({"type": "file_reference", "content": text[:200]})

    if _P_DIRECT_ACTION.search(text):
        results.append({"type": "execution_request", "content": text[:100]})

    if _P_CONFIRM.match(text):
        results.append({"type": "confirm", "content": text[:50]})

    if _P_CONSTRAINT.search(text):
        results.append({"type": "constraint", "content": text[:200]})

    # M13: 正则预筛结果加 confidence，短文本匹配降权
    # 如果文本较长(>50字)且只匹配了一个模式，可能是正则误匹配，标记低置信度
    _specific_types = {"correction", "rejection", "scenario_shift"}
    for r in results:
        if r["type"] in _specific_types and len(text) > 50 and len(results) == 1:
            r["confidence"] = "low"
        else:
            r["confidence"] = "high"

    # 如果没命中任何特殊模式，视为 new_fact
    non_noise_types = {"correction", "rejection", "scenario_shift", "file_rejection",
                       "file_reference", "execution_request", "confirm", "constraint"}
    if not any(r["type"] in non_noise_types for r in results):
        if len(text) > 5:
            results.append({"type": "new_fact", "content": text[:300], "confidence": "high"})
        else:
            results.append({"type": "ambiguous_noise", "content": text[:100], "confidence": "low"})

    return results


def _detect_scenario_from_text(text: str) -> str:
    """从文本中识别场景类型。"""
    scores: dict[str, int] = {}
    for scenario, keywords in _SCENARIO_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[scenario] = score
    if scores:
        return max(scores, key=scores.get)  # type: ignore[arg-type]
    return "unknown"


def _fingerprint_assistant_questions(text: str) -> list[str]:
    """从 assistant 回复中提取问题类别指纹。"""
    categories = []
    for cat, pattern in _QUESTION_CATEGORIES.items():
        if pattern.search(text):
            categories.append(cat)
    return categories


# ══════════════════════════════════════════════════════════════════════════════
# 3. 状态提取 + 对账
# ══════════════════════════════════════════════════════════════════════════════

def _extract_session_state(
    history_messages: list[dict],
    user_message: str,
    has_existing_prompt: bool = False,
) -> StudioSessionState:
    """从对话历史 + 当前用户消息中提取并对账 StudioSessionState。"""
    state = StudioSessionState(has_existing_prompt=has_existing_prompt)

    user_msgs = [m["content"] for m in history_messages if m.get("role") == "user"]
    asst_msgs = [m["content"] for m in history_messages if m.get("role") == "assistant"]

    state.total_user_rounds = len(user_msgs) + 1

    # M12: 限制扫描窗口，避免 O(n²) 全量重建；只扫描最近 20 轮
    _SCAN_WINDOW = 20
    user_msgs_scan = user_msgs[-_SCAN_WINDOW:]
    asst_msgs_scan = asst_msgs[-_SCAN_WINDOW:]

    # ── 第一遍：扫描历史建立基线状态 ──

    # 从第一条用户消息提取初始目标和场景
    all_user_text = " ".join(user_msgs + [user_message])
    if user_msgs:
        state.goal_summary = user_msgs[0].strip()[:150]
    else:
        state.goal_summary = user_message.strip()[:150]

    # 场景识别（优先从全部用户文本中识别）
    state.scenario_type = _detect_scenario_from_text(all_user_text)

    # 扫描历史 assistant 消息（M12: 仅扫描最近窗口）
    for a in asst_msgs_scan:
        if "studio_summary" in a:
            state.has_outputted_summary = True
        if "studio_draft" in a or "studio_diff" in a:
            state.has_outputted_draft = True
            state.rounds_since_draft = 0

        # 提取 assistant 已问过的问题类别
        q_cats = _fingerprint_assistant_questions(a)
        for qc in q_cats:
            if qc not in state.asked_question_categories:
                state.asked_question_categories.append(qc)

    # 计算距上次 draft 轮数（M12: 仅扫描最近窗口）
    if state.has_outputted_draft:
        count = 0
        for a in reversed(asst_msgs_scan):
            if "studio_draft" in a or "studio_diff" in a:
                break
            count += 1
        state.rounds_since_draft = count

    # 扫描用户消息提取事实（M12: 仅扫描最近窗口）
    for um in user_msgs_scan:
        facts = _classify_user_input(um)
        for f in facts:
            if f["type"] == "new_fact":
                if f["content"] not in state.confirmed_facts:
                    state.confirmed_facts.append(f["content"][:200])
            elif f["type"] == "constraint":
                if f["content"] not in state.active_constraints:
                    state.active_constraints.append(f["content"][:200])
            elif f["type"] in ("correction", "rejection"):
                if f["content"] not in state.rejected_assumptions:
                    state.rejected_assumptions.append(f["content"][:200])
            elif f["type"] == "file_rejection":
                state.file_need_status = "forbidden"
                state.file_forbidden_countdown = 5
            elif f["type"] == "file_reference":
                if state.file_need_status != "forbidden":
                    state.file_need_status = "user_requested"
            elif f["type"] == "scenario_shift":
                state.scenario_type = f.get("scenario", state.scenario_type)

    # ── 第二遍：对账当前用户消息（最高优先级）──

    current_facts = _classify_user_input(user_message)
    state.current_reconciled_facts = current_facts

    for f in current_facts:
        ftype = f["type"]

        if ftype == "correction":
            state.current_mode = "refine"
            state.user_corrections.append(f["content"][:200])

        elif ftype == "rejection":
            if f["content"] not in state.rejected_assumptions:
                state.rejected_assumptions.append(f["content"][:200])

        elif ftype == "scenario_shift":
            new_scenario = f.get("scenario", "unknown")
            if new_scenario != "unknown" and new_scenario != state.scenario_type:
                state.current_scenario_shift = new_scenario
                state.scenario_type = new_scenario
            state.current_mode = "refine"

        elif ftype == "file_rejection":
            state.file_need_status = "forbidden"
            state.file_forbidden_countdown = 5
            if "file_rejection" not in [r.get("type") for r in state.current_reconciled_facts]:
                pass  # already added above

        elif ftype == "file_reference":
            if state.file_need_status == "forbidden":
                # 用户重新主动提及文件，解除 forbidden
                state.file_need_status = "user_requested"
                state.file_forbidden_countdown = 0
            else:
                state.file_need_status = "user_requested"

        elif ftype == "execution_request":
            state.current_mode = "draft"

        elif ftype == "confirm":
            if state.has_outputted_summary and not state.has_outputted_draft:
                state.current_mode = "draft"

        elif ftype == "new_fact":
            if f["content"] not in state.confirmed_facts:
                state.confirmed_facts.append(f["content"][:200])

        elif ftype == "constraint":
            if f["content"] not in state.active_constraints:
                state.active_constraints.append(f["content"][:200])

    # ── 递减 file_forbidden_countdown ──
    if state.file_forbidden_countdown > 0 and state.file_need_status == "forbidden":
        state.file_forbidden_countdown -= 1

    # ── Draft Readiness Score 计算 ──
    score = 0
    if state.goal_summary:
        score += 1
    if state.scenario_type != "unknown":
        score += 1
    # 检查是否有输出格式相关事实
    output_keywords = ["输出", "格式", "返回", "产出", "结果", "结论"]
    if any(any(kw in f for kw in output_keywords) for f in state.confirmed_facts):
        score += 1
    if state.active_constraints:
        score += 1
    if any(f["type"] == "execution_request" for f in current_facts):
        score += 2  # 用户直接要求，强制足够
    state.draft_readiness_score = score

    # ── 模式最终判定（冲突解决）──
    # 优先级：用户明确纠偏 > 用户要求执行 > 自动判定
    explicit_mode_set = any(
        f["type"] in ("correction", "scenario_shift", "execution_request", "confirm")
        for f in current_facts
    )

    if not explicit_mode_set:
        if state.has_outputted_draft:
            state.current_mode = "revise"
        elif state.draft_readiness_score >= 3:
            state.current_mode = "draft"
        elif state.total_user_rounds >= 3 and not state.has_outputted_draft:
            state.current_mode = "draft"  # 强制推进
        elif state.total_user_rounds == 1:
            state.current_mode = "discover"
        # else: keep whatever was set

    # ── 检查重复问题 ──
    if asst_msgs and state.current_mode == "discover":
        last_asst = asst_msgs[-1] if asst_msgs else ""
        last_q_cats = _fingerprint_assistant_questions(last_asst)
        # 检查是否所有问题类别都已问过
        if last_q_cats and all(qc in state.asked_question_categories for qc in last_q_cats):
            state.current_repeat_blocked = True
            state.current_mode = "draft"  # 强制退避

    # ── 生成 context_rollup ──
    if state.rejected_assumptions or state.user_corrections:
        rollup_parts = []
        if state.user_corrections:
            rollup_parts.append(f"用户纠正：{'；'.join(state.user_corrections[-3:])}")
        if state.rejected_assumptions:
            rollup_parts.append(f"已否定：{'；'.join(state.rejected_assumptions[-3:])}")
        rollup_parts.append(f"当前方向：{state.scenario_type} / {state.goal_summary[:60]}")
        if state.confirmed_facts:
            rollup_parts.append(f"已确认：{'；'.join(state.confirmed_facts[-5:])}")
        state.context_rollup = "\n".join(rollup_parts)

    return state


# ══════════════════════════════════════════════════════════════════════════════
# 4. 场景化推进模板
# ══════════════════════════════════════════════════════════════════════════════

_SCENARIO_PROMPTS: dict[str, str] = {
    "workflow_approval": """当前场景：**审批/流程/规则判断**
首轮优先确认：这个 skill 判什么？判定依据来自哪里？输出是结论、原因还是证据链？失败/无法判定时怎么处理？
禁止优先追问：文件结构、示例文件、附属 md。
推进策略：先形成判定框架 → 再补输入槽位、证据、规则、边界。""",

    "knowledge_qa": """当前场景：**知识库/文档问答**
首轮优先确认：回答范围、引用方式、幻觉控制要求、知识源类型。
允许早问文件，但必须具体："如果你已有制度文档或 FAQ，我可以直接基于它整理；没有我先搭框架。"
推进策略：先定回答范围和质量标准 → 再补知识源和边界。""",

    "data_analysis": """当前场景：**数据表分析/指标/报表**
首轮优先确认：看什么数据？输出粒度？是否允许明细？是否需要权限约束？
禁止默认追问"上传什么文件"，除非用户明确依赖数据样本。
推进策略：先定分析目标和输出形式 → 再补数据源和过滤逻辑。""",

    "tool_executor": """当前场景：**工具调用/API/自动化**
首轮优先确认：工具是必须还是可选？需要读什么输入？输出是执行结果还是计划？失败 fallback？
推进策略：先定工具边界和调用契约 → 再补输入输出格式。""",

    "writing_generation": """当前场景：**写作/文案/内容生成**
首轮优先确认：受众、风格、输出格式、禁忌/边界。
通常不应优先问文件。
推进策略：先定风格和输出标准 → 再补受众画像和内容约束。""",

    "classification_extraction": """当前场景：**提取/分类/结构化输出**
首轮优先确认：输入类型、分类或提取 schema、输出 JSON/表格/文本、歧义处理方式。
推进策略：先定 schema → 再补边界 case 和输出格式。""",

    "multi_step_agent": """当前场景：**多步 Agent/工作流编排**
首轮优先确认：有几步？每步做什么？步骤间如何传递上下文？失败时回退还是中断？
推进策略：先定步骤链 → 再补每步的输入输出契约。""",

    "unknown": """场景尚未确定。请从用户描述中识别最可能的业务场景，然后按对应场景的推进策略进行。
可选场景：审批管理、知识库问答、数据分析、工具执行、写作生成、分类提取、多步 Agent。""",
}


# ══════════════════════════════════════════════════════════════════════════════
# 5. 状态渲染
# ══════════════════════════════════════════════════════════════════════════════

def _render_session_state(state: StudioSessionState) -> str:
    """将 StudioSessionState 渲染为 system prompt 注入文本。"""
    parts: list[str] = []

    # ── 场景 ──
    scenario_prompt = _SCENARIO_PROMPTS.get(state.scenario_type, _SCENARIO_PROMPTS["unknown"])
    parts.append(scenario_prompt)

    # ── 模式指令 ──
    mode_map = {
        "discover": (
            "你正在了解用户需求。已知目标：{goal}。\n"
            "如果用户首条消息信息量充足（目标+场景+输出已知），直接输出 studio_summary。\n"
            "否则最多问 1 个关键问题（开放式、不给选项、不问文件）。"
        ),
        "refine": (
            "用户正在纠偏或切换方向。你必须：\n"
            "1. 先明确承认修正：「明白，已按你的修正调整」\n"
            "2. 复述新的理解，指出与之前的区别\n"
            "3. 直接推进（出 studio_summary 或 studio_draft/studio_diff），不退回追问模式\n"
            "4. 不要重复已被否定的假设"
        ),
        "draft": (
            "信息充足（readiness={score}/5），请直接输出结构化产物：\n"
            "- 编辑器已有内容 → 优先 studio_diff\n"
            "- 编辑器为空 → 输出完整 studio_draft\n"
            "- 不要再追问。如果确实缺关键信息，在草稿中标注假设。"
        ),
        "revise": (
            "用户在迭代已有草稿。\n"
            "- 细节修改 → studio_diff\n"
            "- 方向性大改 → studio_draft\n"
            "- 每次说清改了什么、为什么"
        ),
        "file_grounding": (
            "用户要求基于文件工作。请阅读附属文件正文，基于文件内容推进。"
        ),
        "test_ready": (
            "Skill 已就绪，引导用户测试。"
        ),
    }
    mode_text = mode_map.get(state.current_mode, "")
    if state.current_mode == "discover":
        mode_text = mode_text.format(goal=state.goal_summary[:80] or "待确认")
    elif state.current_mode == "draft":
        mode_text = mode_text.format(score=state.draft_readiness_score)
    parts.append(f"\n当前模式：**{state.current_mode}**\n{mode_text}")

    if state.session_mode == "create_new_skill" and state.architect_phase and state.architect_phase != "ready_for_draft":
        parts.append(
            f"\n**!! Architect 阶段未完成 !!** 当前仍处于 {state.architect_phase}。"
            "在进入 ready_for_draft 前，禁止输出 studio_draft / studio_diff，"
            "也禁止声称草稿或治理卡片已经生成。"
            "如果用户催促直接出草稿，你必须先说明当前阶段尚未完成，然后继续用 architect_question / architect_phase_summary / architect_structure / architect_priority_matrix / architect_ooda_decision 推进。"
        )

    # ── 纠偏警告（最高优先级）──
    if state.user_corrections:
        corrections = "；".join(state.user_corrections[-3:])
        parts.append(
            f"\n**!! 用户纠偏（必须优先响应）!!**\n"
            f"用户说：「{corrections}」\n"
            f"你必须先承认并吸收这个修正，更新当前理解，不允许忽略或继续沿旧方向。"
        )

    # ── 场景切换 ──
    if state.current_scenario_shift:
        parts.append(
            f"\n**!! 场景切换 !!** 用户要求切换到 {state.current_scenario_shift}。"
            f"回复开头必须明确：「已切换到 {state.current_scenario_shift} 场景」，并按新场景模板推进。"
        )

    # ── 已确认事实 ──
    if state.confirmed_facts:
        facts = "\n".join(f"  - {f[:100]}" for f in state.confirmed_facts[-8:])
        parts.append(f"\n已确认事实（不要重复确认）：\n{facts}")

    # ── 约束 ──
    if state.active_constraints:
        constraints = "\n".join(f"  - {c[:100]}" for c in state.active_constraints[-5:])
        parts.append(f"\n用户约束：\n{constraints}")

    # ── 被否定假设 ──
    if state.rejected_assumptions:
        rejections = "\n".join(f"  - {r[:100]}" for r in state.rejected_assumptions[-5:])
        parts.append(f"\n被否定的内容（绝对不要再提或以此为前提）：\n{rejections}")

    # ── 文件状态 ──
    file_hints = {
        "forbidden": (
            "**用户明确禁止问文件**。在接下来的对话中绝对不要主动提及文件、上传、md、附件。"
            "除非用户自己重新主动提到文件。"
        ),
        "not_needed": (
            "用户未提及文件需求。不要主动追问「需要什么文件」。"
            "只有在你能具体说明「缺少 XX 信息导致无法判断 YY」时才可提及文件，"
            "且必须用这个句式：「如果你有现成的需求文档或示例输入输出，我可以基于它改进；没有我也可以先搭第一版框架。」"
        ),
        "optional": "文件是可选的。不主动追问，但如果用户提到可以配合。",
        "required": "当前任务需要文件。请引导用户提供具体文件。",
        "user_requested": "用户主动要求使用文件。请围绕文件内容展开工作。",
    }
    parts.append(f"\n文件状态：{file_hints.get(state.file_need_status, '')}")

    # ── 防重复 ──
    if state.asked_question_categories:
        cats = "、".join(state.asked_question_categories)
        parts.append(f"\n已问过的问题类别（不要重复）：{cats}")

    if state.current_repeat_blocked:
        parts.append(
            "\n**!! 重复阻断 !!** 你的上一轮追问重复了已问过的内容。"
            "本轮禁止继续追问，必须：总结已知 → 列出假设 → 直接出草稿。"
        )

    # ── 上下文 rollup ──
    if state.context_rollup:
        parts.append(f"\n上下文摘要（优先参考此摘要，不依赖长历史）：\n{state.context_rollup}")

    # ── 进度 ──
    progress = f"已对话 {state.total_user_rounds} 轮"
    if state.has_outputted_summary:
        progress += "，已输出过 summary"
    if state.has_outputted_draft:
        progress += f"，已输出过 draft（距上次 {state.rounds_since_draft} 轮）"
    if state.has_existing_prompt:
        progress += "，编辑器已有 Prompt 内容"
    progress += f"，readiness={state.draft_readiness_score}/5"
    parts.append(f"\n进度：{progress}")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 6. System prompt 构建
# ══════════════════════════════════════════════════════════════════════════════

_STUDIO_SYSTEM = """你是 Skill Studio 的高级创作顾问。你的使命是帮助用户快速构建有深度、架构正确的 AI Skill（系统提示词）。

## 核心行为规则（优先级从高到低，严格遵守，违反任何一条视为严重错误）

1. **用户最新输入优先于你的旧判断**：用户刚说的话是最高权威。如果用户在纠正你，你必须先承认修正、更新理解，不允许继续沿旧方向。

2. **已纠正事实优先于对话模板**：一旦用户否定了某个方向、假设或前提，该内容永久标记为"不可再用"，不允许再以此为基础追问或生成。

3. **场景化推进优先于通用套路**：不同业务场景（审批管理 vs 知识库 vs 数据分析 vs 工具型 vs 写作 vs 分类提取）必须有不同的首轮问题和推进策略。不允许所有场景用同一个模板化引导。

4. **信息足够时直接推进，不允许机械追问**：当 readiness score >= 3 或用户明确要求时，直接出草稿。不允许"为了完整性"继续追问已有答案的问题。

5. **错误上下文要降权**：如果你之前走错了方向并被用户纠正，不要再受旧方向影响。参考上下文摘要而非长历史。

6. **文件不是默认前置条件**：不主动追问文件。只有用户主动提到文件、或你能具体说明缺什么信息导致无法判断什么时，才可提及文件。且必须提供无文件的替代路径。

7. **不重复追问**：同一类别的问题（文件/目标用户/输出格式/约束/工具）只问一次。用户已回答的不再问，用户已拒绝的不再提。

8. **先响应，再引导**：每轮回复优先序 = 回应用户刚才说的话 → 更新当前理解 → 给出推进动作 → 如仍缺信息则问最多 1 个问题。

## 当前会话状态（单一事实来源）
{session_state_context}

## 当前编辑器状态
{editor_context}

## 草稿质量标准
- 有清晰的推理链条，而非简单模板填充
- MECE 覆盖主要输入场景
- 输出格式明确，有质量标准
- 包含边界情况处理
- 草稿**必须使用 `## ` 标题分段**（如：## 角色定义 / ## 核心任务 / ## 处理逻辑 / ## 输出格式 / ## 约束条件）

## 输出规则
- 正常对话回复直接用中文文本输出，不需要代码块包裹。
- 当你完成 1-2 轮追问、需要让用户确认需求理解时，在回复末尾附加：
```studio_summary
{{"title": "需求理解摘要", "items": [{{"label": "目标用户", "value": "..."}}, {{"label": "核心场景", "value": "..."}}, {{"label": "期望输出", "value": "..."}}, {{"label": "关键约束", "value": "..."}}], "next_action": "generate_draft"}}
```
- 当你要给用户提供可采纳的完整草稿时，在回复末尾附加：
```studio_draft
{{"name": "skill名称", "system_prompt": "完整的system prompt内容", "change_note": "一句话说明这版做了什么"}}
```
- 当你要给用户提供针对当前编辑器内容的局部修改时，在回复末尾附加：
```studio_diff
{{"ops": [操作数组], "change_note": "一句话说明改了什么"}}
```
  **ops 操作类型**（可一次提交多个 op）：
  - `{{"type": "replace", "old": "编辑器中精确存在的文本片段", "new": "替换后的文本"}}` — 改措辞、改单行、改整段
  - `{{"type": "insert_after", "anchor": "编辑器中精确存在的定位行", "content": "要插入的新内容"}}` — 在某行/段之后插入
  - `{{"type": "insert_before", "anchor": "编辑器中精确存在的定位行", "content": "要插入的新内容"}}` — 在某行/段之前插入
  - `{{"type": "delete", "old": "编辑器中精确存在的文本片段"}}` — 删除某段
  - `{{"type": "append", "content": "追加到末尾的新内容"}}` — 追加到文件末尾
  **关键规则**：
  - `old` 和 `anchor` 字段的值必须从「当前编辑器状态」中**精确复制**，不能自己编造或凭记忆猜测
  - 如果修改影响了后续编号，必须用额外的 `replace` op 更新这些编号
  - 单行改措辞 → 一个 `replace`；扩充整个章节 → 一个 `replace` 替换该 `## ` 章节块；新增章节 → `insert_before` 或 `insert_after`
- 当你要返回测试结果时，在回复末尾附加：
```studio_test_result
{{"input": "测试输入", "output": "模型输出", "passed": true, "issues": [], "suggestion": "改进建议"}}
```
- 当你分析需求后发现 Skill 需要工具能力，在回复末尾附加：
```studio_tool_suggestion
{{"suggestions": [{{"name": "工具名称", "reason": "为什么需要这个工具", "action": "bind_existing 或 create_new", "tool_id": null}}]}}
```
  - 如果「可用工具列表」中有匹配的工具，action 填 `"bind_existing"` 并填入 `tool_id`
  - 如果没有匹配的，action 填 `"create_new"`，tool_id 为 null
  - studio_tool_suggestion 可以和其他块同时出现
- 当编辑器内容适合拆分时，附加 studio_file_split 块建议拆分：
```studio_file_split
{{"files": [{{"filename": "example-xxx.md", "category": "example", "content": "拆出的完整内容", "reason": "原因"}}], "main_prompt_after_split": "拆分后的主文件完整内容", "change_note": "说明"}}
```
  - 文件命名规范：example 前缀 `example-`，知识库后缀 `-kb`，参考资料前缀 `reference-`，模板前缀 `template-`
  - category 可选值：`example` / `knowledge-base` / `reference` / `template`
- 当你完成 Skill Architect 的某个阶段时，在回复末尾附加：
```studio_phase_progress
{{"completed_phase": "phase1", "phase_label": "问题定义", "deliverables": ["根因定义", "使用场景", "复杂度分类"], "next_phase": "phase2", "next_label": "要素拆解"}}
```
  - completed_phase: phase1 / phase2 / phase3
  - deliverables: 该阶段的产出清单
  - next_phase: 下一阶段（phase3 完成后为 null）
- 除 studio_tool_suggestion、studio_file_split、studio_governance_action、studio_phase_progress 和 architect_* 块外，其他结构化块每次回复最多输出一个。JSON 必须合法。
- 不要解释这些代码块的格式，直接输出。

## Skill Architect 工作流事件（当处于 architect_mode 时使用）
{architect_output_rules}

## Memo 驱动编排规则（当存在 Skill Memo 时生效）
{memo_context}"""

_MEMO_CONTEXT_TEMPLATE = """当前 Skill 存在 Memo 工作流，你必须围绕 Memo 任务状态引导用户：
- 生命周期阶段：{lifecycle_stage}
- 当前状态：{status_summary}
- 当前任务：{current_task_desc}
- 下一任务：{next_task_desc}
- 持久提醒：{notices_desc}
- 最近测试：{latest_test_desc}
- 最近完成：{recent_progress}

**编排行为规则（严格遵守）**：
1. 有持久提醒且未开始整改时，优先询问是否进入第一个未完成任务
2. 有进行中的任务时，明确说"你已完成 AAA，接下来做 BBB 吗"
3. 当任务绑定目标文件后，在回复末尾附加：
```studio_editor_target
{{"mode": "open_or_create", "file_type": "asset", "filename": "目标文件名"}}
```
4. 当检测到子任务完成后，在回复末尾附加：
```studio_context_rollup
{{"task_id": "任务ID", "summary": "xxx任务已经完成"}}
```
5. 若存在 memo，在回复开头附加：
```studio_memo_status
{{"lifecycle_stage": "阶段", "status_summary": "摘要", "has_open_todos": true/false, "can_test": true/false}}
```
6. 若存在当前任务，必须附加：
```studio_task_focus
{{"task_id": "ID", "title": "标题", "description": "描述", "target_files": [...], "acceptance_hint": "保存文件后此步骤会自动完成。"}}
```
7. 当 memo 无待办时，引导测试，附加 studio_memo_status 中 can_test=true
8. 保存前不算完成，不要提前说"已完成"
"""

_MEMO_NO_CONTEXT = "当前 Skill 没有 Memo 工作流，按常规创作流程进行。"

_EDITOR_CONTEXT_TEMPLATE = """- 当前选中的 Skill ID：{skill_id}
- 编辑器是否有未保存修改：{is_dirty}
- 当前编辑器中的 Prompt（前 2000 字，共 {line_count} 行）：
```
{editor_prompt}
```
- 当前附属文件：{existing_files}
- 当前可用工具（已注册，可直接绑定）：
{available_tools}"""

_EDITOR_NO_CONTEXT = "用户尚未选中任何 Skill，编辑器为空。"


def _build_memo_context(memo_data: dict | None) -> str:
    """将 memo 视图数据构建为 prompt 注入文本。"""
    if not memo_data or not memo_data.get("lifecycle_stage"):
        return _MEMO_NO_CONTEXT

    current = memo_data.get("current_task")
    next_t = memo_data.get("next_task")
    notices = memo_data.get("persistent_notices", [])
    latest_test = memo_data.get("latest_test")
    payload = memo_data.get("memo", {})
    progress_log = payload.get("progress_log", [])

    current_desc = f"{current['title']}（目标文件：{', '.join(current.get('target_files', []))}）" if current else "无"
    next_desc = next_t["title"] if next_t else "无"
    notices_desc = "、".join(n["title"] for n in notices) if notices else "无"
    test_desc = f"{latest_test['status']} — {latest_test['summary']}" if latest_test else "无"
    recent = progress_log[-3:] if progress_log else []
    recent_desc = "、".join(r["summary"] for r in recent) if recent else "无"

    base = _MEMO_CONTEXT_TEMPLATE.format(
        lifecycle_stage=memo_data.get("lifecycle_stage", "unknown"),
        status_summary=memo_data.get("status_summary", ""),
        current_task_desc=current_desc,
        next_task_desc=next_desc,
        notices_desc=notices_desc,
        latest_test_desc=test_desc,
        recent_progress=recent_desc,
    )

    # fixing 阶段 + 最近测试失败 → 追加结构化 fix context
    if memo_data.get("lifecycle_stage") == "fixing" and latest_test and latest_test.get("status") == "failed":
        details = latest_test.get("details", {})
        quality_detail = details.get("quality_detail", {})
        avg_score = quality_detail.get("avg_score", "N/A")
        top_deductions = quality_detail.get("top_deductions", [])

        # 尝试获取结构化 fix tasks
        tasks = payload.get("tasks", [])
        fix_tasks = [t for t in tasks if t.get("type", "").startswith("fix_") and t.get("status") in ("todo", "in_progress")]

        if fix_tasks:
            lines = []
            for i, task in enumerate(fix_tasks, 1):
                line = f"{i}. [{task.get('type', '')}] {task.get('title', '')}"
                target = task.get("target_ref", "")
                if target:
                    line += f" (目标: {target})"
                acceptance = task.get("acceptance_rule_text", "")
                if acceptance:
                    line += f"\n   验收标准: {acceptance}"
                lines.append(line)

            # 找到当前任务
            current_id = payload.get("current_task_id")
            current_fix = next((t for t in fix_tasks if t.get("id") == current_id), fix_tasks[0] if fix_tasks else None)

            fix_section = (
                f"\n\n## 沙盒测试整改模式\n"
                f"上次沙盒测试未通过（综合分 {avg_score}），共 {len(fix_tasks)} 项待修复：\n"
                + "\n".join(lines)
            )
            if current_fix:
                fix_section += (
                    f"\n\n### 当前整改任务\n"
                    f"- 任务: {current_fix.get('title', '')}\n"
                    f"- 类型: {current_fix.get('type', '')}\n"
                    f"- 目标: {current_fix.get('target_ref', '未指定')}\n"
                    f"- 验收: {current_fix.get('acceptance_rule_text', '未指定')}\n"
                )
            fix_section += (
                "\n\n### 你的行为指引\n"
                "1. 先复述失败主因，让用户确认理解一致\n"
                "2. 建议从当前整改任务开始修复\n"
                "3. 根据 target_ref 指向具体文件位置\n"
                "4. 修改后推动任务完成，建议进行局部重测\n"
            )
            base += fix_section

        elif top_deductions:
            # fallback: 旧逻辑
            lines = []
            for i, d in enumerate(top_deductions, 1):
                dim = d.get("dimension", "unknown")
                reason = d.get("reason", "")
                fix = d.get("fix_suggestion", "")
                line = f"{i}. [{dim}] {reason}"
                if fix:
                    line += f" → 建议: {fix}"
                lines.append(line)
            fix_section = (
                f"\n\n## 沙盒测试待修复\n"
                f"上次沙盒测试未通过（综合分 {avg_score}），发现以下问题：\n"
                + "\n".join(lines)
                + "\n请询问用户是否按此计划逐项修复。"
            )
            base += fix_section

    return base


def _build_system(
    selected_skill_id: int | None,
    editor_prompt: str | None,
    editor_is_dirty: bool,
    available_tools: str = "（暂无已注册工具）",
    source_files: list[dict] | None = None,
    source_files_content: str = "",
    selected_source_filename: str | None = None,
    memo_context: dict | None = None,
    session_state: StudioSessionState | None = None,
    skill_metadata: dict | None = None,
) -> str:
    if editor_prompt and editor_prompt.strip():
        line_count = editor_prompt.count("\n") + 1
        if source_files:
            files_desc = "、".join(
                f"{f.get('filename', '?')}({f.get('category', '未分类')})"
                for f in source_files
            )
        else:
            files_desc = "（暂无附属文件）"
        ctx = _EDITOR_CONTEXT_TEMPLATE.format(
            skill_id=selected_skill_id or "未选择",
            is_dirty="是" if editor_is_dirty else "否",
            line_count=line_count,
            editor_prompt=editor_prompt[:2000],
            existing_files=files_desc,
            available_tools=available_tools,
        )
    else:
        ctx = _EDITOR_NO_CONTEXT

    memo_text = _build_memo_context(memo_context)
    state_text = _render_session_state(session_state) if session_state else "（首轮对话，尚无历史状态）"
    # M14: 用 XML tag 包裹用户内容，防止 Skill 内容注入 system prompt 指令
    # ── Architect 输出规则 ──
    if session_state and session_state.architect_phase:
        confirmed_list = ", ".join(
            k for k, v in (session_state.phase_confirmed or {}).items() if v
        ) or "无"
        architect_rules = _ARCHITECT_OUTPUT_RULES.format(
            current_phase=session_state.architect_phase,
            ooda_round=session_state.ooda_round,
            confirmed_phases=confirmed_list,
        )
    else:
        architect_rules = _NO_ARCHITECT_RULES

    result = _STUDIO_SYSTEM.format(
        editor_context=f"<user_content type=\"editor\">\n{ctx}\n</user_content>",
        memo_context=memo_text,
        session_state_context=f"<user_content type=\"session_state\">\n{state_text}\n</user_content>",
        architect_output_rules=architect_rules,
    )

    # 注入附属文件正文
    if source_files_content:
        result += "\n\n## 附属文件正文（可直接阅读和引用）\n"
        result += "以下是当前 Skill 的附属文件内容。当用户提到「读取文件」、「按 md 文件理解需求」等时，请基于这些内容回答。\n"
        result += source_files_content

    if selected_source_filename:
        result += f"\n\n> 用户当前正在编辑器中查看附属文件：**{selected_source_filename}**。当用户说「这个文件」、「当前文件」时，指的就是它。\n"

    # ── 辅助 Skill 策略注入 ──
    if session_state and session_state.session_mode:
        active_assists = ASSIST_SKILL_RULES.get(session_state.session_mode, [])
        if active_assists:
            assist_parts = []
            for skill_key in active_assists:
                prompt = ASSIST_SKILL_PROMPTS.get(skill_key)
                if prompt:
                    assist_parts.append(prompt)
            if assist_parts:
                result += "\n\n" + "\n\n".join(assist_parts)

    # ── 审计 addon（optimize/audit 首轮）──
    if session_state and session_state.session_mode in ("optimize_existing_skill", "audit_imported_skill"):
        if session_state.total_user_rounds <= 1:
            result += "\n\n" + _AUDIT_SYSTEM_ADDON

    # ── 治理动作 addon（optimize/audit 场景始终注入）──
    if session_state and session_state.session_mode in ("optimize_existing_skill", "audit_imported_skill"):
        result += "\n\n" + _GOVERNANCE_SYSTEM_ADDON

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 7. 历史消息裁剪 — 错误方向降权
# ══════════════════════════════════════════════════════════════════════════════

def _trim_history_with_rollup(
    history_messages: list[dict],
    state: StudioSessionState,
    max_recent: int = 6,
) -> list[dict]:
    """基于 session state 裁剪历史消息。

    - create 模式：只保留最近 4 轮（8 条）
    - optimize/audit 模式：保留最近 6 轮（12 条）
    - 如果有 context_rollup（说明经历过纠偏），前置摘要
    - 否则按 session_mode 裁剪
    """
    # 按 session_mode 调整保留轮数
    if state.session_mode == "create_new_skill":
        effective_max = min(max_recent, 8)  # 4 轮 = 8 条
    else:
        effective_max = min(max_recent * 2, 12)  # 6 轮 = 12 条

    if not state.context_rollup:
        if len(history_messages) > effective_max:
            return history_messages[-effective_max:]
        return history_messages

    # 有 rollup：只保留最近消息 + 前置摘要
    recent = history_messages[-effective_max:] if len(history_messages) > effective_max else history_messages
    rollup_msg = {
        "role": "assistant",
        "content": f"[上下文摘要] {state.context_rollup}",
    }
    return [rollup_msg] + recent


# ══════════════════════════════════════════════════════════════════════════════
# 8. Post-processing
# ══════════════════════════════════════════════════════════════════════════════

_BLOCK_PATTERN = re.compile(
    r"```(studio_draft|studio_diff|studio_test_result|studio_summary|studio_tool_suggestion|studio_file_split|studio_memo_status|studio_task_focus|studio_editor_target|studio_persistent_notices|studio_context_rollup|studio_audit|studio_governance_action|studio_phase_progress|architect_question|architect_phase_summary|architect_structure|architect_priority_matrix|architect_ooda_decision|architect_ready_for_draft)\s*\n([\s\S]*?)\n```",
    re.IGNORECASE,
)


def _extract_events(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """从完整 LLM 输出中提取 studio_* 块。"""
    events: list[tuple[str, dict]] = []
    clean = text

    for m in _BLOCK_PATTERN.finditer(text):
        evt_name = m.group(1).lower()
        try:
            payload = json.loads(m.group(2))
            events.append((evt_name, payload))
        except Exception:
            pass
        clean = clean.replace(m.group(0), "")

    return clean.strip(), events


_WRAPPER_BLOCK_RE = re.compile(
    r"^\s*```(?:markdown|md|text|plain|)?\s*\n([\s\S]*?)\n\s*```\s*$"
)


def _strip_wrapper_codeblock(text: str) -> str:
    """若整段文本只是一个 fenced code block 包裹，剥离外层。"""
    m = _WRAPPER_BLOCK_RE.match(text)
    if m:
        return m.group(1)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# 9. Stream runner
# ══════════════════════════════════════════════════════════════════════════════

async def run_stream(
    db: Session,
    conv_id: int,
    workspace_system_context: str,
    history_messages: list[dict],
    user_message: str,
    model_config: dict,
    selected_skill_id: int | None = None,
    editor_prompt: str | None = None,
    editor_is_dirty: bool = False,
    available_tools: str = "（暂无已注册工具）",
    source_files: list[dict] | None = None,
    source_files_content: str = "",
    selected_source_filename: str | None = None,
    memo_context: dict | None = None,
    skill_metadata: dict | None = None,
) -> AsyncIterator[tuple[str, dict] | str]:
    """流式运行 studio agent (V2)。"""

    # ── 1. 会话状态提取 + Fact Reconciliation ──
    session_state = _extract_session_state(
        history_messages,
        user_message,
        has_existing_prompt=bool(editor_prompt and editor_prompt.strip()),
    )

    # ── 1b. 任务路由（轻量规则，不调 LLM）──
    session_mode, route_reason, active_assists = _resolve_session_mode(
        selected_skill_id, editor_prompt, user_message,
        skill_metadata, session_state.total_user_rounds,
    )
    session_state.session_mode = session_mode

    logger.info(
        f"[studio_agent_v2] conv={conv_id} skill={selected_skill_id} "
        f"session_mode={session_mode} "
        f"scenario={session_state.scenario_type} mode={session_state.current_mode} "
        f"rounds={session_state.total_user_rounds} readiness={session_state.draft_readiness_score} "
        f"file_status={session_state.file_need_status} "
        f"corrections={len(session_state.user_corrections)} "
        f"rejections={len(session_state.rejected_assumptions)} "
        f"repeat_blocked={session_state.current_repeat_blocked}"
    )

    # ── 加载 Architect 工作流状态（若有）──
    arch_state = None
    try:
        from app.models.skill import ArchitectWorkflowState
        arch_state = db.query(ArchitectWorkflowState).filter(
            ArchitectWorkflowState.conversation_id == conv_id
        ).first()
    except Exception:
        pass  # 表可能不存在

    # 推断当前 architect phase
    arch_phase = ""
    if arch_state:
        arch_phase = arch_state.workflow_phase or "phase_1_why"
    elif session_mode == "create_new_skill":
        arch_phase = "phase_1_why"
    elif session_mode in ("optimize_existing_skill", "audit_imported_skill"):
        arch_phase = "phase_3_how"

    # 同步到 session_state
    session_state.architect_phase = arch_phase
    session_state.ooda_round = arch_state.ooda_round if arch_state else 0
    session_state.phase_confirmed = dict(arch_state.phase_confirmed or {}) if arch_state else {}

    if session_mode == "create_new_skill" and arch_phase and arch_phase != "ready_for_draft" and not session_state.has_outputted_draft:
        if session_state.current_mode in ("draft", "revise", "test_ready"):
            has_refine_signal = any(
                fact["type"] in ("correction", "scenario_shift")
                for fact in session_state.current_reconciled_facts
            )
            session_state.current_mode = "refine" if has_refine_signal else "discover"
        session_state.draft_readiness_score = min(session_state.draft_readiness_score, 2)

    workflow_mode = "architect_mode" if arch_phase else "none"
    next_action = _next_action_for_session_mode(session_mode)
    complexity_level = estimate_complexity_level(
        session_mode=session_mode,
        workflow_mode=workflow_mode,
        next_action=next_action,
        user_message=user_message,
        has_files=bool(source_files),
        has_memo=bool(memo_context),
        history_count=len(history_messages),
    )
    execution_strategy = choose_execution_strategy(
        complexity_level=complexity_level,
        workflow_mode=workflow_mode,
        next_action=next_action,
    )
    user_id = None
    try:
        from app.models.conversation import Conversation

        conversation = db.get(Conversation, conv_id)
        user_id = conversation.user_id if conversation else None
    except Exception:
        user_id = None
    rollout_decision = resolve_rollout_decision(
        db,
        user_id=user_id,
        session_mode=session_mode,
        workflow_mode=workflow_mode,
    )
    execution_strategy = apply_rollout_to_execution_strategy(
        execution_strategy,
        flags=rollout_decision.flags,
    )
    lane_statuses = lane_statuses_for_rollout(execution_strategy, flags=rollout_decision.flags)

    # ── 阶段 1: routing ──
    yield ("status", {"stage": "routing"})
    yield ("status", {
        "stage": "classified",
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        **lane_statuses,
    })

    # 统一事件名：route_status（与 conversations.py 结构化模式对齐）
    yield ("route_status", {
        "session_mode": session_mode,
        "route_reason": route_reason,
        "active_assist_skills": active_assists,
        "next_action": next_action,
        "workflow_mode": workflow_mode,
        "initial_phase": arch_phase,
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        **lane_statuses,
    })
    # 向后兼容旧前端
    yield ("studio_route", {
        "session_mode": session_mode,
        "route_reason": route_reason,
        "active_assist_skills": active_assists,
        "next_action": next_action,
        "workflow_mode": workflow_mode,
        "initial_phase": arch_phase,
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        **lane_statuses,
    })

    # assist_skills_status（与 conversations.py 对齐）
    yield ("assist_skills_status", {
        "skills": active_assists,
        "session_mode": session_mode,
    })

    # architect_phase_status（与 conversations.py 对齐）
    if arch_phase:
        yield ("architect_phase_status", {
            "phase": arch_phase,
            "mode_source": session_mode,
            "ooda_round": arch_state.ooda_round if arch_state else 0,
            "phase_confirmed": arch_state.phase_confirmed if arch_state else {},
        })

    # ── 2. 发送 reconciliation 结果（前端"已采纳"显示用）──
    reconciled_display = []
    for f in session_state.current_reconciled_facts:
        if f["type"] == "correction":
            reconciled_display.append({"type": "correction", "text": f["content"][:80]})
        elif f["type"] == "scenario_shift":
            reconciled_display.append({"type": "scenario_shift", "text": f.get("scenario", "")})
        elif f["type"] == "file_rejection":
            reconciled_display.append({"type": "file_rejection", "text": "不使用文件"})
        elif f["type"] == "execution_request":
            reconciled_display.append({"type": "execution_request", "text": "直接出草稿"})
        elif f["type"] == "new_fact":
            reconciled_display.append({"type": "new_fact", "text": f["content"][:60]})
        elif f["type"] == "constraint":
            reconciled_display.append({"type": "constraint", "text": f["content"][:60]})

    if reconciled_display:
        yield ("studio_reconciled_facts", {"facts": reconciled_display})

    if session_state.current_scenario_shift:
        yield ("studio_direction_shift", {
            "from": "unknown",
            "to": session_state.current_scenario_shift,
        })

    yield ("studio_file_need_status", {
        "status": session_state.file_need_status,
        "forbidden_countdown": session_state.file_forbidden_countdown,
    })

    if session_state.current_repeat_blocked:
        yield ("studio_repeat_blocked", {
            "reason": "连续重复追问，已自动切换到 draft 模式",
            "blocked_categories": session_state.asked_question_categories,
        })

    # ── 阶段 2: auditing（optimize/audit 场景）──
    if session_mode in ("optimize_existing_skill", "audit_imported_skill"):
        yield ("status", {"stage": "auditing"})

    # ── 3. 构建 system prompt ──
    system_content = _build_system(
        selected_skill_id, editor_prompt, editor_is_dirty,
        available_tools, source_files, source_files_content,
        selected_source_filename, memo_context, session_state,
        skill_metadata=skill_metadata,
    )
    if workspace_system_context:
        system_content = system_content + "\n\n## 额外上下文\n" + workspace_system_context

    # ── 4. 历史消息裁剪（错误方向降权）──
    trimmed_history = _trim_history_with_rollup(history_messages, session_state)

    llm_messages: list[dict] = [{"role": "system", "content": system_content}]
    for m in trimmed_history:
        llm_messages.append(m)

    # ── 阶段 3: generating ──
    yield ("status", {"stage": "generating"})

    # ── 5. 流式调用 LLM ──
    full_content = ""
    try:
        async for item in llm_gateway.chat_stream_typed(
            model_config=model_config, messages=llm_messages, tools=None
        ):
            if isinstance(item, str):
                yield item
                continue
            ctype, cdata = item
            if ctype == "content":
                full_content += cdata
                yield ("content_block_delta", {"index": 0, "delta": {"text": cdata}})
                yield ("delta", {"text": cdata})
    except Exception as e:
        # H20: LLM 流式调用异常优雅降级，发送 error event 而非直接崩溃
        logger.error(f"[studio_agent] LLM streaming error: {e}")
        yield ("error", {"message": f"AI 服务暂时不可用: {type(e).__name__}", "error_type": "server_error", "retryable": True})
        return

    # ── 6. Post-process ──
    clean_text, events = _extract_events(full_content)
    clean_text = _strip_wrapper_codeblock(clean_text)

    if clean_text != full_content:
        yield ("replace", {"text": clean_text})

    # ── 6b. 处理所有提取到的结构化事件 + architect 阶段推进 ──
    _PHASE_ORDER = ["phase_1_why", "phase_2_what", "phase_3_how", "ooda_iteration", "ready_for_draft"]

    for evt_name, payload in events:
        yield (evt_name, payload)

        # ── 审计升级：quality_score < 40 → 回到 phase_1_why ──
        if evt_name == "studio_audit":
            q_score = payload.get("quality_score", 100)
            severity = payload.get("severity", "low")
            phase_entry = payload.get("phase_entry", "phase_3_how")
            if q_score < 40 or severity == "critical":
                phase_entry = "phase_1_why"
            # 也发 audit_summary（与 conversations.py 协议对齐）
            yield ("audit_summary", {
                "verdict": "poor" if q_score < 40 else "needs_work" if q_score < 70 else "good",
                "issues": payload.get("issues", []),
                "recommended_path": payload.get("recommended_path", "optimize"),
                "quality_score": q_score,
                "phase_entry": phase_entry,
            })
            # 升级 architect phase
            if arch_state and phase_entry:
                try:
                    arch_state.workflow_phase = phase_entry
                    arch_state.workflow_mode = "architect_mode"
                    db.commit()
                except Exception:
                    pass
            yield ("architect_phase_status", {
                "phase": phase_entry,
                "mode_source": session_mode,
                "ooda_round": arch_state.ooda_round if arch_state else 0,
                "upgrade_reason": f"审计评分 {q_score}，严重度 {severity}",
            })
            yield ("route_status", {
                "session_mode": session_mode,
                "route_reason": f"审计评分 {q_score}，升级到 {phase_entry}",
                "active_assist_skills": active_assists + (["skill-architect-master"] if "skill-architect-master" not in active_assists else []),
                "workflow_mode": "architect_mode",
                "initial_phase": phase_entry,
            })

        # ── architect_phase_summary 确认 → 推进阶段 ──
        if evt_name == "architect_phase_summary":
            completed_phase = payload.get("phase", "")
            ready = payload.get("ready_for_next", False)
            if ready and completed_phase and arch_state:
                try:
                    # 标记阶段已确认
                    confirmed = dict(arch_state.phase_confirmed or {})
                    confirmed[completed_phase] = True
                    arch_state.phase_confirmed = confirmed

                    # 推进到下一阶段
                    cur_idx = _PHASE_ORDER.index(completed_phase) if completed_phase in _PHASE_ORDER else -1
                    if cur_idx >= 0 and cur_idx < len(_PHASE_ORDER) - 1:
                        next_phase = _PHASE_ORDER[cur_idx + 1]
                        arch_state.workflow_phase = next_phase
                        db.commit()
                        yield ("architect_phase_status", {
                            "phase": next_phase,
                            "mode_source": session_mode,
                            "ooda_round": arch_state.ooda_round,
                            "phase_confirmed": confirmed,
                            "transition": f"{completed_phase} → {next_phase}",
                        })
                except Exception as e:
                    logger.warning(f"[studio_agent] phase progression error: {e}")

        # ── architect_ooda_decision → 处理收敛或回调 ──
        if evt_name == "architect_ooda_decision":
            decision = payload.get("decision", "")
            if arch_state:
                try:
                    arch_state.ooda_round = (arch_state.ooda_round or 0) + 1
                    if decision == "continue_to_draft":
                        arch_state.workflow_phase = "ready_for_draft"
                    elif "phase_" in decision:
                        # 回调到指定阶段
                        arch_state.workflow_phase = decision.replace("回调到", "").strip()
                    db.commit()
                    yield ("architect_phase_status", {
                        "phase": arch_state.workflow_phase,
                        "mode_source": session_mode,
                        "ooda_round": arch_state.ooda_round,
                        "ooda_decision": decision,
                    })
                except Exception as e:
                    logger.warning(f"[studio_agent] OODA update error: {e}")

        # ── architect_ready_for_draft → 标记就绪 ──
        if evt_name == "architect_ready_for_draft":
            if arch_state:
                try:
                    arch_state.workflow_phase = "ready_for_draft"
                    db.commit()
                except Exception:
                    pass

        # ── studio_governance_action → 也发 governance_card 对齐 ──
        if evt_name == "studio_governance_action":
            yield ("governance_card", {
                "id": payload.get("card_id", ""),
                "type": "staged_edit",
                "title": payload.get("title", ""),
                "content": payload,
                "status": "pending",
                "actions": [{"label": "采纳", "type": "adopt"}, {"label": "跳过", "type": "reject"}],
            })

    # ── 阶段 4: done ──
    yield ("status", {"stage": "done"})

    # ── 7. 发送完整 state update（前端面板用）──
    yield ("studio_state_update", {
        "scenario": session_state.scenario_type,
        "mode": session_state.current_mode,
        "session_mode": session_mode,
        "architect_phase": arch_state.workflow_phase if arch_state else arch_phase,
        "ooda_round": arch_state.ooda_round if arch_state else 0,
        "goal": session_state.goal_summary[:80],
        "confirmed_facts": session_state.confirmed_facts[-5:],
        "active_constraints": session_state.active_constraints[-3:],
        "rejected": session_state.rejected_assumptions[-3:],
        "file_status": session_state.file_need_status,
        "readiness": session_state.draft_readiness_score,
        "has_draft": session_state.has_outputted_draft,
        "total_rounds": session_state.total_user_rounds,
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        "fast_status": "completed",
        "deep_status": lane_statuses["deep_status"],
        "reconciled_facts": reconciled_display,
        "direction_shift": {
            "from": "unknown",
            "to": session_state.current_scenario_shift,
        } if session_state.current_scenario_shift else None,
        "file_need_status": {
            "status": session_state.file_need_status,
            "forbidden_countdown": session_state.file_forbidden_countdown,
        },
        "repeat_blocked": {
            "reason": "连续重复追问，已自动切换到 draft 模式",
            "blocked_categories": session_state.asked_question_categories,
        } if session_state.current_repeat_blocked else None,
    })

    yield ("__full_content__", {"text": clean_text or full_content})


# ══════════════════════════════════════════════════════════════════════════════
# 10. Draft test (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

async def run_draft_test(
    system_prompt: str,
    test_input: str,
    model_config: dict,
) -> dict:
    """基于草稿 system_prompt 跑一次推理并返回测试报告。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_input},
    ]
    try:
        response_text, _ = await llm_gateway.chat(model_config, messages, temperature=0.3, max_tokens=1000)
        return {
            "input": test_input,
            "output": response_text,
            "passed": True,
            "issues": [],
            "suggestion": "",
        }
    except Exception as e:
        return {
            "input": test_input,
            "output": "",
            "passed": False,
            "issues": [str(e)],
            "suggestion": "请检查 Prompt 是否包含必要的角色定义和任务说明",
        }
