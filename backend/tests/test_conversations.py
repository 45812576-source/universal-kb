"""TC-CONV: Conversation CRUD and message flow (skill engine mocked)."""
import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.models.workspace import Workspace
from app.models.conversation import Conversation
from app.models.event_bus import UnifiedEvent


# ── Create conversation ───────────────────────────────────────────────────────

def test_create_conversation(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv1")

    resp = client.post("/api/conversations", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data


def test_create_conversation_requires_auth(client):
    resp = client.post("/api/conversations")
    assert resp.status_code in (401, 403)


# ── List conversations ────────────────────────────────────────────────────────

def test_list_conversations_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv2")

    resp = client.get("/api/conversations", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_conversations_only_own(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv3a", Role.EMPLOYEE, dept.id)
    _make_user(db, "conv3b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "conv3a")
    t2 = _login(client, "conv3b")

    client.post("/api/conversations", headers=_auth(t1))
    client.post("/api/conversations", headers=_auth(t2))

    resp1 = client.get("/api/conversations", headers=_auth(t1))
    resp2 = client.get("/api/conversations", headers=_auth(t2))

    assert len(resp1.json()) == 1
    assert len(resp2.json()) == 1
    # IDs should be different
    assert resp1.json()[0]["id"] != resp2.json()[0]["id"]


# ── Get messages ──────────────────────────────────────────────────────────────

def test_get_messages_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv4", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv4")

    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]

    resp = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_messages_other_user_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv5a", Role.EMPLOYEE, dept.id)
    _make_user(db, "conv5b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "conv5a")
    t2 = _login(client, "conv5b")

    r = client.post("/api/conversations", headers=_auth(t1))
    conv_id = r.json()["id"]

    resp = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(t2))
    assert resp.status_code == 404


def test_get_messages_nonexistent_conv(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv6", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv6")

    resp = client.get("/api/conversations/99999/messages", headers=_auth(token))
    assert resp.status_code == 404


def test_get_studio_state_returns_harness_snapshot(client, db):
    dept = _make_dept(db)
    user = _make_user(db, "conv_studio_state", Role.EMPLOYEE, dept.id)
    workspace = Workspace(
        name="Skill Studio",
        workspace_type="skill_studio",
        created_by=user.id,
    )
    db.add(workspace)
    db.flush()
    conv = Conversation(
        user_id=user.id,
        title="Studio 会话",
        workspace_id=workspace.id,
        skill_id=123,
    )
    db.add(conv)
    db.commit()

    from app.harness.contracts import AgentType, HarnessSessionKey
    from app.harness.gateway import get_session_store

    store = get_session_store()
    session_key = HarnessSessionKey(
        user_id=user.id,
        agent_type=AgentType.SKILL_STUDIO,
        workspace_id=workspace.id,
        target_type="skill",
        target_id=123,
    )
    session = store.create_or_get_session(session_key)
    session.metadata["studio_state"] = {
        "scenario": "optimize_existing_skill",
        "mode": "draft",
        "goal": "补齐提示词",
        "file_status": "forbidden",
        "readiness": 3,
        "has_draft": False,
        "total_rounds": 2,
        "reconciled_facts": [{"type": "constraint", "text": "不要改结构"}],
        "direction_shift": {"from": "unknown", "to": "audit_imported_skill"},
        "file_need_status": {"status": "forbidden", "forbidden_countdown": 1},
        "repeat_blocked": {"reason": "连续重复追问，已自动切换到 draft 模式"},
    }

    token = _login(client, "conv_studio_state")
    resp = client.get(f"/api/conversations/{conv.id}/studio-state?skill_id=123", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["conversation_id"] == conv.id
    assert data["skill_id"] == 123
    assert data["studio_state"]["goal"] == "补齐提示词"
    assert data["studio_state"]["file_need_status"]["forbidden_countdown"] == 1
    assert data["recovery"]["source"] == "memory"
    assert data["recovery"]["cold_start"] is False
    assert data["recovery"]["recovered_at"] is None


def test_get_studio_state_recovers_from_persisted_event(client, db):
    dept = _make_dept(db)
    user = _make_user(db, "conv_studio_state_event", Role.EMPLOYEE, dept.id)
    workspace = Workspace(
        name="Skill Studio",
        workspace_type="skill_studio",
        created_by=user.id,
    )
    db.add(workspace)
    db.flush()
    conv = Conversation(
        user_id=user.id,
        title="Studio 会话",
        workspace_id=workspace.id,
        skill_id=456,
    )
    db.add(conv)
    db.flush()
    db.add(UnifiedEvent(
        event_type="harness.studio.state_saved",
        source_type="harness",
        payload={
            "target_type": "skill",
            "target_id": 456,
            "studio_state": {
                "scenario": "audit_imported_skill",
                "mode": "draft",
                "goal": "事件表恢复",
                "file_status": "user_requested",
                "readiness": 2,
                "has_draft": True,
                "total_rounds": 4,
                "reconciled_facts": [{"type": "new_fact", "text": "从事件表恢复"}],
            },
            "run_id": "",
        },
        user_id=user.id,
        workspace_id=workspace.id,
    ))
    db.commit()

    token = _login(client, "conv_studio_state_event")
    resp = client.get(f"/api/conversations/{conv.id}/studio-state", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["skill_id"] == 456
    assert data["studio_state"]["goal"] == "事件表恢复"
    assert data["studio_state"]["reconciled_facts"][0]["text"] == "从事件表恢复"
    assert data["recovery"]["source"] == "persisted"
    assert data["recovery"]["cold_start"] is True
    assert isinstance(data["recovery"]["recovered_at"], str)


def test_session_store_recovers_studio_state_on_cold_start(db):
    dept = _make_dept(db)
    user = _make_user(db, "conv_studio_state_cold", Role.EMPLOYEE, dept.id)
    workspace = Workspace(
        name="Skill Studio",
        workspace_type="skill_studio",
        created_by=user.id,
    )
    db.add(workspace)
    db.flush()
    db.add(UnifiedEvent(
        event_type="harness.studio.state_saved",
        source_type="harness",
        payload={
            "target_type": "skill",
            "target_id": 789,
            "studio_state": {
                "scenario": "optimize_existing_skill",
                "mode": "draft",
                "goal": "冷启动恢复",
                "readiness": 4,
                "reconciled_facts": [{"type": "constraint", "text": "保留原有口径"}],
            },
            "run_id": "",
        },
        user_id=user.id,
        workspace_id=workspace.id,
    ))
    db.commit()

    from app.harness.contracts import AgentType, HarnessSessionKey
    from app.harness.session_store import SessionStore

    store = SessionStore()
    session_key = HarnessSessionKey(
        user_id=user.id,
        agent_type=AgentType.SKILL_STUDIO,
        workspace_id=workspace.id,
        target_type="skill",
        target_id=789,
    )
    session = store.create_or_get_session(session_key, agent_type=AgentType.SKILL_STUDIO, db=db)

    assert session.metadata["studio_state"]["goal"] == "冷启动恢复"
    assert session.metadata["studio_state"]["reconciled_facts"][0]["text"] == "保留原有口径"


# ── Send message ──────────────────────────────────────────────────────────────

def test_send_message_and_get_reply(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv7", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv7")

    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]

    with patch(
        "app.services.skill_engine.SkillEngine.execute",
        new=AsyncMock(return_value=("模拟回复", {})),
    ):
        resp = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=_auth(token),
            json={"content": "你好，请介绍一下抖音投放策略"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "assistant"
    assert data["content"] == "模拟回复"


def test_send_message_persisted_in_history(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv8", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv8")

    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]

    with patch(
        "app.services.skill_engine.SkillEngine.execute",
        new=AsyncMock(return_value=("回复内容", {})),
    ):
        client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=_auth(token),
            json={"content": "用户消息"},
        )

    msgs = client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token)).json()
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "assistant" in roles


def test_send_message_updates_conversation_title(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv9", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv9")

    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]

    with patch(
        "app.services.skill_engine.SkillEngine.execute",
        new=AsyncMock(return_value=("OK", {})),
    ):
        client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=_auth(token),
            json={"content": "这是对话标题消息"},
        )

    convs = client.get("/api/conversations", headers=_auth(token)).json()
    conv = next(c for c in convs if c["id"] == conv_id)
    assert "这是对话标题消息" in conv["title"]


def test_send_message_to_nonexistent_conv(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv10", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv10")

    with patch(
        "app.services.skill_engine.SkillEngine.execute",
        new=AsyncMock(return_value=("OK", {})),
    ):
        resp = client.post(
            "/api/conversations/99999/messages",
            headers=_auth(token),
            json={"content": "测试"},
        )
    assert resp.status_code == 404


def test_send_message_to_other_users_conv(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv11a", Role.EMPLOYEE, dept.id)
    _make_user(db, "conv11b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "conv11a")
    t2 = _login(client, "conv11b")

    r = client.post("/api/conversations", headers=_auth(t1))
    conv_id = r.json()["id"]

    with patch(
        "app.services.skill_engine.SkillEngine.execute",
        new=AsyncMock(return_value=("OK", {})),
    ):
        resp = client.post(
            f"/api/conversations/{conv_id}/messages",
            headers=_auth(t2),
            json={"content": "入侵"},
        )
    assert resp.status_code == 404


# ── Delete conversation ───────────────────────────────────────────────────────

def test_delete_conversation(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv12", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "conv12")

    r = client.post("/api/conversations", headers=_auth(token))
    conv_id = r.json()["id"]

    resp = client.delete(f"/api/conversations/{conv_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    convs = client.get("/api/conversations", headers=_auth(token)).json()
    assert not any(c["id"] == conv_id for c in convs)


def test_delete_other_users_conversation(client, db):
    dept = _make_dept(db)
    _make_user(db, "conv13a", Role.EMPLOYEE, dept.id)
    _make_user(db, "conv13b", Role.EMPLOYEE, dept.id)
    db.commit()

    t1 = _login(client, "conv13a")
    t2 = _login(client, "conv13b")

    r = client.post("/api/conversations", headers=_auth(t1))
    conv_id = r.json()["id"]

    resp = client.delete(f"/api/conversations/{conv_id}", headers=_auth(t2))
    assert resp.status_code == 404
