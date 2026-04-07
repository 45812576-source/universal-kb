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
from app.utils.sql_safe import qi
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
    "L2": "aggregates",
    "L3": "rows",
    "L4": "rows",
}


@dataclass
class ViewReadResult:
    ok: bool = True
    error: str | None = None
    mode: str = "rows"  # rows | aggregates | mixed | decision | blocked
    table_id: int | None = None
    table_name: str = ""
    view_id: int | None = None
    view_name: str = ""
    # rows 模式
    fields: list[dict] = dc_field(default_factory=list)
    rows: list[dict] = dc_field(default_factory=list)
    # aggregates 模式
    group_by: list[str] = dc_field(default_factory=list)
    metrics: list[dict] = dc_field(default_factory=list)
    buckets: list[dict] = dc_field(default_factory=list)
    # mixed 模式
    grouped_rows: list[dict] = dc_field(default_factory=list)
    sample_rows: list[dict] = dc_field(default_factory=list)
    # 通用
    summary: dict = dc_field(default_factory=dict)
    warnings: list[str] = dc_field(default_factory=list)
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
            "warnings": self.warnings,
        }
        if self.error:
            d["error"] = self.error
        if self.mode == "rows":
            d["rows"] = self.rows
        elif self.mode == "aggregates":
            d["group_by"] = self.group_by
            d["metrics"] = self.metrics
            d["buckets"] = self.buckets
        elif self.mode == "mixed":
            d["grouped_rows"] = self.grouped_rows
            d["sample_rows"] = self.sample_rows
        if self.summary:
            d["summary"] = self.summary
        return d


# ─── 可用性判定 ────────────────────────────────────────────────────────────────

@dataclass
class ViewAvailability:
    available: bool = True
    risk_flags: list[str] = dc_field(default_factory=list)
    display_mode: str = "rows"
    view_state: str = "available"  # v4 §7.1
    unavailable_reason: str | None = None  # v4 §7.2


def assess_view_availability(
    view: TableView,
    policy: PolicyResult,
    bt: BusinessTable | None = None,
) -> ViewAvailability:
    """判定视图对当前用户是否可用，返回可用性 + 风险标记 + 展示模式。

    v4 §7.1 状态分类: available / invalid_schema / sync_failed /
    permission_blocked / risk_blocked / compile_failed
    """
    result = ViewAvailability()

    # v4 §7.1: view_state 检查
    view_state = getattr(view, "view_state", None) or "available"
    if view_state == "invalid_schema":
        result.available = False
        result.view_state = "invalid_schema"
        result.risk_flags.append("INVALID_SCHEMA")
        result.display_mode = "blocked"
        result.unavailable_reason = getattr(view, "view_invalid_reason", None) or "视图 schema 已失效"
        return result
    if view_state == "sync_failed":
        result.available = False
        result.view_state = "sync_failed"
        result.risk_flags.append("SYNC_FAILED")
        result.display_mode = "blocked"
        result.unavailable_reason = getattr(view, "view_invalid_reason", None) or "数据同步失败"
        return result
    if view_state == "compile_failed":
        result.available = False
        result.view_state = "compile_failed"
        result.risk_flags.append("COMPILE_FAILED")
        result.display_mode = "blocked"
        result.unavailable_reason = getattr(view, "view_invalid_reason", None) or "视图编译失败"
        return result

    # 无可见字段
    if not (view.visible_field_ids or []):
        result.available = False
        result.risk_flags.append("NO_FIELDS")
        result.view_state = "compile_failed"
        result.unavailable_reason = "视图无可见字段"

    # 权限拒绝
    if policy.denied:
        result.available = False
        result.view_state = "permission_blocked"
        result.risk_flags.append("ACCESS_DENIED")
        result.unavailable_reason = "无访问权限"
        return result

    # 披露级别
    effective_dl = policy.disclosure_level
    ceiling = view.disclosure_ceiling
    if ceiling and DISCLOSURE_ORDER.get(ceiling, 0) < DISCLOSURE_ORDER.get(effective_dl, 0):
        effective_dl = ceiling

    if effective_dl == "L0":
        result.available = False
        result.view_state = "risk_blocked"
        result.risk_flags.append("L0_BLOCKED")
        result.display_mode = "blocked"
        result.unavailable_reason = "披露级别为 L0（禁止访问）"
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

    # v4 §7.1: 最终状态判定
    if result.available:
        result.view_state = "available"

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
) -> tuple[str, dict]:
    """根据视图 + 权限构建 SQL。返回 (sql_string, bind_params)。"""
    bind_params: dict = {}

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
                agg_exprs.append(f"{func_name.upper()}({qi(col, '列名')}) AS {qi(alias, '别名')}")

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

    sql = f"SELECT {select_clause} FROM {qi(table_name, '表名')}"

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

    # 2. 行权限过滤（参数化）
    row_filter, row_params = build_row_filter_sql(policy, user, table_name)
    if row_filter:
        where_parts.append(row_filter)
        bind_params.update(row_params)

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

    # LIMIT — v4 §2.4: 优先级固定 1.权限引擎上限 2.视图row_limit 3.调用参数limit，取三者最小值
    policy_limit = getattr(policy, 'max_row_limit', None) or 500  # 权限引擎上限（默认500硬顶）
    view_limit = view.row_limit or 500
    effective_limit = min(policy_limit, view_limit, limit)
    effective_limit = max(1, min(effective_limit, 500))  # 硬顶500
    sql += f" LIMIT {effective_limit}"

    return sql, bind_params


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

    aggregate_mode = (output_mode == "aggregates")

    # v4 §2.3: mixed 模式仅在 L3/L4 且视图明确定义了 aggregate_rule_json 时可用
    has_aggregate_rules = bool(view.aggregate_rule_json and (view.aggregate_rule_json.get("aggregates") or view.aggregate_rule_json.get("group_by")))
    if effective_dl in ("L3", "L4") and has_aggregate_rules and not aggregate_mode:
        output_mode = "mixed"
        result.mode = "mixed"

    try:
        sql, sql_params = _build_view_sql(
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

        raw_result = db.execute(text(sql), sql_params)
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

    # 7. 组装结果 (v4 §2.3)
    if aggregate_mode:
        # aggregates 模式: group_by + metrics + buckets + summary
        agg_rules = view.aggregate_rule_json or {}
        result.group_by = agg_rules.get("group_by", [])
        result.metrics = agg_rules.get("aggregates", [])
        result.buckets = rows
        result.summary = {"total_buckets": len(rows)}
        result.applied_rules.append("L2_AGGREGATE_ONLY")
        result.total = len(rows)
        result.warnings.append("仅返回聚合结果，不包含明细行")
    elif output_mode == "mixed":
        # mixed 模式: summary + grouped_rows + sample_rows
        # 先跑一遍聚合 SQL
        try:
            agg_sql, agg_params = _build_view_sql(
                table_name=bt.table_name,
                visible_columns=visible_col_names,
                view=view,
                policy=policy,
                user=user,
                extra_filters=filters,
                limit=limit,
                aggregate_mode=True,
            )
            ok2, reason2 = data_engine.validate_sql(agg_sql, "read", [bt.table_name])
            if ok2:
                agg_result = db.execute(text(agg_sql), agg_params)
                agg_col_names = list(agg_result.keys())
                agg_raw = agg_result.fetchall()
                agg_rows = [dict(zip(agg_col_names, [_serialize_value(c) for c in row])) for row in agg_raw]
                result.grouped_rows = agg_rows
        except Exception:
            result.warnings.append("聚合查询失败，仅返回明细")

        result.sample_rows = rows[:20]  # 受控明细上限 20 行
        result.summary = {"total_rows": len(rows), "sample_count": min(len(rows), 20)}
        result.total = len(rows)
        result.applied_rules.append("MIXED_MODE")
        result.warnings.append("mixed 模式：同时返回聚合结果和受控明细样本")
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


# ─── v4 §5.2/§5.3: 视图失效检测与重编译 ────────────────────────────────────────

def revalidate_view(db: Session, view: TableView) -> tuple[bool, str | None]:
    """校验视图的字段/过滤/聚合/分组是否仍然有效。

    v4 §5.2: 每次同步后执行：
    - 视图字段存在性检查
    - 视图过滤字段存在性检查
    - 聚合字段类型检查
    - 分组字段可枚举性检查

    返回 (is_valid, reason_if_invalid)
    """
    bt = db.get(BusinessTable, view.table_id)
    if not bt:
        return False, "关联数据表不存在"

    all_fields = db.query(TableField).filter(TableField.table_id == view.table_id).all()
    field_ids = {f.id for f in all_fields}
    field_by_name = {(f.physical_column_name or f.field_name): f for f in all_fields}

    reasons = []

    # 1. 视图字段存在性检查
    view_field_ids = view.visible_field_ids or []
    missing_field_ids = [fid for fid in view_field_ids if fid not in field_ids]
    if missing_field_ids:
        reasons.append(f"视图引用了 {len(missing_field_ids)} 个已删除的字段")

    # 2. 视图过滤字段存在性检查
    filter_rules = view.filter_rule_json or {}
    for flt in (filter_rules.get("filters") or []):
        field_name = flt.get("field", "")
        if field_name and field_name not in field_by_name:
            reasons.append(f"过滤条件引用了不存在的字段: {field_name}")

    # 3. 聚合字段类型检查
    agg_rules = view.aggregate_rule_json or {}
    for agg in (agg_rules.get("aggregates") or []):
        field_name = agg.get("field", "")
        func_name = agg.get("func", "").upper()
        if field_name and field_name in field_by_name:
            f = field_by_name[field_name]
            if func_name in ("SUM", "AVG") and f.field_type not in ("number", "currency", "percent"):
                reasons.append(f"聚合函数 {func_name} 不适用于字段 {field_name} (类型: {f.field_type})")
        elif field_name:
            reasons.append(f"聚合引用了不存在的字段: {field_name}")

    # 4. 分组字段可枚举性检查
    for gf in (agg_rules.get("group_by") or []):
        if gf in field_by_name:
            f = field_by_name[gf]
            if f.field_type in ("long_text", "json", "attachment"):
                reasons.append(f"分组字段 {gf} 的类型 {f.field_type} 不适合分组")
        else:
            reasons.append(f"分组引用了不存在的字段: {gf}")

    # 5. 排序字段检查
    sort_rules = view.sort_rule_json or {}
    for sr in (sort_rules.get("sorts") or []):
        field_name = sr.get("field", "")
        if field_name and field_name not in field_by_name:
            reasons.append(f"排序引用了不存在的字段: {field_name}")

    if reasons:
        return False, "；".join(reasons)
    return True, None


def revalidate_table_views(db: Session, table_id: int) -> list[dict]:
    """v4 §5.2: 对表下所有视图执行失效检测，更新 view_state。"""
    views = db.query(TableView).filter(TableView.table_id == table_id).all()
    results = []

    for v in views:
        is_valid, reason = revalidate_view(db, v)
        old_state = getattr(v, "view_state", "available")

        if is_valid:
            v.view_state = "available"
            v.view_invalid_reason = None
        else:
            v.view_state = "invalid_schema"
            v.view_invalid_reason = reason

        results.append({
            "view_id": v.id,
            "view_name": v.name,
            "old_state": old_state,
            "new_state": v.view_state,
            "reason": reason,
        })

    db.flush()
    return results


def rebuild_default_view(db: Session, table_id: int) -> TableView | None:
    """v4 §5.3: 重建系统默认视图。"""
    from app.routers.data_assets import ensure_default_view
    # 先删除旧的系统默认视图
    old = db.query(TableView).filter(
        TableView.table_id == table_id,
        TableView.is_system == True,  # noqa: E712
        TableView.is_default == True,  # noqa: E712
    ).first()
    if old:
        db.delete(old)
        db.flush()

    return ensure_default_view(db, table_id)
