"""TC-INPUTPROCESSOR: Test the AI extraction pipeline with mocked LLM."""
import json
import pytest
from unittest.mock import AsyncMock, patch
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
