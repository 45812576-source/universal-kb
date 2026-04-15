"""Studio 后端能力模块测试 — rename / route / audit / governance / staged edit。"""
import asyncio
import datetime as dt
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.conftest import (
    TestingSessionLocal, _make_dept, _make_user, _make_model_config,
    _make_skill, _login, _auth,
)
from app.models.user import Role
from app.models.skill import (
    Skill, SkillVersion, SkillFolderAlias, SkillAuditResult, StagedEdit, SkillStatus,
)
from app.models.event_bus import UnifiedEvent


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def seeded(db):
    """创建部门、admin、model_config、skill，返回 dict。"""
    dept = _make_dept(db)
    admin = _make_user(db, "studio_admin", Role.SUPER_ADMIN, dept.id)
    mc = _make_model_config(db)
    skill = _make_skill(db, admin.id, name="测试技能Alpha")
    # 给 skill 设置 folder_key
    skill.folder_key = f"skill-{skill.id}"
    db.commit()
    return {"dept": dept, "admin": admin, "mc": mc, "skill": skill}


@pytest.fixture
def token(client, seeded):
    return _login(client, "studio_admin")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Rename
# ═══════════════════════════════════════════════════════════════════════════════

class TestRename:
    def test_rename_with_folder_sync(self, client, token, seeded):
        """rename_folder=true: name + folder_key 都变，旧 folder_key 保留为 alias。"""
        skill = seeded["skill"]
        old_fk = skill.folder_key

        resp = client.patch(
            f"/api/skills/{skill.id}/rename",
            json={"display_name": "新名称Beta", "rename_folder": True},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "新名称Beta"
        assert data["folder_synced"] is True
        assert data["folder_key"] != old_fk
        assert data["previous_folder_key"] == old_fk

        # 验证 alias 已写入
        db = TestingSessionLocal()
        alias = db.query(SkillFolderAlias).filter(
            SkillFolderAlias.old_folder_key == old_fk
        ).first()
        assert alias is not None
        assert alias.skill_id == skill.id
        db.close()

    def test_rename_without_folder_sync(self, client, token, seeded):
        """rename_folder=false: 只改 name，folder_key 不变，不产生 alias。"""
        skill = seeded["skill"]
        old_fk = skill.folder_key

        resp = client.patch(
            f"/api/skills/{skill.id}/rename",
            json={"display_name": "仅改名字", "rename_folder": False},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "仅改名字"
        assert data["folder_synced"] is False
        assert data["folder_key"] == old_fk

    def test_rename_conflict(self, client, token, seeded, db):
        """名称冲突时返回 400。"""
        admin = seeded["admin"]
        _make_skill(db, admin.id, name="已存在的名称")
        db.commit()

        resp = client.patch(
            f"/api/skills/{seeded['skill'].id}/rename",
            json={"display_name": "已存在的名称"},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "同名" in resp.text

    def test_rename_empty_name(self, client, token, seeded):
        """空名称返回 400。"""
        resp = client.patch(
            f"/api/skills/{seeded['skill'].id}/rename",
            json={"display_name": "   "},
            headers=_auth(token),
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Route（服务级测试）
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoute:
    def test_route_no_skill(self, db):
        """无 skill_id → create_new_skill，含 brainstorming + mckinsey。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=None, user_message="我想做一个新的营销技能")
        assert r.session_mode == "create_new_skill"
        assert "brainstorming" in r.active_assist_skills
        assert "mckinsey" in r.active_assist_skills
        assert r.next_action == "collect_requirements"

    def test_route_imported_skill(self, db, seeded):
        """imported skill → audit_imported_skill。"""
        from app.services.studio_router import route_session
        skill = seeded["skill"]
        skill.source_type = "imported"
        db.commit()

        r = route_session(db, skill_id=skill.id, user_message="看看这个技能怎么样")
        assert r.session_mode == "audit_imported_skill"
        assert "mckinsey" in r.active_assist_skills
        assert "quality_audit" in r.active_assist_skills
        assert r.next_action == "run_audit"

    def test_route_existing_skill(self, db, seeded):
        """普通已有 skill → optimize_existing_skill，含 prompt_optimizer + mckinsey。"""
        from app.services.studio_router import route_session
        # 确保 system_prompt 足够长（>50 字符），避免被归类为 empty_or_minimal
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        v.system_prompt = "你是专业的营销分析助手，擅长用麦肯锡 MECE 框架拆解消费者洞察，并生成结构化报告。"
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我改改这个")
        assert r.session_mode == "optimize_existing_skill"
        assert "prompt_optimizer" in r.active_assist_skills
        assert "mckinsey" in r.active_assist_skills

    def test_route_user_intent_audit(self, db, seeded):
        """用户消息含审计关键词 → 走 audit 路径。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我审计一下这个技能")
        assert r.session_mode == "audit_imported_skill"
        assert r.next_action == "run_audit"
        assert r.route_reason == "user_intent_audit"

    def test_route_user_intent_create(self, db, seeded):
        """用户消息含创建关键词 → 走 create 路径（即使有 skill_id）。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=seeded["skill"].id, user_message="我想从零新建一个")
        assert r.session_mode == "create_new_skill"
        assert r.route_reason == "user_intent_create"

    def test_route_empty_skill_redirect_to_create(self, db, seeded):
        """skill 无 system_prompt → 引导 create/brainstorming。"""
        from app.services.studio_router import route_session
        # 清空 system_prompt
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        v.system_prompt = ""
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="")
        assert r.session_mode == "create_new_skill"
        assert r.route_reason == "empty_or_minimal_skill"


class TestWorkflowOrchestrator:
    @pytest.mark.asyncio
    async def test_bootstrap_workflow_architect_mode(self, db, seeded):
        """新建场景 bootstrap → 返回统一 workflow_state，并初始化 architect 状态。"""
        from app.models.conversation import Conversation
        from app.models.skill import ArchitectWorkflowState
        from app.services.studio_workflow_orchestrator import bootstrap_workflow

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        result = await bootstrap_workflow(
            db,
            workflow_id="run_test_architect",
            conversation_id=conv.id,
            skill_id=None,
            user_message="我想做一个新的营销分析技能",
        )

        assert result.workflow_state["session_mode"] == "create_new_skill"
        assert result.workflow_state["workflow_mode"] == "architect_mode"
        assert result.workflow_state["phase"] == "phase_1_why"
        assert result.workflow_state["next_action"] == "collect_requirements"
        assert result.workflow_state["complexity_level"] == "medium"
        assert result.workflow_state["execution_strategy"] == "fast_then_deep"
        assert result.workflow_state["metadata"]["latency"]["request_accepted_at"]
        assert result.workflow_state["metadata"]["latency"]["classified_at"]
        assert result.architect_phase_status is not None
        assert result.route_status["workflow_mode"] == "architect_mode"
        assert result.route_status["complexity_level"] == "medium"
        assert "skill-architect-master" in result.assist_skills_status["skills"]

        arch_state = db.query(ArchitectWorkflowState).filter(
            ArchitectWorkflowState.conversation_id == conv.id
        ).first()
        assert arch_state is not None
        assert arch_state.workflow_phase == "phase_1_why"

    @pytest.mark.asyncio
    async def test_bootstrap_workflow_applies_user_rollout_flags(self, db, seeded):
        """用户级 flag 可以关闭 deep/patch，并写入 workflow metadata。"""
        from app.models.conversation import Conversation
        from app.services.studio_workflow_orchestrator import bootstrap_workflow

        admin = seeded["admin"]
        admin.feature_flags = {
            "skill_studio_deep_lane_enabled": False,
            "skill_studio_patch_protocol_enabled": False,
        }
        conv = Conversation(user_id=admin.id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        result = await bootstrap_workflow(
            db,
            workflow_id="run_rollout_flags",
            conversation_id=conv.id,
            skill_id=None,
            user_message="我想做一个新的营销分析技能",
            user_id=admin.id,
        )

        rollout = result.workflow_state["metadata"]["rollout"]
        assert rollout["flags"]["deep_lane_enabled"] is False
        assert rollout["flags"]["patch_protocol_enabled"] is False
        assert result.workflow_state["execution_strategy"] == "fast_only"
        assert result.workflow_state["deep_status"] == "not_requested"
        assert result.route_status["execution_strategy"] == "fast_only"

    @pytest.mark.asyncio
    async def test_bootstrap_workflow_audit_promotes_to_review_cards(self, db, seeded):
        """审计 bootstrap → 自动运行 audit/governance，并把 next_action 提升为 review_cards。"""
        from app.models.conversation import Conversation
        from app.services.studio_workflow_orchestrator import bootstrap_workflow

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        skill = seeded["skill"]
        skill.source_type = "imported"
        db.commit()
        db.refresh(conv)

        audit_result = SimpleNamespace(
            verdict="needs_work",
            issues=[{"severity": "high", "category": "structure", "description": "缺少角色定义"}],
            recommended_path="minor_edit",
            audit_id=42,
        )
        governance_result = SimpleNamespace(
            cards=[{"id": "card_1", "type": "staged_edit", "title": "补充角色定义", "content": {"summary": "补充系统角色"}}],
            staged_edits=[{"id": "9", "target_type": "system_prompt", "summary": "添加角色定义", "diff_ops": [], "risk_level": "low", "status": "pending"}],
        )

        with patch("app.services.studio_auditor.run_audit", new=AsyncMock(return_value=audit_result)), patch(
            "app.services.studio_governance.generate_governance_actions",
            new=AsyncMock(return_value=governance_result),
        ):
            result = await bootstrap_workflow(
                db,
                workflow_id="run_test_audit",
                conversation_id=conv.id,
                skill_id=skill.id,
                user_message="帮我审计一下这个导入 skill",
            )

        assert result.workflow_state["session_mode"] == "audit_imported_skill"
        assert result.workflow_state["phase"] == "review"
        assert result.workflow_state["next_action"] == "review_cards"
        assert result.workflow_state["complexity_level"] == "high"
        assert result.workflow_state["execution_strategy"] == "fast_then_deep"
        assert result.route_status["next_action"] == "review_cards"
        assert result.route_status["deep_status"] == "pending"
        assert result.audit_summary is not None
        assert result.audit_summary["audit_id"] == 42
        assert len(result.cards) == 1
        assert len(result.staged_edits) == 1

    @pytest.mark.asyncio
    async def test_bootstrap_workflow_reuses_existing_recovery_without_rerunning_audit(self, db, seeded):
        from app.models.conversation import Conversation
        from app.services import skill_memo_service
        from app.services.studio_workflow_orchestrator import bootstrap_workflow

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        skill = seeded["skill"]
        skill.source_type = "imported"
        db.commit()
        db.refresh(conv)

        skill_memo_service.sync_workflow_recovery(
            db,
            skill.id,
            workflow_state={
                "workflow_id": "run_existing",
                "conversation_id": conv.id,
                "skill_id": skill.id,
                "session_mode": "audit_imported_skill",
                "workflow_mode": "none",
                "phase": "review",
                "next_action": "review_cards",
                "route_reason": "imported_skill",
                "active_assist_skills": ["mckinsey", "quality_audit"],
            },
            cards=[{
                "id": "reuse_card_1",
                "type": "staged_edit",
                "title": "已有整改卡片",
                "status": "pending",
                "content": {"summary": "复用已有整改卡片"},
            }],
            staged_edits=[{
                "id": "reuse_edit_1",
                "target_type": "system_prompt",
                "summary": "复用已有 staged edit",
                "diff_ops": [],
                "risk_level": "low",
                "status": "pending",
            }],
            user_id=seeded["admin"].id,
            commit=True,
        )

        with patch("app.services.studio_auditor.run_audit", new=AsyncMock(side_effect=AssertionError("should_not_rerun_audit"))):
            result = await bootstrap_workflow(
                db,
                workflow_id="run_recovered",
                conversation_id=conv.id,
                skill_id=skill.id,
                user_message="继续处理上一个整改项",
                user_id=seeded["admin"].id,
            )

        assert result.workflow_state["workflow_id"] == "run_recovered"
        assert result.workflow_state["next_action"] == "review_cards"
        assert result.workflow_state["complexity_level"] == "medium"
        assert result.route_status["next_action"] == "review_cards"
        assert result.cards[0]["id"] == "reuse_card_1"
        assert result.staged_edits[0]["id"] == "reuse_edit_1"

    def test_bootstrap_preflight_remediation_builds_workflow_state(self, db, seeded):
        """preflight remediation 也走统一 workflow_state 返回面。"""
        from app.services.studio_workflow_orchestrator import bootstrap_preflight_remediation

        result = bootstrap_preflight_remediation(
            db,
            workflow_id="preflight-1",
            skill_id=seeded["skill"].id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "missing_description", "issue": "description 为空"}],
                }],
            },
        )

        assert result.workflow_state["workflow_mode"] == "preflight_remediation"
        assert result.workflow_state["phase"] == "remediate"
        assert result.workflow_state["next_action"] == "review_cards"
        assert result.workflow_state["execution_strategy"] == "deep_resume"
        assert result.workflow_state["metadata"]["source"] == "preflight_remediation"
        assert len(result.cards) == 1
        assert result.cards[0]["source"] == "preflight_remediation"
        assert len(result.staged_edits) == 1
        assert result.staged_edits[0]["source"] == "preflight_remediation"

    @pytest.mark.asyncio
    async def test_bootstrap_sandbox_remediation_builds_workflow_state(self, db, seeded):
        """sandbox remediation 也走统一 workflow_state 返回面。"""
        from app.services.studio_workflow_orchestrator import bootstrap_sandbox_remediation

        governance_result = SimpleNamespace(
            cards=[{
                "id": "sandbox_card_1",
                "type": "followup_prompt",
                "title": "补充工具治理",
                "content": {"summary": "同步工具绑定"},
                "status": "pending",
                "actions": [{"label": "一键处理", "type": "adopt"}],
            }],
            staged_edits=[{
                "id": "11",
                "target_type": "system_prompt",
                "target_key": None,
                "summary": "补充沙盒整改要求",
                "risk_level": "medium",
                "diff_ops": [],
                "status": "pending",
            }],
        )

        with patch(
            "app.services.sandbox_governance.build_sandbox_report_governance",
            new=AsyncMock(return_value=governance_result),
        ):
            result = await bootstrap_sandbox_remediation(
                db,
                workflow_id="sandbox-1",
                skill_id=seeded["skill"].id,
                report=SimpleNamespace(id=88),
            )

        assert result.workflow_state["workflow_mode"] == "sandbox_remediation"
        assert result.workflow_state["phase"] == "remediate"
        assert result.workflow_state["next_action"] == "review_cards"
        assert result.workflow_state["execution_strategy"] == "deep_resume"
        assert result.workflow_state["metadata"]["report_id"] == 88
        assert len(result.cards) == 1
        assert result.cards[0]["source"] == "sandbox_remediation"
        assert len(result.staged_edits) == 1
        assert result.staged_edits[0]["source"] == "sandbox_remediation"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Audit（接口级测试，mock LLM）
# ═══════════════════════════════════════════════════════════════════════════════

class TestAudit:
    @pytest.mark.asyncio
    async def test_audit_returns_structured_result(self, db, seeded):
        """审计引擎正确解析 LLM 返回并持久化。"""
        from app.services.studio_auditor import run_audit

        mock_response = (
            '{"verdict": "needs_work", "issues": [{"severity": "high", '
            '"category": "structure", "description": "缺少角色定义"}], '
            '"recommended_path": "minor_edit"}'
        )

        with patch.object(
            __import__("app.services.llm_gateway", fromlist=["llm_gateway"]).llm_gateway,
            "chat",
            new=AsyncMock(return_value=(mock_response, {})),
        ):
            result = await run_audit(db, seeded["skill"].id)

        assert result.verdict == "needs_work"
        assert len(result.issues) == 1
        assert result.issues[0]["severity"] == "high"
        assert result.recommended_path == "minor_edit"

        # 验证持久化
        row = db.query(SkillAuditResult).filter(
            SkillAuditResult.skill_id == seeded["skill"].id
        ).first()
        assert row is not None
        assert row.quality_verdict == "needs_work"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Governance + Staged Edit
# ═══════════════════════════════════════════════════════════════════════════════

class TestGovernance:
    @pytest.mark.asyncio
    async def test_generate_governance_actions(self, db, seeded):
        """治理引擎生成 cards + staged edits 并持久化。"""
        from app.services.studio_governance import generate_governance_actions

        # 先创建一个 audit result
        audit_row = SkillAuditResult(
            skill_id=seeded["skill"].id,
            quality_verdict="needs_work",
            issues=[{"severity": "high", "category": "structure", "description": "缺少角色定义"}],
            recommended_path="minor_edit",
        )
        db.add(audit_row)
        db.commit()
        db.refresh(audit_row)

        mock_response = (
            '{"cards": [{"title": "补充角色定义", "description": "在 prompt 开头添加角色声明", '
            '"severity": "high", "category": "structure", "suggested_action": "staged_edit"}], '
            '"staged_edits": [{"target_type": "system_prompt", "target_key": null, '
            '"summary": "添加角色定义", "risk_level": "low", '
            '"diff_ops": [{"op": "insert", "old": "", "new": "你是一个专业的营销助手。\\n"}]}]}'
        )

        with patch.object(
            __import__("app.services.llm_gateway", fromlist=["llm_gateway"]).llm_gateway,
            "chat",
            new=AsyncMock(return_value=(mock_response, {})),
        ):
            result = await generate_governance_actions(
                db, seeded["skill"].id, audit_id=audit_row.id,
            )

        assert len(result.cards) == 1
        assert result.cards[0]["severity"] == "high"
        assert len(result.staged_edits) == 1
        assert result.staged_edits[0]["status"] == "pending"

        # 验证持久化
        se = db.query(StagedEdit).filter(
            StagedEdit.skill_id == seeded["skill"].id
        ).first()
        assert se is not None
        assert se.status == "pending"


class TestStagedEditAdoptReject:
    def _create_staged_edit(self, db, skill_id, target_type="system_prompt", diff_ops=None):
        se = StagedEdit(
            skill_id=skill_id,
            target_type=target_type,
            diff_ops=diff_ops or [{"op": "replace", "old": "你是测试助手。", "new": "你是高级测试助手。"}],
            summary="升级角色定义",
            risk_level="low",
            status="pending",
        )
        db.add(se)
        db.commit()
        db.refresh(se)
        return se

    def test_adopt_system_prompt_replace(self, db, seeded):
        """adopt replace op → 新版本 system_prompt 已修改。"""
        from app.services.studio_governance import adopt_staged_edit

        se = self._create_staged_edit(db, seeded["skill"].id)
        result = adopt_staged_edit(db, se.id, seeded["admin"].id)

        assert result["ok"] is True
        assert result["new_version"] == 2

        # 验证新版本
        v2 = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id,
            SkillVersion.version == 2,
        ).first()
        assert v2 is not None
        assert "高级测试助手" in v2.system_prompt

    def test_adopt_system_prompt_insert(self, db, seeded):
        """adopt insert op → 内容追加到末尾或锚点后。"""
        from app.services.studio_governance import adopt_staged_edit

        se = self._create_staged_edit(
            db, seeded["skill"].id,
            diff_ops=[{"op": "insert", "old": "你是测试助手。", "new": "\n请使用中文回复。"}],
        )
        result = adopt_staged_edit(db, se.id, seeded["admin"].id)
        assert result["ok"] is True

        latest = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id,
        ).order_by(SkillVersion.version.desc()).first()
        assert "请使用中文回复" in latest.system_prompt

    def test_adopt_system_prompt_delete(self, db, seeded):
        """adopt delete op → 指定文本被删除。"""
        from app.services.studio_governance import adopt_staged_edit

        se = self._create_staged_edit(
            db, seeded["skill"].id,
            diff_ops=[{"op": "delete", "old": "你是测试助手。"}],
        )
        result = adopt_staged_edit(db, se.id, seeded["admin"].id)
        assert result["ok"] is True

        latest = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id,
        ).order_by(SkillVersion.version.desc()).first()
        assert "你是测试助手。" not in latest.system_prompt

    def test_adopt_idempotent(self, db, seeded):
        """已 adopted 的 edit 再次 adopt → ok + already_adopted。"""
        from app.services.studio_governance import adopt_staged_edit

        se = self._create_staged_edit(db, seeded["skill"].id)
        adopt_staged_edit(db, se.id, seeded["admin"].id)
        result = adopt_staged_edit(db, se.id, seeded["admin"].id)
        assert result["ok"] is True
        assert result.get("already_adopted") is True

    def test_reject(self, db, seeded):
        """reject → status 变为 rejected。"""
        from app.services.studio_governance import reject_staged_edit

        se = self._create_staged_edit(db, seeded["skill"].id)
        result = reject_staged_edit(db, se.id, seeded["admin"].id)
        assert result["ok"] is True

        db.refresh(se)
        assert se.status == "rejected"


class TestWorkflowActionEndpoint:
    def _create_staged_edit(self, db, skill_id):
        se = StagedEdit(
            skill_id=skill_id,
            target_type="system_prompt",
            diff_ops=[{"op": "replace", "old": "你是测试助手。", "new": "你是工作流测试助手。"}],
            summary="工作流接口采纳测试",
            risk_level="low",
            status="pending",
        )
        db.add(se)
        db.commit()
        db.refresh(se)
        return se

    def test_workflow_action_adopt_staged_edit(self, client, token, seeded, db):
        se = self._create_staged_edit(db, seeded["skill"].id)

        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={"action": "adopt_staged_edit", "staged_edit_id": se.id},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["action"] == "adopt_staged_edit"
        assert data["updated_staged_edit_status"] == "adopted"
        assert data["memo_refresh_required"] is True
        assert data["workflow_state_patch"] == {}
        assert data["result"]["target_type"] == "system_prompt"

    def test_workflow_action_reject_staged_edit(self, client, token, seeded, db):
        se = self._create_staged_edit(db, seeded["skill"].id)

        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={"action": "reject_staged_edit", "staged_edit_id": se.id},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["action"] == "reject_staged_edit"
        assert data["updated_staged_edit_status"] == "rejected"

    def test_workflow_action_returns_recovery_workflow_state_patch(self, client, token, seeded, db):
        from app.services import skill_memo_service

        se = self._create_staged_edit(db, seeded["skill"].id)
        skill_memo_service.sync_workflow_recovery(
            db,
            seeded["skill"].id,
            workflow_state={
                "workflow_id": "run_test_patch",
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "preflight_remediation",
                "phase": "remediate",
                "next_action": "review_cards",
                "route_reason": "preflight_failed",
            },
            cards=[{
                "id": "card_patch_1",
                "title": "补齐描述",
                "type": "staged_edit",
                "status": "pending",
                "content": {"summary": "补齐描述", "staged_edit_id": str(se.id)},
                "actions": [{"label": "采纳", "type": "adopt"}],
            }],
            staged_edits=[{
                "id": str(se.id),
                "target_type": "system_prompt",
                "summary": "工作流状态补丁测试",
                "risk_level": "low",
                "diff_ops": se.diff_ops,
                "status": "pending",
            }],
            user_id=seeded["admin"].id,
            commit=True,
        )

        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={"action": "reject_staged_edit", "card_id": "card_patch_1", "staged_edit_id": se.id},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow_state_patch"]["workflow_mode"] == "preflight_remediation"
        assert data["workflow_state_patch"]["next_action"] == "continue_chat"

    def test_workflow_action_prepare_next_step_returns_recommendation(self, client, token, seeded, db):
        from app.services import skill_memo_service

        skill_memo_service.sync_workflow_recovery(
            db,
            seeded["skill"].id,
            workflow_state={
                "workflow_id": "run_prepare_next",
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "preflight_remediation",
                "phase": "validate",
                "next_action": "run_sandbox",
                "route_reason": "preflight_passed",
                "metadata": {
                    "test_recommendation": {
                        "action": "run_sandbox",
                        "scope": "sandbox",
                        "label": "运行沙盒测试",
                    },
                },
            },
            user_id=seeded["admin"].id,
            commit=True,
        )

        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={"action": "prepare_next_step"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow_state_patch"]["next_action"] == "run_sandbox"
        assert data["result"]["test_recommendation"]["scope"] == "sandbox"

    def test_workflow_action_followup_prompt_dispatch_updates_card(self, client, token, seeded, db, monkeypatch):
        from app.services import skill_memo_service

        skill_memo_service.sync_workflow_recovery(
            db,
            seeded["skill"].id,
            workflow_state={
                "workflow_id": "run_followup_dispatch",
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "preflight_remediation",
                "phase": "remediate",
                "next_action": "review_cards",
                "route_reason": "preflight_failed",
            },
            cards=[{
                "id": "card_followup_1",
                "title": "归档知识文件",
                "type": "followup_prompt",
                "status": "pending",
                "content": {
                    "summary": "归档知识文件",
                    "preflight_action": "confirm_archive",
                    "action_payload": {"confirmations": [{"filename": "reference.md"}]},
                },
                "actions": [{"label": "执行", "type": "adopt"}],
            }],
            user_id=seeded["admin"].id,
            commit=True,
        )

        called: dict[str, object] = {}

        def _fake_confirm(db_session, *, skill_id, user, confirmations):
            called["skill_id"] = skill_id
            called["user_id"] = user.id
            called["confirmations"] = confirmations
            return {"ok": True, "results": [{"filename": "reference.md", "ok": True}]}

        monkeypatch.setattr(
            "app.services.studio_followup_actions.confirm_knowledge_archive",
            _fake_confirm,
        )

        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={
                "action": "confirm_archive",
                "card_id": "card_followup_1",
                "payload": {"confirmations": [{"filename": "reference.md"}]},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["action"] == "confirm_archive"
        assert data["updated_card_status"] == "adopted"
        assert called["skill_id"] == seeded["skill"].id
        assert called["user_id"] == seeded["admin"].id
        assert called["confirmations"] == [{"filename": "reference.md"}]

        memo = skill_memo_service.get_memo(db, seeded["skill"].id)
        assert memo["workflow_recovery"]["cards"][0]["status"] == "adopted"

    def test_workflow_action_missing_staged_edit_id_returns_400(self, client, token, seeded):
        resp = client.post(
            f"/api/skills/{seeded['skill'].id}/workflow/actions",
            json={"action": "adopt_staged_edit"},
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_reject_idempotent(self, db, seeded):
        """已 rejected 再次 reject → ok + already_rejected。"""
        from app.services.studio_governance import reject_staged_edit

        se = self._create_staged_edit(db, seeded["skill"].id)
        reject_staged_edit(db, se.id, seeded["admin"].id)
        result = reject_staged_edit(db, se.id, seeded["admin"].id)
        assert result["ok"] is True
        assert result.get("already_rejected") is True

    def test_reject_then_adopt_fails(self, db, seeded):
        """rejected edit 不能再 adopt（非 pending）。"""
        from app.services.studio_governance import reject_staged_edit

        se = self._create_staged_edit(db, seeded["skill"].id)
        reject_staged_edit(db, se.id, seeded["admin"].id)

        db.refresh(se)
        assert se.status == "rejected"


class TestStudioRunsWorkflowBootstrap:
    @pytest.mark.asyncio
    async def test_studio_runs_bootstrap_executes_after_first_round(self, db, seeded):
        from app.models.conversation import Conversation, Message, MessageRole
        from app.services.studio_runs import StudioRun, StudioRunRegistry

        conv = Conversation(user_id=seeded["admin"].id, skill_id=seeded["skill"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        db.add_all([
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第一轮"),
            Message(conversation_id=conv.id, role=MessageRole.ASSISTANT, content="第一轮回复"),
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第二轮"),
        ])
        db.commit()

        registry = StudioRunRegistry()
        run = StudioRun(
            id="run_multi_turn",
            conversation_id=conv.id,
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            content="继续推进",
        )

        async def _empty_stream(*args, **kwargs):
            if False:
                yield None

        bootstrap_result = SimpleNamespace(
            workflow_state={
                "workflow_id": run.id,
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "none",
                "phase": "review",
                "next_action": "continue_chat",
            },
            route_status={"next_action": "continue_chat", "workflow_mode": "none"},
            assist_skills_status={"skills": ["prompt_optimizer"], "session_mode": "optimize_existing_skill"},
            architect_phase_status=None,
            audit_summary=None,
            cards=[],
            staged_edits=[],
        )

        with patch("app.services.studio_workflow_orchestrator.bootstrap_workflow", new=AsyncMock(return_value=bootstrap_result)) as bootstrap_mock, patch(
            "app.services.studio_runs.SessionLocal",
            TestingSessionLocal,
        ), patch(
            "app.harness.adapters.build_skill_studio_request",
            return_value={"conversation_id": conv.id, "skill_id": seeded["skill"].id},
        ), patch(
            "app.harness.profiles.skill_studio.skill_studio_profile.run_stream",
            new=_empty_stream,
        ), patch(
            "app.config.settings.STUDIO_STRUCTURED_MODE",
            "on",
        ):
            await registry._execute(run, {})

        bootstrap_mock.assert_awaited_once()
        called = bootstrap_mock.await_args.kwargs
        assert called["conversation_id"] == conv.id
        assert called["skill_id"] == seeded["skill"].id
        assert run.status == "completed"
        patch_events = [data for _, event, data in run.events if event == "patch_applied"]
        assert patch_events
        assert patch_events[0]["run_id"] == run.id
        assert patch_events[0]["run_version"] == 1
        assert patch_events[0]["patch_type"] == "workflow_patch"

    @pytest.mark.asyncio
    async def test_studio_runs_respects_patch_protocol_rollout_flag(self, db, seeded):
        from app.models.conversation import Conversation, Message, MessageRole
        from app.services.studio_runs import StudioRun, StudioRunRegistry

        conv = Conversation(user_id=seeded["admin"].id, skill_id=seeded["skill"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
        db.add_all([
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第一轮"),
            Message(conversation_id=conv.id, role=MessageRole.ASSISTANT, content="第一轮回复"),
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第二轮"),
        ])
        db.commit()

        registry = StudioRunRegistry()
        run = StudioRun(
            id="run_patch_disabled",
            conversation_id=conv.id,
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            content="继续推进",
        )

        async def _empty_stream(*args, **kwargs):
            if False:
                yield None

        bootstrap_result = SimpleNamespace(
            workflow_state={
                "workflow_id": run.id,
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "none",
                "phase": "review",
                "next_action": "continue_chat",
                "metadata": {
                    "rollout": {
                        "flags": {
                            "patch_protocol_enabled": False,
                            "frontend_run_protocol_enabled": True,
                        },
                    },
                },
            },
            route_status={"next_action": "continue_chat", "workflow_mode": "none"},
            assist_skills_status={"skills": ["prompt_optimizer"], "session_mode": "optimize_existing_skill"},
            architect_phase_status=None,
            audit_summary=None,
            cards=[],
            staged_edits=[],
        )

        with patch("app.services.studio_workflow_orchestrator.bootstrap_workflow", new=AsyncMock(return_value=bootstrap_result)), patch(
            "app.services.studio_runs.SessionLocal",
            TestingSessionLocal,
        ), patch(
            "app.harness.adapters.build_skill_studio_request",
            return_value={"conversation_id": conv.id, "skill_id": seeded["skill"].id},
        ), patch(
            "app.harness.profiles.skill_studio.skill_studio_profile.run_stream",
            new=_empty_stream,
        ), patch(
            "app.config.settings.STUDIO_STRUCTURED_MODE",
            "on",
        ):
            await registry._execute(run, {})

        patch_events = [data for _, event, data in run.events if event == "patch_applied"]
        workflow_events = [data for _, event, data in run.events if event == "workflow_event"]
        assert patch_events == []
        assert workflow_events

    @pytest.mark.asyncio
    async def test_studio_runs_emits_deep_summary_and_evidence_patches(self, db, seeded):
        from app.harness.events import EventName, emit
        from app.models.conversation import Conversation, Message, MessageRole
        from app.services.studio_runs import StudioRun, StudioRunRegistry

        conv = Conversation(user_id=seeded["admin"].id, skill_id=seeded["skill"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
        db.add_all([
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第一轮"),
            Message(conversation_id=conv.id, role=MessageRole.ASSISTANT, content="第一轮回复"),
            Message(conversation_id=conv.id, role=MessageRole.USER, content="第二轮"),
        ])
        db.commit()

        registry = StudioRunRegistry()
        run = StudioRun(
            id="run_deep_patch",
            conversation_id=conv.id,
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            content="继续推进",
        )

        async def _stream(*args, **kwargs):
            yield emit(EventName.STATUS, {"stage": "first_useful_response"})
            yield emit(EventName.DELTA, {"text": "Deep Lane 完整补完"})
            yield emit(EventName.STATUS, {"stage": "deep_started"})
            yield emit(EventName.STATUS, {"stage": "deep_completed"})

        bootstrap_result = SimpleNamespace(
            workflow_state={
                "workflow_id": run.id,
                "session_mode": "optimize_existing_skill",
                "workflow_mode": "none",
                "phase": "review",
                "next_action": "continue_chat",
                "execution_strategy": "fast_then_deep",
                "deep_status": "pending",
                "metadata": {
                    "rollout": {
                        "flags": {
                            "patch_protocol_enabled": True,
                            "frontend_run_protocol_enabled": True,
                        },
                    },
                },
            },
            route_status={"next_action": "continue_chat", "workflow_mode": "none"},
            assist_skills_status={"skills": ["prompt_optimizer"], "session_mode": "optimize_existing_skill"},
            architect_phase_status=None,
            audit_summary={"verdict": "needs_work", "quality_score": 58},
            cards=[{"id": "card_1", "title": "治理卡片", "type": "staged_edit", "actions": []}],
            staged_edits=[{"id": "edit_1", "target_type": "system_prompt", "summary": "补齐约束", "risk_level": "low", "diff_ops": []}],
        )

        with patch("app.services.studio_workflow_orchestrator.bootstrap_workflow", new=AsyncMock(return_value=bootstrap_result)), patch(
            "app.services.studio_runs.SessionLocal",
            TestingSessionLocal,
        ), patch(
            "app.harness.adapters.build_skill_studio_request",
            return_value={"conversation_id": conv.id, "skill_id": seeded["skill"].id},
        ), patch(
            "app.harness.profiles.skill_studio.skill_studio_profile.run_stream",
            new=_stream,
        ), patch(
            "app.config.settings.STUDIO_STRUCTURED_MODE",
            "on",
        ):
            await registry._execute(run, {})

        patch_events = [data for _, event, data in run.events if event == "patch_applied"]
        patch_types = [event["patch_type"] for event in patch_events]
        assert "deep_summary_patch" in patch_types
        assert "evidence_patch" in patch_types
        deep_summary = next(event for event in patch_events if event["patch_type"] == "deep_summary_patch")
        evidence = next(event for event in patch_events if event["patch_type"] == "evidence_patch")
        assert "Deep Lane 完整补完" in deep_summary["payload"]["summary"]
        assert "已生成 1 张治理卡片" in evidence["payload"]["evidence"]
        assert "已生成 1 个 staged edit" in evidence["payload"]["evidence"]

    @pytest.mark.asyncio
    async def test_create_supersedes_previous_active_run(self, db, seeded):
        from app.services.studio_runs import StudioRun, StudioRunRegistry

        registry = StudioRunRegistry()
        old_run = StudioRun(
            id="run_old",
            conversation_id=99,
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            content="旧请求",
            run_version=1,
            status="running",
        )
        registry._runs[old_run.id] = old_run
        registry._active_by_conversation[99] = old_run.id
        registry._version_by_conversation[99] = 1

        async def _noop_execute(run, req_payload):
            run.status = "completed"

        registry._execute = _noop_execute  # type: ignore[method-assign]

        new_run = await registry.create(
            conversation_id=99,
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            content="新请求",
            req_payload={},
        )

        assert old_run.status == "superseded"
        assert old_run.superseded_by == new_run.id
        assert any(event == "run_superseded" for _, event, _ in old_run.events)
        assert new_run.run_version == 2
        assert registry._active_by_conversation[99] == new_run.id


class TestStudioContextDigest:
    def test_build_context_digest_bundle_returns_lightweight_digests(self):
        from app.services.studio_context_digest import build_context_digest_bundle

        bundle = build_context_digest_bundle(
            history_messages=[
                {"role": "user", "content": "请帮我优化这个 Skill"},
                {"role": "assistant", "content": "先看下当前结构"},
            ],
            memo_context={
                "lifecycle_stage": "fixing",
                "current_task": {"title": "补充角色定义"},
                "latest_test": {"summary": "缺少角色说明"},
                "tasks": [{"id": "t1", "title": "修复角色定义", "status": "todo"}],
                "persistent_notices": [{"id": "n1", "status": "active"}],
                "workflow_recovery": {
                    "workflow_state": {"phase": "review", "next_action": "review_cards"},
                    "cards": [{"id": "c1", "status": "pending"}],
                    "staged_edits": [{"id": "e1", "status": "pending"}],
                    "updated_at": "2026-04-15T00:00:00+00:00",
                },
            },
            source_files=[{"filename": "guide.md", "category": "reference", "size": 128}],
            editor_prompt="你是技能优化助手",
        )

        assert bundle["conversation_digest"]["message_count"] == 2
        assert bundle["memo_digest"]["current_task"] == "补充角色定义"
        assert bundle["recovery_digest"]["pending_cards_count"] == 1
        assert bundle["source_file_index_digest"]["file_count"] == 1
        assert bundle["editor_prompt_digest"]["length"] > 0
        assert bundle["signature"]

    def test_build_context_digest_bundle_reuses_persisted_cache_entries(self):
        from app.services.studio_context_digest import build_context_digest_bundle

        first_bundle = build_context_digest_bundle(
            history_messages=[{"role": "user", "content": "请优化"}],
            memo_context={
                "lifecycle_stage": "editing",
                "current_task": {"title": "补充限制"},
                "tasks": [{"id": "t1", "title": "补充限制", "status": "todo"}],
                "persistent_notices": [],
                "workflow_recovery": {
                    "workflow_state": {"phase": "review", "next_action": "review_cards"},
                    "cards": [],
                    "staged_edits": [],
                    "updated_at": "2026-04-15T00:00:00+00:00",
                },
                "context_digest_cache": {"schema_version": 1, "updated_at": None, "entries": {}},
            },
            source_files=[{"filename": "guide.md", "category": "reference", "size": 128}],
            editor_prompt="你是技能优化助手",
            persisted_cache={"schema_version": 1, "updated_at": None, "entries": {}},
            include_cache_payload=True,
        )
        second_bundle = build_context_digest_bundle(
            history_messages=[{"role": "user", "content": "请优化"}],
            memo_context={
                "lifecycle_stage": "editing",
                "current_task": {"title": "补充限制"},
                "tasks": [{"id": "t1", "title": "补充限制", "status": "todo"}],
                "persistent_notices": [],
                "workflow_recovery": {
                    "workflow_state": {"phase": "review", "next_action": "review_cards"},
                    "cards": [],
                    "staged_edits": [],
                    "updated_at": "2026-04-15T00:00:00+00:00",
                },
            },
            source_files=[{"filename": "guide.md", "category": "reference", "size": 128}],
            editor_prompt="你是技能优化助手",
            persisted_cache=first_bundle["cache_payload"],
            include_cache_payload=True,
        )

        assert first_bundle["cache"]["cache_changed"] is True
        assert second_bundle["cache"]["entries"]["memo_digest"]["status"] == "hit"
        assert second_bundle["cache"]["entries"]["recovery_digest"]["status"] == "hit"
        assert second_bundle["cache"]["entries"]["source_file_index_digest"]["status"] == "hit"
        assert second_bundle["cache"]["cache_changed"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _slugify 单元测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestSlugify:
    def test_ascii_name(self):
        from app.routers.skills import _slugify
        assert _slugify("Hello World") == "hello-world"

    def test_chinese_name(self):
        from app.routers.skills import _slugify
        slug = _slugify("消费者洞察")
        assert slug.startswith("skill-")
        assert len(slug) > 0

    def test_mixed_name(self):
        from app.routers.skills import _slugify
        slug = _slugify("AI Marketing 2024")
        assert "ai-marketing-2024" == slug

    def test_empty_name(self):
        from app.routers.skills import _slugify
        slug = _slugify("")
        assert slug.startswith("skill-")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _apply_diff_ops 单元测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyDiffOps:
    def test_replace(self):
        from app.services.studio_governance import _apply_diff_ops
        result = _apply_diff_ops("hello world", [{"op": "replace", "old": "world", "new": "earth"}])
        assert result == "hello earth"

    def test_insert_with_anchor(self):
        from app.services.studio_governance import _apply_diff_ops
        result = _apply_diff_ops("hello world", [{"op": "insert", "old": "hello", "new": " dear"}])
        assert result == "hello dear world"

    def test_insert_no_anchor(self):
        from app.services.studio_governance import _apply_diff_ops
        result = _apply_diff_ops("hello", [{"op": "insert", "old": "", "new": " world"}])
        assert result == "hello\n world"

    def test_delete(self):
        from app.services.studio_governance import _apply_diff_ops
        result = _apply_diff_ops("hello cruel world", [{"op": "delete", "old": " cruel"}])
        assert result == "hello world"

    def test_multiple_ops(self):
        from app.services.studio_governance import _apply_diff_ops
        result = _apply_diff_ops(
            "你是助手。请回答。",
            [
                {"op": "replace", "old": "助手", "new": "专业助手"},
                {"op": "insert", "old": "请回答。", "new": "用中文。"},
            ],
        )
        assert "专业助手" in result
        assert "用中文。" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Architect Mode — Route + 状态 API
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchitectRoute:
    """验证 route_session 的 architect_mode 判断逻辑。"""

    def test_new_skill_triggers_architect(self, db):
        """无 skill_id + 模糊需求 → architect_mode + phase_1_why。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=None, user_message="我想做一个分析用户的技能")
        assert r.workflow_mode == "architect_mode"
        assert r.initial_phase == "phase_1_why"

    def test_simple_patch_skips_architect(self, db, seeded):
        """小 patch 关键词 → 不启用 architect。"""
        from app.services.studio_router import route_session
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        v.system_prompt = "你是专业的营销分析助手，擅长用麦肯锡 MECE 框架拆解消费者洞察，并生成结构化报告。"
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我润色一下措辞")
        assert r.workflow_mode == "none"

    def test_spec_ready_goes_phase3(self, db):
        """已有完整 spec → architect_mode + phase_3_how。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=None, user_message="spec 已经齐了，按这个来生成")
        assert r.workflow_mode == "architect_mode"
        assert r.initial_phase == "phase_3_how"

    def test_existing_skill_poor_structure_triggers_architect(self, db, seeded):
        """已有 skill 但 prompt 无角色/无结构 → architect phase_1。"""
        from app.services.studio_router import route_session
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        # 足够长但无角色定义、无结构化标记
        v.system_prompt = "请帮用户分析消费者洞察并给出建议，要求全面、专业、有深度、有广度、有高度，不能遗漏任何角度。"
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我改进这个")
        assert r.workflow_mode == "architect_mode"
        assert r.initial_phase == "phase_1_why"

    def test_existing_skill_good_structure_skips_architect(self, db, seeded):
        """已有 skill 且 prompt 有角色+结构 → 不启用 architect。"""
        from app.services.studio_router import route_session
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        v.system_prompt = "## Role\n你是专业的营销分析助手。\n\n## 步骤\n1. 分析目标\n2. 输出报告\n\n## 输出格式\nJSON"
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我改进这个")
        assert r.workflow_mode == "none"

    def test_audit_intent_never_sets_architect(self, db, seeded):
        """审计意图 → audit 路径，不设 architect（由 audit 后决定）。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=seeded["skill"].id, user_message="审计一下这个技能")
        assert r.session_mode == "audit_imported_skill"
        assert r.workflow_mode == "none"


class TestStudioLatencyPolicy:
    def test_audit_request_is_high_and_fast_then_deep(self):
        from app.services.studio_latency_policy import choose_execution_strategy, estimate_complexity_level

        level = estimate_complexity_level(
            session_mode="audit_imported_skill",
            workflow_mode="none",
            next_action="run_audit",
            user_message="请完整审计这个导入 Skill 并给出整改方案",
        )
        strategy = choose_execution_strategy(
            complexity_level=level,
            workflow_mode="none",
            next_action="run_audit",
        )

        assert level == "high"
        assert strategy == "fast_then_deep"

    def test_simple_patch_can_stay_fast_only(self):
        from app.services.studio_latency_policy import choose_execution_strategy, estimate_complexity_level

        level = estimate_complexity_level(
            session_mode="optimize_existing_skill",
            workflow_mode="none",
            next_action="start_editing",
            user_message="帮我小改一下措辞",
        )
        strategy = choose_execution_strategy(
            complexity_level=level,
            workflow_mode="none",
            next_action="start_editing",
        )

        assert level == "simple"
        assert strategy == "fast_only"

    def test_medium_request_exposes_sla_policy(self):
        from app.services.studio_latency_policy import build_sla_policy

        policy = build_sla_policy(
            complexity_level="medium",
            execution_strategy="fast_then_deep",
            sla_degrade_enabled=True,
        )

        assert policy["enabled"] is True
        assert policy["probe_after_s"] == 10
        assert policy["degrade_after_s"] == 20
        assert policy["deadline_after_s"] == 30
        assert policy["force_two_stage_after_s"] is None


class TestSkillStudioRuntimeLatency:
    @staticmethod
    def _build_request(*, user_id: int, skill_id: int, conversation_id: int):
        from app.harness.contracts import AgentType, HarnessContext, HarnessRequest, HarnessSessionKey

        return HarnessRequest(
            session_key=HarnessSessionKey(
                user_id=user_id,
                agent_type=AgentType.SKILL_STUDIO,
                workspace_id=1,
                target_type="skill",
                target_id=skill_id,
                conversation_id=conversation_id,
            ),
            agent_type=AgentType.SKILL_STUDIO,
            user_id=user_id,
            input_text="请完整审计这个技能并给出整改方案",
            context=HarnessContext(
                workspace_id=1,
                conversation_id=conversation_id,
                skill_id=skill_id,
                target_type="skill",
                target_id=skill_id,
            ),
        )

    @pytest.mark.asyncio
    async def test_profile_records_runtime_latency_markers(self, db, seeded):
        from app.harness.events import EventName
        from app.harness.profiles.skill_studio import SkillStudioAgentProfile
        from app.models.conversation import Conversation

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        request = self._build_request(
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            conversation_id=conv.id,
        )
        profile = SkillStudioAgentProfile()

        async def fake_stream(**kwargs):
            yield ("status", {
                "stage": "classified",
                "complexity_level": "high",
                "execution_strategy": "fast_then_deep",
                "fast_status": "pending",
                "deep_status": "pending",
            })
            yield ("delta", {"text": "首"})
            yield ("delta", {"text": "答"})
            yield ("status", {"stage": "done"})
            yield ("__full_content__", {"text": "首答"})

        with (
            patch.object(SkillStudioAgentProfile, "_build_history", return_value=[]),
            patch.object(SkillStudioAgentProfile, "_get_available_tools", return_value=[]),
            patch.object(SkillStudioAgentProfile, "_get_source_files", return_value=([], "", False)),
            patch.object(SkillStudioAgentProfile, "_get_memo", return_value=""),
            patch.object(SkillStudioAgentProfile, "_get_skill_metadata", return_value={}),
            patch("app.services.studio_agent.run_stream", side_effect=fake_stream),
        ):
            events = [event async for event in profile.run_stream(
                request,
                db,
                conv,
                selected_skill_id=seeded["skill"].id,
            )]

        run_started = next(event for event in events if event.event == EventName.RUN_STARTED)
        run = profile.store.get_run(run_started.run_id)
        assert run is not None
        assert run.metadata["fast_started_at"]
        assert run.metadata["first_token_at"]
        assert run.metadata["first_useful_response_at"]
        assert run.metadata["deep_started_at"]
        assert run.metadata["deep_completed_at"]
        assert run.metadata["run_completed_at"]
        assert run.metadata["latency"]["first_token_at"] == run.metadata["first_token_at"]
        stages = [event.data.get("stage") for event in events if event.event == EventName.STATUS]
        assert "fast_started" in stages
        assert "first_token" in stages
        assert "first_useful_response" in stages
        assert "deep_started" in stages
        assert "deep_completed" in stages

    @pytest.mark.asyncio
    async def test_profile_forces_sla_first_response_and_final_replace(self, db, seeded):
        from app.harness.events import EventName
        from app.harness.profiles.skill_studio import SkillStudioAgentProfile
        from app.models.conversation import Conversation

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        request = self._build_request(
            user_id=seeded["admin"].id,
            skill_id=seeded["skill"].id,
            conversation_id=conv.id,
        )
        profile = SkillStudioAgentProfile()

        async def slow_stream(**kwargs):
            await asyncio.sleep(0.05)
            yield ("delta", {"text": "真正结果"})
            yield ("status", {"stage": "done"})
            yield ("__full_content__", {"text": "真正结果"})

        with (
            patch.object(SkillStudioAgentProfile, "_build_history", return_value=[]),
            patch.object(SkillStudioAgentProfile, "_get_available_tools", return_value=[]),
            patch.object(SkillStudioAgentProfile, "_get_source_files", return_value=([], "", False)),
            patch.object(SkillStudioAgentProfile, "_get_memo", return_value=""),
            patch.object(SkillStudioAgentProfile, "_get_skill_metadata", return_value={}),
            patch(
                "app.harness.profiles.skill_studio.build_sla_policy",
                return_value={
                    "enabled": True,
                    "probe_after_s": 0.005,
                    "degrade_after_s": 0.01,
                    "force_two_stage_after_s": None,
                    "deadline_after_s": 0.015,
                    "two_stage_expected": True,
                },
            ),
            patch("app.services.studio_agent.run_stream", side_effect=slow_stream),
        ):
            events = [event async for event in profile.run_stream(
                request,
                db,
                conv,
                selected_skill_id=seeded["skill"].id,
            )]

        event_names = [event.event for event in events]
        assert EventName.FALLBACK_TEXT in event_names
        assert EventName.REPLACE in event_names

        fallback_event = next(event for event in events if event.event == EventName.FALLBACK_TEXT)
        replace_event = next(
            event for event in events
            if event.event == EventName.REPLACE and event.data.get("source") == "sla_final_replace"
        )
        assert "首轮" in fallback_event.data["text"]
        assert replace_event.data["text"] == "真正结果"


class TestStudioAdminMetrics:
    def test_admin_metrics_dashboard_returns_rollup(self, client, token, seeded, db):
        now = dt.datetime.utcnow()
        run_id = "run_metrics_1"
        db.add_all([
            UnifiedEvent(
                event_type="harness.run.created",
                source_type="harness",
                payload={"run_id": run_id, "agent_type": "skill_studio"},
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now,
            ),
            UnifiedEvent(
                event_type="harness.run.metadata_updated",
                source_type="harness",
                payload={
                    "run_id": run_id,
                    "metadata_patch": {
                        "latency": {
                            "request_accepted_at": "2026-04-15T00:00:00+00:00",
                            "fast_started_at": "2026-04-15T00:00:01+00:00",
                            "first_token_at": "2026-04-15T00:00:02+00:00",
                            "first_useful_response_at": "2026-04-15T00:00:08+00:00",
                            "deep_started_at": "2026-04-15T00:00:09+00:00",
                            "deep_completed_at": "2026-04-15T00:00:18+00:00",
                            "run_completed_at": "2026-04-15T00:00:20+00:00",
                        },
                        "rollout": {
                            "eligible": True,
                            "scope": "global_default",
                            "reason": "global_default",
                            "flags": {
                                "dual_lane_enabled": True,
                                "fast_lane_enabled": True,
                                "deep_lane_enabled": True,
                                "sla_degrade_enabled": True,
                                "patch_protocol_enabled": True,
                                "frontend_run_protocol_enabled": True,
                            },
                        },
                    },
                },
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now + dt.timedelta(seconds=1),
            ),
            UnifiedEvent(
                event_type="harness.run.status_changed",
                source_type="harness",
                payload={"run_id": run_id, "status": "completed", "error": None},
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now + dt.timedelta(seconds=2),
            ),
        ])
        db.commit()

        resp = client.get("/api/admin/studio/metrics?days=7&limit=10", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_count"] == 1
        assert data["status_counts"]["completed"] == 1
        assert data["first_useful_response"]["count"] == 1
        assert data["first_useful_response"]["p50_s"] == 8.0
        assert data["deep_completed"]["p50_s"] == 18.0
        assert data["first_token"]["p50_s"] == 1.0
        assert data["records"][0]["metadata"]["rollout"]["flags"]["deep_lane_enabled"] is True

    def test_admin_metrics_export_returns_csv(self, client, token, seeded, db):
        now = dt.datetime.utcnow()
        run_id = "run_metrics_csv"
        db.add_all([
            UnifiedEvent(
                event_type="harness.run.created",
                source_type="harness",
                payload={"run_id": run_id, "agent_type": "skill_studio"},
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now,
            ),
            UnifiedEvent(
                event_type="harness.run.metadata_updated",
                source_type="harness",
                payload={
                    "run_id": run_id,
                    "metadata_patch": {
                        "latency": {
                            "request_accepted_at": "2026-04-15T00:00:00+00:00",
                            "first_useful_response_at": "2026-04-15T00:00:05+00:00",
                            "deep_completed_at": "2026-04-15T00:00:14+00:00",
                            "run_completed_at": "2026-04-15T00:00:15+00:00",
                        },
                    },
                },
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now + dt.timedelta(seconds=1),
            ),
            UnifiedEvent(
                event_type="harness.run.status_changed",
                source_type="harness",
                payload={"run_id": run_id, "status": "completed", "error": None},
                user_id=seeded["admin"].id,
                workspace_id=1,
                created_at=now + dt.timedelta(seconds=2),
            ),
        ])
        db.commit()

        resp = client.get("/api/admin/studio/metrics/export?days=7&limit=10", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        assert "run_id,created_at,status,user_id,workspace_id" in resp.text
        assert "run_metrics_csv" in resp.text
        assert "5.0" in resp.text


class TestSkillStudioSourceFileLoading:
    def test_source_files_are_index_only_by_default(self, db, seeded):
        from app.harness.profiles.skill_studio import SkillStudioAgentProfile

        skill = seeded["skill"]
        skill.source_files = [{"filename": "guide.md", "category": "reference"}]
        db.commit()

        with patch(
            "app.services.skill_engine._read_source_files",
            side_effect=AssertionError("should_not_read_source_files"),
        ):
            files, content, loaded = SkillStudioAgentProfile._get_source_files(
                db,
                skill.id,
                user_message="帮我优化一下这个 Skill 的表达",
            )

        assert len(files) == 1
        assert content == ""
        assert loaded is False

    def test_source_files_load_when_user_mentions_file_content(self, db, seeded):
        from app.harness.profiles.skill_studio import SkillStudioAgentProfile

        skill = seeded["skill"]
        skill.source_files = [{"filename": "guide.md", "category": "reference"}]
        db.commit()

        with patch("app.services.skill_engine._read_source_files", return_value="# guide"):
            files, content, loaded = SkillStudioAgentProfile._get_source_files(
                db,
                skill.id,
                user_message="请读取文件内容后帮我修复",
            )

        assert len(files) == 1
        assert content == "# guide"
        assert loaded is True


class TestArchitectStateAPI:
    """验证 architect state GET / PATCH 端点。"""

    def test_get_state_no_architect(self, client, token, seeded, db):
        """无 architect 状态 → 返回 workflow_mode=none。"""
        # 先创建一个 conversation
        resp = client.post("/api/conversations", json={}, headers=_auth(token))
        conv_id = resp.json()["id"]

        resp = client.get(
            f"/api/conversations/conversations/{conv_id}/architect-state",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["workflow_mode"] == "none"

    def test_create_and_read_state(self, client, token, seeded, db):
        """通过 route 创建 architect 状态，然后 GET 读取。"""
        from app.models.skill import ArchitectWorkflowState

        resp = client.post("/api/conversations", json={}, headers=_auth(token))
        conv_id = resp.json()["id"]

        # 手动插入 architect state
        state = ArchitectWorkflowState(
            conversation_id=conv_id,
            skill_id=seeded["skill"].id,
            workflow_mode="architect_mode",
            workflow_phase="phase_1_why",
        )
        db.add(state)
        db.commit()

        resp = client.get(
            f"/api/conversations/conversations/{conv_id}/architect-state",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_mode"] == "architect_mode"
        assert data["workflow_phase"] == "phase_1_why"
        assert data["ooda_round"] == 0

    def test_patch_advance_phase(self, client, token, seeded, db):
        """PATCH 推进阶段 + 写入 phase_outputs。"""
        from app.models.skill import ArchitectWorkflowState

        resp = client.post("/api/conversations", json={}, headers=_auth(token))
        conv_id = resp.json()["id"]

        state = ArchitectWorkflowState(
            conversation_id=conv_id,
            skill_id=seeded["skill"].id,
            workflow_mode="architect_mode",
            workflow_phase="phase_1_why",
        )
        db.add(state)
        db.commit()

        resp = client.patch(
            f"/api/conversations/conversations/{conv_id}/architect-state",
            json={
                "workflow_phase": "phase_2_what",
                "phase_outputs": {"phase_1_why": {"root_cause": "维度不全"}},
                "phase_confirmed": {"phase_1_why": True},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["workflow_phase"] == "phase_2_what"
        assert data["phase_confirmed"]["phase_1_why"] is True

    def test_patch_ooda_round(self, client, token, seeded, db):
        """PATCH 更新 OODA 轮次。"""
        from app.models.skill import ArchitectWorkflowState

        resp = client.post("/api/conversations", json={}, headers=_auth(token))
        conv_id = resp.json()["id"]

        state = ArchitectWorkflowState(
            conversation_id=conv_id,
            workflow_mode="architect_mode",
            workflow_phase="ooda_iteration",
        )
        db.add(state)
        db.commit()

        resp = client.patch(
            f"/api/conversations/conversations/{conv_id}/architect-state",
            json={"ooda_round": 2},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["ooda_round"] == 2

    def test_patch_nonexistent_state(self, client, token, seeded, db):
        """无 architect 状态时 PATCH → 404。"""
        resp = client.post("/api/conversations", json={}, headers=_auth(token))
        conv_id = resp.json()["id"]

        resp = client.patch(
            f"/api/conversations/conversations/{conv_id}/architect-state",
            json={"workflow_phase": "phase_2_what"},
            headers=_auth(token),
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Architect 事件协议层
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchitectEvents:
    """验证 studio_architect_events 事件构造函数和状态推进。"""

    def test_make_phase_status(self):
        from app.services.studio_architect_events import make_phase_status
        evt = make_phase_status("phase_1_why", "create_new_skill", 0)
        assert evt["event"] == "architect_phase_status"
        assert evt["data"]["phase"] == "phase_1_why"

    def test_make_question(self):
        from app.services.studio_architect_events import make_question
        evt = make_question("这个 Skill 要解决什么问题？", "phase_1_why",
                           options=["提高效率", "降低成本", "其他"], framework="5 Whys")
        assert evt["event"] == "architect_question"
        assert len(evt["data"]["options"]) == 3
        assert evt["data"]["framework"] == "5 Whys"

    def test_make_question_minimal(self):
        from app.services.studio_architect_events import make_question
        evt = make_question("问题描述", "phase_2_what")
        assert "options" not in evt["data"]
        assert "framework" not in evt["data"]

    def test_make_phase_summary(self):
        from app.services.studio_architect_events import make_phase_summary
        evt = make_phase_summary("phase_1_why", {"root_cause": "维度不全"}, confirmed=True)
        assert evt["event"] == "architect_phase_summary"
        assert evt["data"]["confirmed"] is True

    def test_make_structure(self):
        from app.services.studio_architect_events import make_structure
        evt = make_structure("issue_tree", "消费者洞察维度", {"nodes": ["a", "b"]})
        assert evt["event"] == "architect_structure"
        assert evt["data"]["type"] == "issue_tree"

    def test_make_priority_matrix(self):
        from app.services.studio_architect_events import make_priority_matrix
        items = [
            {"label": "目标人群", "priority": "P0", "reason": "核心"},
            {"label": "渠道", "priority": "P1", "reason": "重要"},
        ]
        evt = make_priority_matrix(items)
        assert evt["event"] == "architect_priority_matrix"
        assert len(evt["data"]["items"]) == 2

    def test_make_ooda_decision(self):
        from app.services.studio_architect_events import make_ooda_decision
        evt = make_ooda_decision(1, "rollback", "维度遗漏", rollback_to="phase_2_what")
        assert evt["event"] == "architect_ooda_decision"
        assert evt["data"]["rollback_to"] == "phase_2_what"

    def test_make_ooda_converged(self):
        from app.services.studio_architect_events import make_ooda_decision
        evt = make_ooda_decision(2, "converged", "已收敛")
        assert "rollback_to" not in evt["data"]

    def test_make_ready_for_draft(self):
        from app.services.studio_architect_events import make_ready_for_draft
        evt = make_ready_for_draft({"p0": ["目标人群"]}, exit_to="generate_governance_actions")
        assert evt["event"] == "architect_ready_for_draft"
        assert evt["data"]["exit_to"] == "generate_governance_actions"

    def test_advance_phase(self, db, seeded):
        """advance_phase: phase_1 → phase_2，自动 confirm + merge outputs。"""
        from app.models.skill import ArchitectWorkflowState
        from app.services.studio_architect_events import advance_phase
        from app.models.conversation import Conversation

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        state = ArchitectWorkflowState(
            conversation_id=conv.id,
            workflow_mode="architect_mode",
            workflow_phase="phase_1_why",
        )
        db.add(state)
        db.commit()

        result = advance_phase(db, conv.id, phase_outputs={"root_cause": "维度不全"})
        assert result is not None
        assert result.workflow_phase == "phase_2_what"
        assert result.phase_confirmed.get("phase_1_why") is True
        assert result.phase_outputs.get("phase_1_why", {}).get("root_cause") == "维度不全"

    def test_advance_ooda_increments_round(self, db, seeded):
        """advance_phase on ooda_iteration: ooda_round + 1, 留在 ooda。"""
        from app.models.skill import ArchitectWorkflowState
        from app.services.studio_architect_events import advance_phase
        from app.models.conversation import Conversation

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        state = ArchitectWorkflowState(
            conversation_id=conv.id,
            workflow_mode="architect_mode",
            workflow_phase="ooda_iteration",
            ooda_round=1,
        )
        db.add(state)
        db.commit()

        result = advance_phase(db, conv.id)
        assert result.workflow_phase == "ooda_iteration"  # 留在 ooda
        assert result.ooda_round == 2

    def test_rollback_phase(self, db, seeded):
        """rollback_phase: ooda → phase_2，清除 phase_2+ confirmed。"""
        from app.models.skill import ArchitectWorkflowState
        from app.services.studio_architect_events import rollback_phase
        from app.models.conversation import Conversation

        conv = Conversation(user_id=seeded["admin"].id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        state = ArchitectWorkflowState(
            conversation_id=conv.id,
            workflow_mode="architect_mode",
            workflow_phase="ooda_iteration",
            phase_confirmed={"phase_1_why": True, "phase_2_what": True, "phase_3_how": True},
        )
        db.add(state)
        db.commit()

        result = rollback_phase(db, conv.id, "phase_2_what")
        assert result.workflow_phase == "phase_2_what"
        assert result.phase_confirmed.get("phase_1_why") is True  # 保留
        assert "phase_2_what" not in result.phase_confirmed  # 已清除
        assert "phase_3_how" not in result.phase_confirmed

    def test_route_includes_architect_master_skill(self, db):
        """architect_mode 激活时 assist_skills 包含 skill-architect-master。"""
        from app.services.studio_router import route_session
        r = route_session(db, skill_id=None, user_message="我想做一个分析用户的技能")
        assert r.workflow_mode == "architect_mode"
        assert "skill-architect-master" in r.active_assist_skills

    def test_route_no_architect_master_when_disabled(self, db, seeded):
        """非 architect_mode 时不包含 skill-architect-master。"""
        from app.services.studio_router import route_session
        v = db.query(SkillVersion).filter(
            SkillVersion.skill_id == seeded["skill"].id
        ).first()
        v.system_prompt = "## Role\n你是专业助手。\n## 步骤\n1. 分析\n2. 输出"
        db.commit()
        r = route_session(db, skill_id=seeded["skill"].id, user_message="帮我润色一下")
        assert r.workflow_mode == "none"
        assert "skill-architect-master" not in r.active_assist_skills
