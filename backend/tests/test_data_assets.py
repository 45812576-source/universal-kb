"""TC-DATA-ASSETS: Phase 1 数据资产测试。

覆盖：
- 新模型 CRUD (DataFolder, TableField, TableSyncJob, SkillTableBinding)
- BusinessTable 扩展字段
- data_assets API endpoints (目录/表列表/详情/移动/画像/绑定/同步)
- field_profiler 基本逻辑
"""
from app.utils.time_utils import utcnow
import pytest
from sqlalchemy import text
from tests.conftest import _make_user, _make_dept, _make_skill, _make_model_config, _login, _auth
from app.models.user import Role
from app.models.business import (
    BusinessTable, DataFolder, TableField, TableSyncJob, SkillTableBinding, TableView,
    SkillDataGrant, TablePermissionPolicy, TableRoleGroup,
)
from app.services.data_asset_access import is_data_asset_table, should_use_asset_safe_default


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


def _create_physical_table(db, table_name: str, columns_sql: str):
    db.execute(text(f"DROP TABLE IF EXISTS `{table_name}`"))
    db.execute(text(f"CREATE TABLE `{table_name}` ({columns_sql})"))
    db.commit()


def _setup_admin(db):
    dept = _make_dept(db)
    admin = _make_user(db, "da_admin", Role.SUPER_ADMIN, dept.id)
    db.commit()
    return admin, dept


class TestDataAssetAccessHelpers:
    def test_legacy_folder_marker_counts_as_data_asset(self, db):
        owner = _make_user(db, "asset_helper_owner", Role.EMPLOYEE)
        bt = _make_business_table(
            db,
            "asset_helper_legacy_folder",
            "旧目录标记表",
            owner.id,
        )
        bt.validation_rules = {"folder_id": 123}
        db.commit()

        assert is_data_asset_table(bt) is True

    def test_blank_unfiled_table_remains_legacy(self, db):
        owner = _make_user(db, "asset_helper_blank_owner", Role.EMPLOYEE)
        bt = _make_business_table(db, "asset_helper_blank", "空白旧表", owner.id)
        db.commit()

        assert is_data_asset_table(bt) is False

    def test_safe_default_skips_admin_owner_and_new_policy(self, db):
        dept = _make_dept(db, "资产 helper 部门")
        owner = _make_user(db, "asset_helper_owner2", Role.EMPLOYEE, dept.id)
        peer = _make_user(db, "asset_helper_peer", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "asset_helper_admin", Role.SUPER_ADMIN, dept.id)
        folder = DataFolder(name="helper共享目录", workspace_scope="company")
        db.add(folder)
        db.flush()
        bt = _make_business_table(db, "asset_helper_safe", "安全默认表", owner.id, folder_id=folder.id)
        db.commit()

        assert should_use_asset_safe_default(peer, bt, has_new_policy=False) is True
        assert should_use_asset_safe_default(owner, bt, has_new_policy=False) is False
        assert should_use_asset_safe_default(admin, bt, has_new_policy=False) is False
        assert should_use_asset_safe_default(peer, bt, has_new_policy=True) is False


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

    def test_employee_can_create_personal_folder(self, client, db):
        dept = _make_dept(db, "销售")
        employee = _make_user(db, "folder_user", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "folder_user")
        resp = client.post("/api/data-assets/folders", headers=_auth(token), json={"name": "我的目录"})
        assert resp.status_code == 200
        folder = db.get(DataFolder, resp.json()["id"])
        assert folder.workspace_scope == "personal"
        assert folder.owner_id == employee.id


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

    def test_personal_table_hidden_from_other_employee(self, client, db):
        dept = _make_dept(db, "运营")
        owner = _make_user(db, "owner_emp", Role.EMPLOYEE, dept.id)
        other = _make_user(db, "other_emp", Role.EMPLOYEE, dept.id)
        folder = DataFolder(name="owner_root", workspace_scope="personal", owner_id=owner.id)
        db.add(folder)
        db.flush()
        _make_business_table(db, "tbl_private_asset", "我的私有表", owner.id, folder_id=folder.id)
        db.commit()

        owner_token = _login(client, "owner_emp")
        other_token = _login(client, "other_emp")
        owner_resp = client.get("/api/data-assets/tables?bucket=mine", headers=_auth(owner_token))
        other_resp = client.get("/api/data-assets/tables", headers=_auth(other_token))

        assert owner_resp.status_code == 200
        assert owner_resp.json()["total"] == 1
        assert other_resp.status_code == 200
        assert other_resp.json()["total"] == 0

    def test_owner_can_move_own_table_to_personal_folder(self, client, db):
        dept = _make_dept(db, "商务")
        owner = _make_user(db, "move_owner", Role.EMPLOYEE, dept.id)
        personal_folder = DataFolder(name="我的数据", workspace_scope="personal", owner_id=owner.id)
        db.add(personal_folder)
        db.flush()
        bt = _make_business_table(db, "tbl_move_owner", "移动测试", owner.id)
        db.commit()

        token = _login(client, "move_owner")
        resp = client.patch(
            f"/api/data-assets/tables/{bt.id}/move",
            headers=_auth(token),
            json={"folder_id": personal_folder.id},
        )
        assert resp.status_code == 200
        db.refresh(bt)
        assert bt.folder_id == personal_folder.id

    def test_company_table_appears_in_shared_bucket_for_other_employee(self, client, db):
        dept = _make_dept(db, "运营")
        owner = _make_user(db, "shared_owner", Role.EMPLOYEE, dept.id)
        other = _make_user(db, "shared_other", Role.EMPLOYEE, dept.id)
        company_folder = DataFolder(name="公司数据", workspace_scope="company")
        db.add(company_folder)
        db.flush()
        _make_business_table(db, "tbl_company_asset", "公司共享表", owner.id, folder_id=company_folder.id)
        db.commit()

        other_token = _login(client, "shared_other")
        resp = client.get("/api/data-assets/tables?bucket=shared", headers=_auth(other_token))

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["table_name"] == "tbl_company_asset"


class TestDataRowsRegression:
    def test_shared_asset_rows_do_not_fallback_to_legacy_scope(self, client, db):
        allowed_dept = _make_dept(db, "资产允许部门")
        owner = _make_user(db, "asset_owner_rows", Role.EMPLOYEE, allowed_dept.id)
        peer = _make_user(db, "asset_peer_rows", Role.EMPLOYEE, allowed_dept.id)
        company_folder = DataFolder(name="共享资产目录", workspace_scope="company")
        db.add(company_folder)
        db.flush()
        _create_physical_table(db, "usr_asset_safe_rows", "id INTEGER PRIMARY KEY, name TEXT")
        db.execute(text("INSERT INTO `usr_asset_safe_rows` (id, name) VALUES (1, 'Visible')"))
        bt = BusinessTable(
            table_name="usr_asset_safe_rows",
            display_name="共享资产行表",
            description="",
            ddl_sql="",
            validation_rules={
                "row_scope": "department",
                "row_department_ids": [allowed_dept.id],
            },
            workflow={},
            owner_id=owner.id,
            folder_id=company_folder.id,
        )
        db.add(bt)
        db.commit()

        peer_token = _login(client, "asset_peer_rows")
        peer_resp = client.get("/api/data/usr_asset_safe_rows/rows", headers=_auth(peer_token))
        assert peer_resp.status_code == 200
        assert peer_resp.json()["total"] == 0
        assert peer_resp.json()["rows"] == []

        owner_token = _login(client, "asset_owner_rows")
        owner_resp = client.get("/api/data/usr_asset_safe_rows/rows", headers=_auth(owner_token))
        assert owner_resp.status_code == 200
        assert owner_resp.json()["total"] == 1
        assert owner_resp.json()["rows"][0]["name"] == "Visible"

    def test_owner_can_read_private_rows(self, client, db):
        dept = _make_dept(db, "客服")
        owner = _make_user(db, "row_owner", Role.EMPLOYEE, dept.id)
        _create_physical_table(db, "usr_private_rows", "id INTEGER PRIMARY KEY, name TEXT")
        db.execute(text("INSERT INTO `usr_private_rows` (id, name) VALUES (1, 'Alice')"))
        bt = BusinessTable(
            table_name="usr_private_rows",
            display_name="私有行表",
            description="",
            ddl_sql="",
            validation_rules={"row_scope": "private"},
            workflow={},
            owner_id=owner.id,
        )
        db.add(bt)
        db.commit()

        token = _login(client, "row_owner")
        resp = client.get("/api/data/usr_private_rows/rows", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["rows"][0]["name"] == "Alice"

    def test_multi_select_rows_are_normalized_roundtrip(self, client, db):
        admin, _ = _setup_admin(db)
        _create_physical_table(db, "usr_multi_rows", "id INTEGER PRIMARY KEY, tags TEXT")
        db.execute(text("INSERT INTO `usr_multi_rows` (id, tags) VALUES (1, '[\"A\", \"B\"]')"))
        bt = BusinessTable(
            table_name="usr_multi_rows",
            display_name="多选行表",
            description="",
            ddl_sql="",
            validation_rules={"row_scope": "private"},
            workflow={},
            owner_id=admin.id,
        )
        db.add(bt)
        db.flush()
        db.add(TableField(
            table_id=bt.id,
            field_name="tags",
            physical_column_name="tags",
            display_name="标签",
            field_type="multi_select",
            enum_values=["A", "B", "C"],
        ))
        db.commit()

        token = _login(client, "da_admin")
        list_resp = client.get("/api/data/usr_multi_rows/rows", headers=_auth(token))
        assert list_resp.status_code == 200
        assert list_resp.json()["rows"][0]["tags"] == ["A", "B"]

        update_resp = client.put(
            "/api/data/usr_multi_rows/rows/1",
            headers=_auth(token),
            json={"data": {"tags": ["C"]}},
        )
        assert update_resp.status_code == 200
        stored = db.execute(text("SELECT tags FROM `usr_multi_rows` WHERE id = 1")).scalar()
        assert stored == "[\"C\"]"

    def test_shared_asset_sample_does_not_fallback_to_legacy_scope(self, client, db):
        allowed_dept = _make_dept(db, "采样允许部门")
        owner = _make_user(db, "asset_owner_sample", Role.EMPLOYEE, allowed_dept.id)
        peer = _make_user(db, "asset_peer_sample", Role.EMPLOYEE, allowed_dept.id)
        company_folder = DataFolder(name="采样共享目录v2", workspace_scope="company")
        db.add(company_folder)
        db.flush()
        _create_physical_table(db, "usr_asset_safe_sample", "id INTEGER PRIMARY KEY, name TEXT")
        db.execute(text("INSERT INTO `usr_asset_safe_sample` (id, name) VALUES (1, 'Visible')"))
        bt = BusinessTable(
            table_name="usr_asset_safe_sample",
            display_name="共享资产采样表",
            description="",
            ddl_sql="",
            validation_rules={
                "row_scope": "department",
                "row_department_ids": [allowed_dept.id],
            },
            workflow={},
            owner_id=owner.id,
            folder_id=company_folder.id,
        )
        db.add(bt)
        db.commit()

        peer_token = _login(client, "asset_peer_sample")
        peer_resp = client.get("/api/data/usr_asset_safe_sample/sample", headers=_auth(peer_token))
        assert peer_resp.status_code == 200
        assert peer_resp.json()["total"] == 0
        assert peer_resp.json()["rows"] == []

        owner_token = _login(client, "asset_owner_sample")
        owner_resp = client.get("/api/data/usr_asset_safe_sample/sample", headers=_auth(owner_token))
        assert owner_resp.status_code == 200
        assert owner_resp.json()["total"] == 1
        assert owner_resp.json()["rows"][0]["name"] == "Visible"

    def test_sample_rows_respects_legacy_department_scope(self, client, db):
        allowed_dept = _make_dept(db, "允许部门")
        denied_dept = _make_dept(db, "其他部门")
        owner = _make_user(db, "sample_owner", Role.EMPLOYEE, allowed_dept.id)
        denied_user = _make_user(db, "sample_denied", Role.EMPLOYEE, denied_dept.id)
        company_folder = DataFolder(name="采样共享目录", workspace_scope="company")
        db.add(company_folder)
        db.flush()
        _create_physical_table(db, "usr_sample_dept", "id INTEGER PRIMARY KEY, name TEXT")
        db.execute(text("INSERT INTO `usr_sample_dept` (id, name) VALUES (1, 'Visible')"))
        bt = BusinessTable(
            table_name="usr_sample_dept",
            display_name="部门采样表",
            description="",
            ddl_sql="",
            validation_rules={
                "row_scope": "department",
                "row_department_ids": [allowed_dept.id],
            },
            workflow={},
            owner_id=owner.id,
            folder_id=company_folder.id,
        )
        db.add(bt)
        db.commit()

        denied_token = _login(client, "sample_denied")
        denied_resp = client.get("/api/data/usr_sample_dept/sample", headers=_auth(denied_token))
        assert denied_resp.status_code == 200
        assert denied_resp.json()["total"] == 0
        assert denied_resp.json()["rows"] == []

        owner_token = _login(client, "sample_owner")
        owner_resp = client.get("/api/data/usr_sample_dept/sample", headers=_auth(owner_token))
        assert owner_resp.status_code == 200
        assert owner_resp.json()["total"] == 1
        assert owner_resp.json()["rows"][0]["name"] == "Visible"


# ═══════════════════════════════════════════════════════════════════════════════
# API tests — Bindings
# ═══════════════════════════════════════════════════════════════════════════════

class TestBindingAPI:
    def test_create_binding(self, client, db):
        admin, _ = _setup_admin(db)
        _make_model_config(db)
        skill = _make_skill(db, admin.id)
        bt = _make_business_table(db, "bind_tbl", "绑定表", admin.id, publish_status="published")
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
        role_group = TableRoleGroup(table_id=bt.id, name="测试角色组", user_ids=[admin.id])
        db.add(role_group)
        db.flush()
        binding = SkillTableBinding(skill_id=skill.id, table_id=bt.id, view_id=view.id,
                                    binding_type="runtime_read", created_by=admin.id)
        db.add(binding)
        db.add(SkillDataGrant(
            skill_id=skill.id,
            table_id=bt.id,
            view_id=view.id,
            grant_mode="runtime_read",
            allowed_actions=["read"],
            max_disclosure_level="L3",
            approval_required=False,
            audit_level="full",
        ))
        db.add(TablePermissionPolicy(
            table_id=bt.id,
            role_group_id=role_group.id,
            view_id=view.id,
            disclosure_level="L3",
            row_access_mode="all",
            field_access_mode="all",
            tool_permission_mode="full",
        ))
        db.commit()
        token = _login(client, "da_admin")
        resp = client.get(f"/api/data-assets/views/{view.id}/impact", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["affected_skills"]) == 1
        assert data["can_delete"] is False
        assert data["binding_count"] == 1
        assert data["grant_count"] == 1
        assert data["policy_count"] == 1


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
    def test_normalize_fields_fills_empty_names(self):
        from app.services.bitable_sync import BitableSync
        sync = BitableSync()
        fields = [
            {"field_name": "文本", "type": 1},
            {"field_name": "", "type": 1},
            {"field_name": "", "type": 17},
            {"field_name": "文本", "type": 3},
        ]

        normalized = sync._normalize_fields(fields)
        col_map = sync._build_col_map(normalized)

        assert [f["field_name"] for f in normalized] == ["文本", "未命名字段2", "未命名字段3", "文本_2"]
        assert normalized[1]["_source_field_name"] == ""
        assert normalized[2]["_source_field_name"] == ""
        assert all(col_map[f["field_name"]] for f in normalized)
        assert len(set(col_map.values())) == len(normalized)
        assert "" not in col_map.values()

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
