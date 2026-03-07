from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.services.vector_service import index_knowledge, delete_knowledge_vectors


def approve_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str = ""
) -> KnowledgeEntry:
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")
    entry.status = KnowledgeStatus.APPROVED
    entry.reviewed_by = reviewer_id
    entry.review_note = note

    # Vectorize and index into Milvus
    try:
        milvus_ids = index_knowledge(entry.id, entry.content)
        entry.milvus_ids = milvus_ids
    except Exception as e:
        # Log but don't fail — knowledge is approved even if vectorization fails
        import logging
        logging.getLogger(__name__).warning(
            f"Vectorization failed for knowledge {knowledge_id}: {e}"
        )

    db.commit()
    return entry


def reject_knowledge(
    db: Session, knowledge_id: int, reviewer_id: int, note: str
) -> KnowledgeEntry:
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"Knowledge {knowledge_id} not found")
    entry.status = KnowledgeStatus.REJECTED
    entry.reviewed_by = reviewer_id
    entry.review_note = note
    db.commit()
    return entry
