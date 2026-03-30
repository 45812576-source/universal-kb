"""交互式沙盒测试 API — 多步骤向导，证据驱动，反幻觉。

流程：start → input_slot_review → tool_review → permission_review → run → report → submit-approval

LLM 允许/禁止白名单：
  允许：读取测试人声明做一致性核对、识别 prompt 反幻觉限制、对真实执行输出做质量评价
  禁止：生成测试输入/mock 参数/补全字段/编造知识/数据/推断缺失输入
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import time
from itertools import product
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User, Role
from app.models.skill import Skill, SkillVersion
from app.models.tool import ToolRegistry
from app.models.business import BusinessTable, DataOwnership, SkillDataQuery
from app.models.knowledge import KnowledgeEntry
from app.models.permission import (
    DataScopePolicy,
    GlobalDataMask,
    RoleMaskOverride,
    SkillMaskOverride,
    RoleOutputMask,
)
from app.models.sandbox import (
    SandboxTestSession,
    SandboxTestEvidence,
    SandboxTestCase,
    SandboxTestReport,
    SessionStatus,
    SessionStep,
    SlotSourceKind,
    EvidenceType,
    CaseVerdict,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandbox/interactive", tags=["sandbox-interactive"])


# ─── Pydantic Schemas ────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    target_type: str  # "skill" | "tool"
    target_id: int
    target_version: Optional[int] = None


class SlotConfirmation(BaseModel):
    slot_key: str
    chosen_source: str  # chat_text / knowledge / data_table / system_runtime
    chat_example: Optional[str] = None
    knowledge_entry_id: Optional[int] = None
    rag_query: Optional[str] = None
    rag_expected_ids: Optional[List[int]] = None
    table_name: Optional[str] = None
    field_name: Optional[str] = None


class InputSlotsRequest(BaseModel):
    slots: List[SlotConfirmation]


class ToolProvenanceItem(BaseModel):
    tool_id: int
    confirmed: bool
    input_provenance: List[dict]  # [{field_name, source_kind, source_ref}]


class ToolReviewRequest(BaseModel):
    tools: List[ToolProvenanceItem]


class PermissionConfirmItem(BaseModel):
    table_name: str
    confirmed: bool
    included_in_test: bool


class PermissionReviewRequest(BaseModel):
    tables: List[PermissionConfirmItem]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_input_slots(skill: Skill, version: SkillVersion, db: Session) -> list[dict]:
    """规则优先提取输入槽位，不由 LLM 判断。"""
    slots = []
    seen_keys = set()

    # 1. SkillVersion.required_inputs
    # 结构化业务事实字段默认禁止 chat_text，除非显式 freetext=true
    for ri in (version.required_inputs or []):
        key = ri.get("key", ri.get("label", ""))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        is_freetext = ri.get("freetext", False)
        if is_freetext:
            allowed = ["chat_text", "knowledge", "data_table", "system_runtime"]
            structured = False
        else:
            # 默认视为结构化业务事实，禁止 chat_text
            allowed = ["knowledge", "data_table", "system_runtime"]
            structured = True
        slots.append({
            "slot_key": key,
            "label": ri.get("label", key),
            "structured": structured,
            "required": True,
            "allowed_sources": allowed,
            "chosen_source": None,
            "evidence_status": "pending",
            "evidence_ref": None,
            "chat_example": None,
            "knowledge_entry_id": None,
            "table_name": None,
            "field_name": None,
        })

    # 2. Skill.data_queries
    for dq in (skill.data_queries or []):
        key = dq.get("query_name", dq.get("table_name", ""))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        slots.append({
            "slot_key": key,
            "label": dq.get("description", key),
            "structured": True,
            "required": True,
            "allowed_sources": ["data_table"],
            "chosen_source": "data_table",
            "evidence_status": "pending",
            "evidence_ref": None,
            "chat_example": None,
            "knowledge_entry_id": None,
            "table_name": dq.get("table_name"),
            "field_name": None,
        })

    # 3. bound_tools manifest data_sources
    for tool in list(skill.bound_tools):
        config = tool.config or {}
        manifest = config.get("manifest", {})
        for ds in manifest.get("data_sources", []):
            key = ds.get("key", ds.get("type", ""))
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            ds_type = ds.get("type", "")
            if ds_type == "registered_table":
                allowed = ["data_table"]
                chosen = "data_table"
            elif ds_type == "uploaded_file":
                allowed = ["system_runtime"]
                chosen = "system_runtime"
            elif ds_type == "chat_context":
                allowed = ["chat_text"]
                chosen = "chat_text"
            else:
                allowed = ["chat_text", "knowledge", "data_table", "system_runtime"]
                chosen = None
            slots.append({
                "slot_key": key,
                "label": ds.get("description", key),
                "structured": ds_type in ("registered_table",),
                "required": ds.get("required", True),
                "allowed_sources": allowed,
                "chosen_source": chosen,
                "evidence_status": "pending",
                "evidence_ref": None,
                "chat_example": None,
                "knowledge_entry_id": None,
                "table_name": ds.get("key") if ds_type == "registered_table" else None,
                "field_name": None,
            })

    # 4. knowledge_tags → 知识库依赖
    if skill.knowledge_tags:
        for tag in skill.knowledge_tags:
            key = f"knowledge_tag:{tag}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            slots.append({
                "slot_key": key,
                "label": f"知识标签: {tag}",
                "structured": False,
                "required": False,
                "allowed_sources": ["knowledge"],
                "chosen_source": "knowledge",
                "evidence_status": "pending",
                "evidence_ref": None,
                "chat_example": None,
                "knowledge_entry_id": None,
                "table_name": None,
                "field_name": None,
            })

    # 5. source_files category=knowledge-base
    for sf in (skill.source_files or []):
        if sf.get("category") == "knowledge-base":
            key = f"kb_file:{sf['filename']}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            slots.append({
                "slot_key": key,
                "label": f"知识库文件: {sf['filename']}",
                "structured": False,
                "required": True,
                "allowed_sources": ["knowledge"],
                "chosen_source": "knowledge",
                "evidence_status": "pending",
                "evidence_ref": None,
                "chat_example": None,
                "knowledge_entry_id": None,
                "table_name": None,
                "field_name": None,
            })

    return slots


def _detect_tools(skill: Skill) -> list[dict]:
    """自动发现绑定 tool，返回结构化列表。

    对每个字段基于 manifest/data_sources 语义预约束 allowed_sources：
    - registered_table → 只允许 data_table
    - uploaded_file → 只允许 system_runtime
    - chat_context → 允许 chat_text（前提是非结构化）
    - 其他/无 manifest → 默认禁止 chat_text（结构化业务事实字符串同样禁止）
    """
    tools = []
    for t in list(skill.bound_tools):
        config = t.config or {}
        manifest = config.get("manifest", {})
        schema = t.input_schema or {}
        required_fields = schema.get("required", [])
        properties = schema.get("properties", {})

        # 建立 manifest data_sources key→ds 映射
        ds_map = {}
        for ds in manifest.get("data_sources", []):
            ds_key = ds.get("key", "")
            if ds_key:
                ds_map[ds_key] = ds

        input_provenance = []
        for field_name in required_fields:
            field_def = properties.get(field_name, {})
            field_type = field_def.get("type", "string")
            ds = ds_map.get(field_name)

            # 基于 manifest data_source 类型确定允许来源和默认来源
            source_kind = None
            if ds:
                ds_type = ds.get("type", "")
                if ds_type == "registered_table":
                    allowed_sources = ["data_table"]
                    source_kind = "data_table"
                elif ds_type == "uploaded_file":
                    allowed_sources = ["system_runtime"]
                    source_kind = "system_runtime"
                elif ds_type == "chat_context":
                    allowed_sources = ["chat_text"]
                    source_kind = "chat_text"
                else:
                    allowed_sources = ["knowledge", "data_table", "system_runtime"]
            else:
                # 无 manifest 声明：非基本类型或 string 都默认禁止 chat_text
                is_primitive_freetext = field_type == "string" and field_def.get("freetext", False)
                if is_primitive_freetext:
                    allowed_sources = ["chat_text", "knowledge", "data_table", "system_runtime"]
                elif field_type in ("integer", "number", "boolean", "array", "object"):
                    allowed_sources = ["data_table", "system_runtime"]
                else:
                    # string 字段默认视为结构化业务事实，禁止 chat_text
                    allowed_sources = ["knowledge", "data_table", "system_runtime"]

            input_provenance.append({
                "field_name": field_name,
                "field_type": field_type,
                "description": field_def.get("description", ""),
                "source_kind": source_kind,
                "allowed_sources": allowed_sources,
                "source_ref": None,
                "resolved_value_preview": None,
                "verified": False,
            })

        # 计算 tool 内容哈希（用于审批时校验 tool 未被篡改）
        tool_content = json.dumps({"schema": schema, "config": config}, sort_keys=True, default=str)
        tool_hash = hashlib.sha256(tool_content.encode()).hexdigest()[:16]

        tools.append({
            "tool_id": t.id,
            "tool_name": t.display_name or t.name,
            "description": t.description or "",
            "input_schema": schema,
            "manifest_data_sources": manifest.get("data_sources", []),
            "preconditions": manifest.get("preconditions", []),
            "confirmed": False,
            "input_provenance": input_provenance,
            "content_hash": tool_hash,
        })
    return tools


def _build_permission_snapshot(skill: Skill, db: Session) -> list[dict]:
    """自动发现相关数据资产，构建权限快照。"""
    table_names = set()

    # 从 data_queries 收集
    for dq in (skill.data_queries or []):
        if dq.get("table_name"):
            table_names.add(dq["table_name"])

    # 从 bound_tools manifest 收集
    for tool in list(skill.bound_tools):
        config = tool.config or {}
        manifest = config.get("manifest", {})
        for ds in manifest.get("data_sources", []):
            if ds.get("type") == "registered_table" and ds.get("key"):
                table_names.add(ds["key"])

    # 从 SkillDataQuery 收集
    sdqs = db.query(SkillDataQuery).filter(SkillDataQuery.skill_id == skill.id).all()
    for sdq in sdqs:
        if sdq.table_name:
            table_names.add(sdq.table_name)

    snapshots = []
    for tn in sorted(table_names):
        bt = db.query(BusinessTable).filter(BusinessTable.table_name == tn).first()
        if not bt:
            snapshots.append({
                "table_name": tn,
                "display_name": tn,
                "row_visibility": "unknown",
                "ownership_rules": {},
                "field_masks": [],
                "groupable_fields": [],
                "confirmed": False,
                "included_in_test": True,
                "warning": f"数据表 '{tn}' 未在系统中注册",
            })
            continue

        # DataOwnership
        ownership = db.query(DataOwnership).filter(DataOwnership.table_name == tn).first()
        ownership_rules = {}
        if ownership:
            ownership_rules = {
                "owner_field": ownership.owner_field,
                "department_field": ownership.department_field,
                "visibility_level": ownership.visibility_level.value if ownership.visibility_level else "detail",
            }

        # DataScopePolicy — 收集所有目标类型的策略
        scope_policies = db.query(DataScopePolicy).filter(
            DataScopePolicy.business_table_id == bt.id
        ).all()
        row_visibility_set = set()
        scope_policy_details = []
        for sp in scope_policies:
            vis = sp.visibility_level.value if sp.visibility_level else "own"
            row_visibility_set.add(vis)
            detail = {
                "target_type": sp.target_type.value if sp.target_type else None,
                "visibility": vis,
                "output_mask": sp.output_mask or [],
            }
            if sp.target_type and sp.target_type.value == "position" and sp.target_position_id:
                from app.models.permission import Position
                pos = db.get(Position, sp.target_position_id)
                detail["target_position_id"] = sp.target_position_id
                detail["target_position_name"] = pos.name if pos else None
            elif sp.target_type and sp.target_type.value == "role" and sp.target_role:
                detail["target_role"] = sp.target_role
            scope_policy_details.append(detail)

        # 字段遮罩：GlobalDataMask + RoleMaskOverride + SkillMaskOverride
        field_masks = []

        global_masks = db.query(GlobalDataMask).all()
        for gm in global_masks:
            field_masks.append({
                "field_name": gm.field_name,
                "mask_action": gm.mask_action.value,
                "mask_params": gm.mask_params or {},
                "level": "global",
            })

        # RoleMaskOverride — 按岗位覆盖全局规则
        role_masks = db.query(RoleMaskOverride).all()
        for rm in role_masks:
            from app.models.permission import Position
            pos = db.get(Position, rm.position_id)
            field_masks.append({
                "field_name": rm.field_name,
                "mask_action": rm.mask_action.value,
                "mask_params": rm.mask_params or {},
                "level": "role",
                "position_id": rm.position_id,
                "position_name": pos.name if pos else None,
            })

        # SkillMaskOverride — Skill 级覆盖
        skill_masks = db.query(SkillMaskOverride).filter(
            SkillMaskOverride.skill_id == skill.id
        ).all()
        for sm in skill_masks:
            from app.models.permission import Position
            pos = db.get(Position, sm.position_id) if sm.position_id else None
            field_masks.append({
                "field_name": sm.field_name,
                "mask_action": sm.mask_action.value,
                "mask_params": sm.mask_params or {},
                "level": "skill",
                "position_id": sm.position_id,
                "position_name": pos.name if pos else None,
            })

        # RoleOutputMask — 角色×数据域×字段输出遮罩
        role_output_masks = []
        # 需要知道表关联的 data_domain
        bt_domain_ids = set()
        for sp in scope_policies:
            if sp.data_domain_id:
                bt_domain_ids.add(sp.data_domain_id)
        # 也从 GlobalDataMask 获取关联 domain
        for gm in global_masks:
            if gm.data_domain_id:
                bt_domain_ids.add(gm.data_domain_id)

        if bt_domain_ids:
            rom_query = db.query(RoleOutputMask).filter(
                RoleOutputMask.data_domain_id.in_(list(bt_domain_ids))
            ).all()
            for rom in rom_query:
                from app.models.permission import Position
                pos = db.get(Position, rom.position_id)
                role_output_masks.append({
                    "position_id": rom.position_id,
                    "position_name": pos.name if pos else None,
                    "data_domain_id": rom.data_domain_id,
                    "field_name": rom.field_name,
                    "mask_action": rom.mask_action.value,
                })

        # DDL 提取可分组字段
        groupable_fields = []
        if bt.ddl_sql:
            # 简单提取列名
            import re
            cols = re.findall(r"`(\w+)`\s+(?:VARCHAR|ENUM|INT|DATE|TINYINT)", bt.ddl_sql, re.IGNORECASE)
            groupable_fields = cols[:20]

        snapshots.append({
            "table_name": tn,
            "display_name": bt.display_name,
            "row_visibility": ",".join(sorted(row_visibility_set)) if row_visibility_set else "all",
            "ownership_rules": ownership_rules,
            "scope_policies": scope_policy_details,
            "field_masks": field_masks,
            "role_output_masks": role_output_masks,
            "groupable_fields": groupable_fields,
            "confirmed": False,
            "included_in_test": True,
        })

    return snapshots


def _generate_semantic_matrix(session: SandboxTestSession) -> list[dict]:
    """基于权限语义维度生成最小覆盖矩阵。

    穷尽权限语义，不穷举业务值。
    维度：行级可见性 × 字段输出语义 × 分组语义 × tool 前置条件语义
    """
    # 收集权限快照中的语义维度
    row_visibilities = set()
    field_semantics = set()
    group_semantics = {"none"}
    tool_preconditions = set()

    for snap in (session.permission_snapshot or []):
        if not snap.get("included_in_test"):
            continue
        # 行可见性
        for rv in snap.get("row_visibility", "all").split(","):
            rv = rv.strip()
            if rv:
                row_visibilities.add(rv)
        # 字段遮罩语义
        for fm in snap.get("field_masks", []):
            action = fm.get("mask_action", "keep")
            field_semantics.add(action)
        # 分组语义
        gf = snap.get("groupable_fields", [])
        if gf:
            group_semantics.add("single_field")
            if len(gf) > 1:
                group_semantics.add("multi_field")

    # Tool 前置条件语义
    for tr in (session.tool_review or []):
        if tr.get("confirmed"):
            tool_preconditions.add("callable")
        else:
            tool_preconditions.add("precondition_failed")

    if not row_visibilities:
        row_visibilities = {"all"}
    if not field_semantics:
        field_semantics = {"keep"}
    if not tool_preconditions:
        tool_preconditions = {"callable"}

    # 生成语义组合
    combos = list(product(
        sorted(row_visibilities),
        sorted(field_semantics),
        sorted(group_semantics),
        sorted(tool_preconditions),
    ))

    theoretical = len(combos)

    # 语义去重：如果行可见性=blocked，字段和分组不影响
    semantic_combos = []
    seen = set()
    for rv, fs, gs, tp in combos:
        if rv == "blocked":
            key = (rv, "any", "any", tp)
        else:
            key = (rv, fs, gs, tp)
        if key not in seen:
            seen.add(key)
            semantic_combos.append({
                "row_visibility": rv,
                "field_output_semantic": fs if rv != "blocked" else "N/A",
                "group_semantic": gs if rv != "blocked" else "N/A",
                "tool_precondition": tp,
            })

    return semantic_combos, theoretical


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.post("/start")
async def start_session(
    req: StartRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建测试会话，锁定目标对象和版本，提取输入槽位。"""
    if req.target_type == "skill":
        skill = db.get(Skill, req.target_id)
        if not skill:
            raise HTTPException(404, "Skill 不存在")
        # 获取版本
        version_q = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id)
        if req.target_version:
            version = version_q.filter(SkillVersion.version == req.target_version).first()
        else:
            version = version_q.order_by(SkillVersion.version.desc()).first()
        if not version:
            raise HTTPException(400, "无可用版本")

        detected_slots = _extract_input_slots(skill, version, db)
        tool_info = _detect_tools(skill)

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=version.version,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.DRAFT,
            current_step=SessionStep.INPUT_SLOT_REVIEW,
            detected_slots=detected_slots,
            tool_review=tool_info,
        )
    elif req.target_type == "tool":
        tool = db.get(ToolRegistry, req.target_id)
        if not tool:
            raise HTTPException(404, "工具不存在")
        session = SandboxTestSession(
            target_type="tool",
            target_id=tool.id,
            target_name=tool.display_name or tool.name,
            tester_id=user.id,
            status=SessionStatus.DRAFT,
            current_step=SessionStep.TOOL_REVIEW,
            detected_slots=[],
            tool_review=[{
                "tool_id": tool.id,
                "tool_name": tool.display_name or tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema or {},
                "manifest_data_sources": (tool.config or {}).get("manifest", {}).get("data_sources", []),
                "preconditions": (tool.config or {}).get("manifest", {}).get("preconditions", []),
                "confirmed": False,
                "input_provenance": [],
            }],
        )
    else:
        raise HTTPException(400, "target_type 必须是 skill 或 tool")

    db.add(session)
    db.commit()
    db.refresh(session)

    return {
        "session_id": session.id,
        "current_step": session.current_step.value,
        "status": session.status.value,
        "target_type": session.target_type,
        "target_id": session.target_id,
        "target_version": session.target_version,
        "target_name": session.target_name,
        "detected_slots": session.detected_slots,
        "tool_review": session.tool_review,
    }


@router.get("/{session_id}")
async def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取当前会话状态。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    return _serialize_session(session)


@router.post("/{session_id}/input-slots")
async def submit_input_slots(
    session_id: int,
    req: InputSlotsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交 Q1 输入槽位来源确认。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if session.current_step != SessionStep.INPUT_SLOT_REVIEW:
        raise HTTPException(400, f"当前步骤不是 input_slot_review，而是 {session.current_step.value}")

    slots = session.detected_slots or []
    slot_map = {s["slot_key"]: s for s in slots}
    blocked_reasons = []

    for conf in req.slots:
        slot = slot_map.get(conf.slot_key)
        if not slot:
            continue

        # 统一使用 allowed_sources 做来源校验（allowed_sources 已在 _extract_input_slots 中
        # 基于字段语义预计算，结构化字段的 allowed_sources 不包含 chat_text）
        if conf.chosen_source not in slot["allowed_sources"]:
            reason = (
                f"槽位 '{slot['label']}' 不允许来源 '{conf.chosen_source}'，"
                f"允许来源：{slot['allowed_sources']}"
            )
            if slot["structured"] and conf.chosen_source == "chat_text":
                reason = (
                    f"槽位 '{slot['label']}' 是结构化业务事实，不允许使用 chat_text 提供，"
                    f"必须从知识库或数据表取得（允许来源：{slot['allowed_sources']}）"
                )
            blocked_reasons.append(reason)
            slot["evidence_status"] = "failed"
            slot["chosen_source"] = conf.chosen_source
            continue

        slot["chosen_source"] = conf.chosen_source
        slot["chat_example"] = conf.chat_example
        slot["knowledge_entry_id"] = conf.knowledge_entry_id
        slot["table_name"] = conf.table_name
        slot["field_name"] = conf.field_name

        # 验证知识来源
        if conf.chosen_source == "knowledge":
            if not conf.knowledge_entry_id:
                blocked_reasons.append(f"槽位 '{slot['label']}' 选择知识库来源但未绑定具体知识对象")
                slot["evidence_status"] = "failed"
                continue

            ke = db.get(KnowledgeEntry, conf.knowledge_entry_id)
            if not ke:
                blocked_reasons.append(f"槽位 '{slot['label']}' 绑定的知识对象 #{conf.knowledge_entry_id} 不存在")
                slot["evidence_status"] = "failed"
                continue

            # RAG 检索验证
            if conf.rag_query:
                try:
                    from app.services.vector_service import search_knowledge
                    hits = search_knowledge(
                        conf.rag_query,
                        top_k=10,
                        knowledge_id_filter=conf.rag_expected_ids or [conf.knowledge_entry_id],
                    )
                    hit_ids = [h.get("knowledge_id") for h in hits]
                    if conf.knowledge_entry_id not in hit_ids:
                        blocked_reasons.append(
                            f"槽位 '{slot['label']}' 的 RAG 检索未命中绑定知识对象 #{conf.knowledge_entry_id}，"
                            "无法测试"
                        )
                        slot["evidence_status"] = "failed"
                        db.add(SandboxTestEvidence(
                            session_id=session_id,
                            evidence_type=EvidenceType.RAG_SAMPLE,
                            step="input_slot_review",
                            slot_key=conf.slot_key,
                            source_kind=SlotSourceKind.KNOWLEDGE,
                            knowledge_entry_id=conf.knowledge_entry_id,
                            rag_query=conf.rag_query,
                            rag_expected_ids=conf.rag_expected_ids or [conf.knowledge_entry_id],
                            rag_actual_hits=[{"id": h.get("knowledge_id"), "score": h.get("score"), "chunk_preview": h.get("text", "")[:100]} for h in hits[:5]],
                            rag_hit=False,
                        ))
                        continue
                    else:
                        slot["evidence_status"] = "verified"
                        slot["evidence_ref"] = f"knowledge_entry:{conf.knowledge_entry_id}"
                        db.add(SandboxTestEvidence(
                            session_id=session_id,
                            evidence_type=EvidenceType.RAG_SAMPLE,
                            step="input_slot_review",
                            slot_key=conf.slot_key,
                            source_kind=SlotSourceKind.KNOWLEDGE,
                            knowledge_entry_id=conf.knowledge_entry_id,
                            rag_query=conf.rag_query,
                            rag_expected_ids=conf.rag_expected_ids or [conf.knowledge_entry_id],
                            rag_actual_hits=[{"id": h.get("knowledge_id"), "score": h.get("score"), "chunk_preview": h.get("text", "")[:100]} for h in hits[:5]],
                            rag_hit=True,
                        ))
                except Exception as e:
                    blocked_reasons.append(f"槽位 '{slot['label']}' RAG 检索执行失败: {e}")
                    slot["evidence_status"] = "failed"
                    continue
            else:
                slot["evidence_status"] = "verified"
                slot["evidence_ref"] = f"knowledge_entry:{conf.knowledge_entry_id}"

            # 记录知识绑定证据
            db.add(SandboxTestEvidence(
                session_id=session_id,
                evidence_type=EvidenceType.KNOWLEDGE_BINDING,
                step="input_slot_review",
                slot_key=conf.slot_key,
                source_kind=SlotSourceKind.KNOWLEDGE,
                source_ref=f"knowledge_entry:{conf.knowledge_entry_id}",
                knowledge_entry_id=conf.knowledge_entry_id,
            ))

        elif conf.chosen_source == "data_table":
            if not conf.table_name:
                blocked_reasons.append(f"槽位 '{slot['label']}' 选择数据表来源但未指定表名")
                slot["evidence_status"] = "failed"
                continue
            bt = db.query(BusinessTable).filter(BusinessTable.table_name == conf.table_name).first()
            if not bt:
                blocked_reasons.append(f"槽位 '{slot['label']}' 引用的数据表 '{conf.table_name}' 未注册")
                slot["evidence_status"] = "failed"
                continue
            slot["evidence_status"] = "verified"
            slot["evidence_ref"] = f"table:{conf.table_name}.{conf.field_name or '*'}"
            db.add(SandboxTestEvidence(
                session_id=session_id,
                evidence_type=EvidenceType.INPUT_SLOT,
                step="input_slot_review",
                slot_key=conf.slot_key,
                source_kind=SlotSourceKind.DATA_TABLE,
                source_ref=f"{conf.table_name}.{conf.field_name or '*'}",
                table_name=conf.table_name,
                field_name=conf.field_name,
                verified=True,
            ))

        elif conf.chosen_source == "chat_text":
            if not conf.chat_example:
                blocked_reasons.append(
                    f"槽位 '{slot['label']}' 选择 chat 提供但未给出示例文本（禁止 LLM 自动生成，必须由测试人手写）"
                )
                slot["evidence_status"] = "failed"
                continue
            slot["evidence_status"] = "verified"
            slot["evidence_ref"] = f"chat_example:{conf.chat_example[:50]}"
            db.add(SandboxTestEvidence(
                session_id=session_id,
                evidence_type=EvidenceType.INPUT_SLOT,
                step="input_slot_review",
                slot_key=conf.slot_key,
                source_kind=SlotSourceKind.CHAT_TEXT,
                source_ref=conf.chat_example,
                verified=True,
            ))

        elif conf.chosen_source == "system_runtime":
            slot["evidence_status"] = "verified"
            slot["evidence_ref"] = "system_runtime"
            db.add(SandboxTestEvidence(
                session_id=session_id,
                evidence_type=EvidenceType.INPUT_SLOT,
                step="input_slot_review",
                slot_key=conf.slot_key,
                source_kind=SlotSourceKind.SYSTEM_RUNTIME,
                source_ref="system_runtime",
                verified=True,
            ))

    # 检查是否有 required slot 未确认
    for slot in slots:
        if slot["required"] and slot["evidence_status"] == "pending":
            # 未在提交列表中
            conf_keys = {c.slot_key for c in req.slots}
            if slot["slot_key"] not in conf_keys:
                blocked_reasons.append(f"必填槽位 '{slot['label']}' 未提交来源确认")
                slot["evidence_status"] = "failed"

    session.detected_slots = slots

    if blocked_reasons:
        session.status = SessionStatus.CANNOT_TEST
        session.blocked_reason = "\n".join(blocked_reasons)
    else:
        # 进入下一步
        if session.tool_review and len(session.tool_review) > 0:
            session.current_step = SessionStep.TOOL_REVIEW
        else:
            session.current_step = SessionStep.PERMISSION_REVIEW
        session.status = SessionStatus.DRAFT

    db.commit()
    return _serialize_session(session)


@router.post("/{session_id}/tool-review")
async def submit_tool_review(
    session_id: int,
    req: ToolReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交 Q2 Tool 确认与 input provenance。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if session.current_step != SessionStep.TOOL_REVIEW:
        raise HTTPException(400, f"当前步骤不是 tool_review，而是 {session.current_step.value}")

    tools = session.tool_review or []
    tool_map = {t["tool_id"]: t for t in tools}
    blocked_reasons = []

    # 有效的 source_kind 枚举值
    VALID_SOURCE_KINDS = {e.value for e in SlotSourceKind}

    for item in req.tools:
        tool_data = tool_map.get(item.tool_id)
        if not tool_data:
            continue

        tool_data["confirmed"] = item.confirmed
        if not item.confirmed:
            continue

        # 建立已知字段的 allowed_sources 映射
        provenance_map = {p["field_name"]: p for p in tool_data.get("input_provenance", [])}

        # 验证 input provenance
        for prov in item.input_provenance:
            field_name = prov.get("field_name")
            source_kind = prov.get("source_kind")
            source_ref = prov.get("source_ref")

            # 严格枚举校验
            if source_kind and source_kind not in VALID_SOURCE_KINDS:
                blocked_reasons.append(
                    f"Tool '{tool_data['tool_name']}' 字段 '{field_name}' 的 source_kind "
                    f"'{source_kind}' 不合法，允许值: {sorted(VALID_SOURCE_KINDS)}"
                )
                continue

            # 检查 per-field allowed_sources 约束（来自 _detect_tools 预计算）
            field_meta = provenance_map.get(field_name, {})
            allowed = field_meta.get("allowed_sources")
            if allowed and source_kind and source_kind not in allowed:
                blocked_reasons.append(
                    f"Tool '{tool_data['tool_name']}' 字段 '{field_name}' 不允许来源 "
                    f"'{source_kind}'，允许来源: {allowed}"
                )
                continue

            if not source_kind or not source_ref:
                # 检查是否是 required
                required = tool_data.get("input_schema", {}).get("required", [])
                if field_name in required:
                    blocked_reasons.append(
                        f"Tool '{tool_data['tool_name']}' 的必填字段 '{field_name}' 无法 resolve 来源"
                    )
                continue

            # 记录证据
            db.add(SandboxTestEvidence(
                session_id=session_id,
                evidence_type=EvidenceType.TOOL_PROVENANCE,
                step="tool_review",
                tool_id=item.tool_id,
                field_name=field_name,
                source_kind=SlotSourceKind(source_kind),
                source_ref=source_ref,
                verified=True,
            ))

        # 更新 tool_data 中的 provenance
        tool_data["input_provenance"] = item.input_provenance

    session.tool_review = tools

    if blocked_reasons:
        session.status = SessionStatus.CANNOT_TEST
        session.blocked_reason = "\n".join(blocked_reasons)
    else:
        session.current_step = SessionStep.PERMISSION_REVIEW
        session.status = SessionStatus.DRAFT

    db.commit()
    return _serialize_session(session)


@router.post("/{session_id}/permission-review")
async def submit_permission_review(
    session_id: int,
    req: PermissionReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交 Q3 权限快照确认。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if session.current_step != SessionStep.PERMISSION_REVIEW:
        raise HTTPException(400, f"当前步骤不是 permission_review，而是 {session.current_step.value}")

    # 构建权限快照（如果尚未构建）
    if not session.permission_snapshot:
        if session.target_type == "skill":
            skill = db.get(Skill, session.target_id)
            if skill:
                session.permission_snapshot = _build_permission_snapshot(skill, db)

    snapshots = session.permission_snapshot or []
    snap_map = {s["table_name"]: s for s in snapshots}
    blocked_reasons = []

    for item in req.tables:
        snap = snap_map.get(item.table_name)
        if not snap:
            continue
        snap["confirmed"] = item.confirmed
        snap["included_in_test"] = item.included_in_test

        if not item.confirmed:
            blocked_reasons.append(
                f"数据表 '{item.table_name}' 的权限配置与业务预期不一致，测试阻断"
            )

        # 记录权限快照证据
        db.add(SandboxTestEvidence(
            session_id=session_id,
            evidence_type=EvidenceType.PERMISSION_SNAPSHOT,
            step="permission_review",
            table_name=item.table_name,
            snapshot_data=snap,
            verified=item.confirmed,
        ))

    session.permission_snapshot = snapshots

    if blocked_reasons:
        session.status = SessionStatus.BLOCKED
        session.blocked_reason = "\n".join(blocked_reasons)
    else:
        session.current_step = SessionStep.CASE_GENERATION
        session.status = SessionStatus.READY_TO_RUN

    db.commit()
    return _serialize_session(session)


@router.post("/{session_id}/run")
async def run_tests(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """生成测试矩阵并执行。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if session.status not in (SessionStatus.READY_TO_RUN, SessionStatus.DRAFT) or \
       session.current_step not in (SessionStep.CASE_GENERATION, SessionStep.PERMISSION_REVIEW):
        # 允许从 CASE_GENERATION 或 READY_TO_RUN 启动
        if session.current_step != SessionStep.CASE_GENERATION:
            raise HTTPException(400, f"当前状态不允许执行测试: step={session.current_step.value}, status={session.status.value}")

    session.current_step = SessionStep.EXECUTION
    session.status = SessionStatus.RUNNING
    db.commit()

    # 1. 生成语义矩阵
    semantic_combos, theoretical = _generate_semantic_matrix(session)
    session.theoretical_combo_count = theoretical
    session.semantic_combo_count = len(semantic_combos)

    # 阈值检查
    MAX_SEMANTIC_COMBOS = 50
    if len(semantic_combos) > MAX_SEMANTIC_COMBOS:
        session.status = SessionStatus.BLOCKED
        session.blocked_reason = (
            f"权限语义组合数 {len(semantic_combos)} 超过阈值 {MAX_SEMANTIC_COMBOS}，"
            "需要拆分权限策略或缩小测试范围后重试"
        )
        session.current_step = SessionStep.PERMISSION_REVIEW
        db.commit()
        return _serialize_session(session)

    # 2. 构建测试输入（来自测试人确认的真实来源，不由 LLM 生成）
    test_input_text = _build_test_input_from_evidence(session, db)

    # 如果真实取数失败，session 已被标记为 cannot_test
    if session.status == SessionStatus.CANNOT_TEST:
        db.commit()
        return _serialize_session(session)

    # 3. 获取 system_prompt
    system_prompt = ""
    if session.target_type == "skill":
        skill = db.get(Skill, session.target_id)
        if skill:
            version = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id, SkillVersion.version == session.target_version)
                .first()
            )
            if version:
                system_prompt = version.system_prompt or ""
                from app.services.skill_engine import _read_source_files
                file_ctx = _read_source_files(skill.id, skill.source_files or [])
                if file_ctx:
                    system_prompt += file_ctx

    # 4. 逐用例执行
    from app.services.llm_gateway import llm_gateway
    cases = []

    for idx, combo in enumerate(semantic_combos):
        # 构建权限注入后的 prompt
        permission_injection = (
            f"\n\n## 当前权限上下文\n"
            f"- 行级可见范围: {combo['row_visibility']}\n"
            f"- 字段输出语义: {combo['field_output_semantic']}\n"
            f"- 分组语义: {combo['group_semantic']}\n"
            f"- 工具前置条件: {combo['tool_precondition']}\n"
        )
        full_prompt = system_prompt + permission_injection

        case = SandboxTestCase(
            session_id=session_id,
            case_index=idx,
            row_visibility=combo["row_visibility"],
            field_output_semantic=combo["field_output_semantic"],
            group_semantic=combo["group_semantic"],
            tool_precondition=combo["tool_precondition"],
            input_provenance={s["slot_key"]: f"{s.get('chosen_source')}:{s.get('evidence_ref', '')}" for s in (session.detected_slots or []) if s.get("chosen_source")},
            test_input=test_input_text,
            system_prompt_used=full_prompt[:5000],  # 截断存储
        )

        if combo["tool_precondition"] == "precondition_failed":
            case.verdict = CaseVerdict.SKIPPED
            case.verdict_reason = "工具前置条件不满足，跳过执行"
            case.execution_duration_ms = 0
        else:
            t0 = time.time()
            try:
                model_cfg = llm_gateway.resolve_config(db, "sandbox.interactive_exec")
                response, _ = await llm_gateway.chat(
                    model_config=model_cfg,
                    messages=[
                        {"role": "system", "content": full_prompt},
                        {"role": "user", "content": test_input_text},
                    ],
                    temperature=0.7,
                    max_tokens=1500,
                )
                case.llm_response = response
                case.execution_duration_ms = int((time.time() - t0) * 1000)
                case.verdict = CaseVerdict.PASSED  # 暂标记，evaluation 阶段会重新判定
            except Exception as e:
                case.llm_response = f"执行错误: {e}"
                case.execution_duration_ms = int((time.time() - t0) * 1000)
                case.verdict = CaseVerdict.ERROR
                case.verdict_reason = str(e)

        db.add(case)
        cases.append(case)

    session.executed_case_count = len(cases)
    session.current_step = SessionStep.EVALUATION
    db.commit()

    # 5. 评价阶段
    evaluation = await _evaluate_session(session, cases, db)
    session.quality_passed = evaluation["quality_passed"]
    session.usability_passed = evaluation["usability_passed"]
    session.anti_hallucination_passed = evaluation["anti_hallucination_passed"]
    session.approval_eligible = all([
        evaluation["quality_passed"],
        evaluation["usability_passed"],
        evaluation["anti_hallucination_passed"],
    ])
    session.current_step = SessionStep.DONE
    session.status = SessionStatus.COMPLETED
    session.completed_at = datetime.datetime.utcnow()
    db.commit()

    # 6. 生成报告
    from app.services.sandbox_report import generate_report
    report = await generate_report(session, cases, evaluation, db)
    session.report_id = report.id
    db.commit()

    return _serialize_session(session)


@router.get("/{session_id}/report")
async def get_report(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取结构化测试报告。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if not session.report_id:
        raise HTTPException(400, "测试报告尚未生成")

    report = db.get(SandboxTestReport, session.report_id)
    if not report:
        raise HTTPException(404, "测试报告不存在")

    return {
        "report_id": report.id,
        "session_id": report.session_id,
        "target_type": report.target_type,
        "target_id": report.target_id,
        "target_version": report.target_version,
        "target_name": report.target_name,
        "tester_id": report.tester_id,
        "part1_evidence_check": report.part1_evidence_check,
        "part2_test_matrix": report.part2_test_matrix,
        "part3_evaluation": report.part3_evaluation,
        "theoretical_combo_count": report.theoretical_combo_count,
        "semantic_combo_count": report.semantic_combo_count,
        "executed_case_count": report.executed_case_count,
        "quality_passed": report.quality_passed,
        "usability_passed": report.usability_passed,
        "anti_hallucination_passed": report.anti_hallucination_passed,
        "approval_eligible": report.approval_eligible,
        "report_hash": report.report_hash,
        "knowledge_entry_id": report.knowledge_entry_id,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


@router.post("/{session_id}/submit-approval")
async def submit_approval(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """仅在 approval_eligible=true 时允许提交审批。"""
    session = db.get(SandboxTestSession, session_id)
    if not session:
        raise HTTPException(404, "测试会话不存在")
    if not session.approval_eligible:
        raise HTTPException(400, "测试未通过全部三项评价，无法提交审批")
    if not session.report_id:
        raise HTTPException(400, "测试报告未生成")

    report = db.get(SandboxTestReport, session.report_id)

    # 验证报告版本与当前版本一致
    if session.target_type == "skill":
        skill = db.get(Skill, session.target_id)
        if skill:
            latest_ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            if latest_ver and latest_ver.version != session.target_version:
                raise HTTPException(
                    400,
                    f"测试报告基于 v{session.target_version}，但当前最新版本是 v{latest_ver.version}，需重新测试"
                )

    # 验证绑定 tool 内容未被篡改
    for tr in (session.tool_review or []):
        if not tr.get("confirmed"):
            continue
        tool_id = tr.get("tool_id")
        saved_hash = tr.get("content_hash")
        if tool_id and saved_hash:
            tool = db.get(ToolRegistry, tool_id)
            if tool:
                current_content = json.dumps(
                    {"schema": tool.input_schema or {}, "config": tool.config or {}},
                    sort_keys=True, default=str,
                )
                current_hash = hashlib.sha256(current_content.encode()).hexdigest()[:16]
                if current_hash != saved_hash:
                    raise HTTPException(
                        400,
                        f"Tool '{tr.get('tool_name')}' 自测试以来已被修改"
                        f"（测试哈希: {saved_hash}, 当前: {current_hash}），需重新测试"
                    )

    # 创建审批请求
    from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus

    existing = db.query(ApprovalRequest).filter(
        ApprovalRequest.target_id == session.target_id,
        ApprovalRequest.request_type == ApprovalRequestType.SKILL_PUBLISH,
        ApprovalRequest.status == ApprovalStatus.PENDING,
    ).first()
    if existing:
        raise HTTPException(400, "已有待审批的发布请求")

    req_type = ApprovalRequestType.SKILL_PUBLISH if session.target_type == "skill" else ApprovalRequestType.TOOL_PUBLISH

    sandbox_scan_data = {
        "sandbox_test_session_id": session.id,
        "sandbox_test_report_id": report.id if report else None,
        "report_knowledge_id": report.knowledge_entry_id if report else None,
        "report_hash": report.report_hash if report else None,
        "target_version": session.target_version,
    }

    approval = ApprovalRequest(
        request_type=req_type,
        target_id=session.target_id,
        target_type=session.target_type,
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
        security_scan_result=sandbox_scan_data,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    # 异步触发安全扫描，合并 suggested_policy 到 security_scan_result
    if session.target_type == "skill":
        import asyncio
        from app.database import SessionLocal
        from app.services.skill_security_scanner import skill_security_scanner

        async def _run_scan(approval_id: int, skill_id: int, sandbox_data: dict):
            scan_db = SessionLocal()
            try:
                scan_result = await skill_security_scanner.scan(skill_id, scan_db)
                # 合并：保留 sandbox 字段，叠加安全扫描结果（suggested_policy, risk_level 等）
                merged = {**scan_result, **sandbox_data}
                ar = scan_db.get(ApprovalRequest, approval_id)
                if ar:
                    ar.security_scan_result = merged
                    scan_db.commit()
            except Exception as e:
                logger.error(f"沙盒审批后安全扫描失败 approval={approval_id}: {e}")
            finally:
                scan_db.close()

        asyncio.create_task(_run_scan(approval.id, session.target_id, sandbox_scan_data))

    return {
        "approval_request_id": approval.id,
        "session_id": session.id,
        "report_id": session.report_id,
    }


# ─── Internal Helpers ────────────────────────────────────────────────────────

def _build_test_input_from_evidence(session: SandboxTestSession, db: Session) -> str:
    """从测试人确认的真实来源构建测试输入，禁止 LLM 生成。

    data_table 来源会查询真实数据行并应用权限过滤；无法取到数据则标记 cannot_test。
    """
    parts = []
    cannot_test_reasons = []

    for slot in (session.detected_slots or []):
        if slot.get("evidence_status") != "verified":
            continue

        source = slot.get("chosen_source")
        label = slot.get("label", slot.get("slot_key", ""))

        if source == "chat_text" and slot.get("chat_example"):
            parts.append(f"{label}: {slot['chat_example']}")

        elif source == "knowledge" and slot.get("knowledge_entry_id"):
            ke = db.get(KnowledgeEntry, slot["knowledge_entry_id"])
            if ke:
                content_preview = (ke.content or "")[:500]
                parts.append(f"{label}: [知识库 #{ke.id}] {ke.title}\n{content_preview}")
            else:
                cannot_test_reasons.append(f"知识条目 #{slot['knowledge_entry_id']} 不存在")

        elif source == "data_table" and slot.get("table_name"):
            table_name = slot["table_name"]
            field_name = slot.get("field_name")
            real_data = _fetch_real_table_data(table_name, field_name, session.tester_id, db)
            if real_data is None:
                cannot_test_reasons.append(
                    f"数据表 '{table_name}' 无法读取真实数据（表未注册或无权限）"
                )
            elif len(real_data) == 0:
                cannot_test_reasons.append(
                    f"数据表 '{table_name}' 查询结果为空（权限过滤后无可见行）"
                )
            else:
                # 序列化为 Markdown 表格，最多 10 行
                data_text = _format_table_data(table_name, field_name, real_data[:10])
                parts.append(f"{label}:\n{data_text}")

        elif source == "system_runtime":
            parts.append(f"{label}: [系统运行时自动获取]")

    if cannot_test_reasons:
        # 更新 session 状态
        session.status = SessionStatus.CANNOT_TEST
        session.blocked_reason = "真实取数失败:\n" + "\n".join(cannot_test_reasons)

    if not parts:
        return "请基于已绑定的知识库和数据表信息执行分析。"

    return "\n".join(parts)


def _fetch_real_table_data(
    table_name: str, field_name: str | None, tester_id: int, db: Session
) -> list[dict] | None:
    """查询真实数据表行，应用 DataOwnership 行级权限 + 字段遮罩。

    返回 None 表示表不存在/不可读；返回 [] 表示权限过滤后无数据。
    """
    bt = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
    if not bt:
        return None

    # 基础 SELECT
    if field_name and field_name != "*":
        # 验证字段存在于 DDL
        cols = [c["name"] for c in (bt.columns or [])] if bt.columns else []
        if cols and field_name not in cols:
            select_clause = "*"
        else:
            select_clause = f"`{field_name}`"
    else:
        select_clause = "*"

    sql = f"SELECT {select_clause} FROM `{table_name}`"

    # 行级权限注入（DataOwnership）
    from app.models.user import User as UserModel
    tester = db.get(UserModel, tester_id)
    ownership = db.query(DataOwnership).filter(DataOwnership.table_name == table_name).first()

    where_clauses = []
    if ownership and tester and tester.role.value not in ("super_admin",):
        if ownership.owner_field:
            where_clauses.append(f"`{ownership.owner_field}` = {tester.id}")
        if ownership.department_field and tester.department_id:
            dep_clause = f"`{ownership.department_field}` = {tester.department_id}"
            if where_clauses:
                # owner OR department
                where_clauses = [f"({where_clauses[0]} OR {dep_clause})"]
            else:
                where_clauses.append(dep_clause)

    # validation_rules 行级限制
    rules = bt.validation_rules or {}
    row_scope = rules.get("row_scope", "all")
    if row_scope == "private" and tester and tester.role.value not in ("super_admin",):
        return []  # 非管理员不可见
    elif row_scope == "department" and tester:
        dept_ids = rules.get("row_department_ids", [])
        if dept_ids and tester.department_id not in dept_ids and tester.role.value not in ("super_admin",):
            return []

    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    sql += " LIMIT 10"

    try:
        result = db.execute(__import__("sqlalchemy").text(sql))
        rows = [dict(r._mapping) for r in result]
    except Exception as e:
        logger.warning(f"_fetch_real_table_data failed for {table_name}: {e}")
        return None

    # 字段遮罩：hidden_fields
    hidden = set(rules.get("hidden_fields", []))
    if hidden:
        rows = [{k: v for k, v in row.items() if k not in hidden} for row in rows]

    # 三层字段遮罩（GlobalDataMask + RoleMaskOverride + SkillMaskOverride）
    try:
        from app.services.permission_engine import permission_engine
        if rows:
            rows = permission_engine.apply_data_masks(
                rows=rows,
                table_name=table_name,
                user=tester,
                skill_id=None,
                db=db,
            )
    except Exception as e:
        logger.warning(f"Permission mask failed for {table_name}: {e}")

    return rows


def _format_table_data(table_name: str, field_name: str | None, rows: list[dict]) -> str:
    """将查询结果格式化为 Markdown 表格。"""
    if not rows:
        return f"[数据表 {table_name} 无数据]"

    headers = list(rows[0].keys())
    lines = [
        f"数据表: {table_name}" + (f" (字段: {field_name})" if field_name and field_name != "*" else ""),
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        vals = []
        for h in headers:
            v = row.get(h, "")
            # 截断过长值
            s = str(v) if v is not None else ""
            vals.append(s[:80] if len(s) > 80 else s)
        lines.append("| " + " | ".join(vals) + " |")

    if len(rows) == 10:
        lines.append(f"（仅展示前 10 行，实际可能更多）")

    return "\n".join(lines)


async def _evaluate_session(
    session: SandboxTestSession,
    cases: list[SandboxTestCase],
    db: Session,
) -> dict:
    """Part 3 评价：质量 + 易用性 + 反幻觉限制。"""
    evaluation = {
        "quality_passed": False,
        "quality_detail": {},
        "usability_passed": False,
        "usability_detail": {},
        "anti_hallucination_passed": False,
        "anti_hallucination_detail": {},
    }

    # ── 3.1 质量评价 ──
    # 使用 LLM 对真实执行输出做质量评价（LLM 允许动作）
    successful_cases = [c for c in cases if c.verdict in (CaseVerdict.PASSED, None) and c.llm_response]
    if not successful_cases:
        evaluation["quality_detail"] = {"reason": "无成功执行的测试用例"}
        evaluation["quality_passed"] = False
    else:
        from app.services.llm_gateway import llm_gateway

        quality_scores = []
        for case in successful_cases[:5]:  # 最多评估 5 个
            score_prompt = (
                f"你是 AI Skill 质量评审官。评估以下输出是否真正解决了 Skill 定义的问题。\n\n"
                f"Skill 名称：{session.target_name}\n"
                f"测试输入：\n{case.test_input[:500]}\n\n"
                f"权限上下文：行可见={case.row_visibility}, 字段={case.field_output_semantic}\n\n"
                f"AI 输出：\n{case.llm_response[:1500]}\n\n"
                f"评分标准：\n"
                f"1. 目标覆盖度（40%）：是否解决核心问题，而非只碰到子问题\n"
                f"2. 输出完整性（30%）：结构完整，关键信息齐全\n"
                f"3. 关键约束遵守度（30%）：是否遵守权限限制\n\n"
                f"只输出 JSON：{{\"score\": 75, \"coverage\": 80, \"completeness\": 70, \"constraint\": 75, \"reason\": \"一句话\"}}"
            )
            try:
                result, _ = await llm_gateway.chat(
                    model_config=llm_gateway.resolve_config(db, "sandbox.evaluate"),
                    messages=[{"role": "user", "content": score_prompt}],
                    temperature=0.0,
                    max_tokens=300,
                )
                text = result.strip()
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                score_data = json.loads(text.strip())
                quality_scores.append(score_data.get("score", 0))
                # 更新 case verdict
                if score_data.get("score", 0) >= 60:
                    case.verdict = CaseVerdict.PASSED
                    case.verdict_reason = score_data.get("reason", "")
                else:
                    case.verdict = CaseVerdict.FAILED
                    case.verdict_reason = score_data.get("reason", "")
            except Exception:
                quality_scores.append(50)

        avg_score = sum(quality_scores) / len(quality_scores) if quality_scores else 0
        evaluation["quality_passed"] = avg_score >= 70
        evaluation["quality_detail"] = {
            "avg_score": round(avg_score),
            "case_scores": quality_scores,
            "standard": "全面丰富的维度和严谨 SOP 解决问题",
        }

    # ── 3.2 易用性评价 ──
    structured_input_count = 0
    for slot in (session.detected_slots or []):
        if slot.get("structured") and slot.get("chosen_source") == "chat_text":
            structured_input_count += 1

    evaluation["usability_passed"] = structured_input_count <= 5
    evaluation["usability_detail"] = {
        "structured_input_count": structured_input_count,
        "threshold": 5,
        "suggestion": "建议关联数据表和制作数据查询工具后重新测试" if structured_input_count > 5 else None,
    }

    # ── 3.3 反幻觉限制评价 ──
    system_prompt = ""
    if session.target_type == "skill":
        skill = db.get(Skill, session.target_id)
        if skill:
            version = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id, SkillVersion.version == session.target_version)
                .first()
            )
            if version:
                system_prompt = version.system_prompt or ""

    # 检查 prompt 中是否有反幻觉限制语句（LLM 允许动作：识别限制语句）
    anti_hallucination_checks = []
    check_patterns = [
        ("数据推理必须基于证据", ["基于数据", "基于证据", "根据数据", "根据实际", "以数据为依据"]),
        ("无证据时承认无法判断", ["无法判断", "无法确定", "信息不足", "不做推测", "不可臆断", "不可推测", "不得编造"]),
        ("不可用常识补全结构化事实", ["不可编造", "禁止虚构", "不得杜撰", "不允许臆造", "不可补全", "仅使用提供的数据"]),
    ]
    for check_name, keywords in check_patterns:
        found = any(kw in system_prompt for kw in keywords)
        anti_hallucination_checks.append({
            "check": check_name,
            "found": found,
            "keywords_searched": keywords,
        })

    # 也检查 tool 描述中的限制
    for tr in (session.tool_review or []):
        desc = tr.get("description", "")
        for check_name, keywords in check_patterns:
            if any(kw in desc for kw in keywords):
                # 已有工具级限制也算
                for chk in anti_hallucination_checks:
                    if chk["check"] == check_name:
                        chk["found"] = True

    all_checks_passed = all(c["found"] for c in anti_hallucination_checks)
    evaluation["anti_hallucination_passed"] = all_checks_passed
    evaluation["anti_hallucination_detail"] = {
        "checks": anti_hallucination_checks,
        "suggestion": "要求在 prompt 中明确添加反幻觉限制后重新测试" if not all_checks_passed else None,
    }

    db.commit()
    return evaluation


def _serialize_session(session: SandboxTestSession) -> dict:
    """序列化 session 为 API 响应。"""
    return {
        "session_id": session.id,
        "target_type": session.target_type,
        "target_id": session.target_id,
        "target_version": session.target_version,
        "target_name": session.target_name,
        "tester_id": session.tester_id,
        "status": session.status.value if session.status else "draft",
        "current_step": session.current_step.value if session.current_step else "start",
        "blocked_reason": session.blocked_reason,
        "detected_slots": session.detected_slots,
        "tool_review": session.tool_review,
        "permission_snapshot": session.permission_snapshot,
        "theoretical_combo_count": session.theoretical_combo_count,
        "semantic_combo_count": session.semantic_combo_count,
        "executed_case_count": session.executed_case_count,
        "quality_passed": session.quality_passed,
        "usability_passed": session.usability_passed,
        "anti_hallucination_passed": session.anti_hallucination_passed,
        "approval_eligible": session.approval_eligible,
        "report_id": session.report_id,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
    }
