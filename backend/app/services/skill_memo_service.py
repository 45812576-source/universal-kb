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
        "adopted_feedback": [],
        "context_rollups": [],
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
        "memo": payload,
    }


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
) -> dict:
    """测试流程结束后统一回写 memo。"""
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return {"ok": False, "error": "Memo not found"}

    payload = copy.deepcopy(memo.memo_payload or {})

    # 写入 test_history
    record_details = details or {}
    if approval_eligible is not None:
        record_details["approval_eligible"] = approval_eligible
    if blocking_reasons:
        record_details["blocking_reasons"] = blocking_reasons

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
        if has_open:
            memo.lifecycle_stage = "editing"
            memo.status_summary = "测试通过，但仍有待办任务。"
        else:
            memo.lifecycle_stage = "ready_to_submit"
            memo.status_summary = "测试通过，可以提交审核。"

    payload.setdefault("test_history", []).append(test_record)

    # 更新 current_task_id
    if generated_task_ids:
        payload["current_task_id"] = generated_task_ids[0]

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
