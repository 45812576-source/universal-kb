"""TC-AUTO-FILER: 自动归档引擎 + 系统目录树 + 建议流 综合测试。

覆盖:
  A. 系统目录树（ensure_system_folders / get_system_folder_for_taxonomy / get_system_folder_for_board）
  B. 自动归档（auto_file_single / auto_file_batch / undo_batch / undo_single / get_unfiled_entries / get_filing_actions）
  C. API 权限（SUPER_ADMIN 限制 / 普通用户可访问）
  D. 建议流（suggest_folders_batch / accept / reject）
"""
import asyncio

import pytest

from app.data.knowledge_taxonomy import TAXONOMY
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_filing import KnowledgeFilingAction, KnowledgeFilingSuggestion
from app.models.user import Role
from app.services.auto_filer import (
    auto_file_batch,
    auto_file_single,
    get_filing_actions,
    get_unfiled_entries,
    undo_batch,
    undo_single,
)
from app.services.system_folder_service import (
    ensure_system_folders,
    get_system_folder_for_board,
    get_system_folder_for_taxonomy,
)
from tests.conftest import _auth, _login, _make_dept, _make_user


# ── Helper ────────────────────────────────────────────────────────────────────


def _make_entry(
    db,
    user_id,
    title="测试文档",
    taxonomy_code=None,
    taxonomy_board=None,
    folder_id=None,
    classification_confidence=None,
):
    """快速创建一条 KnowledgeEntry 用于测试。"""
    entry = KnowledgeEntry(
        title=title,
        content="自动归档测试内容",
        category="experience",
        source_type="manual",
        taxonomy_code=taxonomy_code,
        taxonomy_board=taxonomy_board,
        folder_id=folder_id,
        classification_confidence=classification_confidence,
        created_by=user_id,
    )
    db.add(entry)
    db.flush()
    return entry


# ═══════════════════════════════════════════════════════════════════════════════
# A. 系统目录树
# ═══════════════════════════════════════════════════════════════════════════════


class TestSystemFolderTree:
    """A1-A4: 系统归档树创建与查询。"""

    def test_ensure_creates_board_roots_and_taxonomy_leaves(self, db):
        """A1: 空库首次调用 ensure_system_folders，应创建 A-F 板块根目录 + 各分类叶子目录。"""
        dept = _make_dept(db)
        user = _make_user(db, "sys_admin1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)

        # 至少应该有 taxonomy 节点数量的叶子
        assert len(mapping) >= len(TAXONOMY), (
            f"叶子目录数 {len(mapping)} 应 >= taxonomy 节点数 {len(TAXONOMY)}"
        )

        # 检查 6 个板块根目录存在
        for board in ["A", "B", "C", "D", "E", "F"]:
            root = (
                db.query(KnowledgeFolder)
                .filter(
                    KnowledgeFolder.is_system == 1,
                    KnowledgeFolder.taxonomy_board == board,
                    KnowledgeFolder.parent_id.is_(None),
                )
                .first()
            )
            assert root is not None, f"板块 {board} 根目录应存在"

    def test_ensure_is_idempotent(self, db):
        """A2: 调用两次不产生重复目录，ID 保持稳定。"""
        dept = _make_dept(db)
        user = _make_user(db, "sys_admin2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping1 = ensure_system_folders(db, owner_id=user.id)
        count1 = db.query(KnowledgeFolder).filter(KnowledgeFolder.is_system == 1).count()

        mapping2 = ensure_system_folders(db, owner_id=user.id)
        count2 = db.query(KnowledgeFolder).filter(KnowledgeFolder.is_system == 1).count()

        assert count1 == count2, "两次调用不应增加目录数量"
        # 同一个 taxonomy_code 的 folder_id 应一致
        for code in mapping1:
            assert mapping1[code] == mapping2[code], f"taxonomy {code} 的 folder_id 应稳定"

    def test_get_system_folder_for_taxonomy(self, db):
        """A3: 通过 taxonomy_code 查询目录 ID 应返回正确值。"""
        dept = _make_dept(db)
        user = _make_user(db, "sys_admin3", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)

        # 取第一个 taxonomy 节点验证
        first_code = TAXONOMY[0]["code"]
        folder_id = get_system_folder_for_taxonomy(db, first_code)
        assert folder_id is not None
        assert folder_id == mapping[first_code]

    def test_get_system_folder_for_taxonomy_not_found(self, db):
        """A3b: 不存在的 taxonomy_code 返回 None。"""
        result = get_system_folder_for_taxonomy(db, "ZZZ.999")
        assert result is None

    def test_get_system_folder_for_board(self, db):
        """A4: 通过 board 字母查询板块根目录 ID。"""
        dept = _make_dept(db)
        user = _make_user(db, "sys_admin4", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)

        for board in ["A", "B", "C", "D", "E", "F"]:
            folder_id = get_system_folder_for_board(db, board)
            assert folder_id is not None, f"板块 {board} 根目录 ID 不应为 None"

        # 不存在的板块
        assert get_system_folder_for_board(db, "Z") is None


# ═══════════════════════════════════════════════════════════════════════════════
# B. 自动归档
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutoFileSingle:
    """B1-B4: 单条自动归档逻辑。"""

    def test_taxonomy_code_exact_match(self, db):
        """B1: taxonomy_code 精确匹配 → 归档到系统目录，写 KnowledgeFilingAction。"""
        dept = _make_dept(db)
        user = _make_user(db, "filer1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]
        expected_folder = mapping[code]

        entry = _make_entry(db, user.id, title="精确匹配文档", taxonomy_code=code)
        db.commit()

        action = auto_file_single(db, entry, batch_id="test-batch-1", user_id=user.id)
        db.commit()

        assert action is not None
        assert action.action_type == "auto_file"
        assert action.to_folder_id == expected_folder
        assert entry.folder_id == expected_folder
        assert action.decision_source == "taxonomy"
        assert action.confidence > 0
        assert action.batch_id == "test-batch-1"
        assert action.created_by == user.id

    def test_taxonomy_board_fallback_to_board_root(self, db):
        """B2: 只有 taxonomy_board，无同板块历史 → 归到板块根目录。"""
        dept = _make_dept(db)
        user = _make_user(db, "filer2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)
        board_root_id = get_system_folder_for_board(db, "A")

        entry = _make_entry(db, user.id, title="板块回退文档", taxonomy_board="A")
        db.commit()

        action = auto_file_single(db, entry, user_id=user.id)
        db.commit()

        assert action is not None
        assert entry.folder_id == board_root_id
        assert action.confidence == 0.3  # 低置信度

    def test_taxonomy_board_uses_history_distribution(self, db):
        """B2b: 只有 taxonomy_board，但同板块有已归档文档 → 使用历史分布。"""
        dept = _make_dept(db)
        user = _make_user(db, "filer2b", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)
        # 先创建一些已归档到某个目录的同板块文档
        code = TAXONOMY[0]["code"]
        target_folder = mapping[code]
        for i in range(5):
            _make_entry(
                db, user.id,
                title=f"历史文档{i}",
                taxonomy_board=TAXONOMY[0]["board"],
                folder_id=target_folder,
            )
        db.commit()

        # 新文档只有 board，没有 code
        entry = _make_entry(
            db, user.id,
            title="等待历史分布归档",
            taxonomy_board=TAXONOMY[0]["board"],
        )
        db.commit()

        action = auto_file_single(db, entry, user_id=user.id)
        db.commit()

        assert action is not None
        assert action.decision_source == "board_neighbors"
        assert entry.folder_id == target_folder

    def test_already_has_folder_id_skips(self, db):
        """B3: 已有 folder_id → 跳过，返回 None。"""
        dept = _make_dept(db)
        user = _make_user(db, "filer3", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)

        entry = _make_entry(
            db, user.id,
            title="已归档文档",
            taxonomy_code=TAXONOMY[0]["code"],
            folder_id=999,
        )
        db.commit()

        action = auto_file_single(db, entry, user_id=user.id)
        assert action is None
        assert entry.folder_id == 999  # 未改变

    def test_no_taxonomy_no_candidates_skips(self, db):
        """B4: 既没有 taxonomy_code 也没有 taxonomy_board → 跳过。"""
        dept = _make_dept(db)
        user = _make_user(db, "filer4", Role.SUPER_ADMIN, dept.id)
        db.commit()

        entry = _make_entry(db, user.id, title="无分类文档")
        db.commit()

        action = auto_file_single(db, entry, user_id=user.id)
        assert action is None
        assert entry.folder_id is None


class TestAutoFileBatch:
    """B5: 批量自动归档。"""

    def test_batch_stats_correct(self, db):
        """B5: filed + skipped = total，batch_id 一致。"""
        dept = _make_dept(db)
        user = _make_user(db, "batch1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]

        # 3 条可归档（有 taxonomy_code）
        for i in range(3):
            _make_entry(db, user.id, title=f"待归档{i}", taxonomy_code=code)
        # 2 条不可归档（无分类信息）
        for i in range(2):
            _make_entry(db, user.id, title=f"不可归档{i}")
        # 1 条已归档（应跳过）
        _make_entry(db, user.id, title="已归档", taxonomy_code=code, folder_id=999)
        db.commit()

        stats = auto_file_batch(db, user_id=user.id)

        assert stats["total"] == 5  # folder_id=None 的才会被查出来
        assert stats["filed"] == 3
        assert stats["skipped"] == 2
        assert stats["filed"] + stats["skipped"] == stats["total"]
        assert stats["batch_id"].startswith("batch-")

        # 确认所有 action 的 batch_id 一致
        actions = (
            db.query(KnowledgeFilingAction)
            .filter(KnowledgeFilingAction.batch_id == stats["batch_id"])
            .all()
        )
        assert len(actions) == 3


class TestUndoBatch:
    """B6: 批量撤销。"""

    def test_undo_batch_reverts_entries_still_at_target(self, db):
        """B6: 只撤销 folder_id 还在 to_folder_id 的条目，并创建 undo_auto_file 记录。"""
        dept = _make_dept(db)
        user = _make_user(db, "undo1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]
        target_folder = mapping[code]

        # 创建 2 条可归档文档
        e1 = _make_entry(db, user.id, title="撤销测试1", taxonomy_code=code)
        e2 = _make_entry(db, user.id, title="撤销测试2", taxonomy_code=code)
        db.commit()

        stats = auto_file_batch(db, user_id=user.id)
        batch_id = stats["batch_id"]

        assert e1.folder_id == target_folder
        assert e2.folder_id == target_folder

        # 手动把 e2 移到别的目录（模拟用户手动移动后不应被撤销）
        e2.folder_id = 88888
        db.commit()

        count = undo_batch(db, batch_id)

        # 只有 e1 被撤销（e2 已被手动移走）
        assert count == 1
        db.refresh(e1)
        db.refresh(e2)
        assert e1.folder_id is None  # 恢复到原始 None
        assert e2.folder_id == 88888  # 不受影响

        # 检查 undo_auto_file 记录
        undo_actions = (
            db.query(KnowledgeFilingAction)
            .filter(
                KnowledgeFilingAction.batch_id == batch_id,
                KnowledgeFilingAction.action_type == "undo_auto_file",
            )
            .all()
        )
        assert len(undo_actions) == 1
        assert undo_actions[0].from_folder_id == target_folder
        assert undo_actions[0].to_folder_id is None


class TestUndoSingle:
    """B7: 单条撤销。"""

    def test_undo_single_auto_file(self, db):
        """B7a: 正常撤销一条 auto_file 操作。"""
        dept = _make_dept(db)
        user = _make_user(db, "undo_s1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]

        entry = _make_entry(db, user.id, title="单条撤销", taxonomy_code=code)
        db.commit()

        action = auto_file_single(db, entry, batch_id="s-batch", user_id=user.id)
        db.commit()

        assert entry.folder_id is not None
        ok = undo_single(db, action.id)
        assert ok is True
        db.refresh(entry)
        assert entry.folder_id is None

    def test_undo_single_non_auto_file_returns_false(self, db):
        """B7b: 非 auto_file 类型的 action 不可撤销。"""
        dept = _make_dept(db)
        user = _make_user(db, "undo_s2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        entry = _make_entry(db, user.id, title="手动操作")
        db.commit()

        # 手动创建一条 manual_move 类型的 action
        manual_action = KnowledgeFilingAction(
            knowledge_id=entry.id,
            action_type="manual_move",
            from_folder_id=None,
            to_folder_id=100,
            decision_source="manual",
            created_by=user.id,
        )
        db.add(manual_action)
        db.commit()

        ok = undo_single(db, manual_action.id)
        assert ok is False

    def test_undo_single_already_moved_returns_false(self, db):
        """B7c: 条目已被手动移走（folder_id != to_folder_id）→ 不可撤销。"""
        dept = _make_dept(db)
        user = _make_user(db, "undo_s3", Role.SUPER_ADMIN, dept.id)
        db.commit()

        mapping = ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]

        entry = _make_entry(db, user.id, title="已移走文档", taxonomy_code=code)
        db.commit()

        action = auto_file_single(db, entry, batch_id="s-batch-2", user_id=user.id)
        db.commit()

        # 模拟用户手动移走
        entry.folder_id = 77777
        db.commit()

        ok = undo_single(db, action.id)
        assert ok is False
        db.refresh(entry)
        assert entry.folder_id == 77777  # 不变


class TestGetUnfiledAndActions:
    """B8-B9: 查询未归档和操作记录。"""

    def test_get_unfiled_entries(self, db):
        """B8: 列出所有 folder_id=None 的文档。"""
        dept = _make_dept(db)
        user = _make_user(db, "unfiled1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        _make_entry(db, user.id, title="未归档1")
        _make_entry(db, user.id, title="未归档2")
        _make_entry(db, user.id, title="已归档", folder_id=123)
        db.commit()

        result = get_unfiled_entries(db)
        assert len(result) == 2
        titles = [r["title"] for r in result]
        assert "未归档1" in titles
        assert "未归档2" in titles

    def test_get_filing_actions_by_batch(self, db):
        """B9: 按 batch_id 查询操作记录。"""
        dept = _make_dept(db)
        user = _make_user(db, "actions1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]

        for i in range(3):
            _make_entry(db, user.id, title=f"记录测试{i}", taxonomy_code=code)
        db.commit()

        stats = auto_file_batch(db, user_id=user.id)
        batch_id = stats["batch_id"]

        actions = get_filing_actions(db, batch_id=batch_id)
        assert len(actions) == 3
        for a in actions:
            assert a["batch_id"] == batch_id
            assert a["action_type"] == "auto_file"

    def test_get_filing_actions_all(self, db):
        """B9b: 不指定 batch_id 时返回所有操作。"""
        dept = _make_dept(db)
        user = _make_user(db, "actions2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        ensure_system_folders(db, owner_id=user.id)
        code = TAXONOMY[0]["code"]
        _make_entry(db, user.id, title="全量查询", taxonomy_code=code)
        db.commit()

        auto_file_batch(db, user_id=user.id)

        all_actions = get_filing_actions(db)
        assert len(all_actions) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# C. API 权限测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestFilingAPIPermissions:
    """C1-C3: 自动归档 API 端点权限校验。"""

    def test_auto_run_requires_super_admin(self, client, db):
        """C1: POST /api/knowledge/filing/auto-run 普通员工应返回 403。"""
        dept = _make_dept(db)
        _make_user(db, "emp_c1", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "emp_c1")

        resp = client.post("/api/knowledge/filing/auto-run", headers=_auth(token))
        assert resp.status_code == 403

    def test_auto_run_ok_for_super_admin(self, client, db):
        """C1b: POST /api/knowledge/filing/auto-run 超管应成功。"""
        dept = _make_dept(db)
        _make_user(db, "sa_c1", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_c1")

        resp = client.post("/api/knowledge/filing/auto-run", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "batch_id" in data

    def test_ensure_system_tree_requires_super_admin(self, client, db):
        """C2: POST /api/knowledge/filing/ensure-system-tree 普通员工应返回 403。"""
        dept = _make_dept(db)
        _make_user(db, "emp_c2", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "emp_c2")

        resp = client.post("/api/knowledge/filing/ensure-system-tree", headers=_auth(token))
        assert resp.status_code == 403

    def test_ensure_system_tree_ok_for_super_admin(self, client, db):
        """C2b: 超管调用 ensure-system-tree 应成功。"""
        dept = _make_dept(db)
        _make_user(db, "sa_c2", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_c2")

        resp = client.post("/api/knowledge/filing/ensure-system-tree", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["nodes"] > 0

    def test_unfiled_accessible_to_all_logged_in(self, client, db):
        """C3: GET /api/knowledge/filing/unfiled 所有登录用户可访问。"""
        dept = _make_dept(db)
        _make_user(db, "emp_c3", Role.EMPLOYEE, dept.id)
        db.commit()
        token = _login(client, "emp_c3")

        resp = client.get("/api/knowledge/filing/unfiled", headers=_auth(token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unfiled_requires_auth(self, client):
        """C3b: GET /api/knowledge/filing/unfiled 未登录应 401/403。"""
        resp = client.get("/api/knowledge/filing/unfiled")
        assert resp.status_code in (401, 403)

    def test_undo_batch_api(self, client, db):
        """C4: POST /api/knowledge/filing/undo 超管调用。"""
        dept = _make_dept(db)
        user = _make_user(db, "sa_c4", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_c4")

        # 先初始化系统树 + 创建文档 + 批量归档
        client.post("/api/knowledge/filing/ensure-system-tree", headers=_auth(token))

        # 直接通过 DB 创建可归档文档
        code = TAXONOMY[0]["code"]
        _make_entry(db, user.id, title="API撤销测试", taxonomy_code=code)
        db.commit()

        run_resp = client.post("/api/knowledge/filing/auto-run", headers=_auth(token))
        assert run_resp.status_code == 200
        batch_id = run_resp.json()["batch_id"]

        # 撤销
        undo_resp = client.post(
            "/api/knowledge/filing/undo",
            headers=_auth(token),
            json={"batch_id": batch_id},
        )
        assert undo_resp.status_code == 200
        assert undo_resp.json()["ok"] is True

    def test_undo_single_api(self, client, db):
        """C5: POST /api/knowledge/filing/undo-single/{action_id}。"""
        dept = _make_dept(db)
        user = _make_user(db, "sa_c5", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_c5")

        client.post("/api/knowledge/filing/ensure-system-tree", headers=_auth(token))

        code = TAXONOMY[0]["code"]
        entry = _make_entry(db, user.id, title="单条API撤销", taxonomy_code=code)
        db.commit()

        run_resp = client.post("/api/knowledge/filing/auto-run", headers=_auth(token))
        batch_id = run_resp.json()["batch_id"]

        # 获取操作记录找到 action_id
        actions_resp = client.get(
            f"/api/knowledge/filing/actions?batch_id={batch_id}",
            headers=_auth(token),
        )
        assert actions_resp.status_code == 200
        actions = actions_resp.json()
        assert len(actions) >= 1

        action_id = actions[0]["id"]
        undo_resp = client.post(
            f"/api/knowledge/filing/undo-single/{action_id}",
            headers=_auth(token),
        )
        assert undo_resp.status_code == 200
        assert undo_resp.json()["ok"] is True

    def test_filing_actions_api(self, client, db):
        """C6: GET /api/knowledge/filing/actions 可查询操作记录。"""
        dept = _make_dept(db)
        _make_user(db, "sa_c6", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sa_c6")

        resp = client.get("/api/knowledge/filing/actions", headers=_auth(token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ═══════════════════════════════════════════════════════════════════════════════
# D. 建议流 (filing_suggester)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFilingSuggester:
    """D1-D4: 归档建议生成、接受、拒绝。"""

    def test_suggest_generates_suggestion_with_history(self, db):
        """D1: 有同分类已归档文档时，suggest_folders_batch 应生成建议。"""
        from app.services.filing_suggester import suggest_folders_batch

        dept = _make_dept(db)
        user = _make_user(db, "sugg1", Role.SUPER_ADMIN, dept.id)
        db.commit()

        # 创建一个用户自建 folder
        folder = KnowledgeFolder(
            name="营销资料", created_by=user.id, is_system=0,
        )
        db.add(folder)
        db.flush()

        # 创建已归档到该 folder 的同分类文档（作为历史参考）
        code = TAXONOMY[0]["code"]
        board = TAXONOMY[0]["board"]
        for i in range(5):
            _make_entry(
                db, user.id,
                title=f"历史参考{i}",
                taxonomy_code=code,
                taxonomy_board=board,
                folder_id=folder.id,
            )

        # 创建待建议的文档
        target = _make_entry(
            db, user.id,
            title="需要建议的文档",
            taxonomy_code=code,
            taxonomy_board=board,
        )
        db.commit()

        results = asyncio.get_event_loop().run_until_complete(
            suggest_folders_batch(db, [target.id], user.id)
        )

        assert len(results) == 1
        r = results[0]
        assert r["knowledge_id"] == target.id
        assert r["suggestion"] is not None
        assert r["suggestion"]["confidence"] > 0
        assert r["suggestion"]["reason"] != ""

        # 检查写入了 KnowledgeFilingSuggestion 表
        s = (
            db.query(KnowledgeFilingSuggestion)
            .filter(KnowledgeFilingSuggestion.knowledge_id == target.id)
            .first()
        )
        assert s is not None
        assert s.status == "pending"
        assert s.confidence > 0

    def test_suggest_no_match_returns_none(self, db):
        """D2: 无匹配历史时，建议为 None。"""
        from app.services.filing_suggester import suggest_folders_batch

        dept = _make_dept(db)
        user = _make_user(db, "sugg2", Role.SUPER_ADMIN, dept.id)
        db.commit()

        # 创建一条无分类信息的文档
        entry = _make_entry(db, user.id, title="无法建议的文档")
        db.commit()

        results = asyncio.get_event_loop().run_until_complete(
            suggest_folders_batch(db, [entry.id], user.id)
        )

        assert len(results) == 1
        assert results[0]["suggestion"] is None

    def test_accept_suggestion_updates_folder(self, client, db):
        """D3: 接受建议 → entry.folder_id 更新，suggestion.status = accepted。"""
        dept = _make_dept(db)
        user = _make_user(db, "sugg3", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sugg3")

        # 创建 folder 和 entry
        folder = KnowledgeFolder(name="建议目标", created_by=user.id, is_system=0)
        db.add(folder)
        db.flush()

        entry = _make_entry(db, user.id, title="接受建议测试")
        db.flush()

        # 手动创建一条 suggestion
        suggestion = KnowledgeFilingSuggestion(
            knowledge_id=entry.id,
            suggested_folder_id=folder.id,
            suggested_folder_path="建议目标",
            confidence=0.85,
            reason="测试建议",
            based_on={"taxonomy_code": "A1.1"},
            status="pending",
        )
        db.add(suggestion)
        db.commit()

        resp = client.post(
            f"/api/knowledge/{entry.id}/filing-suggestion/accept",
            headers=_auth(token),
            json={"suggestion_id": suggestion.id},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["folder_id"] == folder.id

        db.refresh(entry)
        db.refresh(suggestion)
        assert entry.folder_id == folder.id
        assert suggestion.status == "accepted"

    def test_reject_suggestion_updates_status(self, client, db):
        """D4: 拒绝建议 → suggestion.status = rejected，folder_id 不变。"""
        dept = _make_dept(db)
        user = _make_user(db, "sugg4", Role.SUPER_ADMIN, dept.id)
        db.commit()
        token = _login(client, "sugg4")

        folder = KnowledgeFolder(name="拒绝目标", created_by=user.id, is_system=0)
        db.add(folder)
        db.flush()

        entry = _make_entry(db, user.id, title="拒绝建议测试")
        db.flush()

        suggestion = KnowledgeFilingSuggestion(
            knowledge_id=entry.id,
            suggested_folder_id=folder.id,
            suggested_folder_path="拒绝目标",
            confidence=0.6,
            reason="测试拒绝",
            based_on={"taxonomy_board": "A"},
            status="pending",
        )
        db.add(suggestion)
        db.commit()

        resp = client.post(
            f"/api/knowledge/{entry.id}/filing-suggestion/reject",
            headers=_auth(token),
            json={"suggestion_id": suggestion.id},
        )
        assert resp.status_code == 200

        db.refresh(entry)
        db.refresh(suggestion)
        assert entry.folder_id is None  # 不变
        assert suggestion.status == "rejected"
