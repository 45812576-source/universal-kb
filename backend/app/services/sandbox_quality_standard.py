"""Shared quality standard for sandbox evaluation and preflight checks."""

from __future__ import annotations

QUALITY_PASS_THRESHOLD = 70
QUALITY_SCORE_TEMPERATURE = 0.1  # 比 0.0 更稳定，避免贪心退化
QUALITY_SCORE_MAX_TOKENS = 1024

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


def build_quality_score_prompt(
    *,
    skill_name: str,
    description: str,
    test_input: str,
    response: str,
    dimension_lines: str | None = None,
    json_example: str | None = None,
    system_prompt: str = "",
    knowledge_summary: str = "",
    permission_context: str = "",
    baseline_section: str = "",
) -> str:
    """统一评分 prompt，供 preflight 和 interactive sandbox 共用。"""
    if dimension_lines is None:
        dimension_lines = build_quality_dimension_lines()
    if json_example is None:
        json_example = build_quality_json_example()

    parts: list[str] = [
        "你是 AI Skill 质量评审官。评估以下输出是否真正解决了 Skill 定义的问题。",
        "",
        f"Skill 名称：{skill_name}",
        f"Skill 目标：{description}",
    ]

    if system_prompt:
        parts += ["", f"System Prompt 摘要（前 1500 字）：\n{system_prompt[:1500]}"]

    if knowledge_summary:
        parts += ["", f"知识库检索结果：\n{knowledge_summary}"]

    if permission_context:
        parts += ["", f"权限上下文：{permission_context}"]

    parts += [
        "",
        f"测试输入：\n{test_input}",
        "",
        f"AI 输出：\n{response}",
        "",
        f"评分标准（四维度各 0-100，从 100 分起扣，逐维度独立评分）：",
        dimension_lines,
        "",
        "稳定性要求：",
        "- 从 100 分起扣，逐维度独立评分",
        "- 相同质量的回复在不同轮次应得到一致的分数（波动 ≤ 5 分）",
        "- 不要因为格式偏好或措辞风格扣分，只关注实质内容",
        "",
        "补充要求：",
        "- 正确性要结合知识库命中情况判断；如果知识库已有明确支撑但回复未使用、误用或编造，应扣分",
        "- 约束遵守要检查是否越出 Skill 目标、输入边界、权限边界或约束条件；无法完全验证权限时，重点判断是否出现越权臆断",
        "- 可行动性要检查输出是否给出可执行结论、步骤、判断依据，而不是停留在空泛描述",
        "- 对每个扣分项，说明扣分维度、扣分值、原因和修复建议",
    ]

    if baseline_section:
        parts.append(baseline_section)

    parts += [
        "",
        f"只输出 JSON（不要其他内容）：",
        json_example,
    ]

    return "\n".join(parts)
