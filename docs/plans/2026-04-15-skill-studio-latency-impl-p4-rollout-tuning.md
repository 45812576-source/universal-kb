# P4 实施计划：灰度发布、回归验证与策略调优

日期：2026-04-15  
阶段：`P4`  
上游总览：`docs/plans/2026-04-15-skill-studio-latency-implementation-overview.md`  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 0. 验收结论与定位

如果 `P1-P3` 已经把 Skill Studio 改造成双通道与真实中间态结构，则 `P4` 的任务是确保这套系统可以稳定上线、可度量调优、可回滚，而不是停留在实验状态。

如果没有 `P4`，则会面临：

- 不知道新策略是否真的提升了线上等待体验；
- complexity 误判可能让一部分请求过度降级或过度耗时；
- Deep Lane 成功率、首答质量与真实满意度无法闭环。

## 1. 阶段目标

本阶段负责：

- 灰度开关与发布策略；
- 回归验证体系；
- complexity / model route / 降级阈值调优；
- 线上 SLA 命中率与失败率观测；
- 用户等待体验复盘。

## 2. 范围

### In Scope

- feature flag / rollout 开关；
- 线上监控仪表；
- 回归清单；
- 调优策略；
- 失败回滚方案；
- 版本复盘。

### Out of Scope

- 新的大架构能力；
- 作业平台化重写；
- 新一轮 UI 设计。

## 3. 关键问题

- 如果直接全量切换新策略，可能会把 complexity 误判带到所有用户；
- 如果没有首答质量回看，只盯时延，可能会把响应变快但内容变差；
- 如果没有 Deep Lane 监控，可能出现首答快了但补完大面积失败。

## 4. 灰度策略

建议至少具备以下开关：

- `skill_studio_dual_lane_enabled`
- `skill_studio_fast_lane_enabled`
- `skill_studio_deep_lane_enabled`
- `skill_studio_sla_degrade_enabled`
- `skill_studio_patch_protocol_enabled`
- `skill_studio_frontend_run_protocol_enabled`

灰度顺序建议：

1. 内部账号灰度
2. 指定团队灰度
3. 部分场景灰度
4. 全量开启

## 5. 回归维度

### 场景回归

- 新建 Skill
- 修改 Skill
- 导入审计修复

### 复杂度回归

- simple
- medium
- high

### 状态回归

- 无 memo / recovery
- 有 memo / recovery
- 有 source files
- 有 sandbox remediation

### 异常回归

- Fast Lane 失败
- Deep Lane 失败
- run superseded
- patch 回填失败

## 6. 调优指标

### 时延指标

- `first_useful_response` P50 / P75 / P90
- `deep_completed` P50 / P75 / P90
- first token latency

### 质量指标

- 首答是否被继续追问澄清
- 用户是否立即要求“继续说完”
- Deep Lane 补完是否被采纳
- staged edits 采纳率

### 稳定性指标

- run 失败率
- Deep Lane 失败率
- patch 回填失败率
- superseded 误杀率

## 7. 调优对象

### complexity 判定

- 误把 high 判成 medium
- 误把 simple 判成 high

### 模型路由

- 哪些场景适合轻模型首答
- 哪些场景要更早升级重模型

### SLA 阈值

- 中等复杂的降级触发点
- 高复杂的两段式切换时机

### 上下文裁剪

- 哪些上下文对首答最有帮助
- 哪些上下文只会拖慢但不增益

## 8. 交付物

### 运行交付

1. 灰度开关
2. 回滚策略
3. 监控面板
4. 调优记录模板

### 文档交付

1. 上线检查清单
2. 回归用例清单
3. 调优手册
4. 复盘模板

## 9. 实施步骤

### Step 1：接入 feature flags

- 后端 lane / degrade / patch 开关
- 前端 run protocol 开关

### Step 2：建立监控面板

- 首答 SLA 命中率
- Deep Lane 完成率
- patch 失败率
- run superseded 分布

### Step 3：建立回归矩阵

- 三类场景
- 三类 complexity
- 多种上下文负载组合

### Step 4：灰度发布

- 内部灰度
- 小流量灰度
- 扩大灰度

### Step 5：调优与复盘

- 识别误判与慢链路
- 调整 complexity / model route / degrade 阈值
- 产出复盘结论

## 10. 推荐文件边界

- `backend` 与 `frontend` 各自的配置 / flag 接入点
- 监控查询或导出脚本
- 必要的回归测试文件
- 文档与检查清单

## 11. 测试与验收

### 验收标准

- 新方案可灰度、可回滚；
- 首答 SLA 命中率有可见提升；
- Deep Lane 成功率可观测；
- 用户等待相关吐槽显著下降；
- 若质量下降，可快速回溯到 complexity / route / prompt / context 维度。

## 12. 风险点

- 如果只盯时延不盯质量，会出现“快但没用”；
- 如果没有 feature flag，线上风险不可控；
- 如果没有回归矩阵，某些场景会在灰度时意外退化。

## 13. 结论

如果 `P4` 完成，则 Skill Studio 的时延治理不再是一次性优化，而是进入可持续发布、可持续调优、可持续复盘的工程状态。
