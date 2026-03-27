"""Studio Agent — 专为 Skill Studio 设计的 orchestrator。

职责：
- 基于用户消息、对话历史和前端传入的编辑上下文（editor_prompt、selected_skill_id 等）
  动态构建 LLM messages。
- 流式调用 LLM，从响应中提取结构化 studio_* 代码块并发出对应 SSE 事件。
- 支持草稿测试：若用户要求测试，基于 editor_prompt 构造一次单轮推理并返回结果。

使用方：conversations.py 的 skill_studio 快速路径。
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

# ── System prompt for studio agent ────────────────────────────────────────────

_STUDIO_SYSTEM = """你是 Skill Studio 的高级创作顾问。你的使命是通过专业咨询方法论，引导用户构建有深度、架构正确、能真正解决问题的 AI Skill（系统提示词）。

## 核心原则

**不要急于给答案，先追根问底。** 一个好的 Skill 来自对问题的深刻理解，而非表面的 prompt 拼凑。

## 你的咨询工具箱

根据场景灵活运用以下方法论（不必每次都用，选择最适合当前对话的）：

1. **丰田五问（5 Whys）**：当用户说"我想做 X"时，连续追问为什么，直到触及真正的业务痛点。
   - 用户："我要做一个写周报的 skill" → 为什么需要？谁看？看什么？现在的痛点是什么？
2. **MECE 分解**：确保 Skill 的职责边界互不重叠、完全穷尽。
   - 梳理输入类型、输出格式、异常情况，确认无遗漏无重复。
3. **金字塔原理**：先结论后论据。Skill 的 prompt 结构应该是：角色定义 → 核心任务 → 约束条件 → 输出格式。
4. **SCQA 框架**：Situation → Complication → Question → Answer，帮用户理清 skill 要解决的完整故事线。
5. **决策树思维**：当 skill 需要处理多种情况时，帮用户构建清晰的 if-then 分支逻辑。

## 工作流程

### 阶段一：需求澄清（必经）
- 用户提出想法时，**不要直接生成草稿**
- 用 1-2 个关键问题深挖：目标用户是谁？解决什么具体问题？成功标准是什么？
- 信息严重不足时，可以给出一个"假设框架"让用户确认或修正

### 阶段二：架构设计
- 信息充分后，先用文字描述 skill 的设计思路（角色、核心逻辑、输入输出、边界）
- 指出潜在的坑和需要用户确认的决策点
- 用户确认后再生成完整草稿

### 阶段三：迭代优化
- 基于编辑器中的现有 prompt，提出具体改进建议
- 用 diff 而非重写，让用户清楚改了什么、为什么改

### 阶段四：测试验证
- 当用户要求测试时，构造有代表性的测试用例（包括边界情况）
- 评估输出质量，给出具体可行的改进建议

## Skill 质量评估维度
当审视一个 Skill 时，从以下维度检查：
- **思维架构**：是否有清晰的推理链条，而非简单的模板填充？
- **边界完备**：是否 MECE 覆盖了所有输入场景？异常情况有处理吗？
- **输出可控**：输出格式是否明确？是否有质量标准？
- **可测试性**：能否用具体用例验证 skill 是否达标？

## 当前编辑器状态
{editor_context}

## 输出规则
- 正常对话回复直接用中文文本输出，不需要代码块包裹。
- 当你要给用户提供可采纳的完整草稿时，在回复末尾附加：
```studio_draft
{{"name": "skill名称", "system_prompt": "完整的system prompt内容", "change_note": "一句话说明这版做了什么"}}
```
- 当你要给用户提供针对当前编辑器内容的局部修改时，在回复末尾附加：
```studio_diff
{{"system_prompt": {{"old": "原始片段（或关键词）", "new": "修改后的完整prompt"}}}}
```
- 当你要返回测试结果时，在回复末尾附加：
```studio_test_result
{{"input": "测试输入", "output": "模型输出", "passed": true, "issues": [], "suggestion": "改进建议"}}
```
- 每次回复最多输出一个结构化块。JSON 必须合法。
- 不要解释这些代码块的格式，直接输出。"""

_EDITOR_CONTEXT_TEMPLATE = """- 当前选中的 Skill ID：{skill_id}
- 编辑器是否有未保存修改：{is_dirty}
- 当前编辑器中的 Prompt（前 2000 字）：
```
{editor_prompt}
```"""

_EDITOR_NO_CONTEXT = "用户尚未选中任何 Skill，编辑器为空。"


def _build_system(
    selected_skill_id: int | None,
    editor_prompt: str | None,
    editor_is_dirty: bool,
) -> str:
    if editor_prompt and editor_prompt.strip():
        ctx = _EDITOR_CONTEXT_TEMPLATE.format(
            skill_id=selected_skill_id or "未选择",
            is_dirty="是" if editor_is_dirty else "否",
            editor_prompt=editor_prompt[:2000],
        )
    else:
        ctx = _EDITOR_NO_CONTEXT
    return _STUDIO_SYSTEM.format(editor_context=ctx)


_BLOCK_PATTERN = re.compile(
    r"```(studio_draft|studio_diff|studio_test_result)\s*\n([\s\S]*?)\n```",
    re.IGNORECASE,
)


def _extract_events(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """从完整 LLM 输出中提取 studio_* 块，返回 (clean_text, [(event_name, payload)])。"""
    events: list[tuple[str, dict]] = []
    clean = text

    for m in _BLOCK_PATTERN.finditer(text):
        evt_name = m.group(1).lower()
        try:
            payload = json.loads(m.group(2))
            events.append((evt_name, payload))
        except Exception:
            pass
        clean = clean.replace(m.group(0), "")

    return clean.strip(), events


async def run_stream(
    db: Session,
    conv_id: int,
    workspace_system_context: str,
    history_messages: list[dict],
    user_message: str,
    model_config: dict,
    selected_skill_id: int | None = None,
    editor_prompt: str | None = None,
    editor_is_dirty: bool = False,
) -> AsyncIterator[tuple[str, dict] | str]:
    """
    流式运行 studio agent。
    yield: (event_name, data_dict) — 结构化 SSE 事件
         | str                    — keepalive ping（直接透传）
    文本 delta 通过 (content_block_delta, {...}) 和 (delta, {...}) 发出。
    流程结束前，若有 studio_* 块，先 yield 这些结构化事件，再 yield done 事件。
    """
    # 优先使用动态构建的 studio system prompt；若 workspace 没有设置 system_context 则
    # 直接使用内置 _STUDIO_SYSTEM（仍能工作，只是没有 workspace 级自定义内容）
    system_content = _build_system(selected_skill_id, editor_prompt, editor_is_dirty)
    if workspace_system_context:
        # 将 workspace system_context 追加到 studio system 之后，作为补充上下文
        system_content = system_content + "\n\n## 额外上下文\n" + workspace_system_context

    llm_messages: list[dict] = [{"role": "system", "content": system_content}]
    for m in history_messages:
        llm_messages.append(m)

    logger.info(
        f"[studio_agent] conv={conv_id} skill={selected_skill_id} "
        f"dirty={editor_is_dirty} prompt_len={len(editor_prompt or '')}"
    )

    full_content = ""
    async for item in llm_gateway.chat_stream_typed(
        model_config=model_config, messages=llm_messages, tools=None
    ):
        if isinstance(item, str):
            # keepalive ping
            yield item
            continue
        ctype, cdata = item
        if ctype == "content":
            full_content += cdata
            yield ("content_block_delta", {"index": 0, "delta": {"text": cdata}})
            yield ("delta", {"text": cdata})

    # Post-process: extract studio_* blocks
    clean_text, events = _extract_events(full_content)

    # If clean_text differs from full_content (blocks were stripped), send replace event
    if events:
        yield ("replace", {"text": clean_text})

    for evt_name, payload in events:
        yield (evt_name, payload)

    yield ("__full_content__", {"text": clean_text or full_content})


async def run_draft_test(
    system_prompt: str,
    test_input: str,
    model_config: dict,
) -> dict:
    """
    基于草稿 system_prompt 跑一次推理并返回测试报告。
    不依赖数据库中已保存的 skill version。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_input},
    ]
    try:
        response_text, _ = await llm_gateway.chat(model_config, messages, temperature=0.3, max_tokens=1000)
        return {
            "input": test_input,
            "output": response_text,
            "passed": True,
            "issues": [],
            "suggestion": "",
        }
    except Exception as e:
        return {
            "input": test_input,
            "output": "",
            "passed": False,
            "issues": [str(e)],
            "suggestion": "请检查 Prompt 是否包含必要的角色定义和任务说明",
        }
