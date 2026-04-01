"""Tests for Opencode × Le Desk data view bridge — v4 spec compliance.

Covers:
1. ensure_default_view: v4 §4 安全条件（字段画像、同步状态、标签过滤、ceiling 规则）
2. assess_view_availability: v4 §7.1 view_state 状态分类
3. ViewReadResult 三种输出模型: rows/aggregates/mixed (v4 §2.3)
4. limit 优先级: 权限引擎→view.row_limit→调用参数 (v4 §2.4)
5. data_table_reader: 禁止普通用户整表 fallback (v4 §1.1/§8)
6. 视图失效检测 revalidate_view (v4 §5.2)
7. 列表/详情 API: 分组格式 + view_state + 后端二次校验 (v4 §7)
"""
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import (
    _make_dept, _make_user, _login, _auth,
)
from app.models.user import Role
from app.models.business import (
    BusinessTable, TableField, TableView,
    TableRoleGroup, TablePermissionPolicy,
)
from app.services.data_view_runtime import (
    ViewReadResult, ViewAvailability,
    assess_view_availability, execute_view_read,
    revalidate_view, revalidate_table_views,
)
from app.services.policy_engine import PolicyResult
from app.routers.data_assets import ensure_default_view


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_table(db, name="test_table", display="测试表", **kwargs):
    defaults = dict(
        table_name=name,
        display_name=display,
        source_type="blank",
        owner_id=1,
        field_profile_status="ready",
    )
    defaults.update(kwargs)
    bt = BusinessTable(**defaults)
    db.add(bt)
    db.flush()
    return bt


def _make_fields(db, table_id, count=3, **field_kwargs):
    fields = []
    for i in range(count):
        defaults = dict(
            table_id=table_id,
            field_name=f"field_{i}",
            display_name=f"字段{i}",
            physical_column_name=f"field_{i}",
            field_type="text",
            sort_order=i,
            field_role_tags=["dimension"],
        )
        defaults.update(field_kwargs)
        f = TableField(**defaults)
        db.add(f)
        db.flush()
        fields.append(f)
    return fields


def _make_view(db, table_id, name="测试视图", field_ids=None, **kwargs):
    defaults = dict(
        table_id=table_id,
        name=name,
        view_purpose="explore",
        view_kind="list",
        visible_field_ids=field_ids or [],
        is_system=False,
        is_default=False,
        view_state="available",
    )
    defaults.update(kwargs)
    v = TableView(**defaults)
    db.add(v)
    db.flush()
    return v


def _make_role_group(db, table_id, user_ids=None, skill_ids=None):
    rg = TableRoleGroup(
        table_id=table_id,
        name="测试角色组",
        group_type="human_role",
        user_ids=user_ids or [],
        skill_ids=skill_ids or [],
    )
    db.add(rg)
    db.flush()
    return rg


def _make_policy(db, table_id, role_group_id, view_id=None, disclosure="L4", **kwargs):
    defaults = dict(
        table_id=table_id,
        role_group_id=role_group_id,
        view_id=view_id,
        disclosure_level=disclosure,
        row_access_mode="all",
        field_access_mode="all",
        tool_permission_mode="full",
    )
    defaults.update(kwargs)
    p = TablePermissionPolicy(**defaults)
    db.add(p)
    db.flush()
    return p


# ─── Test: ensure_default_view v4 §4 ─────────────────────────────────────────

class TestEnsureDefaultView:

    def test_creates_default_view_with_dimension_metric_fields(self, db):
        """v4 §4.2: 只纳入 dimension/metric 字段"""
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 3)  # default tags=["dimension"]
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert view is not None
        assert view.name == "默认视图"
        assert view.is_system is True
        assert view.is_default is True
        assert view.disclosure_ceiling == "L3"
        assert view.view_state == "available"

    def test_excludes_sensitive_fields_sets_l2(self, db):
        """v4 §4.2/§4.3: S3/S4 字段排除 → ceiling=L2"""
        bt = _make_table(db)
        f_dim = TableField(
            table_id=bt.id, field_name="dept", display_name="部门",
            physical_column_name="dept", field_type="text", sort_order=0,
            field_role_tags=["dimension"],
        )
        f_sensitive = TableField(
            table_id=bt.id, field_name="salary", display_name="薪资",
            physical_column_name="salary", field_type="number", sort_order=1,
            is_sensitive=True, field_role_tags=["metric"],
        )
        db.add_all([f_dim, f_sensitive])
        db.flush()
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert view is not None
        assert f_dim.id in view.visible_field_ids
        assert f_sensitive.id not in view.visible_field_ids
        assert view.disclosure_ceiling == "L2"

    def test_excludes_identifier_fields_sets_l3(self, db):
        """v4 §4.2: identifier 排除"""
        bt = _make_table(db)
        f_dim = TableField(
            table_id=bt.id, field_name="dept", display_name="部门",
            physical_column_name="dept", field_type="text", sort_order=0,
            field_role_tags=["dimension"],
        )
        f_id = TableField(
            table_id=bt.id, field_name="emp_id", display_name="工号",
            physical_column_name="emp_id", field_type="text", sort_order=1,
            field_role_tags=["identifier"],
        )
        db.add_all([f_dim, f_id])
        db.flush()
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert f_dim.id in view.visible_field_ids
        assert f_id.id not in view.visible_field_ids
        assert view.disclosure_ceiling == "L3"

    def test_excludes_tagless_fields(self, db):
        """v4 §4.2: 标签缺失字段默认排除"""
        bt = _make_table(db)
        f_tagged = TableField(
            table_id=bt.id, field_name="metric1", display_name="指标1",
            physical_column_name="metric1", field_type="number", sort_order=0,
            field_role_tags=["metric"],
        )
        f_tagless = TableField(
            table_id=bt.id, field_name="unknown", display_name="未标注",
            physical_column_name="unknown", field_type="text", sort_order=1,
            field_role_tags=[],
        )
        db.add_all([f_tagged, f_tagless])
        db.flush()
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert view is not None
        assert f_tagged.id in view.visible_field_ids
        assert f_tagless.id not in view.visible_field_ids

    def test_never_allows_l4(self, db):
        """v4 §4.3: 默认不允许 L4"""
        bt = _make_table(db)
        _make_fields(db, bt.id, 3)
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert view.disclosure_ceiling in ("L2", "L3")

    def test_requires_field_profile_ready(self, db):
        """v4 §4.1: 字段画像未完成 → 不生成"""
        bt = _make_table(db, field_profile_status="pending")
        _make_fields(db, bt.id, 3)
        db.commit()

        view = ensure_default_view(db, bt.id)
        assert view is None

    def test_requires_sync_not_failed(self, db):
        """v4 §4.1: 同步失败 → 不生成"""
        bt = _make_table(db, sync_status="failed")
        _make_fields(db, bt.id, 3)
        db.commit()

        view = ensure_default_view(db, bt.id)
        assert view is None

    def test_skips_if_any_view_exists(self, db):
        """v4 §4.1: 已有任何现有视图 → 不生成新的（但更新已有默认）"""
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        # 创建一个非默认视图
        _make_view(db, bt.id, name="自定义视图", field_ids=[fields[0].id])
        db.commit()

        view = ensure_default_view(db, bt.id)
        assert view is None  # 有其他视图存在，不自动生成默认

    def test_updates_existing_default_view(self, db):
        """已有默认视图 → 同步更新 ceiling"""
        bt = _make_table(db)
        f_dim = TableField(
            table_id=bt.id, field_name="dept", display_name="部门",
            physical_column_name="dept", field_type="text", sort_order=0,
            field_role_tags=["dimension"],
        )
        f_sens = TableField(
            table_id=bt.id, field_name="phone", display_name="电话",
            physical_column_name="phone", field_type="text", sort_order=1,
            is_sensitive=True, field_role_tags=["identifier"],
        )
        db.add_all([f_dim, f_sens])
        db.flush()
        existing = _make_view(
            db, bt.id, name="默认视图",
            field_ids=[f_dim.id, f_sens.id],
            is_system=True, is_default=True,
            disclosure_ceiling=None,
        )
        db.commit()

        result = ensure_default_view(db, bt.id)
        db.commit()

        assert result.id == existing.id
        assert result.disclosure_ceiling == "L2"
        assert f_sens.id not in result.visible_field_ids

    def test_returns_none_when_no_fields(self, db):
        bt = _make_table(db)
        db.commit()
        result = ensure_default_view(db, bt.id)
        assert result is None


# ─── Test: assess_view_availability v4 §7.1 ──────────────────────────────────

class TestAssessViewAvailability:

    def test_l4_available(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.available is True
        assert result.display_mode == "rows"
        assert result.view_state == "available"

    def test_l0_blocked(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L0", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert result.display_mode == "blocked"
        assert result.view_state == "risk_blocked"
        assert result.unavailable_reason is not None

    def test_l2_aggregate(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L2", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.display_mode == "aggregate"
        assert "AGGREGATE_ONLY" in result.risk_flags

    def test_denied_policy_permission_blocked(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=True, deny_reasons=["no access"])
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert result.view_state == "permission_blocked"
        assert "ACCESS_DENIED" in result.risk_flags

    def test_invalid_schema_state(self, db):
        """v4 §7.1: view_state=invalid_schema → 不可用"""
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields],
                          view_state="invalid_schema",
                          view_invalid_reason="字段已删除")
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert result.view_state == "invalid_schema"
        assert "INVALID_SCHEMA" in result.risk_flags
        assert "字段已删除" in result.unavailable_reason

    def test_compile_failed_state(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields],
                          view_state="compile_failed",
                          view_invalid_reason="编译错误")
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert result.view_state == "compile_failed"

    def test_sync_failed_flag(self, db):
        bt = _make_table(db)
        bt.sync_status = "failed"
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)
        assert "SYNC_FAILED" in result.risk_flags


# ─── Test: revalidate_view v4 §5.2 ───────────────────────────────────────────

class TestRevalidateView:

    def test_valid_view_passes(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 3)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        is_valid, reason = revalidate_view(db, view)
        assert is_valid is True
        assert reason is None

    def test_missing_fields_detected(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields] + [99999])
        db.commit()

        is_valid, reason = revalidate_view(db, view)
        assert is_valid is False
        assert "已删除的字段" in reason

    def test_missing_filter_field_detected(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields],
                          filter_rule_json={"filters": [{"field": "nonexistent", "op": "eq", "value": "x"}]})
        db.commit()

        is_valid, reason = revalidate_view(db, view)
        assert is_valid is False
        assert "不存在的字段" in reason

    def test_revalidate_table_views_updates_state(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        v_good = _make_view(db, bt.id, name="好的", field_ids=[f.id for f in fields])
        v_bad = _make_view(db, bt.id, name="坏的", field_ids=[f.id for f in fields] + [99999])
        db.commit()

        results = revalidate_table_views(db, bt.id)
        db.commit()

        good_result = [r for r in results if r["view_id"] == v_good.id][0]
        bad_result = [r for r in results if r["view_id"] == v_bad.id][0]

        assert good_result["new_state"] == "available"
        assert bad_result["new_state"] == "invalid_schema"


# ─── Test: data_table_reader v4 §1.1/§8 ─────────────────────────────────────

class TestDataTableReaderV4:

    @pytest.mark.asyncio
    async def test_view_id_routes_to_execute_view_read(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "viewer", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute

        mock_result = ViewReadResult(
            ok=True, mode="rows", table_id=bt.id, view_id=view.id,
            fields=[{"field_name": "f1"}], rows=[{"f1": "v1"}], total=1,
        )

        with patch("app.services.data_view_runtime.execute_view_read", return_value=mock_result):
            result = await reader_execute(
                {"view_id": view.id, "limit": 10},
                db, user_id=user.id,
            )

        assert result["ok"] is True
        assert result["view_id"] == view.id

    @pytest.mark.asyncio
    async def test_non_admin_without_view_gets_error(self, db):
        """v4 §1.1: 非admin无视图→直接报错，不允许整表 fallback"""
        dept = _make_dept(db)
        user = _make_user(db, "viewer", Role.EMPLOYEE, dept.id)
        bt = _make_table(db, name="no_view_table")
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute
        result = await reader_execute(
            {"table_name": "no_view_table"},
            db, user_id=user.id,
        )

        assert result["ok"] is False
        assert "视图" in result["error"]

    @pytest.mark.asyncio
    async def test_admin_without_explicit_switch_gets_error(self, db):
        """v4 §1.1: admin 不显式设 admin_raw_table=true → 不允许整表读"""
        dept = _make_dept(db)
        admin = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        bt = _make_table(db, name="admin_test_table")
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute
        result = await reader_execute(
            {"table_name": "admin_test_table"},
            db, user_id=admin.id,
        )

        assert result["ok"] is False
        assert "admin_raw_table" in result["error"]

    @pytest.mark.asyncio
    async def test_view_state_invalid_returns_error_code(self, db):
        """v4 §7.3: view_state 非 available → 返回明确错误码"""
        dept = _make_dept(db)
        user = _make_user(db, "viewer", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields],
                          view_state="invalid_schema",
                          view_invalid_reason="字段已删除")
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute
        result = await reader_execute(
            {"view_id": view.id},
            db, user_id=user.id,
        )

        assert result["ok"] is False
        assert result["error_code"] == "view_invalid_schema"

    @pytest.mark.asyncio
    async def test_no_params_returns_view_required_error(self, db):
        """v4 §8.1: 无 view_id 无 table_name → 明确要求提供 view_id"""
        dept = _make_dept(db)
        user = _make_user(db, "viewer", Role.EMPLOYEE, dept.id)
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute
        result = await reader_execute({}, db, user_id=user.id)

        assert result["ok"] is False
        assert "view_id" in result["error"]


# ─── Test: API endpoints (v4 grouped format) ────────────────────────────────

class TestDataViewsAPI:

    def test_list_data_views_grouped(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        rg = _make_role_group(db, bt.id, user_ids=[admin.id])
        _make_policy(db, bt.id, rg.id, view_id=view.id, disclosure="L4")
        db.commit()

        token = _login(client, "admin")
        resp = client.get("/api/dev-studio/data-views", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "tables" in data
        table_group = [t for t in data["tables"] if t["table_id"] == bt.id][0]
        view_item = [v for v in table_group["views"] if v["view_id"] == view.id][0]
        assert view_item["view_name"] == "测试视图"
        assert view_item["result_mode"] is not None
        assert view_item["risk_level"] in ("low", "medium", "high")
        assert "view_state" in view_item
        assert "unavailable_reason" in view_item

    def test_list_filters_denied_views_for_non_admin(self, client, db):
        dept = _make_dept(db)
        employee = _make_user(db, "employee", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        token = _login(client, "employee")
        resp = client.get("/api/dev-studio/data-views", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        all_view_ids = []
        for t in data["tables"]:
            for v in t["views"]:
                all_view_ids.append(v["view_id"])
        assert view.id not in all_view_ids

    def test_view_detail_includes_view_state(self, client, db):
        dept = _make_dept(db)
        admin = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        rg = _make_role_group(db, bt.id, user_ids=[admin.id])
        _make_policy(db, bt.id, rg.id, view_id=view.id, disclosure="L4")
        db.commit()

        token = _login(client, "admin")
        resp = client.get(f"/api/dev-studio/data-views/{view.id}", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["view"]["view_state"] == "available"
        assert data["view"]["result_mode"] is not None
        assert data["availability"]["view_state"] == "available"
        assert "unavailable_reason" in data["availability"]

    def test_view_detail_403_for_denied_user(self, client, db):
        dept = _make_dept(db)
        employee = _make_user(db, "employee", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        token = _login(client, "employee")
        resp = client.get(f"/api/dev-studio/data-views/{view.id}", headers=_auth(token))
        assert resp.status_code == 403

    def test_unavailable_view_returns_reason(self, client, db):
        """v4 §7.2: 不可用视图包含原因"""
        dept = _make_dept(db)
        admin = _make_user(db, "admin", Role.SUPER_ADMIN, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[], disclosure_ceiling="L0")
        rg = _make_role_group(db, bt.id, user_ids=[admin.id])
        _make_policy(db, bt.id, rg.id, disclosure="L0")
        db.commit()

        token = _login(client, "admin")
        resp = client.get("/api/dev-studio/data-views", headers=_auth(token))
        data = resp.json()
        for t in data["tables"]:
            for v in t["views"]:
                if v["view_id"] == view.id:
                    assert v["available"] is False
                    assert v["unavailable_reason"] is not None
                    assert v["view_state"] != "available"
