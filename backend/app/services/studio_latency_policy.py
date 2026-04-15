"""Skill Studio latency policy helpers.

统一复杂度分级、执行策略、SLA 节点与初始 lane 状态，供 bootstrap 和主运行链共享。
"""
from __future__ import annotations

from typing import Any


def estimate_complexity_level(
    *,
    session_mode: str,
    workflow_mode: str,
    next_action: str,
    user_message: str,
    has_files: bool = False,
    has_memo: bool = False,
    history_count: int = 0,
) -> str:
    score = 0
    text = (user_message or "").strip()

    if workflow_mode == "architect_mode":
        score += 2
    if next_action == "run_audit":
        score += 3
    if session_mode == "audit_imported_skill":
        score += 2
    if session_mode == "create_new_skill":
        score += 1
    if has_files:
        score += 1
    if has_memo:
        score += 1
    if history_count >= 12:
        score += 1
    if len(text) >= 180:
        score += 1

    heavy_keywords = (
        "审计", "重构", "整改", "导入", "完整", "系统性", "全面", "全量", "修复",
        "sandbox", "preflight", "governance", "workflow", "架构", "方案",
    )
    if any(keyword.lower() in text.lower() for keyword in heavy_keywords):
        score += 1

    if score >= 4:
        return "high"
    if score >= 1:
        return "medium"
    return "simple"


def choose_execution_strategy(
    *,
    complexity_level: str,
    workflow_mode: str,
    next_action: str,
) -> str:
    if next_action == "review_cards":
        return "deep_resume"
    if complexity_level == "simple" and workflow_mode != "architect_mode":
        return "fast_only"
    return "fast_then_deep"


def initial_lane_statuses(execution_strategy: str) -> dict[str, str]:
    if execution_strategy == "fast_only":
        return {"fast_status": "pending", "deep_status": "not_requested"}
    if execution_strategy == "deep_resume":
        return {"fast_status": "pending", "deep_status": "pending"}
    return {"fast_status": "pending", "deep_status": "pending"}


def merge_latency_metadata_fields(
    metadata: dict[str, Any] | None,
    updates: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(metadata or {})
    latency = dict(base.get("latency") or {})
    for key, value in (updates or {}).items():
        if value:
            latency[key] = value
    if latency:
        base["latency"] = latency
    return base


def merge_latency_metadata(
    metadata: dict[str, Any] | None,
    *,
    accepted_at: str | None = None,
    classified_at: str | None = None,
    context_ready_at: str | None = None,
    fast_started_at: str | None = None,
    first_token_at: str | None = None,
    first_useful_response_at: str | None = None,
    deep_started_at: str | None = None,
    deep_completed_at: str | None = None,
    run_completed_at: str | None = None,
    sla_checkpoint_at: str | None = None,
    sla_degraded_at: str | None = None,
    two_stage_forced_at: str | None = None,
) -> dict[str, Any]:
    return merge_latency_metadata_fields(
        metadata,
        {
            "request_accepted_at": accepted_at,
            "classified_at": classified_at,
            "context_ready_at": context_ready_at,
            "fast_started_at": fast_started_at,
            "first_token_at": first_token_at,
            "first_useful_response_at": first_useful_response_at,
            "deep_started_at": deep_started_at,
            "deep_completed_at": deep_completed_at,
            "run_completed_at": run_completed_at,
            "sla_checkpoint_at": sla_checkpoint_at,
            "sla_degraded_at": sla_degraded_at,
            "two_stage_forced_at": two_stage_forced_at,
        },
    )


def build_sla_policy(
    *,
    complexity_level: str,
    execution_strategy: str,
    sla_degrade_enabled: bool = True,
) -> dict[str, Any]:
    enabled = bool(sla_degrade_enabled and complexity_level in {"medium", "high"})
    probe_after_s: int | None = None
    degrade_after_s: int | None = None
    force_two_stage_after_s: int | None = None
    deadline_after_s: int | None = None

    if complexity_level == "medium":
        probe_after_s = 10
        degrade_after_s = 20
        deadline_after_s = 30
    elif complexity_level == "high":
        probe_after_s = 15
        force_two_stage_after_s = 35 if execution_strategy != "fast_only" else None
        deadline_after_s = 60

    return {
        "enabled": enabled,
        "complexity_level": complexity_level,
        "execution_strategy": execution_strategy,
        "probe_after_s": probe_after_s if enabled else None,
        "degrade_after_s": degrade_after_s if enabled else None,
        "force_two_stage_after_s": force_two_stage_after_s if enabled else None,
        "deadline_after_s": deadline_after_s if enabled else None,
        "two_stage_expected": execution_strategy in {"fast_then_deep", "deep_resume"},
    }
