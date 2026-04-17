"""Shared test fixtures for the Universal KB backend."""
import os
import uuid
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import create_engine, text, LargeBinary, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.config import settings
from app.api_envelope import ApiEnvelopeException, api_envelope_exception_handler
from app.models.user import User, Role, Department
from app.models.skill import ModelConfig, Skill, SkillStatus, SkillMode, SkillVersion
import app.models.sandbox  # noqa: F401 — ensure sandbox tables exist in test DB
import app.models.knowledge_job  # noqa: F401 — ensure knowledge_jobs table exists in test DB
import app.models.skill_knowledge_ref  # noqa: F401 — ensure skill knowledge ref tables exist in test DB
import app.models.skill_governance  # noqa: F401 — ensure skill governance tables exist in test DB
import app.models.org_memory  # noqa: F401 — ensure org memory tables exist in test DB
from app.services.auth_service import hash_password

# Use an in-memory SQLite for speed; override as needed
TEST_DB_PATH = f"/tmp/test_universal_kb_{uuid.uuid4().hex}.db"
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH}"
TEST_UPLOAD_DIR = "/tmp/universal_kb_test_uploads"

# SQLite 不支持 MySQL 的 LONGBLOB，编译时映射为 BLOB
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.ext.compiler import compiles

@compiles(LONGBLOB, "sqlite")
def _compile_longblob_sqlite(type_, compiler, **kw):
    return "BLOB"

engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    os.makedirs(TEST_UPLOAD_DIR, exist_ok=True)
    settings.UPLOAD_DIR = TEST_UPLOAD_DIR
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        os.unlink(TEST_DB_PATH)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    if os.path.exists(TEST_DB_PATH):
        os.unlink(TEST_DB_PATH)


@pytest.fixture(autouse=True)
def clean_tables():
    """Truncate all tables between tests for isolation."""
    yield
    db = TestingSessionLocal()
    try:
        # SQLite: PRAGMA 必须在自动提交模式或独立事务中执行
        conn = db.connection()
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        for table in Base.metadata.sorted_tables:
            try:
                conn.execute(table.delete())
            except Exception:
                pass
        conn.execute(text("PRAGMA foreign_keys = ON"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@pytest.fixture
def db():
    database = TestingSessionLocal()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def client():
    from app.routers import (
        auth, knowledge, knowledge_governance, user_workspace_config,
        mcp_server, admin, conversations, data_assets,
        skills, business_tables, data_tables, audit, skill_suggestions,
        contributions, table_views, tools, files, intel, lark,
        web_apps, workspaces, skill_market, mcp_tokens, drafts,
        tasks, projects, permissions, skill_policies, approvals,
        handoff, output_schemas, sandbox, sandbox_interactive,
        skill_memos, collab, knowledge_admin, knowledge_tags,
        dev_studio, events, skill_governance, sandbox_case_plans,
        org_memory,
    )

    test_app = FastAPI(title="Universal KB Test API")
    test_app.add_exception_handler(ApiEnvelopeException, api_envelope_exception_handler)
    test_app.add_exception_handler(HTTPException, api_envelope_exception_handler)
    test_app.add_exception_handler(StarletteHTTPException, api_envelope_exception_handler)
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    test_app.include_router(auth.router)
    test_app.include_router(admin.router)
    test_app.include_router(skills.router)
    test_app.include_router(knowledge.router)
    test_app.include_router(knowledge_governance.router)
    test_app.include_router(data_assets.router)
    test_app.include_router(user_workspace_config.router)
    test_app.include_router(mcp_server.router)
    test_app.include_router(mcp_tokens.router)
    test_app.include_router(conversations.router)
    test_app.include_router(business_tables.router)
    test_app.include_router(table_views.router)
    test_app.include_router(data_tables.router)
    test_app.include_router(audit.router)
    test_app.include_router(skill_suggestions.router)
    test_app.include_router(contributions.router)
    test_app.include_router(tools.router)
    test_app.include_router(files.router)
    test_app.include_router(intel.router)
    test_app.include_router(lark.router)
    test_app.include_router(web_apps.router)
    test_app.include_router(workspaces.router)
    test_app.include_router(skill_market.router)
    test_app.include_router(drafts.router)
    test_app.include_router(tasks.router)
    test_app.include_router(projects.router)
    test_app.include_router(permissions.router)
    test_app.include_router(skill_policies.router)
    test_app.include_router(approvals.router)
    test_app.include_router(handoff.router)
    test_app.include_router(output_schemas.router)
    test_app.include_router(sandbox.router)
    test_app.include_router(sandbox_interactive.router)
    test_app.include_router(skill_memos.router)
    test_app.include_router(collab.router)
    test_app.include_router(knowledge_admin.router)
    test_app.include_router(knowledge_tags.router)
    test_app.include_router(dev_studio.router)
    test_app.include_router(events.router)
    test_app.include_router(org_memory.router)
    test_app.include_router(skill_governance.router)
    test_app.include_router(sandbox_case_plans.router)
    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app, raise_server_exceptions=True) as c:
        yield c
    test_app.dependency_overrides.clear()


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _make_dept(db, name="测试部门"):
    dept = Department(name=name, category="test", business_unit="test")
    db.add(dept)
    db.flush()
    return dept


def _make_user(db, username="testuser", role=Role.EMPLOYEE, dept_id=None, password="Test1234!"):
    u = User(
        username=username,
        password_hash=hash_password(password),
        display_name=username,
        role=role,
        department_id=dept_id,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_model_config(db):
    mc = ModelConfig(
        name="test-model",
        provider="openai",
        model_id="gpt-4o-mini",
        api_base="http://localhost:9999",
        api_key_env="TEST_API_KEY",
        max_tokens=1024,
        temperature="0.7",
        is_default=True,
    )
    db.add(mc)
    db.flush()
    return mc


def _make_skill(db, user_id, name=None, status=SkillStatus.PUBLISHED):
    if name is None:
        name = f"测试Skill_{uuid.uuid4().hex[:8]}"
    skill = Skill(
        name=name,
        description="用于测试",
        mode=SkillMode.HYBRID,
        status=status,
        knowledge_tags=["测试"],
        auto_inject=True,
        created_by=user_id,
        data_queries=[],
        tools=[],
    )
    db.add(skill)
    db.flush()
    v = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt="你是测试助手。",
        variables=[],
        created_by=user_id,
        change_note="初始版本",
    )
    db.add(v)
    db.flush()
    return skill


def _login(client, username="testuser", password="Test1234!"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


from app.models.intel import IntelSource, IntelSourceType, IntelEntry, IntelEntryStatus
from app.models.tool import ToolRegistry, ToolType
from app.models.web_app import WebApp
import secrets


def _make_intel_source(db, name="测试源", source_type=IntelSourceType.MANUAL):
    src = IntelSource(
        name=name,
        source_type=source_type,
        config={},
        is_active=True,
    )
    db.add(src)
    db.flush()
    return src


def _make_intel_entry(db, source_id=None, title="测试情报", status=IntelEntryStatus.PENDING):
    entry = IntelEntry(
        source_id=source_id,
        title=title,
        content="情报内容详情",
        url="https://example.com",
        tags=["测试"],
        industry="电商",
        platform="抖音",
        status=status,
        auto_collected=False,
    )
    db.add(entry)
    db.flush()
    return entry


def _make_tool(db, user_id, name=None, tool_type=ToolType.BUILTIN):
    if name is None:
        name = f"test_tool_{uuid.uuid4().hex[:8]}"
    tool = ToolRegistry(
        name=name,
        display_name=f"工具-{name}",
        description="测试工具",
        tool_type=tool_type,
        config={},
        input_schema={},
        output_format="json",
        created_by=user_id,
        is_active=True,
    )
    db.add(tool)
    db.flush()
    return tool


def _make_web_app(db, user_id, name="测试应用", is_public=False):
    web_app = WebApp(
        name=name,
        description="测试用",
        html_content="<html><body>Hello</body></html>",
        created_by=user_id,
        is_public=is_public,
        share_token=secrets.token_urlsafe(16),
    )
    db.add(web_app)
    db.flush()
    return web_app
