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


def test_get_draft(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]
    resp = client.get(f"/api/drafts/{draft_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["title"] == "618大促ROI提升"


def test_get_draft_not_found(client, setup):
    token, user = setup
    resp = client.get("/api/drafts/99999", headers=_auth(token))
    assert resp.status_code == 404


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


def test_convert_already_converted_fails(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]
    client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    resp = client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    assert resp.status_code == 400


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


def test_discard_then_convert_fails(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]
    client.post(f"/api/drafts/{draft_id}/discard", headers=_auth(token))
    resp = client.post(f"/api/drafts/{draft_id}/convert", headers=_auth(token))
    assert resp.status_code == 400


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


def test_confirmations_empty_after_confirm(client, setup):
    token, user = setup
    with patch(
        "app.services.input_processor.llm_gateway.chat",
        new=AsyncMock(return_value=KNOWLEDGE_LLM_RESPONSE),
    ):
        create_resp = client.post("/api/raw-inputs", headers=_auth(token), data={"text": "内容"})

    draft_id = create_resp.json()["draft"]["id"]
    client.patch(f"/api/drafts/{draft_id}/confirm", headers=_auth(token), json={
        "confirmed_fields": {"visibility": "all"},
    })

    resp = client.get("/api/confirmations", headers=_auth(token))
    assert len(resp.json()) == 0
