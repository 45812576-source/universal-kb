"""Yjs 协同服务：管理协同文档生命周期、Yjs 状态持久化、snapshot 落库。

前端通过 WebSocket 连接，后端负责：
1. 文档初始化（从 content_html 导入 → Yjs state）
2. Yjs updates 持久化
3. 定时 snapshot 落库
4. Presence 管理

## 文档真源同步协议

编辑态：yjs_state 是唯一权威源
  - 前端通过 WS save_state 定期写入 yjs_state + 同时回写 content_html/content
  - snapshot 在 save_state 时或手动创建时生成

恢复态：snapshot → 覆写 content_html + 清空 yjs_state
  - restore_snapshot 读取 snapshot_json.html → 回写 content_html/content
  - 清空 yjs_state，前端重连时得到空 state + initialHtml 注入
  - 创建 "restore" 类型 snapshot 作为审计记录

三源优先级：yjs_state > content_html > snapshot_json
"""
from __future__ import annotations

import datetime
import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_doc import KnowledgeDoc, KnowledgeDocSnapshot, KnowledgeDocComment

logger = logging.getLogger(__name__)


def get_or_create_doc(db: Session, knowledge_id: int) -> KnowledgeDoc:
    """获取或创建协同文档。懒初始化。"""
    doc = (
        db.query(KnowledgeDoc)
        .filter(KnowledgeDoc.knowledge_id == knowledge_id)
        .first()
    )
    if doc:
        return doc

    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        raise ValueError(f"KnowledgeEntry {knowledge_id} not found")

    doc = KnowledgeDoc(
        knowledge_id=knowledge_id,
        doc_type="cloud_doc",
        yjs_doc_key=f"kb-{knowledge_id}-{uuid.uuid4().hex[:8]}",
        collab_status="initializing",
    )
    db.add(doc)
    db.flush()

    # 如果已有 content_html，创建 import snapshot
    if entry.content_html:
        snapshot = KnowledgeDocSnapshot(
            knowledge_id=knowledge_id,
            snapshot_type="import",
            snapshot_json={"html": entry.content_html},
            preview_text=(entry.content or "")[:500],
            created_by=entry.created_by,
        )
        db.add(snapshot)
        db.flush()
        doc.current_snapshot_id = snapshot.id

    doc.collab_status = "ready"
    db.commit()
    return doc


def save_yjs_state(
    db: Session,
    knowledge_id: int,
    state_bytes: bytes,
    html: Optional[str] = None,
    plain_text: Optional[str] = None,
) -> None:
    """持久化 Yjs 完整文档状态 + 同步回写 content_html/content。

    由前端定期发送完整 state 快照，后端直接替换存储。
    当 html/plain_text 一并传入时，同时更新 KnowledgeEntry，保证真源同步。
    """
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.knowledge_id == knowledge_id).first()
    if not doc:
        return
    doc.yjs_state = state_bytes
    doc.updated_at = datetime.datetime.utcnow()

    # 同步回写 content_html / content
    if html is not None:
        entry = db.get(KnowledgeEntry, knowledge_id)
        if entry:
            entry.content_html = html
            if plain_text is not None:
                entry.content = plain_text
            entry.updated_at = datetime.datetime.utcnow()

    db.commit()


def load_yjs_state(db: Session, knowledge_id: int) -> Optional[bytes]:
    """加载 Yjs 文档状态。"""
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.knowledge_id == knowledge_id).first()
    if not doc:
        return None
    return doc.yjs_state


def create_snapshot(
    db: Session,
    knowledge_id: int,
    snapshot_type: str = "autosave",
    snapshot_json: Optional[dict] = None,
    yjs_snapshot: Optional[bytes] = None,
    user_id: Optional[int] = None,
) -> KnowledgeDocSnapshot:
    """创建文档快照。

    当 snapshot_json 未提供时，自动从 KnowledgeEntry.content_html 抓取当前内容，
    确保快照始终包含可恢复的 HTML。
    """
    # 自动抓取当前内容
    if not snapshot_json:
        entry = db.get(KnowledgeEntry, knowledge_id)
        if entry and entry.content_html:
            snapshot_json = {
                "html": entry.content_html,
                "text": (entry.content or "")[:500],
            }

    preview = ""
    if snapshot_json and "text" in snapshot_json:
        preview = snapshot_json["text"][:500]

    snapshot = KnowledgeDocSnapshot(
        knowledge_id=knowledge_id,
        snapshot_type=snapshot_type,
        snapshot_json=snapshot_json,
        yjs_snapshot=yjs_snapshot,
        preview_text=preview,
        created_by=user_id,
    )
    db.add(snapshot)
    db.flush()

    # 更新 doc 的 current_snapshot_id
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.knowledge_id == knowledge_id).first()
    if doc:
        doc.current_snapshot_id = snapshot.id

    db.commit()
    return snapshot


def list_snapshots(db: Session, knowledge_id: int, limit: int = 50) -> list[dict]:
    """列出文档快照（含用户名）。"""
    snapshots = (
        db.query(KnowledgeDocSnapshot)
        .filter(KnowledgeDocSnapshot.knowledge_id == knowledge_id)
        .order_by(KnowledgeDocSnapshot.created_at.desc())
        .limit(limit)
        .all()
    )
    user_ids = {s.created_by for s in snapshots if s.created_by}
    names = _user_name_map(db, user_ids)

    return [
        {
            "id": s.id,
            "snapshot_type": s.snapshot_type,
            "preview_text": s.preview_text,
            "created_by": s.created_by,
            "created_by_name": names.get(s.created_by, f"用户#{s.created_by}") if s.created_by else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in snapshots
    ]


def restore_snapshot(
    db: Session,
    knowledge_id: int,
    snapshot_id: int,
    user_id: Optional[int] = None,
) -> Optional[KnowledgeDocSnapshot]:
    """恢复到指定快照版本。

    1. 读取 snapshot_json.html
    2. 回写 content_html / content
    3. 清空 yjs_state（前端重连时 initialHtml 注入）
    4. 创建 "restore" 审计快照
    """
    snapshot = db.get(KnowledgeDocSnapshot, snapshot_id)
    if not snapshot or snapshot.knowledge_id != knowledge_id:
        return None

    html = ""
    if snapshot.snapshot_json and isinstance(snapshot.snapshot_json, dict):
        html = snapshot.snapshot_json.get("html", "")

    if not html:
        return None

    # 回写 KnowledgeEntry
    entry = db.get(KnowledgeEntry, knowledge_id)
    if entry:
        entry.content_html = html
        # 简单提取纯文本（去 HTML 标签）
        import re
        entry.content = re.sub(r"<[^>]+>", "", html).strip()[:50000]
        entry.updated_at = datetime.datetime.utcnow()

    # 清空 yjs_state，让前端重连时用 initialHtml 重建
    doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.knowledge_id == knowledge_id).first()
    if doc:
        doc.yjs_state = None
        doc.updated_at = datetime.datetime.utcnow()

    # 创建审计快照
    restore_snap = KnowledgeDocSnapshot(
        knowledge_id=knowledge_id,
        snapshot_type="restore",
        snapshot_json={"html": html, "restored_from": snapshot_id},
        preview_text=(entry.content or "")[:500] if entry else "",
        created_by=user_id,
    )
    db.add(restore_snap)
    db.flush()

    if doc:
        doc.current_snapshot_id = restore_snap.id

    db.commit()
    return restore_snap


def sync_to_entry(db: Session, knowledge_id: int, html: str, plain_text: str) -> None:
    """将协同编辑的内容同步回 KnowledgeEntry.content_html / content。

    由前端定期触发或 snapshot 时触发。
    """
    entry = db.get(KnowledgeEntry, knowledge_id)
    if not entry:
        return
    entry.content_html = html
    entry.content = plain_text
    entry.updated_at = datetime.datetime.utcnow()
    db.commit()


# ── Comments ──────────────────────────────────────────────────────────


def create_comment(
    db: Session,
    knowledge_id: int,
    user_id: int,
    content: str,
    block_key: Optional[str] = None,
    anchor_from: Optional[int] = None,
    anchor_to: Optional[int] = None,
) -> KnowledgeDocComment:
    """创建评论。"""
    comment = KnowledgeDocComment(
        knowledge_id=knowledge_id,
        block_key=block_key,
        anchor_from=anchor_from,
        anchor_to=anchor_to,
        content=content,
        created_by=user_id,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def _user_name_map(db: Session, user_ids: set[int]) -> dict[int, str]:
    """批量查询用户 display_name。"""
    if not user_ids:
        return {}
    from app.models.user import User
    users = db.query(User.id, User.display_name, User.username).filter(User.id.in_(user_ids)).all()
    return {u.id: (u.display_name or u.username or f"用户#{u.id}") for u in users}


def list_comments(db: Session, knowledge_id: int) -> list[dict]:
    """列出文档评论（含用户名）。"""
    comments = (
        db.query(KnowledgeDocComment)
        .filter(KnowledgeDocComment.knowledge_id == knowledge_id)
        .order_by(KnowledgeDocComment.created_at)
        .all()
    )
    # 批量获取用户名
    user_ids = {c.created_by for c in comments if c.created_by}
    user_ids |= {c.resolved_by for c in comments if c.resolved_by}
    names = _user_name_map(db, user_ids)

    return [
        {
            "id": c.id,
            "block_key": c.block_key,
            "anchor_from": c.anchor_from,
            "anchor_to": c.anchor_to,
            "content": c.content,
            "status": c.status,
            "created_by": c.created_by,
            "created_by_name": names.get(c.created_by, f"用户#{c.created_by}"),
            "resolved_by": c.resolved_by,
            "resolved_by_name": names.get(c.resolved_by, "") if c.resolved_by else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        }
        for c in comments
    ]


def resolve_comment(db: Session, comment_id: int, user_id: int) -> bool:
    """解决评论。"""
    comment = db.get(KnowledgeDocComment, comment_id)
    if not comment:
        return False
    comment.status = "resolved"
    comment.resolved_by = user_id
    comment.resolved_at = datetime.datetime.utcnow()
    db.commit()
    return True


# ── Presence (in-memory) ──────────────────────────────────────────────

# room_key → {user_id: {name, color, cursor, last_seen}}
_presence_store: dict[str, dict[int, dict]] = {}

_COLORS = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7", "#DDA0DD", "#98D8C8", "#F7DC6F"]


def join_room(room_key: str, user_id: int, user_name: str) -> dict:
    """用户加入协同房间。返回 presence 信息。"""
    if room_key not in _presence_store:
        _presence_store[room_key] = {}

    color_idx = len(_presence_store[room_key]) % len(_COLORS)
    _presence_store[room_key][user_id] = {
        "name": user_name,
        "color": _COLORS[color_idx],
        "cursor": None,
        "last_seen": datetime.datetime.utcnow().isoformat(),
    }
    return _presence_store[room_key][user_id]


def leave_room(room_key: str, user_id: int) -> None:
    """用户离开协同房间。"""
    if room_key in _presence_store:
        _presence_store[room_key].pop(user_id, None)
        if not _presence_store[room_key]:
            del _presence_store[room_key]


def get_presence(room_key: str) -> dict[int, dict]:
    """获取房间内所有用户的 presence。"""
    return _presence_store.get(room_key, {})
