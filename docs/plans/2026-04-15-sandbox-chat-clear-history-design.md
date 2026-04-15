# 沙盒短期聊天清空与历史测试找回设计

## 背景

沙盒测试工作台默认保持单一入口和单一 active conversation。用户需要在不创建新工作台的前提下，手动清空上一轮测试留下的短期聊天记录，同时保留已沉淀的测试 session、测试报告和 skill memo。

## 设计原则

- 短期聊天记录只对应 `conversations.messages`，可由用户手动清空。
- 长期测试资产对应 `sandbox_test_sessions`、`sandbox_test_reports` 和 `skill_memos`，不参与聊天清空。
- 历史找回通过独立沙盒历史接口完成，不依赖聊天消息是否还存在。
- 清空操作需要二次确认，并明确提示不会删除 session、memo 和报告。

## 后端接口

- 复用现有 `DELETE /api/conversations/{conv_id}/messages` 清空当前 conversation 的短期消息。
- 新增 `GET /api/sandbox/interactive/history` 返回当前用户可见的沙盒历史 session 列表。
- 保留现有 `GET /api/sandbox/interactive/{session_id}` 和 `GET /api/sandbox/interactive/{session_id}/report` 作为详情与报告找回入口。

## 前端交互

- `le-desk` 沙盒工作台展示“清空当前对话”和“历史测试记录”入口。
- `universal-kb` 聊天页在 sandbox workspace 下展示同样入口。
- 历史面板按时间倒序展示 session，展示目标、版本、状态、阶段、报告是否存在。
- 查看 session 或报告不会把旧 chat 回灌到当前聊天区。

## 验收标准

- 如果用户清空当前对话，则当前聊天消息消失，且同一个沙盒工作台仍可继续发起测试。
- 如果用户清空当前对话，则历史 session、测试报告和 memo 不会被删除。
- 如果用户打开历史测试记录，则可以找回之前测试的 session 和已生成报告。
- 如果后端历史列表加载失败，则只影响历史面板，不影响当前沙盒聊天。
