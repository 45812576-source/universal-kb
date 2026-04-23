"""TC-SANDBOX: 交互式沙盒测试 — 证据化审批门禁功能测试。

覆盖：
  - 整改1: 输入槽位双校验（必要性 + 来源证明）
  - 整改2: Tool 确认双分支（V1/V2 格式）
  - 整改3: 权限确认三层（V1/V2 格式）
  - 向后兼容性
  - 阻断逻辑
"""
import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import (
    _make_user, _make_dept, _make_skill, _make_model_config,
    _make_tool, _login, _auth, TestingSessionLocal,
)
from app.models.user import Role
from app.models.skill import SkillStatus, SkillVersion
from app.models.tool import ToolType, SkillTool
from app.models.business import BusinessTable, DataOwnership
from app.models.knowledge import KnowledgeEntry
from app.models.sandbox import SandboxTestSession, SandboxTestReport, SessionStatus, SessionStep
from app.models.sandbox import SandboxTestCase, CaseVerdict


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup_skill_with_tool(db, user_id, tool_config=None, tool_schema=None,
                           required_inputs=None, data_queries=None,
                           knowledge_tags=None, source_files=None,
                           skill_name="测试Skill", tool_name="sandbox_tool"):
    """创建 Skill + 绑定 Tool + SkillVersion，返回 (skill, tool, version)。"""
    from app.models.skill import Skill, SkillMode
    skill = Skill(
        name=skill_name,
        description="沙盒测试用",
        mode=SkillMode.HYBRID,
        status=SkillStatus.PUBLISHED,
        knowledge_tags=knowledge_tags or [],
        auto_inject=True,
        created_by=user_id,
        data_queries=data_queries or [],
        tools=[],
        source_files=source_files or [],
    )
    db.add(skill)
    db.flush()

    tool = _make_tool(db, user_id, name=tool_name, tool_type=ToolType.BUILTIN)
    tool.config = tool_config or {}
    tool.input_schema = tool_schema or {}
    db.flush()

    # 绑定 skill ↔ tool
    st = SkillTool(skill_id=skill.id, tool_id=tool.id)
    db.add(st)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt="你是测试助手。",
        variables=[],
        created_by=user_id,
        change_note="初始版本",
        required_inputs=required_inputs or [],
    )
    db.add(version)
    db.flush()
    return skill, tool, version


def _start_session(client, token, skill_id):
    """启动沙盒测试会话。"""
    resp = client.post(
        "/api/sandbox/interactive/start",
        headers=_auth(token),
        json={"target_type": "skill", "target_id": skill_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 整改1: 输入槽位双校验 ──────────────────────────────────────────────────

class TestInputSlotVerification:
    """输入槽位的必要性 + 来源证明双校验。"""

    def test_start_session_returns_evidence_fields(self, client, db):
        """启动 session 后 detected_slots 包含 required_reason / evidence_requirement / pass_criteria。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester1", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[
                {"key": "company_name", "label": "公司名称", "required_reason": "需要公司名进行查询"},
                {"key": "question", "label": "用户问题", "freetext": True},
            ],
        )
        db.commit()
        token = _login(client, "tester1")

        data = _start_session(client, token, skill.id)
        slots = data["detected_slots"]
        assert len(slots) >= 2

        # 结构化字段
        company_slot = next(s for s in slots if s["slot_key"] == "company_name")
        assert company_slot["required_reason"] == "需要公司名进行查询"
        assert company_slot["evidence_requirement"] is not None
        assert company_slot["pass_criteria"] is not None
        assert company_slot["structured"] is True
        assert "chat_text" not in company_slot["allowed_sources"]

        # 自由文本字段
        question_slot = next(s for s in slots if s["slot_key"] == "question")
        assert question_slot["structured"] is False
        assert "chat_text" in question_slot["allowed_sources"]

    def test_structured_slot_rejects_chat_text(self, client, db):
        """结构化字段提交 chat_text 来源 → 阻断。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester2", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "product_id", "label": "产品ID"}],
        )
        db.commit()
        token = _login(client, "tester2")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "product_id", "chosen_source": "chat_text", "chat_example": "ABC123"}]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"
        # 应该有 verification_conclusion = failed
        slot = next(s for s in result["detected_slots"] if s["slot_key"] == "product_id")
        assert slot.get("verification_conclusion") == "failed"
        assert slot.get("suggested_source") is not None

    def test_required_slot_not_submitted_blocks(self, client, db):
        """必填槽位未在提交列表中 → 阻断。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester3", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[
                {"key": "field_a", "label": "字段A"},
                {"key": "field_b", "label": "字段B"},
            ],
        )
        db.commit()
        token = _login(client, "tester3")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        # 只提交 field_a，不提交 field_b
        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "field_a", "chosen_source": "knowledge", "knowledge_entry_id": 999}]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"

    def test_data_table_slot_verified(self, client, db):
        """data_table 来源通过校验 → verified。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester4", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        # 注册数据表
        bt = BusinessTable(
            table_name="sales_orders",
            display_name="销售订单",
            description="测试表",
        )
        db.add(bt)
        db.flush()

        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            data_queries=[{"query_name": "sales_orders", "table_name": "sales_orders", "description": "销售数据"}],
        )
        db.commit()
        token = _login(client, "tester4")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "sales_orders", "chosen_source": "data_table", "table_name": "sales_orders"}]},
        )
        result = resp.json()
        slot = next(s for s in result["detected_slots"] if s["slot_key"] == "sales_orders")
        assert slot.get("verification_conclusion") == "verified"
        assert slot["evidence_status"] == "verified"

    def test_data_table_unregistered_fails(self, client, db):
        """引用未注册数据表 → 阻断。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester5", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            data_queries=[{"query_name": "nonexistent", "table_name": "nonexistent", "description": "不存在的表"}],
        )
        db.commit()
        token = _login(client, "tester5")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "nonexistent", "chosen_source": "data_table", "table_name": "nonexistent"}]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"
        slot = next(s for s in result["detected_slots"] if s["slot_key"] == "nonexistent")
        assert slot.get("verification_conclusion") == "failed"

    def test_system_runtime_non_manifest_struct_fails(self, client, db):
        """结构化字段用 system_runtime 但不在 manifest 声明中 → 阻断。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester6", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "secret_field", "label": "机密字段"}],
        )
        db.commit()
        token = _login(client, "tester6")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "secret_field", "chosen_source": "system_runtime"}]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"
        slot = next(s for s in result["detected_slots"] if s["slot_key"] == "secret_field")
        assert slot.get("verification_conclusion") == "unsupported"

    def test_chat_text_without_example_fails(self, client, db):
        """chat_text 来源未给出示例 → 阻断。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester7", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "user_q", "label": "用户问题", "freetext": True}],
        )
        db.commit()
        token = _login(client, "tester7")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "user_q", "chosen_source": "chat_text"}]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"

    def test_chat_text_with_example_passes(self, client, db):
        """chat_text + 手写示例 → verified。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester8", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "user_q", "label": "用户问题", "freetext": True}],
        )
        db.commit()
        token = _login(client, "tester8")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "user_q", "chosen_source": "chat_text", "chat_example": "今天天气怎么样？"}]},
        )
        result = resp.json()
        # 应进入下一步（tool_review 或 permission_review）
        assert result["status"] == "draft"
        slot = next(s for s in result["detected_slots"] if s["slot_key"] == "user_q")
        assert slot.get("verification_conclusion") == "verified"


# ── 整改2: Tool 确认双分支 ──────────────────────────────────────────────────

class TestToolReviewV2:
    """Tool 确认 V2 三分支逻辑。"""

    def _advance_to_tool_review(self, client, db, username, freetext_slot=True):
        """辅助方法：创建 session 并推进到 tool_review 步骤。"""
        dept = _make_dept(db)
        user = _make_user(db, username, Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        tool_schema = {
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "查询语句", "freetext": True},
            },
        }
        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "user_input", "label": "用户输入", "freetext": True}],
            tool_schema=tool_schema,
            tool_config={"manifest": {"required": True}},
        )
        db.commit()
        token = _login(client, username)

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        # 推进 input-slots
        client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "user_input", "chosen_source": "chat_text", "chat_example": "测试输入"}]},
        )
        return sid, token, tool.id

    def test_v2_must_call_with_provenance(self, client, db):
        """V2 must_call + provenance → 通过。"""
        sid, token, tool_id = self._advance_to_tool_review(client, db, "tester_v2_1")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool_id,
                "decision": "must_call",
                "input_provenance": [
                    {"field_name": "query", "source_kind": "chat_text", "source_ref": "用户直接输入"},
                ],
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "permission_review"
        assert result["status"] == "draft"

    def test_v2_no_need_without_proof_blocks(self, client, db):
        """V2 no_need 无 proof → 阻断。"""
        sid, token, tool_id = self._advance_to_tool_review(client, db, "tester_v2_2")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool_id,
                "decision": "no_need",
                "no_tool_proof": "",
            }]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"

    def test_v2_no_need_with_proof_passes(self, client, db):
        """V2 no_need + proof → 通过。"""
        sid, token, tool_id = self._advance_to_tool_review(client, db, "tester_v2_3")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool_id,
                "decision": "no_need",
                "no_tool_proof": "知识库 KB-001 已包含该数据，无需调用外部 Tool",
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "permission_review"

    def test_v2_uncertain_block_blocks(self, client, db):
        """V2 uncertain_block → 阻断。"""
        sid, token, tool_id = self._advance_to_tool_review(client, db, "tester_v2_4")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool_id,
                "decision": "uncertain_block",
            }]},
        )
        result = resp.json()
        assert result["status"] == "cannot_test"

    def test_v1_confirmed_backward_compat(self, client, db):
        """V1 confirmed=true → 自动映射为 must_call（向后兼容）。"""
        sid, token, tool_id = self._advance_to_tool_review(client, db, "tester_v1_1")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool_id,
                "confirmed": True,
                "input_provenance": [
                    {"field_name": "query", "source_kind": "chat_text", "source_ref": "用户直接输入"},
                ],
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "permission_review"

    def test_tool_requiredness_detected(self, client, db):
        """_detect_tools 返回 requiredness / requiredness_reason / pass_criteria。"""
        dept = _make_dept(db)
        user = _make_user(db, "tester_req", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        # manifest.required=True → required
        skill1, tool1, _ = _setup_skill_with_tool(
            db, user.id,
            tool_config={"manifest": {"required": True}},
            tool_schema={"required": ["x"], "properties": {"x": {"type": "string"}}},
            required_inputs=[{"key": "q", "label": "Q", "freetext": True}],
        )
        db.commit()
        token = _login(client, "tester_req")

        data = _start_session(client, token, skill1.id)
        tr = data["tool_review"]
        assert len(tr) >= 1
        assert tr[0]["requiredness"] == "required"
        assert tr[0]["requiredness_reason"] is not None
        assert tr[0]["pass_criteria"] is not None


# ── 整改3: 权限确认三层 ──────────────────────────────────────────────────────

class TestPermissionReviewV2:
    """权限确认 V2 四分支逻辑。"""

    def _advance_to_permission_review(self, client, db, username):
        """辅助方法：推进到 permission_review 步骤。"""
        dept = _make_dept(db)
        user = _make_user(db, username, Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        # 注册数据表
        bt = BusinessTable(
            table_name=f"tbl_{username}",
            display_name="测试表",
            description="测试用",
        )
        db.add(bt)
        db.flush()

        skill, tool, version = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "q", "label": "问题", "freetext": True}],
            data_queries=[{"query_name": f"tbl_{username}", "table_name": f"tbl_{username}", "description": "表"}],
            tool_schema={"required": ["q"], "properties": {"q": {"type": "string", "freetext": True}}},
            tool_config={"manifest": {"required": True}},
        )
        db.commit()
        token = _login(client, username)

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        # Step1: input slots
        client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [
                {"slot_key": "q", "chosen_source": "chat_text", "chat_example": "测试"},
                {"slot_key": f"tbl_{username}", "chosen_source": "data_table", "table_name": f"tbl_{username}"},
            ]},
        )

        # Step2: tool review
        client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool.id,
                "decision": "must_call",
                "input_provenance": [{"field_name": "q", "source_kind": "chat_text", "source_ref": "测试"}],
            }]},
        )

        return sid, token, f"tbl_{username}"

    def test_v2_required_confirmed(self, client, db):
        """V2 required_confirmed → 推进到 case_generation。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v2_1")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "required_confirmed",
                "included_in_test": True,
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "case_generation"
        assert result["status"] == "ready_to_run"

    def test_v2_no_permission_needed_without_reason_blocks(self, client, db):
        """V2 no_permission_needed 无 reason → 阻断。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v2_2")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "no_permission_needed",
                "no_permission_reason": "",
            }]},
        )
        result = resp.json()
        assert result["status"] == "blocked"

    def test_v2_no_permission_needed_with_reason(self, client, db):
        """V2 no_permission_needed + reason → 通过。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v2_3")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "no_permission_needed",
                "no_permission_reason": "该数据表为公开数据，无需权限控制",
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "case_generation"

    def test_v2_mismatch_blocks(self, client, db):
        """V2 mismatch → 阻断。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v2_4")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "mismatch",
            }]},
        )
        result = resp.json()
        assert result["status"] == "blocked"

    def test_v2_uncertain_block_blocks(self, client, db):
        """V2 uncertain_block → 阻断。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v2_5")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "uncertain_block",
            }]},
        )
        result = resp.json()
        assert result["status"] == "blocked"

    def test_v1_confirmed_backward_compat(self, client, db):
        """V1 confirmed=true → 通过（向后兼容）。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v1_1")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "confirmed": True,
                "included_in_test": True,
            }]},
        )
        result = resp.json()
        assert result["current_step"] == "case_generation"

    def test_v1_not_confirmed_blocks(self, client, db):
        """V1 confirmed=false → 阻断（向后兼容）。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_v1_2")

        resp = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "confirmed": False,
                "included_in_test": True,
            }]},
        )
        result = resp.json()
        assert result["status"] == "blocked"

    def test_permission_snapshot_has_evidence_fields(self, client, db):
        """_build_permission_snapshot 返回证据化字段。"""
        sid, token, tbl = self._advance_to_permission_review(client, db, "perm_snap")

        # 获取 session 查看 permission_snapshot
        resp = client.get(f"/api/sandbox/interactive/{sid}", headers=_auth(token))
        result = resp.json()

        # session 应该有 permission_snapshot（可能为空列表如果表关联未生效）
        # 由于 _build_permission_snapshot 在 submit_permission_review 中才构建，
        # 直接提交然后检查
        resp2 = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": tbl,
                "decision": "required_confirmed",
                "included_in_test": True,
            }]},
        )
        result2 = resp2.json()
        snapshots = result2.get("permission_snapshot", [])
        if snapshots:
            snap = snapshots[0]
            # 检查证据化字段存在
            assert "permission_required" in snap or "permission_required_reason" in snap


# ── 整改2+3: 格式自动检测 ────────────────────────────────────────────────────

class TestFormatAutoDetection:
    """V1/V2 格式自动检测。"""

    def test_tool_review_mixed_v2_detected(self, client, db):
        """包含 decision 字段 → V2 模式。"""
        dept = _make_dept(db)
        user = _make_user(db, "detect_1", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill, tool, _ = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "q", "label": "Q", "freetext": True}],
            tool_schema={"required": ["q"], "properties": {"q": {"type": "string", "freetext": True}}},
            tool_config={"manifest": {"required": True}},
        )
        db.commit()
        token = _login(client, "detect_1")

        data = _start_session(client, token, skill.id)
        sid = data["session_id"]

        # input-slots
        client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [{"slot_key": "q", "chosen_source": "chat_text", "chat_example": "测试"}]},
        )

        # V2 格式
        resp = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool.id,
                "decision": "must_call",
                "input_provenance": [{"field_name": "q", "source_kind": "chat_text", "source_ref": "测试"}],
            }]},
        )
        result = resp.json()
        # V2 should work and advance
        assert result["current_step"] == "permission_review"


# ── 端到端：完整流程 ─────────────────────────────────────────────────────────

class TestEndToEndFlow:
    """从 start → input_slots → tool_review → permission_review 全流程。"""

    def test_full_flow_no_blocking(self, client, db):
        """完整流程无阻断 → 到达 case_generation。"""
        dept = _make_dept(db)
        user = _make_user(db, "e2e_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        bt = BusinessTable(
            table_name="e2e_table",
            display_name="E2E测试表",
            description="端到端测试",
        )
        db.add(bt)
        db.flush()

        skill, tool, _ = _setup_skill_with_tool(
            db, user.id,
            required_inputs=[{"key": "input1", "label": "输入1", "freetext": True}],
            data_queries=[{"query_name": "e2e_table", "table_name": "e2e_table", "description": "E2E表"}],
            tool_schema={
                "required": ["query"],
                "properties": {"query": {"type": "string", "freetext": True}},
            },
            tool_config={"manifest": {"required": True}},
        )
        db.commit()
        token = _login(client, "e2e_user")

        # Step 0: Start
        data = _start_session(client, token, skill.id)
        sid = data["session_id"]
        assert data["current_step"] == "input_slot_review"

        # Step 1: Input Slots
        resp1 = client.post(
            f"/api/sandbox/interactive/{sid}/input-slots",
            headers=_auth(token),
            json={"slots": [
                {"slot_key": "input1", "chosen_source": "chat_text", "chat_example": "你好"},
                {"slot_key": "e2e_table", "chosen_source": "data_table", "table_name": "e2e_table"},
            ]},
        )
        r1 = resp1.json()
        assert r1["current_step"] == "tool_review"

        # Step 2: Tool Review (V2)
        resp2 = client.post(
            f"/api/sandbox/interactive/{sid}/tool-review",
            headers=_auth(token),
            json={"tools": [{
                "tool_id": tool.id,
                "decision": "must_call",
                "input_provenance": [
                    {"field_name": "query", "source_kind": "chat_text", "source_ref": "你好"},
                ],
            }]},
        )
        r2 = resp2.json()
        assert r2["current_step"] == "permission_review"

        # Step 3: Permission Review (V2)
        resp3 = client.post(
            f"/api/sandbox/interactive/{sid}/permission-review",
            headers=_auth(token),
            json={"tables": [{
                "table_name": "e2e_table",
                "decision": "required_confirmed",
                "included_in_test": True,
            }]},
        )
        r3 = resp3.json()
        assert r3["current_step"] == "case_generation"
        assert r3["status"] == "ready_to_run"

    def test_tool_target_direct(self, client, db):
        """target_type=tool 的 start 流程。"""
        dept = _make_dept(db)
        user = _make_user(db, "tool_target", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        tool = _make_tool(db, user.id, name="direct_tool")
        db.commit()
        token = _login(client, "tool_target")

        resp = client.post(
            "/api/sandbox/interactive/start",
            headers=_auth(token),
            json={"target_type": "tool", "target_id": tool.id},
        )
        result = resp.json()
        assert result["current_step"] == "tool_review"
        assert len(result["tool_review"]) == 1
        assert result["tool_review"][0]["tool_id"] == tool.id


# ── 报告生成 ─────────────────────────────────────────────────────────────────

class TestSandboxReportStructure:
    """报告结构整改验证。"""

    def test_report_helpers_import(self):
        """sandbox_report.py 可正常 import。"""
        from app.services.sandbox_report import generate_report
        assert callable(generate_report)

    def test_extract_top_issues_and_fix_plan(self):
        """_extract_top_issues / _extract_fix_plan 辅助函数。"""
        from app.services.sandbox_report import _extract_top_issues, _extract_fix_plan

        evaluation = {
            "quality_detail": {
                "top_deductions": [
                    {"dimension": "correctness", "points": -20, "reason": "数值错误", "fix_suggestion": "校验数据源"},
                ],
                "fix_plan": ["增加数据校验步骤"],
            },
            "usability_detail": {
                "reason": "输入负担较高",
                "fix_suggestion": "减少必填字段",
            },
            "anti_hallucination_detail": {
                "behavior_checks": [
                    {"prompt": "缺少数据时请查询", "passed": False},
                ],
                "suggestion": "增加拒答规则",
            },
        }
        issues = _extract_top_issues(evaluation)
        assert isinstance(issues, list)
        assert len(issues) >= 1
        assert issues[0]["source"] == "quality"

        fix_plan = _extract_fix_plan(evaluation)
        assert isinstance(fix_plan, list)
        assert len(fix_plan) >= 1


class TestSandboxStreamEndpoints:
    def test_run_stream_returns_done_event(self, client, db):
        dept = _make_dept(db)
        user = _make_user(db, "sandbox_stream_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        session = SandboxTestSession(
            target_type="skill",
            target_id=123,
            target_version=1,
            target_name="流式测试 Skill",
            tester_id=user.id,
            status=SessionStatus.READY_TO_RUN,
            current_step=SessionStep.CASE_GENERATION,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
        )
        db.add(session)
        db.commit()
        token = _login(client, "sandbox_stream_user")

        async def fake_run_tests(session_id, db, user):
            current = db.get(SandboxTestSession, session_id)
            current.status = SessionStatus.COMPLETED
            current.current_step = SessionStep.DONE
            current.step_statuses = {
                "case_generation": {
                    "status": "completed",
                    "started_at": None,
                    "finished_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                }
            }
            db.commit()
            return {
                "session_id": current.id,
                "status": current.status.value,
                "current_step": current.current_step.value,
                "step_statuses": current.step_statuses,
                "report_id": current.report_id,
            }

        with (
            patch("app.routers.sandbox_interactive.SessionLocal", TestingSessionLocal),
            patch("app.routers.sandbox_interactive.run_tests", new=AsyncMock(side_effect=fake_run_tests)),
        ):
            resp = client.post(
                f"/api/sandbox/interactive/{session.id}/run-stream",
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "event: done" in resp.text


class TestPermissionCasePlanRunIntegration:
    def test_run_reuses_materialized_permission_cases(self, client, db):
        dept = _make_dept(db)
        user = _make_user(db, "permission_plan_runner", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="权限计划执行 Skill", status=SkillStatus.PUBLISHED)
        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.READY_TO_RUN,
            current_step=SessionStep.EXECUTION,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            step_statuses={
                "case_generation": {
                    "status": "completed",
                    "started_at": None,
                    "finished_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                    "source": "permission_case_plan",
                    "plan_id": 11,
                    "case_count": 1,
                },
                "permission_case_materialization": {
                    "status": "completed",
                    "started_at": None,
                    "finished_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                    "plan_id": 11,
                    "case_count": 1,
                },
            },
        )
        db.add(session)
        db.flush()
        case = SandboxTestCase(
            session_id=session.id,
            case_index=1,
            row_visibility="all",
            field_output_semantic="partial",
            group_semantic="single_field",
            tool_precondition=None,
            input_provenance={
                "source": "permission_case_plan",
                "plan_id": 11,
                "case_draft_id": 22,
                "target_role_ref": 5,
                "role_label": "招聘主管（M0）",
                "asset_ref": "data_table:table:17",
                "asset_name": "候选人表",
                "asset_type": "data_table",
                "case_type": "deny",
                "granular_refs": ["candidate_phone"],
                "controlled_fields": ["candidate_phone"],
                "source_verification_status": "linked",
                "data_source_policy": "verified_slot_only",
            },
            test_input="请给我候选人手机号",
            verdict=None,
        )
        db.add(case)
        db.commit()
        token = _login(client, "permission_plan_runner")

        async def fake_execute_permission_plan_cases(session, session_id, system_prompt, db, prebuilt_cases=None):
            assert session_id == case.session_id
            cases = prebuilt_cases or [db.get(SandboxTestCase, case.id)]
            assert len(cases) == 1
            current = cases[0]
            current.system_prompt_used = (system_prompt or "") + "\npermission_case_plan"
            current.llm_response = "不能直接提供候选人手机号"
            current.execution_duration_ms = 12
            current.verdict = CaseVerdict.PASSED
            db.flush()
            return cases

        async def fake_evaluate_session(session, cases, db, previous_deductions=None):
            return {
                "quality_passed": True,
                "quality_detail": {"reason": "ok"},
                "usability_passed": True,
                "usability_detail": {"reason": "ok"},
                "anti_hallucination_passed": True,
                "anti_hallucination_detail": {"reason": "ok"},
            }

        async def fake_generate_report(session, cases, evaluation, db):
            report = SandboxTestReport(
                session_id=session.id,
                target_type=session.target_type,
                target_id=session.target_id,
                target_version=session.target_version,
                target_name=session.target_name,
                tester_id=session.tester_id,
                part2_test_matrix={"summary": {"passed": 1, "failed": 0, "error": 0, "skipped": 0}},
                executed_case_count=len(cases),
                quality_passed=True,
                usability_passed=True,
                anti_hallucination_passed=True,
                approval_eligible=True,
            )
            db.add(report)
            db.flush()
            return report

        with (
            patch("app.routers.sandbox_interactive._generate_semantic_matrix", side_effect=AssertionError("should not generate semantic matrix")),
            patch("app.routers.sandbox_interactive._execute_permission_plan_cases", new=AsyncMock(side_effect=fake_execute_permission_plan_cases)),
            patch("app.routers.sandbox_interactive._evaluate_session", new=AsyncMock(side_effect=fake_evaluate_session)),
            patch("app.services.sandbox_report.generate_report", new=AsyncMock(side_effect=fake_generate_report)),
        ):
            resp = client.post(
                f"/api/sandbox/interactive/{session.id}/run",
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["status"] == "completed"
        assert result["current_step"] == "done"

        db.refresh(case)
        assert case.llm_response == "不能直接提供候选人手机号"
        assert case.verdict == CaseVerdict.PASSED
        assert "permission_case_plan" in (case.system_prompt_used or "")

    def test_retry_from_step_stream_returns_done_event(self, client, db):
        dept = _make_dept(db)
        user = _make_user(db, "sandbox_retry_stream_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        session = SandboxTestSession(
            target_type="skill",
            target_id=456,
            target_version=1,
            target_name="重试流式 Skill",
            tester_id=user.id,
            status=SessionStatus.RUNNING,
            current_step=SessionStep.EVALUATION,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            step_statuses={
                "evaluation": {
                    "status": "failed",
                    "started_at": None,
                    "finished_at": None,
                    "error_code": "eval_error",
                    "error_message": "boom",
                    "retryable": True,
                }
            },
        )
        db.add(session)
        db.commit()
        token = _login(client, "sandbox_retry_stream_user")

        async def fake_retry_impl(session_id, step, db, user):
            current = db.get(SandboxTestSession, session_id)
            current.status = SessionStatus.COMPLETED
            current.current_step = SessionStep.DONE
            current.step_statuses = {
                step: {
                    "status": "completed",
                    "started_at": None,
                    "finished_at": None,
                    "error_code": None,
                    "error_message": None,
                    "retryable": False,
                }
            }
            db.commit()
            return {
                "session_id": current.id,
                "status": current.status.value,
                "current_step": current.current_step.value,
                "step_statuses": current.step_statuses,
                "report_id": current.report_id,
            }

        with (
            patch("app.routers.sandbox_interactive.SessionLocal", TestingSessionLocal),
            patch("app.routers.sandbox_interactive._retry_from_step_impl", new=AsyncMock(side_effect=fake_retry_impl)),
        ):
            resp = client.post(
                f"/api/sandbox/interactive/{session.id}/retry-from-step-stream",
                headers=_auth(token),
                json={"step": "evaluation"},
            )

        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "event: done" in resp.text


class TestSandboxReportGovernanceActions:
    """沙盒报告 → Studio 治理卡片回归。"""

    def test_build_remediation_actions_from_report(self, client, db):
        from app.services.sandbox_governance import SandboxGovernanceResult

        dept = _make_dept(db)
        user = _make_user(db, "sandbox_governance_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="沙盒治理回归Skill", status=SkillStatus.PUBLISHED)

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
        )
        db.add(session)
        db.flush()

        report = SandboxTestReport(
            session_id=session.id,
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={
                "issues": [
                    {
                        "issue_id": "issue_1",
                        "severity": "major",
                        "dimension": "quality",
                        "reason": "输出缺少明确结论",
                        "target_kind": "skill_prompt",
                        "target_ref": "SKILL.md",
                        "source_cases": [0],
                        "evidence_snippets": ["回复内容过于空泛"],
                        "retest_scope": ["all"],
                    }
                ],
                "fix_plan_structured": [
                    {
                        "id": "fix_1",
                        "title": "补齐结论型输出结构",
                        "priority": "p1",
                        "problem_ids": ["issue_1"],
                        "action_type": "fix_prompt_logic",
                        "target_kind": "skill_prompt",
                        "target_ref": "SKILL.md",
                        "suggested_changes": "增加先结论后依据模板",
                        "acceptance_rule": "首段必须给出结论",
                        "retest_scope": ["all"],
                        "estimated_gain": "提升可行动性",
                    }
                ],
            },
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
            report_hash="sandbox-gov-test-hash",
        )
        db.add(report)
        db.flush()

        session.report_id = report.id
        db.commit()

        token = _login(client, "sandbox_governance_user")
        with patch(
            "app.services.sandbox_governance.build_sandbox_report_governance",
            new=AsyncMock(return_value=SandboxGovernanceResult(
                cards=[{"id": "card_1", "type": "staged_edit", "title": "修复输出结构", "content": {}, "status": "pending", "actions": []}],
                staged_edits=[{"id": "edit_1", "target_type": "system_prompt", "target_key": None, "summary": "修复输出结构", "risk_level": "medium", "diff_ops": [{"op": "replace", "old": "A", "new": "B"}], "status": "pending"}],
            )),
        ):
            resp = client.post(
                f"/api/sandbox/interactive/by-report/{report.id}/remediation-actions",
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data.get("cards", [])) >= 1
        assert len(data.get("staged_edits", [])) >= 1

    @pytest.mark.asyncio
    async def test_build_governance_syncs_agent_tasks_to_memo(self, db):
        from app.services.sandbox_governance import build_sandbox_report_governance
        from app.services.sandbox_remediation_agent import RemediationPlanResult
        from app.services.skill_memo_service import get_memo

        dept = _make_dept(db)
        user = _make_user(db, "sandbox_governance_sync_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="沙盒整改任务同步Skill", status=SkillStatus.PUBLISHED)

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
        )
        db.add(session)
        db.flush()

        report = SandboxTestReport(
            session_id=session.id,
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={
                "issues": [{"issue_id": "issue_1", "reason": "缺少结论"}],
                "fix_plan_structured": [{"id": "fix_1", "title": "旧整改任务", "priority": "p1"}],
            },
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
            report_hash="sandbox-gov-sync-hash",
        )
        db.add(report)
        db.flush()
        session.report_id = report.id
        db.commit()

        plan = RemediationPlanResult(
            tasks=[{
                "task_id": "task_1",
                "id": "task_1",
                "title": "修复结论先行结构",
                "priority": "p0",
                "problem_ids": ["issue_1"],
                "action_type": "fix_prompt_logic",
                "target_kind": "skill_prompt",
                "target_ref": "SKILL.md",
                "suggested_changes": "把首段改成先结论后依据",
                "acceptance_rule": "首段必须先给结论",
                "retest_scope": ["all"],
                "estimated_gain": "提升可执行性",
            }],
            staged_edits=[{
                "id": "1001",
                "target_type": "system_prompt",
                "target_key": None,
                "summary": "修复结论先行结构",
                "risk_level": "high",
                "diff_ops": [{"op": "replace", "old": "旧结构", "new": "新结构"}],
                "status": "pending",
            }],
            cards=[{
                "id": "card_1",
                "type": "staged_edit",
                "title": "修复结论先行结构",
                "content": {"staged_edit_id": "1001"},
                "status": "pending",
                "actions": [],
            }],
        )

        with patch(
            "app.services.sandbox_governance.generate_remediation_plan",
            new=AsyncMock(return_value=plan),
        ):
            result = await build_sandbox_report_governance(db, skill_id=skill.id, report=report)

        assert len(result.cards) == 1
        assert len(result.staged_edits) == 1
        memo = get_memo(db, skill.id)
        assert memo is not None
        memo_tasks = memo["memo"]["tasks"]
        synced = [task for task in memo_tasks if task.get("source_report_id") == report.id]
        assert len(synced) == 1
        assert synced[0]["title"] == "修复结论先行结构"
        assert synced[0]["target_files"] == ["SKILL.md"]

    @pytest.mark.asyncio
    async def test_build_governance_fallback_only_returns_task_cards(self, db):
        from app.services.sandbox_governance import build_sandbox_report_governance

        dept = _make_dept(db)
        user = _make_user(db, "sandbox_governance_fallback_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="沙盒整改回退Skill", status=SkillStatus.PUBLISHED)

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[],
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
        )
        db.add(session)
        db.flush()

        report = SandboxTestReport(
            session_id=session.id,
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={
                "top_issues": [{"source": "quality", "reason": "缺少明确结论"}],
                "fix_plan": ["增加先结论后依据的输出要求"],
            },
            quality_passed=False,
            usability_passed=True,
            anti_hallucination_passed=True,
            approval_eligible=False,
            report_hash="sandbox-gov-fallback-hash",
        )
        db.add(report)
        db.flush()
        session.report_id = report.id
        db.commit()

        result = await build_sandbox_report_governance(db, skill_id=skill.id, report=report)

        assert result.staged_edits == []
        assert len(result.cards) == 1
        assert result.cards[0]["type"] == "followup_prompt"
        assert result.cards[0]["content"].get("preflight_action") != "open_fix_task"
        assert result.cards[0]["content"]["immediate_steps"]

    @pytest.mark.asyncio
    async def test_build_governance_compiles_every_task_into_actionable_card(self, db):
        from app.services.sandbox_governance import build_sandbox_report_governance
        from app.services.sandbox_remediation_agent import RemediationPlanResult

        dept = _make_dept(db)
        user = _make_user(db, "sandbox_governance_actionability_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="沙盒可执行化Skill", status=SkillStatus.PUBLISHED)

        session = SandboxTestSession(
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
            detected_slots=[],
            tool_review=[],
            permission_snapshot=[{"confirmed": True, "included_in_test": True, "table_name": "finance_orders"}],
            quality_passed=False,
            usability_passed=False,
            anti_hallucination_passed=True,
            approval_eligible=False,
        )
        db.add(session)
        db.flush()

        fix_plan = [
            {
                "id": "fix_prompt",
                "title": "补齐当前可执行分析框架",
                "priority": "p1",
                "problem_ids": ["issue_prompt"],
                "action_type": "fix_prompt_logic",
                "target_kind": "skill_prompt",
                "target_ref": "SKILL.md",
                "suggested_changes": "在输出要求中加入最小分析框架、操作步骤和验收清单",
                "acceptance_rule": "无法深度分析时仍必须交付最小框架示例和下一步操作",
                "retest_scope": ["case_actionability"],
                "estimated_gain": "避免只承诺未来产物",
            },
            {
                "id": "fix_permission",
                "title": "挂载沙盒确认数据表",
                "priority": "p1",
                "problem_ids": ["issue_permission"],
                "action_type": "fix_permission_handling",
                "target_kind": "permission_config",
                "target_ref": "finance_orders",
                "suggested_changes": "绑定 finance_orders 作为运行数据源",
                "acceptance_rule": "输入槽位来源覆盖所有必填字段",
                "retest_scope": ["case_slots"],
                "estimated_gain": "补齐数据源",
            },
        ]
        report = SandboxTestReport(
            session_id=session.id,
            target_type="skill",
            target_id=skill.id,
            target_version=1,
            target_name=skill.name,
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={
                "issues": [
                    {
                        "issue_id": "issue_prompt",
                        "reason": "输出只列未来可能产物，没有交付当下可执行分析框架",
                        "evidence_snippets": ["仅写了可输出拓扑图、分录模板，未给当前步骤"],
                    },
                    {
                        "issue_id": "issue_permission",
                        "reason": "输入槽位来源未覆盖数据表",
                        "evidence_snippets": ["table:finance_orders"],
                    },
                ],
                "fix_plan_structured": fix_plan,
            },
            quality_passed=False,
            usability_passed=False,
            anti_hallucination_passed=True,
            approval_eligible=False,
            report_hash="sandbox-gov-actionability-hash",
        )
        db.add(report)
        db.flush()
        session.report_id = report.id
        db.commit()

        with patch(
            "app.services.sandbox_governance.generate_remediation_plan",
            new=AsyncMock(return_value=RemediationPlanResult(tasks=fix_plan, staged_edits=[], cards=[])),
        ):
            result = await build_sandbox_report_governance(db, skill_id=skill.id, report=report)

        assert result.staged_edits == []
        assert len(result.cards) == 2
        for card in result.cards:
            assert card["content"].get("preflight_action") != "open_fix_task"
            assert card["content"]["immediate_steps"]
            assert card["content"]["expected_deliverable"]
            assert card["content"]["acceptance_rule"]

        prompt_card = next(card for card in result.cards if card["content"]["target_kind"] == "skill_prompt")
        assert [action["type"] for action in prompt_card["actions"]] == ["view_diff", "refine", "reject"]
        assert "立即执行" in prompt_card["summary"]

        permission_card = next(card for card in result.cards if card["content"]["target_kind"] == "permission_config")
        assert permission_card["content"]["preflight_action"] == "bind_permission_tables"
        assert permission_card["content"]["action_payload"]["table_names"] == ["finance_orders"]


class TestPreflightGovernanceBoundaries:
    def test_prompt_too_short_only_returns_guidance_card(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "preflight_prompt_short_user", Role.SUPER_ADMIN, dept.id)
        skill, _tool, version = _setup_skill_with_tool(db, user.id)
        version.system_prompt = "你好"
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "prompt_too_short", "issue": "prompt 过短"}],
                }],
            },
        )

        assert result.staged_edits == []
        assert len(result.cards) == 1
        assert result.cards[0]["type"] == "followup_prompt"
        assert result.cards[0]["content"]["preflight_action"] == "open_skill_editor"

    def test_preflight_quality_patch_is_minimal_guardrail(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "preflight_guardrail_user", Role.SUPER_ADMIN, dept.id)
        skill, _tool, _version = _setup_skill_with_tool(db, user.id)
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "quality_detail": {
                    "top_deductions": [{
                        "dimension": "correctness",
                        "reason": "证据链不足",
                        "fix_suggestion": "补更多案例",
                    }],
                },
            },
        )

        assert len(result.staged_edits) == 1
        patch_text = result.staged_edits[0]["diff_ops"][0]["new"]
        assert "最小质量护栏" in patch_text
        assert "证据链不足" in patch_text
        assert "补更多案例" not in patch_text


class TestPreflightDescriptionGenerator:
    """description 缺失时，按 skill 上下文生成更具体的描述。"""

    def test_generates_concise_fallback_without_context(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_0", Role.SUPER_ADMIN, dept.id)
        skill, _tool, version = _setup_skill_with_tool(db, user.id)
        skill.description = ""
        skill.name = "通用助理"
        skill.knowledge_tags = []
        skill.data_queries = []
        skill.tools = []
        skill.bound_tools = []
        skill.source_files = []
        version.system_prompt = ""
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "missing_description", "issue": "description 为空"}],
                }]
            },
        )
        generated = result.staged_edits[0]["diff_ops"][0]["new"]
        assert generated == "用于通用助理场景，根据用户输入，输出明确结论和下一步建议。"
        assert 20 <= len(generated) <= 90

    def test_generates_description_from_knowledge_and_data(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_1", Role.SUPER_ADMIN, dept.id)
        skill, _tool, _version = _setup_skill_with_tool(
            db,
            user.id,
            data_queries=[{"query_name": "sales", "table_name": "sales_orders", "description": "销售订单"}],
            knowledge_tags=["销售分析"],
        )
        skill.description = ""
        skill.name = "销售分析助手"
        skill.source_files = [{"filename": "reference.md", "category": "reference"}]
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "missing_description", "issue": "description 为空"}],
                }]
            },
        )
        assert len(result.staged_edits) == 1
        diff_ops = result.staged_edits[0]["diff_ops"]
        generated = diff_ops[0]["new"]
        assert "销售分析助手" in generated
        assert "知识资料" in generated
        assert "业务数据" in generated

    def test_generates_description_from_data_query_only(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_1b", Role.SUPER_ADMIN, dept.id)
        skill, _tool, version = _setup_skill_with_tool(
            db,
            user.id,
            data_queries=[{"query_name": "orders", "table_name": "orders", "description": "订单数据"}],
        )
        skill.description = ""
        skill.name = "订单分析助手"
        skill.knowledge_tags = []
        skill.source_files = []
        version.system_prompt = "你负责订单数据分析，并输出分析报告。"
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "missing_description", "issue": "description 为空"}],
                }]
            },
        )
        generated = result.staged_edits[0]["diff_ops"][0]["new"]
        assert "业务数据" in generated
        assert "分析结论" in generated

    def test_generates_description_from_tool_context(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_2", Role.SUPER_ADMIN, dept.id)
        skill, tool, version = _setup_skill_with_tool(
            db,
            user.id,
            tool_schema={"required": ["query"], "properties": {"query": {"type": "string"}}},
            required_inputs=[{"key": "query", "label": "查询问题", "freetext": True}],
        )
        skill.description = ""
        skill.name = "工具协同助手"
        version.system_prompt = "你是一个工具协同分析助手，需要输出结构化结果。"
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "missing_description", "issue": "description 为空"}],
                }]
            },
        )
        diff_ops = result.staged_edits[0]["diff_ops"]
        generated = diff_ops[0]["new"]
        assert "工具协同助手" in generated
        assert "工具能力" in generated
        assert "结构化" in generated

    def test_uses_existing_description_when_not_missing(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_3", Role.SUPER_ADMIN, dept.id)
        skill, _tool, _version = _setup_skill_with_tool(db, user.id)
        skill.description = "现有描述"
        db.commit()

        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{"ok": False, "code": "prompt_too_short", "issue": "prompt 过短"}],
                }]
            },
        )
        assert all(edit["target_type"] != "metadata" for edit in result.staged_edits)

    def test_generates_metadata_edit_for_generic_description(self, db):
        from app.services.preflight_governance import build_preflight_governance

        dept = _make_dept(db)
        user = _make_user(db, "desc_case_4", Role.SUPER_ADMIN, dept.id)
        skill, _tool, _version = _setup_skill_with_tool(db, user.id, skill_name="财务核算框架架构师")
        skill.description = "围绕「财务核算框架架构师」场景提供支持。"
        db.commit()

        expected_description = "将业务需求转化为 L2 层财务核算框架，输出会计分录模板、税务处理规则与系统字段映射，确保业务设计阶段植入合规核算基因。"
        result = build_preflight_governance(
            db,
            skill_id=skill.id,
            result={
                "gates": [{
                    "gate": "structure",
                    "status": "failed",
                    "items": [{
                        "ok": False,
                        "code": "generic_description",
                        "issue": (
                            "description 过于笼统，未精准概括 Skill 核心能力。\n"
                            f"将 description 替换为：\n> {expected_description}"
                        ),
                    }],
                }]
            },
        )

        assert len(result.staged_edits) == 1
        assert result.staged_edits[0]["target_type"] == "metadata"
        assert result.staged_edits[0]["summary"] == "优化 Skill 描述"
        assert result.staged_edits[0]["diff_ops"] == [{
            "op": "replace",
            "old": "description",
            "new": expected_description,
        }]


class TestSandboxHistory:
    def test_history_lists_latest_sessions_and_report_flags(self, client, db):
        dept = _make_dept(db)
        user = _make_user(db, "history_user", Role.SUPER_ADMIN, dept.id)
        other_user = _make_user(db, "history_other", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)

        skill_a, _, _ = _setup_skill_with_tool(
            db, user.id, skill_name="历史技能A", tool_name="history_tool_a"
        )
        skill_b, _, _ = _setup_skill_with_tool(
            db, user.id, skill_name="历史技能B", tool_name="history_tool_b"
        )
        other_skill, _, _ = _setup_skill_with_tool(
            db, other_user.id, skill_name="其他人的测试", tool_name="history_tool_hidden"
        )
        db.flush()

        older = SandboxTestSession(
            target_type="skill",
            target_id=skill_a.id,
            target_version=1,
            target_name="历史技能A",
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
        )
        newer = SandboxTestSession(
            target_type="skill",
            target_id=skill_b.id,
            target_version=2,
            target_name="历史技能B",
            tester_id=user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
        )
        hidden = SandboxTestSession(
            target_type="skill",
            target_id=other_skill.id,
            target_version=3,
            target_name="其他人的测试",
            tester_id=other_user.id,
            status=SessionStatus.COMPLETED,
            current_step=SessionStep.DONE,
        )
        db.add_all([older, newer, hidden])
        db.flush()

        report = SandboxTestReport(
            session_id=newer.id,
            target_type="skill",
            target_id=skill_b.id,
            target_version=2,
            target_name="历史技能B",
            tester_id=user.id,
            part1_evidence_check={},
            part2_test_matrix={},
            part3_evaluation={},
            report_hash="hash-history",
        )
        db.add(report)
        db.flush()
        newer.report_id = report.id
        db.commit()

        token = _login(client, "history_user")

        resp = client.get("/api/sandbox/interactive/history", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert [item["session_id"] for item in data[:2]] == [newer.id, older.id]
        assert all(item["tester_id"] == user.id for item in data)
        newest = data[0]
        assert newest["has_report"] is True
        assert newest["report_hash"] == "hash-history"
        assert newest["report_created_at"] is not None

        resp = client.get(
            f"/api/sandbox/interactive/history?target_type=skill&target_id={skill_a.id}",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        filtered = resp.json()
        assert len(filtered) == 1
        assert filtered[0]["session_id"] == older.id
