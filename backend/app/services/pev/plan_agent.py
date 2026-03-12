"""PlanAgent：使用 lite 模型生成/重新生成 PEV 执行计划。"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway
from app.services.pev.prompts import (
    PLAN_SYSTEM,
    PLAN_USER,
    REPLAN_SYSTEM,
    REPLAN_USER,
)

logger = logging.getLogger(__name__)


def _parse_plan_json(raw: str) -> dict:
    """从 LLM 输出中提取 JSON 计划，支持 markdown 代码块包裹。"""
    cleaned = raw.strip()
    # 去掉 markdown 代码块
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
    return json.loads(cleaned)


def _context_summary(context: dict) -> str:
    if not context:
        return "（暂无）"
    keys = list(context.keys())
    return f"已完成步骤：{', '.join(keys)}（共 {len(keys)} 步）"


class PlanAgent:

    async def generate_plan(
        self,
        goal: str,
        scenario: str,
        context: dict,
        db: Session,
    ) -> dict:
        """生成执行计划，返回 plan dict（含 steps 列表）。"""
        lite_config = llm_gateway.get_lite_config()
        # PlanAgent 允许更长的输出
        lite_config = {**lite_config, "max_tokens": 2048}

        messages = [
            {"role": "system", "content": PLAN_SYSTEM},
            {
                "role": "user",
                "content": PLAN_USER.format(
                    scenario=scenario,
                    goal=goal,
                    context_summary=_context_summary(context),
                ),
            },
        ]

        raw, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=messages,
            temperature=0.2,
        )

        try:
            plan = _parse_plan_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"PlanAgent: 计划 JSON 解析失败: {e}\n原始输出: {raw[:500]}")
            raise ValueError(f"PlanAgent 生成的计划格式无效: {e}") from e

        if not isinstance(plan.get("steps"), list):
            raise ValueError("PlanAgent 返回的计划缺少 steps 字段")

        return plan

    async def replan(
        self,
        original_plan: dict,
        failed_step: dict,
        verify_feedback: str,
        context: dict,
        db: Session,
    ) -> dict:
        """在某步骤验证失败且重试耗尽后，生成调整后的计划。"""
        lite_config = llm_gateway.get_lite_config()
        lite_config = {**lite_config, "max_tokens": 2048}

        original_steps = original_plan.get("steps") or []
        messages = [
            {"role": "system", "content": REPLAN_SYSTEM},
            {
                "role": "user",
                "content": REPLAN_USER.format(
                    scenario=original_plan.get("scenario", "unknown"),
                    goal=original_plan.get("goal", ""),
                    original_step_count=len(original_steps),
                    failed_step_key=failed_step.get("step_key", ""),
                    failed_step_desc=failed_step.get("description", ""),
                    verify_feedback=verify_feedback,
                    context_summary=_context_summary(context),
                ),
            },
        ]

        raw, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=messages,
            temperature=0.2,
        )

        try:
            plan = _parse_plan_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"PlanAgent.replan: JSON 解析失败: {e}\n原始输出: {raw[:500]}")
            raise ValueError(f"PlanAgent replan 格式无效: {e}") from e

        if not isinstance(plan.get("steps"), list):
            raise ValueError("PlanAgent replan 缺少 steps 字段")

        return plan


plan_agent = PlanAgent()
