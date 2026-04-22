from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.org_memory import (
    OrgGovernanceSnapshot,
    OrgGovernanceSnapshotRun,
    OrgGovernanceSnapshotSourceLink,
    OrgGovernanceSnapshotTab,
    OrgMemorySource,
)
from app.models.user import User
from app.schemas.org_governance_snapshot import WorkspaceSnapshotEventRequest
from app.services.org_governance_policy_projection import project_governance_outputs
from app.services.org_governance_snapshot_parser import TAB_KEYS, parse_tab_markdown


TAB_TITLES = {
    "organization": "组织",
    "department": "部门",
    "role": "岗位",
    "person": "人员",
    "okr": "OKR",
    "process": "业务流程",
}

TAB_SUBHEADINGS = {
    "organization": {
        "facts": ["组织定位", "组织结构总览", "正式组织线", "行政汇报线", "业务协作线", "矩阵 / 项目线", "治理角色线"],
        "governance": ["默认共享边界", "决策机制", "资源归属机制", "审批与升级机制", "例外授权机制", "权限控制影响"],
        "analysis": ["组织瓶颈", "主要风险", "推断与假设"],
        "actions": ["待确认项", "下一步动作"],
    },
    "department": {
        "facts": ["部门使命", "职责范围", "职责边界", "输入输出", "上下游协作", "团队配置", "服务对象"],
        "governance": ["部门数据域", "资源归属", "默认可见范围", "跨部门共享条件", "审批责任", "权限控制影响"],
        "analysis": ["现阶段问题", "协作断点", "风险判断", "推断与假设"],
        "actions": ["待确认项", "本季度重点", "下一步动作"],
    },
    "role": {
        "facts": ["岗位目的", "核心职责", "辅助职责", "不负责事项", "关键产出", "汇报关系", "协作对象", "能力要求"],
        "governance": ["可执行动作", "需审批动作", "禁止动作", "可访问资源类型", "审批权 / 复核权 / 配置权", "敏感操作边界", "权限控制影响"],
        "analysis": ["常见失误", "评估标准", "职责分离风险", "推断与假设"],
        "actions": ["待确认项", "下一步动作"],
    },
    "person": {
        "facts": ["基本信息", "当前岗位", "所属部门", "汇报关系", "职责承担", "复合身份", "项目角色"],
        "governance": ["当前授权", "代理授权", "临时授权", "例外访问", "授权到期条件", "回收触发条件", "权限控制影响"],
        "analysis": ["优势标签", "短板风险", "适配度判断", "职责分离风险", "推断与假设"],
        "actions": ["待确认项", "发展建议", "近期动作"],
    },
    "okr": {
        "facts": ["目标背景", "O 定义", "KR 列表", "责任归属", "关联部门 / 岗位 / 人员", "当前进展"],
        "governance": ["执行资源需求", "跨部门协作范围", "所需权限能力", "权限缺口", "风险控制", "权限控制影响"],
        "analysis": ["风险阻塞", "目标与职责匹配度", "推断与假设"],
        "actions": ["待确认项", "纠偏动作", "下一步动作"],
    },
    "process": {
        "facts": ["流程目标", "触发条件", "适用范围", "输入材料", "关键步骤", "责任角色", "输出结果", "SLA / 时效", "异常处理"],
        "governance": ["节点责任人", "节点动作", "节点资源", "查看 / 编辑 / 审批 / 导出边界", "脱敏点", "审批点", "异常升级路径", "权限控制影响"],
        "analysis": ["当前断点", "职责切换风险", "职责分离风险", "推断与假设"],
        "actions": ["待确认项", "优化建议", "下一步动作"],
    },
}


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _json(value: Any, fallback: Any) -> Any:
    return value if value is not None else fallback


def _empty_sync_status() -> dict[str, Any]:
    return {
        "markdown_saved": False,
        "structured_updated": False,
        "failed_sections": [],
        "parser_warnings": [],
    }


def _empty_change_summary() -> dict[str, list[dict[str, Any]]]:
    return {"added": [], "changed": [], "removed": []}


def _requested_tabs(scope: str, active_tab: str | None = None) -> list[str]:
    if scope == "all":
        return list(TAB_KEYS)
    if scope == "active_tab":
        return [active_tab] if active_tab in TAB_KEYS else list(TAB_KEYS)
    return [scope] if scope in TAB_KEYS else list(TAB_KEYS)


def _frontmatter(tab_key: str, title: str, version: str, source_titles: list[str]) -> str:
    source_materials = json.dumps(source_titles, ensure_ascii=False)
    return "\n".join([
        "---",
        f"snapshot_type: {tab_key}",
        f'title: "{title}"',
        'subject_id: ""',
        f'version: "{version}"',
        "status: draft",
        'owner: ""',
        f'updated_at: "{_now().isoformat()}"',
        "confidence: 0.6",
        f"source_materials: {source_materials}",
        "missing_items: []",
        "conflicts: []",
        "---",
        "",
    ])


def _section(title: str, subheadings: list[str], bullets: list[str] | None = None) -> str:
    lines = [f"## {title}", ""]
    for index, heading in enumerate(subheadings):
        lines.extend([f"### {heading}", ""])
        if bullets and index == 0:
            lines.extend([f"- {item}" for item in bullets])
            lines.append("")
    return "\n".join(lines)


def build_tab_markdown(
    tab_key: str,
    version: str,
    source_refs: list[dict[str, str]],
    source_summary: str = "",
) -> str:
    spec = TAB_SUBHEADINGS[tab_key]
    title = f"{TAB_TITLES[tab_key]}治理快照"
    source_titles = [item["title"] for item in source_refs]
    facts = [source_summary or "资料已接入，关键治理事实等待进一步抽取与人工确认。"]
    governance = ["所有权限候选默认仅作为治理中间产物，不直接写入正式权限引擎。"]
    analysis = ["涉及权限、审批、资源归属或跨部门共享的事实需要保留证据与确认状态。"]
    actions = ["请补充资源 owner、审批 owner、原文 / 脱敏共享边界等高影响字段。"]
    evidence = [
        f"- source:{item['source_id']} {item['title']}"
        for item in source_refs
    ] or ["- 暂无来源资料。"]
    return "\n".join([
        _frontmatter(tab_key, title, version, source_titles),
        _section("事实区", spec["facts"], facts),
        _section("治理语义区", spec["governance"], governance),
        _section("分析区", spec["analysis"], analysis),
        _section("行动区", spec["actions"], actions),
        "## 证据",
        "",
        *evidence,
        "",
        "## 变更摘要",
        "",
        "- 首次生成治理快照 Markdown。",
        "",
    ])


def classify_sources(db: Session, source_ids: list[int], pasted_materials: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    categories = {
        "organization_structure": [],
        "department_responsibility": [],
        "role_definition": [],
        "person_roster": [],
        "okr": [],
        "process_sop": [],
        "permission_policy": [],
        "resource_inventory": [],
        "meeting_or_discussion": [],
        "ambiguous": [],
    }
    keywords = {
        "organization_structure": ("组织", "架构", "汇报", "部门"),
        "department_responsibility": ("职责", "使命", "边界"),
        "role_definition": ("岗位", "角色", "能力"),
        "person_roster": ("人员", "姓名", "花名册"),
        "okr": ("OKR", "目标", "KR", "指标"),
        "process_sop": ("流程", "SOP", "步骤"),
        "permission_policy": ("权限", "审批", "可见", "授权"),
        "resource_inventory": ("资源", "文档", "系统", "数据"),
        "meeting_or_discussion": ("会议", "纪要", "讨论"),
    }
    materials: list[dict[str, Any]] = []
    for source_id in source_ids:
        source = db.get(OrgMemorySource, source_id)
        if source:
            raw_text = " ".join(str(record) for record in (source.raw_records_json or [])[:5])
            materials.append({
                "source_type": source.source_type,
                "source_id": source.id,
                "title": source.title,
                "text": f"{source.title} {source.owner_name or ''} {raw_text}",
            })
    for index, text in enumerate(pasted_materials or []):
        materials.append({"source_type": "pasted_material", "source_id": f"pasted-{index + 1}", "title": f"粘贴资料 {index + 1}", "text": text})

    for material in materials:
        matched = False
        for category, words in keywords.items():
            if any(word in material["text"] for word in words):
                categories[category].append({k: material[k] for k in ("source_type", "source_id", "title")})
                matched = True
        if not matched:
            categories["ambiguous"].append({k: material[k] for k in ("source_type", "source_id", "title")})
    return categories


def snapshot_to_detail(snapshot: OrgGovernanceSnapshot) -> dict[str, Any]:
    tabs = {tab.tab_key: tab for tab in snapshot.tabs or []}
    sync_status = _empty_sync_status()
    failed_sections: list[dict[str, Any]] = []
    parser_warnings: list[str] = []
    for tab in tabs.values():
        status = tab.sync_status_json or {}
        failed_sections.extend(status.get("failed_sections") or [])
        parser_warnings.extend(tab.parser_warnings_json or status.get("parser_warnings") or [])
    sync_status.update({
        "markdown_saved": bool(tabs),
        "structured_updated": all((tab.sync_status_json or {}).get("structured_updated") for tab in tabs.values()) if tabs else False,
        "failed_sections": failed_sections,
        "parser_warnings": parser_warnings,
    })
    return {
        "id": snapshot.id,
        "workspace_id": snapshot.workspace_id,
        "workspace_type": snapshot.workspace_type,
        "app": snapshot.app,
        "title": snapshot.title,
        "version": snapshot.version,
        "status": snapshot.status,
        "scope": snapshot.scope,
        "source_snapshot_id": snapshot.source_snapshot_id,
        "base_snapshot_id": snapshot.base_snapshot_id,
        "confidence_score": snapshot.confidence_score or 0,
        "markdown_by_tab": _json(snapshot.markdown_by_tab_json, {}),
        "structured_by_tab": _json(snapshot.structured_by_tab_json, {}),
        "governance_outputs": _json(snapshot.governance_outputs_json, {}),
        "missing_items": _json(snapshot.missing_items_json, []),
        "conflicts": _json(snapshot.conflicts_json, []),
        "low_confidence_items": _json(snapshot.low_confidence_items_json, []),
        "separation_of_duty_risks": _json(snapshot.separation_of_duty_risks_json, []),
        "change_summary": _json(snapshot.change_summary_json, _empty_change_summary()),
        "sync_status": sync_status,
        "created_at": _iso(snapshot.created_at),
        "updated_at": _iso(snapshot.updated_at),
    }


def run_to_dto(run: OrgGovernanceSnapshotRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "event_type": run.event_type,
        "status": run.status,
        "workspace_id": run.workspace_id,
        "workspace_type": run.workspace_type,
        "app": run.app,
        "response_summary": run.response_summary_json,
        "error_message": run.error_message,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "completed_at": _iso(run.completed_at),
    }


def list_workspace_snapshots(db: Session, workspace_id: str | None = None, app: str | None = None) -> list[dict[str, Any]]:
    query = db.query(OrgGovernanceSnapshot)
    if workspace_id:
        query = query.filter(OrgGovernanceSnapshot.workspace_id == workspace_id)
    if app:
        query = query.filter(OrgGovernanceSnapshot.app == app)
    snapshots = query.order_by(OrgGovernanceSnapshot.created_at.desc(), OrgGovernanceSnapshot.id.desc()).all()
    return [
        {
            "id": item.id,
            "workspace_id": item.workspace_id,
            "workspace_type": item.workspace_type,
            "app": item.app,
            "title": item.title,
            "version": item.version,
            "status": item.status,
            "scope": item.scope,
            "confidence_score": item.confidence_score or 0,
            "missing_count": len(item.missing_items_json or []),
            "conflict_count": len(item.conflicts_json or []),
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }
        for item in snapshots
    ]


def _source_context(
    db: Session,
    source_ids: list[int],
    pasted_materials: list[str],
) -> tuple[list[dict[str, str]], str, list[OrgMemorySource]]:
    sources = [source for source_id in source_ids if (source := db.get(OrgMemorySource, source_id))]
    source_refs = [
        {"source_id": str(source.id), "title": source.title}
        for source in sources
    ]
    source_refs.extend([
        {"source_id": f"pasted-{index + 1}", "title": f"粘贴资料 {index + 1}"}
        for index, _ in enumerate(pasted_materials)
    ])
    summary_parts = []
    for source in sources:
        summary_parts.append(f"{source.title}（{source.source_type}）")
    for material in pasted_materials[:3]:
        summary_parts.append(material[:80])
    return source_refs, "；".join(summary_parts), sources


def _merge_change_summary(items: list[dict[str, list[dict[str, Any]]]]) -> dict[str, list[dict[str, Any]]]:
    merged = _empty_change_summary()
    for item in items:
        for key in merged:
            merged[key].extend(item.get(key) or [])
    return merged


def _upsert_tab(
    db: Session,
    snapshot: OrgGovernanceSnapshot,
    tab_key: str,
    markdown: str,
    structured: dict[str, Any],
    sync_status: dict[str, Any],
    parser_warnings: list[str],
    user: User | None,
) -> None:
    tab = (
        db.query(OrgGovernanceSnapshotTab)
        .filter(OrgGovernanceSnapshotTab.snapshot_id == snapshot.id, OrgGovernanceSnapshotTab.tab_key == tab_key)
        .first()
    )
    if not tab:
        tab = OrgGovernanceSnapshotTab(snapshot_id=snapshot.id, tab_key=tab_key)
        db.add(tab)
    tab.markdown = markdown
    tab.structured_json = structured
    tab.sync_status_json = sync_status
    tab.parser_warnings_json = parser_warnings
    tab.updated_by = user.id if user else None
    tab.updated_at = _now()


def _sync_markdown_payload(
    db: Session,
    snapshot: OrgGovernanceSnapshot,
    markdown_by_tab: dict[str, str],
    user: User | None,
) -> dict[str, Any]:
    previous_structured = snapshot.structured_by_tab_json or {}
    structured_by_tab = dict(previous_structured)
    change_summaries: list[dict[str, list[dict[str, Any]]]] = []
    failed_sections: list[dict[str, Any]] = []
    parser_warnings: list[str] = []
    all_ok = True

    for tab_key, markdown in markdown_by_tab.items():
        if tab_key not in TAB_KEYS:
            continue
        result = parse_tab_markdown(tab_key, markdown, previous_structured.get(tab_key))
        if result["ok"]:
            structured_by_tab[tab_key] = result["structured"]
        else:
            all_ok = False
        failed_sections.extend(result["failed_sections"])
        parser_warnings.extend(result["parser_warnings"])
        change_summaries.append(result["change_summary"])
        _upsert_tab(
            db,
            snapshot,
            tab_key,
            markdown,
            structured_by_tab.get(tab_key) or {},
            {
                "markdown_saved": True,
                "structured_updated": bool(result["ok"]),
                "failed_sections": result["failed_sections"],
                "parser_warnings": result["parser_warnings"],
            },
            result["parser_warnings"],
            user,
        )

    governance_projection = project_governance_outputs(structured_by_tab, snapshot.conflicts_json or [])
    governance_outputs = {
        "authority_map": governance_projection["authority_map"],
        "resource_access_matrix": governance_projection["resource_access_matrix"],
        "approval_route_candidates": governance_projection["approval_route_candidates"],
        "policy_hints": governance_projection["policy_hints"],
        "governance_questions": governance_projection["governance_questions"],
    }
    snapshot.markdown_by_tab_json = {**(snapshot.markdown_by_tab_json or {}), **markdown_by_tab}
    snapshot.structured_by_tab_json = structured_by_tab
    snapshot.governance_outputs_json = governance_outputs
    snapshot.separation_of_duty_risks_json = governance_projection["separation_of_duty_risks"]
    snapshot.change_summary_json = _merge_change_summary(change_summaries)
    snapshot.status = "reviewed" if all_ok and snapshot.status == "synced" else snapshot.status
    snapshot.updated_at = _now()
    return {
        "status": "synced" if all_ok else "partial_sync",
        "structured_by_tab": structured_by_tab,
        "governance_outputs": governance_outputs,
        "separation_of_duty_risks": governance_projection["separation_of_duty_risks"],
        "change_summary": snapshot.change_summary_json,
        "sync_status": {
            "markdown_saved": True,
            "structured_updated": all_ok,
            "failed_sections": failed_sections,
            "parser_warnings": parser_warnings,
        },
    }


def create_or_update_snapshot_from_event(db: Session, req: WorkspaceSnapshotEventRequest, user: User) -> dict[str, Any]:
    source_refs, source_summary, sources = _source_context(db, req.sources.source_ids, req.sources.pasted_materials)
    source_titles = [item["title"] for item in source_refs]
    version = f"gov-snapshot-{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    snapshot = None
    if req.snapshot.snapshot_id:
        snapshot = db.get(OrgGovernanceSnapshot, req.snapshot.snapshot_id)
        if not snapshot:
            raise HTTPException(404, "组织治理快照不存在")

    if not snapshot:
        snapshot = OrgGovernanceSnapshot(
            workspace_id=req.workspace.workspace_id,
            workspace_type=req.workspace.workspace_type,
            app=req.workspace.app,
            title=req.snapshot.title or "组织治理快照",
            version=version,
            status="draft",
            scope=req.snapshot.scope,
            source_snapshot_id=req.snapshot.source_snapshot_id,
            base_snapshot_id=req.snapshot.base_snapshot_id,
            confidence_score=0.6 if source_titles else 0.3,
            created_by=user.id,
            markdown_by_tab_json={},
            structured_by_tab_json={},
            governance_outputs_json={},
            missing_items_json=[],
            conflicts_json=[],
            low_confidence_items_json=[],
            separation_of_duty_risks_json=[],
            change_summary_json=_empty_change_summary(),
        )
        db.add(snapshot)
        db.flush()
        for source in sources:
            db.add(OrgGovernanceSnapshotSourceLink(
                snapshot_id=snapshot.id,
                source_type=source.source_type,
                source_id=str(source.id),
                source_uri=source.source_uri,
                title=source.title,
                evidence_refs_json=[{"label": source.title, "section": source.external_version or "latest", "excerpt": source.latest_parse_note or ""}],
            ))

    tabs = _requested_tabs(req.snapshot.scope, req.snapshot.active_tab)
    markdown_by_tab = dict(snapshot.markdown_by_tab_json or {})
    for tab_key in tabs:
        markdown_by_tab[tab_key] = (
            req.editor.existing_markdown_by_tab.get(tab_key)
            or markdown_by_tab.get(tab_key)
            or build_tab_markdown(tab_key, snapshot.version, source_refs, source_summary)
        )

    sync_payload = _sync_markdown_payload(db, snapshot, {key: markdown_by_tab[key] for key in tabs}, user)
    classification = classify_sources(db, req.sources.source_ids, req.sources.pasted_materials)
    missing_items = []
    if not source_titles and req.options.allow_missing_items:
        missing_items.append({
            "field": "sources",
            "label": "治理快照来源资料",
            "snapshot_type": req.snapshot.scope,
            "reason": "未提供可追溯来源，无法形成高置信治理事实。",
            "impact": "blocks_policy_generation",
            "suggested_input_type": "text",
        })
    snapshot.missing_items_json = missing_items
    snapshot.low_confidence_items_json = [] if source_titles else [{
        "label": "来源资料不足",
        "snapshot_type": req.snapshot.scope,
        "reason": "当前仅能生成模板，不能自动应用权限候选。",
        "evidence_refs": [],
        "confidence": 0.3,
        "suggested_confirmation": "追加组织、岗位、人员、OKR 或流程资料。",
    }]
    snapshot.status = "ready_for_review" if sync_payload["status"] == "synced" else "partial_sync"
    db.flush()
    detail = snapshot_to_detail(snapshot)
    detail.update({
        "status": snapshot.status,
        "active_tab": req.snapshot.active_tab if req.snapshot.scope == "active_tab" else None,
        "source_classification": classification,
        "form_questions": [],
        "sync_status": sync_payload["sync_status"],
    })
    return detail


def analyze_sources(db: Session, req: WorkspaceSnapshotEventRequest) -> dict[str, Any]:
    classification = classify_sources(db, req.sources.source_ids, req.sources.pasted_materials)
    has_sources = any(classification[key] for key in classification)
    return {
        "status": "ready_for_review" if has_sources else "needs_input",
        "active_tab": req.snapshot.active_tab,
        "source_classification": classification,
        "markdown_by_tab": {},
        "structured_by_tab": {},
        "governance_outputs": {
            "authority_map": [],
            "resource_access_matrix": [],
            "approval_route_candidates": [],
            "policy_hints": [],
            "governance_questions": [],
        },
        "form_questions": [] if has_sources else [{
            "field": "sources.source_ids",
            "label": "来源资料",
            "reason": "未检测到组织治理来源资料。",
            "input_type": "source_select",
            "options": [],
            "required": False,
            "impact": "blocks_policy_generation",
        }],
        "missing_items": [],
        "conflicts": [],
        "low_confidence_items": [],
        "separation_of_duty_risks": [],
        "change_summary": _empty_change_summary(),
        "sync_status": _empty_sync_status(),
    }


def sync_snapshot_from_markdown(
    db: Session,
    snapshot: OrgGovernanceSnapshot,
    markdown_by_tab: dict[str, str],
    user: User | None,
) -> dict[str, Any]:
    sync_payload = _sync_markdown_payload(db, snapshot, markdown_by_tab, user)
    snapshot.status = sync_payload["status"]
    db.flush()
    detail = snapshot_to_detail(snapshot)
    detail.update({
        "status": sync_payload["status"],
        "active_tab": next(iter(markdown_by_tab.keys()), None) if len(markdown_by_tab) == 1 else None,
        "form_questions": [],
        "sync_status": sync_payload["sync_status"],
    })
    return detail


def handle_snapshot_event(db: Session, req: WorkspaceSnapshotEventRequest, user: User) -> dict[str, Any]:
    run = OrgGovernanceSnapshotRun(
        run_id=uuid.uuid4().hex,
        event_type=req.event_type,
        workspace_id=req.workspace.workspace_id,
        workspace_type=req.workspace.workspace_type,
        app=req.workspace.app,
        user_id=user.id,
        status="running",
        request_payload_json=req.model_dump(mode="json"),
    )
    db.add(run)
    db.flush()
    try:
        if req.event_type == "snapshot.analyze_sources":
            result = analyze_sources(db, req)
        elif req.event_type in {"snapshot.generate", "snapshot.update", "snapshot.append_sources", "snapshot.resolve_questions"}:
            result = create_or_update_snapshot_from_event(db, req, user)
        elif req.event_type == "snapshot.sync_from_markdown":
            snapshot_id = req.snapshot.snapshot_id
            if not snapshot_id:
                raise HTTPException(400, "snapshot.sync_from_markdown 需要 snapshot.snapshot_id")
            snapshot = db.get(OrgGovernanceSnapshot, snapshot_id)
            if not snapshot:
                raise HTTPException(404, "组织治理快照不存在")
            markdown_by_tab = dict(req.editor.existing_markdown_by_tab)
            if req.editor.tab_key and req.editor.markdown is not None:
                markdown_by_tab[req.editor.tab_key] = req.editor.markdown
            if not markdown_by_tab:
                markdown_by_tab = snapshot.markdown_by_tab_json or {}
            result = sync_snapshot_from_markdown(db, snapshot, markdown_by_tab, user)
        else:
            raise HTTPException(400, f"不支持的事件类型：{req.event_type}")

        run.status = "completed"
        run.response_summary_json = {
            "status": result.get("status"),
            "snapshot_id": result.get("id"),
            "missing_count": len(result.get("missing_items") or []),
            "conflict_count": len(result.get("conflicts") or []),
        }
        run.completed_at = _now()
        result["run_id"] = run.run_id
        db.commit()
        return result
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = _now()
        db.commit()
        raise


def save_tab_markdown(
    db: Session,
    snapshot_id: int,
    tab_key: str,
    markdown: str,
    user: User,
) -> dict[str, Any]:
    if tab_key not in TAB_KEYS:
        raise HTTPException(400, "不支持的 Tab")
    snapshot = db.get(OrgGovernanceSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织治理快照不存在")
    result = sync_snapshot_from_markdown(db, snapshot, {tab_key: markdown}, user)
    db.commit()
    return result


def sync_all_markdown(db: Session, snapshot_id: int, user: User) -> dict[str, Any]:
    snapshot = db.get(OrgGovernanceSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织治理快照不存在")
    markdown_by_tab = snapshot.markdown_by_tab_json or {}
    if not markdown_by_tab:
        raise HTTPException(422, "当前快照没有可同步的 Markdown")
    result = sync_snapshot_from_markdown(db, snapshot, markdown_by_tab, user)
    db.commit()
    return result


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def workspace_snapshot_governance_version_to_dto(snapshot: OrgGovernanceSnapshot) -> dict[str, Any]:
    outputs = snapshot.governance_outputs_json or {}
    matrix = outputs.get("resource_access_matrix") or []
    policy_hints = outputs.get("policy_hints") or []
    typed_resources = [
        {
            "name": item.get("resource_name") or item.get("resource") or "组织治理快照",
            "type": str(item.get("resource_type") or "governance_snapshot"),
        }
        for item in matrix
        if isinstance(item, dict)
    ]
    knowledge_bases = _unique_strings([
        item["name"]
        for item in typed_resources
        if item["type"] not in {"data_table", "business_table", "table"}
    ])
    data_tables = _unique_strings([
        item["name"]
        for item in typed_resources
        if item["type"] in {"data_table", "business_table", "table"}
    ])
    rules = []
    for index, item in enumerate(matrix):
        if not isinstance(item, dict):
            continue
        resource_name = str(item.get("resource_name") or "组织治理快照")
        resource_type = str(item.get("resource_type") or "governance_snapshot")
        status = str(item.get("status") or "")
        approval_required = bool(item.get("approval_required"))
        actions = item.get("actions") or []
        conditions = item.get("conditions") or []
        evidence_level = item.get("evidence_level") or "derived"
        decision = "deny" if status == "blocked" else "require_approval" if approval_required else "allow"
        redaction_mode = item.get("redaction_mode") if item.get("redaction_mode") in {"raw", "masked", "summary", "pattern_only"} else "summary"
        access_scope = item.get("visibility_scope") if item.get("visibility_scope") in {"self", "manager_chain", "department", "cross_department", "company"} else "department"
        rules.append({
            "id": index + 1,
            "skill_id": 0,
            "skill_name": "组织治理候选策略",
            "knowledge_bases": [] if resource_type in {"data_table", "business_table", "table"} else [resource_name],
            "data_tables": [resource_name] if resource_type in {"data_table", "business_table", "table"} else [],
            "access_scope": access_scope,
            "redaction_mode": redaction_mode,
            "decision": decision,
            "rationale": f"由工作台治理中间产物派生；动作：{', '.join(str(action) for action in actions) or 'view'}；证据级别：{evidence_level}。",
            "required_domains": [str(condition) for condition in conditions],
        })
    affected_skills = [{"skill_id": 0, "skill_name": "组织治理候选策略"}] if rules or policy_hints else []
    return {
        "id": snapshot.id,
        "derived_from_snapshot_id": snapshot.id,
        "derived_from_snapshot_version": snapshot.version,
        "version": 1,
        "status": "draft",
        "summary": f"{snapshot.title} 的候选治理版本",
        "impact_summary": "此版本由工作台 Markdown 结构化结果与治理中间产物派生，当前仅作为候选策略用于联调验收，不直接写入正式权限引擎。",
        "knowledge_bases": knowledge_bases,
        "data_tables": data_tables,
        "affected_skills": affected_skills,
        "skill_access_rules": rules,
        "created_at": _iso(snapshot.created_at),
        "activated_at": None,
    }


def get_workspace_snapshot_governance_version(db: Session, snapshot_id: int) -> dict[str, Any]:
    snapshot = db.get(OrgGovernanceSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织治理快照不存在")
    return workspace_snapshot_governance_version_to_dto(snapshot)


def derive_governance_version(db: Session, snapshot_id: int) -> dict[str, Any]:
    snapshot = db.get(OrgGovernanceSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织治理快照不存在")
    result = workspace_snapshot_governance_version_to_dto(snapshot)
    result.update({
        "snapshot_id": snapshot.id,
        "status": "candidate_generated",
        "message": "已从组织治理快照派生候选治理输出；首版不直接写入正式权限引擎。",
        "governance_outputs": snapshot.governance_outputs_json or {},
        "separation_of_duty_risks": snapshot.separation_of_duty_risks_json or [],
    })
    return result
