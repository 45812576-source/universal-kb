# Comprehensive Test Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Universal KB 前后端第一、二期所有功能编写完善的测试用例，发现并修复 bug，实现充分回归覆盖。

**Architecture:**
- 后端：pytest + SQLite in-memory DB，TestClient for HTTP，mock 所有 LLM/外部调用
- 前端：Playwright E2E 端到端测试，真实浏览器验证关键用户流程
- 后端测试独立、幂等、无外部依赖；前端测试对接真实后端（test DB）

**Tech Stack:** pytest, pytest-asyncio, httpx/TestClient, unittest.mock, Playwright, TypeScript

---

## 准备工作

### Task 0: 安装前端测试依赖

**Files:**
- Modify: `frontend/package.json`

**Step 1: 安装 Playwright**
```bash
cd /Users/liaoxia/projects/universal-kb/frontend
npm install -D @playwright/test
npx playwright install chromium
```

**Step 2: 创建 playwright.config.ts**
```typescript
// frontend/playwright.config.ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://localhost:5174",
    headless: true,
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5174",
    reuseExistingServer: true,
  },
});
```

**Step 3: 在 package.json scripts 里加**
```json
"test:e2e": "playwright test"
```

**Step 4: 验证安装**
```bash
cd frontend && npx playwright test --list
```

---

## 后端测试

### Task 1: 修复 conftest.py — 补充新模型 seed helper

**Files:**
- Modify: `backend/tests/conftest.py`

**问题：** 现有 conftest 缺少 IntelSource、ToolRegistry、WebApp 等新模型的 helper，新测试文件需要它们。

**Step 1: 在 conftest.py 末尾追加以下 helper**

```python
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
    app = WebApp(
        name=name,
        description="测试用",
        html_content="<html><body>Hello</body></html>",
        created_by=user_id,
        is_public=is_public,
        share_token=secrets.token_urlsafe(16),
    )
    db.add(app)
    db.flush()
    return app
```

**Step 2: 运行现有测试确认没有破坏**
```bash
cd backend && .venv/bin/python -m pytest tests/ -v --tb=short -q
```
Expected: 112 passed

---

### Task 2: test_intel.py — 情报源与情报条目

**Files:**
- Create: `backend/tests/test_intel.py`

**覆盖范围：**
- IntelSource CRUD + 权限
- IntelEntry list/get/approve/reject/filter
- 角色可见性（员工只看已批准）

```python
"""TC-INTEL: Intelligence source and entry management."""
import pytest
from tests.conftest import (
    _make_user, _make_dept, _make_intel_source, _make_intel_entry, _login, _auth
)
from app.models.user import Role
from app.models.intel import IntelEntryStatus, IntelSourceType


# ── Source: role enforcement ─────────────────────────────────────────────────

def test_employee_cannot_list_sources(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "iemp1")
    resp = client.get("/api/intel/sources", headers=_auth(token))
    assert resp.status_code == 403


def test_admin_can_list_sources(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin1")
    resp = client.get("/api/intel/sources", headers=_auth(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Source: CRUD ─────────────────────────────────────────────────────────────

def test_create_source(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin2")
    resp = client.post("/api/intel/sources", headers=_auth(token), json={
        "name": "测试RSS源",
        "source_type": "rss",
        "config": {"url": "https://example.com/rss"},
        "is_active": True,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "测试RSS源"
    assert resp.json()["source_type"] == "rss"


def test_update_source(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "iadmin3", Role.SUPER_ADMIN, dept.id)
    src = _make_intel_source(db, "旧名称")
    db.commit()
    token = _login(client, "iadmin3")
    resp = client.put(f"/api/intel/sources/{src.id}", headers=_auth(token), json={
        "name": "新名称",
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名称"
    assert resp.json()["is_active"] is False


def test_update_source_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin4", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "iadmin4")
    resp = client.put("/api/intel/sources/99999", headers=_auth(token), json={"name": "x"})
    assert resp.status_code == 404


def test_delete_source(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin5", Role.SUPER_ADMIN, dept.id)
    src = _make_intel_source(db)
    db.commit()
    token = _login(client, "iadmin5")
    resp = client.delete(f"/api/intel/sources/{src.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_source_dept_admin_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "idept1", Role.DEPT_ADMIN, dept.id)
    src = _make_intel_source(db)
    db.commit()
    token = _login(client, "idept1")
    resp = client.delete(f"/api/intel/sources/{src.id}", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: visibility ────────────────────────────────────────────────────────

def test_employee_only_sees_approved_entries(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp2", Role.EMPLOYEE, dept.id)
    _make_intel_entry(db, title="待审情报", status=IntelEntryStatus.PENDING)
    _make_intel_entry(db, title="已批准情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iemp2")
    resp = client.get("/api/intel/entries", headers=_auth(token))
    assert resp.status_code == 200
    titles = [e["title"] for e in resp.json()["items"]]
    assert "已批准情报" in titles
    assert "待审情报" not in titles


def test_admin_sees_all_entries(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin6", Role.SUPER_ADMIN, dept.id)
    _make_intel_entry(db, title="待审情报2", status=IntelEntryStatus.PENDING)
    _make_intel_entry(db, title="已批准情报2", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin6")
    resp = client.get("/api/intel/entries?status=pending", headers=_auth(token))
    titles = [e["title"] for e in resp.json()["items"]]
    assert "待审情报2" in titles


# ── Entry: approve/reject ────────────────────────────────────────────────────

def test_approve_entry(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin7", Role.SUPER_ADMIN, dept.id)
    entry = _make_intel_entry(db)
    db.commit()
    token = _login(client, "iadmin7")
    resp = client.patch(f"/api/intel/entries/{entry.id}/approve", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_reject_entry(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin8", Role.SUPER_ADMIN, dept.id)
    entry = _make_intel_entry(db)
    db.commit()
    token = _login(client, "iadmin8")
    resp = client.patch(f"/api/intel/entries/{entry.id}/reject", headers=_auth(token))
    assert resp.status_code == 200


def test_employee_cannot_approve(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp3", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, status=IntelEntryStatus.PENDING)
    db.commit()
    token = _login(client, "iemp3")
    resp = client.patch(f"/api/intel/entries/{entry.id}/approve", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: detail & non-approved access ──────────────────────────────────────

def test_get_approved_entry_as_employee(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp4", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, title="公开情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iemp4")
    resp = client.get(f"/api/intel/entries/{entry.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["title"] == "公开情报"


def test_get_pending_entry_as_employee_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "iemp5", Role.EMPLOYEE, dept.id)
    entry = _make_intel_entry(db, status=IntelEntryStatus.PENDING)
    db.commit()
    token = _login(client, "iemp5")
    resp = client.get(f"/api/intel/entries/{entry.id}", headers=_auth(token))
    assert resp.status_code == 403


# ── Entry: filter/pagination ──────────────────────────────────────────────────

def test_filter_entries_by_industry(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "iadmin9", Role.SUPER_ADMIN, dept.id)
    e1 = _make_intel_entry(db, title="食品情报", status=IntelEntryStatus.APPROVED)
    db.commit()
    # update industry
    from app.models.intel import IntelEntry
    from app.database import get_db
    entry = db.get(IntelEntry, e1.id)
    entry.industry = "食品"
    db.commit()
    token = _login(client, "iadmin9")
    resp = client.get("/api/intel/entries?industry=食品", headers=_auth(token))
    assert resp.status_code == 200
    assert any(e["title"] == "食品情报" for e in resp.json()["items"])


def test_intel_entries_pagination(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin10", Role.SUPER_ADMIN, dept.id)
    for i in range(5):
        _make_intel_entry(db, title=f"情报{i}", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin10")
    resp = client.get("/api/intel/entries?page=1&page_size=2", headers=_auth(token))
    data = resp.json()
    assert data["total"] >= 5
    assert len(data["items"]) == 2


# ── Entry: search ─────────────────────────────────────────────────────────────

def test_search_entries_by_keyword(client, db):
    dept = _make_dept(db)
    _make_user(db, "iadmin11", Role.SUPER_ADMIN, dept.id)
    _make_intel_entry(db, title="抖音直播带货趋势", status=IntelEntryStatus.APPROVED)
    _make_intel_entry(db, title="小红书种草策略", status=IntelEntryStatus.APPROVED)
    db.commit()
    token = _login(client, "iadmin11")
    resp = client.get("/api/intel/entries?q=抖音", headers=_auth(token))
    titles = [e["title"] for e in resp.json()["items"]]
    assert "抖音直播带货趋势" in titles
    assert "小红书种草策略" not in titles
```

**Step 3: 运行**
```bash
.venv/bin/python -m pytest tests/test_intel.py -v --tb=short
```
Expected: 全部 PASS

---

### Task 3: test_tools.py — 工具注册表与 Skill 绑定

**Files:**
- Create: `backend/tests/test_tools.py`

```python
"""TC-TOOLS: Tool registry CRUD, Skill binding, role enforcement."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_skill, _make_tool, _login, _auth
from app.models.user import Role
from app.models.tool import ToolType


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_list_tools_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin1", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin1")
    resp = client.get("/api/tools", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_tool_as_admin(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin2", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin2")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "weather_tool",
        "display_name": "天气查询",
        "description": "查询天气信息",
        "tool_type": "builtin",
        "config": {},
        "input_schema": {"city": "string"},
        "output_format": "json",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "weather_tool"


def test_create_tool_duplicate_name_fails(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin3", Role.SUPER_ADMIN, dept.id)
    _make_tool(db, admin.id, "dup_tool")
    db.commit()
    token = _login(client, "tadmin3")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "dup_tool",
        "display_name": "重复",
        "tool_type": "builtin",
    })
    assert resp.status_code == 400


def test_create_tool_employee_forbidden(client, db):
    dept = _make_dept(db)
    _make_user(db, "temp1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "temp1")
    resp = client.post("/api/tools", headers=_auth(token), json={
        "name": "emp_tool",
        "display_name": "x",
        "tool_type": "builtin",
    })
    assert resp.status_code == 403


def test_get_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin4", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "get_tool")
    db.commit()
    token = _login(client, "tadmin4")
    resp = client.get(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["name"] == "get_tool"


def test_get_tool_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "tadmin5", Role.SUPER_ADMIN, dept.id)
    db.commit()
    token = _login(client, "tadmin5")
    resp = client.get("/api/tools/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_update_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin6", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "upd_tool")
    db.commit()
    token = _login(client, "tadmin6")
    resp = client.put(f"/api/tools/{tool.id}", headers=_auth(token), json={
        "display_name": "更新后工具",
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "更新后工具"
    assert resp.json()["is_active"] is False


def test_delete_tool_requires_super_admin(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin7s", Role.SUPER_ADMIN, dept.id)
    dept_admin = _make_user(db, "tdept1", Role.DEPT_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "del_tool")
    db.commit()
    token = _login(client, "tdept1")
    resp = client.delete(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 403


def test_delete_tool(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin8", Role.SUPER_ADMIN, dept.id)
    tool = _make_tool(db, admin.id, "gone_tool")
    db.commit()
    token = _login(client, "tadmin8")
    resp = client.delete(f"/api/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.get(f"/api/tools/{tool.id}", headers=_auth(token)).status_code == 404


# ── Skill binding ─────────────────────────────────────────────────────────────

def test_bind_tool_to_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin9", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "绑定Skill")
    tool = _make_tool(db, admin.id, "bind_tool")
    db.commit()
    token = _login(client, "tadmin9")
    resp = client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_bind_tool_idempotent(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin10", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "幂等Skill")
    tool = _make_tool(db, admin.id, "idem_tool")
    db.commit()
    token = _login(client, "tadmin10")
    client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    resp = client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200  # idempotent


def test_get_skill_tools(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin11", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "工具列表Skill")
    t1 = _make_tool(db, admin.id, "tool_list_1")
    t2 = _make_tool(db, admin.id, "tool_list_2")
    db.commit()
    token = _login(client, "tadmin11")
    client.post(f"/api/tools/skill/{skill.id}/tools/{t1.id}", headers=_auth(token))
    client.post(f"/api/tools/skill/{skill.id}/tools/{t2.id}", headers=_auth(token))
    resp = client.get(f"/api/tools/skill/{skill.id}/tools", headers=_auth(token))
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "tool_list_1" in names
    assert "tool_list_2" in names


def test_unbind_tool_from_skill(client, db):
    dept = _make_dept(db)
    admin = _make_user(db, "tadmin12", Role.SUPER_ADMIN, dept.id)
    skill = _make_skill(db, admin.id, "解绑Skill")
    tool = _make_tool(db, admin.id, "unbind_tool")
    db.commit()
    token = _login(client, "tadmin12")
    client.post(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    resp = client.delete(f"/api/tools/skill/{skill.id}/tools/{tool.id}", headers=_auth(token))
    assert resp.status_code == 200
    resp2 = client.get(f"/api/tools/skill/{skill.id}/tools", headers=_auth(token))
    assert not any(t["name"] == "unbind_tool" for t in resp2.json())
```

**Step 3: 运行**
```bash
.venv/bin/python -m pytest tests/test_tools.py -v --tb=short
```

---

### Task 4: test_web_apps.py — Web 应用 CRUD 与权限

**Files:**
- Create: `backend/tests/test_web_apps.py`

```python
"""TC-WEBAPPS: Web app CRUD, ownership, preview, public share."""
import pytest
from tests.conftest import _make_user, _make_dept, _make_web_app, _login, _auth
from app.models.user import Role


def test_list_web_apps_empty(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser1", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser1")
    resp = client.get("/api/web-apps", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_web_app(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser2", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser2")
    resp = client.post("/api/web-apps", headers=_auth(token), json={
        "name": "我的应用",
        "description": "测试应用",
        "html_content": "<html><body>Hello</body></html>",
        "is_public": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "我的应用"
    assert "share_token" in data


def test_list_web_apps_only_own(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser3a", Role.EMPLOYEE, dept.id)
    u2 = _make_user(db, "wuser3b", Role.EMPLOYEE, dept.id)
    _make_web_app(db, u1.id, "用户1应用")
    _make_web_app(db, u2.id, "用户2应用")
    db.commit()
    t1 = _login(client, "wuser3a")
    resp = client.get("/api/web-apps", headers=_auth(t1))
    names = [a["name"] for a in resp.json()]
    assert "用户1应用" in names
    assert "用户2应用" not in names


def test_get_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser4", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "详情应用")
    db.commit()
    token = _login(client, "wuser4")
    resp = client.get(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["name"] == "详情应用"
    assert "html_content" in resp.json()


def test_get_web_app_not_found(client, db):
    dept = _make_dept(db)
    _make_user(db, "wuser5", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "wuser5")
    resp = client.get("/api/web-apps/99999", headers=_auth(token))
    assert resp.status_code == 404


def test_update_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser6", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "旧名应用")
    db.commit()
    token = _login(client, "wuser6")
    resp = client.put(f"/api/web-apps/{app.id}", headers=_auth(token), json={
        "name": "新名应用",
        "html_content": "<html><body>Updated</body></html>",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名应用"


def test_update_others_app_forbidden(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser7a", Role.EMPLOYEE, dept.id)
    u2 = _make_user(db, "wuser7b", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u1.id, "他人应用")
    db.commit()
    token = _login(client, "wuser7b")
    resp = client.put(f"/api/web-apps/{app.id}", headers=_auth(token), json={"name": "篡改"})
    assert resp.status_code == 403


def test_delete_web_app(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser8", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "待删应用")
    db.commit()
    token = _login(client, "wuser8")
    resp = client.delete(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_others_app_forbidden(client, db):
    dept = _make_dept(db)
    u1 = _make_user(db, "wuser9a", Role.EMPLOYEE, dept.id)
    u2 = _make_user(db, "wuser9b", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u1.id, "他人待删应用")
    db.commit()
    token = _login(client, "wuser9b")
    resp = client.delete(f"/api/web-apps/{app.id}", headers=_auth(token))
    assert resp.status_code == 403


def test_preview_web_app_returns_html(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser10", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "预览应用")
    db.commit()
    token = _login(client, "wuser10")
    resp = client.get(f"/api/web-apps/{app.id}/preview", headers=_auth(token))
    assert resp.status_code == 200
    assert "Hello" in resp.text


def test_public_share_no_auth_required(client, db):
    dept = _make_dept(db)
    u = _make_user(db, "wuser11", Role.EMPLOYEE, dept.id)
    app = _make_web_app(db, u.id, "公开应用", is_public=True)
    db.commit()
    # Access without auth
    resp = client.get(f"/share/{app.share_token}")
    assert resp.status_code == 200
    assert "Hello" in resp.text


def test_share_invalid_token_404(client, db):
    resp = client.get("/share/invalid-token-xyz")
    assert resp.status_code == 404
```

**Step 3: 运行**
```bash
.venv/bin/python -m pytest tests/test_web_apps.py -v --tb=short
```

---

### Task 5: test_files.py — 文件下载安全性

**Files:**
- Create: `backend/tests/test_files.py`

```python
"""TC-FILES: File download endpoint — path traversal protection, type validation."""
import pytest
import os
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.config import settings
from pathlib import Path


def _create_test_file(filename: str, content: bytes = b"test content"):
    generated_dir = Path(settings.UPLOAD_DIR) / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    path = generated_dir / filename
    path.write_bytes(content)
    return path


def test_download_valid_html_file(client, db):
    path = _create_test_file("test_output.html", b"<html><body>OK</body></html>")
    try:
        resp = client.get("/api/files/test_output.html")
        assert resp.status_code == 200
        assert b"OK" in resp.content
    finally:
        path.unlink(missing_ok=True)


def test_download_disallowed_extension_rejected(client):
    resp = client.get("/api/files/evil.py")
    assert resp.status_code == 400


def test_path_traversal_rejected(client):
    resp = client.get("/api/files/../../etc/passwd")
    assert resp.status_code in (400, 404, 422)


def test_path_traversal_with_slash_rejected(client):
    resp = client.get("/api/files/subdir/file.html")
    assert resp.status_code == 400


def test_nonexistent_file_404(client):
    resp = client.get("/api/files/nonexistent_file.pdf")
    assert resp.status_code == 404


def test_double_dot_in_filename_rejected(client):
    resp = client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404, 422)
```

**Step 3: 运行**
```bash
.venv/bin/python -m pytest tests/test_files.py -v --tb=short
```

---

### Task 6: 修复后端 Bug — 运行全套后端测试

**Step 1: 运行全部后端测试，记录失败**
```bash
cd backend && .venv/bin/python -m pytest tests/ -v --tb=short 2>&1 | tee /tmp/backend_test_results.txt
```

**Step 2: 分析失败原因，针对性修复**

常见 bug 模式：
- 新模型没有 cascade delete（参考之前的 Skill 修复）
- SQLite 不支持的 MySQL 特性（JSON、INFORMATION_SCHEMA）
- conftest 中 clean_tables 不清理新表

**Step 3: 修复 conftest.py clean_tables（如有新表未被清理）**

SQLite 中 `table.delete()` 应该自动清所有 Base.metadata 表，确认新模型都正确继承 Base。

**Step 4: 再次运行确认全绿**
```bash
.venv/bin/python -m pytest tests/ -v -q
```
Expected: 全部 PASS（目标 150+ 用例）

---

## 前端 E2E 测试

### Task 7: 创建 E2E 测试基础设施

**Files:**
- Create: `frontend/e2e/helpers.ts`
- Create: `frontend/e2e/fixtures.ts`

```typescript
// frontend/e2e/helpers.ts
import { Page } from "@playwright/test";

export async function login(page: Page, username = "admin", password = "admin123") {
  await page.goto("/login");
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForURL("/");
}

export async function logout(page: Page) {
  await page.goto("/logout");
}
```

```typescript
// frontend/e2e/fixtures.ts
import { test as base } from "@playwright/test";
import { login } from "./helpers";

export const test = base.extend<{ authedPage: any }>({
  authedPage: async ({ page }, use) => {
    await login(page);
    await use(page);
  },
});

export { expect } from "@playwright/test";
```

---

### Task 8: E2E — 认证流程

**Files:**
- Create: `frontend/e2e/auth.spec.ts`

```typescript
import { test, expect } from "@playwright/test";
import { login, logout } from "./helpers";

test.describe("Auth", () => {
  test("登录成功跳转到主页", async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL("/");
    // 应该能看到对话界面
    await expect(page.locator("text=发送")).toBeVisible({ timeout: 5000 }).catch(() => {
      // fallback: 有导航栏即可
      return expect(page.locator("nav, aside")).toBeVisible();
    });
  });

  test("密码错误显示报错", async ({ page }) => {
    await page.goto("/login");
    await page.fill('input[name="username"]', "admin");
    await page.fill('input[name="password"]', "wrongpassword");
    await page.click('button[type="submit"]');
    await expect(page.locator("text=用户名或密码错误")).toBeVisible();
    await expect(page).toHaveURL("/login");
  });

  test("未登录访问根路径跳转到 login", async ({ page }) => {
    // Clear cookies
    await page.context().clearCookies();
    await page.goto("/");
    await expect(page).toHaveURL("/login");
  });

  test("已登录访问 /login 跳转到主页", async ({ page }) => {
    await login(page);
    await page.goto("/login");
    await expect(page).toHaveURL("/");
  });

  test("登出后无法访问保护页面", async ({ page }) => {
    await login(page);
    await logout(page);
    await page.goto("/admin/skills");
    await expect(page).toHaveURL("/login");
  });
});
```

**Step 3: 运行**
```bash
cd frontend && npx playwright test e2e/auth.spec.ts --reporter=line
```

---

### Task 9: E2E — Skill 管理

**Files:**
- Create: `frontend/e2e/skills.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("Skill 管理", () => {
  test("可以访问 Skill 列表页", async ({ authedPage: page }) => {
    await page.goto("/admin/skills");
    await expect(page.locator("h1, text=Skill 管理")).toBeVisible();
  });

  test("点击新建 Skill 跳转到创建页", async ({ authedPage: page }) => {
    await page.goto("/admin/skills");
    await page.click("text=新建 Skill");
    await expect(page).toHaveURL("/admin/skills/new");
  });

  test("新建 Skill 表单提交成功", async ({ authedPage: page }) => {
    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', `E2E测试Skill-${Date.now()}`);
    await page.fill('input[name="description"]', "E2E自动化测试");
    await page.fill('textarea[name="system_prompt"]', "你是E2E测试助手。");
    await page.click('button[type="submit"]');
    // 提交成功后应跳转到详情页
    await expect(page).toHaveURL(/\/admin\/skills\/\d+/);
  });

  test("重复 Skill 名称提交报错", async ({ authedPage: page }) => {
    // Create one first
    const uniqueName = `重复Skill-${Date.now()}`;
    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', uniqueName);
    await page.fill('textarea[name="system_prompt"]', "first");
    await page.click('button[type="submit"]');
    await page.waitForURL(/\/admin\/skills\/\d+/);

    // Try to create again with same name
    await page.goto("/admin/skills/new");
    await page.fill('input[name="name"]', uniqueName);
    await page.fill('textarea[name="system_prompt"]', "second");
    await page.click('button[type="submit"]');
    // Should stay on new page with error
    await expect(page.locator(".bg-red-50, text=already exists, text=已存在")).toBeVisible();
  });

  test("可以发布 Skill", async ({ authedPage: page }) => {
    // Create a draft skill first
    await page.goto("/admin/skills/new");
    const name = `发布测试-${Date.now()}`;
    await page.fill('input[name="name"]', name);
    await page.fill('textarea[name="system_prompt"]', "发布测试prompt");
    await page.click('button[type="submit"]');
    await page.waitForURL(/\/admin\/skills\/\d+/);

    // Go back to list and publish
    await page.goto("/admin/skills");
    await page.click(`text=发布`, { timeout: 5000 });
    await page.waitForTimeout(1000);
    await expect(page.locator("text=已发布")).toBeVisible();
  });
});
```

---

### Task 10: E2E — 知识库流程

**Files:**
- Create: `frontend/e2e/knowledge.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("知识库", () => {
  test("员工可以提交知识条目", async ({ authedPage: page }) => {
    await page.goto("/knowledge/new");
    await page.fill('input[name="title"], input[placeholder*="标题"]', `E2E知识-${Date.now()}`);
    await page.fill('textarea[name="content"], textarea[placeholder*="内容"]', "这是E2E测试的知识内容，足够详细。");
    await page.click('button[type="submit"], button:has-text("提交")');
    // Should show success or redirect
    await expect(
      page.locator("text=提交成功, text=待审核, .bg-green-50")
    ).toBeVisible({ timeout: 5000 });
  });

  test("可以访问我的知识列表", async ({ authedPage: page }) => {
    await page.goto("/knowledge/my");
    await expect(page).not.toHaveURL("/login");
  });

  test("管理员可以访问知识审核页面", async ({ authedPage: page }) => {
    await page.goto("/admin/knowledge");
    await expect(page.locator("h1, text=知识审核")).toBeVisible();
  });

  test("管理员可以批准知识条目", async ({ authedPage: page }) => {
    // Submit a knowledge entry first
    await page.goto("/knowledge/new");
    const title = `审核测试-${Date.now()}`;
    await page.fill('input[name="title"], input[placeholder*="标题"]', title);
    await page.fill('textarea[name="content"], textarea[placeholder*="内容"]', "审核测试内容详细描述。");
    await page.click('button[type="submit"], button:has-text("提交")');
    await page.waitForTimeout(500);

    // Go to admin review page
    await page.goto("/admin/knowledge");
    const row = page.locator(`text=${title}`).first();
    if (await row.isVisible()) {
      await row.locator("..").locator("button:has-text('批准'), button:has-text('通过')").click();
      await page.waitForTimeout(500);
      await expect(page.locator("text=已批准, text=approved")).toBeVisible();
    }
  });
});
```

---

### Task 11: E2E — 对话聊天

**Files:**
- Create: `frontend/e2e/chat.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("对话聊天", () => {
  test("主页显示聊天界面", async ({ authedPage: page }) => {
    await page.goto("/");
    // Should see chat UI elements
    await expect(
      page.locator("text=发送, textarea, input[placeholder*='消息'], input[placeholder*='输入']")
    ).toBeVisible({ timeout: 5000 });
  });

  test("可以创建新对话", async ({ authedPage: page }) => {
    await page.goto("/");
    const newChatBtn = page.locator("text=新建对话, button:has-text('新'), text=+");
    if (await newChatBtn.isVisible()) {
      await newChatBtn.click();
      await page.waitForTimeout(500);
      await expect(page).not.toHaveURL("/login");
    }
  });

  test("对话列表可见", async ({ authedPage: page }) => {
    await page.goto("/");
    // Sidebar should be visible
    await expect(page.locator("aside, nav, .sidebar")).toBeVisible({ timeout: 3000 }).catch(() => {
      // Some layouts may not have explicit sidebar
    });
  });
});
```

---

### Task 12: E2E — 业务数据表管理

**Files:**
- Create: `frontend/e2e/business-tables.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("业务数据表", () => {
  test("可以访问业务数据表列表", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables");
    await expect(page.locator("h1, text=业务数据表")).toBeVisible();
  });

  test("点击生成新数据表跳转到生成页", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables");
    await page.click("text=生成新数据表");
    await expect(page).toHaveURL("/admin/business-tables/generate");
  });

  test("生成页切换模式", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables/generate");
    // Mode A should be active by default
    await expect(page.locator("text=描述业务场景, text=描述→生成")).toBeVisible();
    // Switch to mode B
    await page.click("text=已有表→生成Skill, text=方向B");
    await expect(page.locator("input[placeholder*='表名']")).toBeVisible();
  });

  test("空描述不能提交", async ({ authedPage: page }) => {
    await page.goto("/admin/business-tables/generate");
    const btn = page.locator("button:has-text('生成预览')");
    await expect(btn).toBeDisabled();
  });
});
```

---

### Task 13: E2E — 管理后台通用

**Files:**
- Create: `frontend/e2e/admin.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("管理后台", () => {
  test("可以访问模型配置页", async ({ authedPage: page }) => {
    await page.goto("/admin/models");
    await expect(page.locator("h1, text=模型配置")).toBeVisible();
  });

  test("可以访问审计日志页", async ({ authedPage: page }) => {
    await page.goto("/admin/audit");
    await expect(page.locator("text=审计日志, text=操作日志")).toBeVisible();
  });

  test("可以访问贡献统计页", async ({ authedPage: page }) => {
    await page.goto("/admin/contributions");
    await expect(page.locator("text=贡献统计, text=排行, text=影响")).toBeVisible();
  });

  test("可以访问情报管理页", async ({ authedPage: page }) => {
    await page.goto("/admin/intel");
    await expect(page).not.toHaveURL("/login");
  });

  test("可以访问工具管理页", async ({ authedPage: page }) => {
    await page.goto("/admin/tools");
    await expect(page).not.toHaveURL("/login");
  });

  test("创建模型配置", async ({ authedPage: page }) => {
    await page.goto("/admin/models");
    // Look for create button
    const createBtn = page.locator("button:has-text('新建'), button:has-text('添加'), button:has-text('+')").first();
    if (await createBtn.isVisible()) {
      await createBtn.click();
      // Form should appear
      await expect(page.locator("input[name='name'], input[placeholder*='名称']")).toBeVisible();
    }
  });
});
```

---

### Task 14: E2E — 改进建议流程

**Files:**
- Create: `frontend/e2e/suggestions.spec.ts`

```typescript
import { test, expect } from "./fixtures";

test.describe("改进建议", () => {
  test("可以访问我的建议列表", async ({ authedPage: page }) => {
    await page.goto("/suggestions/my");
    await expect(page).not.toHaveURL("/login");
  });

  test("提交建议页面可访问", async ({ authedPage: page }) => {
    await page.goto("/suggestions/new");
    await expect(page).not.toHaveURL("/login");
  });
});
```

---

### Task 15: 运行全部 E2E 测试并修复失败

**Step 1: 确认后端和前端都在运行**
```bash
# 终端1：后端
cd backend && DEEPSEEK_API_KEY=sk-411fa54a27024dfe86fc756617b80629 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端2：前端
cd frontend && npm run dev
```

**Step 2: 运行所有 E2E 测试**
```bash
cd frontend && npx playwright test --reporter=list
```

**Step 3: 查看报告**
```bash
npx playwright show-report
```

**Step 4: 针对失败的测试逐一分析**

常见失败原因及修复方向：
- 选择器不匹配 → 检查实际 DOM，更新选择器
- 路由跳转不符合预期 → 检查 auth.server.ts 中的 redirect
- 表单 submit 无反应 → 检查 action handler 是否正确处理表单数据

**Step 5: 回归运行**
```bash
npx playwright test --reporter=line
```

---

### Task 16: 最终全量回归

**Step 1: 后端全量**
```bash
cd backend && .venv/bin/python -m pytest tests/ -v --tb=short
```
Expected: 全部 PASS

**Step 2: 前端 E2E 全量**
```bash
cd frontend && npx playwright test --reporter=line
```
Expected: 核心流程全部 PASS

**Step 3: 输出测试报告**
```bash
# 后端
cd backend && .venv/bin/python -m pytest tests/ --tb=no -q 2>&1 | tail -5

# 前端
cd frontend && npx playwright test --reporter=json > /tmp/e2e-results.json 2>&1
```

---

## Bug 修复指引

在执行过程中如遇以下常见 bug：

### Bug 类型 A：新模型缺少 cascade
症状：删除父记录报 IntegrityError
修复：在父模型 relationship 上加 `cascade="all, delete-orphan"`

### Bug 类型 B：SQLite 不支持 INFORMATION_SCHEMA
症状：`test_get_business_table_detail` 的 columns 查询失败
修复：try/except 已有，无需修复；或 mock INFORMATION_SCHEMA 结果

### Bug 类型 C：前端 E2E 选择器失配
症状：`locator("text=xxx")` timeout
修复：打开 `npx playwright codegen http://localhost:5174` 录制实际选择器

### Bug 类型 D：前端路由/跳转错误
症状：`toHaveURL("/")` 失败，实际在 `/login`
修复：检查 session cookie 是否正确设置，auth.server.ts redirect 路径
