# Skill Studio 卡片编排大修 — M4 后端

日期：2026-04-21

状态：设计稿

关联后端：
- `backend/app/services/studio_card_resolver.py`
- `backend/app/services/studio_card_service.py`
- `backend/app/services/studio_session_service.py`
- `backend/app/services/studio_agent.py`
- `backend/app/services/studio_workflow_protocol.py`
- `backend/app/services/studio_patch_bus.py`
- `backend/app/harness/events.py`
- `backend/app/harness/profiles/skill_studio.py`

关联前端：
- `le-desk/src/lib/studio-card-types.ts`
- `le-desk/src/components/skill-studio/CardQueue.tsx`
- `le-desk/src/components/skill-studio/CardDetail.tsx`

---

## 1. 现状与 M3 已交付

M1/M2 完成了 active_card metadata 全链路接线、contract_id 透传、prompt 元数据注入。
M3 后端已交付以下能力：

| 能力 | 落地位置 | 状态 |
|------|---------|------|
| CardResolver — Architect 卡片注册表 14 张卡 | `studio_card_resolver.py` | 已上线 |
| Artifact 持久化 — `save_card_artifact` / `complete_card` / `mark_cards_stale` / `get_card_artifacts` | `studio_card_service.py` | 已上线 |
| Patch 协议 — `card_patch` / `card_status_patch` / `artifact_patch` SSE 事件 | `events.py` / `skill_studio.py` / `studio_patch_bus.py` | 已上线 |
| Session 聚合 — `completed_card_ids` / `card_artifacts` / `stale_card_ids` / `card_queue_ledger` | `studio_session_service.py` + `StudioSessionData` | 已上线 |
| Queue Window — `blocking_signal` / `resume_hint` / `active_card_explanation` / `pending_artifacts` | `_build_card_queue_window()` | 已上线 |

### M3 遗留 gap

1. **CardResolver 仅覆盖 Architect 创作流** — `create_new_skill` + `architect_phase` 之外的模式（`optimize_existing_skill` / `audit_imported_skill`）没有卡片生成逻辑
2. **Artifact 到 Prompt 的闭环缺失** — `card_artifacts` 存了但 AI prompt 没消费，下一卡看不到上一卡产物
3. **card_status_patch emit 时机不全** — 只在 `architect_phase_summary` / `architect_ready_for_draft` / `studio_phase_progress` 三处 emit，用户手动操作（采纳/拒绝/跳过）不触发
4. **stale 机制未被触发** — `mark_cards_stale()` 写好了但没有调用者
5. **Queue Window 是一次性快照** — SSE 流中不 push window 更新，前端需要 refetch 才能刷新
6. **CardResolver 是纯静态注册表** — 无法根据 AI 返回内容动态追加/跳过卡片
7. **Ledger 不跟 session_mode 走** — 所有模式共用一个 ledger 结构，缺乏模式隔离

---

## 2. M4 目标

将卡片编排从"Architect 专用静态注册表"升级为"全模式、AI 可干预、前端实时推送"的闭环编排系统。

### 核心交付

| # | 能力 | 描述 |
|---|------|------|
| B4 | **多模式 CardResolver** | `optimize_existing_skill` 和 `audit_imported_skill` 也有卡片注册表和生成逻辑 |
| B5 | **Artifact → Prompt 闭环** | 前卡 artifact 注入后卡 system prompt，AI 可见前序产物 |
| B6 | **用户动作 → card_status_patch** | 用户采纳/拒绝/跳过 staged_edit 时 emit card_status_patch + 更新 ledger |
| B7 | **Stale 联动触发** | 上游卡重做时自动 mark 下游卡 stale + emit stale_patch |
| B8 | **Queue Window 实时推送** | SSE 流中 emit `queue_window_patch`，前端不需要 refetch |
| B9 | **AI 动态卡片提案** | AI 可在回复中提议新增/跳过/合并卡片，后端验证后写入 |

---

## 3. B4: 多模式 CardResolver

### 3.1 Optimize 模式卡片注册表

`optimize_existing_skill` 生命周期按 **治理 → 修改 → 验证** 顺序：

```python
OPTIMIZE_CARDS: list[dict[str, Any]] = [
    # ── 治理卡 ──
    {"id": "governance:audit-review", "contract_id": "optimize.governance.audit_review",
     "title": "审计结果确认卡", "phase": "governance", "kind": "confirm", "mode": "report", "priority": 200},
    {"id": "governance:constraint-check", "contract_id": "optimize.governance.constraint_check",
     "title": "全局约束检查卡", "phase": "governance", "kind": "governance", "mode": "report", "priority": 199},

    # ── 修改卡 ──
    {"id": "refine:prompt-edit", "contract_id": "optimize.refine.prompt_edit",
     "title": "Prompt 修改卡", "phase": "refine", "kind": "refine", "mode": "file", "priority": 150},
    {"id": "refine:example-edit", "contract_id": "optimize.refine.example_edit",
     "title": "示例修改卡", "phase": "refine", "kind": "refine", "mode": "file", "priority": 149},
    {"id": "refine:tool-binding", "contract_id": "optimize.refine.tool_binding",
     "title": "工具绑定修改卡", "phase": "refine", "kind": "refine", "mode": "file", "priority": 148},

    # ── 验证卡 ──
    {"id": "validation:preflight", "contract_id": "optimize.validation.preflight",
     "title": "Preflight 预检卡", "phase": "validation", "kind": "validation", "mode": "report", "priority": 100},
    {"id": "validation:sandbox-run", "contract_id": "optimize.validation.sandbox_run",
     "title": "沙盒执行验证卡", "phase": "validation", "kind": "validation", "mode": "report", "priority": 99},
]
```

### 3.2 Audit 模式卡片注册表

`audit_imported_skill` 生命周期按 **审计 → 整改 → 发布** 顺序：

```python
AUDIT_CARDS: list[dict[str, Any]] = [
    # ── 审计卡 ──
    {"id": "audit:quality-scan", "contract_id": "audit.scan.quality",
     "title": "质量审计卡", "phase": "audit", "kind": "governance", "mode": "report", "priority": 200},
    {"id": "audit:security-scan", "contract_id": "audit.scan.security",
     "title": "安全审计卡", "phase": "audit", "kind": "governance", "mode": "report", "priority": 199},

    # ── 整改卡 ──
    {"id": "fixing:critical-issues", "contract_id": "audit.fixing.critical",
     "title": "严重问题整改卡", "phase": "fixing", "kind": "fixing", "mode": "file", "priority": 180},
    {"id": "fixing:moderate-issues", "contract_id": "audit.fixing.moderate",
     "title": "一般问题整改卡", "phase": "fixing", "kind": "fixing", "mode": "file", "priority": 170},

    # ── 发布前验证 ──
    {"id": "release:preflight-recheck", "contract_id": "audit.release.preflight_recheck",
     "title": "整改后 Preflight 复查卡", "phase": "release", "kind": "validation", "mode": "report", "priority": 100},
    {"id": "release:publish-gate", "contract_id": "audit.release.publish_gate",
     "title": "发布门禁卡", "phase": "release", "kind": "release", "mode": "report", "priority": 90},
]
```

### 3.3 resolve_cards 重构

当前 `resolve_cards()` 硬编码只看 `ARCHITECT_CARDS`。重构为：

```python
def resolve_cards(db, skill_id, *, session_mode, architect_phase, ...):
    if session_mode == "create_new_skill":
        registry = ARCHITECT_CARDS
        phase_groups = _ARCHITECT_PHASE_GROUPS
    elif session_mode == "optimize_existing_skill":
        registry = OPTIMIZE_CARDS
        phase_groups = _OPTIMIZE_PHASE_GROUPS
    elif session_mode == "audit_imported_skill":
        registry = AUDIT_CARDS
        phase_groups = _AUDIT_PHASE_GROUPS
    else:
        return CardResolverResult(cards=list(cards or []))

    # 后续逻辑不变：过滤 completed → 构建卡片 → 排序 → 建议 active
```

**文件**: `studio_card_resolver.py`

### 3.4 _PHASE_GROUPS 扩展

```python
_OPTIMIZE_PHASE_GROUPS = {
    "governance": ["governance"],
    "refine": ["governance", "refine"],
    "validation": ["governance", "refine", "validation"],
}
_AUDIT_PHASE_GROUPS = {
    "audit": ["audit"],
    "fixing": ["audit", "fixing"],
    "release": ["audit", "fixing", "release"],
}
```

### 3.5 session_service 集成

`studio_session_service.get_studio_session()` 中，去掉 `session_mode == "create_new_skill"` 的硬判断，改为：

```python
# M4: 所有模式都调 CardResolver
if session_mode and session_mode != "unknown":
    try:
        resolver_result = resolve_cards(db, skill_id, ...)
        ...
    except Exception:
        ...
```

`architect_phase` 参数对 optimize/audit 模式需映射：
- optimize: 从 `lifecycle_stage` 推断 → `governance` / `refine` / `validation`
- audit: 从 `workflow_state.metadata.audit_phase` 或 `lifecycle_stage` 推断

---

## 4. B5: Artifact → Prompt 闭环

### 4.1 问题

当前 `save_card_artifact()` 把阶段产物存到 `recovery.card_artifacts[contract_id][artifact_key]`，但 AI prompt 不读这些数据。下一张卡开始工作时，AI 看不到前序卡的产出。

### 4.2 方案

在 `studio_agent.py` 的 `_build_card_directive()` 中，注入 active card 的前序 artifacts：

```python
def _build_card_directive(
    active_card, active_card_contract_id, ...,
    card_artifacts: dict[str, Any] | None = None,   # M4 新增
):
    ...
    # M4: 注入前序 artifact
    if card_artifacts and active_card_contract_id:
        prior_artifacts = _collect_prior_artifacts(active_card_contract_id, card_artifacts)
        if prior_artifacts:
            directive += f"\n\n## 前序卡片产物\n{_format_artifacts_for_prompt(prior_artifacts)}"
    ...
```

### 4.3 _collect_prior_artifacts

```python
def _collect_prior_artifacts(
    current_contract_id: str,
    card_artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    """收集当前卡之前所有已完成卡的 artifact，按 contract_id 排序。"""
    # 从 contract_id 推断 phase 序列
    # 例：当前是 architect.what.mece → 收集所有 architect.why.* 的 artifact
    prefix_parts = current_contract_id.split(".")
    if len(prefix_parts) < 2:
        return []

    results = []
    for cid, artifacts in card_artifacts.items():
        if cid == current_contract_id:
            continue
        # 同 domain (architect/optimize/audit) 的前序 artifact 都收集
        if cid.split(".")[0] == prefix_parts[0]:
            for key, data in (artifacts if isinstance(artifacts, dict) else {}).items():
                results.append({"contract_id": cid, "artifact_key": key, "data": data})

    return sorted(results, key=lambda x: x["contract_id"])
```

### 4.4 _format_artifacts_for_prompt

将 artifact list 转为 LLM-readable 文本（markdown 格式）。限制总长度不超过 4000 token，超出时截断最早的 artifact 并标注。

### 4.5 传参链路

`studio_agent.run_stream()` → 从 `memo.workflow_recovery.card_artifacts` 读取 → 传入 `_build_card_directive()` → 注入 system prompt。

**文件**: `studio_agent.py`

---

## 5. B6: 用户动作 → card_status_patch

### 5.1 问题

M3 只在 AI 事件（`architect_phase_summary` / `architect_ready_for_draft` / `studio_phase_progress`）时 emit card_status_patch。用户手动操作 staged_edit（采纳/拒绝/跳过）不触发。

### 5.2 方案

在 `studio_card_service.py` 的以下函数中，返回结果增加 `events` 字段供调用方 emit：

**5.2a. adopt_staged_edit 后标记卡片**

当 staged_edit 被采纳后，如果关联的卡片所有 staged_edit 都已 resolved，自动标记卡片为 `accepted`。

在 `studio_card_service.py` 中新增：

```python
def check_card_completion_after_edit(
    db: Session,
    skill_id: int,
    *,
    origin_card_id: str,
) -> dict[str, Any] | None:
    """检查卡片关联的所有 staged_edit 是否都已 resolved。
    如果是，返回建议的 card_status_patch 事件。
    """
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == skill_id).first()
    if not memo:
        return None

    recovery = (memo.memo_payload or {}).get("workflow_recovery") or {}
    staged_edits = recovery.get("staged_edits") or []
    cards = recovery.get("cards") or []

    # 找到该卡片关联的所有 staged_edit
    card_edits = [
        e for e in staged_edits
        if isinstance(e, dict) and e.get("origin_card_id") == origin_card_id
    ]
    if not card_edits:
        return None

    # 检查是否全部 resolved
    resolved_statuses = {"adopted", "rejected", "skipped"}
    all_resolved = all(e.get("status") in resolved_statuses for e in card_edits)
    if not all_resolved:
        return None

    # 全部 resolved → 建议标记卡片
    adopted_count = sum(1 for e in card_edits if e.get("status") == "adopted")
    if adopted_count > 0:
        return {
            "card_id": origin_card_id,
            "new_status": "accepted",
            "reason": f"all_{len(card_edits)}_edits_resolved",
        }
    else:
        return {
            "card_id": origin_card_id,
            "new_status": "rejected",
            "reason": "all_edits_rejected_or_skipped",
        }
```

**5.2b. 在 action 处理层调用**

在 `studio_workflow_orchestrator.py` 或 `studio_card_service.py` 的 `handle_adopt_action` / `handle_reject_action` 末尾调用 `check_card_completion_after_edit()`，如果有返回值则 emit `card_status_patch`。

**文件**: `studio_card_service.py`, `studio_workflow_orchestrator.py`

---

## 6. B7: Stale 联动触发

### 6.1 问题

`mark_cards_stale()` 存在但没有调用者。场景：用户在 Phase 2 采纳了一个修改 → Phase 1 的 5Whys 卡产物可能需要标记 stale。

### 6.2 触发规则

| 触发条件 | stale 目标 | 原因 |
|---------|-----------|------|
| 上游 Architect 卡被重做（OODA 回调） | 该 phase 及之后 phase 的所有已完成卡 | 前提已变 |
| Prompt 主文件被用户手动修改 | 所有 `kind=refine` 且 `file_role=main_prompt` 的已完成卡 | 内容已变 |
| 沙盒验证失败 | 关联的 `kind=validation` 卡 | 验证结果无效 |

### 6.3 实现

**6.3a. OODA 回调时标记 stale**

在 `studio_agent.py` 的 `architect_ooda_decision` 处理段，当 decision 包含回调（`phase_` 开头）时：

```python
# 回调到指定阶段 → 标记后续阶段所有已完成卡 stale
if "phase_" in decision:
    callback_phase = decision.replace("回调到", "").strip()
    from app.services.studio_card_resolver import ARCHITECT_CARDS
    from app.services.studio_card_service import mark_cards_stale

    # 找到 callback_phase 之后的所有阶段的已完成卡
    phase_order = ["phase_1_why", "phase_2_what", "phase_3_how"]
    callback_idx = phase_order.index(callback_phase) if callback_phase in phase_order else -1
    later_phases = phase_order[callback_idx + 1:] if callback_idx >= 0 else []

    stale_ids = [
        c["id"] for c in ARCHITECT_CARDS
        if c["phase"] in later_phases and c["id"] in completed_card_ids_set
    ]
    if stale_ids:
        mark_cards_stale(db, selected_skill_id, card_ids=stale_ids, reason=f"ooda_callback_to_{callback_phase}")
        yield ("stale_patch", {"card_ids": stale_ids, "reason": f"ooda_callback_to_{callback_phase}"})
```

**6.3b. 新增 stale_patch 事件**

在 `events.py` 新增：
```python
STALE_PATCH = "stale_patch"
```

在 `skill_studio.py` `_STUDIO_EVENT_MAP` 新增映射。在 `studio_patch_bus.py` `PATCH_TYPE_BY_EVENT` 新增注册。

**文件**: `studio_agent.py`, `events.py`, `skill_studio.py`, `studio_patch_bus.py`

---

## 7. B8: Queue Window 实时推送

### 7.1 问题

当前 `card_queue_window` 只在 `get_studio_session()` 时构建一次。SSE 流中不 push window 更新，前端需要 refetch 才能看到窗口变化。

### 7.2 方案

每次 emit `card_status_patch` 或 `artifact_patch` 后，都追加 emit 一个 `queue_window_patch` 事件。

**7.2a. 新增事件**

`events.py`:
```python
QUEUE_WINDOW_PATCH = "queue_window_patch"
```

**7.2b. studio_agent 中自动 emit**

在 `studio_agent.py` 的事件后处理段，每次 yield `card_status_patch` 后紧跟：

```python
# M4: 推送 queue_window 更新
try:
    from app.services.studio_session_service import _build_card_queue_window
    # 重新读取最新 recovery 构建 window
    memo = db.query(SkillMemo).filter(SkillMemo.skill_id == selected_skill_id).first()
    if memo:
        recovery = (memo.memo_payload or {}).get("workflow_recovery") or {}
        all_cards = recovery.get("cards") or []
        completed = recovery.get("completed_card_ids") or []
        new_window = _build_card_queue_window(
            all_cards, active_card_id, workflow_state_dict,
            completed_card_ids=completed,
            staged_edits=recovery.get("staged_edits") or [],
        )
        if new_window:
            yield ("queue_window_patch", new_window)
except Exception as e:
    logger.warning(f"[studio_agent] queue_window_patch error: {e}")
```

**7.2c. 持久化 window 到 recovery**

每次构建新 window 后，同时写入 `recovery["queue_window"]`，这样 `get_studio_session()` 可以读取持久化的 window 而不必每次重建。

**文件**: `studio_agent.py`, `events.py`, `skill_studio.py`, `studio_patch_bus.py`

---

## 8. B9: AI 动态卡片提案

### 8.1 问题

CardResolver 是纯静态注册表。AI 无法根据用户输入动态调整卡片（例如跳过不需要的分析卡、追加自定义卡）。

### 8.2 方案 — AI 提案 + 后端验证

AI 在 structured output 中新增 `card_proposals` 字段：

```json
{
  "card_proposals": [
    {"action": "skip", "card_id": "create:architect:cynefin", "reason": "用户场景简单，不需要 Cynefin 框架"},
    {"action": "add", "title": "竞品分析卡", "phase": "phase_2_what", "contract_id": "architect.what.competitor_analysis"},
    {"action": "merge", "source_ids": ["create:architect:pre-mortem", "create:architect:red-team"], "title": "风险综合分析卡"}
  ]
}
```

### 8.3 后端验证规则

在 `studio_card_service.py` 新增 `apply_card_proposals()`：

```python
def apply_card_proposals(
    db: Session,
    skill_id: int,
    *,
    proposals: list[dict[str, Any]],
    user_id: int | None = None,
) -> dict[str, Any]:
    """应用 AI 提出的卡片变更提案。

    验证规则：
    - skip: 只能跳过 status=queued 且不在 completed_card_ids 中的卡
    - add: contract_id 不能与已有卡冲突，phase 必须合法
    - merge: source_ids 必须都存在且 status=queued
    """
```

### 8.4 studio_agent 集成

在 `run_stream()` 解析 AI 回复的 structured output 时：

```python
if "card_proposals" in parsed_output:
    proposals = parsed_output["card_proposals"]
    result = apply_card_proposals(db, selected_skill_id, proposals=proposals, user_id=user_id)
    if result.get("applied"):
        for applied in result["applied"]:
            yield ("card_patch", applied)
```

### 8.5 安全约束

- 每轮最多处理 3 个 proposal
- `add` 操作创建的卡片 priority 不得高于注册表卡片的最大 priority
- `skip` 不能跳过 `kind=confirm` 或 `kind=governance` 的卡
- `merge` 后的新卡继承 source_ids 中最高 priority

**文件**: `studio_card_service.py`, `studio_agent.py`

---

## 9. 涉及文件汇总

| 文件 | 改动类型 | 涉及 B-item |
|------|---------|-------------|
| `backend/app/services/studio_card_resolver.py` | 重构 — 多模式注册表 + `resolve_cards` 路由 | B4 |
| `backend/app/services/studio_card_service.py` | 新增 `check_card_completion_after_edit` / `apply_card_proposals` | B6, B9 |
| `backend/app/services/studio_session_service.py` | `get_studio_session` 去掉 create_new_skill 硬编码 | B4 |
| `backend/app/services/studio_agent.py` | artifact→prompt 注入 / stale 触发 / window 推送 / AI proposal 解析 | B5, B7, B8, B9 |
| `backend/app/services/studio_workflow_protocol.py` | （无结构变更，M3 字段已足够） | — |
| `backend/app/services/studio_patch_bus.py` | 注册 `stale_patch` / `queue_window_patch` | B7, B8 |
| `backend/app/harness/events.py` | 新增 `STALE_PATCH` / `QUEUE_WINDOW_PATCH` | B7, B8 |
| `backend/app/harness/profiles/skill_studio.py` | `_STUDIO_EVENT_MAP` 新增映射 | B7, B8 |
| `backend/app/services/studio_workflow_orchestrator.py` | action 处理后调 `check_card_completion_after_edit` | B6 |

---

## 10. 实施顺序

```
B4 (多模式 CardResolver)
 ↓
B5 (Artifact → Prompt)  ←─ 依赖 B4 的多模式卡片才能测试完整链路
 ↓
B6 (用户动作 → card_status_patch)  ←─ 独立，可并行
 ↓
B7 (Stale 联动)  ←─ 依赖 B4 的注册表 + B6 的事件
 ↓
B8 (Queue Window 推送)  ←─ 依赖 B7 的 stale_patch 触发
 ↓
B9 (AI 动态提案)  ←─ 依赖 B4 的注册表结构
```

推荐执行顺序：B4 → B5 → B6（可与 B5 并行）→ B7 → B8 → B9

---

## 11. 验证方式

### 11.1 单元测试

| 测试文件 | 覆盖 |
|---------|------|
| `test_studio_card_resolver.py` | B4: optimize/audit 模式卡片生成、phase_group 过滤、已完成卡不重复 |
| `test_studio_card_service.py` | B6: `check_card_completion_after_edit` 逻辑、B9: `apply_card_proposals` 验证规则 |
| `test_studio_agent.py` | B5: artifact 注入 prompt、B7: OODA 回调 stale 触发、B8: window 推送 |

### 11.2 集成验证

1. **B4 验证**: `optimize_existing_skill` 模式 → GET /studio/session → 返回 governance/refine/validation 阶段卡片
2. **B5 验证**: Phase 1 完成后 → Phase 2 首卡的 prompt 包含 Phase 1 artifact 摘要
3. **B6 验证**: 采纳全部 staged_edit → 自动 emit `card_status_patch` 标记卡片 accepted
4. **B7 验证**: OODA 回调到 phase_1 → phase_2/phase_3 已完成卡被标 stale + SSE 收到 `stale_patch`
5. **B8 验证**: emit `card_status_patch` 后 → SSE 紧跟 `queue_window_patch` 事件
6. **B9 验证**: AI 回复包含 `card_proposals: [{action: "skip", ...}]` → 卡片被标记跳过 + SSE 收到 `card_patch`

### 11.3 回归

```bash
pytest backend/tests/test_studio_agent.py backend/tests/test_studio_architect.py backend/tests/test_studio_session_protocol.py -x
```

---

## 12. 风险与控制

| 风险 | 控制 |
|------|------|
| Artifact 注入 prompt 过长导致 context 超限 | 4000 token 硬上限 + 截断最早 artifact |
| AI 动态提案被滥用（不断 add 卡片） | 每轮 3 条上限 + priority 天花板 + kind 限制 |
| Queue Window 频繁推送导致前端抖动 | debounce：同一 run 内合并多次 window 变更为一次 push |
| 多模式 CardResolver 增加代码复杂度 | 注册表常量隔离 + 共用 resolve 逻辑 + 充分单测 |
| stale 级联过深导致大量卡片失效 | stale 只标记直接下游，不递归 |
