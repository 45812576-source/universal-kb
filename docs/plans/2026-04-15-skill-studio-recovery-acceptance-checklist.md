# Skill Studio 后端重启恢复链验收 Checklist

日期：2026-04-15  
范围：`universal-kb` 后端协议 + `le-desk` 前端恢复提示  
目标：验证 Skill Studio 在后端进程重启后仍能恢复会话状态，并能在前端明确展示恢复来源与草稿影响。

## 1. 协议前提

如果一次 Skill Studio 运行结束时产生 `studio_state_update`，则后端必须把状态双写到：

- 进程内 `HarnessSession.metadata["studio_state"]`
- 持久化事件 `UnifiedEvent(event_type="harness.studio.state_saved")`

如果后端进程重启导致内存 session 清空，则后续读状态或新建 session 时必须从 `UnifiedEvent` 回填。

## 2. 关键文件

后端：

- `backend/app/harness/session_store.py`
  - `create_or_get_session()`：新建 session 时回填持久化 `studio_state`
  - `get_studio_state_snapshot()`：统一返回 `studio_state + recovery`
  - `_load_persisted_studio_state()`：按 `user_id + workspace_id + skill_id` 查最新持久化状态
- `backend/app/routers/conversations.py`
  - `GET /api/conversations/{conv_id}/studio-state`：向前端返回恢复快照
- `backend/tests/test_conversations.py`
  - 覆盖内存读取、事件表恢复、冷启动新 store 回填

前端：

- `src/components/skill-studio/StudioChat.tsx`
  - 并行加载历史消息和 `studio-state`
  - 解析 `recovery` 并传给状态栏
  - 计算恢复对当前编辑器草稿的影响
- `src/components/skill-studio/RouteStatusBar.tsx`
  - 展示恢复 badge
  - 点击展开来源、时间、Skill、会话和草稿影响
- `src/components/skill-studio/studio-state-adapter.ts`
  - 解析恢复元数据
  - 推导恢复对编辑器草稿的影响

## 3. 自动化验证

### 3.1 后端最小回归

在 `project/universal-kb` 执行：

```bash
pytest -q backend/tests/test_conversations.py -k 'studio_state'
```

通过标准：

- 如果存在内存 session，则接口返回 `recovery.source = "memory"`
- 如果只存在 `harness.studio.state_saved` 事件，则接口返回 `recovery.source = "persisted"`
- 如果新建空 `SessionStore()`，则 `create_or_get_session(..., db=db)` 能回填 `metadata["studio_state"]`

### 3.2 前端最小回归

在 `project/le-desk` 执行：

```bash
npx vitest run \
  src/components/skill-studio/__tests__/studio-state-adapter.test.ts \
  src/components/skill-studio/__tests__/route-status-bar.test.tsx
```

通过标准：

- 如果后端返回 `recovery.source = "persisted"` 且 `cold_start = true`，则前端解析为冷启动恢复
- 如果点击恢复 badge，则详情中显示来源、Skill、会话和草稿影响
- 如果恢复包含待采纳草稿或当前编辑器本地修改，则详情能说明是否写入编辑器

## 4. 手工验收流程

### 4.1 准备一条可恢复状态

1. 打开 `le-desk` 的 Skill Studio 页面。
2. 选择一个已有 Skill，发起一轮 Studio 对话。
3. 等待本轮完成，确保后端产生 `studio_state_update`。
4. 在后端数据库中确认存在事件：
   - `event_type = "harness.studio.state_saved"`
   - `source_type = "harness"`
   - `payload.target_type = "skill"`
   - `payload.target_id = 当前 skill_id`
   - `payload.studio_state` 为对象

通过标准：

- 如果事件存在，则持久化链已经有可恢复数据。
- 如果事件不存在，则先检查 `studio_agent.py` 是否产出 `studio_state_update`，再检查 `skill_studio.py` 是否调用持久化逻辑。

### 4.2 模拟后端重启

1. 停止 `universal-kb` 后端服务。
2. 重新启动后端服务。
3. 不发送新的 Studio 消息，直接刷新 `le-desk` 的 Skill Studio 页面。

通过标准：

- 如果接口命中持久化恢复，则 `GET /api/conversations/{conv_id}/studio-state?skill_id={skill_id}` 返回：
  - `studio_state` 非空
  - `recovery.source = "persisted"`
  - `recovery.cold_start = true`
  - `recovery.recovered_at` 非空

### 4.3 验证前端恢复提示

刷新页面后观察 Studio Chat 顶部状态栏。

通过标准：

- 如果后端返回 `source = "persisted"`，则状态栏显示 `恢复：冷启动恢复`
- 如果点击恢复 badge，则展开区域显示：
  - `来源：持久化事件冷启动回填`
  - `时间：...`
  - `Skill #...`
  - `会话 #...`
  - `草稿：...`

### 4.4 验证草稿影响说明

按以下场景分别观察展开详情中的 `草稿：...` 文案。

| 场景 | 预期文案 |
| --- | --- |
| 有 `pendingDraft.system_prompt` | 已恢复待采纳草稿，尚未写入编辑器 |
| `sessionState.has_draft = true` 且编辑器有本地未保存修改 | 已恢复草稿上下文，当前编辑器仍保留本地修改 |
| `sessionState.has_draft = true` 且编辑器已有内容 | 已恢复草稿上下文，当前编辑器已有可继续编辑内容 |
| `sessionState.has_draft = true` 但编辑器为空 | 检测到历史草稿记录，但当前编辑器未加载对应内容 |
| 编辑器已有内容但恢复状态不含草稿 | 本次恢复未改写当前编辑器内容 |
| 编辑器为空且恢复状态不含草稿 | 本次恢复仅同步会话状态，未影响编辑器草稿 |

## 5. 验收结论模板

如果后端自动化回归通过、前端自动化回归通过、手工刷新后能看到 `恢复：冷启动恢复`，且展开详情能说明草稿影响，则“后端重启后也能恢复”的持久化链路验收通过。

如果接口能返回 `studio_state` 但前端不显示恢复 badge，则优先检查：

- `StudioChat.tsx` 是否读取 `response.recovery`
- `RouteStatusBar.tsx` 是否收到 `recoveryInfo`
- 当前恢复来源是否为 `none`

如果前端显示恢复 badge 但草稿影响不准确，则优先检查：

- `pendingDraft` 是否由历史消息恢复
- `sessionState.has_draft` 是否来自持久化 `studio_state`
- `editorIsDirty` 与 `currentPrompt` 是否反映当前编辑器状态

## 6. 当前边界

如果 `UnifiedEvent` 表被清空，则后端重启后无法恢复历史 Studio transient state。

如果前端编辑器内容来自用户本地未保存修改，则恢复逻辑只提示影响，不会自动覆盖编辑器。

如果多个后端进程同时写入同一 `skill_id` 的恢复事件，则当前策略按 `created_at desc, id desc` 取最新事件。
