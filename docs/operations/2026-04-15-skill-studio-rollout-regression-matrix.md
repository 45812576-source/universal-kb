# Skill Studio Rollout 回归矩阵

日期：2026-04-15  
适用阶段：`P4` 灰度发布 / 回滚 / 调优

## 1. 使用方式

- 每次调整 `complexity / execution_strategy / SLA / patch protocol` 之一，都跑一轮矩阵；
- 每行至少记录：结果、负责人、时间、问题链接；
- 若任一 `关键回归项` 失败，则本轮不进入扩大灰度。

## 2. 场景矩阵

| 场景 | complexity | memo/recovery | source files | 关键回归项 | 结果 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 新建 Skill | simple | 无 | 无 | `accepted → first_useful_response` 正常；无 deep 污染 |  |  |
| 新建 Skill | medium | 无 | 有索引 | 首答不读取全量文件；可看到 deep patch |  |  |
| 新建 Skill | high | 有 memo | 有索引 | `two_stage_forced` 正常；deep patch 可回填 |  |  |
| 修改 Skill | simple | 有 memo | 无 | `fast_only` 正常；不误发 deep patch |  |  |
| 修改 Skill | medium | 有 memo | 有索引 | 首答先给优化方向；后续有 evidence patch |  |  |
| 修改 Skill | high | 有 recovery | 有索引 | deep summary / governance patch / staged edit patch 都能落地 |  |  |
| 导入审计修复 | medium | 有 recovery | 无 | 首答先给阻塞项；audit patch 正常 |  |  |
| 导入审计修复 | high | 有 recovery | 有索引 | `deep_completed` 后能看到 deep summary + evidence |  |  |

## 3. 状态矩阵

| 状态 | 验证点 | 结果 | 备注 |
| --- | --- | --- | --- |
| `accepted` | request accepted 时间戳存在 |  |  |
| `context_ready` | context digest 存在且可复用 |  |  |
| `fast_started` | fast lane 节点存在 |  |  |
| `first_token` | 首 token 埋点存在 |  |  |
| `first_useful_response` | 首答阶段正常展示 |  |  |
| `deep_started` | deep lane 开始后不覆盖首答 |  |  |
| `deep_completed` | deep patch 到达后状态转 completed |  |  |
| `superseded` | 旧 run 归档且不污染新 run |  |  |

## 4. 异常矩阵

| 异常 | 预期 | 结果 | 备注 |
| --- | --- | --- | --- |
| Fast Lane 超时 | 触发 SLA fallback 首答 |  |  |
| Deep Lane 失败 | 首答保留，错误不覆盖主消息 |  |  |
| Patch protocol 关闭 | 不再发 `patch_applied` |  |  |
| Frontend run protocol 关闭 | 不再发 `workflow_event` |  |  |
| 新请求打断旧请求 | 旧 run `superseded`，新 run version +1 |  |  |

## 5. 发布闸门

- `first_useful_response` P90 不高于上一轮基线 110%
- `run_failure_rate` 不高于上一轮基线 105%
- `deep_completion_rate` 不低于上一轮基线 95%
- `superseded` 行为抽样无旧 patch 污染

## 6. 记录字段

- 执行人：
- 执行日期：
- 代码版本：
- 灰度范围：
- 结论：
- 阻塞项：
