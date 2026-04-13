# G2 实施计划：Chat Runtime 与统一安全管线

日期：2026-04-13  
组别：`G2`  
主题：Chat Runtime / ToolLoop / SecurityPipeline

## 1. 目标

如果系统要先有一个真正走 Hermes 标准的生产 Agent，则最合理的首个落点是 Chat。

本组负责：

- 将 Chat 迁移到统一 `AgentRuntime`
- 抽离 `ToolLoop`
- 建立统一 `SecurityPipeline`
- 修复 workspace skill 边界击穿问题

## 2. 范围

### In Scope

- `skill_engine.prepare()` 的拆分
- `skill_engine._handle_tool_calls_stream()` 抽象为统一 `ToolLoop`
- `conversations.py` Chat 流式与非流式路径的 runtime 接入
- 工具调用前统一 Guard / Approval / Scope 检查
- workspace skill 边界修复

### Out of Scope

- Skill Studio architect workflow
- Sandbox 报告结构
- Dev Studio 进程管理
- Project handoff/report

## 3. 当前问题

- 如果工作台边界必须封闭，则合并全局 published skill 是错误策略。
- 如果工具调用需要权限控制，则当前 Chat Agent Loop 直接并行执行工具不符合 Hermes 标准。
- 如果同步/流式使用不同内部执行路径，则后续 replay 与审计会不一致。

## 4. 交付物

1. `AgentRuntime` 的首个生产落地版本
2. `ToolLoop` 通用实现
3. `SecurityPipeline` 通用实现
4. Chat 路径接入 adapter
5. workspace skill fallback 策略开关

## 5. 实施步骤

### Step 1：拆分 `skill_engine.prepare`

- 抽出 `SkillRouter`
- 抽出 `ContextAssembler`
- 抽出 `PromptBuilder`
- 抽出 `KnowledgeInjector`

### Step 2：抽出统一 `ToolLoop`

- 兼容 native function calling
- 兼容文本 `tool_call` fallback
- 兼容多轮工具调用
- 统一 tool result / error context / round_start / round_end 事件

### Step 3：实现 `SecurityPipeline`

- `AuthGuard`
- `ScopeGuard`
- `ModelGrantGuard`
- `ToolPermissionGuard`
- `ApprovalGuard`
- `OutputFilter`

### Step 4：修复 workspace 技能边界

- 默认只在 workspace 已挂载技能内路由
- 引入显式配置 `allow_global_skill_fallback`
- 补充个人工作台配置与 workspace config 的一致性约束

### Step 5：接入 Chat 入口

- 非流式与流式都统一调用 runtime
- 输出统一 `HarnessRun` / `HarnessStep`

## 6. 推荐文件边界

- `backend/app/services/skill_engine.py`
- `backend/app/routers/conversations.py`
- `backend/app/harness/runtime.py`
- `backend/app/harness/security.py`
- `backend/app/harness/capabilities.py`

## 7. 测试

- ToolLoop 多轮调用测试
- 审批阻塞测试
- workspace skill 边界测试
- Chat SSE 事件兼容测试
- 同步/流式结果一致性测试

## 8. 验收标准

- 如果 Chat 调用工具，则工具执行前必须经过统一 `SecurityPipeline`
- 如果工具需要审批，则运行状态进入 `waiting_approval`
- 如果 workspace 已挂载 skills，则默认不会路由到未挂载全局 skill
- 如果是同步和流式 Chat 请求，则内部使用同一个 runtime 主链

## 9. 依赖

- 依赖 `G1` 提供的契约与事件模型

## 10. 交接条件

如果 `G2` 完成，则其他组应能复用：

- `AgentRuntime`
- `ToolLoop`
- `SecurityPipeline`
- Chat 作为首个参考 Agent Profile 的接入模式
