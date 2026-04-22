from app.services.studio_agent import _build_card_directive


def test_m2_main_prompt_and_example_use_different_directives_for_same_request():
    main_prompt_directive = _build_card_directive(
        active_card_mode="file",
        active_card_title="主 Prompt 调整",
        active_card_target="SKILL.md",
        active_card_id="card-main",
        active_card_validation_source=None,
        active_card_file_role="main_prompt",
        active_card_handoff_policy="open_file_workspace",
        active_card_queue_window=None,
        active_card_contract_id="confirm.staged_edit_review",
        active_card_context_summary="用户说：帮我改一下这个文件",
    )
    example_directive = _build_card_directive(
        active_card_mode="file",
        active_card_title="Example 调整",
        active_card_target="example-basic.md",
        active_card_id="card-example",
        active_card_validation_source=None,
        active_card_file_role="example",
        active_card_handoff_policy="open_file_workspace",
        active_card_queue_window=None,
        active_card_contract_id="confirm.staged_edit_review",
        active_card_context_summary="用户说：帮我改一下这个文件",
    )

    assert main_prompt_directive != example_directive
    assert "文件角色：main_prompt" in main_prompt_directive
    assert "`studio_draft`：生成完整主 Prompt 草稿时使用" in main_prompt_directive
    assert "`studio_diff`：对当前主 Prompt 做最小 staged edit 时使用" in main_prompt_directive

    assert "文件角色：example" in example_directive
    assert "不要继续追问“这个 Skill 要解决什么根因”" in example_directive
    assert "禁止输出重写主 Prompt 的 `studio_draft`" in example_directive
    assert "studio_card_handoff" in example_directive


def test_m2_tool_directive_forbids_fake_implementation_and_requires_handoff():
    directive = _build_card_directive(
        active_card_mode="file",
        active_card_title="实现天气工具",
        active_card_target="tool-weather.md",
        active_card_id="card-tool",
        active_card_validation_source=None,
        active_card_file_role="tool",
        active_card_handoff_policy="open_opencode",
        active_card_queue_window=None,
        active_card_contract_id="confirm.staged_edit_review",
        active_card_context_summary="用户说：帮我实现这个工具",
    )

    assert "文件角色：tool" in directive
    assert "严禁输出 `studio_diff`" in directive
    assert "严禁输出 `studio_draft`" in directive
    assert "`studio_external_edit_request`" in directive
    assert "confirm.bind_back" in directive
    assert "交接策略：open_opencode" in directive


def test_m2_reference_and_tool_stay_separate_in_allowed_outputs():
    reference_directive = _build_card_directive(
        active_card_mode="file",
        active_card_title="整理资料",
        active_card_target="reference-api.md",
        active_card_id="card-reference",
        active_card_validation_source=None,
        active_card_file_role="reference",
        active_card_handoff_policy="open_file_workspace",
        active_card_queue_window=None,
        active_card_contract_id="confirm.staged_edit_review",
        active_card_context_summary="用户说：帮我改一下这个文件",
    )
    tool_directive = _build_card_directive(
        active_card_mode="file",
        active_card_title="实现资料抓取工具",
        active_card_target="tool-fetcher.md",
        active_card_id="card-tool",
        active_card_validation_source=None,
        active_card_file_role="tool",
        active_card_handoff_policy="open_development_studio",
        active_card_queue_window=None,
        active_card_contract_id="confirm.staged_edit_review",
        active_card_context_summary="用户说：帮我改一下这个文件",
    )

    assert "`studio_diff`：只允许整理当前 Reference 文件的结构" in reference_directive
    assert "禁止输出直接重写主 Prompt 的 `studio_draft`" in reference_directive
    assert "严禁输出 `studio_diff`" in tool_directive
    assert "`studio_external_edit_request`" in tool_directive
