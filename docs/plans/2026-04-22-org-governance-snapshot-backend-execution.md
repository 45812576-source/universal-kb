# 组织治理快照后端执行文件

## 1. 目标

本文件用于指导 `universal-kb` 后端把“组织治理快照”能力建设成 `le-desk` 与 `universal-kb` 共用的服务层，承接工作台按钮事件，输出：

- 六类快照 Tab 的 Markdown 长文档
- 由 Markdown 派生的结构化 JSON/YAML
- 面向权限控制的治理中间产物：
  - `authority_map`
  - `resource_access_matrix`
  - `approval_route_candidates`
  - `policy_hints`

同时需要保证：

- 支持工作台上下文
- 支持多源资料输入
- 支持人工修订 Markdown 后回写结构化结果
- 支持高风险权限候选不自动生效

## 2. 非目标

本期后端不负责：

- 自动把候选权限直接写入正式权限引擎
- 实时协同编辑
- 通用富文本存储
- 完整审批系统重做
- 一次性替换现有全部 `org_memory` 接口

## 3. 现状判断

当前基础：

- 已有 `workspace`、`conversation`、`org_memory` 模型
- 已有 `/api/org-memory` 基础路由
- 已有 source ingest、snapshot、proposal、governance version 能力
- 已有 Skill 治理与 mounted permission 相关服务

当前问题：

- 现有 `OrgMemorySnapshot` 结构更偏“源文档解析结果”，不适合承载多 Tab Markdown 真源
- 当前快照数据结构不足以保存：
  - `markdown_by_tab`
  - `structured_by_tab`
  - `authority_map`
  - `resource_access_matrix`
  - `approval_route_candidates`
  - `policy_hints`
- 当前接口不支持按钮事件协议
- 当前服务没有“Markdown 同步回结构化”的解析器

## 4. 后端交付物

本期后端必须交付：

### 4.1 数据层

- 新增治理快照相关表
- 支持版本化、按工作台绑定、按 Tab 存储

### 4.2 服务层

- 统一按钮事件入口
- 资料分析
- 六类快照生成
- Markdown 结构化同步
- 治理中间产物生成
- SoD 风险识别

### 4.3 API 层

- 新增工作台快照事件接口
- 新增运行状态接口
- 新增快照列表接口
- 新增快照详情接口
- 新增单 Tab Markdown 保存接口
- 新增手动同步接口

### 4.4 提示词层

- 将 `workspace-org-governance-snapshot` Skill 定稿内容仓库化
- 固化为后端可控 Prompt Profile

## 5. 数据模型设计

不建议继续把新能力直接塞进旧 `org_memory_snapshots`。

建议新增 4 张表。

### 5.1 `org_governance_snapshot_runs`

用途：

- 保存每次按钮事件运行记录
- 支持前端轮询和错误追踪

建议字段：

```python
id
run_id
event_type
workspace_id
workspace_type
app
user_id
status
request_payload_json
response_summary_json
error_message
created_at
updated_at
completed_at
```

### 5.2 `org_governance_snapshots`

用途：

- 保存一版完整治理快照

建议字段：

```python
id
workspace_id
workspace_type
app
title
version
status
scope
source_snapshot_id
base_snapshot_id
confidence_score
markdown_by_tab_json
structured_by_tab_json
governance_outputs_json
missing_items_json
conflicts_json
low_confidence_items_json
separation_of_duty_risks_json
change_summary_json
created_by
created_at
updated_at
```

### 5.3 `org_governance_snapshot_source_links`

用途：

- 建立快照与资料来源的追踪关系

建议字段：

```python
id
snapshot_id
source_type
source_id
source_uri
title
evidence_refs_json
created_at
```

### 5.4 `org_governance_snapshot_tabs`

用途：

- 单 Tab 独立保存 Markdown、结构化和同步状态

建议字段：

```python
id
snapshot_id
tab_key
markdown
structured_json
sync_status_json
parser_warnings_json
updated_by
updated_at
```

## 6. API 协议

## 6.1 统一按钮事件入口

```http
POST /api/org-memory/workspace-snapshot-events
```

事件类型：

- `snapshot.generate`
- `snapshot.update`
- `snapshot.analyze_sources`
- `snapshot.sync_from_markdown`
- `snapshot.append_sources`
- `snapshot.resolve_questions`

请求体结构：

```yaml
event_type:
workspace:
snapshot:
sources:
editor:
form:
options:
```

要求：

- 所有按钮事件统一从这里进入
- 后端内部再路由到对应 handler

## 6.2 查询运行状态

```http
GET /api/org-memory/workspace-snapshot-runs/{run_id}
```

用途：

- 前端轮询生成进度
- 定位错误

## 6.3 查询快照列表

```http
GET /api/org-memory/workspace-snapshots?workspace_id=...&app=...
```

用途：

- 返回版本列表
- 返回基本状态与时间

## 6.4 查询快照详情

```http
GET /api/org-memory/workspace-snapshots/{snapshot_id}
```

返回：

- `markdown_by_tab`
- `structured_by_tab`
- `governance_outputs`
- `missing_items`
- `conflicts`
- `low_confidence_items`
- `separation_of_duty_risks`
- `change_summary`
- `sync_status`

## 6.5 保存单 Tab Markdown

```http
PUT /api/org-memory/workspace-snapshots/{snapshot_id}/tabs/{tab_key}/markdown
```

用途：

- Markdown 真源保存
- 内部自动触发 `snapshot.sync_from_markdown`

## 6.6 手动同步全部 Markdown

```http
POST /api/org-memory/workspace-snapshots/{snapshot_id}/sync
```

用途：

- 重算全部结构化结果
- 调试或批量修复时使用

## 6.7 派生治理版本

```http
POST /api/org-memory/workspace-snapshots/{snapshot_id}/governance-version
```

用途：

- 从新治理快照派生当前已有治理版本链路
- 与现有 org memory 主链路衔接

## 7. 服务层拆分

建议新增：

```text
backend/app/services/org_governance_snapshot_service.py
backend/app/services/org_governance_snapshot_parser.py
backend/app/services/org_governance_snapshot_prompt.py
backend/app/services/org_governance_policy_projection.py
```

### 7.1 `org_governance_snapshot_service.py`

职责：

- 接收事件
- 协调 source 分类、生成、同步、保存
- 产出统一响应

核心函数建议：

- `handle_snapshot_event(payload, user, db)`
- `handle_generate(...)`
- `handle_update(...)`
- `handle_analyze_sources(...)`
- `handle_sync_from_markdown(...)`
- `handle_append_sources(...)`
- `handle_resolve_questions(...)`

### 7.2 `org_governance_snapshot_parser.py`

职责：

- 解析 Markdown
- 解析 frontmatter
- 解析固定 section
- 回写 `structured_by_tab`
- 生成 `change_summary`

核心函数建议：

- `parse_tab_markdown(tab_key, markdown, previous_structured=None)`
- `sync_snapshot_from_markdown(snapshot, tab_key, markdown, previous_structured)`
- `extract_fact_sections(...)`
- `extract_governance_sections(...)`
- `build_parser_warning(...)`

### 7.3 `org_governance_snapshot_prompt.py`

职责：

- 仓库内固化 Prompt Profile
- 组装给 LLM 的 system prompt / task prompt

### 7.4 `org_governance_policy_projection.py`

职责：

- 从 `structured_by_tab` 派生治理中间产物
- 生成：
  - `authority_map`
  - `resource_access_matrix`
  - `approval_route_candidates`
  - `policy_hints`

## 8. Prompt Profile 落地

不要运行时读取用户目录下的 Skill 文件。

建议新增仓库文件：

```text
backend/app/prompts/workspace_org_governance_snapshot.md
```

内容来源：

- `~/.agents/skills/workspace-org-governance-snapshot/SKILL.md`

使用方式：

- 后端启动时作为内置 prompt profile 使用
- 如后续需要可再同步到 DB 中的 Skill 记录，但首版先文件化

## 9. 生成流程设计

不建议让 LLM 一步输出所有字段。

建议拆三段。

### 9.1 阶段 A：资料分析

输入：

- source 内容
- workspace 上下文
- scope

输出：

- `source_classification`
- `explicit_facts`
- `derived_facts`
- `assumptions`
- `form_questions`
- `conflicts`

### 9.2 阶段 B：Tab Markdown 生成

输入：

- 资料分析结果
- 用户补缺答案
- 当前 scope
- 旧快照上下文

输出：

- `markdown_by_tab`

### 9.3 阶段 C：结构化与治理派生

输入：

- `markdown_by_tab`
- 旧结构化结果

输出：

- `structured_by_tab`
- `authority_map`
- `resource_access_matrix`
- `approval_route_candidates`
- `policy_hints`

## 10. Markdown 同步规则

同步入口：

- 单 Tab 保存
- 手动全量同步

同步步骤：

1. 保存 Markdown 原文
2. 解析 frontmatter
3. 校验固定二级标题
4. 提取 `事实区`
5. 提取 `治理语义区`
6. 提取 `证据`
7. 计算变更摘要
8. 局部重算治理中间产物
9. 写回 tab record 与 snapshot aggregate

### 10.1 失败保护

如果解析失败：

- Markdown 仍保存成功
- 不覆盖旧结构化结果
- 返回：
  - `status=partial_sync`
  - `failed_sections`
  - `parser_warnings`

### 10.2 覆盖规则

不能把以下情况误当作“删除事实”：

- 标题被改坏
- frontmatter 非法
- parser crash
- 某 section 解析为空

只有在解析明确成功时，才能把字段变更记为 `removed`。

## 11. 治理中间产物派生规则

后端必须生成以下中间产物。

### 11.1 `authority_map`

回答：

- 谁
- 因为什么身份
- 对什么资源
- 拥有什么动作权
- 证据等级和置信度是多少

### 11.2 `resource_access_matrix`

回答：

- 角色 / 部门 / 人员 × 资源 × 动作
- 条件
- 可见范围
- 脱敏模式
- 是否需要审批
- 状态：
  - `auto_apply_candidate`
  - `needs_review`
  - `blocked`

### 11.3 `approval_route_candidates`

回答：

- 某动作触发后建议走哪条审批链
- 推荐 approver 来源
- 风险等级

### 11.4 `policy_hints`

回答：

- 候选规则
- 证据等级
- 置信度
- 是否可自动应用候选

首版原则：

- 只生成候选
- 不直写正式权限表

## 12. 权限与风控规则

### 12.1 自动应用限制

禁止直接标记为 `auto_apply_candidate` 的情况：

- 假设性事实
- 跨部门原文共享
- 高敏数据原文访问
- 导出 / 对外发送
- unresolved conflict
- SoD 高风险场景

### 12.2 默认 `needs_review`

以下默认进入人工确认：

- 敏感资源
- 跨部门访问
- raw access
- export
- publish
- manage
- grant

### 12.3 SoD 风险输出

必须至少识别：

- 创建-审批冲突
- 维护-审计冲突
- 原文-脱敏-发布冲突
- 授权-使用冲突
- 删除-审计冲突

## 13. 与现有 org_memory 链路的衔接

不应立即替换旧 `source -> snapshot -> proposal -> governance version`。

建议衔接方式：

- source 仍复用现有 `OrgMemorySource`
- 新治理快照可通过 `source_snapshot_id` 关联旧 `OrgMemorySnapshot`
- 当用户点击“派生治理版本”时：
  - 从新治理快照生成候选治理结果
  - 映射到当前 proposal / governance version 结构

这样能保证：

- 旧页面继续工作
- 新能力逐步接管

## 14. migration 执行计划

### Phase BE-1：新增表

- 增加四张表
- 增加必要索引：
  - `workspace_id + app + created_at`
  - `run_id`
  - `snapshot_id + tab_key`

### Phase BE-2：schema 与 DTO

- 新增 pydantic request/response schema
- 新增 normalize / serialize 函数

### Phase BE-3：service skeleton

- 事件入口路由联通
- 空模板可返回

### Phase BE-4：Markdown parser

- 先实现 deterministic parser
- parser 成熟后再叠加 LLM 生成链路

### Phase BE-5：LLM 生成

- 接入 prompt profile
- 跑三阶段编排

### Phase BE-6：治理派生

- 生成中间产物
- 输出候选策略

## 15. 实施顺序

### Phase BE-1：数据层

- 新增 model
- 新增 migration
- 新增 repository helper

验收：

- 可以创建空 snapshot record
- 可以创建 tab record

### Phase BE-2：路由与 schema

- 增加事件入口
- 增加 list/detail/save/sync 路由

验收：

- API 文档可见
- schema 示例可校验

### Phase BE-3：Markdown 同步

- 落 parser
- 落 `partial_sync` 保护
- 落 change summary

验收：

- 单 Tab markdown 可同步结构化结果

### Phase BE-4：source analysis

- source classification
- facts extraction
- missing/conflict generation

验收：

- 能返回 `needs_input`

### Phase BE-5：Tab 生成

- 生成六个 Tab markdown
- 支持 `scope=all`
- 支持 `scope=active_tab`

验收：

- `ready_for_review` 可返回

### Phase BE-6：治理中间产物

- 生成四类中间产物
- 落状态判定

验收：

- 权限候选可供前端展示

## 16. 测试计划

### 16.1 单元测试

- schema 校验
- event route dispatch
- markdown parser
- parse failure protection
- change summary
- authority map generation
- policy hint classification
- SoD risk detection

### 16.2 集成测试

- `snapshot.generate`
- `snapshot.update`
- `snapshot.resolve_questions`
- `snapshot.sync_from_markdown`
- `snapshot.append_sources`

### 16.3 回归测试

- 现有 `/api/org-memory/sources`
- 现有 `/api/org-memory/snapshots`
- 现有 proposal / governance version 接口

## 17. 验收标准

- 后端可以接收统一按钮事件协议
- 可以基于 source 生成治理快照
- 可以返回 `needs_input / ready_for_review / partial_sync / failed`
- 可以保存单 Tab Markdown 并同步结构化结果
- 解析失败不会覆盖旧结构化结果
- 可以生成 `authority_map / resource_access_matrix / approval_route_candidates / policy_hints`
- 高风险权限候选不会直接自动生效

## 18. 风险与缓解

### 风险 1：LLM 一次输出过大且不稳定

缓解：

- 拆三阶段
- 允许阶段间持久化

### 风险 2：Markdown parser 与生成模板漂移

缓解：

- 固定模板 heading
- parser 只依赖明确标题
- 加强 parser warning

### 风险 3：旧 org_memory 链路被误伤

缓解：

- 新增接口与新表，不直接改写旧表语义
- 通过派生方式与旧治理版本衔接

### 风险 4：权限候选过于激进

缓解：

- 强制候选层
- 默认 `needs_review`
- 高敏和跨部门 raw access 一律不自动应用

## 19. 后端实施建议

如果要降低交付风险，建议按这个顺序开发：

1. migration + models
2. route + schema
3. markdown parser
4. empty template + detail APIs
5. LLM source analysis
6. markdown generation
7. governance outputs
8. governance version derivation

这样即使 LLM 编排稍晚，也能先把前端保存和同步能力接起来，不阻塞 UI 开发。
