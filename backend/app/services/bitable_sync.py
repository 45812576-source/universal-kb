"""飞书多维表格同步服务 — 支持全量/增量同步。"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.business import BusinessTable, TableField, TableSyncJob
from app.services.bitable_reader import bitable_reader, BitableReader, BitableRecordError
from app.utils.sql_safe import qi

logger = logging.getLogger(__name__)

# Bitable field type → MySQL type mapping
_BITABLE_TYPE_MAP = {
    1: "TEXT", 2: "DOUBLE", 3: "VARCHAR(50)", 4: "TEXT", 5: "DATETIME",
    7: "TINYINT(1)", 11: "TEXT", 13: "TEXT", 15: "TEXT", 17: "TEXT",
    18: "TEXT", 19: "BIGINT", 20: "DOUBLE", 21: "DOUBLE", 22: "BIGINT",
    23: "BIGINT", 24: "TEXT", 25: "TEXT", 1001: "TEXT", 1002: "TEXT",
    1003: "TEXT", 1004: "DOUBLE", 1005: "DOUBLE", 1006: "TEXT",
    1007: "TEXT", 1008: "TEXT",
}

# 飞书字段 type code → 内部 field_type 映射
_BITABLE_TO_FIELD_TYPE = {
    1: "text", 2: "number", 3: "single_select", 4: "multi_select",
    5: "date", 7: "boolean", 11: "person", 13: "text", 15: "url",
    17: "attachment", 18: "text", 19: "number", 20: "currency",
    21: "percent", 22: "number", 23: "number", 24: "department",
    25: "text", 1001: "text", 1002: "text", 1003: "text",
    1004: "number", 1005: "number",
}


class BitableSync:

    async def _get_token(self) -> str:
        return await bitable_reader.get_token()

    async def _fetch_fields(self, token: str, app_token: str, table_id: str) -> list[dict]:
        return await bitable_reader.fetch_fields(token, app_token, table_id)

    async def _fetch_records(
        self,
        token: str,
        app_token: str,
        table_id: str,
        since_ts: Optional[int] = None,
    ) -> tuple[list[dict], dict]:
        """拉取记录（自适应降级）。返回 (records, stats)。"""
        return await bitable_reader.fetch_records_adaptive(
            token, app_token, table_id, since_ts=since_ts,
        )

    def _normalize_fields(self, fields: list[dict]) -> list[dict]:
        """Normalize raw bitable field metadata for stable schema persistence.

        Backward-compatible behavior expected by tests and older callers:
        - blank field names are replaced with ``未命名字段{n}`` (1-based position);
        - duplicate names receive ``_{k}`` suffixes;
        - original source name is preserved in ``_source_field_name``.
        """
        normalized: list[dict] = []
        seen_names: dict[str, int] = {}

        for index, field in enumerate(fields, start=1):
            item = dict(field)
            source_name = (item.get("field_name") or "").strip()
            base_name = source_name or f"未命名字段{index}"

            duplicate_count = seen_names.get(base_name, 0)
            final_name = base_name if duplicate_count == 0 else f"{base_name}_{duplicate_count + 1}"
            seen_names[base_name] = duplicate_count + 1

            item["_source_field_name"] = source_name
            item["field_name"] = final_name
            normalized.append(item)

        return normalized

    def _build_col_map(self, fields: list[dict]) -> dict[str, str]:
        return {f["field_name"]: BitableReader.sanitize_col(f["field_name"]) for f in fields}

    def _create_sync_job(
        self, db: Session, bt: BusinessTable, job_type: str, triggered_by: int | None = None,
        trigger_source: str = "manual",
    ) -> TableSyncJob:
        """创建同步任务记录并标记表为 syncing 状态。"""
        job = TableSyncJob(
            table_id=bt.id,
            source_type=bt.source_type or "lark_bitable",
            job_type=job_type,
            status="running",
            started_at=datetime.datetime.utcnow(),
            triggered_by=triggered_by,
            trigger_source=trigger_source,
        )
        db.add(job)
        bt.sync_status = "syncing"
        db.flush()
        return job

    def _finish_sync_job(
        self, db: Session, bt: BusinessTable, job: TableSyncJob,
        status: str, stats: dict | None = None, error_message: str | None = None,
        error_type: str | None = None,
    ):
        """完成同步任务并更新表状态。"""
        job.status = status
        job.finished_at = datetime.datetime.utcnow()
        job.stats = stats or {}
        job.result_summary = stats or {}
        if error_message:
            job.error_message = error_message
            job.error_type = error_type or "unknown_error"
        db.flush()

        bt.sync_status = status if status in ("success", "partial_success", "failed") else "idle"
        bt.last_synced_at = datetime.datetime.utcnow()
        bt.last_sync_job_id = job.id
        if error_message:
            bt.sync_error = error_message
        else:
            bt.sync_error = None

    def _persist_schema_fields(self, db: Session, bt: BusinessTable, fields: list[dict]):
        """将飞书字段元信息落库到 table_fields，提取枚举值。"""
        existing = {tf.field_name: tf for tf in db.query(TableField).filter(TableField.table_id == bt.id).all()}
        seen = set()

        for idx, f in enumerate(fields):
            fname = f["field_name"]
            ftype_code = f.get("type", 1)
            field_type = _BITABLE_TO_FIELD_TYPE.get(ftype_code, "text")
            col_name = BitableReader.sanitize_col(fname)
            seen.add(fname)

            # 提取枚举值（single_select / multi_select）
            enum_values = None
            enum_source = None
            property_data = f.get("property", {}) or {}
            options = property_data.get("options", [])
            if options and ftype_code in (3, 4):
                enum_values = [opt.get("name", "") for opt in options if opt.get("name")]
                enum_source = "source_declared"

            if fname in existing:
                tf = existing[fname]
                tf.display_name = fname
                tf.physical_column_name = col_name
                tf.field_type = field_type
                tf.source_field_type = str(ftype_code)
                tf.sort_order = idx
                if enum_values is not None:
                    tf.enum_values = enum_values
                    tf.enum_source = enum_source
                tf.description = f.get("description", tf.description)
            else:
                tf = TableField(
                    table_id=bt.id,
                    field_name=fname,
                    display_name=fname,
                    physical_column_name=col_name,
                    field_type=field_type,
                    source_field_type=str(ftype_code),
                    is_nullable=True,
                    is_system=fname.startswith("_"),
                    enum_values=enum_values,
                    enum_source=enum_source,
                    description=f.get("description"),
                    sort_order=idx,
                )
                db.add(tf)

        # 删除飞书已移除的字段
        for fname, tf in existing.items():
            if fname not in seen and not tf.is_system:
                db.delete(tf)

        db.flush()

    def _upsert_records(
        self,
        db: Session,
        table_name: str,
        fields: list[dict],
        records: list[dict],
        col_map: dict[str, str],
    ) -> tuple[int, int]:
        """UPSERT records。返回 (inserted, updated)。"""
        field_names = [f["field_name"] for f in fields]
        inserted = 0
        updated = 0

        for rec in records:
            record_id = rec.get("record_id", "")
            flds = rec.get("fields", {})
            row_data = {"_record_id": record_id}
            for fn in field_names:
                col = col_map.get(fn, BitableReader.sanitize_col(fn))
                row_data[col] = BitableReader.flatten_value(flds.get(fn))

            cols_sql = ", ".join(f"`{k}`" for k in row_data)
            placeholders = ", ".join(f":{k}" for k in row_data)
            update_parts = ", ".join(
                f"`{k}` = :{k}" for k in row_data if k != "_record_id"
            )

            sql = (
                f"INSERT INTO {qi(table_name, '表名')} ({cols_sql}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_parts}"
            )
            try:
                result = db.execute(text(sql), row_data)
                if result.rowcount == 1:
                    inserted += 1
                elif result.rowcount == 2:  # MySQL ON DUPLICATE KEY UPDATE returns 2 for update
                    updated += 1
            except Exception as e:
                logger.warning(f"Upsert failed for record {record_id}: {e}")

        db.commit()
        return inserted, updated

    async def full_sync(
        self,
        db: Session,
        app_token: str,
        table_id: str,
        table_name: str,
        display_name: str = "",
        owner_id: int | None = None,
        triggered_by: int | None = None,
        trigger_source: str = "manual",
        existing_job: TableSyncJob | None = None,
    ) -> dict:
        """全量同步：DROP 重建 + 插入全部记录。

        如果传入 existing_job，则复用该 job 记录（不再内部创建新 job）。
        """
        # 注册/更新 BusinessTable（需要先有 bt 才能建 sync job）
        bt = self._register_table(db, table_name, display_name or table_name, app_token, table_id, "", owner_id)
        if existing_job is not None:
            job = existing_job
            job.table_id = bt.id
            job.status = "running"
            job.started_at = datetime.datetime.utcnow()
            bt.sync_status = "syncing"
            db.flush()
        else:
            job = self._create_sync_job(db, bt, "full_sync", triggered_by, trigger_source)
            job.stage = "queued"
        db.commit()

        try:
            job.stage = "fetch_fields"
            db.commit()
            token = await self._get_token()
            fields = await self._fetch_fields(token, app_token, table_id)

            job.stage = "fetch_records"
            db.commit()
            records, fetch_stats = await self._fetch_records(token, app_token, table_id)

            col_map = self._build_col_map(fields)

            # 建表
            job.stage = "create_table"
            db.commit()
            col_defs = ["  `_record_id` VARCHAR(100) PRIMARY KEY COMMENT '飞书记录ID'"]
            seen_cols = {"_record_id", "_synced_at"}
            for f in fields:
                col = col_map[f["field_name"]]
                if not col or col in seen_cols:
                    continue
                seen_cols.add(col)
                mysql_type = _BITABLE_TYPE_MAP.get(f.get("type", 1), "TEXT")
                col_defs.append(f"  `{col}` {mysql_type} COMMENT '{f['field_name']}'")
            col_defs.append("  `_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
            ddl = f"CREATE TABLE IF NOT EXISTS {qi(table_name, '表名')} (\n" + ",\n".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

            db.execute(text(f"DROP TABLE IF EXISTS {qi(table_name, '表名')}"))
            db.execute(text(ddl))
            db.commit()

            job.stage = "insert_records"
            db.commit()
            inserted, updated = self._upsert_records(db, table_name, fields, records, col_map)

            # 更新 DDL
            job.stage = "register"
            bt.ddl_sql = ddl

            # 字段元信息落库
            self._persist_schema_fields(db, bt, fields)

            sync_status = "partial_success" if fetch_stats.get("truncated") else "success"

            stats = {
                "inserted": inserted, "updated": updated,
                "total_fields": len(fields), "total_records": len(records),
                "truncated": fetch_stats.get("truncated", False),
                "fetch_stats": fetch_stats,
            }
            job.stage = "done"
            self._finish_sync_job(db, bt, job, sync_status, stats)

            # 确保默认系统视图存在
            try:
                from app.routers.data_assets import ensure_default_view
                ensure_default_view(db, bt.id)
            except Exception as e:
                logger.warning(f"ensure_default_view failed for {table_name}: {e}")

            db.commit()

            # 触发字段画像（异步，不阻塞同步完成）
            try:
                from app.services.field_profiler import profile_table
                source_schema = [{"field_name": f["field_name"], "type": f.get("type", 1), "property": f.get("property")} for f in fields]
                await profile_table(db, bt, source_schema_fields=source_schema)
            except Exception as e:
                logger.warning(f"Field profiler failed for {table_name}: {e}")

            return {
                "table_name": table_name,
                "total_fields": len(fields),
                "inserted": inserted,
                "updated": updated,
                "mode": "full",
                "sync_job_id": job.id,
                "effective_page_size": fetch_stats.get("effective_page_size"),
                "degraded": fetch_stats.get("degraded", False),
                "sync_stats": fetch_stats,
            }
        except Exception as e:
            error_type = "auth_error" if "token" in str(e).lower() else "network_error" if "timeout" in str(e).lower() else "unknown_error"
            job.stage = "failed"
            self._finish_sync_job(db, bt, job, "failed", error_message=str(e), error_type=error_type)
            db.commit()
            raise

    async def incremental_sync(
        self,
        db: Session,
        bt: BusinessTable,
        triggered_by: int | None = None,
        trigger_source: str = "manual",
    ) -> dict:
        """增量同步：只拉 last_synced_at 之后修改的记录，UPSERT 到现有表。"""
        rules = bt.validation_rules or {}
        app_token = rules.get("bitable_app_token", "") or (bt.source_ref or {}).get("app_token", "")
        table_id = rules.get("bitable_table_id", "") or (bt.source_ref or {}).get("table_id", "")
        if not app_token or not table_id:
            return {"ok": False, "error": "该表未关联飞书多维表格"}

        last_synced = rules.get("last_synced_at", 0)
        # 也检查新字段
        if not last_synced and bt.last_synced_at:
            last_synced = int(bt.last_synced_at.timestamp())

        job = self._create_sync_job(db, bt, "incremental_sync", triggered_by, trigger_source)
        db.commit()

        try:
            token = await self._get_token()
            fields = await self._fetch_fields(token, app_token, table_id)
            records, fetch_stats = await self._fetch_records(
                token, app_token, table_id,
                since_ts=last_synced if last_synced else None,
            )

            col_map = self._build_col_map(fields)
            inserted, updated = self._upsert_records(db, bt.table_name, fields, records, col_map)

            # 更新 last_synced_at（保持旧字段兼容 + 新字段）
            rules["last_synced_at"] = int(time.time())
            bt.validation_rules = rules
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(bt, "validation_rules")

            # 字段元信息落库
            self._persist_schema_fields(db, bt, fields)

            has_fatal = any(e.get("fatal") for e in fetch_stats.get("errors", []))
            sync_status = "partial_success" if (records and has_fatal) else "success"

            stats = {"inserted": inserted, "updated": updated, "total_fetched": len(records), "fetch_stats": fetch_stats}
            self._finish_sync_job(db, bt, job, sync_status, stats)

            # 确保默认系统视图存在
            try:
                from app.routers.data_assets import ensure_default_view
                ensure_default_view(db, bt.id)
            except Exception as e:
                logger.warning(f"ensure_default_view failed for {bt.table_name}: {e}")

            db.commit()

            # 触发字段画像
            try:
                from app.services.field_profiler import profile_table
                source_schema = [{"field_name": f["field_name"], "type": f.get("type", 1), "property": f.get("property")} for f in fields]
                await profile_table(db, bt, source_schema_fields=source_schema)
            except Exception as e:
                logger.warning(f"Field profiler failed for {bt.table_name}: {e}")

            return {
                "table_name": bt.table_name,
                "inserted": inserted,
                "updated": updated,
                "total_fetched": len(records),
                "mode": "incremental" if last_synced else "full_initial",
                "sync_job_id": job.id,
                "effective_page_size": fetch_stats.get("effective_page_size"),
                "degraded": fetch_stats.get("degraded", False),
                "sync_stats": fetch_stats,
            }
        except Exception as e:
            error_type = "auth_error" if "token" in str(e).lower() else "network_error" if "timeout" in str(e).lower() else "unknown_error"
            self._finish_sync_job(db, bt, job, "failed", error_message=str(e), error_type=error_type)
            db.commit()
            raise

    def _register_table(
        self,
        db: Session,
        table_name: str,
        display_name: str,
        app_token: str,
        table_id: str,
        ddl: str,
        owner_id: int | None,
    ) -> BusinessTable:
        from sqlalchemy.orm.attributes import flag_modified
        existing = db.query(BusinessTable).filter(BusinessTable.table_name == table_name).first()
        now = int(time.time())
        if existing:
            # 如果同名表属于别人（且当前请求指定了 owner），拒绝覆盖
            if owner_id and existing.owner_id and existing.owner_id != owner_id:
                raise ValueError(
                    f"物理表名 '{table_name}' 已被其他用户占用，"
                    f"请在「显示名称」或「sync_table_name」中指定一个不同的名称"
                )
            existing.display_name = display_name
            existing.description = f"飞书多维表格同步 | app_token={app_token} | table_id={table_id}"
            if owner_id:
                existing.owner_id = owner_id
            if ddl:
                existing.ddl_sql = ddl
            rules = dict(existing.validation_rules or {})
            rules.update({
                "bitable_app_token": app_token,
                "bitable_table_id": table_id,
                "last_synced_at": now,
            })
            rules.setdefault("row_scope", "private")
            rules.setdefault("column_scope", "private")
            existing.validation_rules = rules
            flag_modified(existing, "validation_rules")
            # 同步新字段
            existing.source_type = "lark_bitable"
            existing.source_ref = {"app_token": app_token, "table_id": table_id}
            db.flush()
            return existing
        else:
            bt = BusinessTable(
                table_name=table_name,
                display_name=display_name,
                description=f"飞书多维表格同步 | app_token={app_token} | table_id={table_id}",
                ddl_sql=ddl,
                validation_rules={
                    "bitable_app_token": app_token,
                    "bitable_table_id": table_id,
                    "last_synced_at": now,
                    "row_scope": "private",
                    "column_scope": "private",
                },
                owner_id=owner_id,
                source_type="lark_bitable",
                source_ref={"app_token": app_token, "table_id": table_id},
            )
            db.add(bt)
            db.flush()
            return bt


bitable_sync = BitableSync()
