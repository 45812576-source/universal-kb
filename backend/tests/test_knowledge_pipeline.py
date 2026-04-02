"""知识处理流水线测试：模型、上传入队、worker、分类器向量候选、重试接口。"""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from tests.conftest import (
    TestingSessionLocal,
    _auth,
    _login,
    _make_dept,
    _make_model_config,
    _make_user,
    override_get_db,
)

from app.database import get_db
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage
from app.models.knowledge_job import KnowledgeJob
from app.models.user import Role


# ── 模型测试 ──────────────────────────────────────────────────────────────────

class TestKnowledgeJobModel:
    def test_create_job(self, db: Session):
        dept = _make_dept(db)
        user = _make_user(db, role=Role.SUPER_ADMIN, dept_id=dept.id)
        entry = KnowledgeEntry(
            title="测试文档",
            content="测试内容",
            created_by=user.id,
            department_id=dept.id,
            source_type="upload",
        )
        db.add(entry)
        db.flush()

        job = KnowledgeJob(
            knowledge_id=entry.id,
            job_type="render",
            trigger_source="upload",
        )
        db.add(job)
        db.commit()

        assert job.id is not None
        assert job.status == "queued"
        assert job.attempt_count == 0
        assert job.max_attempts == 3

    def test_create_classify_job(self, db: Session):
        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        entry = KnowledgeEntry(
            title="分类测试",
            content="关于抖音投放策略的文档",
            created_by=user.id,
            department_id=dept.id,
        )
        db.add(entry)
        db.flush()

        job = KnowledgeJob(
            knowledge_id=entry.id,
            job_type="classify",
            trigger_source="scheduled",
        )
        db.add(job)
        db.commit()

        assert job.job_type == "classify"
        assert job.trigger_source == "scheduled"


class TestKnowledgeEntryClassificationFields:
    def test_new_fields_exist(self, db: Session):
        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        entry = KnowledgeEntry(
            title="字段测试",
            content="内容",
            created_by=user.id,
            department_id=dept.id,
            classification_status="pending",
            classification_source="keyword",
        )
        db.add(entry)
        db.commit()

        fetched = db.get(KnowledgeEntry, entry.id)
        assert fetched.classification_status == "pending"
        assert fetched.classification_source == "keyword"
        assert fetched.classification_error is None
        assert fetched.classified_at is None

    def test_update_classification_status(self, db: Session):
        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        entry = KnowledgeEntry(
            title="状态更新测试",
            content="内容",
            created_by=user.id,
            department_id=dept.id,
        )
        db.add(entry)
        db.flush()

        entry.classification_status = "success"
        entry.classification_source = "vector_assisted_llm"
        entry.classified_at = datetime.datetime.utcnow()
        entry.classification_confidence = 0.92
        db.commit()

        fetched = db.get(KnowledgeEntry, entry.id)
        assert fetched.classification_status == "success"
        assert fetched.classification_source == "vector_assisted_llm"
        assert fetched.classified_at is not None


# ── Worker 测试 ───────────────────────────────────────────────────────────────

class TestKnowledgeWorker:
    def _seed_entry_and_job(self, db: Session, job_type="classify"):
        dept = _make_dept(db)
        user = _make_user(db, role=Role.SUPER_ADMIN, dept_id=dept.id)
        entry = KnowledgeEntry(
            title="Worker测试文档",
            content="关于小红书种草策略的深度分析报告",
            created_by=user.id,
            department_id=dept.id,
            source_type="upload",
            classification_status="pending",
        )
        db.add(entry)
        db.flush()

        job = KnowledgeJob(
            knowledge_id=entry.id,
            job_type=job_type,
            trigger_source="upload",
        )
        db.add(job)
        db.commit()
        return entry, job

    @patch("app.services.knowledge_worker.SessionLocal")
    def test_process_classify_job_success(self, mock_session_cls):
        """classify job 成功时 status=success, entry.classification_status=success"""
        db = TestingSessionLocal()
        # worker 内部 db.close() 会脱离对象，用 expunge 后重新 query 验证
        mock_session_cls.return_value = db

        entry, job = self._seed_entry_and_job(db, "classify")
        entry_id, job_id = entry.id, job.id

        mock_result = MagicMock()
        mock_result.taxonomy_code = "A1.1"
        mock_result.taxonomy_board = "A"
        mock_result.taxonomy_path = ["A.渠道与平台"]
        mock_result.storage_layer = "L2"
        mock_result.target_kb_ids = []
        mock_result.serving_skill_codes = []
        mock_result.reasoning = "测试"
        mock_result.confidence = 0.85
        mock_result.stage = "llm"

        with patch("app.services.knowledge_worker.asyncio") as mock_asyncio:
            mock_loop = MagicMock()
            mock_asyncio.new_event_loop.return_value = mock_loop
            mock_loop.run_until_complete.return_value = mock_result

            from app.services.knowledge_worker import process_knowledge_jobs
            process_knowledge_jobs()

        # worker 已 close db，用新 session 查
        db2 = TestingSessionLocal()
        job = db2.get(KnowledgeJob, job_id)
        entry = db2.get(KnowledgeEntry, entry_id)

        assert job.status == "success"
        assert job.attempt_count == 1
        assert entry.classification_status == "success"
        assert entry.taxonomy_code == "A1.1"
        assert entry.classification_source == "llm"
        assert entry.classified_at is not None
        db2.close()

    @patch("app.services.knowledge_worker.SessionLocal")
    def test_process_classify_job_failure_retries(self, mock_session_cls):
        """classify job 失败时如果 attempt_count < max_attempts 放回队列"""
        db = TestingSessionLocal()
        mock_session_cls.return_value = db

        entry, job = self._seed_entry_and_job(db, "classify")
        entry_id, job_id = entry.id, job.id

        with patch("app.services.knowledge_worker.asyncio") as mock_asyncio:
            mock_loop = MagicMock()
            mock_asyncio.new_event_loop.return_value = mock_loop
            mock_loop.run_until_complete.side_effect = RuntimeError("LLM down")

            from app.services.knowledge_worker import process_knowledge_jobs
            process_knowledge_jobs()

        db2 = TestingSessionLocal()
        job = db2.get(KnowledgeJob, job_id)
        entry = db2.get(KnowledgeEntry, entry_id)

        # 第一次失败，attempt_count=1 < max_attempts=3，放回队列
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert entry.classification_status == "failed"
        assert "LLM down" in (entry.classification_error or "")
        db2.close()

    @patch("app.services.knowledge_worker.SessionLocal")
    def test_process_render_job_success(self, mock_session_cls):
        """render job 成功"""
        db = TestingSessionLocal()
        mock_session_cls.return_value = db

        entry, job = self._seed_entry_and_job(db, "render")
        entry.doc_render_status = "failed"
        entry.oss_key = "some/key.docx"
        entry.file_ext = ".docx"
        db.commit()
        entry_id, job_id = entry.id, job.id

        with patch("app.services.knowledge_worker._run_render_job") as mock_render:
            def side_effect(db_, j, e):
                j.status = "success"
                e.doc_render_status = "ready"
            mock_render.side_effect = side_effect

            from app.services.knowledge_worker import process_knowledge_jobs
            process_knowledge_jobs()

        db2 = TestingSessionLocal()
        job = db2.get(KnowledgeJob, job_id)
        assert job.status == "success"
        db2.close()

    @patch("app.services.knowledge_worker.SessionLocal")
    def test_process_no_jobs(self, mock_session_cls):
        """无 queued job 时不报错"""
        db = TestingSessionLocal()
        mock_session_cls.return_value = db

        from app.services.knowledge_worker import process_knowledge_jobs
        process_knowledge_jobs()  # should not raise

    @patch("app.services.knowledge_worker.SessionLocal")
    def test_backfill_unclassified(self, mock_session_cls):
        """backfill 为未分类条目创建 classify job"""
        db = TestingSessionLocal()
        mock_session_cls.return_value = db

        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        # 无 taxonomy_code 且 classification_status=None
        entry = KnowledgeEntry(
            title="未分类文档",
            content="某些内容",
            created_by=user.id,
            department_id=dept.id,
        )
        db.add(entry)
        db.commit()
        entry_id = entry.id

        from app.services.knowledge_worker import backfill_unclassified
        backfill_unclassified()

        db2 = TestingSessionLocal()
        jobs = db2.query(KnowledgeJob).filter(
            KnowledgeJob.knowledge_id == entry_id,
            KnowledgeJob.job_type == "classify",
        ).all()
        assert len(jobs) == 1
        assert jobs[0].trigger_source == "scheduled"
        db2.close()


# ── 向量候选辅助测试 ──────────────────────────────────────────────────────────

class TestVectorCandidates:
    def test_get_vector_candidates_success(self, db: Session):
        """有相似已分类文档时返回候选"""
        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        # 创建已分类的知识条目
        for i, code in enumerate(["A1.1", "A1.1", "B2.3"]):
            e = KnowledgeEntry(
                title=f"已分类文档{i}",
                content=f"内容{i}",
                created_by=user.id,
                department_id=dept.id,
                taxonomy_code=code,
                taxonomy_board=code[0],
                taxonomy_path=[f"{code[0]}.板块"],
            )
            db.add(e)
        db.commit()

        # mock vector_service.search_knowledge 返回这些 knowledge_id
        all_entries = db.query(KnowledgeEntry).all()
        mock_hits = [
            {"knowledge_id": e.id, "score": 0.8, "chunk_index": 0, "text": "text"}
            for e in all_entries
        ]

        with patch("app.services.vector_service.search_knowledge", return_value=mock_hits):
            from app.services.knowledge_classifier import _get_vector_candidates
            candidates = _get_vector_candidates("抖音投放", db)

        assert len(candidates) >= 1
        # A1.1 出现 2 次应排第一
        assert candidates[0]["taxonomy_code"] == "A1.1"
        assert candidates[0]["similar_count"] == 2

    def test_get_vector_candidates_milvus_down(self, db: Session):
        """向量搜索失败时返回空"""
        with patch("app.services.vector_service.search_knowledge", side_effect=RuntimeError("Milvus down")):
            from app.services.knowledge_classifier import _get_vector_candidates
            candidates = _get_vector_candidates("something", db)

        assert candidates == []

    def test_get_vector_candidates_no_classified(self, db: Session):
        """向量搜索有结果但无已分类文档时返回空"""
        dept = _make_dept(db)
        user = _make_user(db, role=Role.EMPLOYEE, dept_id=dept.id)
        # 创建未分类的条目
        e = KnowledgeEntry(
            title="未分类",
            content="内容",
            created_by=user.id,
            department_id=dept.id,
        )
        db.add(e)
        db.commit()

        mock_hits = [{"knowledge_id": e.id, "score": 0.9, "chunk_index": 0, "text": "t"}]

        with patch("app.services.vector_service.search_knowledge", return_value=mock_hits):
            from app.services.knowledge_classifier import _get_vector_candidates
            candidates = _get_vector_candidates("test", db)

        assert candidates == []


# ── API 接口测试 ──────────────────────────────────────────────────────────────

class TestKnowledgeAPIs:
    @pytest.fixture(autouse=True)
    def setup_api(self, db, client):
        from app.routers import auth, knowledge

        test_app = FastAPI(title="Knowledge Pipeline Test API")
        test_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        test_app.include_router(auth.router)
        test_app.include_router(knowledge.router)
        test_app.dependency_overrides[get_db] = override_get_db
        self._dept = _make_dept(db, name=f"API测试部门")
        self._user = _make_user(
            db, username="apitest", role=Role.SUPER_ADMIN, dept_id=self._dept.id
        )
        db.commit()
        from fastapi.testclient import TestClient

        with TestClient(test_app, raise_server_exceptions=True) as local_client:
            self._client = local_client
            self._token = _login(local_client, "apitest")
            yield
        test_app.dependency_overrides.clear()

    def test_classify_retry(self, db):
        entry = KnowledgeEntry(
            title="需要重分类",
            content="内容",
            created_by=self._user.id,
            department_id=self._dept.id,
            classification_status="failed",
        )
        db.add(entry)
        db.commit()

        resp = self._client.post(
            f"/api/knowledge/{entry.id}/classify",
            headers=_auth(self._token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "job_id" in data

        job = db.query(KnowledgeJob).filter(
            KnowledgeJob.knowledge_id == entry.id,
            KnowledgeJob.job_type == "classify",
        ).first()
        assert job is not None
        assert job.trigger_source == "retry"

        db.refresh(entry)
        assert entry.classification_status == "pending"

    def test_render_retry_creates_job(self, db):
        entry = KnowledgeEntry(
            title="渲染失败",
            content="内容",
            created_by=self._user.id,
            department_id=self._dept.id,
            doc_render_status="failed",
        )
        db.add(entry)
        db.commit()

        resp = self._client.post(
            f"/api/knowledge/{entry.id}/render",
            headers=_auth(self._token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "queued"

        job = db.query(KnowledgeJob).filter(
            KnowledgeJob.knowledge_id == entry.id,
            KnowledgeJob.job_type == "render",
        ).first()
        assert job is not None

    def test_list_filter_classification_status(self, db):
        for status in ["success", "failed", "pending"]:
            e = KnowledgeEntry(
                title=f"状态{status}",
                content="内容",
                created_by=self._user.id,
                department_id=self._dept.id,
                classification_status=status,
                status=KnowledgeStatus.APPROVED,
                review_stage=ReviewStage.APPROVED,
            )
            db.add(e)
        db.commit()

        resp = self._client.get(
            "/api/knowledge?classification_status=failed",
            headers=_auth(self._token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["classification_status"] == "failed"

    def test_entry_dict_has_new_fields(self, db):
        entry = KnowledgeEntry(
            title="详情测试",
            content="内容详情",
            created_by=self._user.id,
            department_id=self._dept.id,
            classification_status="needs_review",
            classification_confidence=0.4,
            classification_source="vector_assisted_llm",
        )
        db.add(entry)
        db.commit()

        resp = self._client.get(
            f"/api/knowledge/{entry.id}",
            headers=_auth(self._token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["classification_status"] == "needs_review"
        assert data["classification_confidence"] == 0.4
        assert data["classification_source"] == "vector_assisted_llm"
        assert data["can_retry_classification"] is True
