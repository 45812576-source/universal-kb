"""InputEvaluator: 在调用主 LLM 之前评估用户输入的置信度。

置信度 >= 60 → 放行
置信度 < 60  → 返回缺失项，要求用户补充

required_inputs 格式（存在 SkillVersion.required_inputs JSON 字段）：
[
  {"key": "product",  "label": "具体产品",   "desc": "你们卖的是什么产品",        "example": "XX宠物冻干猫粮"},
  {"key": "channel",  "label": "销售渠道",   "desc": "主要在哪里卖",              "example": "抖音自播+达人分销"},
  {"key": "target",   "label": "目标人群",   "desc": "主要客群是谁",              "example": "25-35岁城市养猫女性"},
  {"key": "goal",     "label": "策划目标",   "desc": "这次策划要达成什么结果",    "example": "单月GMV破50万"},
]

每个字段分值 = 100 / 总字段数，全齐才放行。
"""
from __future__ import annotations

import json
import logging
import re

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_EVAL_PROMPT = """你是一个输入完整性评估助手。你的任务是判断用户提供的信息是否足够让 AI 完成下面的工作。

【工作目标】
{purpose}

【需要的信息清单】
{input_checklist}

【用户目前提供的所有信息】（对话历史）
{history}

请逐项检查每个信息是否已被提供（可以是直接说明，也可以从上下文推断）。

重要规则：
1. 只要用户在对话中的任意位置提到过某项信息，哪怕表达不完整，也算已提供（provided=true）
2. 如果 AI 在之前的对话中已经问过某个问题，且用户已经回答（哪怕回答简短），不得再次追问同一问题
3. missing_questions 只列出从未被问过或用户完全没有回答的字段
4. 宁可放行（score 偏高），也不要对同一信息反复追问

只返回 JSON，格式：
{{
  "score": 0-100的整数（每项满足加对应分值），
  "provided": {{"key": true/false, ...}},
  "missing_labels": ["缺少的信息标签1", ...],
  "missing_questions": ["针对缺失信息的追问1（用自然语言，一句话，带示例）", ...]
}}

示例输出：
{{
  "score": 50,
  "provided": {{"product": true, "channel": false, "target": true, "goal": false}},
  "missing_labels": ["销售渠道", "策划目标"],
  "missing_questions": ["你主要在哪些渠道销售？（如抖音自播、天猫旗舰店等）", "这次策划希望达成什么目标？（如单月GMV破50万、新客增长30%等）"]
}}"""


class InputEvaluator:

    def build_checklist_text(self, required_inputs: list[dict]) -> str:
        lines = []
        per_score = round(100 / len(required_inputs)) if required_inputs else 100
        for item in required_inputs:
            score = item.get("score", per_score)
            key = item.get("key", "unknown")
            label = item.get("label", key)
            desc = item.get("desc", "")
            lines.append(
                f"- [{key}] {label}（{score}分）：{desc}"
                + (f"  例如：{item['example']}" if item.get("example") else "")
            )
        return "\n".join(lines)

    async def evaluate(
        self,
        purpose: str,
        required_inputs: list[dict],
        history_messages: list,
        threshold: int = 60,
        current_message: str = "",
    ) -> dict:
        """
        返回:
          {"pass": True/False, "score": int, "missing_questions": [...]}
        current_message: 当前这条消息（含文件内容），补充进历史末尾一起评估
        """
        if not required_inputs:
            return {"pass": True, "score": 100, "missing_questions": []}

        history_parts = [
            f"{m.role.value}: {m.content}" for m in history_messages[-12:]
        ]
        if current_message:
            history_parts.append(f"user: {current_message}")
        history = "\n".join(history_parts)
        checklist = self.build_checklist_text(required_inputs)

        prompt = _EVAL_PROMPT.format(
            purpose=purpose,
            input_checklist=checklist,
            history=history,
        )

        try:
            lite_config = llm_gateway.get_lite_config()
            raw, _ = await llm_gateway.chat(
                model_config=lite_config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=600,
            )
            raw = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(raw)
            score = int(result.get("score", 0))
            return {
                "pass": score >= threshold,
                "score": score,
                "missing_questions": result.get("missing_questions", []),
                "missing_labels": result.get("missing_labels", []),
            }
        except Exception as e:
            logger.warning(f"InputEvaluator failed: {e}")
            # 出错时放行，不阻塞用户
            return {"pass": True, "score": 100, "missing_questions": []}


input_evaluator = InputEvaluator()
