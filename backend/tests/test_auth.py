"""TC-AUTH: Authentication & /me endpoint tests."""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role


def test_login_success(client, db):
    _make_dept(db)
    _make_user(db, "alice", Role.EMPLOYEE)
    db.commit()

    token = _login(client, "alice")
    assert token


def test_login_wrong_password(client, db):
    _make_dept(db)
    _make_user(db, "bob")
    db.commit()

    resp = client.post("/api/auth/login", json={"username": "bob", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401


def test_me_authenticated(client, db):
    _make_dept(db)
    _make_user(db, "carol", Role.SUPER_ADMIN)
    db.commit()

    token = _login(client, "carol")
    resp = client.get("/api/auth/me", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "carol"
    assert data["role"] == "super_admin"


def test_me_unauthenticated(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code in (401, 403)


def test_protected_route_bad_token(client):
    resp = client.get("/api/skills", headers=_auth("bad.token.here"))
    assert resp.status_code in (401, 403)
