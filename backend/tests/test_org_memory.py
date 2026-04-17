from app.models.org_memory import OrgMemoryApprovalLink, OrgMemoryProposal
from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
from tests.conftest import _auth, _login, _make_user


def test_org_memory_end_to_end_flow(client, db):
    user = _make_user(db, username="org_memory_author")
    db.commit()
    token = _login(client, username="org_memory_author")
    headers = _auth(token)

    sources_resp = client.get("/api/org-memory/sources", headers=headers)
    assert sources_resp.status_code == 200, sources_resp.text
    assert sources_resp.json()["items"] == []

    ingest_resp = client.post(
        "/api/org-memory/sources/ingest",
        headers=headers,
        json={
            "source_type": "feishu_doc",
            "source_uri": "https://example.feishu.cn/docx/org-memory-q2",
            "title": "销售组织治理文档（2026Q2）",
            "owner_name": "销售运营组",
        },
    )
    assert ingest_resp.status_code == 200, ingest_resp.text
    ingest_data = ingest_resp.json()
    source_id = ingest_data["source_id"]
    assert ingest_data["status"] == "processing"

    sources_after = client.get("/api/org-memory/sources", headers=headers)
    assert sources_after.status_code == 200, sources_after.text
    source_item = sources_after.json()["items"][0]
    assert source_item["id"] == source_id
    assert source_item["title"] == "销售组织治理文档（2026Q2）"
    assert source_item["source_type"] == "feishu_doc"
    assert source_item["ingest_status"] == "processing"

    snapshot_resp = client.post(
        f"/api/org-memory/sources/{source_id}/snapshots",
        headers=headers,
    )
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    snapshot_data = snapshot_resp.json()
    snapshot_id = snapshot_data["snapshot_id"]
    assert snapshot_data["status"] == "ready"

    snapshots_resp = client.get("/api/org-memory/snapshots", headers=headers)
    assert snapshots_resp.status_code == 200, snapshots_resp.text
    snapshot_item = snapshots_resp.json()["items"][0]
    assert snapshot_item["id"] == snapshot_id
    assert snapshot_item["source_id"] == source_id
    assert isinstance(snapshot_item["units"], list)
    assert isinstance(snapshot_item["roles"], list)
    assert isinstance(snapshot_item["people"], list)
    assert isinstance(snapshot_item["okrs"], list)
    assert isinstance(snapshot_item["processes"], list)
    assert isinstance(snapshot_item["low_confidence_items"], list)

    proposal_resp = client.post(
        f"/api/org-memory/snapshots/{snapshot_id}/proposals",
        headers=headers,
    )
    assert proposal_resp.status_code == 200, proposal_resp.text
    proposal_data = proposal_resp.json()
    proposal_id = proposal_data["proposal_id"]
    assert proposal_data["status"] == "draft"

    proposals_resp = client.get("/api/org-memory/proposals", headers=headers)
    assert proposals_resp.status_code == 200, proposals_resp.text
    proposal_item = proposals_resp.json()["items"][0]
    assert proposal_item["id"] == proposal_id
    assert proposal_item["snapshot_id"] == snapshot_id
    assert proposal_item["proposal_status"] == "draft"
    assert isinstance(proposal_item["structure_changes"], list)
    assert isinstance(proposal_item["classification_rules"], list)
    assert isinstance(proposal_item["skill_mounts"], list)
    assert isinstance(proposal_item["approval_impacts"], list)
    assert isinstance(proposal_item["evidence_refs"], list)

    submit_resp = client.post(
        f"/api/org-memory/proposals/{proposal_id}/submit",
        headers=headers,
    )
    assert submit_resp.status_code == 200, submit_resp.text
    submit_data = submit_resp.json()
    approval_request_id = submit_data["approval_request_id"]
    assert submit_data["proposal_id"] == proposal_id
    assert submit_data["status"] == "submitted"
    assert approval_request_id is not None

    db_proposal = db.get(OrgMemoryProposal, proposal_id)
    assert db_proposal is not None
    assert db_proposal.proposal_status == "pending_approval"
    assert db_proposal.submitted_at is not None

    approval = db.get(ApprovalRequest, approval_request_id)
    assert approval is not None
    assert approval.request_type == ApprovalRequestType.ORG_MEMORY_PROPOSAL
    assert approval.status == ApprovalStatus.PENDING
    assert approval.target_id == proposal_id
    assert approval.target_type == "org_memory_proposal"
    assert approval.requester_id == user.id

    link = (
        db.query(OrgMemoryApprovalLink)
        .filter(OrgMemoryApprovalLink.proposal_id == proposal_id)
        .order_by(OrgMemoryApprovalLink.id.desc())
        .first()
    )
    assert link is not None
    assert link.approval_request_id == approval_request_id


def test_org_memory_snapshot_diff_returns_previous_context(client, db):
    _make_user(db, username="org_memory_diff_author")
    db.commit()
    token = _login(client, username="org_memory_diff_author")
    headers = _auth(token)

    ingest_resp = client.post(
        "/api/org-memory/sources/ingest",
        headers=headers,
        json={
            "source_type": "markdown",
            "source_uri": "manual://org-memory/source-diff",
            "title": "客户成功组织文档",
        },
    )
    source_id = ingest_resp.json()["source_id"]

    first_snapshot = client.post(
        f"/api/org-memory/sources/{source_id}/snapshots",
        headers=headers,
    ).json()["snapshot_id"]
    second_snapshot = client.post(
        f"/api/org-memory/sources/{source_id}/snapshots",
        headers=headers,
    ).json()["snapshot_id"]

    diff_resp = client.get(
        f"/api/org-memory/snapshots/{second_snapshot}/diff",
        headers=headers,
    )
    assert diff_resp.status_code == 200, diff_resp.text
    diff_data = diff_resp.json()
    assert diff_data["snapshot_id"] == second_snapshot
    assert diff_data["previous_snapshot_id"] == first_snapshot
    assert "units" in diff_data
    assert "roles" in diff_data
