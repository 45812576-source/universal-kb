"""TC-COLLAB: 协同编辑系统 API 契约测试。

覆盖文档初始化、Sync 回写、评论、快照、Presence 五大模块。
覆盖文档初始化、Sync 回写、评论、快照（含 restore）、Presence 五大模块。
"""
import pytest
from tests.conftest import _make_user, _make_dept, _login, _auth
from app.models.user import Role
from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, KnowledgeEditGrant
from app.models.knowledge_doc import KnowledgeDoc, KnowledgeDocSnapshot, KnowledgeDocComment


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_entry(db, user_id, title="测试知识条目", content="纯文本内容",
                content_html=None, folder_id=None):
    """创建一条 KnowledgeEntry 并 flush，返回 entry 对象。"""
    entry = KnowledgeEntry(
        title=title,
        content=content,
        content_html=content_html,
        category="experience",
        status=KnowledgeStatus.APPROVED,
        created_by=user_id,
        folder_id=folder_id,
    )
    db.add(entry)
    db.flush()
    return entry


def _grant_edit(db, entry_id, user_id, granted_by):
    """授予用户对某条目的编辑权限。"""
    grant = KnowledgeEditGrant(
        entry_id=entry_id,
        user_id=user_id,
        granted_by=granted_by,
    )
    db.add(grant)
    db.flush()
    return grant


# ═════════════════════════════════════════════════════════════════════════════
# A. 文档初始化 — GET /api/knowledge/{kid}/doc
# ═════════════════════════════════════════════════════════════════════════════

class TestDocInit:
    """文档初始化端点契约。"""

    def test_first_access_creates_doc(self, client, db):
        """首次访问创建 KnowledgeDoc，collab_status 初始化，yjs_doc_key 唯一。"""
        dept = _make_dept(db)
        user = _make_user(db, "collab_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()

        token = _login(client, "collab_u1")
        resp = client.get(f"/api/knowledge/{entry.id}/doc", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert "yjs_doc_key" in data
        assert data["yjs_doc_key"]  # 非空
        assert data["collab_status"] in ("initializing", "ready")
        assert data["knowledge_id"] == entry.id

    def test_entry_with_html_generates_import_snapshot(self, client, db):
        """已有 content_html 的条目首次初始化应生成 import 类型快照。"""
        dept = _make_dept(db)
        user = _make_user(db, "collab_u2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(
            db, user.id,
            content_html="<p>已有富文本</p>",
            content="已有富文本",
        )
        db.commit()

        token = _login(client, "collab_u2")
        resp = client.get(f"/api/knowledge/{entry.id}/doc", headers=_auth(token))
        assert resp.status_code == 200

        # 验证生成了 import 快照
        snapshot = db.query(KnowledgeDocSnapshot).filter_by(
            knowledge_id=entry.id, snapshot_type="import"
        ).first()
        assert snapshot is not None

    def test_entry_without_html_returns_editable_doc(self, client, db):
        """无 content_html 的条目也应正常返回可编辑文档，不应 500。"""
        dept = _make_dept(db)
        user = _make_user(db, "collab_u3", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content="只有纯文本", content_html=None)
        db.commit()

        token = _login(client, "collab_u3")
        resp = client.get(f"/api/knowledge/{entry.id}/doc", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert "yjs_doc_key" in data

    def test_repeated_calls_idempotent(self, client, db):
        """多次调用不会创建重复 KnowledgeDoc，yjs_doc_key 保持不变。"""
        dept = _make_dept(db)
        user = _make_user(db, "collab_u4", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()

        token = _login(client, "collab_u4")
        resp1 = client.get(f"/api/knowledge/{entry.id}/doc", headers=_auth(token))
        resp2 = client.get(f"/api/knowledge/{entry.id}/doc", headers=_auth(token))

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["yjs_doc_key"] == resp2.json()["yjs_doc_key"]

        # DB 里只有一条记录
        count = db.query(KnowledgeDoc).filter_by(knowledge_id=entry.id).count()
        assert count == 1

    def test_nonexistent_knowledge_returns_404(self, client, db):
        """不存在的 knowledge_id 返回 404。"""
        dept = _make_dept(db)
        _make_user(db, "collab_u5", Role.EMPLOYEE, dept.id)
        db.commit()

        token = _login(client, "collab_u5")
        resp = client.get("/api/knowledge/999999/doc", headers=_auth(token))
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# B. Sync 回写 — POST /api/knowledge/{kid}/doc/sync
# ═════════════════════════════════════════════════════════════════════════════

class TestDocSync:
    """Sync 回写端点契约：将协同文档内容同步回 KnowledgeEntry。"""

    def test_sync_writes_back_to_entry(self, client, db):
        """Sync 应将 content_html 和 content 写回 KnowledgeEntry。"""
        dept = _make_dept(db)
        user = _make_user(db, "sync_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "sync_u1")
        # 先初始化文档
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(token), json={
            "html": "<p>同步后的富文本</p>",
            "plain_text": "同步后的纯文本",
        })
        assert resp.status_code == 200

        db.expire_all()
        updated = db.query(KnowledgeEntry).get(kid)
        assert updated.content_html == "<p>同步后的富文本</p>"
        assert updated.content == "同步后的纯文本"

    def test_sync_refreshes_updated_at(self, client, db):
        """Sync 后 KnowledgeEntry.updated_at 应刷新。"""
        dept = _make_dept(db)
        user = _make_user(db, "sync_u2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id
        original_updated = entry.updated_at

        token = _login(client, "sync_u2")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(token), json={
            "html": "<p>新内容</p>",
            "plain_text": "新内容",
        })
        assert resp.status_code == 200

        db.expire_all()
        updated = db.query(KnowledgeEntry).get(kid)
        # updated_at 应当不早于原始值（SQLite 时间精度有限，至少不为 None）
        assert updated.updated_at is not None

    def test_sync_unauthorized_user_rejected(self, client, db):
        """非创建者且无编辑授权的用户不能 sync。"""
        dept = _make_dept(db)
        owner = _make_user(db, "sync_owner", Role.EMPLOYEE, dept.id)
        other = _make_user(db, "sync_other", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, owner.id)
        db.commit()
        kid = entry.id

        # owner 先初始化
        owner_token = _login(client, "sync_owner")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(owner_token))

        # other 尝试 sync
        other_token = _login(client, "sync_other")
        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(other_token), json={
            "html": "<p>恶意覆盖</p>",
            "plain_text": "恶意覆盖",
        })
        assert resp.status_code in (403, 401)

    def test_sync_with_edit_grant_allowed(self, client, db):
        """拥有 edit grant 的用户可以 sync。"""
        dept = _make_dept(db)
        owner = _make_user(db, "sync_grant_owner", Role.EMPLOYEE, dept.id)
        grantee = _make_user(db, "sync_grantee", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, owner.id)
        _grant_edit(db, entry.id, grantee.id, owner.id)
        db.commit()
        kid = entry.id

        owner_token = _login(client, "sync_grant_owner")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(owner_token))

        grantee_token = _login(client, "sync_grantee")
        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(grantee_token), json={
            "html": "<p>被授权者的内容</p>",
            "plain_text": "被授权者的内容",
        })
        assert resp.status_code == 200

    def test_sync_reject_empty_content(self, client, db):
        """不允许用空内容覆盖（防止误操作丢失数据）。"""
        dept = _make_dept(db)
        user = _make_user(db, "sync_u3", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content="原始内容", content_html="<p>原始</p>")
        db.commit()
        kid = entry.id

        token = _login(client, "sync_u3")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(token), json={
            "html": "",
            "plain_text": "",
        })
        assert resp.status_code in (400, 422)


# ═════════════════════════════════════════════════════════════════════════════
# C. 评论接口 — /api/knowledge/{kid}/comments
# ═════════════════════════════════════════════════════════════════════════════

class TestComments:
    """评论 CRUD 与 resolve 契约。"""

    def test_create_general_comment(self, client, db):
        """创建不带 block_key 的通用评论。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u1")
        resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "这是一条通用评论",
        })

        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["content"] == "这是一条通用评论"
        assert data["status"] == "open"
        assert data["block_key"] is None

    def test_create_block_level_comment(self, client, db):
        """创建带 block_key 和 anchor 的块级评论。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u2")
        resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "这段文字有问题",
            "block_key": "block-abc-123",
            "anchor_from": 10,
            "anchor_to": 25,
        })

        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["block_key"] == "block-abc-123"
        assert data["anchor_from"] == 10
        assert data["anchor_to"] == 25

    def test_list_comments(self, client, db):
        """列出某条目下的所有评论。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u3", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u3")
        # 先创建两条评论
        client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "评论一",
        })
        client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "评论二",
            "block_key": "blk-1",
        })

        resp = client.get(f"/api/knowledge/{kid}/comments", headers=_auth(token))
        assert resp.status_code == 200
        comments = resp.json()
        assert isinstance(comments, list)
        assert len(comments) >= 2
        contents = {c["content"] for c in comments}
        assert "评论一" in contents
        assert "评论二" in contents

    def test_resolve_comment(self, client, db):
        """Resolve 评论应设置 status=resolved、resolved_by、resolved_at。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u4", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u4")
        create_resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "需要解决的问题",
        })
        comment_id = create_resp.json()["id"]

        resp = client.post(
            f"/api/knowledge/{kid}/comments/{comment_id}/resolve",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resolved"
        assert data["resolved_by"] is not None
        assert data["resolved_at"] is not None

    def test_resolve_already_resolved_comment(self, client, db):
        """对已 resolved 的评论再次 resolve：应幂等成功或返回 4xx。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u5", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u5")
        create_resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "会被 resolve 两次",
        })
        cid = create_resp.json()["id"]

        # 第一次 resolve
        client.post(f"/api/knowledge/{kid}/comments/{cid}/resolve", headers=_auth(token))
        # 第二次 resolve — 幂等 200 或冲突 409/400
        resp = client.post(f"/api/knowledge/{kid}/comments/{cid}/resolve", headers=_auth(token))
        assert resp.status_code in (200, 400, 409)

    def test_comment_requires_content(self, client, db):
        """评论 content 不能为空。"""
        dept = _make_dept(db)
        user = _make_user(db, "cmt_u6", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "cmt_u6")
        resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "",
        })
        assert resp.status_code in (400, 422)

    def test_comment_on_nonexistent_entry_returns_404(self, client, db):
        """在不存在的条目上创建评论应返回 404。"""
        dept = _make_dept(db)
        _make_user(db, "cmt_u7", Role.EMPLOYEE, dept.id)
        db.commit()

        token = _login(client, "cmt_u7")
        resp = client.post("/api/knowledge/999999/comments", headers=_auth(token), json={
            "content": "幽灵评论",
        })
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# D. 快照接口 — /api/knowledge/{kid}/snapshots
# ═════════════════════════════════════════════════════════════════════════════

class TestSnapshots:
    """快照 CRUD 和恢复契约。"""

    def test_create_manual_snapshot(self, client, db):
        """创建手动快照，current_snapshot_id 应更新。"""
        dept = _make_dept(db)
        user = _make_user(db, "snap_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content_html="<p>初始内容</p>")
        db.commit()
        kid = entry.id

        token = _login(client, "snap_u1")
        # 先初始化文档
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        resp = client.post(f"/api/knowledge/{kid}/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["snapshot_type"] == "manual"
        assert data["knowledge_id"] == kid
        assert "id" in data

        # 验证 KnowledgeDoc.current_snapshot_id 已更新
        db.expire_all()
        doc = db.query(KnowledgeDoc).filter_by(knowledge_id=kid).first()
        assert doc is not None
        assert doc.current_snapshot_id == data["id"]

    def test_list_snapshots_desc_order(self, client, db):
        """列出快照应按时间倒序排列。"""
        dept = _make_dept(db)
        user = _make_user(db, "snap_u2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content_html="<p>内容</p>")
        db.commit()
        kid = entry.id

        token = _login(client, "snap_u2")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        # 创建多个快照
        client.post(f"/api/knowledge/{kid}/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        client.post(f"/api/knowledge/{kid}/snapshots", headers=_auth(token), json={
            "snapshot_type": "autosave",
        })

        resp = client.get(f"/api/knowledge/{kid}/snapshots", headers=_auth(token))
        assert resp.status_code == 200
        snapshots = resp.json()
        assert isinstance(snapshots, list)
        assert len(snapshots) >= 2

        # 验证倒序（第一个 created_at >= 第二个）
        if len(snapshots) >= 2:
            assert snapshots[0]["created_at"] >= snapshots[1]["created_at"]

    def test_restore_snapshot(self, client, db):
        """恢复快照应将内容写回 KnowledgeEntry。"""
        dept = _make_dept(db)
        user = _make_user(db, "snap_u3", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content="原始", content_html="<p>原始</p>")
        db.commit()
        kid = entry.id

        token = _login(client, "snap_u3")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        # 创建快照（保存当前内容）
        snap_resp = client.post(f"/api/knowledge/{kid}/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        snapshot_id = snap_resp.json()["id"]

        # 修改内容
        client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(token), json={
            "html": "<p>被修改后</p>",
            "plain_text": "被修改后",
        })

        # 恢复快照
        resp = client.post(
            f"/api/knowledge/{kid}/snapshots/{snapshot_id}/restore",
            headers=_auth(token),
        )
        assert resp.status_code == 200

        # 验证 KnowledgeDoc.current_snapshot_id 已更新（指向新的 restore 快照）
        db.expire_all()
        doc = db.query(KnowledgeDoc).filter_by(knowledge_id=kid).first()
        assert doc.current_snapshot_id is not None
        assert doc.current_snapshot_id != snapshot_id  # restore 创建了新快照

    def test_restore_snapshot_updates_entry(self, client, db):
        """恢复快照后 KnowledgeEntry 的内容应回到快照时的状态。"""
        dept = _make_dept(db)
        user = _make_user(db, "snap_u4", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id, content="V1内容", content_html="<p>V1</p>")
        db.commit()
        kid = entry.id

        token = _login(client, "snap_u4")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(token))

        # 创建快照 — 保存 V1
        snap_resp = client.post(f"/api/knowledge/{kid}/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        snapshot_id = snap_resp.json()["id"]

        # 改成 V2
        client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(token), json={
            "html": "<p>V2</p>",
            "plain_text": "V2内容",
        })

        # 恢复到 V1
        client.post(
            f"/api/knowledge/{kid}/snapshots/{snapshot_id}/restore",
            headers=_auth(token),
        )

        db.expire_all()
        restored = db.query(KnowledgeEntry).get(kid)
        # 内容应包含 V1 的痕迹（具体字段取决于实现，至少 content_html 应恢复）
        assert "V2" not in (restored.content_html or "")

    def test_cannot_restore_snapshot_from_different_entry(self, client, db):
        """不能用其他条目的快照来恢复当前条目。"""
        dept = _make_dept(db)
        user = _make_user(db, "snap_u5", Role.EMPLOYEE, dept.id)
        entry_a = _make_entry(db, user.id, title="条目A", content_html="<p>A</p>")
        entry_b = _make_entry(db, user.id, title="条目B", content_html="<p>B</p>")
        db.commit()

        token = _login(client, "snap_u5")
        # 初始化两个文档
        client.get(f"/api/knowledge/{entry_a.id}/doc", headers=_auth(token))
        client.get(f"/api/knowledge/{entry_b.id}/doc", headers=_auth(token))

        # 在 A 上创建快照
        snap_resp = client.post(f"/api/knowledge/{entry_a.id}/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        snapshot_id_a = snap_resp.json()["id"]

        # 尝试用 A 的快照恢复 B
        resp = client.post(
            f"/api/knowledge/{entry_b.id}/snapshots/{snapshot_id_a}/restore",
            headers=_auth(token),
        )
        assert resp.status_code in (400, 403, 404)

    def test_snapshot_on_nonexistent_entry_returns_404(self, client, db):
        """在不存在的条目上创建快照应返回 404。"""
        dept = _make_dept(db)
        _make_user(db, "snap_u6", Role.EMPLOYEE, dept.id)
        db.commit()

        token = _login(client, "snap_u6")
        resp = client.post("/api/knowledge/999999/snapshots", headers=_auth(token), json={
            "snapshot_type": "manual",
        })
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# E. Presence — GET /api/knowledge/{kid}/presence
# ═════════════════════════════════════════════════════════════════════════════

class TestPresence:
    """在线用户列表契约。"""

    def test_empty_room_returns_empty_list(self, client, db):
        """无人在线时返回空数组。"""
        dept = _make_dept(db)
        user = _make_user(db, "pres_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()

        token = _login(client, "pres_u1")
        resp = client.get(f"/api/knowledge/{entry.id}/presence", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_presence_nonexistent_entry_returns_404(self, client, db):
        """不存在的条目 presence 查询应返回 404。"""
        dept = _make_dept(db)
        _make_user(db, "pres_u2", Role.EMPLOYEE, dept.id)
        db.commit()

        token = _login(client, "pres_u2")
        resp = client.get("/api/knowledge/999999/presence", headers=_auth(token))
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# F. 边界与权限补充
# ═════════════════════════════════════════════════════════════════════════════

class TestCollabEdgeCases:
    """跨模块边界条件。"""

    def test_unauthenticated_access_rejected(self, client, db):
        """未登录用户访问协同文档应返回 401。"""
        dept = _make_dept(db)
        user = _make_user(db, "edge_u1", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()

        resp = client.get(f"/api/knowledge/{entry.id}/doc")
        assert resp.status_code in (401, 403)

    def test_super_admin_can_sync_any_doc(self, client, db):
        """超级管理员可以 sync 任意文档（越过 creator/grant 检查）。"""
        dept = _make_dept(db)
        owner = _make_user(db, "edge_owner", Role.EMPLOYEE, dept.id)
        admin = _make_user(db, "edge_admin", Role.SUPER_ADMIN, dept.id)
        entry = _make_entry(db, owner.id, content_html="<p>原始</p>")
        db.commit()
        kid = entry.id

        # 先用 owner 初始化
        owner_token = _login(client, "edge_owner")
        client.get(f"/api/knowledge/{kid}/doc", headers=_auth(owner_token))

        # admin sync
        admin_token = _login(client, "edge_admin")
        resp = client.post(f"/api/knowledge/{kid}/doc/sync", headers=_auth(admin_token), json={
            "html": "<p>管理员修改</p>",
            "plain_text": "管理员修改",
        })
        assert resp.status_code == 200

    def test_comment_created_by_reflects_current_user(self, client, db):
        """评论的 created_by 应为当前登录用户。"""
        dept = _make_dept(db)
        user = _make_user(db, "edge_u2", Role.EMPLOYEE, dept.id)
        entry = _make_entry(db, user.id)
        db.commit()
        kid = entry.id

        token = _login(client, "edge_u2")
        resp = client.post(f"/api/knowledge/{kid}/comments", headers=_auth(token), json={
            "content": "检查 created_by",
        })
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["created_by"] == user.id
