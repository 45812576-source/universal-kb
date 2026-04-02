"""TC-ADMIN: Model config CRUD and department listing, role enforcement."""
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from tests.conftest import _make_user, _make_dept, _make_model_config, _login, _auth
from tests.conftest import override_get_db
from app.database import get_db
from app.models.user import Role


def _model_payload(**overrides):
    base = {
        "name": "测试模型",
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "api_base": "http://localhost:9999",
        "api_key_env": "TEST_KEY",
        "max_tokens": 2048,
        "temperature": "0.5",
        "is_default": False,
    }
    base.update(overrides)
    return base


@pytest.fixture
def client():
    from app.routers import auth, admin

    test_app = FastAPI(title="Admin Test API")
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    test_app.include_router(auth.router)
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app, raise_server_exceptions=True) as c:
        yield c
    test_app.dependency_overrides.clear()


# ── Model Config — role enforcement ──────────────────────────────────────────

def test_employee_cannot_list_models(client, db):
    dept = _make_dept(db)
    _make_user(db, "aemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "aemp1")

    resp = client.get("/api/admin/models", headers=_auth(token))
    assert resp.status_code == 403


def test_dept_admin_cannot_list_models(client, db):
    dept = _make_dept(db)
    _make_user(db, "adept1", Role.DEPT_ADMIN, dept.id)
    db.commit()
    token = _login(client, "adept1")

    resp = client.get("/api/admin/models", headers=_auth(token))
    assert resp.status_code == 403


# ── Model Config CRUD ─────────────────────────────────────────────────────────

def test_super_admin_can_list_models(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin1")

    resp = client.get("/api/admin/models", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_create_model_config(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin2")

    resp = client.post("/api/admin/models", headers=_auth(token), json=_model_payload(name="新模型"))
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["name"] == "新模型"


def test_list_models_returns_created(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin3", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin3")

    client.post("/api/admin/models", headers=_auth(token), json=_model_payload(name="列表模型1"))
    client.post("/api/admin/models", headers=_auth(token), json=_model_payload(name="列表模型2"))

    resp = client.get("/api/admin/models", headers=_auth(token))
    names = [m["name"] for m in resp.json()]
    assert "列表模型1" in names
    assert "列表模型2" in names


def test_update_model_config(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin4", Role.SUPER_ADMIN, dept.id)
    mc = _make_model_config(db)
    db.commit()
    token = _login(client, "aadmin4")

    resp = client.put(f"/api/admin/models/{mc.id}", headers=_auth(token), json=_model_payload(
        name="更新后模型", provider="deepseek"
    ))
    assert resp.status_code == 200

    models = client.get("/api/admin/models", headers=_auth(token)).json()
    updated = next((m for m in models if m["id"] == mc.id), None)
    assert updated is not None
    assert updated["name"] == "更新后模型"
    assert updated["provider"] == "deepseek"


def test_update_model_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin5")

    resp = client.put("/api/admin/models/99999", headers=_auth(token), json=_model_payload())
    assert resp.status_code == 404


def test_delete_model_config(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin6", Role.SUPER_ADMIN, dept.id)
    mc = _make_model_config(db)
    db.commit()
    token = _login(client, "aadmin6")

    resp = client.delete(f"/api/admin/models/{mc.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    models = client.get("/api/admin/models", headers=_auth(token)).json()
    assert not any(m["id"] == mc.id for m in models)


def test_delete_model_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin7", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin7")

    resp = client.delete("/api/admin/models/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_set_default_model_clears_previous_default(client, db):
    dept = _make_dept(db)
    _make_user(db, "aadmin8", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin8")

    r1 = client.post("/api/admin/models", headers=_auth(token), json=_model_payload(
        name="默认模型1", is_default=True
    ))
    id1 = r1.json()["id"]

    # Create second model as default — should unset first
    client.post("/api/admin/models", headers=_auth(token), json=_model_payload(
        name="默认模型2", is_default=True
    ))

    models = client.get("/api/admin/models", headers=_auth(token)).json()
    m1 = next(m for m in models if m["id"] == id1)
    assert m1["is_default"] is False


# ── Departments ───────────────────────────────────────────────────────────────

def test_super_admin_can_list_departments(client, db):
    _make_dept(db, "部门X")
    _make_dept(db, "部门Y")
    dept = _make_dept(db, "管理部门")
    _make_user(db, "aadmin9", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "aadmin9")

    resp = client.get("/api/admin/departments", headers=_auth(token))
    assert resp.status_code == 200
    names = [d["name"] for d in resp.json()]
    assert "部门X" in names
    assert "部门Y" in names


def test_dept_admin_can_list_departments(client, db):
    dept = _make_dept(db)
    _make_user(db, "adept2", Role.DEPT_ADMIN, dept.id)
    db.commit()
    token = _login(client, "adept2")

    resp = client.get("/api/admin/departments", headers=_auth(token))
    assert resp.status_code == 200


def test_employee_cannot_list_departments(client, db):
    dept = _make_dept(db)
    _make_user(db, "aemp2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "aemp2")

    resp = client.get("/api/admin/departments", headers=_auth(token))
    assert resp.status_code == 403
