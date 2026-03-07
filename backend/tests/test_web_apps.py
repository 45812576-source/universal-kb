"""TC-WEBAPPS: Web app CRUD, ownership, preview, public share."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_web_app, _login, _auth
from app.models.user import Role


def test_list_web_apps_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser1")
    resp = client.get("/api/web-apps", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_web_app(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser2")
    resp = client.post("/api/web-apps", headers=_auth(token), json={
        "name": "我的应用",
        "description": "测试应用",
        "html_content": "<html><body>Hello</body></html>",
        "is_public": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "我的应用"
    assert "share_token" in data


def test_list_web_apps_only_own(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser3a", Role.EMPLOYEE, dept.id)
    u2 = _make_user(db, "wuser3b", Role.EMPLOYEE, dept.id)
    _make_web_app(db, u1.id, "用户1应用")
    _make_web_app(db, u2.id, "用户2应用")
    db.commit()
    t1 = _login(client, "wuser3a")
    resp = client.get("/api/web-apps", headers=_auth(t1))
    names = [a["name"] for a in resp.json()]
    assert "用户1应用" in names
    assert "用户2应用" not in names


def test_get_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser4", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "详情应用")
    db.commit()
    token = _login(client, "wuser4")
    resp = client.get(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["name"] == "详情应用"
    assert "html_content" in resp.json()


def test_get_web_app_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser5", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser5")
    resp = client.get("/api/web-apps/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_update_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser6", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "旧名应用")
    db.commit()
    token = _login(client, "wuser6")
    resp = client.put(f"/api/web-apps/{app.id}", headers=_auth(token), json={
        "name": "新名应用",
        "html_content": "<html><body>Updated</body></html>",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名应用"


def test_update_others_app_forbidden(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser7a", Role.EMPLOYEE, dept.id)
    _make_user(db, "wuser7b", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u1.id, "他人应用")
    db.commit()
    token = _login(client, "wuser7b")
    resp = client.put(f"/api/web-apps/{app.id}", headers=_auth(token), json={"name": "篡改"})
    assert resp.status_code == 403


def test_delete_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser8", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "待删应用")
    db.commit()
    token = _login(client, "wuser8")
    resp = client.delete(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_others_app_forbidden(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser9a", Role.EMPLOYEE, dept.id)
    _make_user(db, "wuser9b", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u1.id, "他人待删应用")
    db.commit()
    token = _login(client, "wuser9b")
    resp = client.delete(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 403


def test_preview_web_app_returns_html(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser10", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "预览应用")
    db.commit()
    token = _login(client, "wuser10")
    resp = client.get(f"/api/web-apps/{app.id}/preview", headers=_auth(token))
    assert resp.status_code == 200
    assert "Hello" in resp.text


def test_public_share_no_auth_required(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser11", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "公开应用", is_public=True)
    db.commit()
    # Access without auth
    resp = client.get(f"/share/{app.share_token}")
    assert resp.status_code == 200
    assert "Hello" in resp.text


def test_share_invalid_token_404(client, db):
    resp = client.get("/share/invalid-token-xyz")
    assert resp.status_code == 404
