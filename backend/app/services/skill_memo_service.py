"""Skill Memo Service — Skill Studio 状态机核心业务逻辑。

职责：
- 初始化 / 重建 memo
- 导入分析（结构模板比对、缺失项生成）
- 任务状态推进（start / complete / skip）
- 测试结果回写
- 反馈采纳 → 任务生成
- 生命周期自动迁移
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillVersion
from app.models.skill_memo import SkillMemo

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

VALID_LIFECYCLE_STAGES = {
    "analysis", "planning", "editing", "awaiting_test",
    "testing", "fixing", "ready_to_submit", "completed",
}

VALID_TASK_TYPES = {
    "analyze_import", "define_goal", "edit_skill_md", "create_file",
    "update_file", "bind_tool", "create_tool_placeholder", "run_test",
    "fix_after_test", "adopt_feedback_change",
    # 结构化整改任务类型
    "fix_prompt_logic", "fix_input_slot", "fix_tool_usage",
    "fix_knowledge_binding", "fix_permission_handling", "run_targeted_retest",
}

VALID_TASK_STATUSES = {"todo", "in_progress", "done", "skipped"}

# 关键词用于判断 prompt 是否需要 reference / tool / template
_REF_KEYWORDS = re.compile(r"(参考|知识|资料|API|文档|数据库|手册)", re.IGNORECASE)
_TOOL_KEYWORDS = re.compile(r"(调用|获取数据|API|查询|计算|执行|搜索|爬取|请求)", re.IGNORECASE)
_TEMPLATE_KEYWORDS = re.compile(r"(固定格式|模板|输出格式|表格格式|JSON格式|Markdown格式)", re.IGNORECASE)


def _validate_lifecycle(stage: str) -> str:
    """L8: 校验 lifecycle_stage 值合法性。"""
    if stage not in VALID_LIFECYCLE_STAGES:
        raise ValueError(f"非法 lifecycle_stage: {stage!r}")
    return stage


def _validate_task_status(status: str) -> str:
    """L8: 校验 task status 值合法性。"""
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"非法 task status: {status!r}")
    return status


def _new_id(prefix: str = "task") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


class OptimisticLockError(Exception):
    """Raised when a memo was modified concurrently."""


def _save_memo_payload(db: Session, memo: SkillMemo, payload: dict) -> None:
    """M18: Atomically update memo_payload with optimistic version check."""
    payload = _invalidate_context_digest_cache(
        previous_payload=memo.memo_payload if isinstance(memo.memo_payload, dict) else {},
        next_payload=payload,
    )
    old_version = memo.version
    rows = (
        db.query(SkillMemo)
        .filter(SkillMemo.id == memo.id, SkillMemo.version == old_version)
        .update(
            {"memo_payload": payload, "version": old_version + 1},
            synchronize_session="fetch",
        )
    )
    if rows == 0:
        db.rollback()
        raise OptimisticLockError(
            f"SkillMemo {memo.id} was modified concurrently (expected version {old_version})"
        )


def _empty_payload() -> dict:
    return {
        "package_analysis": {
            "skill_md_summary": "",
            "directory_tree": [],
            "structure_template": {
                "required": ["SKILL.md"],
                "recommended": ["example", "reference", "knowledge-base", "template", "tool"],
            },
            "missing_items": [],
        },
        "persistent_notices": [],
        "tasks": [],
        "current_task_id": None,
        "progress_log": [],
        "test_history": [],
        "post_test_diffs": [],
        "adopted_feedback": [],
        "context_rollups": [],
        "workflow_recovery": {
            "workflow_state": None,
            "cards": [],
            "staged_edits": [],
            "updated_at": None,
        },
        "context_digest_cache": {
            "schema_version": 1,
            "updated_at": None,
            "entries": {},
        },
    }


def _empty_workflow_recovery() -> dict:
    return {
        "workflow_state": None,
        "cards": [],
        "staged_edits": [],
        "updated_at": None,
    }


def _empty_context_digest_cache() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": None,
        "entries": {},
    }


def _normalize_context_digest_cache(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("context_digest_cache")
    if not isinstance(raw, dict):
        return _empty_context_digest_cache()
    entries = raw.get("entries")
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "updated_at": raw.get("updated_at"),
        "entries": dict(entries) if isinstance(entries, dict) else {},
    }


def _invalidate_context_digest_cache(
    *,
    previous_payload: dict[str, Any] | None,
    next_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_next = copy.deepcopy(next_payload or _empty_payload())
    cache = _normalize_context_digest_cache(normalized_next)
    prev = previous_payload if isinstance(previous_payload, dict) else {}

    invalidated = False
    if prev.get("tasks") != normalized_next.get("tasks"):
        invalidated = cache["entries"].pop("memo_digest", None) is not None or invalidated
    if prev.get("persistent_notices") != normalized_next.get("persistent_notices"):
        invalidated = cache["entries"].pop("memo_digest", None) is not None or invalidated
    if prev.get("test_history") != normalized_next.get("test_history"):
        invalidated = cache["entries"].pop("memo_digest", None) is not None or invalidated
    if prev.get("current_task_id") != normalized_next.get("current_task_id"):
        invalidated = cache["entries"].pop("memo_digest", None) is not None or invalidated
    if prev.get("workflow_recovery") != normalized_next.get("workflow_recovery"):
        invalidated = cache["entries"].pop("memo_digest", None) is not None or invalidated
        invalidated = cache["entries"].pop("recovery_digest", None) is not None or invalidated

    if invalidated:
        cache["updated_at"] = _now_iso()
    normalized_next["context_digest_cache"] = cache
    return normalized_next


def _get_workflow_recovery(payload: dict[str, Any]) -> dict[str, Any]:
    base = _empty_workflow_recovery()
    raw = payload.get("workflow_recovery")
    if not isinstance(raw, dict):
        return base
    if isinstance(raw.get("workflow_state"), dict):
        base["workflow_state"] = raw.get("workflow_state")
    if isinstance(raw.get("cards"), list):
        base["cards"] = raw.get("cards")
    if isinstance(raw.get("staged_edits"), list):
        base["staged_edits"] = raw.get("staged_edits")
    if raw.get("updated_at") is not None:
        base["updated_at"] = raw.get("updated_at")
    return base


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


def _append_unique(container: dict[str, Any], key: str, value: str) -> bool:
    normalized_value = str(value).strip()
    if not normalized_value:
        return False
    items = _normalize_string_list(container.get(key))
    if normalized_value in items:
        return False
    items.append(normalized_value)
    container[key] = items
    return True


def _workflow_card_content(card: dict[str, Any]) -> dict[str, Any]:
    content = card.get("content")
    return content if isinstance(content, dict) else {}


def _workflow_card_action_payload(card: dict[str, Any]) -> dict[str, Any]:
    action_payload = _workflow_card_content(card).get("action_payload")
    return action_payload if isinstance(action_payload, dict) else {}


def _workflow_card_problem_refs(card: dict[str, Any]) -> list[str]:
    content = _workflow_card_content(card)
    refs = _normalize_string_list(content.get("problem_refs"))
    return refs or _normalize_string_list(_workflow_card_action_payload(card).get("problem_ids"))


def _workflow_card_source_report_id(card: dict[str, Any]) -> int | None:
    content = _workflow_card_content(card)
    raw = content.get("source_report_id")
    if raw is None:
        raw = _workflow_card_action_payload(card).get("source_report_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _workflow_card_target_kind(card: dict[str, Any]) -> str:
    content = _workflow_card_content(card)
    value = content.get("target_kind")
    if value in (None, ""):
        value = _workflow_card_action_payload(card).get("target_kind")
    return str(value or "").strip()


def _workflow_card_target_ref(card: dict[str, Any]) -> str:
    content = _workflow_card_content(card)
    value = content.get("target_ref")
    if value in (None, ""):
        value = _workflow_card_action_payload(card).get("target_ref")
    return str(value or "").strip()


def _task_matches_workflow_card(task: dict[str, Any], card: dict[str, Any]) -> bool:
    task_report_id = task.get("source_report_id")
    card_report_id = _workflow_card_source_report_id(card)
    if task_report_id is not None and card_report_id is not None:
        try:
            if int(task_report_id) != int(card_report_id):
                return False
        except (TypeError, ValueError):
            return False

    task_refs = set(_normalize_string_list(task.get("problem_refs")))
    card_refs = set(_workflow_card_problem_refs(card))
    if task_refs and card_refs:
        return bool(task_refs & card_refs)

    task_target_kind = str(task.get("target_kind") or "").strip()
    card_target_kind = _workflow_card_target_kind(card)
    if task_target_kind and card_target_kind and task_target_kind == card_target_kind:
        task_target_ref = str(task.get("target_ref") or "").strip()
        card_target_ref = _workflow_card_target_ref(card)
        if task_target_ref and card_target_ref:
            return task_target_ref == card_target_ref
        return True

    return False


def _link_workflow_recovery_tasks(payload: dict[str, Any], recovery: dict[str, Any]) -> bool:
    tasks = payload.get("tasks", []) if isinstance(payload.get("tasks"), list) else []
    cards = recovery.get("cards", []) if isinstance(recovery.get("cards"), list) else []
    staged_edits = recovery.get("staged_edits", []) if isinstance(recovery.get("staged_edits"), list) else []
    if not tasks or not cards:
        return False

    staged_edit_lookup = {
        str(edit.get("id")): edit
        for edit in staged_edits
        if isinstance(edit, dict) and edit.get("id") is not None
    }

    changed = False
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            continue
        content = dict(_workflow_card_content(card))
        staged_edit_id = str(content.get("staged_edit_id") or "").strip()
        for task in tasks:
            if not isinstance(task, dict) or not _task_matches_workflow_card(task, card):
                continue
            changed |= _append_unique(task, "workflow_card_ids", card_id)
            changed |= _append_unique(content, "related_task_ids", str(task.get("id") or ""))
            if staged_edit_id:
                changed |= _append_unique(task, "workflow_staged_edit_ids", staged_edit_id)
                edit = staged_edit_lookup.get(staged_edit_id)
                if isinstance(edit, dict):
                    changed |= _append_unique(edit, "related_task_ids", str(task.get("id") or ""))
        if content != _workflow_card_content(card):
            card["content"] = content
            changed = True
    return changed


def _workflow_card_acceptance_rule(card: dict[str, Any]) -> str:
    content = _workflow_card_content(card)
    value = content.get("acceptance_rule")
    if value in (None, ""):
        value = _workflow_card_action_payload(card).get("acceptance_rule")
    return str(value or "").strip()


def _workflow_card_evidence_snippets(card: dict[str, Any]) -> list[str]:
    content = _workflow_card_content(card)
    snippets = content.get("evidence_snippets")
    if not isinstance(snippets, list):
        snippets = _workflow_card_action_payload(card).get("evidence_snippets")
    return [str(item).strip() for item in (snippets or []) if str(item).strip()]


def _audit_task_type_from_card(card: dict[str, Any]) -> str:
    target_kind = _workflow_card_target_kind(card)
    if target_kind == "skill_prompt":
        return "edit_skill_md"
    if target_kind == "source_file":
        return "update_file"
    if target_kind == "tool_binding":
        return "bind_tool"
    return "edit_skill_md"


def _audit_task_target_files(card: dict[str, Any]) -> list[str]:
    target_kind = _workflow_card_target_kind(card)
    target_ref = _workflow_card_target_ref(card)
    if target_kind == "skill_prompt":
        return ["SKILL.md"]
    if target_kind == "source_file" and target_ref:
        return [target_ref]
    return []


def _priority_from_severity(value: str | None) -> str:
    severity = str(value or "").strip().lower()
    if severity == "high":
        return "high"
    if severity == "medium":
        return "medium"
    return "low"


def _sync_import_audit_tasks_from_recovery(payload: dict[str, Any], recovery: dict[str, Any]) -> bool:
    workflow_state = recovery.get("workflow_state") if isinstance(recovery.get("workflow_state"), dict) else {}
    if str(workflow_state.get("session_mode") or "") != "audit_imported_skill":
        return False

    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return False
    cards = recovery.get("cards", [])
    if not isinstance(cards, list) or not cards:
        return False

    changed = False
    pending_audit_task_ids: list[str] = []
    active_card_ids: set[str] = set()
    existing_by_card_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or str(task.get("source") or "") != "import_audit":
            continue
        for card_id in _normalize_string_list(task.get("workflow_card_ids")):
            existing_by_card_id[card_id] = task

    for card in cards:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            continue
        active_card_ids.add(card_id)
        task = existing_by_card_id.get(card_id)
        target_kind = _workflow_card_target_kind(card) or "unknown"
        target_ref = _workflow_card_target_ref(card)
        acceptance_rule = _workflow_card_acceptance_rule(card)
        problem_refs = _workflow_card_problem_refs(card)
        evidence_snippets = _workflow_card_evidence_snippets(card)
        desired_status = "todo" if str(card.get("status") or "pending") == "pending" else "done" if str(card.get("status")) == "adopted" else "skipped"

        if task is None:
            task = {
                "id": _new_id("task"),
                "title": str(card.get("title") or "审计整改任务")[:200],
                "type": _audit_task_type_from_card(card),
                "status": desired_status,
                "priority": _priority_from_severity(card.get("severity")),
                "source": "import_audit",
                "description": str(
                    (_workflow_card_content(card).get("summary"))
                    or (_workflow_card_content(card).get("reason"))
                    or card.get("summary")
                    or ""
                )[:500],
                "target_files": _audit_task_target_files(card),
                "acceptance_rule": {"mode": "custom", "text": acceptance_rule},
                "depends_on": [],
                "started_at": None,
                "completed_at": None,
                "completed_by": None,
                "result_summary": None,
                "problem_refs": problem_refs,
                "target_kind": target_kind,
                "target_ref": target_ref,
                "acceptance_rule_text": acceptance_rule,
                "evidence_snippets": evidence_snippets,
                "workflow_card_ids": [card_id],
            }
            staged_edit_id = str(_workflow_card_content(card).get("staged_edit_id") or "").strip()
            if staged_edit_id:
                task["workflow_staged_edit_ids"] = [staged_edit_id]
            tasks.append(task)
            existing_by_card_id[card_id] = task
            changed = True
        else:
            updated_fields = {
                "title": str(card.get("title") or task.get("title") or "审计整改任务")[:200],
                "type": _audit_task_type_from_card(card),
                "priority": _priority_from_severity(card.get("severity")),
                "description": str(
                    (_workflow_card_content(card).get("summary"))
                    or (_workflow_card_content(card).get("reason"))
                    or card.get("summary")
                    or task.get("description")
                    or ""
                )[:500],
                "target_files": _audit_task_target_files(card),
                "problem_refs": problem_refs,
                "target_kind": target_kind,
                "target_ref": target_ref,
                "acceptance_rule_text": acceptance_rule,
                "evidence_snippets": evidence_snippets,
            }
            for field, value in updated_fields.items():
                if task.get(field) != value:
                    task[field] = value
                    changed = True
            acceptance_payload = {"mode": "custom", "text": acceptance_rule}
            if task.get("acceptance_rule") != acceptance_payload:
                task["acceptance_rule"] = acceptance_payload
                changed = True
            if task.get("status") != desired_status and task.get("status") not in {"done", "skipped"}:
                task["status"] = desired_status
                changed = True
            changed |= _append_unique(task, "workflow_card_ids", card_id)
            staged_edit_id = str(_workflow_card_content(card).get("staged_edit_id") or "").strip()
            if staged_edit_id:
                changed |= _append_unique(task, "workflow_staged_edit_ids", staged_edit_id)

        if task.get("status") in {"todo", "in_progress"}:
            pending_audit_task_ids.append(str(task.get("id") or ""))

    for task in tasks:
        if not isinstance(task, dict) or str(task.get("source") or "") != "import_audit":
            continue
        linked_ids = set(_normalize_string_list(task.get("workflow_card_ids")))
        if linked_ids and not (linked_ids & active_card_ids) and task.get("status") in {"todo", "in_progress"}:
            task["status"] = "skipped"
            task["result_summary"] = "已被新一轮审计结果替代"
            changed = True

    if pending_audit_task_ids and str(workflow_state.get("next_action") or "") == "review_cards":
        next_audit_task_id = pending_audit_task_ids[0]
        if payload.get("current_task_id") != next_audit_task_id:
            payload["current_task_id"] = next_audit_task_id
            changed = True
    return changed


def _resolve_related_notices(payload: dict[str, Any], task_ids: list[str]) -> bool:
    if not task_ids:
        return False
    task_id_set = {str(task_id).strip() for task_id in task_ids if str(task_id).strip()}
    if not task_id_set:
        return False

    resolved = False
    for notice in payload.get("persistent_notices", []):
        if not isinstance(notice, dict):
            continue
        related_task_ids = set(_normalize_string_list(notice.get("related_task_ids")))
        if related_task_ids and task_id_set & related_task_ids and notice.get("status") != "resolved":
            notice["status"] = "resolved"
            resolved = True
    return resolved


def _workflow_lifecycle_stage(workflow_state: dict[str, Any]) -> str:
    workflow_mode = str(workflow_state.get("workflow_mode") or "")
    session_mode = str(workflow_state.get("session_mode") or "")
    phase = str(workflow_state.get("phase") or "")
    next_action = str(workflow_state.get("next_action") or "")

    if workflow_mode in {"preflight_remediation", "sandbox_remediation"} or phase == "remediate":
        return "fixing"
    if phase == "validate" or next_action in {"run_preflight", "run_sandbox", "run_targeted_rerun"}:
        return "awaiting_test"
    if workflow_mode == "architect_mode" or session_mode == "create_new_skill":
        return "planning"
    if next_action in {"run_audit", "review_cards"} or session_mode == "audit_imported_skill":
        return "analysis"
    return "editing"


def _workflow_status_summary(workflow_state: dict[str, Any]) -> str:
    phase = str(workflow_state.get("phase") or "discover")
    next_action = str(workflow_state.get("next_action") or "continue_chat")
    route_reason = str(workflow_state.get("route_reason") or "").strip()
    prefix = f"当前流程阶段：{phase}；下一步：{next_action}"
    return f"{prefix}（{route_reason}）" if route_reason else prefix


def _refresh_workflow_recovery_state(payload: dict[str, Any]) -> bool:
    recovery = _get_workflow_recovery(payload)
    workflow_state = recovery.get("workflow_state") if isinstance(recovery.get("workflow_state"), dict) else None
    if not workflow_state:
        return False

    changed = False
    pending_staged_edits = [
        edit for edit in recovery["staged_edits"]
        if isinstance(edit, dict) and str(edit.get("status") or "pending") == "pending"
    ]
    if not pending_staged_edits and workflow_state.get("next_action") == "review_cards":
        adopted_edits = [
            edit for edit in recovery["staged_edits"]
            if isinstance(edit, dict) and str(edit.get("status") or "") == "adopted"
        ]
        if adopted_edits:
            recommendation = _workflow_test_recommendation(payload, workflow_state)
            metadata = workflow_state.get("metadata") if isinstance(workflow_state.get("metadata"), dict) else {}
            metadata = dict(metadata)
            metadata["test_recommendation"] = recommendation
            workflow_state["metadata"] = metadata
            workflow_state["phase"] = "validate"
            workflow_state["next_action"] = recommendation["action"]
        else:
            workflow_state["next_action"] = "continue_chat"
        changed = True

    if changed:
        recovery["workflow_state"] = workflow_state
        recovery["updated_at"] = _now_iso()
        payload["workflow_recovery"] = recovery
    return changed


def _sync_tasks_from_workflow_action(
    payload: dict[str, Any],
    *,
    card_id: str | None,
    staged_edit_id: str | None,
    updated_card_status: str | None,
    updated_staged_edit_status: str | None,
    user_id: int | None = None,
) -> bool:
    recovery = _get_workflow_recovery(payload)
    changed = False
    tasks = payload.get("tasks", []) if isinstance(payload.get("tasks"), list) else []
    matched_task_ids: list[str] = []

    normalized_card_id = str(card_id) if card_id is not None else None
    normalized_staged_edit_id = str(staged_edit_id) if staged_edit_id is not None else None
    target_status = updated_staged_edit_status or updated_card_status
    if target_status not in {"adopted", "rejected"}:
        if changed:
            payload["workflow_recovery"] = recovery
        return changed

    task_status = "done" if target_status == "adopted" else "skipped"
    task_summary = "已通过 workflow 卡片处理" if target_status == "adopted" else "已忽略对应 workflow 卡片"

    for task in tasks:
        if not isinstance(task, dict) or task.get("type") == "run_targeted_retest":
            continue
        linked_card_ids = set(_normalize_string_list(task.get("workflow_card_ids")))
        linked_staged_edit_ids = set(_normalize_string_list(task.get("workflow_staged_edit_ids")))
        if (
            normalized_card_id and normalized_card_id in linked_card_ids
        ) or (
            normalized_staged_edit_id and normalized_staged_edit_id in linked_staged_edit_ids
        ):
            matched_task_ids.append(str(task.get("id") or ""))
            if task.get("status") not in {"done", "skipped"}:
                task["status"] = task_status
                task["completed_at"] = _now_iso()
                task["completed_by"] = user_id or "workflow_action"
                task["result_summary"] = task_summary
                changed = True

    if _resolve_related_notices(payload, matched_task_ids):
        changed = True
    if matched_task_ids:
        next_task = _pick_next_task(payload)
        payload["current_task_id"] = next_task["id"] if next_task else None
    if changed:
        payload["workflow_recovery"] = recovery
    return changed


def _sync_workflow_recovery_from_completed_tasks(payload: dict[str, Any], completed_task_ids: list[str]) -> bool:
    if not completed_task_ids:
        return False
    recovery = _get_workflow_recovery(payload)
    changed = _link_workflow_recovery_tasks(payload, recovery)
    completed_task_id_set = {str(task_id).strip() for task_id in completed_task_ids if str(task_id).strip()}
    if not completed_task_id_set:
        if changed:
            payload["workflow_recovery"] = recovery
        return changed

    staged_edit_lookup = {
        str(edit.get("id")): edit
        for edit in recovery.get("staged_edits", [])
        if isinstance(edit, dict) and edit.get("id") is not None
    }

    for card in recovery.get("cards", []):
        if not isinstance(card, dict):
            continue
        content = _workflow_card_content(card)
        related_task_ids = set(_normalize_string_list(content.get("related_task_ids")))
        if not (completed_task_id_set & related_task_ids):
            continue
        if card.get("status") == "pending":
            card["status"] = "adopted"
            changed = True
        staged_edit_id = str(content.get("staged_edit_id") or "").strip()
        if staged_edit_id:
            edit = staged_edit_lookup.get(staged_edit_id)
            if isinstance(edit, dict) and edit.get("status") == "pending":
                edit["status"] = "adopted"
                changed = True

    if changed:
        recovery["updated_at"] = _now_iso()
        payload["workflow_recovery"] = recovery
        changed = _refresh_workflow_recovery_state(payload) or changed
    return changed


def _workflow_test_recommendation(
    payload: dict[str, Any],
    workflow_state: dict[str, Any],
) -> dict[str, Any]:
    tasks = payload.get("tasks", []) if isinstance(payload.get("tasks"), list) else []
    targeted_tasks = [
        task for task in tasks
        if isinstance(task, dict)
        and task.get("type") == "run_targeted_retest"
        and task.get("status") in {"todo", "in_progress"}
    ]
    if targeted_tasks:
        issue_ids: list[str] = []
        source_report_id: int | None = None
        for task in targeted_tasks:
            if source_report_id is None and task.get("source_report_id") is not None:
                try:
                    source_report_id = int(task.get("source_report_id"))
                except (TypeError, ValueError):
                    source_report_id = None
            for problem_ref in task.get("problem_refs", []) or []:
                value = str(problem_ref).strip()
                if value and value not in issue_ids:
                    issue_ids.append(value)
        return {
            "action": "run_targeted_rerun",
            "scope": "targeted_rerun",
            "label": "运行局部重测",
            "source_report_id": source_report_id,
            "issue_ids": issue_ids,
        }

    workflow_mode = str(workflow_state.get("workflow_mode") or "")
    route_reason = str(workflow_state.get("route_reason") or "")
    latest_test = (payload.get("test_history") or [])[-1] if payload.get("test_history") else {}
    latest_source = str(latest_test.get("source") or "")

    if workflow_mode == "sandbox_remediation" or route_reason == "sandbox_failed" or latest_source in {"sandbox", "sandbox_interactive"}:
        return {
            "action": "run_sandbox",
            "scope": "sandbox",
            "label": "重新运行沙盒测试",
        }

    return {
        "action": "run_preflight",
        "scope": "preflight",
        "label": "重新运行 Preflight",
    }


def _sync_workflow_recovery_after_test_result(
    payload: dict[str, Any],
    *,
    source: str,
    status: str,
    approval_eligible: bool | None,
    summary: str,
    source_report_id: int | None,
) -> None:
    recovery = _get_workflow_recovery(payload)
    workflow_state = recovery.get("workflow_state") if isinstance(recovery.get("workflow_state"), dict) else None
    if not workflow_state:
        return

    metadata = workflow_state.get("metadata") if isinstance(workflow_state.get("metadata"), dict) else {}
    metadata = dict(metadata)
    metadata["last_test"] = {
        "source": source,
        "status": status,
        "approval_eligible": approval_eligible,
        "summary": summary,
        "source_report_id": source_report_id,
        "updated_at": _now_iso(),
    }
    metadata.pop("test_recommendation", None)

    if status == "passed":
        recovery["cards"] = []
        recovery["staged_edits"] = []
        if source == "preflight":
            metadata["test_recommendation"] = {
                "action": "run_sandbox",
                "scope": "sandbox",
                "label": "运行沙盒测试",
            }
            workflow_state["phase"] = "validate"
            workflow_state["next_action"] = "run_sandbox"
            workflow_state["route_reason"] = "preflight_passed"
        else:
            workflow_state["phase"] = "ready"
            workflow_state["next_action"] = "submit_approval" if approval_eligible is not False else "continue_chat"
            workflow_state["route_reason"] = "tests_passed"
            workflow_state["status"] = "ready" if approval_eligible is not False else str(workflow_state.get("status") or "active")
    else:
        recovery["cards"] = []
        recovery["staged_edits"] = []
        workflow_state["phase"] = "validate"
        if source == "preflight":
            workflow_state["next_action"] = "review_cards"
            workflow_state["route_reason"] = "preflight_failed"
        elif source in {"sandbox", "sandbox_interactive"}:
            workflow_state["next_action"] = "review_cards" if workflow_state.get("workflow_mode") == "sandbox_remediation" else "import_remediation"
            workflow_state["route_reason"] = "sandbox_failed"

    workflow_state["metadata"] = metadata
    recovery["workflow_state"] = workflow_state
    recovery["updated_at"] = _now_iso()
    payload["workflow_recovery"] = recovery


def _ensure_workflow_memo(
    db: Session,
    skill_id: int,
    workflow_state: dict[str, Any],
    user_id: int | None = None,
) -> SkillMemo | None:
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if memo:
        return memo

    skill = db.get(Skill, skill_id)
    if not skill:
        return None

    actor_id = user_id or skill.created_by
    if not actor_id:
        logger.warning("sync_workflow_recovery skipped: skill=%s has no actor", skill_id)
        return None

    session_mode = str(workflow_state.get("session_mode") or "")
    scenario_type = "import_remediation" if session_mode == "audit_imported_skill" else "published_iteration"
    memo = SkillMemo(
        skill_id=skill_id,
        scenario_type=scenario_type,
        lifecycle_stage=_workflow_lifecycle_stage(workflow_state),
        status_summary=_workflow_status_summary(workflow_state),
        goal_summary=skill.description,
        memo_payload=_empty_payload(),
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(memo)
    db.flush()
    return memo


def _build_skill_artifact_snapshot(db: Session, skill_id: int) -> dict | None:
    """生成当前 Skill 可测试内容的稳定指纹。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        return None

    latest_version = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )

    source_files = []
    for item in list(skill.source_files or []):
        if isinstance(item, dict):
            source_files.append({
                "filename": item.get("filename"),
                "size": item.get("size"),
                "category": item.get("category"),
                "path": item.get("path"),
            })

    snapshot = {
        "skill_id": skill_id,
        "version": latest_version.version if latest_version else None,
        "system_prompt_hash": hashlib.sha256(
            ((latest_version.system_prompt if latest_version else "") or "").encode("utf-8")
        ).hexdigest()[:16],
        "description_hash": hashlib.sha256((skill.description or "").encode("utf-8")).hexdigest()[:16],
        "knowledge_tags": sorted(str(tag) for tag in (skill.knowledge_tags or [])),
        "source_files": sorted(source_files, key=lambda item: str(item.get("filename") or "")),
        "bound_tool_ids": sorted(
            int(tool.id) for tool in list(getattr(skill, "bound_tools", []) or []) if getattr(tool, "id", None) is not None
        ),
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    snapshot["fingerprint"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return snapshot


def assess_test_start(db: Session, skill_id: int) -> dict:
    """判断当前 Skill 是否允许开始下一次测试。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"allowed": True}

    payload = memo.memo_payload or _empty_payload()
    test_history = payload.get("test_history", [])
    latest_test = test_history[-1] if test_history else None
    if not latest_test:
        return {"allowed": True}

    last_fingerprint = latest_test.get("artifact_fingerprint")
    if not last_fingerprint:
        return {"allowed": True}

    current_snapshot = _build_skill_artifact_snapshot(db, skill_id)
    if not current_snapshot:
        return {"allowed": True}

    post_test_diffs = payload.get("post_test_diffs", [])
    related_diffs = [
        item for item in post_test_diffs
        if item.get("after_test_id") == latest_test.get("id")
    ]

    if current_snapshot.get("fingerprint") != last_fingerprint or related_diffs:
        return {
            "allowed": True,
            "latest_test": latest_test,
            "current_snapshot": current_snapshot,
            "post_test_diffs": related_diffs,
        }

    source_label = str(latest_test.get("source") or "上次测试")
    version_label = latest_test.get("version")
    summary = str(latest_test.get("summary") or "").strip()
    message = f"{source_label} v{version_label or '?'} 后未检测到新的修改 diff"
    if summary:
        message += f"；上次结论：{summary}"
    message += "。如果问题没有变化，则不启动下一次测试。"
    return {
        "allowed": False,
        "reason": "unchanged_since_last_test",
        "message": message,
        "latest_test": latest_test,
        "current_snapshot": current_snapshot,
        "post_test_diffs": related_diffs,
    }


# ── 读取 ──────────────────────────────────────────────────────────────────────

def get_memo(db: Session, skill_id: int) -> dict | None:
    """返回完整 memo 视图，包含 current_task / next_task / persistent_notices / latest_test。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = memo.memo_payload or _empty_payload()
    tasks = payload.get("tasks", [])
    current_task_id = payload.get("current_task_id")

    current_task = None
    next_task = None
    for t in tasks:
        if t["id"] == current_task_id:
            current_task = t
        elif next_task is None and t["status"] == "todo" and _deps_done(t, tasks):
            next_task = t

    # 若 current_task 已完成但 current_task_id 没清，自动找下一个
    if current_task and current_task["status"] == "done":
        current_task = next_task
        next_task = None
        for t in tasks:
            if current_task and t["id"] != current_task["id"] and t["status"] == "todo" and _deps_done(t, tasks):
                next_task = t
                break

    notices = [n for n in payload.get("persistent_notices", []) if n.get("status") == "active"]

    test_history = payload.get("test_history", [])
    latest_test = test_history[-1] if test_history else None
    workflow_recovery = _get_workflow_recovery(payload)
    context_digest_cache = _normalize_context_digest_cache(payload)

    return {
        "skill_id": skill_id,
        "scenario_type": memo.scenario_type,
        "lifecycle_stage": memo.lifecycle_stage,
        "status_summary": memo.status_summary,
        "goal_summary": memo.goal_summary,
        "persistent_notices": notices,
        "current_task": current_task,
        "next_task": next_task,
        "latest_test": latest_test,
        "workflow_recovery": workflow_recovery,
        "context_digest_cache": context_digest_cache,
        "memo": payload,
    }


def update_context_digest_cache(
    db: Session,
    skill_id: int,
    cache_payload: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = False,
) -> dict | None:
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    normalized_cache = _normalize_context_digest_cache({"context_digest_cache": cache_payload})
    payload["context_digest_cache"] = normalized_cache
    if user_id is not None:
        memo.updated_by = user_id
    _save_memo_payload(db, memo, payload)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(memo)
    return get_memo(db, skill_id)


# ── 初始化 ────────────────────────────────────────────────────────────────────

def init_memo(
    db: Session,
    skill_id: int,
    scenario_type: str,
    goal_summary: str | None,
    user_id: int,
    force_rebuild: bool = False,
) -> dict:
    """按场景初始化或重建 memo。"""
    existing = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()

    if existing and not force_rebuild:
        # 不覆盖已有 memo，直接返回
        return get_memo(db, skill_id)  # type: ignore

    payload = _empty_payload()

    if scenario_type == "new_skill_creation":
        # 新建 Skill：生成默认任务树
        tasks = _build_new_skill_tasks(goal_summary)
        payload["tasks"] = tasks
        lifecycle = "planning"
        summary = "已创建任务计划，准备开始编辑。"
    elif scenario_type == "import_remediation":
        lifecycle = "analysis"
        summary = "导入分析中，请稍候。"
    elif scenario_type == "published_iteration":
        lifecycle = "editing"
        summary = "已发布 Skill 进入迭代模式。"
    else:
        lifecycle = "analysis"
        summary = ""

    if existing:
        existing.scenario_type = scenario_type
        existing.lifecycle_stage = lifecycle
        existing.status_summary = summary
        existing.goal_summary = goal_summary
        existing.memo_payload = payload
        existing.version = (existing.version or 1) + 1
        existing.updated_by = user_id
        db.commit()
    else:
        memo = SkillMemo(
            skill_id=skill_id,
            scenario_type=scenario_type,
            lifecycle_stage=lifecycle,
            status_summary=summary,
            goal_summary=goal_summary,
            memo_payload=payload,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(memo)
        db.commit()

    return get_memo(db, skill_id)  # type: ignore


def _build_new_skill_tasks(goal_summary: str | None) -> list[dict]:
    """为新建 Skill 生成默认任务树。"""
    tasks = []

    tasks.append({
        "id": _new_id("task"),
        "title": "明确 Skill 目标",
        "type": "define_goal",
        "status": "done" if goal_summary else "todo",
        "priority": "high",
        "source": "new_skill_init",
        "description": "明确这个 Skill 解决什么问题、面向谁。",
        "target_files": [],
        "acceptance_rule": {"mode": "manual"},
        "depends_on": [],
        "started_at": None,
        "completed_at": _now_iso() if goal_summary else None,
        "completed_by": None,
        "result_summary": goal_summary,
    })

    edit_task_id = _new_id("task")
    tasks.append({
        "id": edit_task_id,
        "title": "编写主 SKILL.md",
        "type": "edit_skill_md",
        "status": "todo",
        "priority": "high",
        "source": "new_skill_init",
        "description": "编写完整的 system prompt 主文件。",
        "target_files": ["SKILL.md"],
        "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
        "depends_on": [tasks[0]["id"]],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    })

    tasks.append({
        "id": _new_id("task"),
        "title": "补充 example 文件",
        "type": "create_file",
        "status": "todo",
        "priority": "medium",
        "source": "new_skill_init",
        "description": "新增至少一个 example 文件，描述输入和期望输出。",
        "target_files": ["example-basic.md"],
        "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
        "depends_on": [edit_task_id],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    })

    tasks.append({
        "id": _new_id("task"),
        "title": "补充参考资料或知识库",
        "type": "create_file",
        "status": "todo",
        "priority": "low",
        "source": "new_skill_init",
        "description": "如有需要，补充 reference 或 knowledge-base 文件。",
        "target_files": ["reference-basic.md"],
        "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
        "depends_on": [edit_task_id],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    })

    tasks.append({
        "id": _new_id("task"),
        "title": "运行测试",
        "type": "run_test",
        "status": "todo",
        "priority": "high",
        "source": "new_skill_init",
        "description": "运行质量检测，验证 Skill 效果。",
        "target_files": [],
        "acceptance_rule": {"mode": "test_record_created"},
        "depends_on": [edit_task_id],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    })

    return tasks


# ── 导入分析 ──────────────────────────────────────────────────────────────────

def analyze_import(db: Session, skill_id: int, user_id: int) -> dict:
    """导入 Skill 后分析 SKILL.md 和目录树，写入 memo。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        return {"ok": False, "error": "Skill not found"}

    # 获取最新版 system_prompt
    latest_ver = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    system_prompt = latest_ver.system_prompt if latest_ver else ""
    source_files = skill.source_files or []

    # 构建目录树
    directory_tree = ["SKILL.md"] + [f.get("filename", "") for f in source_files if f.get("filename")]

    # 分析 prompt 摘要
    prompt_summary = system_prompt[:200] + ("..." if len(system_prompt) > 200 else "")

    # 按类别分组现有文件
    categories: dict[str, list[str]] = {}
    for f in source_files:
        cat = f.get("category", "other")
        categories.setdefault(cat, []).append(f.get("filename", ""))

    # 检测缺失项
    missing_items: list[dict] = []
    tasks: list[dict] = []
    notices: list[dict] = []

    # 检查 example
    if not categories.get("example"):
        task_id = _new_id("task")
        missing_items.append({
            "code": "missing_example",
            "label": "缺少 example 文件",
            "severity": "warning",
            "required_to_publish": False,
            "related_task_id": task_id,
        })
        notices.append({
            "id": f"notice_missing_example",
            "type": "missing_structure",
            "title": "缺少 example 文件",
            "message": "当前 Skill 缺少可用于演示输入输出的 example 文件。",
            "blocking": False,
            "status": "active",
            "related_task_ids": [task_id],
        })
        tasks.append({
            "id": task_id,
            "title": "补充 example 文件",
            "type": "create_file",
            "status": "todo",
            "priority": "high",
            "source": "import_analysis",
            "description": "补充至少一个 example 文件，说明输入输出示例。",
            "target_files": ["example-basic.md"],
            "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
            "depends_on": [],
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
        })

    # 检查 reference / knowledge-base
    has_ref = bool(categories.get("reference") or categories.get("knowledge-base"))
    if not has_ref and _REF_KEYWORDS.search(system_prompt):
        task_id = _new_id("task")
        missing_items.append({
            "code": "missing_reference",
            "label": "缺少参考资料/知识库文件",
            "severity": "warning",
            "required_to_publish": False,
            "related_task_id": task_id,
        })
        notices.append({
            "id": "notice_missing_reference",
            "type": "missing_structure",
            "title": "缺少参考资料/知识库文件",
            "message": "Skill 的 prompt 中提及了参考资料或知识，但未附带相关文件。",
            "blocking": False,
            "status": "active",
            "related_task_ids": [task_id],
        })
        tasks.append({
            "id": task_id,
            "title": "补充参考资料文件",
            "type": "create_file",
            "status": "todo",
            "priority": "medium",
            "source": "import_analysis",
            "description": "补充 reference 或 knowledge-base 文件。",
            "target_files": ["reference-basic.md"],
            "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
            "depends_on": [],
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
        })

    # 检查 template
    if not categories.get("template") and _TEMPLATE_KEYWORDS.search(system_prompt):
        task_id = _new_id("task")
        missing_items.append({
            "code": "missing_template",
            "label": "缺少输出模板文件",
            "severity": "info",
            "required_to_publish": False,
            "related_task_id": task_id,
        })
        notices.append({
            "id": "notice_missing_template",
            "type": "missing_structure",
            "title": "缺少输出模板文件",
            "message": "Skill 要求固定输出格式，但未附带模板文件。",
            "blocking": False,
            "status": "active",
            "related_task_ids": [task_id],
        })
        tasks.append({
            "id": task_id,
            "title": "补充模板文件",
            "type": "create_file",
            "status": "todo",
            "priority": "low",
            "source": "import_analysis",
            "description": "补充 template 文件以规范输出格式。",
            "target_files": ["template-basic.md"],
            "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
            "depends_on": [],
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
        })

    # 检查 tool
    has_tool = bool(categories.get("tool")) or bool(skill.bound_tools)
    if not has_tool and _TOOL_KEYWORDS.search(system_prompt):
        task_id = _new_id("task")
        missing_items.append({
            "code": "missing_tool",
            "label": "缺少工具绑定",
            "severity": "warning",
            "required_to_publish": False,
            "related_task_id": task_id,
        })
        notices.append({
            "id": "notice_missing_tool",
            "type": "missing_structure",
            "title": "缺少工具绑定",
            "message": "Skill 需要工具能力但未绑定任何工具。",
            "blocking": False,
            "status": "active",
            "related_task_ids": [task_id],
        })
        tasks.append({
            "id": task_id,
            "title": "绑定或创建工具",
            "type": "bind_tool",
            "status": "todo",
            "priority": "medium",
            "source": "import_analysis",
            "description": "为 Skill 绑定已有工具或创建新工具。",
            "target_files": [],
            "acceptance_rule": {"mode": "tool_bound"},
            "depends_on": [],
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
        })

    # 添加测试任务
    test_task_id = _new_id("task")
    tasks.append({
        "id": test_task_id,
        "title": "运行测试",
        "type": "run_test",
        "status": "todo",
        "priority": "high",
        "source": "import_analysis",
        "description": "运行质量检测，验证 Skill 效果。",
        "target_files": [],
        "acceptance_rule": {"mode": "test_record_created"},
        "depends_on": [],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    })

    # 构建 payload
    payload = _empty_payload()
    payload["package_analysis"] = {
        "skill_md_summary": prompt_summary,
        "directory_tree": directory_tree,
        "structure_template": {
            "required": ["SKILL.md"],
            "recommended": ["example", "reference", "knowledge-base", "template", "tool"],
        },
        "missing_items": missing_items,
    }
    payload["persistent_notices"] = notices
    payload["tasks"] = tasks

    # 写入/更新 memo
    existing = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    lifecycle = "editing" if tasks else "awaiting_test"
    summary = f"导入分析完成，发现 {len(missing_items)} 项缺失。" if missing_items else "导入分析完成，结构完整。"

    if existing:
        existing.scenario_type = "import_remediation"
        existing.lifecycle_stage = lifecycle
        existing.status_summary = summary
        existing.memo_payload = payload
        existing.version = (existing.version or 1) + 1
        existing.updated_by = user_id
    else:
        memo = SkillMemo(
            skill_id=skill_id,
            scenario_type="import_remediation",
            lifecycle_stage=lifecycle,
            status_summary=summary,
            memo_payload=payload,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(memo)

    db.commit()

    analysis = {
        "skill_md_summary": prompt_summary,
        "directory_tree": directory_tree,
        "missing_items": [{"code": m["code"], "label": m["label"], "severity": m["severity"]} for m in missing_items],
    }

    return {
        "ok": True,
        "analysis": analysis,
        "memo": get_memo(db, skill_id),
    }


# ── 任务推进 ──────────────────────────────────────────────────────────────────

def start_task(db: Session, skill_id: int, task_id: str, user_id: int) -> dict:
    """用户选择"开始完善"时调用。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or {})
    tasks = payload.get("tasks", [])

    target_task = None
    for t in tasks:
        if t["id"] == task_id:
            target_task = t
            break

    if not target_task:
        return {"ok": False, "error": "Task not found"}

    target_task["status"] = _validate_task_status("in_progress")
    target_task["started_at"] = _now_iso()
    payload["current_task_id"] = task_id

    # 如果是第一次开始任务，lifecycle 应该在 editing
    if memo.lifecycle_stage in ("analysis", "planning"):
        memo.lifecycle_stage = "editing"

    _save_memo_payload(db, memo, payload)
    memo.updated_by = user_id
    memo.status_summary = f"正在进行：{target_task['title']}"
    db.commit()

    # 构建 editor_target
    editor_target = None
    if target_task.get("target_files"):
        fname = target_task["target_files"][0]
        editor_target = {
            "mode": "open_or_create",
            "file_type": "asset" if fname != "SKILL.md" else "prompt",
            "filename": fname,
        }

    return {
        "ok": True,
        "current_task": target_task,
        "editor_target": editor_target,
    }


def complete_from_save(
    db: Session,
    skill_id: int,
    task_id: str,
    filename: str,
    file_type: str,
    content_size: int,
    content_hash: str | None = None,
    version_id: int | None = None,
) -> dict:
    """前端在文件保存成功后调用。检查是否命中当前任务的完成条件。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or {})
    tasks = payload.get("tasks", [])

    target_task = None
    for t in tasks:
        if t["id"] == task_id:
            target_task = t
            break

    if not target_task:
        return {"ok": True, "task_completed": False, "reason": "task_not_found"}

    # 检查是否命中目标文件
    target_files = target_task.get("target_files", [])

    # SKILL.md 特殊处理：prompt 文件保存也算
    if filename == "SKILL.md" or file_type == "prompt":
        matched = "SKILL.md" in target_files
    else:
        matched = filename in target_files

    if not matched:
        return {"ok": True, "task_completed": False, "reason": "saved_file_not_in_target_files", "memo": get_memo(db, skill_id)}

    # 检查 acceptance_rule
    rule = target_task.get("acceptance_rule", {})
    mode = rule.get("mode", "all_target_files_saved_nonempty")

    if mode == "all_target_files_saved_nonempty" and content_size <= 0:
        return {"ok": True, "task_completed": False, "reason": "file_empty", "memo": get_memo(db, skill_id)}

    # 标记任务完成
    target_task["status"] = "done"
    target_task["completed_at"] = _now_iso()
    target_task["result_summary"] = f"文件 {filename} 已保存"

    # 写入 progress_log
    log_entry = {
        "id": _new_id("log"),
        "task_id": task_id,
        "kind": "task_completed",
        "summary": f"已完成{target_task['title']}",
        "created_at": _now_iso(),
    }
    payload.setdefault("progress_log", []).append(log_entry)

    latest_test = (payload.get("test_history") or [])[-1] if payload.get("test_history") else None
    if latest_test:
        payload.setdefault("post_test_diffs", []).append({
            "id": _new_id("diff"),
            "after_test_id": latest_test.get("id"),
            "change_type": "file_save",
            "source": "editor_save",
            "filename": filename,
            "file_type": file_type,
            "content_hash": content_hash,
            "version_id": version_id,
            "content_size": content_size,
            "summary": f"{filename} 已保存",
            "auto_generated": False,
            "created_at": _now_iso(),
        })

    # 写入 context_rollups
    rollup_entry = {
        "id": _new_id("rollup"),
        "task_id": task_id,
        "summary": f"{target_task['title']}已经完成",
        "created_at": _now_iso(),
    }
    payload.setdefault("context_rollups", []).append(rollup_entry)

    # 清除关联 notice
    related_notice_ids = set()
    for notice in payload.get("persistent_notices", []):
        if task_id in notice.get("related_task_ids", []):
            related_notice_ids.add(notice["id"])
    for notice in payload.get("persistent_notices", []):
        if notice["id"] in related_notice_ids:
            notice["status"] = "resolved"

    _sync_workflow_recovery_from_completed_tasks(payload, [task_id])

    # 选择下一个任务
    next_task = _pick_next_task(payload)
    payload["current_task_id"] = next_task["id"] if next_task else None

    # 推进生命周期
    _advance_lifecycle(memo, payload)

    _save_memo_payload(db, memo, payload)
    memo.status_summary = f"已完成{target_task['title']}。" + (f"下一步：{next_task['title']}" if next_task else "所有编辑任务已完成。")
    db.commit()

    # 构建 editor_target
    editor_target = None
    if next_task and next_task.get("target_files"):
        fname = next_task["target_files"][0]
        editor_target = {
            "mode": "open_or_create",
            "file_type": "asset" if fname != "SKILL.md" else "prompt",
            "filename": fname,
        }

    return {
        "ok": True,
        "task_completed": True,
        "completed_task_id": task_id,
        "rollup": rollup_entry["summary"],
        "current_task": next_task,
        "editor_target": editor_target,
        "memo": get_memo(db, skill_id),
    }


def resolve_tool_bound_tasks(db: Session, skill_id: int) -> bool:
    """绑定 Tool 后自动完成 memo 中 acceptance_rule.mode == 'tool_bound' 的任务并清除关联 notice。

    返回 True 表示有任务被完成。
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return False

    payload = copy.deepcopy(memo.memo_payload or {})
    tasks = payload.get("tasks", [])
    completed_any = False

    for task in tasks:
        rule = task.get("acceptance_rule", {})
        if rule.get("mode") == "tool_bound" and task["status"] != "done":
            task["status"] = "done"
            task["completed_at"] = _now_iso()
            task["completed_by"] = "system"
            task["result_summary"] = "工具已绑定"
            completed_any = True

            task_id = task["id"]

            payload.setdefault("progress_log", []).append({
                "id": _new_id("log"),
                "task_id": task_id,
                "kind": "task_completed",
                "summary": f"已完成{task['title']}（工具绑定触发）",
                "created_at": _now_iso(),
            })

            # 清除关联 notice
            for notice in payload.get("persistent_notices", []):
                if task_id in notice.get("related_task_ids", []):
                    notice["status"] = "resolved"

    if not completed_any:
        return False

    completed_task_ids = [
        str(task["id"])
        for task in tasks
        if task.get("status") == "done" and task.get("completed_by") == "system"
    ]
    _sync_workflow_recovery_from_completed_tasks(payload, completed_task_ids)

    # 选择下一个任务 + 推进生命周期
    next_task = _pick_next_task(payload)
    payload["current_task_id"] = next_task["id"] if next_task else None
    _advance_lifecycle(memo, payload)

    _save_memo_payload(db, memo, payload)
    memo.status_summary = "工具已绑定。" + (f"下一步：{next_task['title']}" if next_task else "所有编辑任务已完成。")
    return True


def direct_test(db: Session, skill_id: int, user_id: int) -> dict:
    """用户点击"无需完善直接提交测试"。不清除提醒。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or {})

    # 记录决策日志
    payload.setdefault("progress_log", []).append({
        "id": _new_id("log"),
        "task_id": None,
        "kind": "direct_test_decision",
        "summary": "用户选择直接提交测试，跳过当前待办",
        "created_at": _now_iso(),
    })

    memo.lifecycle_stage = "testing"
    _save_memo_payload(db, memo, payload)
    memo.status_summary = "已进入测试流程。"
    memo.updated_by = user_id
    db.commit()

    return {
        "ok": True,
        "lifecycle_stage": "testing",
        "notices_remain": True,
    }


# ── 测试结果 ──────────────────────────────────────────────────────────────────

def record_test_result(
    db: Session,
    skill_id: int,
    source: str,
    version: int,
    status: str,
    summary: str,
    details: dict | None = None,
    suggested_followups: list[dict] | None = None,
    user_id: int | None = None,
    structured_issues: list[dict] | None = None,
    structured_fix_plan: list[dict] | None = None,
    source_report_id: int | None = None,
    approval_eligible: bool | None = None,
    blocking_reasons: list[str] | None = None,
    source_report_knowledge_id: int | None = None,
    source_report_knowledge_title: str | None = None,
) -> dict:
    """测试流程结束后统一回写 memo。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        init_memo(db, skill_id, "published_iteration", None, user_id or 0)
        memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
        if not memo:
            return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())

    # 写入 test_history
    record_details = details or {}
    if approval_eligible is not None:
        record_details["approval_eligible"] = approval_eligible
    if blocking_reasons:
        record_details["blocking_reasons"] = blocking_reasons
    if source_report_knowledge_id is not None or source_report_knowledge_title:
        record_details["report_knowledge"] = {
            "knowledge_entry_id": source_report_knowledge_id,
            "title": source_report_knowledge_title,
        }

    artifact_snapshot = _build_skill_artifact_snapshot(db, skill_id)

    test_record = {
        "id": _new_id("test"),
        "source": source,
        "version": version,
        "status": status,
        "summary": summary,
        "details": record_details,
        "created_at": _now_iso(),
        "followup_task_ids": [],
        "source_report_id": source_report_id,
        "source_report_knowledge_id": source_report_knowledge_id,
        "source_report_knowledge_title": source_report_knowledge_title,
        "artifact_fingerprint": artifact_snapshot.get("fingerprint") if artifact_snapshot else None,
        "artifact_snapshot": artifact_snapshot,
    }

    generated_task_ids = []

    if status == "failed":
        # 清除旧报告产生的未完成 fix 任务，避免"整改后仍提示同一整改计划"
        if source_report_id:
            existing_tasks = payload.get("tasks", [])
            for t in existing_tasks:
                if (
                    t.get("status") in ("todo", "in_progress")
                    and t.get("source") == "test_failure"
                    and t.get("source_report_id") is not None
                    and t.get("source_report_id") != source_report_id
                ):
                    t["status"] = "superseded"
                    t["result_summary"] = "已被新一轮测试报告替代"

        # 优先使用结构化 fix_plan 生成精细任务
        if structured_fix_plan:
            for fp_item in structured_fix_plan:
                task_id = _new_id("task")
                generated_task_ids.append(task_id)

                # 映射 action_type 到 task type
                action_type = fp_item.get("action_type", "fix_after_test")
                task_type = action_type if action_type in VALID_TASK_TYPES else "fix_after_test"

                payload.setdefault("tasks", []).append({
                    "id": task_id,
                    "title": fp_item.get("title", "修复测试问题")[:200],
                    "type": task_type,
                    "status": "todo",
                    "priority": "high" if fp_item.get("priority") == "p0" else "medium" if fp_item.get("priority") == "p1" else "low",
                    "source": "test_failure",
                    "description": fp_item.get("suggested_changes", summary),
                    "target_files": [],
                    "acceptance_rule": {"mode": "custom", "text": fp_item.get("acceptance_rule", "")},
                    "depends_on": [],
                    "started_at": None,
                    "completed_at": None,
                    "completed_by": None,
                    "result_summary": None,
                    # 结构化整改字段
                    "problem_refs": fp_item.get("problem_ids", []),
                    "target_kind": fp_item.get("target_kind", "unknown"),
                    "target_ref": fp_item.get("target_ref", ""),
                    "retest_scope": fp_item.get("retest_scope", []),
                    "acceptance_rule_text": fp_item.get("acceptance_rule", ""),
                    "source_report_id": source_report_id,
                })

        elif suggested_followups:
            # fallback: 旧逻辑
            for followup in suggested_followups:
                task_id = _new_id("task")
                generated_task_ids.append(task_id)
                payload.setdefault("tasks", []).append({
                    "id": task_id,
                    "title": followup.get("title", "修复测试问题"),
                    "type": followup.get("type", "fix_after_test"),
                    "status": "todo",
                    "priority": "high",
                    "source": "test_failure",
                    "description": summary,
                    "target_files": followup.get("target_files", []),
                    "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
                    "depends_on": [],
                    "started_at": None,
                    "completed_at": None,
                    "completed_by": None,
                    "result_summary": None,
                })

        test_record["followup_task_ids"] = generated_task_ids
        memo.lifecycle_stage = "fixing"
        memo.status_summary = f"测试未通过：{summary}"

        # 写入 persistent_notices
        payload.setdefault("persistent_notices", [])
        # 清除旧的测试相关 notice
        payload["persistent_notices"] = [
            n for n in payload["persistent_notices"]
            if n.get("source") != "sandbox_test"
        ]
        payload["persistent_notices"].append({
            "id": _new_id("notice"),
            "title": "沙盒测试未通过，请按整改计划逐项修复",
            "level": "warning",
            "source": "sandbox_test",
            "created_at": _now_iso(),
            "dismissible": False,
        })
    elif status == "passed":
        # 标记 run_test 类型任务为完成
        for t in payload.get("tasks", []):
            if t["type"] == "run_test" and t["status"] in ("todo", "in_progress"):
                t["status"] = "done"
                t["completed_at"] = _now_iso()
                t["result_summary"] = summary

        # 检查是否还有未完成的非测试任务
        has_open = any(
            t["status"] in ("todo", "in_progress") and t["type"] not in ("run_test",)
            for t in payload.get("tasks", [])
        )
        if source == "preflight":
            if has_open:
                memo.lifecycle_stage = "editing"
                memo.status_summary = "Preflight 通过，但仍有待办任务。"
            else:
                memo.lifecycle_stage = "awaiting_test"
                memo.status_summary = "Preflight 通过，建议继续运行沙盒测试。"
        elif has_open:
            memo.lifecycle_stage = "editing"
            memo.status_summary = "测试通过，但仍有待办任务。"
        else:
            memo.lifecycle_stage = "ready_to_submit"
            memo.status_summary = "测试通过，可以提交审核。"

    payload.setdefault("test_history", []).append(test_record)

    # 更新 current_task_id
    if generated_task_ids:
        payload["current_task_id"] = generated_task_ids[0]

    _sync_workflow_recovery_after_test_result(
        payload,
        source=source,
        status=status,
        approval_eligible=approval_eligible,
        summary=summary,
        source_report_id=source_report_id,
    )

    _save_memo_payload(db, memo, payload)
    if user_id:
        memo.updated_by = user_id
    db.commit()

    return {
        "ok": True,
        "lifecycle_stage": memo.lifecycle_stage,
        "generated_task_ids": generated_task_ids,
        "memo": get_memo(db, skill_id),
    }


def record_post_test_diff(
    db: Session,
    skill_id: int,
    *,
    change_type: str,
    source: str,
    summary: str,
    user_id: int | None = None,
    filename: str | None = None,
    file_type: str | None = None,
    version_id: int | None = None,
    diff_ops: list[dict] | None = None,
    staged_edit_id: int | None = None,
    auto_generated: bool = False,
) -> dict:
    """将测试后的整改 diff 写入 memo，供下一轮测试前校验。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    latest_test = (payload.get("test_history") or [])[-1] if payload.get("test_history") else None
    if not latest_test:
        return {"ok": True, "skipped": True, "reason": "no_test_history"}

    entry = {
        "id": _new_id("diff"),
        "after_test_id": latest_test.get("id"),
        "change_type": change_type,
        "source": source,
        "summary": summary,
        "filename": filename,
        "file_type": file_type,
        "version_id": version_id,
        "diff_ops": diff_ops or [],
        "staged_edit_id": staged_edit_id,
        "auto_generated": auto_generated,
        "created_at": _now_iso(),
    }
    payload.setdefault("post_test_diffs", []).append(entry)
    _save_memo_payload(db, memo, payload)
    if user_id:
        memo.updated_by = user_id
    db.commit()

    return {"ok": True, "entry": entry}


def _remediation_task_target_files(task: dict[str, Any]) -> list[str]:
    target_kind = str(task.get("target_kind") or "").strip()
    target_ref = str(task.get("target_ref") or "").strip()
    if target_kind == "skill_prompt":
        return ["SKILL.md"]
    if target_kind == "source_file" and target_ref:
        return [target_ref]
    return []


def _remediation_priority(priority: str | None) -> str:
    if priority == "p0":
        return "high"
    if priority == "p1":
        return "medium"
    return "low"


def _ensure_remediation_memo(
    db: Session,
    skill_id: int,
    user_id: int | None = None,
) -> SkillMemo | None:
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if memo:
        return memo

    skill = db.get(Skill, skill_id)
    if not skill:
        return None

    actor_id = user_id or skill.created_by
    if not actor_id:
        logger.warning("sync_remediation_tasks skipped: skill=%s has no actor", skill_id)
        return None

    memo = SkillMemo(
        skill_id=skill_id,
        scenario_type="published_iteration",
        lifecycle_stage="fixing",
        status_summary="沙盒测试未通过，等待整改。",
        goal_summary=skill.description,
        memo_payload=_empty_payload(),
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(memo)
    db.flush()
    return memo


def sync_remediation_tasks(
    db: Session,
    skill_id: int,
    tasks: list[dict],
    source_report_id: int,
    user_id: int | None = None,
) -> None:
    """将 remediation agent 生成的任务清单写入 memo，替代旧报告任务。"""
    memo = _ensure_remediation_memo(db, skill_id, user_id=user_id)
    if not memo:
        return

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    existing_tasks = payload.setdefault("tasks", [])
    superseded_ids: set[str] = set()

    for task in existing_tasks:
        if (
            task.get("source") == "test_failure"
            and task.get("source_report_id") == source_report_id
            and task.get("status") in ("todo", "in_progress")
        ):
            task["status"] = "superseded"
            task["result_summary"] = "已由 remediation agent 生成的新整改计划替代"
            superseded_ids.add(str(task.get("id") or ""))

    generated_task_ids: list[str] = []
    fix_task_ids: list[str] = []

    for item in tasks:
        if not isinstance(item, dict):
            continue

        task_id = _new_id("task")
        action_type = item.get("action_type", "fix_after_test")
        task_type = action_type if action_type in VALID_TASK_TYPES else "fix_after_test"
        depends_on = list(fix_task_ids) if task_type == "run_targeted_retest" else []

        memo_task = {
            "id": task_id,
            "title": str(item.get("title") or "修复测试问题")[:200],
            "type": task_type,
            "status": "todo",
            "priority": _remediation_priority(item.get("priority")),
            "source": "test_failure",
            "description": item.get("suggested_changes") or item.get("acceptance_rule") or "",
            "target_files": _remediation_task_target_files(item),
            "acceptance_rule": {"mode": "custom", "text": item.get("acceptance_rule", "")},
            "depends_on": depends_on,
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
            "problem_refs": item.get("problem_ids", []),
            "target_kind": item.get("target_kind", "unknown"),
            "target_ref": item.get("target_ref", ""),
            "retest_scope": item.get("retest_scope", []),
            "acceptance_rule_text": item.get("acceptance_rule", ""),
            "source_report_id": source_report_id,
        }
        existing_tasks.append(memo_task)
        generated_task_ids.append(task_id)
        if task_type != "run_targeted_retest":
            fix_task_ids.append(task_id)

    for record in payload.get("test_history", []):
        if record.get("source_report_id") == source_report_id:
            record["followup_task_ids"] = generated_task_ids

    payload.setdefault("persistent_notices", [])
    payload["persistent_notices"] = [
        notice for notice in payload["persistent_notices"]
        if notice.get("source") != "sandbox_test"
    ]
    payload["persistent_notices"].append({
        "id": _new_id("notice"),
        "title": "沙盒测试未通过，请按 remediation agent 任务逐项修复",
        "level": "warning",
        "source": "sandbox_test",
        "created_at": _now_iso(),
        "dismissible": False,
        "status": "active",
        "related_task_ids": generated_task_ids,
    })

    current_task_id = payload.get("current_task_id")
    if current_task_id in superseded_ids or not current_task_id:
        payload["current_task_id"] = generated_task_ids[0] if generated_task_ids else None

    memo.lifecycle_stage = "fixing"
    memo.status_summary = "沙盒整改计划已同步，请按任务逐项修复。"
    if user_id:
        memo.updated_by = user_id
    elif memo.updated_by is None and memo.created_by is not None:
        memo.updated_by = memo.created_by

    _save_memo_payload(db, memo, payload)
    # 注意：不在此处 commit，由调用方（sandbox_governance）统一 commit，
    # 保证 memo tasks + staged_edits 在同一事务中。
    db.flush()


# ── Workflow Recovery ─────────────────────────────────────────────────────────

def sync_workflow_recovery(
    db: Session,
    skill_id: int,
    *,
    workflow_state: dict[str, Any],
    cards: list[dict[str, Any]] | None = None,
    staged_edits: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
    commit: bool = False,
) -> dict | None:
    """将当前 workflow state + cards + staged edits 持久化到 memo，供刷新恢复。"""
    memo = _ensure_workflow_memo(db, skill_id, workflow_state, user_id=user_id)
    if not memo:
        return None

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    workflow_recovery = {
        "workflow_state": workflow_state,
        "cards": list(cards or []),
        "staged_edits": list(staged_edits or []),
        "updated_at": _now_iso(),
    }
    _sync_import_audit_tasks_from_recovery(payload, workflow_recovery)
    _link_workflow_recovery_tasks(payload, workflow_recovery)
    payload["workflow_recovery"] = workflow_recovery

    next_stage = _workflow_lifecycle_stage(workflow_state)
    if memo.lifecycle_stage in {"analysis", "planning", "editing", "fixing"} or next_stage == "fixing":
        memo.lifecycle_stage = next_stage
    memo.status_summary = _workflow_status_summary(workflow_state)
    if user_id:
        memo.updated_by = user_id
    elif memo.updated_by is None and memo.created_by is not None:
        memo.updated_by = memo.created_by

    _save_memo_payload(db, memo, payload)
    if commit:
        db.commit()
    else:
        db.flush()
    return get_memo(db, skill_id)


def patch_workflow_recovery_action(
    db: Session,
    skill_id: int,
    *,
    card_id: str | None = None,
    staged_edit_id: str | None = None,
    updated_card_status: str | None = None,
    updated_staged_edit_status: str | None = None,
    user_id: int | None = None,
    commit: bool = False,
) -> dict | None:
    """回写 workflow recovery 中卡片 / staged edit 的最新状态。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    recovery = _get_workflow_recovery(payload)
    changed = _link_workflow_recovery_tasks(payload, recovery)
    normalized_staged_edit_id = str(staged_edit_id) if staged_edit_id is not None else None

    for edit in recovery["staged_edits"]:
        if not isinstance(edit, dict):
            continue
        if normalized_staged_edit_id is not None and str(edit.get("id")) == normalized_staged_edit_id:
            if updated_staged_edit_status and edit.get("status") != updated_staged_edit_status:
                edit["status"] = updated_staged_edit_status
                changed = True

    for card in recovery["cards"]:
        if not isinstance(card, dict):
            continue
        matches_card = card_id is not None and str(card.get("id")) == str(card_id)
        content = card.get("content") if isinstance(card.get("content"), dict) else {}
        matches_edit = normalized_staged_edit_id is not None and str(content.get("staged_edit_id") or "") == normalized_staged_edit_id
        if matches_card or matches_edit:
            if updated_card_status and card.get("status") != updated_card_status:
                card["status"] = updated_card_status
                changed = True

    if _sync_tasks_from_workflow_action(
        payload,
        card_id=card_id,
        staged_edit_id=normalized_staged_edit_id,
        updated_card_status=updated_card_status,
        updated_staged_edit_status=updated_staged_edit_status,
        user_id=user_id,
    ):
        changed = True

    if _refresh_workflow_recovery_state(payload):
        recovery = _get_workflow_recovery(payload)
        changed = True
    workflow_state = recovery.get("workflow_state") if isinstance(recovery.get("workflow_state"), dict) else None

    if not changed:
        return get_memo(db, skill_id)

    recovery["updated_at"] = _now_iso()
    payload["workflow_recovery"] = recovery
    if workflow_state:
        memo.status_summary = _workflow_status_summary(workflow_state)
    if user_id:
        memo.updated_by = user_id

    _save_memo_payload(db, memo, payload)
    if commit:
        db.commit()
    else:
        db.flush()
    return get_memo(db, skill_id)


# ── 反馈采纳 ──────────────────────────────────────────────────────────────────

def adopt_feedback(
    db: Session,
    skill_id: int,
    source_type: str,
    source_id: int,
    summary: str,
    task_blueprint: dict,
    user_id: int,
) -> dict:
    """采纳反馈并转成任务。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        # 自动创建 memo（已发布 Skill 可能没有 memo）
        init_memo(db, skill_id, "published_iteration", None, user_id)
        memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
        if not memo:
            return {"ok": False, "error": "Failed to create memo"}

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())

    task_id = _new_id("task")

    # 记录 adopted_feedback
    feedback_entry = {
        "id": _new_id("feedback"),
        "source_type": source_type,
        "source_id": source_id,
        "summary": summary,
        "adopted_at": _now_iso(),
        "generated_task_ids": [task_id],
    }
    payload.setdefault("adopted_feedback", []).append(feedback_entry)

    # 生成任务
    new_task = {
        "id": task_id,
        "title": task_blueprint.get("title", "处理反馈"),
        "type": task_blueprint.get("type", "adopt_feedback_change"),
        "status": "todo",
        "priority": "high",
        "source": "feedback",
        "description": summary,
        "target_files": task_blueprint.get("target_files", ["SKILL.md"]),
        "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
        "depends_on": [],
        "started_at": None,
        "completed_at": None,
        "completed_by": None,
        "result_summary": None,
    }
    payload.setdefault("tasks", []).append(new_task)

    # 如果没有当前任务，设为新任务
    if not payload.get("current_task_id"):
        payload["current_task_id"] = task_id

    # 切回 editing
    if memo.lifecycle_stage in ("ready_to_submit", "completed"):
        memo.lifecycle_stage = "editing"

    _save_memo_payload(db, memo, payload)
    memo.status_summary = f"已采纳反馈：{summary[:50]}"
    memo.updated_by = user_id
    db.commit()

    return {
        "ok": True,
        "generated_task_id": task_id,
        "current_task": new_task,
    }


# ── 长文本 Ingest 接入 ────────────────────────────────────────────────────────

# 文件角色到 memo category 的映射
_ROLE_TO_CATEGORY: dict[str, str] = {
    "input_definition": "reference",
    "knowledge": "knowledge-base",
    "reference": "reference",
    "example": "example",
}


def ingest_from_paste(
    db: Session,
    skill_id: int,
    user_intent: str,
    saved_files: list[dict],
    user_id: int | None = None,
) -> dict:
    """长文本 ingest 完成后接入 memo：推进任务、记录日志、更新 notices。

    saved_files 格式: [{"filename": "x.json", "block_type": "json-schema",
                         "suggested_role": "input_definition", "size": 1234}, ...]
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        # 没有 memo → 自动创建一个 published_iteration 类型的 memo
        init_memo(db, skill_id, "published_iteration", user_intent, user_id or 0)
        memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
        if not memo:
            return {"ok": False, "error": "Failed to create memo"}

    payload = copy.deepcopy(memo.memo_payload or _empty_payload())
    tasks = payload.get("tasks", [])
    completed_task_ids: list[str] = []
    filenames = [f["filename"] for f in saved_files]

    # ── 1. 尝试自动推进匹配的 create_file 任务 ──
    for task in tasks:
        if task["status"] not in ("todo", "in_progress"):
            continue
        if task["type"] not in ("create_file", "update_file"):
            continue
        # 检查 target_files 是否被 ingest 的文件满足
        target_files = task.get("target_files", [])
        if not target_files:
            continue

        # 精确匹配或 category 匹配
        task_cat = _infer_task_category(target_files)
        ingested_cats = {_ROLE_TO_CATEGORY.get(f.get("suggested_role", ""), "other") for f in saved_files}

        matched = any(tf in filenames for tf in target_files) or (task_cat and task_cat in ingested_cats)
        if matched:
            task["status"] = "done"
            task["completed_at"] = _now_iso()
            task["completed_by"] = "ingest_paste"
            task["result_summary"] = f"通过长文本粘贴自动完成，文件：{', '.join(filenames)}"
            completed_task_ids.append(task["id"])

            # 清除关联 notice
            for notice in payload.get("persistent_notices", []):
                if task["id"] in notice.get("related_task_ids", []):
                    notice["status"] = "resolved"

    # ── 2. 写入 progress_log ──
    file_list = "、".join(filenames)
    log_entry = {
        "id": _new_id("log"),
        "task_id": None,
        "kind": "ingest_paste",
        "summary": f"长文本粘贴：{user_intent}。存储文件：{file_list}",
        "created_at": _now_iso(),
    }
    payload.setdefault("progress_log", []).append(log_entry)

    # ── 3. 写入 context_rollups（供 Studio Agent 压缩上下文用） ──
    rollup_entry = {
        "id": _new_id("rollup"),
        "task_id": completed_task_ids[0] if completed_task_ids else None,
        "summary": f"用户通过粘贴提供了{len(saved_files)}个文件（{file_list}），意图：{user_intent}",
        "created_at": _now_iso(),
    }
    payload.setdefault("context_rollups", []).append(rollup_entry)

    # ── 4. 更新 package_analysis.directory_tree ──
    dir_tree = payload.get("package_analysis", {}).get("directory_tree", [])
    for fn in filenames:
        if fn not in dir_tree:
            dir_tree.append(fn)

    # ── 5. 推进 current_task 和 lifecycle ──
    if completed_task_ids:
        _sync_workflow_recovery_from_completed_tasks(payload, completed_task_ids)
        next_task = _pick_next_task(payload)
        payload["current_task_id"] = next_task["id"] if next_task else payload.get("current_task_id")
        _advance_lifecycle(memo, payload)

    # 更新 status_summary
    if completed_task_ids:
        done_titles = [t["title"] for t in tasks if t["id"] in completed_task_ids]
        memo.status_summary = f"长文本粘贴已自动完成：{'、'.join(done_titles)}。"
    else:
        memo.status_summary = f"已接收粘贴文件：{file_list}。"

    _save_memo_payload(db, memo, payload)
    if user_id:
        memo.updated_by = user_id
    db.commit()

    return {
        "ok": True,
        "completed_task_ids": completed_task_ids,
        "rollup": rollup_entry["summary"],
        "memo": get_memo(db, skill_id),
    }


def _infer_task_category(target_files: list[str]) -> str | None:
    """从任务 target_files 推断目标 category。"""
    for f in target_files:
        fl = f.lower()
        if "example" in fl:
            return "example"
        if "reference" in fl or "knowledge" in fl or "kb" in fl:
            return "reference"
        if "template" in fl:
            return "template"
    return None


# ── 辅助方法 ──────────────────────────────────────────────────────────────────

def _deps_done(task: dict, all_tasks: list[dict]) -> bool:
    """检查任务的所有依赖是否已完成。"""
    depends = task.get("depends_on", [])
    if not depends:
        return True
    done_ids = {t["id"] for t in all_tasks if t["status"] in ("done", "skipped")}
    return all(d in done_ids for d in depends)


def _pick_next_task(payload: dict) -> dict | None:
    """找第一个 todo 且依赖已完成的任务。"""
    tasks = payload.get("tasks", [])
    for t in tasks:
        if t["status"] == "todo" and _deps_done(t, tasks):
            return t
    return None


def _advance_lifecycle(memo: SkillMemo, payload: dict) -> None:
    """根据状态迁移表自动推进 lifecycle_stage。"""
    tasks = payload.get("tasks", [])

    # 检查是否所有非测试任务完成
    non_test_tasks = [t for t in tasks if t["type"] not in ("run_test",)]
    all_non_test_done = all(t["status"] in ("done", "skipped") for t in non_test_tasks) if non_test_tasks else True

    if memo.lifecycle_stage == "editing" and all_non_test_done:
        memo.lifecycle_stage = _validate_lifecycle("awaiting_test")
    elif memo.lifecycle_stage == "fixing":
        fix_tasks = [t for t in tasks if t["type"] == "fix_after_test"]
        all_fixes_done = all(t["status"] in ("done", "skipped") for t in fix_tasks) if fix_tasks else True
        if all_fixes_done:
            memo.lifecycle_stage = _validate_lifecycle("awaiting_test")
