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
    declaration_source_mode,
    ensure_permission_declaration_prompt_sync,
    find_position,
    generate_declaration,
    generate_permission_case_plan,
    granular_rule_is_high_risk,
    materialize_permission_case_plan,
    mark_declaration_stale,
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
