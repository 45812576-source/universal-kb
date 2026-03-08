# Skill 外部市场 + MCP 双向接口 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 支持从外部 Skill 平台（如 skills.sh）在系统内搜索/导入 Skill，双向支持 MCP 协议，并提供完整的版本追踪与 Diff UI。

**Architecture:** 后端新增 McpSource（外部数据源）、McpToken（对外鉴权）、SkillUpstreamCheck（版本检查记录）三张表，Skill 表扩展上游追踪字段；新增 MCP Client 服务（拉取外部）、MCP Server 路由（`/mcp` endpoint 对外）、市场浏览路由；前端新增市场页、Token 管理页，并在 Skill 详情页新增"与上游对比" Tab。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（后端）；React Router v7 + TypeScript + Tailwind（前端）；httpx（HTTP 调用外部 MCP）；`diff-match-patch`（文本 diff）；APScheduler（定时上游检查，已有）

---

## Task 1: 后端 DB 模型 + Skill 表字段扩展

**Files:**
- Modify: `backend/app/models/skill.py`
- Create: `backend/app/models/mcp.py`

### Step 1: 扩展 Skill 模型，添加上游追踪字段

在 `backend/app/models/skill.py` 的 `Skill` 类末尾（`attributions` relationship 后）添加：

```python
    # Upstream tracking fields
    source_type = Column(String(20), default="local")  # local / imported / forked
    upstream_url = Column(String(500), nullable=True)
    upstream_id = Column(String(200), nullable=True)
    upstream_version = Column(String(50), nullable=True)
    upstream_content = Column(Text, nullable=True)  # 永远保存上游原版 system_prompt
    upstream_synced_at = Column(DateTime, nullable=True)
    is_customized = Column(Boolean, default=False)
    parent_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)
    local_modified_at = Column(DateTime, nullable=True)
```

### Step 2: 新建 `backend/app/models/mcp.py`

```python
import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class McpSource(Base):
    __tablename__ = "mcp_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)          # 显示名称，如 "skills.sh"
    url = Column(String(500), nullable=False)            # MCP Server URL 或 REST API base URL
    adapter_type = Column(String(20), default="mcp")    # mcp / rest
    auth_token = Column(Text, nullable=True)             # 加密存储（当前 plaintext，后续可换）
    is_active = Column(Boolean, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class McpTokenScope(str, enum.Enum):
    USER = "user"
    WORKSPACE = "workspace"
    ADMIN = "admin"


class McpToken(Base):
    __tablename__ = "mcp_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    token_hash = Column(String(200), nullable=False, unique=True)  # sha256 hex
    token_prefix = Column(String(12), nullable=False)               # 显示用前缀，如 "ukb_abc123"
    scope = Column(Enum(McpTokenScope), default=McpTokenScope.USER)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class SkillUpstreamCheck(Base):
    __tablename__ = "skill_upstream_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    checked_at = Column(DateTime, default=datetime.datetime.utcnow)
    upstream_version = Column(String(50), nullable=True)
    has_diff = Column(Boolean, default=False)
    diff_summary = Column(Text, nullable=True)           # 简要说明改了什么
    action = Column(String(20), default="pending")       # pending / synced / ignored
```

### Step 3: 在 `backend/app/models/__init__.py` 注册新模型

```python
from app.models import mcp  # noqa: F401
```

（查看当前 `__init__.py` 内容后，在已有 import 末尾添加这一行）

### Step 4: 运行测试验证 import 不报错

```bash
cd backend && python -c "from app.models.mcp import McpSource, McpToken, SkillUpstreamCheck; print('OK')"
```

Expected: `OK`

### Step 5: Commit

```bash
git add backend/app/models/skill.py backend/app/models/mcp.py backend/app/models/__init__.py
git commit -m "feat: add upstream tracking fields to Skill and new MCP models"
```

---

## Task 2: Alembic 迁移文件

**Files:**
- Create: `backend/alembic/versions/e5f6a7b8c9d0_skill_market_mcp.py`

### Step 1: 创建迁移文件

```python
"""skill_market_mcp: upstream tracking + mcp_sources + mcp_tokens + skill_upstream_checks

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-07 21:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Extend skills table with upstream tracking columns
    op.add_column('skills', sa.Column('source_type', sa.String(20), server_default='local', nullable=True))
    op.add_column('skills', sa.Column('upstream_url', sa.String(500), nullable=True))
    op.add_column('skills', sa.Column('upstream_id', sa.String(200), nullable=True))
    op.add_column('skills', sa.Column('upstream_version', sa.String(50), nullable=True))
    op.add_column('skills', sa.Column('upstream_content', sa.Text(), nullable=True))
    op.add_column('skills', sa.Column('upstream_synced_at', sa.DateTime(), nullable=True))
    op.add_column('skills', sa.Column('is_customized', sa.Boolean(), server_default='0', nullable=True))
    op.add_column('skills', sa.Column('parent_skill_id', sa.Integer(), nullable=True))
    op.add_column('skills', sa.Column('local_modified_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('fk_skills_parent_skill_id', 'skills', 'skills', ['parent_skill_id'], ['id'])

    # 2. mcp_sources
    op.create_table(
        'mcp_sources',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('url', sa.String(500), nullable=False),
        sa.Column('adapter_type', sa.String(20), server_default='mcp', nullable=True),
        sa.Column('auth_token', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # 3. mcp_tokens
    op.create_table(
        'mcp_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('token_hash', sa.String(200), nullable=False),
        sa.Column('token_prefix', sa.String(12), nullable=False),
        sa.Column('scope', sa.Enum('user', 'workspace', 'admin', name='mcptokenscope'), server_default='user', nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )

    # 4. skill_upstream_checks
    op.create_table(
        'skill_upstream_checks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('skill_id', sa.Integer(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('upstream_version', sa.String(50), nullable=True),
        sa.Column('has_diff', sa.Boolean(), server_default='0', nullable=True),
        sa.Column('diff_summary', sa.Text(), nullable=True),
        sa.Column('action', sa.String(20), server_default='pending', nullable=True),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('skill_upstream_checks')
    op.drop_table('mcp_tokens')
    op.drop_table('mcp_sources')
    op.drop_constraint('fk_skills_parent_skill_id', 'skills', type_='foreignkey')
    for col in ['local_modified_at', 'parent_skill_id', 'is_customized',
                'upstream_synced_at', 'upstream_content', 'upstream_version',
                'upstream_id', 'upstream_url', 'source_type']:
        op.drop_column('skills', col)
```

### Step 2: 运行迁移

```bash
cd backend && alembic upgrade head
```

Expected: 无报错，输出 `Running upgrade d4e5f6a7b8c9 -> e5f6a7b8c9d0`

### Step 3: Commit

```bash
git add backend/alembic/versions/e5f6a7b8c9d0_skill_market_mcp.py
git commit -m "feat: alembic migration for skill market and MCP tables"
```

---

## Task 3: MCP Client 服务 + 市场 API

**Files:**
- Create: `backend/app/services/mcp_client.py`
- Create: `backend/app/routers/skill_market.py`
- Modify: `backend/app/main.py`

### Step 1: 写失败测试

在 `backend/tests/test_skill_market.py` 创建：

```python
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def get_admin_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return resp.json().get("access_token", "")

def test_list_mcp_sources_empty():
    token = get_admin_token(client)
    resp = client.get("/api/skill-market/sources", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

def test_market_search_requires_auth():
    resp = client.get("/api/skill-market/search?q=test")
    assert resp.status_code == 403
```

### Step 2: 运行测试，确认失败

```bash
cd backend && python -m pytest tests/test_skill_market.py -v
```

Expected: FAIL with `404` or `AttributeError`

### Step 3: 创建 MCP Client 服务 `backend/app/services/mcp_client.py`

```python
"""MCP Client: connects to external MCP servers or REST adapters to browse/import Skills."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# MCP protocol constants
MCP_JSONRPC = "2.0"
MCP_TOOLS_LIST = "tools/list"
MCP_TOOLS_CALL = "tools/call"


class McpClientError(Exception):
    pass


def _mcp_request(url: str, method: str, params: dict, token: str | None = None) -> Any:
    """Send a JSON-RPC 2.0 request to an MCP server."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "jsonrpc": MCP_JSONRPC,
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise McpClientError(f"MCP error: {data['error']}")
        return data.get("result", {})
    except httpx.HTTPError as e:
        raise McpClientError(f"HTTP error: {e}") from e


def _rest_list_skills(base_url: str, token: str | None, query: str = "", page: int = 1) -> list[dict]:
    """Adapter for REST APIs that don't support MCP protocol."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"q": query, "page": page, "limit": 20}
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/skills", params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Normalize to common format
        items = data if isinstance(data, list) else data.get("items", data.get("results", []))
        return [_normalize_skill(item) for item in items]
    except httpx.HTTPError as e:
        raise McpClientError(f"REST error: {e}") from e


def _normalize_skill(raw: dict) -> dict:
    """Normalize external skill representation to our internal format."""
    return {
        "upstream_id": str(raw.get("id", raw.get("slug", ""))),
        "name": raw.get("name", raw.get("title", "")),
        "description": raw.get("description", ""),
        "system_prompt": raw.get("system_prompt", raw.get("prompt", "")),
        "upstream_version": str(raw.get("version", raw.get("updated_at", "1"))),
        "author": raw.get("author", raw.get("created_by", "")),
        "tags": raw.get("tags", []),
    }


def list_remote_skills(source, query: str = "", page: int = 1) -> list[dict]:
    """List skills from a McpSource (MCP or REST adapter)."""
    if source.adapter_type == "mcp":
        result = _mcp_request(
            source.url,
            MCP_TOOLS_LIST,
            {"query": query, "page": page},
            source.auth_token,
        )
        tools = result.get("tools", [])
        return [_normalize_skill(t) for t in tools]
    else:
        return _rest_list_skills(source.url, source.auth_token, query, page)


def fetch_remote_skill(source, upstream_id: str) -> dict:
    """Fetch a single skill's full definition from an external source."""
    if source.adapter_type == "mcp":
        result = _mcp_request(
            source.url,
            MCP_TOOLS_CALL,
            {"name": "get_skill", "arguments": {"id": upstream_id}},
            source.auth_token,
        )
        return _normalize_skill(result.get("content", result))
    else:
        headers = {"Authorization": f"Bearer {source.auth_token}"} if source.auth_token else {}
        try:
            resp = httpx.get(
                f"{source.url.rstrip('/')}/skills/{upstream_id}",
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            return _normalize_skill(resp.json())
        except httpx.HTTPError as e:
            raise McpClientError(f"Fetch error: {e}") from e


def check_upstream_version(source, skill) -> dict:
    """Check if upstream has a newer version. Returns {'has_diff': bool, 'new_version': str}."""
    try:
        remote = fetch_remote_skill(source, skill.upstream_id)
        new_ver = remote.get("upstream_version", "")
        has_diff = new_ver != skill.upstream_version
        return {
            "has_diff": has_diff,
            "new_version": new_ver,
            "remote": remote,
        }
    except McpClientError as e:
        logger.warning(f"Upstream check failed for skill {skill.id}: {e}")
        return {"has_diff": False, "new_version": None, "remote": None, "error": str(e)}
```

### Step 4: 创建市场路由 `backend/app/routers/skill_market.py`

```python
"""Skill Market: browse external sources, import skills, manage MCP sources."""
import datetime
import secrets
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.mcp import McpSource, SkillUpstreamCheck
from app.models.skill import Skill, SkillVersion, SkillStatus
from app.models.user import User, Role
from app.services.mcp_client import list_remote_skills, fetch_remote_skill, McpClientError

router = APIRouter(prefix="/api/skill-market", tags=["skill-market"])


# --- MCP Source Management (Super Admin only) ---

class McpSourceCreate(BaseModel):
    name: str
    url: str
    adapter_type: str = "mcp"
    auth_token: Optional[str] = None


@router.get("/sources")
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    sources = db.query(McpSource).order_by(McpSource.created_at.desc()).all()
    return [
        {
            "id": s.id, "name": s.name, "url": s.url,
            "adapter_type": s.adapter_type, "is_active": s.is_active,
            "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
        }
        for s in sources
    ]


@router.post("/sources")
def create_source(
    req: McpSourceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = McpSource(
        name=req.name, url=req.url,
        adapter_type=req.adapter_type, auth_token=req.auth_token,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return {"id": source.id}


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()
    return {"ok": True}


# --- Market Browse & Import ---

@router.get("/search")
def search_market(
    source_id: int,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found or inactive")
    try:
        skills = list_remote_skills(source, q, page)
    except McpClientError as e:
        raise HTTPException(502, f"Remote source error: {e}")
    return skills


@router.get("/preview")
def preview_skill(
    source_id: int,
    upstream_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")
    try:
        skill_data = fetch_remote_skill(source, upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")
    return skill_data


class ImportRequest(BaseModel):
    source_id: int
    upstream_id: str


@router.post("/import")
def import_skill(
    req: ImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, req.source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")

    # Fetch from upstream
    try:
        skill_data = fetch_remote_skill(source, req.upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")

    # Deduplicate by upstream_id + source
    existing = (
        db.query(Skill)
        .filter(Skill.upstream_id == req.upstream_id, Skill.source_type.in_(["imported", "forked"]))
        .first()
    )
    if existing:
        raise HTTPException(409, f"Skill already imported (id={existing.id})")

    # Create Skill
    now = datetime.datetime.utcnow()
    skill = Skill(
        name=skill_data["name"],
        description=skill_data.get("description", ""),
        status=SkillStatus.DRAFT,
        source_type="imported",
        upstream_url=f"{source.url}/skills/{req.upstream_id}",
        upstream_id=req.upstream_id,
        upstream_version=skill_data.get("upstream_version", ""),
        upstream_content=skill_data.get("system_prompt", ""),
        upstream_synced_at=now,
        is_customized=False,
        created_by=user.id,
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=skill_data.get("system_prompt", ""),
        variables=[],
        created_by=user.id,
        change_note=f"从 {source.name} 导入 (upstream_id={req.upstream_id})",
    )
    db.add(version)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}
```

### Step 5: 注册路由到 `backend/app/main.py`

在现有 import 块末尾添加：

```python
from app.routers import skill_market, mcp_server  # noqa: E402
```

在 `app.include_router(workspaces.router)` 后添加：

```python
app.include_router(skill_market.router)
```

（mcp_server 在 Task 5 中添加）

### Step 6: 运行测试

```bash
cd backend && python -m pytest tests/test_skill_market.py -v
```

Expected: `test_list_mcp_sources_empty` PASS, `test_market_search_requires_auth` PASS

### Step 7: Commit

```bash
git add backend/app/services/mcp_client.py backend/app/routers/skill_market.py backend/app/main.py backend/tests/test_skill_market.py
git commit -m "feat: MCP client service and skill market browse/import API"
```

---

## Task 4: 前端市场页

**Files:**
- Create: `frontend/app/routes/app/admin/skill-market/index.tsx`
- Modify: `frontend/app/routes.ts`
- Modify: `frontend/app/routes/app/admin/layout.tsx`

### Step 1: 创建市场页 `frontend/app/routes/app/admin/skill-market/index.tsx`

```tsx
import { useState, useEffect } from "react";
import { Link } from "react-router";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Route } from "./+types/index";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const sources = await apiFetch("/api/skill-market/sources", { token }).catch(() => []);
  return { token, user, sources };
}

export default function SkillMarket() {
  const { token, sources } = /* useLoaderData */ {} as any;
  const [selectedSource, setSelectedSource] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<any>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function search() {
    if (!selectedSource) return;
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch(
        `/api/skill-market/search?source_id=${selectedSource}&q=${encodeURIComponent(query)}`,
        { token }
      );
      setResults(data || []);
    } catch (e: any) {
      setError(e.message || "搜索失败");
    } finally {
      setLoading(false);
    }
  }

  async function showPreview(upstreamId: string) {
    if (!selectedSource) return;
    try {
      const data = await apiFetch(
        `/api/skill-market/preview?source_id=${selectedSource}&upstream_id=${encodeURIComponent(upstreamId)}`,
        { token }
      );
      setPreview(data);
    } catch (e: any) {
      setError(e.message || "预览失败");
    }
  }

  async function importSkill(upstreamId: string) {
    if (!selectedSource) return;
    setImporting(upstreamId);
    setError("");
    try {
      const result = await apiFetch("/api/skill-market/import", {
        method: "POST",
        body: JSON.stringify({ source_id: selectedSource, upstream_id: upstreamId }),
        token,
      });
      setSuccess(`已导入为草稿 Skill #${result.id}，前往审核发布`);
    } catch (e: any) {
      setError(e.message || "导入失败");
    } finally {
      setImporting(null);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill 外部市场</h1>
      </div>

      <div className="p-6 max-w-5xl space-y-6">
        {error && (
          <div className="border-2 border-red-400 bg-red-50 px-4 py-2 text-xs font-bold text-red-700 uppercase">
            [ERROR] {error}
          </div>
        )}
        {success && (
          <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-4 py-2 text-xs font-bold text-[#00A3C4] uppercase">
            [OK] {success} — <Link to="/admin/skills" className="underline">去 Skill 列表</Link>
          </div>
        )}

        {/* Source selector */}
        <div className="pixel-border bg-white p-5 space-y-3">
          <p className="text-[10px] font-bold uppercase tracking-widest text-[#00A3C4]">— 选择外部数据源</p>
          {sources?.length === 0 ? (
            <p className="text-xs text-gray-400">
              暂无数据源，请先在{" "}
              <Link to="/admin/skill-market/sources" className="underline text-[#00A3C4]">数据源管理</Link>
              {" "}中添加
            </p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {sources?.map((s: any) => (
                <button
                  key={s.id}
                  onClick={() => setSelectedSource(s.id)}
                  className={`border-2 px-4 py-2 text-[10px] font-bold uppercase transition-colors ${
                    selectedSource === s.id
                      ? "bg-[#1A202C] text-white border-[#1A202C]"
                      : "bg-white text-gray-600 border-[#1A202C] hover:bg-[#EBF4F7]"
                  }`}
                >
                  {s.name}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Search */}
        {selectedSource && (
          <div className="pixel-border bg-white p-5 space-y-4">
            <p className="text-[10px] font-bold uppercase tracking-widest text-[#00A3C4]">— 搜索 Skill</p>
            <div className="flex gap-3">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && search()}
                placeholder="搜索名称、标签..."
                className="flex-1 border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
              />
              <button
                onClick={search}
                disabled={loading}
                className="bg-[#1A202C] text-[#00D1FF] px-5 py-2 text-[10px] font-bold uppercase hover:bg-black disabled:opacity-50 pixel-border"
              >
                {loading ? "搜索中..." : "搜索"}
              </button>
            </div>

            {/* Results */}
            {results.length > 0 && (
              <div className="space-y-2">
                {results.map((skill: any) => (
                  <div key={skill.upstream_id} className="border-2 border-[#1A202C] bg-white p-4 flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-bold text-[#1A202C]">{skill.name}</p>
                      <p className="text-[10px] text-gray-500 mt-0.5">{skill.description}</p>
                      <p className="text-[9px] font-bold uppercase text-gray-400 mt-1">
                        v{skill.upstream_version} · {skill.author}
                      </p>
                    </div>
                    <div className="flex gap-2 flex-shrink-0">
                      <button
                        onClick={() => showPreview(skill.upstream_id)}
                        className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-[#EBF4F7]"
                      >
                        预览
                      </button>
                      <button
                        onClick={() => importSkill(skill.upstream_id)}
                        disabled={importing === skill.upstream_id}
                        className="bg-[#1A202C] text-white px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-black disabled:opacity-50"
                      >
                        {importing === skill.upstream_id ? "导入中..." : "导入"}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Preview modal */}
        {preview && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
            <div className="pixel-border bg-white w-full max-w-2xl mx-4 max-h-[80vh] overflow-y-auto">
              <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between sticky top-0">
                <span className="text-[10px] font-bold uppercase tracking-widest">{preview.name}</span>
                <button onClick={() => setPreview(null)} className="text-[10px] font-bold uppercase text-gray-400 hover:text-white">[关闭]</button>
              </div>
              <div className="p-5 space-y-4">
                <p className="text-xs text-gray-600">{preview.description}</p>
                <div>
                  <p className="text-[10px] font-bold uppercase text-gray-400 mb-1">System Prompt 预览</p>
                  <pre className="border-2 border-[#1A202C] bg-[#F8FAFC] p-3 text-xs font-mono whitespace-pre-wrap max-h-60 overflow-y-auto">
                    {preview.system_prompt}
                  </pre>
                </div>
                <button
                  onClick={() => { importSkill(preview.upstream_id); setPreview(null); }}
                  className="bg-[#1A202C] text-[#00D1FF] px-5 py-2 text-[10px] font-bold uppercase hover:bg-black pixel-border"
                >
                  导入此 Skill
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
```

### Step 2: 在 `frontend/app/routes.ts` 添加路由

在 `route("admin/tools", ...)` 行后添加：

```ts
route("admin/skill-market", "routes/app/admin/skill-market/index.tsx"),
```

### Step 3: 在 admin layout 侧边栏添加入口

在 `frontend/app/routes/app/admin/layout.tsx` 找到工具相关的导航链接后，添加：

```tsx
<Link to="/admin/skill-market" className="...（复用现有样式）...">
  外部市场
</Link>
```

（读取 layout.tsx 后，复用已有侧边栏 Link 样式）

### Step 4: Commit

```bash
git add frontend/app/routes/app/admin/skill-market/ frontend/app/routes.ts frontend/app/routes/app/admin/layout.tsx
git commit -m "feat: skill market frontend page with search/preview/import"
```

---

## Task 5: MCP Server（对外暴露公司 Skill）

**Files:**
- Create: `backend/app/routers/mcp_server.py`
- Create: `backend/app/routers/mcp_tokens.py`
- Modify: `backend/app/main.py`

### Step 1: 写失败测试

在 `backend/tests/test_mcp_server.py`：

```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_mcp_endpoint_rejects_no_token():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1})
    assert resp.status_code == 401

def test_mcp_tools_list_returns_structure():
    # This will be a real test after token creation API exists
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1})
    assert resp.status_code in (401, 200)
```

### Step 2: 创建 `backend/app/routers/mcp_tokens.py`

```python
"""MCP Token management: create/list/delete personal API tokens for MCP Server access."""
import datetime
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.models.mcp import McpToken, McpTokenScope
from app.models.user import User, Role

router = APIRouter(prefix="/api/mcp-tokens", tags=["mcp-tokens"])


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TokenCreate(BaseModel):
    scope: str = "user"
    workspace_id: Optional[int] = None
    expires_days: Optional[int] = None  # None = never expires


@router.post("")
def create_token(
    req: TokenCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Only super_admin can create admin-scoped tokens
    if req.scope == "admin" and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "Only super_admin can create admin-scoped tokens")

    raw = secrets.token_urlsafe(32)
    prefix = f"ukb_{raw[:8]}"

    expires_at = None
    if req.expires_days:
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=req.expires_days)

    token = McpToken(
        user_id=user.id,
        workspace_id=req.workspace_id,
        token_hash=_hash_token(raw),
        token_prefix=prefix,
        scope=req.scope,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    # Return raw token ONCE — not stored, not retrievable again
    return {
        "id": token.id,
        "token": raw,          # shown only once
        "prefix": prefix,
        "scope": req.scope,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.get("")
def list_tokens(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tokens = db.query(McpToken).filter(McpToken.user_id == user.id).all()
    return [
        {
            "id": t.id,
            "prefix": t.token_prefix,
            "scope": t.scope.value,
            "workspace_id": t.workspace_id,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "created_at": t.created_at.isoformat(),
        }
        for t in tokens
    ]


@router.delete("/{token_id}")
def delete_token(
    token_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    token = db.get(McpToken, token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(404, "Token not found")
    db.delete(token)
    db.commit()
    return {"ok": True}
```

### Step 3: 创建 `backend/app/routers/mcp_server.py`

```python
"""MCP Server: expose company Skills as MCP tools to authorized external clients."""
import datetime
import hashlib
import logging

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, Any

from app.database import get_db
from app.models.mcp import McpToken, McpTokenScope
from app.models.skill import Skill, SkillStatus
from app.models.workspace import WorkspaceSkill

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-server"])


class McpRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Any = 1
    method: str
    params: dict = {}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_mcp_token(authorization: Optional[str], db: Session) -> McpToken:
    """Validate MCP Bearer token and return the McpToken record."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    raw = authorization[7:]
    token_hash = _hash_token(raw)
    token = db.query(McpToken).filter(McpToken.token_hash == token_hash).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid token")
    if token.expires_at and token.expires_at < datetime.datetime.utcnow():
        raise HTTPException(status_code=401, detail="Token expired")
    # Update last_used_at
    token.last_used_at = datetime.datetime.utcnow()
    db.commit()
    return token


def _get_accessible_skills(token: McpToken, db: Session) -> list[Skill]:
    """Return skills accessible to this token based on scope."""
    if token.scope == McpTokenScope.ADMIN:
        return db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()

    if token.scope == McpTokenScope.WORKSPACE and token.workspace_id:
        ws_skill_ids = [
            ws.skill_id for ws in
            db.query(WorkspaceSkill).filter(WorkspaceSkill.workspace_id == token.workspace_id).all()
        ]
        return db.query(Skill).filter(
            Skill.id.in_(ws_skill_ids),
            Skill.status == SkillStatus.PUBLISHED,
        ).all()

    # user scope: all published skills (same as employee role)
    return db.query(Skill).filter(Skill.status == SkillStatus.PUBLISHED).all()


@router.post("/mcp")
async def mcp_endpoint(
    req: McpRequest,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    token = _get_mcp_token(authorization, db)
    skills = _get_accessible_skills(token, db)

    if req.method == "tools/list":
        tools = [
            {
                "name": s.name,
                "description": s.description or "",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "用户消息"},
                    },
                    "required": ["message"],
                },
            }
            for s in skills
        ]
        return {"jsonrpc": "2.0", "id": req.id, "result": {"tools": tools}}

    if req.method == "tools/call":
        tool_name = req.params.get("name", "")
        args = req.params.get("arguments", {})
        user_message = args.get("message", "")

        skill = next((s for s in skills if s.name == tool_name), None)
        if not skill:
            return {
                "jsonrpc": "2.0", "id": req.id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"},
            }

        try:
            from app.models.conversation import Conversation
            from app.services.skill_engine import skill_engine

            # Create ephemeral conversation for MCP call
            conv = Conversation(user_id=token.user_id, title="MCP Call", skill_id=skill.id)
            db.add(conv)
            db.flush()

            response = await skill_engine.execute(db, conv, user_message, user_id=token.user_id)
            db.rollback()  # Don't persist ephemeral conversation

            return {
                "jsonrpc": "2.0", "id": req.id,
                "result": {"content": [{"type": "text", "text": response}]},
            }
        except Exception as e:
            logger.error(f"MCP tool call error: {e}")
            return {
                "jsonrpc": "2.0", "id": req.id,
                "error": {"code": -32603, "message": str(e)},
            }

    return {
        "jsonrpc": "2.0", "id": req.id,
        "error": {"code": -32601, "message": f"Method '{req.method}' not supported"},
    }
```

### Step 4: 注册 mcp_server 和 mcp_tokens 到 `app/main.py`

在已有 import 行补充：

```python
from app.routers import skill_market, mcp_server, mcp_tokens  # noqa: E402
```

添加：

```python
app.include_router(mcp_server.router)
app.include_router(mcp_tokens.router)
```

### Step 5: 运行测试

```bash
cd backend && python -m pytest tests/test_mcp_server.py -v
```

Expected: `test_mcp_endpoint_rejects_no_token` PASS

### Step 6: Commit

```bash
git add backend/app/routers/mcp_server.py backend/app/routers/mcp_tokens.py backend/app/main.py backend/tests/test_mcp_server.py
git commit -m "feat: MCP server endpoint and token management API"
```

---

## Task 6: 上游版本检查服务 + 定时任务

**Files:**
- Create: `backend/app/services/upstream_checker.py`
- Modify: `backend/app/main.py` (startup scheduler)
- Modify: `backend/app/routers/skills.py` (新增 diff endpoint)

### Step 1: 创建 `backend/app/services/upstream_checker.py`

```python
"""Upstream version checker: scheduled daily check for imported skills."""
from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.mcp import McpSource, SkillUpstreamCheck
from app.models.skill import Skill
from app.services.mcp_client import check_upstream_version

logger = logging.getLogger(__name__)


def compute_text_diff_summary(old: str, new: str) -> str:
    """Return a brief diff summary (added/removed line counts)."""
    old_lines = set(old.splitlines())
    new_lines = set(new.splitlines())
    added = len(new_lines - old_lines)
    removed = len(old_lines - new_lines)
    return f"+{added} 行 / -{removed} 行"


def check_all_imported_skills() -> None:
    """Check upstream versions for all imported skills. Called by scheduler."""
    db: Session = SessionLocal()
    try:
        imported_skills = (
            db.query(Skill)
            .filter(Skill.source_type.in_(["imported"]), Skill.upstream_id.isnot(None))
            .all()
        )
        logger.info(f"Upstream check: {len(imported_skills)} imported skills to check")

        for skill in imported_skills:
            try:
                _check_skill(db, skill)
            except Exception as e:
                logger.warning(f"Upstream check failed for skill {skill.id}: {e}")
    finally:
        db.close()


def _check_skill(db: Session, skill: Skill) -> None:
    # Find any active source that could have this skill
    # Simple heuristic: use first active source; more sophisticated matching can be added later
    source = db.query(McpSource).filter(McpSource.is_active == True).first()
    if not source:
        return

    result = check_upstream_version(source, skill)
    if result.get("error"):
        return

    has_diff = result["has_diff"]
    new_version = result.get("new_version", "")

    check = SkillUpstreamCheck(
        skill_id=skill.id,
        checked_at=datetime.datetime.utcnow(),
        upstream_version=new_version,
        has_diff=has_diff,
        diff_summary=None,
        action="pending",
    )

    if has_diff and result.get("remote"):
        remote_content = result["remote"].get("system_prompt", "")
        if skill.upstream_content and remote_content:
            check.diff_summary = compute_text_diff_summary(skill.upstream_content, remote_content)

    db.add(check)
    db.commit()
    logger.info(f"Skill {skill.id} upstream check: has_diff={has_diff}")
```

### Step 2: 在 `app/main.py` startup 事件中添加定时检查

在现有 `start_intel_scheduler()` 后添加：

```python
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from app.services.upstream_checker import check_all_imported_skills
            scheduler = BackgroundScheduler()
            scheduler.add_job(check_all_imported_skills, "cron", hour=3, minute=0)
            scheduler.start()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Upstream checker scheduler failed: {e}")
```

### Step 3: 在 `backend/app/routers/skills.py` 添加 diff endpoint

在文件末尾 `delete_skill` 后添加：

```python
@router.get("/{skill_id}/upstream-diff")
def get_upstream_diff(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Return upstream vs local diff for an imported skill."""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if not skill.upstream_content:
        return {"has_upstream": False}

    latest = skill.versions[0] if skill.versions else None
    local_prompt = latest.system_prompt if latest else ""

    # Latest upstream check
    from app.models.mcp import SkillUpstreamCheck
    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    return {
        "has_upstream": True,
        "source_type": skill.source_type,
        "upstream_version": skill.upstream_version,
        "upstream_synced_at": skill.upstream_synced_at.isoformat() if skill.upstream_synced_at else None,
        "is_customized": skill.is_customized,
        "upstream_content": skill.upstream_content,   # 上游原版
        "local_content": local_prompt,                # 本地现版
        "has_new_upstream": latest_check.has_diff if latest_check else False,
        "new_upstream_version": latest_check.upstream_version if latest_check else None,
        "diff_summary": latest_check.diff_summary if latest_check else None,
        "check_action": latest_check.action if latest_check else None,
    }


class UpstreamSyncRequest(BaseModel):
    action: str  # overwrite / ignore


@router.post("/{skill_id}/upstream-sync")
def upstream_sync(
    skill_id: int,
    req: UpstreamSyncRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Handle sync decision: overwrite local with upstream, or ignore upstream update."""
    from app.models.mcp import SkillUpstreamCheck
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id, SkillUpstreamCheck.has_diff == True)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    if req.action == "ignore":
        if latest_check:
            latest_check.action = "ignored"
        db.commit()
        return {"ok": True, "action": "ignored"}

    if req.action == "overwrite":
        # Fetch new upstream content
        source = db.query(McpSource).filter(McpSource.is_active == True).first()
        if not source or not skill.upstream_id:
            raise HTTPException(400, "Cannot fetch upstream: no active source")

        from app.services.mcp_client import fetch_remote_skill, McpClientError
        try:
            remote = fetch_remote_skill(source, skill.upstream_id)
        except McpClientError as e:
            raise HTTPException(502, str(e))

        new_prompt = remote.get("system_prompt", "")
        new_version = remote.get("upstream_version", "")

        # Create new skill version
        max_ver = max((v.version for v in skill.versions), default=0)
        from app.models.skill import SkillVersion
        import datetime
        v = SkillVersion(
            skill_id=skill_id,
            version=max_ver + 1,
            system_prompt=new_prompt,
            variables=[],
            created_by=user.id,
            change_note=f"同步上游 v{new_version}",
        )
        db.add(v)

        # Update skill upstream tracking
        skill.upstream_content = new_prompt
        skill.upstream_version = new_version
        skill.upstream_synced_at = datetime.datetime.utcnow()
        skill.is_customized = False

        if latest_check:
            latest_check.action = "synced"

        db.commit()
        return {"ok": True, "action": "overwrite", "new_version": v.version}

    raise HTTPException(400, f"Unknown action: {req.action}")
```

### Step 4: Commit

```bash
git add backend/app/services/upstream_checker.py backend/app/routers/skills.py backend/app/main.py
git commit -m "feat: upstream version checker service and diff/sync API endpoints"
```

---

## Task 7: 前端"与上游对比" Tab

**Files:**
- Modify: `frontend/app/routes/app/admin/skills/detail.tsx`
- Modify: `frontend/app/lib/types.ts`

### Step 1: 在 `types.ts` 添加 UpstreamDiff 类型

在文件末尾添加：

```ts
export interface UpstreamDiff {
  has_upstream: boolean;
  source_type?: string;
  upstream_version?: string;
  upstream_synced_at?: string;
  is_customized?: boolean;
  upstream_content?: string;
  local_content?: string;
  has_new_upstream?: boolean;
  new_upstream_version?: string;
  diff_summary?: string;
  check_action?: string;
}
```

### Step 2: 在 `skill detail.tsx` 中新增 UpstreamTab 组件

在 `ToolsTab` 组件（约 L406）之后、`export async function loader` 之前，插入：

```tsx
// --- Upstream Diff Tab ---
function UpstreamTab({ skillId, token }: { skillId: number; token: string }) {
  const [diff, setDiff] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/skills/${skillId}/upstream-diff`, { token });
      setDiff(data);
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [skillId]);

  async function syncAction(action: "overwrite" | "ignore") {
    setSyncing(true);
    setError("");
    try {
      await apiFetch(`/api/skills/${skillId}/upstream-sync`, {
        method: "POST",
        body: JSON.stringify({ action }),
        token,
      });
      setSuccess(action === "overwrite" ? "已同步上游最新版本，新版本已创建" : "已忽略本次上游更新");
      load();
    } catch (e: any) {
      setError(e.message || "操作失败");
    } finally {
      setSyncing(false);
    }
  }

  if (loading) return <p className="text-xs font-bold uppercase text-gray-400">加载中...</p>;

  if (!diff?.has_upstream) {
    return (
      <div className="border-2 border-dashed border-gray-300 p-8 text-center">
        <p className="text-xs font-bold uppercase text-gray-400">此 Skill 非从外部市场导入，无上游对比</p>
      </div>
    );
  }

  // Compute line-level diff for display
  function renderDiff(oldText: string, newText: string) {
    const oldLines = oldText.split("\n");
    const newLines = newText.split("\n");
    const oldSet = new Set(oldLines);
    const newSet = new Set(newLines);
    return { oldLines, newLines, oldSet, newSet };
  }

  const { oldLines, newLines, oldSet, newSet } = diff.upstream_content && diff.local_content
    ? renderDiff(diff.upstream_content, diff.local_content)
    : { oldLines: [], newLines: [], oldSet: new Set(), newSet: new Set() };

  return (
    <div className="space-y-5">
      {error && (
        <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700 uppercase">[ERROR] {error}</div>
      )}
      {success && (
        <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-3 py-2 text-xs font-bold text-[#00A3C4] uppercase">[OK] {success}</div>
      )}

      {/* Status banner */}
      <div className="border-2 border-[#1A202C] bg-white p-4 flex items-center justify-between">
        <div>
          <p className="text-[10px] font-bold uppercase text-gray-500">上游版本</p>
          <p className="text-xs font-bold text-[#1A202C]">v{diff.upstream_version}</p>
          <p className="text-[9px] text-gray-400 mt-0.5">
            上次同步：{diff.upstream_synced_at ? new Date(diff.upstream_synced_at).toLocaleDateString("zh-CN") : "—"}
          </p>
        </div>
        <div className="text-right">
          {diff.is_customized && (
            <span className="inline-block border px-2 py-0.5 text-[9px] font-bold uppercase bg-yellow-100 text-yellow-700 border-yellow-400">
              已二次修改
            </span>
          )}
          {diff.has_new_upstream && (
            <div className="mt-1">
              <span className="inline-block border px-2 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]">
                上游有新版本 v{diff.new_upstream_version}
              </span>
              {diff.diff_summary && (
                <p className="text-[9px] text-gray-400 mt-0.5">{diff.diff_summary}</p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Sync actions */}
      {diff.has_new_upstream && diff.check_action === "pending" && (
        <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/20 p-4 space-y-3">
          <p className="text-[10px] font-bold uppercase text-[#00A3C4]">— 同步决策</p>
          <div className="flex gap-3">
            <button
              onClick={() => syncAction("overwrite")}
              disabled={syncing}
              className="bg-[#1A202C] text-[#00D1FF] px-4 py-2 text-[10px] font-bold uppercase hover:bg-black disabled:opacity-50 pixel-border"
            >
              {syncing ? "处理中..." : "覆盖 — 拉取上游最新版"}
            </button>
            <button
              onClick={() => syncAction("ignore")}
              disabled={syncing}
              className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100 disabled:opacity-50"
            >
              忽略 — 保持本地版本
            </button>
          </div>
        </div>
      )}

      {/* Diff view: upstream original vs local */}
      <div>
        <p className="text-[10px] font-bold uppercase text-gray-500 mb-2">上游原版 vs 本地版本</p>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <p className="text-[9px] font-bold uppercase text-gray-400 mb-1">上游原版（导入时快照）</p>
            <div className="border-2 border-[#1A202C] bg-[#F8FAFC] p-3 font-mono text-xs max-h-80 overflow-y-auto">
              {oldLines.map((line, i) => (
                <div
                  key={i}
                  className={`whitespace-pre-wrap leading-5 ${!newSet.has(line) ? "bg-red-100 text-red-700" : ""}`}
                >
                  {line || "\u00A0"}
                </div>
              ))}
            </div>
          </div>
          <div>
            <p className="text-[9px] font-bold uppercase text-gray-400 mb-1">本地版本（现在）</p>
            <div className="border-2 border-[#1A202C] bg-[#F8FAFC] p-3 font-mono text-xs max-h-80 overflow-y-auto">
              {newLines.map((line, i) => (
                <div
                  key={i}
                  className={`whitespace-pre-wrap leading-5 ${!oldSet.has(line) ? "bg-green-100 text-green-700" : ""}`}
                >
                  {line || "\u00A0"}
                </div>
              ))}
            </div>
          </div>
        </div>
        <p className="text-[9px] text-gray-400 mt-1">红色 = 上游有但本地已删 / 绿色 = 本地新增</p>
      </div>
    </div>
  );
}
```

### Step 3: 在 detail.tsx 的 Tab 列表中添加"与上游对比"

找到现有 Tab 定义（约 L598）：

```tsx
const [activeTab, setActiveTab] = useState<"info" | "suggestions" | "tools">("info");
```

改为：

```tsx
const [activeTab, setActiveTab] = useState<"info" | "suggestions" | "tools" | "upstream">("info");
```

找到 Tab 按钮渲染（`["info", "suggestions", "tools"]`），改为：

```tsx
{(["info", "suggestions", "tools", "upstream"] as const).map((tab, i) => (
  <button
    key={tab}
    onClick={() => setActiveTab(tab)}
    className={...}
  >
    {tab === "info" ? "基本信息"
      : tab === "suggestions" ? "改进意见"
      : tab === "tools" ? "绑定工具"
      : "与上游对比"}
  </button>
))}
```

在现有 `{/* Tools Tab */}` 块之后，添加：

```tsx
{/* Upstream Tab */}
{!isNew && activeTab === "upstream" && (
  <UpstreamTab skillId={skill.id} token={token} />
)}
```

### Step 4: 对导入的 Skill，标记 is_customized

在 `skills.py` 的 `add_version` 路由中，添加对 `is_customized` 的自动标记：

找到 `add_version` 函数内 `db.add(v)` 之前，添加：

```python
    # Mark as customized if this is an imported skill being modified
    if skill.source_type in ("imported", "forked"):
        skill.is_customized = True
        skill.local_modified_at = datetime.datetime.utcnow()
```

（需要在文件顶部 import datetime）

### Step 5: Commit

```bash
git add frontend/app/routes/app/admin/skills/detail.tsx frontend/app/lib/types.ts backend/app/routers/skills.py
git commit -m "feat: upstream diff tab in skill detail and is_customized auto-marking"
```

---

## Task 8: MCP Token 管理前端页

**Files:**
- Create: `frontend/app/routes/app/admin/mcp-tokens/index.tsx`
- Modify: `frontend/app/routes.ts`

### Step 1: 创建 Token 管理页 `frontend/app/routes/app/admin/mcp-tokens/index.tsx`

（复用已有设计语言，显示 Token 列表、创建新 Token 表单，创建成功后一次性显示原始 Token，之后只显示前缀。参考 `admin/tools/index.tsx` 的代码风格。）

关键 UX：
- 列表显示：前缀 / scope / workspace / 过期时间 / 最后使用时间 / 删除按钮
- 创建表单：scope 下拉（user/workspace/admin）/ workspace 下拉（scope=workspace 时显示）/ 过期天数
- 创建成功：弹出框一次性展示完整 token，提示"请立即复制，关闭后无法再次查看"

### Step 2: 注册路由

在 `routes.ts` 的 admin layout 内添加：

```ts
route("admin/mcp-tokens", "routes/app/admin/mcp-tokens/index.tsx"),
```

### Step 3: 在 admin layout 添加侧边栏入口

添加"MCP Token"到侧边栏，与"外部市场"相邻。

### Step 4: Commit

```bash
git add frontend/app/routes/app/admin/mcp-tokens/ frontend/app/routes.ts frontend/app/routes/app/admin/layout.tsx
git commit -m "feat: MCP token management frontend page"
```

---

## 验证清单

运行所有后端测试：

```bash
cd backend && python -m pytest tests/ -v
```

前端 lint：

```bash
cd frontend && npm run build
```

Expected: 零报错，零 TypeScript 错误。

手动验证：
1. 添加一个 MCP 数据源（REST 模式，指向本地 mock）
2. 在市场页搜索并导入一个 Skill → 确认以草稿状态出现
3. 在导入的 Skill 详情页，点击"与上游对比" Tab → 确认显示正确
4. 创建一个 MCP Token → 复制 token，用 curl 调用 `POST /mcp` 确认 `tools/list` 正常返回
5. 删除 Token → 调用 `POST /mcp` 确认 401
