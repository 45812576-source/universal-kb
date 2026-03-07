"""TC-INTEL: Intelligence source and entry management."""
import pytest
from tests.conftest import (
    _make_user, _make_dept, _make_intel_source, _make_intel_entry, _login, _auth
)
from app.models.user import Role
from app.models.intel import IntelEntryStatus, IntelSourceType


# ── Source: role enforcement ─────────────────────────────────────────────────

def test_employee_cannot_list_sources(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "iemp1")
    resp = client.get("/api/intel/sources", headers=_auth(token))
    assert resp.status_code == 403


def test_admin_can_list_sources(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin1")
    resp = client.get("/api/intel/sources", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Source: CRUD ─────────────────────────────────────────────────────────────

def test_create_source(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin2")
    resp = client.post("/api/intel/sources", headers=_auth(token), json={
        "name": "测试RSS源",
        "source_type": "rss",
        "config": {"url": "https://example.com/rss"},
        "is_active": True,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "测试RSS源"
    assert resp.json()["source_type"] == "rss"


def test_update_source(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin3", Role.SUPER_ADMIN, dept.id)
    src = _make_intel_source(db, "旧名称")
    db.commit()
    token = _login(client, "iadmin3")
    resp = client.put(f"/api/intel/sources/{src.id}", headers=_auth(token), json={
        "name": "新名称",
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名称"
    assert resp.json()["is_active"] is False


def test_update_source_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin4", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin4")
    resp = client.put("/api/intel/sources/99999", headers=_auth(token), json={"name": "x"})
    assert resp.status_code == 404


def test_delete_source(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin5", Role.SUPER_ADMIN, dept.id)
    src = _make_intel_source(db)
    db.commit()
    token = _login(client, "iadmin5")
    resp = client.delete(f"/api/intel/sources/{src.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_source_dept_admin_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "idept1", Role.DEPT_ADMIN, dept.id)
    src = _make_intel_source(db)
    db.commit()
    token = _login(client, "idept1")
    resp = client.delete(f"/api/intel/sources/{src.id}", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: visibility ────────────────────────────────────────────────────────

def test_employee_only_sees_approved_entries(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp2", Role.EMPLOYEE, dept.id)
    _make_intel_entry(db, title="待审情报", status=IntelEntryStatus.PENDING)
    _make_intel_entry(db, title="已批准情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iemp2")
    resp = client.get("/api/intel/entries", headers=_auth(token))
    assert resp.status_code == 200
    titles = [e["title"] for e in resp.json()["items"]]
    assert "已批准情报" in titles
    assert "待审情报" not in titles


def test_admin_sees_all_entries(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin6", Role.SUPER_ADMIN, dept.id)
    _make_intel_entry(db, title="待审情报2", status=IntelEntryStatus.PENDING)
    _make_intel_entry(db, title="已批准情报2", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin6")
    resp = client.get("/api/intel/entries?status=pending", headers=_auth(token))
    titles = [e["title"] for e in resp.json()["items"]]
    assert "待审情报2" in titles


# ── Entry: approve/reject ────────────────────────────────────────────────────

def test_approve_entry(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin7", Role.SUPER_ADMIN, dept.id)
    entry = _make_intel_entry(db)
    db.commit()
    token = _login(client, "iadmin7")
    resp = client.patch(f"/api/intel/entries/{entry.id}/approve", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_reject_entry(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin8", Role.SUPER_ADMIN, dept.id)
    entry = _make_intel_entry(db)
    db.commit()
    token = _login(client, "iadmin8")
    resp = client.patch(f"/api/intel/entries/{entry.id}/reject", headers=_auth(token))
    assert resp.status_code == 200


def test_employee_cannot_approve(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp3", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, status=IntelEntryStatus.PENDING)
    db.commit()
    token = _login(client, "iemp3")
    resp = client.patch(f"/api/intel/entries/{entry.id}/approve", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: detail & non-approved access ──────────────────────────────────────

def test_get_approved_entry_as_employee(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp4", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, title="公开情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iemp4")
    resp = client.get(f"/api/intel/entries/{entry.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["title"] == "公开情报"


def test_get_pending_entry_as_employee_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp5", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, status=IntelEntryStatus.PENDING)
    db.commit()
    token = _login(client, "iemp5")
    resp = client.get(f"/api/intel/entries/{entry.id}", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: filter/pagination ──────────────────────────────────────────────────

def test_filter_entries_by_industry(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin9", Role.SUPER_ADMIN, dept.id)
    e1 = _make_intel_entry(db, title="食品情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    from app.models.intel import IntelEntry
    entry = db.get(IntelEntry, e1.id)
    entry.industry = "食品"
    db.commit()
    token = _login(client, "iadmin9")
    resp = client.get("/api/intel/entries?industry=食品", headers=_auth(token))
    assert resp.status_code == 200
    assert any(e["title"] == "食品情报" for e in resp.json()["items"])


def test_intel_entries_pagination(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin10", Role.SUPER_ADMIN, dept.id)
    for i in range(5):
        _make_intel_entry(db, title=f"情报{i}", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin10")
    resp = client.get("/api/intel/entries?page=1&page_size=2", headers=_auth(token))
    data = resp.json()
    assert data["total"] >= 5
    assert len(data["items"]) == 2


# ── Entry: search ─────────────────────────────────────────────────────────────

def test_search_entries_by_keyword(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin11", Role.SUPER_ADMIN, dept.id)
    _make_intel_entry(db, title="抖音直播带货趋势", status=IntelEntryStatus.APPROVED)
    _make_intel_entry(db, title="小红书种草策略", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin11")
    resp = client.get("/api/intel/entries?q=抖音", headers=_auth(token))
    titles = [e["title"] for e in resp.json()["items"]]
    assert "抖音直播带货趋势" in titles
    assert "小红书种草策略" not in titles
