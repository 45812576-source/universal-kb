"""Studio Replay / Eval / Chaos Service — 回放、评估、混沌测试。

Phase B12:
- Replay: 从 agent_run_events 表重放事件序列，支持断点续放
- Eval: 在 card complete 时自动记录 eval_snapshot（卡片产出快照）
- Chaos: allow_chaos_mode 在 run 中注入随机延迟/失败，用于健壮性测试
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator

from sqlalchemy.orm import Session as DBSession

from app.models.agent_run import AgentRun, AgentRunEvent
from app.services import studio_run_event_store

logger = logging.getLogger(__name__)


# ── Replay ───────────────────────────────────────────────────────────────────

def replay_run(
    db: DBSession,
    public_run_id: str,
    *,
    after_sequence: int = 0,
    speed: float = 1.0,
    batch_size: int = 50,
) -> Iterator[dict[str, Any]]:
    """从 DB 重放事件序列 — 生成器模式，支持断点续放。

    speed: 回放速度倍率（1.0 = 原速, 0 = 无延迟即时回放）
    """
    run = db.query(AgentRun).filter(AgentRun.public_run_id == public_run_id).first()
    if not run:
        yield {"type": "error", "error": "run_not_found", "run_id": public_run_id}
        return

    yield {
        "type": "replay_start",
        "run_id": public_run_id,
        "run_status": run.status,
        "run_version": run.run_version,
        "after_sequence": after_sequence,
    }

    offset = after_sequence
    prev_created_at = None

    while True:
        events = studio_run_event_store.get_events_after(
            db, public_run_id, offset, limit=batch_size,
        )
        if not events:
            break

        for event in events:
            # 模拟原始事件间隔
            if speed > 0 and prev_created_at and event.created_at:
                delta = (event.created_at - prev_created_at).total_seconds()
                if delta > 0:
                    time.sleep(min(delta / speed, 2.0))  # cap 单次延迟 2s

            yield {
                "type": "replay_event",
                "sequence": event.sequence,
                "event_type": event.event_type,
                "patch_type": event.patch_type,
                "payload": event.payload_json,
                "idempotency_key": event.idempotency_key,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            prev_created_at = event.created_at
            offset = event.sequence

        if len(events) < batch_size:
            break

    yield {
        "type": "replay_end",
        "run_id": public_run_id,
        "final_sequence": offset,
    }


# ── Eval Snapshot ────────────────────────────────────────────────────────────

def record_eval_snapshot(
    db: DBSession,
    *,
    public_run_id: str,
    run_version: int,
    card_id: str,
    contract_id: str | None = None,
    card_status: str = "completed",
    artifacts: list[dict[str, Any]] | None = None,
    staged_edits: list[dict[str, Any]] | None = None,
    exit_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在 card complete 时记录 eval_snapshot — 以 event 形式存入 event log。

    eval_snapshot 包含卡片完成时的产出物快照，供后续 eval pipeline 消费。
    """
    snapshot = {
        "card_id": card_id,
        "contract_id": contract_id,
        "card_status": card_status,
        "exit_reason": exit_reason,
        "artifact_count": len(artifacts or []),
        "artifacts": artifacts or [],
        "staged_edit_count": len(staged_edits or []),
        "staged_edits_summary": [
            {"id": e.get("id"), "status": e.get("status"), "target_file": e.get("target_file")}
            for e in (staged_edits or []) if isinstance(e, dict)
        ],
        "timestamp": time.time(),
    }
    if extra:
        snapshot["extra"] = extra

    # 写入 event log
    event = studio_run_event_store.append_event(
        db,
        public_run_id=public_run_id,
        run_version=run_version,
        event_type="eval_snapshot",
        patch_type="eval_snapshot",
        payload=snapshot,
    )

    return {
        "ok": True,
        "event_id": event.id if event else None,
        "card_id": card_id,
        "snapshot": snapshot,
    }


# ── Chaos Mode ───────────────────────────────────────────────────────────────

class ChaosConfig:
    """混沌测试配置。"""
    def __init__(
        self,
        *,
        enabled: bool = False,
        failure_rate: float = 0.1,      # 10% 概率注入失败
        max_delay_ms: int = 3000,        # 最大注入延迟 3s
        delay_rate: float = 0.2,         # 20% 概率注入延迟
        affected_categories: frozenset[str] = frozenset({"execute", "publish"}),
    ):
        self.enabled = enabled
        self.failure_rate = failure_rate
        self.max_delay_ms = max_delay_ms
        self.delay_rate = delay_rate
        self.affected_categories = affected_categories


# 默认关闭
_default_chaos = ChaosConfig(enabled=False)


def get_chaos_config() -> ChaosConfig:
    """获取当前 chaos 配置。"""
    return _default_chaos


def set_chaos_config(config: ChaosConfig) -> None:
    """设置 chaos 配置（仅用于测试环境）。"""
    global _default_chaos
    _default_chaos = config


def apply_chaos(
    *,
    tool_name: str,
    category: str,
    chaos_config: ChaosConfig | None = None,
) -> dict[str, Any] | None:
    """在 tool 执行前应用混沌注入。

    返回 None 表示正常通过，返回 dict 表示注入了异常。
    """
    config = chaos_config or _default_chaos
    if not config.enabled:
        return None
    if category not in config.affected_categories:
        return None

    # 注入延迟
    if random.random() < config.delay_rate:
        delay_ms = random.randint(100, config.max_delay_ms)
        time.sleep(delay_ms / 1000)
        logger.info("Chaos: injected %dms delay for %s", delay_ms, tool_name)
        # 延迟不阻止执行，只是慢
        return None

    # 注入失败
    if random.random() < config.failure_rate:
        logger.info("Chaos: injected failure for %s", tool_name)
        return {
            "chaos_injected": True,
            "type": "failure",
            "tool_name": tool_name,
            "message": f"Chaos: 模拟 {tool_name} 执行失败",
        }

    return None
