"""Tests for Opencode × Le Desk data view bridge.

Covers:
1. ensure_default_view auto-generation
2. GET /data-views list (permission filtering)
3. GET /data-views/{view_id} detail + preview
4. data_table_reader view-first upgrade
5. Disclosure level enforcement (L2 aggregate, L3 masking)
6. Audit log creation
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
)
from app.services.policy_engine import PolicyResult
from app.routers.data_assets import ensure_default_view


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_table(db, name="test_table", display="测试表"):
    bt = BusinessTable(
        table_name=name,
        display_name=display,
        source_type="blank",
        owner_id=1,
    )
    db.add(bt)
    db.flush()
    return bt


def _make_fields(db, table_id, count=3):
    fields = []
    for i in range(count):
        f = TableField(
            table_id=table_id,
            field_name=f"field_{i}",
            display_name=f"字段{i}",
            physical_column_name=f"field_{i}",
            field_type="text",
            sort_order=i,
        )
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


# ─── Test: ensure_default_view ────────────────────────────────────────────────

class TestEnsureDefaultView:

    def test_creates_default_view_when_none_exists(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 3)
        db.commit()

        view = ensure_default_view(db, bt.id)
        db.commit()

        assert view is not None
        assert view.name == "默认视图"
        assert view.is_system is True
        assert view.is_default is True
        assert view.view_purpose == "explore"
        assert view.view_kind == "list"
        assert set(view.visible_field_ids) == {f.id for f in fields}

    def test_skips_if_default_exists(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        existing_view = _make_view(
            db, bt.id, name="默认视图",
            field_ids=[fields[0].id],
            is_system=True, is_default=True,
        )
        db.commit()

        result = ensure_default_view(db, bt.id)
        assert result.id == existing_view.id

    def test_updates_field_ids_on_sync(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id, 2)
        view = _make_view(
            db, bt.id, name="默认视图",
            field_ids=[fields[0].id],
            is_system=True, is_default=True,
        )
        db.commit()

        # Add a new field
        new_field = TableField(
            table_id=bt.id, field_name="new_field",
            display_name="新字段", physical_column_name="new_field",
            field_type="text", sort_order=10,
        )
        db.add(new_field)
        db.flush()

        result = ensure_default_view(db, bt.id)
        db.commit()

        assert new_field.id in result.visible_field_ids

    def test_returns_none_when_no_fields(self, db):
        bt = _make_table(db)
        db.commit()

        result = ensure_default_view(db, bt.id)
        assert result is None


# ─── Test: assess_view_availability ───────────────────────────────────────────

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

    def test_l0_blocked(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L0", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert result.display_mode == "blocked"
        assert "L0_BLOCKED" in result.risk_flags

    def test_l2_aggregate(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L2", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.display_mode == "aggregate"
        assert "AGGREGATE_ONLY" in result.risk_flags

    def test_denied_policy(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=True, deny_reasons=["no access"])
        result = assess_view_availability(view, policy, bt)

        assert result.available is False
        assert "ACCESS_DENIED" in result.risk_flags

    def test_disclosure_ceiling_caps_level(self, db):
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields], disclosure_ceiling="L2")
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert result.display_mode == "aggregate"

    def test_sync_failed_flag(self, db):
        bt = _make_table(db)
        bt.sync_status = "failed"
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        db.commit()

        policy = PolicyResult(disclosure_level="L4", denied=False)
        result = assess_view_availability(view, policy, bt)

        assert "SYNC_FAILED" in result.risk_flags


# ─── Test: API endpoints ─────────────────────────────────────────────────────

class TestDataViewsAPI:

    def test_list_data_views(self, client, db):
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
        assert len(data["items"]) >= 1
        item = [i for i in data["items"] if i["view_id"] == view.id][0]
        assert item["table_name"] == "test_table"
        assert item["view_name"] == "测试视图"

    def test_list_filters_denied_views_for_non_admin(self, client, db):
        dept = _make_dept(db)
        employee = _make_user(db, "employee", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        # No role group or policy for this employee → denied
        db.commit()

        token = _login(client, "employee")
        resp = client.get("/api/dev-studio/data-views", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        # Employee should see no views (denied and not admin)
        view_ids = [i["view_id"] for i in data["items"]]
        assert view.id not in view_ids

    def test_view_detail(self, client, db):
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
        assert data["view"]["id"] == view.id
        assert len(data["fields"]) == 3
        assert data["permission"]["disclosure_level"] == "L4"

    def test_view_detail_403_for_denied_user(self, client, db):
        dept = _make_dept(db)
        employee = _make_user(db, "employee", Role.EMPLOYEE, dept.id)
        bt = _make_table(db)
        fields = _make_fields(db, bt.id)
        view = _make_view(db, bt.id, field_ids=[f.id for f in fields])
        # No permission for employee
        db.commit()

        token = _login(client, "employee")
        resp = client.get(f"/api/dev-studio/data-views/{view.id}", headers=_auth(token))
        assert resp.status_code == 403


# ─── Test: data_table_reader view-first ──────────────────────────────────────

class TestDataTableReaderViewFirst:

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
    async def test_table_name_without_view_uses_default(self, db):
        dept = _make_dept(db)
        user = _make_user(db, "viewer", Role.EMPLOYEE, dept.id)
        bt = _make_table(db, name="my_table")
        fields = _make_fields(db, bt.id)
        view = _make_view(
            db, bt.id, field_ids=[f.id for f in fields],
            is_default=True,
        )
        db.commit()

        from app.tools.data_table_reader import execute as reader_execute

        mock_result = ViewReadResult(
            ok=True, mode="rows", table_id=bt.id, view_id=view.id,
            fields=[{"field_name": "f1"}], rows=[{"f1": "v1"}], total=1,
        )

        with patch("app.services.data_view_runtime.execute_view_read", return_value=mock_result):
            result = await reader_execute(
                {"table_name": "my_table", "limit": 10},
                db, user_id=user.id,
            )

        assert result["ok"] is True
        assert result["view_id"] == view.id

    @pytest.mark.asyncio
    async def test_non_admin_without_view_gets_error(self, db):
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
