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

## 14. 首波落地记录

### 14.1 后端全局环境开关

首波接入以下环境变量，默认保持开启，便于在上线前通过环境配置回滚：

- `SKILL_STUDIO_DUAL_LANE_ENABLED`
- `SKILL_STUDIO_FAST_LANE_ENABLED`
- `SKILL_STUDIO_DEEP_LANE_ENABLED`
- `SKILL_STUDIO_SLA_DEGRADE_ENABLED`
- `SKILL_STUDIO_PATCH_PROTOCOL_ENABLED`
- `SKILL_STUDIO_FRONTEND_RUN_PROTOCOL_ENABLED`

如果 `SKILL_STUDIO_DUAL_LANE_ENABLED=false` 或 `SKILL_STUDIO_DEEP_LANE_ENABLED=false`，则后端会把当前 run 的 `execution_strategy` 收敛为 `fast_only`，并把 `deep_status` 标为 `not_requested`。

如果 `SKILL_STUDIO_PATCH_PROTOCOL_ENABLED=false`，则后台 run 仍然发原始 SSE 事件，但不再追加 `patch_applied` 事件。

如果 `SKILL_STUDIO_FRONTEND_RUN_PROTOCOL_ENABLED=false`，则后台 run 仍然发原始 SSE 事件，但不再追加统一 `workflow_event` envelope。

### 14.2 后端 rollout 范围开关

首波支持按以下维度收敛灰度范围：

- `SKILL_STUDIO_ROLLOUT_INTERNAL_ONLY`
- `SKILL_STUDIO_ROLLOUT_USER_IDS`
- `SKILL_STUDIO_ROLLOUT_DEPARTMENT_IDS`
- `SKILL_STUDIO_ROLLOUT_SESSION_MODES`

如果未配置任何范围条件，则默认全量命中。

如果配置了任一范围条件，则用户、部门、内部账号、session mode 任一命中即可进入灰度；否则本次 run 的 effective flags 会全部收敛为关闭状态，除非用户级 feature flag 明确覆盖。

### 14.3 用户级 feature flags

首波复用现有 `users.feature_flags`，支持以下覆盖项：

- `skill_studio_dual_lane_enabled`
- `skill_studio_fast_lane_enabled`
- `skill_studio_deep_lane_enabled`
- `skill_studio_sla_degrade_enabled`
- `skill_studio_patch_protocol_enabled`
- `skill_studio_frontend_run_protocol_enabled`

这些 flag 已加入管理端默认 feature flag 列表，并作为高风险开关纳入权限变更流程。

### 14.4 前端本地开关

首波前端支持以下 `NEXT_PUBLIC` 环境变量：

- `NEXT_PUBLIC_SKILL_STUDIO_FRONTEND_RUN_PROTOCOL_ENABLED`
- `NEXT_PUBLIC_SKILL_STUDIO_PATCH_PROTOCOL_ENABLED`

前端最终以“本地环境变量 + 后端 `workflow_state.metadata.rollout.flags`”共同判定是否启用 run-aware 展示与 patch 消费。

### 14.5 Run 级可观测字段

每次 `bootstrap_workflow` 会把本次 run 的命中结果写入：

```text
workflow_state.metadata.rollout
```

其中包含：

- `eligible`
- `scope`
- `reason`
- `user_id`
- `department_id`
- `session_mode`
- `workflow_mode`
- `flags`

如果线上发生质量或时延退化，则优先按该字段回溯“本次 run 实际命中了哪些策略”。

### 14.6 首波回滚顺序

建议按最小影响面逐层回滚：

1. 如果前端 run-aware 展示异常，则关闭 `NEXT_PUBLIC_SKILL_STUDIO_FRONTEND_RUN_PROTOCOL_ENABLED` 或用户级 `skill_studio_frontend_run_protocol_enabled`；
2. 如果补丁回填异常，则关闭 `SKILL_STUDIO_PATCH_PROTOCOL_ENABLED` 或用户级 `skill_studio_patch_protocol_enabled`；
3. 如果 Deep Lane 质量或成功率异常，则关闭 `SKILL_STUDIO_DEEP_LANE_ENABLED` 或用户级 `skill_studio_deep_lane_enabled`；
4. 如果双通道整体策略异常，则关闭 `SKILL_STUDIO_DUAL_LANE_ENABLED`；
5. 如果需要全量退回旧体验，则关闭 `STUDIO_STRUCTURED_MODE`。

### 14.7 首波回归清单

- 后端：`bootstrap_workflow` 默认命中 rollout metadata；
- 后端：用户级关闭 deep lane 后，`execution_strategy=fast_only` 且 `deep_status=not_requested`；
- 后端：关闭 patch protocol 后，后台 run 不再发 `patch_applied`；
- 前端：后端关闭 `frontend_run_protocol_enabled` 后，不展示 run-aware 历史；
- 前端：后端关闭 `patch_protocol_enabled` 或 frontend run protocol 后，不消费 patch。

### 14.8 P4.5 首波实物补齐

本轮继续把 `P4` 从“后端已有指标聚合能力”推进到“管理员可直接消费的运营面板”：

- 前端管理页：`/admin/studio-metrics`
- 后端面板接口：`GET /api/admin/studio/metrics`
- 指标导出接口：`GET /api/admin/studio/metrics/export`
- 脚本导出入口：`backend/scripts/export_studio_rollout_metrics.py`
- 回归矩阵：`docs/operations/2026-04-15-skill-studio-rollout-regression-matrix.md`
- 调优模板：`docs/operations/2026-04-15-skill-studio-tuning-record-template.md`
- 前端 smoke：`frontend/e2e/admin.spec.ts` 覆盖 `/admin/studio-metrics`
- 后端接口测试：`backend/tests/test_studio_capabilities.py` 覆盖 metrics JSON 与 CSV

如果要进入下一轮扩大灰度，则建议默认执行顺序为：

1. 先看 `/admin/studio-metrics` 的 `first_useful_response`、`deep_completion_rate`、`run_failure_rate`；
2. 再导出 CSV 做按 run 明细抽样；
3. 再按回归矩阵抽样复核 `fast_only / fast_then_deep / SLA fallback / superseded`；
4. 最后把结论回填到调优模板中，决定是否扩大灰度或回滚。

### 14.9 最终验证记录

本轮收口已完成以下验证：

- 前端类型检查：`npm run typecheck`
- 前端全量 E2E：`npm run test:e2e`，`31 passed`
- 后端 Studio 能力测试：`python -m pytest tests/test_studio_capabilities.py tests/test_skill_memo.py::TestWorkflowRecovery -q`，`90 passed`

本轮同时修复了历史 E2E 与当前页面行为不一致的问题：

- 登录后落点兼容 `/chat`；
- 业务数据表生成页标题更新为“生成新数据表”；
- 知识录入改为 `/knowledge/my` 内联表单；
- Skill 发布用例补足发布校验所需 prompt 行数；
- Playwright web server 改为 `build + start`，避免 dev watcher 文件句柄上限问题。
