import os
import tempfile
from typing import Any, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.org_memory import OrgMemoryProposal, OrgMemorySnapshot, OrgMemorySource
from app.models.user import User
from app.services import org_memory_service as service
from app.utils.file_parser import extract_text


router = APIRouter(prefix="/api/org-memory", tags=["org-memory"])


class SourceIngestRequest(BaseModel):
    source_type: str = "markdown"
    source_uri: str
    title: str
    owner_name: str | None = None
    bitable_app_token: str | None = None
    bitable_table_id: str | None = None
    raw_fields: list[dict[str, Any]] | None = None
    raw_records: list[dict[str, Any]] | None = None


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


@router.post("/sources/upload")
async def upload_source(
    file: UploadFile = File(...),
    title: str = Form(None),
    owner_name: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if not suffix:
        raise HTTPException(400, "无法识别文件类型，请上传带扩展名的文件")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        content = await file.read()
        os.write(tmp_fd, content)
        os.close(tmp_fd)

        text = extract_text(tmp_path)
        if not text or not text.strip():
            raise HTTPException(422, "文件内容为空或无法解析")

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        raw_records = [{"content": p} for p in paragraphs]
        raw_fields = [{"name": "content", "type": "text"}]

        payload = {
            "title": title or file.filename or "上传文件",
            "source_type": "upload",
            "source_uri": f"upload://{file.filename or 'unknown'}",
            "owner_name": owner_name,
            "raw_fields": raw_fields,
            "raw_records": raw_records,
        }
        source = service.create_source(db, user, payload)
        return {"source_id": source.id, "status": source.ingest_status}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(OrgMemorySource, source_id)
    if not source:
        raise HTTPException(404, "组织 Memory 源文档不存在")
    service.delete_source(db, source)
    return {"ok": True}


class BatchSnapshotRequest(BaseModel):
    source_ids: List[int]


@router.post("/sources/batch-snapshot")
async def batch_snapshot(
    req: BatchSnapshotRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sources = []
    for sid in req.source_ids:
        source = db.get(OrgMemorySource, sid)
        if not source:
            raise HTTPException(404, f"源文档 #{sid} 不存在")
        sources.append(source)
    snapshots = await service.batch_create_snapshots(db, sources)
    return {
        "snapshots": [{"snapshot_id": s.id, "source_id": s.source_id, "status": s.parse_status} for s in snapshots],
    }


@router.post("/sources/{source_id}/snapshots")
async def create_snapshot(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(OrgMemorySource, source_id)
    if not source:
        raise HTTPException(404, "组织 Memory 源文档不存在")
    snapshot = await service.create_snapshot(db, source)
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
