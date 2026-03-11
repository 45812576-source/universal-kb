"""Prompt compiler: assemble system prompt from skill template + schema constraints + context."""
from __future__ import annotations

import json


def compile(
    system_prompt: str,
    output_schema: dict | None,
    extracted_vars: dict,
    structured_context: dict | None = None,
) -> str:
    """Build a fully-assembled system prompt.

    - Replaces {var} placeholders with extracted_vars
    - Injects structured_context from prior steps
    - Appends output_schema as JSON constraint block
    - If no output_schema, returns prompt as-is (backward-compatible)
    """
    result = system_prompt

    # Variable substitution
    for var, val in extracted_vars.items():
        if val:
            result = result.replace("{" + var.strip("{}") + "}", str(val))

    # Inject upstream structured context
    if structured_context:
        ctx_json = json.dumps(structured_context, ensure_ascii=False, indent=2)
        result += (
            "\n\n## 已有上下文（来自上一步）\n"
            f"```json\n{ctx_json}\n```\n"
            "请基于以上结构化数据继续工作。"
        )

    # Append output schema constraint
    if output_schema:
        schema_json = json.dumps(output_schema, ensure_ascii=False, indent=2)
        result += (
            "\n\n## 输出格式要求\n"
            "严格按以下 JSON Schema 返回结果，只返回 JSON，不要包含其他内容。\n"
            f"```json\n{schema_json}\n```"
        )

    return result


def render_structured_as_markdown(output_schema: dict, data: dict) -> str:
    """Render structured JSON output as human-readable markdown.

    Uses schema property descriptions and structure to produce a clean document.
    """
    lines: list[str] = []
    properties = output_schema.get("properties", {})

    for key, prop_def in properties.items():
        value = data.get(key)
        if value is None:
            continue

        label = prop_def.get("description", key)
        prop_type = prop_def.get("type", "string")

        if prop_type == "array":
            lines.append(f"### {label}")
            item_def = prop_def.get("items", {})
            if item_def.get("type") == "object":
                # Array of objects — render each as a sub-section
                for i, item in enumerate(value, 1):
                    item_props = item_def.get("properties", {})
                    heading = item.get("heading") or item.get("title") or item.get("name") or f"#{i}"
                    lines.append(f"#### {heading}")
                    for ik, ip in item_props.items():
                        iv = item.get(ik)
                        if iv is None or ik in ("heading", "title", "name"):
                            continue
                        il = ip.get("description", ik)
                        if isinstance(iv, list):
                            lines.append(f"**{il}**")
                            for bullet in iv:
                                lines.append(f"- {bullet}")
                        else:
                            lines.append(f"**{il}**: {iv}")
                    lines.append("")
            else:
                # Simple array — bullet list
                for item in value:
                    lines.append(f"- {item}")
            lines.append("")

        elif prop_type == "object":
            lines.append(f"### {label}")
            if isinstance(value, dict):
                for ok, ov in value.items():
                    lines.append(f"- **{ok}**: {ov}")
            else:
                lines.append(str(value))
            lines.append("")

        else:
            # string / number / boolean
            lines.append(f"### {label}\n{value}\n")

    return "\n".join(lines).strip()
