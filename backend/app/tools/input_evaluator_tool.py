"""InputEvaluator builtin tool.

Evaluates whether the conversation has enough information to proceed.
Returns either a pass or a single follow-up question for the missing item.

Input params:
{
  "purpose": "制定Q2绩效考核方案",
  "required_inputs": [
    {"key": "role", "label": "岗位", "desc": "需要制定考核方案的岗位", "example": "产品经理"},
    {"key": "cycle", "label": "考核周期", "desc": "季度/半年/年度", "example": "季度"}
  ],
  "threshold": 60   // optional, default 60
}

Output (pass):
  {"pass": true, "score": 80}

Output (fail — always ONE question):
  {"pass": false, "score": 40, "question": "请问这次考核方案针对哪个岗位？例如：产品经理、销售"}
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def execute(params: dict, db=None) -> dict:
    """Synchronous wrapper — runs the async evaluator in an event loop."""
    return asyncio.run(_execute_async(params, db))


async def _execute_async(params: dict, db=None) -> dict:
    purpose = params.get("purpose", "")
    required_inputs = params.get("required_inputs", [])
    threshold = int(params.get("threshold", 60))

    if not required_inputs:
        return {"pass": True, "score": 100}

    from app.services.input_evaluator import input_evaluator

    # Pass empty history — the Skill's system prompt manages conversation state.
    # The LLM calling this tool should pass the relevant purpose extracted from context.
    result = await input_evaluator.evaluate(
        purpose=purpose,
        required_inputs=required_inputs,
        history_messages=[],
        current_message=purpose,
        threshold=threshold,
    )

    if result["pass"]:
        return {"pass": True, "score": result["score"]}

    # Return only the FIRST missing question — one at a time, brainstorming style
    questions = result.get("missing_questions", [])
    first_question = questions[0] if questions else "请提供更多信息以便继续。"
    return {
        "pass": False,
        "score": result["score"],
        "question": first_question,
        "remaining": len(questions) - 1,  # how many more after this one
    }
