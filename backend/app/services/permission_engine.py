"""运行时权限校验引擎 — 三层脱敏合并 + Skill 可调用判断"""
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


class PermissionEngine:

    # ── Skill 可调用判断 ──────────────────────────────────────────────────────

    def check_skill_callable(self, user: User, skill_id: int, db: Session) -> bool:
        """② Skill 匹配后检查 policy.callable[role]
        - super_admin 始终可调用（break-glass，留审计日志）
        - 找不到 policy → 默认允许（宽松策略）
        - 找到对应角色的 override → 按 callable 字段返回
        """
        if user.role == Role.SUPER_ADMIN:
            logger.info(
                "SUPER_ADMIN break-glass: user_id=%d skill_id=%d action=check_skill_callable",
                user.id, skill_id,
            )
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

    # ── 批量脱敏规则加载（M29: 消除 N+1 查询） ──────────────────────────────

    def _batch_merge_mask_rules(
        self,
        user: User,
        skill_id: int,
        field_names: list[str],
        data_domain_id: int | None,
        db: Session,
    ) -> dict[str, tuple[MaskAction, dict]]:
        """批量加载三层脱敏规则，返回 {field_name: (action, params)}。

        一次性查出所有 Skill 级、角色级、全局规则，再在内存中合并，
        替代逐字段调用 merge_mask_rules 导致的 N+1 查询。
        """
        if not field_names:
            return {}

        # 层级3：Skill 级覆盖 — 一次批量查
        skill_overrides_q = (
            db.query(SkillMaskOverride)
            .filter(
                SkillMaskOverride.skill_id == skill_id,
                SkillMaskOverride.field_name.in_(field_names),
            )
            .all()
        )
        # 按 (field_name, position_id) 索引
        skill_map: dict[str, dict[int | None, SkillMaskOverride]] = {}
        for so in skill_overrides_q:
            skill_map.setdefault(so.field_name, {})[so.position_id] = so

        # 层级2：角色级覆盖
        role_map: dict[str, dict[int | None, RoleMaskOverride]] = {}
        if user.position_id:
            role_overrides_q = (
                db.query(RoleMaskOverride)
                .filter(
                    RoleMaskOverride.position_id == user.position_id,
                    RoleMaskOverride.field_name.in_(field_names),
                )
                .all()
            )
            for ro in role_overrides_q:
                role_map.setdefault(ro.field_name, {})[ro.data_domain_id] = ro

        # 层级1：全局默认
        global_overrides_q = (
            db.query(GlobalDataMask)
            .filter(GlobalDataMask.field_name.in_(field_names))
            .all()
        )
        global_map: dict[str, dict[int | None, GlobalDataMask]] = {}
        for gm in global_overrides_q:
            global_map.setdefault(gm.field_name, {})[gm.data_domain_id] = gm

        # 内存中合并：Skill级 > 角色级 > 全局
        result: dict[str, tuple[MaskAction, dict]] = {}
        for fn in field_names:
            # 层级3: Skill 覆盖
            so_by_pos = skill_map.get(fn, {})
            if so_by_pos:
                so = so_by_pos.get(user.position_id) or so_by_pos.get(None)
                if so:
                    result[fn] = (so.mask_action, so.mask_params or {})
                    continue

            # 层级2: 角色覆盖
            ro_by_domain = role_map.get(fn, {})
            if ro_by_domain:
                ro = ro_by_domain.get(data_domain_id) or ro_by_domain.get(None)
                if ro:
                    result[fn] = (ro.mask_action, ro.mask_params or {})
                    continue

            # 层级1: 全局
            gm_by_domain = global_map.get(fn, {})
            if gm_by_domain:
                gm = gm_by_domain.get(data_domain_id) or gm_by_domain.get(None)
                if gm:
                    result[fn] = (gm.mask_action, gm.mask_params or {})
                    continue

            result[fn] = (MaskAction.KEEP, {})

        return result

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
            logger.info(
                "SUPER_ADMIN break-glass: user_id=%d skill_id=%d action=apply_data_masks rows=%d",
                user.id, skill_id, len(data),
            )
            return data

        # 提取所有字段名
        all_fields: set[str] = set()
        for row in data:
            all_fields.update(row.keys())

        # 批量预加载三层脱敏规则（避免 N+1 查询）
        field_rules = self._batch_merge_mask_rules(
            user, skill_id, list(all_fields), data_domain_id, db
        )

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
            logger.info(
                "SUPER_ADMIN break-glass: user_id=%d action=apply_output_masks domain_id=%s",
                user.id, data_domain_id,
            )
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

        import copy
        result = copy.deepcopy(data)
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
