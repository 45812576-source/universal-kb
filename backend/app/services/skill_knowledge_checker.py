"""Skill 知识引用校验服务。

发布前校验 Skill 引用的知识文件是否在创建者管理范围内，
并构建脱敏快照数据供 SkillKnowledgeReference 写入。
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_admin import KnowledgeFolderGrant
from app.models.knowledge_understanding import KnowledgeUnderstandingProfile
from app.models.skill import Skill
from app.models.user import Role, User
from app.data.sensitivity_rules import DATA_TYPE_REGISTRY

logger = logging.getLogger(__name__)


def check_folder_management_scope(user_id: int, folder_id: Optional[int], db: Session) -> bool:
    """检查用户是否有目录管理权限（复用 _require_folder_grant 逻辑，返回 bool）。"""
    if folder_id is None:
        return True

    user = db.get(User, user_id)
    if not user:
        return False
    if user.role == Role.SUPER_ADMIN:
        return True

    # 用户自建目录
    folder = db.get(KnowledgeFolder, folder_id)
    if not folder:
        return False
    if folder.created_by == user_id:
        return True

    # 沿祖先链查 KnowledgeFolderGrant
    current_id: Optional[int] = folder_id
    visited: set[int] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        grant = db.query(KnowledgeFolderGrant).filter(
            KnowledgeFolderGrant.folder_id == current_id,
            KnowledgeFolderGrant.grantee_user_id == user_id,
        ).first()
        if grant:
            return True
        f = db.get(KnowledgeFolder, current_id)
        if not f:
            break
        current_id = f.parent_id

    return False


def _build_folder_path(folder_id: Optional[int], db: Session) -> str:
    """沿祖先链拼接 /A/B/C 形式的目录路径。"""
    if not folder_id:
        return ""
    parts: list[str] = []
    current_id: Optional[int] = folder_id
    visited: set[int] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        folder = db.get(KnowledgeFolder, current_id)
        if not folder:
            break
        parts.append(folder.name)
        current_id = folder.parent_id
    parts.reverse()
    return "/" + "/".join(parts) if parts else ""


def _build_effective_mask_rules(data_type_hits: list[dict]) -> list[dict]:
    """根据命中的数据类型从 DATA_TYPE_REGISTRY 读取脱敏规则。"""
    rules = []
    seen = set()
    for hit in data_type_hits:
        dtype = hit.get("type", "")
        if dtype in seen or dtype not in DATA_TYPE_REGISTRY:
            continue
        seen.add(dtype)
        reg = DATA_TYPE_REGISTRY[dtype]
        rules.append({
            "data_type": dtype,
            "label": reg.get("label", dtype),
            "mask_action": reg.get("default_mask_action", "keep"),
            "desensitization_level": reg.get("default_desensitization_level", "D0"),
            "display_rule": reg.get("display_rule", ""),
            "summary_rule": reg.get("summary_rule", ""),
        })
    return rules


def validate_skill_knowledge_references(
    skill_id: int,
    user_id: int,
    db: Session,
) -> dict:
    """校验 Skill 引用的所有知识文件，返回完整审查结果。

    Returns:
        {
            "blocked": bool,
            "block_reasons": [...],
            "references": [...],
            "risk_summary": {...},
            "policy_snapshot": {...},
        }
    """
    skill = db.get(Skill, skill_id)
    if not skill:
        return {"blocked": True, "block_reasons": ["Skill 不存在"], "references": [], "risk_summary": {}, "policy_snapshot": {}}

    tags = skill.knowledge_tags or []
    if not tags:
        return {
            "blocked": False,
            "block_reasons": [],
            "references": [],
            "risk_summary": {
                "high_sensitivity_count": 0,
                "missing_mask_config_count": 0,
                "out_of_scope_count": 0,
                "unconfirmed_count": 0,
            },
            "policy_snapshot": {},
        }

    # 通过 tags 匹配知识条目（复用现有 tag 过滤逻辑）
    from sqlalchemy import or_
    tag_filters = []
    for tag in tags:
        tag_filters.append(KnowledgeEntry.industry_tags.contains(f'"{tag}"'))
        tag_filters.append(KnowledgeEntry.platform_tags.contains(f'"{tag}"'))
        tag_filters.append(KnowledgeEntry.topic_tags.contains(f'"{tag}"'))
        tag_filters.append(KnowledgeEntry.linked_skill_codes.contains(f'"{tag}"'))
        tag_filters.append(KnowledgeEntry.serving_skill_codes.contains(f'"{tag}"'))

    entries = db.query(KnowledgeEntry).filter(or_(*tag_filters)).all() if tag_filters else []

    block_reasons: list[str] = []
    references: list[dict] = []
    high_sensitivity_count = 0
    missing_mask_config_count = 0
    out_of_scope_count = 0
    unconfirmed_count = 0

    for entry in entries:
        # 查 KnowledgeUnderstandingProfile
        profile = db.query(KnowledgeUnderstandingProfile).filter(
            KnowledgeUnderstandingProfile.knowledge_id == entry.id
        ).first()

        if not profile:
            missing_mask_config_count += 1
            block_reasons.append(f"「{entry.title}」无文档理解结果，无法确定脱敏策略")
            references.append({
                "knowledge_id": entry.id,
                "title": entry.title or entry.source_file or f"ID-{entry.id}",
                "folder_id": entry.folder_id,
                "folder_path": _build_folder_path(entry.folder_id, db),
                "document_type": None,
                "permission_domain": None,
                "desensitization_level": None,
                "data_type_hits": [],
                "effective_mask_rules": [],
                "mask_rule_source": None,
                "manager_scope_ok": False,
            })
            continue

        desens_level = profile.desensitization_level or "D0"
        data_type_hits = profile.data_type_hits or []
        document_type = profile.document_type
        permission_domain = profile.permission_domain
        confirmed = profile.confirmed_at is not None
        mask_source = profile.masking_source or "rule"

        # 管理范围检查
        scope_ok = check_folder_management_scope(user_id, entry.folder_id, db)
        if not scope_ok:
            out_of_scope_count += 1
            folder_path = _build_folder_path(entry.folder_id, db)
            block_reasons.append(f"无权管理「{entry.title}」的脱敏策略（目录：{folder_path}）")

        # 高敏感度统计
        if desens_level in ("D3", "D4"):
            high_sensitivity_count += 1

        # 未确认统计
        if not confirmed:
            unconfirmed_count += 1

        effective_rules = _build_effective_mask_rules(data_type_hits)
        folder_path = _build_folder_path(entry.folder_id, db)

        references.append({
            "knowledge_id": entry.id,
            "title": profile.display_title or entry.title or entry.source_file or f"ID-{entry.id}",
            "folder_id": entry.folder_id,
            "folder_path": folder_path,
            "document_type": document_type,
            "permission_domain": permission_domain,
            "desensitization_level": desens_level,
            "data_type_hits": [
                {"type": h.get("type", ""), "label": h.get("label", h.get("type", "")), "count": h.get("count", 0)}
                for h in data_type_hits
            ],
            "effective_mask_rules": effective_rules,
            "mask_rule_source": mask_source,
            "manager_scope_ok": scope_ok,
        })

    blocked = out_of_scope_count > 0 or missing_mask_config_count > 0

    return {
        "blocked": blocked,
        "block_reasons": block_reasons,
        "references": references,
        "risk_summary": {
            "high_sensitivity_count": high_sensitivity_count,
            "missing_mask_config_count": missing_mask_config_count,
            "out_of_scope_count": out_of_scope_count,
            "unconfirmed_count": unconfirmed_count,
        },
        "policy_snapshot": {
            "skill_id": skill_id,
            "knowledge_tags": tags,
            "total_references": len(references),
        },
    }
