import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import _make_dept, _make_user, _make_skill, _make_model_config


class TestHarnessContracts:
    def test_workspace_type_contains_all_g1_values(self):
        from app.harness.contracts import WorkspaceType

        values = {item.value for item in WorkspaceType}
        assert {"chat", "opencode", "sandbox", "skill_studio", "project"} <= values

    def test_session_key_validation_for_skill_studio(self):
        from app.harness.contracts import AgentType, HarnessSessionKey

        with pytest.raises(ValueError):
            HarnessSessionKey(user_id=1, agent_type=AgentType.SKILL_STUDIO)

    def test_session_key_validation_for_dev_studio(self):
        from app.harness.contracts import AgentType, HarnessSessionKey

        with pytest.raises(ValueError):
            HarnessSessionKey(user_id=1, agent_type=AgentType.DEV_STUDIO)

    def test_request_has_backward_compatible_user_message_alias(self):
        from app.harness.contracts import AgentType, HarnessRequest, HarnessSessionKey

        req = HarnessRequest(
            session_key=HarnessSessionKey(user_id=1, agent_type=AgentType.PROJECT, project_id=9),
            agent_type=AgentType.PROJECT,
            user_id=1,
            input_text="hello",
        )
        assert req.user_message == "hello"


class TestSessionStoreAndGateway:
    @pytest.mark.asyncio
    async def test_gateway_creates_session_and_run(self, db):
        from app.harness.contracts import AgentType, HarnessRequest, HarnessSessionKey
        from app.harness.events import EventName, emit
        from app.harness.gateway import HarnessGateway
        from app.harness.session_store import SessionStore

        store = SessionStore()
        gateway = HarnessGateway(store=store)

        async def _executor(request, db_session, session_store):
            yield emit(EventName.DELTA, {"text": "ok"})

        from app.harness.gateway import register_executor
        register_executor(AgentType.PROJECT, _executor)

        req = HarnessRequest(
            session_key=HarnessSessionKey(user_id=1, agent_type=AgentType.PROJECT, project_id=7),
            agent_type=AgentType.PROJECT,
            user_id=1,
            input_text="run",
            stream=True,
        )

        events = []
        async for evt in gateway.dispatch(req, db):
            events.append(evt)

        run_id = events[0].data["run_id"]
        snapshot = store.get_run_snapshot(run_id)
        assert snapshot["session"] is not None
        assert snapshot["run"] is not None
        assert snapshot["run"].session_id == snapshot["session"].session_id

    @pytest.mark.asyncio
    async def test_tool_loop_denied_tool_is_not_executed(self, db):
        from app.harness.contracts import SecurityDecisionStatus
        from app.harness.security import SecurityDecision
        from app.harness.tool_loop import ToolLoop, ToolLoopContext

        class DenyPipeline:
            async def check_tool_call(self, **kwargs):
                return SecurityDecision(
                    status=SecurityDecisionStatus.DENY,
                    reason="blocked by test",
                    guard_name="test",
                )

        class FailingToolLoop(ToolLoop):
            async def _execute_tools_parallel(self, db, tool_calls, user_id):
                raise AssertionError("denied tool call should not execute")

        ctx = ToolLoopContext(
            db=db,
            llm_messages=[{"role": "user", "content": "run tool"}],
            model_config={},
            user_id=1,
            workspace_id=2,
            skill_id=3,
            initial_response='```tool_call\n{"tool":"dangerous","params":{}}\n```',
            security_pipeline=DenyPipeline(),
        )

        events = []
        async for item in FailingToolLoop().run(ctx):
            events.append(item)

        assert any(
            isinstance(item, dict)
            and item.get("data", {}).get("security_status") == "deny"
            for item in events
        )
        assert any(
            isinstance(item, dict)
            and item.get("data", {}).get("reason") == "security_blocked"
            for item in events
        )


class TestHarnessAdapters:
    def test_build_chat_request(self):
        from app.harness.adapters import build_chat_request

        req = build_chat_request(
            user_id=1,
            workspace_id=2,
            conversation_id=3,
            user_message="hi",
        )
        assert req.agent_type.value == "chat"
        assert req.input_text == "hi"
        assert req.context.workspace_id == 2
        assert req.context.conversation_id == 3


class TestHarnessRouterIntegration:
    @pytest.mark.asyncio
    async def test_conversation_stream_builds_harness_request(self, db):
        from app.models.conversation import Conversation, Message
        from app.models.workspace import Workspace, WorkspaceStatus
        from app.routers.conversations import SendMessage, stream_message

        dept = _make_dept(db, "harness_conv_dept")
        user = _make_user(db, "harness_conv_user", dept_id=dept.id)
        ws = Workspace(
            name="chat ws",
            status=WorkspaceStatus.PUBLISHED,
            created_by=user.id,
            workspace_type="chat",
        )
        db.add(ws)
        db.flush()
        conv = Conversation(user_id=user.id, workspace_id=ws.id, title="测试")
        db.add(conv)
        db.commit()

        captured = {}

        from app.harness.adapters import build_chat_request as _real_build_chat_request

        def _fake_build_chat_request(**kwargs):
            captured.update(kwargs)
            return _real_build_chat_request(**kwargs)

        req = SendMessage(content="hello harness")
        with patch("app.harness.adapters.build_chat_request", side_effect=_fake_build_chat_request):
            await stream_message(conv.id, req, MagicMock(), db, user)

        msg = db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.id.desc()).first()
        assert captured["conversation_id"] == conv.id
        assert msg.metadata_.get("_harness_request_id")

    @pytest.mark.asyncio
    async def test_project_generate_plan_builds_harness_request(self, db):
        from app.models.project import Project, ProjectStatus, ProjectMember
        from app.routers.projects import generate_plan

        dept = _make_dept(db, "harness_proj_dept")
        owner = _make_user(db, "harness_proj_owner", dept_id=dept.id)
        member = _make_user(db, "harness_proj_member", dept_id=dept.id)
        project = Project(
            name="P1",
            description="desc",
            owner_id=owner.id,
            department_id=dept.id,
            status=ProjectStatus.DRAFT,
        )
        db.add(project)
        db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=member.id, role_desc="成员"))
        db.commit()

        captured = {}

        from app.harness.adapters import build_project_request as _real_build_project_request

        def _fake_build_project_request(**kwargs):
            captured.update(kwargs)
            return _real_build_project_request(**kwargs)

        with patch("app.harness.adapters.build_project_request", side_effect=_fake_build_project_request), \
             patch("app.services.project_engine.project_engine.generate_plan", new=AsyncMock(return_value={"ok": True})):
            result = await generate_plan(project.id, db, owner)

        assert result["ok"] is True
        assert captured["project_id"] == project.id

    @pytest.mark.asyncio
    async def test_dev_studio_entry_builds_harness_request(self, db):
        from app.routers.dev_studio import dev_studio_entry_start

        dept = _make_dept(db, "harness_dev_dept")
        user = _make_user(db, "harness_dev_user", dept_id=dept.id)
        captured = {}

        from app.harness.adapters import build_dev_studio_request as _real_build_dev_request

        def _fake_build_dev_request(**kwargs):
            captured.update(kwargs)
            return _real_build_dev_request(**kwargs)

        entry = SimpleNamespace(
            registration_id=1,
            conversation_id=2,
            workspace_root="/tmp/ws",
            project_dir="/tmp/ws/project",
            runtime_status="stopped",
            runtime_port=None,
            generation=0,
            needs_recover=True,
            recent_conversation_ids=[],
            last_active_at=None,
            session_total=0,
            session_db_health="missing",
            session_db_source="missing",
            session_db_path=None,
            migration_state="none",
        )

        with patch("app.harness.adapters.build_dev_studio_request", side_effect=_fake_build_dev_request), \
             patch("app.services.studio_registry.resolve_entry", return_value=entry), \
             patch("app.routers.dev_studio._ensure_user_instance", new=AsyncMock(return_value={"port": 3000, "url": "http://localhost:3000"})):
            result = await dev_studio_entry_start(user, db)

        assert result["runtime_status"] == "running"
        assert captured["user_id"] == user.id

    @pytest.mark.asyncio
    async def test_sandbox_run_builds_harness_request(self, db):
        from app.models.sandbox import SandboxTestSession, SessionStatus, SessionStep
        from app.routers.sandbox_interactive import run_tests

        dept = _make_dept(db, "harness_sb_dept")
        user = _make_user(db, "harness_sb_user", dept_id=dept.id)
        session = SandboxTestSession(
            target_type="skill",
            target_id=99,
            target_version=1,
            target_name="skill99",
            tester_id=user.id,
            status=SessionStatus.DRAFT,
            current_step=SessionStep.PERMISSION_REVIEW,
        )
        db.add(session)
        db.commit()

        captured = {}

        from app.harness.adapters import build_sandbox_request as _real_build_sandbox_request

        def _fake_build_sandbox_request(**kwargs):
            captured.update(kwargs)
            return _real_build_sandbox_request(**kwargs)

        class StopRun(Exception):
            pass

        with patch("app.harness.adapters.build_sandbox_request", side_effect=_fake_build_sandbox_request), \
             patch("app.routers.sandbox_interactive._extract_previous_deductions", side_effect=StopRun()):
            with pytest.raises(StopRun):
                await run_tests(session.id, db, user)

        db.refresh(session)
        assert captured["session_id"] == session.id
        assert session.step_statuses.get("_harness_request_id")
