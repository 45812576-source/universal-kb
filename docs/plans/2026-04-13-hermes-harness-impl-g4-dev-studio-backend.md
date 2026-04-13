# G4 实施计划：Dev Studio 后端化与隔离修复

日期：2026-04-13  
组别：`G4`  
主题：Dev Studio / OpenCodeBackend / Isolation

## 1. 目标

如果 Dev Studio 继续把进程池、目录布局、DB 修复、状态缓存放在 router 中，则它无法成为 Hermes 标准下稳定的执行后端。

本组负责：

- 抽出 `OpenCodeBackend`
- 抽出 `RuntimeProcessManager`
- 抽出 `WorkdirManager`
- 修复用户多项目、多工作台、多 session 隔离

## 2. 范围

### In Scope

- `dev_studio.py` 中的运行时逻辑后端化
- `_user_instances` 降级为缓存，不再作为真相源
- `StudioRegistration` 粒度升级
- `opencode.db` session/project 归并策略替换

### Out of Scope

- Project orchestration
- Chat ToolLoop
- Skill Studio architect workflow

## 3. 当前问题

- 如果使用多 worker 部署，则模块级 `_user_instances` 无法保证一致性。
- 如果用户参与多个项目，则 `(user_id, workspace_type)` 的注册粒度不够。
- 如果所有 session 被归并到 `global project`，则项目上下文隔离被抹平。

## 4. 交付物

1. `OpenCodeBackend`
2. `RuntimeProcessManager`
3. `WorkdirManager`
4. project-aware/session-aware registration 策略
5. opencode session 映射修复方案

## 5. 实施步骤

### Step 1：抽离运行时组件

- 将进程管理从 router 移出
- 将 workspace layout 管理从 router 移出
- 将 runtime 健康检查从 router 移出

### Step 2：升级注册粒度

- 设计新的 registration key
- 支持 `user_id + workspace_type + workspace_id/project_id`
- 保持旧数据迁移兼容

### Step 3：修复 session 映射策略

- 停止粗暴归并到 `global project`
- 建立 project-aware 或 workspace-aware mapping
- 保证不同项目 session 可见且不串

### Step 4：理清状态真相源

- 数据库是真相源
- 进程句柄内存缓存只是 optimization
- 重启后可从持久状态恢复

### Step 5：接入统一 backend 接口

- Dev Studio API 通过 `OpenCodeBackend` 工作
- 后续 `ProjectOrchestratorProfile` 可直接调用

## 6. 推荐文件边界

- `backend/app/routers/dev_studio.py`
- `backend/app/services/studio_registry.py`
- `backend/app/models/opencode.py`
- `backend/app/harness/backends/opencode.py`

## 7. 测试

- 多项目隔离测试
- 多工作台隔离测试
- 重启恢复测试
- 运行状态持久化测试
- legacy registration 迁移测试

## 8. 验收标准

- 如果同一用户同时打开多个项目 Dev Studio，则 workdir/session 不互串
- 如果服务进程重启，则 registration 和运行状态可恢复
- 如果多个 worker 并发访问，则状态一致
- 如果读取 opencode session，则仍能保留项目/工作台上下文

## 9. 依赖

- 依赖 `G1` 的 session key 契约
- 可与 `G2` 并行开展大部分抽离工作

## 10. 交接条件

如果 `G4` 完成，则 Dev Studio 应成为一个可被其他 Agent/Profile 调用的稳定 backend，而不是带状态的 router。
