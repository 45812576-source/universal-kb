# Hermes Harness 整体验收整改设计

日期：2026-04-13
范围：G1-G5 验收缺口修订

## 1. 整改目标

如果按 Hermes Harness 五组实施计划严格验收，则当前版本必须补齐入口接线、安全闭环、状态持久化、Dev Studio 隔离和项目观测可恢复性。

本次选择完整整改方案，不只修测试，而是将关键运行链路改到可复验状态。

## 2. 整改清单

### G1：基础契约与入口接线

- Dev Studio `/entry` 启动入口构造 `HarnessRequest` 并写入返回元数据。
- Sandbox `/run` 入口构造 `HarnessRequest` 并写入 `SandboxTestSession.step_statuses`。
- `SessionStore` 读取 replay 时优先内存，缺失时从 `UnifiedEvent` 重建最小事件序列。

### G2：Chat Runtime 与安全管线

- `ToolLoop` 对 `SecurityPipeline` 的 `deny` 结果必须真正剔除工具调用，不能只发拒绝事件后继续执行。
- `needs_approval` 进入等待状态并阻断本轮工具执行，保留后续审批接入点。
- 工具安全检查传入 workspace / skill 上下文。

### G3：Skill Studio 与 Sandbox 收敛

- Skill Studio 状态继续写 Harness session，同时通过 `UnifiedEvent` 可 replay。
- Sandbox 保留 evidence wizard，但执行入口明确记录 `sandbox_mode=true` 的 Harness request。
- 报告、case step、证据引用保持 run 可追溯。

### G4：Dev Studio 后端化与隔离

- `StudioRegistration` 增加 `workspace_id/project_id/target_type/target_id` 维度，查询逻辑使用新 registration key。
- 保持旧数据兼容：缺省维度为空时仍能被查询和迁移。
- `opencode.db` sanitize 不再把所有 session 归并为 `global`；只补齐空 project，并保留已有 project/session 上下文。

### G5：项目编排与观测审计

- 项目 run/artifact/audit API 继续使用 `SessionStore`，并通过 `UnifiedEvent` 提供重启后的 replay 兜底。
- 子 session 注册改为明确携带 workspace/project 维度。

## 3. 验收方式

- 运行 `pytest -q backend/tests/test_harness_g1.py backend/tests/test_dev_studio_workspace.py`。
- 增补安全管线与 opencode sanitize 测试，覆盖 deny 不执行、session project 不被归并。
- 若目标测试全部通过，则进入整体回归建议阶段。
