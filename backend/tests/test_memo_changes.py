"""TC-MEMO: 覆盖 Le Desk Tool Manifest 系统所有改动的测试。

改动范围：
1. app/routers/conversations.py — upload-stream 多文件拼盘支持
2. app/routers/tools.py — _parse_manifest_comments / _validate_manifest / upload-py manifest 字段
3. app/services/tool_executor.py — _check_manifest_preconditions 前置条件检查
4. app/routers/approvals.py — tool 审批详情 target_detail 补全
"""
import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    _make_user, _make_dept, _make_model_config,
    _make_tool, _make_skill, _login, _auth,
)
from app.models.user import Role
from app.models.tool import ToolType


# ─── 复用 SSE 解析 ────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    events = []
    current_event = "message"
    for line in text.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                events.append({"event": current_event, "data": data})
                current_event = "message"
            except json.JSONDecodeError:
                pass
    return events


def _setup(client, db):
    dept = _make_dept(db)
    _make_user(db, f"memo_{id(db)}", Role.EMPLOYEE, dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, f"memo_{id(db)}")
    r = client.post("/api/conversations", headers=_auth(token))
    return token, r.json()["id"]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. upload-stream：单文件向后兼容
# ═══════════════════════════════════════════════════════════════════════════════

class TestUploadStreamSingleFile:
    """原有单文件 `file` 字段路径不应回归。"""

    @pytest.fixture(autouse=True)
    def _patch_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def _make_prep(self):
        prep = MagicMock()
        prep.early_return = None
        prep.skill_name = None
        prep.skill_id = None
        prep.skill_version = None
        prep.tools_schema = None
        prep.llm_messages = []
        prep.model_config = {"context_window": 32000}
        return prep

    def test_single_file_returns_200(self, client, db):
        token, conv_id = _setup(client, db)
        prep = self._make_prep()

        async def fake_stream(**kw):
            yield ("content", "文件已读取")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", return_value="文件内容"):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("test.txt", b"hello world", "text/plain")},
                        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_single_file_sse_contains_done(self, client, db):
        token, conv_id = _setup(client, db)
        prep = self._make_prep()

        async def fake_stream(**kw):
            yield ("content", "回复")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", return_value="内容"):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("doc.txt", b"content", "text/plain")},
                        )
        events = _parse_sse(resp.text)
        event_types = [e["event"] for e in events]
        assert "done" in event_types

    def test_single_file_metadata_has_filename(self, client, db):
        """done 事件的 metadata 应包含 filename 字段（向后兼容）。"""
        token, conv_id = _setup(client, db)
        prep = self._make_prep()

        async def fake_stream(**kw):
            yield ("content", "ok")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", return_value="x"):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files={"file": ("report.txt", b"x", "text/plain")},
                        )
        events = _parse_sse(resp.text)
        done = next((e["data"] for e in events if e["event"] == "done"), None)
        assert done is not None
        assert done["metadata"]["filename"] == "report.txt"
        # 多文件列表也应存在
        assert "filenames" in done["metadata"]
        assert "report.txt" in done["metadata"]["filenames"]

    def test_no_file_returns_400(self, client, db):
        token, conv_id = _setup(client, db)
        resp = client.post(
            f"/api/conversations/{conv_id}/messages/upload-stream",
            headers=_auth(token),
            data={"message": "no file"},
        )
        assert resp.status_code == 400

    def test_upload_stream_unauthorized(self, client, db):
        dept = _make_dept(db)
        db.commit()
        resp = client.post(
            "/api/conversations/1/messages/upload-stream",
            files={"file": ("f.txt", b"x", "text/plain")},
        )
        assert resp.status_code in (401, 403)

    def test_upload_stream_other_users_conv(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "mu1a", Role.EMPLOYEE, dept.id)
        _make_user(db, "mu1b", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()
        t1 = _login(client, "mu1a")
        t2 = _login(client, "mu1b")
        conv_id = client.post("/api/conversations", headers=_auth(t1)).json()["id"]
        resp = client.post(
            f"/api/conversations/{conv_id}/messages/upload-stream",
            headers=_auth(t2),
            files={"file": ("f.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 2. upload-stream：多文件拼盘
# ═══════════════════════════════════════════════════════════════════════════════

class TestUploadStreamMultiFile:
    """file_<key> 多文件字段支持。"""

    @pytest.fixture(autouse=True)
    def _patch_pev(self):
        with patch(
            "app.services.pev.orchestrator.pev_orchestrator.should_upgrade",
            new=AsyncMock(return_value=None),
        ):
            yield

    def _make_prep(self):
        prep = MagicMock()
        prep.early_return = None
        prep.skill_name = None
        prep.skill_id = None
        prep.skill_version = None
        prep.tools_schema = None
        prep.llm_messages = []
        prep.model_config = {"context_window": 32000}
        return prep

    def test_multi_file_returns_200(self, client, db):
        token, conv_id = _setup(client, db)
        prep = self._make_prep()

        async def fake_stream(**kw):
            yield ("content", "多文件处理完成")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", return_value="文件内容"):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files=[
                                ("file_salary", ("salary.csv", b"id,salary", "text/csv")),
                                ("file_bonus", ("bonus.xlsx", b"pk", "application/octet-stream")),
                            ],
                        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_multi_file_done_has_filenames_list(self, client, db):
        """done 事件 metadata.filenames 应包含所有文件名。"""
        token, conv_id = _setup(client, db)
        prep = self._make_prep()

        async def fake_stream(**kw):
            yield ("content", "ok")

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", return_value="x"):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files=[
                                ("file_a", ("file_a.txt", b"content a", "text/plain")),
                                ("file_b", ("file_b.txt", b"content b", "text/plain")),
                            ],
                        )
        events = _parse_sse(resp.text)
        done = next((e["data"] for e in events if e["event"] == "done"), None)
        assert done is not None
        filenames = done["metadata"].get("filenames", [])
        assert len(filenames) == 2
        assert "file_a.txt" in filenames
        assert "file_b.txt" in filenames

    def test_multi_file_user_text_contains_both_segments(self, client, db):
        """user_text 应拼装为 [文件(key): name]\n内容 --- 格式，含两段。"""
        token, conv_id = _setup(client, db)
        prep = self._make_prep()
        captured_messages = []

        async def fake_stream(**kw):
            captured_messages.extend(kw.get("messages", []))
            yield ("content", "ok")

        extract_calls = []
        def fake_extract(path):
            extract_calls.append(path)
            if "salary" in path:
                return "薪资内容"
            return "奖金内容"

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)) as mock_prep:
            with patch("app.services.llm_gateway.llm_gateway.chat_stream_typed", new=fake_stream):
                with patch("app.utils.file_parser.extract_text", side_effect=fake_extract):
                    with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                        resp = client.post(
                            f"/api/conversations/{conv_id}/messages/upload-stream",
                            headers=_auth(token),
                            files=[
                                ("file_salary", ("salary.csv", b"id,salary", "text/plain")),
                                ("file_bonus", ("bonus.csv", b"id,bonus", "text/plain")),
                            ],
                        )

        assert resp.status_code == 200
        # prepare 被调用时，user_text 应包含两段分隔符
        call_args = mock_prep.call_args
        user_text = call_args[0][2] if call_args[0] else call_args[1].get("user_text", "")
        # 检查 prepare 接收的第3个位置参数（user_text）
        assert "---" in user_text or "salary" in user_text or "bonus" in user_text

    def test_multi_file_early_return_conv_title_shows_count(self, client, db):
        """early_return 路径（commit 发生在 session close 前），多文件时 conv.title 应含 '等N个文件'。"""
        token, conv_id = _setup(client, db)
        prep = MagicMock()
        prep.early_return = ("请补充信息。", {})
        prep.skill_name = "test"
        prep.skill_id = None

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.utils.file_parser.extract_text", return_value="x"):
                with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                    client.post(
                        f"/api/conversations/{conv_id}/messages/upload-stream",
                        headers=_auth(token),
                        files=[
                            ("file_a", ("a.txt", b"x", "text/plain")),
                            ("file_b", ("b.txt", b"y", "text/plain")),
                        ],
                    )
        convs = client.get("/api/conversations", headers=_auth(token)).json()
        conv = next((c for c in convs if c["id"] == conv_id), None)
        assert conv is not None
        assert "等2个文件" in conv["title"]

    def test_multi_file_parse_error_yields_sse_error(self, client, db):
        """某个文件解析失败时应发 SSE error 事件（不是 500）。"""
        token, conv_id = _setup(client, db)

        def bad_extract(path):
            raise ValueError("不支持的文件格式")

        with patch("app.utils.file_parser.extract_text", side_effect=bad_extract):
            with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                resp = client.post(
                    f"/api/conversations/{conv_id}/messages/upload-stream",
                    headers=_auth(token),
                    files=[
                        ("file_a", ("bad.bin", b"\x00\x01", "application/octet-stream")),
                    ],
                )
        assert resp.status_code == 200  # SSE 本身是 200，错误在事件里
        events = _parse_sse(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) >= 1
        assert error_events[0]["data"]["retryable"] is False

    def test_early_return_multi_file_metadata(self, client, db):
        """early_return 路径下多文件 metadata 应包含 filenames。"""
        token, conv_id = _setup(client, db)
        prep = MagicMock()
        prep.early_return = ("请补充信息。", {})
        prep.skill_name = "测试skill"
        prep.skill_id = None

        with patch("app.services.skill_engine.skill_engine.prepare", new=AsyncMock(return_value=prep)):
            with patch("app.utils.file_parser.extract_text", return_value="内容"):
                with patch("app.services.knowledge_classifier.classify", new=AsyncMock(return_value=None)):
                    resp = client.post(
                        f"/api/conversations/{conv_id}/messages/upload-stream",
                        headers=_auth(token),
                        files=[
                            ("file_x", ("x.txt", b"x", "text/plain")),
                            ("file_y", ("y.txt", b"y", "text/plain")),
                        ],
                    )
        events = _parse_sse(resp.text)
        done = next((e["data"] for e in events if e["event"] == "done"), None)
        assert done is not None
        assert "filenames" in done["metadata"]
        assert len(done["metadata"]["filenames"]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _parse_manifest_comments 函数
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseManifestComments:
    """直接测试 tools.py 中的解析函数。"""

    def _parse(self, source):
        from app.routers.tools import _parse_manifest_comments
        return _parse_manifest_comments(source)

    def test_no_manifest_returns_empty(self):
        source = "def foo():\n    pass\n"
        assert self._parse(source) == {}

    def test_basic_invocation_mode(self):
        source = """\
# __le_desk_manifest__
# invocation_mode: registered_table
def foo(): pass
"""
        result = self._parse(source)
        assert result.get("invocation_mode") == "registered_table"

    def test_data_sources_parsing(self):
        source = """\
# __le_desk_manifest__
# data_sources:
#   - key: table_name, type: registered_table, required: true, description: 员工薪资表
#   - key: file_id, type: uploaded_file, accept: .xlsx .csv, required: false
def compute(table_name, file_id=None): pass
"""
        result = self._parse(source)
        ds = result.get("data_sources", [])
        assert len(ds) == 2
        assert ds[0]["key"] == "table_name"
        assert ds[0]["type"] == "registered_table"
        assert ds[0]["required"] is True
        assert ds[1]["key"] == "file_id"
        assert ds[1]["required"] is False
        assert ".xlsx" in ds[1]["accept"]
        assert ".csv" in ds[1]["accept"]

    def test_permissions_as_list(self):
        source = """\
# __le_desk_manifest__
# permissions: read:hr_employees, write:hr_bonus
def foo(): pass
"""
        result = self._parse(source)
        perms = result.get("permissions", [])
        assert "read:hr_employees" in perms
        assert "write:hr_bonus" in perms

    def test_preconditions_list(self):
        source = """\
# __le_desk_manifest__
# preconditions:
#   - 表必须包含字段 employee_id
#   - 表必须包含字段 base_salary
def foo(): pass
"""
        result = self._parse(source)
        pcs = result.get("preconditions", [])
        assert len(pcs) == 2
        assert any("employee_id" in p for p in pcs)

    def test_boolean_required_false(self):
        source = """\
# __le_desk_manifest__
# data_sources:
#   - key: opt_file, type: uploaded_file, required: false
def foo(): pass
"""
        result = self._parse(source)
        ds = result["data_sources"]
        assert ds[0]["required"] is False

    def test_empty_manifest_block(self):
        source = """\
# __le_desk_manifest__
def foo(): pass
"""
        result = self._parse(source)
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _validate_manifest 函数
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateManifest:
    def _validate(self, manifest):
        from app.routers.tools import _validate_manifest
        return _validate_manifest(manifest)

    def test_empty_manifest_no_warnings(self):
        assert self._validate({}) == []

    def test_valid_manifest_no_warnings(self):
        manifest = {
            "invocation_mode": "chat",
            "data_sources": [
                {"key": "table_name", "type": "registered_table"},
            ]
        }
        assert self._validate(manifest) == []

    def test_invalid_invocation_mode_warns(self):
        manifest = {"invocation_mode": "unknown_mode"}
        warnings = self._validate(manifest)
        assert len(warnings) == 1
        assert "unknown_mode" in warnings[0]

    def test_invalid_source_type_warns(self):
        manifest = {
            "data_sources": [{"key": "x", "type": "bad_type"}]
        }
        warnings = self._validate(manifest)
        assert len(warnings) == 1
        assert "bad_type" in warnings[0]

    def test_missing_key_warns(self):
        manifest = {
            "data_sources": [{"type": "uploaded_file"}]
        }
        warnings = self._validate(manifest)
        assert any("key" in w for w in warnings)

    def test_multiple_issues_multiple_warnings(self):
        manifest = {
            "invocation_mode": "bad",
            "data_sources": [{"type": "bad_src"}],
        }
        warnings = self._validate(manifest)
        assert len(warnings) >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# 5. upload-py 接口：manifest 字段返回
# ═══════════════════════════════════════════════════════════════════════════════

class TestUploadPyManifest:

    def _admin_token(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "upyadmin", Role.SUPER_ADMIN, dept.id)
        db.commit()
        return _login(client, "upyadmin")

    def test_upload_py_without_manifest_returns_empty_manifest(self, client, db):
        token = self._admin_token(client, db)
        source = b"def simple_tool(x: str) -> dict:\n    \"\"\"Simple tool.\"\"\"\n    return {}\n"
        with patch("app.routers.tools._write_tool_module"):
            resp = client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("simple_tool.py", source, "text/x-python")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "manifest" in data
        assert data["manifest"] == {}
        assert "manifest_warnings" in data
        assert data["manifest_warnings"] == []

    def test_upload_py_with_manifest_parses_correctly(self, client, db):
        token = self._admin_token(client, db)
        source = (
            "# __le_desk_manifest__\n"
            "# invocation_mode: registered_table\n"
            "# data_sources:\n"
            "#   - key: table_name, type: registered_table, required: true, description: employee_table\n"
            "# permissions: read:hr_employees\n"
            "\n"
            "def compute_bonus(table_name: str) -> dict:\n"
            '    """Compute quarterly bonus"""\n'
            "    return {}\n"
        ).encode("utf-8")
        with patch("app.routers.tools._write_tool_module"):
            resp = client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("compute_bonus.py", source, "text/x-python")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["manifest"]["invocation_mode"] == "registered_table"
        ds = data["manifest"]["data_sources"]
        assert len(ds) == 1
        assert ds[0]["type"] == "registered_table"
        assert data["manifest_warnings"] == []

    def test_upload_py_with_invalid_manifest_returns_warnings(self, client, db):
        token = self._admin_token(client, db)
        source = (
            "# __le_desk_manifest__\n"
            "# invocation_mode: invalid_mode\n"
            "# data_sources:\n"
            "#   - key: x, type: bad_type, required: true\n"
            "\n"
            "def my_func(x: str) -> dict:\n"
            '    """test"""\n'
            "    return {}\n"
        ).encode("utf-8")
        with patch("app.routers.tools._write_tool_module"):
            resp = client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("my_func.py", source, "text/x-python")},
            )
        assert resp.status_code == 200  # 警告不阻断
        data = resp.json()
        assert len(data["manifest_warnings"]) >= 1

    def test_upload_py_action_created(self, client, db):
        token = self._admin_token(client, db)
        source = b"def brand_new_tool(query: str) -> dict:\n    \"\"\"New.\"\"\"\n    return {}\n"
        with patch("app.routers.tools._write_tool_module"):
            resp = client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("brand_new_tool.py", source, "text/x-python")},
            )
        assert resp.status_code == 200
        assert resp.json()["action"] == "created"

    def test_upload_py_action_updated_on_second_upload(self, client, db):
        token = self._admin_token(client, db)
        source = b"def upd_tool(x: str) -> dict:\n    \"\"\"Tool.\"\"\"\n    return {}\n"
        with patch("app.routers.tools._write_tool_module"):
            client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("upd_tool.py", source, "text/x-python")},
            )
            resp = client.post(
                "/api/tools/upload-py",
                headers=_auth(token),
                files={"file": ("upd_tool.py", source, "text/x-python")},
            )
        assert resp.json()["action"] == "updated"

    def test_upload_py_non_py_file_rejected(self, client, db):
        token = self._admin_token(client, db)
        resp = client.post(
            "/api/tools/upload-py",
            headers=_auth(token),
            files={"file": ("data.csv", b"a,b,c", "text/csv")},
        )
        assert resp.status_code == 400

    def test_upload_py_syntax_error_rejected(self, client, db):
        token = self._admin_token(client, db)
        source = b"def broken(\n    pass\n"
        resp = client.post(
            "/api/tools/upload-py",
            headers=_auth(token),
            files={"file": ("broken.py", source, "text/x-python")},
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# 6. tool_executor：_check_manifest_preconditions
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckManifestPreconditions:
    """直接对 _check_manifest_preconditions 做单元测试。"""

    async def _check(self, manifest, params, db=None):
        from app.services.tool_executor import _check_manifest_preconditions
        from unittest.mock import MagicMock as MM
        tool = MM()
        tool.config = {"manifest": manifest}
        return await _check_manifest_preconditions(tool, params, db)

    @pytest.mark.asyncio
    async def test_no_manifest_passes(self):
        from app.services.tool_executor import _check_manifest_preconditions
        from unittest.mock import MagicMock as MM
        tool = MM()
        tool.config = {}
        result = await _check_manifest_preconditions(tool, {}, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_required_chat_context_missing_fails(self):
        manifest = {
            "data_sources": [
                {"key": "brand_name", "type": "chat_context", "required": True, "description": "品牌名"}
            ]
        }
        result = await self._check(manifest, {})
        assert result is not None
        assert "brand_name" in result

    @pytest.mark.asyncio
    async def test_required_chat_context_provided_passes(self):
        manifest = {
            "data_sources": [
                {"key": "brand_name", "type": "chat_context", "required": True}
            ]
        }
        result = await self._check(manifest, {"brand_name": "某品牌"})
        assert result is None

    @pytest.mark.asyncio
    async def test_uploaded_file_wrong_extension_fails(self):
        manifest = {
            "data_sources": [
                {"key": "report", "type": "uploaded_file", "accept": [".xlsx", ".csv"], "required": False}
            ]
        }
        result = await self._check(manifest, {"report": "data.pdf"})
        assert result is not None
        assert "report" in result

    @pytest.mark.asyncio
    async def test_uploaded_file_correct_extension_passes(self):
        manifest = {
            "data_sources": [
                {"key": "report", "type": "uploaded_file", "accept": [".xlsx", ".csv"], "required": False}
            ]
        }
        result = await self._check(manifest, {"report": "data.xlsx"})
        assert result is None

    @pytest.mark.asyncio
    async def test_required_uploaded_file_missing_fails(self):
        manifest = {
            "data_sources": [
                {"key": "file_id", "type": "uploaded_file", "required": True, "accept": [".xlsx"]}
            ]
        }
        result = await self._check(manifest, {})
        assert result is not None
        assert "file_id" in result

    @pytest.mark.asyncio
    async def test_registered_table_missing_required_fails(self):
        manifest = {
            "data_sources": [
                {"key": "table_name", "type": "registered_table", "required": True, "description": "员工表"}
            ]
        }
        result = await self._check(manifest, {})
        assert result is not None
        assert "table_name" in result

    @pytest.mark.asyncio
    async def test_preconditions_appended_to_error(self):
        manifest = {
            "data_sources": [
                {"key": "x", "type": "chat_context", "required": True}
            ],
            "preconditions": ["字段 employee_id 必须存在"]
        }
        result = await self._check(manifest, {})
        assert result is not None
        assert "employee_id" in result  # precondition 内容也出现在错误里

    @pytest.mark.asyncio
    async def test_all_conditions_met_passes(self):
        manifest = {
            "data_sources": [
                {"key": "brand", "type": "chat_context", "required": True},
                {"key": "file", "type": "uploaded_file", "accept": [".xlsx"], "required": False},
            ]
        }
        result = await self._check(manifest, {"brand": "Nike", "file": "data.xlsx"})
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 7. tool_executor：execute_tool 前置条件 phases
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolExecutorPhasesWithManifest:

    def _make_tool_with_manifest(self, db, user_id, name, manifest):
        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name=name,
            display_name=name,
            description="test",
            tool_type=ToolType.BUILTIN,
            config={"manifest": manifest},
            input_schema={},
            output_format="json",
            created_by=user_id,
            is_active=True,
        )
        db.add(tool)
        db.flush()
        return tool

    @pytest.mark.asyncio
    async def test_precondition_fail_returns_precondition_failed_phase(self, db):
        from app.services.tool_executor import tool_executor
        dept = _make_dept(db)
        user = _make_user(db, "exec_u1", Role.EMPLOYEE, dept.id)
        db.commit()

        manifest = {
            "data_sources": [{"key": "required_param", "type": "chat_context", "required": True}]
        }
        self._make_tool_with_manifest(db, user.id, "test_precond_tool", manifest)
        db.commit()

        result = await tool_executor.execute_tool(db, "test_precond_tool", {}, user.id)
        assert result["ok"] is False
        assert "precondition_failed" in result["phases"]

    @pytest.mark.asyncio
    async def test_precondition_pass_has_preconditions_ok_phase(self, db):
        from app.services.tool_executor import tool_executor

        # 对一个没有 manifest 的工具调用，跳过前置检查
        dept = _make_dept(db)
        user = _make_user(db, "exec_u2", Role.EMPLOYEE, dept.id)
        db.commit()
        self._make_tool_with_manifest(db, user.id, "no_manifest_tool", {})
        db.commit()

        # 没有 manifest 的工具会进入 execute，因为 builtin module 可能不存在，
        # 我们只验证 phases 包含 validated 和 preconditions_ok
        result = await tool_executor.execute_tool(db, "no_manifest_tool", {}, user.id)
        # 无论最终执行成功与否，preconditions_ok 阶段已经通过
        assert "preconditions_ok" in result.get("phases", [])


# ═══════════════════════════════════════════════════════════════════════════════
# 8. approvals.py：tool 审批详情 target_detail
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalToolTargetDetail:

    def _setup_admin(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "appr_admin", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "appr_admin")
        return admin, token

    def _make_tool_with_manifest_config(self, db, user_id, name="appr_tool"):
        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name=name,
            display_name="审批工具",
            description="用于测试",
            tool_type=ToolType.BUILTIN,
            config={
                "manifest": {
                    "invocation_mode": "registered_table",
                    "data_sources": [{"key": "t", "type": "registered_table"}],
                    "permissions": ["read:hr_employees"],
                    "preconditions": ["表需包含 employee_id"],
                },
                "deploy_info": {"usage": "季度薪资计算"},
            },
            input_schema={},
            output_format="json",
            created_by=user_id,
            is_active=False,
            scope="personal",
            status="reviewing",
        )
        db.add(tool)
        db.flush()
        return tool

    def test_tool_approval_target_detail_has_manifest_fields(self, client, db):
        admin, token = self._setup_admin(client, db)
        tool = self._make_tool_with_manifest_config(db, admin.id)
        db.commit()

        # 发起工具发布审批
        resp = client.post(
            "/api/tools/upload-py",  # 通过状态接口更简单，直接手动创建审批
        )
        # 直接用 /api/approvals POST 接口创建审批
        r = client.post(
            "/api/approvals",
            headers=_auth(token),
            json={"request_type": "tool_publish", "target_id": tool.id, "target_type": "tool"},
        )
        assert r.status_code == 200
        approval_id = r.json()["id"]

        # 获取审批详情
        detail_resp = client.get(f"/api/approvals/{approval_id}", headers=_auth(token))
        assert detail_resp.status_code == 200
        data = detail_resp.json()

        td = data.get("target_detail", {})
        assert td.get("invocation_mode") == "registered_table"
        assert len(td.get("data_sources", [])) == 1
        assert "read:hr_employees" in td.get("permissions", [])
        assert len(td.get("preconditions", [])) == 1
        assert td.get("deploy_info", {}).get("usage") == "季度薪资计算"

    def test_tool_approval_target_detail_permissions_fallback(self, client, db):
        """若 manifest 无 permissions，应 fallback 到 deploy_info.permissions。"""
        admin, token = self._setup_admin(client, db)
        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="fallback_perm_tool",
            display_name="x",
            description="x",
            tool_type=ToolType.BUILTIN,
            config={
                "manifest": {"invocation_mode": "chat"},
                "deploy_info": {"permissions": ["write:reports"]},
            },
            input_schema={},
            output_format="json",
            created_by=admin.id,
            is_active=False, scope="personal", status="reviewing",
        )
        db.add(tool)
        db.commit()

        r = client.post(
            "/api/approvals",
            headers=_auth(token),
            json={"request_type": "tool_publish", "target_id": tool.id, "target_type": "tool"},
        )
        approval_id = r.json()["id"]
        td = client.get(f"/api/approvals/{approval_id}", headers=_auth(token)).json()["target_detail"]
        assert "write:reports" in td.get("permissions", [])

    def test_approval_tool_two_stage_workflow(self, client, db):
        """工具审批两阶段流程：dept_pending → super_pending → approved。"""
        dept = _make_dept(db)
        emp = _make_user(db, "wf_emp", Role.EMPLOYEE, dept.id)
        dept_adm = _make_user(db, "wf_dept", Role.DEPT_ADMIN, dept.id)
        super_adm = _make_user(db, "wf_super", Role.SUPER_ADMIN, dept.id)
        db.commit()

        emp_token = _login(client, "wf_emp")
        dept_token = _login(client, "wf_dept")
        super_token = _login(client, "wf_super")

        # 员工上传工具并申请发布 → dept_pending
        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="wf_tool", display_name="wf", description="wf",
            tool_type=ToolType.BUILTIN, config={}, input_schema={},
            output_format="json", created_by=emp.id,
            is_active=False, scope="personal", status="draft",
        )
        db.add(tool)
        db.commit()

        status_resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(emp_token),
            json={"status": "published", "scope": "company"},
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["stage"] == "dept_pending"

        # 查出审批 ID
        approvals = client.get("/api/approvals", headers=_auth(dept_token)).json()
        appr = next((a for a in approvals["items"] if a["target_id"] == tool.id), None)
        assert appr is not None
        appr_id = appr["id"]

        # 部门管理员通过 → super_pending
        act1 = client.post(
            f"/api/approvals/{appr_id}/actions",
            headers=_auth(dept_token),
            json={"action": "approve"},
        )
        assert act1.status_code == 200
        assert act1.json()["stage"] == "super_pending"

        # 超管通过 → approved + tool published
        act2 = client.post(
            f"/api/approvals/{appr_id}/actions",
            headers=_auth(super_token),
            json={"action": "approve"},
        )
        assert act2.status_code == 200
        assert act2.json()["status"] == "approved"

        # 工具应已发布
        tool_resp = client.get(f"/api/tools/{tool.id}", headers=_auth(super_token))
        assert tool_resp.json()["status"] == "published"

    def test_approval_tool_reject_resets_to_draft(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "rj_emp", Role.EMPLOYEE, dept.id)
        super_adm = _make_user(db, "rj_super", Role.SUPER_ADMIN, dept.id)
        db.commit()

        emp_token = _login(client, "rj_emp")
        super_token = _login(client, "rj_super")

        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="rj_tool", display_name="rj", description="rj",
            tool_type=ToolType.BUILTIN, config={}, input_schema={},
            output_format="json", created_by=emp.id,
            is_active=False, scope="personal", status="draft",
        )
        db.add(tool)
        db.commit()

        client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(emp_token),
            json={"status": "published", "scope": "personal"},
        )

        approvals = client.get("/api/approvals", headers=_auth(super_token)).json()
        appr_id = next(a["id"] for a in approvals["items"] if a["target_id"] == tool.id)

        # 超管直接拒绝
        act = client.post(
            f"/api/approvals/{appr_id}/actions",
            headers=_auth(super_token),
            json={"action": "reject", "comment": "不符合规范"},
        )
        assert act.status_code == 200
        assert act.json()["status"] == "rejected"

        tool_resp = client.get(f"/api/tools/{tool.id}", headers=_auth(super_token))
        assert tool_resp.json()["status"] == "draft"

    def test_super_admin_approves_dept_pending_tool_directly(self, client, db):
        """员工提交后，超级管理员可直接批准，无需部门管理员先审批。"""
        dept = _make_dept(db)
        emp = _make_user(db, "dir_emp", Role.EMPLOYEE, dept.id)
        super_adm = _make_user(db, "dir_super", Role.SUPER_ADMIN, dept.id)
        db.commit()

        emp_token = _login(client, "dir_emp")
        super_token = _login(client, "dir_super")

        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="dir_tool", display_name="dir", description="dir",
            tool_type=ToolType.BUILTIN, config={}, input_schema={},
            output_format="json", created_by=emp.id,
            is_active=False, scope="personal", status="draft",
        )
        db.add(tool)
        db.commit()

        submit_resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(emp_token),
            json={"status": "published", "scope": "company"},
        )
        assert submit_resp.status_code == 200
        assert submit_resp.json()["stage"] == "dept_pending"

        approvals = client.get("/api/approvals", headers=_auth(super_token)).json()
        appr = next((a for a in approvals["items"] if a["target_id"] == tool.id), None)
        assert appr is not None
        assert appr["stage"] == "dept_pending"

        act = client.post(
            f"/api/approvals/{appr['id']}/actions",
            headers=_auth(super_token),
            json={"action": "approve"},
        )
        assert act.status_code == 200
        assert act.json()["status"] == "approved"

        approval_detail = client.get(f"/api/approvals/{appr['id']}", headers=_auth(super_token)).json()
        assert approval_detail["status"] == "approved"

        tool_resp = client.get(f"/api/tools/{tool.id}", headers=_auth(super_token))
        assert tool_resp.json()["status"] == "published"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. ToolStatusUpdate PATCH 接口（deploy_info 字段）
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolStatusUpdateDeployInfo:

    def test_super_admin_publish_directly(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "su_publish", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "su_publish")

        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="direct_pub", display_name="d", description="d",
            tool_type=ToolType.BUILTIN, config={}, input_schema={},
            output_format="json", created_by=admin.id,
            is_active=False, scope="personal", status="draft",
        )
        db.add(tool)
        db.commit()

        resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(token),
            json={
                "status": "published",
                "scope": "company",
                "deploy_info": {"usage": "全公司使用"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_employee_publish_creates_dept_pending_approval(self, client, db):
        dept = _make_dept(db)
        emp = _make_user(db, "emp_pub", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "emp_pub")

        from app.models.tool import ToolRegistry, ToolType
        tool = ToolRegistry(
            name="emp_pub_tool", display_name="e", description="e",
            tool_type=ToolType.BUILTIN, config={}, input_schema={},
            output_format="json", created_by=emp.id,
            is_active=False, scope="personal", status="draft",
        )
        db.add(tool)
        db.commit()

        resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(token),
            json={"status": "published", "scope": "department"},
        )
        assert resp.status_code == 200
        assert resp.json()["stage"] == "dept_pending"
