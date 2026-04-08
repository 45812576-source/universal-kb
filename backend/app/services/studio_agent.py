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

logger = logging.getLogger(__name__)


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
- 除 studio_tool_suggestion 和 studio_file_split 外，其他结构化块每次回复最多输出一个。JSON 必须合法。
- 不要解释这些代码块的格式，直接输出。

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

    # fixing 阶段 + 最近测试失败 → 追加 fix plan 提示
    if memo_data.get("lifecycle_stage") == "fixing" and latest_test and latest_test.get("status") == "failed":
        details = latest_test.get("details", {})
        quality_detail = details.get("quality_detail", {})
        avg_score = quality_detail.get("avg_score", "N/A")
        top_deductions = quality_detail.get("top_deductions", [])

        if top_deductions:
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
    result = _STUDIO_SYSTEM.format(
        editor_context=f"<user_content type=\"editor\">\n{ctx}\n</user_content>",
        memo_context=memo_text,
        session_state_context=f"<user_content type=\"session_state\">\n{state_text}\n</user_content>",
    )

    # 注入附属文件正文
    if source_files_content:
        result += "\n\n## 附属文件正文（可直接阅读和引用）\n"
        result += "以下是当前 Skill 的附属文件内容。当用户提到「读取文件」、「按 md 文件理解需求」等时，请基于这些内容回答。\n"
        result += source_files_content

    if selected_source_filename:
        result += f"\n\n> 用户当前正在编辑器中查看附属文件：**{selected_source_filename}**。当用户说「这个文件」、「当前文件」时，指的就是它。\n"

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

    - 如果有 context_rollup（说明经历过纠偏），只保留最近 max_recent 条
      并在前面插入一条 rollup 摘要
    - 否则原样返回
    """
    if not state.context_rollup:
        return history_messages

    # 有 rollup：只保留最近消息 + 前置摘要
    recent = history_messages[-max_recent:] if len(history_messages) > max_recent else history_messages
    rollup_msg = {
        "role": "assistant",
        "content": f"[上下文摘要] {state.context_rollup}",
    }
    return [rollup_msg] + recent


# ══════════════════════════════════════════════════════════════════════════════
# 8. Post-processing
# ══════════════════════════════════════════════════════════════════════════════

_BLOCK_PATTERN = re.compile(
    r"```(studio_draft|studio_diff|studio_test_result|studio_summary|studio_tool_suggestion|studio_file_split|studio_memo_status|studio_task_focus|studio_editor_target|studio_persistent_notices|studio_context_rollup)\s*\n([\s\S]*?)\n```",
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
) -> AsyncIterator[tuple[str, dict] | str]:
    """流式运行 studio agent (V2)。"""

    # ── 1. 会话状态提取 + Fact Reconciliation ──
    session_state = _extract_session_state(
        history_messages,
        user_message,
        has_existing_prompt=bool(editor_prompt and editor_prompt.strip()),
    )

    logger.info(
        f"[studio_agent_v2] conv={conv_id} skill={selected_skill_id} "
        f"scenario={session_state.scenario_type} mode={session_state.current_mode} "
        f"rounds={session_state.total_user_rounds} readiness={session_state.draft_readiness_score} "
        f"file_status={session_state.file_need_status} "
        f"corrections={len(session_state.user_corrections)} "
        f"rejections={len(session_state.rejected_assumptions)} "
        f"repeat_blocked={session_state.current_repeat_blocked}"
    )

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

    # ── 3. 构建 system prompt ──
    system_content = _build_system(
        selected_skill_id, editor_prompt, editor_is_dirty,
        available_tools, source_files, source_files_content,
        selected_source_filename, memo_context, session_state,
    )
    if workspace_system_context:
        system_content = system_content + "\n\n## 额外上下文\n" + workspace_system_context

    # ── 4. 历史消息裁剪（错误方向降权）──
    trimmed_history = _trim_history_with_rollup(history_messages, session_state)

    llm_messages: list[dict] = [{"role": "system", "content": system_content}]
    for m in trimmed_history:
        llm_messages.append(m)

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

    for evt_name, payload in events:
        yield (evt_name, payload)

    # ── 7. 发送完整 state update（前端面板用）──
    yield ("studio_state_update", {
        "scenario": session_state.scenario_type,
        "mode": session_state.current_mode,
        "goal": session_state.goal_summary[:80],
        "confirmed_facts": session_state.confirmed_facts[-5:],
        "active_constraints": session_state.active_constraints[-3:],
        "rejected": session_state.rejected_assumptions[-3:],
        "file_status": session_state.file_need_status,
        "readiness": session_state.draft_readiness_score,
        "has_draft": session_state.has_outputted_draft,
        "total_rounds": session_state.total_user_rounds,
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
