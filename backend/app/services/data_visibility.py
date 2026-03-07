"""Three-level data visibility: detail / desensitized / stats."""
from __future__ import annotations

from typing import Any

from app.models.business import DataOwnership, VisibilityLevel
from app.models.user import Role, User


class DataVisibility:

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

        # Owner: full detail
        if owner_field and row.get(owner_field) == user.id:
            return VisibilityLevel.DETAIL.value

        # Same department: desensitized
        if dept_field and user.department_id and row.get(dept_field) == user.department_id:
            return VisibilityLevel.DESENSITIZED.value

        # Dept admin can see desensitized for all rows
        if user.role == Role.DEPT_ADMIN:
            return VisibilityLevel.DESENSITIZED.value

        # Otherwise stats only
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
            # (aggregate stats should be returned separately if needed)
        return result


data_visibility = DataVisibility()
