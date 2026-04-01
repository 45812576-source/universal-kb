"""知识库收口验收自动化测试 — 5 条后端必过项。

覆盖整改单要求：
1. 上传中文标题优先级测试
2. 创建空白文档默认 personal folder 测试
3. 上传默认 personal folder 测试
4. pending 文档本人可见测试
5. render failed 状态不影响 entry 可见测试
"""
import io
import pytest
from tests.conftest import (
    _make_dept, _make_user, _login, _auth,
    TestingSessionLocal,
)
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder, KnowledgeStatus


# ── helpers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def employee_setup(db):
    """创建部门 + 员工，返回 (user, dept)。"""
    dept = _make_dept(db, "知识测试部")
    user = _make_user(db, "kb_tester", dept_id=dept.id)
    db.commit()
    return user, dept


@pytest.fixture
def token(client, employee_setup):
    return _login(client, "kb_tester")


# ─── Test 1: 上传中文标题优先级 ────────────────────────────────────────────

def test_upload_chinese_title_no_garble(client, token, db):
    """上传中文文件名 md，title 不乱码、不含扩展名、与 source_file 分离。"""
    content = b"# Hello\ntest content"
    files = {"file": ("人事文件生.md", io.BytesIO(content), "text/markdown")}
    # 前端发的 title 是去扩展名后的文件名
    data = {
        "title": "人事文件生",
        "category": "experience",
        "industry_tags": "[]",
        "platform_tags": "[]",
        "topic_tags": "[]",
    }
    resp = client.post("/api/knowledge/upload", files=files, data=data, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # title 不乱码、不含扩展名
    assert body["title"] == "人事文件生", f"title 异常: {body['title']}"
    # source_file 保留原始文件名
    assert body["source_file"] == "人事文件生.md"
    # title 和 source_file 分离
    assert body["title"] != body["source_file"]


# ─── Test 2: 创建空白文档默认进 personal folder ────────────────────────────

def test_create_blank_doc_enters_my_knowledge(client, token, db):
    """POST /knowledge 不传 folder_id → 自动进入'我的知识'。"""
    resp = client.post("/api/knowledge", json={
        "title": "未命名文档",
        "content": "",
        "category": "experience",
    }, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] > 0
    assert body["title"] == "未命名文档"
    assert body["folder_id"] is not None, "folder_id 不应为 null"
    assert body["folder_name"] == "我的知识", f"folder_name 异常: {body.get('folder_name')}"

    # 刷新后仍在 — 验证 DB 持久化
    entry = db.get(KnowledgeEntry, body["id"])
    assert entry is not None
    assert entry.folder_id == body["folder_id"]

    folder = db.get(KnowledgeFolder, entry.folder_id)
    assert folder is not None
    assert folder.name == "我的知识"


# ─── Test 3: 上传默认进 personal folder ────────────────────────────────────

def test_upload_enters_my_knowledge(client, token, db):
    """上传不传 folder_id → 自动进入'我的知识'。"""
    files = {"file": ("test.md", io.BytesIO(b"# test"), "text/markdown")}
    data = {
        "title": "test",
        "category": "experience",
        "industry_tags": "[]",
        "platform_tags": "[]",
        "topic_tags": "[]",
    }
    resp = client.post("/api/knowledge/upload", files=files, data=data, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["folder_id"] is not None, "folder_id 不应为 null"
    assert body["folder_name"] == "我的知识"


# ─── Test 4: pending 文档本人可见 ──────────────────────────────────────────

def test_pending_doc_visible_to_owner(client, token, db, employee_setup):
    """pending 状态文档在本人列表中仍然可见。"""
    user, dept = employee_setup

    # 直接在 DB 创建一个 pending 文档
    folder = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.created_by == user.id,
        KnowledgeFolder.name == "我的知识",
    ).first()
    # 如果 folder 不存在，先确保
    if not folder:
        resp = client.post("/api/knowledge/ensure-my-folder", headers=_auth(token))
        assert resp.status_code == 200
        folder = db.query(KnowledgeFolder).filter(
            KnowledgeFolder.created_by == user.id,
            KnowledgeFolder.name == "我的知识",
        ).first()

    entry = KnowledgeEntry(
        title="待审核测试文档",
        content="这是一个待审核文档",
        category="experience",
        status=KnowledgeStatus.PENDING,
        created_by=user.id,
        department_id=dept.id,
        source_type="manual",
        folder_id=folder.id if folder else None,
    )
    db.add(entry)
    db.commit()

    # 列表中能看到
    resp = client.get("/api/knowledge", headers=_auth(token))
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()]
    assert entry.id in ids, f"pending 文档 id={entry.id} 在列表中不可见"

    # 详情能打开
    resp = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["title"] == "待审核测试文档"


# ─── Test 5: render failed 不影响可见性 ────────────────────────────────────

def test_render_failed_entry_still_visible(client, token, db, employee_setup):
    """doc_render_status=failed 的文档仍在列表、仍可打开。"""
    user, dept = employee_setup

    entry = KnowledgeEntry(
        title="转换失败测试",
        content="正文内容仍在",
        category="experience",
        status=KnowledgeStatus.APPROVED,
        created_by=user.id,
        department_id=dept.id,
        source_type="upload",
        doc_render_status="failed",
        doc_render_error="模拟转换失败",
    )
    db.add(entry)
    db.commit()

    # 列表可见
    resp = client.get("/api/knowledge", headers=_auth(token))
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()]
    assert entry.id in ids, "render failed 文档在列表中不可见"

    # 详情可见 + 正文完整
    resp = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_render_status"] == "failed"
    assert body["doc_render_error"] == "模拟转换失败"
    assert body["content"] == "正文内容仍在"
