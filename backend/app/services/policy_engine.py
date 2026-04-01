"""统一策略引擎 — 新权限模型（TableRoleGroup + TablePermissionPolicy + SkillDataGrant）的运行时。

Phase 1: 后端数据查询走新权限，替代旧的 validation_rules.row_scope + DataOwnership + data_visibility 三件套。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.business import (
    TableRoleGroup,
    TablePermissionPolicy,
    SkillDataGrant,
    TableField,
)

if TYPE_CHECKING:
    from app.models.user import User


# ─── 数据类 ─────────────────────────────────────────────────────────────────

DISCLOSURE_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

# view_kind 约束矩阵
VIEW_KIND_CONSTRAINTS: dict[str, dict] = {
    "metric": {"max_disclosure": "L2", "row_access_modes": ["all"]},
    "review_queue": {"max_disclosure": "L4", "requires_approval": True},
    "list": {},
    "board": {},
    "pivot": {"max_disclosure": "L3"},
}


@dataclass
class PolicyResult:
    denied: bool = False
    deny_reasons: list[str] = field(default_factory=list)
    row_access_mode: str = "none"        # all | owner | department | rule | none
    row_rule_json: dict = field(default_factory=dict)
    visible_field_ids: set[int] = field(default_factory=set)
    field_access_mode: str = "all"       # all | allowlist | blocklist
    disclosure_level: str = "L0"         # L0-L4
    masking_rules: dict = field(default_factory=dict)
    export_permission: bool = False
    tool_permission_mode: str = "deny"   # deny | readonly | readwrite
    source: str = "default_deny"
    matched_role_groups: list[int] = field(default_factory=list)
    effective_grant: dict | None = None


# ─── 披露级别能力矩阵 ────────────────────────────────────────────────────────

_DISCLOSURE_CAPS = {
    "L0": {"can_see_rows": False, "can_see_aggregate": False, "can_see_decision": False, "can_see_masked": False, "can_see_raw": False},
    "L1": {"can_see_rows": False, "can_see_aggregate": False, "can_see_decision": True,  "can_see_masked": False, "can_see_raw": False},
    "L2": {"can_see_rows": False, "can_see_aggregate": True,  "can_see_decision": True,  "can_see_masked": False, "can_see_raw": False},
    "L3": {"can_see_rows": True,  "can_see_aggregate": True,  "can_see_decision": True,  "can_see_masked": True,  "can_see_raw": False},
    "L4": {"can_see_rows": True,  "can_see_aggregate": True,  "can_see_decision": True,  "can_see_masked": True,  "can_see_raw": True},
}


def check_disclosure_capability(level: str) -> dict:
    """返回该披露级别的能力集。"""
    return dict(_DISCLOSURE_CAPS.get(level, _DISCLOSURE_CAPS["L0"]))


# ─── 角色组匹配 ──────────────────────────────────────────────────────────────

def resolve_user_role_groups(
    db: Session,
    table_id: int,
    user: "User",
    skill_id: int | None = None,
) -> list[TableRoleGroup]:
    """找出用户/Skill 所属的角色组。"""
    all_groups = (
        db.query(TableRoleGroup)
        .filter(TableRoleGroup.table_id == table_id)
        .all()
    )
    matched: list[TableRoleGroup] = []
    for g in all_groups:
        if _matches_group(g, user, skill_id):
            matched.append(g)
    return matched


def _matches_group(g: TableRoleGroup, user: "User", skill_id: int | None) -> bool:
    """判断单个角色组是否匹配当前用户/Skill。"""
    gt = g.group_type or "human_role"

    human_match = False
    skill_match = False

    if gt in ("human_role", "mixed"):
        human_match = _matches_human(g, user)
    if gt in ("skill_role", "mixed"):
        skill_match = _matches_skill(g, skill_id)

    if gt == "human_role":
        return human_match
    if gt == "skill_role":
        return skill_match
    # mixed: 两者都要匹配
    return human_match and skill_match


def _matches_human(g: TableRoleGroup, user: "User") -> bool:
    scope = g.subject_scope or "custom"
    if scope == "all_users":
        return True
    # custom: 检查 user_ids / department_ids / role_keys
    user_ids = g.user_ids or []
    if user.id in user_ids:
        return True
    dept_ids = g.department_ids or []
    if user.department_id and user.department_id in dept_ids:
        return True
    role_keys = g.role_keys or []
    if user.role and user.role.value in role_keys:
        return True
    return False


def _matches_skill(g: TableRoleGroup, skill_id: int | None) -> bool:
    if skill_id is None:
        return False
    scope = g.subject_scope or "custom"
    if scope == "all_skills":
        return True
    skill_ids = g.skill_ids or []
    return skill_id in skill_ids


# ─── 策略合并 ────────────────────────────────────────────────────────────────

def resolve_effective_policy(
    db: Session,
    table_id: int,
    role_group_ids: list[int],
    view_id: int | None = None,
    skill_id: int | None = None,
) -> PolicyResult:
    """合并多角色组策略，返回生效结果。"""
    if not role_group_ids:
        return PolicyResult(denied=True, deny_reasons=["未匹配任何角色组"], source="default_deny")

    # 1. 查每个角色组的 policy（view-specific 优先）
    policies: list[TablePermissionPolicy] = []
    for rg_id in role_group_ids:
        p = _find_policy(db, table_id, rg_id, view_id)
        if p:
            policies.append(p)

    if not policies:
        return PolicyResult(
            denied=True,
            deny_reasons=["角色组无对应策略配置"],
            matched_role_groups=role_group_ids,
            source="default_deny",
        )

    # 2. 显式 deny（row_access_mode=none）优先
    deny_policies = [p for p in policies if p.row_access_mode == "none"]
    if deny_policies:
        return PolicyResult(
            denied=True,
            deny_reasons=[p.reason_template or f"角色组 {p.role_group_id} 拒绝访问" for p in deny_policies],
            matched_role_groups=role_group_ids,
            source="multi_group_merge" if len(policies) > 1 else "table_policy",
        )

    # 3. allow 组：合并
    result = _merge_allow_policies(policies, role_group_ids)

    # 3.5 view_kind 约束矩阵 — 强制 cap disclosure
    if view_id is not None:
        from app.models.business import TableView
        view = db.get(TableView, view_id)
        if view and view.view_kind:
            vk_constraint = VIEW_KIND_CONSTRAINTS.get(view.view_kind)
            if vk_constraint:
                max_dl = vk_constraint.get("max_disclosure")
                if max_dl and DISCLOSURE_ORDER.get(max_dl, 0) < DISCLOSURE_ORDER.get(result.disclosure_level, 0):
                    result.disclosure_level = max_dl
        # disclosure_ceiling 也强制 cap
        if view and view.disclosure_ceiling:
            ceil = view.disclosure_ceiling
            if DISCLOSURE_ORDER.get(ceil, 0) < DISCLOSURE_ORDER.get(result.disclosure_level, 0):
                result.disclosure_level = ceil

    # 4. Skill grant 限制
    if skill_id is not None:
        grant = (
            db.query(SkillDataGrant)
            .filter(
                SkillDataGrant.skill_id == skill_id,
                SkillDataGrant.table_id == table_id,
            )
            .first()
        )
        if grant:
            # Skill 必须绑定视图才能访问数据
            if grant.grant_mode == "allow" and not grant.view_id:
                return PolicyResult(
                    denied=True,
                    deny_reasons=["Skill 必须绑定视图才能访问数据"],
                    matched_role_groups=role_group_ids,
                    source="skill_grant",
                )
            if grant.grant_mode == "deny":
                return PolicyResult(
                    denied=True,
                    deny_reasons=["Skill 数据授权被拒绝"],
                    matched_role_groups=role_group_ids,
                    source="skill_grant",
                )
            # max_disclosure 限制
            grant_dl = grant.max_disclosure_level or "L2"
            if DISCLOSURE_ORDER.get(grant_dl, 0) < DISCLOSURE_ORDER.get(result.disclosure_level, 0):
                result.disclosure_level = grant_dl
            result.effective_grant = {
                "id": grant.id,
                "max_disclosure_level": grant_dl,
                "allowed_actions": grant.allowed_actions or [],
                "audit_level": grant.audit_level,
            }
            result.source = "skill_grant"

    return result


def _find_policy(
    db: Session, table_id: int, role_group_id: int, view_id: int | None
) -> TablePermissionPolicy | None:
    """查找策略：view-specific 优先，fallback 到 table-level。"""
    if view_id:
        view_policy = (
            db.query(TablePermissionPolicy)
            .filter(
                TablePermissionPolicy.table_id == table_id,
                TablePermissionPolicy.role_group_id == role_group_id,
                TablePermissionPolicy.view_id == view_id,
            )
            .first()
        )
        if view_policy:
            return view_policy

    return (
        db.query(TablePermissionPolicy)
        .filter(
            TablePermissionPolicy.table_id == table_id,
            TablePermissionPolicy.role_group_id == role_group_id,
            TablePermissionPolicy.view_id.is_(None),
        )
        .first()
    )


def _merge_allow_policies(
    policies: list[TablePermissionPolicy],
    role_group_ids: list[int],
) -> PolicyResult:
    """合并多个 allow 策略：字段取并集，disclosure 取最高，export 取 OR。"""
    # row_access_mode: 优先级 all > department > owner > rule
    ROW_MODE_PRIORITY = {"all": 4, "department": 3, "owner": 2, "rule": 1, "none": 0}
    best_row_mode = "none"
    best_row_rule = {}
    merged_field_ids: set[int] = set()
    merged_blocked_ids: set[int] | None = None  # blocklist 交集
    best_disclosure = "L0"
    merged_masking: dict = {}
    any_export = False
    best_tool_mode = "deny"
    field_access_mode = "all"

    TOOL_PRIORITY = {"readwrite": 3, "readonly": 2, "deny": 1}

    for p in policies:
        # row_access_mode: 取最宽松
        mode = p.row_access_mode or "none"
        if ROW_MODE_PRIORITY.get(mode, 0) > ROW_MODE_PRIORITY.get(best_row_mode, 0):
            best_row_mode = mode
            best_row_rule = p.row_rule_json or {}

        # field: 并集 (allowlist) / 交集 (blocklist)
        fam = p.field_access_mode or "all"
        if fam == "allowlist":
            field_access_mode = "allowlist"
            merged_field_ids.update(p.allowed_field_ids or [])
        elif fam == "blocklist":
            blocked = set(p.blocked_field_ids or [])
            if merged_blocked_ids is None:
                merged_blocked_ids = blocked
            else:
                merged_blocked_ids &= blocked  # 交集 = 只有所有策略都 block 的才 block

        # disclosure: 取最高
        dl = p.disclosure_level or "L0"
        if DISCLOSURE_ORDER.get(dl, 0) > DISCLOSURE_ORDER.get(best_disclosure, 0):
            best_disclosure = dl

        # masking: 后面的覆盖前面的
        if p.masking_rule_json:
            merged_masking.update(p.masking_rule_json)

        # export: OR
        if p.export_permission:
            any_export = True

        # tool: 取最宽松
        tm = p.tool_permission_mode or "deny"
        if TOOL_PRIORITY.get(tm, 0) > TOOL_PRIORITY.get(best_tool_mode, 0):
            best_tool_mode = tm

    # 处理 blocklist → 转为 visible_field_ids（需要外部再过滤）
    result_field_ids = merged_field_ids
    if merged_blocked_ids is not None and field_access_mode != "allowlist":
        field_access_mode = "blocklist"

    return PolicyResult(
        denied=False,
        row_access_mode=best_row_mode,
        row_rule_json=best_row_rule,
        visible_field_ids=result_field_ids,
        field_access_mode=field_access_mode,
        disclosure_level=best_disclosure,
        masking_rules=merged_masking,
        export_permission=any_export,
        tool_permission_mode=best_tool_mode,
        source="multi_group_merge" if len(policies) > 1 else ("view_policy" if policies[0].view_id else "table_policy"),
        matched_role_groups=role_group_ids,
    )


# ─── 字段过滤 ────────────────────────────────────────────────────────────────

def compute_visible_fields(
    fields: list[TableField],
    policy: PolicyResult,
) -> list[TableField]:
    """根据策略过滤可见字段。"""
    if policy.denied:
        return []

    fam = policy.field_access_mode
    if fam == "all":
        return list(fields)
    elif fam == "allowlist":
        allowed = policy.visible_field_ids
        return [f for f in fields if f.id in allowed]
    elif fam == "blocklist":
        # blocklist 需要从 masking_rules 里提取 blocked_field_ids
        # 或者调用方传入（在 merge 时已算好）
        return list(fields)
    return list(fields)


def compute_visible_columns(
    all_columns: list[str],
    fields: list[TableField],
    policy: PolicyResult,
) -> list[str]:
    """根据策略过滤可见列名（用于 list_rows 返回）。"""
    visible_fields = compute_visible_fields(fields, policy)
    if not visible_fields:
        return [] if policy.field_access_mode != "all" else all_columns

    # 用 field_name / physical_column_name 做映射
    visible_names: set[str] = set()
    for f in visible_fields:
        visible_names.add(f.field_name)
        if f.physical_column_name:
            visible_names.add(f.physical_column_name)

    return [c for c in all_columns if c in visible_names]


# ─── 脱敏 ────────────────────────────────────────────────────────────────────

def apply_field_masking(
    rows: list[dict],
    masking_rules: dict,
    fields: list[TableField] | None = None,
) -> list[dict]:
    """对行数据应用脱敏规则。

    masking_rules 格式: {"字段名": "mask_type"} 或 {"字段名": {"type": "mask_type", ...params}}
    支持: phone_mask, name_mask, id_mask, email_mask, amount_range, full_mask
    """
    if not masking_rules or not rows:
        return rows

    result = []
    for row in rows:
        masked_row = dict(row)
        for field_name, rule in masking_rules.items():
            if field_name not in masked_row:
                continue
            val = masked_row[field_name]
            if val is None:
                continue
            val_str = str(val)
            if isinstance(rule, str):
                mask_type = rule
            elif isinstance(rule, dict):
                mask_type = rule.get("type", "full_mask")
            else:
                continue
            masked_row[field_name] = _apply_single_mask(val_str, mask_type)
        result.append(masked_row)
    return result


def _apply_single_mask(val: str, mask_type: str) -> str:
    """应用单个脱敏规则。"""
    if not val:
        return val

    if mask_type == "phone_mask":
        # 138****1234
        if len(val) >= 7:
            return val[:3] + "****" + val[-4:]
        return "****"

    if mask_type == "name_mask":
        # 张*
        if len(val) >= 2:
            return val[0] + "*" * (len(val) - 1)
        return "*"

    if mask_type == "id_mask":
        # 310***********1234
        if len(val) >= 8:
            return val[:3] + "*" * (len(val) - 7) + val[-4:]
        return "****"

    if mask_type == "email_mask":
        # z***@example.com
        at_idx = val.find("@")
        if at_idx > 0:
            return val[0] + "***" + val[at_idx:]
        return "***"

    if mask_type == "amount_range":
        # 尝试解析数字并输出范围
        try:
            num = float(re.sub(r"[^\d.]", "", val))
            if num < 10_000:
                return "1万以下"
            elif num < 100_000:
                return "1万-10万"
            elif num < 500_000:
                return "10万-50万"
            elif num < 1_000_000:
                return "50万-100万"
            else:
                return "100万以上"
        except (ValueError, TypeError):
            return "***"

    # full_mask 或未知类型
    return "***"


# ─── 行过滤 SQL 构建 ─────────────────────────────────────────────────────────

def build_row_filter_sql(
    policy: PolicyResult,
    user: "User",
    table_name: str,
) -> str | None:
    """根据 row_access_mode 构建 WHERE 条件片段。返回 None 表示无需过滤（all）。"""
    mode = policy.row_access_mode
    if mode == "all":
        return None
    if mode == "owner":
        owner_field = policy.row_rule_json.get("owner_field", "owner_id")
        return f"`{owner_field}` = {user.id}"
    if mode == "department":
        dept_field = policy.row_rule_json.get("department_field", "department_id")
        dept_id = user.department_id or 0
        return f"`{dept_field}` = {dept_id}"
    if mode == "rule":
        # 自定义规则 — 暂不实现复杂 DSL，预留
        return None
    # none — 不应走到这里（在 resolve 时已 deny）
    return "1=0"
