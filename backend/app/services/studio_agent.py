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

_STUDIO_SYSTEM = """你是 Skill Studio 的创作助手，帮助用户设计、编写、测试和优化 AI Skill（系统提示词）。

## 你的能力
1. **引导创作**：当用户想新建 skill 时，主动追问目标、使用场景、目标用户、输入/输出、约束。信息不足时只问最关键的 1-2 个问题。
2. **生成草稿**：信息足够时生成完整 system prompt，用结构化格式输出。
3. **修改建议**：基于用户当前编辑器内容提出 diff，而不是重写整个 skill。
4. **测试执行**：当用户要求测试时，模拟一个典型输入并评估当前 skill 的表现，返回测试报告。
5. **解释说明**：解释某个 skill 设计决策或者分析问题。

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
