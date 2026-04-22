from __future__ import annotations

import ast
import re
from typing import Any


TAB_KEYS = ("organization", "department", "role", "person", "okr", "process")
REQUIRED_HEADINGS = ("事实区", "治理语义区", "分析区", "行动区", "证据", "变更摘要")


def _empty_change_summary() -> dict[str, list[dict[str, Any]]]:
    return {"added": [], "changed": [], "removed": []}


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in {"", "null", "None", "~"}:
        return None
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    if value.startswith("[") or value.startswith("{"):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str, list[str]]:
    warnings: list[str] = []
    text = markdown.lstrip()
    if not text.startswith("---"):
        return {}, markdown, ["缺少 YAML frontmatter，已保存 Markdown 但不会覆盖结构化数据。"]

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.S)
    if not match:
        return {}, markdown, ["frontmatter 结束标记无效，已保存 Markdown 但不会覆盖结构化数据。"]

    frontmatter_text = match.group(1)
    body = text[match.end():]
    parsed: dict[str, Any] = {}
    for line in frontmatter_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            warnings.append(f"frontmatter 行无法解析：{stripped}")
            continue
        key, raw_value = stripped.split(":", 1)
        parsed[key.strip()] = _parse_scalar(raw_value)
    return parsed, body, warnings


def parse_fixed_sections(markdown_body: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown_body, flags=re.M))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown_body)
        sections[name] = markdown_body[start:end].strip()

    failed_sections = [
        {"section": heading, "reason": "fixed heading missing"}
        for heading in REQUIRED_HEADINGS
        if heading not in sections
    ]
    return sections, failed_sections


def _extract_bullets(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
    return items


def _section_subsections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", text, flags=re.M))
    if not matches:
        return {}
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[name] = text[start:end].strip()
    return result


def _build_structured(tab_key: str, frontmatter: dict[str, Any], sections: dict[str, str]) -> dict[str, Any]:
    facts = sections.get("事实区", "")
    governance = sections.get("治理语义区", "")
    analysis = sections.get("分析区", "")
    actions = sections.get("行动区", "")
    evidence = sections.get("证据", "")
    fact_subsections = _section_subsections(facts)
    governance_subsections = _section_subsections(governance)

    return {
        "snapshot_type": frontmatter.get("snapshot_type") or tab_key,
        "title": frontmatter.get("title") or "",
        "subject_id": frontmatter.get("subject_id") or "",
        "version": frontmatter.get("version") or "",
        "status": frontmatter.get("status") or "draft",
        "owner": frontmatter.get("owner") or "",
        "updated_at": frontmatter.get("updated_at") or "",
        "confidence": frontmatter.get("confidence") or 0,
        "source_materials": frontmatter.get("source_materials") or [],
        "missing_items": frontmatter.get("missing_items") or [],
        "conflicts": frontmatter.get("conflicts") or [],
        tab_key: {
            "facts_text": facts,
            "governance_semantics_text": governance,
            "analysis_text": analysis,
            "actions_text": actions,
            "evidence_text": evidence,
            "fact_sections": fact_subsections,
            "governance_sections": governance_subsections,
            "fact_bullets": _extract_bullets(facts),
            "governance_bullets": _extract_bullets(governance),
            "action_bullets": _extract_bullets(actions),
        },
    }


def _diff_structured(tab_key: str, previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if not previous:
        return {
            "added": [{"entity_type": tab_key, "entity_name": current.get("title") or tab_key, "field": tab_key, "value": "created"}],
            "changed": [],
            "removed": [],
        }

    summary = _empty_change_summary()
    keys = ("title", "status", "owner", "confidence")
    for key in keys:
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            summary["changed"].append({
                "entity_type": tab_key,
                "entity_name": current.get("title") or tab_key,
                "field": key,
                "before": before,
                "after": after,
            })
    previous_text = ((previous.get(tab_key) or {}).get("facts_text") or "").strip()
    current_text = ((current.get(tab_key) or {}).get("facts_text") or "").strip()
    if previous_text != current_text:
        summary["changed"].append({
            "entity_type": tab_key,
            "entity_name": current.get("title") or tab_key,
            "field": "facts_text",
            "before": "previous" if previous_text else "",
            "after": "updated" if current_text else "",
        })
    return summary


def parse_tab_markdown(
    tab_key: str,
    markdown: str,
    previous_structured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frontmatter, body, warnings = parse_frontmatter(markdown)
    sections, failed_sections = parse_fixed_sections(body)
    if frontmatter.get("snapshot_type") and frontmatter["snapshot_type"] != tab_key:
        warnings.append(f"snapshot_type={frontmatter['snapshot_type']} 与 tab_key={tab_key} 不一致。")

    if failed_sections or not frontmatter:
        return {
            "ok": False,
            "status": "partial_sync",
            "structured": previous_structured or {},
            "failed_sections": failed_sections or [{"section": "frontmatter", "reason": "invalid frontmatter"}],
            "parser_warnings": warnings or ["Markdown 已保存，但结构化同步未覆盖旧数据。"],
            "change_summary": _empty_change_summary(),
        }

    structured = _build_structured(tab_key, frontmatter, sections)
    return {
        "ok": True,
        "status": "synced",
        "structured": structured,
        "failed_sections": [],
        "parser_warnings": warnings,
        "change_summary": _diff_structured(tab_key, previous_structured, structured),
    }
