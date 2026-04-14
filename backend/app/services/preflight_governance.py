"""Deterministic governance actions derived from preflight results."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.skill import Skill, StagedEdit


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


def _default_description(skill: Skill) -> str:
    return f"围绕「{skill.name}」场景，根据用户输入、知识资料与可用工具输出结构化结论和下一步建议。"


def _structure_prompt_patch() -> str:
    return (
        "\n\n## 角色定位\n"
        "- 明确你要解决的问题、适用场景和交付目标\n\n"
        "## 输入要求\n"
        "- 列出完成任务所需的关键信息；信息不足时先追问再继续\n\n"
        "## 处理步骤\n"
        "1. 先确认目标、约束和可用资料\n"
        "2. 再结合知识库或工具完成分析\n"
        "3. 最后输出结构化结论与建议\n\n"
        "## 输出要求\n"
        "- 先给结论，再给依据、风险和下一步动作\n\n"
        "## 边界与禁止项\n"
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
    fix = str(deduction.get("fix_suggestion", "")).strip()
    snippet = _QUALITY_SNIPPETS.get(dimension, "输出前先复核质量风险，并显式说明依据与边界。")
    lines = [f"\n\n## 质量整改要求（{dimension}）", f"- {snippet}"]
    if reason:
        lines.append(f"- 本轮重点问题：{reason}")
    if fix:
        lines.append(f"- 执行要求：{fix}")
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
                    staged = _create_staged_edit(
                        db,
                        skill_id=skill_id,
                        target_type="system_prompt",
                        summary="补齐 SKILL.md 的基础结构与边界要求",
                        diff_ops=[{"op": "insert", "old": "", "new": _structure_prompt_patch()}],
                        risk_level="medium",
                    )
                    staged_edits.append(staged)
                    cards.append(_make_card(
                        f"preflight-structure-prompt-{skill_id}",
                        "补齐 Prompt 结构",
                        item.get("issue", "System Prompt 过短"),
                        reason="当前 Prompt 信息不足，无法满足结构完整性检查。",
                        staged_edit_id=int(staged["id"]),
                    ))
                elif code == "missing_description":
                    staged = _create_staged_edit(
                        db,
                        skill_id=skill_id,
                        target_type="metadata",
                        summary="补充 Skill 描述",
                        diff_ops=[{"op": "replace", "old": "description", "new": _default_description(skill)}],
                        risk_level="low",
                    )
                    staged_edits.append(staged)
                    cards.append(_make_card(
                        f"preflight-structure-description-{skill_id}",
                        "补充 Skill 描述",
                        "一键补齐用于检索和审核展示的 Skill 描述。",
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
                    cards.append(_make_card(
                        f"preflight-knowledge-archive-{skill_id}-{item.get('check')}",
                        f"归档知识文件：{item.get('check')}",
                        "一键按默认路径归档并写入知识库，同时建立向量索引。",
                        card_type="followup_prompt",
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
                    ))
                elif code == "knowledge_missing_vector_index":
                    cards.append(_make_card(
                        f"preflight-knowledge-reindex-{skill_id}-{item.get('knowledge_id')}",
                        f"重建向量索引：{item.get('check')}",
                        "一键重建该知识条目的向量索引，然后可重新执行质量检测。",
                        card_type="followup_prompt",
                        reason=item.get("issue", "已入库但无向量索引"),
                        preflight_action="reindex_knowledge",
                        action_payload={
                            "knowledge_ids": [item.get("knowledge_id")],
                            "filenames": [item.get("check")],
                        },
                    ))

        elif gate.get("gate") == "tools":
            for item in gate.get("items", []):
                if item.get("ok"):
                    continue
                failures = item.get("failures", []) or []
                for idx, failure in enumerate(failures):
                    code = failure.get("code")
                    if code in {"tool_inactive", "tool_module_missing"}:
                        cards.append(_make_card(
                            f"preflight-tools-tool-{skill_id}-{item.get('tool_id')}-{idx}",
                            f"处理工具问题：{item.get('check')}",
                            "一键跳转到 Skills & Tools 页面处理工具状态或实现问题。",
                            card_type="followup_prompt",
                            reason=item.get("issue", "工具未就绪"),
                            preflight_action="navigate_tools",
                            action_payload={
                                "target_url": "/skills",
                                "tool_id": failure.get("tool_id"),
                                "tool_name": failure.get("tool_name"),
                            },
                        ))
                    elif code == "registered_table_missing":
                        cards.append(_make_card(
                            f"preflight-tools-table-{skill_id}-{failure.get('table_name')}",
                            f"补齐业务表：{failure.get('table_name')}",
                            "一键跳转到数据资产页，补充或导入缺失的业务表后再重检。",
                            card_type="followup_prompt",
                            reason=item.get("issue", "registered_table 数据源缺失"),
                            preflight_action="navigate_data_assets",
                            action_payload={
                                "target_url": "/data",
                                "table_name": failure.get("table_name"),
                            },
                        ))

    quality_detail = result.get("quality_detail", {}) or {}
    top_deductions = quality_detail.get("top_deductions", []) or []
    for idx, deduction in enumerate(top_deductions[:3], start=1):
        staged = _create_staged_edit(
            db,
            skill_id=skill_id,
            target_type="system_prompt",
            summary=f"质量整改：{deduction.get('dimension', 'quality')} · {deduction.get('reason', '未达标')}",
            diff_ops=[{"op": "insert", "old": "", "new": _quality_patch(deduction)}],
            risk_level="medium",
        )
        staged_edits.append(staged)
        cards.append(_make_card(
            f"preflight-quality-{skill_id}-{idx}",
            f"修复质量扣分项：{deduction.get('dimension', 'quality')}",
            deduction.get("reason", "质量未达标"),
            reason=deduction.get("fix_suggestion") or "按沙盒标准补齐质量约束。",
            staged_edit_id=int(staged["id"]),
        ))

    db.commit()
    return PreflightGovernanceResult(cards=cards, staged_edits=staged_edits)
