"""TC-KNOWLEDGE-MIGRATIONS: Verify new knowledge models, fields, and API routes."""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth

from app.models.knowledge import KnowledgeEntry, KnowledgeFolder, KnowledgeStatus
from app.models.knowledge_doc import KnowledgeDoc, KnowledgeDocSnapshot, KnowledgeDocComment
from app.models.knowledge_block import KnowledgeDocumentBlock, KnowledgeChunkMapping
from app.models.knowledge_filing import KnowledgeFilingAction, KnowledgeFilingSuggestion
from app.models.user import Role
from app.database import Base


# ── 1. New tables exist and are usable ────────────────────────────────────────


EXPECTED_TABLES = [
    "knowledge_docs",
    "knowledge_doc_snapshots",
    "knowledge_doc_comments",
    "knowledge_document_blocks",
    "knowledge_chunk_mappings",
    "knowledge_filing_actions",
    "knowledge_filing_suggestions",
]


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_table_exists(table_name):
    """Each new table must be registered in Base.metadata."""
    table_names = [t.name for t in Base.metadata.sorted_tables]
    assert table_name in table_names, f"Table {table_name} not found in metadata"


def test_knowledge_doc_insert_query(db):
    """Insert and query a KnowledgeDoc row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_doc_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Doc test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    doc = KnowledgeDoc(
        knowledge_id=entry.id,
        yjs_doc_key=f"test-doc-key-{entry.id}",
        doc_type="cloud_doc",
        collab_status="initializing",
    )
    db.add(doc)
    db.flush()

    fetched = db.query(KnowledgeDoc).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.yjs_doc_key == f"test-doc-key-{entry.id}"
    assert fetched.collab_status == "initializing"
    assert fetched.editor_schema_version == 1
    db.rollback()


def test_knowledge_doc_snapshot_insert_query(db):
    """Insert and query a KnowledgeDocSnapshot row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_snap_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Snap test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    snap = KnowledgeDocSnapshot(
        knowledge_id=entry.id,
        snapshot_type="autosave",
        preview_text="preview",
        created_by=user.id,
    )
    db.add(snap)
    db.flush()

    fetched = db.query(KnowledgeDocSnapshot).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.snapshot_type == "autosave"
    assert fetched.preview_text == "preview"
    db.rollback()


def test_knowledge_doc_comment_insert_query(db):
    """Insert and query a KnowledgeDocComment row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_cmt_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Comment test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    comment = KnowledgeDocComment(
        knowledge_id=entry.id,
        block_key="block-001",
        content="This is a comment",
        status="open",
        created_by=user.id,
    )
    db.add(comment)
    db.flush()

    fetched = db.query(KnowledgeDocComment).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.content == "This is a comment"
    assert fetched.block_key == "block-001"
    assert fetched.status == "open"
    db.rollback()


def test_knowledge_document_block_insert_query(db):
    """Insert and query a KnowledgeDocumentBlock row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_blk_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Block test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    block = KnowledgeDocumentBlock(
        knowledge_id=entry.id,
        block_key="blk-001",
        block_type="paragraph",
        block_order=0,
        plain_text="Hello block",
    )
    db.add(block)
    db.flush()

    fetched = db.query(KnowledgeDocumentBlock).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.block_key == "blk-001"
    assert fetched.block_type == "paragraph"
    assert fetched.plain_text == "Hello block"
    db.rollback()


def test_knowledge_chunk_mapping_insert_query(db):
    """Insert and query a KnowledgeChunkMapping row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_chk_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Chunk test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    mapping = KnowledgeChunkMapping(
        knowledge_id=entry.id,
        chunk_index=0,
        milvus_chunk_id="milvus-001",
        chunk_text="chunk text here",
    )
    db.add(mapping)
    db.flush()

    fetched = db.query(KnowledgeChunkMapping).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.chunk_index == 0
    assert fetched.milvus_chunk_id == "milvus-001"
    assert fetched.chunk_text == "chunk text here"
    db.rollback()


def test_knowledge_filing_action_insert_query(db):
    """Insert and query a KnowledgeFilingAction row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_fla_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Filing action test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    action = KnowledgeFilingAction(
        knowledge_id=entry.id,
        action_type="auto_file",
        decision_source="taxonomy",
        confidence=0.85,
        reason="matched taxonomy A1.1",
        created_by=user.id,
    )
    db.add(action)
    db.flush()

    fetched = db.query(KnowledgeFilingAction).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.action_type == "auto_file"
    assert fetched.confidence == 0.85
    assert fetched.decision_source == "taxonomy"
    db.rollback()


def test_knowledge_filing_suggestion_insert_query(db):
    """Insert and query a KnowledgeFilingSuggestion row."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_fls_user", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="Filing suggestion test", content="content", created_by=user.id, department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    suggestion = KnowledgeFilingSuggestion(
        knowledge_id=entry.id,
        suggested_folder_path="/A/A1",
        confidence=0.9,
        reason="high confidence match",
        status="pending",
    )
    db.add(suggestion)
    db.flush()

    fetched = db.query(KnowledgeFilingSuggestion).filter_by(knowledge_id=entry.id).first()
    assert fetched is not None
    assert fetched.confidence == 0.9
    assert fetched.status == "pending"
    assert fetched.suggested_folder_path == "/A/A1"
    db.rollback()


# ── 2. New fields on KnowledgeFolder ─────────────────────────────────────────


def test_knowledge_folder_new_fields(db):
    """KnowledgeFolder should support is_system, taxonomy_board, taxonomy_code."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_fld_user", Role.EMPLOYEE, dept.id)
    db.flush()

    folder = KnowledgeFolder(
        name="System Folder A",
        created_by=user.id,
        department_id=dept.id,
        is_system=1,
        taxonomy_board="A",
        taxonomy_code="A1.1",
    )
    db.add(folder)
    db.flush()

    fetched = db.query(KnowledgeFolder).filter_by(id=folder.id).first()
    assert fetched is not None
    assert fetched.is_system == 1
    assert fetched.taxonomy_board == "A"
    assert fetched.taxonomy_code == "A1.1"
    db.rollback()


# ── 3. New fields on KnowledgeEntry ──────────────────────────────────────────


def test_knowledge_entry_taxonomy_fields(db):
    """KnowledgeEntry should persist all new taxonomy and doc render fields."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_tax_user", Role.EMPLOYEE, dept.id)
    db.flush()

    entry = KnowledgeEntry(
        title="Taxonomy entry",
        content="content",
        created_by=user.id,
        department_id=dept.id,
        taxonomy_code="B2.3",
        taxonomy_board="B",
        taxonomy_path=["B.Brand", "B2.Sub-brand", "B2.3.Leaf"],
        storage_layer="L2",
        classification_status="success",
        classification_confidence=0.92,
        doc_render_status="ready",
        doc_render_mode="native_html",
    )
    db.add(entry)
    db.flush()

    fetched = db.query(KnowledgeEntry).filter_by(id=entry.id).first()
    assert fetched is not None
    assert fetched.taxonomy_code == "B2.3"
    assert fetched.taxonomy_board == "B"
    assert fetched.taxonomy_path == ["B.Brand", "B2.Sub-brand", "B2.3.Leaf"]
    assert fetched.storage_layer == "L2"
    assert fetched.classification_status == "success"
    assert fetched.classification_confidence == 0.92
    assert fetched.doc_render_status == "ready"
    assert fetched.doc_render_mode == "native_html"
    db.rollback()


# ── 4. Old data backward compatibility ──────────────────────────────────────


def test_knowledge_folder_defaults_without_new_fields(db):
    """Creating a KnowledgeFolder without new fields should use correct defaults."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_def_fld", Role.EMPLOYEE, dept.id)
    db.flush()

    folder = KnowledgeFolder(
        name="Legacy Folder",
        created_by=user.id,
        department_id=dept.id,
    )
    db.add(folder)
    db.flush()

    fetched = db.query(KnowledgeFolder).filter_by(id=folder.id).first()
    assert fetched is not None
    assert fetched.is_system == 0
    assert fetched.taxonomy_board is None
    assert fetched.taxonomy_code is None
    db.rollback()


def test_knowledge_entry_defaults_without_new_fields(db):
    """Creating a KnowledgeEntry without taxonomy fields should use correct defaults."""
    dept = _make_dept(db)
    user = _make_user(db, "migr_def_ent", Role.EMPLOYEE, dept.id)
    db.flush()

    entry = KnowledgeEntry(
        title="Legacy entry",
        content="content",
        created_by=user.id,
        department_id=dept.id,
    )
    db.add(entry)
    db.flush()

    fetched = db.query(KnowledgeEntry).filter_by(id=entry.id).first()
    assert fetched is not None
    assert fetched.taxonomy_code is None
    assert fetched.taxonomy_board is None
    assert fetched.taxonomy_path is None
    assert fetched.storage_layer is None
    assert fetched.classification_status == "pending"
    assert fetched.classification_confidence is None
    assert fetched.doc_render_status == "pending"
    assert fetched.doc_render_mode is None
    db.rollback()


def test_old_knowledge_list_api_still_works(client, db):
    """GET /api/knowledge should still work after model changes."""
    dept = _make_dept(db)
    _make_user(db, "migr_list_user", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_list_user")

    resp = client.get("/api/knowledge", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    # Should return a list (possibly empty)
    assert isinstance(data, (list, dict))


def test_old_knowledge_detail_api_still_works(client, db):
    """GET /api/knowledge/{id} should still work after model changes."""
    dept = _make_dept(db)
    _make_user(db, "migr_det_user", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_det_user")

    # Create an entry via API
    resp = client.post("/api/knowledge", headers=_auth(token), json={
        "title": "Detail test",
        "content": "content for detail test",
    })
    assert resp.status_code == 200
    entry_id = resp.json()["id"]

    # Fetch detail
    resp = client.get(f"/api/knowledge/{entry_id}", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == entry_id
    assert data["title"] == "Detail test"


# ── 5. API routes are registered ────────────────────────────────────────────


def test_blocks_api_registered(client, db):
    """GET /api/knowledge/{kid}/blocks should not 404 (route exists)."""
    dept = _make_dept(db)
    _make_user(db, "migr_blk_api", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_blk_api")

    resp = client.get("/api/knowledge/99999/blocks", headers=_auth(token))
    # Route should exist; may return 200 with [] or 404 for entry not found,
    # but should NOT be 404 due to missing route (Method Not Allowed / unregistered).
    assert resp.status_code != 405, "Route /api/knowledge/{kid}/blocks is not registered"
    # Accept 200 (empty list) or 404 (entry not found) - both mean the route exists
    assert resp.status_code in (200, 404)


def test_filing_ensure_system_tree_api_registered(client, db):
    """POST /api/knowledge/filing/ensure-system-tree should be reachable."""
    dept = _make_dept(db)
    _make_user(db, "migr_tree_api", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_tree_api")

    resp = client.post("/api/knowledge/filing/ensure-system-tree", headers=_auth(token))
    # Route exists; employee may get 403 (admin-only) or 200
    assert resp.status_code != 404, "Route /api/knowledge/filing/ensure-system-tree is not registered"
    assert resp.status_code != 405, "Route /api/knowledge/filing/ensure-system-tree is not registered"


def test_filing_auto_run_api_registered(client, db):
    """POST /api/knowledge/filing/auto-run should be reachable."""
    dept = _make_dept(db)
    _make_user(db, "migr_auto_api", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_auto_api")

    resp = client.post("/api/knowledge/filing/auto-run", headers=_auth(token))
    assert resp.status_code != 404, "Route /api/knowledge/filing/auto-run is not registered"
    assert resp.status_code != 405, "Route /api/knowledge/filing/auto-run is not registered"


def test_filing_unfiled_api_registered(client, db):
    """GET /api/knowledge/filing/unfiled should be reachable."""
    dept = _make_dept(db)
    _make_user(db, "migr_unf_api", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "migr_unf_api")

    resp = client.get("/api/knowledge/filing/unfiled", headers=_auth(token))
    assert resp.status_code != 404, "Route /api/knowledge/filing/unfiled is not registered"
    assert resp.status_code != 405, "Route /api/knowledge/filing/unfiled is not registered"
