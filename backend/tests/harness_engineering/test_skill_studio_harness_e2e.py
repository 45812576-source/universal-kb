"""Strict live E2E gates for the Skill Studio Harness Engineering upgrade."""
from __future__ import annotations

import os
from typing import Any

import pytest

from .conftest import EvidenceRecorder, HarnessApiClient, HarnessE2EConfig


pytestmark = pytest.mark.skipif(
    os.getenv("HARNESS_ENGINEERING_E2E", "").lower() not in {"1", "true", "yes", "on"},
    reason="Set HARNESS_ENGINEERING_E2E=1 to run live Harness Engineering E2E gates",
)


def _payload_run_id(event: dict[str, Any]) -> str | None:
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return None
    if event.get("event") == "patch_applied":
        payload = data.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("run_id") or data.get("run_id") or "")
    return str(data.get("run_id") or data.get("_run_id") or data.get("public_run_id") or "")


def _assert_run_identity(events: list[dict[str, Any]], public_run_id: str) -> None:
    assert public_run_id, "public_run_id must be visible to the test harness"
    for event in events:
        candidate = _payload_run_id(event)
        if candidate:
            assert candidate == public_run_id, f"{event['event']} leaked run identity {candidate}, expected {public_run_id}"


def _patch_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event.get("event") == "patch_applied" and isinstance(event.get("data"), dict)]


def _assert_patch_envelopes(events: list[dict[str, Any]], public_run_id: str) -> None:
    patches = _patch_events(events)
    assert patches, "patch_applied events are required; raw SSE cannot be the canonical UI mutation path"
    seen_patch_seq: set[int] = set()
    for patch in patches:
        assert patch.get("run_id") == public_run_id
        assert isinstance(patch.get("run_version"), int)
        assert isinstance(patch.get("patch_seq"), int)
        assert patch["patch_seq"] > 0
        assert patch["patch_seq"] not in seen_patch_seq
        seen_patch_seq.add(patch["patch_seq"])
        assert patch.get("patch_type"), "patch_type is required"
        assert isinstance(patch.get("payload"), dict), "patch payload must be a dict"
        assert patch["payload"].get("run_id") == public_run_id
        assert patch["payload"].get("run_version") == patch["run_version"]


def test_live_run_persists_event_log_and_replays_after_sequence(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    run_id, live_events = harness_client.stream_studio_message(
        harness_e2e_config.conversation_id,
        skill_id=harness_e2e_config.skill_id,
        content=(
            "Harness Engineering E2E: create a queue_window patch, one visible card, "
            "and a concise first useful response. Do not silently fallback."
        ),
    )
    assert run_id, "X-Studio-Run-Id header must expose the public_run_id"
    evidence.write_sse("01-live-stream.sse", live_events)

    replay_events = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    evidence.write_sse("02-replay-full.sse", replay_events)

    assert replay_events, "DB-backed replay must return events after the stream closes"
    assert any(event["event"] == "studio_run" for event in replay_events), "replay must include run summary"
    assert any(event["event"] == "patch_applied" for event in replay_events), "replay must include canonical patches"
    _assert_run_identity(replay_events, run_id)
    _assert_patch_envelopes(replay_events, run_id)

    split_after = min(2, max(0, len(replay_events) - 1))
    replay_tail = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=split_after)
    evidence.write_sse("03-replay-after-sequence.sse", replay_tail)
    assert replay_tail, "after_sequence reconnect must return the missing suffix"
    assert [item["event"] for item in replay_tail] == [item["event"] for item in replay_events[split_after:]]


def test_cancel_sets_terminal_state_and_stops_replay_progress(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    run_id, prefix_events = harness_client.stream_studio_message(
        harness_e2e_config.conversation_id,
        skill_id=harness_e2e_config.skill_id,
        content=(
            "Harness Engineering cancel gate: start a deep inspection and keep the run open "
            "long enough for cancellation validation."
        ),
        max_events=3,
    )
    assert run_id, "run id must be available before cancellation"
    evidence.write_sse("01-prefix-before-cancel.sse", prefix_events)

    cancel_response = harness_client.cancel_run(harness_e2e_config.conversation_id, run_id)
    evidence.write_json("02-cancel-response.json", cancel_response)
    assert cancel_response["run"]["status"] in {"cancelled", "completed", "failed", "superseded"}
    if cancel_response["run"]["status"] == "completed":
        pytest.skip("Run completed before cancellation reached the server; rerun against a slower Deep Lane fixture")

    replay_events = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_id, after=0)
    evidence.write_sse("03-cancel-replay.sse", replay_events)
    assert any(event["event"] == "status" and event["data"].get("stage") == "cancelled" for event in replay_events)
    assert any(event["event"] == "done" and event["data"].get("cancelled") is True for event in replay_events)


def test_consecutive_runs_supersede_without_identity_bleed(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    run_a, events_a_prefix = harness_client.stream_studio_message(
        harness_e2e_config.conversation_id,
        skill_id=harness_e2e_config.skill_id,
        content="Harness Engineering supersede gate A: run a deep background review.",
        max_events=2,
    )
    run_b, events_b = harness_client.stream_studio_message(
        harness_e2e_config.conversation_id,
        skill_id=harness_e2e_config.skill_id,
        content="Harness Engineering supersede gate B: this newer request must own the active queue.",
    )
    assert run_a and run_b and run_a != run_b
    evidence.write_sse("01-run-a-prefix.sse", events_a_prefix)
    evidence.write_sse("02-run-b-stream.sse", events_b)

    replay_a = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_a, after=0)
    replay_b = harness_client.stream_run_events(harness_e2e_config.conversation_id, run_b, after=0)
    evidence.write_sse("03-run-a-replay.sse", replay_a)
    evidence.write_sse("04-run-b-replay.sse", replay_b)

    _assert_run_identity(replay_a, run_a)
    _assert_run_identity(replay_b, run_b)
    if not any(event["event"] == "run_superseded" for event in replay_a):
        pytest.skip("Run A finished before Run B started; rerun with a slower Deep Lane fixture")
    assert any(event["event"] == "run_superseded" and event["data"].get("superseded_by") == run_b for event in replay_a)


def test_tool_handoff_bind_back_round_trip_stays_structured(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    harness_client.init_session(harness_e2e_config.skill_id, session_mode="optimize")
    card_response = harness_client.create_card(
        harness_e2e_config.skill_id,
        title="Harness Engineering Tool Handoff Fixture",
        summary="Tool implementation must leave Studio through a structured external handoff.",
        target_file="tools/harness_engineering_fixture.py",
        activate=True,
    )
    evidence.write_json("01-card-created.json", card_response)
    assert card_response.get("ok") is True
    card_id = str(card_response.get("card_id") or card_response.get("id") or "")
    assert card_id

    handoff_response = harness_client.handoff_card(harness_e2e_config.skill_id, card_id)
    evidence.write_json("02-handoff-response.json", handoff_response)
    assert handoff_response.get("ok") is True
    assert handoff_response.get("derived_card_id"), "external handoff must create a structured derived card"

    bind_card_id = str(handoff_response.get("derived_card_id") or card_id)
    bind_response = harness_client.bind_back_card(harness_e2e_config.skill_id, bind_card_id)
    evidence.write_json("03-bind-back-response.json", bind_response)
    assert bind_response.get("ok") is True

    session = harness_client.get_session(harness_e2e_config.skill_id)
    evidence.write_json("04-session-after-bind-back.json", session)
    assert "queue_window" in session or "card_queue_window" in session
    assert "cards" in session or "workflow_cards" in session


def test_session_recovery_contains_queue_and_context(
    harness_client: HarnessApiClient,
    harness_e2e_config: HarnessE2EConfig,
    evidence: EvidenceRecorder,
) -> None:
    initialized = harness_client.init_session(harness_e2e_config.skill_id, session_mode="optimize")
    recovered = harness_client.get_session(harness_e2e_config.skill_id)
    evidence.write_json("01-session-init.json", initialized)
    evidence.write_json("02-session-recovered.json", recovered)

    queue = recovered.get("queue_window") or recovered.get("card_queue_window")
    assert isinstance(queue, dict), "studio/session must recover queue_window/card_queue_window"
    assert "active_card_id" in queue
    assert "visible_card_ids" in queue
    assert "backlog_count" in queue
    assert recovered.get("cards") is not None or recovered.get("workflow_cards") is not None
