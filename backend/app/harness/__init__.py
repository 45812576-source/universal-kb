"""Hermes Harness 基础契约与状态层。

本模块定义系统级公共基座：统一枚举、请求/响应契约、会话键、
运行/步骤/制品/审批/记忆引用模型、事件协议、SessionStore 与 HarnessGateway。

G2 新增：
- tool_loop.py: ToolLoop — 统一工具调用循环
- security.py: SecurityPipeline — 统一安全管线（6 Guards + OutputFilter）
- runtime.py: AgentRuntime — Chat 统一运行时

G3 新增：
- profiles/skill_studio.py: SkillStudioAgentProfile — Skill Studio 统一执行 Profile（消除同步/流式双轨）
- profiles/sandbox.py: SandboxAgentProfile — 沙盒测试 Harness 集成（run/step/artifact 追踪）
"""
