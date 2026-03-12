"""三级数据可见性 + 权限引擎集成

原有接口 (determine_level / desensitize_row / apply_visibility) 保持不变，
新增 apply_with_permission_engine 方法接入三层脱敏。
"""
from __future__ import annotations

from typing import Any

from app.models.business import DataOwnership, VisibilityLevel
from app.models.user import Role, User


class DataVisibility:

    # ── 原有三级可见性逻辑（保持不变） ───────────────────────────────────────

    def determine_level(
        self,
        user: User,
        row: dict,
        ownership: DataOwnership,
    ) -> str:
        """Return 'detail', 'desensitized', or 'stats' for a given row + user."""
        if user.role == Role.SUPER_ADMIN:
            return VisibilityLevel.DETAIL.value

        owner_field = ownership.owner_field
        dept_field = ownership.department_field

        if owner_field and row.get(owner_field) == user.id:
            return VisibilityLevel.DETAIL.value

        if dept_field and user.department_id and row.get(dept_field) == user.department_id:
            return VisibilityLevel.DESENSITIZED.value

        if user.role == Role.DEPT_ADMIN:
            return VisibilityLevel.DESENSITIZED.value

        return VisibilityLevel.STATS.value

    def desensitize_row(self, row: dict, desensitize_config: dict) -> dict:
        """Apply field-level desensitization according to config."""
        result = dict(row)
        for field, rule in desensitize_config.items():
            if field not in result:
                continue
            method = rule.get("method", "hide")
            if method == "hide":
                result[field] = "***"
            elif method == "industry_scale":
                industry_field = rule.get("industry_field")
                scale_field = rule.get("scale_field")
                industry = row.get(industry_field, "") if industry_field else ""
                scale = row.get(scale_field, "") if scale_field else ""
                parts = [p for p in [industry, scale] if p]
                result[field] = "某" + "".join(str(p) for p in parts) + "客户" if parts else "某客户"
            else:
                result[field] = "***"
        return result

    def apply_visibility(
        self,
        rows: list[dict],
        user: User,
        ownership: DataOwnership | None,
        desensitize_config: dict | None,
    ) -> list[dict]:
        """Apply visibility rules to a list of rows."""
        if not ownership:
            return rows

        result = []
        for row in rows:
            level = self.determine_level(user, row, ownership)
            if level == VisibilityLevel.DETAIL.value:
                result.append(row)
            elif level == VisibilityLevel.DESENSITIZED.value:
                cfg = desensitize_config or {}
                result.append(self.desensitize_row(row, cfg))
            # stats level: row is excluded from per-row results
        return result

    # ── 新增：接入 PermissionEngine 三层脱敏 ─────────────────────────────────

    def apply_with_permission_engine(
        self,
        rows: list[dict],
        user: User,
        skill_id: int,
        data_domain_id: int | None,
        db: Any,
        ownership: DataOwnership | None = None,
        desensitize_config: dict | None = None,
    ) -> list[dict]:
        """先跑原有三级可见性过滤，再通过 PermissionEngine 做三层字段脱敏。

        调用方传入 skill_id + data_domain_id（均可为 0/None），
        引擎会自动合并 Skill级 > 角色级 > 全局默认规则。
        """
        # 步骤1：原有三级可见性（行过滤）
        if ownership:
            rows = self.apply_visibility(rows, user, ownership, desensitize_config)

        if not rows:
            return rows

        # 步骤2：字段级三层脱敏
        try:
            from app.services.permission_engine import permission_engine
            rows = permission_engine.apply_data_masks(
                user=user,
                skill_id=skill_id or 0,
                data=rows,
                data_domain_id=data_domain_id,
                db=db,
            )
        except Exception:
            # 脱敏失败不影响主流程，降级返回原始行过滤结果
            pass

        return rows

    def apply_output_mask(
        self,
        data: dict,
        user: User,
        data_domain_id: int,
        db: Any,
    ) -> dict:
        """对单条输出记录做输出侧遮罩（⑥步骤）。"""
        try:
            from app.services.permission_engine import permission_engine
            return permission_engine.apply_output_masks(
                user=user,
                data=data,
                data_domain_id=data_domain_id,
                db=db,
            )
        except Exception:
            return data


data_visibility = DataVisibility()
