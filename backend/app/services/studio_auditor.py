"""Studio Auditor — LLM 驱动的 Skill 质量审计引擎。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.skill import Skill, SkillAuditResult, SkillVersion
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)


@dataclass
class AuditIssue:
    severity: str  # "high" | "medium" | "low"
    category: str  # "structure" | "clarity" | "completeness" | "safety" | "performance"
    description: str


@dataclass
class AuditResult:
    verdict: str  # "good" | "needs_work" | "poor"
    issues: list[dict] = field(default_factory=list)
    recommended_path: str = ""  # "minor_edit" | "major_rewrite" | "brainstorming_upgrade"


_AUDIT_PROMPT = """你是 Skill 质量审计专家。评估以下 Skill 的质量并给出结构化审计结论。

## Skill 信息
- 名称: {name}
- 描述: {description}
- 来源: {source_type}

## System Prompt
{system_prompt}

## 附属文件
{source_files_summary}

## 审计维度
1. **结构完整性**: prompt 是否有清晰的角色定义、输入要求、输出格式
2. **表达清晰度**: 指令是否精确无歧义
3. **场景覆盖度**: 是否覆盖了边界情况和异常处理
4. **安全性**: 是否有 prompt injection 风险或敏感信息泄露
5. **性能**: prompt 长度是否合理，是否有冗余

## 输出要求
输出严格 JSON（不要 markdown 代码块），格式：
{{
  "verdict": "good" | "needs_work" | "poor",
  "issues": [
    {{"severity": "high|medium|low", "category": "structure|clarity|completeness|safety|performance", "description": "具体问题描述"}}
  ],
  "recommended_path": "minor_edit" | "major_rewrite" | "brainstorming_upgrade"
}}

- verdict 判断标准: 0 个 high issue = good, 1-2 个 high = needs_work, 3+ 个 high 或整体质量差 = poor
- poor 时 recommended_path 应为 brainstorming_upgrade
"""


def _try_parse_json(raw: str) -> dict | None:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]+\}', cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None


async def run_audit(
    db: Session,
    skill_id: int,
    session_id: int | None = None,
) -> AuditResult:
    """对指定 Skill 执行质量审计，返回结构化结果并持久化。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        return AuditResult(verdict="poor", issues=[{"severity": "high", "category": "structure", "description": "Skill 不存在"}])

    latest_version = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    system_prompt = latest_version.system_prompt if latest_version else "(无 system prompt)"

    source_files = skill.source_files or []
    sf_summary = "\n".join(f"  - {f.get('filename', '?')} ({f.get('category', '?')})" for f in source_files) if source_files else "（无附属文件）"

    prompt = _AUDIT_PROMPT.format(
        name=skill.name,
        description=skill.description or "",
        source_type=skill.source_type or "local",
        system_prompt=system_prompt[:4000],
        source_files_summary=sf_summary,
    )

    model_config = llm_gateway.resolve_config(db, "studio.audit")
    raw_response = await llm_gateway.chat(
        model_config=model_config,
        messages=[{"role": "user", "content": prompt}],
    )
    # llm_gateway.chat 返回 (text, metadata) tuple
    response = raw_response[0] if isinstance(raw_response, tuple) else raw_response

    parsed = _try_parse_json(response)
    if not parsed:
        logger.warning(f"[StudioAuditor] skill={skill_id} LLM 返回无法解析: {response[:200]}")
        return AuditResult(
            verdict="needs_work",
            issues=[{"severity": "medium", "category": "structure", "description": "审计引擎未能生成结构化结果"}],
            recommended_path="minor_edit",
        )

    result = AuditResult(
        verdict=parsed.get("verdict", "needs_work"),
        issues=parsed.get("issues", []),
        recommended_path=parsed.get("recommended_path", "minor_edit"),
    )

    # 持久化
    audit_row = SkillAuditResult(
        skill_id=skill_id,
        session_id=session_id,
        quality_verdict=result.verdict,
        issues=result.issues,
        recommended_path=result.recommended_path,
    )
    db.add(audit_row)
    db.commit()
    db.refresh(audit_row)

    result.audit_id = audit_row.id  # type: ignore[attr-defined]
    return result
