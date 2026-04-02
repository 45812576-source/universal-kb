import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.business import BusinessTable
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_governance import (
    GovernanceDepartmentMission,
    GovernanceFieldTemplate,
    GovernanceKR,
    GovernanceObject,
    GovernanceObjectFacet,
    GovernanceObjectType,
    GovernanceObjective,
    GovernanceRequiredElement,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)
from app.models.project import Project
from app.models.task import Task
from app.models.user import Role, User
from app.services.knowledge_governance_service import (
    create_or_update_governance_suggestion_for_entry,
    create_or_update_governance_suggestion_for_table,
    ensure_governance_defaults,
    ensure_governance_object,
    record_governance_feedback,
)

router = APIRouter(prefix="/api/knowledge-governance", tags=["knowledge-governance"])


DEFAULT_BLUEPRINT = [
    {
        "code": "company_common",
        "name": "公司通行",
        "description": "适用于全公司共识、战略、制度、底层方法论。",
        "level": "company",
        "objective_role": "strategy",
        "libraries": [
            {
                "code": "company_strategy",
                "name": "战略与经营基线",
                "object_type": "knowledge_asset",
                "description": "战略目标、经营地图、组织协同规则。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "owner", "label": "责任人", "required": False},
                    {"key": "effective_date", "label": "生效日期", "required": False},
                ],
                "consumption_scenarios": ["战略复盘", "组织对齐", "管理决策"],
            },
            {
                "code": "company_org_design",
                "name": "组织与机制",
                "object_type": "knowledge_asset",
                "description": "组织结构、职责边界、协同机制、授权规则。",
                "default_update_cycle": "quarterly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "org_scope", "label": "适用范围", "required": False},
                    {"key": "owner", "label": "负责人", "required": False},
                ],
                "consumption_scenarios": ["组织设计", "边界澄清", "协同升级"],
            },
            {
                "code": "company_sop",
                "name": "通用 SOP 与制度",
                "object_type": "sop_ticket",
                "description": "跨部门通用流程、制度、审批和协作规范。",
                "default_update_cycle": "quarterly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "process_owner", "label": "流程负责人", "required": False},
                    {"key": "sla", "label": "时效要求", "required": False},
                ],
                "consumption_scenarios": ["执行指引", "培训", "审计留痕"],
            },
            {
                "code": "company_templates",
                "name": "模板与标准件",
                "object_type": "knowledge_asset",
                "description": "报告模板、审批模板、话术模版、表单标准件。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "template_type", "label": "模板类型", "required": False},
                    {"key": "consumer_role", "label": "使用角色", "required": False},
                ],
                "consumption_scenarios": ["快速复用", "规范输出", "培训"],
            },
            {
                "code": "company_policies",
                "name": "制度与政策原文",
                "object_type": "knowledge_asset",
                "description": "正式制度、政策口径、红线条款、审计留痕依据。",
                "default_update_cycle": "quarterly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "policy_scope", "label": "适用范围", "required": False},
                    {"key": "effective_date", "label": "生效日期", "required": False},
                ],
                "consumption_scenarios": ["制度查询", "审计依据", "风险对齐"],
            },
            {
                "code": "company_meeting_decisions",
                "name": "关键会议与决议",
                "object_type": "knowledge_asset",
                "description": "经营会、专项会、复盘会的纪要、决议和行动项背景。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "meeting_type", "label": "会议类型", "required": False},
                    {"key": "decision_owner", "label": "决议负责人", "required": False},
                ],
                "consumption_scenarios": ["经营跟进", "跨部门对齐", "责任追踪"],
            },
            {
                "code": "company_metrics",
                "name": "经营指标口径",
                "object_type": "knowledge_asset",
                "description": "核心指标定义、口径、分子分母和更新责任。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "metric_name", "label": "指标名称", "required": True},
                    {"key": "metric_owner", "label": "指标负责人", "required": False},
                ],
                "consumption_scenarios": ["经营复盘", "报表解释", "跨团队协同"],
            },
        ],
    },
    {
        "code": "professional_capability",
        "name": "职业能力",
        "description": "岗位胜任力、方法、案例和训练材料。",
        "level": "function",
        "objective_role": "enablement",
        "libraries": [
            {
                "code": "general_capability",
                "name": "通用能力",
                "object_type": "skill_material",
                "description": "跨岗位通用的方法、表达、协作和分析能力。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "skill_level", "label": "适用层级", "required": False},
                    {"key": "training_mode", "label": "训练形式", "required": False},
                ],
                "consumption_scenarios": ["训练营", "入职", "复盘"],
            },
            {
                "code": "role_capability",
                "name": "岗位能力",
                "object_type": "skill_material",
                "description": "按岗位沉淀的胜任力资料，例如客户运营、产品运营、产品、后端开发。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "competency_type", "label": "能力类型", "required": False},
                ],
                "consumption_scenarios": ["岗位赋能", "招聘画像", "绩效校准"],
            },
            {
                "code": "role_sop_playbook",
                "name": "岗位 SOP / Playbook",
                "object_type": "sop_ticket",
                "description": "岗位执行动作、标准动作卡、关键流程和工单处理手册。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "scenario", "label": "适用场景", "required": False},
                ],
                "consumption_scenarios": ["执行提效", "新人上手", "岗位交接"],
            },
            {
                "code": "role_case_repo",
                "name": "岗位案例库",
                "object_type": "case",
                "description": "按岗位拆分的优秀案例、失败案例、复盘案例。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "case_outcome", "label": "结果标签", "required": False},
                ],
                "consumption_scenarios": ["案例复盘", "方法迁移", "训练营"],
            },
            {
                "code": "role_interview_kit",
                "name": "岗位招聘与面试题库",
                "object_type": "skill_material",
                "description": "岗位画像、面试题、试岗题、录用标准和校准案例。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "seniority", "label": "职级", "required": False},
                ],
                "consumption_scenarios": ["招聘", "面试校准", "能力评估"],
            },
            {
                "code": "role_assessment_rubric",
                "name": "岗位考核标准",
                "object_type": "skill_material",
                "description": "岗位 KPI、绩效评价标准、晋升和任职标准。",
                "default_update_cycle": "quarterly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "assessment_cycle", "label": "考核周期", "required": False},
                ],
                "consumption_scenarios": ["绩效校准", "晋升判断", "任职标准对齐"],
            },
            {
                "code": "general_toolkit",
                "name": "通用工具箱",
                "object_type": "knowledge_asset",
                "description": "高频方法、提示词、模板、分析框架和工具操作手册。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "tool_name", "label": "工具名称", "required": False},
                    {"key": "usage_scenario", "label": "适用场景", "required": False},
                ],
                "consumption_scenarios": ["自助提效", "培训", "统一操作方式"],
            },
        ],
    },
    {
        "code": "outsource_intel",
        "name": "Outsource Intel",
        "description": "行业情报，按行业和情报类型组织。",
        "level": "function",
        "objective_role": "enablement",
        "libraries": [
            {
                "code": "industry_intel",
                "name": "行业情报",
                "object_type": "external_intel",
                "description": "按行业细分的市场动态、平台变化、竞品信息、素材趋势。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": True},
                    {"key": "intel_type", "label": "情报类型", "required": True},
                    {"key": "valid_until", "label": "有效期", "required": False},
                ],
                "consumption_scenarios": ["投放选题", "客户经营", "策略判断"],
            },
            {
                "code": "platform_watch",
                "name": "平台动态",
                "object_type": "external_intel",
                "description": "平台规则变化、能力更新、审核口径、流量机制。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "platform", "label": "平台", "required": True},
                    {"key": "change_type", "label": "变化类型", "required": False},
                ],
                "consumption_scenarios": ["投放应对", "策略调整", "风险预警"],
            },
            {
                "code": "creative_trends",
                "name": "素材趋势",
                "object_type": "external_intel",
                "description": "素材形式、卖点表达、爆款方向、创意趋势。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": True},
                    {"key": "creative_type", "label": "素材类型", "required": False},
                ],
                "consumption_scenarios": ["投流创意", "客户方案", "内容判断"],
            },
            {
                "code": "competitor_watch",
                "name": "竞品与对手观察",
                "object_type": "external_intel",
                "description": "竞品投放、竞品定价、竞品策略、代理商动态。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": True},
                    {"key": "competitor_name", "label": "对象名称", "required": False},
                ],
                "consumption_scenarios": ["客户提案", "竞争判断", "策略对标"],
            },
            {
                "code": "industry_map",
                "name": "行业地图",
                "object_type": "external_intel",
                "description": "行业结构、链路、关键玩家、供需变化和周期判断。",
                "default_update_cycle": "monthly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": True},
                    {"key": "segment", "label": "细分赛道", "required": False},
                ],
                "consumption_scenarios": ["新行业进入", "客户研究", "战略判断"],
            },
            {
                "code": "signal_alerts",
                "name": "风险与信号预警",
                "object_type": "external_intel",
                "description": "政策变化、舆情风险、平台处罚、关键突发事件。",
                "default_update_cycle": "daily",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": False},
                    {"key": "signal_level", "label": "风险等级", "required": False},
                ],
                "consumption_scenarios": ["风险预警", "客户提醒", "策略应急"],
            },
            {
                "code": "account_growth_playbook",
                "name": "增长打法情报",
                "object_type": "external_intel",
                "description": "外部增长模型、投放打法、转化链路和爆量规律。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": True},
                    {"key": "growth_theme", "label": "打法主题", "required": False},
                ],
                "consumption_scenarios": ["增长方案", "客户复盘", "策略迁移"],
            },
        ],
    },
    {
        "code": "business_line_execution",
        "name": "业务线作战",
        "description": "按业务线沉淀目标、资源库、案例和执行素材。",
        "level": "department",
        "objective_role": "execution",
        "libraries": [
            {
                "code": "biz_customer_repo",
                "name": "客户库",
                "object_type": "customer",
                "description": "业务线客户对象库，沉淀客户画像、阶段、动作和关键事实。",
                "default_update_cycle": "realtime",
                "default_visibility": "edit",
                "field_schema": [
                    {"key": "customer_name", "label": "客户名称", "required": True},
                    {"key": "owner", "label": "负责人", "required": True},
                    {"key": "stage", "label": "阶段", "required": False},
                ],
                "consumption_scenarios": ["客户经营", "跟进提醒", "交接"],
            },
            {
                "code": "biz_resource_repo",
                "name": "关键资源库",
                "object_type": "knowledge_asset",
                "description": "供应商、渠道、媒体、服务商等关键资源沉淀。",
                "default_update_cycle": "weekly",
                "default_visibility": "edit",
                "field_schema": [
                    {"key": "resource_type", "label": "资源类型", "required": True},
                    {"key": "contact", "label": "联系人", "required": False},
                ],
                "consumption_scenarios": ["资源盘点", "商务协作", "机会匹配"],
            },
            {
                "code": "biz_case_repo",
                "name": "业务案例库",
                "object_type": "case",
                "description": "业务线真实案例、项目案例、客户案例和复盘。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "industry", "label": "行业", "required": False},
                    {"key": "result", "label": "结果", "required": False},
                ],
                "consumption_scenarios": ["复制打法", "培训", "客户说服"],
            },
            {
                "code": "biz_sop",
                "name": "业务线 SOP",
                "object_type": "sop_ticket",
                "description": "业务线专属流程、交付动作、协同接口和工单处理。",
                "default_update_cycle": "weekly",
                "default_visibility": "edit",
                "field_schema": [
                    {"key": "scenario", "label": "场景", "required": False},
                    {"key": "owner", "label": "负责人", "required": False},
                ],
                "consumption_scenarios": ["执行提效", "交接", "跨部门协同"],
            },
            {
                "code": "biz_role_setup",
                "name": "业务线岗位设置",
                "object_type": "skill_material",
                "description": "业务线岗位职责、分工边界、协作接口和任职要求。",
                "default_update_cycle": "quarterly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "role_name", "label": "岗位名称", "required": True},
                    {"key": "owner_team", "label": "归属团队", "required": False},
                ],
                "consumption_scenarios": ["组织设计", "招聘", "交接"],
            },
            {
                "code": "biz_project_delivery",
                "name": "项目与交付资料",
                "object_type": "knowledge_asset",
                "description": "交付说明、项目模板、里程碑、复盘和问题台账。",
                "default_update_cycle": "weekly",
                "default_visibility": "edit",
                "field_schema": [
                    {"key": "project_type", "label": "项目类型", "required": False},
                    {"key": "delivery_owner", "label": "交付负责人", "required": False},
                ],
                "consumption_scenarios": ["项目推进", "交付协同", "项目复盘"],
            },
            {
                "code": "biz_external_signals",
                "name": "业务线外部信号",
                "object_type": "external_intel",
                "description": "服务于业务线绩效提升的外部平台动态、素材情报和竞品动作。",
                "default_update_cycle": "weekly",
                "default_visibility": "read",
                "field_schema": [
                    {"key": "channel", "label": "渠道/平台", "required": False},
                    {"key": "signal_type", "label": "信号类型", "required": False},
                ],
                "consumption_scenarios": ["经营提效", "客户建议", "增长策略"],
            },
        ],
    },
]


def _require_admin(user: User) -> None:
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "仅管理员可执行该操作")


def _objective_dict(item: GovernanceObjective) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "code": item.code,
        "description": item.description,
        "level": item.level,
        "parent_id": item.parent_id,
        "department_id": item.department_id,
        "business_line": item.business_line,
        "objective_role": item.objective_role,
        "sort_order": item.sort_order,
        "is_active": item.is_active,
    }


def _resource_library_dict(item: GovernanceResourceLibrary) -> dict:
    return {
        "id": item.id,
        "objective_id": item.objective_id,
        "name": item.name,
        "code": item.code,
        "description": item.description,
        "library_type": item.library_type,
        "object_type": item.object_type,
        "governance_mode": item.governance_mode,
        "default_visibility": item.default_visibility,
        "default_update_cycle": item.default_update_cycle,
        "field_schema": item.field_schema or [],
        "consumption_scenarios": item.consumption_scenarios or [],
        "collaboration_baseline": item.collaboration_baseline or {},
        "classification_hints": item.classification_hints or {},
        "is_active": item.is_active,
    }


def _suggestion_dict(item: GovernanceSuggestionTask) -> dict:
    return {
        "id": item.id,
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
        "task_type": item.task_type,
        "status": item.status,
        "objective_id": item.objective_id,
        "resource_library_id": item.resource_library_id,
        "object_type_id": item.object_type_id,
        "suggested_payload": item.suggested_payload or {},
        "reason": item.reason,
        "confidence": item.confidence,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _load_subject(db: Session, subject_type: str, subject_id: int):
    subject_type = subject_type.lower()
    if subject_type == "knowledge":
        return db.get(KnowledgeEntry, subject_id)
    if subject_type == "business_table":
        return db.get(BusinessTable, subject_id)
    if subject_type == "project":
        return db.get(Project, subject_id)
    if subject_type == "task":
        return db.get(Task, subject_id)
    return None


def _subject_governance_snapshot(subject) -> dict:
    return {
        "governance_objective_id": getattr(subject, "governance_objective_id", None),
        "resource_library_id": getattr(subject, "resource_library_id", None),
        "object_type_id": getattr(subject, "object_type_id", None),
        "governance_status": getattr(subject, "governance_status", None),
        "governance_note": getattr(subject, "governance_note", None),
    }


class GovernanceObjectiveCreate(BaseModel):
    name: str
    code: str
    description: str | None = None
    level: str = "department"
    parent_id: int | None = None
    department_id: int | None = None
    business_line: str | None = None
    objective_role: str | None = None
    sort_order: int = 0


class GovernanceLibraryCreate(BaseModel):
    objective_id: int
    name: str
    code: str
    description: str | None = None
    library_type: str = "resource_library"
    object_type: str
    governance_mode: str = "ab_fusion"
    default_visibility: str = "read"
    default_update_cycle: str | None = None
    field_schema: list[dict] = Field(default_factory=list)
    consumption_scenarios: list[str] = Field(default_factory=list)
    collaboration_baseline: dict = Field(default_factory=dict)
    classification_hints: dict = Field(default_factory=dict)


class SuggestionCreate(BaseModel):
    subject_type: str
    subject_id: int
    task_type: str = "classify"
    objective_id: int | None = None
    resource_library_id: int | None = None
    object_type_id: int | None = None
    suggested_payload: dict = Field(default_factory=dict)
    reason: str | None = None
    confidence: int = 0


class ApplyGovernanceRequest(BaseModel):
    subject_type: str
    subject_id: int
    objective_id: int | None = None
    resource_library_id: int | None = None
    object_type_id: int | None = None
    governance_status: str = "aligned"
    governance_note: str | None = None


class GovernanceObjectCreate(BaseModel):
    object_type_code: str
    canonical_key: str
    display_name: str
    business_line: str | None = None
    department_id: int | None = None
    owner_id: int | None = None


class BindGovernanceObjectRequest(BaseModel):
    subject_type: str
    subject_id: int
    governance_object_id: int
    facet_name: str | None = None
    visibility_mode: str = "read"
    is_editable: bool = False
    update_cycle: str | None = None


class MergeGovernanceObjectsRequest(BaseModel):
    source_object_id: int
    target_object_id: int


class RejectGovernanceSuggestionRequest(BaseModel):
    note: str | None = None


class GovernanceStrategyTuneRequest(BaseModel):
    is_frozen: bool | None = None
    manual_bias: int | None = None


def _normalized_object_name(value: str | None) -> str:
    text = (value or "").strip().lower()
    return re.sub(r"[\W_]+", "", text)


def _recommended_actions_for_gap(
    gap_type: str,
    *,
    object_id: int,
    linked_knowledge_count: int,
    linked_table_count: int,
    linked_project_count: int,
    linked_task_count: int,
) -> list[dict]:
    actions: list[dict] = []
    if gap_type == "no_facet":
        actions.append({
            "action": "bind_existing_subject",
            "label": "补资源库视角",
            "target_object_id": object_id,
            "description": "把现有文档或数据表重新绑定到该对象，自动生成 facet。",
        })
    elif gap_type == "orphan_object":
        actions.append({
            "action": "bind_existing_subject",
            "label": "挂接现有对象来源",
            "target_object_id": object_id,
            "description": "给对象补至少一个文档、数据表、项目或任务来源。",
        })
    elif gap_type == "missing_table":
        actions.append({
            "action": "create_or_bind_table",
            "label": "补数据表",
            "target_object_id": object_id,
            "description": "该对象已有知识沉淀但没有结构化数据表，建议补表或绑定已有表。",
        })
    elif gap_type == "missing_knowledge":
        actions.append({
            "action": "create_or_bind_knowledge",
            "label": "补知识文档",
            "target_object_id": object_id,
            "description": "该对象已有数据消费但没有说明文档，建议补操作说明、案例或 SOP。",
        })
    elif gap_type == "missing_execution_link":
        actions.append({
            "action": "create_or_bind_task",
            "label": "补任务/项目联动",
            "target_object_id": object_id,
            "description": "对象已经沉淀，但没有项目或任务联动，协同闭环未形成。",
        })

    if linked_knowledge_count == 0 and linked_table_count == 0 and linked_project_count == 0 and linked_task_count == 0:
        actions.append({
            "action": "merge_or_archive",
            "label": "合并或归档",
            "target_object_id": object_id,
            "description": "如果这是误建对象，直接合并到主对象或归档掉。",
        })
    return actions


@router.get("/blueprint")
def get_governance_blueprint(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    objectives = db.query(GovernanceObjective).order_by(GovernanceObjective.sort_order, GovernanceObjective.id).all()
    libraries = db.query(GovernanceResourceLibrary).order_by(GovernanceResourceLibrary.id).all()
    object_types = db.query(GovernanceObjectType).order_by(GovernanceObjectType.id).all()
    field_templates = db.query(GovernanceFieldTemplate).order_by(GovernanceFieldTemplate.object_type_id, GovernanceFieldTemplate.sort_order).all()
    missions = db.query(GovernanceDepartmentMission).order_by(GovernanceDepartmentMission.department_id, GovernanceDepartmentMission.id).all()
    krs = db.query(GovernanceKR).order_by(GovernanceKR.mission_id, GovernanceKR.sort_order, GovernanceKR.id).all()
    required_elements = db.query(GovernanceRequiredElement).order_by(GovernanceRequiredElement.kr_id, GovernanceRequiredElement.sort_order, GovernanceRequiredElement.id).all()
    return {
        "seed_blueprint": DEFAULT_BLUEPRINT,
        "objectives": [_objective_dict(item) for item in objectives],
        "resource_libraries": [_resource_library_dict(item) for item in libraries],
        "object_types": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "description": item.description,
                "dimension_schema": item.dimension_schema or [],
                "baseline_fields": item.baseline_fields or [],
                "default_consumption_modes": item.default_consumption_modes or [],
            }
            for item in object_types
        ],
        "field_templates": [
            {
                "id": item.id,
                "object_type_id": item.object_type_id,
                "field_key": item.field_key,
                "field_label": item.field_label,
                "field_type": item.field_type,
                "is_required": item.is_required,
                "is_editable": item.is_editable,
                "visibility_mode": item.visibility_mode,
                "update_cycle": item.update_cycle,
                "consumer_modes": item.consumer_modes or [],
                "description": item.description,
                "example_values": item.example_values or [],
                "sort_order": item.sort_order,
            }
            for item in field_templates
        ],
        "department_missions": [
            {
                "id": item.id,
                "department_id": item.department_id,
                "objective_id": item.objective_id,
                "name": item.name,
                "code": item.code,
                "core_role": item.core_role,
                "mission_statement": item.mission_statement,
                "upstream_dependencies": item.upstream_dependencies or [],
                "downstream_deliverables": item.downstream_deliverables or [],
            }
            for item in missions
        ],
        "krs": [
            {
                "id": item.id,
                "mission_id": item.mission_id,
                "objective_id": item.objective_id,
                "name": item.name,
                "code": item.code,
                "description": item.description,
                "metric_definition": item.metric_definition,
                "target_value": item.target_value,
                "time_horizon": item.time_horizon,
                "owner_role": item.owner_role,
                "sort_order": item.sort_order,
            }
            for item in krs
        ],
        "required_elements": [
            {
                "id": item.id,
                "kr_id": item.kr_id,
                "name": item.name,
                "code": item.code,
                "element_type": item.element_type,
                "description": item.description,
                "required_library_codes": item.required_library_codes or [],
                "required_object_types": item.required_object_types or [],
                "suggested_update_cycle": item.suggested_update_cycle,
                "sort_order": item.sort_order,
            }
            for item in required_elements
        ],
    }


@router.post("/seed-defaults")
def seed_governance_defaults(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    ensure_governance_defaults(db, created_by=user.id)
    return {"ok": True, "seeded": True, "message": "默认蓝图已同步"}


@router.post("/objectives")
def create_governance_objective(
    req: GovernanceObjectiveCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    item = GovernanceObjective(
        name=req.name,
        code=req.code,
        description=req.description,
        level=req.level,
        parent_id=req.parent_id,
        department_id=req.department_id,
        business_line=req.business_line,
        objective_role=req.objective_role,
        sort_order=req.sort_order,
        created_by=user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _objective_dict(item)


@router.post("/resource-libraries")
def create_resource_library(
    req: GovernanceLibraryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    objective = db.get(GovernanceObjective, req.objective_id)
    if not objective:
        raise HTTPException(404, "治理目标不存在")
    item = GovernanceResourceLibrary(
        objective_id=req.objective_id,
        name=req.name,
        code=req.code,
        description=req.description,
        library_type=req.library_type,
        object_type=req.object_type,
        governance_mode=req.governance_mode,
        default_visibility=req.default_visibility,
        default_update_cycle=req.default_update_cycle,
        field_schema=req.field_schema,
        consumption_scenarios=req.consumption_scenarios,
        collaboration_baseline=req.collaboration_baseline,
        classification_hints=req.classification_hints,
        created_by=user.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _resource_library_dict(item)


@router.get("/suggestions")
def list_governance_suggestions(
    subject_type: str | None = None,
    subject_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(GovernanceSuggestionTask)
    if subject_type:
        q = q.filter(GovernanceSuggestionTask.subject_type == subject_type)
    if subject_id:
        q = q.filter(GovernanceSuggestionTask.subject_id == subject_id)
    if status:
        q = q.filter(GovernanceSuggestionTask.status == status)
    items = q.order_by(GovernanceSuggestionTask.created_at.desc()).all()
    return [_suggestion_dict(item) for item in items]


@router.post("/suggestions")
def create_governance_suggestion(
    req: SuggestionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    subject = _load_subject(db, req.subject_type, req.subject_id)
    if not subject:
        raise HTTPException(404, "治理目标对象不存在")
    item = GovernanceSuggestionTask(
        subject_type=req.subject_type,
        subject_id=req.subject_id,
        task_type=req.task_type,
        objective_id=req.objective_id,
        resource_library_id=req.resource_library_id,
        object_type_id=req.object_type_id,
        suggested_payload=req.suggested_payload,
        reason=req.reason,
        confidence=max(0, min(100, req.confidence)),
        created_by=user.id,
    )
    db.add(item)
    if hasattr(subject, "governance_status"):
        subject.governance_status = "suggested"
    db.commit()
    db.refresh(item)
    return _suggestion_dict(item)


@router.post("/apply")
def apply_governance_alignment(
    req: ApplyGovernanceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    subject = _load_subject(db, req.subject_type, req.subject_id)
    if not subject:
        raise HTTPException(404, "治理目标对象不存在")
    if hasattr(subject, "governance_objective_id"):
        subject.governance_objective_id = req.objective_id
    if hasattr(subject, "resource_library_id"):
        subject.resource_library_id = req.resource_library_id
    if hasattr(subject, "object_type_id"):
        subject.object_type_id = req.object_type_id
    if hasattr(subject, "governance_status"):
        subject.governance_status = req.governance_status
    if hasattr(subject, "governance_note"):
        subject.governance_note = req.governance_note

    pending = (
        db.query(GovernanceSuggestionTask)
        .filter(
            GovernanceSuggestionTask.subject_type == req.subject_type,
            GovernanceSuggestionTask.subject_id == req.subject_id,
            GovernanceSuggestionTask.status == "pending",
        )
        .all()
    )
    for item in pending:
        strategy_key = None
        if isinstance(item.suggested_payload, dict):
            reinforcement_meta = item.suggested_payload.get("reinforcement_meta")
            if isinstance(reinforcement_meta, dict):
                strategy_key = reinforcement_meta.get("strategy_key")
        item.status = "applied"
        item.resolved_by = user.id
        item.resolved_note = "治理挂载已应用"
        item.resolved_at = datetime.datetime.utcnow()
        if strategy_key:
            same_target = (
                item.objective_id == req.objective_id
                and item.resource_library_id == req.resource_library_id
            )
            reward = 1.0 if same_target else -0.7
            record_governance_feedback(
                db,
                subject_type=req.subject_type,
                subject_id=req.subject_id,
                strategy_key=strategy_key,
                event_type="applied" if same_target else "corrected",
                reward=reward,
                created_by=user.id,
                suggestion_id=item.id,
                from_objective_id=item.objective_id,
                from_resource_library_id=item.resource_library_id,
                to_objective_id=req.objective_id,
                to_resource_library_id=req.resource_library_id,
                note=req.governance_note,
            )

    db.commit()
    return {"ok": True}


@router.post("/suggestions/{suggestion_id}/reject")
def reject_governance_suggestion(
    suggestion_id: int,
    req: RejectGovernanceSuggestionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    item = db.get(GovernanceSuggestionTask, suggestion_id)
    if not item:
        raise HTTPException(404, "治理建议不存在")
    if item.status != "pending":
        raise HTTPException(400, "当前建议不是待处理状态")

    item.status = "rejected"
    item.resolved_by = user.id
    item.resolved_note = req.note or "人工拒绝治理建议"
    item.resolved_at = datetime.datetime.utcnow()

    strategy_key = None
    if isinstance(item.suggested_payload, dict):
        reinforcement_meta = item.suggested_payload.get("reinforcement_meta")
        if isinstance(reinforcement_meta, dict):
            strategy_key = reinforcement_meta.get("strategy_key")
    if strategy_key:
        record_governance_feedback(
            db,
            subject_type=item.subject_type,
            subject_id=item.subject_id,
            strategy_key=strategy_key,
            event_type="rejected",
            reward=-1.0,
            created_by=user.id,
            suggestion_id=item.id,
            from_objective_id=item.objective_id,
            from_resource_library_id=item.resource_library_id,
            note=req.note,
        )

    subject = _load_subject(db, item.subject_type, item.subject_id)
    if subject and getattr(subject, "governance_status", None) == "suggested":
        subject.governance_status = "needs_review"
    db.commit()
    return {"ok": True}


@router.post("/knowledge/{entry_id}/suggest")
def generate_governance_suggestion_for_knowledge(
    entry_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    entry = db.get(KnowledgeEntry, entry_id)
    if not entry:
        raise HTTPException(404, "知识文档不存在")
    task = create_or_update_governance_suggestion_for_entry(db, entry, created_by=user.id)
    if not task:
        return {"ok": True, "created": False, "message": "未命中治理建议规则"}
    return {"ok": True, "created": True, "suggestion": _suggestion_dict(task)}


@router.post("/knowledge/suggest-batch")
def generate_governance_suggestions_batch(
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    ensure_governance_defaults(db, created_by=user.id)
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.governance_status.in_([None, "ungoverned", "needs_review", "suggested"]))
        .order_by(KnowledgeEntry.updated_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    created = 0
    updated_ids: list[int] = []
    for entry in entries:
        task = create_or_update_governance_suggestion_for_entry(db, entry, created_by=user.id)
        if task:
            created += 1
            updated_ids.append(entry.id)
    return {"ok": True, "processed": len(entries), "suggested": created, "entry_ids": updated_ids}


@router.get("/subject")
def get_subject_governance(
    subject_type: str,
    subject_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    subject = _load_subject(db, subject_type, subject_id)
    if not subject:
        raise HTTPException(404, "对象不存在")
    suggestions = (
        db.query(GovernanceSuggestionTask)
        .filter(
            GovernanceSuggestionTask.subject_type == subject_type,
            GovernanceSuggestionTask.subject_id == subject_id,
        )
        .order_by(GovernanceSuggestionTask.created_at.desc())
        .all()
    )
    return {
        "subject": _subject_governance_snapshot(subject),
        "suggestions": [_suggestion_dict(item) for item in suggestions],
    }


@router.get("/objects")
def list_governance_objects(
    object_type_code: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(GovernanceObject)
    if object_type_code:
        object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == object_type_code).first()
        if object_type:
            q = q.filter(GovernanceObject.object_type_id == object_type.id)
    if q:
        q = q.filter(GovernanceObject.display_name.ilike(f"%{q}%"))
    items = q.order_by(GovernanceObject.updated_at.desc()).limit(200).all()
    return [
        {
            "id": item.id,
            "object_type_id": item.object_type_id,
            "canonical_key": item.canonical_key,
            "display_name": item.display_name,
            "business_line": item.business_line,
            "department_id": item.department_id,
            "owner_id": item.owner_id,
            "lifecycle_status": item.lifecycle_status,
            "object_payload": item.object_payload or {},
        }
        for item in items
    ]


@router.get("/objects/{object_id}")
def get_governance_object_detail(
    object_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.get(GovernanceObject, object_id)
    if not item:
        raise HTTPException(404, "治理对象不存在")
    facets = (
        db.query(GovernanceObjectFacet)
        .filter(GovernanceObjectFacet.governance_object_id == object_id)
        .order_by(GovernanceObjectFacet.updated_at.desc())
        .all()
    )
    linked_knowledge = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.governance_object_id == object_id)
        .order_by(KnowledgeEntry.updated_at.desc())
        .limit(20)
        .all()
    )
    linked_tables = (
        db.query(BusinessTable)
        .filter(BusinessTable.governance_object_id == object_id)
        .order_by(BusinessTable.updated_at.desc())
        .limit(20)
        .all()
    )
    linked_projects = (
        db.query(Project)
        .filter(
            Project.governance_objective_id == item.object_payload.get("objective_id")
            if isinstance(item.object_payload, dict) and item.object_payload.get("objective_id") is not None
            else False
        )
        .order_by(Project.updated_at.desc())
        .limit(20)
        .all()
    ) if isinstance(item.object_payload, dict) and item.object_payload.get("objective_id") is not None else []
    linked_tasks = (
        db.query(Task)
        .filter(Task.governance_object_id == object_id)
        .order_by(Task.updated_at.desc())
        .limit(20)
        .all()
    )
    return {
        "id": item.id,
        "object_type_id": item.object_type_id,
        "canonical_key": item.canonical_key,
        "display_name": item.display_name,
        "business_line": item.business_line,
        "department_id": item.department_id,
        "owner_id": item.owner_id,
        "lifecycle_status": item.lifecycle_status,
        "object_payload": item.object_payload or {},
        "facets": [
            {
                "id": facet.id,
                "resource_library_id": facet.resource_library_id,
                "facet_key": facet.facet_key,
                "facet_name": facet.facet_name,
                "field_values": facet.field_values or {},
                "consumer_scenarios": facet.consumer_scenarios or [],
                "visibility_mode": facet.visibility_mode,
                "is_editable": facet.is_editable,
                "update_cycle": facet.update_cycle,
                "source_subjects": facet.source_subjects or [],
            }
            for facet in facets
        ],
        "collaboration_baseline": {
            "knowledge_entries": [
                {"id": entry.id, "title": entry.title, "updated_at": entry.updated_at.isoformat() if entry.updated_at else None}
                for entry in linked_knowledge
            ],
            "business_tables": [
                {"id": table.id, "display_name": table.display_name, "table_name": table.table_name, "updated_at": table.updated_at.isoformat() if table.updated_at else None}
                for table in linked_tables
            ],
            "projects": [
                {"id": project.id, "name": project.name, "updated_at": project.updated_at.isoformat() if project.updated_at else None}
                for project in linked_projects
            ],
            "tasks": [
                {"id": task.id, "title": task.title, "status": task.status.value if task.status else None, "updated_at": task.updated_at.isoformat() if task.updated_at else None}
                for task in linked_tasks
            ],
        },
    }


@router.get("/object-conflicts")
def list_governance_object_conflicts(
    object_type_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    q = db.query(GovernanceObject)
    if object_type_code:
        object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == object_type_code).first()
        if object_type:
            q = q.filter(GovernanceObject.object_type_id == object_type.id)
    objects = q.order_by(GovernanceObject.updated_at.desc()).all()
    conflicts: list[dict] = []
    for idx, left in enumerate(objects):
        for right in objects[idx + 1:]:
            if left.object_type_id != right.object_type_id:
                continue
            left_name = (left.display_name or "").strip().lower()
            right_name = (right.display_name or "").strip().lower()
            if not left_name or not right_name:
                continue
            left_normalized = _normalized_object_name(left.display_name)
            right_normalized = _normalized_object_name(right.display_name)
            same_business_line = (
                not left.business_line
                or not right.business_line
                or left.business_line == right.business_line
            )
            same_key = (
                (left.canonical_key and right.canonical_key and left.canonical_key == right.canonical_key)
                or (left_normalized and right_normalized and left_normalized == right_normalized)
            )
            fuzzy_name = left_name == right_name or left_name in right_name or right_name in left_name
            if (same_key or fuzzy_name) and same_business_line:
                conflicts.append({
                    "left_id": left.id,
                    "left_name": left.display_name,
                    "right_id": right.id,
                    "right_name": right.display_name,
                    "object_type_id": left.object_type_id,
                    "reason": "名称或 canonical key 高度相似，疑似重复对象",
                })
    return conflicts[:50]


@router.get("/gaps/overview")
def get_governance_gap_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    pending_suggestions = db.query(GovernanceSuggestionTask).filter(GovernanceSuggestionTask.status == "pending").count()
    objects = db.query(GovernanceObject).all()
    object_gaps = []
    for item in objects:
        facets = db.query(GovernanceObjectFacet).filter(GovernanceObjectFacet.governance_object_id == item.id).all()
        linked_knowledge_count = db.query(KnowledgeEntry).filter(KnowledgeEntry.governance_object_id == item.id).count()
        linked_table_count = db.query(BusinessTable).filter(BusinessTable.governance_object_id == item.id).count()
        linked_project_count = 0
        linked_task_count = db.query(Task).filter(Task.governance_object_id == item.id).count()
        if not facets:
            object_gaps.append({
                "object_id": item.id,
                "display_name": item.display_name,
                "gap_type": "no_facet",
                "reason": "对象已创建但还没有任何资源库视角",
                "recommended_actions": _recommended_actions_for_gap(
                    "no_facet",
                    object_id=item.id,
                    linked_knowledge_count=linked_knowledge_count,
                    linked_table_count=linked_table_count,
                    linked_project_count=linked_project_count,
                    linked_task_count=linked_task_count,
                ),
            })
        if linked_knowledge_count == 0 and linked_table_count == 0 and linked_project_count == 0 and linked_task_count == 0:
            object_gaps.append({
                "object_id": item.id,
                "display_name": item.display_name,
                "gap_type": "orphan_object",
                "reason": "对象未关联任何知识文档或数据表",
                "recommended_actions": _recommended_actions_for_gap(
                    "orphan_object",
                    object_id=item.id,
                    linked_knowledge_count=linked_knowledge_count,
                    linked_table_count=linked_table_count,
                    linked_project_count=linked_project_count,
                    linked_task_count=linked_task_count,
                ),
            })
            continue
        if linked_knowledge_count > 0 and linked_table_count == 0:
            object_gaps.append({
                "object_id": item.id,
                "display_name": item.display_name,
                "gap_type": "missing_table",
                "reason": "对象已有知识文档，但还没有结构化数据表支撑",
                "recommended_actions": _recommended_actions_for_gap(
                    "missing_table",
                    object_id=item.id,
                    linked_knowledge_count=linked_knowledge_count,
                    linked_table_count=linked_table_count,
                    linked_project_count=linked_project_count,
                    linked_task_count=linked_task_count,
                ),
            })
        if linked_table_count > 0 and linked_knowledge_count == 0:
            object_gaps.append({
                "object_id": item.id,
                "display_name": item.display_name,
                "gap_type": "missing_knowledge",
                "reason": "对象已有数据表，但没有文档说明、案例或 SOP 支撑",
                "recommended_actions": _recommended_actions_for_gap(
                    "missing_knowledge",
                    object_id=item.id,
                    linked_knowledge_count=linked_knowledge_count,
                    linked_table_count=linked_table_count,
                    linked_project_count=linked_project_count,
                    linked_task_count=linked_task_count,
                ),
            })
        if (linked_knowledge_count > 0 or linked_table_count > 0) and linked_project_count == 0 and linked_task_count == 0:
            object_gaps.append({
                "object_id": item.id,
                "display_name": item.display_name,
                "gap_type": "missing_execution_link",
                "reason": "对象已有知识或数据，但还没挂到项目/任务，执行协同链路未建立",
                "recommended_actions": _recommended_actions_for_gap(
                    "missing_execution_link",
                    object_id=item.id,
                    linked_knowledge_count=linked_knowledge_count,
                    linked_table_count=linked_table_count,
                    linked_project_count=linked_project_count,
                    linked_task_count=linked_task_count,
                ),
            })
    return {
        "pending_suggestions": pending_suggestions,
        "object_gaps": object_gaps[:100],
        "conflict_count": len(list_governance_object_conflicts(object_type_code=None, db=db, user=user)),
    }


@router.get("/strategy-stats")
def list_governance_strategy_stats(
    subject_type: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    q = db.query(GovernanceStrategyStat)
    if subject_type:
        q = q.filter(GovernanceStrategyStat.subject_type == subject_type)
    items = q.order_by(
        GovernanceStrategyStat.cumulative_reward.desc(),
        GovernanceStrategyStat.success_count.desc(),
        GovernanceStrategyStat.total_count.desc(),
    ).limit(limit).all()
    return [
        {
            "id": item.id,
            "strategy_key": item.strategy_key,
            "strategy_group": item.strategy_group,
            "subject_type": item.subject_type,
            "objective_code": item.objective_code,
            "library_code": item.library_code,
            "department_id": item.department_id,
            "business_line": item.business_line,
            "is_frozen": item.is_frozen,
            "manual_bias": item.manual_bias,
            "total_count": item.total_count,
            "success_count": item.success_count,
            "reject_count": item.reject_count,
            "cumulative_reward": item.cumulative_reward,
            "last_reward": item.last_reward,
            "success_rate": round((item.success_count or 0) / max(item.total_count or 1, 1), 4),
            "last_event_at": item.last_event_at.isoformat() if item.last_event_at else None,
        }
        for item in items
    ]


@router.get("/strategy-risk-stats")
def list_governance_strategy_risk_stats(
    subject_type: str | None = None,
    min_samples: int = 3,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    q = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.total_count >= max(1, min_samples))
    if subject_type:
        q = q.filter(GovernanceStrategyStat.subject_type == subject_type)
    items = q.order_by(
        GovernanceStrategyStat.cumulative_reward.asc(),
        GovernanceStrategyStat.reject_count.desc(),
        GovernanceStrategyStat.total_count.desc(),
    ).limit(limit).all()
    return [
        {
            "id": item.id,
            "strategy_key": item.strategy_key,
            "strategy_group": item.strategy_group,
            "subject_type": item.subject_type,
            "objective_code": item.objective_code,
            "library_code": item.library_code,
            "department_id": item.department_id,
            "business_line": item.business_line,
            "is_frozen": item.is_frozen,
            "manual_bias": item.manual_bias,
            "total_count": item.total_count,
            "success_count": item.success_count,
            "reject_count": item.reject_count,
            "cumulative_reward": item.cumulative_reward,
            "last_reward": item.last_reward,
            "success_rate": round((item.success_count or 0) / max(item.total_count or 1, 1), 4),
            "last_event_at": item.last_event_at.isoformat() if item.last_event_at else None,
        }
        for item in items
        if (item.success_count or 0) / max(item.total_count or 1, 1) < 0.6
    ]


@router.get("/feedback-events")
def list_governance_feedback_events(
    strategy_key: str | None = None,
    subject_type: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    q = db.query(GovernanceFeedbackEvent)
    if strategy_key:
        q = q.filter(GovernanceFeedbackEvent.strategy_key == strategy_key)
    if subject_type:
        q = q.filter(GovernanceFeedbackEvent.subject_type == subject_type)
    items = q.order_by(GovernanceFeedbackEvent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": item.id,
            "suggestion_id": item.suggestion_id,
            "subject_type": item.subject_type,
            "subject_id": item.subject_id,
            "strategy_key": item.strategy_key,
            "event_type": item.event_type,
            "reward_score": item.reward_score,
            "from_objective_id": item.from_objective_id,
            "from_resource_library_id": item.from_resource_library_id,
            "to_objective_id": item.to_objective_id,
            "to_resource_library_id": item.to_resource_library_id,
            "note": item.note,
            "created_by": item.created_by,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in items
    ]


@router.post("/strategy-stats/{stat_id}/tune")
def tune_governance_strategy_stat(
    stat_id: int,
    req: GovernanceStrategyTuneRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    stat = db.get(GovernanceStrategyStat, stat_id)
    if not stat:
        raise HTTPException(404, "策略统计不存在")
    if req.is_frozen is not None:
        stat.is_frozen = req.is_frozen
    if req.manual_bias is not None:
        stat.manual_bias = max(-30, min(30, req.manual_bias))
    db.commit()
    return {"ok": True}


@router.post("/objects/merge")
def merge_governance_objects(
    req: MergeGovernanceObjectsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    if req.source_object_id == req.target_object_id:
        raise HTTPException(400, "不能合并同一个对象")
    source = db.get(GovernanceObject, req.source_object_id)
    target = db.get(GovernanceObject, req.target_object_id)
    if not source or not target:
        raise HTTPException(404, "治理对象不存在")
    if source.object_type_id != target.object_type_id:
        raise HTTPException(400, "仅支持同对象类型合并")

    db.query(KnowledgeEntry).filter(KnowledgeEntry.governance_object_id == source.id).update(
        {KnowledgeEntry.governance_object_id: target.id},
        synchronize_session=False,
    )
    db.query(BusinessTable).filter(BusinessTable.governance_object_id == source.id).update(
        {BusinessTable.governance_object_id: target.id},
        synchronize_session=False,
    )
    db.query(Task).filter(Task.governance_object_id == source.id).update(
        {Task.governance_object_id: target.id},
        synchronize_session=False,
    )

    source_facets = db.query(GovernanceObjectFacet).filter(GovernanceObjectFacet.governance_object_id == source.id).all()
    for facet in source_facets:
        exists = (
            db.query(GovernanceObjectFacet)
            .filter(
                GovernanceObjectFacet.governance_object_id == target.id,
                GovernanceObjectFacet.resource_library_id == facet.resource_library_id,
                GovernanceObjectFacet.facet_key == facet.facet_key,
            )
            .first()
        )
        if exists:
            continue
        facet.governance_object_id = target.id

    source_payload = dict(source.object_payload or {})
    source_payload["feedback_score"] = int(source_payload.get("feedback_score") or 0) - 3
    source_payload["merged_into"] = target.id
    source.object_payload = source_payload
    source.lifecycle_status = "merged"
    db.commit()
    return {"ok": True, "target_object_id": target.id}


@router.post("/objects")
def create_governance_object(
    req: GovernanceObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    item = ensure_governance_object(
        db,
        object_type_code=req.object_type_code,
        canonical_key=req.canonical_key,
        display_name=req.display_name,
        business_line=req.business_line,
        department_id=req.department_id,
        owner_id=req.owner_id,
    )
    if not item:
        raise HTTPException(400, "对象类型不存在")
    return {
        "id": item.id,
        "object_type_id": item.object_type_id,
        "canonical_key": item.canonical_key,
        "display_name": item.display_name,
        "business_line": item.business_line,
        "department_id": item.department_id,
        "owner_id": item.owner_id,
        "lifecycle_status": item.lifecycle_status,
    }


@router.post("/bind-object")
def bind_subject_to_governance_object(
    req: BindGovernanceObjectRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    subject = _load_subject(db, req.subject_type, req.subject_id)
    if not subject:
        raise HTTPException(404, "对象不存在")
    gov_object = db.get(GovernanceObject, req.governance_object_id)
    if not gov_object:
        raise HTTPException(404, "治理对象不存在")

    previous_object_id = getattr(subject, "governance_object_id", None)
    if previous_object_id and previous_object_id != gov_object.id:
        previous_object = db.get(GovernanceObject, previous_object_id)
        if previous_object:
            previous_payload = dict(previous_object.object_payload or {})
            previous_payload["feedback_score"] = int(previous_payload.get("feedback_score") or 0) - 2
            previous_payload["rebind_away_count"] = int(previous_payload.get("rebind_away_count") or 0) + 1
            previous_object.object_payload = previous_payload

    if hasattr(subject, "governance_object_id"):
        subject.governance_object_id = gov_object.id
    if hasattr(subject, "object_type_id"):
        subject.object_type_id = gov_object.object_type_id
    if hasattr(subject, "governance_status"):
        subject.governance_status = "aligned"

    payload = dict(gov_object.object_payload or {})
    payload["bind_count"] = int(payload.get("bind_count") or 0) + 1
    payload["feedback_score"] = int(payload.get("feedback_score") or 0) + 2
    payload["last_bound_subject_type"] = req.subject_type
    payload["last_bound_subject_id"] = req.subject_id
    gov_object.object_payload = payload

    library_id = getattr(subject, "resource_library_id", None)
    if library_id:
        facet_key = f"{req.subject_type}:{req.subject_id}"
        facet = (
            db.query(GovernanceObjectFacet)
            .filter(
                GovernanceObjectFacet.governance_object_id == gov_object.id,
                GovernanceObjectFacet.resource_library_id == library_id,
                GovernanceObjectFacet.facet_key == facet_key,
            )
            .first()
        )
        if not facet:
            facet = GovernanceObjectFacet(
                governance_object_id=gov_object.id,
                resource_library_id=library_id,
                facet_key=facet_key,
                facet_name=req.facet_name or getattr(subject, "display_name", None) or getattr(subject, "title", None) or facet_key,
                field_values={},
                consumer_scenarios=[],
                visibility_mode=req.visibility_mode,
                is_editable=req.is_editable,
                update_cycle=req.update_cycle,
                source_subjects=[{"type": req.subject_type, "id": req.subject_id}],
            )
            db.add(facet)

    pending = (
        db.query(GovernanceSuggestionTask)
        .filter(
            GovernanceSuggestionTask.subject_type == req.subject_type,
            GovernanceSuggestionTask.subject_id == req.subject_id,
            GovernanceSuggestionTask.status == "pending",
        )
        .all()
    )
    for item in pending:
        item.status = "applied"
        item.resolved_by = user.id
        item.resolved_note = "对象绑定已应用"
        item.resolved_at = datetime.datetime.utcnow()

    db.commit()
    return {"ok": True, "governance_object_id": gov_object.id}


@router.post("/business-table/{table_id}/suggest")
def generate_governance_suggestion_for_business_table(
    table_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    table = db.get(BusinessTable, table_id)
    if not table:
        raise HTTPException(404, "数据表不存在")
    task = create_or_update_governance_suggestion_for_table(db, table, created_by=user.id)
    if not task:
        return {"ok": True, "created": False, "message": "未命中治理建议规则"}
    return {"ok": True, "created": True, "suggestion": _suggestion_dict(task)}
