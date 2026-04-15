"""Normalization helpers for table views and row values."""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.models.business import TableField, TableView


def _coerce_int_list(values: Any) -> list[int]:
    if not values:
        return []
    result: list[int] = []
    for item in values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    seen: set[int] = set()
    deduped: list[int] = []
    for item in result:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def normalize_view_config(config: dict | None) -> dict:
    cfg = dict(config or {})
    filters = cfg.get("filters")
    sorts = cfg.get("sorts")
    return {
        "filters": list(filters) if isinstance(filters, list) else [],
        "sorts": list(sorts) if isinstance(sorts, list) else [],
        "group_by": cfg.get("group_by") or "",
        "hidden_columns": [str(v) for v in (cfg.get("hidden_columns") or []) if str(v)],
        "column_widths": cfg.get("column_widths") if isinstance(cfg.get("column_widths"), dict) else {},
    }


def hydrate_view_payload(payload: dict, fields: list[TableField]) -> dict:
    """Build a canonical view payload from API input.

    ``config`` remains as the backward-compatible mirror, while structured
    fields become the actual source of truth.
    """
    config = normalize_view_config(payload.get("config"))

    visible_field_ids = payload.get("visible_field_ids")
    if visible_field_ids is None:
        visible_field_ids = _field_ids_from_config(fields, config)
    visible_field_ids = _coerce_int_list(visible_field_ids)

    hidden_columns = set(config.get("hidden_columns") or [])
    if not visible_field_ids and hidden_columns:
        visible_field_ids = [
            field.id
            for field in fields
            if field.id is not None
            and (field.physical_column_name or field.field_name) not in hidden_columns
            and field.field_name not in hidden_columns
        ]

    config["hidden_columns"] = _hidden_columns_from_visible_fields(fields, visible_field_ids)

    return {
        "name": (payload.get("name") or "").strip(),
        "view_type": (payload.get("view_type") or "grid").strip() or "grid",
        "view_kind": (payload.get("view_kind") or "list").strip() or "list",
        "visibility_scope": (payload.get("visibility_scope") or "table_inherit").strip() or "table_inherit",
        "view_purpose": (payload.get("view_purpose") or None),
        "visible_field_ids": visible_field_ids,
        "allowed_role_group_ids": _coerce_int_list(payload.get("allowed_role_group_ids")),
        "allowed_skill_ids": _coerce_int_list(payload.get("allowed_skill_ids")),
        "disclosure_ceiling": payload.get("disclosure_ceiling"),
        "row_limit": payload.get("row_limit"),
        "config": config,
        "filter_rule_json": {"filters": config["filters"]},
        "sort_rule_json": {"sorts": config["sorts"]},
        "group_rule_json": {"group_by": config["group_by"]} if config["group_by"] else {},
    }


def _field_ids_from_config(fields: list[TableField], config: dict) -> list[int]:
    hidden_columns = set(config.get("hidden_columns") or [])
    result: list[int] = []
    for field in fields:
        if field.id is None:
            continue
        if field.is_hidden_by_default:
            continue
        aliases = {field.field_name}
        if field.physical_column_name:
            aliases.add(field.physical_column_name)
        if aliases & hidden_columns:
            continue
        result.append(field.id)
    return result


def _hidden_columns_from_visible_fields(fields: list[TableField], visible_field_ids: list[int]) -> list[str]:
    if not fields or not visible_field_ids:
        return []
    visible = set(visible_field_ids)
    hidden: list[str] = []
    for field in fields:
        if field.id is None or field.id in visible:
            continue
        hidden.append(field.physical_column_name or field.field_name)
    return hidden


def serialize_view(view: TableView) -> dict:
    config = normalize_view_config(view.config or {})
    filters = (view.filter_rule_json or {}).get("filters") if isinstance(view.filter_rule_json, dict) else None
    sorts = (view.sort_rule_json or {}).get("sorts") if isinstance(view.sort_rule_json, dict) else None
    group_by = (view.group_rule_json or {}).get("group_by") if isinstance(view.group_rule_json, dict) else None

    if isinstance(filters, list):
        config["filters"] = filters
    if isinstance(sorts, list):
        config["sorts"] = sorts
    if group_by:
        config["group_by"] = group_by

    return {
        "id": view.id,
        "table_id": view.table_id,
        "name": view.name,
        "view_type": view.view_type,
        "view_purpose": view.view_purpose,
        "visibility_scope": view.visibility_scope or "table_inherit",
        "is_default": view.is_default or False,
        "is_system": view.is_system or False,
        "config": config,
        "created_by": view.created_by,
        "visible_field_ids": view.visible_field_ids or [],
        "view_kind": view.view_kind or "list",
        "disclosure_ceiling": view.disclosure_ceiling,
        "allowed_role_group_ids": view.allowed_role_group_ids or [],
        "allowed_skill_ids": view.allowed_skill_ids or [],
        "row_limit": view.row_limit,
        "view_state": getattr(view, "view_state", None),
        "view_invalid_reason": getattr(view, "view_invalid_reason", None),
    }


def normalize_scalar_for_field(value: Any, field: TableField | None) -> Any:
    field_type = (field.field_type if field else "") or ""
    normalized_type = field_type.lower()

    if normalized_type in {"single_select", "select"}:
        return _normalize_single_select(value)
    if normalized_type == "multi_select":
        return _normalize_multi_select(value)
    if normalized_type in {"boolean", "checkbox"}:
        return _normalize_boolean(value)
    return _normalize_generic(value)


def normalize_row_payload(data: dict[str, Any], field_map: dict[str, TableField]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for column, value in data.items():
        normalized[column] = _serialize_for_storage(
            normalize_scalar_for_field(value, field_map.get(column)),
            field_map.get(column),
        )
    return normalized


def normalize_row_for_response(row: dict[str, Any], field_map: dict[str, TableField]) -> dict[str, Any]:
    return {
        column: normalize_scalar_for_field(value, field_map.get(column))
        for column, value in row.items()
    }


def _serialize_for_storage(value: Any, field: TableField | None) -> Any:
    field_type = ((field.field_type if field else "") or "").lower()
    if field_type == "multi_select":
        return json.dumps(_extract_option_strings(value), ensure_ascii=False)
    if field_type in {"boolean", "checkbox"}:
        if value is None:
            return None
        return 1 if bool(value) else 0
    return value


def _normalize_generic(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [_normalize_generic(item) for item in parsed]
            except Exception:
                return value
    return value


def _normalize_single_select(value: Any) -> str | None:
    options = _extract_option_strings(value)
    if not options:
        return None
    return options[0]


def _normalize_multi_select(value: Any) -> list[str]:
    return _extract_option_strings(value)


def _normalize_boolean(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _extract_option_strings(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                return _extract_option_strings(parsed)
            except Exception:
                pass
        if "," in stripped:
            return _dedupe_strings(part.strip() for part in stripped.split(","))
        return [stripped]
    if isinstance(value, dict):
        return _extract_option_strings(_extract_option_value(value))
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_extract_option_strings(item))
        return _dedupe_strings(result)
    return [str(value)]


def _extract_option_value(value: dict[str, Any]) -> Any:
    for key in ("label", "text", "name", "value", "display_name"):
        if key in value and value[key] not in (None, ""):
            return value[key]
    return value


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
