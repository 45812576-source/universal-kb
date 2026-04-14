"""Shared quality standard for sandbox evaluation and preflight checks."""

from __future__ import annotations

QUALITY_PASS_THRESHOLD = 70

QUALITY_DIMENSIONS = (
    {
        "field": "coverage_score",
        "key": "coverage",
        "label": "覆盖度",
        "weight": 30,
        "description": "是否解决核心问题、覆盖用户需求的关键维度",
    },
    {
        "field": "correctness_score",
        "key": "correctness",
        "label": "正确性",
        "weight": 30,
        "description": "回答是否准确、是否存在幻觉或错误引用",
    },
    {
        "field": "constraint_score",
        "key": "constraint",
        "label": "约束遵守",
        "weight": 20,
        "description": "是否遵守权限限制、边界条件与明确约束",
    },
    {
        "field": "actionability_score",
        "key": "actionability",
        "label": "可行动性",
        "weight": 20,
        "description": "输出是否具体到可直接用于业务决策或下一步执行",
    },
)


def build_quality_dimension_lines() -> str:
    return "\n".join(
        f"{index}. {item['field']}（{item['label']} {item['weight']}%）：{item['description']}"
        for index, item in enumerate(QUALITY_DIMENSIONS, start=1)
    )


def build_quality_json_example() -> str:
    return (
        '{"score": 75, "coverage_score": 80, "correctness_score": 70, '
        '"constraint_score": 75, "actionability_score": 60, '
        '"deductions": [{"dimension": "correctness", "points": -15, '
        '"reason": "引用了不存在的字段", "fix_suggestion": "限制输出字段白名单", '
        '"status": "FIXED 或 NEW"}], '
        '"reason": "主问题一句话", "fix_suggestion": "整改动作一句话"}'
    )
