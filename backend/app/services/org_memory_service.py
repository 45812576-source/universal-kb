import datetime
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

from app.models.org_memory import (
    OrgMemoryAppliedConfig,
    OrgMemoryApprovalLink,
    OrgMemoryConfigVersion,
    OrgMemoryProposal,
    OrgMemorySnapshot,
    OrgMemorySource,
)
from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus, PermissionAuditLog
from app.models.user import User


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _json(value: Any, fallback: Any) -> Any:
    return value if value is not None else fallback


def source_to_dto(source: OrgMemorySource) -> dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title,
        "source_type": source.source_type,
        "source_uri": source.source_uri,
        "owner_name": source.owner_name or "组织运营组",
        "external_version": source.external_version,
        "fetched_at": _iso(source.fetched_at),
        "ingest_status": source.ingest_status,
        "latest_snapshot_id": source.latest_snapshot_id,
        "latest_snapshot_version": source.latest_snapshot_version,
        "latest_parse_note": source.latest_parse_note,
        "bitable_app_token": source.bitable_app_token,
        "bitable_table_id": source.bitable_table_id,
        "raw_fields": source.raw_fields_json,
        "raw_records_count": len(source.raw_records_json) if source.raw_records_json else 0,
    }


def snapshot_to_dto(snapshot: OrgMemorySnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "source_id": snapshot.source_id,
        "source_title": snapshot.source.title if snapshot.source else "未命名来源",
        "snapshot_version": snapshot.snapshot_version,
        "parse_status": snapshot.parse_status,
        "confidence_score": snapshot.confidence_score or 0,
        "created_at": _iso(snapshot.created_at) or _now().isoformat(),
        "summary": snapshot.summary or "",
        "entity_counts": _json(snapshot.entity_counts_json, {}),
        "units": _json(snapshot.units_json, []),
        "roles": _json(snapshot.roles_json, []),
        "people": _json(snapshot.people_json, []),
        "okrs": _json(snapshot.okrs_json, []),
        "processes": _json(snapshot.processes_json, []),
        "low_confidence_items": _json(snapshot.low_confidence_items_json, []),
    }


def applied_config_to_dto(config: OrgMemoryAppliedConfig) -> dict[str, Any]:
    return {
        "id": config.id,
        "proposal_id": config.proposal_id,
        "approval_request_id": config.approval_request_id,
        "status": config.status,
        "applied_at": _iso(config.applied_at) or _now().isoformat(),
        "knowledge_paths": _json(config.knowledge_paths_json, []),
        "classification_rule_count": config.classification_rule_count or 0,
        "skill_mount_count": config.skill_mount_count or 0,
        "conditions": _json(config.conditions_json, []),
    }


def config_version_to_dto(version: OrgMemoryConfigVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "proposal_id": version.proposal_id,
        "applied_config_id": version.applied_config_id,
        "version": version.version,
        "action": version.action,
        "status": version.status,
        "applied_at": _iso(version.applied_at) or _now().isoformat(),
        "knowledge_paths": _json(version.knowledge_paths_json, []),
        "classification_rule_count": version.classification_rule_count or 0,
        "skill_mount_count": version.skill_mount_count or 0,
        "conditions": _json(version.conditions_json, []),
        "note": version.note,
    }


def proposal_to_dto(proposal: OrgMemoryProposal, db: Session | None = None) -> dict[str, Any]:
    applied_config = None
    configs = list(proposal.applied_configs or [])
    active = next((item for item in sorted(configs, key=lambda item: item.id, reverse=True) if item.status != "rolled_back"), None)
    if active:
        applied_config = applied_config_to_dto(active)

    return {
        "id": proposal.id,
        "snapshot_id": proposal.snapshot_id,
        "title": proposal.title,
        "proposal_status": proposal.proposal_status,
        "risk_level": proposal.risk_level,
        "summary": proposal.summary or "",
        "impact_summary": proposal.impact_summary or "",
        "created_at": _iso(proposal.created_at) or _now().isoformat(),
        "submitted_at": _iso(proposal.submitted_at),
        "structure_changes": _json(proposal.structure_changes_json, []),
        "classification_rules": _json(proposal.classification_rules_json, []),
        "skill_mounts": _json(proposal.skill_mounts_json, []),
        "approval_impacts": _json(proposal.approval_impacts_json, []),
        "evidence_refs": _json(proposal.evidence_refs_json, []),
        "applied_config": applied_config,
    }


def list_sources(db: Session) -> list[dict[str, Any]]:
    return [source_to_dto(item) for item in db.query(OrgMemorySource).order_by(OrgMemorySource.id.desc()).all()]


def list_snapshots(db: Session) -> list[dict[str, Any]]:
    return [snapshot_to_dto(item) for item in db.query(OrgMemorySnapshot).order_by(OrgMemorySnapshot.id.desc()).all()]


def list_proposals(db: Session) -> list[dict[str, Any]]:
    return [proposal_to_dto(item, db) for item in db.query(OrgMemoryProposal).order_by(OrgMemoryProposal.id.desc()).all()]


def create_source(db: Session, user: User, payload: dict[str, Any]) -> OrgMemorySource:
    now = _now()
    raw_fields = payload.get("raw_fields")
    raw_records = payload.get("raw_records")
    source_type = payload.get("source_type") or "markdown"
    has_structured_data = bool(raw_fields or raw_records)
    if has_structured_data and source_type == "upload":
        parse_note = f"已解析上传文件，{len(raw_records or [])} 个段落。"
    elif has_structured_data:
        parse_note = f"已解析飞书多维表格数据，{len(raw_fields or [])} 个字段、{len(raw_records or [])} 条记录。"
    else:
        parse_note = "资料已添加，可与其他资料一起生成快照。"
    source = OrgMemorySource(
        title=str(payload.get("title") or "组织 Memory 源文档"),
        source_type=str(payload.get("source_type") or "markdown"),
        source_uri=str(payload.get("source_uri") or f"manual://org-memory/source-{int(now.timestamp())}"),
        owner_name=str(payload.get("owner_name") or "组织运营组"),
        external_version=f"v{now.strftime('%Y.%m.%d.%H%M%S')}",
        fetched_at=now,
        ingest_status="ready",
        latest_parse_note=parse_note,
        bitable_app_token=payload.get("bitable_app_token"),
        bitable_table_id=payload.get("bitable_table_id"),
        raw_fields_json=raw_fields,
        raw_records_json=raw_records,
        created_by=user.id,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def delete_source(db: Session, source: OrgMemorySource) -> None:
    db.delete(source)
    db.commit()


async def batch_create_snapshots(db: Session, sources: list[OrgMemorySource]) -> list[OrgMemorySnapshot]:
    snapshots = []
    for source in sources:
        snapshot = await create_snapshot(db, source)
        snapshots.append(snapshot)
    return snapshots


def _snapshot_payload(source: OrgMemorySource, snapshot_id: int) -> dict[str, Any]:
    evidence = [{"label": "源文档", "section": source.external_version or "latest", "excerpt": source.title}]
    unit_name = source.owner_name or "组织运营组"
    units = [{
        "id": snapshot_id * 10 + 1,
        "name": unit_name,
        "unit_type": "department",
        "parent_name": "公司",
        "leader_name": None,
        "responsibilities": ["组织治理", "知识目录维护", "跨部门协作规则维护"],
        "evidence_refs": evidence,
    }]
    roles = [{
        "id": snapshot_id * 10 + 2,
        "name": "组织 Memory 维护人",
        "department_name": unit_name,
        "responsibilities": ["维护源文档", "复核结构化快照", "发起统一草案审批"],
        "evidence_refs": evidence,
    }]
    people = [{
        "id": snapshot_id * 10 + 3,
        "name": "待确认人员",
        "department_name": unit_name,
        "role_name": "组织 Memory 维护人",
        "manager_name": None,
        "employment_status": "active",
        "evidence_refs": evidence,
    }]
    okrs = [{
        "id": snapshot_id * 10 + 4,
        "owner_name": unit_name,
        "period": datetime.date.today().strftime("%YQ%q") if False else "当前周期",
        "objective": "提升组织知识复用与审批治理一致性",
        "key_results": ["源文档可追溯", "草案审批可回滚", "Skill 挂载边界清晰"],
        "evidence_refs": evidence,
    }]
    processes = [{
        "id": snapshot_id * 10 + 5,
        "owner_name": unit_name,
        "name": "组织 Memory 草案生效流程",
        "participants": [unit_name, "审批管理员", "Skill 负责人"],
        "outputs": ["知识目录建议", "分类规则建议", "Skill 挂载建议"],
        "risk_points": ["共享边界扩大", "匿名化要求降低", "Skill 消费范围扩大"],
        "evidence_refs": evidence,
    }]
    return {
        "entity_counts": {
            "units": len(units),
            "roles": len(roles),
            "people": len(people),
            "okrs": len(okrs),
            "processes": len(processes),
        },
        "units": units,
        "roles": roles,
        "people": people,
        "okrs": okrs,
        "processes": processes,
        "low_confidence_items": [] if source.source_type != "upload" else [{
            "label": "上传文档格式",
            "reason": "上传文档章节结构可能不稳定，审批前建议复核证据链。",
        }],
    }


async def _llm_extract_org_objects(db: Session, source: OrgMemorySource, snapshot_id: int) -> dict[str, Any]:
    """调用 LLM 分析飞书多维表格数据，提取六类组织对象。失败时 fallback 到硬编码模板。"""
    fields = source.raw_fields_json or []
    records = source.raw_records_json or []

    field_desc = "\n".join(f"- {f.get('name', '?')}（{f.get('type', '?')}）" for f in fields)
    sample_records = records[:30]
    records_text = json.dumps(sample_records, ensure_ascii=False, default=str)
    # 截断避免 token 过长
    if len(records_text) > 12000:
        records_text = records_text[:12000] + "\n... (已截断)"

    prompt = f"""你是组织分析专家。根据飞书多维表格数据，提取组织结构信息。

## 字段定义
{field_desc}

## 数据（前{len(sample_records)}条）
{records_text}

请从数据中去重提取以下六类对象，返回严格 JSON：
- units: 去重的部门/事业部列表，每项 {{"name": "部门名", "unit_type": "department", "responsibilities": ["职责1"]}}
- roles: 去重的岗位列表，每项 {{"name": "岗位名", "department_name": "所属部门"}}
- people: 如有具体人名，每项 {{"name": "姓名", "department_name": "部门", "role_name": "岗位"}}
- okrs: 如有绩效目标/KPI，每项 {{"objective": "目标描述", "key_results": ["KR1"]}}
- processes: 如有流程，每项 {{"name": "流程名", "participants": ["参与方"]}}
- summary: 一句话描述这份数据的内容
- entity_counts: 各类计数 {{"units": N, "roles": N, "people": N, "okrs": N, "processes": N}}

要求：
1. 从数据中去重提取，不要编造不存在的信息
2. 某类不存在则返回空数组
3. 只输出 JSON，不要输出其他文字"""

    try:
        config = llm_gateway.resolve_config(db, "governance.classify")
        messages = [{"role": "user", "content": prompt}]
        content, _usage = await llm_gateway.chat(config, messages, temperature=0.1, max_tokens=4096)

        # 提取 JSON：支持 ```json ... ``` 包裹或直接 JSON
        text = content.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        parsed = json.loads(text)

        # 为每个对象补充 id 和 evidence_refs
        evidence = [{"label": "飞书多维表格", "section": source.external_version or "latest", "excerpt": source.title}]
        for i, unit in enumerate(parsed.get("units", [])):
            unit.setdefault("id", snapshot_id * 100 + i + 1)
            unit.setdefault("unit_type", "department")
            unit.setdefault("parent_name", "公司")
            unit.setdefault("leader_name", None)
            unit.setdefault("responsibilities", [])
            unit["evidence_refs"] = evidence
        for i, role in enumerate(parsed.get("roles", [])):
            role.setdefault("id", snapshot_id * 100 + 50 + i + 1)
            role.setdefault("responsibilities", [])
            role["evidence_refs"] = evidence
        for i, person in enumerate(parsed.get("people", [])):
            person.setdefault("id", snapshot_id * 100 + 200 + i + 1)
            person.setdefault("manager_name", None)
            person.setdefault("employment_status", "active")
            person["evidence_refs"] = evidence
        for i, okr in enumerate(parsed.get("okrs", [])):
            okr.setdefault("id", snapshot_id * 100 + 300 + i + 1)
            okr.setdefault("owner_name", "")
            okr.setdefault("period", "当前周期")
            okr.setdefault("key_results", [])
            okr["evidence_refs"] = evidence
        for i, proc in enumerate(parsed.get("processes", [])):
            proc.setdefault("id", snapshot_id * 100 + 400 + i + 1)
            proc.setdefault("owner_name", "")
            proc.setdefault("participants", [])
            proc.setdefault("outputs", [])
            proc.setdefault("risk_points", [])
            proc["evidence_refs"] = evidence

        units = parsed.get("units", [])
        roles = parsed.get("roles", [])
        people = parsed.get("people", [])
        okrs = parsed.get("okrs", [])
        processes = parsed.get("processes", [])

        return {
            "summary": parsed.get("summary", ""),
            "entity_counts": parsed.get("entity_counts", {
                "units": len(units),
                "roles": len(roles),
                "people": len(people),
                "okrs": len(okrs),
                "processes": len(processes),
            }),
            "units": units,
            "roles": roles,
            "people": people,
            "okrs": okrs,
            "processes": processes,
            "low_confidence_items": [],
        }
    except Exception as e:
        logger.warning(f"LLM 提取组织对象失败，fallback 到硬编码模板: {e}")
        return _snapshot_payload(source, snapshot_id)


def _bitable_snapshot_summary(source: OrgMemorySource) -> str:
    fields = source.raw_fields_json or []
    records = source.raw_records_json or []
    field_names = [f.get("name", "?") for f in fields[:8]]
    sample_lines = []
    for row in records[:3]:
        parts = []
        for fn in field_names[:4]:
            val = row.get(fn)
            if val is not None:
                text = str(val)[:30]
                parts.append(f"{fn}={text}")
        if parts:
            sample_lines.append("  " + ", ".join(parts))
    summary = f"飞书多维表格数据：{len(fields)} 个字段、{len(records)} 条记录。"
    if field_names:
        summary += f"\n字段：{', '.join(field_names)}"
    if sample_lines:
        summary += "\n前几行样本：\n" + "\n".join(sample_lines)
    return summary


async def create_snapshot(db: Session, source: OrgMemorySource) -> OrgMemorySnapshot:
    has_bitable_data = bool(source.raw_fields_json or source.raw_records_json)
    if has_bitable_data:
        summary_text = _bitable_snapshot_summary(source)
    else:
        summary_text = f"已从《{source.title}》抽取组织、岗位、人员、OKR 与流程对象，可继续生成统一草案。"

    snapshot = OrgMemorySnapshot(
        source_id=source.id,
        snapshot_version="pending",
        parse_status="ready",
        confidence_score=0.95 if has_bitable_data else (0.82 if source.source_type == "upload" else 0.9),
        summary=summary_text,
    )
    db.add(snapshot)
    db.flush()

    if has_bitable_data:
        payload = await _llm_extract_org_objects(db, source, snapshot.id)
        # LLM 返回的 summary 覆盖默认摘要
        if payload.get("summary"):
            snapshot.summary = payload["summary"]
    else:
        payload = _snapshot_payload(source, snapshot.id)

    snapshot.snapshot_version = f"snapshot-{datetime.date.today().isoformat()}-{snapshot.id:02d}"
    snapshot.entity_counts_json = payload["entity_counts"]
    snapshot.units_json = payload["units"]
    snapshot.roles_json = payload["roles"]
    snapshot.people_json = payload["people"]
    snapshot.okrs_json = payload["okrs"]
    snapshot.processes_json = payload["processes"]
    snapshot.low_confidence_items_json = payload.get("low_confidence_items", [])
    source.ingest_status = "ready"
    source.fetched_at = _now()
    source.latest_snapshot_id = snapshot.id
    source.latest_snapshot_version = snapshot.snapshot_version
    source.latest_parse_note = (
        f"已生成结构化快照，包含 {len(source.raw_fields_json or [])} 个字段、{len(source.raw_records_json or [])} 条记录的实际数据。"
        if has_bitable_data
        else "已生成结构化快照，六类组织对象字段齐全。"
    )
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _proposal_payload(snapshot: OrgMemorySnapshot, proposal_id: int) -> dict[str, Any]:
    source_title = snapshot.source.title if snapshot.source else "组织 Memory"
    units = _json(snapshot.units_json, [])
    processes = _json(snapshot.processes_json, [])
    dept_scope = units[0]["name"] if units else "组织治理"
    path = f"/{source_title}/组织治理/培训与复盘"
    evidence = units[0].get("evidence_refs", []) if units else []
    if not evidence:
        evidence = [{"label": "快照摘要", "section": snapshot.snapshot_version, "excerpt": snapshot.summary or ""}]
    structure_changes = [{
        "id": proposal_id * 10 + 1,
        "change_type": "create",
        "target_path": path,
        "dept_scope": dept_scope,
        "rationale": "快照显示该组织域包含稳定职责、流程与复盘产物，适合作为知识库目录。",
        "confidence_score": min(snapshot.confidence_score or 0.9, 0.95),
    }]
    classification_rules = [{
        "id": proposal_id * 10 + 2,
        "target_scope": f"{source_title} 相关组织知识",
        "match_signals": ["组织职责", "岗位职责", "业务流程", "复盘材料"],
        "default_folder_path": path,
        "origin_scope": "manager_chain",
        "allowed_scope": "department",
        "usage_purpose": "knowledge_reuse",
        "redaction_mode": "summary",
        "rationale": "组织 Memory 只允许以摘要或匿名化形式进入部门共享知识域。",
    }]
    skill_mounts = [{
        "id": proposal_id * 10 + 3,
        "skill_id": None,
        "skill_name": "组织复盘助手",
        "target_scope": f"{source_title} 知识域",
        "required_domains": ["组织职责", "业务流程", "OKR"],
        "max_allowed_scope": "department",
        "required_redaction_mode": "summary",
        "decision": "require_approval",
        "rationale": "Skill 可消费组织摘要，但挂载到部门知识域前需要审批确认共享边界。",
    }]
    approval_impacts = [{
        "id": proposal_id * 10 + 4,
        "impact_type": "org_memory.proposal.generated",
        "target_asset_name": f"{source_title} 组织 Memory 草案",
        "risk_reason": "草案会影响知识目录、默认分类规则与 Skill 可用知识域。",
        "requires_manual_approval": True,
    }]
    return {
        "title": f"{source_title} Memory 草案 #{proposal_id}",
        "risk_level": "medium" if _json(snapshot.low_confidence_items_json, []) else "low",
        "summary": f"基于 {snapshot.snapshot_version} 生成目录、分类规则、共享边界与 Skill 挂载建议。",
        "impact_summary": f"涉及 {max((snapshot.entity_counts_json or {}).get('units', 1), 1)} 个组织域、{max(len(processes), 1)} 条流程相关规则。",
        "structure_changes": structure_changes,
        "classification_rules": classification_rules,
        "skill_mounts": skill_mounts,
        "approval_impacts": approval_impacts,
        "evidence_refs": evidence,
    }


def create_proposal(db: Session, snapshot: OrgMemorySnapshot) -> OrgMemoryProposal:
    existing = (
        db.query(OrgMemoryProposal)
        .filter(OrgMemoryProposal.snapshot_id == snapshot.id, OrgMemoryProposal.proposal_status == "draft")
        .first()
    )
    if existing:
        return existing
    proposal = OrgMemoryProposal(snapshot_id=snapshot.id, title="pending")
    db.add(proposal)
    db.flush()
    payload = _proposal_payload(snapshot, proposal.id)
    proposal.title = payload["title"]
    proposal.risk_level = payload["risk_level"]
    proposal.summary = payload["summary"]
    proposal.impact_summary = payload["impact_summary"]
    proposal.structure_changes_json = payload["structure_changes"]
    proposal.classification_rules_json = payload["classification_rules"]
    proposal.skill_mounts_json = payload["skill_mounts"]
    proposal.approval_impacts_json = payload["approval_impacts"]
    proposal.evidence_refs_json = payload["evidence_refs"]
    db.commit()
    db.refresh(proposal)
    return proposal


def snapshot_diff(db: Session, snapshot: OrgMemorySnapshot) -> dict[str, Any]:
    previous = (
        db.query(OrgMemorySnapshot)
        .filter(OrgMemorySnapshot.source_id == snapshot.source_id, OrgMemorySnapshot.id < snapshot.id)
        .order_by(OrgMemorySnapshot.id.desc())
        .first()
    )

    def names(items: list[dict[str, Any]], key: str) -> list[str]:
        return [str(item.get(key) or item.get("name") or "") for item in items if item]

    def bucket(current: list[str], old: list[str]) -> dict[str, list[str]]:
        return {
            "added": [item for item in current if item and item not in old],
            "removed": [item for item in old if item and item not in current],
        }

    if not previous:
        return {
            "snapshot_id": snapshot.id,
            "snapshot_version": snapshot.snapshot_version,
            "previous_snapshot_id": None,
            "previous_snapshot_version": None,
            "summary": "当前快照是该源文档的首个版本，暂无可对比的上一版。",
            "units": {"added": names(_json(snapshot.units_json, []), "name"), "removed": []},
            "roles": {"added": names(_json(snapshot.roles_json, []), "name"), "removed": []},
            "people": {"added": names(_json(snapshot.people_json, []), "name"), "removed": []},
            "okrs": {"added": names(_json(snapshot.okrs_json, []), "objective"), "removed": []},
            "processes": {"added": names(_json(snapshot.processes_json, []), "name"), "removed": []},
        }

    current_units = names(_json(snapshot.units_json, []), "name")
    previous_units = names(_json(previous.units_json, []), "name")
    return {
        "snapshot_id": snapshot.id,
        "snapshot_version": snapshot.snapshot_version,
        "previous_snapshot_id": previous.id,
        "previous_snapshot_version": previous.snapshot_version,
        "summary": f"与上一版 {previous.snapshot_version} 相比，组织对象变化已完成结构化比对。",
        "units": bucket(current_units, previous_units),
        "roles": bucket(names(_json(snapshot.roles_json, []), "name"), names(_json(previous.roles_json, []), "name")),
        "people": bucket(names(_json(snapshot.people_json, []), "name"), names(_json(previous.people_json, []), "name")),
        "okrs": bucket(names(_json(snapshot.okrs_json, []), "objective"), names(_json(previous.okrs_json, []), "objective")),
        "processes": bucket(names(_json(snapshot.processes_json, []), "name"), names(_json(previous.processes_json, []), "name")),
    }


def proposal_evidence_pack(proposal: OrgMemoryProposal) -> dict[str, Any]:
    return {
        "summary": proposal.summary,
        "impact_summary": proposal.impact_summary,
        "structure_changes": _json(proposal.structure_changes_json, []),
        "classification_rules": _json(proposal.classification_rules_json, []),
        "skill_mounts": _json(proposal.skill_mounts_json, []),
        "approval_impacts": _json(proposal.approval_impacts_json, []),
        "evidence_refs": _json(proposal.evidence_refs_json, []),
    }


def submit_proposal(db: Session, proposal: OrgMemoryProposal, user: User) -> ApprovalRequest:
    existing = (
        db.query(OrgMemoryApprovalLink)
        .filter(OrgMemoryApprovalLink.proposal_id == proposal.id)
        .order_by(OrgMemoryApprovalLink.id.desc())
        .first()
    )
    if existing:
        approval = db.get(ApprovalRequest, existing.approval_request_id)
        if approval:
            return approval

    approval = ApprovalRequest(
        request_type=ApprovalRequestType.ORG_MEMORY_PROPOSAL,
        target_id=proposal.id,
        target_type="org_memory_proposal",
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
        stage="dept_pending",
        evidence_pack=proposal_evidence_pack(proposal),
        risk_level=proposal.risk_level,
        impact_summary=proposal.impact_summary,
        conditions=[{"reason": proposal.summary or "组织 Memory 草案提交审批"}],
    )
    db.add(approval)
    db.flush()
    db.add(OrgMemoryApprovalLink(
        proposal_id=proposal.id,
        approval_request_id=approval.id,
        external_approval_type="internal_approval",
        external_status="pending",
        callback_payload_json={"source": "org_memory_submit"},
    ))
    proposal.proposal_status = "pending_approval"
    proposal.submitted_at = _now()
    db.commit()
    db.refresh(approval)
    return approval


def _next_config_version(db: Session, proposal_id: int) -> int:
    latest = (
        db.query(OrgMemoryConfigVersion)
        .filter(OrgMemoryConfigVersion.proposal_id == proposal_id)
        .order_by(OrgMemoryConfigVersion.version.desc())
        .first()
    )
    return (latest.version if latest else 0) + 1


def apply_proposal_config(
    db: Session,
    proposal: OrgMemoryProposal,
    approval_request_id: int | None,
    user: User,
    conditions: list | None = None,
) -> OrgMemoryAppliedConfig:
    conditions = conditions or []
    status = "effective_with_conditions" if conditions else "effective"
    active = (
        db.query(OrgMemoryAppliedConfig)
        .filter(OrgMemoryAppliedConfig.proposal_id == proposal.id, OrgMemoryAppliedConfig.status != "rolled_back")
        .order_by(OrgMemoryAppliedConfig.id.desc())
        .first()
    )
    if active:
        proposal.proposal_status = "partially_approved" if conditions else "approved"
        return active

    structure_changes = _json(proposal.structure_changes_json, [])
    classification_rules = _json(proposal.classification_rules_json, [])
    skill_mounts = _json(proposal.skill_mounts_json, [])
    knowledge_paths = [item.get("target_path") for item in structure_changes if isinstance(item, dict) and item.get("target_path")]
    applied = OrgMemoryAppliedConfig(
        proposal_id=proposal.id,
        approval_request_id=approval_request_id,
        status=status,
        knowledge_paths_json=knowledge_paths,
        classification_rule_count=len(classification_rules),
        skill_mount_count=len(skill_mounts),
        conditions_json=conditions,
    )
    db.add(applied)
    db.flush()
    db.add(OrgMemoryConfigVersion(
        proposal_id=proposal.id,
        applied_config_id=applied.id,
        version=_next_config_version(db, proposal.id),
        action="apply",
        status=status,
        knowledge_paths_json=knowledge_paths,
        classification_rule_count=len(classification_rules),
        skill_mount_count=len(skill_mounts),
        conditions_json=conditions,
        note="审批通过后写入组织 Memory 正式配置源",
    ))
    proposal.proposal_status = "partially_approved" if conditions else "approved"
    link = (
        db.query(OrgMemoryApprovalLink)
        .filter(OrgMemoryApprovalLink.proposal_id == proposal.id)
        .order_by(OrgMemoryApprovalLink.id.desc())
        .first()
    )
    if link:
        link.external_status = "approved"
        link.last_synced_at = _now()

    db.add(PermissionAuditLog(
        operator_id=user.id,
        action="org_memory.apply_config",
        target_table="org_memory_applied_configs",
        target_id=applied.id,
        old_values={},
        new_values=applied_config_to_dto(applied),
        reason=f"approval_request_id={approval_request_id}",
    ))
    db.commit()
    db.refresh(applied)
    return applied


def reject_proposal(db: Session, proposal: OrgMemoryProposal, user: User, reason: str | None = None) -> None:
    old_status = proposal.proposal_status
    proposal.proposal_status = "rejected"
    link = (
        db.query(OrgMemoryApprovalLink)
        .filter(OrgMemoryApprovalLink.proposal_id == proposal.id)
        .order_by(OrgMemoryApprovalLink.id.desc())
        .first()
    )
    if link:
        link.external_status = "rejected"
        link.last_synced_at = _now()
    db.add(PermissionAuditLog(
        operator_id=user.id,
        action="org_memory.reject_proposal",
        target_table="org_memory_proposals",
        target_id=proposal.id,
        old_values={"proposal_status": old_status},
        new_values={"proposal_status": "rejected"},
        reason=reason,
    ))


def rollback_proposal_config(db: Session, proposal: OrgMemoryProposal, user: User) -> dict[str, Any]:
    active = (
        db.query(OrgMemoryAppliedConfig)
        .filter(OrgMemoryAppliedConfig.proposal_id == proposal.id, OrgMemoryAppliedConfig.status != "rolled_back")
        .order_by(OrgMemoryAppliedConfig.id.desc())
        .first()
    )
    if not active:
        return {
            "proposal_id": proposal.id,
            "status": "noop",
            "rolled_back_config_id": None,
            "message": "当前草案没有生效配置，无需回滚",
        }
    active.status = "rolled_back"
    db.add(OrgMemoryConfigVersion(
        proposal_id=proposal.id,
        applied_config_id=active.id,
        version=_next_config_version(db, proposal.id),
        action="rollback",
        status="rolled_back",
        knowledge_paths_json=_json(active.knowledge_paths_json, []),
        classification_rule_count=active.classification_rule_count or 0,
        skill_mount_count=active.skill_mount_count or 0,
        conditions_json=_json(active.conditions_json, []),
        note="按版本链路回滚组织 Memory 正式配置",
    ))
    proposal.proposal_status = "approved"
    db.add(PermissionAuditLog(
        operator_id=user.id,
        action="org_memory.rollback_config",
        target_table="org_memory_applied_configs",
        target_id=active.id,
        old_values={"status": "effective"},
        new_values={"status": "rolled_back"},
        reason=f"proposal_id={proposal.id}",
    ))
    db.commit()
    return {
        "proposal_id": proposal.id,
        "status": "rolled_back",
        "rolled_back_config_id": active.id,
        "message": "已按版本链路回滚正式配置",
    }
