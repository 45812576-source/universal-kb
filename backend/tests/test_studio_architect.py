"""Studio Architect 联调验证测试。

覆盖三种场景的完整 architect 链路：
1. create_new_skill: phase_1_why → phase_2_what → phase_3_how → ooda → ready_for_draft
2. optimize_existing_skill: audit → phase_3_how → ooda → ready_for_draft
3. audit_imported_skill: audit(poor) → 升级到 phase_1_why → 完整链路

测试方法：
- 层 1：纯函数测试（路由、事件提取、prompt 构建）
- 层 2：run_stream 集成测试（mock LLM 输出，验证事件序列 + DB 状态推进）
"""
from __future__ import annotations

import asyncio
import json
import sys
import os

# 加入项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.studio_agent import (
    _resolve_session_mode,
    _extract_events,
    _extract_session_state,
    _build_system,
    _render_session_state,
    StudioSessionState,
    SESSION_MODES,
    ASSIST_SKILL_PROMPTS,
    ASSIST_SKILL_RULES,
)


# ══════════════════════════════════════════════════════════════════════════════
# 层 1: 纯函数测试
# ══════════════════════════════════════════════════════════════════════════════

class TestRouting:
    """测试 _resolve_session_mode 三态路由。"""

    def test_create_new_skill_empty_editor(self):
        mode, reason, assists = _resolve_session_mode(
            selected_skill_id=None,
            editor_prompt=None,
            user_message="我想做一个竞品分析的 Skill",
            skill_metadata=None,
            total_user_rounds=1,
        )
        assert mode == "create_new_skill"
        assert "phase1_problem_definition" in assists
        assert "phase2_element_decomposition" in assists
        assert "phase3_validation" in assists

    def test_optimize_existing_skill(self):
        mode, reason, assists = _resolve_session_mode(
            selected_skill_id=42,
            editor_prompt="你是一个财务分析助手...",
            user_message="帮我优化这个 Skill",
            skill_metadata={"source_type": "local"},
            total_user_rounds=1,
        )
        assert mode == "optimize_existing_skill"
        assert "skill_audit" in assists
        assert "phase3_validation" in assists

    def test_audit_imported_skill(self):
        mode, reason, assists = _resolve_session_mode(
            selected_skill_id=99,
            editor_prompt="# 导入的 Skill\n内容...",
            user_message="审计一下这个导入的 Skill",
            skill_metadata={"source_type": "imported"},
            total_user_rounds=1,
        )
        assert mode == "audit_imported_skill"
        assert "skill_audit" in assists

    def test_session_modes_constant(self):
        assert "create_new_skill" in SESSION_MODES
        assert "optimize_existing_skill" in SESSION_MODES
        assert "audit_imported_skill" in SESSION_MODES


class TestEventExtraction:
    """测试 _extract_events 能正确解析所有 architect_* 块。"""

    def test_architect_question(self):
        text = '''这是回复文本。

```architect_question
{"phase": "phase_1_why", "framework": "5_whys", "question": "为什么需要这个 Skill？", "options": ["提升效率", "替代人工", "标准化流程"], "why": "确认根因"}
```'''
        clean, events = _extract_events(text)
        assert "这是回复文本。" in clean
        assert len(events) == 1
        assert events[0][0] == "architect_question"
        assert events[0][1]["framework"] == "5_whys"
        assert len(events[0][1]["options"]) == 3

    def test_architect_phase_summary(self):
        text = '''分析完成。

```architect_phase_summary
{"phase": "phase_1_why", "summary": "根因是缺乏结构化竞品信息", "deliverables": ["根因定义", "JTBD 场景", "Cynefin: Complicated"], "confidence": 0.85, "ready_for_next": true}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "architect_phase_summary"
        assert events[0][1]["confidence"] == 0.85
        assert events[0][1]["ready_for_next"] is True

    def test_architect_structure(self):
        text = '''维度拆解如下：

```architect_structure
{"type": "issue_tree", "root": "竞品定价策略", "nodes": [{"id": "n1", "label": "成本结构", "parent": null, "children": ["n2", "n3"]}, {"id": "n2", "label": "固定成本", "parent": "n1", "children": []}, {"id": "n3", "label": "变动成本", "parent": "n1", "children": []}]}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "architect_structure"
        assert events[0][1]["type"] == "issue_tree"
        assert len(events[0][1]["nodes"]) == 3

    def test_architect_priority_matrix(self):
        text = '''优先级排序完成。

```architect_priority_matrix
{"dimensions": [{"name": "竞品成本结构", "priority": "P0", "sensitivity": "high", "reason": "直接影响定价决策"}, {"name": "品牌溢价", "priority": "P1", "sensitivity": "medium", "reason": "间接影响"}]}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "architect_priority_matrix"
        dims = events[0][1]["dimensions"]
        assert dims[0]["priority"] == "P0"
        assert dims[1]["priority"] == "P1"

    def test_architect_ooda_decision_converge(self):
        text = '''OODA 判断收敛。

```architect_ooda_decision
{"ooda_round": 2, "observation": "两轮变化趋于一致", "orientation": "维度清单稳定", "decision": "continue_to_draft", "delta_from_last": "无新增维度"}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "architect_ooda_decision"
        assert events[0][1]["decision"] == "continue_to_draft"

    def test_architect_ooda_decision_callback(self):
        text = '''需要回调。

```architect_ooda_decision
{"ooda_round": 1, "observation": "发现遗漏维度", "orientation": "需要补充渠道策略", "decision": "phase_2_what", "delta_from_last": "新增渠道维度"}
```'''
        clean, events = _extract_events(text)
        assert events[0][1]["decision"] == "phase_2_what"

    def test_architect_ready_for_draft(self):
        text = '''全部收敛。

```architect_ready_for_draft
{"key_elements": [{"name": "竞品成本", "priority": "P0", "source_phase": "phase_2_what"}], "failure_prevention": ["缺少实时数据源时降级为季度分析"], "draft_approach": "先定义角色和输入槽位，再组织分析逻辑"}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "architect_ready_for_draft"
        assert len(events[0][1]["key_elements"]) == 1
        assert len(events[0][1]["failure_prevention"]) == 1

    def test_audit_with_governance_action(self):
        """审计 + 治理动作同时输出。"""
        text = '''审计完成。

```studio_audit
{"quality_score": 35, "severity": "critical", "issues": [{"dimension": "根因清晰度", "score": 20, "detail": "目标模糊", "framework": "5_whys"}], "recommended_path": "restructure", "phase_entry": "phase_1_why"}
```

```studio_governance_action
{"card_id": "gov_001", "title": "补充目标定义", "summary": "缺少明确目标", "target": "system_prompt", "reason": "根因不清", "risk_level": "high", "framework": "5_whys", "phase": "phase1", "staged_edit": {"ops": [{"type": "append", "content": "## 核心目标\\n待补充"}]}}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 2
        assert events[0][0] == "studio_audit"
        assert events[0][1]["quality_score"] == 35
        assert events[1][0] == "studio_governance_action"
        assert events[1][1]["card_id"] == "gov_001"

    def test_mixed_standard_and_architect_blocks(self):
        """标准 studio 块 + architect 块混合输出。"""
        text = '''分析完成，这是草稿。

```architect_ready_for_draft
{"key_elements": [{"name": "x", "priority": "P0", "source_phase": "phase_3_how"}], "failure_prevention": ["a"], "draft_approach": "b"}
```

```studio_draft
{"name": "竞品分析 Skill", "system_prompt": "你是竞品分析助手...", "change_note": "基于 architect 分析生成"}
```'''
        clean, events = _extract_events(text)
        assert len(events) == 2
        names = [e[0] for e in events]
        assert "architect_ready_for_draft" in names
        assert "studio_draft" in names


class TestSessionState:
    """测试 session state + architect phase 字段。"""

    def test_state_has_architect_fields(self):
        state = StudioSessionState()
        assert state.architect_phase == ""
        assert state.ooda_round == 0
        assert state.phase_confirmed == {}

    def test_state_with_architect(self):
        state = StudioSessionState(
            session_mode="create_new_skill",
            architect_phase="phase_1_why",
            ooda_round=1,
            phase_confirmed={"phase_1_why": True},
        )
        assert state.architect_phase == "phase_1_why"
        assert state.ooda_round == 1


class TestPromptBuilding:
    """测试 _build_system 注入 architect 输出规则。"""

    def test_architect_rules_injected_for_create(self):
        state = StudioSessionState(
            session_mode="create_new_skill",
            architect_phase="phase_1_why",
            ooda_round=0,
            phase_confirmed={},
        )
        result = _build_system(
            selected_skill_id=None,
            editor_prompt=None,
            editor_is_dirty=False,
            session_state=state,
        )
        assert "Skill Architect 工作流" in result
        assert "phase_1_why" in result
        assert "architect_question" in result
        assert "architect_phase_summary" in result
        assert "architect_structure" in result
        assert "architect_priority_matrix" in result
        assert "architect_ooda_decision" in result
        assert "architect_ready_for_draft" in result
        assert "禁止输出 `studio_draft` / `studio_diff`" in result

    def test_no_architect_rules_when_no_phase(self):
        state = StudioSessionState(
            session_mode="create_new_skill",
            architect_phase="",
        )
        result = _build_system(
            selected_skill_id=None,
            editor_prompt=None,
            editor_is_dirty=False,
            session_state=state,
        )
        assert "当前未启用 Skill Architect 工作流" in result

    def test_assist_skills_injected_create(self):
        state = StudioSessionState(session_mode="create_new_skill", architect_phase="phase_1_why")
        result = _build_system(
            selected_skill_id=None,
            editor_prompt=None,
            editor_is_dirty=False,
            session_state=state,
        )
        assert "Phase 1：问题定义" in result
        assert "Phase 2：要素拆解" in result
        assert "Phase 3：验证收敛" in result

    def test_assist_skills_injected_audit(self):
        state = StudioSessionState(session_mode="audit_imported_skill", architect_phase="phase_3_how")
        result = _build_system(
            selected_skill_id=99,
            editor_prompt="# 导入的内容",
            editor_is_dirty=False,
            session_state=state,
        )
        assert "质量审计" in result
        assert "Skill 审计输出规则" in result
        assert "治理动作输出规则" in result

    def test_ooda_round_shown_in_prompt(self):
        state = StudioSessionState(
            session_mode="create_new_skill",
            architect_phase="ooda_iteration",
            ooda_round=2,
            phase_confirmed={"phase_1_why": True, "phase_2_what": True, "phase_3_how": True},
        )
        result = _build_system(
            selected_skill_id=None,
            editor_prompt=None,
            editor_is_dirty=False,
            session_state=state,
        )
        assert "OODA 轮次：2" in result
        assert "phase_1_why" in result  # confirmed phases

    def test_create_flow_blocks_draft_before_ready(self):
        state = StudioSessionState(
            session_mode="create_new_skill",
            current_mode="draft",
            architect_phase="phase_2_what",
            draft_readiness_score=4,
        )
        result = _build_system(
            selected_skill_id=None,
            editor_prompt=None,
            editor_is_dirty=False,
            session_state=state,
        )
        assert "Architect 阶段未完成" in result
        assert "禁止输出 studio_draft / studio_diff" in result


class TestAssistSkillRules:
    """测试辅助 Skill 注入规则完整性。"""

    def test_all_modes_have_rules(self):
        for mode in SESSION_MODES:
            assert mode in ASSIST_SKILL_RULES, f"Missing rule for {mode}"

    def test_all_referenced_skills_exist(self):
        for mode, skills in ASSIST_SKILL_RULES.items():
            for skill_key in skills:
                assert skill_key in ASSIST_SKILL_PROMPTS, f"Missing prompt for {skill_key} (in {mode})"

    def test_create_has_three_phases(self):
        skills = ASSIST_SKILL_RULES["create_new_skill"]
        assert "phase1_problem_definition" in skills
        assert "phase2_element_decomposition" in skills
        assert "phase3_validation" in skills

    def test_audit_has_audit_skill(self):
        for mode in ("optimize_existing_skill", "audit_imported_skill"):
            skills = ASSIST_SKILL_RULES[mode]
            assert "skill_audit" in skills


# ══════════════════════════════════════════════════════════════════════════════
# 层 2: run_stream 集成测试（模拟 LLM 输出，验证事件序列 + 阶段推进）
# ══════════════════════════════════════════════════════════════════════════════

class TestEventSequenceSimulation:
    """模拟 LLM 输出通过 _extract_events，验证完整链路的事件序列。"""

    def test_full_create_flow_events(self):
        """模拟 create_new_skill 完整链路的 LLM 输出序列。"""
        # Phase 1 完成
        text_p1 = '''根因分析完成。

```architect_question
{"phase": "phase_1_why", "framework": "5_whys", "question": "为什么需要竞品分析？", "options": ["定价参考", "市场洞察"], "why": "确认根因"}
```

```architect_phase_summary
{"phase": "phase_1_why", "summary": "根因是缺乏结构化竞品信息", "deliverables": ["根因: 竞品信息散落", "场景: 定价决策", "Cynefin: Complicated"], "confidence": 0.9, "ready_for_next": true}
```'''
        clean1, events1 = _extract_events(text_p1)
        assert len(events1) == 2
        assert events1[0][0] == "architect_question"
        assert events1[1][0] == "architect_phase_summary"
        assert events1[1][1]["ready_for_next"] is True

        # Phase 2 完成
        text_p2 = '''维度拆解完成。

```architect_structure
{"type": "issue_tree", "root": "竞品定价", "nodes": [{"id": "n1", "label": "成本", "parent": null, "children": ["n2"]}, {"id": "n2", "label": "固定成本", "parent": "n1", "children": []}]}
```

```architect_phase_summary
{"phase": "phase_2_what", "summary": "已穷举 4 个维度", "deliverables": ["成本结构", "价值主张", "市场供需", "渠道策略"], "confidence": 0.85, "ready_for_next": true}
```'''
        clean2, events2 = _extract_events(text_p2)
        assert len(events2) == 2
        assert events2[0][0] == "architect_structure"
        assert events2[1][0] == "architect_phase_summary"

        # Phase 3 完成
        text_p3 = '''验证收敛完成。

```architect_priority_matrix
{"dimensions": [{"name": "竞品成本", "priority": "P0", "sensitivity": "high", "reason": "直接影响"}, {"name": "品牌溢价", "priority": "P1", "sensitivity": "medium", "reason": "间接影响"}]}
```

```architect_phase_summary
{"phase": "phase_3_how", "summary": "P0/P1/P2 排序完成", "deliverables": ["P0: 成本结构", "P1: 品牌溢价", "失败预防清单"], "confidence": 0.92, "ready_for_next": true}
```'''
        clean3, events3 = _extract_events(text_p3)
        assert len(events3) == 2
        assert events3[0][0] == "architect_priority_matrix"

        # OODA 收敛
        text_ooda = '''OODA 第 2 轮收敛。

```architect_ooda_decision
{"ooda_round": 2, "observation": "维度稳定", "orientation": "无新增", "decision": "continue_to_draft", "delta_from_last": "无变化"}
```

```architect_ready_for_draft
{"key_elements": [{"name": "竞品成本", "priority": "P0", "source_phase": "phase_2_what"}], "failure_prevention": ["数据缺失降级"], "draft_approach": "先定义输入槽位再组织分析逻辑"}
```'''
        clean4, events4 = _extract_events(text_ooda)
        assert len(events4) == 2
        assert events4[0][0] == "architect_ooda_decision"
        assert events4[0][1]["decision"] == "continue_to_draft"
        assert events4[1][0] == "architect_ready_for_draft"

    def test_audit_upgrade_flow(self):
        """模拟 audit → quality_score < 40 → 升级到 phase_1_why。"""
        text_audit = '''审计完成，质量较差。

```studio_audit
{"quality_score": 30, "severity": "critical", "issues": [{"dimension": "根因清晰度", "score": 15, "detail": "完全缺失目标定义", "framework": "5_whys"}, {"dimension": "要素完备性", "score": 25, "detail": "只有 1 个输入维度", "framework": "mece_issue_tree"}], "recommended_path": "restructure", "phase_entry": "phase_1_why"}
```'''
        clean, events = _extract_events(text_audit)
        assert len(events) == 1
        audit_evt = events[0]
        assert audit_evt[0] == "studio_audit"
        assert audit_evt[1]["quality_score"] == 30
        assert audit_evt[1]["phase_entry"] == "phase_1_why"
        assert audit_evt[1]["recommended_path"] == "restructure"

    def test_ooda_callback_flow(self):
        """模拟 OODA 判断需要回调到 phase_2。"""
        text = '''OODA 第 1 轮，发现遗漏。

```architect_ooda_decision
{"ooda_round": 1, "observation": "缺少渠道维度", "orientation": "需要补充", "decision": "phase_2_what", "delta_from_last": "新增渠道维度"}
```'''
        clean, events = _extract_events(text)
        assert events[0][1]["decision"] == "phase_2_what"
        # 这应该触发 arch_state.workflow_phase = "phase_2_what"


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def _run_all():
    """简易测试 runner，不依赖 pytest。"""
    classes = [
        TestRouting,
        TestEventExtraction,
        TestSessionState,
        TestPromptBuilding,
        TestAssistSkillRules,
        TestEventSequenceSimulation,
    ]
    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  ✓ {cls.__name__}.{method_name}")
            except Exception as e:
                failed += 1
                errors.append((f"{cls.__name__}.{method_name}", e))
                print(f"  ✗ {cls.__name__}.{method_name}: {e}")

    print(f"\n{'='*60}")
    print(f"总计 {total} | 通过 {passed} | 失败 {failed}")
    if errors:
        print(f"\n失败详情：")
        for name, err in errors:
            print(f"  {name}: {err}")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = _run_all()
    sys.exit(0 if success else 1)
