"""studio_agent 单元测试 — 覆盖 _build_system / _extract_events / studio_file_split / _read_source_files。"""
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.conversations import SendMessage, _active_card_meta, _studio_context_meta
from app.services.studio_agent import (
    _build_card_directive,
    _build_system,
    _extract_events,
    _normalize_external_handoff_payload,
    _orchestration_error,
    run_stream,
)
from app.services.skill_engine import _read_source_files


# ── _build_system ────────────────────────────────────────────────────────────


class TestBuildSystem:
    """测试系统提示词构建逻辑。"""

    def test_empty_editor_prompt(self):
        """编辑器为空时返回无上下文提示。"""
        result = _build_system(None, None, False)
        assert "用户尚未选中任何 Skill，编辑器为空" in result

    def test_empty_string_editor_prompt(self):
        result = _build_system(1, "", False)
        assert "用户尚未选中任何 Skill，编辑器为空" in result

    def test_whitespace_editor_prompt(self):
        result = _build_system(1, "   ", False)
        assert "用户尚未选中任何 Skill，编辑器为空" in result

    def test_with_editor_prompt_basic(self):
        """有编辑器内容时包含行数和 prompt 片段。"""
        prompt = "line1\nline2\nline3"
        result = _build_system(42, prompt, True)
        assert "Skill ID：42" in result
        assert "未保存修改：是" in result
        assert "共 3 行" in result
        assert "line1" in result

    def test_editor_prompt_truncated_to_2000(self):
        """超长 prompt 截断到 2000 字。"""
        long_prompt = "a" * 3000
        result = _build_system(1, long_prompt, False)
        # 不应包含完整 3000 字
        assert "a" * 2001 not in result

    def test_is_dirty_false(self):
        result = _build_system(1, "hello", False)
        assert "未保存修改：否" in result

    def test_no_source_files(self):
        """没有附属文件时显示提示。"""
        result = _build_system(1, "hello", False, source_files=None)
        assert "暂无附属文件" in result

    def test_empty_source_files(self):
        result = _build_system(1, "hello", False, source_files=[])
        assert "暂无附属文件" in result

    def test_with_source_files(self):
        """有附属文件时列出文件名和分类。"""
        files = [
            {"filename": "example-demo.md", "category": "example"},
            {"filename": "ref-api.md", "category": "reference"},
        ]
        result = _build_system(1, "hello", False, source_files=files)
        assert "example-demo.md(example)" in result
        assert "ref-api.md(reference)" in result
        assert "暂无附属文件" not in result

    def test_source_files_missing_category(self):
        """附属文件缺少 category 字段时回退为 '未分类'。"""
        files = [{"filename": "data.csv"}]
        result = _build_system(1, "hello", False, source_files=files)
        assert "data.csv(未分类)" in result

    def test_contains_studio_file_split_instruction(self):
        """系统提示词应包含 studio_file_split 的说明。"""
        result = _build_system(1, "hello", False)
        assert "studio_file_split" in result

    def test_selected_skill_id_none_shows_unselected(self):
        result = _build_system(None, "hello", False)
        assert "Skill ID：未选择" in result

    def test_main_prompt_file_role_uses_role_directive(self):
        result = _build_system(
            1,
            "## Role\nfoo",
            False,
            active_card_mode="file",
            active_card_title="主 Prompt 改写",
            active_card_target="SKILL.md",
            active_card_file_role="main_prompt",
        )
        assert "文件角色：main_prompt" in result
        assert "允许 `studio_draft` 和 `studio_diff`" in result
        assert "Why / What / How / Governance / Validation" in result

    def test_example_file_role_avoids_main_prompt_rewrite(self):
        result = _build_system(
            1,
            "## Example\nfoo",
            False,
            active_card_mode="file",
            active_card_title="补 example",
            active_card_target="example-demo.md",
            active_card_file_role="example",
        )
        assert "文件角色：example" in result
        assert "禁止输出重写主 Prompt 的 `studio_draft`" in result
        assert "不要继续追问“这个 Skill 要解决什么根因”" in result

    def test_tool_file_role_forbids_diff_and_draft(self):
        result = _build_system(
            1,
            "tool spec",
            False,
            active_card_mode="file",
            active_card_title="实现天气工具",
            active_card_target="tool-weather.json",
            active_card_file_role="tool",
            active_card_handoff_policy="open_opencode",
            active_card_context_summary="用户希望增加天气查询工具，要求声明权限。",
        )
        assert "文件角色：tool" in result
        assert "严禁输出 `studio_diff`" in result
        assert "严禁输出 `studio_draft`" in result
        assert "当前卡片上下文摘要：用户希望增加天气查询工具" in result
        assert "open_opencode" in result

    def test_unknown_file_role_falls_back_to_unknown_asset(self):
        result = _build_system(
            1,
            "misc",
            False,
            active_card_mode="file",
            active_card_title="未知文件",
            active_card_target="misc.md",
        )
        assert "文件角色：unknown_asset" in result
        assert "先分类，再说明可执行的最小下一步" in result

    def test_fallback_directive_keeps_contract_and_card_metadata(self):
        result = _build_card_directive(
            active_card_mode=None,
            active_card_title="补充主卡信息",
            active_card_target="SKILL.md",
            active_card_id="card_fallback",
            active_card_source_card_id="card_source",
            active_card_staged_edit_id="edit_1",
            active_card_phase="phase_1_why",
            active_card_validation_source={"kind": "sandbox"},
            active_card_file_role="main_prompt",
            active_card_handoff_policy="open_file_workspace",
            active_card_queue_window={"active_card_id": "card_fallback"},
            active_card_contract_id="confirm.staged_edit_review",
            active_card_context_summary="当前卡片虽然没带 mode，但 contract 不能丢。",
        )
        assert "卡片 Contract：confirm.staged_edit_review" in result
        assert "源卡片 ID：card_source" in result
        assert "关联 staged edit：edit_1" in result
        assert "卡片阶段：phase_1_why" in result
        assert "卡片标题：补充主卡信息" in result
        assert "卡片目标：SKILL.md" in result
        assert "文件角色：main_prompt" in result
        assert "交接策略：open_file_workspace" in result
        assert "当前队列窗口" in result
        assert "当前卡片上下文摘要：当前卡片虽然没带 mode，但 contract 不能丢。" in result
        assert "验证来源：" in result

    def test_fallback_directive_keeps_empty_dict_metadata(self):
        result = _build_card_directive(
            active_card_mode=None,
            active_card_title="空字典元数据",
            active_card_target="SKILL.md",
            active_card_id="card_empty_dict",
            active_card_source_card_id=None,
            active_card_staged_edit_id=None,
            active_card_phase=None,
            active_card_validation_source={},
            active_card_file_role="main_prompt",
            active_card_handoff_policy="open_file_workspace",
            active_card_queue_window={},
            active_card_contract_id="confirm.staged_edit_review",
            active_card_context_summary="",
        )
        assert "当前队列窗口：{}" in result
        assert "验证来源：{}" in result


class TestConversationMeta:
    def test_active_card_meta_keeps_all_present_fields_without_mode(self):
        req = SendMessage(
            content="继续",
            active_card_id="card_1",
            active_card_title="卡片标题",
            active_card_target="SKILL.md",
            active_card_source_card_id="card_source",
            active_card_staged_edit_id="edit_1",
            active_card_validation_source={"source": "sandbox"},
            active_card_file_role="main_prompt",
            active_card_handoff_policy="open_file_workspace",
            active_card_queue_window={"active_card_id": "card_1"},
            active_card_context_summary="上下文摘要",
            active_card_contract_id="confirm.staged_edit_review",
            active_card_phase="phase_1_why",
        )
        meta = _active_card_meta(req)
        assert meta["active_card_id"] == "card_1"
        assert meta["active_card_title"] == "卡片标题"
        assert meta["active_card_target"] == "SKILL.md"
        assert meta["active_card_source_card_id"] == "card_source"
        assert meta["active_card_staged_edit_id"] == "edit_1"
        assert meta["active_card_validation_source"] == {"source": "sandbox"}
        assert meta["active_card_file_role"] == "main_prompt"
        assert meta["active_card_handoff_policy"] == "open_file_workspace"
        assert meta["active_card_queue_window"] == {"active_card_id": "card_1"}
        assert meta["active_card_context_summary"] == "上下文摘要"
        assert meta["active_card_contract_id"] == "confirm.staged_edit_review"
        assert meta["active_card_phase"] == "phase_1_why"
        assert "active_card_mode" not in meta

    def test_active_card_meta_keeps_empty_dict_fields(self):
        req = SendMessage(
            content="继续",
            active_card_validation_source={},
            active_card_queue_window={},
        )
        meta = _active_card_meta(req)
        assert meta["active_card_validation_source"] == {}
        assert meta["active_card_queue_window"] == {}

    def test_studio_context_meta_keeps_selected_file_and_editor_state(self):
        req = SendMessage(
            content="继续",
            editor_prompt="",
            editor_is_dirty=False,
            selected_source_filename="example-basic.md",
            active_card_id="card_1",
        )
        meta = _studio_context_meta(req)
        assert meta["active_card_id"] == "card_1"
        assert meta["selected_source_filename"] == "example-basic.md"
        assert meta["editor_is_dirty"] is False
        assert meta["editor_target"] is True


# ── _extract_events ──────────────────────────────────────────────────────────


class TestExtractEvents:
    """测试从 LLM 输出中提取结构化事件。"""

    def test_no_blocks(self):
        """纯文本无结构化块。"""
        text = "这是一段普通回复，没有代码块。"
        clean, events = _extract_events(text)
        assert clean == text
        assert events == []

    def test_extract_studio_draft(self):
        payload = {"name": "test", "system_prompt": "hello", "change_note": "初版"}
        text = f'一些说明\n```studio_draft\n{json.dumps(payload, ensure_ascii=False)}\n```\n后续文字'
        clean, events = _extract_events(text)
        assert "studio_draft" not in clean
        assert "一些说明" in clean
        assert "后续文字" in clean
        assert len(events) == 1
        assert events[0][0] == "studio_draft"
        assert events[0][1]["name"] == "test"

    def test_extract_studio_diff(self):
        payload = {"ops": [{"type": "replace", "old": "a", "new": "b"}], "change_note": "改了"}
        text = f'```studio_diff\n{json.dumps(payload)}\n```'
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "studio_diff"
        assert events[0][1]["ops"][0]["type"] == "replace"

    def test_extract_studio_summary(self):
        payload = {"title": "摘要", "items": [{"label": "目标", "value": "v"}], "next_action": "generate_draft"}
        text = f'回复\n```studio_summary\n{json.dumps(payload, ensure_ascii=False)}\n```'
        clean, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "studio_summary"

    def test_extract_studio_tool_suggestion(self):
        payload = {"suggestions": [{"name": "calc", "reason": "需要计算", "action": "create_new", "tool_id": None}]}
        text = f'```studio_tool_suggestion\n{json.dumps(payload)}\n```'
        _, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "studio_tool_suggestion"

    def test_extract_studio_file_split(self):
        """核心：新增的 studio_file_split 事件能被正确提取。"""
        payload = {
            "files": [
                {"filename": "example-demo.md", "category": "example", "content": "# 示例\n...", "reason": "拆出示例"},
                {"filename": "ref-kb.md", "category": "knowledge-base", "content": "# 知识\n...", "reason": "拆出知识库"},
            ],
            "main_prompt_after_split": "# 精简后的主文件",
            "change_note": "将示例和知识库拆分",
        }
        text = f'AI 建议拆分：\n```studio_file_split\n{json.dumps(payload, ensure_ascii=False)}\n```\n完成。'
        clean, events = _extract_events(text)
        assert "studio_file_split" not in clean
        assert "AI 建议拆分" in clean
        assert "完成" in clean
        assert len(events) == 1
        evt_name, evt_data = events[0]
        assert evt_name == "studio_file_split"
        assert len(evt_data["files"]) == 2
        assert evt_data["files"][0]["filename"] == "example-demo.md"
        assert evt_data["files"][0]["category"] == "example"
        assert evt_data["files"][1]["category"] == "knowledge-base"
        assert evt_data["main_prompt_after_split"] == "# 精简后的主文件"
        assert evt_data["change_note"] == "将示例和知识库拆分"

    def test_extract_multiple_blocks(self):
        """同时出现 studio_draft + studio_file_split（合法场景）。"""
        draft = {"name": "skill", "system_prompt": "long prompt", "change_note": "创建"}
        split = {
            "files": [{"filename": "example-1.md", "category": "example", "content": "...", "reason": "示例"}],
            "main_prompt_after_split": "short prompt",
            "change_note": "拆分",
        }
        text = (
            f'说明\n```studio_draft\n{json.dumps(draft, ensure_ascii=False)}\n```\n'
            f'```studio_file_split\n{json.dumps(split, ensure_ascii=False)}\n```'
        )
        clean, events = _extract_events(text)
        assert len(events) == 2
        names = {e[0] for e in events}
        assert "studio_draft" in names
        assert "studio_file_split" in names
        assert "studio_draft" not in clean
        assert "studio_file_split" not in clean

    def test_extract_draft_and_tool_suggestion(self):
        """studio_draft + studio_tool_suggestion 同时出现。"""
        draft = {"name": "s", "system_prompt": "p", "change_note": "c"}
        tool = {"suggestions": [{"name": "t", "reason": "r", "action": "create_new", "tool_id": None}]}
        text = f'```studio_draft\n{json.dumps(draft)}\n```\n```studio_tool_suggestion\n{json.dumps(tool)}\n```'
        _, events = _extract_events(text)
        assert len(events) == 2

    def test_invalid_json_skipped(self):
        """JSON 解析失败时跳过该块，不报错。"""
        text = '```studio_draft\n{invalid json\n```\nok'
        clean, events = _extract_events(text)
        assert events == []
        assert "ok" in clean

    def test_case_insensitive(self):
        """块名大小写不敏感。"""
        payload = {"name": "x", "system_prompt": "y", "change_note": "z"}
        text = f'```STUDIO_DRAFT\n{json.dumps(payload)}\n```'
        _, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "studio_draft"

    def test_studio_file_split_case_insensitive(self):
        payload = {"files": [], "main_prompt_after_split": "", "change_note": ""}
        text = f'```STUDIO_FILE_SPLIT\n{json.dumps(payload)}\n```'
        _, events = _extract_events(text)
        assert len(events) == 1
        assert events[0][0] == "studio_file_split"

    def test_extract_new_file_role_orchestration_blocks(self):
        blocks = (
            '```studio_file_role_decision\n'
            + json.dumps({"file_role": "tool", "confidence": "high"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_card_handoff\n'
            + json.dumps({"target_role": "main_prompt", "summary": "同步规则"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_queue_update\n'
            + json.dumps({"intent": "switch_to_existing_card"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_external_edit_request\n'
            + json.dumps({"target": "open_opencode"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_bind_back_request\n'
            + json.dumps({"source": "external_edit_returned"}, ensure_ascii=False)
            + '\n```'
        )
        clean, events = _extract_events(blocks)
        assert clean == ""
        assert [name for name, _ in events] == [
            "studio_file_role_decision",
            "studio_card_handoff",
            "studio_queue_update",
            "studio_external_edit_request",
            "studio_bind_back_request",
        ]

    def test_extract_m5_orchestration_blocks(self):
        blocks = (
            '```studio_governance_complete\n'
            + json.dumps({"card_id": "gov-1", "result": "pass"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_refine_staged\n'
            + json.dumps({"origin_card_id": "refine-1"}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_audit_scan_complete\n'
            + json.dumps({"card_id": "audit-1", "issues_count": 0}, ensure_ascii=False)
            + '\n```\n'
            + '```studio_fixing_complete\n'
            + json.dumps({"card_id": "fix-1", "result": "ready_for_validation"}, ensure_ascii=False)
            + '\n```\n'
            + '```card_proposals\n'
            + json.dumps({"proposals": [{"title": "补示例"}]}, ensure_ascii=False)
            + '\n```'
        )
        clean, events = _extract_events(blocks)
        assert clean == ""
        assert [name for name, _ in events] == [
            "studio_governance_complete",
            "studio_refine_staged",
            "studio_audit_scan_complete",
            "studio_fixing_complete",
            "card_proposals",
        ]

    def test_external_edit_request_normalizes_to_handoff_contract(self):
        payload = _normalize_external_handoff_payload(
            "studio_external_edit_request",
            {
                "target": "opencode",
                "summary": "实现天气工具",
                "input_schema": {"city": "string"},
                "acceptance_criteria": ["能返回天气", "失败时有错误提示"],
            },
            active_card_target="tools/weather.py",
            active_card_file_role="tool",
            active_card_handoff_policy=None,
        )
        assert payload == {
            "target_role": "tool",
            "target_file": "tools/weather.py",
            "handoff_policy": "open_opencode",
            "summary": "实现天气工具",
            "handoff_summary": "实现天气工具",
            "acceptance_criteria": ["能返回天气", "失败时有错误提示"],
            "activate": True,
        }

    def test_orchestration_error_reports_no_auto_advance(self):
        error = _orchestration_error(
            step="handoff",
            message="外部交接创建失败",
            active_card_id="card-tool",
            payload={"target": "opencode"},
            retryable=True,
        )
        assert error["error_type"] == "studio_orchestration_error"
        assert error["auto_advanced"] is False
        assert error["retryable"] is True
        assert error["active_card_id"] == "card-tool"

    def test_clean_text_stripped(self):
        """提取后的文本应去除首尾空白。"""
        payload = {"name": "x", "system_prompt": "y", "change_note": "z"}
        text = f'  \n```studio_draft\n{json.dumps(payload)}\n```\n  '
        clean, _ = _extract_events(text)
        assert clean == clean.strip()


# ── _read_source_files ──────────────────────────────────────────────────────


class TestReadSourceFiles:
    """测试附属文件运行时注入。"""

    @pytest.fixture(autouse=True)
    def _setup_dir(self, tmp_path, monkeypatch):
        """创建临时目录结构 uploads/skills/1/."""
        self.skill_dir = tmp_path / "uploads" / "skills" / "1"
        self.skill_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

    def _write(self, filename: str, content: str):
        (self.skill_dir / filename).write_text(content, encoding="utf-8")

    def test_read_text_files(self):
        """正常读取 md 文件并按 category 分组。"""
        self._write("example-demo.md", "# 示例\n示例内容")
        files = [{"filename": "example-demo.md", "category": "example"}]
        result = _read_source_files(1, files)
        assert "## 示例：example-demo.md" in result
        assert "示例内容" in result

    def test_knowledge_base_category(self):
        self._write("kb-data.md", "知识库内容")
        files = [{"filename": "kb-data.md", "category": "knowledge-base"}]
        result = _read_source_files(1, files)
        assert "## 知识库：kb-data.md" in result

    def test_reference_category(self):
        self._write("reference-api.md", "API 参考")
        files = [{"filename": "reference-api.md", "category": "reference"}]
        result = _read_source_files(1, files)
        assert "## 参考资料：reference-api.md" in result

    def test_template_category(self):
        self._write("template-report.md", "模板内容")
        files = [{"filename": "template-report.md", "category": "template"}]
        result = _read_source_files(1, files)
        assert "## 输出模板：template-report.md" in result

    def test_skip_unknown_category(self):
        """未知 category（如 tool、other）应跳过。"""
        self._write("tool-calc.py", "print('hello')")
        files = [{"filename": "tool-calc.py", "category": "tool"}]
        result = _read_source_files(1, files)
        assert result == ""

    def test_skip_binary_extension(self):
        """非文本扩展名应跳过。"""
        self._write("image.png", "fake binary")
        files = [{"filename": "image.png", "category": "example"}]
        result = _read_source_files(1, files)
        assert result == ""

    def test_skip_missing_file(self):
        """文件不存在时跳过，不报错。"""
        files = [{"filename": "nonexistent.md", "category": "example"}]
        result = _read_source_files(1, files)
        assert result == ""

    def test_max_total_chars_limit(self):
        """超过 max_total_chars 时截断。"""
        self._write("big.md", "x" * 500)
        self._write("big2.md", "y" * 500)
        files = [
            {"filename": "big.md", "category": "example"},
            {"filename": "big2.md", "category": "example"},
        ]
        result = _read_source_files(1, files, max_total_chars=600)
        assert "x" * 500 in result
        assert "y" * 500 not in result

    def test_multiple_categories_ordered(self):
        """多个 category 按 knowledge-base → example → reference → template 排序。"""
        self._write("ref.md", "参考")
        self._write("ex.md", "示例")
        self._write("kb.md", "知识")
        files = [
            {"filename": "ref.md", "category": "reference"},
            {"filename": "ex.md", "category": "example"},
            {"filename": "kb.md", "category": "knowledge-base"},
        ]
        result = _read_source_files(1, files)
        kb_pos = result.index("知识库")
        ex_pos = result.index("示例")
        ref_pos = result.index("参考资料")
        assert kb_pos < ex_pos < ref_pos

    def test_empty_source_files(self):
        result = _read_source_files(1, [])
        assert result == ""

    def test_various_text_extensions(self):
        """多种文本扩展名都能读取。"""
        for ext in [".txt", ".json", ".yaml", ".csv", ".sql"]:
            fname = f"data{ext}"
            self._write(fname, f"content-{ext}")
            files = [{"filename": fname, "category": "reference"}]
            result = _read_source_files(1, files)
            assert f"content-{ext}" in result


@pytest.mark.asyncio
async def test_run_stream_falls_back_to_non_stream_when_stream_fails_before_first_token():
    db = MagicMock()
    db.get.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None

    async def broken_stream(*args, **kwargs):
        raise RuntimeError("stream transport closed")
        yield  # pragma: no cover

    with patch("app.services.studio_agent.llm_gateway.chat_stream_typed", new=broken_stream), \
         patch("app.services.studio_agent.llm_gateway.chat", new=AsyncMock(return_value=("fallback reply", {}))):
        events = [
            event async for event in run_stream(
                db=db,
                conv_id=1,
                workspace_system_context="",
                history_messages=[],
                user_message="请帮我修复这个问题",
                model_config={"provider": "ark", "model_id": "test-model"},
            )
        ]

    deltas = [event for event in events if isinstance(event, tuple) and event[0] == "delta"]
    assert deltas
    assert deltas[-1][1]["text"] == "fallback reply"
    assert not any(event[0] == "error" for event in events if isinstance(event, tuple))
