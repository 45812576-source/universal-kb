from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.org_memory import OrgMemoryProposal, OrgMemorySnapshot, OrgMemorySource
from app.models.user import User
from app.services import org_memory_service as service


router = APIRouter(prefix="/api/org-memory", tags=["org-memory"])


class SourceIngestRequest(BaseModel):
    source_type: str = "markdown"
    source_uri: str
    title: str
    owner_name: str | None = None


@router.get("/sources")
def get_sources(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"items": service.list_sources(db)}


@router.get("/snapshots")
def get_snapshots(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"items": service.list_snapshots(db)}


@router.get("/proposals")
def get_proposals(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"items": service.list_proposals(db)}


@router.post("/sources/ingest")
def ingest_source(
    req: SourceIngestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = service.create_source(db, user, req.model_dump())
    return {"source_id": source.id, "status": source.ingest_status}


@router.post("/sources/{source_id}/snapshots")
def create_snapshot(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(OrgMemorySource, source_id)
    if not source:
        raise HTTPException(404, "组织 Memory 源文档不存在")
    snapshot = service.create_snapshot(db, source)
    return {"snapshot_id": snapshot.id, "status": snapshot.parse_status}


@router.post("/snapshots/{snapshot_id}/proposals")
def create_proposal(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    snapshot = db.get(OrgMemorySnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织 Memory 快照不存在")
    proposal = service.create_proposal(db, snapshot)
    return {"proposal_id": proposal.id, "status": proposal.proposal_status}


@router.get("/proposals/{proposal_id}")
def get_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = db.get(OrgMemoryProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "组织 Memory 草案不存在")
    return service.proposal_to_dto(proposal, db)


@router.get("/snapshots/{snapshot_id}/diff")
def get_snapshot_diff(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    snapshot = db.get(OrgMemorySnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404, "组织 Memory 快照不存在")
    return service.snapshot_diff(db, snapshot)


@router.get("/proposals/{proposal_id}/config-versions")
def get_config_versions(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = db.get(OrgMemoryProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "组织 Memory 草案不存在")
    versions = (
        db.query(service.OrgMemoryConfigVersion)
        .filter(service.OrgMemoryConfigVersion.proposal_id == proposal_id)
        .order_by(service.OrgMemoryConfigVersion.version.desc())
        .all()
    )
    return {"items": [service.config_version_to_dto(item) for item in versions]}


@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = db.get(OrgMemoryProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "组织 Memory 草案不存在")
    approval = service.submit_proposal(db, proposal, user)
    return {
        "proposal_id": proposal.id,
        "approval_request_id": approval.id,
        "status": "submitted",
        "message": "已提交审批",
    }


@router.post("/proposals/{proposal_id}/rollback")
def rollback_config(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = db.get(OrgMemoryProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "组织 Memory 草案不存在")
    return service.rollback_proposal_config(db, proposal, user)
