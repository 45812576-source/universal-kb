"""Brainstorming builtin tool.

Drives a structured brainstorming session one step at a time.
The LLM calls this tool to determine what to do next, then acts on the result.

Stages:
  questioning  → not enough context yet, return ONE question
  proposing    → enough context, return 2-3 approaches for user to choose
  ready        → user has chosen an approach, proceed to output

Input params:
{
  "topic": "制定销售部季度绩效考核方案",
  "context_so_far": "公司是SaaS企业，本季度战略重点是留存，销售团队8人",
  "required_context": [
    {"key": "company_bg",  "label": "公司背景",   "desc": "行业/规模/阶段",          "example": "B端SaaS，50人，成长期"},
    {"key": "strategy",    "label": "当期战略目标", "desc": "本季度公司最重要的事",    "example": "NDR提升至110%"},
    {"key": "team_size",   "label": "团队规模",    "desc": "涉及考核的人数和层级",     "example": "销售8人，含2名主管"},
    {"key": "pain_points", "label": "当前痛点",    "desc": "现有考核的主要问题",       "example": "指标全是新签，忽略续费质量"}
  ],
  "chosen_approach": null   // null = still collecting; set to approach index (1/2/3) when user has chosen
}

Output (still questioning):
  {"stage": "questioning", "question": "贵司本季度最重要的战略目标是什么？例如：新客增长、续费留存、某产品线GMV"}

Output (proposing approaches):
  {"stage": "proposing", "approaches": [
    {"index": 1, "title": "OKR分解法", "desc": "...", "pros": "...", "cons": "...", "recommended": true},
    {"index": 2, "title": "KPI对标法", "desc": "...", "pros": "...", "cons": "..."},
    {"index": 3, "title": "BSC平衡计分卡", "desc": "...", "pros": "...", "cons": "..."}
  ]}

Output (ready to generate):
  {"stage": "ready", "summary": "...collected context summary..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

logger = logging.getLogger(__name__)

_BRAINSTORM_PROMPT = """你是一个头脑风暴引导助手，帮助用户逐步厘清需求。

【任务主题】
{topic}

【已收集的背景信息】
{context_so_far}

【需要收集的背景信息清单】
{checklist}

请判断当前阶段：

1. 如果背景信息还不充分（缺少1项及以上关键信息），返回 stage=questioning，并给出**一个**最重要的追问。
   追问要具体，带例子，一句话。

2. 如果背景信息已充分，返回 stage=proposing，给出**2-3个**不同的方案方向，每个方案包含：
   - title：方案名（简短）
   - desc：一句话说明核心思路
   - pros：主要优势
   - cons：主要局限
   - recommended：是否推荐（只有1个可以是true）

只返回JSON，格式如下：
// 追问时：
{{"stage": "questioning", "question": "..."}}

// 提方案时：
{{"stage": "proposing", "approaches": [{{"index": 1, "title": "...", "desc": "...", "pros": "...", "cons": "...", "recommended": true}}, ...]}}
"""

_CHECKLIST_ITEM = "- [{key}] {label}：{desc}{example}"


def execute(params: dict, db=None) -> dict:
    return asyncio.run(_execute_async(params))


async def _execute_async(params: dict) -> dict:
    topic = params.get("topic", "")
    context_so_far = params.get("context_so_far", "").strip()
    required_context = params.get("required_context", [])
    chosen_approach = params.get("chosen_approach")

    # User has already chosen an approach — signal ready
    if chosen_approach is not None:
        return {
            "stage": "ready",
            "summary": context_so_far,
            "chosen_approach": chosen_approach,
        }

    if not required_context:
        return {"stage": "ready", "summary": context_so_far}

    # Build checklist text
    checklist_lines = []
    for item in required_context:
        example = f"  例如：{item['example']}" if item.get("example") else ""
        checklist_lines.append(
            _CHECKLIST_ITEM.format(
                key=item["key"],
                label=item["label"],
                desc=item["desc"],
                example=example,
            )
        )
    checklist = "\n".join(checklist_lines)

    prompt = _BRAINSTORM_PROMPT.format(
        topic=topic,
        context_so_far=context_so_far or "（暂无）",
        checklist=checklist,
    )

    try:
        from app.services.llm_gateway import llm_gateway
        lite_config = llm_gateway.get_lite_config()
        raw, _ = await llm_gateway.chat(
            model_config=lite_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=800,
        )
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        return result
    except Exception as e:
        logger.error(f"Brainstorming tool failed: {e}")
        return {"stage": "questioning", "question": "能告诉我更多关于这个任务的背景吗？"}
