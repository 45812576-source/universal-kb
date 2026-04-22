"""Skill Studio patch protocol — typed patch 作为 UI 状态变更唯一主协议。

Phase B4: StudioPatchEnvelope 升级为包含 public_run_id / patch_id / sequence /
idempotency_key 的完整信封。所有 card / queue / workspace / artifact / error
变更通过 patch service 生成，raw SSE 只输出文本 token 或兼容事件。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_patch_id() -> str:
    return f"p_{uuid.uuid4().hex[:12]}"


# ── Patch Type Registry ───────────────────────────────────────────────────────

PATCH_TYPE_BY_EVENT: dict[str, str] = {
    # run lifecycle
    "studio_run": "run_status_patch",
    "run_superseded": "run_status_patch",
    # workflow / status
    "workflow_state": "workflow_patch",
    "status": "workflow_patch",
    "route_status": "workflow_patch",
    "assist_skills_status": "workflow_patch",
    "architect_phase_status": "workflow_patch",
    # audit
    "audit_summary": "audit_patch",
    # card lifecycle
    "governance_card": "card_patch",
    "card_patch": "card_patch",
    "card_status_patch": "card_status_patch",
    # card queue
    "queue_window_patch": "queue_window_patch",
    "card_queue_patch": "card_queue_patch",
    # staged edit
    "staged_edit_notice": "staged_edit_patch",
    "staged_edit_patch": "staged_edit_patch",
    # artifact
    "artifact_patch": "artifact_patch",
    # workspace
    "workspace_patch": "workspace_patch",
    # stale
    "stale_patch": "stale_patch",
    # timeline
    "timeline_patch": "timeline_patch",
    # transition blocked
    "transition_blocked": "transition_blocked_patch",
    # tool error
    "tool_error": "tool_error_patch",
    # error (explicit, no silent fallback)
    "error": "error_patch",
    # reconcile (memory conflict)
    "reconcile": "reconcile_patch",
    # deep lane
    "deep_summary": "deep_summary_patch",
    "deep_evidence": "evidence_patch",
}


# ── Patch Envelope ────────────────────────────────────────────────────────────

@dataclass
class StudioPatchEnvelope:
    """统一 patch 信封 — 所有 UI 状态变更的唯一载体。

    Phase B4 升级字段:
    - public_run_id: 前端唯一 run 身份
    - patch_id: 全局唯一 patch 标识
    - sequence: run 内单调递增序列号（用于 replay 和去重）
    - idempotency_key: 幂等键（patch_id 与 sequence 组合）
    - target: patch 作用目标（card_id / workspace / queue 等）
    - harness_run_id: 内层审计 ID（可选）
    """
    public_run_id: str
    run_version: int
    patch_seq: int
    patch_type: str
    payload: dict[str, Any]
    patch_id: str = field(default_factory=_new_patch_id)
    sequence: int = 0
    target: str = ""
    idempotency_key: str = ""
    harness_run_id: str | None = None
    created_at: str = field(default_factory=_now_iso)

    # 兼容旧字段
    run_id: str = ""

    def __post_init__(self):
        if not self.run_id:
            self.run_id = self.public_run_id
        if not self.sequence:
            self.sequence = self.patch_seq
        if not self.idempotency_key:
            self.idempotency_key = f"{self.public_run_id}:patch:{self.patch_seq}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 移除空值可选字段保持响应干净
        if not data.get("harness_run_id"):
            data.pop("harness_run_id", None)
        if not data.get("target"):
            data.pop("target", None)
        return data


# ── Public API ────────────────────────────────────────────────────────────────

def patch_type_for_event(event_name: str) -> str | None:
    """根据事件名查找对应 patch type。"""
    return PATCH_TYPE_BY_EVENT.get(event_name)


def attach_run_context(
    payload: dict[str, Any] | None,
    *,
    run_id: str,
    run_version: int,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """向 payload 注入 run 上下文字段。"""
    base = dict(payload or {})
    base.setdefault("run_id", run_id)
    base.setdefault("public_run_id", run_id)
    base.setdefault("run_version", run_version)
    if workflow_id:
        base.setdefault("workflow_id", workflow_id)

    metadata = base.get("metadata")
    next_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    run_metadata = next_metadata.get("run")
    merged_run_metadata = dict(run_metadata) if isinstance(run_metadata, dict) else {}
    merged_run_metadata.setdefault("run_id", run_id)
    merged_run_metadata.setdefault("public_run_id", run_id)
    merged_run_metadata.setdefault("run_version", run_version)
    next_metadata["run"] = merged_run_metadata
    if next_metadata:
        base["metadata"] = next_metadata
    return base


def build_patch_envelope(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    patch_type: str,
    payload: dict[str, Any],
    target: str = "",
    harness_run_id: str | None = None,
) -> dict[str, Any]:
    """构建完整的 patch envelope dict。"""
    return StudioPatchEnvelope(
        public_run_id=run_id,
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type=patch_type,
        target=target,
        harness_run_id=harness_run_id,
        payload=attach_run_context(
            payload,
            run_id=run_id,
            run_version=run_version,
            workflow_id=run_id,
        ),
    ).to_dict()


def should_apply_patch(
    patch: dict[str, Any],
    *,
    active_run_id: str | None,
    active_run_version: int | None,
    applied_patch_seqs: set[int] | None = None,
    applied_idempotency_keys: set[str] | None = None,
) -> bool:
    """判断 patch 是否应当被 apply。

    支持两种去重策略:
    1. patch_seq 去重（旧版兼容）
    2. idempotency_key 去重（Phase B4 推荐）
    """
    if not isinstance(patch, dict):
        return False
    # run 匹配: public_run_id 或 run_id
    patch_run_id = str(patch.get("public_run_id") or patch.get("run_id") or "")
    if active_run_id and patch_run_id != active_run_id:
        return False
    if active_run_version is not None and int(patch.get("run_version") or 0) != active_run_version:
        return False

    # idempotency_key 去重（优先）
    idem_key = str(patch.get("idempotency_key") or "")
    if applied_idempotency_keys and idem_key and idem_key in applied_idempotency_keys:
        return False

    # patch_seq 去重（兼容）
    patch_seq = int(patch.get("patch_seq") or 0)
    if applied_patch_seqs and patch_seq in applied_patch_seqs:
        return False

    return patch_seq > 0


# ── Error Patch Builder ───────────────────────────────────────────────────────

def build_error_patch(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    error_type: str,
    message: str,
    target: str = "",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建显式 error_patch — M5 要求不做静默降级。"""
    payload = {
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }
    if details:
        payload["details"] = details
    return build_patch_envelope(
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type="error_patch",
        target=target,
        payload=payload,
    )
