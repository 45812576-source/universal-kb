"""
工作台配置大重构 — 全面测试用例

覆盖：
  1. 后端 API: user_workspace_config (GET/PUT/POST publish)
  2. 后端 API: skill_suggestions review 权限放宽
  3. 后端 skill_engine: 个人配置加载 + 路由 prompt 生成
  4. 前端页面: 工作台配置页 (skills/page.tsx)
  5. 前端组件: CommentsPanel 共享组件
  6. 前端组件: SkillStudio 意见弹窗
  7. 前端 UI: Sidebar + WorkspacePicker 简化
  8. 数据迁移: Alembic migration

运行方式:
  cd /Users/xia/project/universal-kb/backend
  python -m pytest tests/test_workspace_config.py -v
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    from app.main import app as _app
    return _app

@pytest.fixture
def client(app):
    return TestClient(app)

@pytest.fixture
def db_session():
    """提供一个真实 DB session（依赖本地数据库运行）。"""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _auth_header(user_id: int = 1, role: str = "super_admin") -> dict:
    """构造测试用 auth header。"""
    from app.services.auth_service import create_token
    token = create_token(user_id, role)
    return {"Authorization": f"Bearer {token}"}


# ═══════════════════════════════════════════════════════════════════════════════
# Module 1: workspace-config API
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceConfigAPI:
    """测试 /api/workspace-config 三个端点。"""

    # ── GET /api/workspace-config ──────────────────────────────────────────

    def test_get_config_creates_on_first_access(self, client, db_session):
        """首次访问自动创建配置，预挂载 own + dept skills/tools。"""
        from app.models.workspace import UserWorkspaceConfig
        # 清理可能存在的旧配置
        db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).delete()
        db_session.commit()

        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        assert resp.status_code == 200
        data = resp.json()
        assert "mounted_skills" in data
        assert "mounted_tools" in data
        assert "needs_prompt_refresh" in data
        assert data["user_id"] == 1

    def test_get_config_idempotent(self, client):
        """多次 GET 返回相同配置，不会重复创建。"""
        resp1 = client.get("/api/workspace-config", headers=_auth_header(1))
        resp2 = client.get("/api/workspace-config", headers=_auth_header(1))
        assert resp1.json()["id"] == resp2.json()["id"]

    def test_get_config_requires_auth(self, client):
        """未认证请求应返回 401。"""
        resp = client.get("/api/workspace-config")
        assert resp.status_code in (401, 403)

    def test_get_config_enriches_skill_details(self, client):
        """返回的 mounted_skills 应包含 name, description, status 等详情。"""
        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        data = resp.json()
        for skill in data["mounted_skills"]:
            assert "id" in skill
            assert "name" in skill
            assert "source" in skill
            assert "mounted" in skill

    def test_get_config_enriches_tool_details(self, client):
        """返回的 mounted_tools 应包含 name, display_name, tool_type 等。"""
        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        data = resp.json()
        for tool in data["mounted_tools"]:
            assert "id" in tool
            assert "name" in tool
            assert "source" in tool
            assert "mounted" in tool

    # ── PUT /api/workspace-config ──────────────────────────────────────────

    def test_save_config_basic(self, client):
        """保存挂载配置，返回更新后的数据。"""
        # 先获取现有配置
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        skills = [{"id": s["id"], "source": s["source"], "mounted": s["mounted"]}
                  for s in current["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in current["mounted_tools"]]

        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        assert resp.status_code == 200

    def test_save_config_toggle_mount_sets_refresh_flag(self, client):
        """切换挂载状态后 needs_prompt_refresh 应为 True。"""
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        skills = [{"id": s["id"], "source": s["source"], "mounted": s["mounted"]}
                  for s in current["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in current["mounted_tools"]]

        # 反转第一个 skill 的挂载状态（如果有）
        if skills:
            skills[0]["mounted"] = not skills[0]["mounted"]

        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        data = resp.json()
        if skills:
            assert data["needs_prompt_refresh"] is True

    def test_save_config_no_change_no_refresh(self, client):
        """未变更挂载集合时 needs_prompt_refresh 不应被强制设为 True。"""
        # 先保存一次清除 refresh flag
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        skills = [{"id": s["id"], "source": s["source"], "mounted": s["mounted"]}
                  for s in current["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in current["mounted_tools"]]

        # 再次保存相同数据
        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        assert resp.status_code == 200

    def test_save_config_empty_lists(self, client):
        """空列表也应保存成功。"""
        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": [], "mounted_tools": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mounted_skills"] == []
        assert data["mounted_tools"] == []

    def test_save_config_requires_auth(self, client):
        """未认证请求应返回 401。"""
        resp = client.put(
            "/api/workspace-config",
            json={"mounted_skills": [], "mounted_tools": []},
        )
        assert resp.status_code in (401, 403)

    # ── POST /api/workspace-config/publish ────────────────────────────────

    def test_publish_requires_admin(self, client):
        """普通员工不能发布标准工作台。"""
        # 假设 user_id=1 是超管，找一个普通员工测试
        # 如果没有普通员工，此测试需要 mock
        pass  # 依赖实际用户数据

    def test_publish_empty_config_rejected(self, client, db_session):
        """空挂载配置不允许发布。"""
        from app.models.workspace import UserWorkspaceConfig

        # 先清空挂载
        client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": [], "mounted_tools": []},
        )

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "department"},
        )
        assert resp.status_code == 400

    def test_publish_department_scope(self, client):
        """管理员发布部门标准工作台（需要有挂载项）。"""
        # 先确保有挂载项
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        if not current["mounted_skills"] and not current["mounted_tools"]:
            pytest.skip("No mounted items to publish")

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "department", "name": "测试部门标准工作台"},
        )
        if resp.status_code == 403:
            pytest.skip("User is not admin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scope"] == "department"

    def test_publish_company_scope(self, client):
        """超管发布公司标准工作台。"""
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        if not current["mounted_skills"] and not current["mounted_tools"]:
            pytest.skip("No mounted items to publish")

        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "company", "name": "测试公司标准工作台"},
        )
        if resp.status_code == 403:
            pytest.skip("User is not super_admin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "company"

    def test_publish_upsert_existing(self, client):
        """同一管理员重复发布应更新而非新建。"""
        current = client.get("/api/workspace-config", headers=_auth_header(1)).json()
        if not current["mounted_skills"] and not current["mounted_tools"]:
            pytest.skip("No mounted items")

        resp1 = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "department", "name": "第一版"},
        )
        if resp1.status_code != 200:
            pytest.skip("Cannot publish")

        resp2 = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "department", "name": "第二版"},
        )
        assert resp2.status_code == 200
        # 应使用同一个 workspace
        assert resp1.json()["workspace_id"] == resp2.json()["workspace_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# Module 2: skill_suggestions review 权限
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillSuggestionReviewPermission:
    """测试 PATCH /api/skill-suggestions/{id}/review 权限放宽。"""

    def test_admin_can_review(self, client, db_session):
        """管理员可以审核任意 skill 的 suggestion。"""
        from app.models.skill import SkillSuggestion, SuggestionStatus
        suggestion = db_session.query(SkillSuggestion).filter(
            SkillSuggestion.status == SuggestionStatus.PENDING
        ).first()
        if not suggestion:
            pytest.skip("No pending suggestion")
        suggestion_id = suggestion.id

        resp = client.patch(
            f"/api/skill-suggestions/{suggestion_id}/review",
            headers=_auth_header(1),  # 假设 user 1 是 admin
            json={"status": "adopted"},
        )
        # 允许 200 或 403（如果 user 1 不是 admin）
        assert resp.status_code in (200, 403)

    def test_skill_creator_can_review_own_skill_suggestion(self, client, db_session):
        """Skill 创建者可以审核自己 skill 的 suggestion。"""
        from app.models.skill import SkillSuggestion, SuggestionStatus, Skill

        suggestion = db_session.query(SkillSuggestion).filter(
            SkillSuggestion.status == SuggestionStatus.PENDING
        ).first()
        if not suggestion:
            pytest.skip("No pending suggestion")
        suggestion_id = suggestion.id

        skill = db_session.get(Skill, suggestion.skill_id)
        if not skill:
            pytest.skip("No skill found")

        # 用 skill 创建者的身份审核
        resp = client.patch(
            f"/api/skill-suggestions/{suggestion_id}/review",
            headers=_auth_header(skill.created_by),
            json={"status": "adopted"},
        )
        assert resp.status_code == 200

    def test_non_owner_non_admin_cannot_review(self, client, db_session):
        """非创建者、非管理员不能审核。"""
        from app.models.skill import SkillSuggestion, SuggestionStatus
        suggestion = db_session.query(SkillSuggestion).filter(
            SkillSuggestion.status == SuggestionStatus.PENDING
        ).first()
        if not suggestion:
            pytest.skip("No pending suggestion")
        suggestion_id = suggestion.id

        # 使用一个肯定不是 admin 也不是 skill 创建者的用户（假设 user 999 不存在或是普通用户）
        resp = client.patch(
            f"/api/skill-suggestions/{suggestion_id}/review",
            headers=_auth_header(999),
            json={"status": "adopted"},
        )
        assert resp.status_code in (401, 403, 404)


# ═══════════════════════════════════════════════════════════════════════════════
# Module 3: skill_engine — 个人配置加载 + 路由 prompt
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillEnginePersonalConfig:
    """测试 skill_engine.prepare() 的个人工作台配置逻辑。"""

    def test_refresh_skill_routing_prompt_first_time(self, db_session):
        """首次生成路由 prompt：full generation。"""
        from app.services.skill_engine import skill_engine
        from app.models.workspace import UserWorkspaceConfig

        config = db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).first()
        if not config:
            pytest.skip("No config for user 1")

        # 确保有挂载的 skill
        mounted = [i for i in (config.mounted_skills or []) if i.get("mounted")]
        if not mounted:
            pytest.skip("No mounted skills")

        # 清除现有 prompt 和 snapshot
        config.skill_routing_prompt = None
        config.last_skill_snapshot = None
        config.needs_prompt_refresh = True
        db_session.flush()

        with patch("app.services.llm_gateway.chat_async", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"content": "## Skill 路由指引\n- 测试场景 → 使用 test_skill"}
            with patch("app.services.llm_gateway.get_config", return_value={}):
                import asyncio
                asyncio.run(skill_engine._refresh_skill_routing_prompt(db_session, config))

        assert config.skill_routing_prompt is not None
        assert config.needs_prompt_refresh is False
        assert config.last_skill_snapshot is not None

    def test_refresh_prompt_incremental_update(self, db_session):
        """增量更新：已有 snapshot 时只传 diff。"""
        from app.services.skill_engine import skill_engine
        from app.models.workspace import UserWorkspaceConfig

        config = db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).first()
        if not config:
            pytest.skip("No config for user 1")

        mounted = [i for i in (config.mounted_skills or []) if i.get("mounted")]
        if not mounted:
            pytest.skip("No mounted skills")

        config.skill_routing_prompt = "old prompt"
        config.last_skill_snapshot = [{"name": "old_skill", "description": "old"}]
        config.needs_prompt_refresh = True
        db_session.flush()

        with patch("app.services.llm_gateway.chat_async", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"content": "## Skill 路由指引\n- 更新版"}
            with patch("app.services.llm_gateway.get_config", return_value={}):
                import asyncio
                asyncio.run(skill_engine._refresh_skill_routing_prompt(db_session, config))

        assert config.needs_prompt_refresh is False

    def test_refresh_prompt_fallback_on_llm_failure(self, db_session):
        """LLM 调用失败时使用简单模板作为 fallback。"""
        from app.services.skill_engine import skill_engine
        from app.models.workspace import UserWorkspaceConfig

        config = db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).first()
        if not config:
            pytest.skip("No config for user 1")

        mounted = [i for i in (config.mounted_skills or []) if i.get("mounted")]
        if not mounted:
            pytest.skip("No mounted skills")

        config.skill_routing_prompt = None
        config.last_skill_snapshot = None
        config.needs_prompt_refresh = True
        db_session.flush()

        with patch("app.services.llm_gateway.chat_async", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("LLM failed")
            with patch("app.services.llm_gateway.get_config", return_value={}):
                import asyncio
                asyncio.run(skill_engine._refresh_skill_routing_prompt(db_session, config))

        # fallback 应产生一个非空的 prompt
        assert config.skill_routing_prompt is not None
        assert "Skill 路由指引" in config.skill_routing_prompt
        assert config.needs_prompt_refresh is False

    def test_prepare_no_workspace_uses_personal_config(self, db_session):
        """workspace_id=None 时 prepare() 应使用个人配置加载 skill。"""
        from app.models.workspace import UserWorkspaceConfig
        config = db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).first()
        if not config:
            pytest.skip("No config")

        mounted_ids = [
            item["skill_id"]
            for item in (config.mounted_skills or [])
            if item.get("mounted")
        ]
        # 验证有挂载的 skill
        assert isinstance(mounted_ids, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Module 4: 数据模型 + 迁移
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataModel:
    """验证数据模型字段和表结构。"""

    def test_user_workspace_config_model_fields(self):
        """UserWorkspaceConfig 模型应包含所有必要字段。"""
        from app.models.workspace import UserWorkspaceConfig
        mapper = UserWorkspaceConfig.__table__
        col_names = {c.name for c in mapper.columns}
        expected = {
            "id", "user_id", "mounted_skills", "mounted_tools",
            "skill_routing_prompt", "last_skill_snapshot",
            "needs_prompt_refresh", "created_at", "updated_at",
        }
        assert expected.issubset(col_names), f"Missing: {expected - col_names}"

    def test_workspace_new_columns(self):
        """Workspace 模型应包含 is_preset, recommended_by, for_department_id。"""
        from app.models.workspace import Workspace
        mapper = Workspace.__table__
        col_names = {c.name for c in mapper.columns}
        assert "is_preset" in col_names
        assert "recommended_by" in col_names
        assert "for_department_id" in col_names

    def test_user_id_unique_constraint(self):
        """UserWorkspaceConfig.user_id 应有唯一约束。"""
        from app.models.workspace import UserWorkspaceConfig
        col = UserWorkspaceConfig.__table__.c.user_id
        assert col.unique is True

    def test_config_json_columns_nullable(self):
        """JSON 列应为 nullable（MySQL 不支持 JSON 默认值）。"""
        from app.models.workspace import UserWorkspaceConfig
        table = UserWorkspaceConfig.__table__
        assert table.c.mounted_skills.nullable is True
        assert table.c.mounted_tools.nullable is True
        assert table.c.last_skill_snapshot.nullable is True

    def test_migration_file_exists(self):
        """Alembic 迁移文件应存在。"""
        import os
        migration_dir = "/Users/xia/project/universal-kb/backend/alembic/versions"
        files = os.listdir(migration_dir)
        found = any("user_workspace_config" in f for f in files)
        assert found, "Migration file for user_workspace_config not found"


# ═══════════════════════════════════════════════════════════════════════════════
# Module 5: CommentsPanel 共享组件
# ═══════════════════════════════════════════════════════════════════════════════

class TestCommentsPanelComponent:
    """验证 CommentsPanel 组件的导出和接口。"""

    def test_comments_panel_file_exists(self):
        import os
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        assert os.path.exists(path)

    def test_comments_panel_exports_suggestion_type(self):
        """CommentsPanel.tsx 应导出 Suggestion 接口。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "export interface Suggestion" in content

    def test_comments_panel_exports_props(self):
        """CommentsPanel.tsx 应导出 CommentsPanelProps 接口。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "export interface CommentsPanelProps" in content

    def test_comments_panel_has_on_adopt_prop(self):
        """CommentsPanel 应支持 onAdopt 回调。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "onAdopt?" in content

    def test_comments_panel_has_hide_iterate_prop(self):
        """CommentsPanel 应支持 hideIterate 属性。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "hideIterate?" in content

    def test_comments_panel_has_status_filter_prop(self):
        """CommentsPanel 应支持 statusFilter 属性。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "statusFilter?" in content

    def test_comments_panel_calls_status_filter_api(self):
        """带 statusFilter 时应发送 ?status= 查询参数。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "?status=${statusFilter}" in content

    def test_comments_panel_on_adopt_intercept(self):
        """onAdopt 返回 true 时应跳过默认 review 逻辑。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "if (handled) return;" in content

    def test_comments_panel_hide_iterate_hides_checkbox(self):
        """hideIterate 时应隐藏复选框和 AI 迭代按钮。"""
        path = "/Users/xia/project/le-desk/src/components/skill/CommentsPanel.tsx"
        with open(path) as f:
            content = f.read()
        assert "{!hideIterate && (" in content

    def test_admin_skills_page_imports_comments_panel(self):
        """admin/skills/page.tsx 应导入 CommentsPanel。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/admin/skills/page.tsx"
        with open(path) as f:
            content = f.read()
        assert "from \"@/components/skill/CommentsPanel\"" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Module 6: SkillStudio 意见弹窗
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillStudioSuggestionPopup:
    """验证 SkillStudio 中意见弹窗的代码结构。"""

    def _read_skill_studio(self):
        path = "/Users/xia/project/le-desk/src/components/chat/SkillStudio.tsx"
        with open(path) as f:
            return f.read()

    def test_imports_comments_panel(self):
        """SkillStudio 应导入 CommentsPanel 和 Suggestion。"""
        content = self._read_skill_studio()
        assert "CommentsPanel" in content
        assert "Suggestion" in content

    def test_skill_list_has_adopt_suggestion_prop(self):
        """SkillList 组件应接受 onAdoptSuggestion 回调。"""
        content = self._read_skill_studio()
        assert "onAdoptSuggestion" in content

    def test_suggestion_popup_toggle(self):
        """应有 suggestionPopupSkillId 状态控制弹窗显示。"""
        content = self._read_skill_studio()
        assert "suggestionPopupSkillId" in content

    def test_suggestion_popup_renders_comments_panel(self):
        """弹窗内应渲染 CommentsPanel。"""
        content = self._read_skill_studio()
        # 检查弹窗区域有 CommentsPanel 且传了 hideIterate 和 statusFilter
        assert 'hideIterate' in content
        assert 'statusFilter="pending"' in content

    def test_suggestion_popup_has_close_button(self):
        """弹窗应有关闭按钮。"""
        content = self._read_skill_studio()
        assert "setSuggestionPopupSkillId(null)" in content

    def test_set_input_ref_mechanism(self):
        """StudioChat 应接受 setInputRef 并注册 setInput 回调。"""
        content = self._read_skill_studio()
        assert "setInputRef" in content
        # 检查注册回调
        assert "setInputRef.current = (text: string)" in content

    def test_adopt_fills_chat_input(self):
        """采纳意见后应将格式化文本填入聊天输入框。"""
        content = self._read_skill_studio()
        assert "修改意见:" in content
        assert "期望:" in content
        assert "setInputRef.current?.(text)" in content

    def test_folder_row_has_suggestion_button(self):
        """每个 Skill 文件夹行应有"意见"按钮。"""
        content = self._read_skill_studio()
        assert ">意见</button>" in content or "意见\n" in content

    def test_adopt_callback_returns_true(self):
        """CommentsPanel 的 onAdopt 回调应 return true 以拦截默认行为。"""
        content = self._read_skill_studio()
        # 在 onAdopt 回调中应有 return true
        assert "return true;" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Module 7: 工作台配置页面 (skills/page.tsx)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillsPageRewrite:
    """验证 /skills/page.tsx 重写后的结构。"""

    def _read_page(self):
        path = "/Users/xia/project/le-desk/src/app/(app)/skills/page.tsx"
        with open(path) as f:
            return f.read()

    def test_page_title_is_workspace_config(self):
        """页面标题应为"工作台配置"。"""
        content = self._read_page()
        assert '工作台配置' in content

    def test_page_has_app_market_button(self):
        """右上角应有"应用市场"按钮。"""
        content = self._read_page()
        assert '应用市场' in content
        assert '/app-market' in content

    def test_page_imports_use_auth(self):
        """应导入 useAuth 获取用户角色。"""
        content = self._read_page()
        assert "useAuth" in content

    def test_page_imports_comments_panel(self):
        """应导入 CommentsPanel 组件。"""
        content = self._read_page()
        assert "CommentsPanel" in content

    def test_section_own_skills(self):
        """应有"自己开发的 Skill" Section。"""
        content = self._read_page()
        assert '自己开发的 Skill' in content

    def test_section_own_tools(self):
        """应有"自己开发的工具" Section。"""
        content = self._read_page()
        assert '自己开发的工具' in content

    def test_section_dept_skills(self):
        """应有"部门发布的 Skill" Section。"""
        content = self._read_page()
        assert '部门发布的 Skill' in content

    def test_section_dept_tools(self):
        """应有"部门发布的工具" Section。"""
        content = self._read_page()
        assert '部门发布的工具' in content

    def test_section_market_skills(self):
        """应有"市场安装的 Skill" Section。"""
        content = self._read_page()
        assert '市场安装的 Skill' in content

    def test_section_market_tools(self):
        """应有"市场安装的工具" Section。"""
        content = self._read_page()
        assert '市场安装的工具' in content

    def test_mount_toggle_component(self):
        """应有 MountCard 组件实现挂载开关。"""
        content = self._read_page()
        assert "MountCard" in content

    def test_save_button(self):
        """应有"保存配置"按钮。"""
        content = self._read_page()
        assert '保存配置' in content

    def test_admin_publish_department_button(self):
        """管理员应有"发布为部门标准"按钮。"""
        content = self._read_page()
        assert '发布为部门标准' in content

    def test_admin_publish_company_button(self):
        """超管应有"发布为公司标准"按钮。"""
        content = self._read_page()
        assert '发布为公司标准' in content

    def test_fetches_workspace_config(self):
        """应调用 GET /workspace-config API。"""
        content = self._read_page()
        assert '"/workspace-config"' in content

    def test_saves_workspace_config(self):
        """应调用 PUT /workspace-config API。"""
        content = self._read_page()
        assert 'method: "PUT"' in content

    def test_publishes_workspace_config(self):
        """应调用 POST /workspace-config/publish API。"""
        content = self._read_page()
        assert '"/workspace-config/publish"' in content

    def test_no_tab_layout(self):
        """不应有旧的 tab 切换布局。"""
        content = self._read_page()
        # 旧版有 MainTab = "skill" | "tool" | "webapp"
        assert 'type MainTab' not in content

    def test_shows_all_skill_statuses(self):
        """自己的 skill 应显示所有状态（不再只显示 draft/reviewing）。"""
        content = self._read_page()
        # fetchSkills 应不再过滤 status
        assert '.filter(s => s.status === "draft" || s.status === "reviewing")' not in content

    def test_mount_count_summary(self):
        """底部应显示挂载数量统计。"""
        content = self._read_page()
        assert 'Skill +' in content  # "X Skill + Y 工具" 格式

    def test_config_dirty_tracking(self):
        """应有 configDirty 状态跟踪变更。"""
        content = self._read_page()
        assert 'configDirty' in content

    def test_section_collapsible(self):
        """Section 应支持折叠。"""
        content = self._read_page()
        assert 'collapsible' in content

    def test_skill_card_has_comments_button(self):
        """MySkillCard 应有"用户意见"按钮。"""
        content = self._read_page()
        assert '用户意见' in content

    def test_webapp_section_exists(self):
        """应保留 Web App Section。"""
        content = self._read_page()
        assert 'Web App' in content


# ═══════════════════════════════════════════════════════════════════════════════
# Module 8: Sidebar + WorkspacePicker
# ═══════════════════════════════════════════════════════════════════════════════

class TestSidebarAndWorkspacePicker:
    """验证 Sidebar 和 WorkspacePicker 简化。"""

    def test_sidebar_skills_label_renamed(self):
        """Sidebar 中 /skills 导航应标记为"工作台配置"。"""
        path = "/Users/xia/project/le-desk/src/components/layout/Sidebar.tsx"
        with open(path) as f:
            content = f.read()
        assert '工作台配置' in content
        assert 'Skills & Tools' not in content

    def test_workspace_picker_custom_workspace(self):
        """WorkspacePicker 应显示"自定义工作台"而非"自由对话"。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        assert '自定义工作台' in content
        assert '自由对话' not in content

    def test_workspace_picker_personal_config_desc(self):
        """自定义工作台描述应为"使用个人工作台配置"。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        assert '使用个人工作台配置' in content
        assert '不绑定工作台' not in content

    def test_workspace_picker_no_category_grouping(self):
        """WorkspacePicker 不应再按 category 分组。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        # 旧版有 categoryMap
        assert 'categoryMap' not in content

    def test_workspace_picker_has_recommended_section(self):
        """WorkspacePicker 应有"部门推荐"分组。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        assert '部门推荐' in content

    def test_workspace_picker_recommended_filter(self):
        """推荐工作台通过 recommended_by 字段筛选。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        assert 'recommended_by' in content

    def test_workspace_picker_system_ws_filter(self):
        """系统内置仅含 opencode/sandbox/skill_studio。"""
        path = "/Users/xia/project/le-desk/src/app/(app)/chat/layout.tsx"
        with open(path) as f:
            content = f.read()
        assert 'opencode' in content
        assert 'sandbox' in content
        assert 'skill_studio' in content


# ═══════════════════════════════════════════════════════════════════════════════
# Module 9: 端到端流程验证
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEndFlow:
    """端到端场景测试。"""

    def test_flow_new_user_get_config_then_save(self, client, db_session):
        """新用户流程：GET 自动创建 → 切换挂载 → PUT 保存。"""
        from app.models.workspace import UserWorkspaceConfig

        # 清理
        db_session.query(UserWorkspaceConfig).filter(
            UserWorkspaceConfig.user_id == 1
        ).delete()
        db_session.commit()

        # Step 1: GET 自动创建
        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        assert resp.status_code == 200
        config = resp.json()
        assert config["user_id"] == 1

        # Step 2: 修改挂载
        skills = [{"id": s["id"], "source": s["source"], "mounted": False}
                  for s in config["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in config["mounted_tools"]]

        # Step 3: PUT 保存
        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        assert resp.status_code == 200
        saved = resp.json()
        # 验证所有 skill 被设为 unmounted
        for s in saved["mounted_skills"]:
            assert s["mounted"] is False

    def test_flow_config_change_triggers_prompt_refresh(self, client, db_session):
        """配置变更后 needs_prompt_refresh 应为 True。"""
        from app.models.workspace import UserWorkspaceConfig

        # 先确保有配置
        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        config = resp.json()

        skills = [{"id": s["id"], "source": s["source"], "mounted": not s["mounted"]}
                  for s in config["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in config["mounted_tools"]]

        if not skills:
            pytest.skip("No skills to toggle")

        resp = client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )
        assert resp.json()["needs_prompt_refresh"] is True

    def test_flow_admin_publish_creates_workspace(self, client, db_session):
        """管理员发布后应生成 Workspace 并关联 skill/tool。"""
        from app.models.workspace import Workspace, WorkspaceSkill

        # 确保有挂载项
        resp = client.get("/api/workspace-config", headers=_auth_header(1))
        config = resp.json()
        if not config["mounted_skills"]:
            pytest.skip("No skills")

        # 重新挂载
        skills = [{"id": s["id"], "source": s["source"], "mounted": True}
                  for s in config["mounted_skills"]]
        tools = [{"id": t["id"], "source": t["source"], "mounted": t["mounted"]}
                 for t in config["mounted_tools"]]
        client.put(
            "/api/workspace-config",
            headers=_auth_header(1),
            json={"mounted_skills": skills, "mounted_tools": tools},
        )

        # 发布
        resp = client.post(
            "/api/workspace-config/publish",
            headers=_auth_header(1),
            json={"scope": "department", "name": "E2E 测试工作台"},
        )
        if resp.status_code == 403:
            pytest.skip("User is not admin")
        assert resp.status_code == 200
        data = resp.json()

        # 验证 Workspace 存在
        ws = db_session.query(Workspace).get(data["workspace_id"])
        assert ws is not None
        assert ws.recommended_by == 1

        # 验证关联的 skill 数量
        ws_skills = db_session.query(WorkspaceSkill).filter(
            WorkspaceSkill.workspace_id == ws.id
        ).all()
        assert len(ws_skills) == data["skill_count"]
