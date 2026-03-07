"""Shared test fixtures for the Universal KB backend."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.user import User, Role, Department
from app.models.skill import ModelConfig, Skill, SkillStatus, SkillMode, SkillVersion
from app.services.auth_service import hash_password

# Use an in-memory SQLite for speed; override as needed
TEST_DB_URL = "sqlite:///./test_universal_kb.db"

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
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
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
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


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
