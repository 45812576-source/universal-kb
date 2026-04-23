"""Deterministic governance actions derived from preflight results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import Skill, StagedEdit
from app.services.governance_action_compiler import build_followup_card


@dataclass
class PreflightGovernanceResult:
    cards: list[dict] = field(default_factory=list)
    staged_edits: list[dict] = field(default_factory=list)


_QUALITY_SNIPPETS = {
    "coverage": "回答前先拆解目标，确保核心问题、关键维度和边界场景都被覆盖后再输出。",
    "correctness": "仅基于已知信息、知识库和可验证上下文作答；证据不足时明确说明不确定，禁止补造事实。",
    "constraint": "严格遵守权限、字段边界、输入约束和系统限制；未授权信息一律不输出、不推断。",
    "actionability": "输出必须包含明确结论、判断依据和下一步可执行动作，避免停留在空泛描述。",
}
_DESCRIPTION_REMEDIATION_CODES = {
    "missing_description",
    "generic_description",
    "weak_description",
    "inaccurate_description",
    "description_too_generic",
    "description_too_vague",
    "description_weak",
    "description_inaccurate",
    "poor_description",
}
_DESCRIPTION_REMEDIATION_HINTS = (
    "为空",
    "缺失",
    "笼统",
    "过于笼统",
    "空泛",
    "泛泛",
    "不精准",
    "未精准",
    "不准确",
    "核心能力",
    "检索",
    "展示",
    "审核",
)
_DESCRIPTION_SUGGESTION_KEYS = (
    "suggested_description",
    "replacement_description",
    "new_description",
    "expected_description",
)
_DESCRIPTION_TEXT_KEYS = (
    "suggested_changes",
    "suggestion",
    "recommendation",
    "fix",
    "issue",
    "reason",
    "message",
    "description",
    "acceptance_rule",
)


def _make_card(
    card_id: str,
    title: str,
    summary: str,
    *,
    card_type: str = "staged_edit",
    reason: str | None = None,
    staged_edit_id: int | None = None,
    preflight_action: str | None = None,
    action_payload: dict | None = None,
    actions: list[dict] | None = None,
) -> dict:
    content: dict[str, object] = {"summary": summary}
    if reason:
        content["reason"] = reason
    if staged_edit_id is not None:
        content["staged_edit_id"] = str(staged_edit_id)
    if preflight_action:
        content["preflight_action"] = preflight_action
    if action_payload:
        content["action_payload"] = action_payload
    return {
        "id": card_id,
        "type": card_type,
        "title": title,
        "content": content,
        "status": "pending",
        "actions": actions or (
            [{"label": "查看修改", "type": "view_diff"}, {"label": "采纳", "type": "adopt"}, {"label": "不采纳", "type": "reject"}]
            if staged_edit_id is not None
            else [{"label": "一键处理", "type": "adopt"}, {"label": "忽略", "type": "reject"}]
        ),
    }


def _create_staged_edit(
    db: Session,
    *,
    skill_id: int,
    target_type: str,
    summary: str,
    diff_ops: list[dict],
    target_key: str | None = None,
    risk_level: str = "medium",
) -> dict:
    existing = (
        db.query(StagedEdit)
        .filter(
            StagedEdit.skill_id == skill_id,
            StagedEdit.target_type == target_type,
            StagedEdit.target_key == target_key,
            StagedEdit.summary == summary,
            StagedEdit.status == "pending",
        )
        .order_by(StagedEdit.id.desc())
        .first()
    )
    if existing:
        return {
            "id": str(existing.id),
            "target_type": existing.target_type,
            "target_key": existing.target_key,
            "summary": existing.summary,
            "risk_level": existing.risk_level,
            "diff_ops": existing.diff_ops,
            "status": existing.status,
        }

    row = StagedEdit(
        skill_id=skill_id,
        target_type=target_type,
        target_key=target_key,
        diff_ops=diff_ops,
        summary=summary,
        risk_level=risk_level,
        status="pending",
    )
    db.add(row)
    db.flush()
    return {
        "id": str(row.id),
        "target_type": row.target_type,
        "target_key": row.target_key,
        "summary": row.summary,
        "risk_level": row.risk_level,
        "diff_ops": row.diff_ops,
        "status": row.status,
    }


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _iter_text_fragments(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        fragments: list[str] = []
        for item in value.values():
            fragments.extend(_iter_text_fragments(item, depth=depth + 1))
        return fragments
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_iter_text_fragments(item, depth=depth + 1))
        return fragments
    return []


def _strip_description_candidate(text: str) -> str:
    candidate = re.sub(r"^\s*[-*]\s*", "", text or "")
    candidate = re.sub(r"^\s*>\s*", "", candidate)
    candidate = candidate.strip().strip("`\"'“”‘’「」")
    candidate = re.sub(r"^description\s*(?:字段)?\s*[：:=]\s*", "", candidate, flags=re.IGNORECASE)
    return _clean_text(candidate).strip()


def _valid_description_candidate(text: str) -> bool:
    candidate = _strip_description_candidate(text)
    if not 12 <= len(candidate) <= 180:
        return False
    blocked_markers = ("修复方案", "修改要点", "验收标准", "失败主因", "```")
    if any(marker in candidate for marker in blocked_markers):
        return False
    lowered = candidate.lower()
    if lowered.startswith("description") or "替换为" in candidate:
        return False
    return True


def _extract_description_suggestion(value: Any, *, allow_plain: bool = False) -> str | None:
    if isinstance(value, dict):
        for key in _DESCRIPTION_SUGGESTION_KEYS:
            if key in value:
                candidate = _extract_description_suggestion(value.get(key), allow_plain=True)
                if candidate:
                    return candidate
        for key in _DESCRIPTION_TEXT_KEYS:
            if key in value:
                candidate = _extract_description_suggestion(value.get(key), allow_plain=False)
                if candidate:
                    return candidate
        for item in value.values():
            candidate = _extract_description_suggestion(item, allow_plain=allow_plain)
            if candidate:
                return candidate
        return None

    if isinstance(value, list):
        for item in value:
            candidate = _extract_description_suggestion(item, allow_plain=allow_plain)
            if candidate:
                return candidate
        return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            candidate = _strip_description_candidate(stripped)
            if _valid_description_candidate(candidate):
                return candidate

    patterns = [
        r"(?:将\s*)?description\s*(?:字段)?\s*(?:替换|改写|更新|优化|改成|改为)\s*为[：:]\s*([^\n\r]+)",
        r"(?:替换为|改为|更新为|优化为)[：:]\s*([^\n\r]+)",
        r"description\s*[=:：]\s*([^\n\r]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = _strip_description_candidate(match.group(1))
            if _valid_description_candidate(candidate):
                return candidate

    if allow_plain:
        candidate = _strip_description_candidate(text)
        if _valid_description_candidate(candidate):
            return candidate
    return None


def _is_description_remediation_payload(value: Any) -> bool:
    text = _clean_text(" ".join(_iter_text_fragments(value))).lower()
    if not text:
        return False
    mentions_description = "description" in text or "skill 描述" in text or "skill描述" in text or "描述" in text
    has_remediation_hint = any(hint in text for hint in _DESCRIPTION_REMEDIATION_HINTS)
    return mentions_description and has_remediation_hint


def _description_suggestion_for_item(skill: Skill, item: dict) -> str:
    return _extract_description_suggestion(item) or _default_description(skill)


def _latest_prompt(skill: Skill) -> str:
    versions = list(skill.versions or [])
    if versions:
        first = versions[0]
        return _clean_text(getattr(first, "system_prompt", "") or "")
    return ""


def _extract_scene(skill: Skill, prompt: str) -> str:
    prompt = _clean_text(prompt)
    if skill.name:
        name = str(skill.name).strip()
        if len(name) <= 24:
            return f"用于{name}场景"

    patterns = [
        r"你是([^。；\n]{4,24})",
        r"负责([^。；\n]{4,24})",
        r"帮助用户([^。；\n]{4,24})",
        r"适用于([^。；\n]{4,24})",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt)
        if match:
            phrase = _clean_text(match.group(1))
            if phrase:
                return f"用于{phrase}场景"
    return "用于业务分析与辅助决策场景"


def _extract_inputs(skill: Skill, prompt: str) -> str:
    signals: list[str] = []
    if skill.knowledge_tags:
        signals.append("知识资料")
    if skill.data_queries:
        signals.append("业务数据")
    bound_tools = list(getattr(skill, "bound_tools", []) or [])
    if bound_tools or skill.tools:
        signals.append("工具能力")

    source_files = list(skill.source_files or [])
    file_categories = {str(item.get("category", "")).strip() for item in source_files if isinstance(item, dict)}
    if "knowledge-base" in file_categories and "知识资料" not in signals:
        signals.append("知识资料")
    if ("reference" in file_categories or "example" in file_categories) and "业务规则" not in signals:
        signals.append("业务规则")

    if not signals:
        prompt_lower = prompt.lower()
        if "知识" in prompt or "rag" in prompt_lower or "检索" in prompt:
            signals.append("知识资料")
        if "数据" in prompt or "表" in prompt:
            signals.append("业务数据")
        if "工具" in prompt or "tool" in prompt_lower:
            signals.append("工具能力")

    if not signals:
        return "根据用户输入"
    if len(signals) == 1:
        return f"结合{signals[0]}"
    if len(signals) == 2:
        return f"结合{signals[0]}和{signals[1]}"
    return f"结合{signals[0]}、{signals[1]}与{signals[2]}"


def _extract_output(prompt: str) -> str:
    prompt_lower = prompt.lower()
    if "json" in prompt_lower or "结构化" in prompt:
        return "输出结构化结论和下一步建议"
    if "表格" in prompt or "markdown" in prompt_lower:
        return "输出结构化分析结果和执行建议"
    if "报告" in prompt or "方案" in prompt:
        return "输出分析结论、风险提示和行动建议"
    return "输出明确结论和下一步建议"


def _default_description(skill: Skill) -> str:
    prompt = _latest_prompt(skill)
    scene = _extract_scene(skill, prompt)
    inputs = _extract_inputs(skill, prompt)
    output = _extract_output(prompt)
    return f"{scene}，{inputs}，{output}。"


def _minimal_guardrail_patch() -> str:
    return (
        "\n\n## 最小运行护栏\n"
        "- 信息不足时先追问，不要自行补全未提供的事实\n"
        "- 输出先给结论，再补充依据、边界和下一步动作\n"
        "- 无依据时明确说明不确定\n"
        "- 禁止编造数据、来源或权限外信息\n"
    )


def _placeholder_reference(skill: Skill) -> str:
    return (
        f"# {skill.name} 参考资料\n\n"
        "## 业务背景\n"
        "- 待补充\n\n"
        "## 常用输入口径\n"
        "- 待补充\n\n"
        "## 判断标准\n"
        "- 待补充\n\n"
        "## 输出示例\n"
        "- 待补充\n"
    )


def _quality_patch(deduction: dict) -> str:
    dimension = str(deduction.get("dimension", "quality"))
    reason = str(deduction.get("reason", "")).strip()
    snippet = _QUALITY_SNIPPETS.get(dimension, "输出前先复核质量风险，并显式说明依据与边界。")
    lines = [f"\n\n## 最小质量护栏（{dimension}）", f"- {snippet}"]
    if reason:
        lines.append(f"- 当前重点风险：{reason}")
    return "\n".join(lines) + "\n"


def build_preflight_governance(
    db: Session,
    *,
    skill_id: int,
    result: dict,
) -> PreflightGovernanceResult:
    skill = db.get(Skill, skill_id)
    if not skill:
        return PreflightGovernanceResult()

    cards: list[dict] = []
    staged_edits: list[dict] = []

    for gate in result.get("gates", []):
        if gate.get("status") != "failed":
            continue

        if gate.get("gate") == "structure":
            for item in gate.get("items", []):
                if item.get("ok"):
                    continue
                code = item.get("code")
                if code == "prompt_too_short":
                    cards.append(build_followup_card(
                        card_id=f"preflight-structure-prompt-{skill_id}",
                        title="补齐 Prompt 缺失项",
                        target_kind="skill_prompt",
                        target_ref="SKILL.md",
                        reason="无正式质量检测结论前，只引导补角色、输入、输出与示例，不直接重写主体内容。",
                        preflight_action="open_skill_editor",
                        action_payload={
                            "target_file": "SKILL.md",
                            "allowed_actions": ["fill_missing_sections", "add_examples", "add_templates", "add_guardrails"],
                            "forbidden_actions": ["rewrite_full_prompt", "replace_main_body"],
                            "suggested_sections": ["角色定位", "输入要求", "输出要求", "example/reference/template"],
                        },
                        acceptance_rule="补齐角色、输入、输出、示例等关键结构后再重新执行 preflight。",
                        suggested_changes=item.get("issue", "System Prompt 过短"),
                    ))
                elif code in _DESCRIPTION_REMEDIATION_CODES or _is_description_remediation_payload(item):
                    is_missing = code == "missing_description"
                    summary = "补充 Skill 描述" if is_missing else "优化 Skill 描述"
                    title = summary
                    description = "一键补齐用于检索和审核展示的 Skill 描述。" if is_missing else "一键替换为更精准概括核心能力的 Skill 描述。"
                    staged = _create_staged_edit(
                        db,
                        skill_id=skill_id,
                        target_type="metadata",
                        summary=summary,
                        diff_ops=[{"op": "replace", "old": "description", "new": _description_suggestion_for_item(skill, item)}],
                        risk_level="low",
                    )
                    staged_edits.append(staged)
                    cards.append(_make_card(
                        f"preflight-structure-description-{skill_id}",
                        title,
                        description,
                        reason=item.get("issue", "description 为空"),
                        staged_edit_id=int(staged["id"]),
                    ))
                elif code == "missing_source_files":
                    staged = _create_staged_edit(
                        db,
                        skill_id=skill_id,
                        target_type="source_file",
                        target_key="reference.md",
                        summary="创建参考资料占位文件",
                        diff_ops=[{"op": "insert", "old": "", "new": _placeholder_reference(skill)}],
                        risk_level="low",
                    )
                    staged_edits.append(staged)
                    cards.append(_make_card(
                        f"preflight-structure-source-files-{skill_id}",
                        "创建附属资料占位文件",
                        "一键创建 `reference.md`，先满足附属文件要求，后续可继续补充内容。",
                        reason=item.get("issue", "无任何附属文件"),
                        staged_edit_id=int(staged["id"]),
                    ))

        elif gate.get("gate") == "knowledge":
            for item in gate.get("items", []):
                if item.get("ok"):
                    continue
                code = item.get("code")
                if code == "knowledge_not_archived":
                    cards.append(build_followup_card(
                        card_id=f"preflight-knowledge-archive-{skill_id}-{item.get('check')}",
                        title=f"归档知识文件：{item.get('check')}",
                        target_kind="knowledge_reference",
                        target_ref=str(item.get("check") or ""),
                        reason=item.get("issue", "未入库"),
                        preflight_action="confirm_archive",
                        action_payload={
                            "confirmations": [{
                                "filename": item.get("check"),
                                "target_board": "",
                                "target_category": "general",
                                "display_title": item.get("check"),
                            }],
                        },
                        acceptance_rule="知识文件已入库并建立可用索引。",
                        suggested_changes="按默认路径归档并写入知识库，同时建立向量索引。",
                    ))
                elif code == "knowledge_missing_vector_index":
                    cards.append(build_followup_card(
                        card_id=f"preflight-knowledge-reindex-{skill_id}-{item.get('knowledge_id')}",
                        title=f"重建向量索引：{item.get('check')}",
                        target_kind="knowledge_reference",
                        target_ref=str(item.get("check") or ""),
                        reason=item.get("issue", "已入库但无向量索引"),
                        preflight_action="reindex_knowledge",
                        action_payload={
                            "knowledge_ids": [item.get("knowledge_id")],
                            "filenames": [item.get("check")],
                        },
                        acceptance_rule="对应知识条目存在可用向量索引。",
                        suggested_changes="重建该知识条目的向量索引，然后重新执行质量检测。",
                    ))

        elif gate.get("gate") == "tools":
            for item in gate.get("items", []):
                if item.get("ok"):
                    continue
                failures = item.get("failures", []) or []
                for idx, failure in enumerate(failures):
                    code = failure.get("code")
                    if code in {"tool_inactive", "tool_module_missing"}:
                        cards.append(build_followup_card(
                            card_id=f"preflight-tools-tool-{skill_id}-{item.get('tool_id')}-{idx}",
                            title=f"处理工具问题：{item.get('check')}",
                            target_kind="tool_binding",
                            target_ref=str(item.get("check") or ""),
                            reason=item.get("issue", "工具未就绪"),
                            preflight_action="navigate_tools",
                            action_payload={
                                "target_url": "/skills",
                                "tool_id": failure.get("tool_id"),
                                "tool_name": failure.get("tool_name"),
                            },
                            acceptance_rule="工具处于可用状态且实现/模块完整。",
                            suggested_changes="跳转到 Skills & Tools 页面处理工具状态或实现问题。",
                        ))
                    elif code == "registered_table_missing":
                        cards.append(build_followup_card(
                            card_id=f"preflight-tools-table-{skill_id}-{failure.get('table_name')}",
                            title=f"补齐业务表：{failure.get('table_name')}",
                            target_kind="permission_config",
                            target_ref=str(failure.get("table_name") or ""),
                            reason=item.get("issue", "registered_table 数据源缺失"),
                            preflight_action="navigate_data_assets",
                            action_payload={
                                "target_url": "/data",
                                "table_name": failure.get("table_name"),
                            },
                            acceptance_rule="缺失业务表已在数据资产中可用并可被 Skill 引用。",
                            suggested_changes="跳转到数据资产页，补充或导入缺失的业务表后再重检。",
                        ))

    quality_detail = result.get("quality_detail", {}) or {}
    top_deductions = quality_detail.get("top_deductions", []) or []
    for idx, deduction in enumerate(top_deductions[:2], start=1):
        staged = _create_staged_edit(
            db,
            skill_id=skill_id,
            target_type="system_prompt",
            summary=f"补充最小质量护栏：{deduction.get('dimension', 'quality')}",
            diff_ops=[{"op": "insert", "old": "", "new": _quality_patch(deduction)}],
            risk_level="low",
        )
        staged_edits.append(staged)
        cards.append(_make_card(
            f"preflight-quality-{skill_id}-{idx}",
            f"补充质量护栏：{deduction.get('dimension', 'quality')}",
            deduction.get("reason", "质量未达标"),
            reason="preflight 阶段仅允许追加最小护栏，不生成整段重写草稿。",
            staged_edit_id=int(staged["id"]),
        ))

    db.commit()
    return PreflightGovernanceResult(cards=cards, staged_edits=staged_edits)
