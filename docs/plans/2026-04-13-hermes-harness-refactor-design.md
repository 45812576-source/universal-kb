# Hermes Harness 重构设计草案

日期：2026-04-13  
项目：`/Users/xia/project/universal-kb`  
状态：设计草案  
参照标准：Hermes Agent 整体架构图

## 1. 背景与目标

如果把 Hermes 图作为 le desk-universal kb 的正式 Harness 工程标准，则当前系统需要从“多套 Agent 编排逻辑并列拼接”收敛为“统一 Harness Core + 多个 Agent Profile/Backend”。

当前系统已经具备 Chat、Skill Studio、沙盒测试、Dev Studio、项目协作等能力，但这些能力分别沉淀在 `skill_engine`、`studio_agent`、`sandbox_interactive`、`dev_studio`、`project_engine` 等模块中。它们共享部分业务表与 LLM gateway，却没有共享统一的会话、运行时、安全、状态与观测内核。

本设计目标是：

- 如果入口来自 Chat、Skill Studio、Sandbox、Dev Studio 或 Project，则请求都应进入统一 `HarnessGateway`。
- 如果一次请求会触发模型调用、工具调用、文件写入、数据查询或审批，则这些动作都应经过统一 `SecurityPipeline`。
- 如果一次运行需要追踪、恢复、审计或复盘，则运行过程必须产生统一 `HarnessRun` 与 `HarnessStep`。
- 如果不同 Agent 只是策略不同，则差异应表达为 `AgentProfile`，而不是复制一套运行时。
- 如果 Dev Studio 或 Project 需要隔离上下文，则隔离键应至少包含 `user_id + agent_type + workspace_id + project_id + target_id`。

## 2. 当前缺陷摘要

### 2.1 Chat Harness

如果工作台边界必须严格隔离，则当前 Chat 存在候选 Skill 边界击穿风险：在已有 `workspace_skills` 时仍会合并全局 published Skill。

如果工具调用前必须经过统一 Guard / Approval / Sandbox 判定，则当前 Chat Agent Loop 不合格：模型选择工具后会进入并行执行，缺少独立 Command Guard 层。

如果 Memory 层要求显式用户记忆、会话摘要和长期记忆抽象，则当前 Chat 主要依赖最近消息窗口与压缩，尚未形成独立 memory abstraction。

### 2.2 Skill Studio Harness

如果同一 Agent 只能有一条权威运行路径，则 Skill Studio 存在双轨缺陷：非流式路径可能走 `system_context` 直聊，流式路径可能走 `studio_agent` 结构化编排。

如果 `StudioSessionState` 应成为单一事实来源，则必须让同步和流式入口都收敛到同一 `SkillStudioAgentProfile`。

### 2.3 Sandbox Harness

如果沙盒测试应证明生产行为，则 Sandbox 不能继续作为旁路测试向导独立执行，而应调用生产同款 `AgentRuntime`，并以 `sandbox_mode=true` 增加证据、审批和输出约束。

### 2.4 Dev Studio Harness

如果系统需要多 worker 或横向扩展，则 `dev_studio` 里的模块级 `_user_instances` 进程池不适合作为运行时真相源。

如果同一用户可能同时参与多个项目，则 `StudioRegistration` 仅以 `(user_id, workspace_type)` 唯一的粒度不足以表达多项目、多工作台、多会话隔离。

如果项目上下文必须保真，则将 opencode session 归并到 `global project` 的修复策略需要替换为 project-aware/session-aware 的索引策略。

### 2.5 Project Harness

如果 Project Agent 应建立在统一 Harness 上，则 `project_engine` 不应继续作为独立规划器与同步器，而应成为 `ProjectOrchestratorProfile`，通过统一 runtime 调度 Chat/Dev 子会话、共享 Project Memory、生成 handoff 和 report。

## 3. 推荐重构策略

推荐采用渐进式 Strangler Fig 重构。

如果目标是最小风险地收敛到 Hermes 标准，则应先新增统一 Harness 契约、状态和运行时，再逐步迁移各 Agent；这样可以保留现有 API 与业务功能，并通过 adapter 逐步替换旧路径。

不建议一次性重写所有 Agent。原因是当前系统的业务功能与权限逻辑散落较多，大爆炸式重构会同时影响 Chat、Skill Studio、Sandbox、Dev Studio、Project，回归面过大。

也不建议只做入口和事件协议统一。原因是这只能改善表层一致性，无法解决工具调用守卫、状态分散、Skill Studio 双轨、Dev Studio 多项目隔离等根因问题。

## 4. 目标架构

### 4.1 Entry Layer

现有 API 保留，但入口应变为薄适配器：

- `conversations.py` 只负责 Chat/Skill Studio 的 HTTP 与 SSE 适配。
- `sandbox_interactive.py` 只负责证据采集、报告与审批向导。
- `dev_studio.py` 只负责 Dev Studio 的 HTTP API，不再持有进程池真相。
- `projects.py` 只负责项目业务 API，不再直接承载 Agent 编排。

如果入口需要执行 Agent，则应转换为 `HarnessRequest` 并调用 `HarnessGateway.dispatch()`。

### 4.2 Session & Routing Layer

新增统一组件：

- `HarnessGateway`：统一入口调度。
- `SessionRunner`：一次运行的生命周期管理。
- `SessionStore`：统一读写 session、run、step、artifact、approval。
- `AgentRouter`：根据 `agent_type`、`workspace_type`、`project_id`、`target_id` 选择 Agent Profile。

建议 session key：

```text
user_id
agent_type
workspace_id?
project_id?
target_type?
target_id?
conversation_id?
```

如果 `agent_type=dev_studio`，则必须包含 `workspace_id` 或 `project_id`，避免同一用户多个开发上下文污染。

如果 `agent_type=skill_studio`，则必须包含 `target_type=skill` 与 `target_id=skill_id`，避免不同 Skill Studio 会话互相污染。

### 4.3 Runtime Core

新增统一 `AgentRuntime`，包含：

- `ContextAssembler`：组装 conversation、workspace、project、skill、memory、knowledge。
- `PromptBuilder`：生成 system/user/tool prompt。
- `ContextCompressor`：统一上下文压缩策略。
- `ModelRunner`：调用 `llm_gateway`，但策略不下沉到 provider adapter。
- `ToolLoop`：统一 native function calling 与文本 fallback 工具循环。
- `FallbackPolicy`：统一模型失败、工具失败、超时、上下文溢出降级策略。
- `RunStateMachine`：管理 `created → running → waiting_approval → completed | failed | cancelled`。

如果一个 Agent 只是在 prompt、状态机或 capability 选择上不同，则它应实现 `AgentProfile`，而不是复制 Runtime。

### 4.4 Security Boundaries

新增统一 `SecurityPipeline`，顺序建议为：

```text
AuthGuard
→ ScopeGuard
→ ModelGrantGuard
→ InputScanGuard
→ ToolPermissionGuard
→ ApprovalGuard
→ SandboxGuard
→ OutputFilter
→ MemoryPolicy
```

如果一次工具调用涉及数据查询、文件写入、外部 MCP、OpenCode 后端或高敏感模型，则必须在 `pre_tool_call` 阶段产生明确 `SecurityDecision`。

如果 `SecurityDecision.status=needs_approval`，则 `AgentRuntime` 应进入 `waiting_approval`，而不是直接执行工具。

### 4.5 Capability Layer

统一封装以下能力：

- `SkillCapability`：Skill 路由、Prompt、版本、required inputs、输出 schema。
- `ToolCapability`：内部工具、MCP 工具、文件工具、数据工具。
- `KnowledgeCapability`：RAG、知识权限、脱敏、项目知识。
- `MemoryCapability`：用户记忆、会话摘要、项目 memory、Studio state。
- `BackendCapability`：OpenCode、文件、终端、Web、子 Agent。

如果 Chat 能调用工具，则 Skill Studio、Sandbox、Project 也应通过同一 capability registry 调用工具，而不是各自实现一套路径。

### 4.6 Execution Backends

将执行后端抽象为：

- `OpenCodeBackend`：从 `dev_studio.py` 中抽出进程管理、工作区布局、session DB 读取。
- `FileBackend`：文件上传、生成、artifact 管理。
- `DataBackend`：业务表查询、视图执行、权限脱敏。
- `MCPBackend`：MCP 安装、启动、调用与审批。
- `SubAgentBackend`：后续多 Agent 协作预留。

如果某后端需要进程或外部资源，则状态真相必须写入持久化 store，模块级内存只能作为缓存。

### 4.7 State & Persistence

新增或映射以下状态对象：

- `HarnessSession`：稳定会话身份。
- `HarnessRun`：一次用户请求触发的一次运行。
- `HarnessStep`：模型调用、工具调用、审批、压缩、fallback 等步骤。
- `HarnessArtifact`：生成文件、报告、代码 diff、沙盒证据。
- `HarnessApproval`：审批请求与审批结果。
- `HarnessMemoryRef`：运行引用的记忆、知识、项目上下文。

如果短期不建新表，则可先通过 adapter 映射到现有 `Conversation`、`Message.metadata_`、`SandboxTestSession`、`ProjectContext`、`StudioRegistration`。但中期应迁移到统一 Harness 状态表。

## 5. Agent Profile 设计

### 5.1 ChatAgentProfile

如果请求来自普通 Chat，则使用 `ChatAgentProfile`：

- 负责 Skill 路由策略。
- 负责个人工作台/部门工作台边界策略。
- 使用统一 `ToolLoop`。
- 使用统一 `KnowledgeCapability` 与 `MemoryCapability`。

第一阶段可以从 `skill_engine.prepare()` 拆出 `SkillRouter`、`PromptBuilder`、`KnowledgeInjector`。

### 5.2 SkillStudioAgentProfile

如果请求来自 Skill Studio，则使用 `SkillStudioAgentProfile`：

- 统一同步与流式路径。
- 将 `StudioSessionState` 持久化到 `HarnessSession.state`。
- 保留 architect workflow、audit、governance action、draft readiness 等能力。
- 移除 `system_context` 直聊快速路径。

### 5.3 SandboxAgentProfile

如果请求来自沙盒测试，则使用 `SandboxAgentProfile`：

- 强制 `sandbox_mode=true`。
- 强制 `evidence_required=true`。
- 禁止 mock 输入、禁止自动编造测试数据。
- 执行同款生产 `AgentRuntime`。
- 报告引用真实 `HarnessRun` 与 `HarnessStep`。

### 5.4 DevStudioAgentProfile

如果请求来自 Dev Studio，则使用 `DevStudioAgentProfile`：

- 通过 `OpenCodeBackend` 启停 runtime。
- session key 必须包含 `workspace_id` 或 `project_id`。
- 不再以模块级 `_user_instances` 作为状态真相源。
- 不再把所有 opencode session 归并到 `global project`。

### 5.5 ProjectOrchestratorProfile

如果请求来自项目协作，则使用 `ProjectOrchestratorProfile`：

- 规划成员 workspace。
- 管理项目 shared memory。
- 调度需求 Agent 与开发 Agent。
- 生成 handoff、daily summary、report。
- 将项目上下文作为 `HarnessMemoryRef` 写入统一状态层。

## 6. 分阶段改造路线

### Phase 0：冻结语义与验收线

目标：不改行为，先定义 Hermes 合规标准与回归边界。

工作项：

- 定义 `agent_type` 矩阵：`chat | skill_studio | sandbox | dev_studio | project`。
- 定义 `workspace_type` 枚举，并修正注释与运行时语义漂移。
- 定义统一 SSE 事件协议。
- 补充架构测试，锁定现有 Chat、Skill Studio、Sandbox、Dev Studio、Project 的关键行为。

验收：

- 如果新增或迁移 Harness 组件，则现有 API 行为不破坏。
- 如果 `workspace_type` 使用新增类型，则类型枚举与测试同步覆盖。

### Phase 1：新增 Harness 契约层

目标：建立统一类型和入口，不替换旧实现。

工作项：

- 新增 `backend/app/harness/contracts.py`。
- 新增 `HarnessRequest`、`HarnessResponse`、`HarnessEvent`、`HarnessSessionKey`。
- 新增 `SecurityDecision`、`CapabilityCall`、`RunStatus`。
- 在现有入口旁路生成 `HarnessRequest`，先只记录不执行。

验收：

- 如果 Chat/Skill Studio/Sandbox/Dev Studio/Project 入口被调用，则都能构造标准 `HarnessRequest`。
- 如果出现参数缺失，则能给出统一 validation error。

### Phase 2：统一 Chat Runtime

目标：让 Chat 先跑到统一 Runtime。

工作项：

- 将 `skill_engine._handle_tool_calls_stream` 抽为 `ToolLoop`。
- 将 `skill_engine.prepare` 拆分为 `SkillRouter`、`ContextAssembler`、`PromptBuilder`。
- 增加 `SecurityPipeline.pre_tool_call()`，工具执行前必须返回 allow/deny/needs_approval。
- 修复 workspace Skill 边界：默认不合并全局 Skill，除非配置显式 `allow_global_skill_fallback=true`。

验收：

- 如果 Chat 调用工具，则每个工具调用都有 `HarnessStep`。
- 如果工具需要审批，则 Runtime 停在 `waiting_approval`。
- 如果工作台绑定了 Skill，则不会默认调用未绑定全局 Skill。

### Phase 3：收敛 Skill Studio 双轨

目标：Skill Studio 同步与流式都走同一 Profile。

工作项：

- 将 `studio_agent.run_stream` 封装为 `SkillStudioAgentProfile`。
- 非流式 `system_context` 快速路径改为调用同一个 Profile。
- 将 `StudioSessionState` 写入统一 session state。
- 将 architect workflow 状态与 `HarnessRun` 关联。

验收：

- 如果同一 Skill Studio 会话走同步或流式，则状态推进一致。
- 如果会话恢复，则 architect phase、confirmed facts、draft readiness 不丢失。

### Phase 4：沙盒切换为 Harness 验证模式

目标：沙盒报告证明生产链路，而不是旁路逻辑。

工作项：

- `sandbox_interactive` 保留 evidence wizard。
- `run` 阶段改为调用 `AgentRuntime`。
- `SandboxTestReport` 引用 `HarnessRun`。
- 工具、数据、权限、输出的证据来自 `HarnessStep`。

验收：

- 如果沙盒测试通过，则报告能追溯到真实模型调用、工具输入、工具输出和过滤结果。
- 如果测试输入证据不足，则 Runtime 不进入执行阶段。

### Phase 5：Dev Studio 后端化

目标：将 OpenCode runtime 从 router 中剥离。

工作项：

- 新增 `OpenCodeBackend`。
- 新增 `RuntimeProcessManager` 与 `WorkdirManager`。
- 将 `_user_instances` 降级为进程句柄缓存。
- 将注册粒度升级为 `user_id + workspace_type + workspace_id/project_id`。
- 替换 `global project` 归并策略，保留 project-aware session mapping。

验收：

- 如果同一用户打开两个项目 Dev Studio，则 session、workdir、opencode DB 不互相污染。
- 如果服务多 worker 部署，则运行状态可从持久 store 恢复。

### Phase 6：Project 变成多 Agent 编排器

目标：让项目协作成为统一 Harness 上的 orchestration。

工作项：

- 将 `project_engine` 的 plan/apply/sync/report 改为 `ProjectOrchestratorProfile`。
- 将项目上下文改为 `HarnessMemoryRef`。
- 将需求 workspace 与开发 workspace 建模为项目子 session。
- handoff 生成 `HarnessArtifact`。

验收：

- 如果项目产生需求交接，则开发 Agent 能通过统一 memory/context 获取交接内容。
- 如果项目生成报告，则报告引用项目下相关 runs 与 artifacts。

### Phase 7：统一安全与观测

目标：形成可审计、可回放、可告警的生产 Harness。

工作项：

- 所有模型调用写 `HarnessStep(type=model_call)`。
- 所有工具调用写 `HarnessStep(type=tool_call)`。
- 所有审批写 `HarnessApproval`。
- 所有生成文件写 `HarnessArtifact`。
- 增加 run replay 与 audit log 查询 API。

验收：

- 如果用户投诉一次回答，则可按 run id 追溯上下文、模型、工具、审批、输出过滤。
- 如果工具失败或模型超时，则统一呈现错误类型与 fallback 决策。

### Phase 8：清理旧路径

目标：移除重复编排逻辑，降低维护成本。

工作项：

- 删除或降级 Skill Studio 快速路径。
- 删除 router 中的运行时进程管理逻辑。
- 将 `skill_engine` 保留为 capability/service，不再作为主 runtime。
- 将项目计划、沙盒运行、Dev Studio 后端统一纳入 Harness。

验收：

- 如果新增 Agent 类型，则只需要新增 `AgentProfile` 与必要 backend，不需要复制 runtime。
- 如果系统进入审计或回放场景，则所有 Agent 使用同一状态与事件模型。

## 7. 建议目录结构

```text
backend/app/harness/
  __init__.py
  contracts.py
  gateway.py
  runtime.py
  session_store.py
  security.py
  capabilities.py
  events.py
  profiles/
    chat.py
    skill_studio.py
    sandbox.py
    dev_studio.py
    project.py
  backends/
    opencode.py
    file.py
    data.py
    mcp.py
```

如果短期不新增数据库表，则 `session_store.py` 可先适配现有表；如果进入 Phase 7，则应建立独立 Harness 状态表。

## 8. 关键风险与缓解

- 如果过早迁移所有 Agent，则回归风险过大；缓解方式是先迁移 Chat，再迁移 Skill Studio。
- 如果只做入口统一，则工具守卫和状态分散仍存在；缓解方式是 Phase 2 必须引入 `ToolLoop + SecurityPipeline`。
- 如果 Dev Studio 继续使用 `(user_id, workspace_type)` 注册，则多项目隔离不可达；缓解方式是 Phase 5 升级 session key 与 registration 粒度。
- 如果 Sandbox 不调用生产 Runtime，则测试结论不可证明生产行为；缓解方式是 Phase 4 将 run 阶段改为 `AgentRuntime`。
- 如果 `workspace_type` 语义继续漂移，则权限、不可删除、路由等逻辑会继续散乱；缓解方式是 Phase 0 先冻结枚举。

## 9. 最小可行实施顺序

如果只能选择最小可行路径，则建议：

1. Phase 0：冻结 `agent_type`、`workspace_type`、事件协议。
2. Phase 1：新增 Harness 契约层。
3. Phase 2：迁移 Chat Runtime 与工具守卫。
4. Phase 3：收敛 Skill Studio 双轨。
5. Phase 5：修复 Dev Studio 多项目隔离。
6. Phase 4/6/7：再推进 Sandbox、Project、观测与审计。

## 10. 结论

如果目标是按 Hermes 标准完成工程级 Harness 收敛，则应采用渐进式 Strangler Fig 重构，并以 `HarnessGateway + AgentRuntime + SecurityPipeline + SessionStore + AgentProfile` 为核心架构。

如果目标是降低短期回归风险，则第一阶段不应改业务行为，而应先新增契约层、事件模型与适配器。

如果目标是尽快解决高风险缺陷，则优先级应是：统一工具调用守卫、消除 Skill Studio 双轨、修复 Dev Studio 多项目隔离、封闭 Chat workspace Skill 边界。
