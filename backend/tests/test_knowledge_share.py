"""知识库分享链接自动化测试。"""
import pytest

from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.knowledge_share import KnowledgeShareLink
from app.models.user import Role
from tests.conftest import _auth, _login, _make_dept, _make_user


@pytest.fixture
def share_setup(db):
    dept = _make_dept(db, "分享测试部")
    owner = _make_user(db, "share_owner", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "share_other", Role.EMPLOYEE, dept.id)
    admin = _make_user(db, "share_admin", Role.SUPER_ADMIN, dept.id)
    entry = KnowledgeEntry(
        title="分享测试文档",
        content="正文内容",
        content_html="<p>正文内容</p>",
        category="experience",
        status=KnowledgeStatus.APPROVED,
        created_by=owner.id,
        department_id=dept.id,
        source_type="manual",
        doc_render_status="ready",
    )
    db.add(entry)
    db.commit()
    return {"dept": dept, "owner": owner, "other": other, "admin": admin, "entry": entry}


def test_create_share_link_returns_token(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]

    resp = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["share_token"]
    assert body["share_url"].endswith(body["share_token"])
    assert body["access_scope"] == "public_readonly"
    assert body["is_active"] is True


def test_create_share_link_reuses_active_link(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]

    first = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    second = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["share_token"] == second.json()["share_token"]


def test_other_employee_cannot_create_share(client, db, share_setup):
    token = _login(client, "share_other")
    entry = share_setup["entry"]

    resp = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    assert resp.status_code == 403


def test_admin_can_create_share(client, db, share_setup):
    token = _login(client, "share_admin")
    entry = share_setup["entry"]

    resp = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    assert resp.status_code == 200


def test_list_share_links_returns_current_active_link(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]
    client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))

    resp = client.get(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["is_active"] is True


def test_public_share_endpoint_is_anonymous_and_updates_access_stats(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]
    share_resp = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    share_token = share_resp.json()["share_token"]

    resp = client.get(f"/api/knowledge/public/share/{share_token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "分享测试文档"
    assert body["content_html"] == "<p>正文内容</p>"
    assert body["source_origin_label"] == "工作台"
    assert "visibility_scope" not in body
    assert "raw_title" not in body

    share = db.query(KnowledgeShareLink).filter(KnowledgeShareLink.share_token == share_token).first()
    assert share is not None
    assert share.access_count == 1
    assert share.last_accessed_at is not None


def test_disable_share_link_invalidates_public_access(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]
    share_resp = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token))
    share = share_resp.json()

    close_resp = client.delete(f"/api/knowledge/share-links/{share['id']}", headers=_auth(token))
    assert close_resp.status_code == 200

    public_resp = client.get(f"/api/knowledge/public/share/{share['share_token']}")
    assert public_resp.status_code == 404


def test_regenerate_invalidates_old_token_and_returns_new_one(client, db, share_setup):
    token = _login(client, "share_owner")
    entry = share_setup["entry"]
    first = client.post(f"/api/knowledge/{entry.id}/share-links", headers=_auth(token)).json()

    regen_resp = client.post(f"/api/knowledge/{entry.id}/share-links/regenerate", headers=_auth(token))
    assert regen_resp.status_code == 200
    second = regen_resp.json()
    assert second["share_token"] != first["share_token"]

    old_resp = client.get(f"/api/knowledge/public/share/{first['share_token']}")
    assert old_resp.status_code == 404

    new_resp = client.get(f"/api/knowledge/public/share/{second['share_token']}")
    assert new_resp.status_code == 200
