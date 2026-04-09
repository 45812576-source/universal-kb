"""测试飞书多维表同步 job API：创建 → 轮询 → 状态流转。"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import (
    TestingSessionLocal,
    _make_dept,
    _make_user,
    _make_model_config,
    _login,
    _auth,
)
from app.models.business import TableSyncJob, BusinessTable
from app.models.user import Role


class TestSyncBitableJobs:
    """POST /api/business-tables/sync-bitable/jobs + GET .../jobs/{id}"""

    def _seed(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        db.commit()
        return user

    def test_create_job_returns_job_id(self, client, db):
        """创建 job 应立即返回 job_id，不阻塞。"""
        user = self._seed(db)
        token = _login(client, "admin")

        with patch("app.routers.business_tables.bitable_reader") as mock_reader:
            mock_reader.get_token = AsyncMock(return_value="fake_token")

            resp = client.post(
                "/api/business-tables/sync-bitable/jobs",
                json={
                    "app_token": "test_app_token",
                    "table_id": "tblTestTable",
                    "display_name": "测试表",
                },
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "job_id" in data
        assert "table_id" in data

        # 验证 DB 中 job 存在且状态为 queued
        job = db.query(TableSyncJob).filter(TableSyncJob.id == data["job_id"]).first()
        assert job is not None
        assert job.status == "queued"
        assert job.stage == "queued"

    def test_get_job_status(self, client, db):
        """查询 job 状态。"""
        user = self._seed(db)
        token = _login(client, "admin")

        # 手动建一个 job
        bt = BusinessTable(
            table_name="test_sync_tbl",
            display_name="测试",
            source_type="lark_bitable",
        )
        db.add(bt)
        db.flush()

        job = TableSyncJob(
            table_id=bt.id,
            source_type="lark_bitable",
            job_type="full_sync",
            status="running",
            stage="fetch_records",
            triggered_by=user.id,
        )
        db.add(job)
        db.commit()

        resp = client.get(
            f"/api/business-tables/sync-bitable/jobs/{job.id}",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["stage"] == "fetch_records"

    def test_get_nonexistent_job_404(self, client, db):
        """查询不存在的 job 应 404。"""
        self._seed(db)
        token = _login(client, "admin")

        resp = client.get(
            "/api/business-tables/sync-bitable/jobs/99999",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    def test_get_job_forbidden_for_other_user(self, client, db):
        """非发起人、非管理员不能查看别人的 sync job。"""
        dept = _make_dept(db)
        user1 = _make_user(db, "owner1", Role.SUPER_ADMIN, dept.id)
        user2 = _make_user(db, "viewer2", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()

        bt = BusinessTable(
            table_name="test_auth_tbl",
            display_name="Auth 测试",
            source_type="lark_bitable",
        )
        db.add(bt)
        db.flush()

        job = TableSyncJob(
            table_id=bt.id,
            source_type="lark_bitable",
            job_type="full_sync",
            status="running",
            stage="fetch_records",
            triggered_by=user1.id,
        )
        db.add(job)
        db.commit()

        token2 = _login(client, "viewer2")
        resp = client.get(
            f"/api/business-tables/sync-bitable/jobs/{job.id}",
            headers=_auth(token2),
        )
        assert resp.status_code == 403

    def test_register_table_rejects_cross_user_override(self, db):
        """不同用户同步到同名物理表时，应拒绝覆盖。"""
        from app.services.bitable_sync import bitable_sync

        dept = _make_dept(db)
        user1 = _make_user(db, "user_a", Role.EMPLOYEE, dept.id)
        user2 = _make_user(db, "user_b", Role.EMPLOYEE, dept.id)
        db.commit()

        # user1 先注册一张表
        bitable_sync._register_table(
            db, "shared_table_name", "表1", "app1", "tbl1", "", user1.id,
        )
        db.commit()

        # user2 尝试注册同名表 → 应该抛 ValueError
        import pytest as _pytest
        with _pytest.raises(ValueError, match="已被其他用户占用"):
            bitable_sync._register_table(
                db, "shared_table_name", "表2", "app2", "tbl2", "", user2.id,
            )

    def test_job_stage_transitions_via_full_sync(self, db):
        """验证 full_sync 中 existing_job 的 stage 会更新。"""
        import asyncio
        from app.services.bitable_sync import bitable_sync

        dept = _make_dept(db)
        user = _make_user(db, "sync_user", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        db.commit()

        # 创建 job
        bt = BusinessTable(
            table_name="test_stage_tbl",
            display_name="阶段测试",
            source_type="lark_bitable",
        )
        db.add(bt)
        db.flush()

        job = TableSyncJob(
            table_id=bt.id,
            source_type="lark_bitable",
            job_type="full_sync",
            status="queued",
            stage="queued",
        )
        db.add(job)
        db.commit()

        stages_seen = []

        original_get_token = bitable_sync._get_token
        original_fetch_fields = bitable_sync._fetch_fields
        original_fetch_records = bitable_sync._fetch_records

        async def mock_get_token():
            return "fake"

        async def mock_fetch_fields(token, app_token, table_id):
            stages_seen.append(job.stage)
            return [{"field_name": "名称", "type": 1}]

        async def mock_fetch_records(token, app_token, table_id, since_ts=None):
            stages_seen.append(job.stage)
            return [{"record_id": "r1", "fields": {"名称": "测试"}}], {"effective_page_size": 500}

        bitable_sync._get_token = mock_get_token
        bitable_sync._fetch_fields = mock_fetch_fields
        bitable_sync._fetch_records = mock_fetch_records

        try:
            result = asyncio.get_event_loop().run_until_complete(
                bitable_sync.full_sync(
                    db=db,
                    app_token="app_xxx",
                    table_id="tbl_xxx",
                    table_name="test_stage_tbl",
                    display_name="阶段测试",
                    existing_job=job,
                )
            )
        except Exception:
            # SQLite 不支持 MySQL DDL 语法，expected
            pass
        finally:
            bitable_sync._get_token = original_get_token
            bitable_sync._fetch_fields = original_fetch_fields
            bitable_sync._fetch_records = original_fetch_records

        # fetch_fields 时应该看到 stage=fetch_fields
        # fetch_records 时应该看到 stage=fetch_records
        assert "fetch_fields" in stages_seen
        assert "fetch_records" in stages_seen

    def test_old_sync_endpoint_returns_job_id(self, client, db):
        """老接口 POST /sync-bitable 也应返回 job_id。"""
        self._seed(db)
        token = _login(client, "admin")

        with patch("app.routers.business_tables.bitable_reader") as mock_reader:
            mock_reader.get_token = AsyncMock(return_value="fake_token")

            resp = client.post(
                "/api/business-tables/sync-bitable",
                json={
                    "app_token": "test_app",
                    "table_id": "tblOld",
                    "display_name": "老接口测试",
                },
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "job_id" in data


class TestLarkImportJobs:
    """POST /api/knowledge/import-from-lark/jobs + GET .../jobs/{id}"""

    def _seed(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        _make_model_config(db)
        db.commit()
        return user

    def test_create_import_job(self, client, db):
        """创建飞书导入 job 返回 job_id。"""
        user = self._seed(db)
        token = _login(client, "admin")

        resp = client.post(
            "/api/knowledge/import-from-lark/jobs",
            json={"url": "https://example.feishu.cn/base/abc123"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "job_id" in data

    def test_get_import_job_status(self, client, db):
        """查询导入 job 状态。"""
        from app.models.knowledge_job import KnowledgeJob

        user = self._seed(db)
        token = _login(client, "admin")

        job = KnowledgeJob(
            job_type="lark_import",
            status="running",
            phase="exporting",
            created_by=user.id,
        )
        db.add(job)
        db.commit()

        resp = client.get(
            f"/api/knowledge/import-from-lark/jobs/{job.id}",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["phase"] == "exporting"

    def test_import_job_ownership_check(self, client, db):
        """其他用户不能查看别人的 job。"""
        from app.models.knowledge_job import KnowledgeJob

        dept = _make_dept(db)
        user1 = _make_user(db, "user1", Role.EMPLOYEE, dept.id)
        user2 = _make_user(db, "user2", Role.EMPLOYEE, dept.id)
        _make_model_config(db)
        db.commit()

        job = KnowledgeJob(
            job_type="lark_import",
            status="running",
            phase="exporting",
            created_by=user1.id,
        )
        db.add(job)
        db.commit()

        token2 = _login(client, "user2")
        resp = client.get(
            f"/api/knowledge/import-from-lark/jobs/{job.id}",
            headers=_auth(token2),
        )
        assert resp.status_code == 403
