from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.models.business import BusinessTable
from app.models.knowledge_governance import (
    GovernanceFeedbackEvent,
    GovernanceDepartmentMission,
    GovernanceFieldTemplate,
    GovernanceKR,
    GovernanceObjectType,
    GovernanceRequiredElement,
    GovernanceObjective,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
    GovernanceObject,
    GovernanceObjectFacet,
)
from app.models.user import Department


KEYWORD_RULES = [
    {
        "objective_code": "professional_capability",
        "library_code": "role_capability",
        "object_type_code": "skill_material",
        "keywords": ["客户运营", "产品运营", "产品经理", "后端开发", "岗位", "胜任力", "招聘画像"],
        "reason": "命中岗位能力资料关键词",
        "confidence": 86,
    },
    {
        "objective_code": "company_common",
        "library_code": "company_sop",
        "object_type_code": "sop_ticket",
        "keywords": ["sop", "流程", "制度", "规范", "审批", "工单", "操作指引"],
        "reason": "命中通用 SOP / 制度关键词",
        "confidence": 88,
    },
    {
        "objective_code": "professional_capability",
        "library_code": "general_capability",
        "object_type_code": "skill_material",
        "keywords": ["复盘", "方法论", "训练", "课程", "表达", "协作", "分析"],
        "reason": "命中通用能力资料关键词",
        "confidence": 72,
    },
    {
        "objective_code": "outsource_intel",
        "library_code": "industry_intel",
        "object_type_code": "external_intel",
        "keywords": ["行业", "情报", "竞品", "平台", "素材", "抖音", "小红书", "投放", "趋势"],
        "reason": "命中行业情报关键词",
        "confidence": 84,
    },
]


def _strategy_key(
    *,
    strategy_group: str,
    subject_type: str,
    objective_code: str | None,
    library_code: str | None,
    department_id: int | None = None,
    business_line: str | None = None,
) -> str:
    return "|".join([
        strategy_group,
        subject_type or "-",
        objective_code or "-",
        library_code or "-",
        str(department_id or "-"),
        business_line or "-",
    ])


def _bandit_adjusted_confidence(
    db: Session,
    *,
    base_confidence: int,
    strategy_group: str,
    subject_type: str,
    objective_code: str | None,
    library_code: str | None,
    department_id: int | None = None,
    business_line: str | None = None,
) -> tuple[int, dict[str, Any]]:
    key = _strategy_key(
        strategy_group=strategy_group,
        subject_type=subject_type,
        objective_code=objective_code,
        library_code=library_code,
        department_id=department_id,
        business_line=business_line,
    )
    stat = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.strategy_key == key).first()
    if not stat or stat.total_count <= 0:
        return base_confidence, {
            "strategy_key": key,
            "strategy_group": strategy_group,
            "base_confidence": base_confidence,
            "boost": 0,
            "success_rate": None,
            "samples": 0,
            "department_id": department_id,
            "business_line": business_line,
            "is_frozen": False,
        }

    if stat.is_frozen:
        frozen_confidence = max(1, min(99, base_confidence - 25 + (stat.manual_bias or 0)))
        return frozen_confidence, {
            "strategy_key": key,
            "strategy_group": strategy_group,
            "base_confidence": base_confidence,
            "boost": frozen_confidence - base_confidence,
            "success_rate": round((stat.success_count or 0) / max(stat.total_count or 1, 1), 4),
            "samples": stat.total_count,
            "department_id": department_id,
            "business_line": business_line,
            "is_frozen": True,
        }

    success_rate = stat.success_count / max(stat.total_count, 1)
    exploration_bonus = min(8, int(20 / max(stat.total_count, 2)))
    exploitation_shift = int((success_rate - 0.5) * 30)
    confidence = max(1, min(99, base_confidence + exploitation_shift + exploration_bonus + (stat.manual_bias or 0)))
    return confidence, {
        "strategy_key": key,
        "strategy_group": strategy_group,
        "base_confidence": base_confidence,
        "boost": confidence - base_confidence,
        "success_rate": round(success_rate, 4),
        "samples": stat.total_count,
        "department_id": department_id,
        "business_line": business_line,
        "is_frozen": False,
    }


def record_governance_feedback(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    strategy_key: str,
    event_type: str,
    reward: float,
    created_by: int | None,
    suggestion_id: int | None = None,
    from_objective_id: int | None = None,
    from_resource_library_id: int | None = None,
    to_objective_id: int | None = None,
    to_resource_library_id: int | None = None,
    note: str | None = None,
) -> None:
    reward_score = int(reward * 100)
    event = GovernanceFeedbackEvent(
        suggestion_id=suggestion_id,
        subject_type=subject_type,
        subject_id=subject_id,
        strategy_key=strategy_key,
        event_type=event_type,
        reward_score=reward_score,
        from_objective_id=from_objective_id,
        from_resource_library_id=from_resource_library_id,
        to_objective_id=to_objective_id,
        to_resource_library_id=to_resource_library_id,
        note=note,
        created_by=created_by,
    )
    db.add(event)

    parts = strategy_key.split("|")
    padded = parts + ["-"] * max(0, 6 - len(parts))
    strategy_group, stat_subject_type, objective_code, library_code, department_id_raw, business_line = padded[:6]
    stat = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.strategy_key == strategy_key).first()
    if not stat:
        stat = GovernanceStrategyStat(
            strategy_key=strategy_key,
            strategy_group=strategy_group,
            subject_type=None if stat_subject_type == "-" else stat_subject_type,
            objective_code=None if objective_code == "-" else objective_code,
            library_code=None if library_code == "-" else library_code,
            department_id=None if department_id_raw == "-" else int(department_id_raw),
            business_line=None if business_line == "-" else business_line,
        )
        db.add(stat)
        db.flush()

    stat.total_count += 1
    if reward > 0:
        stat.success_count += 1
    elif reward < 0:
        stat.reject_count += 1
    stat.cumulative_reward += reward_score
    stat.last_reward = reward_score
    stat.last_event_at = event.created_at


def ensure_governance_defaults(db: Session, created_by: int | None = None) -> None:
    from app.routers.knowledge_governance import DEFAULT_BLUEPRINT

    objective_map = {item.code: item for item in db.query(GovernanceObjective).all()}
    for idx, objective_data in enumerate(DEFAULT_BLUEPRINT):
        objective = objective_map.get(objective_data["code"])
        if not objective:
            objective = GovernanceObjective(
                name=objective_data["name"],
                code=objective_data["code"],
                description=objective_data.get("description"),
                level=objective_data.get("level", "company"),
                objective_role=objective_data.get("objective_role"),
                sort_order=idx,
                created_by=created_by,
            )
            db.add(objective)
            db.flush()
            objective_map[objective.code] = objective
        else:
            objective.name = objective_data["name"]
            objective.description = objective_data.get("description")
            objective.level = objective_data.get("level", objective.level)
            objective.objective_role = objective_data.get("objective_role")
            objective.sort_order = idx

        existing_library_map = {
            item.code: item
            for item in db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.objective_id == objective.id).all()
        }
        for lib in objective_data.get("libraries", []):
            library = existing_library_map.get(lib["code"])
            baseline = {
                "readable": True,
                "editable": lib["object_type"] in {"sop_ticket", "knowledge_asset", "customer"},
                "update_cycle": lib.get("default_update_cycle"),
            }
            if not library:
                db.add(
                    GovernanceResourceLibrary(
                        objective_id=objective.id,
                        name=lib["name"],
                        code=lib["code"],
                        description=lib.get("description"),
                        object_type=lib["object_type"],
                        default_visibility=lib.get("default_visibility", "read"),
                        default_update_cycle=lib.get("default_update_cycle"),
                        field_schema=lib.get("field_schema", []),
                        consumption_scenarios=lib.get("consumption_scenarios", []),
                        collaboration_baseline=baseline,
                        classification_hints={"objective_code": objective.code},
                        created_by=created_by,
                    )
                )
                continue

            library.name = lib["name"]
            library.description = lib.get("description")
            library.object_type = lib["object_type"]
            library.default_visibility = lib.get("default_visibility", library.default_visibility)
            library.default_update_cycle = lib.get("default_update_cycle")
            library.field_schema = lib.get("field_schema", [])
            library.consumption_scenarios = lib.get("consumption_scenarios", [])
            library.collaboration_baseline = baseline
            library.classification_hints = {"objective_code": objective.code}
            library.is_active = True

    builtin_object_types = [
        ("customer", "客户", ["customer_name", "owner", "stage", "source", "next_action"], ["read", "edit", "skill_read"]),
        ("sop_ticket", "SOP/工单", ["process_name", "owner", "sla", "status"], ["read", "edit"]),
        ("case", "案例", ["case_type", "industry", "result", "key_learnings"], ["read", "skill_read"]),
        ("external_intel", "外部情报", ["industry", "intel_type", "source", "timeliness"], ["read", "skill_read"]),
        ("skill_material", "岗位技能资料", ["role_name", "skill_level", "training_mode"], ["read", "edit", "skill_read"]),
        ("knowledge_asset", "知识资产", ["owner", "effective_date"], ["read", "edit"]),
    ]
    for code, name, baseline_fields, modes in builtin_object_types:
        exists = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == code).first()
        if not exists:
            db.add(
                GovernanceObjectType(
                    code=code,
                    name=name,
                    baseline_fields=baseline_fields,
                    default_consumption_modes=modes,
                )
            )
            continue
        exists.name = name
        exists.baseline_fields = baseline_fields
        exists.default_consumption_modes = modes
    db.commit()
    _seed_field_templates(db)
    _seed_department_kr_templates(db, created_by=created_by)


def _seed_field_templates(db: Session) -> None:
    field_templates = {
        "customer": [
            ("customer_name", "客户名称", "text", True, True, "edit", "realtime"),
            ("owner", "负责人", "person", True, True, "edit", "realtime"),
            ("stage", "阶段", "select", False, True, "edit", "daily"),
            ("next_action", "下一步动作", "text", False, True, "edit", "daily"),
        ],
        "sop_ticket": [
            ("process_name", "流程名称", "text", True, True, "edit", "manual"),
            ("owner", "负责人", "person", False, True, "edit", "weekly"),
            ("sla", "时效要求", "text", False, True, "read", "manual"),
            ("status", "状态", "select", False, True, "edit", "daily"),
        ],
        "case": [
            ("case_type", "案例类型", "select", True, True, "edit", "weekly"),
            ("industry", "行业", "select", False, True, "edit", "weekly"),
            ("result", "结果", "text", False, True, "read", "weekly"),
            ("key_learnings", "关键经验", "long_text", False, True, "read", "weekly"),
        ],
        "external_intel": [
            ("industry", "行业", "select", True, True, "edit", "weekly"),
            ("intel_type", "情报类型", "select", True, True, "edit", "weekly"),
            ("source", "来源", "text", False, True, "read", "weekly"),
            ("timeliness", "时效", "date", False, True, "read", "weekly"),
        ],
        "skill_material": [
            ("role_name", "岗位名称", "text", False, True, "edit", "monthly"),
            ("skill_level", "能力层级", "select", False, True, "read", "monthly"),
            ("training_mode", "训练形式", "select", False, True, "read", "monthly"),
            ("assessment_hint", "评估提示", "long_text", False, True, "read", "monthly"),
        ],
    }
    object_types = db.query(GovernanceObjectType).all()
    object_type_map = {item.code: item for item in object_types}
    for object_code, templates in field_templates.items():
        object_type = object_type_map.get(object_code)
        if not object_type:
            continue
        for idx, (field_key, field_label, field_type, is_required, is_editable, visibility_mode, update_cycle) in enumerate(templates):
            exists = (
                db.query(GovernanceFieldTemplate)
                .filter(
                    GovernanceFieldTemplate.object_type_id == object_type.id,
                    GovernanceFieldTemplate.field_key == field_key,
                )
                .first()
            )
            if exists:
                continue
            db.add(
                GovernanceFieldTemplate(
                    object_type_id=object_type.id,
                    field_key=field_key,
                    field_label=field_label,
                    field_type=field_type,
                    is_required=is_required,
                    is_editable=is_editable,
                    visibility_mode=visibility_mode,
                    update_cycle=update_cycle,
                    consumer_modes=["read", "edit"],
                    sort_order=idx,
                )
            )
    db.commit()


def _seed_department_kr_templates(db: Session, created_by: int | None = None) -> None:
    departments = db.query(Department).all()
    objective_map = {item.code: item for item in db.query(GovernanceObjective).all()}
    for dept in departments:
        dept_key = (dept.name or f"dept_{dept.id}").strip().lower().replace(" ", "_")
        mission = (
            db.query(GovernanceDepartmentMission)
            .filter(GovernanceDepartmentMission.department_id == dept.id)
            .first()
        )
        if not mission:
            mission = GovernanceDepartmentMission(
                department_id=dept.id,
                objective_id=objective_map.get("business_line_execution").id if objective_map.get("business_line_execution") else None,
                name=f"{dept.name} 部门使命",
                code=f"{dept_key}_mission",
                core_role=f"{dept.name} 在业务链中的核心职责待补充",
                mission_statement=f"{dept.name} 负责承接关键经营目标并沉淀资源库",
                created_by=created_by,
            )
            db.add(mission)
            db.flush()

        kr_specs = [
            ("kr_resource_efficiency", "资源运转效率", "提升资源利用率与协同效率", "资源使用效率"),
            ("kr_case_reuse", "案例复用率", "提升可复用案例与经验沉淀", "案例复用率"),
            ("kr_signal_capture", "外部信号捕获", "提升外部信号采集和策略反应速度", "情报捕获时效"),
        ]
        for idx, (code, name, desc, metric) in enumerate(kr_specs):
            kr = (
                db.query(GovernanceKR)
                .filter(
                    GovernanceKR.mission_id == mission.id,
                    GovernanceKR.code == code,
                )
                .first()
            )
            if not kr:
                kr = GovernanceKR(
                    mission_id=mission.id,
                    objective_id=mission.objective_id,
                    name=name,
                    code=code,
                    description=desc,
                    metric_definition=metric,
                    owner_role=dept.name,
                    sort_order=idx,
                )
                db.add(kr)
                db.flush()

            element_specs = [
                ("roles", "岗位角色设置", "role", ["role_capability", "role_sop_playbook"], ["skill_material", "sop_ticket"]),
                ("sop", "SOP 与执行规范", "process", ["company_sop", "role_sop_playbook"], ["sop_ticket"]),
                ("resource_repo", "关键资源库", "resource", ["biz_resource_repo", "biz_customer_repo"], ["customer", "knowledge_asset"]),
                ("cases", "案例与经验", "resource", ["biz_case_repo", "role_case_repo"], ["case"]),
                ("signals", "外部信息与情报", "external_signal", ["industry_intel", "platform_watch", "creative_trends"], ["external_intel"]),
            ]
            for element_idx, (element_code, element_name, element_type, library_codes, object_types) in enumerate(element_specs):
                exists = (
                    db.query(GovernanceRequiredElement)
                    .filter(
                        GovernanceRequiredElement.kr_id == kr.id,
                        GovernanceRequiredElement.code == element_code,
                    )
                    .first()
                )
                if exists:
                    continue
                db.add(
                    GovernanceRequiredElement(
                        kr_id=kr.id,
                        name=element_name,
                        code=element_code,
                        element_type=element_type,
                        required_library_codes=library_codes,
                        required_object_types=object_types,
                        suggested_update_cycle="weekly",
                        sort_order=element_idx,
                    )
                )
    db.commit()


def ensure_governance_object(
    db: Session,
    *,
    object_type_code: str,
    canonical_key: str,
    display_name: str,
    business_line: str | None = None,
    department_id: int | None = None,
    owner_id: int | None = None,
) -> GovernanceObject | None:
    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == object_type_code).first()
    if not object_type:
        return None
    existing = (
        db.query(GovernanceObject)
        .filter(
            GovernanceObject.object_type_id == object_type.id,
            GovernanceObject.canonical_key == canonical_key,
        )
        .first()
    )
    if existing:
        return existing
    item = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key=canonical_key,
        display_name=display_name,
        business_line=business_line,
        department_id=department_id,
        owner_id=owner_id,
        lifecycle_status="active",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _subject_text(entry: KnowledgeEntry) -> str:
    parts = [
        entry.title or "",
        entry.ai_title or "",
        entry.source_file or "",
        entry.ai_summary or "",
        entry.content[:1000] if entry.content else "",
    ]
    return "\n".join(part for part in parts if part).lower()


def _business_line_for_entry(db: Session, entry: KnowledgeEntry) -> str | None:
    if not entry.department_id:
        return None
    dept = db.get(Department, entry.department_id)
    if not dept:
        return None
    return (dept.business_unit or "").strip() or None


def _resolve_rule_targets(db: Session, rule: dict[str, Any]) -> tuple[GovernanceObjective | None, GovernanceResourceLibrary | None, GovernanceObjectType | None]:
    objective = db.query(GovernanceObjective).filter(GovernanceObjective.code == rule["objective_code"]).first()
    library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == rule["library_code"]).first()
    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == rule["object_type_code"]).first()
    return objective, library, object_type


def _find_matching_kr_and_element(db: Session, library_code: str) -> tuple[GovernanceKR | None, GovernanceRequiredElement | None]:
    krs = db.query(GovernanceKR).order_by(GovernanceKR.id).all()
    for kr in krs:
        elements = (
            db.query(GovernanceRequiredElement)
            .filter(GovernanceRequiredElement.kr_id == kr.id)
            .order_by(GovernanceRequiredElement.sort_order)
            .all()
        )
        for element in elements:
            if library_code in (element.required_library_codes or []):
                return kr, element
    return None, None


def _field_gap_payload(db: Session, object_type: GovernanceObjectType | None, content: str) -> dict[str, Any]:
    if not object_type:
        return {"required_fields": [], "missing_fields": []}
    templates = (
        db.query(GovernanceFieldTemplate)
        .filter(GovernanceFieldTemplate.object_type_id == object_type.id)
        .order_by(GovernanceFieldTemplate.sort_order)
        .all()
    )
    required_fields = [item.field_key for item in templates if item.is_required]
    missing_fields = []
    lowered = content.lower()
    for item in templates:
        if not item.is_required:
            continue
        probe_tokens = [item.field_key.lower(), (item.field_label or "").lower()]
        if not any(token and token in lowered for token in probe_tokens):
            missing_fields.append(item.field_key)
    return {
        "required_fields": required_fields,
        "missing_fields": missing_fields,
        "field_templates": [
            {
                "field_key": item.field_key,
                "field_label": item.field_label,
                "field_type": item.field_type,
                "is_required": item.is_required,
                "visibility_mode": item.visibility_mode,
                "update_cycle": item.update_cycle,
            }
            for item in templates
        ],
    }


def _object_candidates(db: Session, object_type: GovernanceObjectType | None, content: str, business_line: str | None = None) -> list[dict[str, Any]]:
    if not object_type:
        return []
    q = db.query(GovernanceObject).filter(GovernanceObject.object_type_id == object_type.id)
    if business_line:
        q = q.filter((GovernanceObject.business_line == business_line) | (GovernanceObject.business_line.is_(None)))
    candidates = []
    lowered = content.lower()
    for item in q.order_by(GovernanceObject.updated_at.desc()).limit(20).all():
        score = 0
        if item.display_name and item.display_name.lower() in lowered:
            score += 80
        if item.canonical_key and item.canonical_key.lower() in lowered:
            score += 40
        if business_line and item.business_line == business_line:
            score += 10
        object_feedback_score = 0
        if isinstance(item.object_payload, dict):
            object_feedback_score = int(item.object_payload.get("feedback_score") or 0)
            score += min(20, max(-20, object_feedback_score))
        if business_line:
            line_stats = (
                db.query(GovernanceStrategyStat)
                .filter(
                    GovernanceStrategyStat.business_line == business_line,
                    GovernanceStrategyStat.success_count > 0,
                )
                .order_by(GovernanceStrategyStat.success_count.desc(), GovernanceStrategyStat.cumulative_reward.desc())
                .limit(10)
                .all()
            )
            if line_stats:
                line_boost = max((stat.success_count or 0) for stat in line_stats)
                score += min(15, line_boost * 2)
        if score <= 0:
            continue
        candidates.append({
            "id": item.id,
            "display_name": item.display_name,
            "canonical_key": item.canonical_key,
            "score": score,
            "business_line": item.business_line,
            "matched_business_line": business_line and item.business_line == business_line,
            "feedback_score": object_feedback_score,
        })
    return sorted(candidates, key=lambda x: x["score"], reverse=True)[:5]


def infer_governance_suggestion_for_entry(db: Session, entry: KnowledgeEntry) -> dict[str, Any] | None:
    ensure_governance_defaults(db)
    content = _subject_text(entry)
    business_line = _business_line_for_entry(db, entry)

    for rule in KEYWORD_RULES:
        if any(keyword in content for keyword in rule["keywords"]):
            objective, library, object_type = _resolve_rule_targets(db, rule)
            if not objective or not library:
                continue
            kr, element = _find_matching_kr_and_element(db, library.code)
            field_gap = _field_gap_payload(db, object_type, content)
            adjusted_confidence, reinforcement_meta = _bandit_adjusted_confidence(
                db,
                base_confidence=rule["confidence"],
                strategy_group="keyword_rule",
                subject_type="knowledge",
                objective_code=objective.code if objective else None,
                library_code=library.code if library else None,
                department_id=entry.department_id,
                business_line=business_line,
            )
            payload = {
                "business_line": business_line,
                "keywords": [keyword for keyword in rule["keywords"] if keyword in content],
                "taxonomy_board": entry.taxonomy_board,
                "taxonomy_code": entry.taxonomy_code,
                "classification_confidence": entry.classification_confidence,
                "kr_id": kr.id if kr else None,
                "kr_name": kr.name if kr else None,
                "element_id": element.id if element else None,
                "element_name": element.name if element else None,
                "object_candidates": _object_candidates(db, object_type, content, business_line),
                "reinforcement_meta": reinforcement_meta,
                **field_gap,
            }
            return {
                "objective": objective,
                "library": library,
                "object_type": object_type,
                "kr": kr,
                "element": element,
                "task_type": "classify",
                "reason": f"{rule['reason']}；缺失字段 {', '.join(field_gap['missing_fields']) or '无'}",
                "confidence": adjusted_confidence,
                "payload": payload,
            }

    if entry.taxonomy_board in {"B", "C"}:
        objective = db.query(GovernanceObjective).filter(GovernanceObjective.code == "outsource_intel").first()
        library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == "industry_intel").first()
        object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == "external_intel").first()
        if objective and library:
            kr, element = _find_matching_kr_and_element(db, library.code)
            field_gap = _field_gap_payload(db, object_type, content)
            adjusted_confidence, reinforcement_meta = _bandit_adjusted_confidence(
                db,
                base_confidence=58,
                strategy_group="taxonomy_backfill",
                subject_type="knowledge",
                objective_code=objective.code if objective else None,
                library_code=library.code if library else None,
                department_id=entry.department_id,
                business_line=business_line,
            )
            return {
                "objective": objective,
                "library": library,
                "object_type": object_type,
                "kr": kr,
                "element": element,
                "task_type": "classify",
                "reason": "沿用现有 taxonomy 结果回填行业情报建议",
                "confidence": adjusted_confidence,
                "payload": {
                    "business_line": business_line,
                    "from_taxonomy": True,
                    "taxonomy_board": entry.taxonomy_board,
                    "taxonomy_code": entry.taxonomy_code,
                    "kr_id": kr.id if kr else None,
                    "element_id": element.id if element else None,
                    "object_candidates": _object_candidates(db, object_type, content, business_line),
                    "reinforcement_meta": reinforcement_meta,
                    **field_gap,
                },
            }
    return None


def create_or_update_governance_suggestion_for_entry(db: Session, entry: KnowledgeEntry, created_by: int | None = None) -> GovernanceSuggestionTask | None:
    if entry.governance_status == "aligned":
        return None

    inferred = infer_governance_suggestion_for_entry(db, entry)
    if not inferred:
        return None

    existing = (
        db.query(GovernanceSuggestionTask)
        .filter(
            GovernanceSuggestionTask.subject_type == "knowledge",
            GovernanceSuggestionTask.subject_id == entry.id,
            GovernanceSuggestionTask.status == "pending",
        )
        .first()
    )
    if existing:
        existing.objective_id = inferred["objective"].id if inferred["objective"] else None
        existing.resource_library_id = inferred["library"].id if inferred["library"] else None
        existing.object_type_id = inferred["object_type"].id if inferred["object_type"] else None
        entry.governance_kr_id = inferred["kr"].id if inferred.get("kr") else entry.governance_kr_id
        entry.governance_element_id = inferred["element"].id if inferred.get("element") else entry.governance_element_id
        existing.reason = inferred["reason"]
        existing.confidence = inferred["confidence"]
        existing.suggested_payload = inferred["payload"]
        entry.governance_status = "suggested"
        entry.governance_confidence = inferred["confidence"] / 100.0
        entry.governance_note = inferred["reason"]
        db.commit()
        return existing

    task = GovernanceSuggestionTask(
        subject_type="knowledge",
        subject_id=entry.id,
        task_type=inferred["task_type"],
        status="pending",
        objective_id=inferred["objective"].id if inferred["objective"] else None,
        resource_library_id=inferred["library"].id if inferred["library"] else None,
        object_type_id=inferred["object_type"].id if inferred["object_type"] else None,
        suggested_payload=inferred["payload"],
        reason=inferred["reason"],
        confidence=inferred["confidence"],
        created_by=created_by,
    )
    db.add(task)
    entry.governance_status = "suggested"
    entry.governance_kr_id = inferred["kr"].id if inferred.get("kr") else entry.governance_kr_id
    entry.governance_element_id = inferred["element"].id if inferred.get("element") else entry.governance_element_id
    entry.governance_confidence = inferred["confidence"] / 100.0
    entry.governance_note = inferred["reason"]
    db.commit()
    db.refresh(task)
    return task


def infer_governance_suggestion_for_table(db: Session, table: BusinessTable) -> dict[str, Any] | None:
    ensure_governance_defaults(db)
    department = db.get(Department, table.department_id) if table.department_id else None
    business_line = (department.business_unit or "").strip() if department and department.business_unit else None
    text = "\n".join([
        table.display_name or "",
        table.table_name or "",
        table.description or "",
        str(table.source_type or ""),
    ]).lower()

    rules = [
        ("biz_customer_repo", "business_line_execution", "customer", ["客户", "customer", "crm", "线索", "商机"], "命中客户资源库规则", 84),
        ("biz_resource_repo", "business_line_execution", "knowledge_asset", ["资源", "供应商", "媒体", "渠道", "达人"], "命中关键资源库规则", 78),
        ("industry_intel", "outsource_intel", "external_intel", ["情报", "趋势", "竞品", "平台", "投放"], "命中行业情报规则", 74),
    ]
    for library_code, objective_code, object_type_code, keywords, reason, confidence in rules:
        if any(keyword.lower() in text for keyword in keywords):
            objective = db.query(GovernanceObjective).filter(GovernanceObjective.code == objective_code).first()
            library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == library_code).first()
            object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == object_type_code).first()
            if objective and library:
                kr, element = _find_matching_kr_and_element(db, library.code)
                field_gap = _field_gap_payload(db, object_type, text)
                adjusted_confidence, reinforcement_meta = _bandit_adjusted_confidence(
                    db,
                    base_confidence=confidence,
                    strategy_group="table_rule",
                    subject_type="business_table",
                    objective_code=objective.code if objective else None,
                    library_code=library.code if library else None,
                    department_id=table.department_id,
                    business_line=business_line,
                )
                return {
                    "objective": objective,
                    "library": library,
                    "object_type": object_type,
                    "kr": kr,
                    "element": element,
                    "task_type": "align_library",
                    "reason": f"{reason}；缺失字段 {', '.join(field_gap['missing_fields']) or '无'}",
                    "confidence": adjusted_confidence,
                    "payload": {
                        "source_type": table.source_type,
                        "table_name": table.table_name,
                        "business_line": business_line,
                        "kr_id": kr.id if kr else None,
                        "element_id": element.id if element else None,
                        "object_candidates": _object_candidates(db, object_type, text),
                        "reinforcement_meta": reinforcement_meta,
                        **field_gap,
                    },
                }
    return None


def create_or_update_governance_suggestion_for_table(db: Session, table: BusinessTable, created_by: int | None = None) -> GovernanceSuggestionTask | None:
    if table.governance_status == "aligned":
        return None
    inferred = infer_governance_suggestion_for_table(db, table)
    if not inferred:
        return None
    existing = (
        db.query(GovernanceSuggestionTask)
        .filter(
            GovernanceSuggestionTask.subject_type == "business_table",
            GovernanceSuggestionTask.subject_id == table.id,
            GovernanceSuggestionTask.status == "pending",
        )
        .first()
    )
    if existing:
        existing.objective_id = inferred["objective"].id if inferred["objective"] else None
        existing.resource_library_id = inferred["library"].id if inferred["library"] else None
        existing.object_type_id = inferred["object_type"].id if inferred["object_type"] else None
        table.governance_kr_id = inferred["kr"].id if inferred.get("kr") else table.governance_kr_id
        table.governance_element_id = inferred["element"].id if inferred.get("element") else table.governance_element_id
        existing.reason = inferred["reason"]
        existing.confidence = inferred["confidence"]
        existing.suggested_payload = inferred["payload"]
        table.governance_status = "suggested"
        table.governance_note = inferred["reason"]
        db.commit()
        return existing

    task = GovernanceSuggestionTask(
        subject_type="business_table",
        subject_id=table.id,
        task_type=inferred["task_type"],
        status="pending",
        objective_id=inferred["objective"].id if inferred["objective"] else None,
        resource_library_id=inferred["library"].id if inferred["library"] else None,
        object_type_id=inferred["object_type"].id if inferred["object_type"] else None,
        suggested_payload=inferred["payload"],
        reason=inferred["reason"],
        confidence=inferred["confidence"],
        created_by=created_by,
    )
    db.add(task)
    table.governance_status = "suggested"
    table.governance_kr_id = inferred["kr"].id if inferred.get("kr") else table.governance_kr_id
    table.governance_element_id = inferred["element"].id if inferred.get("element") else table.governance_element_id
    table.governance_note = inferred["reason"]
    db.commit()
    db.refresh(task)
    return task
