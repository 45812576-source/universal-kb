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
    # M26: 去掉所有 markdown 代码块标记（不只首尾）
    cleaned = re.sub(r"```(?:json)?\s*\n?", "", cleaned)
    cleaned = cleaned.strip()
    return json.loads(cleaned)


def _validate_step_keys(steps: list[dict]) -> None:
    """M23: 校验 step_key 唯一性。"""
    seen: set[str] = set()
    for s in steps:
        key = s.get("step_key", "")
        if not key:
            raise ValueError("PEV 计划中存在无 step_key 的步骤")
        if key in seen:
            raise ValueError(f"PEV 计划中 step_key 重复: {key}")
        seen.add(key)


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
        plan_config = llm_gateway.resolve_config(db, "pev.plan")
        # PlanAgent 允许更长的输出
        plan_config = {**plan_config, "max_tokens": 2048}

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
            model_config=plan_config,
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

        _validate_step_keys(plan["steps"])  # M23
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
        plan_config = llm_gateway.resolve_config(db, "pev.plan")
        plan_config = {**plan_config, "max_tokens": 2048}

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
            model_config=plan_config,
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

        _validate_step_keys(plan["steps"])  # M23

        # M24: 检查新 step_key 是否与已完成步骤冲突
        completed_keys = set(context.keys()) if context else set()
        for s in plan["steps"]:
            if s["step_key"] in completed_keys:
                raise ValueError(
                    f"Replan step_key '{s['step_key']}' 与已完成步骤冲突"
                )

        return plan


plan_agent = PlanAgent()
