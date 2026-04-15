from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.business import BusinessTable
from app.models.knowledge import KnowledgeEntry
from app.models.sandbox import SandboxTestReport, SandboxTestSession
from app.models.skill import Skill, SkillPreflightResult
from app.models.tool import ToolRegistry
from app.models.user import User


def _check_session_access(session: SandboxTestSession, user: User) -> None:
    if user.role == "super_admin":
        return
    if session.created_by != user.id:
        raise HTTPException(403, "无权访问该测试会话")


def confirm_knowledge_archive(
    db: Session,
    *,
    skill_id: int,
    user: User,
    confirmations: list[dict[str, Any]],
) -> dict[str, Any]:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    results: list[dict[str, Any]] = []
    all_entry_ids: list[int] = []
    created_entry_ids: list[int] = []

    for item in confirmations:
        filename = str(item.get("filename") or "").strip()
        if not filename:
            results.append({"filename": "", "ok": False, "reason": "文件名缺失"})
            continue

        source_files = skill.source_files or []
        file_info = next((file for file in source_files if file.get("filename") == filename), None)
        if not file_info:
            results.append({"filename": filename, "ok": False, "reason": "文件不存在"})
            continue

        file_path = str(file_info.get("path") or "")
        content = ""
        candidate_paths = [file_path] if file_path else []
        if file_path and not os.path.exists(file_path):
            basename = os.path.basename(file_path)
            try:
                from app.services.runtime_process_manager import _get_registry_workspace_root

                ws_root = _get_registry_workspace_root(user.id)
                if ws_root:
                    for subdir in ["project/output", "project", "skill_studio/data", "runtime/config/opencode/skills"]:
                        alt = os.path.join(ws_root, subdir, basename)
                        if os.path.exists(alt):
                            candidate_paths.insert(0, alt)
                            break
            except Exception:
                pass

        for candidate_path in candidate_paths:
            if candidate_path and os.path.exists(candidate_path):
                try:
                    with open(candidate_path, "r", encoding="utf-8") as handle:
                        content = handle.read()
                    if content:
                        break
                except Exception:
                    pass

        if not content:
            results.append({"filename": filename, "ok": False, "reason": "文件内容为空或无法读取"})
            continue

        title = str(item.get("display_title") or filename)
        category = str(item.get("target_category") or "general")
        target_board = str(item.get("target_board") or "") or None

        existing = db.query(KnowledgeEntry).filter(
            (KnowledgeEntry.title == title) | (KnowledgeEntry.source_file == filename)
        ).first()

        is_new = False
        if existing:
            existing.content = content
            existing.category = category
            existing.taxonomy_board = target_board or existing.taxonomy_board
            entry_id = existing.id
        else:
            from app.models.knowledge import KnowledgeStatus
            from app.models.user import get_system_user_id

            entry = KnowledgeEntry(
                title=title,
                content=content,
                category=category,
                status=KnowledgeStatus.APPROVED,
                created_by=get_system_user_id(db),
                source_type="skill_preflight",
                source_file=filename,
                taxonomy_board=target_board,
            )
            db.add(entry)
            db.flush()
            entry_id = entry.id
            is_new = True

        db.commit()
        all_entry_ids.append(entry_id)
        if is_new:
            created_entry_ids.append(entry_id)

        try:
            from app.services.vector_service import delete_knowledge_vectors, index_knowledge

            delete_knowledge_vectors(entry_id)
            index_knowledge(entry_id, content, user.id)
            results.append({"filename": filename, "ok": True, "knowledge_id": entry_id})
        except Exception as exc:
            results.append({
                "filename": filename,
                "ok": True,
                "knowledge_id": entry_id,
                "vector_warning": str(exc),
            })

    db.query(SkillPreflightResult).filter(
        SkillPreflightResult.skill_id == skill_id,
        SkillPreflightResult.gate_name == "knowledge",
    ).delete()
    db.commit()

    failed = [item for item in results if not item.get("ok")]
    return {
        "results": results,
        "knowledge_entry_ids": all_entry_ids,
        "created_entry_ids": created_entry_ids,
        "failed_count": len(failed),
        "failed_files": [item["filename"] for item in failed if item.get("filename")],
    }


def reindex_skill_knowledge(
    db: Session,
    *,
    skill_id: int,
    knowledge_ids: list[int],
    user: User,
) -> dict[str, Any]:
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    from app.services.vector_service import delete_knowledge_vectors, index_knowledge

    results: list[dict[str, Any]] = []
    for knowledge_id in knowledge_ids:
        entry = db.get(KnowledgeEntry, int(knowledge_id))
        if not entry:
            results.append({"knowledge_id": knowledge_id, "ok": False, "reason": "知识条目不存在"})
            continue
        if not (entry.content or "").strip():
            results.append({"knowledge_id": knowledge_id, "ok": False, "reason": "知识条目内容为空"})
            continue
        try:
            delete_knowledge_vectors(entry.id)
            index_knowledge(entry.id, entry.content, user.id, db=db)
            results.append({"knowledge_id": entry.id, "ok": True})
        except Exception as exc:
            results.append({"knowledge_id": entry.id, "ok": False, "reason": str(exc)})

    db.query(SkillPreflightResult).filter(
        SkillPreflightResult.skill_id == skill_id,
        SkillPreflightResult.gate_name == "knowledge",
    ).delete()
    db.commit()

    return {
        "results": results,
        "failed_count": len([item for item in results if not item.get("ok")]),
    }


def apply_sandbox_report_action(
    db: Session,
    *,
    report_id: int,
    action: str,
    payload: dict[str, Any] | None,
    user: User,
) -> dict[str, Any]:
    report = db.get(SandboxTestReport, report_id)
    if not report:
        raise HTTPException(404, "测试报告不存在")

    session = db.get(SandboxTestSession, report.session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    _check_session_access(session, user)
    if session.target_type != "skill":
        raise HTTPException(400, "仅支持 Skill 沙盒报告")

    skill = db.get(Skill, session.target_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    payload = payload or {}
    if action == "bind_sandbox_tools":
        from app.models.tool import SkillTool

        confirmed_tool_ids = {
            int(item.get("tool_id"))
            for item in (session.tool_review or [])
            if item.get("confirmed") and item.get("tool_id")
        }
        requested_tool_ids = {
            int(tool_id)
            for tool_id in (payload.get("tool_ids") or [])
            if str(tool_id).isdigit()
        }
        tool_ids = requested_tool_ids or confirmed_tool_ids
        bound = 0
        skipped = 0
        for tool_id in tool_ids:
            if not db.get(ToolRegistry, tool_id):
                skipped += 1
                continue
            existing = db.query(SkillTool).filter(
                SkillTool.skill_id == skill.id,
                SkillTool.tool_id == tool_id,
            ).first()
            if existing:
                skipped += 1
                continue
            db.add(SkillTool(skill_id=skill.id, tool_id=tool_id))
            bound += 1
        db.commit()
        return {"ok": True, "action": action, "bound": bound, "skipped": skipped, "tool_ids": sorted(tool_ids)}

    if action == "bind_knowledge_references":
        from app.models.skill_knowledge_ref import SkillKnowledgeReference

        knowledge_ids = {
            int(knowledge_id)
            for knowledge_id in (payload.get("knowledge_ids") or [])
            if str(knowledge_id).isdigit()
        }
        if not knowledge_ids:
            for slot in session.detected_slots or []:
                knowledge_id = slot.get("knowledge_entry_id")
                if knowledge_id:
                    knowledge_ids.add(int(knowledge_id))
        existing_version = (
            db.query(SkillKnowledgeReference.publish_version)
            .filter(SkillKnowledgeReference.skill_id == skill.id)
            .order_by(SkillKnowledgeReference.publish_version.desc())
            .first()
        )
        publish_version = (existing_version[0] + 1) if existing_version else 1
        bound = 0
        skipped = 0
        for knowledge_id in knowledge_ids:
            entry = db.get(KnowledgeEntry, knowledge_id)
            if not entry:
                skipped += 1
                continue
            db.add(SkillKnowledgeReference(
                skill_id=skill.id,
                knowledge_id=knowledge_id,
                snapshot_desensitization_level=getattr(entry, "desensitization_level", None),
                snapshot_data_type_hits=getattr(entry, "data_type_hits", []) or [],
                snapshot_document_type=getattr(entry, "document_type", None),
                snapshot_permission_domain=getattr(entry, "permission_domain", None),
                snapshot_mask_rules=[],
                mask_rule_source="sandbox_report",
                folder_id=getattr(entry, "folder_id", None),
                folder_path=getattr(entry, "folder_path", None),
                manager_scope_ok=True,
                publish_version=publish_version,
            ))
            bound += 1
        db.commit()
        return {"ok": True, "action": action, "bound": bound, "skipped": skipped, "knowledge_ids": sorted(knowledge_ids)}

    if action == "bind_permission_tables":
        from app.models.business import SkillDataQuery, SkillTableBinding

        requested_table_names = {
            str(table_name).strip()
            for table_name in (payload.get("table_names") or [])
            if str(table_name).strip()
        }
        confirmed_table_names = {
            str(snap.get("table_name")).strip()
            for snap in (session.permission_snapshot or [])
            if snap.get("confirmed") and snap.get("included_in_test") and snap.get("table_name")
        }
        table_names = requested_table_names or confirmed_table_names
        bound_queries = 0
        bound_bindings = 0
        skipped = 0
        quick_queries = list(skill.data_queries or [])

        for table_name in table_names:
            table = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
            if not table or table.publish_status != "published":
                skipped += 1
                continue

            existing_query = db.query(SkillDataQuery).filter(
                SkillDataQuery.skill_id == skill.id,
                SkillDataQuery.table_name == table_name,
            ).first()
            if not existing_query:
                db.add(SkillDataQuery(
                    skill_id=skill.id,
                    query_name=f"read_{table_name}",
                    query_type="read",
                    table_name=table_name,
                    description=table.display_name or table_name,
                ))
                quick_queries.append({
                    "query_name": f"read_{table_name}",
                    "query_type": "read",
                    "table_name": table_name,
                    "description": table.display_name or table_name,
                })
                bound_queries += 1

            existing_binding = db.query(SkillTableBinding).filter(
                SkillTableBinding.skill_id == skill.id,
                SkillTableBinding.table_id == table.id,
            ).first()
            if not existing_binding:
                db.add(SkillTableBinding(
                    skill_id=skill.id,
                    table_id=table.id,
                    view_id=None,
                    binding_type="runtime_read",
                    alias=table.display_name or table.table_name,
                    description="来自沙盒报告权限确认",
                    created_by=user.id,
                ))
                bound_bindings += 1

            if existing_query and existing_binding:
                skipped += 1

        skill.data_queries = quick_queries
        db.commit()
        return {
            "ok": True,
            "action": action,
            "bound_queries": bound_queries,
            "bound_bindings": bound_bindings,
            "skipped": skipped,
            "table_names": sorted(table_names),
        }

    raise HTTPException(400, f"不支持的动作：{action}")
