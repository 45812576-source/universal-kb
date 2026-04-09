"""组织管理 → 治理引擎双向同步 + 组织基线版本中心

前向：org 变更 → 治理引擎表同步 + 影响面记录
反向：治理引擎状态 → 组织管理基线控制台展示
基线：OrgBaseline 独立状态机 draft → candidate → active → archived
"""

import datetime
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.knowledge_governance import (
    GovernanceBaselineSnapshot,
    GovernanceDepartmentMission,
    GovernanceKR,
    GovernanceRequiredElement,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)
from app.models.org_management import (
    BizProcess,
    BizTerminology,
    CollabProtocol,
    DataAssetOwnership,
    DeptCollaborationLink,
    DeptMissionDetail,
    KpiAssignment,
    KrResourceMapping,
    OkrKeyResult,
    OkrObjective,
    OkrPeriod,
    OrgBaseline,
    OrgChangeEvent,
    OrgChangeImpact,
    OrgImportSession,
    PositionAccessRule,
    PositionCompetencyModel,
    ResourceLibraryDefinition,
)
from app.models.permission import DataScopePolicy, PolicyTargetType, PolicyResourceType, VisibilityScope
from app.models.user import Department, User

logger = logging.getLogger(__name__)

# ── 访问范围映射 ──────────────────────────────────────────────────────────────
_ACCESS_RANGE_TO_VISIBILITY = {
    "none": None,
    "own": VisibilityScope.OWN,
    "own_client": VisibilityScope.OWN,
    "assigned": VisibilityScope.OWN,
    "department": VisibilityScope.DEPT,
    "all": VisibilityScope.ALL,
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. 前向同步：组织变更 → 治理引擎
# ══════════════════════════════════════════════════════════════════════════════

def sync_to_governance(db: Session, change_event: OrgChangeEvent):
    """根据变更事件分发到对应同步逻辑，同时记录影响面"""
    entity_type = change_event.entity_type
    try:
        match entity_type:
            case "dept_mission":
                _sync_mission(db, change_event)
            case "biz_process":
                _sync_process(db, change_event)
            case "terminology":
                _sync_terminology(db, change_event)
            case "data_asset":
                _sync_data_asset(db, change_event)
            case "access_rule":
                _sync_access_rule(db, change_event)
            case "department":
                _record_impact(db, change_event, "mission_sync", "department", change_event.entity_id, "部门变更可能影响 mission 同步")
            case "okr_objective" | "okr_key_result":
                _record_impact(db, change_event, "resource_library", "okr", change_event.entity_id, "OKR 变更可能影响资源库映射")
            case _:
                return
        # 每次同步后累积变更到当前 draft/candidate 基线
        _accumulate_to_pending_baseline(db, change_event)
    except Exception:
        logger.exception(f"sync_to_governance failed for event {change_event.id} entity_type={entity_type}")


def _sync_mission(db: Session, event: OrgChangeEvent):
    detail = db.query(DeptMissionDetail).filter(DeptMissionDetail.id == event.entity_id).first()
    if not detail:
        return
    missions = db.query(GovernanceDepartmentMission).filter(
        GovernanceDepartmentMission.department_id == detail.department_id
    ).all()
    for m in missions:
        if detail.mission_summary:
            m.mission_statement = detail.mission_summary
        if detail.core_functions:
            m.core_role = "; ".join(f["name"] for f in detail.core_functions if isinstance(f, dict) and "name" in f)
        if detail.upstream_deps:
            m.upstream_dependencies = detail.upstream_deps
        if detail.downstream_deliveries:
            m.downstream_deliverables = detail.downstream_deliveries
        m.source = "import"
        m.updated_at = datetime.datetime.utcnow()
    _record_impact(db, event, "mission_sync", "governance_department_mission", detail.department_id, f"部门 {detail.department_id} 职责已同步")


def _sync_process(db: Session, event: OrgChangeEvent):
    process = db.query(BizProcess).filter(BizProcess.id == event.entity_id).first()
    if not process or not process.process_nodes:
        return
    involved_data = set()
    for node in (process.process_nodes or []):
        if isinstance(node, dict):
            for d in (node.get("input_data") or []):
                involved_data.add(d)
            for d in (node.get("output_data") or []):
                involved_data.add(d)
    _record_impact(db, event, "resource_library", "biz_process", process.id, f"流程 {process.name} 涉及数据域: {involved_data}")


def _sync_terminology(db: Session, event: OrgChangeEvent):
    term = db.query(BizTerminology).filter(BizTerminology.id == event.entity_id).first()
    if not term or not term.resource_library_code:
        return
    lib = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == term.resource_library_code).first()
    if not lib:
        return
    hints = lib.classification_hints or {}
    keyword_list = hints.get("keywords", [])
    if term.term not in keyword_list:
        keyword_list.append(term.term)
    for alias in (term.aliases or []):
        if alias not in keyword_list:
            keyword_list.append(alias)
    hints["keywords"] = keyword_list
    lib.classification_hints = hints
    lib.updated_at = datetime.datetime.utcnow()
    _record_impact(db, event, "classification_rule", "resource_library", lib.id, f"资源库 {lib.code} classification_hints 已更新")


def _sync_data_asset(db: Session, event: OrgChangeEvent):
    asset = db.query(DataAssetOwnership).filter(DataAssetOwnership.id == event.entity_id).first()
    if not asset or not asset.resource_library_code:
        return
    lib = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == asset.resource_library_code).first()
    if not lib:
        return
    lib.consumer_departments = asset.consumer_department_ids or []
    if asset.update_frequency:
        lib.default_update_cycle = asset.update_frequency
    lib.updated_at = datetime.datetime.utcnow()
    _record_impact(db, event, "resource_library", "resource_library", lib.id, f"资源库 {lib.code} consumer/cycle 已同步")


def _sync_access_rule(db: Session, event: OrgChangeEvent):
    rule = db.query(PositionAccessRule).filter(PositionAccessRule.id == event.entity_id).first()
    if not rule:
        return
    visibility = _ACCESS_RANGE_TO_VISIBILITY.get(rule.access_range)
    if visibility is None:
        db.query(DataScopePolicy).filter(
            DataScopePolicy.target_type == PolicyTargetType.POSITION,
            DataScopePolicy.target_position_id == rule.position_id,
        ).delete(synchronize_session=False)
        _record_impact(db, event, "access_policy", "data_scope_policy", rule.position_id, f"岗位 {rule.position_id} 策略已删除", severity="high")
        return
    policy = db.query(DataScopePolicy).filter(
        DataScopePolicy.target_type == PolicyTargetType.POSITION,
        DataScopePolicy.target_position_id == rule.position_id,
    ).first()
    if policy:
        policy.visibility_level = visibility
        policy.output_mask = rule.excluded_fields or []
    else:
        policy = DataScopePolicy(
            target_type=PolicyTargetType.POSITION,
            target_position_id=rule.position_id,
            resource_type=PolicyResourceType.DATA_DOMAIN,
            visibility_level=visibility,
            output_mask=rule.excluded_fields or [],
        )
        db.add(policy)
    _record_impact(db, event, "access_policy", "data_scope_policy", rule.position_id, f"岗位 {rule.position_id} 策略已同步")


# ══════════════════════════════════════════════════════════════════════════════
# 2. 组织基线版本中心
# ══════════════════════════════════════════════════════════════════════════════

def create_initial_baseline(db: Session, user_id: int, import_session_id: int) -> OrgBaseline:
    """首次导入 → 创建 v0.1 组织基线 + 同步创建治理引擎 snapshot"""
    existing = db.query(OrgBaseline).filter(OrgBaseline.version == "v0.1").first()
    if existing:
        return existing

    summary = _build_snapshot_summary(db)

    baseline = OrgBaseline(
        version="v0.1",
        version_type="init",
        status="active",
        snapshot_summary=summary,
        diff_from_previous=[],
        impact_analysis={},
        trigger_source="import",
        trigger_import_session_id=import_session_id,
        created_by=user_id,
        activated_by=user_id,
        activated_at=datetime.datetime.utcnow(),
    )
    db.add(baseline)
    db.flush()

    # 同步创建治理引擎 snapshot
    gov_snapshot = GovernanceBaselineSnapshot(
        change_type="init",
        version="v0.1",
        version_type="init",
        snapshot_data={"source": "org_baseline", "org_baseline_id": baseline.id},
        stats_data=summary,
        changed_by=user_id,
        is_active=True,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(gov_snapshot)
    db.flush()

    baseline.governance_snapshot_id = gov_snapshot.id
    db.flush()
    return baseline


def create_candidate_baseline(db: Session, user_id: int, note: str | None = None, trigger_source: str = "manual") -> OrgBaseline:
    """从当前组织状态创建候选版本"""
    current_active = get_active_baseline(db)
    prev_version = current_active.version if current_active else "v0.0"
    new_version = _next_version(prev_version)

    summary = _build_snapshot_summary(db)

    # 计算与当前 active 版本的差异
    diff = _compute_diff_from_active(db, current_active)

    # 影响面分析
    impact = _compute_impact_analysis(db, diff)

    baseline = OrgBaseline(
        version=new_version,
        version_type="incremental",
        status="candidate",
        snapshot_summary=summary,
        diff_from_previous=diff,
        impact_analysis=impact,
        trigger_source=trigger_source,
        created_by=user_id,
        note=note,
    )
    db.add(baseline)
    db.flush()
    return baseline


def activate_baseline(db: Session, baseline_id: int, user_id: int) -> OrgBaseline:
    """candidate → active，旧 active → archived"""
    baseline = db.query(OrgBaseline).get(baseline_id)
    if not baseline:
        raise ValueError("基线版本不存在")
    if baseline.status != "candidate":
        raise ValueError(f"只有 candidate 状态的基线可以激活，当前状态: {baseline.status}")

    # 归档旧 active
    old_active = get_active_baseline(db)
    if old_active:
        old_active.status = "archived"
        old_active.archived_at = datetime.datetime.utcnow()

    baseline.status = "active"
    baseline.activated_by = user_id
    baseline.activated_at = datetime.datetime.utcnow()

    # 同步到治理引擎
    gov_snapshot = GovernanceBaselineSnapshot(
        change_type="incremental",
        version=baseline.version,
        version_type="incremental",
        snapshot_data={"source": "org_baseline", "org_baseline_id": baseline.id},
        stats_data=baseline.snapshot_summary,
        changed_by=user_id,
        is_active=True,
        created_at=datetime.datetime.utcnow(),
    )
    # 旧治理 snapshot 设为非活跃
    db.query(GovernanceBaselineSnapshot).filter(
        GovernanceBaselineSnapshot.is_active == True  # noqa: E712
    ).update({"is_active": False})
    db.add(gov_snapshot)
    db.flush()

    baseline.governance_snapshot_id = gov_snapshot.id
    db.flush()
    return baseline


def get_active_baseline(db: Session) -> OrgBaseline | None:
    return db.query(OrgBaseline).filter(OrgBaseline.status == "active").first()


def get_candidate_baseline(db: Session) -> OrgBaseline | None:
    return db.query(OrgBaseline).filter(OrgBaseline.status == "candidate").order_by(OrgBaseline.created_at.desc()).first()


def _accumulate_to_pending_baseline(db: Session, event: OrgChangeEvent):
    """将变更记录累积到当前 candidate 基线的 diff 中"""
    candidate = get_candidate_baseline(db)
    if not candidate:
        return
    diff = candidate.diff_from_previous or []
    diff.append({
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "change_type": event.change_type,
        "summary": f"{event.entity_type}#{event.entity_id} {event.change_type}",
        "event_id": event.id,
    })
    candidate.diff_from_previous = diff
    candidate.impact_analysis = _compute_impact_analysis(db, diff)


def _build_snapshot_summary(db: Session) -> dict:
    """构建当前组织全貌统计"""
    return {
        "department_count": db.query(Department).filter(Department.lifecycle_status != "dissolved").count(),
        "user_count": db.query(User).filter(User.is_active == True, User.username != "_system").count(),  # noqa: E712
        "position_count": db.query(func.count()).select_from(PositionAccessRule).scalar() or 0,
        "okr_period_count": db.query(OkrPeriod).count(),
        "okr_objective_count": db.query(OkrObjective).count(),
        "kr_count": db.query(OkrKeyResult).count(),
        "kpi_count": db.query(KpiAssignment).count(),
        "dept_mission_count": db.query(DeptMissionDetail).count(),
        "biz_process_count": db.query(BizProcess).filter(BizProcess.is_active == True).count(),  # noqa: E712
        "terminology_count": db.query(BizTerminology).count(),
        "data_asset_count": db.query(DataAssetOwnership).count(),
        "collab_link_count": db.query(DeptCollaborationLink).count(),
        "access_rule_count": db.query(PositionAccessRule).count(),
        "competency_model_count": db.query(PositionCompetencyModel).count(),
        "resource_lib_def_count": db.query(ResourceLibraryDefinition).count(),
        "kr_mapping_count": db.query(KrResourceMapping).count(),
        "collab_protocol_count": db.query(CollabProtocol).count(),
        "generated_at": datetime.datetime.utcnow().isoformat(),
    }


def _compute_diff_from_active(db: Session, active_baseline: OrgBaseline | None) -> list[dict]:
    """计算自上次 active baseline 以来的变更"""
    if not active_baseline or not active_baseline.activated_at:
        return []
    events = db.query(OrgChangeEvent).filter(
        OrgChangeEvent.created_at > active_baseline.activated_at
    ).order_by(OrgChangeEvent.created_at).all()
    return [
        {
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "change_type": e.change_type,
            "summary": f"{e.entity_type}#{e.entity_id} {e.change_type}",
            "event_id": e.id,
        }
        for e in events
    ]


def _compute_impact_analysis(db: Session, diff: list[dict]) -> dict:
    """根据 diff 计算对治理体系的影响面"""
    affected_libs = set()
    affected_policies = 0
    affected_rules = 0
    affected_missions = 0

    for item in diff:
        et = item.get("entity_type", "")
        if et in ("dept_mission", "department"):
            affected_missions += 1
        elif et in ("terminology", "data_asset", "biz_process"):
            affected_libs.add(item.get("entity_id", 0))
        elif et == "access_rule":
            affected_policies += 1
            affected_rules += 1

    return {
        "affected_resource_libraries": len(affected_libs),
        "affected_policies": affected_policies,
        "affected_rules": affected_rules,
        "affected_missions": affected_missions,
        "total_changes": len(diff),
    }


def _next_version(current: str) -> str:
    parts = current.lstrip("v").split(".")
    if len(parts) == 2:
        major, minor = int(parts[0]), int(parts[1])
        return f"v{major}.{minor + 1}"
    return f"{current}.1"


# ══════════════════════════════════════════════════════════════════════════════
# 3. 影响面记录
# ══════════════════════════════════════════════════════════════════════════════

def _record_impact(db: Session, event: OrgChangeEvent, impact_type: str,
                   target_type: str, target_id: int | None, description: str,
                   severity: str = "medium"):
    """记录变更对治理体系的影响"""
    active = get_active_baseline(db)
    if not active:
        return
    impact = OrgChangeImpact(
        baseline_id=active.id,
        change_event_id=event.id,
        impact_type=impact_type,
        impact_target_type=target_type,
        impact_target_id=target_id,
        severity=severity,
        description=description,
    )
    db.add(impact)


# ══════════════════════════════════════════════════════════════════════════════
# 4. 反向回流：治理引擎状态 → 组织管理展示
# ══════════════════════════════════════════════════════════════════════════════

def get_governance_sync_status(db: Session) -> dict:
    """获取治理引擎与组织管理的同步状态全貌"""
    active_baseline = get_active_baseline(db)
    candidate_baseline = get_candidate_baseline(db)

    # 治理引擎 active snapshot
    gov_snapshot = db.query(GovernanceBaselineSnapshot).filter(
        GovernanceBaselineSnapshot.is_active == True  # noqa: E712
    ).first()

    # Mission 同步状态
    depts = db.query(Department).filter(Department.lifecycle_status != "dissolved").all()
    dept_ids = [d.id for d in depts]
    missions_with_detail = db.query(DeptMissionDetail.department_id).all()
    mission_dept_ids = {m[0] for m in missions_with_detail}
    gov_missions = db.query(GovernanceDepartmentMission.department_id).distinct().all()
    gov_mission_dept_ids = {m[0] for m in gov_missions}

    mission_synced = mission_dept_ids & gov_mission_dept_ids
    mission_pending = mission_dept_ids - gov_mission_dept_ids
    mission_missing = set(dept_ids) - mission_dept_ids

    # 资源库缺基线字段
    all_libs = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.is_active == True).all()  # noqa: E712
    libs_missing_fields = [
        {"id": lib.id, "code": lib.code, "name": lib.name, "issue": "缺少 field_schema 定义"}
        for lib in all_libs if not lib.field_schema
    ]
    libs_missing_cycle = [
        {"id": lib.id, "code": lib.code, "name": lib.name, "issue": "缺少更新周期"}
        for lib in all_libs if not lib.default_update_cycle
    ]
    libs_missing_consumer = [
        {"id": lib.id, "code": lib.code, "name": lib.name, "issue": "无消费部门"}
        for lib in all_libs if not lib.consumer_departments
    ]

    # 访问规则落治理策略状态
    access_rules = db.query(PositionAccessRule).all()
    policies = db.query(DataScopePolicy).filter(DataScopePolicy.target_type == PolicyTargetType.POSITION).all()
    policy_position_ids = {p.target_position_id for p in policies}
    rules_synced = sum(1 for r in access_rules if r.position_id in policy_position_ids)
    rules_pending = sum(1 for r in access_rules if r.position_id not in policy_position_ids)

    # 组织变更触发的建议任务
    pending_suggestions = db.query(GovernanceSuggestionTask).filter(
        GovernanceSuggestionTask.status == "pending"
    ).count()

    # 未解决的影响项
    unresolved_impacts = 0
    if active_baseline:
        unresolved_impacts = db.query(OrgChangeImpact).filter(
            OrgChangeImpact.baseline_id == active_baseline.id,
            OrgChangeImpact.resolved == False,  # noqa: E712
        ).count()

    # baseline 一致性
    baseline_consistent = True
    if active_baseline and gov_snapshot:
        baseline_consistent = active_baseline.governance_snapshot_id == gov_snapshot.id

    return {
        # 基线版本
        "active_baseline": {
            "version": active_baseline.version if active_baseline else None,
            "status": active_baseline.status if active_baseline else "no_data",
            "activated_at": active_baseline.activated_at.isoformat() if active_baseline and active_baseline.activated_at else None,
            "snapshot_summary": active_baseline.snapshot_summary if active_baseline else {},
        },
        "candidate_baseline": {
            "version": candidate_baseline.version if candidate_baseline else None,
            "diff_count": len(candidate_baseline.diff_from_previous or []) if candidate_baseline else 0,
            "impact_analysis": candidate_baseline.impact_analysis if candidate_baseline else {},
        } if candidate_baseline else None,
        "governance_snapshot": {
            "version": gov_snapshot.version if gov_snapshot else None,
            "is_active": gov_snapshot.is_active if gov_snapshot else False,
        },
        "baseline_consistent": baseline_consistent,

        # Mission 同步
        "mission_sync": {
            "total_depts": len(dept_ids),
            "synced": len(mission_synced),
            "pending_sync": len(mission_pending),
            "missing_detail": len(mission_missing),
        },

        # 资源库基线
        "resource_library_gaps": {
            "total": len(all_libs),
            "missing_fields": libs_missing_fields,
            "missing_cycle": libs_missing_cycle,
            "missing_consumer": libs_missing_consumer,
        },

        # 访问规则同步
        "access_rule_sync": {
            "total_rules": len(access_rules),
            "synced_to_policy": rules_synced,
            "pending_sync": rules_pending,
        },

        # 治理任务
        "governance_tasks": {
            "pending_suggestions": pending_suggestions,
        },

        # 影响面
        "unresolved_impacts": unresolved_impacts,
    }
