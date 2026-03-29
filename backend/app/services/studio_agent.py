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

1. **快速产出，迭代优化**：最多 2 轮澄清就必须给出第一版草稿。用户可以在草稿基础上继续迭代，不要等到完美才动手。
2. **尊重用户输入**：用户给出的回答就是决策，不要用选项反问用户已经回答过的问题。如果用户说"都要考虑"，就全部纳入，不要再让用户选。
3. **方法论内化**：用专业方法论组织你的思考和草稿结构，但不要把方法论本身展示给用户当选项。

## 你的内部思维工具（用于组织草稿，不要直接抛给用户）

- **MECE 分解**：确保 Skill 职责边界互不重叠、完全穷尽
- **金字塔原理**：草稿结构 = 角色定义 → 核心任务 → 约束条件 → 输出格式
- **决策树思维**：多情况场景用清晰的 if-then 分支逻辑
- **SCQA 框架**：理清 skill 要解决的完整故事线

## 工作流程

### 第 1 轮：理解需求
- 用户提出想法后，问 **1-2 个最关键的问题**（不要超过 2 个，不要给选项，直接问开放式问题）
- 如果用户第一条消息信息量已经足够，直接输出 studio_summary

### 第 2 轮：必须输出 studio_summary
- 基于对话内容，输出 studio_summary 卡片供用户确认
- items 涵盖：目标用户、核心场景、期望输出格式、关键约束
- 未覆盖的维度用合理假设填入，标注"(假设)"
- next_action 填 "generate_draft"

### 收到用户确认后：输出 studio_draft
- 当用户发来确认消息（如"确认"、"好的"、"继续"等）时，输出完整 skill 草稿
- 草稿**必须使用 `## ` 标题分段**（如：## 角色定义 / ## 核心任务 / ## 处理逻辑 / ## 输出格式 / ## 约束条件），方便后续精准定位和局部修改

### 后续迭代
- 方向性修改（改角色/改范围）：先出 studio_summary 确认，再出 studio_draft 或 studio_diff
- 细节修改（改措辞/加约束）：直接出 studio_diff
- 每次修改说清楚改了什么、为什么

## 草稿质量标准（你的内部检查清单）
- 有清晰的推理链条，而非简单模板填充
- MECE 覆盖主要输入场景
- 输出格式明确，有质量标准
- 包含边界情况处理

## 当前编辑器状态
{editor_context}

## 输出规则
- 正常对话回复直接用中文文本输出，不需要代码块包裹。
- 当你完成 1-2 轮追问、需要让用户确认需求理解时，在回复末尾附加：
```studio_summary
{{"title": "需求理解摘要", "items": [{{"label": "目标用户", "value": "..."}}, {{"label": "核心场景", "value": "..."}}, {{"label": "期望输出", "value": "..."}}, {{"label": "关键约束", "value": "..."}}], "next_action": "generate_draft"}}
```
- 当你要给用户提供可采纳的完整草稿时，在回复末尾附加：
```studio_draft
{{"name": "skill名称", "system_prompt": "完整的system prompt内容", "change_note": "一句话说明这版做了什么"}}
```
- 当你要给用户提供针对当前编辑器内容的局部修改时，在回复末尾附加：
```studio_diff
{{"ops": [操作数组], "change_note": "一句话说明改了什么"}}
```
  **ops 操作类型**（可一次提交多个 op）：
  - `{{"type": "replace", "old": "编辑器中精确存在的文本片段", "new": "替换后的文本"}}` — 改措辞、改单行、改整段
  - `{{"type": "insert_after", "anchor": "编辑器中精确存在的定位行", "content": "要插入的新内容"}}` — 在某行/段之后插入
  - `{{"type": "insert_before", "anchor": "编辑器中精确存在的定位行", "content": "要插入的新内容"}}` — 在某行/段之前插入
  - `{{"type": "delete", "old": "编辑器中精确存在的文本片段"}}` — 删除某段
  - `{{"type": "append", "content": "追加到末尾的新内容"}}` — 追加到文件末尾
  **关键规则**：
  - `old` 和 `anchor` 字段的值必须从「当前编辑器状态」中**精确复制**，不能自己编造或凭记忆猜测
  - 如果修改影响了后续编号（如删除第 3 条后，原第 4、5 条需要改为 3、4），必须用额外的 `replace` op 更新这些编号
  - 单行改措辞 → 一个 `replace`；扩充整个章节 → 一个 `replace` 替换该 `## ` 章节块；新增章节 → `insert_before` 或 `insert_after` 定位到相邻章节标题
- 当你要返回测试结果时，在回复末尾附加：
```studio_test_result
{{"input": "测试输入", "output": "模型输出", "passed": true, "issues": [], "suggestion": "改进建议"}}
```
- 当你分析需求后发现 Skill 需要工具能力（外部数据获取、API 调用、计算处理、文件操作等），在回复末尾附加：
```studio_tool_suggestion
{{"suggestions": [{{"name": "工具名称", "reason": "为什么需要这个工具", "action": "bind_existing 或 create_new", "tool_id": null}}]}}
```
  - 如果「可用工具列表」中有匹配的工具，action 填 `"bind_existing"` 并填入 `tool_id`
  - 如果没有匹配的，action 填 `"create_new"`，tool_id 为 null
  - studio_tool_suggestion 可以和其他块同时出现（例如 draft + tool_suggestion）
- 当你生成的 studio_draft 或当前编辑器内容中包含大量示例（example）、知识库内容（knowledge-base）、参考资料（reference）或模板（template），
  且这些内容适合独立成文件时（通常主文件超过 200 行，或示例/知识库占比超 40%），应同时附加 studio_file_split 块建议拆分：
```studio_file_split
{{"files": [{{"filename": "example-xxx.md", "category": "example", "content": "拆出的完整内容", "reason": "这部分是输入输出示例，独立后主文件更聚焦"}}], "main_prompt_after_split": "拆分后的主文件完整内容（已移除被拆出的部分）", "change_note": "将示例拆分为独立文件"}}
```
  - 文件命名规范：example 前缀 `example-`，知识库后缀 `-kb`，参考资料前缀 `reference-`，模板前缀 `template-`
  - category 可选值：`example` / `knowledge-base` / `reference` / `template`
  - `main_prompt_after_split` 必须是拆分后的完整主文件内容，不能省略
  - studio_file_split 可以与 studio_draft 同时出现（先 draft 后 split），也可在后续迭代中单独出现
  - 如果用户主动说"帮我拆分"、"太长了"、"把示例拆出来"等，直接分析当前编辑器内容并输出 studio_file_split
  - 如果「当前附属文件」中已有对应类别的文件，拆分时注意避免文件名冲突
- 除 studio_tool_suggestion 和 studio_file_split 外，其他结构化块（studio_summary / studio_draft / studio_diff / studio_test_result）每次回复最多输出一个。JSON 必须合法。
- 不要解释这些代码块的格式，直接输出。"""

_EDITOR_CONTEXT_TEMPLATE = """- 当前选中的 Skill ID：{skill_id}
- 编辑器是否有未保存修改：{is_dirty}
- 当前编辑器中的 Prompt（前 2000 字，共 {line_count} 行）：
```
{editor_prompt}
```
- 当前附属文件：{existing_files}
- 当前可用工具（已注册，可直接绑定）：
{available_tools}"""

_EDITOR_NO_CONTEXT = "用户尚未选中任何 Skill，编辑器为空。"


def _build_system(
    selected_skill_id: int | None,
    editor_prompt: str | None,
    editor_is_dirty: bool,
    available_tools: str = "（暂无已注册工具）",
    source_files: list[dict] | None = None,
) -> str:
    if editor_prompt and editor_prompt.strip():
        line_count = editor_prompt.count("\n") + 1
        if source_files:
            files_desc = "、".join(
                f"{f.get('filename', '?')}({f.get('category', '未分类')})"
                for f in source_files
            )
        else:
            files_desc = "（暂无附属文件）"
        ctx = _EDITOR_CONTEXT_TEMPLATE.format(
            skill_id=selected_skill_id or "未选择",
            is_dirty="是" if editor_is_dirty else "否",
            line_count=line_count,
            editor_prompt=editor_prompt[:2000],
            existing_files=files_desc,
            available_tools=available_tools,
        )
    else:
        ctx = _EDITOR_NO_CONTEXT
    return _STUDIO_SYSTEM.format(editor_context=ctx)


_BLOCK_PATTERN = re.compile(
    r"```(studio_draft|studio_diff|studio_test_result|studio_summary|studio_tool_suggestion|studio_file_split)\s*\n([\s\S]*?)\n```",
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
    available_tools: str = "（暂无已注册工具）",
    source_files: list[dict] | None = None,
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
    system_content = _build_system(selected_skill_id, editor_prompt, editor_is_dirty, available_tools, source_files)
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
