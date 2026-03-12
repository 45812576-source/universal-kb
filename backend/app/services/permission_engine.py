"""运行时权限校验引擎 — 三层脱敏合并 + Skill 可调用判断"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.permission import (
    GlobalDataMask,
    MaskAction,
    RoleMaskOverride,
    RoleOutputMask,
    RolePolicyOverride,
    SkillMaskOverride,
    SkillPolicy,
)
from app.models.user import Role, User


class PermissionEngine:

    # ── Skill 可调用判断 ──────────────────────────────────────────────────────

    def check_skill_callable(self, user: User, skill_id: int, db: Session) -> bool:
        """② Skill 匹配后检查 policy.callable[role]
        - super_admin 始终可调用
        - 找不到 policy → 默认允许（宽松策略）
        - 找到对应角色的 override → 按 callable 字段返回
        """
        if user.role == Role.SUPER_ADMIN:
            return True

        policy = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
        if not policy:
            return True

        if user.position_id:
            override = (
                db.query(RolePolicyOverride)
                .filter(
                    RolePolicyOverride.skill_policy_id == policy.id,
                    RolePolicyOverride.position_id == user.position_id,
                )
                .first()
            )
            if override is not None:
                return override.callable

        return True

    # ── 数据范围获取 ──────────────────────────────────────────────────────────

    def get_data_scope(self, user: User, skill_id: int, db: Session) -> dict:
        """③ 获取 policy.data_scope[role]，返回数据范围字典"""
        policy = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
        if not policy:
            return {}

        if user.position_id:
            override = (
                db.query(RolePolicyOverride)
                .filter(
                    RolePolicyOverride.skill_policy_id == policy.id,
                    RolePolicyOverride.position_id == user.position_id,
                )
                .first()
            )
            if override and override.data_scope:
                return override.data_scope

        return policy.default_data_scope or {}

    # ── 三层脱敏合并 ──────────────────────────────────────────────────────────

    def merge_mask_rules(
        self,
        user: User,
        skill_id: int,
        field_name: str,
        data_domain_id: int | None,
        db: Session,
    ) -> tuple[MaskAction, dict]:
        """三层优先级合并：Skill级 > 角色级 > 全局
        返回 (mask_action, mask_params)
        """
        # 层级3：Skill 级覆盖（最严格）
        skill_override_q = db.query(SkillMaskOverride).filter(
            SkillMaskOverride.skill_id == skill_id,
            SkillMaskOverride.field_name == field_name,
        )
        if user.position_id:
            # 优先查岗位级，其次查全局（position_id=None）
            skill_pos = skill_override_q.filter(
                SkillMaskOverride.position_id == user.position_id
            ).first()
            skill_global = skill_override_q.filter(
                SkillMaskOverride.position_id.is_(None)
            ).first()
            skill_override = skill_pos or skill_global
        else:
            skill_override = skill_override_q.filter(
                SkillMaskOverride.position_id.is_(None)
            ).first()

        if skill_override:
            return skill_override.mask_action, skill_override.mask_params or {}

        # 层级2：角色级覆盖
        if user.position_id:
            role_q = db.query(RoleMaskOverride).filter(
                RoleMaskOverride.position_id == user.position_id,
                RoleMaskOverride.field_name == field_name,
            )
            if data_domain_id:
                role_override = (
                    role_q.filter(RoleMaskOverride.data_domain_id == data_domain_id).first()
                    or role_q.filter(RoleMaskOverride.data_domain_id.is_(None)).first()
                )
            else:
                role_override = role_q.first()

            if role_override:
                return role_override.mask_action, role_override.mask_params or {}

        # 层级1：全局默认
        global_q = db.query(GlobalDataMask).filter(
            GlobalDataMask.field_name == field_name,
        )
        if data_domain_id:
            global_rule = (
                global_q.filter(GlobalDataMask.data_domain_id == data_domain_id).first()
                or global_q.filter(GlobalDataMask.data_domain_id.is_(None)).first()
            )
        else:
            global_rule = global_q.first()

        if global_rule:
            return global_rule.mask_action, global_rule.mask_params or {}

        return MaskAction.KEEP, {}

    # ── 字段脱敏执行 ──────────────────────────────────────────────────────────

    def _apply_mask_action(self, value: Any, action: MaskAction, params: dict) -> Any:
        if value is None:
            return value
        if action == MaskAction.KEEP or action == MaskAction.SHOW:
            return value
        if action in (MaskAction.HIDE, MaskAction.REMOVE):
            return None
        if action == MaskAction.TRUNCATE:
            length = params.get("length", 4)
            return str(value)[:length] + "..." if len(str(value)) > length else value
        if action == MaskAction.PARTIAL:
            prefix = params.get("prefix_len", 3)
            s = str(value)
            return s[:prefix] + "*" * max(len(s) - prefix, 0)
        if action == MaskAction.RANGE:
            # 数值范围化，例如 50000 → "40000-60000"
            try:
                v = float(value)
                step = float(params.get("step", 10000))
                low = int(v // step) * step
                return f"{int(low)}-{int(low + step)}"
            except (ValueError, TypeError):
                return "***"
        if action == MaskAction.RANK:
            return params.get("rank_label", "Top N")
        if action == MaskAction.AGGREGATE:
            return params.get("aggregate_label", "统计值")
        if action == MaskAction.REPLACE:
            return params.get("replace_value", "***")
        if action == MaskAction.NOISE:
            try:
                import random
                v = float(value)
                ratio = float(params.get("noise_ratio", 0.1))
                noise = v * ratio * (random.random() * 2 - 1)
                return round(v + noise, 2)
            except (ValueError, TypeError):
                return value
        if action == MaskAction.LABEL_ONLY:
            return params.get("label", str(value)[:2] + "***")
        return value

    # ── 数据脱敏应用 ──────────────────────────────────────────────────────────

    def apply_data_masks(
        self,
        user: User,
        skill_id: int,
        data: list[dict],
        data_domain_id: int | None = None,
        db: Session | None = None,
    ) -> list[dict]:
        """④ 三层脱敏合并 + 字段级替换"""
        if not data or db is None:
            return data

        if user.role == Role.SUPER_ADMIN:
            return data

        # 提取所有字段名
        all_fields: set[str] = set()
        for row in data:
            all_fields.update(row.keys())

        # 预先计算每个字段的 mask 规则（避免每行重复查询）
        field_rules: dict[str, tuple[MaskAction, dict]] = {}
        for field in all_fields:
            action, params = self.merge_mask_rules(user, skill_id, field, data_domain_id, db)
            field_rules[field] = (action, params)

        result = []
        for row in data:
            masked = {}
            for k, v in row.items():
                action, params = field_rules.get(k, (MaskAction.KEEP, {}))
                if action == MaskAction.REMOVE:
                    continue  # 整列移除
                masked[k] = self._apply_mask_action(v, action, params)
            result.append(masked)
        return result

    # ── 输出遮罩 ─────────────────────────────────────────────────────────────

    def apply_output_masks(
        self,
        user: User,
        data: dict,
        data_domain_id: int,
        db: Session,
    ) -> dict:
        """⑥ 输出侧遮罩（RoleOutputMask）"""
        if user.role == Role.SUPER_ADMIN:
            return data

        if not user.position_id:
            return data

        masks = (
            db.query(RoleOutputMask)
            .filter(
                RoleOutputMask.position_id == user.position_id,
                RoleOutputMask.data_domain_id == data_domain_id,
            )
            .all()
        )

        if not masks:
            return data

        result = dict(data)
        for mask in masks:
            if mask.field_name in result:
                if mask.mask_action in (MaskAction.HIDE, MaskAction.REMOVE):
                    result.pop(mask.field_name, None)
                else:
                    result[mask.field_name] = self._apply_mask_action(
                        result[mask.field_name], mask.mask_action, {}
                    )
        return result


permission_engine = PermissionEngine()
