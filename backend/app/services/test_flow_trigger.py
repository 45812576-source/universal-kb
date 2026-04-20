"""测试流入口解析 — 从 le-desk test-flow-service.ts:resolveEntry 1:1 下沉。"""
import re
from typing import Any

from sqlalchemy.orm import Session

from app.services.skill_governance_service import (
    latest_case_plan,
    serialize_case_plan,
)
from app.services.test_flow_readiness import check_readiness

# 复用前端同一正则（中英文意图匹配）
GENERATE_CASE_INTENT_PATTERN = re.compile(
    r"(生成|产出|输出|给我|帮我).{0,12}(测试用例|测试集|case|cases)",
    re.IGNORECASE,
)


def has_generate_case_intent(content: str) -> bool:
    return bool(GENERATE_CASE_INTENT_PATTERN.search(content))


def _normalize_candidates(
    mentioned_skill_ids: list[int] | None,
) -> list[int]:
    """只使用显式 @ 提及的 skill_ids，不隐式合并 selected_skill_id。"""
    merged = list(mentioned_skill_ids or [])
    merged = [sid for sid in merged if isinstance(sid, int) and sid > 0]
    return list(dict.fromkeys(merged))  # 去重保序


def _summarize_plan(plan_dict: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan_dict:
        return None
    materialization = plan_dict.get("materialization")
    return {
        "id": plan_dict.get("id"),
        "skill_id": plan_dict.get("skill_id"),
        "plan_version": plan_dict.get("plan_version"),
        "status": plan_dict.get("status"),
        "case_count": plan_dict.get("case_count"),
        "focus_mode": plan_dict.get("focus_mode"),
        "materialized_session_id": materialization.get("sandbox_session_id") if materialization else None,
    }


def resolve_test_flow_entry(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """解析测试流入口，返回 action 指令。

    Payload 字段：
    - content: str — 用户输入
    - selected_skill_id: int | None
    - mentioned_skill_ids: list[int]
    - candidate_skills: list[{id, name, status?}]
    - entry_source: str
    - conversation_id: int | None
    """
    content = payload.get("content", "")

    # 1) 无意图 → 透传到默认对话
    if not has_generate_case_intent(content):
        return {"action": "chat_default", "reason": "missing_generate_case_intent"}

    # 2) 只使用显式 @ 提及的 skill — 不隐式合并 selected_skill_id
    matched_skill_ids = _normalize_candidates(
        payload.get("mentioned_skill_ids"),
    )
    candidates_map = {
        s["id"]: s for s in (payload.get("candidate_skills") or [])
    }

    if not matched_skill_ids:
        return {"action": "chat_default", "reason": "missing_skill_target"}

    # 3) 多匹配 → 让用户选
    if len(matched_skill_ids) > 1:
        return {
            "action": "pick_skill",
            "reason": "multiple_skill_targets",
            "candidates": [
                candidates_map.get(sid, {"id": sid, "name": f"Skill #{sid}"})
                for sid in matched_skill_ids
            ],
        }

    # 4) 单匹配 → 检查就绪性
    skill_id = matched_skill_ids[0]
    readiness = check_readiness(db, skill_id)
    skill_info = candidates_map.get(skill_id, {"id": skill_id, "name": f"Skill #{skill_id}"})

    if not readiness.get("ready"):
        plan = latest_case_plan(db, skill_id)
        return {
            "action": "mount_blocked",
            "reason": "skill_mount_not_ready",
            "skill": skill_info,
            "blocking_issues": readiness.get("blocking_issues", ["missing_permission_mount"]),
            "mount_cta": readiness.get("mount_cta"),
            "latest_plan": _summarize_plan(serialize_case_plan(plan)),
        }

    # 5) ready → 检查是否有历史 plan
    plan = latest_case_plan(db, skill_id)
    if plan and plan.id:
        return {
            "action": "choose_existing_plan",
            "reason": "existing_case_plan_found",
            "skill": skill_info,
            "latest_plan": _summarize_plan(serialize_case_plan(plan)),
        }

    # 6) ready + 无历史 plan → 直接生成
    return {
        "action": "generate_cases",
        "reason": "ready_without_existing_plan",
        "skill": skill_info,
    }
