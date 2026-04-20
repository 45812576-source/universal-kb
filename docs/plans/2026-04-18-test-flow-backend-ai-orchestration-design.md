# 测试流真实后端化与 AI 编排设计

日期：2026-04-18

状态：设计稿

关联前端：
- `le-desk/docs/plans/2026-04-17-skill-governance-role-package-writeback-design.md`
- `le-desk/src/lib/test-flow-types.ts`
- `le-desk/src/lib/server/test-flow-service.ts`
- `le-desk/src/components/skill-studio/SkillGovernancePanel.tsx`
- `le-desk/src/app/(app)/chat/[id]/page.tsx`

关联后端：
- `backend/app/routers/sandbox_case_plans.py`
- `backend/app/routers/sandbox_interactive.py`
- `backend/app/services/skill_governance_service.py`
- `backend/app/services/studio_workflow_orchestrator.py`
- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/models/skill_governance.py`
- `backend/app/models/sandbox.py`

---

## 1. 目标

把 `le-desk` 里已经完成的测试流 UI 和交互，切换到 `universal-kb` 真实后端，并复用现有 Skill 治理、Sandbox 执行、Workflow 编排能力，形成以下闭环：

1. 聊天消息触发测试流
2. 校验 Skill 挂载 readiness
3. 读取或生成最新 case 版本
4. 用户修改并确认执行
5. materialize 到 sandbox 执行
6. 执行结果与 case 版本一一对应保存
7. Skill Studio 与 sandbox 两个入口都能查看和继续使用

本设计优先遵守以下前提：

- 前端主 UI 结构不返工
- 现有 REST 契约尽量不改，只把本地 BFF 逻辑下沉
- `universal-kb` 成为测试流事实源
- case 属于 Skill 资产，而不是某个 chat session 资产
- 执行记录与 case 版本强绑定，可追溯、可复用

---

## 2. 产品规则落地约束

### 2.1 统一触发条件

只有同时满足以下 3 个条件，系统才进入测试流：

1. 消息中明确 `@` 到目标 Skill
2. 同一条消息包含“生成测试用例”意图
3. 该 Skill 权限挂载 readiness 为完成

否则：

- 未命中意图或未命中 Skill：继续普通聊天
- 挂载未完成：进入阻断态，不生成 case

### 2.2 多 Skill 规则

如果一条消息里命中多个 Skill：

- 不要求用户重说
- 后端返回 `pick_skill` 动作和候选 Skill 列表
- 前端展示选择卡
- 用户选中 1 个 Skill 后，继续当前流程

### 2.3 挂载阻断规则

如果挂载未完成：

- 不调用 LLM 生成 case
- 不创建新的 case 版本
- 返回阻断原因、缺失挂载项、治理 CTA 元数据

Skill Studio 入口里，CTA 要指向现有治理面板链路；sandbox 入口里，CTA 返回可跳转的治理目标信息，但不把用户强制踢出当前会话。

### 2.4 已有 case 规则

如果目标 Skill 存在最近一版 case：

- 不直接复用
- 不直接覆盖
- 先返回最近版本摘要
- 等用户在 `复用 / 基于现有修改 / 重新生成` 三个动作中做出选择

### 2.5 执行规则

进入生成态后：

- Agent 首反应只生成 case 草案
- 用户后续修改优先作用在 case 上
- 只有用户显式确认后，才允许执行

### 2.6 资产规则

- case 归属 Skill 资产
- 测试结果必须与实际执行使用的 case 版本强绑定
- Skill Studio / sandbox chat 两个入口共用同一份 case 和执行历史

---

## 3. 现状与差距

### 3.1 前端现状

`le-desk` 已具备以下能力：

- 统一的测试流状态与类型定义：`src/lib/test-flow-types.ts`
- 基于本地 BFF 的 resolve 逻辑：`src/lib/server/test-flow-service.ts`
- 基于本地 JSON 的 run link 持久化：`src/lib/server/test-flow-db.ts`
- Skill Studio 治理面板已有挂载治理链路
- sandbox 历史抽屉已有会话与详情读取逻辑

也就是说，前端目前已经把“解析 → 阻断 → case 分流 → 执行结果挂接”的主交互跑通，但主逻辑仍然不在真实后端。

### 3.2 后端现状

`universal-kb` 已存在可复用能力：

1. `sandbox_case_plans` 已支持：
   - readiness
   - latest
   - generate
   - review
   - case update
   - materialize

2. `skill_governance_service` 已支持：
   - 权限声明同步
   - 最新 case plan 读取
   - 挂载 readiness 计算

3. `sandbox_interactive` 已支持：
   - session / report / history
   - 测试执行与结果保存

4. `studio_workflow_*` 已提供：
   - 统一 `workflow_state`
   - 统一 workflow event / workflow patch
   - 多入口共用的编排骨架

### 3.3 核心差距

当前还缺以下真实后端能力：

1. **消息级测试流触发解析** 仍在前端 BFF
2. **多 Skill 选择与阻断分流** 没有统一后端状态机
3. **case 作为 Skill 资产的版本治理** 还不完整
4. **执行记录与 plan version 的会话级绑定** 仍在前端本地 JSON
5. **history / report 自动带出 source case 信息** 尚未落到后端

---

## 4. 方案选择

### 4.1 方案 A：真实后端化 + 复用现有 workflow 骨架 + 新增 test flow 子状态机

这是本设计的推荐方案。

核心原则：

- `le-desk` 保留 UI 和 `/api/proxy`
- `universal-kb` 接管测试流决策与持久化
- 复用现有 `sandbox-case-plans`、`sandbox-interactive`、`skill-governance`
- 在现有 workflow 协议下新增 `test_flow` 状态机

### 4.2 方案 B：前端继续持有触发与分流，后端只负责执行与持久化

优点是开发更快，但缺点明显：

- 规则分散在前端
- 双入口一致性容易漂
- 无法让后端成为唯一事实源

### 4.3 结论

如果目标是“不返工前端，同时让真实后端接管测试流主逻辑”，则采用 **方案 A**。

---

## 5. 总体架构

```text
Skill Studio Chat / Sandbox Chat / Governance Panel
  ↓
le-desk /api/proxy/*
  ↓
universal-kb
  ├─ /api/test-flow/resolve-entry           触发解析、多 Skill 分流、阻断分流
  ├─ /api/sandbox-case-plans/*              case 版本读取、生成、编辑、materialize
  ├─ /api/sandbox/interactive/*             执行、历史、报告
  ├─ /api/skills/{skill_id}/workflow/actions  治理 CTA / workflow 行为
  ├─ skill_governance_service               挂载 readiness / 最新治理态
  ├─ test_flow_* services                   测试流编排
  ├─ workflow protocol/orchestrator         统一状态与事件
  └─ DB
      ├─ test_case_plan_drafts
      ├─ test_case_drafts
      ├─ sandbox_case_materializations
      ├─ sandbox_test_sessions
      ├─ sandbox_test_reports
      └─ test_flow_run_links                新增
```

---

## 6. 后端模块拆分

建议新增以下模块：

### 6.1 Router

- `backend/app/routers/test_flow.py`

职责：

- 暴露 `resolve-entry`
- 暴露最新摘要读取与 run link 查询（如果前端后续需要）
- 将前端 BFF 当前入口转成真实后端入口

### 6.2 Trigger Resolver

- `backend/app/services/test_flow_trigger.py`

职责：

- 解析消息中的 `@Skill`
- 识别“生成测试用例”意图
- 合并 `selected_skill_id`、`mentioned_skill_ids`
- 输出 `chat_default / pick_skill / mount_blocked / choose_existing_plan / generate_cases`

### 6.3 Readiness Service

- `backend/app/services/test_flow_readiness.py`

职责：

- 读取 Skill 挂载 readiness
- 聚合挂载阻断原因
- 转成前端统一可消费的 `blocking_issues + cta`

这里直接复用 `skill_governance_service.permission_case_plan_readiness()`。

### 6.4 Case Asset Service

- `backend/app/services/test_flow_cases.py`

职责：

- 读取 Skill 最近一版 case plan
- 按 `reuse / revise / regenerate` 做 plan 分流
- 创建新的 case plan version
- 保存用户编辑
- 计算 plan 摘要

### 6.5 Execution Service

- `backend/app/services/test_flow_execution.py`

职责：

- materialize plan 到 sandbox
- 创建 session 与 materialization 记录
- 记录本次 run 使用的 `plan_id / plan_version`

### 6.6 History Linker

- `backend/app/services/test_flow_history.py`

职责：

- 会话级保存 `session_id ↔ plan_version ↔ skill_id`
- 装饰 sandbox history/session/report 返回
- 让两个入口看到同一份可追溯链路

### 6.7 Workflow Adapter

- `backend/app/services/test_flow_workflow.py`

职责：

- 把 test flow 状态映射到统一 `workflow_state`
- 通过现有 workflow protocol 输出卡片、patch、事件

---

## 7. 数据模型设计

## 7.1 复用现有表

### `test_case_plan_drafts`

继续作为 Skill 级 case plan 主表。

已有字段已满足：

- `skill_id`
- `plan_version`
- `status`
- `focus_mode`
- `case_count`
- `governance_version`

### `test_case_drafts`

继续作为单条 case 草稿表。

已有字段已满足：

- `plan_id`
- `skill_id`
- `prompt`
- `expected_behavior`
- `status`
- `edited_by_user`

### `sandbox_case_materializations`

继续作为 case draft 到 sandbox case 的映射表。

### `sandbox_test_sessions`

继续作为执行会话主表。

### `sandbox_test_reports`

继续作为执行结果报告表。

## 7.2 扩展现有表

为满足“已有 case 不覆盖、修改要形成新版本、执行与版本绑定”的规则，建议扩展 `test_case_plan_drafts`：

- `source_plan_id`：来源 plan，自引用
- `generation_mode`：`generate | reuse | revise | regenerate`
- `entry_source`：`sandbox_chat | skill_studio_chat | skill_governance_panel`
- `conversation_id`：可空，用于回到原会话
- `summary_json`：最近版本摘要，供前端摘要卡直接展示
- `confirmed_at`：用户确认执行时间
- `latest_materialized_session_id`：最近一次执行产生的 session
- `last_used_at`：最近一次被复用或执行时间

其中最关键的是：

- `source_plan_id`
- `generation_mode`
- `summary_json`

原因：

1. `基于现有修改` 必须 fork 新 plan，不应在已存在 plan 上直接覆盖
2. `重新生成` 也应保留谱系，便于回溯
3. 摘要卡需要稳定读取摘要而不是每次临时计算

## 7.3 新增表：`test_flow_run_links`

建议新增一张后端替代前端本地 JSON 的表。

### 字段

- `id`
- `session_id`，唯一
- `report_id`，可空
- `skill_id`
- `plan_id`
- `plan_version`
- `case_count`
- `entry_source`
- `decision_mode`，`reuse | revise | regenerate`
- `conversation_id`，可空
- `workflow_id`，可空
- `created_by`
- `created_at`
- `updated_at`

### 作用

1. 让 `history / session / report` 都能关联回源 case plan
2. 替代前端 `.skill-governance/test-flow.json`
3. 让 Skill Studio / sandbox 共用一份 run link

### 为什么不直接复用 `sandbox_case_materializations`

因为 `sandbox_case_materializations` 是 case 级映射，不能表达：

- 一个 session 来自哪一版 plan
- 入口来源是什么
- 用户当时选择的是复用还是重新生成
- 当前 session 对应哪个 conversation / workflow

因此应保留 `sandbox_case_materializations` 做 case 粒度映射，再增加 `test_flow_run_links` 做 session 粒度映射。

---

## 8. 状态机设计

### 8.1 总体原则

测试流不新起一套独立 Agent runtime，而是挂在现有 workflow 协议下运行。

当消息命中测试流时：

- `workflow_mode = "test_flow"`
- `phase` 进入测试流阶段
- `metadata.test_flow` 承载详细状态

当未命中测试流时：

- 保持现有对话路由逻辑不变

### 8.2 状态枚举

建议定义：

- `idle`
- `choose_skill`
- `check_mount`
- `blocked`
- `case_branch`
- `case_edit`
- `execute`
- `review`

### 8.3 状态迁移

#### `idle`

进入条件：

- 没有命中“生成测试用例”意图
- 或没有命中目标 Skill

行为：

- 继续普通聊天

#### `choose_skill`

进入条件：

- 同一条消息命中多个 Skill

行为：

- 返回候选 Skill 列表
- 等待用户选择

#### `check_mount`

进入条件：

- 已确定唯一 Skill

行为：

- 调用 readiness service

#### `blocked`

进入条件：

- readiness 不通过

行为：

- 返回阻断原因
- 返回治理 CTA
- 不进入生成

#### `case_branch`

进入条件：

- readiness 通过

行为：

- 读取最新 plan
- 如果存在，则返回摘要和三个动作：
  - `reuse`
  - `revise`
  - `regenerate`
- 如果不存在，则直接转 `case_edit`

#### `case_edit`

进入条件：

- 用户选择 `generate / revise / regenerate`
- 或首次没有历史 plan

行为：

- 调用 LLM 生成或改写 case
- 返回可编辑 case 列表
- 用户继续输入时，优先改 case
- 未确认前不得执行

#### `execute`

进入条件：

- 用户明确点击或确认“执行”

行为：

- plan 标记 `confirmed`
- materialize 到 sandbox
- 创建 run link

#### `review`

进入条件：

- sandbox session 完成，得到 report

行为：

- 返回本次执行摘要
- history 自动可见
- 两个入口都可继续查看 / 复用 / 修改

### 8.4 workflow_state 结构

建议在现有 `WorkflowStateData.metadata` 中加入：

```json
{
  "test_flow": {
    "entry_source": "sandbox_chat",
    "matched_skill_ids": [12, 18],
    "selected_skill_id": 12,
    "phase_status": "case_edit",
    "blocking_issues": [],
    "latest_plan_summary": {
      "plan_id": 81,
      "plan_version": 4,
      "case_count": 9,
      "focus_mode": "risk_focused"
    },
    "decision_mode": "revise",
    "pending_plan_id": 82,
    "pending_plan_version": 5,
    "run_link_id": 301,
    "sandbox_session_id": 9001,
    "report_id": 10021
  }
}
```

### 8.5 workflow event 类型

建议在现有 `workflow_event / workflow_patch` 框架下新增以下 payload 类型：

- `test_flow_resolution`
- `test_flow_blocked`
- `test_flow_plan_summary`
- `test_flow_case_draft`
- `test_flow_execution_started`
- `test_flow_execution_finished`

这样前端仍只需要消费统一 workflow 流，不需要再为测试流单独造一套 SSE 协议。

---

## 9. 触发与判定设计

### 9.1 规则优先，LLM 后置

测试流触发必须采用“规则优先，LLM 后置”的策略。

#### 规则层负责

- `@Skill` 命中
- generate case intent 命中
- 多 Skill 分流
- 挂载 readiness 校验
- 历史 case 是否存在
- 是否允许执行

#### LLM 只负责

- 生成 case 草案
- 基于已有 plan 改写 case
- 生成 plan 摘要文案

### 9.2 意图判定

先复用前端现有正则：

```text
/(生成|产出|输出|给我|帮我).{0,12}(测试用例|测试集|case|cases)/i
```

后端实现时允许增加轻量语义补充，但不允许把“是否进入测试流”完全交给 LLM 自由判断。

### 9.3 Skill 命中

优先规则：

- 明文 `@Skill名称`
- 前端已传入 `mentioned_skill_ids`
- `selected_skill_id` 作为用户显式补选结果

判定顺序：

1. `selected_skill_id`
2. `mentioned_skill_ids`
3. 会话里当前强绑定 skill（如果入口为 skill studio 且会话只绑定单 Skill，可作为补充候选）

---

## 10. Case 版本策略

### 10.1 核心原则

任何会影响执行语义的 case 修改，都必须形成新的 `plan_version`。

### 10.2 各动作处理

#### 复用 `reuse`

- 不创建新 plan
- 直接复用现有最新 plan
- materialize 时在 run link 上记录 `decision_mode = reuse`

#### 基于现有修改 `revise`

- 先 fork 最新 plan 为新版本
- 新 plan 复制所有 cases
- 用户编辑只作用在新 plan

#### 重新生成 `regenerate`

- 以当前 Skill + 治理态为基线生成新 plan version
- 与旧 plan 保持 `source_plan_id` 谱系

### 10.3 禁止直接覆盖

以下对象禁止原地覆盖：

- 已执行过的 plan
- 已被引用到历史 session 的 plan
- 已作为最近稳定版本被展示给前端的 plan

这样才能保证“测试结果与实际执行 case 版本一一对应”。

---

## 11. API 设计

## 11.1 新增：`POST /api/test-flow/resolve-entry`

这是前端当前本地 BFF 入口的真实后端版本。

### 请求

```json
{
  "entry_source": "sandbox_chat",
  "conversation_id": 501,
  "content": "@销售复盘助手 生成测试用例",
  "selected_skill_id": null,
  "mentioned_skill_ids": [402],
  "candidate_skills": [
    { "id": 402, "name": "销售复盘助手" }
  ]
}
```

### 响应动作

- `chat_default`
- `pick_skill`
- `mount_blocked`
- `choose_existing_plan`
- `generate_cases`

### 响应体

沿用前端当前 `TestFlowResolveResponse`：

```json
{
  "ok": true,
  "data": {
    "action": "choose_existing_plan",
    "reason": "existing_case_plan_found",
    "skill": { "id": 402, "name": "销售复盘助手" },
    "blocking_issues": [],
    "latest_plan": {
      "id": 81,
      "skill_id": 402,
      "plan_version": 4,
      "status": "ready",
      "case_count": 9,
      "focus_mode": "risk_focused",
      "materialized_session_id": 9000
    }
  }
}
```

## 11.2 复用：`GET /api/sandbox-case-plans/{skill_id}/readiness`

保持现有路径，增强返回：

- `blocking_issues`
- `governance_version`
- `permission_declaration_version`
- `mount_cta`

新增建议字段：

```json
{
  "mount_cta": {
    "action": "open_skill_governance",
    "skill_id": 402,
    "panel": "mount_readiness",
    "focus_key": "missing_role_asset_mounts"
  }
}
```

## 11.3 复用：`GET /api/sandbox-case-plans/{skill_id}/latest`

保持现有路径，增强返回：

- `plan.summary`
- `plan.source_plan_id`
- `plan.generation_mode`
- `plan.latest_materialized_session_id`

前端摘要卡直接用这份数据。

## 11.4 复用：`POST /api/sandbox-case-plans/{skill_id}/generate`

现有接口继续保留，但语义扩展为：

- 首次生成
- 基于现有 plan 再生成
- 后续可由 `mode` 区分生成策略

建议请求补充：

```json
{
  "mode": "risk_focused",
  "generation_mode": "generate",
  "source_plan_id": null,
  "entry_source": "skill_studio_chat",
  "conversation_id": 501,
  "max_case_count": 10
}
```

## 11.5 新增：`POST /api/sandbox-case-plans/{plan_id}/fork`

用于“基于现有修改”。

### 请求

```json
{
  "mode": "revise",
  "entry_source": "skill_studio_chat",
  "conversation_id": 501
}
```

### 响应

```json
{
  "ok": true,
  "data": {
    "plan_id": 82,
    "plan_version": 5,
    "source_plan_id": 81,
    "generation_mode": "revise",
    "status": "draft"
  }
}
```

## 11.6 复用：`PUT /api/sandbox-case-plans/{plan_id}/cases/{case_id}`

继续作为单条 case 编辑接口。

注意：

- 只允许编辑当前 draft plan
- 不允许跨 plan 修改历史已执行版本

## 11.7 新增：`POST /api/sandbox-case-plans/{plan_id}/confirm`

语义：用户确认这一版 case 可以执行，但还未真正 materialize。

### 响应

```json
{
  "ok": true,
  "data": {
    "plan_id": 82,
    "plan_version": 5,
    "status": "confirmed",
    "confirmed_at": "2026-04-18T16:20:00+08:00"
  }
}
```

如果实现上想保持最小改动，也可以把“确认”作为 `materialize` 前置校验而不单独开表状态；但从产品语义上，单独显式 `confirm` 更清晰。

## 11.8 扩展：`POST /api/sandbox-case-plans/{plan_id}/materialize`

现有接口保留，但请求体扩展为：

```json
{
  "sandbox_session_id": null,
  "entry_source": "sandbox_chat",
  "decision_mode": "revise",
  "conversation_id": 501,
  "workflow_id": "run_tf_001"
}
```

服务端除创建 sandbox session 外，还必须：

1. 写入 `test_flow_run_links`
2. 回写 `latest_materialized_session_id`
3. 记录 `last_used_at`

### 返回

```json
{
  "ok": true,
  "data": {
    "materialized_count": 8,
    "sandbox_session_id": 9001,
    "status": "ready_to_run"
  }
}
```

## 11.9 历史与详情装饰

以下接口不改路径，但响应中自动补充 source case 信息：

- `GET /api/sandbox/interactive/history`
- `GET /api/sandbox/interactive/{session_id}`
- `GET /api/sandbox/interactive/{session_id}/report`

新增响应字段：

- `source_case_plan_id`
- `source_case_plan_version`
- `source_case_count`
- `test_entry_source`
- `test_decision_mode`
- `source_conversation_id`

这正是当前前端本地 `rememberTestFlowBackendResponse()` 在做的事情，迁移后应由后端直接完成。

---

## 12. AI 编排设计

## 12.1 编排目标

AI 编排只负责：

- 在 `case_edit` 阶段生成或改写 case
- 在 `case_branch` 阶段生成摘要文案
- 在 `review` 阶段生成执行总结文案

AI 编排不负责：

- 判断是否进入测试流
- 判断挂载是否可执行
- 决定是否跳过确认直接执行

## 12.2 Prompt 输入

### 生成 case 时输入

- Skill 名称与描述
- 当前 Skill 最新版本关键信息
- 当前治理状态：
  - readiness
  - role policy bundle
  - permission declaration
- 当前挂载资产范围
- 历史最新 plan（如果是 revise / regenerate）
- 用户当前消息与修改要求
- 生成边界：
  - 不补造不存在的挂载
  - 不编造知识/数据来源
  - 不假设未确认权限

### 输出 schema

建议输出结构化 JSON：

```json
{
  "summary": "本轮覆盖 3 类高风险场景，共 8 条 case。",
  "cases": [
    {
      "case_type": "permission_boundary",
      "risk_tags": ["scope_expand", "masked_output"],
      "test_goal": "验证摘要输出不会泄露原文敏感字段",
      "test_input": "……",
      "expected_behavior": "……"
    }
  ]
}
```

### revise 模式约束

如果是 `revise`：

- 优先保留原有 case 结构
- 仅针对用户新增要求做增量修改
- 不应无理由全部重写

### regenerate 模式约束

如果是 `regenerate`：

- 允许重算测试矩阵
- 允许替换 case 列表
- 但必须保留与旧 plan 的谱系关系

## 12.3 编排接入点

建议新增：

- `backend/app/services/test_flow_llm.py`

内部调用：

- `llm_gateway`

并由 `test_flow_cases.py` 统一调度。

不要把 LLM 逻辑直接塞进 router。

---

## 13. 双入口适配

## 13.1 Skill Studio 入口

入口来源值：`skill_studio_chat`

要求：

- 阻断态返回治理 CTA，不跳出当前上下文
- workflow patch 继续走当前 studio store
- 生成后的 case panel 与治理卡片并存，不要创建第二套 runtime

如果前端当前治理打开链路已经存在，则后端只需返回稳定 CTA 元数据，不必新增专门跳转接口。

## 13.2 Sandbox Chat 入口

入口来源值：`sandbox_chat`

要求：

- 执行完成后会话自动进入历史抽屉可见状态
- `history / session / report` 都能回看到源 case 版本

因此 run link 必须在后端 materialize 阶段落库，而不是等前端观察响应后本地补记。

## 13.3 Governance Panel 入口

入口来源值：`skill_governance_panel`

要求：

- 直接复用同一组 case 资产接口
- 如果由面板直接触发 generate / revise / execute，也必须写入同样的 `plan_version` 与 `run_links`

---

## 14. 实施方案

### 14.1 P0：把前端 BFF 主逻辑下沉

目标：

- 后端提供 `POST /api/test-flow/resolve-entry`
- 后端装饰 `history / session / report`
- 前端不再依赖本地 `test-flow-db.ts` 作为事实源

交付：

- 新 router/service
- 新 run link 表
- 基础测试

### 14.2 P1：完善 case 版本治理

目标：

- 支持 `fork / confirm`
- revise / regenerate 形成新 `plan_version`
- latest 摘要卡可稳定读取

### 14.3 P2：接入 workflow patch / event

目标：

- 聊天入口触发时回写统一 workflow_state
- Skill Studio / sandbox chat 同步使用统一状态机

### 14.4 P3：灰度切流

目标：

- `le-desk` 保留 fallback
- 后端优先、fallback 次之
- 验证稳定后删除前端本地事实源逻辑

---

## 15. 测试设计

### 15.1 单元测试

建议新增：

- `backend/tests/test_test_flow_trigger.py`
- `backend/tests/test_test_flow_cases.py`
- `backend/tests/test_test_flow_history.py`

重点覆盖：

1. 未命中意图 → `chat_default`
2. 多 Skill → `pick_skill`
3. 未挂载 → `mount_blocked`
4. 已有历史 plan → `choose_existing_plan`
5. revise → fork 新版本
6. regenerate → 新 plan version
7. materialize → 创建 run link
8. history/session/report 自动带出 source case 信息

### 15.2 API 测试

建议新增：

- `backend/tests/test_test_flow_api.py`

覆盖：

- `POST /api/test-flow/resolve-entry`
- `POST /api/sandbox-case-plans/{plan_id}/fork`
- `POST /api/sandbox-case-plans/{plan_id}/confirm`
- `POST /api/sandbox-case-plans/{plan_id}/materialize`

### 15.3 集成测试

目标场景：

1. Skill Studio 入口：
   - 命中 generate intent
   - readiness 不通过
   - 返回阻断卡 CTA

2. sandbox 入口：
   - 命中 generate intent
   - 生成新 plan
   - confirm + materialize
   - history 抽屉能看到 source case 信息

3. 已有 case：
   - latest 存在
   - 选择 revise
   - fork 新版本
   - 执行后 report 与新 version 对应

---

## 16. 风险与控制

### 风险 1：已有 plan 被误覆盖

控制：

- revise/regenerate 一律新建 `plan_version`
- 历史已执行 plan 禁止原地编辑

### 风险 2：前后端双事实源不一致

控制：

- run link 改为后端落库
- 前端仅做展示与 fallback

### 风险 3：规则判定漂移

控制：

- 进入测试流的判定必须规则优先
- LLM 不参与入口判定

### 风险 4：双入口状态不同步

控制：

- 所有入口最终都读写同一组：
  - `test_case_plan_drafts`
  - `sandbox_case_materializations`
  - `test_flow_run_links`

---

## 17. 结论

如果前提是：

- `le-desk` 当前 UI 和交互不返工
- 测试用例应成为 Skill 资产
- Skill Studio 与 sandbox 两个入口必须共用同一测试流
- 执行结果必须与 case 版本一一对应

则推荐按本设计实施：

1. 在 `universal-kb` 新增 `test_flow` 真实后端入口
2. 复用现有 `sandbox-case-plans`、`sandbox-interactive`、`skill-governance`
3. 扩展 `test_case_plan_drafts` 支持版本谱系
4. 新增 `test_flow_run_links` 作为会话级事实源
5. 用现有 workflow 协议承载 `test_flow` 状态机

这样可以在不重做前端主交互的前提下，把当前本地 BFF 测试流平滑替换为真实后端能力。
