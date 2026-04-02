"""Shared test fixtures for the Universal KB backend."""
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, LargeBinary, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models.user import User, Role, Department
from app.models.skill import ModelConfig, Skill, SkillStatus, SkillMode, SkillVersion
from app.services.auth_service import hash_password

# Use an in-memory SQLite for speed; override as needed
TEST_DB_URL = "sqlite:////tmp/test_universal_kb.db"

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
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_tables():
    """Truncate all tables between tests for isolation."""
    yield
    db = TestingSessionLocal()
    try:
        db.execute(text("PRAGMA foreign_keys = OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            try:
                db.execute(table.delete())
            except Exception:
                db.rollback()
        db.execute(text("PRAGMA foreign_keys = ON"))
        db.commit()
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
    from app.routers import auth, knowledge, knowledge_governance, user_workspace_config, mcp_server, admin

    test_app = FastAPI(title="Universal KB Test API")
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    test_app.include_router(auth.router)
    test_app.include_router(knowledge.router)
    test_app.include_router(knowledge_governance.router)
    test_app.include_router(user_workspace_config.router)
    test_app.include_router(mcp_server.router)
    test_app.include_router(admin.router)
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


def _make_skill(db, user_id, name="测试Skill", status=SkillStatus.PUBLISHED):
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


def _make_tool(db, user_id, name="test_tool", tool_type=ToolType.BUILTIN):
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
