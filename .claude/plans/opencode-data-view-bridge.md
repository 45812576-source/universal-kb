# Opencode × Le Desk 数据表打通 — 实施计划

## 总览

基于 v2 方案严格实施，7 个后端任务 + 3 个前端任务 + 1 个测试任务。
核心链路：`BusinessTable + TableView + TablePermissionPolicy + policy_engine` → opencode 可见视图列表 → 视图读取 → 权限裁剪。

---

## 后端任务

### Task 1: 新增 `data_view_runtime.py` 视图执行层服务
**文件**: `backend/app/services/data_view_runtime.py`（新建）

职责：
- `resolve_view_query(db, view: TableView, user: User, skill_id, filters, columns, limit)` → 根据视图配置解析查询范围：
  - 从 `visible_field_ids` 解析可见字段（关联 `TableField`）
  - 从 `filter_rule_json` 构建默认 WHERE
  - 从 `sort_rule_json` 构建 ORDER BY
  - 从 `group_rule_json` 构建 GROUP BY
  - 从 `aggregate_rule_json` 判断是否 aggregate-only 模式
  - `row_limit` 硬限制
  - `disclosure_ceiling` 决定输出模式
- `execute_view_read(db, view_id, user, skill_id, filters, columns, limit)` → 完整执行链：
  1. 查 TableView + BusinessTable
  2. 调 `policy_engine.resolve_user_role_groups` + `resolve_effective_policy`
  3. 根据 disclosure_level 决定模式：rows / aggregates / decision_only
  4. 构建 SQL（复用 data_engine.validate_sql）
  5. 执行查询
  6. 调 `policy_engine.apply_field_masking` 脱敏
  7. 调 `policy_engine.compute_visible_columns` 字段裁剪
  8. 返回 `{"mode": "rows|aggregates|decision", "fields": [...], "rows": [...], "summary": {...}, "applied_rules": [...]}`

依赖：`policy_engine.py`（已有），`business.py` 模型（已有）

### Task 2: 新增 `GET /api/dev-studio/data-views` 接口
**文件**: `backend/app/routers/dev_studio.py`（追加）

查询参数：`q`, `source_type`, `table_id`, `only_bindable=true`, `include_direct_table=false`

逻辑：
1. 查所有非归档 `BusinessTable`
2. LEFT JOIN `TableView`（只取 `view_purpose in (skill_runtime, explore, ops)` 且非归档、有 visible_field_ids）
3. 对每个 view，调 `policy_engine.resolve_user_role_groups` 判断用户是否匹配 `allowed_role_group_ids`
4. 组装返回字段（含 table 信息 + view 信息 + field_count + record_count_cache + sync_status + disclosure_ceiling + risk_flags）
5. 若 `include_direct_table=true` 且用户是 admin，还返回无视图的裸表

返回格式：
```json
[{
  "table_id": 1, "table_name": "customers", "display_name": "客户主数据",
  "source_type": "lark_bitable", "sync_status": "success",
  "view_id": 5, "view_name": "风控汇总", "view_purpose": "explore",
  "view_kind": "list", "disclosure_ceiling": "L3",
  "field_count": 12, "record_count_cache": 500,
  "risk_flags": []
}]
```

### Task 3: 新增 `GET /api/dev-studio/data-views/{view_id}` 详情接口
**文件**: `backend/app/routers/dev_studio.py`（追加）

返回：
- 表信息（table_id, table_name, display_name, source_type）
- 视图信息（view_id, name, view_kind, disclosure_ceiling, view_purpose）
- 可见字段列表（field_name, display_name, field_type, is_enum, enum_values, is_sensitive）
- 当前用户权限摘要（disclosure_level, row_access_mode, tool_permission_mode, denied, deny_reasons）
- 预览数据前 20 行（经过权限裁剪 + 脱敏）
- 风险提示

逻辑：
1. 查 TableView + BusinessTable + TableField
2. 调 policy_engine 获取当前用户权限
3. 若 denied → 403
4. 若非 denied → 调 `data_view_runtime.execute_view_read(limit=20)` 获取预览数据
5. 组装详情返回

### Task 4: 升级 `data_table_reader.py` 为视图优先
**文件**: `backend/app/tools/data_table_reader.py`（修改）

修改 `execute()` 函数：

新增入参支持：`view_id`, `view_name`

查找顺序：
1. `view_id` → 直接查 `TableView`
2. `table_id + view_name` → 查 `TableView.table_id + name`
3. `table_name + view_name` → 先查 `BusinessTable.table_name`，再查其下的 `TableView.name`
4. `table_name`（无 view_name） → 仅 admin 或高级授权下 fallback 整表

行为变更：
- 有 view → 走 `data_view_runtime.execute_view_read()`
- 无 view 但有 table → 检查用户权限，非 admin 返回错误提示"请指定视图"
- 工作区文件匹配 → 返回"文件已上传，尚未导入为数据表"（保留已有逻辑）
- 返回里补 `table_id`, `view_id`

### Task 5: 自动生成默认系统视图
**文件**: `backend/app/routers/data_assets.py`（追加函数），在同步完成/表创建后调用

新增函数 `ensure_default_view(db, table_id)`:
1. 查 `TableView.table_id == table_id, is_system == True, is_default == True`
2. 若不存在 → 创建：
   - `name = "默认视图"`
   - `view_purpose = "explore"`
   - `view_kind = "list"`
   - `is_system = True`
   - `is_default = True`
   - `visible_field_ids = [所有非 hidden 字段的 id]`
   - `disclosure_ceiling = None`（继承表级策略）
3. 在以下位置调用：
   - 飞书同步成功后（`sync` 接口末尾）
   - 外部导入表创建后（`business_tables.py` 的 create 接口）
   - `lark/probe` 成功并创建表后

### Task 6: 视图可用性判定 + 风险标记
**文件**: `backend/app/services/data_view_runtime.py`（追加）

新增函数 `assess_view_availability(view, user_policy)`:
- 返回 `{"available": bool, "risk_flags": [...], "display_mode": "rows|aggregate|decision"}`
- 规则：
  - 无可见字段 → `available=False`, flag="NO_FIELDS"
  - 当前用户 denied → `available=False`, flag="ACCESS_DENIED"
  - `disclosure_ceiling == "L0"` → `available=False`, flag="L0_BLOCKED"
  - `disclosure_ceiling == "L1"` → `display_mode="decision"`, flag="DECISION_ONLY"
  - `disclosure_ceiling == "L2"` → `display_mode="aggregate"`, flag="AGGREGATE_ONLY"
  - `disclosure_ceiling in ("L3", "L4")` → `display_mode="rows"`
  - `L3` → flag="MASKED_DETAIL"
  - `sync_status == "failed"` → flag="SYNC_FAILED"

### Task 7: 审计日志
**文件**: `backend/app/services/data_view_runtime.py`（追加）

在 `execute_view_read` 中，成功读取后写入 `AuditLog`：
- `user_id`, `table_name`, `operation="opencode_view_read"`, `new_values={"view_id": ..., "mode": ..., "row_count": ..., "disclosure_level": ...}`

---

## 前端任务

### Task 8: DevStudio 增加"数据资产视图"面板
**文件**: `le-desk/src/components/chat/DevStudio.tsx`（修改）

新增组件 `DataViewPanel`:
- 顶部搜索框 + source_type 筛选标签（全部 / 飞书多维表 / 外部导入）
- 列表项展示：表名、视图名、来源图标、view_kind、disclosure 标签、field_count、record_count、sync_status
- 披露级别标签：L1=红色"仅结论"、L2=橙色"仅汇总"、L3=黄色"脱敏明细"、L4=绿色"明细可读"
- 风险标记展示

按钮栏新增按钮："选择数据视图"，点击展开 DataViewPanel

API 调用：`GET /dev-studio/data-views`

### Task 9: 视图详情抽屉 + opencode 提示注入
**文件**: `le-desk/src/components/chat/DevStudio.tsx`（追加组件 `DataViewDetailDrawer`）

点击列表项后打开抽屉：
- 视图用途、可见字段列表（field_name + field_type + is_sensitive 标记）
- 枚举字段展示 enum_values
- 当前用户权限摘要（disclosure_level, row_access_mode）
- 预览数据表格（前 20 行，经裁剪）
- 披露限制说明

动作按钮：
- "插入到 opencode" → 向 opencode iframe 的工作区写入结构化提示文件 `inbox/_data_context.md`：
  ```
  ## 可用数据视图
  - 视图名：{view_name}
  - 所属表：{display_name} ({table_name})
  - 读取方式：使用 data_table_reader 工具，参数 view_id={view_id}
  - 披露级别：{disclosure_ceiling}
  - 可见字段：{field_list}
  ```
- "复制视图引用" → 复制 `view_id=123` 到剪贴板

API 调用：`GET /dev-studio/data-views/{view_id}`

### Task 10: 数据资产页增加"在 OpenCode 中使用"按钮
**文件**: `le-desk/src/app/(app)/data/components/manage/ViewBar.tsx` 或相关视图操作组件

在视图级别增加按钮：
- 仅在 view 存在时显示
- 点击 → 跳转到 `/dev-studio?view_id={view_id}`
- DevStudio 页面接收 query param 后自动打开该视图详情抽屉

---

## 测试任务

### Task 11: 测试用例
**文件**: `backend/tests/test_opencode_data_views.py`（新建）

测试覆盖：
1. 表同步后自动生成默认视图
2. `GET /data-views` 只返回当前用户有权限的视图
3. `GET /data-views` 不返回无字段的视图
4. `GET /data-views/{view_id}` 返回正确的字段列表和预览
5. `GET /data-views/{view_id}` 对 denied 用户返回 403
6. `data_table_reader` 传 view_id 能走视图读取链路
7. `data_table_reader` 无 view 时非 admin 返回提示
8. L2 视图只返回聚合结果
9. L3 视图返回脱敏数据
10. 审计日志正确写入

---

## 执行顺序

```
Task 1 (data_view_runtime) → Task 5 (默认视图) → Task 6 (可用性判定) → Task 7 (审计)
                            ↓
Task 2 (列表接口) → Task 3 (详情接口) → Task 4 (reader 升级)
                                        ↓
Task 8 (前端面板) → Task 9 (详情抽屉) → Task 10 (数据页按钮)
                                        ↓
                                    Task 11 (测试)
```

先做 Task 1 + 5，建立核心执行层和默认视图保障，然后依次推进接口和前端。
