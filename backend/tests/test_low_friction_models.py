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
