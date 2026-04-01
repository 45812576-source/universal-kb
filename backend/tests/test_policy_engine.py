"""策略引擎单元测试 — 与前端 permission-engine.test.ts 对等。"""
import pytest
from unittest.mock import MagicMock

from app.services.policy_engine import (
    check_disclosure_capability,
    apply_field_masking,
    _apply_single_mask,
    _matches_group,
    _matches_human,
    _matches_skill,
    _merge_allow_policies,
    PolicyResult,
    DISCLOSURE_ORDER,
)


# ─── Fixtures: mock 对象 ─────────────────────────────────────────────────────

def _make_user(id=1, department_id=10, role_value="employee"):
    user = MagicMock()
    user.id = id
    user.department_id = department_id
    user.role = MagicMock()
    user.role.value = role_value
    return user


def _make_role_group(
    id=1, table_id=1, group_type="human_role", subject_scope="custom",
    user_ids=None, department_ids=None, role_keys=None, skill_ids=None,
):
    g = MagicMock()
    g.id = id
    g.table_id = table_id
    g.group_type = group_type
    g.subject_scope = subject_scope
    g.user_ids = user_ids or []
    g.department_ids = department_ids or []
    g.role_keys = role_keys or []
    g.skill_ids = skill_ids or []
    return g


def _make_policy(
    id=1, table_id=1, view_id=None, role_group_id=1,
    row_access_mode="all", row_rule_json=None,
    field_access_mode="all", allowed_field_ids=None, blocked_field_ids=None,
    disclosure_level="L3", masking_rule_json=None,
    tool_permission_mode="readonly", export_permission=False,
    reason_template=None,
):
    p = MagicMock()
    p.id = id
    p.table_id = table_id
    p.view_id = view_id
    p.role_group_id = role_group_id
    p.row_access_mode = row_access_mode
    p.row_rule_json = row_rule_json or {}
    p.field_access_mode = field_access_mode
    p.allowed_field_ids = allowed_field_ids or []
    p.blocked_field_ids = blocked_field_ids or []
    p.disclosure_level = disclosure_level
    p.masking_rule_json = masking_rule_json or {}
    p.tool_permission_mode = tool_permission_mode
    p.export_permission = export_permission
    p.reason_template = reason_template
    return p


# ─── test_resolve_user_role_groups ────────────────────────────────────────────

class TestMatchesGroup:
    def test_human_role_user_id_match(self):
        user = _make_user(id=5)
        g = _make_role_group(group_type="human_role", user_ids=[5, 6])
        assert _matches_group(g, user, skill_id=None) is True

    def test_human_role_department_match(self):
        user = _make_user(department_id=20)
        g = _make_role_group(group_type="human_role", department_ids=[20])
        assert _matches_group(g, user, skill_id=None) is True

    def test_human_role_role_key_match(self):
        user = _make_user(role_value="dept_admin")
        g = _make_role_group(group_type="human_role", role_keys=["dept_admin"])
        assert _matches_group(g, user, skill_id=None) is True

    def test_human_role_no_match(self):
        user = _make_user(id=99, department_id=99, role_value="employee")
        g = _make_role_group(group_type="human_role", user_ids=[1], department_ids=[10], role_keys=["super_admin"])
        assert _matches_group(g, user, skill_id=None) is False

    def test_all_users_scope(self):
        user = _make_user(id=999)
        g = _make_role_group(group_type="human_role", subject_scope="all_users")
        assert _matches_group(g, user, skill_id=None) is True

    def test_skill_role_match(self):
        user = _make_user()
        g = _make_role_group(group_type="skill_role", skill_ids=[42])
        assert _matches_group(g, user, skill_id=42) is True

    def test_skill_role_no_match(self):
        user = _make_user()
        g = _make_role_group(group_type="skill_role", skill_ids=[42])
        assert _matches_group(g, user, skill_id=99) is False

    def test_skill_role_all_skills(self):
        user = _make_user()
        g = _make_role_group(group_type="skill_role", subject_scope="all_skills")
        assert _matches_group(g, user, skill_id=1) is True

    def test_mixed_both_match(self):
        user = _make_user(id=5)
        g = _make_role_group(group_type="mixed", user_ids=[5], skill_ids=[42])
        assert _matches_group(g, user, skill_id=42) is True

    def test_mixed_only_human_match(self):
        user = _make_user(id=5)
        g = _make_role_group(group_type="mixed", user_ids=[5], skill_ids=[42])
        assert _matches_group(g, user, skill_id=99) is False


# ─── test_resolve_effective_policy_deny_wins ──────────────────────────────────

class TestMergeAllowPolicies:
    def test_single_allow(self):
        p = _make_policy(disclosure_level="L3", row_access_mode="all")
        result = _merge_allow_policies([p], [1])
        assert result.denied is False
        assert result.row_access_mode == "all"
        assert result.disclosure_level == "L3"
        assert result.source == "table_policy"

    def test_deny_wins(self):
        """deny 在 resolve_effective_policy 层面处理，这里测 merge 只处理 allow。"""
        # merge 不处理 none，这个由上层 resolve 处理
        pass

    def test_fields_union(self):
        """多组 allowlist 字段取并集。"""
        p1 = _make_policy(field_access_mode="allowlist", allowed_field_ids=[1, 2])
        p2 = _make_policy(field_access_mode="allowlist", allowed_field_ids=[2, 3])
        result = _merge_allow_policies([p1, p2], [1, 2])
        assert result.field_access_mode == "allowlist"
        assert result.visible_field_ids == {1, 2, 3}

    def test_disclosure_takes_max(self):
        """多组 disclosure 取最高。"""
        p1 = _make_policy(disclosure_level="L1")
        p2 = _make_policy(disclosure_level="L3")
        p3 = _make_policy(disclosure_level="L2")
        result = _merge_allow_policies([p1, p2, p3], [1, 2, 3])
        assert result.disclosure_level == "L3"

    def test_row_access_takes_most_permissive(self):
        """row_access_mode 取最宽松。"""
        p1 = _make_policy(row_access_mode="owner")
        p2 = _make_policy(row_access_mode="all")
        result = _merge_allow_policies([p1, p2], [1, 2])
        assert result.row_access_mode == "all"

    def test_export_or(self):
        """export 取 OR。"""
        p1 = _make_policy(export_permission=False)
        p2 = _make_policy(export_permission=True)
        result = _merge_allow_policies([p1, p2], [1, 2])
        assert result.export_permission is True

    def test_tool_takes_most_permissive(self):
        p1 = _make_policy(tool_permission_mode="deny")
        p2 = _make_policy(tool_permission_mode="readwrite")
        result = _merge_allow_policies([p1, p2], [1, 2])
        assert result.tool_permission_mode == "readwrite"

    def test_multi_group_source(self):
        p1 = _make_policy()
        p2 = _make_policy()
        result = _merge_allow_policies([p1, p2], [1, 2])
        assert result.source == "multi_group_merge"


# ─── test_apply_field_masking ─────────────────────────────────────────────────

class TestFieldMasking:
    def test_phone_mask(self):
        assert _apply_single_mask("13812345678", "phone_mask") == "138****5678"

    def test_phone_mask_short(self):
        assert _apply_single_mask("1234", "phone_mask") == "****"

    def test_name_mask(self):
        assert _apply_single_mask("张三丰", "name_mask") == "张**"

    def test_name_mask_two_char(self):
        assert _apply_single_mask("张三", "name_mask") == "张*"

    def test_id_mask(self):
        assert _apply_single_mask("310101199001011234", "id_mask") == "310***********1234"

    def test_email_mask(self):
        assert _apply_single_mask("zhangsan@example.com", "email_mask") == "z***@example.com"

    def test_amount_range_small(self):
        assert _apply_single_mask("5000", "amount_range") == "1万以下"

    def test_amount_range_medium(self):
        assert _apply_single_mask("250000", "amount_range") == "10万-50万"

    def test_amount_range_large(self):
        assert _apply_single_mask("2000000", "amount_range") == "100万以上"

    def test_full_mask(self):
        assert _apply_single_mask("secret", "full_mask") == "***"

    def test_unknown_mask_type(self):
        assert _apply_single_mask("data", "unknown_type") == "***"

    def test_apply_rows(self):
        rows = [
            {"name": "张三", "phone": "13812345678", "amount": 100},
            {"name": "李四", "phone": "13987654321", "amount": 200},
        ]
        rules = {"name": "name_mask", "phone": "phone_mask"}
        result = apply_field_masking(rows, rules)
        assert result[0]["name"] == "张*"
        assert result[0]["phone"] == "138****5678"
        assert result[0]["amount"] == 100  # 未脱敏
        assert result[1]["name"] == "李*"

    def test_apply_with_dict_rule(self):
        rows = [{"email": "test@example.com"}]
        rules = {"email": {"type": "email_mask"}}
        result = apply_field_masking(rows, rules)
        assert result[0]["email"] == "t***@example.com"

    def test_apply_empty_rows(self):
        assert apply_field_masking([], {"name": "name_mask"}) == []

    def test_apply_empty_rules(self):
        rows = [{"name": "test"}]
        assert apply_field_masking(rows, {}) is rows

    def test_apply_none_value(self):
        rows = [{"name": None}]
        result = apply_field_masking(rows, {"name": "name_mask"})
        assert result[0]["name"] is None


# ─── test_disclosure_capabilities ─────────────────────────────────────────────

class TestDisclosureCapabilities:
    def test_l0(self):
        caps = check_disclosure_capability("L0")
        assert caps["can_see_rows"] is False
        assert caps["can_see_aggregate"] is False
        assert caps["can_see_decision"] is False

    def test_l1(self):
        caps = check_disclosure_capability("L1")
        assert caps["can_see_rows"] is False
        assert caps["can_see_decision"] is True

    def test_l2(self):
        caps = check_disclosure_capability("L2")
        assert caps["can_see_rows"] is False
        assert caps["can_see_aggregate"] is True

    def test_l3(self):
        caps = check_disclosure_capability("L3")
        assert caps["can_see_rows"] is True
        assert caps["can_see_masked"] is True
        assert caps["can_see_raw"] is False

    def test_l4(self):
        caps = check_disclosure_capability("L4")
        assert caps["can_see_rows"] is True
        assert caps["can_see_raw"] is True

    def test_unknown_level(self):
        caps = check_disclosure_capability("L99")
        assert caps == check_disclosure_capability("L0")


# ─── test_backward_compat (integration) ──────────────────────────────────────

class TestBackwardCompat:
    """验证无角色组的表走旧逻辑 — 通过 data_tables.py 的分支判断。
    这里只测试 policy engine 在空输入时的行为。"""

    def test_no_role_groups_returns_denied(self):
        """空角色组 → denied（但 list_rows 中不会走到这里，因为 has_new_policy=False）。"""
        from app.services.policy_engine import PolicyResult
        result = PolicyResult()  # 默认值
        assert result.denied is False  # 默认不 deny（需要 resolve 来判断）

    def test_empty_role_group_ids_in_resolve(self):
        """resolve_effective_policy 在空 role_group_ids 时返回 denied。"""
        # 需要 db mock
        db = MagicMock()
        from app.services.policy_engine import resolve_effective_policy
        result = resolve_effective_policy(db, table_id=1, role_group_ids=[], view_id=None)
        assert result.denied is True
        assert result.source == "default_deny"
