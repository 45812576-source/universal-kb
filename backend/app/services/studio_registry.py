"""统一实例注册表 — workspace 是持久身份对象，进程只是可回收壳。"""
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models.opencode import StudioRegistration, OpenCodeWorkspaceMapping
from app.models.conversation import Conversation
from app.models.workspace import Workspace
from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeSessionInfo:
    """opencode.db 中单个 session 的摘要信息。"""
    id: str
    title: Optional[str]
    directory: Optional[str]
    message_count: int
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass
class StudioEntryResolution:
    registration_id: int
    conversation_id: int
    workspace_root: str
    project_dir: str
    runtime_status: str  # stopped|starting|running|unhealthy
    runtime_port: Optional[int]
    generation: int
    needs_recover: bool
    recent_conversation_ids: list  # le-desk Conversation 的 id（兼容历史）
    last_active_at: Optional[str]  # ISO 格式最近活跃时间
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None
    registration_key: str = "default"
    # session 概要（entry 只给总数和健康状态，详情走 GET /sessions）
    session_total: int = 0
    session_db_health: str = "unknown"  # healthy | degraded | missing | error
    session_db_source: str = "missing"  # runtime_data | migrated | missing
    session_db_path: Optional[str] = None
    migration_state: str = "none"  # none | migrated | needs_repair | error


def _registration_key(
    workspace_type: str,
    *,
    workspace_id: Optional[int] = None,
    project_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
) -> str:
    parts = [workspace_type]
    if workspace_id is not None:
        parts.append(f"ws:{workspace_id}")
    if project_id is not None:
        parts.append(f"proj:{project_id}")
    if target_type:
        parts.append(f"target:{target_type}")
    if target_id is not None:
        parts.append(f"tid:{target_id}")
    return ":".join(parts) if len(parts) > 1 else "default"


def resolve_entry(
    db: Session,
    user: User,
    workspace_type: str,
    skill_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    project_id: Optional[int] = None,
) -> StudioEntryResolution:
    """查或创建注册表记录 + 确保 conversation 存在，返回稳定入口。

    此函数是前端入口页的唯一后端依赖，保证：
    - 同一 (user, workspace_type) 永远返回同一 registration
    - primary_conversation_id 始终有效
    - workspace_root/project_dir 始终存在于磁盘
    - skill_studio 使用独立 project_dir（workspace_root/skill_studio/），与 opencode cwd 隔离
    """
    from app.services.workdir_manager import (
        _workspace_root_for_user,
        _workspace_project_dir,
        _workspace_skill_studio_dir,
        ensure_workspace_layout,
    )

    reg_key = _registration_key(
        workspace_type,
        workspace_id=workspace_id,
        project_id=project_id,
        target_type="skill" if skill_id else None,
        target_id=skill_id,
    )

    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user.id,
            StudioRegistration.workspace_type == workspace_type,
            StudioRegistration.registration_key == reg_key,
        )
        .first()
    )
    if reg is None and reg_key == "default":
        reg = (
            db.query(StudioRegistration)
            .filter(
                StudioRegistration.user_id == user.id,
                StudioRegistration.workspace_type == workspace_type,
            )
            .first()
        )
        if reg is not None and getattr(reg, "registration_key", None) != "default":
            reg = None

    # 首次：创建注册记录
    if reg is None:
        workspace_root = _workspace_root_for_user(user.id, user.display_name or "")
        # skill_studio 使用独立目录，不再与 opencode 共用 project_dir
        if workspace_type == "skill_studio":
            project_dir = _workspace_skill_studio_dir(workspace_root)
        else:
            project_dir = _workspace_project_dir(workspace_root)

        # skill_studio 没有独立 runtime，标记 n/a
        reg = StudioRegistration(
            user_id=user.id,
            workspace_type=workspace_type,
            workspace_id=workspace_id,
            project_id=project_id,
            target_type="skill" if skill_id else None,
            target_id=skill_id,
            registration_key=reg_key,
            workspace_root=workspace_root,
            project_dir=project_dir,
            runtime_status="stopped" if workspace_type == "opencode" else "n/a",
            generation=0,
        )
        db.add(reg)
        db.flush()
    else:
        changed = False
        if getattr(reg, "registration_key", None) in (None, ""):
            reg.registration_key = reg_key
            changed = True
        if workspace_id is not None and reg.workspace_id != workspace_id:
            reg.workspace_id = workspace_id
            changed = True
        if project_id is not None and reg.project_id != project_id:
            reg.project_id = project_id
            changed = True
        if skill_id is not None and (reg.target_type != "skill" or reg.target_id != skill_id):
            reg.target_type = "skill"
            reg.target_id = skill_id
            changed = True
        if changed:
            db.flush()

    # 确保磁盘目录存在
    if reg.workspace_root:
        ensure_workspace_layout(reg.workspace_root, display_name=user.display_name or "")

    # 确保 primary_conversation_id 有效
    conv_valid = False
    if reg.primary_conversation_id:
        conv = db.get(Conversation, reg.primary_conversation_id)
        if conv and conv.is_active and conv.user_id == user.id:
            conv_valid = True

    if not conv_valid:
        # 尝试找已有的同类型 conversation
        conv = _find_or_create_conversation(db, user, workspace_type)
        reg.primary_conversation_id = conv.id

    # 当指定 skill_id 时，查找/创建该 skill 的独立 conversation
    if skill_id:
        target_conv_id = _find_or_create_skill_conversation(db, user, workspace_type, skill_id).id
    else:
        target_conv_id = reg.primary_conversation_id

    now = datetime.datetime.utcnow()
    reg.last_active_at = now
    reg.last_verified_at = now

    # 查询该用户所有活跃的同 workspace_type conversation，返回最近 5 个 id
    ws = (
        db.query(Workspace)
        .filter(Workspace.workspace_type == workspace_type)
        .first()
    )
    recent_conversation_ids: list[int] = []
    if ws:
        recent_convs = (
            db.query(Conversation.id)
            .filter(
                Conversation.user_id == user.id,
                Conversation.workspace_id == ws.id,
                Conversation.is_active == True,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(5)
            .all()
        )
        recent_conversation_ids = [row[0] for row in recent_convs]

    db.commit()

    needs_recover = reg.runtime_status in ("stopped", "unhealthy")

    # 探测 session db 状态（仅 opencode 需要）
    if workspace_type == "opencode":
        probe = probe_session_db(reg.workspace_root)
    else:
        probe = SessionDBProbe(
            db_path=None, db_health="n/a", db_source="n/a",
            total=0, error=None, migration_state="none",
        )

    return StudioEntryResolution(
        registration_id=reg.id,
        conversation_id=target_conv_id,
        workspace_root=reg.workspace_root,
        project_dir=reg.project_dir,
        runtime_status=reg.runtime_status,
        runtime_port=reg.runtime_port,
        generation=reg.generation,
        needs_recover=needs_recover,
        recent_conversation_ids=recent_conversation_ids,
        last_active_at=reg.last_active_at.isoformat() if reg.last_active_at else None,
        workspace_id=reg.workspace_id,
        project_id=reg.project_id,
        registration_key=reg.registration_key or "default",
        session_total=probe.total,
        session_db_health=probe.db_health,
        session_db_source=probe.db_source,
        session_db_path=probe.db_path,
        migration_state=probe.migration_state,
    )


def _find_or_create_conversation(
    db: Session, user: User, workspace_type: str
) -> Conversation:
    """查找或创建 workspace_type 对应的 conversation。"""
    # 找到对应 workspace
    ws = (
        db.query(Workspace)
        .filter(Workspace.workspace_type == workspace_type)
        .first()
    )

    if ws:
        # 找已有 conversation
        existing = (
            db.query(Conversation)
            .filter(
                Conversation.user_id == user.id,
                Conversation.workspace_id == ws.id,
                Conversation.is_active == True,
            )
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if existing:
            return existing

        # 创建新 conversation
        title_map = {
            "opencode": "OpenCode 开发",
            "skill_studio": "Skill Studio",
        }
        conv = Conversation(
            user_id=user.id,
            workspace_id=ws.id,
            title=title_map.get(workspace_type, ws.name),
        )
        db.add(conv)
        db.flush()
        return conv

    # 无 workspace 记录时创建独立 conversation
    conv = Conversation(
        user_id=user.id,
        title=f"{workspace_type} 会话",
    )
    db.add(conv)
    db.flush()
    return conv


def _find_or_create_skill_conversation(
    db: Session, user: User, workspace_type: str, skill_id: int
) -> Conversation:
    """查找或创建某个 Skill 的独立 conversation。"""
    ws = (
        db.query(Workspace)
        .filter(Workspace.workspace_type == workspace_type)
        .first()
    )
    ws_id = ws.id if ws else None

    # 按 user_id + workspace_id + skill_id 精确匹配
    filters = [
        Conversation.user_id == user.id,
        Conversation.skill_id == skill_id,
        Conversation.is_active == True,
    ]
    if ws_id:
        filters.append(Conversation.workspace_id == ws_id)

    existing = (
        db.query(Conversation)
        .filter(*filters)
        .order_by(Conversation.updated_at.desc())
        .first()
    )
    if existing:
        return existing

    # 创建新 conversation
    from app.models.skill import Skill as SkillModel
    skill = db.get(SkillModel, skill_id)
    title = f"Skill Studio - {skill.name}" if skill else f"Skill Studio - Skill #{skill_id}"

    conv = Conversation(
        user_id=user.id,
        workspace_id=ws_id,
        skill_id=skill_id,
        title=title,
    )
    db.add(conv)
    db.flush()
    return conv


def update_runtime_status(
    db: Session,
    user_id: int,
    workspace_type: str,
    status: str,
    port: Optional[int] = None,
    bump_generation: bool = False,
    registration_key: str = "default",
) -> Optional[StudioRegistration]:
    """更新运行时状态。"""
    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
            StudioRegistration.registration_key == registration_key,
        )
        .first()
    )
    if not reg and registration_key == "default":
        reg = (
            db.query(StudioRegistration)
            .filter(
                StudioRegistration.user_id == user_id,
                StudioRegistration.workspace_type == workspace_type,
            )
            .first()
        )
    if not reg:
        return None

    old_status = reg.runtime_status

    # 先判断恢复（用旧状态），再写入新状态
    if status == "running" and old_status in ("stopped", "unhealthy"):
        reg.last_recovered_at = datetime.datetime.utcnow()

    reg.runtime_status = status
    if port is not None:
        reg.runtime_port = port
    if bump_generation:
        reg.generation = (reg.generation or 0) + 1
    if status == "running":
        reg.last_active_at = datetime.datetime.utcnow()

    db.commit()
    return reg


def resolve_studio_project_dir(
    db: Session,
    user_id: int,
    workspace_type: str,
    *,
    registration_key: str = "default",
) -> Optional[str]:
    """返回该用户工作台的 project_dir。skill_studio 使用独立目录，不再与 opencode 共用。"""
    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
            StudioRegistration.registration_key == registration_key,
        )
        .first()
    )
    if not reg and registration_key == "default":
        reg = (
            db.query(StudioRegistration)
            .filter(
                StudioRegistration.user_id == user_id,
                StudioRegistration.workspace_type == workspace_type,
            )
            .first()
        )
    if reg and reg.project_dir:
        return reg.project_dir
    # skill_studio 不再 fallback 到 opencode 的 project_dir
    return None


def get_registration(
    db: Session, user_id: int, workspace_type: str, registration_key: str = "default"
) -> Optional[StudioRegistration]:
    """只读查询。"""
    reg = (
        db.query(StudioRegistration)
        .filter(
            StudioRegistration.user_id == user_id,
            StudioRegistration.workspace_type == workspace_type,
            StudioRegistration.registration_key == registration_key,
        )
        .first()
    )
    if not reg and registration_key == "default":
        return (
            db.query(StudioRegistration)
            .filter(
                StudioRegistration.user_id == user_id,
                StudioRegistration.workspace_type == workspace_type,
            )
            .first()
        )
    return reg


def migrate_existing_users(db: Session) -> dict:
    """迁移现有用户数据到注册表。返回 {migrated: int, errors: [...]}。"""
    from app.services.workdir_manager import _workspace_root_for_user, _workspace_project_dir, _workspace_skill_studio_dir

    migrated = 0
    errors = []

    # 1. 从 OpenCodeWorkspaceMapping 补建 opencode registration
    mappings = db.query(OpenCodeWorkspaceMapping).all()
    for m in mappings:
        try:
            existing = (
                db.query(StudioRegistration)
                .filter(
                    StudioRegistration.user_id == m.user_id,
                    StudioRegistration.workspace_type == "opencode",
                )
                .first()
            )
            if existing:
                continue

            workspace_root = m.directory or _workspace_root_for_user(m.user_id)
            project_dir = _workspace_project_dir(workspace_root)

            # 找最近活跃的 opencode conversation
            ws = db.query(Workspace).filter(Workspace.workspace_type == "opencode").first()
            conv_id = None
            if ws:
                conv = (
                    db.query(Conversation)
                    .filter(
                        Conversation.user_id == m.user_id,
                        Conversation.workspace_id == ws.id,
                        Conversation.is_active == True,
                    )
                    .order_by(Conversation.updated_at.desc())
                    .first()
                )
                if conv:
                    conv_id = conv.id

            reg = StudioRegistration(
                user_id=m.user_id,
                workspace_type="opencode",
                registration_key="default",
                workspace_root=workspace_root,
                project_dir=project_dir,
                primary_conversation_id=conv_id,
                runtime_status="stopped",
                generation=0,
            )
            db.add(reg)
            migrated += 1
        except Exception as e:
            errors.append(f"opencode user={m.user_id}: {e}")

    # 2. 补建 skill_studio registration
    skill_ws = db.query(Workspace).filter(Workspace.workspace_type == "skill_studio").first()
    if skill_ws:
        convs = (
            db.query(Conversation)
            .filter(
                Conversation.workspace_id == skill_ws.id,
                Conversation.is_active == True,
            )
            .all()
        )
        seen_users = set()
        for c in convs:
            if c.user_id in seen_users:
                continue
            seen_users.add(c.user_id)
            try:
                existing = (
                    db.query(StudioRegistration)
                    .filter(
                        StudioRegistration.user_id == c.user_id,
                        StudioRegistration.workspace_type == "skill_studio",
                    )
                    .first()
                )
                if existing:
                    continue

                # skill_studio 使用独立目录
                workspace_root = _workspace_root_for_user(c.user_id)
                project_dir = _workspace_skill_studio_dir(workspace_root)
                reg = StudioRegistration(
                    user_id=c.user_id,
                    workspace_type="skill_studio",
                    workspace_id=skill_ws.id,
                    target_type="skill" if c.skill_id else None,
                    target_id=c.skill_id,
                    registration_key=_registration_key(
                        "skill_studio",
                        workspace_id=skill_ws.id,
                        target_type="skill" if c.skill_id else None,
                        target_id=c.skill_id,
                    ),
                    workspace_root=workspace_root,
                    project_dir=project_dir,
                    primary_conversation_id=c.id,
                    runtime_status="n/a",
                    generation=0,
                )
                db.add(reg)
                migrated += 1
            except Exception as e:
                errors.append(f"skill_studio user={c.user_id}: {e}")

    db.commit()
    logger.info(f"[StudioRegistry] 迁移完成: migrated={migrated}, errors={len(errors)}")
    return {"migrated": migrated, "errors": errors}


def migrate_skill_conversations(db: Session, user: User) -> dict:
    """将用户旧共享 Skill Studio conversation 中按 metadata.skill_id 标记的消息
    迁移到各 Skill 的独立 conversation。

    幂等：同一消息不会被重复迁移（按 conversation_id 判断归属）。
    """
    from app.models.conversation import Message

    ws = (
        db.query(Workspace)
        .filter(Workspace.workspace_type == "skill_studio")
        .first()
    )
    if not ws:
        return {"migrated": 0, "skills": []}

    # 找到用户在 skill_studio workspace 下、没有 skill_id 的 conversation（即旧共享会话）
    shared_convs = (
        db.query(Conversation)
        .filter(
            Conversation.user_id == user.id,
            Conversation.workspace_id == ws.id,
            Conversation.skill_id == None,
            Conversation.is_active == True,
        )
        .all()
    )
    if not shared_convs:
        return {"migrated": 0, "skills": []}

    shared_conv_ids = [c.id for c in shared_convs]

    # 扫描这些 conversation 中所有含 skill_id 的消息（MySQL JSON 兼容）
    from sqlalchemy import func, cast, Integer
    msgs = (
        db.query(Message)
        .filter(
            Message.conversation_id.in_(shared_conv_ids),
            func.json_extract(Message.metadata_, "$.skill_id").isnot(None),
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    migrated = 0
    skill_ids_seen = set()
    # 缓存 skill_id -> target conversation
    _conv_cache: dict[int, Conversation] = {}

    for msg in msgs:
        meta = msg.metadata_ or {}
        skill_id = meta.get("skill_id")
        if not skill_id:
            continue

        # 获取或创建该 skill 的独立 conversation
        if skill_id not in _conv_cache:
            _conv_cache[skill_id] = _find_or_create_skill_conversation(
                db, user, "skill_studio", skill_id
            )

        target_conv = _conv_cache[skill_id]

        # 如果消息已经在目标 conversation 里就跳过（幂等）
        if msg.conversation_id == target_conv.id:
            continue

        # 移动消息到目标 conversation
        msg.conversation_id = target_conv.id
        migrated += 1
        skill_ids_seen.add(skill_id)

    db.commit()
    logger.info(
        f"[StudioRegistry] Skill conversation 迁移: user={user.id} "
        f"migrated={migrated} skills={list(skill_ids_seen)}"
    )
    return {"migrated": migrated, "skills": list(skill_ids_seen)}


def migrate_legacy_session_db(workspace_root: str) -> str:
    """一次性迁移 legacy opencode.db 到 runtime/data/ 唯一路径。

    返回迁移状态: "none" | "migrated" | "needs_repair" | "error"
    - none: legacy 不存在，无需迁移
    - migrated: 本次或之前已迁移成功
    - needs_repair: 两边都存在且不一致
    - error: 迁移过程出错
    """
    import os
    import shutil
    import sqlite3
    import time

    canonical = os.path.join(workspace_root, "runtime", "data", "opencode", "opencode.db")
    legacy = os.path.join(workspace_root, ".local", "share", "opencode", "opencode.db")

    def _copy_db_family(src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            src_sidecar = src + suffix
            dst_sidecar = dst + suffix
            if os.path.exists(src_sidecar):
                shutil.copy2(src_sidecar, dst_sidecar)
            elif os.path.exists(dst_sidecar):
                os.remove(dst_sidecar)

    def _session_stats(path: str) -> tuple[int, int]:
        con = sqlite3.connect(path, timeout=5)
        try:
            count = con.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            latest = con.execute("SELECT COALESCE(MAX(time_updated), 0) FROM session").fetchone()[0] or 0
            return int(count), int(latest)
        finally:
            con.close()

    legacy_exists = os.path.exists(legacy)
    canonical_exists = os.path.exists(canonical)

    if not legacy_exists:
        return "none"

    if not canonical_exists:
        # legacy 存在、canonical 不存在 → 迁移
        try:
            _copy_db_family(legacy, canonical)
            logger.info(f"[SessionDB] 迁移成功: {legacy} → {canonical}")
            return "migrated"
        except Exception as e:
            logger.error(f"[SessionDB] 迁移失败: {e}")
            return "error"

    # 两边都存在 → 一致性检测
    try:
        new_count, new_latest = _session_stats(canonical)
        old_count, old_latest = _session_stats(legacy)
        if new_count > old_count or (new_count == old_count and new_latest >= old_latest):
            # canonical 是超集或相等，视为已迁移
            return "migrated"

        # canonical 比 legacy 少，或同数量但更旧：自动修复为 legacy 最新快照
        backup = f"{canonical}.bak.{int(time.time())}"
        try:
            shutil.copy2(canonical, backup)
        except Exception:
            backup = ""

        _copy_db_family(legacy, canonical)
        logger.warning(
            "[SessionDB] 检测到 canonical 落后，已自动修复: "
            f"legacy_count={old_count}, canonical_count={new_count}, "
            f"legacy_latest={old_latest}, canonical_latest={new_latest}, "
            f"backup={backup or 'none'}"
        )
        return "migrated"
    except Exception as e:
        logger.error(f"[SessionDB] 一致性检测失败: {e}")
        return "needs_repair"


@dataclass
class SessionDBProbe:
    """session db 探测结果。"""
    db_path: Optional[str]
    db_health: str  # healthy | missing | error
    db_source: str  # runtime_data | missing
    total: int
    error: Optional[str]
    migration_state: str  # none | migrated | needs_repair | error


def probe_session_db(workspace_root: str) -> SessionDBProbe:
    """探测 session db 状态并返回诊断结果。唯一读取路径：runtime/data/opencode/opencode.db。"""
    import os
    import sqlite3

    # 先做迁移
    migration_state = migrate_legacy_session_db(workspace_root)

    canonical = os.path.join(workspace_root, "runtime", "data", "opencode", "opencode.db")
    if not os.path.exists(canonical):
        return SessionDBProbe(
            db_path=None, db_health="missing", db_source="missing",
            total=0, error=None, migration_state=migration_state,
        )

    try:
        con = sqlite3.connect(canonical, timeout=5)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "session" not in tables:
            con.close()
            return SessionDBProbe(
                db_path=canonical, db_health="healthy", db_source="runtime_data",
                total=0, error=None, migration_state=migration_state,
            )
        total = con.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        con.close()
        health = "healthy" if migration_state != "needs_repair" else "degraded"
        return SessionDBProbe(
            db_path=canonical, db_health=health, db_source="runtime_data",
            total=total, error=None, migration_state=migration_state,
        )
    except Exception as e:
        return SessionDBProbe(
            db_path=canonical, db_health="error", db_source="runtime_data",
            total=0, error=f"{type(e).__name__}: {e}", migration_state=migration_state,
        )


def read_opencode_sessions(
    workspace_root: str, page: int = 1, page_size: int = 20,
) -> tuple[list, int, SessionDBProbe]:
    """分页读取 opencode.db 的 session 摘要。唯一路径：runtime/data/opencode/opencode.db。

    返回: (sessions, total, probe)
    """
    import os
    import sqlite3

    probe = probe_session_db(workspace_root)
    if not probe.db_path or probe.db_health in ("missing", "error"):
        return [], 0, probe

    try:
        con = sqlite3.connect(probe.db_path, timeout=5)
        con.row_factory = sqlite3.Row

        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "session" not in tables:
            con.close()
            return [], 0, probe

        total = con.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        offset = (page - 1) * page_size

        has_message = "message" in tables
        if has_message:
            rows = con.execute(
                "SELECT s.id, s.title, s.directory, s.project_id, "
                "s.time_created, s.time_updated, COUNT(m.id) AS msg_count "
                "FROM session s LEFT JOIN message m ON m.session_id = s.id "
                "GROUP BY s.id "
                "ORDER BY s.time_updated DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, title, directory, project_id, "
                "time_created, time_updated, 0 AS msg_count "
                "FROM session ORDER BY time_updated DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall()

        sessions = [
            OpenCodeSessionInfo(
                id=row["id"],
                title=row["title"],
                directory=row["directory"],
                message_count=row["msg_count"],
                created_at=row["time_created"],
                updated_at=row["time_updated"],
            )
            for row in rows
        ]

        con.close()
        return sessions, total, probe
    except Exception as e:
        probe.db_health = "error"
        probe.error = f"{type(e).__name__}: {e}"
        return [], 0, probe
