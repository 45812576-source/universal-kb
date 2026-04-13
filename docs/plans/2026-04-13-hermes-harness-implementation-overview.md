# Hermes Harness 实施计划总览

日期：2026-04-13  
项目：`/Users/xia/project/universal-kb`  
状态：实施计划草案  
上游设计：`docs/plans/2026-04-13-hermes-harness-refactor-design.md`

## 1. 目标

如果设计稿已经确认采用 Hermes 标准重构，则实施阶段的目标不是一次性重写全部 Agent，而是把改造拆成若干可以并行推进、但边界清晰的工作组。

本实施计划将重构拆成 5 个工作组：

1. `G1` 基础契约与状态层
2. `G2` Chat Runtime 与统一安全管线
3. `G3` Skill Studio 与 Sandbox 收敛
4. `G4` Dev Studio 后端化与隔离修复
5. `G5` Project Orchestrator 与观测审计

每组都有自己的 implement 文档、边界、交付物、依赖和验收标准。

## 2. 并行原则

如果两个组会修改同一核心运行链路，则不应在第一波完全并行。

如果某组主要依赖统一契约、事件模型、session key 或状态表，则它必须建立在 `G1` 的接口冻结版本之上。

如果某组只是消费 `HarnessGateway`、`AgentRuntime`、`SecurityPipeline` 的接口，而不定义这些接口，则它可以在 `G1` 产出接口草案后并行推进。

## 3. 推荐波次

### Wave 0：基线冻结

- 负责人：架构 owner + QA
- 目标：冻结枚举、事件协议、回归基线
- 建议时长：2~3 天

### Wave 1：第一批并行

- `G1` 基础契约与状态层
- `G2` Chat Runtime 与统一安全管线
- `G4` Dev Studio 后端化预拆分

说明：

- `G2` 可在 `G1` 给出契约草案后开始实现 adapter。
- `G4` 可先做“从 router 中抽离 manager/backends”的代码整理，不阻塞 `G1` 完整落地。

### Wave 2：第二批并行

- `G3` Skill Studio 与 Sandbox 收敛
- `G5` Project Orchestrator 与观测审计

说明：

- `G3` 依赖 `G1/G2` 的 runtime、事件、状态模型。
- `G5` 依赖 `G1` 的 session/memory/run 结构，也依赖 `G2/G4` 提供的统一子 Agent 执行能力。

### Wave 3：总集成

- 各组共同参与
- 目标：清理旧路径、做端到端回归、补文档和运行手册

## 4. 分组依赖图

```text
G1 ──┬──> G2 ──┬──> G3
     │         └──> G5
     └──> G4 ─────> G5

G2 ───────────────> G3
```

## 5. 组边界

### G1：基础契约与状态层

- 负责定义 `HarnessRequest`、`HarnessResponse`、`HarnessEvent`、`HarnessSessionKey`
- 负责统一 session/run/step/artifact/approval/memory 引用结构
- 负责 `SessionStore`、`HarnessGateway`、事件协议和类型枚举
- 不负责具体 Agent prompt 逻辑

### G2：Chat Runtime 与统一安全管线

- 负责把 Chat 路径迁到 `AgentRuntime`
- 负责统一 `ToolLoop`
- 负责 `SecurityPipeline`
- 负责 workspace skill 边界修复
- 不负责 Skill Studio architect workflow

### G3：Skill Studio 与 Sandbox 收敛

- 负责 Skill Studio 同步/流式统一
- 负责 `StudioSessionState` 持久化接入
- 负责 Sandbox 改为生产 Runtime 验证模式
- 不负责 OpenCode 进程管理

### G4：Dev Studio 后端化与隔离修复

- 负责 `OpenCodeBackend`、`RuntimeProcessManager`、`WorkdirManager`
- 负责 session/project 隔离修复
- 负责清理 router 中的运行时逻辑
- 不负责 Project orchestration

### G5：Project Orchestrator 与观测审计

- 负责 `ProjectOrchestratorProfile`
- 负责 shared project memory / handoff / report
- 负责统一观测、run replay、audit 查询
- 不负责底层 ToolLoop 或 OpenCode backend 的基础实现

## 6. 每组文档

- `docs/plans/2026-04-13-hermes-harness-impl-g1-foundation.md`
- `docs/plans/2026-04-13-hermes-harness-impl-g2-chat-runtime-security.md`
- `docs/plans/2026-04-13-hermes-harness-impl-g3-studio-sandbox.md`
- `docs/plans/2026-04-13-hermes-harness-impl-g4-dev-studio-backend.md`
- `docs/plans/2026-04-13-hermes-harness-impl-g5-project-observability.md`

## 7. 最小里程碑

### M1：接口冻结

如果 `G1` 完成，则其他组可以基于固定契约开展实现，不再反复改入口模型。

### M2：Chat 跑通统一 Runtime

如果 `G2` 完成，则系统已拥有第一个真正走统一 Harness 的生产 Agent。

### M3：Skill Studio / Sandbox 收敛

如果 `G3` 完成，则系统不再保留 Studio 双轨，也不再让 Sandbox 成为旁路测试链。

### M4：Dev Studio 多项目隔离

如果 `G4` 完成，则 OpenCode runtime 的最大结构性风险被消除。

### M5：Project 编排与统一观测

如果 `G5` 完成，则 Hermes 标准中的会话层、能力层、执行层、状态层基本闭环。

## 8. 风险控制

- 如果多个组同时直接修改 `conversations.py`，则合并冲突会很高；应由 `G2` 拥有 Chat 主入口，`G3` 只拥有 Skill Studio 相关分支。
- 如果多个组同时改同一批模型表，则应优先由 `G1` 定义持久化策略，其他组通过 adapter 接入。
- 如果 `G4` 继续保留内存态真相源，则后续 `G5` 的项目编排会建立在不稳定基座上。
- 如果 `G3` 在 `G2` 之前启动大规模 runtime 改造，则容易重复造 Agent Loop。

## 9. 建议执行方式

如果团队有足够人手，则建议每组指定 1 位 owner，并增加 1 位 reviewer 交叉审查。

如果团队人手有限，则建议实际执行顺序为：

1. `G1`
2. `G2`
3. `G4`
4. `G3`
5. `G5`

## 10. 结论

如果要把 Hermes 重构从“架构设计”推进到“研发执行”，则最优方式是采用 5 组并行、分波推进的实施模式。

如果要控制风险，则必须让 `G1` 先冻结契约，让 `G2` 先做首个统一 Runtime，再让 `G3/G4/G5` 接到统一底座上扩展。
