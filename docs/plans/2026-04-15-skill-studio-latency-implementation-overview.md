# Skill Studio 响应时延治理 — 分阶段实施总览

日期：2026-04-15  
项目：`/Users/xia/project/universal-kb` + `/Users/xia/project/le-desk`  
状态：实施计划草案  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 1. 目标

如果设计稿已经确认采用 `Fast Lane + Deep Lane` 双通道架构，则实施阶段的目标不是立即重写所有 Skill Studio 能力，而是分阶段把“真提速”“真回填”“真展示”三件事稳定落地。

本实施计划拆成 5 个阶段：

1. `P0` 基线观测与 SLA 埋点
2. `P1` 后端编排与首答真提速
3. `P2` 上下文摘要、缓存与异步回填
4. `P3` 前端运行协议与真实中间态
5. `P4` 灰度、回归与策略调优

## 2. 分阶段原则

- 如果某阶段负责定义统一 run 协议、复杂度分级和 lane 状态，则后续阶段必须建立在该协议之上；
- 如果某阶段只是消费 run 协议与 patch 协议，则它可以在协议冻结后并行推进；
- 如果一个优化只改善“显示”，但不改善真实首答时延，则不能算第一阶段完成；
- 如果一个能力会影响三类场景的一致性，则必须先在统一编排层收敛，再做各场景细化。

## 3. 推荐波次

### Wave 0：基线冻结

- `P0` 基线观测与 SLA 埋点
- 目标：让团队先看到当前慢在哪里，而不是凭感觉优化

### Wave 1：后端真提速

- `P1` 后端编排与首答真提速
- `P2` 的摘要缓存底座预埋

说明：

- `P1` 是第一优先级，因为它直接决定 `30 秒 / 60 秒` 是否可能达标；
- `P2` 中与缓存、摘要相关的最小能力可以在 `P1` 中预埋，但不应阻塞 `P1` 首波上线。

### Wave 2：回填闭环

- `P2` 上下文摘要、缓存与异步回填

说明：

- 如果 `P1` 已能交付首答，但 Deep Lane 仍不稳定，则 `P2` 负责把异步补完与 patch 协议做实。

### Wave 3：前端协议升级

- `P3` 前端运行协议与真实中间态

说明：

- `P3` 以 `P1/P2` 输出的 run 协议和 patch 协议为前提；
- 不先做 P1/P2，则 P3 很容易退化成伪进度方案。

### Wave 4：灰度与调优

- `P4` 灰度、回归与策略调优

说明：

- 重点是观察实际 SLA 命中率、复杂度分级误判率、Deep Lane 成功率，以及用户主观等待感受。

## 4. 分阶段依赖图

```text
P0 ──> P1 ──┬──> P2 ──> P3 ──> P4
            └──────────────────> P4
```

## 5. 阶段边界

### P0：基线观测与 SLA 埋点

- 负责定义 Skill Studio 延迟口径：
  - request accepted
  - classified
  - first useful response ready
  - deep lane completed
- 负责埋点：
  - 场景类型
  - 复杂度等级
  - history/context 大小
  - memo/recovery 加载耗时
  - LLM 首 token / 首答完成 / deep 完成耗时
- 负责输出现状基线报表
- 不改变主运行策略

### P1：后端编排与首答真提速

- 负责统一 run 状态与阶段机
- 负责复杂度分级与执行策略判定
- 负责 Fast Lane / Deep Lane 分流
- 负责两段式回答与 SLA 降级机制
- 负责把完整审计 / 治理从首答阻塞链路中拆出
- 不负责前端中间态最终形态

### P2：上下文摘要、缓存与异步回填

- 负责构建 Core / Extended / Deep 三层上下文
- 负责 memo digest / recovery digest / conversation digest / skill snapshot 缓存
- 负责 patch 协议、run version、superseded 规则
- 负责 Deep Lane 结果回填
- 不负责 UI 表现优化

### P3：前端运行协议与真实中间态

- 负责 run 生命周期前端消费
- 负责“首答完成 / Deep 补完中 / run 已过期”状态展示
- 负责把 deep patch 落到固定区域
- 负责避免旧 run 污染当前 run
- 不负责改变后端 SLA 判定逻辑

### P4：灰度、回归与策略调优

- 负责灰度策略与开关控制
- 负责观测复杂度分级质量
- 负责模型路由策略调优
- 负责 SLA 命中率、错误率、Deep Lane 回填成功率、用户等待体验复盘
- 不新增新的大架构能力

## 6. 每阶段核心交付物

### P0 交付物

- 延迟口径文档
- 运行埋点字段清单
- 首版现状报表
- 慢链路 Top N 分析

### P1 交付物

- `run / workflow` 统一状态协议
- `complexity_level + execution_strategy` 判定逻辑
- Fast Lane 首答链路
- Deep Lane 后台续跑链路
- 两段式回答策略

### P2 交付物

- context digest 构建器
- memo / recovery 摘要缓存
- patch 协议
- Deep Lane 回填状态机
- superseded 处理规则

### P3 交付物

- 前端 run-aware store
- 首答完成与 Deep Lane 补完展示
- 旧 run 归档 / 过期展示
- 审计 / 治理 / edits 补丁落点

### P4 交付物

- 灰度开关
- SLA 监控面板
- 调优手册
- 回归清单与复盘结论

## 7. 代码归属建议

### 后端主归属：`universal-kb`

重点涉及：

- `backend/app/services/studio_workflow_orchestrator.py`
- `backend/app/services/studio_agent.py`
- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/services/skill_memo_service.py`
- `backend/app/routers/conversations.py`
- `backend/tests/test_studio_capabilities.py`

### 前端主归属：`le-desk`

重点涉及：

- `src/components/skill-studio/StudioChat.tsx`
- `src/components/skill-studio/index.tsx`
- `src/components/skill-studio/RouteStatusBar.tsx`
- `src/components/skill-studio/workflow-protocol.ts`
- `src/lib/studio-store.ts`

## 8. 里程碑定义

### M0：基线可见

如果 `P0` 完成，则团队能量化看到“慢在哪里”，而不是继续凭主观感受讨论。

### M1：首答达标

如果 `P1` 完成，则 Skill Studio 至少具备在主路径上满足 `30 秒 / 60 秒` 首答目标的结构能力。

### M2：补完闭环

如果 `P2` 完成，则 Deep Lane 不再是松散后台任务，而是可回填、可追踪、可过期控制的同轮补完链路。

### M3：前端真实进度

如果 `P3` 完成，则用户能明确知道首答是否完成、Deep Lane 是否仍在运行、等待是否仍有价值。

### M4：可灰度、可调优、可复盘

如果 `P4` 完成，则该方案具备稳定上线和持续优化能力。

## 9. 首批验收指标

### 功能验收

- 三类场景都接入统一阶段机；
- 首答与深答在协议层可区分；
- 治理 / 审计不再默认阻塞首答；
- Deep Lane 结果能增量回填，不污染新 run。

### 时延验收

- 中等复杂请求的首答 P50 / P75 / P90 有明显下降；
- 高复杂请求的首答 P50 / P75 / P90 有明显下降；
- Deep Lane 存在时，首答时间不因完整治理卡片生成而明显恶化。

### 体验验收

- 用户能明确知道当前是否已拿到首答；
- 用户能明确知道后台是否还在补充结果；
- 用户不会再因为旧 run 补丁回流而感到状态错乱。

## 10. 第一波建议执行顺序

为了尽快把“真提速”做出来，建议先从以下顺序开始：

1. `P0`：埋点与基线冻结
2. `P1-A`：统一 run 协议 + complexity / strategy 判定
3. `P1-B`：Fast Lane 首答链路
4. `P1-C`：SLA 降级 + 两段式回答
5. `P2-A`：memo / recovery digest
6. `P2-B`：Deep Lane patch 回填
7. `P3`：前端 run-aware 展示
8. `P4`：灰度与调优

## 11. 结论

如果要把 Skill Studio 的慢问题从“主观抱怨”变成“可治理工程问题”，则最优方式不是先修提示词或先修 UI，而是按以下顺序推进：

- 先做观测；
- 再做后端真提速；
- 再做异步回填；
- 最后让前端把真实阶段展示出来。

如果按这个分阶段计划执行，则第一阶段就能落到真实提速，第二阶段才能基于真实状态做中间态呈现，而不是继续停留在“看起来有反馈”的层面。

## 12. 阶段文档索引

- `docs/plans/2026-04-15-skill-studio-latency-impl-p0-baseline-observability.md`
- `docs/plans/2026-04-15-skill-studio-latency-impl-p1-backend-sla.md`
- `docs/plans/2026-04-15-skill-studio-latency-impl-p2-context-cache-patching.md`
- `docs/plans/2026-04-15-skill-studio-latency-impl-p3-frontend-runtime-protocol.md`
- `docs/plans/2026-04-15-skill-studio-latency-impl-p4-rollout-tuning.md`
