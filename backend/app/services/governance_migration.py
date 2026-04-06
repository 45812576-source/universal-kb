"""跨公司迁移服务：导出骨架 → AI 匹配 → 差异补入 → 灰度验证 → 发布。

核心函数：
- export_skeleton: 导出全量骨架 JSON，脱敏公司/人名
- match_skeleton: AI 分类每项为 directly_reusable / needs_adaptation / missing
- import_skeleton: reusable 自动创建, adaptation 创建 draft, missing 链接缺口补入
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge_governance import (
    GovernanceBaselineSnapshot,
    GovernanceObjective,
    GovernanceObjectType,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)

logger = logging.getLogger(__name__)

# ── 脱敏规则 ──────────────────────────────────────────────────────────────────

_COMPANY_PATTERNS = [
    (re.compile(r"(?:公司|集团|企业|Corp|Inc|Ltd|Co\.)[\s\S]{0,20}", re.IGNORECASE), "[公司]"),
]

_PERSON_PATTERNS = [
    (re.compile(r"[\u4e00-\u9fff]{2,4}(?:总|经理|主管|主任|老师|先生|女士)"), "[人名]"),
]


def _anonymize_text(text: str) -> str:
    """脱敏公司名和人名。"""
    if not text:
        return text
    for pat, repl in _COMPANY_PATTERNS:
        text = pat.sub(repl, text)
    for pat, repl in _PERSON_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _anonymize_dict(d: dict) -> dict:
    """递归脱敏 dict 中的字符串值。"""
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _anonymize_text(v)
        elif isinstance(v, dict):
            result[k] = _anonymize_dict(v)
        elif isinstance(v, list):
            result[k] = [_anonymize_dict(i) if isinstance(i, dict) else (_anonymize_text(i) if isinstance(i, str) else i) for i in v]
        else:
            result[k] = v
    return result


# ── 导出 ──────────────────────────────────────────────────────────────────────


def export_skeleton(db: Session, anonymize: bool = True) -> dict[str, Any]:
    """导出全量骨架 JSON，可选脱敏。"""
    objectives = db.query(GovernanceObjective).filter(
        GovernanceObjective.is_active == True
    ).all()
    libraries = db.query(GovernanceResourceLibrary).filter(
        GovernanceResourceLibrary.is_active == True
    ).all()
    object_types = db.query(GovernanceObjectType).all()
    strategies = db.query(GovernanceStrategyStat).filter(
        GovernanceStrategyStat.total_count >= 5,
    ).all()

    skeleton = {
        "format_version": "1.0",
        "objectives": [
            {
                "code": o.code,
                "name": o.name,
                "description": o.description or "",
                "level": o.level,
                "objective_role": o.objective_role,
            }
            for o in objectives
        ],
        "resource_libraries": [
            {
                "code": l.code,
                "name": l.name,
                "description": l.description or "",
                "objective_code": next((o.code for o in objectives if o.id == l.objective_id), ""),
                "object_type": l.object_type,
                "governance_mode": l.governance_mode,
                "default_update_cycle": l.default_update_cycle,
                "classification_hints": l.classification_hints or {},
            }
            for l in libraries
        ],
        "object_types": [
            {
                "code": t.code,
                "name": t.name,
                "description": t.description or "",
                "baseline_fields": t.baseline_fields or [],
            }
            for t in object_types
        ],
        "strategy_stats_summary": [
            {
                "strategy_group": s.strategy_group,
                "library_code": s.library_code,
                "objective_code": s.objective_code,
                "total_count": s.total_count,
                "success_rate": round((s.success_count or 0) / max(s.total_count, 1), 4),
            }
            for s in strategies
        ],
    }

    if anonymize:
        skeleton = _anonymize_dict(skeleton)

    logger.info(f"[Migration] exported skeleton: {len(skeleton['objectives'])} objectives, {len(skeleton['resource_libraries'])} libraries")
    return skeleton


# ── 匹配 ──────────────────────────────────────────────────────────────────────


async def _llm_match(db: Session, exported: dict, target_context: dict) -> list[dict]:
    """AI 分类每项：directly_reusable / needs_adaptation / missing。"""
    from app.services.llm_gateway import llm_gateway

    prompt = f"""你是知识治理迁移助手。以下是源公司的治理骨架，请评估每个资源库在目标公司的适用性。

## 源骨架
{json.dumps(exported.get("resource_libraries", []), ensure_ascii=False, indent=2)}

## 目标公司上下文
{json.dumps(target_context, ensure_ascii=False, indent=2)}

## 输出格式（JSON 数组）
```json
[
  {{
    "library_code": "...",
    "match_status": "directly_reusable | needs_adaptation | missing",
    "reason": "匹配/不匹配原因",
    "adaptation_notes": "需要适配时的说明（可选）"
  }}
]
```
只输出 JSON 数组。"""

    try:
        config = llm_gateway.resolve_config(db, "governance.suggest")
        response, _usage = await llm_gateway.chat(
            config,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2048,
        )

        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[Migration] LLM match failed: {e}")
        # fallback: 全部标为 needs_adaptation
        return [
            {
                "library_code": lib["code"],
                "match_status": "needs_adaptation",
                "reason": f"LLM 匹配失败，默认需要适配: {e}",
            }
            for lib in exported.get("resource_libraries", [])
        ]


def match_skeleton(db: Session, exported: dict, target_context: dict) -> list[dict]:
    """AI 匹配每项的适用性。同步包装。"""
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_llm_match(db, exported, target_context))
        loop.close()
        return result
    except Exception as e:
        logger.warning(f"[Migration] match_skeleton error: {e}")
        return [
            {
                "library_code": lib["code"],
                "match_status": "needs_adaptation",
                "reason": str(e),
            }
            for lib in exported.get("resource_libraries", [])
        ]


# ── 导入 ──────────────────────────────────────────────────────────────────────


def import_skeleton(
    db: Session,
    exported: dict,
    matched: list[dict],
    user_id: int | None = None,
) -> dict[str, Any]:
    """根据匹配结果导入骨架。

    - directly_reusable: 自动创建 objective + library + object_type
    - needs_adaptation: 创建 draft suggestion 供管理员调整
    - missing: 链接到缺口补入流程
    """
    match_map = {m["library_code"]: m for m in matched}
    obj_map = {o.code: o for o in db.query(GovernanceObjective).all()}
    ot_map = {t.code: t for t in db.query(GovernanceObjectType).all()}

    stats = {"reusable": 0, "adaptation": 0, "missing": 0, "created_objectives": [], "created_libraries": []}

    # 确保 objectives 存在
    for obj_data in exported.get("objectives", []):
        if obj_data["code"] not in obj_map:
            obj = GovernanceObjective(
                name=obj_data["name"],
                code=obj_data["code"],
                description=obj_data.get("description"),
                level=obj_data.get("level", "company"),
                objective_role=obj_data.get("objective_role"),
            )
            db.add(obj)
            db.flush()
            obj_map[obj.code] = obj
            stats["created_objectives"].append(obj.code)

    # 确保 object_types 存在
    for ot_data in exported.get("object_types", []):
        if ot_data["code"] not in ot_map:
            ot = GovernanceObjectType(
                code=ot_data["code"],
                name=ot_data["name"],
                description=ot_data.get("description"),
                baseline_fields=ot_data.get("baseline_fields", []),
            )
            db.add(ot)
            db.flush()
            ot_map[ot.code] = ot

    # 处理 libraries
    for lib_data in exported.get("resource_libraries", []):
        match_info = match_map.get(lib_data["code"], {"match_status": "needs_adaptation"})
        status = match_info.get("match_status", "needs_adaptation")

        objective = obj_map.get(lib_data.get("objective_code", ""))

        if status == "directly_reusable":
            # 检查是否已存在
            existing = db.query(GovernanceResourceLibrary).filter(
                GovernanceResourceLibrary.code == lib_data["code"],
            ).first()
            if not existing and objective:
                lib = GovernanceResourceLibrary(
                    objective_id=objective.id,
                    name=lib_data["name"],
                    code=lib_data["code"],
                    description=lib_data.get("description"),
                    object_type=lib_data.get("object_type", "knowledge_asset"),
                    governance_mode=lib_data.get("governance_mode", "ab_fusion"),
                    default_update_cycle=lib_data.get("default_update_cycle"),
                    classification_hints=lib_data.get("classification_hints", {}),
                )
                db.add(lib)
                stats["created_libraries"].append(lib_data["code"])
            stats["reusable"] += 1

        elif status == "needs_adaptation":
            # 创建 draft suggestion
            task = GovernanceSuggestionTask(
                subject_type="knowledge",
                subject_id=0,
                task_type="migration_adapt",
                status="pending",
                objective_id=objective.id if objective else None,
                reason=f"迁移适配: {lib_data['name']} — {match_info.get('reason', '')}",
                confidence=0,
                suggested_payload={
                    "source_library": lib_data,
                    "match_info": match_info,
                    "adaptation_notes": match_info.get("adaptation_notes", ""),
                },
                created_by=user_id,
            )
            db.add(task)
            stats["adaptation"] += 1

        else:  # missing
            # 链接到缺口补入流程
            task = GovernanceSuggestionTask(
                subject_type="knowledge",
                subject_id=0,
                task_type="gap_fix",
                status="pending",
                reason=f"迁移缺失: {lib_data['name']} — {match_info.get('reason', '')}",
                confidence=0,
                suggested_payload={
                    "source_library": lib_data,
                    "match_info": match_info,
                    "from_migration": True,
                },
                created_by=user_id,
            )
            db.add(task)
            stats["missing"] += 1

    db.flush()
    logger.info(
        f"[Migration] import done: reusable={stats['reusable']}, "
        f"adaptation={stats['adaptation']}, missing={stats['missing']}"
    )
    return stats
