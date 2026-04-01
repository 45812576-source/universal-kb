"""字段画像服务 — 分析表字段的统计特征。

两种模式:
1. 飞书来源 (source_schema_fields 非空): 字段类型/枚举已从同步落库，只补 sample_values / distinct_count / null_ratio
2. 非飞书来源 (source_schema_fields is None): 从 INFORMATION_SCHEMA + 数据采样推断全部信息
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import BusinessTable, TableField

logger = logging.getLogger(__name__)

# 根据 field_type 推断筛选/分组/排序能力
_GROUPABLE_TYPES = {"single_select", "multi_select", "boolean", "person", "department"}
_NON_SORTABLE_TYPES = {"attachment", "json", "multi_select"}

# 枚举源优先级: synced > manual > inferred > observed
ENUM_SOURCE_PRIORITY = {"synced": 3, "manual": 2, "inferred": 1, "observed": 0}


def _infer_field_capabilities(field_type: str) -> dict:
    return {
        "is_filterable": field_type not in ("attachment", "json"),
        "is_groupable": field_type in _GROUPABLE_TYPES,
        "is_sortable": field_type not in _NON_SORTABLE_TYPES,
    }


async def profile_table(
    db: Session,
    bt: BusinessTable,
    source_schema_fields: Optional[list[dict]] = None,
):
    """分析表字段画像并更新 table_fields + business_tables.field_profile_status。"""
    try:
        bt.field_profile_status = "profiling"
        db.flush()

        if source_schema_fields is not None:
            await _profile_with_source_schema(db, bt)
        else:
            await _profile_from_scratch(db, bt)

        bt.field_profile_status = "ready"
        bt.field_profile_error = None

        # 更新记录数缓存
        try:
            result = db.execute(text(f"SELECT COUNT(*) FROM `{bt.table_name}`"))
            bt.record_count_cache = result.scalar()
        except Exception:
            pass

        db.commit()
    except Exception as e:
        logger.error(f"Field profiling failed for {bt.table_name}: {e}")
        bt.field_profile_status = "failed"
        bt.field_profile_error = str(e)[:500]
        db.commit()
        raise


async def _profile_with_source_schema(db: Session, bt: BusinessTable):
    """飞书来源: 字段类型/枚举已落库，只补统计数据。"""
    fields = db.query(TableField).filter(TableField.table_id == bt.id).all()
    for tf in fields:
        caps = _infer_field_capabilities(tf.field_type)
        tf.is_filterable = caps["is_filterable"]
        tf.is_groupable = caps["is_groupable"]
        tf.is_sortable = caps["is_sortable"]

        col = tf.physical_column_name or tf.field_name
        try:
            _fill_stats(db, bt.table_name, col, tf)
        except Exception as e:
            logger.warning(f"Stats failed for {bt.table_name}.{col}: {e}")

    db.flush()


async def _profile_from_scratch(db: Session, bt: BusinessTable):
    """非飞书来源: 从 INFORMATION_SCHEMA 读列信息 + 数据采样。"""
    # 读取物理列信息
    result = db.execute(text(
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl "
        "ORDER BY ORDINAL_POSITION"
    ), {"tbl": bt.table_name})
    columns = result.fetchall()

    existing = {tf.field_name: tf for tf in db.query(TableField).filter(TableField.table_id == bt.id).all()}
    seen = set()

    for idx, (col_name, data_type, nullable, comment) in enumerate(columns):
        if col_name.startswith("_"):
            continue
        seen.add(col_name)
        field_type = _mysql_type_to_field_type(data_type)

        if col_name in existing:
            tf = existing[col_name]
            tf.field_type = tf.field_type or field_type  # 不覆盖已有类型
            tf.physical_column_name = col_name
            tf.is_nullable = nullable == "YES"
            tf.sort_order = idx
        else:
            tf = TableField(
                table_id=bt.id,
                field_name=comment or col_name,
                display_name=comment or col_name,
                physical_column_name=col_name,
                field_type=field_type,
                is_nullable=nullable == "YES",
                sort_order=idx,
            )
            db.add(tf)
            db.flush()

        caps = _infer_field_capabilities(tf.field_type)
        tf.is_filterable = caps["is_filterable"]
        tf.is_groupable = caps["is_groupable"]
        tf.is_sortable = caps["is_sortable"]

        try:
            _fill_stats(db, bt.table_name, col_name, tf)
            # 对非飞书来源，推断枚举值（但不覆盖更高优先级的来源）
            existing_priority = ENUM_SOURCE_PRIORITY.get(tf.enum_source or "", -1)
            new_priority = ENUM_SOURCE_PRIORITY.get("observed", 0)
            if existing_priority < new_priority and tf.distinct_count_cache and tf.distinct_count_cache <= 20:
                total = db.execute(text(f"SELECT COUNT(*) FROM `{bt.table_name}`")).scalar() or 1
                ratio = tf.distinct_count_cache / total
                if ratio <= 0.6:
                    vals = db.execute(text(
                        f"SELECT DISTINCT `{col_name}` FROM `{bt.table_name}` "
                        f"WHERE `{col_name}` IS NOT NULL LIMIT 20"
                    ))
                    tf.enum_values = [str(r[0]) for r in vals if r[0] is not None]
                    tf.enum_source = "observed"
                    if tf.field_type == "text":
                        tf.field_type = "single_select"
                        tf.is_groupable = True
        except Exception as e:
            logger.warning(f"Stats failed for {bt.table_name}.{col_name}: {e}")

    db.flush()


def _fill_stats(db: Session, table_name: str, col_name: str, tf: TableField):
    """填充 sample_values / distinct_count / null_ratio。"""
    # distinct count
    result = db.execute(text(
        f"SELECT COUNT(DISTINCT `{col_name}`) FROM `{table_name}`"
    ))
    tf.distinct_count_cache = result.scalar()

    # null ratio
    result = db.execute(text(
        f"SELECT COUNT(*) as total, SUM(CASE WHEN `{col_name}` IS NULL THEN 1 ELSE 0 END) as nulls "
        f"FROM `{table_name}`"
    ))
    row = result.fetchone()
    total, nulls = row[0] or 0, row[1] or 0
    tf.null_ratio = round(nulls / total, 4) if total > 0 else None

    # 敏感字段不采集 sample_values
    if tf.is_sensitive:
        tf.sample_values = []
        return

    # sample values (max 10 distinct non-null)
    result = db.execute(text(
        f"SELECT DISTINCT `{col_name}` FROM `{table_name}` "
        f"WHERE `{col_name}` IS NOT NULL LIMIT 10"
    ))
    tf.sample_values = [str(r[0]) for r in result if r[0] is not None]


def suggest_enum_upgrade(db: Session, table_id: int) -> list[dict]:
    """找出可能应该升级为枚举的 free text 字段。"""
    from app.models.business import BusinessTable
    bt = db.get(BusinessTable, table_id)
    if not bt:
        return []

    fields = db.query(TableField).filter(
        TableField.table_id == table_id,
        TableField.is_free_text == True,  # noqa: E712
        TableField.is_enum == False,  # noqa: E712
    ).all()

    suggestions = []
    for tf in fields:
        if tf.distinct_count_cache and tf.distinct_count_cache <= 20:
            total = bt.record_count_cache or 1
            ratio = tf.distinct_count_cache / total if total > 0 else 1
            if ratio <= 0.5:
                suggestions.append({
                    "field_id": tf.id,
                    "field_name": tf.field_name,
                    "display_name": tf.display_name,
                    "distinct_count": tf.distinct_count_cache,
                    "suggested_values": tf.sample_values[:20] if tf.sample_values else [],
                })
    return suggestions


def _mysql_type_to_field_type(data_type: str) -> str:
    """MySQL DATA_TYPE → 内部 field_type。"""
    dt = data_type.lower()
    if dt in ("int", "bigint", "smallint", "tinyint", "mediumint", "float", "double", "decimal"):
        return "number"
    if dt in ("datetime", "timestamp"):
        return "datetime"
    if dt == "date":
        return "date"
    if dt in ("tinyint(1)", "bit"):
        return "boolean"
    if dt == "json":
        return "json"
    return "text"
