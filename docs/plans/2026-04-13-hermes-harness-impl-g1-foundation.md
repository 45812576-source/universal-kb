# G1 实施计划：基础契约与状态层

日期：2026-04-13  
组别：`G1`  
主题：Foundation / Contracts / Session / State  
上游文档：`docs/plans/2026-04-13-hermes-harness-refactor-design.md`  
总览文档：`docs/plans/2026-04-13-hermes-harness-implementation-overview.md`

## 0. 验收结论与定位

如果把 `G1` 当作真正可开工的 implement 文档，则旧版本不合格。

不合格原因主要有四类：

- 缺少明确完成定义，导致“做多少算完成”不清楚。
- 缺少任务粒度和阶段切分，无法直接排期与分工。
- 缺少迁移决策与兼容策略，容易让后续组各自实现 adapter。
- 缺少对外接口冻结清单，无法作为 `G2/G3/G4/G5` 的稳定依赖。

本次修订的目标，是把 `G1` 从“方向性说明”提升为“可以交给一组研发直接开工”的实施文档。

## 1. 目标

如果 Hermes Harness 要真正落地，则首先必须建立统一契约层与状态层。

本组负责定义系统级公共基座：

- `HarnessRequest`
- `HarnessResponse`
- `HarnessEvent`
- `HarnessSessionKey`
- `HarnessSessionRecord`
- `HarnessRunRecord`
- `HarnessStepRecord`
- `HarnessArtifactRecord`
- `HarnessApprovalRecord`
- `HarnessMemoryRef`

如果本组不能先冻结这些公共接口，则后续各组会在不同模块里重复定义自己的运行模型，导致重构再次发散。

## 2. 本组产出在整体工程中的角色

如果 `G1` 完成，则它应向其他组提供 3 类稳定依赖：

1. **统一类型契约**：请求、响应、事件、状态枚举、session key。
2. **统一持久化接口**：session/run/step/artifact/approval/memory 的读写接口。
3. **兼容适配约束**：旧入口和旧状态表如何映射到新模型。

如果 `G1` 没有给出这 3 类依赖，则：

- `G2` 无法稳定接入 `AgentRuntime`
- `G3` 无法稳定写 Skill Studio / Sandbox 的状态
- `G4` 无法稳定定义 Dev Studio 的 registration 粒度
- `G5` 无法稳定做 run replay 和项目聚合

## 3. 范围

### In Scope

- 新增 `backend/app/harness/contracts.py`
- 新增 `backend/app/harness/events.py`
- 新增 `backend/app/harness/gateway.py`
- 新增 `backend/app/harness/session_store.py`
- 必要时新增 `backend/app/models/harness.py`
- 冻结 `agent_type` / `workspace_type` / `run_status` / `step_type`
- 设计并落地基础状态持久化方案
- 提供旧表到新状态层的 adapter
- 输出给其他组的接入说明

### Out of Scope

- 不负责具体 Chat Prompt 迁移
- 不负责 `ToolLoop` 实现
- 不负责 Studio architect workflow
- 不负责 OpenCode backend 进程管理
- 不负责项目协同业务逻辑

## 4. 关键问题

- 如果 `workspace_type` 继续以字符串散落在各模块中，则类型会继续漂移。
- 如果 `StudioRegistration`、`Conversation`、`SandboxTestSession`、`ProjectContext` 各自保持独立状态真相，则无法建立统一 run replay。
- 如果没有统一 `HarnessEvent` 协议，则 SSE 和日志系统无法共享同一语义。
- 如果没有统一 `SessionKey` 粒度，则 Dev Studio / Skill Studio / Project 的隔离边界无法达成一致。
- 如果没有 adapter 边界，则每个组都会自己写状态映射，最终失控。

## 5. 设计决策

### 5.1 是否直接新增 Harness 状态表

本组采用“两段式决策”：

- **阶段一**：先建立统一接口与 adapter，允许继续复用现有表。
- **阶段二**：当 `G2/G3/G4` 跑通后，再决定是否建立独立 Harness 状态表。

原因：

- 如果现在立刻大规模加表，会与 `G2/G3/G4/G5` 并行开发产生强耦合。
- 如果完全不定义新状态模型，后续又会退化成旧表拼装。

因此，本组必须先冻结“逻辑模型”，但“物理落库方式”允许分阶段。

### 5.2 逻辑模型优先于物理模型

本组先冻结如下逻辑对象：

- `HarnessSessionRecord`
- `HarnessRunRecord`
- `HarnessStepRecord`
- `HarnessArtifactRecord`
- `HarnessApprovalRecord`
- `HarnessMemoryRef`

如果当前用旧表承载，则 `SessionStore` 负责映射，不允许业务层直接依赖底层旧表细节。

### 5.3 入口先适配，不先替换

本组只要求在现有入口处新增 `HarnessRequest` 构造逻辑，不要求第一阶段就替换所有旧执行路径。

## 6. 必须冻结的接口

### 6.1 枚举

必须冻结以下枚举：

- `AgentType`
  - `chat`
  - `skill_studio`
  - `sandbox`
  - `dev_studio`
  - `project`

- `WorkspaceType`
  - `chat`
  - `opencode`
  - `sandbox`
  - `skill_studio`
  - `project`

- `RunStatus`
  - `created`
  - `running`
  - `waiting_approval`
  - `completed`
  - `failed`
  - `cancelled`

- `StepType`
  - `request_received`
  - `context_assembled`
  - `model_call`
  - `tool_call`
  - `approval_requested`
  - `approval_resolved`
  - `artifact_written`
  - `fallback_applied`
  - `output_emitted`

- `SecurityDecisionStatus`
  - `allow`
  - `deny`
  - `needs_approval`

### 6.2 请求对象

`HarnessRequest` 至少必须包含：

- `request_id`
- `agent_type`
- `user_id`
- `conversation_id`
- `workspace_id`
- `workspace_type`
- `project_id`
- `target_type`
- `target_id`
- `input_text`
- `input_files`
- `stream`
- `metadata`

### 6.3 Session Key

`HarnessSessionKey` 至少必须包含：

- `user_id`
- `agent_type`
- `workspace_id`
- `project_id`
- `target_type`
- `target_id`
- `conversation_id`

规则：

- 如果 `agent_type=skill_studio`，则 `target_type=skill` 且 `target_id` 必填。
- 如果 `agent_type=dev_studio`，则 `workspace_id` 或 `project_id` 必须至少存在一个。
- 如果 `agent_type=project`，则 `project_id` 必填。

### 6.4 事件对象

`HarnessEvent` 至少必须包含：

- `event_id`
- `run_id`
- `session_id`
- `event_type`
- `timestamp`
- `payload`

并约束 SSE、日志、回放三者共享同一事件语义。

## 7. 状态映射策略

### 7.1 旧表到逻辑对象的映射

| 逻辑对象 | 第一阶段承载来源 |
| --- | --- |
| `HarnessSessionRecord` | `Conversation` / `StudioRegistration` / `SandboxTestSession` 组合映射 |
| `HarnessRunRecord` | 新建轻量记录，或先用 `Message.metadata_` + 独立 run 表 |
| `HarnessStepRecord` | 新建轻量 step 表，避免塞回 message metadata |
| `HarnessArtifactRecord` | 文件记录 / 报告记录 / 下载元信息映射 |
| `HarnessApprovalRecord` | 复用审批相关表 |
| `HarnessMemoryRef` | `ProjectContext` / Studio 状态 / 知识引用映射 |

### 7.2 强制约束

- 业务代码不得直接拼装 run/step 的落库逻辑，必须经过 `SessionStore`
- 其他组不得直接定义新的 session key 规则
- 其他组不得定义与 `HarnessEvent` 平行的第二套事件协议

## 8. 交付物

### 核心代码交付

1. `contracts.py`
2. `events.py`
3. `session_store.py`
4. `gateway.py`
5. 必要的状态模型或 adapter

### 文档交付

1. 接口冻结清单
2. 状态映射说明
3. adapter 使用示例
4. 事件协议说明
5. 给 `G2/G3/G4/G5` 的接入约束说明

### 测试交付

1. 类型与枚举测试
2. session key 测试
3. 事件协议测试
4. adapter 测试
5. 入口构造 `HarnessRequest` 的测试

## 9. 实施步骤

### Step 1：冻结枚举与关键标识

输出：

- `AgentType`
- `WorkspaceType`
- `RunStatus`
- `StepType`
- `SecurityDecisionStatus`

完成标准：

- 所有枚举有唯一定义来源
- 所有调用方统一 import
- `workspace_type` 注释与代码语义一致

### Step 2：定义请求/响应/事件契约

输出：

- `HarnessRequest`
- `HarnessResponse`
- `HarnessContext`
- `HarnessEvent`

完成标准：

- 支持序列化/反序列化
- 字段命名冻结
- 有最小示例

### Step 3：定义 session key 与逻辑状态对象

输出：

- `HarnessSessionKey`
- `HarnessSessionRecord`
- `HarnessRunRecord`
- `HarnessStepRecord`
- `HarnessArtifactRecord`
- `HarnessApprovalRecord`
- `HarnessMemoryRef`

完成标准：

- key 规则写成代码而非仅文档描述
- 对 `skill_studio` / `dev_studio` / `project` 的特例明确

### Step 4：实现 `SessionStore`

输出：

- 统一创建 session
- 统一创建 run
- 统一写 step
- 统一写 artifact
- 统一写 approval
- 统一读 replay 基础数据

完成标准：

- 其他组只需调用 `SessionStore`，不需要关心底层旧表

### Step 5：设计并实现 adapter

目标：

- 映射 `Conversation`
- 映射 `StudioRegistration`
- 映射 `SandboxTestSession`
- 映射 `ProjectContext`

完成标准：

- 不破坏现有业务表
- 能从旧上下文构造逻辑模型

### Step 6：落入口适配器

目标：

- `conversations.py` 中可构造 `HarnessRequest`
- `sandbox_interactive.py` 中可构造 `HarnessRequest`
- `dev_studio.py` 中可构造 `HarnessRequest`
- `projects.py` 中可构造 `HarnessRequest`

完成标准：

- 入口至少具备“标准化请求”能力
- 可被后续 Runtime 接管

### Step 7：输出对外接入说明

目标：

- 为 `G2/G3/G4/G5` 提供一页式接入规则

内容至少包括：

- 如何拿 `HarnessSessionKey`
- 如何创建 `HarnessRun`
- 如何写 `HarnessStep`
- 哪些字段不能自定义

## 10. 文件 ownership

本组主 ownership：

- `backend/app/harness/contracts.py`
- `backend/app/harness/events.py`
- `backend/app/harness/gateway.py`
- `backend/app/harness/session_store.py`
- `backend/app/models/harness.py`（如新增）

协作 ownership：

- `backend/app/models/workspace.py`
- `backend/app/models/opencode.py`
- `backend/app/routers/conversations.py`
- `backend/app/routers/sandbox_interactive.py`
- `backend/app/routers/dev_studio.py`
- `backend/app/routers/projects.py`

约束：

- 对入口 router 的改动仅限“标准化请求构造”和轻量 adapter 接线
- 不在本组中重构具体 Agent 业务逻辑

## 11. 推荐代码结构

```text
backend/app/harness/
  contracts.py
  events.py
  gateway.py
  session_store.py
```

如果需要模型层，则新增：

```text
backend/app/models/harness.py
```

## 12. 测试计划

### 单元测试

- 枚举稳定性测试
- `HarnessRequest` 序列化测试
- `HarnessSessionKey` 生成测试
- `HarnessEvent` 结构测试
- `SessionStore` adapter 测试

### 集成测试

- `conversations.py` 构造 `HarnessRequest`
- `sandbox_interactive.py` 构造 `HarnessRequest`
- `dev_studio.py` 构造 `HarnessRequest`
- `projects.py` 构造 `HarnessRequest`

### 回归测试

- 原有接口不因引入 adapter 而改变行为
- 原有 `workspace_type` 相关逻辑不被破坏

## 13. 风险与缓解

- 如果本组直接推动全量数据库重构，则会阻塞其他组；缓解方式是先冻结逻辑模型，物理落库分阶段推进。
- 如果本组只写文档不写 adapter，则后续组会继续各自接入口；缓解方式是本组必须至少完成“入口标准化请求构造”。
- 如果本组不冻结事件协议，则 `G2/G3/G5` 会形成多套 SSE 语义；缓解方式是把 `HarnessEvent` 作为唯一标准事件模型。
- 如果本组不冻结 `SessionKey`，则 `G4` 无法修复 Dev Studio 隔离；缓解方式是 `SessionKey` 规则必须以代码形式提交。

## 14. 完成定义

如果要判定 `G1` 完成，则必须同时满足以下条件：

1. 所有核心枚举已冻结并落代码。
2. `HarnessRequest` / `HarnessEvent` / `HarnessSessionKey` 已落代码。
3. `SessionStore` 已能完成基础 session/run/step 写入。
4. 4 个主要入口已能构造 `HarnessRequest`。
5. 已输出给其他组的接入说明。
6. 相关测试已通过。

如果只完成类型定义、没有 `SessionStore` 与入口 adapter，则 `G1` 不算完成。

## 15. 对外接口冻结物

本组结束时必须给其他组一份冻结清单，至少包括：

- `HarnessRequest` 字段定义
- `HarnessEvent` 字段定义
- `HarnessSessionKey` 规则
- `SessionStore` 方法签名
- 枚举来源文件
- 兼容适配边界说明

## 16. 依赖

- 无前置实现依赖
- 为 `G2/G3/G4/G5` 提供接口基座

## 17. 交接条件

如果 `G1` 完成，则必须输出：

- 类型接口说明
- 事件字段说明
- 状态映射说明
- 给其他组的接入示例
- 一份冻结清单

如果这些文档或代码缺失，则不允许宣告 `G1` 完成。
