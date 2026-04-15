# P2 实施计划：上下文摘要、缓存与 Deep Lane 回填

日期：2026-04-15  
阶段：`P2`  
上游总览：`docs/plans/2026-04-15-skill-studio-latency-implementation-overview.md`  
上游设计：`docs/plans/2026-04-15-skill-studio-latency-architecture-design.md`

## 0. 验收结论与定位

如果 `P1` 已经把首答链路和深答链路拆开，则 `P2` 的任务不是再做一遍提速，而是把双通道运行真正做稳：

- Fast Lane 不再反复装载重上下文；
- Deep Lane 的结果不再松散漂浮；
- 同一轮 run 的补丁能安全回填，而不会污染新请求。

如果没有 `P2`，则 P1 即使短期有效，也会在多轮对话、长历史、memo / recovery 较重时逐渐回退。

## 1. 阶段目标

本阶段负责三件事：

1. 上下文分层；
2. 摘要缓存；
3. 补丁式回填。

完成后应实现：

- Fast Lane 使用首答最小上下文；
- memo / recovery / source file 默认以 digest 形式进入首答；
- Deep Lane 回填以 patch 协议增量更新；
- 旧 run 不会覆盖新 run。

## 2. 范围

### In Scope

- Core / Extended / Deep 三层上下文结构；
- conversation / skill / memo / recovery 摘要缓存；
- source file 索引优先策略；
- patch 协议；
- run version / patch sequence；
- superseded 规则。

### Out of Scope

- 前端最终 UI 呈现；
- 灰度平台；
- 长期作业平台化重构。

## 3. 关键问题

- 如果 Fast Lane 继续读取全量 memo / recovery，则首答会重新变重；
- 如果 source files 默认全量进 prompt，则高复杂请求仍会明显变慢；
- 如果 Deep Lane 结果是整块覆盖，则前端状态会频繁错乱；
- 如果没有 superseded 规则，旧 run 的补丁会污染新请求。

## 4. 上下文分层设计

### Core Context

仅供首答链路使用：

- 当前用户消息
- 当前场景、复杂度、执行策略
- 最近强相关历史摘要
- 当前 workflow state 摘要
- 当前主要风险 / 约束 / 下一步摘要

### Extended Context

按需进入首答：

- memo digest
- recovery digest
- 最近测试摘要
- source file 索引摘要
- 最近 staged edits 摘要

### Deep Context

仅供 Deep Lane：

- 完整 memo
- 完整 recovery
- 长历史
- 完整 source file 内容
- 完整治理卡片 / remediation 实体

## 5. 摘要缓存设计

### 必需缓存

- `ConversationContextDigest`
- `SkillRuntimeSnapshot`
- `MemoDigest`
- `RecoveryDigest`
- `SourceFileIndexDigest`

### 设计原则

- 摘要对象必须可版本化；
- 摘要对象必须可单独失效；
- 摘要对象必须能支持首答最小包；
- 缓存命中后不应再次执行重解析。

### Digest 最小字段建议

#### MemoDigest

- lifecycle_stage
- current_task
- next_task
- latest_test_summary
- has_open_todos
- has_pending_cards

#### RecoveryDigest

- workflow_phase
- next_action
- pending_cards_count
- pending_edits_count
- updated_at / signature

#### SourceFileIndexDigest

- filename
- filetype
- size
- modified_at
- keyword_summary
- embedding / hash 预留位（可后续扩展）

## 6. Deep Lane 回填协议

### Patch 类型

- `deep_summary_patch`
- `audit_patch`
- `governance_patch`
- `staged_edit_patch`
- `workflow_patch`
- `evidence_patch`

### Patch 公共字段

- `run_id`
- `run_version`
- `patch_seq`
- `patch_type`
- `created_at`
- `payload`

### 幂等规则

- 相同 `run_id + patch_seq` 只应用一次；
- 前端与后端都按 `run_version` 判定是否过期；
- 旧 run patch 可存档，但不得覆盖当前 active run。

## 7. superseded 规则

当用户在 Deep Lane 未完成时发起新请求：

- 若新请求只是当前问题的轻追问，可尝试复用当前 run；
- 若新请求改变方向，则旧 run 标记 `superseded`；
- 被 superseded 的 run 只能归档展示，不能覆盖主状态。

## 8. 推荐文件边界

- `backend/app/services/studio_agent.py`
- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/services/skill_memo_service.py`
- 必要时新增：
  - `backend/app/services/studio_context_digest.py`
  - `backend/app/services/studio_patch_bus.py`

## 9. 实施步骤

### Step 1：定义上下文对象

- 定义 Core / Extended / Deep 三层结构
- 把 Fast Lane 输入切换到最小包

### Step 2：实现 digest 构建器

- memo digest
- recovery digest
- conversation digest
- source file index digest

### Step 3：接入缓存与失效

- skill 变更触发失效
- memo 更新触发失效
- recovery 更新触发失效

### Step 4：定义 patch 协议

- patch schema
- version 规则
- superseded 规则

### Step 5：Deep Lane 回填

- 让 Deep Lane 以 patch 回填
- 保证旧 run 不污染新 run

## 10. 测试与验收

### 测试

- digest 命中与失效测试
- Fast Lane 最小上下文测试
- source file 索引优先测试
- patch 幂等测试
- superseded run 测试

### 验收标准

- 首答 prompt 体积显著下降；
- memo / recovery 不再全量阻塞首答；
- Deep Lane 结果能被稳定回填；
- 新请求发起后，旧 patch 不再覆盖当前状态。

## 11. 风险点

- 如果 digest 与真实对象签名不一致，会导致脏缓存；
- 如果 patch 粒度过粗，会让前端回填不稳定；
- 如果 superseded 规则过于激进，可能误中断仍相关的 Deep Lane。

## 12. 结论

如果 `P2` 完成，则 Skill Studio 会从“先提速，再慢慢回退”升级成“长期可维持的双通道系统”。

如果 `P2` 未完成，则 `P1` 的收益会被长上下文和重复装载逐步吞掉。
