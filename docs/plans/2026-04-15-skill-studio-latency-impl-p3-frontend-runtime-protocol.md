# P3 实施计划：前端运行协议与真实中间态

日期：2026-04-15  
阶段：`P3`  
上游总览：`docs/plans/2026-04-15-skill-studio-latency-implementation-overview.md`  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 0. 验收结论与定位

如果 `P1/P2` 已经提供了统一 run 协议和 patch 回填，则 `P3` 的任务不是“做更花的 loading”，而是把后端真实运行状态准确地翻译成用户能理解的交互状态。

如果没有 `P3`，则用户仍然会遇到：

- 不知道首答是不是已经完成；
- 不知道 Deep Lane 是否还值得等；
- 不知道当前补丁属于哪一轮 run；
- 不知道旧结果为什么突然冒出来。

## 1. 阶段目标

本阶段负责：

- 让前端以 run 为中心消费 Skill Studio 状态；
- 明确区分首答完成与深答补完；
- 将 Deep Lane 补丁落到固定区域；
- 让过期 run 与当前 run 明确隔离。

## 2. 范围

### In Scope

- 前端 run-aware store；
- run lifecycle 协议消费；
- 首答完成状态呈现；
- Deep Lane 运行与回填状态呈现；
- run superseded 呈现；
- 审计 / 治理 / edits 补丁落点。

### Out of Scope

- 后端 SLA 判定逻辑；
- digest 生成；
- 灰度平台。

## 3. 关键问题

- 如果前端继续把所有流式事件视为“同一条消息的 streaming”，则无法表达两段式回答；
- 如果补丁继续直接插入主消息流，则主对话会被深层结果冲乱；
- 如果 store 只有一份平铺状态，则旧 run 很容易覆盖当前 run。

## 4. 协议消费模型

前端必须围绕统一 run 生命周期工作：

- `run_started`
- `run_classified`
- `fast_status`
- `fast_result`
- `deep_status`
- `patch_applied`
- `run_completed`
- `run_superseded`

每个事件都必须绑定：

- `run_id`
- `run_version`
- `conversation_id`
- `workflow_id`

## 5. UI 状态原则

### 必须明确展示的状态

- 当前 run 是否已拿到首答
- 当前 run 是否仍在 Deep Lane 补完
- 当前等待是否仍有价值
- 当前看到的 patch 属于哪一轮 run

### 禁止的状态展示

- 纯时间驱动的假进度
- 只有 spinner 没有语义说明
- 前端自己猜“应该快完成了”

## 6. 建议组件归属

### `StudioChat`

- 负责 run 生命周期
- 负责首答消息与深答补丁分流
- 负责 superseded run 处理

### `RouteStatusBar`

- 负责展示当前 run 的阶段摘要
- 负责展示下一步是否是用户动作或后台补完

### `GovernanceTimeline`

- 负责承接 governance patch

### `staged edits` 相关区域

- 负责承接 staged edit patch

### `store`

- 需要升级为 run-aware
- 维护 active run / archived runs / patch state

## 7. 交付物

### 代码交付

1. run-aware store
2. run lifecycle 事件处理器
3. 首答完成状态
4. Deep Lane 补完状态
5. superseded run 归档逻辑

### 交互交付

1. 首答完成提示
2. Deep Lane 补完提示
3. 旧 run 过期提示
4. patch 落点一致性

## 8. 实施步骤

### Step 1：扩展协议类型

- 扩展 `workflow-protocol.ts`
- 定义 run lifecycle 类型
- 定义 patch 类型

### Step 2：升级 store

- 增加 active run
- 增加 archived runs
- 增加 patch application state

### Step 3：改造 `StudioChat`

- 区分首答消息与深答补丁
- 区分 fast completed 与 deep running
- 处理 superseded run

### Step 4：改造状态栏与侧栏

- `RouteStatusBar` 展示真实阶段
- 治理 / edits / 审计结果进入固定区域

### Step 5：交互兜底

- Deep Lane 失败可重试
- 旧 run 补丁进历史区
- 当前 run 永远优先显示

## 9. 推荐文件边界

- `/Users/xia/project/le-desk/src/components/skill-studio/StudioChat.tsx`
- `/Users/xia/project/le-desk/src/components/skill-studio/index.tsx`
- `/Users/xia/project/le-desk/src/components/skill-studio/RouteStatusBar.tsx`
- `/Users/xia/project/le-desk/src/components/skill-studio/workflow-protocol.ts`
- `/Users/xia/project/le-desk/src/lib/studio-store.ts`

## 10. 测试与验收

### 测试

- run lifecycle reducer 测试
- 首答完成状态测试
- Deep Lane patch 落点测试
- superseded run 测试
- 旧 patch 不污染当前 run 测试

### 验收标准

- 用户能明确知道是否已拿到首答；
- Deep Lane 运行时，用户知道后台仍在补完；
- 旧 run 不会打乱当前 run；
- 主聊天流不被治理 / edits 补丁冲乱。

## 11. 风险点

- 如果 store 改造不彻底，旧 run 仍会覆盖当前状态；
- 如果 patch 落点不稳定，用户会觉得界面跳动和错乱；
- 如果前端自己猜状态而不是消费真实协议，则会重新引入伪进度。

## 12. 结论

如果 `P3` 完成，则 Skill Studio 的前端体验会第一次真正建立在后端真实运行状态之上，而不是建立在 loading 与猜测之上。
