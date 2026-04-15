# P1 实施计划：Skill Studio 后端编排与首答 SLA 真提速

日期：2026-04-15  
阶段：`P1`  
上游总览：`docs/plans/2026-04-15-skill-studio-latency-implementation-overview.md`  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 1. 阶段目标

如果第一阶段以“真提速”为目标，则本阶段不能只加中间态提示，而必须把 Skill Studio 的主执行链路从“同步重链路跑到底”改成“首答优先”的后端编排结构。

本阶段完成后，应至少满足以下结果：

- 请求具备统一的 `run / workflow` 状态；
- 系统能在入口侧判定复杂度与执行策略；
- 首答链路与深答链路解耦；
- 完整审计 / 完整治理不再默认阻塞首答；
- 支持两段式回答与 SLA 驱动降级。

## 2. 本阶段边界

### 包含

- 后端统一 run 协议；
- complexity / strategy 判定；
- Fast Lane 执行链；
- Deep Lane 启动与状态记录；
- 两段式回答；
- 首答 SLA 降级逻辑；
- 埋点首答完成时刻。

### 不包含

- 完整 digest / cache 体系；
- 前端最终中间态展示；
- 全量 patch 协议；
- 灰度平台与长期调优。

## 3. 核心拆分

### P1-A：统一 run 协议

目标：

- 为每轮 Skill Studio 请求建立统一 `run_id`、`workflow_id`、`complexity_level`、`execution_strategy`、`fast_status`、`deep_status`。

重点文件：

- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/services/studio_workflow_orchestrator.py`

任务：

1. 定义 `complexity_level = simple | medium | high`
2. 定义 `execution_strategy = fast_only | fast_then_deep | deep_resume`
3. 扩展 workflow state，加入 lane 状态与首答里程碑字段
4. 输出统一 route payload / workflow payload

验收：

- bootstrap 结果能返回统一 run 状态；
- 前端即使暂未消费全部字段，也不会破坏兼容链路。

### P1-B：复杂度分级与执行策略判定

目标：

- 在不依赖重模型的前提下，为请求快速判定复杂度与执行策略。

重点文件：

- `backend/app/services/studio_workflow_orchestrator.py`
- `backend/app/services/studio_agent.py`

判定输入建议：

- 场景类型；
- 历史消息长度；
- 是否存在 source files；
- 是否存在 memo / recovery；
- 是否需要 audit / remediation；
- 用户是否显式要求完整分析。

验收：

- 新建 / 修改 / 导入审计修复 三类场景都能产出 complexity 与 strategy；
- 判定逻辑不依赖重模型调用。

### P1-C：Fast Lane 首答链路

目标：

- 建立只为“首份可用反馈”服务的轻链路。

重点文件：

- `backend/app/services/studio_agent.py`

任务：

1. 在 `run_stream` 前半段拆出 `Fast Lane` 分支
2. 仅使用首答最小上下文构建首答 prompt
3. 将审计、治理、完整 remediation 从首答阻塞链路中拆出
4. 为首答打明确完成标记

验收：

- 中高复杂请求都可以先交付首答，再补充深答；
- optimize / audit 场景不再必须等完整审计结果才能开始回答。

### P1-D：Deep Lane 启动与两段式回答

目标：

- 在首答完成后，允许系统续跑深层结果，而不阻塞当前主消息交付。

重点文件：

- `backend/app/services/studio_agent.py`
- `backend/app/services/studio_workflow_orchestrator.py`

任务：

1. 建立 `Deep Lane` 启动条件
2. 在 run 状态中记录 `deep_pending / deep_running`
3. 输出“首答已完成，深层结果继续补完”的标准状态
4. 为后续 patch 回填预留结果结构

验收：

- 两段式回答具备明确协议；
- Deep Lane 可以独立于首答继续执行。

### P1-E：SLA 驱动降级

目标：

- 保证首答窗口到达时系统会主动交付，而不是继续被完整链路拖住。

重点文件：

- `backend/app/services/studio_agent.py`

策略：

- 中等复杂：
  - `T+10s` 检查首答准备度
  - `T+20s` 缩上下文 / 降模板
  - `T+30s` 必须交付首答
- 高复杂：
  - `T+15s` 检查准备度
  - `T+35s` 强制切两段式
  - `T+60s` 必须交付首答

验收：

- 即使深答未完成，也不会继续无限拖延首答；
- 降级后产物仍然是“可用反馈”，不是空泛状态提示。

## 4. 建议文件改动面

### 必改

- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/services/studio_workflow_orchestrator.py`
- `backend/app/services/studio_agent.py`

### 高概率需要补充

- `backend/app/routers/conversations.py`
- `backend/app/services/skill_memo_service.py`
- `backend/tests/test_studio_capabilities.py`

## 5. 测试与验收

### 单元 / 服务层

- complexity 分级测试
- execution strategy 判定测试
- Fast Lane 首答完成测试
- 两段式回答测试
- SLA 降级触发测试

### 场景测试

- 新建 Skill：首轮澄清不等完整 architect 深推理
- 修改 Skill：首答先给改动方向，不等完整治理
- 导入审计修复：首答先给阻塞项与优先级，不等完整 remediation

### 指标验证

- 记录 `request_accepted_at`
- 记录 `classified_at`
- 记录 `first_useful_response_at`
- 记录 `deep_started_at`
- 记录 `deep_completed_at`

验收口径：

- 如果 `first_useful_response_at - request_accepted_at` 显著下降，则说明 P1 有效；
- 如果只是多了状态事件，但首答耗时无改善，则说明 P1 未达标。

## 6. 风险点

- 如果 complexity 判定过重，则入口本身就会变慢；
- 如果 Fast Lane 仍然依赖完整 memo / recovery / source files，则仍会退化成旧链路；
- 如果 Deep Lane 与首答共用同一完整 prompt 构建逻辑，则并没有真正解耦；
- 如果没有明确 `first_useful_response` 事件，则后续 P3 无法准确消费。

## 7. 建议执行顺序

1. 先冻结统一 run 协议
2. 再实现 complexity / strategy 判定
3. 再从 `run_stream` 中抽出 Fast Lane
4. 再接入 Deep Lane 状态
5. 最后做 SLA 降级与测试

## 8. 结论

如果第一阶段要真正回应“Skill Studio 太慢”的问题，则最关键的不是 UI，而是先把后端主链路改成“首答优先”的双通道编排。

如果本阶段完成，则系统将第一次具备：

- 可度量的首答目标；
- 可执行的首答链路；
- 不阻塞首答的深答链路；
- 面向第二阶段前端真实中间态的协议基础。
