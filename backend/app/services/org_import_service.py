"""组织管理导入流程控制：upload → parse → confirm → apply

支持 CSV/XLSX 表格解析，AI 整理，确认后写入正式表。
"""

import datetime
import io
import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.org_management import (
    BizProcess,
    BizTerminology,
    DataAssetOwnership,
    DeptCollaborationLink,
    DeptMissionDetail,
    KpiAssignment,
    OkrKeyResult,
    OkrObjective,
    OkrPeriod,
    OrgImportSession,
    PositionAccessRule,
)
from app.models.permission import Position
from app.models.user import Department, User
from app.services.org_baseline_sync import create_initial_baseline, sync_to_governance
from app.services.org_change_tracker import track_change, track_create

logger = logging.getLogger(__name__)


def parse_upload_file(file_content: bytes, filename: str) -> tuple[list[dict], int]:
    """解析 CSV/XLSX 文件为 list[dict]

    Returns:
        (rows, row_count)
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(file_content))
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(io.BytesIO(file_content))
        else:
            raise ValueError(f"不支持的文件格式: {ext}，请上传 CSV 或 XLSX")
    except Exception as e:
        raise ValueError(f"文件解析失败: {e}")

    # 清理列名
    df.columns = [str(c).strip() for c in df.columns]
    # NaN → None
    df = df.where(pd.notnull(df), None)
    rows = df.to_dict(orient="records")
    return rows, len(rows)


def create_import_session(
    db: Session,
    import_type: str,
    file_name: str,
    file_path: str,
    raw_data: list[dict],
    row_count: int,
    user_id: int,
) -> OrgImportSession:
    """创建导入会话"""
    session = OrgImportSession(
        import_type=import_type,
        file_name=file_name,
        file_path=file_path,
        raw_data=raw_data,
        status="uploading",
        row_count=row_count,
        created_by=user_id,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(session)
    db.flush()
    return session


async def run_ai_parse(db: Session, session: OrgImportSession, model_config: dict | None = None):
    """触发 AI 解析"""
    session.status = "parsing"
    db.flush()

    from app.services.org_import_ai import ai_parse_import_data

    try:
        parsed_data, parse_note = await ai_parse_import_data(
            db, session.import_type, session.raw_data, model_config
        )
        session.ai_parsed_data = parsed_data
        session.ai_parse_note = parse_note
        session.status = "parsed"

        # 计算解析成功行数
        if isinstance(parsed_data, list):
            session.parsed_count = len(parsed_data)
        elif isinstance(parsed_data, dict):
            # OKR 等嵌套结构
            session.parsed_count = session.row_count
        else:
            session.parsed_count = 0
    except Exception as e:
        session.status = "failed"
        session.ai_parse_note = f"解析失败: {e}"
        logger.exception(f"AI parse failed for session {session.id}")

    db.flush()


def confirm_session(db: Session, session_id: int) -> OrgImportSession:
    """用户确认解析结果"""
    session = db.query(OrgImportSession).get(session_id)
    if not session:
        raise ValueError("导入会话不存在")
    if session.status not in ("parsed",):
        raise ValueError(f"当前状态 {session.status} 不允许确认")
    session.status = "confirmed"
    db.flush()
    return session


def apply_session(db: Session, session_id: int, user_id: int) -> OrgImportSession:
    """将确认后的数据写入正式表"""
    session = db.query(OrgImportSession).get(session_id)
    if not session:
        raise ValueError("导入会话不存在")
    if session.status != "confirmed":
        raise ValueError(f"当前状态 {session.status} 不允许应用")

    data = session.ai_parsed_data
    if data is None:
        raise ValueError("没有可应用的数据")

    apply_fn = _APPLY_HANDLERS.get(session.import_type)
    if not apply_fn:
        raise ValueError(f"未实现的导入类型: {session.import_type}")

    try:
        apply_fn(db, data, session.id, user_id)
        session.status = "applied"
        session.applied_at = datetime.datetime.utcnow()

        # 判断是否首次导入 → 触发基线初始化
        applied_count = db.query(OrgImportSession).filter(
            OrgImportSession.status == "applied",
            OrgImportSession.id != session.id,
        ).count()
        if applied_count == 0:
            snapshot = create_initial_baseline(db, user_id, session.id)
            session.baseline_snapshot_id = snapshot.id

        db.flush()
    except Exception as e:
        session.status = "failed"
        session.ai_parse_note = (session.ai_parse_note or "") + f"\n应用失败: {e}"
        db.flush()
        raise

    return session


# ── 各类型 Apply 处理器 ──────────────────────────────────────────────────────

def _apply_org_structure(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入部门数据"""
    dept_name_map = {d.name: d for d in db.query(Department).all()}

    for row in data:
        name = row.get("name")
        if not name:
            continue
        existing = dept_name_map.get(name)
        if existing:
            # 更新
            for field in ("code", "level", "headcount_budget", "lifecycle_status", "category", "business_unit"):
                if row.get(field) is not None:
                    setattr(existing, field, row[field])
        else:
            parent_id = None
            parent_name = row.get("parent_name")
            if parent_name and parent_name in dept_name_map:
                parent_id = dept_name_map[parent_name].id

            dept = Department(
                name=name,
                parent_id=parent_id,
                code=row.get("code"),
                category=row.get("category"),
                business_unit=row.get("business_unit"),
                level=row.get("level"),
                headcount_budget=row.get("headcount_budget"),
                lifecycle_status=row.get("lifecycle_status", "active"),
            )
            db.add(dept)
            db.flush()
            dept_name_map[name] = dept
            track_create(db, "department", dept, user_id, "import", session_id)


def _apply_roster(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入花名册数据"""
    from app.services.auth_service import hash_password

    dept_map = {d.name: d.id for d in db.query(Department).all()}
    pos_map = {p.name: p.id for p in db.query(Position).all()}
    user_map = {u.display_name: u for u in db.query(User).all()}

    for row in data:
        display_name = row.get("display_name")
        if not display_name:
            continue

        dept_id = dept_map.get(row.get("department_name"))
        pos_id = pos_map.get(row.get("position_name"))

        existing = user_map.get(display_name)
        if existing:
            for field, col in [
                ("employee_no", "employee_no"), ("job_title", "job_title"),
                ("job_level", "job_level"), ("employee_status", "employee_status"),
                ("entry_date", "entry_date"), ("exit_date", "exit_date"),
            ]:
                val = row.get(col)
                if val is not None:
                    setattr(existing, field, val)
            if dept_id:
                existing.department_id = dept_id
            if pos_id:
                existing.position_id = pos_id
        else:
            username = row.get("username") or row.get("employee_no") or display_name
            user = User(
                username=username,
                password_hash=hash_password("changeme123"),
                display_name=display_name,
                department_id=dept_id,
                position_id=pos_id,
                employee_no=row.get("employee_no"),
                job_title=row.get("job_title"),
                job_level=row.get("job_level"),
                employee_status=row.get("employee_status", "active"),
                entry_date=row.get("entry_date"),
                exit_date=row.get("exit_date"),
            )
            db.add(user)
            db.flush()
            user_map[display_name] = user
            track_create(db, "user", user, user_id, "import", session_id)


def _apply_okr(db: Session, data: Any, session_id: int, user_id: int):
    """写入 OKR 数据"""
    if isinstance(data, dict):
        periods_data = data.get("periods", [])
        objectives_data = data.get("objectives", [])
    else:
        return

    period_map = {}
    for p in periods_data:
        period = OkrPeriod(
            name=p["name"],
            period_type=p.get("period_type", "quarter"),
            start_date=p["start_date"],
            end_date=p["end_date"],
            status="active",
            created_by=user_id,
        )
        db.add(period)
        db.flush()
        period_map[p["name"]] = period.id
        track_create(db, "okr_period", period, user_id, "import", session_id)

    dept_map = {d.name: d.id for d in db.query(Department).all()}
    user_map = {u.display_name: u.id for u in db.query(User).filter(User.is_active == True).all()}  # noqa: E712

    for obj_data in objectives_data:
        period_id = period_map.get(obj_data.get("period_name"))
        if not period_id:
            continue

        owner_type = obj_data.get("owner_type", "company")
        owner_id = 0
        if owner_type == "department":
            owner_id = dept_map.get(obj_data.get("owner_name"), 0)
        elif owner_type == "user":
            owner_id = user_map.get(obj_data.get("owner_name"), 0)

        objective = OkrObjective(
            period_id=period_id,
            owner_type=owner_type,
            owner_id=owner_id,
            title=obj_data["title"],
            weight=obj_data.get("weight", 1.0),
            status="active",
            import_session_id=session_id,
            created_by=user_id,
        )
        db.add(objective)
        db.flush()
        track_create(db, "okr_objective", objective, user_id, "import", session_id)

        for kr_data in obj_data.get("key_results", []):
            kr = OkrKeyResult(
                objective_id=objective.id,
                title=kr_data["title"],
                metric_type=kr_data.get("metric_type", "number"),
                target_value=kr_data.get("target_value"),
                current_value=kr_data.get("current_value"),
                unit=kr_data.get("unit"),
                weight=kr_data.get("weight", 1.0),
                owner_user_id=user_map.get(kr_data.get("owner_name")),
                import_session_id=session_id,
            )
            db.add(kr)
            db.flush()
            track_create(db, "okr_key_result", kr, user_id, "import", session_id)


def _apply_kpi(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入 KPI 数据"""
    user_map = {u.display_name: u for u in db.query(User).filter(User.is_active == True).all()}  # noqa: E712
    period_map = {p.name: p.id for p in db.query(OkrPeriod).all()}

    for row in data:
        user = user_map.get(row.get("user_name"))
        if not user:
            continue
        period_id = period_map.get(row.get("period_name"))
        if not period_id:
            continue

        assignment = KpiAssignment(
            user_id=user.id,
            period_id=period_id,
            position_id=user.position_id,
            department_id=user.department_id,
            kpi_data=row.get("kpi_items", []),
            total_score=row.get("total_score"),
            level=row.get("level"),
            status="evaluated",
            import_session_id=session_id,
        )
        db.add(assignment)
        db.flush()
        track_create(db, "kpi_assignment", assignment, user_id, "import", session_id)


def _apply_dept_mission(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入部门职责数据"""
    dept_map = {d.name: d.id for d in db.query(Department).all()}

    for row in data:
        dept_id = dept_map.get(row.get("department_name"))
        if not dept_id:
            continue

        existing = db.query(DeptMissionDetail).filter(DeptMissionDetail.department_id == dept_id).first()
        if existing:
            existing.mission_summary = row.get("mission_summary", existing.mission_summary)
            existing.core_functions = row.get("core_functions", existing.core_functions)
            existing.upstream_deps = row.get("upstream_deps", existing.upstream_deps)
            existing.downstream_deliveries = row.get("downstream_deliveries", existing.downstream_deliveries)
            existing.owned_data_types = row.get("owned_data_types", existing.owned_data_types)
        else:
            detail = DeptMissionDetail(
                department_id=dept_id,
                mission_summary=row.get("mission_summary"),
                core_functions=row.get("core_functions", []),
                upstream_deps=row.get("upstream_deps", []),
                downstream_deliveries=row.get("downstream_deliveries", []),
                owned_data_types=row.get("owned_data_types", []),
                import_session_id=session_id,
                created_by=user_id,
            )
            db.add(detail)
            db.flush()
            track_create(db, "dept_mission", detail, user_id, "import", session_id)

            # 同步到治理引擎
            from app.models.org_management import OrgChangeEvent
            evt = OrgChangeEvent(
                entity_type="dept_mission", entity_id=detail.id,
                change_type="imported", change_source="import",
                import_session_id=session_id, created_by=user_id,
            )
            db.add(evt)
            db.flush()
            sync_to_governance(db, evt)


def _apply_biz_process(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入业务流程数据"""
    for row in data:
        code = row.get("code")
        if not code:
            continue
        existing = db.query(BizProcess).filter(BizProcess.code == code).first()
        if existing:
            existing.name = row.get("name", existing.name)
            existing.description = row.get("description", existing.description)
            existing.process_nodes = row.get("process_nodes", existing.process_nodes)
        else:
            process = BizProcess(
                name=row["name"],
                code=code,
                description=row.get("description"),
                process_nodes=row.get("process_nodes", []),
                import_session_id=session_id,
                created_by=user_id,
            )
            db.add(process)
            db.flush()
            track_create(db, "biz_process", process, user_id, "import", session_id)


def _apply_terminology(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入业务术语数据"""
    dept_map = {d.name: d.id for d in db.query(Department).all()}

    for row in data:
        term_text = row.get("term")
        if not term_text:
            continue
        term = BizTerminology(
            term=term_text,
            aliases=row.get("aliases", []),
            definition=row.get("definition"),
            resource_library_code=row.get("resource_library_code"),
            department_id=dept_map.get(row.get("department_name")),
            import_session_id=session_id,
        )
        db.add(term)
        db.flush()
        track_create(db, "terminology", term, user_id, "import", session_id)

        # 同步到治理引擎
        from app.models.org_management import OrgChangeEvent
        evt = OrgChangeEvent(
            entity_type="terminology", entity_id=term.id,
            change_type="imported", change_source="import",
            import_session_id=session_id, created_by=user_id,
        )
        db.add(evt)
        db.flush()
        sync_to_governance(db, evt)


def _apply_data_asset(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入数据资产数据"""
    dept_map = {d.name: d.id for d in db.query(Department).all()}

    for row in data:
        asset_code = row.get("asset_code")
        if not asset_code:
            continue
        owner_dept_id = dept_map.get(row.get("owner_department_name"))
        if not owner_dept_id:
            continue
        consumer_ids = [dept_map[n] for n in row.get("consumer_department_names", []) if n in dept_map]

        asset = DataAssetOwnership(
            asset_name=row["asset_name"],
            asset_code=asset_code,
            owner_department_id=owner_dept_id,
            update_frequency=row.get("update_frequency", "manual"),
            consumer_department_ids=consumer_ids,
            resource_library_code=row.get("resource_library_code"),
            description=row.get("description"),
            import_session_id=session_id,
        )
        db.add(asset)
        db.flush()
        track_create(db, "data_asset", asset, user_id, "import", session_id)


def _apply_collab_matrix(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入协作矩阵数据"""
    dept_map = {d.name: d.id for d in db.query(Department).all()}

    for row in data:
        dept_a_id = dept_map.get(row.get("dept_a_name"))
        dept_b_id = dept_map.get(row.get("dept_b_name"))
        if not dept_a_id or not dept_b_id:
            continue

        link = DeptCollaborationLink(
            dept_a_id=dept_a_id,
            dept_b_id=dept_b_id,
            frequency=row.get("frequency", "medium"),
            scenarios=row.get("scenarios", []),
            import_session_id=session_id,
        )
        db.add(link)
        db.flush()
        track_create(db, "collab_link", link, user_id, "import", session_id)


def _apply_access_matrix(db: Session, data: list[dict], session_id: int, user_id: int):
    """写入访问矩阵数据"""
    pos_map = {p.name: p.id for p in db.query(Position).all()}

    for row in data:
        pos_id = pos_map.get(row.get("position_name"))
        if not pos_id:
            continue

        # upsert
        existing = db.query(PositionAccessRule).filter(
            PositionAccessRule.position_id == pos_id,
            PositionAccessRule.data_domain == row.get("data_domain"),
        ).first()

        if existing:
            existing.access_range = row.get("access_range", existing.access_range)
            existing.excluded_fields = row.get("excluded_fields", existing.excluded_fields)
        else:
            rule = PositionAccessRule(
                position_id=pos_id,
                data_domain=row["data_domain"],
                access_range=row.get("access_range", "none"),
                excluded_fields=row.get("excluded_fields", []),
                import_session_id=session_id,
            )
            db.add(rule)
            db.flush()
            track_create(db, "access_rule", rule, user_id, "import", session_id)


# ── Handler 映射 ─────────────────────────────────────────────────────────────

_APPLY_HANDLERS = {
    "org_structure": _apply_org_structure,
    "roster": _apply_roster,
    "okr": _apply_okr,
    "kpi": _apply_kpi,
    "dept_mission": _apply_dept_mission,
    "biz_process": _apply_biz_process,
    "terminology": _apply_terminology,
    "data_asset": _apply_data_asset,
    "collab_matrix": _apply_collab_matrix,
    "access_matrix": _apply_access_matrix,
}
