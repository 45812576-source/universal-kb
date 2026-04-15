"""Skill Studio rollout dashboard and export helpers."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.event_bus import UnifiedEvent
from app.services.studio_runs import studio_run_registry

_RUN_EVENT_TYPES = {
    "harness.run.created",
    "harness.run.metadata_updated",
    "harness.run.status_changed",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(start_value: Any, end_value: Any) -> float | None:
    start_dt = _parse_iso(start_value)
    end_dt = _parse_iso(end_value)
    if not start_dt or not end_dt:
        return None
    duration = (end_dt - start_dt).total_seconds()
    return duration if duration >= 0 else None


def _percentile(values: list[float], ratio: float) -> float | None:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * ratio
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _merge_metadata(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_skill_studio_run_records(
    db: Session,
    *,
    since_days: int = 7,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    since = _now_utc() - timedelta(days=max(since_days, 1))
    events = (
        db.query(UnifiedEvent)
        .filter(
            UnifiedEvent.source_type == "harness",
            UnifiedEvent.event_type.in_(_RUN_EVENT_TYPES),
            UnifiedEvent.created_at >= since,
        )
        .order_by(UnifiedEvent.created_at.asc(), UnifiedEvent.id.asc())
        .limit(max(limit, 1) * 5)
        .all()
    )

    records: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            continue
        record = records.setdefault(run_id, {
            "run_id": run_id,
            "agent_type": None,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "user_id": event.user_id,
            "workspace_id": event.workspace_id,
            "status": None,
            "error": None,
            "metadata": {},
        })
        if event.event_type == "harness.run.created":
            record["agent_type"] = payload.get("agent_type")
            record["created_at"] = event.created_at.isoformat() if event.created_at else record["created_at"]
        elif event.event_type == "harness.run.metadata_updated":
            metadata_patch = payload.get("metadata_patch")
            if isinstance(metadata_patch, dict):
                record["metadata"] = _merge_metadata(record["metadata"], metadata_patch)
        elif event.event_type == "harness.run.status_changed":
            record["status"] = payload.get("status")
            record["error"] = payload.get("error")

    filtered = [record for record in records.values() if record.get("agent_type") == "skill_studio"]
    filtered.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return filtered[: max(limit, 1)]


def build_studio_rollout_dashboard(
    db: Session,
    *,
    since_days: int = 7,
    limit: int = 1000,
) -> dict[str, Any]:
    records = _load_skill_studio_run_records(db, since_days=since_days, limit=limit)

    first_useful_latencies: list[float] = []
    deep_completed_latencies: list[float] = []
    first_token_latencies: list[float] = []
    status_counts: dict[str, int] = {}
    deep_completed_count = 0
    first_useful_count = 0
    deep_missing_count = 0

    for record in records:
        status = str(record.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        latency = metadata.get("latency") if isinstance(metadata.get("latency"), dict) else {}
        first_useful = _duration_seconds(latency.get("request_accepted_at"), latency.get("first_useful_response_at"))
        deep_completed = _duration_seconds(latency.get("request_accepted_at"), latency.get("deep_completed_at"))
        first_token = _duration_seconds(latency.get("fast_started_at"), latency.get("first_token_at"))
        if first_useful is not None:
            first_useful_count += 1
            first_useful_latencies.append(first_useful)
        if deep_completed is not None:
            deep_completed_count += 1
            deep_completed_latencies.append(deep_completed)
        elif latency.get("deep_started_at"):
            deep_missing_count += 1
        if first_token is not None:
            first_token_latencies.append(first_token)

    snapshot = studio_run_registry.metrics_snapshot()
    total_runs = len(records)
    completed_runs = status_counts.get("completed", 0)
    failed_runs = status_counts.get("failed", 0)

    return {
        "window_days": since_days,
        "run_count": total_runs,
        "status_counts": status_counts,
        "first_useful_response": {
            "count": first_useful_count,
            "p50_s": _percentile(first_useful_latencies, 0.50),
            "p75_s": _percentile(first_useful_latencies, 0.75),
            "p90_s": _percentile(first_useful_latencies, 0.90),
        },
        "deep_completed": {
            "count": deep_completed_count,
            "missing_after_start": deep_missing_count,
            "p50_s": _percentile(deep_completed_latencies, 0.50),
            "p75_s": _percentile(deep_completed_latencies, 0.75),
            "p90_s": _percentile(deep_completed_latencies, 0.90),
        },
        "first_token": {
            "count": len(first_token_latencies),
            "p50_s": _percentile(first_token_latencies, 0.50),
            "p75_s": _percentile(first_token_latencies, 0.75),
            "p90_s": _percentile(first_token_latencies, 0.90),
        },
        "quality_proxy": {
            "deep_completion_rate": round((deep_completed_count / total_runs), 4) if total_runs else None,
            "run_failure_rate": round((failed_runs / total_runs), 4) if total_runs else None,
            "completion_rate": round((completed_runs / total_runs), 4) if total_runs else None,
        },
        "runtime_snapshot": snapshot,
        "records": records,
    }


def export_studio_rollout_dashboard_csv(
    db: Session,
    *,
    since_days: int = 7,
    limit: int = 1000,
) -> str:
    dashboard = build_studio_rollout_dashboard(db, since_days=since_days, limit=limit)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "run_id",
            "created_at",
            "status",
            "user_id",
            "workspace_id",
            "first_useful_response_s",
            "deep_completed_s",
            "first_token_s",
            "request_accepted_at",
            "first_useful_response_at",
            "deep_completed_at",
            "run_completed_at",
            "error",
        ],
    )
    writer.writeheader()
    for record in dashboard["records"]:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        latency = metadata.get("latency") if isinstance(metadata.get("latency"), dict) else {}
        writer.writerow({
            "run_id": record.get("run_id"),
            "created_at": record.get("created_at"),
            "status": record.get("status"),
            "user_id": record.get("user_id"),
            "workspace_id": record.get("workspace_id"),
            "first_useful_response_s": _duration_seconds(latency.get("request_accepted_at"), latency.get("first_useful_response_at")),
            "deep_completed_s": _duration_seconds(latency.get("request_accepted_at"), latency.get("deep_completed_at")),
            "first_token_s": _duration_seconds(latency.get("fast_started_at"), latency.get("first_token_at")),
            "request_accepted_at": latency.get("request_accepted_at"),
            "first_useful_response_at": latency.get("first_useful_response_at"),
            "deep_completed_at": latency.get("deep_completed_at"),
            "run_completed_at": latency.get("run_completed_at"),
            "error": record.get("error"),
        })
    return output.getvalue()
