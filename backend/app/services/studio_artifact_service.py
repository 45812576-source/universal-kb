"""Studio Artifact Service — 卡片产出物管理。

Phase B7: card 完成后自动产出 artifact，通过 artifact_patch 下发前端。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def create_card_artifact(
    *,
    card_id: str,
    contract_id: str,
    artifact_type: str,  # summary | draft | staged_edit | report | evidence
    title: str,
    content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建一个 card artifact 记录。"""
    artifact_id = f"art_{uuid.uuid4().hex[:10]}"
    return {
        "artifact_id": artifact_id,
        "card_id": card_id,
        "contract_id": contract_id,
        "artifact_type": artifact_type,
        "title": title,
        "content": content or {},
        "metadata": metadata or {},
        "status": "active",
    }


def mark_artifacts_stale(
    *,
    card_id: str,
    artifacts: list[dict[str, Any]],
    reason: str = "card_superseded",
) -> list[dict[str, Any]]:
    """将指定卡片的所有 artifact 标记为 stale。"""
    stale_list = []
    for art in artifacts:
        if isinstance(art, dict) and art.get("card_id") == card_id:
            art["status"] = "stale"
            art["stale_reason"] = reason
            stale_list.append(art)
    return stale_list


def build_artifact_patch(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """构建 artifact_patch 信封。"""
    from app.services.studio_patch_bus import build_patch_envelope
    return build_patch_envelope(
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type="artifact_patch",
        target=artifact.get("card_id", ""),
        payload=artifact,
    )
