"""Patch envelope 单元测试 — 覆盖 patch_id / sequence / idempotency_key / target / public_run_id / run_version。"""
from __future__ import annotations

import pytest

from app.services.studio_patch_bus import (
    StudioPatchEnvelope,
    build_error_patch,
    build_patch_envelope,
    patch_type_for_event,
    should_apply_patch,
    PATCH_TYPE_BY_EVENT,
)


# ── StudioPatchEnvelope fields ────────────────────────────────────────────────

class TestStudioPatchEnvelope:

    def test_patch_id_auto_generated(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=1, patch_seq=1,
            patch_type="card_patch", payload={},
        )
        assert env.patch_id.startswith("p_")
        assert len(env.patch_id) > 4

    def test_patch_id_unique(self):
        ids = set()
        for _ in range(100):
            env = StudioPatchEnvelope(
                public_run_id="run-1", run_version=1, patch_seq=1,
                patch_type="test", payload={},
            )
            ids.add(env.patch_id)
        assert len(ids) == 100

    def test_sequence_defaults_to_patch_seq(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=1, patch_seq=42,
            patch_type="test", payload={},
        )
        assert env.sequence == 42

    def test_idempotency_key_auto_generated(self):
        env = StudioPatchEnvelope(
            public_run_id="run-abc", run_version=3, patch_seq=7,
            patch_type="test", payload={},
        )
        assert env.idempotency_key == "run-abc:patch:7"

    def test_run_id_mirrors_public_run_id(self):
        env = StudioPatchEnvelope(
            public_run_id="run-xyz", run_version=1, patch_seq=1,
            patch_type="test", payload={},
        )
        assert env.run_id == "run-xyz"

    def test_target_preserved(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=1, patch_seq=1,
            patch_type="card_patch", payload={}, target="card_abc",
        )
        assert env.target == "card_abc"

    def test_to_dict_contains_all_required_fields(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=2, patch_seq=5,
            patch_type="card_patch", payload={"foo": "bar"},
            target="card_123",
        )
        d = env.to_dict()
        assert d["public_run_id"] == "run-1"
        assert d["run_version"] == 2
        assert d["patch_seq"] == 5
        assert d["patch_type"] == "card_patch"
        assert d["sequence"] == 5
        assert d["idempotency_key"] == "run-1:patch:5"
        assert d["target"] == "card_123"
        assert "patch_id" in d
        assert "created_at" in d

    def test_to_dict_omits_empty_harness_run_id(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={},
        )
        d = env.to_dict()
        assert "harness_run_id" not in d

    def test_to_dict_includes_harness_run_id_when_set(self):
        env = StudioPatchEnvelope(
            public_run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={}, harness_run_id="h_run_1",
        )
        d = env.to_dict()
        assert d["harness_run_id"] == "h_run_1"


# ── build_patch_envelope ──────────────────────────────────────────────────────

class TestBuildPatchEnvelope:

    def test_returns_dict_with_all_fields(self):
        result = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=3,
            patch_type="workflow_patch", payload={"stage": "generating"},
            target="workflow",
        )
        assert isinstance(result, dict)
        assert result["public_run_id"] == "run-1"
        assert result["run_version"] == 1
        assert result["patch_seq"] == 3
        assert result["patch_type"] == "workflow_patch"
        assert result["target"] == "workflow"
        assert result["sequence"] == 3
        assert "patch_id" in result
        assert "idempotency_key" in result
        assert "payload" in result

    def test_payload_gets_run_context_injected(self):
        result = build_patch_envelope(
            run_id="run-x", run_version=2, patch_seq=1,
            patch_type="card_patch", payload={"card_id": "c1"},
        )
        payload = result["payload"]
        assert payload["run_id"] == "run-x"
        assert payload["public_run_id"] == "run-x"
        assert payload["run_version"] == 2
        assert payload["card_id"] == "c1"

    def test_harness_run_id_passed_through(self):
        result = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={}, harness_run_id="h_123",
        )
        assert result["harness_run_id"] == "h_123"


# ── build_error_patch ─────────────────────────────────────────────────────────

class TestBuildErrorPatch:

    def test_error_patch_type(self):
        result = build_error_patch(
            run_id="run-1", run_version=1, patch_seq=1,
            error_type="denied", message="Denied",
        )
        assert result["patch_type"] == "error_patch"

    def test_error_patch_payload(self):
        result = build_error_patch(
            run_id="run-1", run_version=1, patch_seq=1,
            error_type="not_in_contract", message="Tool not allowed",
            details={"tool": "sandbox.run"},
        )
        payload = result["payload"]
        assert payload["error_type"] == "not_in_contract"
        assert payload["message"] == "Tool not allowed"
        assert payload["details"] == {"tool": "sandbox.run"}
        assert payload["retryable"] is False

    def test_error_patch_retryable(self):
        result = build_error_patch(
            run_id="run-1", run_version=1, patch_seq=1,
            error_type="timeout", message="Timeout", retryable=True,
        )
        assert result["payload"]["retryable"] is True


# ── should_apply_patch ────────────────────────────────────────────────────────

class TestShouldApplyPatch:

    def test_accepts_matching_run(self):
        patch = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={},
        )
        assert should_apply_patch(patch, active_run_id="run-1", active_run_version=1) is True

    def test_rejects_wrong_run_id(self):
        patch = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={},
        )
        assert should_apply_patch(patch, active_run_id="run-2", active_run_version=1) is False

    def test_rejects_wrong_version(self):
        patch = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=1,
            patch_type="test", payload={},
        )
        assert should_apply_patch(patch, active_run_id="run-1", active_run_version=2) is False

    def test_idempotency_key_dedup(self):
        patch = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=5,
            patch_type="test", payload={},
        )
        applied_keys = {"run-1:patch:5"}
        assert should_apply_patch(
            patch, active_run_id="run-1", active_run_version=1,
            applied_idempotency_keys=applied_keys,
        ) is False

    def test_patch_seq_dedup(self):
        patch = build_patch_envelope(
            run_id="run-1", run_version=1, patch_seq=3,
            patch_type="test", payload={},
        )
        applied_seqs = {3}
        assert should_apply_patch(
            patch, active_run_id="run-1", active_run_version=1,
            applied_patch_seqs=applied_seqs,
        ) is False


# ── patch_type_for_event ──────────────────────────────────────────────────────

class TestPatchTypeForEvent:

    def test_tool_error_maps_to_tool_error_patch(self):
        assert patch_type_for_event("tool_error") == "tool_error_patch"

    def test_error_maps_to_error_patch(self):
        assert patch_type_for_event("error") == "error_patch"

    def test_unknown_event_returns_none(self):
        assert patch_type_for_event("nonexistent_event") is None

    def test_all_registered_events_have_patch_suffix(self):
        for event_name, patch_type in PATCH_TYPE_BY_EVENT.items():
            assert patch_type.endswith("_patch"), f"{event_name} -> {patch_type} missing _patch suffix"
