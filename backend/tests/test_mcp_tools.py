"""TC-MCP: MCP 工具上传、AI 配置生成、审批发布、executor 路径测试。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import _auth, _login, _make_dept, _make_tool, _make_user
from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
from app.models.tool import ToolRegistry, ToolType
from app.models.user import Role


# tool_publish 审批需要完整 checklist（5 项）
_TOOL_APPROVE_CHECKLIST = [{"status": "approved"}] * 5


# ── 辅助：构造 zip ─────────────────────────────────────────────────────────────

def _make_zip(files: dict[str, str]) -> bytes:
    """files: {相对路径: 内容}，返回 zip bytes。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


PYTHON_ZIP = _make_zip({
    "server.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef root(): return {}\n",
    "requirements.txt": "fastapi\nuvicorn\n",
})

NODE_ZIP = _make_zip({
    "index.js": "const express = require('express'); const app = express(); app.listen(process.env.PORT||3000);\n",
    "package.json": json.dumps({"name": "mcp-server", "main": "index.js", "dependencies": {}}),
})

UNKNOWN_ZIP = _make_zip({
    "README.md": "# My MCP Server\n",
})


# ── analyze_zip ───────────────────────────────────────────────────────────────

class TestAnalyzeZip:
    def test_python_project_detected(self, tmp_path):
        from app.services.mcp_installer import analyze_zip
        zp = tmp_path / "server.zip"
        zp.write_bytes(PYTHON_ZIP)
        r = analyze_zip(str(zp))
        assert r["project_type"] == "python"
        assert "python" in r["run_cmd"]
        assert r["entry_file"] in ("server.py", "main.py")

    def test_node_project_detected(self, tmp_path):
        from app.services.mcp_installer import analyze_zip
        zp = tmp_path / "node.zip"
        zp.write_bytes(NODE_ZIP)
        r = analyze_zip(str(zp))
        assert r["project_type"] == "node"
        assert "node" in r["run_cmd"]

    def test_unknown_project_warns(self, tmp_path):
        from app.services.mcp_installer import analyze_zip
        zp = tmp_path / "unknown.zip"
        zp.write_bytes(UNKNOWN_ZIP)
        r = analyze_zip(str(zp))
        assert r["project_type"] == "unknown"
        assert len(r["warnings"]) > 0

    def test_top_level_prefix_stripped(self, tmp_path):
        """zip 里有顶层目录前缀时能正确解析。"""
        from app.services.mcp_installer import analyze_zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("my-server/server.py", "")
            zf.writestr("my-server/requirements.txt", "requests\n")
        zp = tmp_path / "prefixed.zip"
        zp.write_bytes(buf.getvalue())
        r = analyze_zip(str(zp))
        assert r["project_type"] == "python"


# ── extract_zip ───────────────────────────────────────────────────────────────

class TestExtractZip:
    def test_files_extracted(self, tmp_path):
        from app.services.mcp_installer import extract_zip, _INSTALL_ROOT
        zp = tmp_path / "s.zip"
        zp.write_bytes(PYTHON_ZIP)
        with patch("app.services.mcp_installer._INSTALL_ROOT", tmp_path / "servers"):
            install_dir = extract_zip(str(zp), "test_tool")
        assert (install_dir / "server.py").exists()
        assert (install_dir / "requirements.txt").exists()

    def test_existing_dir_overwritten(self, tmp_path):
        from app.services.mcp_installer import extract_zip
        servers = tmp_path / "servers"
        servers.mkdir()
        (servers / "test_tool").mkdir()
        (servers / "test_tool" / "old_file.txt").write_text("old")
        zp = tmp_path / "s.zip"
        zp.write_bytes(PYTHON_ZIP)
        with patch("app.services.mcp_installer._INSTALL_ROOT", servers):
            install_dir = extract_zip(str(zp), "test_tool")
        assert not (install_dir / "old_file.txt").exists()
        assert (install_dir / "server.py").exists()


# ── POST /api/tools/upload-mcp ────────────────────────────────────────────────

class TestUploadMcp:
    def test_upload_python_zip(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp1")

        with patch("app.services.mcp_installer._INSTALL_ROOT", Path("/tmp/le_desk_test_servers")):
            resp = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("server.zip", PYTHON_ZIP, "application/zip")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "created"
        assert data["project_type"] == "python"
        assert "python" in data["run_cmd"]
        assert isinstance(data["id"], int)

    def test_upload_node_zip(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp2", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp2")

        with patch("app.services.mcp_installer._INSTALL_ROOT", Path("/tmp/le_desk_test_servers")):
            resp = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("node_server.zip", NODE_ZIP, "application/zip")},
            )
        assert resp.status_code == 200
        assert resp.json()["project_type"] == "node"

    def test_upload_non_zip_rejected(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp3", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp3")
        resp = client.post(
            "/api/tools/upload-mcp",
            headers=_auth(token),
            files={"file": ("bad.py", b"print('hello')", "text/plain")},
        )
        assert resp.status_code == 400

    def test_upload_creates_draft_tool(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp4", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp4")

        with patch("app.services.mcp_installer._INSTALL_ROOT", Path("/tmp/le_desk_test_servers")):
            resp = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("draft_server.zip", PYTHON_ZIP, "application/zip")},
            )
        tool_id = resp.json()["id"]
        tool = db.get(ToolRegistry, tool_id)
        assert tool is not None
        assert tool.status == "draft"
        assert tool.is_active is False
        assert tool.tool_type == ToolType.MCP

    def test_upload_same_zip_updates_existing(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp5", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp5")

        with patch("app.services.mcp_installer._INSTALL_ROOT", Path("/tmp/le_desk_test_servers")):
            r1 = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("my_server.zip", PYTHON_ZIP, "application/zip")},
            )
            r2 = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("my_server.zip", PYTHON_ZIP, "application/zip")},
            )
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.json()["action"] == "updated"

    def test_upload_unknown_zip_has_warning(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "umcp6", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "umcp6")

        with patch("app.services.mcp_installer._INSTALL_ROOT", Path("/tmp/le_desk_test_servers")):
            resp = client.post(
                "/api/tools/upload-mcp",
                headers=_auth(token),
                files={"file": ("unknown_server.zip", UNKNOWN_ZIP, "application/zip")},
            )
        assert resp.status_code == 200
        assert len(resp.json()["warnings"]) > 0


# ── POST /api/tools/generate-mcp-config ──────────────────────────────────────

class TestGenerateMcpConfig:
    def test_returns_structured_config(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "gmcp1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "gmcp1")

        mock_config = {
            "display_name": "HR绩效查询",
            "description": "查询员工绩效数据",
            "invocation_mode": "chat",
            "data_sources": [],
            "permissions": ["read:hr_employees"],
            "preconditions": [],
            "env_requirements": "需要 HR_DB_URL",
        }
        with patch("app.services.llm_gateway.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (json.dumps(mock_config), {})
            resp = client.post(
                "/api/tools/generate-mcp-config",
                headers=_auth(token),
                json={"description": "连接HR数据库查询员工绩效"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "HR绩效查询"
        assert "read:hr_employees" in data["permissions"]
        assert data["invocation_mode"] == "chat"

    def test_handles_llm_json_in_code_block(self, client, db):
        """LLM 返回 ```json ... ``` 包裹时能正确解析。"""
        dept = _make_dept(db)
        _make_user(db, "gmcp2", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "gmcp2")

        mock_config = {"display_name": "测试", "description": "x", "invocation_mode": "chat",
                       "data_sources": [], "permissions": [], "preconditions": [], "env_requirements": ""}
        wrapped = f"```json\n{json.dumps(mock_config)}\n```"
        with patch("app.services.llm_gateway.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (wrapped, {})
            resp = client.post(
                "/api/tools/generate-mcp-config",
                headers=_auth(token),
                json={"description": "测试工具"},
            )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "测试"

    def test_llm_failure_returns_500(self, client, db):
        dept = _make_dept(db)
        _make_user(db, "gmcp3", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "gmcp3")

        with patch("app.services.llm_gateway.llm_gateway.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.side_effect = Exception("LLM unavailable")
            resp = client.post(
                "/api/tools/generate-mcp-config",
                headers=_auth(token),
                json={"description": "任何描述"},
            )
        assert resp.status_code == 500

    def test_requires_auth(self, client, db):
        resp = client.post("/api/tools/generate-mcp-config", json={"description": "test"})
        assert resp.status_code in (401, 403)


# ── PATCH /api/tools/{id}/status（MCP 发布审批流）────────────────────────────

class TestMcpToolStatusFlow:
    def _make_mcp_tool(self, db, user_id, name="mcp_flow_tool"):
        tool = ToolRegistry(
            name=name,
            display_name="MCP流程工具",
            description="测试",
            tool_type=ToolType.MCP,
            config={"install_dir": "/tmp/fake", "project_type": "python", "run_cmd": "python server.py"},
            input_schema={},
            output_format="json",
            is_active=False,
            scope="personal",
            status="draft",
            created_by=user_id,
        )
        db.add(tool)
        db.flush()
        return tool

    def test_direct_publish_blocked(self, client, db):
        """工具不能单独发布，应返回 400。"""
        dept = _make_dept(db)
        emp = _make_user(db, "mflow_emp1", Role.EMPLOYEE, dept.id)
        tool = self._make_mcp_tool(db, emp.id, "mcp_emp_flow1")
        db.commit()
        token = _login(client, "mflow_emp1")
        resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(token),
            json={"status": "published", "scope": "company"},
        )
        assert resp.status_code == 400

    def test_archive_allowed(self, client, db):
        """归档操作应正常通过。"""
        dept = _make_dept(db)
        sa = _make_user(db, "mflow_sa1", Role.SUPER_ADMIN, dept.id)
        tool = self._make_mcp_tool(db, sa.id, "mcp_sa_flow1")
        db.commit()
        token = _login(client, "mflow_sa1")
        resp = client.patch(
            f"/api/tools/{tool.id}/status",
            headers=_auth(token),
            json={"status": "archived"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"


# ── 审批 approve → install_and_start 被调用 ──────────────────────────────────

class TestApprovalInstallHook:
    def _setup_approval(self, db, stage="super_pending"):
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        dept = _make_dept(db)
        sa = _make_user(db, f"apinst_sa_{stage}", Role.SUPER_ADMIN, dept.id)
        emp = _make_user(db, f"apinst_emp_{stage}", Role.EMPLOYEE, dept.id)
        tool = ToolRegistry(
            name=f"mcp_apinst_{stage}",
            display_name="MCP审批安装",
            description="",
            tool_type=ToolType.MCP,
            config={"install_dir": "/tmp/fake", "project_type": "python", "run_cmd": "python server.py"},
            input_schema={},
            output_format="json",
            is_active=False,
            scope="company",
            status="reviewing",
            created_by=emp.id,
        )
        db.add(tool)
        db.flush()
        # 创建沙盒测试报告（审批通过需要关联）
        from app.models.sandbox import SandboxTestSession, SandboxTestReport
        session = SandboxTestSession(
            target_type="tool", target_id=tool.id, tester_id=sa.id,
        )
        db.add(session)
        db.flush()
        report = SandboxTestReport(
            session_id=session.id, target_type="tool", target_id=tool.id,
            tester_id=sa.id, approval_eligible=True, report_hash="testhash",
        )
        db.add(report)
        db.flush()

        req = ApprovalRequest(
            request_type=ApprovalRequestType.TOOL_PUBLISH,
            target_id=tool.id,
            target_type="tool",
            requester_id=emp.id,
            status=ApprovalStatus.PENDING,
            stage=stage,
            security_scan_result={
                "sandbox_test_report_id": report.id,
                "report_hash": report.report_hash,
            },
            evidence_pack={
                "tool_manifest": {"invocation_mode": "test"},
                "deploy_info": {"usage": "test"},
                "test_result": {"tested": True},
                "permission_declaration": [],
                "rollback_plan": "卸载工具即可",
            },
        )
        db.add(req)
        db.commit()
        return sa, tool, req

    def test_approve_super_pending_calls_installer(self, client, db):
        sa, tool, req = self._setup_approval(db, stage="super_pending")
        token = _login(client, f"apinst_sa_super_pending")

        mock_result = {"ok": True, "url": "http://localhost:12345"}
        with patch("app.services.mcp_installer.install_and_start", new_callable=AsyncMock) as mock_install:
            mock_install.return_value = mock_result
            resp = client.post(
                f"/api/approvals/{req.id}/actions",
                headers=_auth(token),
                json={"action": "approve", "checklist_result": _TOOL_APPROVE_CHECKLIST},
            )
        assert resp.status_code == 200
        mock_install.assert_called_once()

    def test_approve_super_pending_install_failure_recorded(self, client, db):
        """安装失败时审批仍成功，但 tool.is_active=False，comment 含错误信息。"""
        sa, tool, req = self._setup_approval(db, stage="super_pending")
        token = _login(client, f"apinst_sa_super_pending")

        with patch("app.services.mcp_installer.install_and_start", new_callable=AsyncMock) as mock_install:
            mock_install.return_value = {"ok": False, "error": "启动超时"}
            resp = client.post(
                f"/api/approvals/{req.id}/actions",
                headers=_auth(token),
                json={"action": "approve", "checklist_result": _TOOL_APPROVE_CHECKLIST},
            )
        assert resp.status_code == 200
        db.refresh(tool)
        assert tool.is_active is False

    def test_approve_dept_pending_advances_stage(self, client, db):
        """部门管理员通过第一步，stage 变为 super_pending，不触发安装。"""
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        dept = _make_dept(db)
        sa = _make_user(db, "apinst_sa_dp", Role.SUPER_ADMIN, dept.id)
        da = _make_user(db, "apinst_da_dp", Role.DEPT_ADMIN, dept.id)
        emp = _make_user(db, "apinst_emp_dp", Role.EMPLOYEE, dept.id)
        tool = ToolRegistry(
            name="mcp_apinst_dp",
            display_name="MCP部门审批",
            description="",
            tool_type=ToolType.MCP,
            config={"install_dir": "/tmp/fake", "project_type": "python", "run_cmd": "python server.py"},
            input_schema={},
            output_format="json",
            is_active=False,
            scope="company",
            status="reviewing",
            created_by=emp.id,
        )
        db.add(tool)
        db.flush()
        # 创建沙盒测试报告（审批通过需要关联）
        from app.models.sandbox import SandboxTestSession, SandboxTestReport
        session = SandboxTestSession(
            target_type="tool", target_id=tool.id, tester_id=sa.id,
        )
        db.add(session)
        db.flush()
        report = SandboxTestReport(
            session_id=session.id, target_type="tool", target_id=tool.id,
            tester_id=sa.id, approval_eligible=True, report_hash="testhash",
        )
        db.add(report)
        db.flush()

        req = ApprovalRequest(
            request_type=ApprovalRequestType.TOOL_PUBLISH,
            target_id=tool.id,
            target_type="tool",
            requester_id=emp.id,
            status=ApprovalStatus.PENDING,
            stage="dept_pending",
            security_scan_result={
                "sandbox_test_report_id": report.id,
                "report_hash": report.report_hash,
            },
            evidence_pack={
                "tool_manifest": {"invocation_mode": "test"},
                "deploy_info": {"usage": "test"},
                "test_result": {"tested": True},
                "permission_declaration": [],
                "rollback_plan": "卸载工具即可",
            },
        )
        db.add(req)
        db.commit()
        token = _login(client, "apinst_da_dp")

        with patch("app.services.mcp_installer.install_and_start", new_callable=AsyncMock) as mock_install:
            resp = client.post(
                f"/api/approvals/{req.id}/actions",
                headers=_auth(token),
                json={"action": "approve", "checklist_result": _TOOL_APPROVE_CHECKLIST},
            )
        assert resp.status_code == 200
        mock_install.assert_not_called()
        db.refresh(req)
        assert req.stage == "super_pending"

    def test_reject_rolls_back_tool_to_draft(self, client, db):
        sa, tool, req = self._setup_approval(db, stage="super_pending")
        token = _login(client, f"apinst_sa_super_pending")
        resp = client.post(
            f"/api/approvals/{req.id}/actions",
            headers=_auth(token),
            json={"action": "reject", "comment": "安全审查未通过"},
        )
        assert resp.status_code == 200
        db.refresh(tool)
        assert tool.status == "draft"
        assert tool.is_active is False


# ── mcp_installer.install_and_start 单元测试 ─────────────────────────────────

class TestInstallAndStart:
    @pytest.mark.asyncio
    async def test_missing_install_dir_returns_error(self, db):
        from app.services.mcp_installer import install_and_start
        dept = _make_dept(db)
        user = _make_user(db, "inst_u1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        tool = ToolRegistry(
            name="inst_missing",
            display_name="x",
            tool_type=ToolType.MCP,
            config={},  # 缺少 install_dir
            input_schema={},
            output_format="json",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()
        result = await install_and_start(db, tool)
        assert result["ok"] is False
        assert "install_dir" in result["error"] or "run_cmd" in result["error"]

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_error(self, db):
        from app.services.mcp_installer import install_and_start
        dept = _make_dept(db)
        user = _make_user(db, "inst_u2", Role.SUPER_ADMIN, dept.id)
        db.commit()
        tool = ToolRegistry(
            name="inst_nodir",
            display_name="x",
            tool_type=ToolType.MCP,
            config={"install_dir": "/nonexistent/path", "run_cmd": "python server.py", "project_type": "python"},
            input_schema={},
            output_format="json",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()
        result = await install_and_start(db, tool)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_successful_start_writes_url(self, tmp_path, db):
        from app.services.mcp_installer import install_and_start, _running_procs
        dept = _make_dept(db)
        user = _make_user(db, "inst_u3", Role.SUPER_ADMIN, dept.id)
        db.commit()

        # 创建假的 server.py（立即就绪，不真正监听）
        (tmp_path / "server.py").write_text("# dummy")
        (tmp_path / "requirements.txt").write_text("")

        tool = ToolRegistry(
            name="inst_ok",
            display_name="x",
            tool_type=ToolType.MCP,
            config={"install_dir": str(tmp_path), "run_cmd": "python server.py", "project_type": "python"},
            input_schema={},
            output_format="json",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()

        # mock 依赖安装（直接通过）和 HTTP 等待（立即就绪）
        with patch("app.services.mcp_installer._install_dependencies", new_callable=AsyncMock, return_value=(True, "")), \
             patch("app.services.mcp_installer._wait_for_http", new_callable=AsyncMock, return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc
            result = await install_and_start(db, tool)

        assert result["ok"] is True
        assert result["url"].startswith("http://localhost:")
        db.refresh(tool)
        assert tool.config["url"] == result["url"]
        assert tool.is_active is True
        # 清理进程注册表
        _running_procs.pop(tool.id, None)

    @pytest.mark.asyncio
    async def test_startup_timeout_returns_error(self, tmp_path, db):
        from app.services.mcp_installer import install_and_start
        dept = _make_dept(db)
        user = _make_user(db, "inst_u4", Role.SUPER_ADMIN, dept.id)
        db.commit()
        (tmp_path / "server.py").write_text("")

        tool = ToolRegistry(
            name="inst_timeout",
            display_name="x",
            tool_type=ToolType.MCP,
            config={"install_dir": str(tmp_path), "run_cmd": "python server.py", "project_type": "python"},
            input_schema={},
            output_format="json",
            created_by=user.id,
        )
        db.add(tool)
        db.commit()

        with patch("app.services.mcp_installer._install_dependencies", new_callable=AsyncMock, return_value=(True, "")), \
             patch("app.services.mcp_installer._wait_for_http", new_callable=AsyncMock, return_value=False), \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stderr = None
            mock_popen.return_value = mock_proc
            result = await install_and_start(db, tool)

        assert result["ok"] is False
        assert "超时" in result["error"]


# ── tool_executor MCP 路径 ────────────────────────────────────────────────────

class TestToolExecutorMcp:
    @pytest.mark.asyncio
    async def test_mcp_missing_url_raises(self, db):
        from app.services.tool_executor import ToolExecutor
        dept = _make_dept(db)
        user = _make_user(db, "texec_u1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        tool = ToolRegistry(
            name="exec_no_url",
            display_name="x",
            tool_type=ToolType.MCP,
            config={},  # 没有 url
            input_schema={},
            output_format="json",
            is_active=True,
            created_by=user.id,
        )
        db.add(tool)
        db.commit()

        executor = ToolExecutor()
        result = await executor.execute_tool(db, "exec_no_url", {}, user.id)
        assert result["ok"] is False
        assert "URL" in result["error"] or "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_mcp_calls_http_rpc(self, db):
        from app.services.tool_executor import ToolExecutor
        dept = _make_dept(db)
        user = _make_user(db, "texec_u2", Role.SUPER_ADMIN, dept.id)
        db.commit()
        tool = ToolRegistry(
            name="exec_with_url",
            display_name="x",
            tool_type=ToolType.MCP,
            config={"url": "http://localhost:9999"},
            input_schema={},
            output_format="json",
            is_active=True,
            created_by=user.id,
        )
        db.add(tool)
        db.commit()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"answer": 42}}
        mock_resp.raise_for_status = MagicMock()

        executor = ToolExecutor()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await executor.execute_tool(db, "exec_with_url", {"key": "val"}, user.id)

        assert result["ok"] is True
        assert result["result"]["answer"] == 42
        # 确认调用了 /rpc 端点
        call_url = mock_client.post.call_args[0][0]
        assert call_url.endswith("/rpc")
