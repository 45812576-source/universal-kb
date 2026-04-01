"""视图执行层 — 将 TableView 配置解析为受权限裁剪的查询，统一服务 opencode / skill / 前端。

核心链路：
  TableView → 字段解析 + 过滤/排序/分组/聚合 → policy_engine 权限合并 → SQL 构建 → 执行 → 脱敏 → 输出
"""
from __future__ import annotations

import datetime
import decimal
import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import (
    AuditLog,
    BusinessTable,
    TableField,
    TableView,
)
from app.services.policy_engine import (
    DISCLOSURE_ORDER,
    PolicyResult,
    build_row_filter_sql,
    check_disclosure_capability,
    compute_visible_columns,
    compute_visible_fields,
    resolve_effective_policy,
    resolve_user_role_groups,
    apply_field_masking,
)

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


# ─── 输出模式 ─────────────────────────────────────────────────────────────────

OUTPUT_MODE_MAP = {
    "L0": "blocked",
    "L1": "decision",
    "L2": "aggregate",
    "L3": "rows",
    "L4": "rows",
}


@dataclass
class ViewReadResult:
    ok: bool = True
    error: str | None = None
    mode: str = "rows"  # rows | aggregate | decision | blocked
    table_id: int | None = None
    table_name: str = ""
    view_id: int | None = None
    view_name: str = ""
    fields: list[dict] = dc_field(default_factory=list)
    rows: list[dict] = dc_field(default_factory=list)
    summary: dict = dc_field(default_factory=dict)
    total: int = 0
    applied_rules: list[str] = dc_field(default_factory=list)
    disclosure_level: str = "L0"
    capabilities: dict = dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "ok": self.ok,
            "mode": self.mode,
            "table_id": self.table_id,
            "table_name": self.table_name,
            "view_id": self.view_id,
            "view_name": self.view_name,
            "fields": self.fields,
            "total": self.total,
            "applied_rules": self.applied_rules,
            "disclosure_level": self.disclosure_level,
            "capabilities": self.capabilities,
        }
        if self.error:
            d["error"] = self.error
        if self.mode == "rows":
            d["rows"] = self.rows
        if self.summary:
            d["summary"] = self.summary
        return d


# ─── 可用性判定 ────────────────────────────────────────────────────────────────

@dataclass
class ViewAvailability:
    available: bool = True
    risk_flags: list[str] = dc_field(default_factory=list)
    display_mode: str = "rows"


def assess_view_availability(
    view: TableView,
    policy: PolicyResult,
    bt: BusinessTable | None = None,
) -> ViewAvailability:
    """判定视图对当前用户是否可用，返回可用性 + 风险标记 + 展示模式。"""
    result = ViewAvailability()

    # 无可见字段
    if not (view.visible_field_ids or []):
        result.available = False
        result.risk_flags.append("NO_FIELDS")

    # 权限拒绝
    if policy.denied:
        result.available = False
        result.risk_flags.append("ACCESS_DENIED")
        return result

    # 披露级别
    effective_dl = policy.disclosure_level
    ceiling = view.disclosure_ceiling
    if ceiling and DISCLOSURE_ORDER.get(ceiling, 0) < DISCLOSURE_ORDER.get(effective_dl, 0):
        effective_dl = ceiling

    if effective_dl == "L0":
        result.available = False
        result.risk_flags.append("L0_BLOCKED")
        result.display_mode = "blocked"
    elif effective_dl == "L1":
        result.display_mode = "decision"
        result.risk_flags.append("DECISION_ONLY")
    elif effective_dl == "L2":
        result.display_mode = "aggregate"
        result.risk_flags.append("AGGREGATE_ONLY")
    elif effective_dl == "L3":
        result.display_mode = "rows"
        result.risk_flags.append("MASKED_DETAIL")
    else:  # L4
        result.display_mode = "rows"

    # 同步状态
    if bt and bt.sync_status == "failed":
        result.risk_flags.append("SYNC_FAILED")

    return result


# ─── 视图查询解析 ──────────────────────────────────────────────────────────────

def _serialize_value(v: Any) -> Any:
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


_SAFE_FIELD_RE = re.compile(r'^[\w\u4e00-\u9fff]+$')


def _build_view_sql(
    table_name: str,
    visible_columns: list[str],
    view: TableView,
    policy: PolicyResult,
    user: "User",
    extra_filters: list[dict] | None = None,
    extra_columns: list[str] | None = None,
    limit: int = 50,
    aggregate_mode: bool = False,
) -> str:
    """根据视图 + 权限构建 SQL。"""

    # 确定 SELECT 列
    if aggregate_mode:
        # 聚合模式：COUNT + 分组字段
        agg_rules = view.aggregate_rule_json or {}
        group_fields = []
        agg_exprs = ["COUNT(*) AS `_count`"]

        for rule in (agg_rules.get("aggregates") or []):
            func_name = rule.get("func", "COUNT")
            col = rule.get("field", "")
            alias = rule.get("alias", f"{func_name}_{col}")
            if col and _SAFE_FIELD_RE.match(col) and func_name.upper() in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
                agg_exprs.append(f"{func_name.upper()}(`{col}`) AS `{alias}`")

        for gf in (agg_rules.get("group_by") or []):
            if _SAFE_FIELD_RE.match(gf) and gf in visible_columns:
                group_fields.append(gf)

        if not group_fields:
            # 无分组 → 全表聚合
            select_clause = ", ".join(agg_exprs)
        else:
            select_clause = ", ".join(f"`{g}`" for g in group_fields) + ", " + ", ".join(agg_exprs)
    else:
        # 行模式
        if extra_columns:
            cols = [c for c in extra_columns if c in visible_columns]
            if not cols:
                cols = visible_columns
        else:
            cols = visible_columns
        select_clause = ", ".join(f"`{c}`" for c in cols)

    sql = f"SELECT {select_clause} FROM `{table_name}`"

    # WHERE 条件
    where_parts: list[str] = []

    # 1. 视图级过滤
    view_filters = view.filter_rule_json or {}
    for vf in (view_filters.get("filters") or []):
        field = vf.get("field", "")
        op = vf.get("op", "eq")
        val = vf.get("value", "")
        if field and _SAFE_FIELD_RE.match(field) and field in visible_columns:
            clause = _build_filter_clause(field, op, val)
            if clause:
                where_parts.append(clause)

    # 2. 行权限过滤
    row_filter = build_row_filter_sql(policy, user, table_name)
    if row_filter:
        where_parts.append(row_filter)

    # 3. 用户额外过滤
    for ef in (extra_filters or []):
        field = ef.get("field", "")
        op = ef.get("op", "eq")
        val = ef.get("value", "")
        if field and _SAFE_FIELD_RE.match(field) and field in visible_columns:
            clause = _build_filter_clause(field, op, val)
            if clause:
                where_parts.append(clause)

    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    # GROUP BY（聚合模式）
    if aggregate_mode:
        agg_rules = view.aggregate_rule_json or {}
        group_fields = [gf for gf in (agg_rules.get("group_by") or []) if _SAFE_FIELD_RE.match(gf) and gf in visible_columns]
        if group_fields:
            sql += " GROUP BY " + ", ".join(f"`{g}`" for g in group_fields)
    else:
        # ORDER BY（行模式）
        sort_rules = view.sort_rule_json or {}
        sort_parts = []
        for sr in (sort_rules.get("sorts") or []):
            field = sr.get("field", "")
            direction = sr.get("direction", "asc").upper()
            if field and _SAFE_FIELD_RE.match(field) and field in visible_columns and direction in ("ASC", "DESC"):
                sort_parts.append(f"`{field}` {direction}")
        if sort_parts:
            sql += " ORDER BY " + ", ".join(sort_parts)

    # LIMIT
    view_limit = view.row_limit
    effective_limit = min(limit, view_limit) if view_limit else limit
    effective_limit = min(effective_limit, 500)
    sql += f" LIMIT {effective_limit}"

    return sql


_OP_MAP = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "contains": "LIKE", "starts": "LIKE", "ends": "LIKE",
    "is_null": "IS NULL", "is_not_null": "IS NOT NULL",
}


def _build_filter_clause(field: str, op: str, val: Any) -> str | None:
    if op not in _OP_MAP:
        return None
    if op == "is_null":
        return f"`{field}` IS NULL"
    if op == "is_not_null":
        return f"`{field}` IS NOT NULL"

    sql_op = _OP_MAP[op]
    if op == "contains":
        val_escaped = str(val).replace("'", "''")
        return f"`{field}` LIKE '%{val_escaped}%'"
    elif op == "starts":
        val_escaped = str(val).replace("'", "''")
        return f"`{field}` LIKE '{val_escaped}%'"
    elif op == "ends":
        val_escaped = str(val).replace("'", "''")
        return f"`{field}` LIKE '%{val_escaped}'"
    elif isinstance(val, (int, float)):
        return f"`{field}` {sql_op} {val}"
    else:
        val_escaped = str(val).replace("'", "''")
        return f"`{field}` {sql_op} '{val_escaped}'"


# ─── 核心执行 ─────────────────────────────────────────────────────────────────

def execute_view_read(
    db: Session,
    view_id: int,
    user: "User",
    skill_id: int | None = None,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    limit: int = 50,
) -> ViewReadResult:
    """完整视图读取链路：权限检查 → SQL 构建 → 执行 → 脱敏 → 输出。"""

    # 1. 查视图 + 表
    view = db.get(TableView, view_id)
    if not view:
        return ViewReadResult(ok=False, error=f"视图 {view_id} 不存在")

    bt = db.get(BusinessTable, view.table_id)
    if not bt:
        return ViewReadResult(ok=False, error=f"视图关联的数据表不存在")

    if bt.is_archived:
        return ViewReadResult(ok=False, error=f"数据表 '{bt.display_name}' 已归档")

    # 2. 权限检查
    role_groups = resolve_user_role_groups(db, bt.id, user, skill_id=skill_id)
    group_ids = [g.id for g in role_groups]
    policy = resolve_effective_policy(db, bt.id, group_ids, view_id=view.id, skill_id=skill_id)

    if policy.denied:
        return ViewReadResult(
            ok=False,
            error="无权访问此视图: " + "; ".join(policy.deny_reasons),
            table_id=bt.id,
            table_name=bt.table_name,
            view_id=view.id,
            view_name=view.name,
            disclosure_level=policy.disclosure_level,
        )

    # 3. 计算有效披露级别
    effective_dl = policy.disclosure_level
    ceiling = view.disclosure_ceiling
    if ceiling and DISCLOSURE_ORDER.get(ceiling, 0) < DISCLOSURE_ORDER.get(effective_dl, 0):
        effective_dl = ceiling
    caps = check_disclosure_capability(effective_dl)
    output_mode = OUTPUT_MODE_MAP.get(effective_dl, "rows")

    # 4. 字段解析
    all_fields = db.query(TableField).filter(TableField.table_id == bt.id).order_by(TableField.sort_order).all()
    visible_fields = compute_visible_fields(all_fields, policy)

    # 再按视图 visible_field_ids 裁剪
    view_field_ids = set(view.visible_field_ids or [])
    if view_field_ids:
        visible_fields = [f for f in visible_fields if f.id in view_field_ids]

    if not visible_fields:
        return ViewReadResult(
            ok=False,
            error="此视图无可见字段",
            table_id=bt.id,
            table_name=bt.table_name,
            view_id=view.id,
            view_name=view.name,
        )

    # 构建可见列名
    visible_col_names = []
    field_name_map: dict[str, TableField] = {}
    for f in visible_fields:
        col_name = f.physical_column_name or f.field_name
        visible_col_names.append(col_name)
        field_name_map[col_name] = f

    fields_info = [
        {
            "id": f.id,
            "field_name": f.field_name,
            "display_name": f.display_name or f.field_name,
            "field_type": f.field_type,
            "is_enum": f.is_enum or False,
            "enum_values": f.enum_values or [],
            "is_sensitive": f.is_sensitive or False,
        }
        for f in visible_fields
    ]

    result = ViewReadResult(
        table_id=bt.id,
        table_name=bt.table_name,
        view_id=view.id,
        view_name=view.name,
        fields=fields_info,
        disclosure_level=effective_dl,
        capabilities=caps,
        mode=output_mode,
    )

    # 5. 根据模式执行
    if output_mode == "blocked":
        result.ok = False
        result.error = "此视图的披露级别不允许读取任何数据"
        result.applied_rules.append("L0_BLOCKED")
        return result

    if output_mode == "decision":
        result.applied_rules.append("L1_DECISION_ONLY")
        result.summary = {"message": "此视图仅提供决策级信息，不返回明细或汇总数据"}
        return result

    aggregate_mode = (output_mode == "aggregate")

    try:
        sql = _build_view_sql(
            table_name=bt.table_name,
            visible_columns=visible_col_names,
            view=view,
            policy=policy,
            user=user,
            extra_filters=filters,
            extra_columns=columns,
            limit=limit,
            aggregate_mode=aggregate_mode,
        )

        # SQL 安全校验
        from app.services.data_engine import data_engine
        ok, reason = data_engine.validate_sql(sql, "read", [bt.table_name])
        if not ok:
            return ViewReadResult(ok=False, error=f"SQL 校验失败: {reason}", table_id=bt.id, view_id=view.id)

        raw_result = db.execute(text(sql))
        col_names = list(raw_result.keys())
        raw_rows = raw_result.fetchall()
        rows = [dict(zip(col_names, [_serialize_value(c) for c in row])) for row in raw_rows]

    except Exception as e:
        logger.error(f"data_view_runtime execute failed: {e}")
        return ViewReadResult(ok=False, error=f"查询执行失败: {e}", table_id=bt.id, view_id=view.id)

    # 6. 脱敏
    if policy.masking_rules:
        rows = apply_field_masking(rows, policy.masking_rules, visible_fields)
        result.applied_rules.append("FIELD_MASKING")

    if effective_dl == "L3":
        result.applied_rules.append("L3_MASKED_DETAIL")

    # 7. 组装结果
    if aggregate_mode:
        result.summary = {"aggregates": rows}
        result.applied_rules.append("L2_AGGREGATE_ONLY")
        result.total = len(rows)
    else:
        result.rows = rows
        result.total = len(rows)

    # 8. 审计日志
    try:
        audit = AuditLog(
            user_id=user.id,
            table_name=bt.table_name,
            operation="opencode_view_read",
            new_values={
                "view_id": view.id,
                "view_name": view.name,
                "mode": output_mode,
                "row_count": len(rows),
                "disclosure_level": effective_dl,
                "skill_id": skill_id,
            },
        )
        db.add(audit)
        db.commit()
    except Exception:
        logger.warning("Failed to write audit log for view read", exc_info=True)

    return result
