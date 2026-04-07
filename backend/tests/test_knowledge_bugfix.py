"""测试三个知识库 Bug 修复：ZIP 目录结构、标题显示、删除生效。"""
import io
import zipfile
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role


# ── Bug 2: _display_title 应优先使用文件名标题 ─────────────────────────────────

def test_display_title_prefers_filename_over_ai(client, db):
    """上传 md 文件后，标题应该是文件名而非 AI 总结标题。"""
    dept = _make_dept(db)
    _make_user(db, "title_user", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "title_user")

    # 上传一个 md 文件，文件名为 "我的学习笔记.md"
    content = b"# Some H1 Title\n\nThis is content about marketing strategies."
    resp = client.post(
        "/api/knowledge/upload",
        headers=_auth(token),
        files={"file": ("我的学习笔记.md", io.BytesIO(content), "text/markdown")},
        data={
            "title": "我的学习笔记",  # 前端发送的不含扩展名的文件名
            "category": "experience",
            "industry_tags": "[]",
            "platform_tags": "[]",
            "topic_tags": "[]",
        },
    )
    assert resp.status_code == 200
    entry_id = resp.json()["id"]

    # 获取详情，标题应该是"我的学习笔记"而非 AI 生成的
    detail = client.get(f"/api/knowledge/{entry_id}", headers=_auth(token))
    assert detail.status_code == 200
    assert detail.json()["title"] == "我的学习笔记"


# ── Bug 3: 删除知识条目应成功 ──────────────────────────────────────────────────

def test_delete_knowledge_entry(client, db):
    """删除知识条目应清理关联记录并成功返回。"""
    dept = _make_dept(db)
    _make_user(db, "del_user", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "del_user")

    # 创建一条知识
    resp = client.post("/api/knowledge", headers=_auth(token), json={
        "title": "待删除文档",
        "content": "这是一段要被删除的内容",
        "category": "experience",
    })
    assert resp.status_code == 200
    entry_id = resp.json()["id"]

    # 删除
    del_resp = client.delete(f"/api/knowledge/{entry_id}", headers=_auth(token))
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    # 确认已不存在
    get_resp = client.get(f"/api/knowledge/{entry_id}", headers=_auth(token))
    assert get_resp.status_code == 404


def test_delete_knowledge_with_related_records(client, db):
    """带有关联记录（如 knowledge_jobs）的条目也应能删除成功。"""
    from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
    dept = _make_dept(db)
    user = _make_user(db, "del_user2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "del_user2")

    # 上传文件（会自动创建 knowledge_jobs 等关联记录）
    content = b"Test content for deletion"
    resp = client.post(
        "/api/knowledge/upload",
        headers=_auth(token),
        files={"file": ("test_delete.txt", io.BytesIO(content), "text/plain")},
        data={
            "title": "test_delete",
            "category": "experience",
            "industry_tags": "[]",
            "platform_tags": "[]",
            "topic_tags": "[]",
        },
    )
    assert resp.status_code == 200
    entry_id = resp.json()["id"]

    # 删除应成功
    del_resp = client.delete(f"/api/knowledge/{entry_id}", headers=_auth(token))
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True


# ── Bug 1: ZIP 上传应保留目录结构 ──────────────────────────────────────────────

def _make_zip(file_tree: dict[str, bytes]) -> bytes:
    """构建内存 zip，file_tree: {"dir/sub/file.md": b"content", ...}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in file_tree.items():
            zf.writestr(path, data)
    return buf.getvalue()


def test_zip_upload_preserves_directory_structure(client, db):
    """ZIP 内的目录结构应映射为 KnowledgeFolder 层级。"""
    dept = _make_dept(db)
    _make_user(db, "zip_user", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "zip_user")

    zip_data = _make_zip({
        "策略文档/投放/抖音投放.md": b"# Douyin\ncontent",
        "策略文档/投放/快手投放.md": b"# Kuaishou\ncontent",
        "策略文档/报告.pdf": b"%PDF-fake",
        "根文件.txt": b"root content",
    })

    resp = client.post(
        "/api/knowledge/upload",
        headers=_auth(token),
        files={"file": ("docs.zip", io.BytesIO(zip_data), "application/zip")},
        data={
            "title": "docs",
            "category": "experience",
            "industry_tags": "[]",
            "platform_tags": "[]",
            "topic_tags": "[]",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["zip"] is True

    successful = [r for r in data["results"] if r.get("id")]
    assert len(successful) == 4

    # 验证文件夹结构已创建
    folders_resp = client.get("/api/knowledge/folders?owner_only=true", headers=_auth(token))
    assert folders_resp.status_code == 200
    folders = folders_resp.json()
    folder_names = {f["name"] for f in folders}
    assert "策略文档" in folder_names
    assert "投放" in folder_names

    # 验证"投放"文件夹的 parent 是"策略文档"
    strategy_folder = next(f for f in folders if f["name"] == "策略文档")
    delivery_folder = next(f for f in folders if f["name"] == "投放")
    assert delivery_folder["parent_id"] == strategy_folder["id"]


def test_zip_upload_strips_single_root_wrapper(client, db):
    """ZIP 中只有一个根目录包装时应自动剥离。"""
    dept = _make_dept(db)
    _make_user(db, "zip_user2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "zip_user2")

    # 常见场景：zip 内所有文件都在 "project/" 目录下
    zip_data = _make_zip({
        "project/readme.txt": b"readme",
        "project/docs/guide.md": b"guide content",
    })

    resp = client.post(
        "/api/knowledge/upload",
        headers=_auth(token),
        files={"file": ("wrapped.zip", io.BytesIO(zip_data), "application/zip")},
        data={
            "title": "wrapped",
            "category": "experience",
            "industry_tags": "[]",
            "platform_tags": "[]",
            "topic_tags": "[]",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["zip"] is True

    # 不应创建 "project" 文件夹，但应创建 "docs" 文件夹
    folders_resp = client.get("/api/knowledge/folders?owner_only=true", headers=_auth(token))
    folders = folders_resp.json()
    folder_names = {f["name"] for f in folders}
    assert "project" not in folder_names
    assert "docs" in folder_names
