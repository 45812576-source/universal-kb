"""$ref 引用解析 + 拓扑排序工具。"""
from __future__ import annotations

import re
from typing import Any


def topological_sort(steps: list[dict]) -> list[dict]:
    """按 depends_on 进行拓扑排序，返回可执行顺序。

    同一层（无互相依赖）的步骤保持原相对顺序（方便并行）。
    若检测到循环依赖，抛出 ValueError。
    """
    key_to_step = {s["step_key"]: s for s in steps}
    visited: set[str] = set()
    temp_mark: set[str] = set()
    result: list[dict] = []

    def visit(key: str):
        if key in temp_mark:
            raise ValueError(f"PEV 计划存在循环依赖：{key}")
        if key in visited:
            return
        temp_mark.add(key)
        step = key_to_step.get(key)
        if step:
            for dep in step.get("depends_on") or []:
                if dep in key_to_step:
                    visit(dep)
        temp_mark.discard(key)
        visited.add(key)
        if step:
            result.append(step)

    for s in steps:
        visit(s["step_key"])

    return result


def resolve_inputs(input_spec: dict, context: dict) -> dict:
    """将 input_spec 中的 $ref 引用替换为 context 中的实际值。

    引用格式：
      "$step_key.field"  → context["step_key"]["field"]
      "$step_key"        → context["step_key"]（整个步骤结果）

    非引用字面量原样保留。
    """
    return {k: _resolve_value(v, context) for k, v in input_spec.items()}


def _resolve_value(value: Any, context: dict) -> Any:
    if isinstance(value, str):
        return _resolve_str(value, context)
    if isinstance(value, dict):
        return {k: _resolve_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, context) for v in value]
    return value


_REF_PATTERN = re.compile(r"^\$([a-zA-Z_][a-zA-Z0-9_]*)(?:\.(.+))?$")


def _resolve_str(value: str, context: dict) -> Any:
    m = _REF_PATTERN.match(value)
    if not m:
        return value

    step_key = m.group(1)
    field_path = m.group(2)  # 可能是 None 或 "field" 或 "a.b.c"

    step_result = context.get(step_key)
    if step_result is None:
        return value  # 未找到时原样返回

    if field_path is None:
        return step_result

    # 支持嵌套路径如 "a.b.c"
    parts = field_path.split(".")
    current = step_result
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return value  # 路径不存在，原样返回
        if current is None:
            return value
    return current
