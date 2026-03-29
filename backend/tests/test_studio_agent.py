"""studio_agent 单元测试 — 覆盖 _build_system / _extract_events / studio_file_split / _read_source_files。"""
import json
import os
import pytest

from app.services.studio_agent import _build_system, _extract_events
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
