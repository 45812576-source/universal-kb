# 低摩擦 Input 系统 — 技术设计文档

基于《低摩擦Input系统实施文档》，本文档定义 Phase 1 MVP 的完整技术方案，覆盖数据模型、API、前端页面、AI Prompt、事件触发。

---

## 1. 范围界定

### Phase 1 MVP 做什么
1. **Chat 委托台改造** — 从聊天泡泡升级为「左对话 + 右成果卡」双栏布局
2. **多模态输入条** — 文本/粘贴/拖拽文件/截图/语音/URL，统一 Drop Zone
3. **Knowledge Draft 链路** — raw_input → extraction → draft → knowledge_entry
4. **Opportunity Draft 链路** — raw_input → extraction → draft → opportunity
5. **待确认流页面** — 集中处理低置信度字段

### Phase 1 不做
- 自动采集微信/飞书消息
- Feedback / CandidateEvaluation 链路（Phase 2）
- learning_samples 闭环训练（Phase 3）
- 旧页面 `/knowledge/new` `/suggestions/new` 的删除（保留兼容）

---

## 2. 数据模型

### 2.1 新增表

所有新表在同一个 Alembic migration `f6a7b8c9d0e1_low_friction_input.py` 中创建。

#### `raw_inputs` — 原始输入层

```python
class RawInputSourceType(str, enum.Enum):
    TEXT = "text"
    VOICE = "voice"
    FILE = "file"
    IMAGE = "image"
    URL = "url"
    PASTE = "paste"          # 粘贴聊天/邮件
    MULTI = "multi"          # 混合输入

class RawInputStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"

class RawInput(Base):
    __tablename__ = "raw_inputs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id    = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_type     = Column(Enum(RawInputSourceType), nullable=False)
    source_channel  = Column(String(50), default="web")  # web / lark / api
    raw_text        = Column(Text, nullable=True)         # 原始文本/语音转写
    attachment_urls = Column(JSON, default=list)           # 文件/图片路径列表
    context_json    = Column(JSON, default=dict)           # 额外上下文（来源URL等）
    status          = Column(Enum(RawInputStatus), default=RawInputStatus.RECEIVED)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
```

#### `input_extractions` — AI 抽取层

```python
class DetectedObjectType(str, enum.Enum):
    KNOWLEDGE = "knowledge"
    OPPORTUNITY = "opportunity"
    FEEDBACK = "feedback"
    CANDIDATE_EVAL = "candidate_evaluation"
    SKILL_SUGGESTION = "skill_suggestion"
    TASK_UPDATE = "task_update"
    UNKNOWN = "unknown"

class InputExtraction(Base):
    __tablename__ = "input_extractions"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id          = Column(Integer, ForeignKey("raw_inputs.id"), nullable=False, unique=True)
    detected_intent       = Column(String(100))                        # "沉淀知识" / "记录商机" / ...
    detected_object_type  = Column(Enum(DetectedObjectType), nullable=False)
    summary               = Column(Text)                               # 一句话摘要
    entities_json         = Column(JSON, default=dict)                 # 识别出的实体 {客户名, 产品, 人名...}
    fields_json           = Column(JSON, default=dict)                 # 抽取的结构化字段
    confidence_json       = Column(JSON, default=dict)                 # 每个字段的置信度 0-1
    uncertain_fields      = Column(JSON, default=list)                 # 需要用户确认的字段列表
    extractor_version     = Column(String(50), default="v1")
    created_at            = Column(DateTime, default=datetime.datetime.utcnow)
```

#### `drafts` — 草稿层（成果物卡片的数据源）

```python
class DraftStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"
    CONVERTED = "converted"         # 已转为正式对象

class Draft(Base):
    __tablename__ = "drafts"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    object_type           = Column(Enum(DetectedObjectType), nullable=False)
    source_raw_input_id   = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    source_extraction_id  = Column(Integer, ForeignKey("input_extractions.id"), nullable=True)
    conversation_id       = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id         = Column(Integer, ForeignKey("users.id"), nullable=False)

    title                 = Column(String(200))
    summary               = Column(Text)
    fields_json           = Column(JSON, default=dict)       # 所有字段
    tags_json             = Column(JSON, default=dict)       # {industry: [], platform: [], topic: []}
    pending_questions     = Column(JSON, default=list)        # [{field, question, options?, type}]
    confirmed_fields      = Column(JSON, default=dict)       # 用户确认过的字段
    user_corrections      = Column(JSON, default=list)       # [{field, ai_value, user_value, ts}]
    suggested_actions     = Column(JSON, default=list)        # ["保存知识", "加入跟进", ...]

    status                = Column(Enum(DraftStatus), default=DraftStatus.DRAFT)
    formal_object_id      = Column(Integer, nullable=True)    # 转为正式对象后的 ID
    created_at            = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at            = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
```

#### `opportunities` — 商机正式对象（新表）

```python
class OpportunityStage(str, enum.Enum):
    LEAD = "lead"                # 线索
    CONTACT = "contact"          # 已接触
    NEEDS_ANALYSIS = "needs"     # 探需中
    PROPOSAL = "proposal"        # 提案中
    NEGOTIATION = "negotiation"  # 谈判中
    WON = "won"
    LOST = "lost"

class Opportunity(Base):
    __tablename__ = "opportunities"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    title             = Column(String(200), nullable=False)
    customer_name     = Column(String(200))
    industry          = Column(String(100))
    stage             = Column(Enum(OpportunityStage), default=OpportunityStage.LEAD)
    needs_summary     = Column(Text)
    key_contacts      = Column(JSON, default=list)   # [{name, role, is_decision_maker}]
    risk_points       = Column(JSON, default=list)
    next_actions      = Column(JSON, default=list)
    priority          = Column(String(20), default="normal")  # high / normal / low
    source_draft_id   = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    created_by_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id     = Column(Integer, ForeignKey("departments.id"), nullable=True)
    created_at        = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
```

#### `learning_samples` — 学习样本层

```python
class LearningSample(Base):
    __tablename__ = "learning_samples"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id        = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    draft_id            = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    object_type         = Column(String(50), nullable=False)
    task_type           = Column(String(50))              # "object_detection" / "field_extraction" / "tag_suggestion"
    model_output_json   = Column(JSON, default=dict)      # AI 原始输出
    user_correction_json = Column(JSON, default=dict)     # 用户修正
    final_answer_json   = Column(JSON, default=dict)      # 最终确认值
    created_by_id       = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at          = Column(DateTime, default=datetime.datetime.utcnow)
```

### 2.2 现有表改动

**`messages` 表** — 新增 `draft_id` 列，将 assistant 消息与生成的草稿关联：

```python
# 新增列
draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
```

**`knowledge_entries` 表** — 新增 `source_draft_id` 列：

```python
source_draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
```

---

## 3. AI 处理流水线

### 3.1 统一处理流程

```
用户输入 (多模态)
    │
    ▼
┌─────────────────────┐
│  1. Normalize        │  文件→extract_text / 图片→OCR/描述 / 语音→ASR / URL→fetch
│     → raw_text       │
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│  2. Detect & Extract │  一次 LLM 调用，同时完成：
│     → object_type    │  - 识别对象类型 (knowledge/opportunity/feedback/...)
│     → fields_json    │  - 抽取结构化字段
│     → confidence     │  - 标记低置信字段
│     → summary        │  - 生成一句话摘要
│     → pending_qs     │  - 生成待确认问题
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│  3. Build Draft      │  组装草稿卡 + 建议动作列表
│     → Draft object   │
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│  4. Return to UI     │  左侧：摘要 + "已为你生成XX草稿"
│     → Card + Actions │  右侧：结构化卡片 + 待确认问题 + 一键动作
└─────────────────────┘
```

### 3.2 核心 Prompt：Detect & Extract

```python
DETECT_AND_EXTRACT_PROMPT = """你是企业知识管理系统的 AI 助手。用户将输入原始工作素材（聊天记录、会议纪要、客户沟通、经验总结等），你需要：

1. **识别对象类型** — 这段内容最适合沉淀为哪种业务对象？
   可选类型: knowledge / opportunity / feedback / candidate_evaluation / skill_suggestion / unknown

2. **抽取结构化字段** — 根据识别的类型抽取对应字段（见下方 schema）

3. **评估置信度** — 对每个抽取字段给出 0-1 的置信度

4. **生成待确认问题** — 对置信度 < 0.7 的字段生成简洁的确认问题（给选项优先）

5. **生成一句话摘要**

6. **建议后续动作** — 2-4 个用户可能想执行的动作

## 各类型字段 Schema

### knowledge
- title: 标题
- content_summary: 内容摘要（200字以内）
- knowledge_type: experience / methodology / case_study / data / template / external
- industry_tags: []
- platform_tags: []
- topic_tags: []
- applicable_departments: []
- visibility: all / department

### opportunity
- title: 商机标题
- customer_name: 客户名称
- industry: 行业
- stage: lead / contact / needs / proposal / negotiation
- needs_summary: 核心需求
- key_contacts: [{name, role, is_decision_maker}]
- risk_points: []
- next_actions: []
- priority: high / normal / low

### feedback
- title: 反馈标题
- customer_name: 客户名称
- feedback_type: bug / feature_request / config_issue / training_issue / churn_risk
- severity: critical / high / medium / low
- description: 问题描述
- affects_renewal: true / false
- suggested_team: 建议流转团队

---

用户原始输入:
{raw_text}

请严格返回以下 JSON 格式，不要返回其他内容:
```json
{
  "object_type": "knowledge",
  "intent": "沉淀经验知识",
  "summary": "...",
  "fields": { ... },
  "confidence": { "title": 0.9, "knowledge_type": 0.6, ... },
  "pending_questions": [
    {"field": "knowledge_type", "question": "这条内容更像什么类型？", "options": ["经验总结", "方法论", "案例"], "type": "single_choice"},
    {"field": "visibility", "question": "谁可以看到这条知识？", "options": ["全员可见", "仅本部门"], "type": "single_choice"}
  ],
  "suggested_actions": ["保存为知识草稿", "继续补充", "分享给团队"]
}
```"""
```

### 3.3 内容标准化 — Normalizer

新建 `services/input_normalizer.py`：

```python
class InputNormalizer:
    """将多模态输入统一转为 raw_text + attachment_urls"""

    async def normalize(self, raw_input: RawInput, db: Session) -> str:
        """返回标准化后的文本，同时更新 raw_input.raw_text"""
        parts = []

        # 1. 直接文本
        if raw_input.raw_text:
            parts.append(raw_input.raw_text)

        # 2. 附件处理
        for attachment_url in (raw_input.attachment_urls or []):
            ext = Path(attachment_url).suffix.lower()
            if ext in ('.pdf', '.docx', '.pptx', '.md', '.txt'):
                text = extract_text(attachment_url)  # 复用现有 file_parser
                parts.append(f"[文件: {Path(attachment_url).name}]\n{text}")
            elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
                desc = await self._describe_image(attachment_url)
                parts.append(f"[图片描述]\n{desc}")
            elif ext in ('.mp3', '.wav', '.m4a'):
                transcript = await self._transcribe_audio(attachment_url)
                parts.append(f"[语音转写]\n{transcript}")

        # 3. URL 抓取
        if url := (raw_input.context_json or {}).get("url"):
            page_text = await self._fetch_url(url)
            parts.append(f"[网页内容: {url}]\n{page_text}")

        combined = "\n\n---\n\n".join(parts)
        raw_input.raw_text = combined
        return combined

    async def _describe_image(self, path: str) -> str:
        """调用 LLM 视觉能力描述图片内容"""
        # 使用 DeepSeek-VL 或 Qwen-VL
        ...

    async def _transcribe_audio(self, path: str) -> str:
        """调用 ASR 服务转写语音"""
        # 使用 FunASR 或 Whisper API
        ...

    async def _fetch_url(self, url: str) -> str:
        """抓取网页内容，转为纯文本"""
        ...
```

### 3.4 处理编排 — InputProcessor

新建 `services/input_processor.py`：

```python
class InputProcessor:
    """统一处理流水线：normalize → detect & extract → build draft"""

    def __init__(self):
        self.normalizer = InputNormalizer()

    async def process(self, raw_input_id: int, db: Session) -> Draft:
        raw_input = db.get(RawInput, raw_input_id)
        raw_input.status = RawInputStatus.PROCESSING
        db.flush()

        # Step 1: Normalize
        normalized_text = await self.normalizer.normalize(raw_input, db)

        # Step 2: Detect & Extract (单次 LLM 调用)
        model_config = llm_gateway.get_config(db)
        result_json = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": DETECT_AND_EXTRACT_PROMPT.format(raw_text=normalized_text)}],
            temperature=0.1,
            max_tokens=2000,
        )
        parsed = json.loads(result_json)

        # Step 3: Save extraction
        extraction = InputExtraction(
            raw_input_id=raw_input.id,
            detected_intent=parsed.get("intent"),
            detected_object_type=parsed.get("object_type", "unknown"),
            summary=parsed.get("summary"),
            fields_json=parsed.get("fields", {}),
            confidence_json=parsed.get("confidence", {}),
            uncertain_fields=[q["field"] for q in parsed.get("pending_questions", [])],
        )
        db.add(extraction)
        db.flush()

        # Step 4: Build draft
        draft = Draft(
            object_type=parsed.get("object_type", "unknown"),
            source_raw_input_id=raw_input.id,
            source_extraction_id=extraction.id,
            conversation_id=raw_input.conversation_id,
            created_by_id=raw_input.created_by_id,
            title=parsed.get("fields", {}).get("title", parsed.get("summary", "")[:60]),
            summary=parsed.get("summary"),
            fields_json=parsed.get("fields", {}),
            tags_json={
                "industry": parsed.get("fields", {}).get("industry_tags", []),
                "platform": parsed.get("fields", {}).get("platform_tags", []),
                "topic": parsed.get("fields", {}).get("topic_tags", []),
            },
            pending_questions=parsed.get("pending_questions", []),
            suggested_actions=parsed.get("suggested_actions", []),
            status=DraftStatus.WAITING_CONFIRMATION,
        )
        db.add(draft)

        raw_input.status = RawInputStatus.EXTRACTED
        db.commit()
        db.refresh(draft)
        return draft

input_processor = InputProcessor()
```

---

## 4. API 设计

### 4.1 新增 API 端点

所有新端点放在 `routers/drafts.py`：

```
POST   /api/raw-inputs              — 创建原始输入（支持 multipart/form-data）
GET    /api/raw-inputs/:id          — 查看原始输入详情

GET    /api/drafts                  — 列出我的草稿（可过滤 status/object_type）
GET    /api/drafts/:id              — 查看草稿详情
PATCH  /api/drafts/:id/confirm      — 确认字段 + 回答待确认问题
POST   /api/drafts/:id/convert      — 确认并转为正式对象
POST   /api/drafts/:id/discard      — 丢弃草稿

GET    /api/drafts/pending-questions — 待确认流（聚合所有 waiting_confirmation 草稿的 pending_questions）
POST   /api/drafts/:id/answer       — 回答单个待确认问题
```

### 4.2 核心 API 详细设计

#### `POST /api/raw-inputs`

接收多模态输入，触发异步处理。

```python
@router.post("/api/raw-inputs")
async def create_raw_input(
    text: str = Form(None),
    files: list[UploadFile] = File(None),
    url: str = Form(None),
    source_type: str = Form("text"),
    workspace_id: int = Form(None),
    conversation_id: int = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 1. 保存附件到 uploads/
    attachment_urls = []
    if files:
        for f in files:
            path = save_upload(f)
            attachment_urls.append(path)

    # 2. 创建 raw_input
    raw_input = RawInput(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        created_by_id=user.id,
        source_type=source_type,
        raw_text=text,
        attachment_urls=attachment_urls,
        context_json={"url": url} if url else {},
    )
    db.add(raw_input)
    db.flush()

    # 3. 同步处理（MVP 先同步，后续改 Celery 异步）
    draft = await input_processor.process(raw_input.id, db)

    return {
        "raw_input_id": raw_input.id,
        "draft": draft_to_dict(draft),
    }
```

#### `PATCH /api/drafts/:id/confirm`

用户确认/修正字段：

```python
class ConfirmRequest(BaseModel):
    confirmed_fields: dict = {}         # {field_name: confirmed_value}
    corrections: dict = {}              # {field_name: corrected_value}

@router.patch("/api/drafts/{draft_id}/confirm")
def confirm_draft_fields(
    draft_id: int,
    req: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)

    # 合并确认
    existing_confirmed = draft.confirmed_fields or {}
    existing_confirmed.update(req.confirmed_fields)
    draft.confirmed_fields = existing_confirmed

    # 记录纠错到 user_corrections + learning_samples
    if req.corrections:
        corrections = draft.user_corrections or []
        for field, new_val in req.corrections.items():
            ai_val = draft.fields_json.get(field)
            corrections.append({
                "field": field,
                "ai_value": ai_val,
                "user_value": new_val,
                "ts": datetime.utcnow().isoformat(),
            })
            # 更新 fields_json
            draft.fields_json[field] = new_val

            # 写 learning_sample
            sample = LearningSample(
                draft_id=draft.id,
                raw_input_id=draft.source_raw_input_id,
                object_type=draft.object_type.value,
                task_type="field_correction",
                model_output_json={"field": field, "value": ai_val},
                user_correction_json={"field": field, "value": new_val},
                final_answer_json={"field": field, "value": new_val},
                created_by_id=user.id,
            )
            db.add(sample)
        draft.user_corrections = corrections

    # 移除已确认的 pending_questions
    all_answered = set(req.confirmed_fields.keys()) | set(req.corrections.keys())
    draft.pending_questions = [
        q for q in (draft.pending_questions or [])
        if q["field"] not in all_answered
    ]

    if not draft.pending_questions:
        draft.status = DraftStatus.CONFIRMED

    db.commit()
    return draft_to_dict(draft)
```

#### `POST /api/drafts/:id/convert`

确认并转为正式对象：

```python
@router.post("/api/drafts/{draft_id}/convert")
def convert_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)
    fields = {**draft.fields_json, **draft.confirmed_fields}

    if draft.object_type == DetectedObjectType.KNOWLEDGE:
        entry = KnowledgeEntry(
            title=draft.title or fields.get("title", ""),
            content=fields.get("content_summary", draft.summary or ""),
            category=fields.get("knowledge_type", "experience"),
            industry_tags=fields.get("industry_tags", []),
            platform_tags=fields.get("platform_tags", []),
            topic_tags=fields.get("topic_tags", []),
            created_by=user.id,
            department_id=user.department_id,
            source_type="ai_draft",
            source_draft_id=draft.id,
        )
        db.add(entry)
        db.flush()
        draft.formal_object_id = entry.id

    elif draft.object_type == DetectedObjectType.OPPORTUNITY:
        opp = Opportunity(
            title=draft.title or fields.get("title", ""),
            customer_name=fields.get("customer_name"),
            industry=fields.get("industry"),
            stage=fields.get("stage", "lead"),
            needs_summary=fields.get("needs_summary"),
            key_contacts=fields.get("key_contacts", []),
            risk_points=fields.get("risk_points", []),
            next_actions=fields.get("next_actions", []),
            priority=fields.get("priority", "normal"),
            source_draft_id=draft.id,
            created_by_id=user.id,
            department_id=user.department_id,
        )
        db.add(opp)
        db.flush()
        draft.formal_object_id = opp.id

    draft.status = DraftStatus.CONVERTED
    db.commit()

    # 触发后续事件（见第6节）
    trigger_post_conversion(draft, db)

    return {"draft_id": draft.id, "formal_object_id": draft.formal_object_id, "object_type": draft.object_type.value}
```

### 4.3 Chat 消息集成

改造现有 `POST /api/conversations/:id/messages`，在 skill_engine 之前插入低摩擦处理分支：

```python
@router.post("/{conv_id}/messages")
async def send_message(conv_id, req, db, user):
    conv = db.get(Conversation, conv_id)

    # 保存 user message
    user_msg = Message(conversation_id=conv_id, role=MessageRole.USER, content=req.content)
    db.add(user_msg)
    db.flush()

    # 判断是否走低摩擦链路（有附件 or 内容长度 > 100 or 系统判断是素材输入）
    if req.attachments or should_use_draft_flow(req.content):
        raw_input = RawInput(
            conversation_id=conv_id,
            created_by_id=user.id,
            source_type=detect_source_type(req),
            raw_text=req.content,
            attachment_urls=req.attachments or [],
        )
        db.add(raw_input)
        db.flush()

        draft = await input_processor.process(raw_input.id, db)

        # 生成助手消息，关联 draft
        assistant_msg = Message(
            conversation_id=conv_id,
            role=MessageRole.ASSISTANT,
            content=f"已为你生成{draft.object_type.value}草稿「{draft.title}」，请在右侧卡片中确认。",
            draft_id=draft.id,
            metadata_={"draft_id": draft.id, "object_type": draft.object_type.value},
        )
        db.add(assistant_msg)
        db.commit()
        return message_with_draft(assistant_msg, draft)

    # 否则走原有 skill_engine 链路
    response = await skill_engine.execute(db, conv, req.content, user_id=user.id)
    ...
```

---

## 5. 前端设计

### 5.1 Chat 委托台改造 — `conversation.tsx`

从单栏泡泡 → 双栏布局：

```
┌──────────────────────────────────────────────────────────────────┐
│ [Workspace]                                              [User] │
├─────────────────────────────────┬────────────────────────────────┤
│                                 │                                │
│  对话区（左侧 60%）              │  成果物卡片区（右侧 40%）         │
│                                 │                                │
│  User: 粘贴了一段客户聊天...      │  ┌────────────────────────┐    │
│                                 │  │ 🏢 商机草稿             │    │
│  KB: 已为你生成商机草稿           │  │                        │    │
│      「XX客户投放需求」           │  │ 客户: XX广告             │    │
│      请在右侧确认。              │  │ 行业: 快消  [✓]          │    │
│                                 │  │ 阶段: 探需中 [▼]         │    │
│                                 │  │ 需求: ...               │    │
│                                 │  │ 联系人: 张总(拍板者?)     │    │
│                                 │  │                        │    │
│                                 │  │ ⚠ 待确认:               │    │
│                                 │  │ · 这位更像拍板者还是      │    │
│                                 │  │   影响者? [拍板] [影响]   │    │
│                                 │  │                        │    │
│                                 │  │ [保存商机] [加入跟进]     │    │
│                                 │  │ [生成提案] [丢弃]        │    │
│                                 │  └────────────────────────┘    │
│                                 │                                │
├─────────────────────────────────┴────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ 📎 🎤  输入消息或拖入文件...                        [发送 →] │ │
│ └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

#### 关键前端组件

```
components/
├── chat/
│   ├── ChatLayout.tsx          # 双栏布局容器
│   ├── MessageList.tsx         # 左侧对话列表（复用现有 MessageBubble）
│   ├── DraftPanel.tsx          # 右侧成果物卡片区
│   ├── DraftCard.tsx           # 单个草稿卡片（字段展示 + 确认 + 动作）
│   ├── PendingQuestion.tsx     # 待确认问题组件（选项按钮/输入）
│   ├── MultimodalInput.tsx     # 底部多模态输入条
│   └── QuickActions.tsx        # 快捷委托按钮（首屏）
```

### 5.2 MultimodalInput 输入条

替换现有的 textarea 输入，支持：

```typescript
interface MultimodalInputProps {
  onSubmit: (data: {
    text?: string;
    files?: File[];
    audioBlob?: Blob;
    url?: string;
  }) => void;
  isLoading: boolean;
}
```

**交互规则：**
- 整个输入条是 drop target（`onDrop` 接收文件）
- `Ctrl+V` 检测：文本→插入文本 / 图片→添加附件预览 / URL→显示链接卡片
- 录音按钮：点击开始录音 → 再点击停止 → 显示波形预览 → 自动追加
- 附件缩略图显示在输入框上方，可 ×删除
- `Ctrl+Enter` 或点击发送

### 5.3 DraftCard 草稿卡片

核心交互：

```typescript
interface DraftCardProps {
  draft: Draft;
  onConfirmField: (field: string, value: any) => void;
  onCorrectField: (field: string, newValue: any) => void;
  onAnswerQuestion: (field: string, answer: any) => void;
  onConvert: () => void;
  onDiscard: () => void;
}
```

**卡片结构：**
1. 头部：对象类型图标 + 标题 + 状态徽章
2. 字段区：每个字段一行，高置信度显示 ✓，低置信度显示可编辑
3. 待确认问题区：每个问题一行，选项按钮或短输入框
4. 动作栏：suggested_actions 渲染为按钮

**字段可编辑性：**
- 置信度 ≥ 0.8：显示值 + 小✓，点击可改
- 置信度 0.5-0.8：显示值 + 黄色边框，突出可编辑
- 置信度 < 0.5：显示在待确认问题区

### 5.4 首屏改造

空对话页不再显示"发送第一条消息"，而是显示：

```
┌─────────────────────────────────────────┐
│                                         │
│    今天想让系统帮你处理什么？              │
│                                         │
│    [📝 沉淀一条经验]  [🏢 记录一个商机]   │
│    [💬 处理客户反馈]  [📋 自由对话]       │
│                                         │
│    ── 最近草稿 ──                        │
│    • XX客户商机 (待确认)  [继续]          │
│    • 618投放经验 (已确认) [查看]          │
│                                         │
│    ── 待确认 (3) ──                      │
│    • 这位联系人更像? [拍板者] [影响者]     │
│    • 这条经验是? [SOP] [案例]            │
│                                         │
└─────────────────────────────────────────┘
```

### 5.5 待确认流页面 — 新路由 `/confirmations`

独立页面，信息流式展示所有 pending_questions：

```
┌─────────────────────────────────────────┐
│ 待确认事项 (12)                          │
├─────────────────────────────────────────┤
│                                         │
│ ┌─────────────────────────────────────┐ │
│ │ 📝 "618大促投放ROI方法论"            │ │
│ │                                     │ │
│ │ 这条内容更像什么类型？                │ │
│ │ [经验总结] [方法论] [案例]            │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ ┌─────────────────────────────────────┐ │
│ │ 🏢 "XX客户投放需求"                  │ │
│ │                                     │ │
│ │ 张总更像拍板者还是影响者？            │ │
│ │ [拍板者] [影响者] [不确定]            │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ ┌─────────────────────────────────────┐ │
│ │ 💬 "客户报告数据异常"                 │ │
│ │                                     │ │
│ │ 这更像bug还是配置问题？              │ │
│ │ [Bug] [配置问题] [培训问题]           │ │
│ └─────────────────────────────────────┘ │
│                                         │
└─────────────────────────────────────────┘
```

每条 3-5 秒可完成确认，类似 Tinder 式刷卡体验。

---

## 6. 事件触发机制

### 6.1 Knowledge 转换后触发

```python
def on_knowledge_converted(entry: KnowledgeEntry, db: Session):
    # 1. 进入 Milvus 向量索引
    index_to_milvus(entry)

    # 2. 挂接相关 workspace/skill
    auto_link_knowledge(entry, db)

    # 3. 推荐给相关角色（可选，Phase 2）
    # notify_relevant_users(entry, db)
```

### 6.2 Opportunity 转换后触发

```python
def on_opportunity_converted(opp: Opportunity, db: Session):
    # 1. 创建首条跟进任务（可选，Phase 2）
    # create_follow_up_task(opp, db)

    # 2. 高优先级商机通知（可选）
    # if opp.priority == "high": notify_manager(opp, db)
    pass
```

MVP 阶段事件触发保持最简，只做向量索引和知识挂接。

---

## 7. 文件结构变更

### 后端新增文件

```
backend/app/
├── models/
│   ├── raw_input.py          # RawInput, RawInputSourceType, RawInputStatus
│   ├── extraction.py         # InputExtraction, DetectedObjectType
│   ├── draft.py              # Draft, DraftStatus
│   ├── opportunity.py        # Opportunity, OpportunityStage
│   └── learning_sample.py    # LearningSample
├── routers/
│   └── drafts.py             # raw-inputs + drafts API
├── services/
│   ├── input_normalizer.py   # 多模态输入标准化
│   └── input_processor.py    # 统一处理流水线
└── alembic/versions/
    └── f6a7b8c9d0e1_low_friction_input.py
```

### 前端新增文件

```
frontend/app/
├── components/
│   └── chat/
│       ├── ChatLayout.tsx
│       ├── DraftPanel.tsx
│       ├── DraftCard.tsx
│       ├── PendingQuestion.tsx
│       ├── MultimodalInput.tsx
│       └── QuickActions.tsx
├── routes/app/
│   └── confirmations/
│       └── index.tsx           # 待确认流页面
└── lib/
    └── draft-api.ts            # Draft 相关 API 调用封装
```

### 现有文件修改

```
backend/app/
├── models/knowledge.py     # +source_draft_id 列
├── models/conversation.py  # Message +draft_id 列
├── routers/conversations.py # send_message 插入低摩擦分支
└── main.py                 # 注册 drafts router

frontend/app/
├── routes/app/chat/conversation.tsx  # 重构为双栏布局
├── routes/app/chat/index.tsx         # 首屏改造
└── routes.ts                          # 新增 /confirmations 路由
```

---

## 8. 实施顺序

### Step 1: 数据层（后端）
1. 创建 5 个 model 文件
2. 写 Alembic migration
3. 跑 migration

### Step 2: AI 处理层（后端）
1. `input_normalizer.py` — 先支持 text + file，图片/语音/URL 后补
2. `input_processor.py` — 核心 prompt + 流水线
3. 手动测试 prompt 效果

### Step 3: API 层（后端）
1. `drafts.py` — raw-inputs + drafts CRUD
2. 改造 `conversations.py` — 插入低摩擦分支
3. Postman/curl 测通

### Step 4: 前端组件（前端）
1. `MultimodalInput.tsx` — 替换现有 textarea
2. `DraftCard.tsx` + `PendingQuestion.tsx` — 草稿卡片
3. `DraftPanel.tsx` — 右侧面板
4. `ChatLayout.tsx` — 双栏容器

### Step 5: 页面集成（前端）
1. `conversation.tsx` — 改为双栏
2. `chat/index.tsx` — 首屏改造
3. `/confirmations` — 待确认流页面

### Step 6: 联调 + 验证
1. Knowledge 全链路：输入 → 草稿 → 确认 → knowledge_entry
2. Opportunity 全链路
3. 修正 prompt，调整置信度阈值

---

## 9. MVP 验收标准

1. **用户可以粘贴一段文字/拖入文件到输入条，3 秒内看到结构化草稿卡片**
2. **草稿卡片展示 AI 抽取的字段，高置信度字段自动确认，低置信度字段有选项可点**
3. **用户点击"保存"，草稿转为正式 knowledge_entry 或 opportunity**
4. **用户纠错会被记录到 learning_samples**
5. **待确认流页面可以刷卡式快速处理所有 pending questions**
6. **旧的 `/knowledge/new` 页面保留可用，不强制迁移**
