"""TC-AUDIT: Audit log listing, pagination, and filters."""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.models.business import AuditLog


def _seed_audit_log(db, user_id, table_name="sales_records", operation="INSERT", row_id="1"):
    log = AuditLog(
        user_id=user_id,
        table_name=table_name,
        operation=operation,
        row_id=row_id,
        new_values={"amount": 100},
    )
    db.add(log)
    db.flush()
    return log


# ── Access control ────────────────────────────────────────────────────────────

def test_employee_cannot_view_audit_logs(client, db):
    dept = _make_dept(db)
    _make_user(db, "audemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "audemp1")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    assert resp.status_code == 403


def test_super_admin_can_view_audit_logs(client, db):
    dept = _make_dept(db)
    _make_user(db, "audadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "audadmin1")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "logs" in data
    assert "total" in data


def test_dept_admin_can_view_audit_logs(client, db):
    dept = _make_dept(db)
    _make_user(db, "auddept1", Role.DEPT_ADMIN, dept.id)
    db.commit()
    token = _login(client, "auddept1")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    assert resp.status_code == 200


# ── List audit logs ───────────────────────────────────────────────────────────

def test_audit_logs_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "audadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "audadmin2")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["logs"] == []


def test_audit_logs_returns_seeded_data(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin3", Role.SUPER_ADMIN, dept.id)
    _seed_audit_log(db, admin.id, "orders", "INSERT", "1")
    _seed_audit_log(db, admin.id, "orders", "UPDATE", "1")
    db.commit()
    token = _login(client, "audadmin3")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


# ── Filters ───────────────────────────────────────────────────────────────────

def test_filter_by_table_name(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin4", Role.SUPER_ADMIN, dept.id)
    _seed_audit_log(db, admin.id, "table_a", "INSERT")
    _seed_audit_log(db, admin.id, "table_b", "INSERT")
    db.commit()
    token = _login(client, "audadmin4")

    resp = client.get("/api/audit-logs?table_name=table_a", headers=_auth(token))
    assert resp.status_code == 200
    logs = resp.json()["logs"]
    assert all(log["table_name"] == "table_a" for log in logs)
    assert resp.json()["total"] == 1


def test_filter_by_user_id(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin5a", Role.SUPER_ADMIN, dept.id)
    other = _make_user(db, "audadmin5b", Role.SUPER_ADMIN, dept.id)
    _seed_audit_log(db, admin.id, "tbl", "INSERT")
    _seed_audit_log(db, other.id, "tbl", "INSERT")
    db.commit()
    token = _login(client, "audadmin5a")

    resp = client.get(f"/api/audit-logs?user_id={admin.id}", headers=_auth(token))
    assert resp.status_code == 200
    logs = resp.json()["logs"]
    assert all(log["user_id"] == admin.id for log in logs)
    assert resp.json()["total"] == 1


def test_filter_by_operation(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin6", Role.SUPER_ADMIN, dept.id)
    _seed_audit_log(db, admin.id, "tbl2", "INSERT")
    _seed_audit_log(db, admin.id, "tbl2", "DELETE")
    db.commit()
    token = _login(client, "audadmin6")

    resp = client.get("/api/audit-logs?operation=INSERT", headers=_auth(token))
    assert resp.status_code == 200
    logs = resp.json()["logs"]
    assert all(log["operation"] == "INSERT" for log in logs)
    assert resp.json()["total"] == 1


# ── Pagination ────────────────────────────────────────────────────────────────

def test_pagination(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin7", Role.SUPER_ADMIN, dept.id)
    for i in range(5):
        _seed_audit_log(db, admin.id, "paginate_tbl", "INSERT", str(i))
    db.commit()
    token = _login(client, "audadmin7")

    resp = client.get("/api/audit-logs?page=1&page_size=2", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["logs"]) == 2
    assert data["page"] == 1
    assert data["page_size"] == 2


def test_pagination_page2(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin8", Role.SUPER_ADMIN, dept.id)
    for i in range(5):
        _seed_audit_log(db, admin.id, "paginate2_tbl", "INSERT", str(i))
    db.commit()
    token = _login(client, "audadmin8")

    resp_p1 = client.get("/api/audit-logs?page=1&page_size=3&table_name=paginate2_tbl", headers=_auth(token))
    resp_p2 = client.get("/api/audit-logs?page=2&page_size=3&table_name=paginate2_tbl", headers=_auth(token))

    p1_ids = {log["id"] for log in resp_p1.json()["logs"]}
    p2_ids = {log["id"] for log in resp_p2.json()["logs"]}
    assert len(p2_ids) == 2
    assert p1_ids.isdisjoint(p2_ids)


def test_audit_log_fields(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "audadmin9", Role.SUPER_ADMIN, dept.id)
    _seed_audit_log(db, admin.id, "fields_tbl", "UPDATE", "42")
    db.commit()
    token = _login(client, "audadmin9")

    resp = client.get("/api/audit-logs", headers=_auth(token))
    log = resp.json()["logs"][0]
    assert "id" in log
    assert "user_id" in log
    assert "table_name" in log
    assert "operation" in log
    assert "row_id" in log
    assert "created_at" in log
