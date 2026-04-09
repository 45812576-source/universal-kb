"""知识库云文档专项回归测试。

覆盖：
1. doc_renderer — 状态语义、ready-empty 禁令、error 保留
2. enforce_no_ready_empty — 系统性 ready-empty 禁止
3. 上传链路 — 文件上传 → 转换 → 状态正确
4. 详情接口 — ready/failed 条目返回正确字段
"""
import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock

from tests.conftest import (
    TestingSessionLocal, _make_dept, _make_user, _make_model_config, _login, _auth,
)
from app.models.user import Role
from app.models.knowledge import KnowledgeEntry


# ── Helper ──

def _make_entry(db, user_id, **overrides):
    defaults = dict(
        title="测试文档",
        content="",
        category="experience",
        source_type="upload",
        status="approved",
        review_stage="auto_approved",
        review_level=0,
        capture_mode="manual",
        created_by=user_id,
        industry_tags=[],
        platform_tags=[],
        topic_tags=[],
        sensitivity_flags=[],
    )
    defaults.update(overrides)
    entry = KnowledgeEntry(**defaults)
    db.add(entry)
    db.flush()
    return entry


# ══════════════════════════════════════════════════════════════════════════════
# 1. enforce_no_ready_empty 单元测试
# ══════════════════════════════════════════════════════════════════════════════

class TestEnforceNoReadyEmpty:
    """系统禁令：ready 状态必须有可展示内容。"""

    def test_ready_with_content_html_stays_ready(self, db):
        """有 content_html 的 ready 条目不被降级。"""
        dept = _make_dept(db)
        user = _make_user(db, "user1", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            content_html="<p>Hello</p>")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "ready"
        assert entry.doc_render_error is None

    def test_ready_with_content_stays_ready(self, db):
        """有纯文本 content 的 ready 条目不被降级。"""
        dept = _make_dept(db)
        user = _make_user(db, "user2", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            content="纯文本内容")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "ready"

    def test_ready_with_onlyoffice_stays_ready(self, db):
        """有 oss_key + OnlyOffice 扩展名的 ready 条目不被降级。"""
        dept = _make_dept(db)
        user = _make_user(db, "user3", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            oss_key="knowledge/test.docx",
                            file_ext=".docx")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "ready"

    def test_ready_empty_downgraded_to_failed(self, db):
        """无任何内容的 ready 条目必须降级为 failed。"""
        dept = _make_dept(db)
        user = _make_user(db, "user4", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            content_html="",
                            content="")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "failed"
        assert entry.doc_render_error is not None
        assert "ready" in entry.doc_render_error

    def test_ready_with_only_whitespace_downgraded(self, db):
        """仅有空白字符的 ready 条目必须降级。"""
        dept = _make_dept(db)
        user = _make_user(db, "user5", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            content_html="   ",
                            content="\n\t  ")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "failed"

    def test_failed_status_not_affected(self, db):
        """非 ready 状态不受影响。"""
        dept = _make_dept(db)
        user = _make_user(db, "user6", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="failed",
                            doc_render_error="原始错误",
                            content_html="",
                            content="")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "failed"
        assert entry.doc_render_error == "原始错误"

    def test_ready_with_pdf_oss_stays_ready(self, db):
        """PDF 文件有 oss_key 时可走 iframe 预览，不降级。"""
        dept = _make_dept(db)
        user = _make_user(db, "user7", Role.EMPLOYEE, dept.id)
        db.commit()
        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            oss_key="knowledge/test.pdf",
                            file_ext=".pdf")
        db.commit()

        from app.services.doc_renderer import enforce_no_ready_empty
        enforce_no_ready_empty(entry)
        assert entry.doc_render_status == "ready"


# ══════════════════════════════════════════════════════════════════════════════
# 2. render_from_content 单元测试
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderFromContent:
    """render_from_content 纯文本 → HTML 转换。"""

    def test_markdown_rendered(self):
        from app.services.doc_renderer import render_from_content
        html = render_from_content("# 标题\n\n正文", ".md")
        assert html is not None
        assert "<h1>" in html or "标题" in html

    def test_txt_wrapped_in_p(self):
        from app.services.doc_renderer import render_from_content
        html = render_from_content("第一行\n第二行", ".txt")
        assert html is not None
        assert "<p>" in html

    def test_empty_returns_none(self):
        from app.services.doc_renderer import render_from_content
        assert render_from_content("", ".md") is None
        assert render_from_content("", ".txt") is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. doc_render_error 保留测试
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderErrorPreservation:
    """render 失败时 doc_render_error 不被覆盖。"""

    def test_render_entry_preserves_error_on_failure(self, db):
        """render_entry 失败路径必须保留 error 信息。"""
        dept = _make_dept(db)
        user = _make_user(db, "user_err1", Role.EMPLOYEE, dept.id)
        db.commit()

        entry = _make_entry(db, user.id,
                            doc_render_status="pending",
                            content="",
                            content_html="",
                            oss_key=None)
        db.commit()

        from app.services.doc_renderer import render_entry
        result = render_entry(db, entry.id)

        db.refresh(entry)
        assert entry.doc_render_status == "failed"
        assert entry.doc_render_error is not None
        assert len(entry.doc_render_error) > 0

    def test_render_from_path_preserves_error_on_failure(self, db):
        """render_from_path 异常时 → 失败 + 保留错误原因。"""
        dept = _make_dept(db)
        user = _make_user(db, "user_err2", Role.EMPLOYEE, dept.id)
        db.commit()

        entry = _make_entry(db, user.id,
                            doc_render_status="pending",
                            file_ext=".docx")
        db.commit()

        # 创建一个无效的 docx 文件（随便写点内容，不是真的 docx）
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False, mode="w") as f:
            f.write("not a real docx")
            tmp_path = f.name

        try:
            from app.services.doc_renderer import render_from_path
            render_from_path(db, entry, tmp_path)

            # 无效 docx 应该抛异常 → failed + 有错误信息
            assert entry.doc_render_status == "failed"
            assert entry.doc_render_error is not None
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# 4. 上传接口集成测试
# ══════════════════════════════════════════════════════════════════════════════

class TestUploadIntegration:
    """知识库文件上传端到端测试。"""

    def test_upload_txt_creates_entry_with_content(self, client, db):
        """上传 txt 文件 → 创建条目，content 非空。"""
        dept = _make_dept(db)
        user = _make_user(db, "uploader1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "uploader1")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("这是一段测试正文内容，用于验证上传流程。")
            tmp_path = f.name

        try:
            with open(tmp_path, "rb") as fp:
                resp = client.post(
                    "/api/knowledge/upload",
                    headers=_auth(token),
                    data={"title": "测试txt上传", "category": "experience"},
                    files={"file": ("test.txt", fp, "text/plain")},
                )
            assert resp.status_code == 200, resp.text
            result = resp.json()
            assert "id" in result

            # 验证条目
            entry = db.get(KnowledgeEntry, result["id"])
            assert entry is not None
            assert entry.file_ext == ".txt"
            # 内容应该有值
            assert entry.content or entry.content_html
        finally:
            os.unlink(tmp_path)

    def test_upload_md_creates_content_html(self, client, db):
        """上传 markdown 文件 → content_html 生成。"""
        dept = _make_dept(db)
        user = _make_user(db, "uploader2", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "uploader2")

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("# 标题\n\n这是正文\n\n## 子标题\n\n- 要点1\n- 要点2")
            tmp_path = f.name

        try:
            with open(tmp_path, "rb") as fp:
                resp = client.post(
                    "/api/knowledge/upload",
                    headers=_auth(token),
                    data={"title": "测试md上传", "category": "methodology"},
                    files={"file": ("test.md", fp, "text/markdown")},
                )
            assert resp.status_code == 200, resp.text
            entry = db.get(KnowledgeEntry, resp.json()["id"])
            # Markdown 应该有 content_html
            assert entry.content_html or entry.content
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# 5. 详情接口测试
# ══════════════════════════════════════════════════════════════════════════════

class TestDetailAPI:
    """知识条目详情接口字段验证。"""

    def test_ready_entry_has_content(self, client, db):
        """ready 条目详情必须包含 content_html 或 content。"""
        dept = _make_dept(db)
        user = _make_user(db, "detail_user1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "detail_user1")

        entry = _make_entry(db, user.id,
                            doc_render_status="ready",
                            doc_render_mode="native_html",
                            content_html="<p>可查看内容</p>",
                            content="可查看内容")
        db.commit()

        resp = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_render_status"] == "ready"
        assert data.get("content_html") or data.get("content")

    def test_failed_entry_has_error(self, client, db):
        """failed 条目详情必须包含 doc_render_error。"""
        dept = _make_dept(db)
        user = _make_user(db, "detail_user2", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "detail_user2")

        entry = _make_entry(db, user.id,
                            doc_render_status="failed",
                            doc_render_error="转换超时",
                            content="")
        db.commit()

        resp = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_render_status"] == "failed"
        assert data.get("doc_render_error") == "转换超时"

    def test_list_shows_render_status(self, client, db):
        """列表接口返回 doc_render_status 字段。"""
        dept = _make_dept(db)
        user = _make_user(db, "detail_user3", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "detail_user3")

        _make_entry(db, user.id,
                    title="可查看文档",
                    doc_render_status="ready",
                    content_html="<p>内容</p>")
        _make_entry(db, user.id,
                    title="失败文档",
                    doc_render_status="failed",
                    doc_render_error="解析失败")
        db.commit()

        resp = client.get("/api/knowledge", headers=_auth(token))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2

        statuses = {e["title"]: e.get("doc_render_status") for e in items}
        assert statuses.get("可查看文档") == "ready"
        assert statuses.get("失败文档") == "failed"
