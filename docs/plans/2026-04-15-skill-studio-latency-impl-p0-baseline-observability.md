# P0 实施计划：Skill Studio 延迟基线与 SLA 观测

日期：2026-04-15  
阶段：`P0`  
上游总览：`docs/plans/2026-04-15-skill-studio-latency-implementation-overview.md`  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 0. 验收结论与定位

如果把 `P0` 当作真正可开工的实施计划，则它的目标不是“顺手加几个日志”，而是建立后续所有优化都共享的时间口径、事件边界和慢链路诊断依据。

如果没有 `P0`，则后续 `P1-P4` 会持续面临三个问题：

- 不知道慢在入口分类、上下文装载还是模型调用；
- 不知道首答到底有没有满足 `30 秒 / 60 秒`；
- 不知道优化后是“真的快了”，还是只是把等待挪到了别处。

## 1. 阶段目标

本阶段负责把 Skill Studio 延迟问题从主观吐槽转成可量化工程对象。

核心目标：

- 定义统一时延口径；
- 建立 run 级时延观测；
- 输出三类场景的基线数据；
- 找出当前首答阻塞链路 Top N。

## 2. 范围

### In Scope

- 定义 Skill Studio 请求阶段时间点；
- 在后端埋点 accepted / classified / fast_started / first_useful_response / deep_started / deep_completed；
- 统计场景、复杂度、上下文大小与时延关联；
- 为前端记录可感知等待里程碑；
- 产出基线报表格式。

### Out of Scope

- 不改变执行策略；
- 不重构 prompt；
- 不改变 UI；
- 不做模型分级。

## 3. 核心问题

- 如果没有 `first_useful_response_at`，则无法证明首答 SLA 是否达标；
- 如果没有把 memo / recovery / source files 装载耗时拆开，则无法定位上下文装载成本；
- 如果没有按场景拆分统计，则优化结果会被平均数掩盖；
- 如果没有 run 级 trace，则无法追踪 Deep Lane 何时开始和何时结束。

## 4. 统一时间口径

本阶段必须冻结以下时间点定义：

- `request_accepted_at`
  - 后端真正接收 Skill Studio 请求的时刻
- `classified_at`
  - 场景识别 + complexity / strategy 判定完成
- `context_ready_at`
  - 首答链路需要的最小上下文准备完成
- `fast_started_at`
  - Fast Lane 模型调用开始
- `first_token_at`
  - 首 token 返回
- `first_useful_response_at`
  - 第一份可用反馈交付完成
- `deep_started_at`
  - Deep Lane 开始
- `deep_completed_at`
  - Deep Lane 补完完成
- `run_completed_at`
  - 当前 run 主流程结束

定义要求：

- 所有时间点都绑定同一 `run_id`；
- 前后端展示优先使用后端记录时间；
- `first_useful_response_at` 必须有明确判定依据，不能用 `stream ended` 代替。

## 5. 观测维度

### 请求维度

- 场景类型：新建 / 修改 / 导入审计修复
- complexity level：simple / medium / high
- execution strategy：fast_only / fast_then_deep / deep_resume

### 上下文维度

- 历史消息条数
- 历史消息 token 估算
- memo 是否存在、memo 载入耗时
- recovery 是否存在、recovery 载入耗时
- source files 数量、总大小、命中文件数
- 当前 editor prompt 长度

### 模型维度

- 路由是否调用模型
- Fast Lane 模型名
- Deep Lane 模型名
- first token latency
- first useful response latency
- deep completion latency

### 结果维度

- 首答是否达标
- 是否触发 SLA 降级
- 是否进入两段式回答
- Deep Lane 是否成功回填

## 6. 交付物

### 代码交付

1. Skill Studio run 时延埋点
2. 统一 latency event 结构
3. 基础统计查询接口或导出脚本

### 文档交付

1. 时延口径说明
2. 埋点字段清单
3. 基线报表模板
4. 慢链路诊断报告模板

### 数据交付

1. 三类场景的首版延迟基线
2. P50 / P75 / P90 首答耗时
3. Top N 阻塞链路列表

## 7. 推荐文件边界

- `backend/app/services/studio_agent.py`
- `backend/app/services/studio_workflow_orchestrator.py`
- `backend/app/routers/conversations.py`
- `backend/tests/test_studio_capabilities.py`
- 必要时新增 `backend/app/services/studio_latency_metrics.py`

## 8. 实施步骤

### Step 1：冻结时间口径

- 明确定义每个时点
- 明确 `first_useful_response_at` 判定规则
- 写入协议文档

### Step 2：后端埋点

- accepted / classified / context_ready
- fast_started / first_token / first_useful_response
- deep_started / deep_completed / run_completed

### Step 3：上下文成本拆分

- memo load
- recovery load
- source files load
- prompt build
- history trim

### Step 4：统计与导出

- 产出场景级统计
- 产出 complexity 级统计
- 产出阻塞链路明细

### Step 5：验收基线

- 生成首版报告
- 确认后续阶段统一使用该口径

## 9. 测试与验收

### 测试

- 时间点字段完整性测试
- run_id 关联测试
- 场景 / complexity 统计字段测试
- `first_useful_response_at` 判定测试

### 验收标准

- 任意一轮 Skill Studio run 都能完整采样关键时间点；
- 首答 SLA 可用数据验证；
- 团队可以明确指出当前最慢的是哪几段；
- 后续优化前后能基于同一口径对比。

## 10. 风险点

- 如果埋点只写日志、不结构化，则无法稳定分析；
- 如果 `first_useful_response` 判定含糊，则 SLA 数据失真；
- 如果上下文装载成本没有拆分，P1 会难以定位真正提速点。

## 11. 结论

如果 `P0` 完成，则 Skill Studio 的“慢”会第一次变成可度量、可回归、可证明的工程问题。

如果 `P0` 未完成，则后续所有阶段的“提速”都缺乏统一验证标准。
