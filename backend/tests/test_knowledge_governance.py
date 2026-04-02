"""知识治理底座自动化测试。"""
from app.models.business import BusinessTable
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
from app.models.knowledge_governance import (
    GovernanceFeedbackEvent,
    GovernanceObject,
    GovernanceObjectType,
    GovernanceObjective,
    GovernanceResourceLibrary,
    GovernanceStrategyStat,
    GovernanceSuggestionTask,
)
from app.models.user import Role
from tests.conftest import _auth, _login, _make_dept, _make_user


def _seed_admin_and_entry(db):
    dept = _make_dept(db, "治理测试部")
    admin = _make_user(db, "gov_admin", Role.SUPER_ADMIN, dept.id)
    employee = _make_user(db, "gov_employee", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(
        title="客户复盘案例",
        content="客户案例正文",
        category="experience",
        status=KnowledgeStatus.APPROVED,
        created_by=employee.id,
        department_id=dept.id,
        source_type="manual",
        doc_render_status="ready",
    )
    db.add(entry)
    db.commit()
    return admin, employee, entry


def test_seed_default_governance_blueprint(client, db):
    admin, _, _ = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")

    resp = client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True

    objectives = db.query(GovernanceObjective).all()
    libraries = db.query(GovernanceResourceLibrary).all()
    assert any(item.code == "company_common" for item in objectives)
    assert any(item.code == "industry_intel" for item in libraries)
    assert any(item.code == "company_metrics" for item in libraries)
    assert any(item.code == "biz_external_signals" for item in libraries)


def test_seed_default_governance_blueprint_is_idempotent_and_backfills_new_libraries(client, db):
    admin, _, _ = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")

    first = client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))
    assert first.status_code == 200, first.text

    company_common = db.query(GovernanceObjective).filter(GovernanceObjective.code == "company_common").first()
    assert company_common is not None
    library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == "company_metrics").first()
    assert library is not None
    db.delete(library)
    db.commit()

    second = client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))
    assert second.status_code == 200, second.text

    restored = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == "company_metrics").first()
    assert restored is not None
    assert restored.objective_id == company_common.id


def test_create_governance_suggestion_marks_subject_suggested(client, db):
    admin, _, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    objective = db.query(GovernanceObjective).filter(GovernanceObjective.code == "professional_capability").first()
    library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == "role_capability").first()

    resp = client.post(
        "/api/knowledge-governance/suggestions",
        headers=_auth(token),
        json={
            "subject_type": "knowledge",
            "subject_id": entry.id,
            "task_type": "classify",
            "objective_id": objective.id,
            "resource_library_id": library.id,
            "suggested_payload": {"role_name": "客户运营岗"},
            "reason": "命中了岗位能力关键词",
            "confidence": 87,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confidence"] == 87

    db.refresh(entry)
    assert entry.governance_status == "suggested"
    suggestion = db.query(GovernanceSuggestionTask).filter(GovernanceSuggestionTask.subject_id == entry.id).first()
    assert suggestion is not None


def test_apply_governance_alignment_updates_knowledge_and_closes_pending_suggestions(client, db):
    admin, _, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    objective = db.query(GovernanceObjective).filter(GovernanceObjective.code == "outsource_intel").first()
    library = db.query(GovernanceResourceLibrary).filter(GovernanceResourceLibrary.code == "industry_intel").first()

    client.post(
        "/api/knowledge-governance/suggestions",
        headers=_auth(token),
        json={
            "subject_type": "knowledge",
            "subject_id": entry.id,
            "task_type": "classify",
            "objective_id": objective.id,
            "resource_library_id": library.id,
            "suggested_payload": {"industry": "电商", "intel_type": "素材趋势"},
            "reason": "需要挂到外部情报资源库",
            "confidence": 76,
        },
    )

    resp = client.post(
        "/api/knowledge-governance/apply",
        headers=_auth(token),
        json={
            "subject_type": "knowledge",
            "subject_id": entry.id,
            "objective_id": objective.id,
            "resource_library_id": library.id,
            "governance_status": "aligned",
            "governance_note": "人工确认挂载到行业情报",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    db.refresh(entry)
    assert entry.governance_objective_id == objective.id
    assert entry.resource_library_id == library.id
    assert entry.governance_status == "aligned"

    suggestion = db.query(GovernanceSuggestionTask).filter(GovernanceSuggestionTask.subject_id == entry.id).first()
    assert suggestion is not None
    assert suggestion.status == "applied"


def test_employee_cannot_seed_or_create_governance_config(client, db):
    _, employee, _ = _seed_admin_and_entry(db)
    token = _login(client, "gov_employee")

    seed_resp = client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))
    assert seed_resp.status_code == 403

    create_resp = client.post(
        "/api/knowledge-governance/objectives",
        headers=_auth(token),
        json={"name": "非法目标", "code": "bad_case"},
    )
    assert create_resp.status_code == 403


def test_generate_single_knowledge_governance_suggestion_by_keywords(client, db):
    _admin, _employee, entry = _seed_admin_and_entry(db)
    entry.title = "客户运营岗位胜任力训练营"
    entry.content = "这是一份客户运营岗位的能力模型和训练材料"
    db.add(entry)
    db.commit()
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    resp = client.post(f"/api/knowledge-governance/knowledge/{entry.id}/suggest", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["suggestion"]["confidence"] >= 80
    assert body["suggestion"]["suggested_payload"]["reinforcement_meta"]["strategy_group"] == "keyword_rule"

    db.refresh(entry)
    assert entry.governance_status == "suggested"
    assert entry.governance_note is not None


def test_generate_batch_governance_suggestions_for_existing_entries(client, db):
    _admin, _employee, entry = _seed_admin_and_entry(db)
    entry.title = "抖音行业情报周报"
    entry.content = "整理抖音投放趋势、竞品素材和平台规则变化"
    db.add(entry)
    db.commit()
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    resp = client.post("/api/knowledge-governance/knowledge/suggest-batch?limit=10", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] >= 1
    assert entry.id in body["entry_ids"]


def test_gap_overview_reports_missing_table_and_missing_knowledge(client, db):
    admin, employee, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == "customer").first()
    assert object_type is not None

    doc_only = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:doc-only",
        display_name="仅文档客户对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    table_only = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:table-only",
        display_name="仅表客户对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    db.add_all([doc_only, table_only])
    db.commit()

    entry.governance_object_id = doc_only.id
    table = BusinessTable(
        table_name="customer_table_only",
        display_name="仅表客户对象",
        description="结构化客户台账",
        owner_id=employee.id,
        department_id=employee.department_id,
        governance_object_id=table_only.id,
    )
    db.add(table)
    db.commit()

    resp = client.get("/api/knowledge-governance/gaps/overview", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    gap_types = {(item["object_id"], item["gap_type"]) for item in body["object_gaps"]}
    assert (doc_only.id, "missing_table") in gap_types
    assert (table_only.id, "missing_knowledge") in gap_types
    missing_table_gap = next(item for item in body["object_gaps"] if item["object_id"] == doc_only.id and item["gap_type"] == "missing_table")
    assert any(action["action"] == "create_or_bind_table" for action in missing_table_gap["recommended_actions"])


def test_merge_governance_objects_relinks_subjects(client, db):
    admin, employee, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == "customer").first()
    assert object_type is not None

    target = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:cid-main",
        display_name="CID 客户对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    source = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:cid-main-copy",
        display_name="CID客户对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    db.add_all([target, source])
    db.commit()

    entry.governance_object_id = source.id
    db.add(entry)
    db.commit()

    resp = client.post(
        "/api/knowledge-governance/objects/merge",
        headers=_auth(token),
        json={"source_object_id": source.id, "target_object_id": target.id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    db.refresh(entry)
    db.refresh(source)
    assert entry.governance_object_id == target.id
    assert source.lifecycle_status == "merged"


def test_apply_governance_alignment_records_feedback_and_strategy_stats(client, db):
    admin, _, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    entry.title = "客户运营岗位胜任力训练营"
    entry.content = "这是一份客户运营岗位的能力模型和训练材料"
    db.add(entry)
    db.commit()

    suggest_resp = client.post(f"/api/knowledge-governance/knowledge/{entry.id}/suggest", headers=_auth(token))
    assert suggest_resp.status_code == 200, suggest_resp.text

    suggestion = db.query(GovernanceSuggestionTask).filter(
        GovernanceSuggestionTask.subject_type == "knowledge",
        GovernanceSuggestionTask.subject_id == entry.id,
        GovernanceSuggestionTask.status == "pending",
    ).first()
    assert suggestion is not None
    objective = db.get(GovernanceObjective, suggestion.objective_id)
    library = db.get(GovernanceResourceLibrary, suggestion.resource_library_id)
    assert objective is not None
    assert library is not None

    apply_resp = client.post(
        "/api/knowledge-governance/apply",
        headers=_auth(token),
        json={
            "subject_type": "knowledge",
            "subject_id": entry.id,
            "objective_id": objective.id,
            "resource_library_id": library.id,
            "governance_status": "aligned",
            "governance_note": "采纳建议",
        },
    )
    assert apply_resp.status_code == 200, apply_resp.text

    event = db.query(GovernanceFeedbackEvent).filter(GovernanceFeedbackEvent.suggestion_id == suggestion.id).first()
    assert event is not None
    assert event.event_type == "applied"
    assert event.reward_score == 100

    strategy_key = suggestion.suggested_payload["reinforcement_meta"]["strategy_key"]
    stat = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.strategy_key == strategy_key).first()
    assert stat is not None
    assert stat.total_count >= 1
    assert stat.success_count >= 1


def test_reject_governance_suggestion_records_negative_feedback(client, db):
    admin, _, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    entry.title = "客户运营岗位胜任力训练营"
    entry.content = "这是一份客户运营岗位的能力模型和训练材料"
    db.add(entry)
    db.commit()

    suggest_resp = client.post(f"/api/knowledge-governance/knowledge/{entry.id}/suggest", headers=_auth(token))
    assert suggest_resp.status_code == 200, suggest_resp.text

    suggestion = db.query(GovernanceSuggestionTask).filter(
        GovernanceSuggestionTask.subject_type == "knowledge",
        GovernanceSuggestionTask.subject_id == entry.id,
        GovernanceSuggestionTask.status == "pending",
    ).first()
    assert suggestion is not None

    reject_resp = client.post(
        f"/api/knowledge-governance/suggestions/{suggestion.id}/reject",
        headers=_auth(token),
        json={"note": "这条建议不对"},
    )
    assert reject_resp.status_code == 200, reject_resp.text

    db.refresh(suggestion)
    assert suggestion.status == "rejected"

    event = db.query(GovernanceFeedbackEvent).filter(GovernanceFeedbackEvent.suggestion_id == suggestion.id).first()
    assert event is not None
    assert event.event_type == "rejected"
    assert event.reward_score == -100

    strategy_key = suggestion.suggested_payload["reinforcement_meta"]["strategy_key"]
    stat = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.strategy_key == strategy_key).first()
    assert stat is not None
    assert stat.reject_count >= 1


def test_tune_strategy_stat_can_freeze_and_adjust_bias(client, db):
    admin, _, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    entry.title = "客户运营岗位胜任力训练营"
    entry.content = "这是一份客户运营岗位的能力模型和训练材料"
    db.add(entry)
    db.commit()

    client.post(f"/api/knowledge-governance/knowledge/{entry.id}/suggest", headers=_auth(token))
    suggestion = db.query(GovernanceSuggestionTask).filter(
        GovernanceSuggestionTask.subject_type == "knowledge",
        GovernanceSuggestionTask.subject_id == entry.id,
    ).first()
    strategy_key = suggestion.suggested_payload["reinforcement_meta"]["strategy_key"]

    client.post(
        "/api/knowledge-governance/apply",
        headers=_auth(token),
        json={
            "subject_type": "knowledge",
            "subject_id": entry.id,
            "objective_id": suggestion.objective_id,
            "resource_library_id": suggestion.resource_library_id,
            "governance_status": "aligned",
        },
    )
    stat = db.query(GovernanceStrategyStat).filter(GovernanceStrategyStat.strategy_key == strategy_key).first()
    assert stat is not None

    tune_resp = client.post(
        f"/api/knowledge-governance/strategy-stats/{stat.id}/tune",
        headers=_auth(token),
        json={"is_frozen": True, "manual_bias": 10},
    )
    assert tune_resp.status_code == 200, tune_resp.text

    db.refresh(stat)
    assert stat.is_frozen is True
    assert stat.manual_bias == 10


def test_rebind_object_reduces_previous_object_feedback_score(client, db):
    admin, employee, entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == "customer").first()
    original = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:orig",
        display_name="原对象",
        owner_id=employee.id,
        lifecycle_status="active",
        object_payload={"feedback_score": 6},
    )
    target = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:new",
        display_name="新对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    db.add_all([original, target])
    db.commit()

    entry.governance_object_id = original.id
    db.add(entry)
    db.commit()

    resp = client.post(
        "/api/knowledge-governance/bind-object",
        headers=_auth(token),
        json={
          "subject_type": "knowledge",
          "subject_id": entry.id,
          "governance_object_id": target.id,
        },
    )
    assert resp.status_code == 200, resp.text

    db.refresh(original)
    db.refresh(target)
    assert original.object_payload["feedback_score"] == 4
    assert original.object_payload["rebind_away_count"] == 1
    assert target.object_payload["feedback_score"] >= 2


def test_merge_object_reduces_source_feedback_score(client, db):
    admin, employee, _entry = _seed_admin_and_entry(db)
    token = _login(client, "gov_admin")
    client.post("/api/knowledge-governance/seed-defaults", headers=_auth(token))

    object_type = db.query(GovernanceObjectType).filter(GovernanceObjectType.code == "customer").first()
    target = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:merge-target",
        display_name="主对象",
        owner_id=employee.id,
        lifecycle_status="active",
    )
    source = GovernanceObject(
        object_type_id=object_type.id,
        canonical_key="customer:merge-source",
        display_name="副对象",
        owner_id=employee.id,
        lifecycle_status="active",
        object_payload={"feedback_score": 5},
    )
    db.add_all([target, source])
    db.commit()

    resp = client.post(
        "/api/knowledge-governance/objects/merge",
        headers=_auth(token),
        json={"source_object_id": source.id, "target_object_id": target.id},
    )
    assert resp.status_code == 200, resp.text

    db.refresh(source)
    assert source.object_payload["feedback_score"] == 2
    assert source.object_payload["merged_into"] == target.id
