import pytest
from fastapi import FastAPI, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.testclient import TestClient

from app.api_envelope import ApiEnvelopeException, api_envelope_exception_handler
from app.database import get_db
from tests.conftest import _auth, _login, _make_user, override_get_db


@pytest.fixture
def org_client():
    from app.routers import auth, org_memory

    test_app = FastAPI(title="Org Governance Snapshot Test API")
    test_app.add_exception_handler(ApiEnvelopeException, api_envelope_exception_handler)
    test_app.add_exception_handler(HTTPException, api_envelope_exception_handler)
    test_app.add_exception_handler(StarletteHTTPException, api_envelope_exception_handler)
    test_app.include_router(auth.router)
    test_app.include_router(org_memory.router)
    test_app.dependency_overrides[get_db] = override_get_db
    with TestClient(test_app, raise_server_exceptions=True) as client:
        yield client
    test_app.dependency_overrides.clear()


def _create_source(client, headers):
    resp = client.post(
        "/api/org-memory/sources/ingest",
        headers=headers,
        json={
            "source_type": "markdown",
            "source_uri": "manual://org-governance/sales",
            "title": "销售组织治理资料",
            "owner_name": "销售运营组",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["source_id"]


def test_workspace_snapshot_generate_and_detail(org_client, db):
    _make_user(db, username="governance_snapshot_author")
    db.commit()
    token = _login(org_client, username="governance_snapshot_author")
    headers = _auth(token)
    source_id = _create_source(org_client, headers)

    resp = org_client.post(
        "/api/org-memory/workspace-snapshot-events",
        headers=headers,
        json={
            "event_type": "snapshot.generate",
            "workspace": {
                "app": "universal-kb",
                "workspace_id": "ws-org-001",
                "workspace_type": "org_memory",
            },
            "snapshot": {"scope": "all", "title": "销售组织治理快照"},
            "sources": {"source_ids": [source_id]},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ready_for_review"
    assert data["run_id"]
    assert set(data["markdown_by_tab"]) == {"organization", "department", "role", "person", "okr", "process"}
    assert "authority_map" in data["governance_outputs"]
    assert "resource_access_matrix" in data["governance_outputs"]
    assert data["sync_status"]["structured_updated"] is True

    list_resp = org_client.get(
        "/api/org-memory/workspace-snapshots",
        headers=headers,
        params={"workspace_id": "ws-org-001", "app": "universal-kb"},
    )
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["items"][0]["id"] == data["id"]

    detail_resp = org_client.get(f"/api/org-memory/workspace-snapshots/{data['id']}", headers=headers)
    assert detail_resp.status_code == 200, detail_resp.text
    detail = detail_resp.json()
    assert detail["markdown_by_tab"]["organization"].startswith("---")
    assert detail["structured_by_tab"]["organization"]["snapshot_type"] == "organization"
    assert f"source:{source_id} 销售组织治理资料" in detail["structured_by_tab"]["organization"]["organization"]["evidence_text"]

    governance_resp = org_client.get(
        f"/api/org-memory/workspace-snapshots/{data['id']}/governance-version",
        headers=headers,
    )
    assert governance_resp.status_code == 200, governance_resp.text
    governance = governance_resp.json()
    assert governance["derived_from_snapshot_id"] == data["id"]
    assert governance["status"] == "draft"
    assert governance["skill_access_rules"]

    run_resp = org_client.get(f"/api/org-memory/workspace-snapshot-runs/{data['run_id']}", headers=headers)
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json()["status"] == "completed"


def test_workspace_snapshot_markdown_sync_partial_preserves_structured(org_client, db):
    _make_user(db, username="governance_snapshot_sync_author")
    db.commit()
    token = _login(org_client, username="governance_snapshot_sync_author")
    headers = _auth(token)

    create_resp = org_client.post(
        "/api/org-memory/workspace-snapshot-events",
        headers=headers,
        json={
            "event_type": "snapshot.generate",
            "workspace": {"workspace_id": "ws-org-002", "workspace_type": "org_memory"},
            "snapshot": {"scope": "organization"},
            "sources": {"pasted_materials": ["组织部门共享需要审批，涉及客户资料需要脱敏。"]},
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    snapshot_id = create_resp.json()["id"]
    previous_structured = create_resp.json()["structured_by_tab"]["organization"]

    broken_resp = org_client.put(
        f"/api/org-memory/workspace-snapshots/{snapshot_id}/tabs/organization/markdown",
        headers=headers,
        json={"markdown": "# 被编辑坏的标题\n\n没有固定二级标题"},
    )
    assert broken_resp.status_code == 200, broken_resp.text
    broken = broken_resp.json()
    assert broken["status"] == "partial_sync"
    assert broken["sync_status"]["markdown_saved"] is True
    assert broken["sync_status"]["structured_updated"] is False
    assert broken["structured_by_tab"]["organization"] == previous_structured

    detail_resp = org_client.get(f"/api/org-memory/workspace-snapshots/{snapshot_id}", headers=headers)
    assert detail_resp.status_code == 200, detail_resp.text
    assert detail_resp.json()["markdown_by_tab"]["organization"].startswith("# 被编辑坏的标题")


def test_workspace_snapshot_analyze_sources_needs_input(org_client, db):
    _make_user(db, username="governance_snapshot_analyze_author")
    db.commit()
    token = _login(org_client, username="governance_snapshot_analyze_author")
    headers = _auth(token)

    resp = org_client.post(
        "/api/org-memory/workspace-snapshot-events",
        headers=headers,
        json={
            "event_type": "snapshot.analyze_sources",
            "workspace": {"workspace_id": "ws-org-003", "workspace_type": "org_memory"},
            "snapshot": {"scope": "all"},
            "sources": {},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "needs_input"
    assert data["form_questions"]
