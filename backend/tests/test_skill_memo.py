"""Skill Memo — 全场景用例 + 压力测试。

覆盖范围：
1. 数据模型完整性
2. 三大场景 (new_skill_creation / import_remediation / published_iteration)
3. 任务推进完整生命周期
4. 边界条件 / 异常路径
5. API 路由层
6. 状态机迁移
7. 并发 & 压力测试
"""
import pytest
import json
import threading
import time

from tests.conftest import (
    _make_user,
    _make_dept,
    _make_model_config,
    _make_skill,
    _login,
    _auth,
    TestingSessionLocal,
)

from app.models.skill import Skill, SkillStatus, SkillMode, SkillVersion
from app.models.skill_memo import SkillMemo
from app.services import skill_memo_service


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 数据模型基础测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestSkillMemoModel:
    """ORM 模型与数据库表映射是否正确。"""

    def test_create_memo(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "memo_model_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="Model测试Skill")

        memo = SkillMemo(
            skill_id=skill.id,
            scenario_type="new_skill_creation",
            lifecycle_stage="planning",
            status_summary="初始化",
            memo_payload={"tasks": [], "persistent_notices": []},
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(memo)
        db.commit()

        fetched = db.query(SkillMemo).filter(SkillMemo.skill_id == skill.id).first()
        assert fetched is not None
        assert fetched.scenario_type == "new_skill_creation"
        assert fetched.lifecycle_stage == "planning"
        assert fetched.version == 1
        assert fetched.memo_payload["tasks"] == []

    def test_unique_skill_id_constraint(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "unique_test_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="UniqueSkill")

        memo1 = SkillMemo(
            skill_id=skill.id,
            scenario_type="new_skill_creation",
            lifecycle_stage="planning",
            status_summary="first",
            memo_payload={},
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(memo1)
        db.commit()

        memo2 = SkillMemo(
            skill_id=skill.id,
            scenario_type="import_remediation",
            lifecycle_stage="analysis",
            status_summary="duplicate",
            memo_payload={},
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(memo2)
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

    def test_json_payload_roundtrip(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "json_test_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="JSONSkill")

        complex_payload = {
            "tasks": [{"id": "task_abc", "title": "测试中文任务", "status": "todo"}],
            "persistent_notices": [{"id": "n1", "title": "提醒", "status": "active"}],
            "nested": {"deep": {"value": [1, 2, 3]}},
        }
        memo = SkillMemo(
            skill_id=skill.id,
            scenario_type="new_skill_creation",
            lifecycle_stage="planning",
            status_summary="",
            memo_payload=complex_payload,
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(memo)
        db.commit()

        fetched = db.query(SkillMemo).filter(SkillMemo.id == memo.id).first()
        assert fetched.memo_payload["tasks"][0]["title"] == "测试中文任务"
        assert fetched.memo_payload["nested"]["deep"]["value"] == [1, 2, 3]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Service 层 — 新建 Skill 场景
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewSkillCreation:
    """场景一：new_skill_creation 完整生命周期。"""

    def _setup(self, db):
        dept = _make_dept(db, "新建场景部门")
        user = _make_user(db, "new_creator", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="新建Skill测试", status=SkillStatus.DRAFT)
        return user, skill

    def test_init_with_goal(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "帮助用户写营销文案", user.id
        )
        assert result is not None
        assert result["lifecycle_stage"] == "planning"
        assert result["scenario_type"] == "new_skill_creation"
        # 有 goal_summary 时 define_goal 应该已完成
        tasks = result["memo"]["tasks"]
        goal_task = [t for t in tasks if t["type"] == "define_goal"][0]
        assert goal_task["status"] == "done"

    def test_init_without_goal(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", None, user.id
        )
        tasks = result["memo"]["tasks"]
        goal_task = [t for t in tasks if t["type"] == "define_goal"][0]
        assert goal_task["status"] == "todo"

    def test_default_task_tree(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "测试目标", user.id
        )
        tasks = result["memo"]["tasks"]
        types = [t["type"] for t in tasks]
        assert "define_goal" in types
        assert "edit_skill_md" in types
        assert "create_file" in types
        assert "run_test" in types
        assert len(tasks) == 5  # goal + edit + example + reference + test

    def test_task_dependencies(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "测试目标", user.id
        )
        tasks = result["memo"]["tasks"]
        goal_id = tasks[0]["id"]
        edit_id = tasks[1]["id"]

        # edit_skill_md depends on define_goal
        assert goal_id in tasks[1]["depends_on"]
        # create_file depends on edit_skill_md
        assert edit_id in tasks[2]["depends_on"]

    def test_idempotent_init(self, db):
        """不 force_rebuild 时，重复 init 返回已有 memo。"""
        user, skill = self._setup(db)
        r1 = skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标A", user.id)
        r2 = skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标B", user.id)
        # 应该返回相同的 memo (不覆盖)
        assert r1["goal_summary"] == r2["goal_summary"]

    def test_force_rebuild(self, db):
        """force_rebuild=True 时覆盖已有 memo。"""
        user, skill = self._setup(db)
        skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标A", user.id)
        r2 = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "目标B", user.id, force_rebuild=True
        )
        assert r2["goal_summary"] == "目标B"

    def test_full_lifecycle_new_skill(self, db):
        """完整走一遍新建 Skill 生命周期：init → start → save → complete → test。"""
        user, skill = self._setup(db)

        # 1. 初始化
        memo = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "写营销文案", user.id
        )
        tasks = memo["memo"]["tasks"]

        # 2. define_goal 已完成（有 goal_summary），找下一个可做的任务
        edit_task = [t for t in tasks if t["type"] == "edit_skill_md"][0]
        assert memo["current_task"] is None or memo["current_task"]["type"] != "edit_skill_md"

        # 3. 开始 edit_skill_md
        start_result = skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        assert start_result["ok"]
        assert start_result["current_task"]["status"] == "in_progress"
        assert start_result["editor_target"]["filename"] == "SKILL.md"

        # 4. 保存 SKILL.md → 完成任务
        complete_result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "SKILL.md", "prompt", 500
        )
        assert complete_result["ok"]
        assert complete_result["task_completed"]

        # 5. 自动切到下一任务
        assert complete_result["current_task"] is not None

        # 6. 保存 example
        example_task = complete_result["current_task"]
        skill_memo_service.start_task(db, skill.id, example_task["id"], user.id)
        ex_result = skill_memo_service.complete_from_save(
            db, skill.id, example_task["id"], "example-basic.md", "asset", 200
        )
        assert ex_result["ok"]
        assert ex_result["task_completed"]

        # 7. 记录测试通过
        test_result = skill_memo_service.record_test_result(
            db, skill.id, "preflight", 1, "passed", "质量检测通过", user_id=user.id
        )
        assert test_result["ok"]

        # 8. 验证最终状态
        final = skill_memo_service.get_memo(db, skill.id)
        # 应该处于 awaiting_test 或 ready_to_submit
        assert final["lifecycle_stage"] in ("awaiting_test", "ready_to_submit", "editing")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Service 层 — 导入分析场景
# ═══════════════════════════════════════════════════════════════════════════════


class TestImportRemediation:
    """场景二：import_remediation。"""

    def _setup(self, db, prompt="你是营销助手。请根据参考资料生成文案。", source_files=None):
        dept = _make_dept(db, "导入部门")
        user = _make_user(db, "importer", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="导入Skill", status=SkillStatus.DRAFT)
        # 更新 prompt
        ver = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).first()
        if ver:
            ver.system_prompt = prompt
        if source_files is not None:
            skill.source_files = source_files
        db.commit()
        return user, skill

    def test_analyze_missing_example(self, db):
        """无 example 文件时检测到缺失。"""
        user, skill = self._setup(db, source_files=[])
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        assert result["ok"]
        codes = [m["code"] for m in result["analysis"]["missing_items"]]
        assert "missing_example" in codes

    def test_analyze_missing_reference_with_keyword(self, db):
        """prompt 含参考关键词但无 reference 文件时检测到缺失。"""
        user, skill = self._setup(
            db,
            prompt="你是助手。请根据参考资料和API文档来回答。",
            source_files=[]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        codes = [m["code"] for m in result["analysis"]["missing_items"]]
        assert "missing_reference" in codes

    def test_analyze_no_missing_reference_without_keyword(self, db):
        """prompt 不含参考关键词时，不报 missing_reference。"""
        user, skill = self._setup(
            db,
            prompt="你是一个简单的助手。请回答用户问题。",
            source_files=[]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        codes = [m["code"] for m in result["analysis"]["missing_items"]]
        assert "missing_reference" not in codes

    def test_analyze_missing_template(self, db):
        """prompt 含固定格式关键词时检测到缺失模板。"""
        user, skill = self._setup(
            db,
            prompt="请按照固定格式输出JSON格式的报告。",
            source_files=[{"filename": "example-1.md", "size": 100, "category": "example"}]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        codes = [m["code"] for m in result["analysis"]["missing_items"]]
        assert "missing_template" in codes

    def test_analyze_complete_structure(self, db):
        """结构完整时不报缺失。"""
        user, skill = self._setup(
            db,
            prompt="你是助手。",
            source_files=[
                {"filename": "example-1.md", "size": 100, "category": "example"},
                {"filename": "reference-api.md", "size": 200, "category": "reference"},
            ]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        assert len(result["analysis"]["missing_items"]) == 0

    def test_analyze_generates_notices(self, db):
        """缺失项应同时生成 persistent_notices。"""
        user, skill = self._setup(db, source_files=[])
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        memo = result["memo"]
        assert len(memo["persistent_notices"]) > 0
        assert all(n["status"] == "active" for n in memo["persistent_notices"])

    def test_analyze_generates_tasks(self, db):
        """缺失项应生成对应任务。"""
        user, skill = self._setup(db, source_files=[])
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        memo = result["memo"]
        tasks = memo["memo"]["tasks"]
        # 至少有 missing_example 的 create_file 任务 + run_test
        assert len(tasks) >= 2

    def test_analyze_always_has_test_task(self, db):
        """即使结构完整，也应有 run_test 任务。"""
        user, skill = self._setup(
            db,
            prompt="你是助手。",
            source_files=[{"filename": "example-1.md", "size": 100, "category": "example"}]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        types = [t["type"] for t in result["memo"]["memo"]["tasks"]]
        assert "run_test" in types

    def test_analyze_directory_tree(self, db):
        """directory_tree 应包含 SKILL.md 和所有 source_files。"""
        user, skill = self._setup(
            db,
            source_files=[
                {"filename": "example-1.md", "size": 100, "category": "example"},
                {"filename": "ref.md", "size": 50, "category": "reference"},
            ]
        )
        result = skill_memo_service.analyze_import(db, skill.id, user.id)
        tree = result["analysis"]["directory_tree"]
        assert "SKILL.md" in tree
        assert "example-1.md" in tree
        assert "ref.md" in tree


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Service 层 — 任务推进核心逻辑
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskProgression:
    """任务 start → complete → next 链路。"""

    def _setup_with_memo(self, db):
        dept = _make_dept(db, "任务推进部门")
        user = _make_user(db, "task_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="任务推进Skill", status=SkillStatus.DRAFT)
        memo = skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "测试目标", user.id
        )
        return user, skill, memo

    def test_start_task_not_found(self, db):
        user, skill, memo = self._setup_with_memo(db)
        result = skill_memo_service.start_task(db, skill.id, "nonexistent_id", user.id)
        assert not result["ok"]
        assert "not found" in result["error"].lower() or "Task" in result["error"]

    def test_start_task_changes_status(self, db):
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        result = skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        assert result["ok"]
        assert result["current_task"]["status"] == "in_progress"

    def test_start_task_sets_lifecycle_to_editing(self, db):
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        updated = skill_memo_service.get_memo(db, skill.id)
        assert updated["lifecycle_stage"] == "editing"

    def test_complete_wrong_file(self, db):
        """保存的文件不在 target_files 里，不应完成任务。"""
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "random_file.md", "asset", 100
        )
        assert result["ok"]
        assert not result["task_completed"]

    def test_complete_empty_file(self, db):
        """保存空文件不应完成任务。"""
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "SKILL.md", "prompt", 0
        )
        assert result["ok"]
        assert not result["task_completed"]

    def test_complete_writes_progress_log(self, db):
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "SKILL.md", "prompt", 500
        )
        updated = result["memo"]
        log = updated["memo"]["progress_log"]
        assert len(log) >= 1
        assert log[-1]["kind"] == "task_completed"

    def test_complete_writes_context_rollup(self, db):
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "SKILL.md", "prompt", 500
        )
        assert "rollup" in result
        rollups = result["memo"]["memo"]["context_rollups"]
        assert len(rollups) >= 1

    def test_complete_clears_related_notice(self, db):
        """完成任务时应清除关联的 persistent_notice。"""
        dept = _make_dept(db, "清除通知部门")
        user = _make_user(db, "notice_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="清除通知Skill", status=SkillStatus.DRAFT)
        ver = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).first()
        if ver:
            ver.system_prompt = "你是助手"
        db.commit()

        # 使用 analyze_import 生成带 notice 的 memo
        skill_memo_service.analyze_import(db, skill.id, user.id)
        memo = skill_memo_service.get_memo(db, skill.id)
        notices = memo["persistent_notices"]
        assert len(notices) > 0  # 应该有 missing_example

        # 找到关联任务
        task_id = notices[0]["related_task_ids"][0]
        skill_memo_service.start_task(db, skill.id, task_id, user.id)

        # 完成任务
        task = next(t for t in memo["memo"]["tasks"] if t["id"] == task_id)
        filename = task["target_files"][0] if task["target_files"] else "example-basic.md"
        skill_memo_service.complete_from_save(
            db, skill.id, task_id, filename, "asset", 200
        )

        # 验证 notice 已 resolved
        updated = skill_memo_service.get_memo(db, skill.id)
        active_notices = [n for n in updated["persistent_notices"] if n["status"] == "active"]
        resolved_in_payload = [
            n for n in updated["memo"]["persistent_notices"] if n["status"] == "resolved"
        ]
        assert len(resolved_in_payload) >= 1

    def test_auto_picks_next_task(self, db):
        user, skill, memo = self._setup_with_memo(db)
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        skill_memo_service.start_task(db, skill.id, edit_task["id"], user.id)
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task["id"], "SKILL.md", "prompt", 500
        )
        assert result["current_task"] is not None
        assert result["current_task"]["type"] in ("create_file", "run_test")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Service 层 — 直接测试 & 测试结果
# ═══════════════════════════════════════════════════════════════════════════════


class TestDirectTestAndResults:
    """direct_test + record_test_result。"""

    def _setup(self, db):
        dept = _make_dept(db, "测试部门")
        user = _make_user(db, "tester", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="测试Skill", status=SkillStatus.DRAFT)
        skill_memo_service.init_memo(
            db, skill.id, "new_skill_creation", "目标", user.id
        )
        return user, skill

    def test_direct_test_no_memo(self, db):
        dept = _make_dept(db, "无memo部门")
        user = _make_user(db, "no_memo_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="NoMemoSkill", status=SkillStatus.DRAFT)
        result = skill_memo_service.direct_test(db, skill.id, user.id)
        assert not result["ok"]

    def test_direct_test_sets_testing(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.direct_test(db, skill.id, user.id)
        assert result["ok"]
        assert result["lifecycle_stage"] == "testing"
        assert result["notices_remain"] is True

    def test_direct_test_writes_log(self, db):
        user, skill = self._setup(db)
        skill_memo_service.direct_test(db, skill.id, user.id)
        memo = skill_memo_service.get_memo(db, skill.id)
        log = memo["memo"]["progress_log"]
        assert any(l["kind"] == "direct_test_decision" for l in log)

    def test_test_passed(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.record_test_result(
            db, skill.id, "preflight", 1, "passed", "全部通过", user_id=user.id
        )
        assert result["ok"]
        memo = skill_memo_service.get_memo(db, skill.id)
        assert memo["latest_test"]["status"] == "passed"

    def test_test_failed_generates_tasks(self, db):
        user, skill = self._setup(db)
        followups = [
            {"title": "修复输出格式", "target_files": ["SKILL.md"]},
            {"title": "补充示例", "target_files": ["example-fix.md"]},
        ]
        result = skill_memo_service.record_test_result(
            db, skill.id, "sandbox", 1, "failed", "格式不符合要求",
            suggested_followups=followups, user_id=user.id,
        )
        assert result["ok"]
        assert len(result["generated_task_ids"]) == 2
        memo = skill_memo_service.get_memo(db, skill.id)
        assert memo["lifecycle_stage"] == "fixing"

    def test_test_passed_marks_run_test_done(self, db):
        user, skill = self._setup(db)
        skill_memo_service.record_test_result(
            db, skill.id, "manual", 1, "passed", "通过", user_id=user.id
        )
        memo = skill_memo_service.get_memo(db, skill.id)
        run_tests = [t for t in memo["memo"]["tasks"] if t["type"] == "run_test"]
        for t in run_tests:
            assert t["status"] == "done"

    def test_test_no_memo(self, db):
        dept = _make_dept(db, "无memo测试部")
        user = _make_user(db, "no_memo_tester", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="NoMemoTestSkill", status=SkillStatus.DRAFT)
        result = skill_memo_service.record_test_result(
            db, skill.id, "manual", 1, "passed", "通过", user_id=user.id
        )
        assert result["ok"]
        memo = skill_memo_service.get_memo(db, skill.id)
        assert memo is not None
        assert memo["latest_test"]["status"] == "passed"

    def test_test_result_records_report_knowledge_and_fingerprint(self, db):
        user, skill = self._setup(db)
        result = skill_memo_service.record_test_result(
            db,
            skill.id,
            "sandbox_interactive",
            1,
            "failed",
            "需要整改",
            user_id=user.id,
            source_report_id=88,
            source_report_knowledge_id=123,
            source_report_knowledge_title="2026-04-15-测试员-技能-v1-沙盒测试报告",
        )
        assert result["ok"]
        latest = skill_memo_service.get_memo(db, skill.id)["latest_test"]
        assert latest["source_report_id"] == 88
        assert latest["source_report_knowledge_id"] == 123
        assert latest["source_report_knowledge_title"] == "2026-04-15-测试员-技能-v1-沙盒测试报告"
        assert latest["artifact_fingerprint"]
        assert latest["details"]["report_knowledge"]["knowledge_entry_id"] == 123

    def test_assess_test_start_blocks_unchanged_and_allows_after_diff(self, db):
        user, skill = self._setup(db)
        skill_memo_service.record_test_result(
            db, skill.id, "preflight", 1, "failed", "知识库未就绪", user_id=user.id
        )
        blocked = skill_memo_service.assess_test_start(db, skill.id)
        assert not blocked["allowed"]
        assert blocked["reason"] == "unchanged_since_last_test"

        diff_result = skill_memo_service.record_post_test_diff(
            db,
            skill.id,
            change_type="staged_edit_adopted",
            source="studio_governance",
            summary="已采纳自动整改 diff",
            user_id=user.id,
            diff_ops=[{"op": "insert", "old": "", "new": "\n补充知识库说明"}],
            auto_generated=True,
        )
        assert diff_result["ok"]

        allowed = skill_memo_service.assess_test_start(db, skill.id)
        assert allowed["allowed"]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Service 层 — 反馈采纳
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdoptFeedback:

    def test_adopt_creates_task(self, db):
        dept = _make_dept(db, "反馈部门")
        user = _make_user(db, "feedback_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="反馈Skill", status=SkillStatus.DRAFT)
        skill_memo_service.init_memo(db, skill.id, "published_iteration", None, user.id)

        result = skill_memo_service.adopt_feedback(
            db, skill.id, "comment", 42, "需要增加错误处理", {}, user.id
        )
        assert result["ok"]
        assert result["generated_task_id"]
        assert result["current_task"]["type"] == "adopt_feedback_change"

    def test_adopt_auto_creates_memo(self, db):
        """已发布 Skill 没有 memo 时，adopt_feedback 自动创建。"""
        dept = _make_dept(db, "自动创建部")
        user = _make_user(db, "auto_create_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="AutoCreateMemoSkill", status=SkillStatus.PUBLISHED)

        result = skill_memo_service.adopt_feedback(
            db, skill.id, "comment", 1, "增加示例", {}, user.id
        )
        assert result["ok"]
        memo = skill_memo_service.get_memo(db, skill.id)
        assert memo is not None

    def test_adopt_records_feedback_entry(self, db):
        dept = _make_dept(db, "记录反馈部")
        user = _make_user(db, "record_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="RecordFeedbackSkill", status=SkillStatus.DRAFT)
        skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标", user.id)

        skill_memo_service.adopt_feedback(
            db, skill.id, "comment", 99, "改进输出", {}, user.id
        )
        memo = skill_memo_service.get_memo(db, skill.id)
        feedbacks = memo["memo"]["adopted_feedback"]
        assert len(feedbacks) == 1
        assert feedbacks[0]["source_id"] == 99

    def test_adopt_reverts_lifecycle(self, db):
        """在 ready_to_submit 状态下采纳反馈，应回到 editing。"""
        dept = _make_dept(db, "回退部门")
        user = _make_user(db, "revert_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="RevertSkill", status=SkillStatus.DRAFT)
        skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标", user.id)

        # 手动设 ready_to_submit
        sm = db.query(SkillMemo).filter(SkillMemo.skill_id == skill.id).first()
        sm.lifecycle_stage = "ready_to_submit"
        db.commit()

        skill_memo_service.adopt_feedback(
            db, skill.id, "comment", 1, "需要改进", {}, user.id
        )
        memo = skill_memo_service.get_memo(db, skill.id)
        assert memo["lifecycle_stage"] == "editing"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Service 层 — 状态机迁移
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycleStateMachine:
    """_advance_lifecycle 状态迁移规则。"""

    def test_editing_to_awaiting_test(self, db):
        """所有非测试任务完成 → editing → awaiting_test。"""
        dept = _make_dept(db, "状态机部门")
        user = _make_user(db, "lifecycle_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="LifecycleSkill", status=SkillStatus.DRAFT)

        # 手动构建简单 memo
        payload = skill_memo_service._empty_payload()
        edit_task_id = "task_edit"
        payload["tasks"] = [
            {
                "id": edit_task_id, "title": "编辑", "type": "edit_skill_md",
                "status": "in_progress", "priority": "high", "source": "test",
                "description": "edit", "target_files": ["SKILL.md"],
                "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
                "depends_on": [], "started_at": None, "completed_at": None,
                "completed_by": None, "result_summary": None,
            },
            {
                "id": "task_test", "title": "测试", "type": "run_test",
                "status": "todo", "priority": "high", "source": "test",
                "description": "test", "target_files": [],
                "acceptance_rule": {"mode": "test_record_created"},
                "depends_on": [edit_task_id], "started_at": None,
                "completed_at": None, "completed_by": None, "result_summary": None,
            },
        ]
        payload["current_task_id"] = edit_task_id

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="editing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        # 完成编辑任务
        result = skill_memo_service.complete_from_save(
            db, skill.id, edit_task_id, "SKILL.md", "prompt", 500
        )
        updated = skill_memo_service.get_memo(db, skill.id)
        assert updated["lifecycle_stage"] == "awaiting_test"

    def test_fixing_to_awaiting_test(self, db):
        """所有 fix_after_test 完成 → fixing → awaiting_test。"""
        dept = _make_dept(db, "修复部门")
        user = _make_user(db, "fix_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="FixSkill", status=SkillStatus.DRAFT)

        payload = skill_memo_service._empty_payload()
        fix_id = "task_fix1"
        payload["tasks"] = [{
            "id": fix_id, "title": "修复", "type": "fix_after_test",
            "status": "in_progress", "priority": "high", "source": "test_failure",
            "description": "fix it", "target_files": ["SKILL.md"],
            "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
            "depends_on": [], "started_at": None, "completed_at": None,
            "completed_by": None, "result_summary": None,
        }]
        payload["current_task_id"] = fix_id

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="fixing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        result = skill_memo_service.complete_from_save(
            db, skill.id, fix_id, "SKILL.md", "prompt", 300
        )
        updated = skill_memo_service.get_memo(db, skill.id)
        assert updated["lifecycle_stage"] == "awaiting_test"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Service 层 — get_memo 边界条件
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetMemoEdgeCases:

    def test_get_memo_nonexistent(self, db):
        result = skill_memo_service.get_memo(db, 99999)
        assert result is None

    def test_get_memo_empty_payload(self, db):
        dept = _make_dept(db, "空载部门")
        user = _make_user(db, "empty_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="EmptySkill")
        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="planning", status_summary="",
            memo_payload={}, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()
        result = skill_memo_service.get_memo(db, skill.id)
        assert result is not None
        assert result["current_task"] is None
        assert result["next_task"] is None
        assert result["latest_test"] is None


class TestSyncRemediationTasks:
    """agent 生成的整改任务应覆盖旧报告任务并写回 memo。"""

    def test_sync_remediation_tasks_supersedes_old_report_tasks(self, db):
        dept = _make_dept(db, "整改同步部门")
        user = _make_user(db, "remediation_sync_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="整改同步Skill", status=SkillStatus.PUBLISHED)

        payload = skill_memo_service._empty_payload()
        payload["tasks"] = [{
            "id": "task_old_fix",
            "title": "旧整改任务",
            "type": "fix_after_test",
            "status": "todo",
            "priority": "high",
            "source": "test_failure",
            "description": "旧描述",
            "target_files": ["SKILL.md"],
            "acceptance_rule": {"mode": "custom", "text": "旧规则"},
            "depends_on": [],
            "started_at": None,
            "completed_at": None,
            "completed_by": None,
            "result_summary": None,
            "source_report_id": 99,
        }]
        payload["current_task_id"] = "task_old_fix"
        payload["test_history"] = [{
            "id": "test_1",
            "source": "sandbox_interactive",
            "version": 1,
            "status": "failed",
            "summary": "失败",
            "details": {},
            "created_at": "2026-04-15T00:00:00",
            "followup_task_ids": ["task_old_fix"],
            "source_report_id": 99,
        }]

        memo = SkillMemo(
            skill_id=skill.id,
            scenario_type="published_iteration",
            lifecycle_stage="fixing",
            status_summary="旧整改中",
            memo_payload=payload,
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(memo)
        db.commit()

        skill_memo_service.sync_remediation_tasks(
            db,
            skill_id=skill.id,
            tasks=[{
                "title": "修复输出结构",
                "priority": "p0",
                "action_type": "fix_prompt_logic",
                "target_kind": "skill_prompt",
                "target_ref": "SKILL.md",
                "problem_ids": ["issue_1"],
                "suggested_changes": "增加结论段",
                "acceptance_rule": "首段给结论",
                "retest_scope": ["all"],
            }],
            source_report_id=99,
            user_id=user.id,
        )

        refreshed = skill_memo_service.get_memo(db, skill.id)
        assert refreshed is not None
        tasks = refreshed["memo"]["tasks"]
        old_task = next(task for task in tasks if task["id"] == "task_old_fix")
        assert old_task["status"] == "superseded"
        new_task = next(task for task in tasks if task["id"] != "task_old_fix")
        assert new_task["title"] == "修复输出结构"
        assert new_task["target_files"] == ["SKILL.md"]
        assert refreshed["memo"]["current_task_id"] == new_task["id"]
        assert refreshed["memo"]["test_history"][0]["followup_task_ids"] == [new_task["id"]]

    def test_sync_remediation_tasks_creates_retest_dependency_chain(self, db):
        dept = _make_dept(db, "整改依赖部门")
        user = _make_user(db, "remediation_chain_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="整改依赖Skill", status=SkillStatus.PUBLISHED)

        skill_memo_service.sync_remediation_tasks(
            db,
            skill_id=skill.id,
            tasks=[
                {
                    "title": "补齐主 prompt 结构",
                    "priority": "p1",
                    "action_type": "fix_prompt_logic",
                    "target_kind": "skill_prompt",
                    "target_ref": "SKILL.md",
                    "problem_ids": ["issue_1"],
                    "suggested_changes": "补齐输出模板",
                    "acceptance_rule": "模板完整",
                    "retest_scope": ["all"],
                },
                {
                    "title": "运行定向回归",
                    "priority": "p1",
                    "action_type": "run_targeted_retest",
                    "target_kind": "unknown",
                    "target_ref": "",
                    "problem_ids": ["issue_1"],
                    "suggested_changes": "只回归失败 case",
                    "acceptance_rule": "问题消失",
                    "retest_scope": ["case_1"],
                },
            ],
            source_report_id=100,
            user_id=user.id,
        )

        refreshed = skill_memo_service.get_memo(db, skill.id)
        assert refreshed is not None
        tasks = refreshed["memo"]["tasks"]
        fix_task = next(task for task in tasks if task["type"] == "fix_prompt_logic")
        retest_task = next(task for task in tasks if task["type"] == "run_targeted_retest")
        assert retest_task["depends_on"] == [fix_task["id"]]

    def test_get_memo_stale_current_task_id(self, db):
        """current_task_id 指向已完成的任务时，自动选下一个。"""
        dept = _make_dept(db, "过期部门")
        user = _make_user(db, "stale_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="StaleSkill")

        payload = skill_memo_service._empty_payload()
        payload["tasks"] = [
            {"id": "t1", "title": "done task", "type": "edit_skill_md",
             "status": "done", "priority": "high", "source": "test",
             "description": "", "target_files": ["SKILL.md"],
             "acceptance_rule": {"mode": "manual"}, "depends_on": [],
             "started_at": None, "completed_at": None, "completed_by": None, "result_summary": None},
            {"id": "t2", "title": "next task", "type": "create_file",
             "status": "todo", "priority": "medium", "source": "test",
             "description": "", "target_files": ["example.md"],
             "acceptance_rule": {"mode": "all_target_files_saved_nonempty"}, "depends_on": [],
             "started_at": None, "completed_at": None, "completed_by": None, "result_summary": None},
        ]
        payload["current_task_id"] = "t1"  # 指向已完成的

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="editing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        result = skill_memo_service.get_memo(db, skill.id)
        # 应该自动切到 t2
        assert result["current_task"]["id"] == "t2"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Service 层 — 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════


class TestHelpers:

    def test_deps_done_no_deps(self):
        task = {"depends_on": []}
        assert skill_memo_service._deps_done(task, [])

    def test_deps_done_all_done(self):
        tasks = [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "skipped"},
        ]
        task = {"depends_on": ["a", "b"]}
        assert skill_memo_service._deps_done(task, tasks)

    def test_deps_done_some_pending(self):
        tasks = [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "todo"},
        ]
        task = {"depends_on": ["a", "b"]}
        assert not skill_memo_service._deps_done(task, tasks)

    def test_pick_next_task_respects_deps(self):
        payload = {
            "tasks": [
                {"id": "a", "status": "todo", "depends_on": ["b"]},
                {"id": "b", "status": "todo", "depends_on": []},
            ]
        }
        next_task = skill_memo_service._pick_next_task(payload)
        assert next_task["id"] == "b"  # b 没有依赖，应该先选

    def test_pick_next_task_all_done(self):
        payload = {
            "tasks": [
                {"id": "a", "status": "done", "depends_on": []},
            ]
        }
        assert skill_memo_service._pick_next_task(payload) is None

    def test_empty_payload_structure(self):
        p = skill_memo_service._empty_payload()
        assert "package_analysis" in p
        assert "persistent_notices" in p
        assert "tasks" in p
        assert "progress_log" in p
        assert "test_history" in p
        assert "adopted_feedback" in p
        assert "context_rollups" in p


# ═══════════════════════════════════════════════════════════════════════════════
# 10. API 路由层测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoAPI:
    """通过 HTTP 测试所有 /api/skills/{id}/memo 端点。"""

    def _setup(self, db, client):
        dept = _make_dept(db, "API测试部门")
        user = _make_user(db, "api_user", dept_id=dept.id)
        _make_model_config(db)
        skill = _make_skill(db, user.id, name="API测试Skill", status=SkillStatus.DRAFT)
        db.commit()
        token = _login(client, "api_user")
        return user, skill, token

    def test_get_memo_empty(self, db, client):
        user, skill, token = self._setup(db, client)
        resp = client.get(f"/api/skills/{skill.id}/memo", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_id"] == skill.id
        assert data.get("memo") is None

    def test_init_memo_api(self, db, client):
        user, skill, token = self._setup(db, client)
        resp = client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation", "goal_summary": "API测试目标"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"]
        assert data["memo"] is not None

    def test_get_memo_after_init(self, db, client):
        user, skill, token = self._setup(db, client)
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation"},
            headers=_auth(token),
        )
        resp = client.get(f"/api/skills/{skill.id}/memo", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["lifecycle_stage"] == "planning"

    def test_analyze_import_api(self, db, client):
        user, skill, token = self._setup(db, client)
        resp = client.post(
            f"/api/skills/{skill.id}/memo/analyze-import",
            json={"trigger": "import_zip"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"]

    def test_start_task_api(self, db, client):
        user, skill, token = self._setup(db, client)
        # Init first
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation", "goal_summary": "测试"},
            headers=_auth(token),
        )
        memo_resp = client.get(f"/api/skills/{skill.id}/memo", headers=_auth(token))
        tasks = memo_resp.json()["memo"]["tasks"]
        edit_task = [t for t in tasks if t["type"] == "edit_skill_md"][0]

        resp = client.post(
            f"/api/skills/{skill.id}/memo/tasks/{edit_task['id']}/start",
            json={"source": "studio_chat"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"]

    def test_complete_from_save_api(self, db, client):
        user, skill, token = self._setup(db, client)
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation", "goal_summary": "测试"},
            headers=_auth(token),
        )
        memo = client.get(f"/api/skills/{skill.id}/memo", headers=_auth(token)).json()
        edit_task = [t for t in memo["memo"]["tasks"] if t["type"] == "edit_skill_md"][0]
        client.post(
            f"/api/skills/{skill.id}/memo/tasks/{edit_task['id']}/start",
            json={},
            headers=_auth(token),
        )

        resp = client.post(
            f"/api/skills/{skill.id}/memo/tasks/{edit_task['id']}/complete-from-save",
            json={"filename": "SKILL.md", "file_type": "prompt", "content_size": 500},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"]
        assert data["task_completed"]

    def test_direct_test_api(self, db, client):
        user, skill, token = self._setup(db, client)
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation"},
            headers=_auth(token),
        )
        resp = client.post(
            f"/api/skills/{skill.id}/memo/direct-test",
            json={"source": "persistent_notice"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"]

    def test_test_result_api(self, db, client):
        user, skill, token = self._setup(db, client)
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation"},
            headers=_auth(token),
        )
        resp = client.post(
            f"/api/skills/{skill.id}/memo/test-result",
            json={"source": "manual", "version": 1, "status": "passed", "summary": "通过"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"]

    def test_adopt_feedback_api(self, db, client):
        user, skill, token = self._setup(db, client)
        client.post(
            f"/api/skills/{skill.id}/memo/init",
            json={"scenario_type": "new_skill_creation"},
            headers=_auth(token),
        )
        resp = client.post(
            f"/api/skills/{skill.id}/memo/adopt-feedback",
            json={
                "source_type": "comment",
                "source_id": 1,
                "summary": "增加错误处理",
                "task_blueprint": {"title": "增加错误处理逻辑"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"]

    def test_unauthorized_access(self, db, client):
        """无 token 应返回 401。"""
        resp = client.get("/api/skills/1/memo")
        assert resp.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. 压力测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestStress:
    """并发 & 大数据量测试。"""

    def test_many_tasks_in_payload(self, db):
        """memo 中有大量任务时 service 仍正常工作。"""
        dept = _make_dept(db, "压力部门")
        user = _make_user(db, "stress_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="StressSkill")

        payload = skill_memo_service._empty_payload()
        # 生成 200 个任务
        for i in range(200):
            payload["tasks"].append({
                "id": f"task_{i:04d}",
                "title": f"任务 #{i}",
                "type": "create_file",
                "status": "todo",
                "priority": "medium",
                "source": "stress_test",
                "description": f"压力测试任务 {i}",
                "target_files": [f"file_{i}.md"],
                "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
                "depends_on": [f"task_{i-1:04d}"] if i > 0 else [],
                "started_at": None,
                "completed_at": None,
                "completed_by": None,
                "result_summary": None,
            })

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="editing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        # get_memo 应该在合理时间内返回
        import time
        start = time.time()
        result = skill_memo_service.get_memo(db, skill.id)
        elapsed = time.time() - start
        assert result is not None
        assert elapsed < 2.0  # 应该很快

        # 只有第一个任务（无依赖）可做
        next_task = result["next_task"] or result["current_task"]
        if next_task:
            assert next_task["id"] == "task_0000"

    def test_many_progress_logs(self, db):
        """大量 progress_log 不影响性能。"""
        dept = _make_dept(db, "大日志部门")
        user = _make_user(db, "log_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="LogSkill")

        payload = skill_memo_service._empty_payload()
        for i in range(500):
            payload["progress_log"].append({
                "id": f"log_{i}",
                "task_id": f"task_{i}",
                "kind": "task_completed",
                "summary": f"完成了任务 {i}" * 10,  # 较长的文本
                "created_at": skill_memo_service._now_iso(),
            })

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="editing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        result = skill_memo_service.get_memo(db, skill.id)
        assert result is not None

    def test_rapid_complete_from_save(self, db):
        """快速连续调用 complete_from_save。"""
        dept = _make_dept(db, "快速部门")
        user = _make_user(db, "rapid_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="RapidSkill")

        payload = skill_memo_service._empty_payload()
        for i in range(10):
            payload["tasks"].append({
                "id": f"task_{i}",
                "title": f"文件任务 {i}",
                "type": "create_file",
                "status": "todo",
                "priority": "medium",
                "source": "test",
                "description": "",
                "target_files": [f"file_{i}.md"],
                "acceptance_rule": {"mode": "all_target_files_saved_nonempty"},
                "depends_on": [],
                "started_at": None,
                "completed_at": None,
                "completed_by": None,
                "result_summary": None,
            })
        payload["current_task_id"] = "task_0"

        sm = SkillMemo(
            skill_id=skill.id, scenario_type="new_skill_creation",
            lifecycle_stage="editing", status_summary="",
            memo_payload=payload, created_by=user.id, updated_by=user.id,
        )
        db.add(sm)
        db.commit()

        # 连续完成所有任务
        for i in range(10):
            skill_memo_service.start_task(db, skill.id, f"task_{i}", user.id)
            result = skill_memo_service.complete_from_save(
                db, skill.id, f"task_{i}", f"file_{i}.md", "asset", 100 + i
            )
            assert result["ok"]
            assert result["task_completed"]

        final = skill_memo_service.get_memo(db, skill.id)
        done_count = sum(1 for t in final["memo"]["tasks"] if t["status"] == "done")
        assert done_count == 10
        assert len(final["memo"]["progress_log"]) == 10

    def test_many_test_results(self, db):
        """记录大量测试结果。"""
        dept = _make_dept(db, "多测试部门")
        user = _make_user(db, "multi_test_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="MultiTestSkill")
        skill_memo_service.init_memo(db, skill.id, "new_skill_creation", "目标", user.id)

        for i in range(50):
            status = "passed" if i % 3 == 0 else "failed"
            skill_memo_service.record_test_result(
                db, skill.id, "sandbox", i + 1, status,
                f"测试 #{i+1} {'通过' if status == 'passed' else '失败'}",
                user_id=user.id,
            )

        memo = skill_memo_service.get_memo(db, skill.id)
        assert len(memo["memo"]["test_history"]) == 50
        assert memo["latest_test"]["version"] == 50

    def test_many_feedbacks(self, db):
        """连续采纳大量反馈。"""
        dept = _make_dept(db, "多反馈部门")
        user = _make_user(db, "multi_fb_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="MultiFeedbackSkill")
        skill_memo_service.init_memo(db, skill.id, "published_iteration", None, user.id)

        for i in range(30):
            skill_memo_service.adopt_feedback(
                db, skill.id, "comment", i + 1, f"反馈 #{i+1}", {}, user.id
            )

        memo = skill_memo_service.get_memo(db, skill.id)
        assert len(memo["memo"]["adopted_feedback"]) == 30
        # 应该有 30 个反馈任务
        feedback_tasks = [t for t in memo["memo"]["tasks"] if t["type"] == "adopt_feedback_change"]
        assert len(feedback_tasks) == 30


# ═══════════════════════════════════════════════════════════════════════════════
# 12. studio_agent 集成测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestStudioAgentIntegration:
    """验证 studio_agent.py 的 memo 上下文构建。"""

    def test_build_memo_context_none(self):
        from app.services.studio_agent import _build_memo_context
        result = _build_memo_context(None)
        assert "没有 Memo" in result

    def test_build_memo_context_empty(self):
        from app.services.studio_agent import _build_memo_context
        result = _build_memo_context({})
        assert "没有 Memo" in result

    def test_build_memo_context_with_data(self):
        from app.services.studio_agent import _build_memo_context
        memo_data = {
            "lifecycle_stage": "editing",
            "status_summary": "正在编辑中",
            "current_task": {"title": "编写主 SKILL.md", "target_files": ["SKILL.md"]},
            "next_task": {"title": "补充示例"},
            "persistent_notices": [{"title": "缺少 example"}],
            "latest_test": None,
            "memo": {"progress_log": []},
        }
        result = _build_memo_context(memo_data)
        assert "editing" in result
        assert "编写主 SKILL.md" in result
        assert "补充示例" in result
        assert "缺少 example" in result

    def test_build_system_with_memo(self):
        from app.services.studio_agent import _build_system
        system = _build_system(
            selected_skill_id=1,
            editor_prompt="你是助手。",
            editor_is_dirty=False,
            memo_context={
                "lifecycle_stage": "editing",
                "status_summary": "进行中",
                "current_task": {"title": "测试任务", "target_files": []},
                "next_task": None,
                "persistent_notices": [],
                "latest_test": None,
                "memo": {"progress_log": []},
            },
        )
        assert "editing" in system
        assert "Memo" in system

    def test_build_system_without_memo(self):
        from app.services.studio_agent import _build_system
        system = _build_system(
            selected_skill_id=1,
            editor_prompt="你是助手。",
            editor_is_dirty=False,
            memo_context=None,
        )
        assert "没有 Memo" in system

    def test_block_pattern_includes_memo_events(self):
        from app.services.studio_agent import _BLOCK_PATTERN
        test_text = """回复内容
```studio_memo_status
{"lifecycle_stage": "editing", "status_summary": "进行中", "has_open_todos": true, "can_test": false}
```
```studio_task_focus
{"task_id": "task_abc", "title": "编写主文件", "description": "编写", "target_files": ["SKILL.md"], "acceptance_hint": "保存后完成"}
```
```studio_editor_target
{"mode": "open_or_create", "file_type": "asset", "filename": "example-1.md"}
```
"""
        from app.services.studio_agent import _extract_events
        clean, events = _extract_events(test_text)
        event_names = [e[0] for e in events]
        assert "studio_memo_status" in event_names
        assert "studio_task_focus" in event_names
        assert "studio_editor_target" in event_names
        assert len(events) == 3
        # clean text should not contain the blocks
        assert "studio_memo_status" not in clean


class TestSandboxPreflightGuard:

    def test_preflight_blocks_when_no_change_after_last_test(self, client, db):
        dept = _make_dept(db, "preflight_guard_dept")
        user = _make_user(db, "preflight_guard_user", dept_id=dept.id)
        skill = _make_skill(db, user.id, name="PreflightGuardSkill", status=SkillStatus.DRAFT)
        db.commit()

        skill_memo_service.record_test_result(
            db,
            skill.id,
            "preflight",
            1,
            "failed",
            "上次质量检测未通过",
            user_id=user.id,
        )

        token = _login(client, user.username)
        resp = client.get(f"/api/sandbox/preflight/{skill.id}", headers=_auth(token))

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "未检测到新的修改 diff" in detail
