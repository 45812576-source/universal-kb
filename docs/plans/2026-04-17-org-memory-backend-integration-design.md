# Org Memory 后端接入最小设计

日期：2026-04-17

## 背景

`backend/app/routers/org_memory.py`、`backend/app/services/org_memory_service.py` 与 `backend/app/models/org_memory.py` 已经存在，但真实后端应用入口和测试装配尚未完整接入，导致这条链路即使代码已写出，也无法稳定对外提供接口。

## 目标

本轮只做最小后端接入，不扩展前端范围：

1. 把 `org_memory` router 注册到真实后端应用。
2. 把 `org_memory` models 注册到模型聚合与测试建表流程。
3. 修复 `submit_proposal` 依赖的审批枚举缺口。
4. 增加一组主链路 API 测试，覆盖 `sources -> snapshots -> proposals -> submit`。

## 方案选择

采用最小接入方案，不重写 `org_memory` service，也不顺手扩展更多接口能力。

原因：

- 现有 router/service 已覆盖后端一期主链路；
- 当前最核心风险不是业务逻辑缺失，而是入口未接入、测试未兜底；
- 先让真实后端“可跑、可测、可代理”，再切前端最稳。

## 变更范围

### 1. 应用接入

- `backend/app/main.py`
  - 导入 `org_memory` router
  - `app.include_router(org_memory.router)`

### 2. 模型注册

- `backend/app/models/__init__.py`
  - 导入 `OrgMemorySource`
  - 导入 `OrgMemorySnapshot`
  - 导入 `OrgMemoryProposal`
  - 导入 `OrgMemoryAppliedConfig`
  - 导入 `OrgMemoryConfigVersion`
  - 导入 `OrgMemoryApprovalLink`

### 3. 审批枚举补齐

- `backend/app/models/permission.py`
  - 增加 `ApprovalRequestType.ORG_MEMORY_PROPOSAL`

### 4. 测试装配

- `backend/tests/conftest.py`
  - 确保 `app.models.org_memory` 被导入，测试建表时包含相关表
  - 测试 API app 中注册 `org_memory` router

### 5. 主链路测试

- `backend/tests/test_org_memory.py`
  - 校验空列表读取
  - 校验导入 source
  - 校验生成 snapshot
  - 校验生成 proposal
  - 校验提交审批并创建 `approval_request`

## 验收标准

如果后端接入完成，则应满足：

1. `GET /api/org-memory/sources` 能返回空列表或真实数据。
2. `POST /api/org-memory/sources/ingest` 能创建 source。
3. `POST /api/org-memory/sources/{id}/snapshots` 能生成结构化 snapshot。
4. `POST /api/org-memory/snapshots/{id}/proposals` 能生成 proposal。
5. `POST /api/org-memory/proposals/{id}/submit` 能创建审批单并返回 `approval_request_id`。
6. 对应测试可在后端测试环境中通过。
