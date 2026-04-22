from app.services.studio_agent import _build_system


def test_m2_q2_scenario1_main_prompt_can_continue_full_creation_flow():
    result = _build_system(
        7,
        "# Skill\n\n当前主 Prompt 草稿",
        False,
        active_card_id="card-main",
        active_card_title="主 Prompt 创作",
        active_card_mode="file",
        active_card_target="SKILL.md",
        active_card_file_role="main_prompt",
        active_card_handoff_policy="open_file_workspace",
        active_card_queue_window={
            "active_card_id": "card-main",
            "visible_card_ids": ["card-main", "card-what", "card-how"],
            "backlog_count": 0,
            "phase": "why",
            "max_visible": 5,
            "reveal_policy": "stage_gated",
        },
        active_card_context_summary="当前正在 Why → What → How 主线中推进主 Prompt。",
    )

    assert "主 Prompt 编排 Agent" in result
    assert "`studio_draft`：生成完整主 Prompt 草稿时使用" in result
    assert "`studio_diff`：对当前主 Prompt 做最小 staged edit 时使用" in result
    assert "明确下一步 Sandbox / Preflight / Governance 验收路径" in result
    assert "当前队列窗口" in result


def test_m2_q2_scenario2_example_does_not_chase_root_cause_and_requires_handoff():
    result = _build_system(
        7,
        "## Example\n\n输入：...\n输出：...",
        False,
        active_card_id="card-example",
        active_card_title="补充 example 文件",
        active_card_mode="file",
        active_card_target="example-basic.md",
        active_card_file_role="example",
        active_card_handoff_policy="open_file_workspace",
        active_card_context_summary="用户先要求新增示例，随后说主 Prompt 也要引用。",
    )

    assert "Example 创作与校准 Agent" in result
    assert "不要继续追问“这个 Skill 要解决什么根因”" in result
    assert "不允许在当前卡混写主 Prompt；如需主 Prompt 配套调整，创建 handoff" in result
    assert "用户说“主 Prompt 也要体现这个规则”时，输出 `studio_card_handoff`" in result


def test_m2_q2_scenario3_tool_handoff_avoids_fake_completion_and_keeps_bind_back():
    result = _build_system(
        7,
        "tool requirement spec",
        False,
        active_card_id="card-tool",
        active_card_title="实现天气工具",
        active_card_mode="file",
        active_card_target="tool-weather.md",
        active_card_file_role="tool",
        active_card_handoff_policy="open_development_studio",
        active_card_context_summary="用户要求创建工具，后续需要回绑并进入治理和 Sandbox。",
    )

    assert "Tool 需求交接 Agent" in result
    assert "严禁输出 `studio_diff`" in result
    assert "严禁输出 `studio_draft`" in result
    assert "不在 Studio Chat 中假装开发完成" in result
    assert "创建 `confirm.bind_back` 确认卡" in result
    assert "明确回流绑定与 Sandbox 验收条件" in result
