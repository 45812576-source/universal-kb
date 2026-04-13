"""Hermes Harness 入口适配器 — 将现有入口参数转换为 HarnessRequest。

Phase 1: 这些函数在现有 router 中以旁路方式调用，仅记录不执行。
Phase 2+: 替换现有入口逻辑，改为调用 HarnessGateway.dispatch()。
"""
from __future__ import annotations

from typing import Any, Optional

from app.harness.contracts import (
    AgentType,
    HarnessContext,
    HarnessRequest,
    HarnessSessionKey,
)


def build_chat_request(
    *,
    user_id: int,
    workspace_id: int,
    conversation_id: int,
    user_message: str,
    stream: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> HarnessRequest:
    """从 Chat 入口参数构造 HarnessRequest。

    对应 conversations.py 的 /conversations/{id}/messages/stream
    """
    session_key = HarnessSessionKey(
        user_id=user_id,
        agent_type=AgentType.CHAT,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    return HarnessRequest(
        session_key=session_key,
        agent_type=AgentType.CHAT,
        user_id=user_id,
        input_text=user_message,
        context=HarnessContext(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        ),
        stream=stream,
        metadata=metadata or {},
    )


def build_skill_studio_request(
    *,
    user_id: int,
    workspace_id: int,
    skill_id: int,
    conversation_id: Optional[int] = None,
    user_message: str,
    stream: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> HarnessRequest:
    """从 Skill Studio 入口参数构造 HarnessRequest。

    对应 conversations.py 的 studio 流式/同步路径。
    """
    session_key = HarnessSessionKey(
        user_id=user_id,
        agent_type=AgentType.SKILL_STUDIO,
        workspace_id=workspace_id,
        target_type="skill",
        target_id=skill_id,
    )
    return HarnessRequest(
        session_key=session_key,
        agent_type=AgentType.SKILL_STUDIO,
        user_id=user_id,
        input_text=user_message,
        context=HarnessContext(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            skill_id=skill_id,
            target_type="skill",
            target_id=skill_id,
        ),
        stream=stream,
        metadata=metadata or {},
    )


def build_sandbox_request(
    *,
    user_id: int,
    target_type: str,           # "skill" | "tool"
    target_id: int,
    session_id: int,            # SandboxTestSession.id
    user_message: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> HarnessRequest:
    """从 Sandbox 入口参数构造 HarnessRequest。

    对应 sandbox_interactive.py 的测试执行入口。
    """
    session_key = HarnessSessionKey(
        user_id=user_id,
        agent_type=AgentType.SANDBOX,
        target_type=target_type,
        target_id=target_id,
    )
    return HarnessRequest(
        session_key=session_key,
        agent_type=AgentType.SANDBOX,
        user_id=user_id,
        input_text=user_message,
        context=HarnessContext(
            target_type=target_type,
            target_id=target_id,
            extra={"sandbox_session_id": session_id},
        ),
        stream=False,
        sandbox_mode=True,
        metadata=metadata or {},
    )


def build_dev_studio_request(
    *,
    user_id: int,
    workspace_id: int,
    project_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    user_message: str,
    stream: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> HarnessRequest:
    """从 Dev Studio 入口参数构造 HarnessRequest。

    对应 dev_studio.py 的对话入口。
    """
    session_key = HarnessSessionKey(
        user_id=user_id,
        agent_type=AgentType.DEV_STUDIO,
        workspace_id=workspace_id,
        project_id=project_id,
    )
    return HarnessRequest(
        session_key=session_key,
        agent_type=AgentType.DEV_STUDIO,
        user_id=user_id,
        input_text=user_message,
        context=HarnessContext(
            workspace_id=workspace_id,
            project_id=project_id,
            conversation_id=conversation_id,
        ),
        stream=stream,
        metadata=metadata or {},
    )


def build_project_request(
    *,
    user_id: int,
    project_id: int,
    user_message: str,
    stream: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> HarnessRequest:
    """从 Project 入口参数构造 HarnessRequest。

    对应 projects.py 的项目协作入口。
    """
    session_key = HarnessSessionKey(
        user_id=user_id,
        agent_type=AgentType.PROJECT,
        project_id=project_id,
    )
    return HarnessRequest(
        session_key=session_key,
        agent_type=AgentType.PROJECT,
        user_id=user_id,
        input_text=user_message,
        context=HarnessContext(
            project_id=project_id,
        ),
        stream=stream,
        metadata=metadata or {},
    )
