# Skill Governance 源域真相改造实施计划

日期：2026-04-17

## 1. 目标

把 Skill Governance 从“自己生成并保存岗位 × 资产权限规则”的治理内核，改成“读取源域权限事实并为 Skill Studio 做只读聚合”的协作层。

核心变化：

- 表权限以数据表 / 视图 / `SkillDataGrant` / `TablePermissionPolicy` / `TableRoleGroup` 为真相。
- 知识权限以 `SkillKnowledgeReference`、知识目录权限、脱敏快照和知识治理结果为真相。
- 组织边界以 `SkillServiceRole`、岗位 / 部门 / org memory 提案为输入，不在统一治理里重复决策。
- Skill Governance 保留声明生成、挂载、测试计划、合同校验等“翻译与验证”能力。
- 旧 `RolePolicyBundle` / `RoleAssetPolicy` / `RoleAssetGranularRule` 先兼容只读，再逐步下线。

## 2. 不做的事

第一轮不直接删除旧表和旧接口，避免破坏历史数据、测试和前端调用。

第一轮不把表权限、知识权限的编辑能力搬到 Skill Governance。需要修改权限时，应跳转或回写到对应源域：

- 表权限：数据资产 / 表视图 / 表授权模块。
- 知识权限：知识权限、知识治理、脱敏反馈模块。
- 组织边界：服务岗位、组织记忆或岗位画像模块。

## 3. 推荐落地顺序

### Phase 1：新增只读源域聚合层

目标：让 Studio 先能看到“真实来源权限投影”，不再只能看旧 bundle。

后端新增：

- `GET /api/skill-governance/{skill_id}/mount-context`
- `GET /api/skill-governance/{skill_id}/mounted-permissions`

前端新增：

- 在 Skill Studio 权限面板顶部展示“源域挂载上下文 / 已挂载权限摘要”。
- 旧“岗位 × 资产策略”卡片先保留，但标记为旧版兼容。
- 旧“生成策略 / 确认策略 / 细则编辑”动作先不删除，但在 UI 上弱化。

验收：

- 新接口只读，不写 `RolePolicyBundle`、`RoleAssetPolicy`、`RoleAssetGranularRule`。
- 前端能在没有 bundle 的情况下展示源域权限摘要。
- 旧测试不因新字段缺失失败。

### Phase 2：声明与测试计划改用新聚合层

目标：声明生成和 case plan 生成不再依赖旧 bundle 作为唯一输入。

后端调整：

- `generate_declaration` 优先读取 `mount-context`。
- `generate_permission_case_plan` 优先读取 `mounted-permissions`。
- 历史 declaration / plan 继续保留 `bundle_id`，但新增 source metadata：
  - `source_mode = "domain_projection"`
  - `input_snapshot = mount_context`
  - `permission_projection_version`

兼容策略：

- 如果 `mount-context` 不可用且存在旧 bundle，则 fallback 到旧 bundle。
- 如果两者都不可用，则返回明确 blocking issue。

验收：

- 新 Skill 不需要先生成旧 bundle，也能生成权限声明。
- 有历史 bundle 的 Skill 仍可按旧流程打开和读取。
- case plan 的 source refs 能指向源域投影，而不是只指向 granular rule。

### Phase 3：冻结并下线旧治理内核

目标：真正移除“统一治理自产权限规则”的能力。

后端处理：

- 冻结以下接口为 deprecated：
  - `POST /suggest-role-asset-policies`
  - `PUT /role-asset-policies/confirm`
  - `GET /role-asset-policies`
  - `PUT /role-asset-policies/{policy_id}/granular-rules/{rule_id}`
  - `POST /suggest-granular-rules`
  - `PUT /granular-rules/confirm`
- 保留只读历史查询，避免历史页面无法打开。
- 删除或迁移后台 job 中创建 bundle / policy / granular rules 的逻辑。

前端处理：

- 删除旧“生成岗位 × 资产策略”主路径。
- 删除旧 granular 编辑主路径。
- 仅保留历史 bundle 只读折叠区，或完全迁移到审计 / 历史页面。

验收：

- 新流程中没有任何入口会创建新的 `RolePolicyBundle`。
- 权限变更只能在源域发生。
- Skill Governance 只负责聚合、声明、挂载、验证。

## 4. 新接口契约

### `GET /api/skill-governance/{skill_id}/mount-context`

用途：返回 Skill 运行所需的最小挂载上下文，面向声明生成、case plan 和 Studio 顶部摘要。

建议返回结构：

```json
{
  "skill_id": 7,
  "workspace_id": 1,
  "source_mode": "domain_projection",
  "projection_version": 3,
  "skill_content_version": 5,
  "roles": [],
  "assets": [],
  "permission_summary": {
    "table_count": 2,
    "knowledge_count": 3,
    "tool_count": 1,
    "high_risk_count": 2,
    "blocking_issues": []
  },
  "source_refs": []
}
```

字段说明：

- `roles`：来自 `SkillServiceRole` 的服务岗位输入。
- `assets`：来自 `SkillBoundAsset` 同步后的绑定资产。
- `permission_summary`：跨源域摘要，不表达新的权限决策。
- `source_refs`：用于追溯来源，如 `skill_data_grant`、`table_permission_policy`、`skill_knowledge_reference`。

### `GET /api/skill-governance/{skill_id}/mounted-permissions`

用途：返回当前 Skill 已挂载的权限投影，面向 Studio “已挂载权限摘要”和测试计划。

建议返回结构：

```json
{
  "skill_id": 7,
  "source_mode": "domain_projection",
  "table_permissions": [],
  "knowledge_permissions": [],
  "tool_permissions": [],
  "risk_controls": [],
  "blocking_issues": [],
  "deprecated_bundle": null
}
```

字段说明：

- `table_permissions`：从 `SkillDataGrant`、表绑定、表字段、视图和表权限策略聚合。
- `knowledge_permissions`：从 `SkillKnowledgeReference`、知识脱敏快照和知识权限域聚合。
- `tool_permissions`：从 Skill 绑定工具和 tool manifest 权限聚合。
- `risk_controls`：只读列出敏感字段、高风险 chunk、写权限工具等控制项。
- `deprecated_bundle`：如存在历史 bundle，仅用于兼容展示，不作为真相来源。

## 5. 后端文件清单

### 第一批必须改

- `backend/app/services/skill_governance_service.py`
  - 新增 `build_mount_context`。
  - 新增 `build_mounted_permissions`。
  - 新增源域投影序列化函数。
  - 复用 `sync_bound_assets`、`active_roles`、`active_assets`。
  - 不在新函数里创建或修改 bundle / policy / granular rule。
- `backend/app/routers/skill_governance.py`
  - 新增 `GET /{skill_id}/mount-context`。
  - 新增 `GET /{skill_id}/mounted-permissions`。
  - 两个接口都调用 `assert_skill_governance_access`。
- `backend/tests/test_skill_governance.py`
  - 新增 mount context 接口测试。
  - 新增 mounted permissions 接口测试。
  - 覆盖无旧 bundle、有旧 bundle、只有表绑定、只有知识引用等场景。

### 第二批建议改

- `backend/app/services/skill_governance_jobs.py`
  - Phase 2 时让 declaration / case plan job 读取新 source metadata。
- `backend/app/models/skill_governance.py`
  - Phase 2 如需持久化 `source_mode` / `input_snapshot`，再加字段和 migration。
- `backend/alembic/versions/*`
  - Phase 2 需要字段时新增 migration。

### 暂不改

- `backend/app/models/business.py`
  - 作为表权限源域读取，不在本轮改模型。
- `backend/app/models/knowledge_permission.py`
  - 作为知识权限源域读取，不在本轮改模型。
- `backend/app/models/skill_knowledge_ref.py`
  - 作为知识引用快照源域读取，不在本轮改模型。

## 6. 前端文件清单

### 第一批必须改

- `le-desk/src/components/skill-studio/SkillGovernancePanel.tsx`
  - 加载 `mount-context`。
  - 加载 `mounted-permissions`。
  - 将源域权限摘要放在旧 bundle 卡片之前。
  - 旧 bundle 卡片标记为“旧版兼容”。
- `le-desk/src/components/skill-studio/SkillGovernanceCards.tsx`
  - 新增 `MountContextCard` 或 `MountedPermissionsCard`。
  - 新增 TypeScript 类型：
    - `MountContext`
    - `MountedPermissions`
    - `MountedTablePermission`
    - `MountedKnowledgePermission`
    - `MountedRiskControl`
- `le-desk/src/components/skill-studio/__tests__/skill-governance-panel.test.tsx`
  - mock 新接口。
  - 覆盖没有 bundle 时仍显示源域投影。
- `le-desk/src/components/skill-studio/__tests__/skill-governance-cards.test.tsx`
  - 覆盖新卡片展示表权限、知识权限和风险控制项。

### 第二批建议改

- `le-desk/src/components/skill-studio/SkillGovernanceCards.tsx`
  - Phase 2 后弱化旧 RoleAssetPolicyCard 的默认入口。
  - Phase 3 后迁移成只读历史区。

## 7. 数据投影规则

### 表权限投影

输入：

- `SkillTableBinding`
- `SkillDataGrant`
- `BusinessTable`
- `TableView`
- `TableField`
- `TablePermissionPolicy`
- `TableRoleGroup`

输出建议：

- `asset_ref`
- `table_id`
- `view_id`
- `grant_mode`
- `allowed_actions`
- `max_disclosure_level`
- `row_access_mode`
- `field_access_mode`
- `allowed_fields`
- `blocked_fields`
- `masking_rules`
- `source_refs`

原则：

- 如果存在 `SkillDataGrant`，则以 grant 为 Skill 级授权真相。
- 如果没有 grant 但存在 Skill 表绑定，则返回 `blocking_issues: ["missing_skill_data_grant"]`，不自动推断允许。
- 字段敏感性来自 `TableField` 和表权限策略，不在 Skill Governance 里新建规则。

### 知识权限投影

输入：

- `SkillKnowledgeReference`
- `KnowledgeEntry`
- `KnowledgePermissionGrant`
- `KnowledgeMaskRuleVersion`
- `KnowledgeMaskFeedback`

输出建议：

- `knowledge_id`
- `title`
- `folder_id`
- `folder_path`
- `publish_version`
- `snapshot_desensitization_level`
- `snapshot_data_type_hits`
- `snapshot_mask_rules`
- `manager_scope_ok`
- `source_refs`

原则：

- 已发布引用以 `SkillKnowledgeReference` 快照为准。
- 如引用缺少 manager scope 或脱敏快照，则返回 blocking issue，不在 Skill Governance 自动补权限。
- chunk 级风险来自知识治理 / 脱敏快照，不再写入 `RoleAssetGranularRule`。

### 工具权限投影

输入：

- Skill 绑定工具
- Tool manifest permissions

输出建议：

- `tool_id`
- `tool_name`
- `tool_type`
- `permission_count`
- `write_capable`
- `risk_flags`
- `source_refs`

原则：

- Tool 写权限只作为风险控制项展示。
- 是否允许调用应回到工具授权或 Skill 发布策略，不由 Skill Governance 新建规则决定。

## 8. 测试清单

### 后端定向测试

- `pytest backend/tests/test_skill_governance.py -k mount_context`
- `pytest backend/tests/test_skill_governance.py -k mounted_permissions`
- `pytest backend/tests/test_skill_governance.py -k declaration`
- `pytest backend/tests/test_skill_governance.py -k permission_case_plan`

### 后端回归测试

- `pytest backend/tests`

### 前端定向测试

- `npx vitest run src/components/skill-studio/__tests__/skill-governance-panel.test.tsx`
- `npx vitest run src/components/skill-studio/__tests__/skill-governance-cards.test.tsx`

### 前端类型与回归

- `npx tsc --noEmit`
- `npx vitest run`
- `npm run lint`

## 9. 迁移检查清单

### Phase 1 完成条件

- [x] 后端新增 `mount-context` 接口。
- [x] 后端新增 `mounted-permissions` 接口。
- [x] 两个新接口只读，不创建旧 bundle / policy / granular rule。
- [x] 新接口能返回表权限投影。
- [x] 新接口能返回知识权限投影。
- [x] 新接口能返回工具权限风险控制项。
- [x] 新接口包含 `source_refs`，可追溯到源域。
- [ ] 前端能展示源域挂载上下文。
- [ ] 前端能展示已挂载权限摘要。
- [ ] 没有旧 bundle 时，前端不报错。
- [ ] 有旧 bundle 时，前端明确标记为兼容信息。
- [x] 后端新增测试通过。
- [ ] 前端新增测试通过。

### Phase 2 完成条件

- [x] declaration 生成优先使用 `mount-context`。
- [x] case plan 生成优先使用 `mounted-permissions`。
- [x] 新生成 declaration 标记 `source_mode = domain_projection`。
- [ ] 新生成 case plan 保存 `input_snapshot = mount_context`。
- [x] fallback 到旧 bundle 的路径仍可用。
- [x] blocking issues 可解释缺少的源域授权。
- [x] 历史 declaration / case plan 可继续读取。

### Phase 3 完成条件

- [x] 旧策略生成接口标记 deprecated。
- [ ] 前端不再默认展示“生成岗位 × 资产策略”。
- [ ] 前端不再提供 granular rule 编辑主入口。
- [x] 新流程不再创建 `RolePolicyBundle`。
- [x] 新流程不再创建 `RoleAssetPolicy`。
- [x] 新流程不再创建 `RoleAssetGranularRule`。
- [x] 历史 bundle 有只读查看或迁移路径。
- [ ] 删除旧逻辑前完成数据保留方案评审。

## 10. 风险与回滚

风险：

- 源域数据不完整时，新接口可能返回较多 blocking issues。
- 前端同时展示新投影和旧 bundle，短期内可能让用户困惑。
- Phase 2 改 declaration / case plan 输入时，可能影响历史测试断言。

控制：

- Phase 1 只新增只读接口和只读卡片。
- 旧 bundle 流程先保留，不影响当前用户路径。
- 新接口返回 `source_mode` 和 `source_refs`，便于定位问题来源。
- 每个 Phase 单独提交，便于回滚。

回滚：

- Phase 1 可直接隐藏前端新卡片，后端新增接口不影响旧流程。
- Phase 2 可恢复 declaration / case plan fallback 优先级，让旧 bundle 重新成为主输入。
- Phase 3 删除前必须先保留迁移分支或只读历史入口。

## 11. 建议提交拆分

### Commit 1：后端只读聚合接口

内容：

- 新增 `build_mount_context`。
- 新增 `build_mounted_permissions`。
- 新增两个 router endpoint。
- 新增后端测试。

### Commit 2：前端源域投影展示

内容：

- 新增源域权限摘要类型和卡片。
- Skill Governance Panel 加载新接口。
- 旧 bundle 卡片标记为兼容。
- 新增前端测试。

### Commit 3：声明与测试计划切源域输入

内容：

- declaration 生成优先使用 mount context。
- case plan 生成优先使用 mounted permissions。
- source metadata 写入 draft。
- 更新后端测试。

### Commit 4：旧治理内核降级

内容：

- deprecated 旧接口。
- 前端隐藏旧生成 / 确认入口。
- 历史 bundle 只读化。
- 补充回归测试。

## 12. 第一轮执行建议

如果要尽快开始且控制风险，则第一轮只执行 Commit 1 和 Commit 2。

第一轮完成后，产品上能看到：

- 这个 Skill 服务哪些岗位。
- 绑定了哪些表、知识和工具。
- 当前真实源域授权是否完整。
- 哪些资产存在敏感字段、高风险知识片段或写权限工具风险。
- 旧 bundle 只是历史兼容信息，而不是新的权限真相。

第一轮完成后，技术上仍保留：

- 旧 bundle 生成。
- 旧 granular rule 编辑。
- 旧 declaration / case plan 生成路径。

这样可以先把“源域为真相”的信息架构立起来，再逐步替换生成逻辑，最后删除旧内核。
