"""Skill Studio context digest helpers.

P2 提供可版本化、可失效的摘要缓存对象，供 Fast Lane / 观测埋点复用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


_CACHE_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _snippet(text: str | None, *, limit: int = 120) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def _estimate_tokens(text: str | None) -> int:
    content = str(text or "")
    return max(0, len(content) // 4)


def _empty_digest_cache() -> dict[str, Any]:
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "updated_at": None,
        "entries": {},
    }


def _normalize_digest_cache(cache: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cache, dict):
        return _empty_digest_cache()
    entries = cache.get("entries")
    return {
        "schema_version": int(cache.get("schema_version") or _CACHE_SCHEMA_VERSION),
        "updated_at": cache.get("updated_at"),
        "entries": dict(entries) if isinstance(entries, dict) else {},
    }


def _cache_source_signature(label: str, source_payload: Any) -> str:
    return _stable_hash({"label": label, "source": source_payload})


def _memo_digest_source_payload(memo_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(memo_context, dict):
        return None
    normalized = dict(memo_context)
    normalized.pop("context_digest_cache", None)
    memo_payload = normalized.get("memo")
    if isinstance(memo_payload, dict):
        normalized["memo"] = {
            key: value
            for key, value in memo_payload.items()
            if key != "context_digest_cache"
        }
    return normalized


def _resolve_cached_digest_entry(
    *,
    cache: dict[str, Any],
    entry_name: str,
    source_payload: Any,
    builder: Any,
    persistent: bool,
) -> tuple[Any, dict[str, Any], bool]:
    entries = cache.setdefault("entries", {})
    cached_entry = entries.get(entry_name)
    source_signature = _cache_source_signature(entry_name, source_payload)
    if isinstance(cached_entry, dict) and cached_entry.get("source_signature") == source_signature:
        digest = cached_entry.get("digest")
        if digest is not None:
            return digest, {
                "status": "hit",
                "persistent": persistent,
                "source_signature": source_signature,
                "cached_at": cached_entry.get("cached_at"),
            }, False

    digest = builder(source_payload)
    entries[entry_name] = {
        "source_signature": source_signature,
        "digest": digest,
        "cached_at": _now_iso(),
    }
    return digest, {
        "status": "miss",
        "persistent": persistent,
        "source_signature": source_signature,
        "cached_at": entries[entry_name]["cached_at"],
    }, True


def build_conversation_context_digest(history_messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    history = list(history_messages or [])
    normalized = []
    total_tokens = 0
    last_user_message = ""

    for message in history[-8:]:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "")
        total_tokens += _estimate_tokens(content)
        if role == "user" and content.strip():
            last_user_message = content
        normalized.append({
            "role": role,
            "content": _snippet(content, limit=80),
        })

    digest = {
        "message_count": len(history),
        "estimated_tokens": total_tokens,
        "recent_messages": normalized,
        "last_user_message": _snippet(last_user_message),
    }
    digest["signature"] = _stable_hash(digest)
    return digest


def build_memo_digest(memo_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(memo_context, dict) or not memo_context:
        return None

    current_task = memo_context.get("current_task") if isinstance(memo_context.get("current_task"), dict) else {}
    latest_test = memo_context.get("latest_test") if isinstance(memo_context.get("latest_test"), dict) else {}
    tasks = memo_context.get("tasks") if isinstance(memo_context.get("tasks"), list) else []
    persistent_notices = memo_context.get("persistent_notices") if isinstance(memo_context.get("persistent_notices"), list) else []

    next_task_title = ""
    for task in tasks:
        if isinstance(task, dict) and task.get("status") in {"todo", "in_progress"}:
            next_task_title = str(task.get("title") or "")
            break

    digest = {
        "lifecycle_stage": memo_context.get("lifecycle_stage"),
        "current_task": _snippet(current_task.get("title") or current_task.get("description")),
        "next_task": _snippet(next_task_title),
        "latest_test_summary": _snippet(latest_test.get("summary")),
        "has_open_todos": any(isinstance(task, dict) and task.get("status") in {"todo", "in_progress"} for task in tasks),
        "has_pending_cards": any(
            isinstance(notice, dict) and str(notice.get("status") or "active") == "active"
            for notice in persistent_notices
        ),
    }
    digest["signature"] = _stable_hash(digest)
    return digest


def build_recovery_digest(recovery: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(recovery, dict) or not recovery:
        return None

    workflow_state = recovery.get("workflow_state") if isinstance(recovery.get("workflow_state"), dict) else {}
    cards = recovery.get("cards") if isinstance(recovery.get("cards"), list) else []
    staged_edits = recovery.get("staged_edits") if isinstance(recovery.get("staged_edits"), list) else []

    digest = {
        "workflow_phase": workflow_state.get("phase"),
        "next_action": workflow_state.get("next_action"),
        "pending_cards_count": sum(1 for card in cards if isinstance(card, dict) and card.get("status") == "pending"),
        "pending_edits_count": sum(1 for edit in staged_edits if isinstance(edit, dict) and edit.get("status") == "pending"),
        "updated_at": recovery.get("updated_at"),
    }
    digest["signature"] = _stable_hash(digest)
    return digest


def build_source_file_index_digest(source_files: list[dict[str, Any]] | None) -> dict[str, Any]:
    files = list(source_files or [])
    normalized_files: list[dict[str, Any]] = []
    total_size = 0

    for item in files[:20]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("name") or "")
        filetype = str(item.get("filetype") or item.get("category") or "")
        size = int(item.get("size") or 0)
        total_size += max(size, 0)
        normalized_files.append({
            "filename": filename,
            "filetype": filetype,
            "size": size,
            "modified_at": item.get("modified_at"),
            "keyword_summary": _snippet(item.get("summary") or item.get("description") or item.get("keywords")),
        })

    digest = {
        "file_count": len(files),
        "total_size": total_size,
        "files": normalized_files,
    }
    digest["signature"] = _stable_hash(digest)
    return digest


def build_context_digest_bundle(
    *,
    history_messages: list[dict[str, Any]] | None,
    memo_context: dict[str, Any] | None,
    source_files: list[dict[str, Any]] | None,
    editor_prompt: str | None = None,
    persisted_cache: dict[str, Any] | None = None,
    include_cache_payload: bool = False,
) -> dict[str, Any]:
    recovery = memo_context.get("workflow_recovery") if isinstance(memo_context, dict) else None
    editor_prompt_text = str(editor_prompt or "")
    cache = _normalize_digest_cache(persisted_cache)
    cache_meta: dict[str, Any] = {}
    cache_changed = False

    conversation_digest, cache_meta["conversation_digest"], _ = _resolve_cached_digest_entry(
        cache=cache,
        entry_name="conversation_digest",
        source_payload=list(history_messages or []),
        builder=build_conversation_context_digest,
        persistent=False,
    )
    memo_digest, cache_meta["memo_digest"], memo_changed = _resolve_cached_digest_entry(
        cache=cache,
        entry_name="memo_digest",
        source_payload=_memo_digest_source_payload(memo_context),
        builder=build_memo_digest,
        persistent=True,
    )
    recovery_digest, cache_meta["recovery_digest"], recovery_changed = _resolve_cached_digest_entry(
        cache=cache,
        entry_name="recovery_digest",
        source_payload=recovery if isinstance(recovery, dict) else None,
        builder=build_recovery_digest,
        persistent=True,
    )
    source_file_index_digest, cache_meta["source_file_index_digest"], source_changed = _resolve_cached_digest_entry(
        cache=cache,
        entry_name="source_file_index_digest",
        source_payload=list(source_files or []),
        builder=build_source_file_index_digest,
        persistent=True,
    )
    cache_changed = memo_changed or recovery_changed or source_changed

    bundle = {
        "conversation_digest": conversation_digest,
        "memo_digest": memo_digest,
        "recovery_digest": recovery_digest,
        "source_file_index_digest": source_file_index_digest,
        "editor_prompt_digest": {
            "length": len(editor_prompt_text),
            "estimated_tokens": _estimate_tokens(editor_prompt_text),
            "signature": _stable_hash({"length": len(editor_prompt_text), "text": _snippet(editor_prompt_text, limit=240)}),
        },
        "cache": {
            "schema_version": cache.get("schema_version", _CACHE_SCHEMA_VERSION),
            "updated_at": cache.get("updated_at"),
            "entries": cache_meta,
            "cache_changed": cache_changed,
        },
    }
    bundle["signature"] = _stable_hash({
        "conversation_digest": conversation_digest,
        "memo_digest": memo_digest,
        "recovery_digest": recovery_digest,
        "source_file_index_digest": source_file_index_digest,
        "editor_prompt_digest": bundle["editor_prompt_digest"],
    })
    if cache_changed:
        cache["updated_at"] = _now_iso()
        bundle["cache"]["updated_at"] = cache["updated_at"]
    if include_cache_payload:
        bundle["cache_payload"] = {
            "schema_version": cache.get("schema_version", _CACHE_SCHEMA_VERSION),
            "updated_at": cache.get("updated_at"),
            "entries": {
                key: value
                for key, value in (cache.get("entries") or {}).items()
                if key in {"memo_digest", "recovery_digest", "source_file_index_digest"}
            },
        }
    return bundle
