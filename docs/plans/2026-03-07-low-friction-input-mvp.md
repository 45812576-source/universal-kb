# 低摩擦 Input 系统 MVP 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建完整的低摩擦输入链路：用户扔素材进来 → AI 自动生成结构化草稿卡 → 用户极简确认 → 转为正式对象（knowledge / opportunity / feedback）

**Architecture:** 四层中间层（raw_inputs → input_extractions → drafts → formal objects）+ Chat 委托台（双栏布局：左对话 + 右成果卡）。现有 conversations 接口扩展支持多模态输入和草稿生成，新增 drafts router 处理整个草稿生命周期。

**Tech Stack:** Python FastAPI + SQLAlchemy + MySQL（后端），React Router v7 + TypeScript + Tailwind（前端），DeepSeek via LLMGateway（AI 抽取），pytest + TestClient（测试）

---

## Task 1: 数据模型 — raw_inputs / input_extractions / drafts

**Files:**
- Create: `backend/app/models/raw_input.py`
- Create: `backend/app/models/draft.py`
- Modify: `backend/app/models/knowledge.py` — 新增 `source_draft_id`, `raw_input_id`, `capture_mode` 列
- Modify: `backend/app/models/conversation.py` — Message 新增 `draft_id` 列
- Create: `backend/alembic/versions/f6a7b8c9d0e1_low_friction_input.py`
- Test: `backend/tests/test_low_friction_models.py`

**Step 1: 写 raw_input.py**

```python
# backend/app/models/raw_input.py
import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base


class RawInputSourceType(str, enum.Enum):
    TEXT = "text"
    VOICE = "voice"
    FILE = "file"
    IMAGE = "image"
    URL = "url"
    PASTE = "paste"
    MULTI = "multi"


class RawInputStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"


class DetectedObjectType(str, enum.Enum):
    KNOWLEDGE = "knowledge"
    OPPORTUNITY = "opportunity"
    FEEDBACK = "feedback"
    UNKNOWN = "unknown"


class RawInput(Base):
    __tablename__ = "raw_inputs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id    = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_type     = Column(Enum(RawInputSourceType), nullable=False, default=RawInputSourceType.TEXT)
    source_channel  = Column(String(50), default="web")
    raw_text        = Column(Text, nullable=True)
    attachment_urls = Column(JSON, default=list)
    context_json    = Column(JSON, default=dict)
    status          = Column(Enum(RawInputStatus), default=RawInputStatus.RECEIVED)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class InputExtraction(Base):
    __tablename__ = "input_extractions"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id         = Column(Integer, ForeignKey("raw_inputs.id"), nullable=False, unique=True)
    detected_intent      = Column(String(200), nullable=True)
    detected_object_type = Column(Enum(DetectedObjectType), nullable=False, default=DetectedObjectType.UNKNOWN)
    summary              = Column(Text, nullable=True)
    entities_json        = Column(JSON, default=dict)
    fields_json          = Column(JSON, default=dict)
    confidence_json      = Column(JSON, default=dict)
    uncertain_fields     = Column(JSON, default=list)
    extractor_version    = Column(String(50), default="v1")
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
```

**Step 2: 写 draft.py**

```python
# backend/app/models/draft.py
import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base
from app.models.raw_input import DetectedObjectType


class DraftStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"
    CONVERTED = "converted"


class Draft(Base):
    __tablename__ = "drafts"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    object_type          = Column(Enum(DetectedObjectType), nullable=False)
    source_raw_input_id  = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    source_extraction_id = Column(Integer, ForeignKey("input_extractions.id"), nullable=True)
    conversation_id      = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_by_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    title                = Column(String(200), nullable=True)
    summary              = Column(Text, nullable=True)
    fields_json          = Column(JSON, default=dict)
    tags_json            = Column(JSON, default=dict)
    pending_questions    = Column(JSON, default=list)
    confirmed_fields     = Column(JSON, default=dict)
    user_corrections     = Column(JSON, default=list)
    suggested_actions    = Column(JSON, default=list)
    status               = Column(Enum(DraftStatus), default=DraftStatus.WAITING_CONFIRMATION)
    formal_object_id     = Column(Integer, nullable=True)
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at           = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class LearningSample(Base):
    __tablename__ = "learning_samples"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    raw_input_id         = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
    draft_id             = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    object_type          = Column(String(50), nullable=False)
    task_type            = Column(String(50), nullable=True)
    model_output_json    = Column(JSON, default=dict)
    user_correction_json = Column(JSON, default=dict)
    final_answer_json    = Column(JSON, default=dict)
    created_by_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at           = Column(DateTime, default=datetime.datetime.utcnow)
```

**Step 3: 修改 knowledge.py — 追加列**

在 `KnowledgeEntry` 类的 `milvus_ids` 列之后追加：

```python
# 在 milvus_ids 之后加
source_draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
raw_input_id    = Column(Integer, ForeignKey("raw_inputs.id"), nullable=True)
capture_mode    = Column(String(50), default="manual_form")  # manual_form / chat_delegate / voice_delegate
```

**Step 4: 修改 conversation.py — Message 追加 draft_id**

在 `metadata_` 列之后追加：

```python
draft_id = Column(Integer, ForeignKey("drafts.id"), nullable=True)
```

**Step 5: 新增 opportunities 和 feedback_items 模型**

```python
# backend/app/models/opportunity.py
import datetime
import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from app.database import Base


class OpportunityStage(str, enum.Enum):
    LEAD = "lead"
    CONTACT = "contact"
    NEEDS = "needs"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"


class OpportunityStatus(str, enum.Enum):
    ACTIVE = "active"
    WON = "won"
    LOST = "lost"
    ON_HOLD = "on_hold"


class Opportunity(Base):
    __tablename__ = "opportunities"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    title            = Column(String(200), nullable=False)
    customer_name    = Column(String(200), nullable=True)
    industry         = Column(String(100), nullable=True)
    stage            = Column(Enum(OpportunityStage), default=OpportunityStage.LEAD)
    priority         = Column(String(20), default="normal")
    needs_summary    = Column(Text, nullable=True)
    decision_map     = Column(JSON, default=list)
    risk_points      = Column(JSON, default=list)
    next_actions     = Column(JSON, default=list)
    source_draft_id  = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    created_by_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id    = Column(Integer, ForeignKey("departments.id"), nullable=True)
    status           = Column(Enum(OpportunityStatus), default=OpportunityStatus.ACTIVE)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# backend/app/models/feedback_item.py
class FeedbackType(str, enum.Enum):
    BUG = "bug"
    FEATURE_REQUEST = "feature_request"
    CONFIG_ISSUE = "config_issue"
    TRAINING_ISSUE = "training_issue"
    CHURN_RISK = "churn_risk"


class FeedbackItem(Base):
    __tablename__ = "feedback_items"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    title             = Column(String(200), nullable=True)
    customer_name     = Column(String(200), nullable=True)
    feedback_type     = Column(Enum(FeedbackType), nullable=True)
    severity          = Column(String(20), default="medium")  # critical/high/medium/low
    description       = Column(Text, nullable=True)
    affected_module   = Column(String(100), nullable=True)
    renewal_risk_level = Column(String(20), default="low")
    routed_team       = Column(String(100), nullable=True)
    knowledgeworthy   = Column(Integer, default=0)  # 0=False, 1=True (SQLite compat)
    source_draft_id   = Column(Integer, ForeignKey("drafts.id"), nullable=True)
    created_by_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    status            = Column(String(20), default="open")  # open/routed/closed
    created_at        = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
```

**Step 6: 写 Alembic migration**

```python
# backend/alembic/versions/f6a7b8c9d0e1_low_friction_input.py
"""low friction input tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('raw_inputs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(50), nullable=False, server_default='text'),
        sa.Column('source_channel', sa.String(50), server_default='web'),
        sa.Column('raw_text', sa.Text(), nullable=True),
        sa.Column('attachment_urls', sa.JSON(), nullable=True),
        sa.Column('context_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(50), server_default='received'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('input_extractions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('raw_input_id', sa.Integer(), nullable=False),
        sa.Column('detected_intent', sa.String(200), nullable=True),
        sa.Column('detected_object_type', sa.String(50), server_default='unknown'),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('entities_json', sa.JSON(), nullable=True),
        sa.Column('fields_json', sa.JSON(), nullable=True),
        sa.Column('confidence_json', sa.JSON(), nullable=True),
        sa.Column('uncertain_fields', sa.JSON(), nullable=True),
        sa.Column('extractor_version', sa.String(50), server_default='v1'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['raw_input_id'], ['raw_inputs.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('raw_input_id'),
    )
    op.create_table('drafts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('object_type', sa.String(50), nullable=False),
        sa.Column('source_raw_input_id', sa.Integer(), nullable=True),
        sa.Column('source_extraction_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('fields_json', sa.JSON(), nullable=True),
        sa.Column('tags_json', sa.JSON(), nullable=True),
        sa.Column('pending_questions', sa.JSON(), nullable=True),
        sa.Column('confirmed_fields', sa.JSON(), nullable=True),
        sa.Column('user_corrections', sa.JSON(), nullable=True),
        sa.Column('suggested_actions', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(50), server_default='waiting_confirmation'),
        sa.Column('formal_object_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_raw_input_id'], ['raw_inputs.id']),
        sa.ForeignKeyConstraint(['source_extraction_id'], ['input_extractions.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('learning_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('raw_input_id', sa.Integer(), nullable=True),
        sa.Column('draft_id', sa.Integer(), nullable=True),
        sa.Column('object_type', sa.String(50), nullable=False),
        sa.Column('task_type', sa.String(50), nullable=True),
        sa.Column('model_output_json', sa.JSON(), nullable=True),
        sa.Column('user_correction_json', sa.JSON(), nullable=True),
        sa.Column('final_answer_json', sa.JSON(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['raw_input_id'], ['raw_inputs.id']),
        sa.ForeignKeyConstraint(['draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('opportunities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('customer_name', sa.String(200), nullable=True),
        sa.Column('industry', sa.String(100), nullable=True),
        sa.Column('stage', sa.String(50), server_default='lead'),
        sa.Column('priority', sa.String(20), server_default='normal'),
        sa.Column('needs_summary', sa.Text(), nullable=True),
        sa.Column('decision_map', sa.JSON(), nullable=True),
        sa.Column('risk_points', sa.JSON(), nullable=True),
        sa.Column('next_actions', sa.JSON(), nullable=True),
        sa.Column('source_draft_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(20), server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('feedback_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(200), nullable=True),
        sa.Column('customer_name', sa.String(200), nullable=True),
        sa.Column('feedback_type', sa.String(50), nullable=True),
        sa.Column('severity', sa.String(20), server_default='medium'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('affected_module', sa.String(100), nullable=True),
        sa.Column('renewal_risk_level', sa.String(20), server_default='low'),
        sa.Column('routed_team', sa.String(100), nullable=True),
        sa.Column('knowledgeworthy', sa.Integer(), server_default='0'),
        sa.Column('source_draft_id', sa.Integer(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), server_default='open'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_draft_id'], ['drafts.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # Extend existing tables
    op.add_column('knowledge_entries', sa.Column('source_draft_id', sa.Integer(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('raw_input_id', sa.Integer(), nullable=True))
    op.add_column('knowledge_entries', sa.Column('capture_mode', sa.String(50), server_default='manual_form'))
    op.add_column('messages', sa.Column('draft_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('messages', 'draft_id')
    op.drop_column('knowledge_entries', 'capture_mode')
    op.drop_column('knowledge_entries', 'raw_input_id')
    op.drop_column('knowledge_entries', 'source_draft_id')
    op.drop_table('feedback_items')
    op.drop_table('opportunities')
    op.drop_table('learning_samples')
    op.drop_table('drafts')
    op.drop_table('input_extractions')
    op.drop_table('raw_inputs')
```

**Step 7: 写模型测试（仅验证表结构可以在 SQLite 下建成）**

```python
# backend/tests/test_low_friction_models.py
"""TC-LOWFRICTION-MODELS: Verify new tables are created and basic CRUD works."""
from app.models.raw_input import RawInput, InputExtraction, RawInputSourceType, RawInputStatus, DetectedObjectType
from app.models.draft import Draft, DraftStatus, LearningSample
from app.models.opportunity import Opportunity
from app.models.feedback_item import FeedbackItem
from tests.conftest import _make_user, _make_dept


def test_raw_input_create(db):
    dept = _make_dept(db)
    user = _make_user(db, "ri_user", dept_id=dept.id)
    db.commit()

    ri = RawInput(
        created_by_id=user.id,
        source_type=RawInputSourceType.TEXT,
        raw_text="测试原始输入",
    )
    db.add(ri)
    db.commit()
    db.refresh(ri)

    assert ri.id is not None
    assert ri.status == RawInputStatus.RECEIVED
    assert ri.raw_text == "测试原始输入"


def test_extraction_create(db):
    dept = _make_dept(db)
    user = _make_user(db, "ex_user", dept_id=dept.id)
    db.commit()

    ri = RawInput(created_by_id=user.id, source_type=RawInputSourceType.TEXT, raw_text="测试")
    db.add(ri)
    db.flush()

    ext = InputExtraction(
        raw_input_id=ri.id,
        detected_object_type=DetectedObjectType.KNOWLEDGE,
        summary="这是一条经验总结",
        fields_json={"title": "测试标题"},
        confidence_json={"title": 0.9},
    )
    db.add(ext)
    db.commit()
    db.refresh(ext)

    assert ext.id is not None
    assert ext.detected_object_type == DetectedObjectType.KNOWLEDGE


def test_draft_create(db):
    dept = _make_dept(db)
    user = _make_user(db, "dr_user", dept_id=dept.id)
    db.commit()

    draft = Draft(
        object_type=DetectedObjectType.KNOWLEDGE,
        created_by_id=user.id,
        title="测试草稿",
        summary="这是摘要",
        fields_json={"title": "测试草稿", "knowledge_type": "experience"},
        pending_questions=[{"field": "visibility", "question": "谁可以看？", "options": ["全员", "部门"], "type": "single_choice"}],
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)

    assert draft.id is not None
    assert draft.status == DraftStatus.WAITING_CONFIRMATION
    assert len(draft.pending_questions) == 1


def test_opportunity_create(db):
    dept = _make_dept(db)
    user = _make_user(db, "opp_user", dept_id=dept.id)
    db.commit()

    opp = Opportunity(
        title="XX客户商机",
        customer_name="XX公司",
        industry="快消",
        created_by_id=user.id,
    )
    db.add(opp)
    db.commit()
    db.refresh(opp)

    assert opp.id is not None
    assert opp.stage == "lead"


def test_feedback_item_create(db):
    dept = _make_dept(db)
    user = _make_user(db, "fb_user", dept_id=dept.id)
    db.commit()

    fb = FeedbackItem(
        title="数据报错问题",
        customer_name="YY客户",
        feedback_type="bug",
        created_by_id=user.id,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    assert fb.id is not None
    assert fb.status == "open"
```

**Step 8: 把新模型注册进 main.py imports（让 Base.metadata 包含这些表）**

在 `backend/app/main.py` 顶部 imports 区域追加（在现有 from app.routers 之前）：

```python
# 注册新模型到 Base.metadata
from app.models import raw_input, draft, opportunity, feedback_item  # noqa: F401
```

**Step 9: 运行测试验证**

```bash
cd backend
python -m pytest tests/test_low_friction_models.py -v
```

期望：5个测试全部 PASS

**Step 10: Commit**

```bash
git add backend/app/models/raw_input.py backend/app/models/draft.py \
        backend/app/models/opportunity.py backend/app/models/feedback_item.py \
        backend/app/models/knowledge.py backend/app/models/conversation.py \
        backend/app/main.py backend/tests/test_low_friction_models.py \
        backend/alembic/versions/f6a7b8c9d0e1_low_friction_input.py
git commit -m "feat: add low-friction input data models (raw_inputs, extractions, drafts, opportunities, feedback_items)"
```

---

## Task 2: AI 处理服务 — InputProcessor

**Files:**
- Create: `backend/app/services/input_processor.py`
- Test: `backend/tests/test_input_processor.py`

**Step 1: 写 input_processor.py**

这个服务负责：原始输入标准化 → 单次 LLM 抽取 → 保存 extraction → 生成 draft

```python
# backend/app/services/input_processor.py
"""统一输入处理流水线: normalize → detect&extract → save extraction → build draft"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.draft import Draft, DraftStatus, LearningSample
from app.models.raw_input import (
    DetectedObjectType, InputExtraction,
    RawInput, RawInputStatus,
)
from app.services.llm_gateway import llm_gateway
from app.utils.file_parser import extract_text

logger = logging.getLogger(__name__)

# ── Prompt ─────────────────────────────────────────────────────────────────────

DETECT_AND_EXTRACT_PROMPT = """你是企业知识管理系统的 AI 助手，帮助员工将工作素材自动结构化。

用户将输入原始工作素材（聊天记录、会议纪要、客户沟通、经验总结、客户反馈等）。

你需要完成以下任务：

## 1. 识别对象类型
判断这段内容最适合沉淀为哪种业务对象：
- knowledge：经验总结、方法论、案例、SOP、FAQ、模板、外部资料
- opportunity：销售商机、客户需求、业务拓展线索
- feedback：客户反馈、问题报告、投诉、需求建议
- unknown：无法判断

## 2. 抽取结构化字段

### knowledge 类型字段：
- title: 标题（20字以内）
- content_summary: 内容摘要（100-200字）
- knowledge_type: experience / methodology / case_study / data / template / external
- industry_tags: 行业标签数组，如 ["食品", "美妆"]
- platform_tags: 平台标签数组，如 ["抖音", "小红书"]
- topic_tags: 主题标签数组，如 ["ROI优化", "投放策略"]
- visibility: all（全员）/ department（仅本部门）

### opportunity 类型字段：
- title: 商机标题（20字以内）
- customer_name: 客户名称
- industry: 行业
- stage: lead / contact / needs / proposal / negotiation
- needs_summary: 核心需求摘要（100字以内）
- decision_map: 决策角色列表，每项含 {name, role, is_decision_maker}
- risk_points: 风险点数组
- next_actions: 下一步建议数组
- priority: high / normal / low

### feedback 类型字段：
- title: 反馈标题（20字以内）
- customer_name: 客户名称
- feedback_type: bug / feature_request / config_issue / training_issue / churn_risk
- severity: critical / high / medium / low
- description: 问题描述（100字以内）
- affected_module: 影响模块
- renewal_risk_level: high / medium / low
- routed_team: 建议流转团队（如"产品组"/"技术组"/"客成组"）
- knowledgeworthy: true / false（是否值得沉淀为FAQ）

## 3. 评估置信度
对每个字段给出 0.0-1.0 的置信度。

## 4. 生成待确认问题
对置信度 < 0.7 的**关键字段**生成确认问题（最多3个），要求：
- 只问对后续动作最关键的字段
- 必须提供可点选选项
- 问题简洁，3-5秒可答完

## 5. 生成一句话摘要（30字以内）

## 6. 建议后续动作（2-4个）

---

用户原始输入:
{raw_text}

严格返回以下 JSON，不要返回其他任何内容:
{{
  "object_type": "knowledge|opportunity|feedback|unknown",
  "intent": "意图描述",
  "summary": "一句话摘要（30字以内）",
  "fields": {{ ... }},
  "confidence": {{ "field_name": 0.9, ... }},
  "pending_questions": [
    {{
      "field": "field_name",
      "question": "问题",
      "options": ["选项1", "选项2"],
      "type": "single_choice"
    }}
  ],
  "suggested_actions": ["动作1", "动作2"]
}}"""


def _normalize_text(raw_input: RawInput) -> str:
    """将多模态输入标准化为纯文本。MVP 阶段只支持 text + file。"""
    parts = []

    if raw_input.raw_text:
        parts.append(raw_input.raw_text)

    for url in (raw_input.attachment_urls or []):
        try:
            ext = Path(url).suffix.lower()
            if ext in ('.txt', '.pdf', '.docx', '.pptx', '.md'):
                text = extract_text(url)
                parts.append(f"[文件内容: {Path(url).name}]\n{text[:3000]}")
            # 图片/语音/URL 留给 Phase 2 扩展
        except Exception as e:
            logger.warning(f"Failed to extract file {url}: {e}")

    return "\n\n---\n\n".join(parts) if parts else ""


def _parse_object_type(raw: str) -> DetectedObjectType:
    mapping = {
        "knowledge": DetectedObjectType.KNOWLEDGE,
        "opportunity": DetectedObjectType.OPPORTUNITY,
        "feedback": DetectedObjectType.FEEDBACK,
    }
    return mapping.get(raw.lower(), DetectedObjectType.UNKNOWN)


async def process_raw_input(raw_input_id: int, db: Session) -> Draft:
    """主入口：处理一个 raw_input，返回生成的 Draft。"""
    raw_input = db.get(RawInput, raw_input_id)
    if not raw_input:
        raise ValueError(f"RawInput {raw_input_id} not found")

    raw_input.status = RawInputStatus.PROCESSING
    db.flush()

    # Step 1: normalize
    normalized = _normalize_text(raw_input)
    if normalized:
        raw_input.raw_text = normalized

    if not normalized.strip():
        raw_input.status = RawInputStatus.FAILED
        db.commit()
        raise ValueError("Empty content after normalization")

    # Step 2: LLM detect & extract
    try:
        model_config = llm_gateway.get_config(db)
        result_str = await llm_gateway.chat(
            model_config=model_config,
            messages=[{
                "role": "user",
                "content": DETECT_AND_EXTRACT_PROMPT.format(raw_text=normalized[:4000]),
            }],
            temperature=0.1,
            max_tokens=2000,
        )
        # 清理可能的 markdown 代码块
        result_str = result_str.strip()
        if result_str.startswith("```"):
            result_str = result_str.split("```")[1]
            if result_str.startswith("json"):
                result_str = result_str[4:]
        parsed = json.loads(result_str.strip())
    except Exception as e:
        logger.error(f"LLM extraction failed for raw_input {raw_input_id}: {e}")
        raw_input.status = RawInputStatus.FAILED
        db.commit()
        raise

    # Step 3: save extraction
    object_type = _parse_object_type(parsed.get("object_type", "unknown"))
    extraction = InputExtraction(
        raw_input_id=raw_input.id,
        detected_intent=parsed.get("intent", ""),
        detected_object_type=object_type,
        summary=parsed.get("summary", ""),
        fields_json=parsed.get("fields", {}),
        confidence_json=parsed.get("confidence", {}),
        uncertain_fields=[q["field"] for q in parsed.get("pending_questions", [])],
    )
    db.add(extraction)
    db.flush()

    # Step 4: build draft
    fields = parsed.get("fields", {})
    title = (
        fields.get("title")
        or parsed.get("summary", "")[:60]
        or "未命名草稿"
    )
    tags_json = {
        "industry": fields.get("industry_tags", []),
        "platform": fields.get("platform_tags", []),
        "topic": fields.get("topic_tags", []),
    }

    draft = Draft(
        object_type=object_type,
        source_raw_input_id=raw_input.id,
        source_extraction_id=extraction.id,
        conversation_id=raw_input.conversation_id,
        created_by_id=raw_input.created_by_id,
        title=title,
        summary=parsed.get("summary", ""),
        fields_json=fields,
        tags_json=tags_json,
        pending_questions=parsed.get("pending_questions", []),
        suggested_actions=parsed.get("suggested_actions", []),
        status=DraftStatus.WAITING_CONFIRMATION,
    )
    db.add(draft)

    raw_input.status = RawInputStatus.EXTRACTED
    db.commit()
    db.refresh(draft)
    return draft
```

**Step 2: 写测试（mock LLM，不实际调用）**

```python
# backend/tests/test_input_processor.py
"""TC-INPUTPROCESSOR: Test the AI extraction pipeline with mocked LLM."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.models.raw_input import RawInput, RawInputSourceType, RawInputStatus, DetectedObjectType
from app.models.draft import DraftStatus
from app.services.input_processor import process_raw_input
from tests.conftest import _make_user, _make_dept, _make_model_config


KNOWLEDGE_LLM_RESPONSE = json.dumps({
    "object_type": "knowledge",
    "intent": "沉淀投放经验",
    "summary": "618大促ROI提升方法论",
    "fields": {
        "title": "618大促ROI提升方法论",
        "content_summary": "通过分时竞价和创意轮播提升ROI",
        "knowledge_type": "methodology",
        "industry_tags": ["电商"],
        "platform_tags": ["天猫"],
        "topic_tags": ["ROI优化"],
        "visibility": "all",
    },
    "confidence": {
        "title": 0.95,
        "knowledge_type": 0.6,
        "visibility": 0.5,
    },
    "pending_questions": [
        {
            "field": "visibility",
            "question": "这条知识谁可以看到？",
            "options": ["全员可见", "仅本部门"],
            "type": "single_choice",
        }
    ],
    "suggested_actions": ["保存为知识草稿", "继续补充"],
})


OPPORTUNITY_LLM_RESPONSE = json.dumps({
    "object_type": "opportunity",
    "intent": "记录客户商机",
    "summary": "XX客户有投放需求，预算100万",
    "fields": {
        "title": "XX客户Q2投放商机",
        "customer_name": "XX广告有限公司",
        "industry": "快消",
        "stage": "needs",
        "needs_summary": "希望在抖音做品牌投放，Q2预算100万",
        "decision_map": [{"name": "张总", "role": "市场总监", "is_decision_maker": True}],
        "risk_points": ["决策周期长"],
        "next_actions": ["发送案例集"],
        "priority": "high",
    },
    "confidence": {
        "title": 0.9,
        "stage": 0.65,
        "priority": 0.7,
    },
    "pending_questions": [
        {
            "field": "stage",
            "question": "当前商机处于哪个阶段？",
            "options": ["初步接触", "探需中", "提案阶段"],
            "type": "single_choice",
        }
    ],
    "suggested_actions": ["保存商机", "生成提案大纲"],
})


@pytest.fixture
def user_with_model(db):
    dept = _make_dept(db)
    user = _make_user(db, "proc_user", dept_id=dept.id)
    mc = _make_model_config(db)
    db.commit()
    return user, mc


@pytest.mark.asyncio
async def test_process_knowledge_raw_input(db, user_with_model):
    user, mc = user_with_model

    ri = RawInput(
        created_by_id=user.id,
        source_type=RawInputSourceType.TEXT,
        raw_text="我在618期间通过分时竞价和创意轮播，把ROI从2提升到了3.5，主要是...",
    )
    db.add(ri)
    db.flush()

    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        draft = await process_raw_input(ri.id, db)

    assert draft.object_type == DetectedObjectType.KNOWLEDGE
    assert draft.title == "618大促ROI提升方法论"
    assert draft.status == DraftStatus.WAITING_CONFIRMATION
    assert len(draft.pending_questions) == 1
    assert draft.pending_questions[0]["field"] == "visibility"

    # raw_input should be marked extracted
    db.refresh(ri)
    assert ri.status == RawInputStatus.EXTRACTED

    # extraction saved
    from app.models.raw_input import InputExtraction
    ext = db.query(InputExtraction).filter_by(raw_input_id=ri.id).first()
    assert ext is not None
    assert ext.detected_object_type == DetectedObjectType.KNOWLEDGE


@pytest.mark.asyncio
async def test_process_opportunity_raw_input(db, user_with_model):
    user, mc = user_with_model

    ri = RawInput(
        created_by_id=user.id,
        source_type=RawInputSourceType.PASTE,
        raw_text="客户张总说他们Q2想在抖音投放，预算大概100万，让我们发案例集...",
    )
    db.add(ri)
    db.flush()

    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=OPPORTUNITY_LLM_RESPONSE),
    ):
        draft = await process_raw_input(ri.id, db)

    assert draft.object_type == DetectedObjectType.OPPORTUNITY
    assert draft.title == "XX客户Q2投放商机"
    assert len(draft.pending_questions) == 1


@pytest.mark.asyncio
async def test_process_empty_input_fails(db, user_with_model):
    user, mc = user_with_model

    ri = RawInput(
        created_by_id=user.id,
        source_type=RawInputSourceType.TEXT,
        raw_text="",
    )
    db.add(ri)
    db.flush()

    with pytest.raises(ValueError, match="Empty content"):
        await process_raw_input(ri.id, db)

    db.refresh(ri)
    assert ri.status == RawInputStatus.FAILED


@pytest.mark.asyncio
async def test_process_handles_llm_failure(db, user_with_model):
    user, mc = user_with_model

    ri = RawInput(
        created_by_id=user.id,
        source_type=RawInputSourceType.TEXT,
        raw_text="一些内容",
    )
    db.add(ri)
    db.flush()

    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(side_effect=Exception("LLM timeout")),
    ):
        with pytest.raises(Exception, match="LLM timeout"):
            await process_raw_input(ri.id, db)

    db.refresh(ri)
    assert ri.status == RawInputStatus.FAILED
```

**Step 3: 安装 pytest-asyncio（如果没有）**

```bash
cd backend
pip install pytest-asyncio
```

在 `backend/pyproject.toml` 或 `backend/pytest.ini` 中添加（如果没有这些文件就在根目录建 `pytest.ini`）：

```ini
# backend/pytest.ini
[pytest]
asyncio_mode = auto
```

**Step 4: 运行测试**

```bash
cd backend
python -m pytest tests/test_input_processor.py -v
```

期望：4个测试全部 PASS

**Step 5: Commit**

```bash
git add backend/app/services/input_processor.py backend/tests/test_input_processor.py backend/pytest.ini
git commit -m "feat: add InputProcessor AI pipeline with LLM extraction and draft generation"
```

---

## Task 3: Drafts API

**Files:**
- Create: `backend/app/routers/drafts.py`
- Modify: `backend/app/main.py` — 注册 drafts router
- Test: `backend/tests/test_drafts.py`

**Step 1: 写 drafts.py**

```python
# backend/app/routers/drafts.py
"""Low-friction input API: raw_inputs + drafts lifecycle."""
import datetime
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.draft import Draft, DraftStatus, LearningSample
from app.models.feedback_item import FeedbackItem
from app.models.knowledge import KnowledgeEntry
from app.models.opportunity import Opportunity
from app.models.raw_input import DetectedObjectType, RawInput, RawInputSourceType
from app.models.user import User
from app.services.input_processor import process_raw_input

router = APIRouter(prefix="/api", tags=["drafts"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _draft_dict(d: Draft) -> dict:
    return {
        "id": d.id,
        "object_type": d.object_type.value if d.object_type else "unknown",
        "title": d.title,
        "summary": d.summary,
        "fields": d.fields_json or {},
        "tags": d.tags_json or {},
        "pending_questions": d.pending_questions or [],
        "confirmed_fields": d.confirmed_fields or {},
        "suggested_actions": d.suggested_actions or [],
        "status": d.status.value if d.status else "draft",
        "formal_object_id": d.formal_object_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _save_upload(file: UploadFile) -> str:
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "file")[1]
    path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    content = file.file.read()
    with open(path, "wb") as f:
        f.write(content)
    return path


# ── raw-inputs ────────────────────────────────────────────────────────────────

@router.post("/raw-inputs")
async def create_raw_input(
    text: Optional[str] = Form(None),
    source_type: str = Form("text"),
    source_channel: str = Form("web"),
    workspace_id: Optional[int] = Form(None),
    conversation_id: Optional[int] = Form(None),
    url: Optional[str] = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接收多模态原始输入，触发 AI 处理，返回生成的 draft。"""
    attachment_urls = []
    for f in files:
        if f.filename:
            path = _save_upload(f)
            attachment_urls.append(path)

    ri = RawInput(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        created_by_id=user.id,
        source_type=source_type,
        source_channel=source_channel,
        raw_text=text,
        attachment_urls=attachment_urls,
        context_json={"url": url} if url else {},
    )
    db.add(ri)
    db.flush()

    draft = await process_raw_input(ri.id, db)
    return {"raw_input_id": ri.id, "draft": _draft_dict(draft)}


# ── drafts ────────────────────────────────────────────────────────────────────

@router.get("/drafts")
def list_drafts(
    status: Optional[str] = None,
    object_type: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Draft).filter(Draft.created_by_id == user.id)
    if status:
        q = q.filter(Draft.status == status)
    if object_type:
        q = q.filter(Draft.object_type == object_type)
    drafts = q.order_by(Draft.created_at.desc()).limit(50).all()
    return [_draft_dict(d) for d in drafts]


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    return _draft_dict(draft)


class ConfirmRequest(BaseModel):
    confirmed_fields: dict = {}
    corrections: dict = {}


@router.patch("/drafts/{draft_id}/confirm")
def confirm_draft_fields(
    draft_id: int,
    req: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户确认字段或纠错，记录 learning sample。"""
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")

    # 合并已确认字段
    confirmed = dict(draft.confirmed_fields or {})
    confirmed.update(req.confirmed_fields)
    draft.confirmed_fields = confirmed

    # 记录纠错
    if req.corrections:
        corrections = list(draft.user_corrections or [])
        for field, new_val in req.corrections.items():
            ai_val = (draft.fields_json or {}).get(field)
            corrections.append({
                "field": field,
                "ai_value": ai_val,
                "user_value": new_val,
                "ts": datetime.datetime.utcnow().isoformat(),
            })
            # 更新 fields_json
            fields = dict(draft.fields_json or {})
            fields[field] = new_val
            draft.fields_json = fields

            # 写 learning sample
            sample = LearningSample(
                draft_id=draft.id,
                raw_input_id=draft.source_raw_input_id,
                object_type=draft.object_type.value if draft.object_type else "unknown",
                task_type="field_correction",
                model_output_json={"field": field, "value": ai_val},
                user_correction_json={"field": field, "value": new_val},
                final_answer_json={"field": field, "value": new_val},
                created_by_id=user.id,
            )
            db.add(sample)
        draft.user_corrections = corrections

    # 移除已确认的 pending_questions
    answered = set(req.confirmed_fields.keys()) | set(req.corrections.keys())
    draft.pending_questions = [
        q for q in (draft.pending_questions or [])
        if q.get("field") not in answered
    ]

    if not draft.pending_questions:
        draft.status = DraftStatus.CONFIRMED

    db.commit()
    return _draft_dict(draft)


@router.post("/drafts/{draft_id}/convert")
def convert_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将草稿转为正式对象（knowledge_entry / opportunity / feedback_item）。"""
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    if draft.status == DraftStatus.CONVERTED:
        raise HTTPException(400, "Draft already converted")
    if draft.status == DraftStatus.DISCARDED:
        raise HTTPException(400, "Draft is discarded")

    # 合并 AI 字段 + 用户确认字段（用户确认优先）
    fields = {**(draft.fields_json or {}), **(draft.confirmed_fields or {})}

    formal_id = None

    if draft.object_type == DetectedObjectType.KNOWLEDGE:
        entry = KnowledgeEntry(
            title=draft.title or fields.get("title", "未命名"),
            content=fields.get("content_summary", draft.summary or ""),
            category=fields.get("knowledge_type", "experience"),
            industry_tags=fields.get("industry_tags", []),
            platform_tags=fields.get("platform_tags", []),
            topic_tags=fields.get("topic_tags", []),
            created_by=user.id,
            department_id=user.department_id,
            source_type="ai_draft",
            source_draft_id=draft.id,
            raw_input_id=draft.source_raw_input_id,
            capture_mode="chat_delegate",
        )
        db.add(entry)
        db.flush()
        formal_id = entry.id

        # 尝试写入向量库（失败不阻塞）
        try:
            from app.services.knowledge_service import approve_knowledge
            # 知识条目仍需审核流程，不直接 approve
        except Exception:
            pass

    elif draft.object_type == DetectedObjectType.OPPORTUNITY:
        opp = Opportunity(
            title=draft.title or fields.get("title", "未命名商机"),
            customer_name=fields.get("customer_name"),
            industry=fields.get("industry"),
            stage=fields.get("stage", "lead"),
            priority=fields.get("priority", "normal"),
            needs_summary=fields.get("needs_summary"),
            decision_map=fields.get("decision_map", []),
            risk_points=fields.get("risk_points", []),
            next_actions=fields.get("next_actions", []),
            source_draft_id=draft.id,
            created_by_id=user.id,
            department_id=user.department_id,
        )
        db.add(opp)
        db.flush()
        formal_id = opp.id

    elif draft.object_type == DetectedObjectType.FEEDBACK:
        fb = FeedbackItem(
            title=draft.title or fields.get("title", "未命名反馈"),
            customer_name=fields.get("customer_name"),
            feedback_type=fields.get("feedback_type"),
            severity=fields.get("severity", "medium"),
            description=fields.get("description", draft.summary or ""),
            affected_module=fields.get("affected_module"),
            renewal_risk_level=fields.get("renewal_risk_level", "low"),
            routed_team=fields.get("routed_team"),
            knowledgeworthy=1 if fields.get("knowledgeworthy") else 0,
            source_draft_id=draft.id,
            created_by_id=user.id,
        )
        db.add(fb)
        db.flush()
        formal_id = fb.id

    else:
        raise HTTPException(400, f"Cannot convert object_type: {draft.object_type}")

    draft.formal_object_id = formal_id
    draft.status = DraftStatus.CONVERTED
    db.commit()

    return {
        "draft_id": draft.id,
        "object_type": draft.object_type.value,
        "formal_object_id": formal_id,
    }


@router.post("/drafts/{draft_id}/discard")
def discard_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    draft = db.get(Draft, draft_id)
    if not draft or draft.created_by_id != user.id:
        raise HTTPException(404, "Draft not found")
    draft.status = DraftStatus.DISCARDED
    db.commit()
    return {"ok": True}


# ── Confirmations feed ────────────────────────────────────────────────────────

@router.get("/confirmations")
def get_pending_confirmations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户所有 waiting_confirmation 草稿的待确认问题（信息流）。"""
    drafts = (
        db.query(Draft)
        .filter(
            Draft.created_by_id == user.id,
            Draft.status == DraftStatus.WAITING_CONFIRMATION,
        )
        .order_by(Draft.created_at.desc())
        .limit(20)
        .all()
    )

    items = []
    for d in drafts:
        for q in (d.pending_questions or []):
            items.append({
                "draft_id": d.id,
                "draft_title": d.title,
                "object_type": d.object_type.value if d.object_type else "unknown",
                **q,
            })
    return items
```

**Step 2: 注册 router 到 main.py**

在 `backend/app/main.py` 末尾加：

```python
from app.routers import drafts  # noqa: E402
app.include_router(drafts.router)
```

**Step 3: 写测试**

```python
# backend/tests/test_drafts.py
"""TC-DRAFTS: raw-inputs submission, draft lifecycle, convert to formal objects."""
import json
import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import _make_user, _make_dept, _make_model_config, _login, _auth

KNOWLEDGE_LLM_RESPONSE = json.dumps({
    "object_type": "knowledge",
    "intent": "沉淀投放经验",
    "summary": "618大促ROI提升方法论",
    "fields": {
        "title": "618大促ROI提升",
        "content_summary": "通过分时竞价提升ROI",
        "knowledge_type": "methodology",
        "industry_tags": ["电商"],
        "platform_tags": ["天猫"],
        "topic_tags": ["ROI优化"],
        "visibility": "all",
    },
    "confidence": {"title": 0.95, "visibility": 0.5},
    "pending_questions": [
        {"field": "visibility", "question": "谁可以看？", "options": ["全员", "部门"], "type": "single_choice"}
    ],
    "suggested_actions": ["保存草稿"],
})

OPPORTUNITY_LLM_RESPONSE = json.dumps({
    "object_type": "opportunity",
    "intent": "记录商机",
    "summary": "XX客户有需求",
    "fields": {
        "title": "XX客户商机",
        "customer_name": "XX公司",
        "industry": "快消",
        "stage": "needs",
        "needs_summary": "需要投放",
        "decision_map": [],
        "risk_points": [],
        "next_actions": ["发方案"],
        "priority": "high",
    },
    "confidence": {"title": 0.9, "stage": 0.6},
    "pending_questions": [
        {"field": "stage", "question": "商机阶段？", "options": ["初步接触", "探需中"], "type": "single_choice"}
    ],
    "suggested_actions": ["保存商机"],
})

FEEDBACK_LLM_RESPONSE = json.dumps({
    "object_type": "feedback",
    "intent": "记录客户反馈",
    "summary": "客户报告数据异常",
    "fields": {
        "title": "数据展示异常",
        "customer_name": "YY公司",
        "feedback_type": "bug",
        "severity": "high",
        "description": "报表数据不对",
        "affected_module": "数据报表",
        "renewal_risk_level": "medium",
        "routed_team": "技术组",
        "knowledgeworthy": False,
    },
    "confidence": {"feedback_type": 0.9, "severity": 0.7},
    "pending_questions": [],
    "suggested_actions": ["流转技术组"],
})


@pytest.fixture
def setup(db, client):
    dept = _make_dept(db)
    user = _make_user(db, "draft_user", dept_id=dept.id)
    _make_model_config(db)
    db.commit()
    token = _login(client, "draft_user")
    return token, user


# ── raw-inputs ────────────────────────────────────────────────────────────────

def test_create_raw_input_knowledge(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        resp = client.post("/api/raw-inputs", headers=_auth(token), data={
            "text": "618期间分时竞价ROI从2提升到3.5",
            "source_type": "text",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert "draft" in data
    draft = data["draft"]
    assert draft["object_type"] == "knowledge"
    assert draft["title"] == "618大促ROI提升"
    assert draft["status"] == "waiting_confirmation"
    assert len(draft["pending_questions"]) == 1


def test_create_raw_input_requires_auth(client, db):
    _make_dept(db)
    db.commit()
    resp = client.post("/api/raw-inputs", data={"text": "test"})
    assert resp.status_code in (401, 403)


# ── draft lifecycle ───────────────────────────────────────────────────────────

def test_list_drafts(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    resp = client.get("/api/drafts", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_confirm_fields_removes_pending_question(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]

    # 确认 visibility 字段
    resp = client.patch(f"/api/drafts/{draft_id}/confirm", headers=_auth(token), json={
        "confirmed_fields": {"visibility": "all"},
    })
    assert resp.status_code == 200
    data = resp.json()
    # pending_questions 应该清空
    assert len(data["pending_questions"]) == 0
    assert data["status"] == "confirmed"


def test_correct_field_records_learning_sample(client, setup, db):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]

    resp = client.patch(f"/api/drafts/{draft_id}/confirm", headers=_auth(token), json={
        "corrections": {"knowledge_type": "case_study"},
    })
    assert resp.status_code == 200

    from app.models.draft import LearningSample
    sample = db.query(LearningSample).filter_by(draft_id=draft_id).first()
    assert sample is not None
    assert sample.task_type == "field_correction"
    assert sample.user_correction_json["value"] == "case_study"


def test_convert_knowledge_draft(client, setup, db):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]

    resp = client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["object_type"] == "knowledge"
    assert data["formal_object_id"] is not None

    from app.models.knowledge import KnowledgeEntry
    entry = db.get(KnowledgeEntry, data["formal_object_id"])
    assert entry is not None
    assert entry.capture_mode == "chat_delegate"
    assert entry.source_draft_id == draft_id


def test_convert_opportunity_draft(client, setup, db):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=OPPORTUNITY_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "客户聊天内容"})

    draft_id = create_resp.json()["draft"]["id"]
    resp = client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    assert resp.status_code == 200

    from app.models.opportunity import Opportunity
    opp = db.get(Opportunity, resp.json()["formal_object_id"])
    assert opp is not None
    assert opp.customer_name == "XX公司"


def test_convert_feedback_draft(client, setup, db):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=FEEDBACK_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "客户反馈内容"})

    draft_id = create_resp.json()["draft"]["id"]
    resp = client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    assert resp.status_code == 200

    from app.models.feedback_item import FeedbackItem
    fb = db.get(FeedbackItem, resp.json()["formal_object_id"])
    assert fb is not None
    assert fb.feedback_type == "bug"


def test_discard_draft(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]
    resp = client.post(f"/api/drafts/{draft_id}/discard", headers=_auth(token))
    assert resp.status_code == 200

    get_resp = client.get(f"/api/drafts/{draft_id}", headers=_auth(token))
    assert get_resp.json()["status"] == "discarded"


def test_get_pending_confirmations(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    resp = client.get("/api/confirmations", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["field"] == "visibility"
    assert "draft_id" in items[0]
```

**Step 4: 运行测试**

```bash
cd backend
python -m pytest tests/test_drafts.py -v
```

期望：所有测试 PASS

**Step 5: Commit**

```bash
git add backend/app/routers/drafts.py backend/app/main.py backend/tests/test_drafts.py
git commit -m "feat: add drafts router with raw-inputs, draft lifecycle, and convert endpoints"
```

---

## Task 4: 前端 — MultimodalInput 组件

**Files:**
- Create: `frontend/app/components/chat/MultimodalInput.tsx`
- Create: `frontend/app/lib/draft-api.ts`

**Step 1: 写 draft-api.ts**

```typescript
// frontend/app/lib/draft-api.ts
import { apiFetch } from "~/lib/api";

export interface PendingQuestion {
  field: string;
  question: string;
  options?: string[];
  type: "single_choice" | "text";
}

export interface DraftData {
  id: number;
  object_type: "knowledge" | "opportunity" | "feedback" | "unknown";
  title: string | null;
  summary: string | null;
  fields: Record<string, any>;
  tags: { industry?: string[]; platform?: string[]; topic?: string[] };
  pending_questions: PendingQuestion[];
  confirmed_fields: Record<string, any>;
  suggested_actions: string[];
  status: "draft" | "waiting_confirmation" | "confirmed" | "discarded" | "converted";
  formal_object_id: number | null;
}

export async function submitRawInput(
  params: {
    text?: string;
    files?: File[];
    conversationId?: number;
    workspaceId?: number;
  },
  token: string
): Promise<{ raw_input_id: number; draft: DraftData }> {
  const form = new FormData();
  if (params.text) form.append("text", params.text);
  if (params.conversationId) form.append("conversation_id", String(params.conversationId));
  if (params.workspaceId) form.append("workspace_id", String(params.workspaceId));
  form.append("source_type", params.files?.length ? "file" : "text");
  params.files?.forEach((f) => form.append("files", f));

  return apiFetch("/api/raw-inputs", { method: "POST", body: form, token });
}

export async function confirmDraftFields(
  draftId: number,
  params: { confirmed_fields?: Record<string, any>; corrections?: Record<string, any> },
  token: string
): Promise<DraftData> {
  return apiFetch(`/api/drafts/${draftId}/confirm`, {
    method: "PATCH",
    body: JSON.stringify(params),
    token,
  });
}

export async function convertDraft(draftId: number, token: string) {
  return apiFetch(`/api/drafts/${draftId}/convert`, { method: "POST", token });
}

export async function discardDraft(draftId: number, token: string) {
  return apiFetch(`/api/drafts/${draftId}/discard`, { method: "POST", token });
}

export async function getPendingConfirmations(token: string) {
  return apiFetch("/api/confirmations", { token });
}
```

**Step 2: 写 MultimodalInput.tsx**

```tsx
// frontend/app/components/chat/MultimodalInput.tsx
import { useRef, useState, useCallback } from "react";

interface Attachment {
  file: File;
  preview?: string;
}

interface MultimodalInputProps {
  onSubmit: (data: { text: string; files: File[] }) => void;
  isLoading: boolean;
  placeholder?: string;
}

export function MultimodalInput({ onSubmit, isLoading, placeholder }: MultimodalInputProps) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback((files: FileList | File[]) => {
    const newAttachments: Attachment[] = Array.from(files).map((file) => {
      const preview = file.type.startsWith("image/")
        ? URL.createObjectURL(file)
        : undefined;
      return { file, preview };
    });
    setAttachments((prev) => [...prev, ...newAttachments]);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      if (e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles]
  );

  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData.items;
      const imageItems = Array.from(items).filter((i) => i.type.startsWith("image/"));
      if (imageItems.length > 0) {
        e.preventDefault();
        const files = imageItems.map((i) => i.getAsFile()!).filter(Boolean);
        addFiles(files);
      }
      // URL detection
      const pastedText = e.clipboardData.getData("text");
      if (pastedText && /^https?:\/\//.test(pastedText.trim())) {
        // URL will just be part of text — handled by processor
      }
    },
    [addFiles]
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    if (!text.trim() && attachments.length === 0) return;
    onSubmit({ text: text.trim(), files: attachments.map((a) => a.file) });
    setText("");
    setAttachments([]);
  };

  const removeAttachment = (idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <div className="border-t-2 border-[#1A202C] bg-white flex-shrink-0">
      <div
        className={`max-w-3xl mx-auto px-4 py-3 ${isDragOver ? "bg-[#CCF2FF]" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setIsDragOver(true); }}
        onDragLeave={() => setIsDragOver(false)}
        onDrop={handleDrop}
      >
        {/* Attachment previews */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {attachments.map((att, idx) => (
              <div key={idx} className="relative flex items-center gap-1.5 border-2 border-[#1A202C] bg-[#EBF4F7] px-2 py-1">
                {att.preview ? (
                  <img src={att.preview} alt="" className="w-8 h-8 object-cover" />
                ) : (
                  <span className="text-[9px] font-bold uppercase text-[#00A3C4]">
                    {att.file.name.split(".").pop()?.toUpperCase()}
                  </span>
                )}
                <span className="text-[9px] font-bold text-gray-600 max-w-[80px] truncate">
                  {att.file.name}
                </span>
                <button
                  onClick={() => removeAttachment(idx)}
                  className="text-gray-400 hover:text-red-500 font-bold text-xs leading-none"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Drop zone hint */}
        {isDragOver && (
          <div className="text-center py-2 text-[10px] font-bold uppercase tracking-widest text-[#00A3C4]">
            松开以添加文件
          </div>
        )}

        {/* Input area */}
        <div className="border-2 border-[#1A202C] bg-[#F8FAFC] focus-within:border-[#00D1FF] transition-colors">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={placeholder || "输入消息、粘贴内容、或拖入文件... (Ctrl+Enter 发送)"}
            rows={2}
            disabled={isLoading}
            className="w-full resize-none bg-transparent px-4 pt-3 pb-1 text-xs font-bold text-[#1A202C] placeholder:text-gray-400 placeholder:font-normal focus:outline-none leading-relaxed"
          />
          <div className="flex items-center justify-between px-4 pb-3">
            <div className="flex items-center gap-3">
              {/* File picker */}
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="text-gray-400 hover:text-[#00D1FF] transition-colors"
                title="上传文件"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
                    d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
                </svg>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".pdf,.docx,.pptx,.md,.txt,.png,.jpg,.jpeg"
                className="hidden"
                onChange={(e) => e.target.files && addFiles(e.target.files)}
              />
              <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400">
                支持文字 · 文件 · 截图
              </span>
            </div>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={isLoading || (!text.trim() && attachments.length === 0)}
              className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 flex items-center gap-2 transition-colors"
            >
              {isLoading ? (
                <span className="flex gap-1">
                  <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce [animation-delay:-0.3s]" />
                  <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce [animation-delay:-0.15s]" />
                  <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce" />
                </span>
              ) : (
                <>
                  发送
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M13 7l5 5m0 0l-5 5m5-5H6" />
                  </svg>
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
```

**Step 3: Commit**

```bash
git add frontend/app/lib/draft-api.ts frontend/app/components/chat/MultimodalInput.tsx
git commit -m "feat: add MultimodalInput component and draft-api client"
```

---

## Task 5: 前端 — DraftCard 草稿卡片组件

**Files:**
- Create: `frontend/app/components/chat/DraftCard.tsx`
- Create: `frontend/app/components/chat/DraftPanel.tsx`

**Step 1: 写 DraftCard.tsx**

```tsx
// frontend/app/components/chat/DraftCard.tsx
import { useState } from "react";
import type { DraftData, PendingQuestion } from "~/lib/draft-api";

const OBJECT_TYPE_LABELS: Record<string, string> = {
  knowledge: "📝 知识草稿",
  opportunity: "🏢 商机草稿",
  feedback: "💬 反馈草稿",
  unknown: "❓ 未识别",
};

const OBJECT_TYPE_COLORS: Record<string, string> = {
  knowledge: "#00D1FF",
  opportunity: "#00CC99",
  feedback: "#F6AD55",
  unknown: "#A0AEC0",
};

interface DraftCardProps {
  draft: DraftData;
  onConfirmFields: (confirmed: Record<string, any>, corrections?: Record<string, any>) => void;
  onConvert: () => void;
  onDiscard: () => void;
  isConverting?: boolean;
}

function PendingQuestionItem({
  question,
  onAnswer,
}: {
  question: PendingQuestion;
  onAnswer: (field: string, value: string) => void;
}) {
  const [customValue, setCustomValue] = useState("");

  return (
    <div className="border-l-2 border-yellow-400 pl-3 py-1">
      <p className="text-[10px] font-bold text-gray-600 mb-1.5">{question.question}</p>
      {question.type === "single_choice" && question.options ? (
        <div className="flex flex-wrap gap-1.5">
          {question.options.map((opt) => (
            <button
              key={opt}
              onClick={() => onAnswer(question.field, opt)}
              className="px-3 py-1 text-[9px] font-bold uppercase border-2 border-[#1A202C] bg-white hover:bg-[#CCF2FF] hover:border-[#00D1FF] transition-colors"
            >
              {opt}
            </button>
          ))}
        </div>
      ) : (
        <div className="flex gap-2">
          <input
            value={customValue}
            onChange={(e) => setCustomValue(e.target.value)}
            className="flex-1 border-2 border-[#1A202C] px-2 py-1 text-xs focus:outline-none focus:border-[#00D1FF]"
            placeholder="输入答案..."
          />
          <button
            onClick={() => { onAnswer(question.field, customValue); setCustomValue(""); }}
            className="px-3 py-1 text-[9px] font-bold bg-[#1A202C] text-white"
          >
            确认
          </button>
        </div>
      )}
    </div>
  );
}

function FieldRow({ label, value }: { label: string; value: string | string[] }) {
  const displayValue = Array.isArray(value) ? value.join(", ") || "—" : value || "—";
  return (
    <div className="flex gap-2 py-1 border-b border-gray-100">
      <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400 w-16 flex-shrink-0 pt-0.5">
        {label}
      </span>
      <span className="text-[10px] font-bold text-[#1A202C] flex-1">{displayValue}</span>
    </div>
  );
}

export function DraftCard({ draft, onConfirmFields, onConvert, onDiscard, isConverting }: DraftCardProps) {
  const color = OBJECT_TYPE_COLORS[draft.object_type] || "#A0AEC0";
  const label = OBJECT_TYPE_LABELS[draft.object_type] || "草稿";

  const handleAnswer = (field: string, value: string) => {
    onConfirmFields({ [field]: value });
  };

  // Render key fields based on object type
  const renderFields = () => {
    const f = { ...draft.fields, ...draft.confirmed_fields };
    if (draft.object_type === "knowledge") {
      return (
        <>
          {f.knowledge_type && <FieldRow label="类型" value={f.knowledge_type} />}
          {f.industry_tags?.length > 0 && <FieldRow label="行业" value={f.industry_tags} />}
          {f.platform_tags?.length > 0 && <FieldRow label="平台" value={f.platform_tags} />}
          {f.topic_tags?.length > 0 && <FieldRow label="主题" value={f.topic_tags} />}
          {f.visibility && <FieldRow label="可见" value={f.visibility === "all" ? "全员" : "本部门"} />}
        </>
      );
    }
    if (draft.object_type === "opportunity") {
      return (
        <>
          {f.customer_name && <FieldRow label="客户" value={f.customer_name} />}
          {f.industry && <FieldRow label="行业" value={f.industry} />}
          {f.stage && <FieldRow label="阶段" value={f.stage} />}
          {f.priority && <FieldRow label="优先级" value={f.priority} />}
          {f.needs_summary && <FieldRow label="需求" value={f.needs_summary} />}
        </>
      );
    }
    if (draft.object_type === "feedback") {
      return (
        <>
          {f.customer_name && <FieldRow label="客户" value={f.customer_name} />}
          {f.feedback_type && <FieldRow label="类型" value={f.feedback_type} />}
          {f.severity && <FieldRow label="严重度" value={f.severity} />}
          {f.renewal_risk_level && <FieldRow label="续费风险" value={f.renewal_risk_level} />}
          {f.routed_team && <FieldRow label="建议流转" value={f.routed_team} />}
        </>
      );
    }
    return null;
  };

  const isConverted = draft.status === "converted";
  const isDiscarded = draft.status === "discarded";

  return (
    <div className="border-2 border-[#1A202C] bg-white overflow-hidden">
      {/* Header */}
      <div className="px-4 py-2.5 flex items-center justify-between" style={{ backgroundColor: `${color}22`, borderBottom: `2px solid ${color}` }}>
        <span className="text-[10px] font-bold uppercase tracking-widest" style={{ color }}>
          {label}
        </span>
        {isConverted && (
          <span className="text-[9px] font-bold uppercase text-green-600 bg-green-100 px-2 py-0.5 border border-green-400">
            ✓ 已保存
          </span>
        )}
        {isDiscarded && (
          <span className="text-[9px] font-bold uppercase text-gray-400">已丢弃</span>
        )}
      </div>

      <div className="p-4 space-y-3">
        {/* Title */}
        <div>
          <p className="text-xs font-bold text-[#1A202C] leading-snug">{draft.title || "（无标题）"}</p>
          {draft.summary && (
            <p className="text-[10px] text-gray-500 mt-0.5 leading-relaxed">{draft.summary}</p>
          )}
        </div>

        {/* Fields */}
        <div className="space-y-0">{renderFields()}</div>

        {/* Pending questions */}
        {draft.pending_questions.length > 0 && !isConverted && !isDiscarded && (
          <div className="space-y-2">
            <p className="text-[9px] font-bold uppercase tracking-widest text-yellow-600">
              ⚠ 待确认
            </p>
            {draft.pending_questions.map((q) => (
              <PendingQuestionItem key={q.field} question={q} onAnswer={handleAnswer} />
            ))}
          </div>
        )}

        {/* Actions */}
        {!isConverted && !isDiscarded && (
          <div className="flex flex-wrap gap-2 pt-1">
            <button
              onClick={onConvert}
              disabled={isConverting}
              className="px-4 py-1.5 text-[9px] font-bold uppercase tracking-widest bg-[#1A202C] text-white hover:bg-black disabled:opacity-50 transition-colors"
            >
              {isConverting ? "保存中..." : draft.object_type === "knowledge" ? "保存知识" : draft.object_type === "opportunity" ? "保存商机" : "保存反馈"}
            </button>
            {draft.suggested_actions.filter(a => !a.includes("保存")).slice(0, 2).map((action) => (
              <button
                key={action}
                className="px-4 py-1.5 text-[9px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white hover:bg-gray-100 transition-colors"
              >
                {action}
              </button>
            ))}
            <button
              onClick={onDiscard}
              className="px-3 py-1.5 text-[9px] font-bold uppercase tracking-widest text-gray-400 hover:text-red-500 transition-colors ml-auto"
            >
              丢弃
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
```

**Step 2: 写 DraftPanel.tsx**

```tsx
// frontend/app/components/chat/DraftPanel.tsx
import type { DraftData } from "~/lib/draft-api";
import { DraftCard } from "./DraftCard";

interface DraftPanelProps {
  draft: DraftData | null;
  onConfirmFields: (confirmed: Record<string, any>, corrections?: Record<string, any>) => void;
  onConvert: () => void;
  onDiscard: () => void;
  isConverting?: boolean;
}

export function DraftPanel({ draft, onConfirmFields, onConvert, onDiscard, isConverting }: DraftPanelProps) {
  if (!draft) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center p-8">
        <div className="w-2 h-2 bg-[#00D1FF] mb-4" />
        <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
          输入素材后，系统将在这里生成结构化草稿
        </p>
      </div>
    );
  }

  return (
    <div className="p-4 overflow-y-auto h-full">
      <DraftCard
        draft={draft}
        onConfirmFields={onConfirmFields}
        onConvert={onConvert}
        onDiscard={onDiscard}
        isConverting={isConverting}
      />
    </div>
  );
}
```

**Step 3: Commit**

```bash
git add frontend/app/components/chat/DraftCard.tsx frontend/app/components/chat/DraftPanel.tsx
git commit -m "feat: add DraftCard and DraftPanel components for structured output display"
```

---

## Task 6: 前端 — Chat 页面重构为双栏布局

**Files:**
- Modify: `frontend/app/routes/app/chat/conversation.tsx` — 全面重构

**Step 1: 重构 conversation.tsx**

```tsx
// frontend/app/routes/app/chat/conversation.tsx
import { useEffect, useRef, useState } from "react";
import { data, useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/conversation";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";
import type { Message } from "~/lib/types";
import { MultimodalInput } from "~/components/chat/MultimodalInput";
import { DraftPanel } from "~/components/chat/DraftPanel";
import { submitRawInput, confirmDraftFields, convertDraft, discardDraft } from "~/lib/draft-api";
import type { DraftData } from "~/lib/draft-api";

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const [messages, convList] = await Promise.all([
    apiFetch(`/api/conversations/${params.id}/messages`, { token }),
    apiFetch("/api/conversations", { token }),
  ]);
  const conv = (convList as any[]).find((c: any) => String(c.id) === params.id);
  return { messages, conversationId: params.id, workspace: conv?.workspace ?? null, token };
}

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-5`}>
      {!isUser && (
        <div className="w-7 h-7 bg-[#00D1FF] border-2 border-[#1A202C] text-[#1A202C] flex items-center justify-center text-[8px] font-bold mr-3 flex-shrink-0 mt-0.5 uppercase tracking-wider">
          KB
        </div>
      )}
      <div className={`max-w-[80%]`}>
        <div className={`px-4 py-3 ${isUser ? "bg-[#1A202C] text-white border-2 border-[#1A202C]" : "bg-white text-[#1A202C] border-2 border-[#1A202C]"}`}>
          <p className="text-xs font-bold whitespace-pre-wrap leading-relaxed">{message.content}</p>
        </div>
        {!isUser && (message as any).metadata?.draft_id && (
          <div className="mt-1.5 flex items-center gap-1.5">
            <div className="w-1 h-1 bg-[#00D1FF]" />
            <p className="text-[9px] text-[#00A3C4] uppercase font-bold tracking-widest">
              已生成草稿 #{(message as any).metadata.draft_id}
            </p>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-7 h-7 bg-[#00CC99] border-2 border-[#1A202C] flex items-center justify-center text-[8px] font-bold ml-3 flex-shrink-0 mt-0.5 uppercase text-white">
          Me
        </div>
      )}
    </div>
  );
}

export default function ConversationPage() {
  const { messages: initialMessages, conversationId, workspace, token } = useLoaderData<typeof loader>() as any;
  const [messages, setMessages] = useState<Message[]>(initialMessages);
  const [currentDraft, setCurrentDraft] = useState<DraftData | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  const handleSubmit = async ({ text, files }: { text: string; files: File[] }) => {
    if (!text && files.length === 0) return;

    // Optimistic user message
    const optimisticMsg: Message = {
      id: -Date.now(),
      role: "user",
      content: text || `[上传了 ${files.length} 个文件]`,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimisticMsg]);
    setIsProcessing(true);

    try {
      // Use low-friction input flow
      const result = await submitRawInput(
        { text, files, conversationId: Number(conversationId) },
        token
      );
      setCurrentDraft(result.draft);

      // Add assistant message
      const assistantMsg: Message = {
        id: -Date.now() - 1,
        role: "assistant",
        content: `已为你生成「${result.draft.title || "草稿"}」，请在右侧确认。`,
        created_at: new Date().toISOString(),
        metadata: { draft_id: result.draft.id },
      } as any;
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        {
          id: -Date.now() - 1,
          role: "assistant",
          content: "处理失败，请重试。",
          created_at: new Date().toISOString(),
        } as Message,
      ]);
    } finally {
      setIsProcessing(false);
    }
  };

  const handleConfirmFields = async (confirmed: Record<string, any>, corrections?: Record<string, any>) => {
    if (!currentDraft) return;
    try {
      const updated = await confirmDraftFields(currentDraft.id, { confirmed_fields: confirmed, corrections }, token);
      setCurrentDraft(updated);
    } catch (e) {
      console.error("Confirm failed", e);
    }
  };

  const handleConvert = async () => {
    if (!currentDraft) return;
    setIsConverting(true);
    try {
      await convertDraft(currentDraft.id, token);
      setCurrentDraft((prev) => prev ? { ...prev, status: "converted" } : null);
    } catch (e) {
      console.error("Convert failed", e);
    } finally {
      setIsConverting(false);
    }
  };

  const handleDiscard = async () => {
    if (!currentDraft) return;
    try {
      await discardDraft(currentDraft.id, token);
      setCurrentDraft((prev) => prev ? { ...prev, status: "discarded" } : null);
    } catch (e) {
      console.error("Discard failed", e);
    }
  };

  return (
    <div className="flex h-full bg-[#F0F4F8]">
      {/* Left: conversation (60%) */}
      <div className="flex flex-col flex-1 min-w-0 border-r-2 border-[#1A202C]">
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-2xl mx-auto px-6 py-8">
            {messages.length === 0 && (
              <div className="flex flex-col items-center justify-center py-20 text-center">
                <div className="w-2 h-2 bg-[#00D1FF] mb-4" />
                <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
                  把工作素材扔进来，AI 帮你整理
                </p>
                <p className="text-[9px] text-gray-300 mt-1">支持文字 · 文件 · 截图 · 粘贴</p>
              </div>
            )}
            {messages.map((msg, i) => (
              <MessageBubble key={msg.id < 0 ? `opt-${i}` : msg.id} message={msg} />
            ))}
            {isProcessing && (
              <div className="flex justify-start mb-5">
                <div className="w-7 h-7 bg-[#00D1FF] border-2 border-[#1A202C] flex items-center justify-center text-[8px] font-bold mr-3 flex-shrink-0 uppercase tracking-wider">KB</div>
                <div className="bg-white border-2 border-[#1A202C] px-4 py-3">
                  <div className="flex space-x-1.5 items-center h-4">
                    <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce [animation-delay:-0.3s]" />
                    <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce [animation-delay:-0.15s]" />
                    <div className="w-1.5 h-1.5 bg-[#00D1FF] animate-bounce" />
                  </div>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
        <MultimodalInput onSubmit={handleSubmit} isLoading={isProcessing} />
      </div>

      {/* Right: draft panel (40%) */}
      <div className="w-96 flex-shrink-0 overflow-hidden flex flex-col bg-white">
        <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-4 py-2.5">
          <span className="text-[10px] font-bold uppercase tracking-widest text-[#1A202C]">成果物</span>
        </div>
        <div className="flex-1 overflow-hidden">
          <DraftPanel
            draft={currentDraft}
            onConfirmFields={handleConfirmFields}
            onConvert={handleConvert}
            onDiscard={handleDiscard}
            isConverting={isConverting}
          />
        </div>
      </div>
    </div>
  );
}
```

**Step 2: 检查 types.ts 是否有 Message 类型，如没有则确认从 conversation.tsx 原本引用的路径**

```bash
cat frontend/app/lib/types.ts 2>/dev/null || echo "no types file"
```

如果 `Message` 类型不存在，在 `frontend/app/lib/types.ts` 中添加：

```typescript
export interface Message {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  metadata?: Record<string, any>;
}
```

**Step 3: 运行前端开发服务器验证编译无错误**

```bash
cd frontend
npm run build 2>&1 | head -50
```

期望：无 TypeScript 错误，build 成功

**Step 4: Commit**

```bash
git add frontend/app/routes/app/chat/conversation.tsx frontend/app/lib/types.ts
git commit -m "feat: refactor conversation page to dual-column layout with DraftPanel"
```

---

## Task 7: 前端 — 待确认流页面

**Files:**
- Create: `frontend/app/routes/app/confirmations/index.tsx`
- Modify: `frontend/app/routes.ts` — 注册新路由

**Step 1: 写 confirmations/index.tsx**

```tsx
// frontend/app/routes/app/confirmations/index.tsx
import { useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import { confirmDraftFields } from "~/lib/draft-api";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const items = await apiFetch("/api/confirmations", { token });
  return { items, token };
}

const OBJECT_TYPE_LABELS: Record<string, string> = {
  knowledge: "📝 知识",
  opportunity: "🏢 商机",
  feedback: "💬 反馈",
};

export default function ConfirmationsPage() {
  const { items: initialItems, token } = useLoaderData<typeof loader>() as any;
  const [items, setItems] = useState<any[]>(initialItems);
  const [answering, setAnswering] = useState<string | null>(null);

  const handleAnswer = async (draftId: number, field: string, value: string) => {
    const key = `${draftId}-${field}`;
    setAnswering(key);
    try {
      await confirmDraftFields(draftId, { confirmed_fields: { [field]: value } }, token);
      setItems((prev) => prev.filter((item) => !(item.draft_id === draftId && item.field === field)));
    } catch (e) {
      console.error("Answer failed", e);
    } finally {
      setAnswering(null);
    }
  };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">待确认</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">
            {items.length} 项待处理
          </p>
        </div>
      </div>

      <div className="p-6 max-w-xl space-y-4">
        {items.length === 0 && (
          <div className="text-center py-16">
            <div className="w-2 h-2 bg-[#00CC99] mx-auto mb-4" />
            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
              全部处理完毕
            </p>
          </div>
        )}
        {items.map((item: any) => {
          const key = `${item.draft_id}-${item.field}`;
          const isAnswering = answering === key;
          return (
            <div key={key} className="border-2 border-[#1A202C] bg-white overflow-hidden">
              <div className="bg-[#EBF4F7] px-4 py-2 flex items-center gap-2 border-b-2 border-[#1A202C]">
                <span className="text-[9px] font-bold text-[#00A3C4] uppercase tracking-widest">
                  {OBJECT_TYPE_LABELS[item.object_type] || item.object_type}
                </span>
                <span className="text-[9px] text-gray-400 font-bold">·</span>
                <span className="text-[9px] font-bold text-gray-600 truncate max-w-[200px]">
                  {item.draft_title}
                </span>
              </div>
              <div className="p-4">
                <p className="text-xs font-bold text-[#1A202C] mb-3">{item.question}</p>
                {item.options ? (
                  <div className="flex flex-wrap gap-2">
                    {item.options.map((opt: string) => (
                      <button
                        key={opt}
                        onClick={() => handleAnswer(item.draft_id, item.field, opt)}
                        disabled={isAnswering}
                        className="px-4 py-1.5 text-[9px] font-bold uppercase border-2 border-[#1A202C] bg-white hover:bg-[#CCF2FF] hover:border-[#00D1FF] disabled:opacity-50 transition-colors"
                      >
                        {opt}
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-[9px] text-gray-400">（需要在草稿页面处理）</p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

**Step 2: 检查 routes.ts 格式并添加新路由**

先查看现有 routes.ts 格式：

```bash
cat frontend/app/routes.ts
```

然后在合适位置添加 confirmations 路由（仿照现有路由格式）。通常格式是：

```typescript
// 在已有路由之后添加
route("/confirmations", "routes/app/confirmations/index.tsx"),
```

**Step 3: 在侧边栏导航添加"待确认"入口**

查找并修改侧边栏组件（一般在 `routes/app/layout.tsx`）：

```bash
grep -n "knowledge" frontend/app/routes/app/layout.tsx | head -20
```

在导航列表中加入待确认入口，参考现有导航项的格式添加：

```tsx
{ href: "/confirmations", label: "待确认", icon: "⚠" }
```

**Step 4: Build 验证**

```bash
cd frontend
npm run build 2>&1 | head -30
```

**Step 5: Commit**

```bash
git add frontend/app/routes/app/confirmations/ frontend/app/routes.ts frontend/app/routes/app/layout.tsx
git commit -m "feat: add confirmations feed page for pending draft questions"
```

---

## Task 8: 端到端联调与验证

**Step 1: 运行完整后端测试套件**

```bash
cd backend
python -m pytest tests/test_low_friction_models.py tests/test_input_processor.py tests/test_drafts.py -v
```

期望：所有测试 PASS

**Step 2: 运行全量后端测试（确保没有回归）**

```bash
cd backend
python -m pytest -v 2>&1 | tail -30
```

期望：全部 PASS，无失败

**Step 3: 运行 Alembic migration（开发数据库）**

```bash
cd backend
alembic upgrade head
```

期望：migration 成功应用

**Step 4: 手动联调 Knowledge 链路**

启动后端：
```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

用 curl 测试（需要先登录获取 token）：

```bash
# 1. 登录
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. 提交原始输入（需要真实 LLM 配置）
curl -X POST http://localhost:8000/api/raw-inputs \
  -H "Authorization: Bearer $TOKEN" \
  -F "text=我在618期间通过分时竞价策略，把ROI从2提升到3.5。核心方法是：上午10-12点加价20%，晚上8-10点加价30%，其余时段降价10%。"

# 3. 记录返回的 draft_id，然后确认
curl -X PATCH http://localhost:8000/api/drafts/1/confirm \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"confirmed_fields": {"visibility": "all"}}'

# 4. 转换为正式知识
curl -X POST http://localhost:8000/api/drafts/1/convert \
  -H "Authorization: Bearer $TOKEN"
```

期望：每步都返回正确数据，最终 knowledge_entries 表中有新记录

**Step 5: 验证待确认流**

```bash
curl -X GET http://localhost:8000/api/confirmations \
  -H "Authorization: Bearer $TOKEN"
```

期望：返回还有 pending_questions 的草稿问题列表

**Step 6: 前端开发服务器验证**

```bash
cd frontend
npm run dev
```

打开 http://localhost:5173/chat 验证：
1. 发送一段文字，右侧应出现草稿卡片
2. 卡片中的待确认问题可以点选
3. 点选后问题消失，状态变 confirmed
4. 点击"保存知识"，调用 convert 接口成功

**Step 7: 最终 Commit**

```bash
git add -A
git commit -m "feat: complete low-friction input MVP - full chain from raw_input to formal objects"
```

---

## 验收 Checklist

- [ ] `python -m pytest tests/test_low_friction_models.py -v` — 全 PASS
- [ ] `python -m pytest tests/test_input_processor.py -v` — 全 PASS
- [ ] `python -m pytest tests/test_drafts.py -v` — 全 PASS
- [ ] `python -m pytest -v` — 全量无回归
- [ ] `npm run build` — 无 TypeScript 错误
- [ ] 用户可以粘贴文字，看到草稿卡片（右侧面板）
- [ ] 待确认问题可以点选确认，问题从卡片消失
- [ ] 字段纠错后，learning_samples 有新记录
- [ ] 点击"保存"后，knowledge_entries / opportunities / feedback_items 有新记录
- [ ] `/confirmations` 页面显示所有待处理问题，点选后消失
- [ ] 旧的 `/knowledge/new` 页面仍可正常使用
