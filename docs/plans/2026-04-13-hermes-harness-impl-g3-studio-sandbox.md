# G3 实施计划：Skill Studio 与 Sandbox 收敛

日期：2026-04-13  
组别：`G3`  
主题：Skill Studio / Sandbox / Structured Session State

## 1. 目标

如果 Skill Studio 和 Sandbox 继续保留自己的旁路运行链，则 Hermes 标准无法闭环。

本组负责：

- Skill Studio 同步/流式路径统一
- `StudioSessionState` 接入统一 session state
- Sandbox 切换为生产 Runtime 验证模式

## 2. 范围

### In Scope

- `studio_agent` 与 `conversations.py` Skill Studio 路径统一
- `system_context` 直聊快速路径下线
- architect workflow 状态接入 `HarnessSession`
- `sandbox_interactive` 的 run 阶段改用统一 `AgentRuntime`
- 沙盒报告引用真实 run/step

### Out of Scope

- OpenCode backend
- 项目编排
- Chat ToolLoop 底层实现

## 3. 当前问题

- 如果 Skill Studio 同步与流式走不同主链，则状态推进和结果不一致。
- 如果 `StudioSessionState` 不是系统级真相源，则 architect workflow 无法可靠恢复。
- 如果 Sandbox 不调用生产 Runtime，则测试报告不能证明生产行为。

## 4. 交付物

1. `SkillStudioAgentProfile`
2. `StudioSessionState` 持久化接入
3. Sandbox 生产 Runtime 验证模式
4. 沙盒报告与 run/step 关联

## 5. 实施步骤

### Step 1：统一 Skill Studio 主链

- 将同步入口改为调用 `SkillStudioAgentProfile`
- 将流式入口改为调用同一个 Profile
- 移除 `system_context` 直聊快速路径

### Step 2：迁移结构化状态

- 把 `StudioSessionState` 写入统一 session state
- 定义状态序列化格式
- 建立 phase、facts、draft readiness、ooda_round 的恢复逻辑

### Step 3：结构化事件统一

- architect question
- architect phase summary
- governance action
- studio audit
- studio draft

这些事件都应映射到 `HarnessEvent`

### Step 4：Sandbox 接入 Runtime

- 保留 evidence wizard
- `run` 阶段调用 `AgentRuntime`
- 强制 `sandbox_mode=true`
- 禁止 mock/自动补全测试输入

### Step 5：报告与追溯

- 报告关联真实 `HarnessRun`
- 每条工具、权限、审批证据可追溯到 `HarnessStep`

## 6. 推荐文件边界

- `backend/app/services/studio_agent.py`
- `backend/app/routers/conversations.py`
- `backend/app/routers/sandbox_interactive.py`
- `backend/app/models/sandbox.py`
- `backend/app/harness/profiles/skill_studio.py`
- `backend/app/harness/profiles/sandbox.py`

## 7. 测试

- Skill Studio 同步/流式一致性测试
- architect workflow 恢复测试
- Sandbox run → report 追溯测试
- 禁止 mock 输入测试
- 审批与证据完整性测试

## 8. 验收标准

- 如果 Skill Studio 同一会话走同步和流式，则状态与结果一致
- 如果 Studio 会话中断恢复，则 phase 和 confirmed facts 不丢
- 如果 Sandbox 测试完成，则报告能追溯到真实 runtime run/steps
- 如果测试输入证据不足，则不会进入执行阶段

## 9. 依赖

- 依赖 `G1` 的 session state / event 契约
- 依赖 `G2` 的 `AgentRuntime` 和 `SecurityPipeline`

## 10. 交接条件

如果 `G3` 完成，则系统应不再保留：

- Skill Studio 双轨运行
- Sandbox 旁路执行主链
