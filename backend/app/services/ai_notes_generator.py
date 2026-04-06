"""AI 结构化笔记生成服务。

按文件类型选择 prompt 模板，生成结构化笔记 HTML。
长文档走 MapReduce（复用 knowledge_understanding 的分块逻辑）。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.services.llm_gateway import SLOT_REGISTRY, llm_gateway

logger = logging.getLogger(__name__)

# 注册 LLM slot
SLOT_REGISTRY["knowledge.ai_notes"] = {
    "name": "AI 笔记生成",
    "category": "知识",
    "desc": "上传文件后生成结构化阅读笔记/会议纪要 HTML",
    "fallback": "default",
}

# 长文档阈值（字符数）
_LONG_DOC_THRESHOLD = 3000

# ── Prompt 模板 ──────────────────────────────────────────────────────────────

_AUDIO_PROMPT = """你是一位专业的会议纪要整理助手。请根据以下音频转写文本，生成结构化的会议纪要 HTML。

## 音频转写文本
{content}

---
请严格按以下 HTML 结构输出，不含 markdown 代码块包装：

<h2>会议主题</h2>
<p>（一句话概括会议核心主题）</p>

<h2>议题与讨论</h2>
<ul>
<li><strong>议题1</strong>：讨论要点...</li>
<li><strong>议题2</strong>：讨论要点...</li>
</ul>

<h2>关键结论</h2>
<ul>
<li>结论1...</li>
<li>结论2...</li>
</ul>

<h2>待办事项</h2>
<ul data-type="taskList">
<li data-type="taskItem" data-checked="false">待办1（负责人/截止日期）</li>
<li data-type="taskItem" data-checked="false">待办2（负责人/截止日期）</li>
</ul>

规则：
- 保留原文中的具体数据点（数字、百分比、金额、日期）
- 保留关键人名/角色
- 待办事项尽可能标注负责人和截止时间
- 如果内容不像会议（如访谈、演讲），请调整标题和结构适配"""

_DOCUMENT_PROMPT = """你是一位专业的文档分析助手。请根据以下文档内容，生成结构化的阅读笔记 HTML。

## 文件名: {filename}
## 文档内容
{content}

---
请严格按以下 HTML 结构输出，不含 markdown 代码块包装：

<h2>文档概述</h2>
<p>（2-3句话概括文档核心内容和目的）</p>

<h2>核心要点</h2>
<ul>
<li>要点1...</li>
<li>要点2...</li>
<li>要点3...</li>
</ul>

<h2>关键数据</h2>
<ul>
<li>数据点1...</li>
<li>数据点2...</li>
</ul>

<h2>结论与建议</h2>
<ul>
<li>结论/建议1...</li>
<li>结论/建议2...</li>
</ul>

规则：
- 保留原文中的具体数据点（数字、百分比、金额、日期），不得模糊化
- 核心要点控制在 3-7 条
- 如果文档中没有明确的数据，「关键数据」部分可以写"本文档无量化数据"
- 结论与建议部分提炼文档的核心观点和行动指引"""

_IMAGE_PROMPT = """你是一位图片分析助手。请根据以下图片内容描述，生成结构化的图片笔记 HTML。

## 图片信息
文件名: {filename}
内容描述:
{content}

---
请按以下 HTML 结构输出，不含 markdown 代码块包装：

<h2>图片概述</h2>
<p>（描述图片主要内容）</p>

<h2>关键信息</h2>
<ul>
<li>信息点1...</li>
<li>信息点2...</li>
</ul>

<h2>使用建议</h2>
<p>（这张图片适合用于什么场景）</p>"""

# 长文档 Map 阶段 prompt
_CHUNK_MAP_PROMPT = """请对以下文档片段提取关键信息摘要。

要求：
- 保留原文中的具体数据点（数字、百分比、金额、日期）
- 保留关键实体名称
- 保留核心结论和因果关系
- 输出 150-300 字，纯文本

## 文档片段（第 {chunk_idx}/{total_chunks} 段）
{chunk_text}

---
请直接输出摘要文本："""

# 长文档 Reduce 阶段 prompt
_REDUCE_NOTES_PROMPT = """你是一位专业的文档分析助手。以下是一篇长文档各段落的摘要，请合并生成结构化的阅读笔记 HTML。

## 文件名: {filename}
## 各段摘要
{chunk_summaries}

---
请严格按以下 HTML 结构输出，不含 markdown 代码块包装：

<h2>文档概述</h2>
<p>（2-3句话概括文档核心内容和目的）</p>

<h2>核心要点</h2>
<ul>
<li>要点1...</li>
<li>要点2...</li>
</ul>

<h2>关键数据</h2>
<ul>
<li>数据点1...</li>
</ul>

<h2>结论与建议</h2>
<ul>
<li>结论/建议1...</li>
</ul>

规则：
- 保留原文中的具体数据点，不得模糊化
- 核心要点控制在 3-7 条"""


# ── 文件类型分组 ─────────────────────────────────────────────────────────────

_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".svg"}
_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
                  ".csv", ".txt", ".md", ".html", ".htm"}


def _detect_type(file_ext: str) -> str:
    """根据扩展名判断文件类型组。"""
    ext = (file_ext or "").lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    return "document"


def _clean_html_output(raw: str) -> str:
    """清理 LLM 输出中的 markdown 代码块包装。"""
    raw = raw.strip()
    if raw.startswith("```"):
        # 去掉首行 ```html 或 ```
        lines = raw.split("\n")
        lines = lines[1:]  # 去掉 ```html
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw.strip()


async def generate_ai_notes(
    knowledge_id: int,
    content: str,
    filename: str,
    file_ext: str,
    db: Session,
) -> str:
    """为知识条目生成 AI 结构化笔记 HTML。

    Returns: 生成的 HTML 字符串
    Raises: Exception on failure
    """
    if not content or not content.strip():
        raise ValueError("无内容可生成笔记")

    file_type = _detect_type(file_ext)
    model_config = llm_gateway.resolve_config(db, "knowledge.ai_notes")

    is_long = len(content) > _LONG_DOC_THRESHOLD and file_type == "document"

    if file_type == "audio" or file_type == "video":
        return await _generate_audio_notes(content, model_config)
    elif file_type == "image":
        return await _generate_image_notes(content, filename, model_config)
    elif is_long:
        return await _generate_long_doc_notes(content, filename, db, model_config)
    else:
        return await _generate_doc_notes(content, filename, model_config)


async def _generate_audio_notes(content: str, model_config: dict) -> str:
    """音频/视频 → 会议纪要。"""
    prompt = _AUDIO_PROMPT.format(content=content[:8000])
    result, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )
    return _clean_html_output(result)


async def _generate_image_notes(content: str, filename: str, model_config: dict) -> str:
    """图片 → 内容描述笔记。"""
    prompt = _IMAGE_PROMPT.format(content=content[:3000], filename=filename or "未知")
    result, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1000,
    )
    return _clean_html_output(result)


async def _generate_doc_notes(content: str, filename: str, model_config: dict) -> str:
    """短文档 → 结构化阅读笔记。"""
    prompt = _DOCUMENT_PROMPT.format(content=content[:5000], filename=filename or "未知")
    result, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )
    return _clean_html_output(result)


async def _generate_long_doc_notes(
    content: str, filename: str, db: Session, model_config: dict
) -> str:
    """长文档 → Map-Reduce 两阶段笔记。"""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=3000,
        chunk_overlap=300,
        separators=["\n\n", "\n", "。", "；", "，", " "],
    )
    chunks = splitter.split_text(content)
    summaries: list[str] = []

    for idx, chunk in enumerate(chunks, 1):
        prompt = _CHUNK_MAP_PROMPT.format(
            chunk_idx=idx, total_chunks=len(chunks), chunk_text=chunk
        )
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )
        summaries.append(result.strip())

    chunk_summaries_text = "\n\n---\n\n".join(
        f"【第{i+1}段】{s}" for i, s in enumerate(summaries)
    )

    prompt = _REDUCE_NOTES_PROMPT.format(
        filename=filename or "未知",
        chunk_summaries=chunk_summaries_text,
    )
    result, _ = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )
    return _clean_html_output(result)
