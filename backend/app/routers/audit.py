"""Audit log API — paginated view for admins."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models.business import AuditLog
from app.models.user import User, Role

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("")
def list_audit_logs(
    table_name: str = Query(None),
    user_id: int = Query(None),
    operation: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    q = db.query(AuditLog)
    if table_name:
        q = q.filter(AuditLog.table_name == table_name)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    if operation:
        q = q.filter(AuditLog.operation == operation.upper())

    total = q.count()
    logs = (
        q.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "logs": [
            {
                "id": log.id,
                "user_id": log.user_id,
                "table_name": log.table_name,
                "operation": log.operation,
                "row_id": log.row_id,
                "old_values": log.old_values,
                "new_values": log.new_values,
                "sql_executed": log.sql_executed,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ],
    }
