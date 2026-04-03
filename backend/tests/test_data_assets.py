"""TC-DATA-ASSETS: Phase 1 数据资产测试。

覆盖：
- 新模型 CRUD (DataFolder, TableField, TableSyncJob, SkillTableBinding)
- BusinessTable 扩展字段
- data_assets API endpoints (目录/表列表/详情/移动/画像/绑定/同步)
- field_profiler 基本逻辑
"""
from app.utils.time_utils import utcnow
import pytest
from tests.conftest import _make_user, _make_dept, _make_skill, _make_model_config, _login, _auth
from app.models.user import Role
from app.models.business import (
    BusinessTable, DataFolder, TableField, TableSyncJob, SkillTableBinding, TableView,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_business_table(db, table_name="test_bt", display_name="测试表", owner_id=1, **kwargs):
    bt = BusinessTable(
        table_name=table_name,
        display_name=display_name,
        description="测试用",
        ddl_sql="CREATE TABLE test_bt (id INT PRIMARY KEY);",
        validation_rules={},
        workflow={},
        owner_id=owner_id,
        **kwargs,
    )
    db.add(bt)
    db.flush()
    return bt


def _make_folder(db, name="测试文件夹", parent_id=None, workspace_scope="company"):
    f = DataFolder(name=name, parent_id=parent_id, workspace_scope=workspace_scope)
    db.add(f)
    db.flush()
    return f


def _setup_admin(db):
    dept = _make_dept(db)
    admin = _make_user(db, "da_admin", Role.SUPER_ADMIN, dept.id)
    db.commit()
    return admin, dept


# ═══════════════════════════════════════════════════════════════════════════════
# Model layer tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataFolderModel:
    def test_create_folder(self, db):
        f = _make_folder(db, "数据中心")
        db.commit()
        assert f.id is not None
        assert f.name == "数据中心"
        assert f.workspace_scope == "company"

    def test_nested_folders(self, db):
        parent = _make_folder(db, "父目录")
        child = _make_folder(db, "子目录", parent_id=parent.id)
        db.commit()
        assert child.parent_id == parent.id
        assert child.parent.id == parent.id

    def test_folder_archive(self, db):
        f = _make_folder(db, "归档测试")
        f.is_archived = True
        db.commit()
        assert db.get(DataFolder, f.id).is_archived is True


class TestTableFieldModel:
    def test_create_field(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "tf_test", "字段测试表", admin.id)
        tf = TableField(
            table_id=bt.id,
            field_name="客户名称",
            display_name="客户名称",
            field_type="text",
            is_nullable=True,
        )
        db.add(tf)
        db.commit()
        assert tf.id is not None
        assert tf.table.id == bt.id

    def test_field_with_enum(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "tf_enum", "枚举测试", admin.id)
        tf = TableField(
            table_id=bt.id,
            field_name="状态",
            field_type="single_select",
            enum_values=["待跟进", "已签约", "已流失"],
            enum_source="source_declared",
        )
        db.add(tf)
        db.commit()
        loaded = db.get(TableField, tf.id)
        assert loaded.enum_values == ["待跟进", "已签约", "已流失"]
        assert loaded.enum_source == "source_declared"


class TestTableSyncJobModel:
    def test_create_sync_job(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "sj_test", "同步测试", admin.id)
        job = TableSyncJob(
            table_id=bt.id,
            source_type="lark_bitable",
            job_type="full_sync",
            status="queued",
            trigger_source="manual",
        )
        db.add(job)
        db.commit()
        assert job.id is not None
        assert job.status == "queued"

    def test_sync_job_lifecycle(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "sj_life", "生命周期测试", admin.id)
        job = TableSyncJob(
            table_id=bt.id, source_type="lark_bitable", job_type="full_sync",
            status="running", started_at=utcnow(),
        )
        db.add(job)
        db.flush()
        job.status = "success"
        job.finished_at = utcnow()
        job.stats = {"inserted": 100, "updated": 5}
        db.commit()
        loaded = db.get(TableSyncJob, job.id)
        assert loaded.status == "success"
        assert loaded.stats["inserted"] == 100


class TestSkillTableBindingModel:
    def test_create_binding(self, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "stb_test", "绑定测试", admin.id)
        binding = SkillTableBinding(
            skill_id=skill.id,
            table_id=bt.id,
            binding_type="runtime_read",
            created_by=admin.id,
        )
        db.add(binding)
        db.commit()
        assert binding.id is not None
        assert binding.skill.id == skill.id
        assert binding.table.id == bt.id


class TestBusinessTableExtensions:
    def test_new_fields_default(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "ext_test", "扩展测试", admin.id)
        db.commit()
        assert bt.source_type == "blank"
        assert bt.sync_status == "idle"
        assert bt.field_profile_status == "pending"
        assert bt.is_archived is False

    def test_folder_relationship(self, db):
        admin, _ = _setup_admin(db)
        folder = _make_folder(db, "关联文件夹")
        bt = _make_business_table(db, "ext_folder", "文件夹关联", admin.id, folder_id=folder.id)
        db.commit()
        assert bt.folder.id == folder.id

    def test_lark_source(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "lark_src", "飞书表", admin.id,
                                  source_type="lark_bitable",
                                  source_ref={"app_token": "abc", "table_id": "tbl123"})
        db.commit()
        assert bt.source_ref["app_token"] == "abc"


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Folders
# ═══════════════════════════════════════════════════════════════════════════════

class TestFolderAPI:
    def test_list_folders_empty(self, client, db):
        admin, _ = _setup_admin(db)
        token = _login(client, "da_admin")
        resp = client.get("/api/data-assets/folders", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_create_folder(self, client, db):
        admin, _ = _setup_admin(db)
        token = _login(client, "da_admin")
        resp = client.post("/api/data-assets/folders", headers=_auth(token),
                           json={"name": "新文件夹"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新文件夹"
        assert data["id"] > 0

    def test_create_nested_folder(self, client, db):
        admin, _ = _setup_admin(db)
        token = _login(client, "da_admin")
        r1 = client.post("/api/data-assets/folders", headers=_auth(token),
                         json={"name": "父"})
        parent_id = r1.json()["id"]
        r2 = client.post("/api/data-assets/folders", headers=_auth(token),
                         json={"name": "子", "parent_id": parent_id})
        assert r2.status_code == 200
        child_id = r2.json()["id"]
        # Verify via folder tree
        child = db.get(DataFolder, child_id)
        assert child.parent_id == parent_id

    def test_rename_folder(self, client, db):
        admin, _ = _setup_admin(db)
        folder = _make_folder(db, "旧名")
        db.commit()
        token = _login(client, "da_admin")
        resp = client.patch(f"/api/data-assets/folders/{folder.id}", headers=_auth(token),
                            json={"name": "新名"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        db.refresh(folder)
        assert folder.name == "新名"

    def test_delete_folder(self, client, db):
        admin, _ = _setup_admin(db)
        folder = _make_folder(db, "删除测试")
        db.commit()
        token = _login(client, "da_admin")
        resp = client.delete(f"/api/data-assets/folders/{folder.id}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Tables
# ═══════════════════════════════════════════════════════════════════════════════

class TestTableAPI:
    def test_list_tables_enriched(self, client, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "tbl_list", "列表测试", admin.id)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get("/api/data-assets/tables", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        tables = data["items"]
        assert len(tables) >= 1
        t = next(t for t in tables if t["id"] == bt.id)
        assert "field_count" in t
        assert "bound_skills" in t

    def test_list_tables_filter_by_folder(self, client, db):
        admin, _ = _setup_admin(db)
        folder = _make_folder(db, "筛选文件夹")
        bt1 = _make_business_table(db, "in_folder", "在文件夹", admin.id, folder_id=folder.id)
        bt2 = _make_business_table(db, "no_folder", "无文件夹", admin.id)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/tables?folder_id={folder.id}", headers=_auth(token))
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["items"]]
        assert bt1.id in ids
        assert bt2.id not in ids

    def test_get_table_detail(self, client, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "tbl_detail", "详情测试", admin.id)
        # 添加一些字段
        tf = TableField(table_id=bt.id, field_name="名称", field_type="text", sort_order=0)
        db.add(tf)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/tables/{bt.id}", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == bt.id
        assert "fields" in data
        assert len(data["fields"]) == 1
        assert data["fields"][0]["field_name"] == "名称"
        assert "views" in data
        assert "bindings" in data
        assert "recent_sync_jobs" in data

    def test_get_table_not_found(self, client, db):
        admin, _ = _setup_admin(db)
        token = _login(client, "da_admin")
        resp = client.get("/api/data-assets/tables/99999", headers=_auth(token))
        assert resp.status_code == 404

    def test_move_table(self, client, db):
        admin, _ = _setup_admin(db)
        folder = _make_folder(db, "目标文件夹")
        bt = _make_business_table(db, "tbl_move", "移动测试", admin.id)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.patch(f"/api/data-assets/tables/{bt.id}/move", headers=_auth(token),
                            json={"folder_id": folder.id})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        db.refresh(bt)
        assert bt.folder_id == folder.id


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Bindings
# ═══════════════════════════════════════════════════════════════════════════════

class TestBindingAPI:
    def test_create_binding(self, client, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "bind_tbl", "绑定表", admin.id)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.post("/api/data-assets/bindings", headers=_auth(token), json={
            "skill_id": skill.id,
            "table_id": bt.id,
            "binding_type": "runtime_read",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_id"] == skill.id
        assert data["table_id"] == bt.id

    def test_list_bindings(self, client, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "bind_list", "列表绑定", admin.id)
        binding = SkillTableBinding(skill_id=skill.id, table_id=bt.id, binding_type="runtime_read", created_by=admin.id)
        db.add(binding)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/tables/{bt.id}/bindings", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) >= 1

    def test_delete_binding(self, client, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "bind_del", "删除绑定", admin.id)
        binding = SkillTableBinding(skill_id=skill.id, table_id=bt.id, binding_type="runtime_read", created_by=admin.id)
        db.add(binding)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.delete(f"/api/data-assets/bindings/{binding.id}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Sync jobs
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncJobAPI:
    def test_list_sync_jobs(self, client, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "sj_api", "同步API", admin.id)
        job = TableSyncJob(table_id=bt.id, source_type="lark_bitable", job_type="full_sync", status="success")
        db.add(job)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/tables/{bt.id}/sync-jobs", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) == 1

    def test_sync_requires_lark_source(self, client, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "sj_no_lark", "无飞书", admin.id, source_type="blank")
        db.commit()
        token = _login(client, "da_admin")
        resp = client.post(f"/api/data-assets/tables/{bt.id}/sync", headers=_auth(token))
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Profile
# ═══════════════════════════════════════════════════════════════════════════════

class TestProfileAPI:
    def test_get_profile(self, client, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "prof_tbl", "画像表", admin.id)
        tf = TableField(table_id=bt.id, field_name="金额", field_type="number",
                        distinct_count_cache=50, null_ratio=0.1, sample_values=["100", "200"])
        db.add(tf)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/tables/{bt.id}/profile", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["profile_status"] == "pending"
        assert len(data["field_profiles"]) == 1
        assert data["field_profiles"][0]["field_name"] == "金额"
        assert data["field_profiles"][0]["distinct_count"] == 50


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Views impact
# ═══════════════════════════════════════════════════════════════════════════════

class TestViewImpactAPI:
    def test_view_impact_check(self, client, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "vi_tbl", "视图影响", admin.id)
        view = TableView(table_id=bt.id, name="测试视图", view_type="grid", config={}, created_by=admin.id)
        db.add(view)
        db.flush()
        binding = SkillTableBinding(skill_id=skill.id, table_id=bt.id, view_id=view.id,
                                    binding_type="runtime_read", created_by=admin.id)
        db.add(binding)
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/views/{view.id}/impact", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["affected_skills"]) == 1
        assert data["can_delete"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# field_profiler unit test
# ═══════════════════════════════════════════════════════════════════════════════

class TestFieldProfilerUnit:
    def test_infer_capabilities(self):
        from app.services.field_profiler import _infer_field_capabilities
        caps = _infer_field_capabilities("single_select")
        assert caps["is_groupable"] is True
        assert caps["is_filterable"] is True

        caps2 = _infer_field_capabilities("attachment")
        assert caps2["is_filterable"] is False
        assert caps2["is_sortable"] is False

    def test_mysql_type_mapping(self):
        from app.services.field_profiler import _mysql_type_to_field_type
        assert _mysql_type_to_field_type("int") == "number"
        assert _mysql_type_to_field_type("varchar") == "text"
        assert _mysql_type_to_field_type("datetime") == "datetime"
        assert _mysql_type_to_field_type("json") == "json"


# ═══════════════════════════════════════════════════════════════════════════════
# bitable_sync schema persistence test
# ═══════════════════════════════════════════════════════════════════════════════

class TestBitableSyncSchema:
    def test_persist_schema_fields(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "schema_test", "Schema测试", admin.id, source_type="lark_bitable")
        db.commit()

        from app.services.bitable_sync import BitableSync
        sync = BitableSync()
        fields = [
            {"field_name": "客户名称", "type": 1, "property": {}, "description": "客户全称"},
            {"field_name": "状态", "type": 3, "property": {"options": [{"name": "待跟进"}, {"name": "已签约"}]}},
            {"field_name": "负责人", "type": 11, "property": {}},
        ]
        sync._persist_schema_fields(db, bt, fields)
        db.commit()

        persisted = db.query(TableField).filter(TableField.table_id == bt.id).order_by(TableField.sort_order).all()
        assert len(persisted) == 3
        assert persisted[0].field_name == "客户名称"
        assert persisted[0].field_type == "text"
        assert persisted[0].description == "客户全称"
        assert persisted[1].field_name == "状态"
        assert persisted[1].field_type == "single_select"
        assert persisted[1].enum_values == ["待跟进", "已签约"]
        assert persisted[1].enum_source == "source_declared"
        assert persisted[2].field_type == "person"

    def test_persist_removes_deleted_fields(self, db):
        admin, _ = _setup_admin(db)
        bt = _make_business_table(db, "schema_del", "删除字段测试", admin.id, source_type="lark_bitable")
        # 预存一个旧字段
        old = TableField(table_id=bt.id, field_name="旧字段", field_type="text")
        db.add(old)
        db.commit()

        from app.services.bitable_sync import BitableSync
        sync = BitableSync()
        # 同步时不再包含 "旧字段"
        fields = [{"field_name": "新字段", "type": 1, "property": {}}]
        sync._persist_schema_fields(db, bt, fields)
        db.commit()

        remaining = db.query(TableField).filter(TableField.table_id == bt.id).all()
        names = [f.field_name for f in remaining]
        assert "新字段" in names
        assert "旧字段" not in names
