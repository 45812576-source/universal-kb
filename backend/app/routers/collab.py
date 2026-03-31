"""协同编辑路由：WebSocket（Yjs sync）+ REST（文档/快照/评论）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.auth_service import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["collab"])


# ── Pydantic schemas ────────────────────────────────────────────────

class DocInitResponse(BaseModel):
    id: int
    knowledge_id: int
    doc_type: str
    yjs_doc_key: str
    collab_status: str
    has_yjs_state: bool
    current_snapshot_id: Optional[int] = None


class SnapshotCreate(BaseModel):
    snapshot_type: str = "manual"
    snapshot_json: Optional[dict] = None


class CommentCreate(BaseModel):
    content: str
    block_key: Optional[str] = None
    anchor_from: Optional[int] = None
    anchor_to: Optional[int] = None


class SyncContentRequest(BaseModel):
    html: str
    plain_text: str


# ── WebSocket: Yjs 协同 ────────────────────────────────────────────

# room_key → set[WebSocket]
_rooms: dict[str, set[WebSocket]] = {}
# websocket → (room_key, user_id, user_name)
_ws_meta: dict[WebSocket, tuple[str, int, str]] = {}


def _authenticate_ws(token: str, db: Session) -> Optional[User]:
    """从 token 解析用户，用于 WebSocket 鉴权。"""
    payload = decode_token(token)
    if not payload:
        return None
    user = db.get(User, int(payload.get("sub", 0)))
    if not user or not user.is_active:
        return None
    return user


@router.websocket("/collab/{knowledge_id}")
async def collab_ws(
    websocket: WebSocket,
    knowledge_id: int,
    token: str = Query(...),
):
    """Yjs 协同 WebSocket 端点。

    协议：
    - 连接时通过 ?token=xxx 鉴权
    - 连接后服务端发送 JSON: {"type":"sync_state","state":"<base64>"} 完整 Yjs 快照
    - 二进制消息：Yjs 增量 update，广播给同房间其他人（不持久化）
    - 文本 JSON {"type":"save_state","state":"<base64>"}：前端定期发送完整快照，后端持久化
    - 文本 JSON {"type":"awareness",...}：转发给同房间其他人
    - 连接关闭前前端应发送最终 save_state
    """
    import base64
    import json

    db: Session = next(get_db())
    try:
        user = _authenticate_ws(token, db)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        from app.services.collab_service import get_or_create_doc, load_yjs_state, save_yjs_state, join_room, leave_room

        doc = get_or_create_doc(db, knowledge_id)
        room_key = doc.yjs_doc_key

        await websocket.accept()

        # 加入房间
        if room_key not in _rooms:
            _rooms[room_key] = set()
        _rooms[room_key].add(websocket)
        _ws_meta[websocket] = (room_key, user.id, user.username)

        presence = join_room(room_key, user.id, user.username)
        logger.info(f"[Collab] User {user.username} joined room {room_key}")

        # 发送已有的完整 Yjs 状态（JSON 包装，便于前端区分）
        yjs_state = load_yjs_state(db, knowledge_id)
        await websocket.send_text(json.dumps({
            "type": "sync_state",
            "state": base64.b64encode(yjs_state).decode() if yjs_state else "",
        }))

        # 广播 join 事件
        await _broadcast_text(room_key, websocket, {
            "type": "user_joined",
            "user_id": user.id,
            "user_name": user.username,
            "color": presence["color"],
        })

        try:
            while True:
                message = await websocket.receive()

                if "bytes" in message and message["bytes"]:
                    # Yjs 增量 update → 仅广播给其他客户端
                    await _broadcast_bytes(room_key, websocket, message["bytes"])

                elif "text" in message and message["text"]:
                    try:
                        msg = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "save_state":
                        # 前端发来完整 Yjs state 快照 + html/text → 持久化 + 同步回写
                        state_b64 = msg.get("state", "")
                        if state_b64:
                            try:
                                save_db = next(get_db())
                                save_yjs_state(
                                    save_db, knowledge_id,
                                    base64.b64decode(state_b64),
                                    html=msg.get("html"),
                                    plain_text=msg.get("text"),
                                )
                            except Exception as e:
                                logger.error(f"[Collab] Failed to save Yjs state: {e}")

                    elif msg_type == "awareness":
                        await _broadcast_text(room_key, websocket, msg)

                    elif msg_type == "cursor":
                        await _broadcast_text(room_key, websocket, {
                            "type": "cursor",
                            "user_id": user.id,
                            "user_name": user.username,
                            "cursor": msg.get("cursor"),
                        })

        except WebSocketDisconnect:
            pass
        finally:
            _rooms.get(room_key, set()).discard(websocket)
            _ws_meta.pop(websocket, None)
            if room_key in _rooms and not _rooms[room_key]:
                del _rooms[room_key]

            leave_room(room_key, user.id)
            logger.info(f"[Collab] User {user.username} left room {room_key}")

            await _broadcast_text(room_key, None, {
                "type": "user_left",
                "user_id": user.id,
                "user_name": user.username,
            })
    finally:
        db.close()


async def _broadcast_bytes(room_key: str, sender: WebSocket, data: bytes):
    """广播二进制消息给同房间其他连接。"""
    peers = _rooms.get(room_key, set())
    for ws in list(peers):
        if ws is sender:
            continue
        try:
            await ws.send_bytes(data)
        except Exception:
            peers.discard(ws)


async def _broadcast_text(room_key: str, sender: Optional[WebSocket], msg: dict):
    """广播 JSON 消息给同房间其他连接。"""
    import json
    text = json.dumps(msg, ensure_ascii=False)
    peers = _rooms.get(room_key, set())
    for ws in list(peers):
        if ws is sender:
            continue
        try:
            await ws.send_text(text)
        except Exception:
            peers.discard(ws)


# ── REST: 文档初始化 ────────────────────────────────────────────────

@router.get("/{kid}/doc")
def get_doc(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取或初始化协同文档信息。"""
    from app.services.collab_service import get_or_create_doc
    try:
        doc = get_or_create_doc(db, kid)
    except ValueError:
        raise HTTPException(404, "Knowledge entry not found")
    return DocInitResponse(
        id=doc.id,
        knowledge_id=doc.knowledge_id,
        doc_type=doc.doc_type,
        yjs_doc_key=doc.yjs_doc_key,
        collab_status=doc.collab_status,
        has_yjs_state=doc.yjs_state is not None,
        current_snapshot_id=doc.current_snapshot_id,
    )


@router.post("/{kid}/doc/sync")
def sync_doc_content(
    kid: int,
    req: SyncContentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将协同编辑的内容同步回 KnowledgeEntry。"""
    from app.models.knowledge import KnowledgeEntry, KnowledgeEditGrant
    from app.models.user import Role

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    # 权限：创建者 / 超管 / 有 edit grant
    if entry.created_by != user.id and user.role != Role.SUPER_ADMIN:
        grant = db.query(KnowledgeEditGrant).filter_by(
            entry_id=kid, user_id=user.id
        ).first()
        if not grant:
            raise HTTPException(403, "No permission to sync this document")

    if not req.html and not req.plain_text:
        raise HTTPException(422, "内容不能为空")

    from app.services.collab_service import sync_to_entry
    sync_to_entry(db, kid, req.html, req.plain_text)
    return {"ok": True}


# ── REST: 快照 ─────────────────────────────────────────────────────

@router.get("/{kid}/snapshots")
def list_snapshots(
    kid: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出文档快照历史。"""
    from app.services.collab_service import list_snapshots as _list
    return _list(db, kid, limit=limit)


@router.post("/{kid}/snapshots")
def create_snapshot(
    kid: int,
    req: SnapshotCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动创建快照。"""
    from app.models.knowledge import KnowledgeEntry
    if not db.get(KnowledgeEntry, kid):
        raise HTTPException(404, "Knowledge entry not found")
    from app.services.collab_service import create_snapshot as _create
    snap = _create(
        db, kid,
        snapshot_type=req.snapshot_type,
        snapshot_json=req.snapshot_json,
        user_id=user.id,
    )
    return {
        "id": snap.id,
        "knowledge_id": kid,
        "snapshot_type": snap.snapshot_type,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }


@router.post("/{kid}/snapshots/{sid}/restore")
def restore_snapshot(
    kid: int,
    sid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """恢复到指定快照版本。清空 yjs_state，回写 content_html。"""
    from app.models.knowledge import KnowledgeEntry, KnowledgeEditGrant
    from app.models.user import Role

    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")

    # 权限：创建者 / 超管 / 有 edit grant
    if entry.created_by != user.id and user.role != Role.SUPER_ADMIN:
        grant = db.query(KnowledgeEditGrant).filter_by(
            entry_id=kid, user_id=user.id
        ).first()
        if not grant:
            raise HTTPException(403, "No permission to restore snapshot")

    from app.services.collab_service import restore_snapshot as _restore
    snap = _restore(db, kid, sid, user_id=user.id)
    if not snap:
        raise HTTPException(404, "Snapshot not found or has no HTML content")
    return {
        "ok": True,
        "snapshot_id": snap.id,
        "snapshot_type": snap.snapshot_type,
    }


# ── REST: 评论 ─────────────────────────────────────────────────────

@router.get("/{kid}/comments")
def list_comments(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出文档评论。"""
    from app.services.collab_service import list_comments as _list
    return _list(db, kid)


@router.post("/{kid}/comments")
def create_comment(
    kid: int,
    req: CommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建评论。"""
    from app.models.knowledge import KnowledgeEntry
    if not req.content or not req.content.strip():
        raise HTTPException(422, "评论内容不能为空")
    entry = db.get(KnowledgeEntry, kid)
    if not entry:
        raise HTTPException(404, "Knowledge entry not found")
    from app.services.collab_service import create_comment as _create
    comment = _create(
        db, kid,
        user_id=user.id,
        content=req.content,
        block_key=req.block_key,
        anchor_from=req.anchor_from,
        anchor_to=req.anchor_to,
    )
    return {
        "id": comment.id,
        "content": comment.content,
        "block_key": comment.block_key,
        "anchor_from": comment.anchor_from,
        "anchor_to": comment.anchor_to,
        "status": comment.status,
        "created_by": comment.created_by,
        "created_by_name": user.display_name or user.username,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


@router.post("/{kid}/comments/{comment_id}/resolve")
def resolve_comment(
    kid: int,
    comment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解决评论。"""
    from app.services.collab_service import resolve_comment as _resolve
    from app.models.knowledge_doc import KnowledgeDocComment
    ok = _resolve(db, comment_id, user.id)
    if not ok:
        raise HTTPException(404, "Comment not found")
    comment = db.get(KnowledgeDocComment, comment_id)
    return {
        "ok": True,
        "status": comment.status if comment else "resolved",
        "resolved_by": comment.resolved_by if comment else user.id,
        "resolved_at": comment.resolved_at.isoformat() if comment and comment.resolved_at else None,
    }


# ── REST: Presence ─────────────────────────────────────────────────

@router.get("/{kid}/presence")
def get_presence(
    kid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取文档在线用户。"""
    from app.services.collab_service import get_or_create_doc, get_presence as _get
    try:
        doc = get_or_create_doc(db, kid)
    except ValueError:
        raise HTTPException(404, "Knowledge entry not found")
    presence = _get(doc.yjs_doc_key)
    return [
        {"user_id": uid, **info}
        for uid, info in presence.items()
    ]
