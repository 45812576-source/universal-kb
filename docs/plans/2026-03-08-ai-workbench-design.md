# AI Skill 工作台 — 融合设计文档

> 日期：2026-03-08
> 定位：将 universal-kb 从"知识库系统"升级为"AI Skill 工作台"
> 策略：后端复用 + 前端重写（Next.js 16 替换 React Router）

---

## 一、现状盘点：universal-kb 已有什么

### 后端（Python FastAPI）— 完成度很高，直接复用

| 模块 | 文件 | 状态 | 复用策略 |
|------|------|:----:|---------|
| **Skill 数据模型** | `models/skill.py` | ✅ 完善 | 直接复用。Skill/SkillVersion/SkillSuggestion/SkillAttribution 四表完整 |
| **Skill 执行引擎** | `services/skill_engine.py` | ✅ 完善 | 直接复用。意图匹配→变量提取→知识注入→LLM调用→工具调用 全链路 |
| **Skill 编辑器** | `services/skill_editor.py` | ✅ 完善 | 直接复用。自然语言编辑→diff预览→apply 三步流程 |
| **工具注册+执行** | `models/tool.py` + `services/tool_executor.py` | ✅ 完善 | 直接复用。支持 MCP/Builtin/HTTP 三种工具类型 |
| **MCP 客户端** | `services/mcp_client.py` | ✅ 完善 | 直接复用。支持外部 MCP Server 和 REST 适配 |
| **知识库模型** | `models/knowledge.py` | ✅ 完善 | 直接复用。分级审核(L0-L3)、分类树、向量化、敏感词检测 |
| **知识库服务** | `services/knowledge_service.py` | ✅ 完善 | 直接复用。submit→auto_review→approve 全流程 |
| **向量检索** | `services/vector_service.py` | ✅ 完善 | 直接复用。Milvus + BAAI/bge-m3 |
| **LLM 网关** | `services/llm_gateway.py` | ✅ 完善 | 直接复用。OpenAI-compatible，支持多模型配置 |
| **Text-to-SQL** | `services/data_engine.py` | ✅ 完善 | 直接复用。自然语言→SQL→执行→格式化 |
| **情报采集** | `models/intel.py` + `services/intel_collector.py` | ✅ 完善 | 直接复用。RSS/爬虫/Webhook/手动四种源 |
| **对话模型** | `models/conversation.py` | ✅ 完善 | 直接复用。Conversation + Message |
| **草稿系统** | `models/draft.py` | ✅ 完善 | 直接复用。raw_input→draft→confirm→convert |
| **工作台模型** | `models/workspace.py` | ✅ 完善 | 直接复用。Workspace 绑定 Skill/Tool/DataTable |
| **用户/部门/权限** | `models/user.py` | ✅ 完善 | 直接复用。三级角色 + 部门层级 |
| **飞书集成** | `services/lark_client.py` + `services/lark_bot.py` | ✅ 完善 | 直接复用 |
| **PPT/Excel生成** | `tools/ppt_generator.py` + `tools/excel_generator.py` | ✅ 基础 | 复用+增强（模板系统） |
| **审计日志** | `routers/audit.py` | ✅ 完善 | 直接复用 |
| **上游同步** | `services/upstream_checker.py` | ✅ 完善 | 直接复用。Skill 市场导入+自动更新检查 |

**结论：后端 95% 可直接复用，不需要重写。**

### 前端（React Router 7）— 需要重写

| 问题 | 说明 |
|------|------|
| 框架选择 | React Router 7 (Remix 风格)，与团队技术栈(Next.js)不统一 |
| UI 风格 | Pixel Art 极客风格（像素图标、方块边框），**用户喜欢这个风格** |
| 组件库 | Radix UI 原语 + 手写 Tailwind，无 shadcn |
| 功能完整性 | 页面框架搭好，但前端一直有问题（用户原话） |

**决策：前端用 Next.js 16 重写，保留 Pixel Art 设计风格和配色方案。**

### 基础设施（Docker Compose）— 直接复用

```
MySQL 8.0          — 业务数据库 ✅
Milvus 2.4         — 向量检索   ✅
Redis 7            — 缓存/队列  ✅
（缺）ONLYOFFICE   — 需新增
```

---

## 二、升级目标：从知识库到工作台

### 2.1 产品升级点

| 维度 | 现状（universal-kb） | 目标（AI Workbench） |
|------|---------------------|---------------------|
| 核心体验 | 知识库管理 + 对话 | **对话驱动一切**，知识库是底层能力 |
| Skill | 管理员配置为主 | 员工自助调用 + 管理员运营 |
| 产出 | 文本对话 | 文本 + PPT/Excel/Word **在线编辑** |
| 权限 | Owner 可看全部 | **Skill 黑盒**：调用者只看输入/输出 |
| 工具链 | MCP/Builtin/HTTP | + **本地服务器 MCP** + Claude Code CLI |
| 前端 | React Router（有问题） | Next.js 16（团队技术栈） |

### 2.2 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                     Next.js 16 前端（重写）                       │
│                                                                   │
│  对话工作台 │ Skill商店 │ 知识管理 │ 文件编辑器 │ 管理后台         │
│  (ONLYOFFICE嵌入)                                                │
└──────────────────────────┬────────────────────────────────────────┘
                           │ API Calls
┌──────────────────────────▼────────────────────────────────────────┐
│                   Python FastAPI 后端（复用）                       │
│                                                                    │
│  skill_engine │ tool_executor │ knowledge_service │ llm_gateway    │
│  skill_editor │ mcp_client    │ vector_service    │ data_engine    │
│  intel_collector │ lark_client │ review_policy     │ auth_service   │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │ 新增模块                                                  │     │
│  │ ① file_manager — OSS 文件管理 + ONLYOFFICE 回调          │     │
│  │ ② mcp_gateway  — 本地服务器 MCP 代理                      │     │
│  │ ③ stream_chat   — SSE 流式对话（替代当前同步调用）          │     │
│  │ ④ skill_acl     — Skill 黑盒权限控制                      │     │
│  └──────────────────────────────────────────────────────────┘     │
└──────────────────────────┬────────────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────────────┐
│                        基础设施                                    │
│                                                                    │
│  MySQL 8.0 │ Milvus 2.4 │ Redis 7 │ ONLYOFFICE(新增) │ OSS(新增) │
│                                                                    │
│  ┌────────────────────────────────────────┐                       │
│  │ 本地服务器（你的电脑）via SSH/HTTPS      │                       │
│  │ MCP Server │ Claude Code CLI            │                       │
│  └────────────────────────────────────────┘                       │
└───────────────────────────────────────────────────────────────────┘
```

---

## 三、后端新增模块设计

### 3.1 流式对话（stream_chat）

当前 `skill_engine.execute()` 是同步返回完整响应，用户体验差。需改为 SSE 流式。

**改动范围：**
- `services/skill_engine.py` — 新增 `execute_stream()` 方法
- `routers/conversations.py` — 新增 SSE endpoint `GET /api/conversations/{id}/stream`
- 复用 `llm_gateway.chat_stream()`（已有）

```python
# 新增 endpoint 伪代码
@router.get("/api/conversations/{conv_id}/stream")
async def stream_chat(conv_id: int, content: str):
    async def event_generator():
        async for chunk in skill_engine.execute_stream(db, conversation, content):
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**chunk 类型：**
```json
{"type": "text", "content": "一段文本..."}
{"type": "tool_call", "tool": "generate_pptx", "status": "running"}
{"type": "tool_result", "tool": "generate_pptx", "result": {"file_id": "xxx", "url": "/api/files/xxx"}}
{"type": "knowledge_ref", "refs": [{"title": "...", "score": 0.85}]}
{"type": "done", "message_id": 42}
```

### 3.2 Skill 黑盒权限（skill_acl）

**新增文件：** `services/skill_acl.py`

```python
class SkillACL:
    """Skill 三级权限控制"""

    def can_view_internals(self, user, skill) -> bool:
        """Owner 和 Admin 可以看内部逻辑"""
        if user.role == Role.SUPER_ADMIN:
            return True
        if skill.created_by == user.id:
            return True
        if user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id:
            return True
        return False

    def can_invoke(self, user, skill) -> bool:
        """调用权限检查"""
        if skill.scope == "company":
            return True
        if skill.scope == "department":
            return skill.department_id == user.department_id
        if skill.scope == "personal":
            return skill.created_by == user.id
        return False

    def filter_skill_response(self, user, skill, full_data) -> dict:
        """根据权限过滤返回字段"""
        if self.can_view_internals(user, skill):
            return full_data  # Owner/Admin 看全部
        # 普通调用者只看公开信息
        return {
            "id": full_data["id"],
            "name": full_data["name"],
            "description": full_data["description"],
            "input_schema": full_data.get("variables", []),
            "sample_output": full_data.get("sample_output"),
            "scope": full_data["scope"],
            "department": full_data.get("department"),
        }
```

**改动范围：**
- `routers/skills.py` — GET 接口加权限过滤
- `models/skill.py` — 无需改动（已有 scope/created_by/department_id 字段）

### 3.3 文件管理 + ONLYOFFICE（file_manager）

**新增文件：** `services/file_manager.py`

```python
class FileManager:
    """统一文件管理：本地存储 + ONLYOFFICE 集成"""

    async def save_generated_file(self, file_bytes, filename, creator_id) -> dict:
        """保存 AI 生成的文件"""
        file_id = uuid4().hex
        path = f"generated/{file_id}/{filename}"
        # 写入本地 uploads/ 目录（第一版）
        # 未来迁移 OSS 只需改此处
        ...
        return {"file_id": file_id, "url": f"/api/files/{file_id}", "filename": filename}

    def get_onlyoffice_config(self, file_id, user, mode="edit") -> dict:
        """生成 ONLYOFFICE Document Server 配置"""
        return {
            "document": {
                "fileType": self._get_extension(file_id),
                "key": file_id,
                "title": self._get_filename(file_id),
                "url": f"{BACKEND_URL}/api/files/{file_id}/download",
            },
            "editorConfig": {
                "mode": mode,
                "callbackUrl": f"{BACKEND_URL}/api/files/{file_id}/callback",
                "user": {"id": str(user.id), "name": user.display_name},
                "lang": "zh-CN",
            },
        }

    async def onlyoffice_callback(self, file_id, body) -> dict:
        """ONLYOFFICE 保存回调"""
        status = body.get("status")
        if status == 2:  # 文档已保存
            download_url = body.get("url")
            # 下载编辑后的文件覆盖本地
            ...
        return {"error": 0}
```

**路由新增：**
```
GET  /api/files/{file_id}/download     — 文件下载
GET  /api/files/{file_id}/editor-config — ONLYOFFICE 编辑器配置
POST /api/files/{file_id}/callback     — ONLYOFFICE 保存回调
```

### 3.4 本地服务器 MCP 网关（mcp_gateway）

**新增文件：** `services/mcp_gateway.py`

```python
class McpGateway:
    """代理调用本地服务器上的 MCP Server"""

    async def call_local_mcp(self, tool_name: str, params: dict) -> dict:
        """通过 SSH 或 HTTPS 调用本地服务器的 MCP Server"""
        # 方案 A：本地服务器运行一个 HTTP 代理
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LOCAL_SERVER_URL}/mcp/call",
                json={"tool": tool_name, "params": params},
                timeout=120,
            )
            return resp.json()

    async def request_new_tool(self, description: str) -> dict:
        """请求 Claude Code CLI 生成新工具（需人工审批）"""
        # 发送到本地服务器的审批队列
        ...
```

**本地服务器侧需要部署的：**
```
你的电脑上运行一个轻量 HTTP 服务：
  POST /mcp/call          — 接收工具调用请求，转发给 MCP Server
  POST /mcp/generate      — 接收工具生成请求，调用 Claude Code CLI
  GET  /mcp/tools         — 列出可用工具
```

---

## 四、前端重写设计

### 4.1 技术选型

| 项目 | 选择 | 理由 |
|------|------|------|
| 框架 | Next.js 16 App Router | 团队技术栈统一 |
| UI 组件 | shadcn/ui + Radix UI | 比手写 Tailwind 效率高 |
| 样式 | Tailwind CSS | 复用 |
| 图标 | Pixel Art 自定义组件 | **保留现有设计风格** |
| 字体 | Roboto Mono | 保留（像素风配等宽字体） |
| 文件编辑 | ONLYOFFICE JS SDK | PPT/Excel/Word 在线编辑 |
| 状态管理 | React Server Components + SWR | Next.js 16 原生模式 |
| 认证 | NextAuth | 对接后端 JWT |

### 4.2 设计风格（从 universal-kb 前端提取）

```
配色方案（保留）：
  主色：    #00D1FF (亮青色)
  辅助：    #00A3C4 (深青色)
  强调：    #00CC99 (绿色)
  背景：    #F0F4F8 (浅灰蓝)
  侧边栏：  #EBF4F7 (更浅灰蓝)
  文字：    #1A202C (深灰黑)
  边框：    #1A202C 2px solid (粗边框)

设计语言（保留）：
  - 方块像素图标（PixelIcon 组件复用）
  - 粗 2px 边框
  - 全大写小字号标签（text-[10px] font-bold uppercase tracking-widest）
  - 方形头像/按钮（无圆角或极小圆角）
  - 像素化 hover 效果
```

### 4.3 页面结构

```
ai-workbench/
├── src/app/
│   ├── (auth)/
│   │   └── login/page.tsx
│   │
│   ├── (app)/                          # 需登录
│   │   ├── layout.tsx                  # 侧边栏布局（移植 PixelIcon 设计）
│   │   │
│   │   ├── chat/                       # 对话（核心页面）
│   │   │   ├── page.tsx                # 对话列表 + 新建对话
│   │   │   └── [id]/page.tsx           # 对话详情（流式输出 + 文件卡片 + 草稿面板）
│   │   │
│   │   ├── skills/                     # Skill 商店（员工视角）
│   │   │   ├── page.tsx                # Skill 列表（只看名称/描述/输入要求）
│   │   │   └── [id]/page.tsx           # Skill 详情（黑盒：只看公开信息）
│   │   │
│   │   ├── knowledge/                  # 知识管理
│   │   │   ├── page.tsx                # 我的知识列表
│   │   │   └── new/page.tsx            # 新建/上传知识
│   │   │
│   │   ├── files/                      # 文件管理
│   │   │   ├── page.tsx                # 文件列表（我的文件 + 共享文件）
│   │   │   └── [id]/edit/page.tsx      # ONLYOFFICE 在线编辑
│   │   │
│   │   ├── workspaces/                 # 工作台
│   │   │   └── page.tsx                # 我可用的工作台列表
│   │   │
│   │   ├── tasks/                      # 待办
│   │   │   └── page.tsx
│   │   │
│   │   ├── intel/                      # 情报中心
│   │   │   ├── page.tsx                # 情报流
│   │   │   └── sources/page.tsx        # 数据源管理
│   │   │
│   │   ├── data/                       # 数据表
│   │   │   ├── page.tsx                # 表列表
│   │   │   └── [tableName]/page.tsx    # 表数据查看
│   │   │
│   │   └── admin/                      # 管理后台
│   │       ├── skills/                 # Skill 管理（Owner 视角：完整编辑）
│   │       │   ├── page.tsx
│   │       │   └── [id]/page.tsx       # Skill 详情（prompt/工具链/版本/反馈）
│   │       ├── knowledge/page.tsx      # 知识审核
│   │       ├── models/page.tsx         # 模型配置
│   │       ├── tools/page.tsx          # 工具注册管理
│   │       ├── workspaces/             # 工作台管理
│   │       │   ├── page.tsx
│   │       │   └── [id]/page.tsx
│   │       ├── skill-market/page.tsx   # 外部 Skill 市场
│   │       ├── mcp-tokens/page.tsx     # MCP Token 管理
│   │       ├── intel/page.tsx          # 情报管理
│   │       ├── audit/page.tsx          # 操作审计
│   │       ├── contributions/page.tsx  # 贡献排行
│   │       ├── users/page.tsx          # 用户管理
│   │       └── business-tables/        # 业务表管理
│   │           ├── page.tsx
│   │           └── generate/page.tsx
│   │
│   ├── api/                            # API Routes (代理到后端)
│   │   └── [...proxy]/route.ts         # 统一代理到 FastAPI 后端
│   │
│   └── layout.tsx                      # Root layout
│
├── src/components/
│   ├── ui/                             # shadcn/ui 组件
│   ├── pixel/                          # 像素风格组件
│   │   ├── PixelIcon.tsx               # 移植像素图标
│   │   ├── PixelButton.tsx
│   │   └── PixelBadge.tsx
│   ├── chat/
│   │   ├── MessageBubble.tsx           # 消息气泡（移植+增强）
│   │   ├── StreamingMessage.tsx        # 流式输出组件（新增）
│   │   ├── FileCard.tsx                # 文件卡片（预览/编辑入口）
│   │   ├── ToolCallIndicator.tsx       # 工具调用状态（新增）
│   │   ├── MultimodalInput.tsx         # 输入框（移植）
│   │   └── DraftPanel.tsx              # 草稿面板（移植）
│   ├── editor/
│   │   └── DocumentEditor.tsx          # ONLYOFFICE 嵌入组件（新增）
│   └── skill/
│       ├── SkillCard.tsx               # Skill 卡片（黑盒视角）
│       └── SkillEditor.tsx             # Skill 编辑器（Owner 视角）
│
├── src/lib/
│   ├── api.ts                          # 后端 API 调用封装
│   ├── auth.ts                         # NextAuth 配置
│   ├── types.ts                        # TypeScript 类型
│   └── sse.ts                          # SSE 流式连接工具（新增）
```

### 4.4 核心页面交互设计

#### 对话页（最核心）

```
┌──────────────────────────────────────────────────────────────┐
│ ┌─────┐                                                      │
│ │侧边栏│  ┌──────────────────────┬───────────────────────┐   │
│ │     │  │      对话区 (60%)      │    产出面板 (40%)      │   │
│ │ 对话 │  │                       │                        │   │
│ │ Skill│  │  [KB] 已为您生成3条    │  ┌──────────────────┐ │   │
│ │ 知识 │  │  短视频脚本...         │  │ 📄 脚本.pptx      │ │   │
│ │ 文件 │  │                       │  │ [在线编辑] [下载]  │ │   │
│ │ 数据 │  │  📎 脚本.pptx 已生成   │  │                   │ │   │
│ │ 情报 │  │  📎 数据.xlsx 已生成   │  │  ONLYOFFICE       │ │   │
│ │     │  │                       │  │  编辑器区域         │ │   │
│ │ ─── │  │  🔧 调用 query_crm... │  │                   │ │   │
│ │ 管理 │  │  ✅ 查询完成           │  │                   │ │   │
│ │     │  │                       │  └──────────────────┘ │   │
│ │     │  │                       │                        │   │
│ │     │  ├───────────────────────┤  草稿面板（知识沉淀）    │   │
│ │     │  │ 输入框 + 文件上传      │  [📚入库] [📋创任务]   │   │
│ └─────┘  └──────────────────────┴───────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

右侧面板根据 AI 输出**动态切换**：
- 生成了文件 → 显示 ONLYOFFICE 编辑器
- 纯文本回答 → 显示草稿面板（知识沉淀/创建任务）
- 查询了数据 → 显示数据表格（可导出 Excel）

#### Skill 商店页（员工视角）

```
┌─────────────────────────────────────────────────┐
│  🔍 搜索 Skill...        [我的] [部门] [全公司]  │
├─────────────────────────────────────────────────┤
│                                                   │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐          │
│  │ ⚡ S31   │  │ ⚡ S33   │  │ ⚡ S37   │          │
│  │ KPI拆解  │  │ 内容策略 │  │ 视频脚本 │          │
│  │          │  │          │  │          │          │
│  │ 输入:    │  │ 输入:    │  │ 输入:    │          │
│  │ GMV目标  │  │ 品类     │  │ 选题     │          │
│  │ 当前数据 │  │ 产品卖点 │  │ 产品信息 │          │
│  │          │  │          │  │          │          │
│  │ [调用]   │  │ [调用]   │  │ [调用]   │          │
│  └─────────┘  └─────────┘  └─────────┘          │
│                                                   │
│  看不到：prompt内容、工具链配置、知识库绑定         │
└─────────────────────────────────────────────────┘
```

---

## 五、数据库变更

### 5.1 现有表（无需修改）

所有 universal-kb 的表保持不变：
- `users`, `departments`
- `skills`, `skill_versions`, `skill_suggestions`, `skill_attributions`
- `knowledge_entries`, `knowledge_revisions`
- `conversations`, `messages`
- `tool_registry`, `skill_tools`
- `workspaces`, `workspace_skills`, `workspace_tools`, `workspace_data_tables`
- `model_configs`
- `business_tables`, `data_ownership`, `audit_logs`
- `intel_sources`, `intel_entries`
- `raw_inputs`, `input_extractions`, `drafts`, `learning_samples`
- `mcp_sources`, `mcp_tokens`

### 5.2 新增表

```sql
-- 文件管理表
CREATE TABLE files (
    id INT PRIMARY KEY AUTO_INCREMENT,
    file_id VARCHAR(64) UNIQUE NOT NULL,          -- UUID
    filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20) NOT NULL,               -- pptx / xlsx / docx / pdf
    file_size BIGINT DEFAULT 0,
    storage_path VARCHAR(1000) NOT NULL,           -- 本地路径或 OSS key
    storage_type VARCHAR(20) DEFAULT 'local',      -- local / oss
    source_type VARCHAR(50) DEFAULT 'generated',   -- generated / uploaded / edited
    source_message_id INT NULL,                    -- 关联的消息 ID
    source_skill_id INT NULL,                      -- 关联的 Skill ID
    created_by INT NOT NULL,
    department_id INT NULL,
    visibility VARCHAR(20) DEFAULT 'personal',     -- personal / department / company
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id),
    FOREIGN KEY (source_message_id) REFERENCES messages(id),
    FOREIGN KEY (source_skill_id) REFERENCES skills(id)
);

-- MCP 网关配置（本地服务器连接）
CREATE TABLE mcp_gateways (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(200) NOT NULL,
    endpoint_url VARCHAR(500) NOT NULL,            -- 本地服务器地址
    auth_type VARCHAR(20) DEFAULT 'token',         -- token / ssh_key
    auth_credential VARCHAR(500),                   -- 加密存储
    is_active BOOLEAN DEFAULT TRUE,
    last_health_check DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 六、Docker Compose 变更

```yaml
# 在现有 docker-compose.yml 基础上新增：

  onlyoffice:
    image: onlyoffice/documentserver:latest
    ports:
      - "8080:80"
    environment:
      JWT_ENABLED: "true"
      JWT_SECRET: "your-onlyoffice-jwt-secret"
    volumes:
      - onlyoffice_data:/var/www/onlyoffice/Data

volumes:
  # 现有的保留
  mysql_data:
  etcd_data:
  minio_data:
  milvus_data:
  redis_data:
  # 新增
  onlyoffice_data:
```

---

## 七、分期实施路线图

### Phase 1：基础框架（1 周）

**目标：** Next.js 前端跑通，对接后端，核心对话能用。

- [ ] Next.js 16 项目初始化（在 universal-kb/ 下新建 `web/` 目录）
- [ ] 移植 Pixel Art 设计系统（配色/PixelIcon/布局）
- [ ] NextAuth 对接后端 JWT 认证
- [ ] API 代理层（Next.js API Routes → FastAPI）
- [ ] 侧边栏布局（移植 layout.tsx 设计）
- [ ] 对话列表 + 新建对话页
- [ ] 对话详情页（基础版：同步消息，无流式）
- [ ] 后端新增 `skill_acl.py`，Skill API 加权限过滤

### Phase 2：流式对话 + 文件生成（1 周）

**目标：** 对话体验达到生产可用水平。

- [ ] 后端 `stream_chat` SSE 端点
- [ ] 前端 `StreamingMessage` 组件
- [ ] 前端 `ToolCallIndicator` 组件（显示工具调用过程）
- [ ] 前端 `FileCard` 组件（文件预览入口）
- [ ] Docker 新增 ONLYOFFICE Document Server
- [ ] 后端 `file_manager.py` + 文件管理路由
- [ ] 前端 `DocumentEditor` 组件（ONLYOFFICE 嵌入）
- [ ] 前端文件在线编辑页 `files/[id]/edit`

### Phase 3：Skill 商店 + 知识管理（1 周）

**目标：** 员工可以浏览和调用 Skill，管理知识。

- [ ] Skill 商店页面（黑盒视角：名称/描述/输入要求/调用）
- [ ] Skill 管理页面（Owner 视角：完整编辑/版本/反馈）
- [ ] 知识管理页面（我的知识/新建/上传）
- [ ] 知识审核页面（管理员）
- [ ] 草稿面板移植（DraftPanel）
- [ ] 消息→知识沉淀（📚入库按钮）

### Phase 4：管理后台 + 工具管理（1 周）

**目标：** 管理能力完整。

- [ ] 模型配置页
- [ ] 工具注册管理页（MCP/Builtin/HTTP）
- [ ] 工作台管理页（Workspace 配置）
- [ ] 业务表管理页
- [ ] 外部 Skill 市场页
- [ ] MCP Token 管理页
- [ ] 用户管理页
- [ ] 操作审计页
- [ ] 贡献排行页

### Phase 5：本地 MCP 网关 + 高级功能（1 周）

**目标：** 打通本地服务器，完成最后一公里。

- [ ] 后端 `mcp_gateway.py`
- [ ] 本地服务器 HTTP 代理服务（轻量 Flask/FastAPI）
- [ ] Claude Code CLI 工具生成（需审批流）
- [ ] 情报中心前端
- [ ] 数据表前端
- [ ] 待办/任务前端
- [ ] 飞书/企微推送集成测试

---

## 八、项目目录结构（最终）

```
universal-kb/
├── backend/                    # Python FastAPI（现有，复用）
│   ├── app/
│   │   ├── models/             # 数据模型（现有 + files 新增）
│   │   ├── routers/            # API 路由（现有 + stream/files 新增）
│   │   ├── services/           # 服务层（现有 + 4个新增模块）
│   │   ├── tools/              # 内置工具（现有）
│   │   ├── data/               # 知识分类树（现有）
│   │   └── utils/              # 工具函数（现有）
│   ├── tests/
│   └── requirements.txt
│
├── web/                        # Next.js 16 前端（全新）
│   ├── src/
│   │   ├── app/                # App Router 页面
│   │   ├── components/         # 组件
│   │   └── lib/                # 工具函数
│   ├── public/
│   ├── package.json
│   └── next.config.ts
│
├── docker-compose.yml          # 基础设施（现有 + ONLYOFFICE）
└── docs/
    └── plans/                  # 设计文档
```

---

## 九、风险与决策记录

| 风险 | 缓解方案 |
|------|---------|
| ONLYOFFICE 资源占用大 | 社区版单实例够用；先评估服务器配置 |
| 流式 + 工具调用复杂度 | 先实现文本流式，工具调用第二步再加 |
| 本地 MCP 网关安全性 | 仅内网访问 + Token 认证；第一版不暴露公网 |
| 前端工作量大 | Phase 1-3 优先，Phase 4-5 可并行或延后 |
| React Router → Next.js 迁移 | 后端 API 零改动，只是前端消费方式变了 |

---

*本文档为 AI Skill 工作台融合设计 V1。核心策略：后端几乎零改动复用 universal-kb，前端用 Next.js 16 重写，新增 4 个后端模块（流式/权限/文件/MCP网关）。*
