"""AI 智能命名服务：根据文件内容自动生成标题、摘要、标签和质量评分。"""
from __future__ import annotations

import json
import logging
import re

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_NAMING_PROMPT = """你是企业知识管理系统的命名助手。请根据以下文档信息生成结构化元数据。

## 文件信息
- 原始文件名: {filename}
- 文件类型: {file_type}
- 规则预处理提取的线索: {hints}

## 文档内容（前3000字）
{content}

---
请严格返回以下 JSON，不含其他任何内容:
{{
  "title": "简明的中文标题（≤50字，描述文档核心主题）",
  "summary": "100-200字的摘要，概括文档的核心内容、关键结论和适用场景",
  "tags": {{
    "industry": ["行业标签，如 快消、美妆、电商"],
    "platform": ["平台标签，如 抖音、小红书、Meta"],
    "topic": ["主题标签，如 投放策略、ROI优化、客户案例"]
  }},
  "quality_score": 0.85
}}

## 质量评分标准（0-1）:
- 0.9+: 有独特方法论/数据洞察/完整案例，结构清晰
- 0.7-0.9: 有价值的经验总结或操作指南，但可能缺少数据支撑
- 0.5-0.7: 一般性信息，如日常沟通记录、简单笔记
- 0.3-0.5: 碎片化信息，缺少上下文
- <0.3: 无效或重复内容"""


def _extract_hints(filename: str, content: str) -> dict:
    """规则预处理：从文件名和内容中提取结构化线索。"""
    hints = {}

    # 从文件名提取日期
    date_pattern = re.search(r"(\d{4})[-_.]?(\d{1,2})[-_.]?(\d{1,2})?", filename)
    if date_pattern:
        hints["date"] = date_pattern.group(0)

    # 从文件名提取平台名
    platforms = {
        "抖音": ["抖音", "douyin", "tiktok", "巨量"],
        "快手": ["快手", "kuaishou", "ks"],
        "小红书": ["小红书", "xhs", "redbook"],
        "微信": ["微信", "wechat", "wx"],
        "百度": ["百度", "baidu", "bd"],
        "Meta": ["meta", "facebook", "fb", "instagram"],
        "Google": ["google", "gdn", "sem"],
    }
    found_platforms = []
    fname_lower = filename.lower()
    for platform, keywords in platforms.items():
        if any(kw in fname_lower for kw in keywords):
            found_platforms.append(platform)
    if found_platforms:
        hints["platforms"] = found_platforms

    # 从文件名提取文档类型线索
    doc_types = {
        "周报": ["周报", "weekly"],
        "月报": ["月报", "monthly"],
        "复盘": ["复盘", "review"],
        "方案": ["方案", "plan", "proposal"],
        "报告": ["报告", "report"],
        "案例": ["案例", "case"],
        "培训": ["培训", "training"],
    }
    for doc_type, keywords in doc_types.items():
        if any(kw in fname_lower for kw in keywords):
            hints["doc_type"] = doc_type
            break

    return hints


async def auto_name(
    content: str,
    filename: str = "",
    file_type: str = "",
) -> dict:
    """AI 自动命名：生成标题、摘要、标签和质量评分。

    Returns:
        {
            "title": str,
            "summary": str,
            "tags": {"industry": [], "platform": [], "topic": []},
            "quality_score": float
        }
    """
    if not content or not content.strip():
        return {
            "title": filename or "未命名文档",
            "summary": "",
            "tags": {"industry": [], "platform": [], "topic": []},
            "quality_score": 0.1,
        }

    hints = _extract_hints(filename, content)

    prompt = _NAMING_PROMPT.format(
        filename=filename or "未知",
        file_type=file_type or "未知",
        hints=json.dumps(hints, ensure_ascii=False) if hints else "无",
        content=content[:3000],
    )

    try:
        config = llm_gateway.get_lite_config()
        # 命名需要更多 token
        config["max_tokens"] = 1000

        import httpx
        import os
        api_key = os.getenv(config["api_key_env"], "") or config.get("api_key", "")
        resp = httpx.post(
            f"{config['api_base']}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": config["model_id"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        result_str = resp.json()["choices"][0]["message"]["content"].strip()

        # 清理 markdown 代码块
        if result_str.startswith("```"):
            result_str = result_str.split("```")[1]
            if result_str.startswith("json"):
                result_str = result_str[4:]
        result_str = result_str.strip()

        parsed = json.loads(result_str)

        # 校验和清理
        title = (parsed.get("title") or filename or "未命名文档")[:500]
        summary = (parsed.get("summary") or "")[:2000]
        tags = parsed.get("tags", {})
        if not isinstance(tags, dict):
            tags = {"industry": [], "platform": [], "topic": []}
        quality_score = max(0.0, min(1.0, float(parsed.get("quality_score", 0.5))))

        return {
            "title": title,
            "summary": summary,
            "tags": {
                "industry": tags.get("industry", [])[:10],
                "platform": tags.get("platform", [])[:10],
                "topic": tags.get("topic", [])[:10],
            },
            "quality_score": quality_score,
        }
    except Exception as e:
        logger.warning(f"AI naming failed: {e}")
        # 降级：用规则生成
        hints = _extract_hints(filename, content)
        fallback_title = filename or "未命名文档"
        return {
            "title": fallback_title,
            "summary": content[:200],
            "tags": {
                "industry": [],
                "platform": hints.get("platforms", []),
                "topic": [hints["doc_type"]] if "doc_type" in hints else [],
            },
            "quality_score": 0.5,
        }
