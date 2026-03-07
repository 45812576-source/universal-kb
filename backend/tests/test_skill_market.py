import pytest
from tests.conftest import _make_dept, _make_user, _login, _auth
from app.models.user import Role


def test_list_mcp_sources_empty(client, db):
    _make_dept(db)
    _make_user(db, "admin", Role.SUPER_ADMIN)
    db.commit()

    token = _login(client, "admin")
    resp = client.get("/api/skill-market/sources", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_market_search_requires_auth(client):
    resp = client.get("/api/skill-market/search?source_id=1&q=test")
    assert resp.status_code in (401, 403)
