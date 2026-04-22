"""Chaos and recovery gates for Skill Studio Harness Engineering."""
from __future__ import annotations

import json
import os
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from .conftest import EvidenceRecorder, HarnessApiClient, HarnessE2EConfig


pytestmark = pytest.mark.skipif(
    os.getenv("HARNESS_ENGINEERING_E2E", "").lower() not in {"1", "true", "yes", "on"},
    reason="Set HARNESS_ENGINEERING_E2E=1 to run live Harness Engineering chaos gates",
)


def _patch_keys(events: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    keys: list[tuple[str, int, str]] = []
    for event in events:
        if event.get("event") != "patch_applied":
            continue
        data = event.get("data") or {}
        keys.append((str(data.get("run_id")), int(data.get("patch_seq") or 0), str(data.get("patch_type") or "")))
    return keys


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError(f"payload_json must decode to dict, got {type(value).__name__}")


def _start_reference_run(
    harness_client: HarnessApiClient,
    config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
    name: str,
) -> str:
    run_id, events = harness_client.stream_studio_message(
        config.conversation_id,
        skill_id=config.skill_id,
        content=(
            f"Harness Engineering chaos reference run {name}: emit route, queue, patch, "
            "and at least one recoverable artifact event."
        ),
    )
    assert run_id
    evidence.write_sse(f"{name}-live.sse", events)
    return run_id


def test_duplicate_replay_is_idempotent_and_patch_keys_are_stable(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    run_id = _start_reference_run(harness_client, harness_e2e_config, evidence, "duplicate-replay")

    first = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    second = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    evidence.write_sse("01-first-replay.sse", first)
    evidence.write_sse("02-second-replay.sse", second)

    assert [event["event"] for event in first] == [event["event"] for event in second]
    assert _patch_keys(first) == _patch_keys(second)
    assert len(_patch_keys(first)) == len(set(_patch_keys(first))), "duplicate patches must not appear in one replay"


def test_after_sequence_gap_recovery_matches_full_replay_suffix(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    run_id = _start_reference_run(harness_client, harness_e2e_config, evidence, "gap-recovery")
    full = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    assert len(full) >= 4, "gap recovery needs at least four events to prove suffix replay"

    split = max(1, len(full) // 2)
    tail = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=split)
    evidence.write_sse("01-full-replay.sse", full)
    evidence.write_sse("02-tail-replay.sse", tail)

    assert [event["event"] for event in tail] == [event["event"] for event in full[split:]]
    assert _patch_keys(tail) == _patch_keys(full[split:])


def test_db_event_log_has_monotonic_sequence_and_idempotency_keys(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    if not harness_e2e_config.database_url:
        pytest.skip("Set HARNESS_E2E_DATABASE_URL to validate agent_run_events directly")

    run_id = _start_reference_run(harness_client, harness_e2e_config, evidence, "db-event-log")
    engine = create_engine(harness_e2e_config.database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                select sequence, event_type, patch_type, payload_json, idempotency_key, run_version, harness_run_id
                from agent_run_events
                where public_run_id = :run_id
                order by sequence asc
                """
            ),
            {"run_id": run_id},
        ).mappings().all()

    evidence.write_json("01-agent-run-events.json", [dict(row) for row in rows])
    assert rows, "agent_run_events must persist every run event"

    sequences = [int(row["sequence"]) for row in rows]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences)), "sequence must be unique within a public_run_id"

    idem_keys = [row["idempotency_key"] for row in rows]
    assert all(idem_keys), "every event needs an idempotency_key"
    assert len(idem_keys) == len(set(idem_keys)), "idempotency_key must prevent duplicate event append"

    patch_rows = [row for row in rows if row["event_type"] == "patch_applied"]
    assert patch_rows, "patch_applied rows are required for canonical UI replay"
    for row in patch_rows:
        payload = _json_payload(row["payload_json"])
        assert payload["run_id"] == run_id
        assert payload["run_version"] == row["run_version"]
        assert payload["payload"]["run_id"] == run_id
        assert payload["payload"]["run_version"] == row["run_version"]


def test_old_run_late_replay_does_not_change_new_active_run(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    old_run = _start_reference_run(harness_client, harness_e2e_config, evidence, "old-run")
    new_run = _start_reference_run(harness_client, harness_e2e_config, evidence, "new-run")
    assert old_run != new_run

    old_replay = harness_client.stream_run_events(harness_e2e_config.conversation_id, old_run, after=0)
    new_active = harness_client.active_run(harness_e2e_config.conversation_id, harness_e2e_config.skill_id)
    evidence.write_sse("01-old-replay-late.sse", old_replay)
    evidence.write_json("02-active-after-old-replay.json", new_active)

    active_run = new_active.get("run")
    if active_run and active_run.get("status") in {"queued", "running", "waiting_tool", "waiting_user", "waiting_approval"}:
        assert active_run["public_run_id"] == new_run

    for event in old_replay:
        data = event.get("data") or {}
        if event["event"] == "patch_applied":
            assert data.get("run_id") == old_run
        elif data.get("run_id"):
            assert data.get("run_id") == old_run


def test_manual_backend_restart_replay_gate(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    if os.getenv("HARNESS_ENGINEERING_RESTART_VALIDATION", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.skip(
            "Set HARNESS_ENGINEERING_RESTART_VALIDATION=1 after manually restarting the backend "
            "to validate cold DB replay"
        )
    run_id = os.getenv("HARNESS_E2E_EXISTING_RUN_ID")
    if not run_id:
        pytest.skip("Set HARNESS_E2E_EXISTING_RUN_ID to a run created before backend restart")

    replay = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    active = harness_client.active_run(harness_e2e_config.conversation_id, harness_e2e_config.skill_id)
    evidence.write_sse("01-post-restart-replay.sse", replay)
    evidence.write_json("02-post-restart-active-run.json", active)

    assert replay, "cold replay after backend restart must return persisted run events"
    assert active["source"] in {"db", "db_recent", "memory"}
    assert active["run"] is not None
