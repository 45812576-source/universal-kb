"""Skill Studio 权限治理 API."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api_envelope import raise_api_error
from app.database import get_db
from app.dependencies import get_current_user
from app.models.skill_governance import (
    PermissionDeclarationDraft,
    RoleAssetGranularRule,
    RoleAssetPolicy,
    RolePolicyBundle,
    SkillGovernanceJob,
    SkillRoleAssetMountOverride,
    SkillRoleKnowledgeOverride,
    SkillRolePackage,
    SkillBoundAsset,
    SkillServiceRole,
    TestCaseDraft,
    TestCasePlanDraft,
)
from app.models.user import User
from app.services.skill_governance_service import (
    active_assets,
    active_roles,
    assert_skill_governance_access,
    build_mount_context,
    build_mounted_permissions,
    bump_governance_version,
    declaration_source_mode,
    ensure_permission_declaration_prompt_sync,
    find_position,
    find_role_by_role_key,
    generate_declaration,
    generate_permission_case_plan,
    granular_rule_is_high_risk,
    materialize_permission_case_plan,
    mark_declaration_stale,
    mark_case_plan_stale,
    mount_permission_declaration_to_skill,
    granular_rule_requires_override,
    goal_summary_for_position,
    latest_bundle,
    latest_case_plan,
    latest_declaration,
    mark_downstream_stale,
    ok,
    permission_case_plan_readiness,
    permission_case_plan_state,
    permission_contract_review_summary,
    resolve_workspace_id,
    serialize_asset,
    serialize_bundle,
    serialize_case_draft,
    serialize_case_plan,
    serialize_declaration,
    serialize_granular_rule,
    serialize_policy,
    serialize_role,
    role_key_for_role,
    split_org_path,
    suggest_role_asset_policies,
    sync_bound_assets,
    update_declaration_text,
)
from app.services.skill_governance_jobs import (
    create_governance_job,
    process_governance_job,
    serialize_governance_job,
    session_factory_for_current_bind,
)

router = APIRouter(prefix="/api/skill-governance", tags=["skill-governance"])


def _legacy_governance_replacements(skill_id: int) -> list[str]:
    return [
        f"/api/skill-governance/{skill_id}/mount-context",
        f"/api/skill-governance/{skill_id}/mounted-permissions",
        f"/api/skill-governance/{skill_id}/declarations/generate",
        f"/api/sandbox-case-plans/{skill_id}/generate",
    ]


def _raise_legacy_governance_write_deprecated(
    skill_id: int,
    endpoint: str,
    **extra: object,
) -> None:
    details = {
        "skill_id": skill_id,
        "endpoint": endpoint,
        "source_mode": "domain_projection",
        "historical_access": "read_only",
        "replacement_endpoints": _legacy_governance_replacements(skill_id),
        **extra,
    }
    raise_api_error(
        410,
        "governance.legacy_write_deprecated",
        "旧治理写入接口已冻结，请改用源域权限投影与声明/测试计划流程",
        details,
    )


class ServiceRoleInput(BaseModel):
    org_path: str = Field(min_length=1, max_length=512)
    position_name: str = Field(min_length=1, max_length=128)
    position_level: Optional[str] = None
    role_label: Optional[str] = None
    goal_summary: Optional[str] = None


class SaveServiceRolesRequest(BaseModel):
    roles: list[ServiceRoleInput] = []


class SuggestPoliciesRequest(BaseModel):
    mode: str = "initial"
    bundle_scope: str = "latest"
    async_job: bool = False


class ConfirmRoleAssetPolicyItem(BaseModel):
    id: int
    allowed: Optional[bool] = None
    default_output_style: Optional[str] = None
    insufficient_evidence_behavior: Optional[str] = None
    allowed_question_types: Optional[list[str]] = None
    forbidden_question_types: Optional[list[str]] = None


class ConfirmRoleAssetPoliciesRequest(BaseModel):
    bundle_id: int
    policies: list[ConfirmRoleAssetPolicyItem] = []


class UpdateRoleAssetPolicyRequest(BaseModel):
    allowed: Optional[bool] = None
    default_output_style: Optional[str] = None
    insufficient_evidence_behavior: Optional[str] = None
    allowed_question_types: Optional[list[str]] = None
    forbidden_question_types: Optional[list[str]] = None
    review_status: Optional[str] = None
    risk_level: Optional[str] = None


class GenerateDeclarationRequest(BaseModel):
    bundle_id: Optional[int] = None
    async_job: bool = False


class SuggestGranularRulesRequest(BaseModel):
    bundle_id: int
    risk_only: bool = True


class UpdateDeclarationRequest(BaseModel):
    text: str = Field(min_length=1)
    status: str = "edited"


class AdoptDeclarationRequest(BaseModel):
    action: str = Field(default="confirm", pattern="^(confirm|edit)$")
    edited_text: Optional[str] = None


class UpdateGranularRuleRequest(BaseModel):
    suggested_policy: Optional[str] = Field(default=None, min_length=1, max_length=32)
    mask_style: Optional[str] = Field(default=None, max_length=32)
    confirmed: Optional[bool] = None
    author_override_reason: Optional[str] = None


class ConfirmGranularRuleItem(BaseModel):
    id: int
    suggested_policy: Optional[str] = Field(default=None, min_length=1, max_length=32)
    mask_style: Optional[str] = Field(default=None, max_length=32)
    confirmed: Optional[bool] = None
    author_override_reason: Optional[str] = None


class ConfirmGranularRulesRequest(BaseModel):
    bundle_id: int
    rules: list[ConfirmGranularRuleItem] = []


class GeneratePermissionCasePlanRequest(BaseModel):
    focus_mode: str = "risk_focused"
    max_cases: int = Field(default=12, ge=1, le=50)
    async_job: bool = False


class UpdateCaseDraftRequest(BaseModel):
    status: Optional[str] = None
    prompt: Optional[str] = Field(default=None, min_length=1)
    expected_behavior: Optional[str] = Field(default=None, min_length=1)
    source_verification_status: Optional[str] = Field(default=None, min_length=1, max_length=32)


class RolePackageRoleInput(BaseModel):
    org_path: str = Field(min_length=1, max_length=512)
    position_name: str = Field(min_length=1, max_length=128)
    position_level: str = ""
    role_label: str = Field(min_length=1, max_length=256)


class RolePackageFieldRuleInput(BaseModel):
    policy_id: int
    rule_id: int
    asset_id: int
    target_ref: str = Field(min_length=1, max_length=255)
    suggested_policy: str = Field(min_length=1, max_length=32)
    mask_style: Optional[str] = Field(default=None, max_length=32)
    confirmed: bool = False
    author_override_reason: Optional[str] = None


class RolePackageKnowledgeInput(BaseModel):
    asset_id: int
    asset_ref: str = Field(min_length=1, max_length=128)
    knowledge_id: int
    desensitization_level: str = Field(default="inherit", min_length=1, max_length=32)
    grant_actions: list[str] = []
    enabled: bool = True
    source_refs: list[dict[str, object]] = []


class RolePackageAssetMountInput(BaseModel):
    asset_id: int
    asset_ref_type: str = Field(min_length=1, max_length=32)
    asset_ref_id: int
    binding_mode: str = Field(min_length=1, max_length=32)
    enabled: bool = True


class RolePackageContentInput(BaseModel):
    field_rules: list[RolePackageFieldRuleInput] = []
    knowledge_permissions: list[RolePackageKnowledgeInput] = []
    asset_mounts: list[RolePackageAssetMountInput] = []


class SaveRolePackageRequest(BaseModel):
    role_key: str = Field(min_length=1, max_length=768)
    role: RolePackageRoleInput
    writeback_mode: str = Field(default="upsert_role_package", pattern="^upsert_role_package$")
    stale_downstream: list[str] = []
    package: RolePackageContentInput


def _serialize_role_package(package: SkillRolePackage) -> dict[str, object]:
    return {
        "id": package.id,
        "skill_id": package.skill_id,
        "role_key": package.role_key,
        "role": {
            "org_path": package.org_path,
            "position_name": package.position_name,
            "position_level": package.position_level or "",
            "role_label": package.role_label,
        },
        "package_version": package.package_version,
        "governance_version": package.governance_version,
        "status": package.status,
        "field_rules": package.field_rules_json or [],
        "knowledge_permissions": [
            {
                "asset_id": item.asset_id,
                "asset_ref": item.asset_ref,
                "knowledge_id": item.knowledge_id,
                "desensitization_level": item.desensitization_level,
                "grant_actions": item.grant_actions_json or [],
                "enabled": bool(item.enabled),
                "source_refs": item.source_refs_json or [],
            }
            for item in package.knowledge_overrides
        ],
        "asset_mounts": [
            {
                "asset_id": item.asset_id,
                "asset_ref_type": item.asset_ref_type,
                "asset_ref_id": item.asset_ref_id,
                "binding_mode": item.binding_mode,
                "enabled": bool(item.enabled),
            }
            for item in package.asset_overrides
        ],
        "source_projection_version": package.source_projection_version,
        "updated_at": package.updated_at.isoformat() if package.updated_at else None,
    }


@router.get("/{skill_id}/summary")
def get_governance_summary(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    assets, changed = sync_bound_assets(db, skill, workspace_id)
    if changed:
        db.commit()
    bundle = latest_bundle(db, skill_id)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    if declaration_changed:
        db.commit()
    roles = active_roles(db, skill_id)
    mount_context = build_mount_context(db, skill)
    blocking_issues: list[str] = []
    if not roles:
        blocking_issues.append("missing_service_roles")
    if not assets:
        blocking_issues.append("missing_bound_assets")
    if declaration and declaration_source_mode(declaration) == "legacy_bundle" and (not bundle or not bundle.policies):
        blocking_issues.append("missing_role_asset_policies")
    blocking_issues.extend(mount_context["permission_summary"]["blocking_issues"])
    if not declaration or declaration.status == "stale":
        blocking_issues.append("missing_confirmed_declaration")
    return ok({
        "skill_id": skill_id,
        "governance_version": mount_context["projection_version"] if mount_context else (bundle.governance_version if bundle else 0),
        "bundle": serialize_bundle(bundle),
        "declaration": serialize_declaration(declaration),
        "summary": {
            "service_role_count": len(roles),
            "bound_asset_count": len(assets),
            "blocking_issues": sorted(set(blocking_issues)),
            "stale": bool((bundle and bundle.status == "stale") or (declaration and declaration.status == "stale")),
        },
    })


@router.get("/{skill_id}/service-roles")
def get_service_roles(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    bundle = latest_bundle(db, skill_id)
    roles = active_roles(db, skill_id)
    return ok({
        "skill_id": skill_id,
        "governance_version": bundle.governance_version if bundle else 0,
        "roles": [serialize_role(role) for role in roles],
    })


@router.put("/{skill_id}/service-roles")
def save_service_roles(
    skill_id: int,
    req: SaveServiceRolesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    requested_keys: set[tuple[str, str, str]] = set()
    normalized_roles: list[ServiceRoleInput] = []
    for item in req.roles:
        org_path = item.org_path.strip()
        position_name = item.position_name.strip()
        position_level = (item.position_level or "").strip()
        key = (org_path, position_name, position_level)
        if key in requested_keys:
            continue
        requested_keys.add(key)
        normalized_roles.append(ServiceRoleInput(
            org_path=org_path,
            position_name=position_name,
            position_level=position_level,
            role_label=item.role_label,
            goal_summary=item.goal_summary,
        ))

    existing = db.query(SkillServiceRole).filter(SkillServiceRole.skill_id == skill_id).all()
    existing_by_key = {
        (role.org_path, role.position_name, role.position_level or ""): role for role in existing
    }
    for role in existing:
        if (role.org_path, role.position_name, role.position_level or "") not in requested_keys and role.status == "active":
            role.status = "inactive"
            role.updated_by = user.id

    for item in normalized_roles:
        position = find_position(db, item.position_name, item.org_path)
        auto_goal_summary, goal_refs, source_dataset = goal_summary_for_position(position)
        org_parts = split_org_path(item.org_path)
        label = item.role_label or f"{item.position_name}{f'（{item.position_level}）' if item.position_level else ''}"
        key = (item.org_path, item.position_name, item.position_level or "")
        role = existing_by_key.get(key)
        payload = {
            "workspace_id": workspace_id,
            "org_path": item.org_path,
            "position_name": item.position_name,
            "position_level": item.position_level or "",
            "role_label": label,
            "goal_summary": item.goal_summary or auto_goal_summary,
            "goal_refs_json": goal_refs,
            "source_dataset": source_dataset,
            "status": "active",
            "updated_by": user.id,
            **org_parts,
        }
        if role:
            for field, value in payload.items():
                setattr(role, field, value)
        else:
            db.add(SkillServiceRole(
                skill_id=skill_id,
                created_by=user.id,
                **payload,
            ))

    stale_downstream = mark_downstream_stale(db, skill_id, ["service_roles_changed"])
    db.commit()
    bundle = latest_bundle(db, skill_id)
    return ok({
        "governance_version": (bundle.governance_version if bundle else 0),
        "bundle_status": (bundle.status if bundle else "draft"),
        "stale_downstream": sorted(set(stale_downstream)),
        "roles": [serialize_role(role) for role in active_roles(db, skill_id)],
    })


@router.get("/{skill_id}/role-packages")
def list_role_packages(
    skill_id: int,
    include_projection: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    packages = (
        db.query(SkillRolePackage)
        .filter(SkillRolePackage.skill_id == skill_id)
        .order_by(SkillRolePackage.role_label, SkillRolePackage.id)
        .all()
    )
    data: dict[str, object] = {
        "skill_id": skill_id,
        "packages": [_serialize_role_package(package) for package in packages],
    }
    if include_projection:
        data["projection_roles"] = [serialize_role(role) for role in active_roles(db, skill_id)]
    return ok(data)


@router.put("/{skill_id}/role-packages/{role_key:path}")
def save_role_package(
    skill_id: int,
    role_key: str,
    req: SaveRolePackageRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    request_role_key = req.role_key.strip()
    if request_role_key != role_key.strip():
        raise_api_error(
            400,
            "governance.role_package_key_mismatch",
            "路径中的角色 key 与请求体不一致",
            {"path_role_key": role_key, "body_role_key": req.role_key},
        )
    role = find_role_by_role_key(db, skill_id, request_role_key)
    if not role:
        raise_api_error(
            404,
            "governance.service_role_not_found",
            "角色 package 对应的服务角色不存在",
            {"skill_id": skill_id, "role_key": request_role_key},
        )

    field_rule_payloads: list[dict[str, object]] = []
    for item in req.package.field_rules:
        rule = db.get(RoleAssetGranularRule, item.rule_id)
        if not rule or rule.policy.bundle.skill_id != skill_id:
            raise_api_error(
                404,
                "governance.granular_rule_not_found",
                "字段规则不存在或不属于当前 Skill",
                {"skill_id": skill_id, "rule_id": item.rule_id},
            )
        if rule.policy.id != item.policy_id or rule.policy.asset.id != item.asset_id:
            raise_api_error(
                400,
                "governance.granular_rule_ref_mismatch",
                "字段规则引用与当前策略不一致",
                {"rule_id": item.rule_id, "policy_id": item.policy_id, "asset_id": item.asset_id},
            )
        if role_key_for_role(rule.policy.role) != request_role_key:
            raise_api_error(
                400,
                "governance.granular_rule_role_mismatch",
                "字段规则不属于当前角色 package",
                {"rule_id": item.rule_id, "role_key": request_role_key},
            )
        if granular_rule_requires_override(rule, item.suggested_policy, item.mask_style) and not (item.author_override_reason or "").strip():
            raise_api_error(
                400,
                "governance.override_reason_required",
                "高风险字段放开原值或取消脱敏时必须填写原因",
                {"rule_id": item.rule_id, "target_ref": item.target_ref},
            )
        rule.suggested_policy = item.suggested_policy
        rule.mask_style = item.mask_style
        rule.confirmed = item.confirmed
        rule.author_override_reason = (item.author_override_reason or "").strip() or None
        field_rule_payloads.append(item.model_dump())

    knowledge_asset_ids = {item.asset_id for item in req.package.knowledge_permissions}
    asset_mount_ids = {item.asset_id for item in req.package.asset_mounts}
    all_asset_ids = knowledge_asset_ids | asset_mount_ids
    if all_asset_ids:
        existing_assets = {
            asset.id: asset
            for asset in db.query(SkillBoundAsset)
            .filter(SkillBoundAsset.skill_id == skill_id, SkillBoundAsset.id.in_(all_asset_ids))
            .all()
        }
        missing_asset_ids = sorted(all_asset_ids - set(existing_assets))
        if missing_asset_ids:
            raise_api_error(
                404,
                "governance.package_asset_not_found",
                "package 中包含不存在或不属于当前 Skill 的资产",
                {"skill_id": skill_id, "asset_ids": missing_asset_ids},
            )
    else:
        existing_assets = {}

    governance_version = bump_governance_version(
        db,
        skill,
        user,
        workspace_id,
        "role_package_changed",
    )
    package = (
        db.query(SkillRolePackage)
        .filter(SkillRolePackage.skill_id == skill_id, SkillRolePackage.role_key == request_role_key)
        .first()
    )
    if package:
        package.package_version = int(package.package_version or 0) + 1
        package.skill_service_role_id = role.id
        package.org_path = req.role.org_path.strip()
        package.position_name = req.role.position_name.strip()
        package.position_level = (req.role.position_level or "").strip()
        package.role_label = req.role.role_label.strip()
        package.field_rules_json = field_rule_payloads
        package.governance_version = governance_version
        package.source_projection_version = governance_version
        package.status = "active"
        package.updated_by = user.id
    else:
        package = SkillRolePackage(
            skill_id=skill_id,
            workspace_id=workspace_id,
            skill_service_role_id=role.id,
            role_key=request_role_key,
            org_path=req.role.org_path.strip(),
            position_name=req.role.position_name.strip(),
            position_level=(req.role.position_level or "").strip(),
            role_label=req.role.role_label.strip(),
            package_version=1,
            governance_version=governance_version,
            status="active",
            field_rules_json=field_rule_payloads,
            source_projection_version=governance_version,
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(package)
        db.flush()

    existing_knowledge = {
        item.knowledge_id: item
        for item in db.query(SkillRoleKnowledgeOverride)
        .filter(SkillRoleKnowledgeOverride.skill_id == skill_id, SkillRoleKnowledgeOverride.role_key == request_role_key)
        .all()
    }
    requested_knowledge_ids = {item.knowledge_id for item in req.package.knowledge_permissions}
    for knowledge_id, override in list(existing_knowledge.items()):
        if knowledge_id not in requested_knowledge_ids:
            db.delete(override)
    for item in req.package.knowledge_permissions:
        asset = existing_assets[item.asset_id]
        if asset.asset_type != "knowledge_base":
            raise_api_error(
                400,
                "governance.package_asset_type_mismatch",
                "知识遮蔽 package 只能引用知识库资产",
                {"asset_id": item.asset_id, "asset_type": asset.asset_type},
            )
        override = existing_knowledge.get(item.knowledge_id)
        payload = {
            "package_id": package.id,
            "skill_id": skill_id,
            "role_key": request_role_key,
            "asset_id": item.asset_id,
            "asset_ref": item.asset_ref,
            "knowledge_id": item.knowledge_id,
            "desensitization_level": item.desensitization_level,
            "grant_actions_json": item.grant_actions,
            "enabled": item.enabled,
            "source_refs_json": item.source_refs,
        }
        if override:
            for field, value in payload.items():
                setattr(override, field, value)
        else:
            db.add(SkillRoleKnowledgeOverride(**payload))

    existing_mounts = {
        item.asset_id: item
        for item in db.query(SkillRoleAssetMountOverride)
        .filter(SkillRoleAssetMountOverride.skill_id == skill_id, SkillRoleAssetMountOverride.role_key == request_role_key)
        .all()
    }
    requested_mount_asset_ids = {item.asset_id for item in req.package.asset_mounts}
    for asset_id, override in list(existing_mounts.items()):
        if asset_id not in requested_mount_asset_ids:
            db.delete(override)
    for item in req.package.asset_mounts:
        asset = existing_assets[item.asset_id]
        payload = {
            "package_id": package.id,
            "skill_id": skill_id,
            "role_key": request_role_key,
            "asset_id": item.asset_id,
            "asset_ref_type": item.asset_ref_type,
            "asset_ref_id": item.asset_ref_id,
            "binding_mode": item.binding_mode,
            "enabled": item.enabled,
        }
        if asset.asset_ref_type != item.asset_ref_type or asset.asset_ref_id != item.asset_ref_id:
            raise_api_error(
                400,
                "governance.package_asset_ref_mismatch",
                "资产挂载 package 引用与已绑定资产不一致",
                {"asset_id": item.asset_id, "asset_ref_type": item.asset_ref_type, "asset_ref_id": item.asset_ref_id},
            )
        override = existing_mounts.get(item.asset_id)
        if override:
            for field, value in payload.items():
                setattr(override, field, value)
        else:
            db.add(SkillRoleAssetMountOverride(**payload))

    stale_downstream = [
        "role_package",
        "mounted_permissions",
        "permission_declaration",
        "sandbox_case_plan",
    ]
    declaration = latest_declaration(db, skill_id)
    mark_declaration_stale(declaration, ["role_package_changed"])
    mark_case_plan_stale(db, skill_id, ["role_package_changed"])
    db.commit()
    db.refresh(package)
    return ok({
        "skill_id": skill_id,
        "role_key": request_role_key,
        "package_id": package.id,
        "package_version": package.package_version,
        "governance_version": governance_version,
        "stale_downstream": stale_downstream,
        "package": _serialize_role_package(package),
    })


@router.get("/{skill_id}/bound-assets")
def get_bound_assets(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    assets, changed = sync_bound_assets(db, skill, workspace_id)
    if changed:
        db.commit()
        assets = active_assets(db, skill_id)
    return ok({
        "skill_id": skill_id,
        "assets": [serialize_asset(asset) for asset in assets],
    })


@router.get("/{skill_id}/mount-context")
def get_mount_context(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    _, changed = sync_bound_assets(db, skill, workspace_id)
    if changed:
        db.commit()
        db.refresh(skill)
    return ok(build_mount_context(db, skill))


@router.get("/{skill_id}/mounted-permissions")
def get_mounted_permissions(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    _, changed = sync_bound_assets(db, skill, workspace_id)
    if changed:
        db.commit()
        db.refresh(skill)
    return ok(build_mounted_permissions(db, skill))


@router.post("/{skill_id}/bound-assets/refresh")
def refresh_bound_assets(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    before = {
        (asset.asset_type, asset.asset_ref_type, asset.asset_ref_id): asset
        for asset in active_assets(db, skill_id)
    }
    assets, changed = sync_bound_assets(db, skill, workspace_id)
    stale_downstream: list[str] = []
    if changed:
        db.commit()
        assets = active_assets(db, skill_id)
        stale_downstream = ["role_asset_policies", "role_asset_granular_rules", "permission_declaration"]
    after = {
        (asset.asset_type, asset.asset_ref_type, asset.asset_ref_id): asset
        for asset in assets
    }
    added = [
        asset.id for key, asset in after.items()
        if key not in before
    ]
    updated = [
        asset.id for key, asset in after.items()
        if key in before and (
            asset.asset_name != before[key].asset_name
            or (asset.binding_scope_json or {}) != (before[key].binding_scope_json or {})
            or (asset.sensitivity_summary_json or {}) != (before[key].sensitivity_summary_json or {})
            or (asset.risk_flags_json or []) != (before[key].risk_flags_json or [])
            or asset.status != before[key].status
        )
    ]
    removed = [
        asset.id for key, asset in before.items()
        if key not in after
    ]
    bundle = latest_bundle(db, skill_id)
    return ok({
        "skill_id": skill_id,
        "governance_version": bundle.governance_version if bundle else 0,
        "created_bundle_id": None,
        "asset_changes": {
            "added": added,
            "removed": removed,
            "updated": updated,
        },
        "stale_downstream": stale_downstream,
        "assets": [serialize_asset(asset) for asset in assets],
    })


@router.post("/{skill_id}/suggest-role-asset-policies", deprecated=True)
def suggest_policies(
    skill_id: int,
    req: SuggestPoliciesRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "suggest-role-asset-policies",
        mode=req.mode,
        bundle_scope=req.bundle_scope,
        async_job=req.async_job,
    )


@router.get("/{skill_id}/jobs/{job_id}")
def get_governance_job(
    skill_id: int,
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    job = db.get(SkillGovernanceJob, job_id)
    if not job or job.skill_id != skill_id:
        raise_api_error(
            404,
            "governance.job_not_found",
            "Governance job not found",
            {"skill_id": skill_id, "job_id": job_id},
        )
    return ok(serialize_governance_job(job))


@router.get("/{skill_id}/role-asset-policies", deprecated=True)
def get_role_asset_policies(
    skill_id: int,
    bundle_id: Optional[int] = Query(None),
    include_rules: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    if bundle_id is not None:
        bundle = db.get(RolePolicyBundle, bundle_id)
    else:
        bundle = latest_bundle(db, skill_id)
    if not bundle or bundle.skill_id != skill_id:
        return ok({
            "bundle_id": bundle_id,
            "bundle_version": 0,
            "review_status": "draft",
            "source_mode": "legacy_bundle",
            "deprecated": True,
            "read_only": True,
            "replacement_endpoints": _legacy_governance_replacements(skill_id),
            "items": [],
        })
    items = (
        db.query(RoleAssetPolicy)
        .filter(RoleAssetPolicy.bundle_id == bundle.id)
        .order_by(RoleAssetPolicy.skill_service_role_id, RoleAssetPolicy.skill_bound_asset_id)
        .all()
    )
    return ok({
        "bundle_id": bundle.id,
        "bundle_version": bundle.bundle_version,
        "governance_version": bundle.governance_version,
        "review_status": bundle.status,
        "source_mode": "legacy_bundle",
        "deprecated": True,
        "read_only": True,
        "replacement_endpoints": _legacy_governance_replacements(skill_id),
        "items": [serialize_policy(policy, include_rules=include_rules) for policy in items],
    })


@router.put("/{skill_id}/role-asset-policies/confirm", deprecated=True)
def confirm_role_asset_policies(
    skill_id: int,
    req: ConfirmRoleAssetPoliciesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "role-asset-policies/confirm",
        bundle_id=req.bundle_id,
        policy_ids=[item.id for item in req.policies],
    )


@router.put("/{skill_id}/role-asset-policies/{policy_id}", deprecated=True)
def update_role_asset_policy(
    skill_id: int,
    policy_id: int,
    req: UpdateRoleAssetPolicyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "role-asset-policies/{policy_id}",
        policy_id=policy_id,
    )


@router.post("/{skill_id}/suggest-granular-rules", deprecated=True)
def suggest_granular_rules(
    skill_id: int,
    req: SuggestGranularRulesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "suggest-granular-rules",
        bundle_id=req.bundle_id,
        risk_only=req.risk_only,
    )


@router.get("/{skill_id}/granular-rules", deprecated=True)
def get_granular_rules(
    skill_id: int,
    bundle_id: int = Query(...),
    risk_only: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    bundle = db.get(RolePolicyBundle, bundle_id)
    if not bundle or bundle.skill_id != skill_id:
        raise_api_error(404, "governance.bundle_not_found", "Bundle not found", {"skill_id": skill_id, "bundle_id": bundle_id})
    policies = (
        db.query(RoleAssetPolicy)
        .filter(RoleAssetPolicy.bundle_id == bundle.id)
        .all()
    )
    field_rules: list[dict] = []
    chunk_rules: list[dict] = []
    for policy in policies:
        for rule in policy.granular_rules:
            if risk_only and not granular_rule_is_high_risk(rule):
                continue
            serialized = serialize_granular_rule(rule)
            target = field_rules if rule.granularity_type == "field" else chunk_rules
            target.append(serialized)
    return ok({
        "bundle_id": bundle.id,
        "source_mode": "legacy_bundle",
        "deprecated": True,
        "read_only": True,
        "replacement_endpoints": _legacy_governance_replacements(skill_id),
        "field_rules": field_rules,
        "chunk_rules": chunk_rules,
    })


@router.put("/{skill_id}/granular-rules/confirm", deprecated=True)
def confirm_granular_rules(
    skill_id: int,
    req: ConfirmGranularRulesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "granular-rules/confirm",
        bundle_id=req.bundle_id,
        rule_ids=[item.id for item in req.rules],
    )


@router.get("/{skill_id}/role-asset-policies/{policy_id}/granular-rules", deprecated=True)
def get_policy_granular_rules(
    skill_id: int,
    policy_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    policy = db.get(RoleAssetPolicy, policy_id)
    if not policy or not policy.bundle or policy.bundle.skill_id != skill_id:
        raise_api_error(404, "governance.policy_not_found", "Policy not found", {"skill_id": skill_id, "policy_id": policy_id})
    return ok({
        "policy_id": policy_id,
        "source_mode": "legacy_bundle",
        "deprecated": True,
        "read_only": True,
        "replacement_endpoints": _legacy_governance_replacements(skill_id),
        "items": [serialize_granular_rule(rule) for rule in policy.granular_rules],
    })


@router.get("/{skill_id}/permission-declaration")
def get_permission_declaration(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    return ok({
        "skill_id": skill_id,
        "bundle": serialize_bundle(bundle),
        "declaration": serialize_declaration(declaration),
        "readiness": readiness,
    })


@router.get("/{skill_id}/declarations/latest")
def get_latest_declaration_contract(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    if declaration_changed:
        db.commit()
    payload = serialize_declaration(declaration)
    return ok(payload or {})


@router.post("/{skill_id}/permission-declaration")
def post_permission_declaration(
    skill_id: int,
    req: GenerateDeclarationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    if req.async_job:
        job = create_governance_job(
            db,
            skill_id=skill_id,
            workspace_id=resolve_workspace_id(db, skill_id),
            job_type="permission_declaration_generation",
            payload={"bundle_id": req.bundle_id},
            created_by=user.id,
        )
        db.commit()
        background_tasks.add_task(process_governance_job, job.id, session_factory_for_current_bind(db))
        return ok({
            "job_id": job.id,
            "status": "queued",
            "job": serialize_governance_job(job),
        })
    declaration = generate_declaration(db, skill, user, req.bundle_id)
    db.commit()
    return ok({
        "job_id": f"governance-declaration-{skill_id}-{declaration.id}",
        "status": declaration.status,
        "declaration": serialize_declaration(declaration),
    })


@router.post("/{skill_id}/declarations/generate")
def generate_declaration_contract(
    skill_id: int,
    req: GenerateDeclarationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    if req.async_job:
        job = create_governance_job(
            db,
            skill_id=skill_id,
            workspace_id=resolve_workspace_id(db, skill_id),
            job_type="permission_declaration_generation",
            payload={"bundle_id": req.bundle_id},
            created_by=user.id,
        )
        db.commit()
        background_tasks.add_task(process_governance_job, job.id, session_factory_for_current_bind(db))
        return ok({
            "job_id": job.id,
            "status": "queued",
            "job": serialize_governance_job(job),
        })
    declaration = generate_declaration(db, skill, user, req.bundle_id)
    db.commit()
    return ok({
        "job_id": f"declaration-writer-{skill_id}-{declaration.bundle_id or declaration.id}",
        "status": "queued",
        "declaration_id": declaration.id,
    })


@router.put("/{skill_id}/permission-declaration/{declaration_id}")
def put_permission_declaration(
    skill_id: int,
    declaration_id: int,
    req: UpdateDeclarationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = db.get(PermissionDeclarationDraft, declaration_id)
    if not declaration or declaration.skill_id != skill_id:
        raise_api_error(
            404,
            "governance.declaration_not_found",
            "Declaration not found",
            {"skill_id": skill_id, "declaration_id": declaration_id},
        )
    update_declaration_text(declaration, req.text, user)
    db.commit()
    db.refresh(declaration)
    return ok({"declaration": serialize_declaration(declaration)})


@router.put("/{skill_id}/declarations/{declaration_id}/adopt")
def adopt_permission_declaration(
    skill_id: int,
    declaration_id: int,
    req: AdoptDeclarationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    declaration = db.get(PermissionDeclarationDraft, declaration_id)
    if not declaration or declaration.skill_id != skill_id:
        raise_api_error(
            404,
            "governance.declaration_not_found",
            "Declaration not found",
            {"skill_id": skill_id, "declaration_id": declaration_id},
        )

    if req.action == "edit":
        edited_text = (req.edited_text or "").strip()
        if not edited_text:
            raise_api_error(
                400,
                "governance.edited_text_required",
                "edited_text is required when action=edit",
                {"skill_id": skill_id, "declaration_id": declaration_id},
            )
        update_declaration_text(declaration, edited_text, user)
    elif req.edited_text:
        raise_api_error(
            400,
            "governance.edited_text_not_allowed",
            "edited_text is only allowed when action=edit",
            {"skill_id": skill_id, "declaration_id": declaration_id},
        )

    mounted_version = mount_permission_declaration_to_skill(db, skill, declaration, user)
    db.commit()
    db.refresh(declaration)
    return ok({
        "declaration_id": declaration.id,
        "declaration_version": declaration.id,
        "status": declaration.status,
        "skill_content_version": mounted_version.version,
        "mounted": True,
        "mount_target": "permission_declaration_block",
        "mount_mode": "replace_managed_block",
        "declaration": serialize_declaration(declaration),
    })


@router.post("/{skill_id}/permission-declaration/{declaration_id}/mount")
def mount_permission_declaration(
    skill_id: int,
    declaration_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    declaration = db.get(PermissionDeclarationDraft, declaration_id)
    if not declaration or declaration.skill_id != skill_id:
        raise_api_error(
            404,
            "governance.declaration_not_found",
            "Declaration not found",
            {"skill_id": skill_id, "declaration_id": declaration_id},
        )
    mounted_version = mount_permission_declaration_to_skill(db, skill, declaration, user)
    db.commit()
    db.refresh(declaration)
    return ok({
        "declaration": serialize_declaration(declaration),
        "skill_version": {
            "id": mounted_version.id,
            "version": mounted_version.version,
        },
    })


@router.put("/{skill_id}/role-asset-policies/{policy_id}/granular-rules/{rule_id}", deprecated=True)
def update_policy_granular_rule(
    skill_id: int,
    policy_id: int,
    rule_id: int,
    req: UpdateGranularRuleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    _raise_legacy_governance_write_deprecated(
        skill_id,
        "role-asset-policies/{policy_id}/granular-rules/{rule_id}",
        policy_id=policy_id,
        rule_id=rule_id,
    )


@router.get("/{skill_id}/permission-case-plans/latest")
def get_latest_permission_case_plan(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    declaration = latest_declaration(db, skill_id)
    declaration, declaration_changed = ensure_permission_declaration_prompt_sync(db, skill_id, declaration)
    bundle = latest_bundle(db, skill_id)
    if declaration_changed:
        db.commit()
    readiness = permission_case_plan_readiness(db, skill_id, declaration=declaration, bundle=bundle)
    plan = latest_case_plan(db, skill_id)
    return ok({
        "skill_id": skill_id,
        "readiness": readiness,
        "plan": serialize_case_plan(plan),
        "plan_state": permission_case_plan_state(
            db,
            skill_id,
            plan=plan,
            declaration=declaration,
            bundle=bundle,
        ),
    })


@router.post("/{skill_id}/permission-case-plans")
def generate_permission_case_plan_route(
    skill_id: int,
    req: GeneratePermissionCasePlanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    workspace_id = resolve_workspace_id(db, skill_id)
    if req.async_job:
        job = create_governance_job(
            db,
            skill_id=skill_id,
            workspace_id=workspace_id,
            job_type="permission_case_plan_generation",
            payload={"focus_mode": req.focus_mode, "max_cases": req.max_cases},
            created_by=user.id,
        )
        db.commit()
        background_tasks.add_task(process_governance_job, job.id, session_factory_for_current_bind(db))
        return ok({
            "job_id": job.id,
            "status": "queued",
            "job": serialize_governance_job(job),
        })
    plan = generate_permission_case_plan(
        db,
        skill,
        user,
        workspace_id=workspace_id,
        focus_mode=req.focus_mode,
        max_cases=req.max_cases,
    )
    db.commit()
    db.refresh(plan)
    return ok({
        "job_id": f"permission-case-plan-{skill_id}-{plan.id}",
        "status": plan.status,
        "plan": serialize_case_plan(plan),
    })


@router.post("/{skill_id}/permission-case-plans/{plan_id}/materialize")
def materialize_permission_case_plan_route(
    skill_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = assert_skill_governance_access(db, skill_id, user)
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan or plan.skill_id != skill_id:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"skill_id": skill_id, "plan_id": plan_id})
    result = materialize_permission_case_plan(db, skill, user, plan)
    db.commit()
    latest_plan = latest_case_plan(db, skill_id)
    return ok({
        **result,
        "plan": serialize_case_plan(latest_plan or plan),
        "review": permission_contract_review_summary(db, skill_id, plan_id),
    })


@router.get("/{skill_id}/permission-case-plans/{plan_id}/contract-review")
def get_permission_contract_review(
    skill_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    return ok({
        "review": permission_contract_review_summary(db, skill_id, plan_id),
    })


@router.put("/{skill_id}/permission-case-plans/{plan_id}/cases/{case_id}")
def update_permission_case_draft(
    skill_id: int,
    plan_id: int,
    case_id: int,
    req: UpdateCaseDraftRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_skill_governance_access(db, skill_id, user)
    plan = db.get(TestCasePlanDraft, plan_id)
    if not plan or plan.skill_id != skill_id:
        raise_api_error(404, "sandbox.case_plan_not_found", "Case plan not found", {"skill_id": skill_id, "plan_id": plan_id})
    case = db.get(TestCaseDraft, case_id)
    if not case or case.plan_id != plan_id or case.skill_id != skill_id:
        raise_api_error(
            404,
            "sandbox.case_draft_not_found",
            "Case draft not found",
            {"skill_id": skill_id, "plan_id": plan_id, "case_id": case_id},
        )
    data = req.model_dump(exclude_unset=True)
    if "source_verification_status" in data:
        raise_api_error(
            400,
            "sandbox.source_verification_locked",
            "source_verification_status 不可直接编辑",
            {"skill_id": skill_id, "plan_id": plan_id, "case_id": case_id},
        )
    edited = False
    if "status" in data:
        case.status = data["status"]
    if "prompt" in data:
        case.prompt = data["prompt"]
        edited = True
    if "expected_behavior" in data:
        case.expected_behavior = data["expected_behavior"]
        edited = True
    if edited:
        case.edited_by_user = True
    db.commit()
    db.refresh(case)
    return ok({
        "plan_id": plan_id,
        "item": serialize_case_draft(case),
    })
