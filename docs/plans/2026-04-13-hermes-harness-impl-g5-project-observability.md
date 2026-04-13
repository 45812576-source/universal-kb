# G5 实施计划：Project Orchestrator 与观测审计

日期：2026-04-13  
组别：`G5`  
主题：Project Orchestration / Shared Memory / Observability

## 1. 目标

如果 Hermes 标准要在系统级闭环，则 Project 不能继续只做业务壳层，观测审计也不能继续散落在各模块中。

本组负责：

- 实现 `ProjectOrchestratorProfile`
- 建立 shared project memory 与 handoff artifact
- 落地统一观测、run replay、audit 查询

## 2. 范围

### In Scope

- `project_engine` orchestrator 化
- 项目 shared memory 模型
- handoff / report 与 run/artifact 关联
- 统一运行观测与审计查询

### Out of Scope

- OpenCode 进程基础管理
- Chat ToolLoop 底层
- Skill Studio 结构化状态细节

## 3. 当前问题

- 如果 Project 只负责 plan/apply/sync/report，而不调度统一 runtime，则项目能力只是业务层拼装，不是 Harness 原生能力。
- 如果项目上下文只存在 `ProjectContext` 摘要中，则需求交接、开发执行、复盘报告无法共享同一运行事实。
- 如果 run/step/artifact/approval 不统一，则审计与回放无法覆盖全链路。

## 4. 交付物

1. `ProjectOrchestratorProfile`
2. shared project memory 方案
3. handoff artifact 方案
4. run replay / audit API
5. 项目报告引用 runs/artifacts 的关联模型

## 5. 实施步骤

### Step 1：项目上下文重构

- 将 `ProjectContext` 与 `HarnessMemoryRef` 关联
- 将需求摘要、验收标准、handoff 状态纳入统一 memory bus

### Step 2：项目子 session 建模

- 需求 workspace = 子 session
- 开发 workspace = 子 session
- 项目 owner / member 通过 orchestrator 调度

### Step 3：交接物统一

- handoff 内容作为 `HarnessArtifact`
- report 内容作为 `HarnessArtifact`
- 与项目 runs 关联

### Step 4：统一观测

- run 查询
- step 查询
- artifact 查询
- approval 查询
- replay 能力

### Step 5：管理与审计 API

- 项目级运行追踪
- Agent 级运行聚合
- 失败统计、耗时、模型、工具、审批指标

## 6. 推荐文件边界

- `backend/app/services/project_engine.py`
- `backend/app/routers/projects.py`
- `backend/app/models/project.py`
- `backend/app/harness/profiles/project.py`
- `backend/app/harness/session_store.py`

## 7. 测试

- 项目子 session 协同测试
- handoff 可追溯测试
- report 与 run/artifact 关联测试
- replay 查询测试
- 审计聚合测试

## 8. 验收标准

- 如果项目发生需求交接，则开发 Agent 可通过统一 memory/context 读取交接物
- 如果项目生成报告，则报告可追溯到相关 runs 与 artifacts
- 如果需要审计，则可按项目、用户、Agent、工具、模型维度聚合查询
- 如果需要复盘，则可按 run id 回放关键信息链路

## 9. 依赖

- 依赖 `G1` 的状态契约
- 依赖 `G2` 的 runtime 能力
- 依赖 `G4` 的 Dev Studio backend

## 10. 交接条件

如果 `G5` 完成，则项目协作、交接、报告、审计和回放应统一建立在 Harness 的 session/run/step/artifact/memory 模型之上。
