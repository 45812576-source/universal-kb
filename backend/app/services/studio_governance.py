"""Studio Governance — 治理动作 + staged edit 生成与应用。"""
from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillAuditResult, SkillVersion, StagedEdit
from app.services.llm_gateway import llm_gateway
from app.services.studio_workflow_adapter import normalize_workflow_card, normalize_workflow_staged_edit

logger = logging.getLogger(__name__)


@dataclass
class GovernanceCard:
    title: str
    description: str
    severity: str  # "high" | "medium" | "low"
    category: str
    suggested_action: str  # "staged_edit" | "manual_review" | "brainstorming"


@dataclass
class GovernanceResult:
    cards: list[dict] = field(default_factory=list)
    staged_edits: list[dict] = field(default_factory=list)


_GOVERNANCE_PROMPT = """你是 Skill 治理专家。根据审计结果，生成治理建议卡片和具体的修改建议。

## Skill 信息
- 名称: {name}
- System Prompt (前 2000 字):
{system_prompt}

## 审计结果
- 评级: {verdict}
- 问题:
{issues_text}

## 输出要求
输出严格 JSON（不要 markdown 代码块），格式：
{{
  "cards": [
    {{
      "title": "问题简述",
      "description": "详细描述",
      "severity": "high|medium|low",
      "category": "structure|clarity|completeness|safety|performance",
      "suggested_action": "staged_edit|manual_review|brainstorming",
      "problem_refs": ["issue_1"],
      "target_kind": "skill_prompt|source_file|tool_binding|knowledge_reference|unknown",
      "target_ref": "SKILL.md 或文件名",
      "acceptance_rule": "修复后如何验收",
      "evidence_snippets": ["命中的问题证据"]
    }}
  ],
  "staged_edits": [
    {{
      "target_type": "system_prompt",
      "target_key": null,
      "problem_refs": ["issue_id 或 issue_index"],
      "summary": "修改摘要",
      "risk_level": "low|medium|high",
      "diff_ops": [
        {{"op": "replace", "old": "原文片段（前 50 字）", "new": "替换后内容"}}
      ]
    }}
  ]
}}

- 默认只生成 cards，不要为了 high severity issue 强行生成 staged_edit
- 只有当审计 issue 同时具备 target_ref/target_file、acceptance_rule 和 evidence/evidence_snippets 时，才允许生成 staged_edit
- 每个 staged_edit 必须填写 problem_refs，且只能引用具备上述证据的 issue
- diff_ops 只能修改命中 issue 对应的问题，不允许泛化重写全文
- replace/delete 的 old 必须是 System Prompt 中存在的精确原文；insert 的 old 必须是非空且存在的锚点
- 没有精确 old/new 文本时，只输出 card，不输出 staged_edits
- medium issue 生成 card + 可选 staged_edit（仍需满足证据条件）
- low issue 仅生成 card
"""


def _try_parse_json(raw: str) -> dict | None:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]+\}', cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None


def _issue_ref(issue: dict, index: int) -> str:
    return str(
        issue.get("issue_id")
        or issue.get("id")
        or issue.get("code")
        or f"issue_{index + 1}"
    )


def _has_formal_edit_evidence(issue: dict) -> bool:
    target_ref = issue.get("target_ref") or issue.get("target_file")
    acceptance = issue.get("acceptance_rule") or issue.get("acceptance_rule_text") or issue.get("acceptance")
    evidence = issue.get("evidence_snippets") or issue.get("evidence") or issue.get("evidence_text")
    return bool(target_ref and acceptance and evidence)


def _formal_issue_refs(issues: list[dict]) -> set[str]:
    refs: set[str] = set()
    for index, issue in enumerate(issues):
        if isinstance(issue, dict) and _has_formal_edit_evidence(issue):
            refs.add(_issue_ref(issue, index))
    return refs


def _edit_problem_refs(edit: dict) -> set[str]:
    raw = (
        edit.get("problem_refs")
        or edit.get("problem_ids")
        or edit.get("issue_ids")
        or edit.get("issue_refs")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _valid_diff_ops_for_prompt(diff_ops: list[dict], system_prompt: str) -> list[dict]:
    valid_ops: list[dict] = []
    for op in diff_ops:
        if not isinstance(op, dict):
            continue
        action = str(op.get("op") or "").strip().lower()
        old = str(op.get("old") or "")
        new = str(op.get("new") or "")
        if action == "replace" and old and old != new and old in system_prompt:
            valid_ops.append({"op": "replace", "old": old, "new": new})
        elif action == "delete" and old and old in system_prompt:
            valid_ops.append({"op": "delete", "old": old})
        elif action == "insert" and old and new and old in system_prompt:
            valid_ops.append({"op": "insert", "old": old, "new": new})
    return valid_ops


def _normalize_governance_edit(
    raw: dict,
    *,
    formal_refs: set[str],
    system_prompt: str,
) -> dict | None:
    refs = _edit_problem_refs(raw)
    if not refs or not refs.intersection(formal_refs):
        return None
    target_type = raw.get("target_type", "system_prompt")
    if target_type != "system_prompt":
        return None
    diff_ops = _valid_diff_ops_for_prompt(raw.get("diff_ops", []) or [], system_prompt)
    if not diff_ops:
        return None
    return {
        "target_type": "system_prompt",
        "target_key": None,
        "diff_ops": diff_ops,
        "summary": str(raw.get("summary") or "治理修改")[:200],
        "risk_level": raw.get("risk_level", "medium"),
        "problem_refs": sorted(refs),
    }


def _normalize_governance_card(raw: dict, *, has_staged_edits: bool) -> dict:
    card = dict(raw)
    if not has_staged_edits and card.get("suggested_action") == "staged_edit":
        card["suggested_action"] = "manual_review"
    if not has_staged_edits:
        card.setdefault("type", "followup_prompt")
    content = card.get("content") if isinstance(card.get("content"), dict) else {}
    raw_problem_refs = card.get("problem_refs")
    if not isinstance(raw_problem_refs, list):
        raw_problem_refs = []
    content = {
        **content,
        "problem_refs": list(_edit_problem_refs(card) or raw_problem_refs),
        "target_kind": str(card.get("target_kind") or content.get("target_kind") or "unknown"),
        "target_ref": str(card.get("target_ref") or content.get("target_ref") or ""),
        "acceptance_rule": str(card.get("acceptance_rule") or content.get("acceptance_rule") or ""),
        "evidence_snippets": list(card.get("evidence_snippets") or content.get("evidence_snippets") or []),
    }
    if content["target_kind"] == "skill_prompt" and not content["target_ref"]:
        content["target_ref"] = "SKILL.md"
    card["content"] = content
    return card


async def generate_governance_actions(
    db: Session,
    skill_id: int,
    audit_id: int | None = None,
    session_id: int | None = None,
) -> GovernanceResult:
    """基于审计结果生成治理卡片和 staged edits。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        return GovernanceResult()

    # 获取最新审计结果
    audit: SkillAuditResult | None = None
    if audit_id:
        audit = db.get(SkillAuditResult, audit_id)
    if not audit:
        audit = (
            db.query(SkillAuditResult)
            .filter(SkillAuditResult.skill_id == skill_id)
            .order_by(SkillAuditResult.created_at.desc())
            .first()
        )
    if not audit:
        return GovernanceResult()

    latest_version = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    system_prompt = latest_version.system_prompt if latest_version else ""

    issues_text = "\n".join(
        f"  - [{i.get('severity', '?')}] {i.get('category', '?')}: {i.get('description', '')}"
        for i in (audit.issues or [])
    )

    prompt = _GOVERNANCE_PROMPT.format(
        name=skill.name,
        system_prompt=system_prompt[:2000],
        verdict=audit.quality_verdict,
        issues_text=issues_text or "（无具体问题）",
    )

    model_config = llm_gateway.resolve_config(db, "studio.governance")
    raw_response = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
    )
    # llm_gateway.chat 返回 (text, metadata) tuple
    response = raw_response[0] if isinstance(raw_response, tuple) else raw_response

    parsed = _try_parse_json(response)
    if not parsed:
        logger.warning(f"[StudioGovernance] skill={skill_id} LLM 返回无法解析")
        return GovernanceResult()

    formal_refs = _formal_issue_refs(audit.issues or [])

    # 持久化 staged edits：无正式问题证据时只保留治理卡片，不落可采纳 diff。
    staged_edit_results = []
    for raw_edit in parsed.get("staged_edits", []):
        if not isinstance(raw_edit, dict):
            continue
        se = _normalize_governance_edit(
            raw_edit,
            formal_refs=formal_refs,
            system_prompt=system_prompt,
        )
        if not se:
            continue
        row = StagedEdit(
            skill_id=skill_id,
            session_id=session_id,
            target_type=se.get("target_type", "system_prompt"),
            target_key=se.get("target_key"),
            diff_ops=se.get("diff_ops", []),
            summary=se.get("summary", ""),
            risk_level=se.get("risk_level", "medium"),
            status="pending",
        )
        db.add(row)
        db.flush()
        staged_edit_results.append({
            "id": row.id,
            "target_type": row.target_type,
            "target_key": row.target_key,
            "summary": row.summary,
            "risk_level": row.risk_level,
            "diff_ops": row.diff_ops,
            "status": "pending",
        })

    db.commit()

    raw_cards = [card for card in parsed.get("cards", []) if isinstance(card, dict)]
    has_staged_edits = bool(staged_edit_results)
    normalized_cards = [
        normalize_workflow_card(card, source_type="studio_governance", phase="review")
        for card in (_normalize_governance_card(card, has_staged_edits=has_staged_edits) for card in raw_cards)
    ]
    normalized_staged_edits = [
        normalize_workflow_staged_edit(edit, source_type="studio_governance")
        for edit in staged_edit_results
        if isinstance(edit, dict)
    ]

    return GovernanceResult(
        cards=normalized_cards,
        staged_edits=normalized_staged_edits,
    )


def _apply_diff_ops(text: str, ops: list[dict]) -> str:
    """将 diff_ops 列表应用到文本，支持 replace / insert / delete。"""
    for op in ops:
        action = op.get("op", "replace")
        old = op.get("old", "")
        new = op.get("new", "")

        if action == "replace" and old:
            text = text.replace(old, new, 1)
        elif action == "insert":
            # insert: 在 old（锚点文本）之后插入 new；无锚点则追加到末尾
            if old and old in text:
                idx = text.index(old) + len(old)
                text = text[:idx] + new + text[idx:]
            else:
                text = text + "\n" + new
        elif action == "delete" and old:
            text = text.replace(old, "", 1)
    return text


def _apply_source_file_edit(skill: Skill, edit: StagedEdit) -> bool:
    """将 diff_ops 应用到 source_file，返回是否成功。"""
    import os
    from pathlib import Path

    target_key = edit.target_key
    if not target_key:
        return False

    source_files = skill.source_files or []
    matched = [f for f in source_files if f.get("filename") == target_key]
    if not matched:
        file_path = Path(f"uploads/skills/{skill.id}/{target_key}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        content = _apply_diff_ops("", edit.diff_ops or [])
        file_path.write_text(content, encoding="utf-8")
        source_files.append({
            "filename": target_key,
            "path": str(file_path),
            "size": len(content.encode("utf-8")),
            "category": "reference",
        })
        skill.source_files = source_files
        return True

    file_path = Path(matched[0].get("path", ""))
    if not file_path.exists():
        file_path = Path(f"uploads/skills/{skill.id}/{target_key}")
        file_path.parent.mkdir(parents=True, exist_ok=True)

    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")
        new_content = _apply_diff_ops(content, edit.diff_ops or [])
        file_path.write_text(new_content, encoding="utf-8")

        # 更新 source_files 中的 size
        for f in source_files:
            if f.get("filename") == target_key:
                f["size"] = len(new_content.encode("utf-8"))
        skill.source_files = source_files
        return True

    content = _apply_diff_ops("", edit.diff_ops or [])
    file_path.write_text(content, encoding="utf-8")
    for f in source_files:
        if f.get("filename") == target_key:
            f["path"] = str(file_path)
            f["size"] = len(content.encode("utf-8"))
            f.setdefault("category", "reference")
    skill.source_files = source_files
    return True



def adopt_staged_edit(db: Session, edit_id: int, user_id: int) -> dict:
    """将 staged edit 应用到正式内容，创建新版本。

    支持 target_type:
    - system_prompt: diff_ops 应用到 system_prompt，创建新 SkillVersion
    - source_file: diff_ops 应用到指定附属文件
    - metadata: diff_ops 应用到 skill 元数据字段
    """
    edit = db.get(StagedEdit, edit_id)
    if not edit:
        return {"ok": False, "error": "staged_edit_not_found"}

    # 幂等: 已 adopted 直接返回成功
    if edit.status == "adopted":
        return {"ok": True, "already_adopted": True}

    skill = db.get(Skill, edit.skill_id)
    if not skill:
        return {"ok": False, "error": "skill_not_found"}

    latest_version = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    if not latest_version:
        return {"ok": False, "error": "no_version_found"}

    new_version_created = False

    if edit.target_type == "system_prompt" and edit.diff_ops:
        new_prompt = _apply_diff_ops(latest_version.system_prompt, edit.diff_ops)
        new_version = SkillVersion(
            skill_id=skill.id,
            version=latest_version.version + 1,
            system_prompt=new_prompt,
            variables=latest_version.variables,
            required_inputs=latest_version.required_inputs,
            output_schema=latest_version.output_schema,
            model_config_id=latest_version.model_config_id,
            change_note=f"[Studio Governance] {edit.summary}",
            created_by=user_id,
        )
        db.add(new_version)
        new_version_created = True

    elif edit.target_type == "source_file" and edit.diff_ops:
        success = _apply_source_file_edit(skill, edit)
        if not success:
            return {"ok": False, "error": "source_file_apply_failed"}

    elif edit.target_type == "metadata" and edit.diff_ops:
        # metadata diff: 每个 op 的 old 是字段名，new 是新值
        for op in edit.diff_ops:
            field_name = op.get("old", "")
            new_value = op.get("new", "")
            if field_name == "description":
                skill.description = new_value
            elif field_name == "knowledge_tags" and isinstance(new_value, list):
                skill.knowledge_tags = new_value

    edit.status = "adopted"
    edit.resolved_at = datetime.datetime.utcnow()
    edit.resolved_by = user_id
    db.commit()

    try:
        from app.services.skill_memo_service import record_post_test_diff

        record_post_test_diff(
            db,
            skill.id,
            change_type="staged_edit_adopted",
            source="studio_governance",
            summary=edit.summary or "已采纳自动整改 diff",
            user_id=user_id,
            filename=edit.target_key if edit.target_type == "source_file" else "SKILL.md" if edit.target_type == "system_prompt" else None,
            file_type="asset" if edit.target_type == "source_file" else "prompt" if edit.target_type == "system_prompt" else "metadata",
            version_id=(new_version.version if new_version_created else latest_version.version),  # type: ignore[possibly-undefined]
            diff_ops=edit.diff_ops or [],
            staged_edit_id=edit.id,
            auto_generated=True,
        )
    except Exception as memo_err:
        logger.warning("record_post_test_diff failed for staged_edit %s: %s", edit.id, memo_err)

    result = {"ok": True, "skill_id": skill.id, "target_type": edit.target_type}
    if new_version_created:
        result["new_version"] = new_version.version  # type: ignore[possibly-undefined]
    return result


def reject_staged_edit(db: Session, edit_id: int, user_id: int) -> dict:
    """拒绝 staged edit。"""
    edit = db.get(StagedEdit, edit_id)
    if not edit:
        return {"ok": False, "error": "staged_edit_not_found"}

    # 幂等
    if edit.status == "rejected":
        return {"ok": True, "already_rejected": True}

    edit.status = "rejected"
    edit.resolved_at = datetime.datetime.utcnow()
    edit.resolved_by = user_id
    db.commit()

    return {"ok": True}
