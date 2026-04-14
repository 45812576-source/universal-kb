"""Dev Studio — opencode web 全局单例进程 + save-to-tool/skill.

路由薄层：HTTP handler 调用 workdir_manager / runtime_process_manager / opencode_backend 服务模块。
"""
import asyncio
import base64
import glob as _glob
import json
import os
import shutil
import tempfile
import time as _time
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.utils.sql_safe import qi
from app.models.skill import Skill, SkillStatus, SkillVersion
from app.models.tool import ToolRegistry, ToolType, SkillTool
from app.models.user import User, Role

# ─── 从服务模块导入 ──────────────────────────────────────────────────────────

from app.services.workdir_manager import (
    RUNTIME_IGNORE_DIRS,
    WORKSPACE_MAX_GB,
    _dir_size_bytes,
    _studio_root,
    _user_opencode_db_path,
    _workspace_alias_roots,
    _workspace_project_dir,
    _workspace_root_for_user,
    _workspace_runtime_config_dir,
    _workspace_runtime_data_dir,
    _workspace_skill_studio_dir,
    ensure_workspace_layout,
    resolve_workspace_path,
)

from app.services.runtime_process_manager import (
    MAX_FD_COUNT,
    MAX_RSS_MB,
    _get_proc_tree_rss_mb,
    _get_registry_project_dir,
    _mark_registry_stopped,
    _port_for_user,
    _port_open,
    _restart_history,
    _MAX_RESTARTS_PER_HOUR,
    _user_instances,
    shutdown_all_instances,
    list_all_instances,
    get_instance_info,
)

from app.services.opencode_backend import (
    _resolve_default_model,
    _runtime_fallback,
    _usage_counter,
    _write_opencode_config,
    _WINDOW_5H,
    _WINDOW_7D,
    _WINDOW_30D,
    _count_ai_calls,
    prepare_and_start,
    set_provider_fallback as _set_provider_fallback_impl,
    get_provider_status as _get_provider_status_impl,
)

router = APIRouter(prefix="/api/dev-studio", tags=["dev-studio"])

# 兼容旧测试/调用点；真实实现已迁移到 OpenCodeBackend.prepare_and_start。
_ensure_user_instance = prepare_and_start


# ─── 路由级辅助函数 ──────────────────────────────────────────────────────────

def _user_workdir(user: User) -> str:
    """返回当前用户的 project 目录路径（用户可见文件）。

    优先级：
    1. 注册表 project_dir — 持久化的单一真相源。
    2. 回退到 workspace_root_for_user + ensure_workspace_layout（首次使用、迁移场景）。
    """
    try:
        from app.database import SessionLocal as _WdSL
        from app.models.opencode import StudioRegistration as _SR
        _wdb = _WdSL()
        try:
            _reg = (
                _wdb.query(_SR)
                .filter(_SR.user_id == user.id, _SR.workspace_type == "opencode")
                .first()
            )
            if _reg and _reg.project_dir and os.path.isdir(_reg.project_dir):
                return _reg.project_dir
        finally:
            _wdb.close()
    except Exception:
        pass

    workdir = _workspace_root_for_user(user.id, user.display_name or "")
    project_dir, _ = ensure_workspace_layout(workdir, display_name=user.display_name or "")
    return project_dir


def _user_skill_studio_dir(user: User) -> str:
    """返回当前用户的 skill_studio 隔离目录。"""
    try:
        from app.database import SessionLocal as _SsSL
        from app.models.opencode import StudioRegistration as _SR
        _sdb = _SsSL()
        try:
            _reg = (
                _sdb.query(_SR)
                .filter(_SR.user_id == user.id, _SR.workspace_type == "opencode")
                .first()
            )
            if _reg and _reg.workspace_root:
                ss_dir = _workspace_skill_studio_dir(_reg.workspace_root)
                os.makedirs(ss_dir, exist_ok=True)
                return ss_dir
        finally:
            _sdb.close()
    except Exception:
        pass

    workdir = _workspace_root_for_user(user.id, user.display_name or "")
    ensure_workspace_layout(workdir, display_name=user.display_name or "")
    ss_dir = _workspace_skill_studio_dir(workdir)
    os.makedirs(ss_dir, exist_ok=True)
    return ss_dir


def _safe_path(workdir: str, rel: str) -> str:
    """将相对路径解析为绝对路径，确保不超出 workdir（防路径穿越）。"""
    abs_path = os.path.normpath(os.path.join(workdir, rel.lstrip("/")))
    if not (abs_path == workdir or abs_path.startswith(workdir + os.sep)):
        raise HTTPException(400, "路径不合法")
    return abs_path


_TREE_SKIP = RUNTIME_IGNORE_DIRS

def _tree(base: str, rel: str = "") -> list:
    """递归列出目录树，返回节点列表（跳过隐藏/构建/依赖目录）。"""
    abs_dir = os.path.join(base, rel) if rel else base
    nodes = []
    try:
        entries = sorted(os.scandir(abs_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return nodes
    for entry in entries:
        if entry.is_dir() and (entry.name in _TREE_SKIP or entry.name.startswith(".")):
            continue
        node_rel = os.path.join(rel, entry.name) if rel else entry.name
        if entry.is_dir():
            nodes.append({"name": entry.name, "path": node_rel, "type": "dir", "children": _tree(base, node_rel)})
        else:
            stat = entry.stat()
            nodes.append({"name": entry.name, "path": node_rel, "type": "file", "size": stat.st_size, "mtime": stat.st_mtime})
    return nodes


_REQUIRED_TOP_DIRS = ("inbox", "work", "export", "archive")
_MAX_UPLOAD_FILE_BYTES = 200 * 1024 * 1024  # 单文件上限 200MB


# ─── 运行时 fallback 开关 ──────────────────────────────────────────────────

@router.post("/provider-fallback")
async def set_provider_fallback(
    enable: bool,
    reset_counter: bool = False,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """手动触发/关闭百炼→ARK fallback，刷新 opencode.json 并重启 opencode 进程。"""
    result = _set_provider_fallback_impl(enable, reset_counter)

    from app.config import settings as _settings
    from app.database import SessionLocal as _SL
    from app.models.opencode import UserModelGrant as _UMG
    bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
    ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
    _lemondata_raw = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")

    # 全量刷新：扫描 STUDIO_WORKSPACE_ROOT 下所有用户目录，更新磁盘配置
    from app.config import settings as _cfg_fb
    _studio_root_path = os.path.abspath(os.path.expanduser(
        getattr(_cfg_fb, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
    ))
    _updated_wdirs = set()
    if os.path.isdir(_studio_root_path):
        for _entry in os.scandir(_studio_root_path):
            if not _entry.is_dir():
                continue
            wdir = _entry.path
            ensure_workspace_layout(wdir)
            _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=enable)
            _updated_wdirs.add(wdir)

    # 从注册表读所有 opencode 用户，按授权补写 lemondata key + 终止活跃进程
    from app.models.opencode import StudioRegistration as _FbSR
    _fb_db = _SL()
    try:
        _fb_regs = _fb_db.query(_FbSR).filter(
            _FbSR.workspace_type == "opencode", _FbSR.workspace_root != ""
        ).all()
        _fb_uid_wdir = {r.user_id: r.workspace_root for r in _fb_regs}
    finally:
        _fb_db.close()

    for uid, wdir in _fb_uid_wdir.items():
        _uid_lemondata_key = ""
        if _lemondata_raw:
            _db = _SL()
            try:
                _grant = (
                    _db.query(_UMG)
                    .filter(
                        _UMG.user_id == uid,
                        _UMG.model_key.like("%gpt%") | _UMG.model_key.like("lemondata/%"),
                    )
                    .first()
                )
                if _grant:
                    _uid_lemondata_key = _lemondata_raw
            finally:
                _db.close()
        if _uid_lemondata_key:
            _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=enable, lemondata_key=_uid_lemondata_key)
        inst = _user_instances.get(uid)
        if inst:
            proc = inst.get("proc")
            if proc is not None and proc.returncode is None:
                proc.terminate()
                inst["proc"] = None
                _mark_registry_stopped(uid)

    return {
        "fallback_enabled": enable,
        "default_model": result["default_model"],
        "message": "opencode.json 已更新，进程将在下次请求时重启",
    }


@router.get("/provider-status")
async def get_provider_status(
    user: User = Depends(get_current_user),
):
    """查询百炼三窗口实际调用次数及当前 provider 配置。"""
    return _get_provider_status_impl()


# ─── GET /instance — 启动/获取当前用户的独立实例 ──────────────────────────────

@router.get("/instance")
async def get_instance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    info = await prepare_and_start(user.id, display_name=user.display_name or "")

    from app.models.opencode import OpenCodeWorkspaceMapping
    mapping = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.user_id == user.id,
        OpenCodeWorkspaceMapping.directory != None,
    ).first()
    workspace_root = _workspace_root_for_user(user.id, user.display_name or "")
    if mapping is None:
        mapping = OpenCodeWorkspaceMapping(
            user_id=user.id,
            directory=workspace_root,
            opencode_workspace_name=user.display_name,
        )
        db.add(mapping)
        db.commit()
    elif mapping.directory != workspace_root:
        mapping.directory = workspace_root
        db.commit()

    return {"url": info["url"], "port": info["port"], "status": "ready"}


# ─── GET /entry — 统一入口 API（前端唯一入口）────────────────────────────────

@router.get("/entry")
def dev_studio_entry(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回稳定的工作区入口：注册表 + conversation + runtime 状态 + opencode.db 全量 session。"""
    from app.services.studio_registry import resolve_entry
    entry = resolve_entry(db, user, "opencode")
    return {
        "registration_id": entry.registration_id,
        "conversation_id": entry.conversation_id,
        "workspace_root": entry.workspace_root,
        "project_dir": entry.project_dir,
        "runtime_status": entry.runtime_status,
        "runtime_port": entry.runtime_port,
        "generation": entry.generation,
        "needs_recover": entry.needs_recover,
        "recent_conversation_ids": entry.recent_conversation_ids,
        "last_active_at": entry.last_active_at,
        "session_total": entry.session_total,
        "session_db_health": entry.session_db_health,
        "session_db_source": entry.session_db_source,
        "session_db_path": entry.session_db_path,
        "migration_state": entry.migration_state,
    }


@router.post("/entry")
async def dev_studio_entry_start(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """统一入口 — 返回注册表信息并自动启动/恢复 runtime。"""
    from app.services.studio_registry import resolve_entry
    entry = resolve_entry(db, user, "opencode")
    harness_request_id = None
    try:
        from app.harness.adapters import build_dev_studio_request
        _h_req = build_dev_studio_request(
            user_id=user.id,
            workspace_id=getattr(entry, "workspace_id", None) or 0,
            project_id=getattr(entry, "project_id", None),
            conversation_id=entry.conversation_id,
            user_message="dev_studio.entry.start",
            stream=False,
            metadata={"source": "dev_studio.entry"},
        )
        harness_request_id = _h_req.request_id
    except Exception:
        harness_request_id = None

    port = None
    url = None
    runtime_error = None
    try:
        info = await _ensure_user_instance(user.id, display_name=user.display_name or "")
        port = info["port"]
        url = info["url"]
        runtime_status = "running"
    except HTTPException as e:
        runtime_error = e.detail
        runtime_status = entry.runtime_status
    except Exception as e:
        runtime_error = str(e)
        runtime_status = entry.runtime_status

    return {
        "registration_id": entry.registration_id,
        "conversation_id": entry.conversation_id,
        "workspace_root": entry.workspace_root,
        "project_dir": entry.project_dir,
        "runtime_status": runtime_status,
        "runtime_port": port or entry.runtime_port,
        "generation": entry.generation,
        "needs_recover": False if runtime_status == "running" else entry.needs_recover,
        "recent_conversation_ids": entry.recent_conversation_ids,
        "last_active_at": entry.last_active_at,
        "session_total": entry.session_total,
        "session_db_health": entry.session_db_health,
        "session_db_source": entry.session_db_source,
        "session_db_path": entry.session_db_path,
        "migration_state": entry.migration_state,
        "port": port,
        "url": url,
        "runtime_error": runtime_error,
        "harness_request_id": harness_request_id,
    }


@router.get("/health")
def dev_studio_health(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户 opencode 运行时状态。"""
    from app.services.studio_registry import get_registration
    reg = get_registration(db, user.id, "opencode")
    if not reg:
        return {"runtime_status": "unregistered", "generation": 0}
    inst = _user_instances.get(user.id)
    pid = None
    process_alive = False
    if inst and inst.get("proc"):
        if inst["proc"].returncode is None:
            pid = inst["proc"].pid
            process_alive = True

    result = {
        "runtime_status": reg.runtime_status,
        "runtime_port": reg.runtime_port,
        "generation": reg.generation,
        "last_active_at": reg.last_active_at.isoformat() if reg.last_active_at else None,
        "process_alive": process_alive,
    }
    if user.role == Role.SUPER_ADMIN:
        result["debug"] = {
            "workspace_root": reg.workspace_root,
            "project_dir": reg.project_dir,
            "runtime_pid": pid,
            "port": reg.runtime_port,
            "last_recovered_at": reg.last_recovered_at.isoformat() if reg.last_recovered_at else None,
            "last_verified_at": reg.last_verified_at.isoformat() if reg.last_verified_at else None,
        }
    return result


# ─── POST /session-repair ─────────────────────────────────────────────────────

@router.post("/session-repair")
def dev_studio_session_repair(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """扫描所有可能的 opencode.db 位置，尝试修复/迁移丢失的 session 数据。"""
    from app.services.studio_registry import (
        get_registration, migrate_legacy_session_db, probe_session_db,
    )
    import sqlite3

    reg = get_registration(db, user.id, "opencode")
    if not reg or not reg.workspace_root:
        return {"ok": False, "error": "尚未注册工作区"}

    ws_root = reg.workspace_root
    migration_state = migrate_legacy_session_db(ws_root)
    probe = probe_session_db(ws_root)

    found_legacy_dbs = []
    seen_db_paths: set[str] = set()
    for candidate_root in _workspace_alias_roots(ws_root, user.display_name or ""):
        for sub in [
            os.path.join(candidate_root, "runtime", "data", "opencode", "opencode.db"),
            os.path.join(candidate_root, ".local", "share", "opencode", "opencode.db"),
        ]:
            normalized_sub = os.path.normpath(sub)
            if normalized_sub in seen_db_paths or not os.path.exists(normalized_sub):
                continue
            seen_db_paths.add(normalized_sub)
            try:
                con = sqlite3.connect(normalized_sub, timeout=3)
                count = con.execute("SELECT COUNT(*) FROM session").fetchone()[0]
                con.close()
                found_legacy_dbs.append({
                    "path": normalized_sub,
                    "dir": os.path.basename(candidate_root),
                    "session_count": count,
                })
            except Exception:
                pass

    repaired = False
    if probe.total == 0 and found_legacy_dbs:
        best = max(found_legacy_dbs, key=lambda x: x["session_count"])
        canonical = os.path.join(ws_root, "runtime", "data", "opencode", "opencode.db")
        if best["session_count"] > 0 and os.path.normpath(best["path"]) != os.path.normpath(canonical):
            try:
                os.makedirs(os.path.dirname(canonical), exist_ok=True)
                shutil.copy2(best["path"], canonical)
                for suffix in ("-wal", "-shm"):
                    src_s = best["path"] + suffix
                    dst_s = canonical + suffix
                    if os.path.exists(src_s):
                        shutil.copy2(src_s, dst_s)
                repaired = True
                probe = probe_session_db(ws_root)
            except Exception:
                pass

    return {
        "ok": True,
        "migration_state": migration_state,
        "db_health": probe.db_health,
        "db_path": probe.db_path,
        "session_total": probe.total,
        "found_legacy_dbs": found_legacy_dbs,
        "repaired": repaired,
    }


# ─── GET /runtime-health ─────────────────────────────────────────────────────

@router.get("/runtime-health")
async def dev_studio_runtime_health(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """探测 OpenCode upstream 可达性、静态资源、RPC、WebSocket、session db 状态。"""
    import httpx
    from app.services.studio_registry import get_registration, probe_session_db

    reg = get_registration(db, user.id, "opencode")
    if not reg or not reg.runtime_port:
        probe = probe_session_db(reg.workspace_root) if reg else None
        return {
            "runtime_reachable": False,
            "static_ok": False,
            "rpc_ok": False,
            "ws_ok": False,
            "session_db_health": probe.db_health if probe else "missing",
            "runtime_status": reg.runtime_status if reg else "unregistered",
        }

    base = f"http://127.0.0.1:{reg.runtime_port}"
    results = {
        "runtime_reachable": False,
        "static_ok": False,
        "rpc_ok": False,
        "ws_ok": False,
        "session_db_health": "unknown",
        "runtime_status": reg.runtime_status,
    }

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{base}/")
            results["runtime_reachable"] = True
            results["static_ok"] = r.status_code == 200 and "text/html" in (r.headers.get("content-type") or "")
        except Exception:
            pass
        try:
            r = await client.get(f"{base}/session/list")
            results["rpc_ok"] = r.status_code < 500
        except Exception:
            pass

    try:
        import websockets
        async with websockets.connect(f"ws://127.0.0.1:{reg.runtime_port}/ws", open_timeout=3):
            results["ws_ok"] = True
    except Exception:
        results["ws_ok"] = None

    probe = probe_session_db(reg.workspace_root)
    results["session_db_health"] = probe.db_health
    return results


# ─── GET /session-audit ──────────────────────────────────────────────────────

@router.get("/session-audit")
def dev_studio_session_audit(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """直接读取用户 opencode.db，返回真实 session 列表。"""
    import sqlite3

    target_user_id = user.id
    from app.services.studio_registry import get_registration
    reg = get_registration(db, target_user_id, "opencode")
    if not reg:
        return {"error": "该用户没有 OpenCode 注册记录", "sessions": []}

    from app.services.studio_registry import probe_session_db
    probe = probe_session_db(reg.workspace_root)
    db_path = probe.db_path
    if not db_path or not os.path.exists(db_path):
        return {
            "workspace_root": reg.workspace_root,
            "db_exists": False,
            "db_health": probe.db_health,
            "migration_state": probe.migration_state,
            "sessions": [],
        }

    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        sessions = []
        total_messages = 0
        if "session" in tables:
            rows = con.execute(
                "SELECT id, title, directory, project_id, time_created, time_updated "
                "FROM session ORDER BY time_updated DESC"
            ).fetchall()
            for row in rows:
                msg_count = 0
                if "message" in tables:
                    msg_row = con.execute(
                        "SELECT COUNT(*) FROM message WHERE session_id=?", (row["id"],)
                    ).fetchone()
                    msg_count = msg_row[0] if msg_row else 0
                total_messages += msg_count
                normalized_directory = resolve_workspace_path(
                    reg.workspace_root,
                    row["directory"],
                    prefer_existing=True,
                    default_to_project=True,
                    allow_external=False,
                ) if row["directory"] else None
                sessions.append({
                    "id": row["id"],
                    "title": row["title"],
                    "directory": normalized_directory,
                    "project_id": row["project_id"],
                    "created_at": row["time_created"],
                    "updated_at": row["time_updated"],
                    "message_count": msg_count,
                })

        db_size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 2)
        wal_path = db_path + "-wal"
        wal_size_mb = round(os.path.getsize(wal_path) / 1024 / 1024, 2) if os.path.exists(wal_path) else 0
        new_path = os.path.join(reg.workspace_root, "runtime", "data", "opencode", "opencode.db")
        db_source = "runtime_data" if db_path == new_path else "legacy_local_share"

        con.close()
        return {
            "workspace_root": reg.workspace_root,
            "project_dir": reg.project_dir,
            "opencode_db_path": db_path,
            "db_source": db_source,
            "db_exists": True,
            "db_size_mb": db_size_mb,
            "wal_size_mb": wal_size_mb,
            "session_count": len(sessions),
            "total_messages": total_messages,
            "sessions": sessions,
        }
    except Exception as e:
        return {
            "workspace_root": reg.workspace_root,
            "opencode_db_path": db_path,
            "db_exists": True,
            "error": str(e),
            "sessions": [],
        }


@router.get("/session-audit/{target_user_id}")
def dev_studio_session_audit_admin(
    target_user_id: int,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: Session = Depends(get_db),
):
    """管理员版：查看指定用户的 OpenCode session 诊断。"""
    import sqlite3

    from app.services.studio_registry import get_registration
    reg = get_registration(db, target_user_id, "opencode")
    if not reg:
        return {"error": "该用户没有 OpenCode 注册记录", "sessions": []}

    db_path = _user_opencode_db_path(reg.workspace_root)
    if not db_path or not os.path.exists(db_path):
        return {
            "user_id": target_user_id,
            "workspace_root": reg.workspace_root,
            "db_exists": False,
            "sessions": [],
        }

    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        sessions = []
        total_messages = 0
        if "session" in tables:
            rows = con.execute(
                "SELECT id, title, directory, project_id, time_created, time_updated "
                "FROM session ORDER BY time_updated DESC"
            ).fetchall()
            for row in rows:
                msg_count = 0
                if "message" in tables:
                    msg_row = con.execute(
                        "SELECT COUNT(*) FROM message WHERE session_id=?", (row["id"],)
                    ).fetchone()
                    msg_count = msg_row[0] if msg_row else 0
                total_messages += msg_count
                normalized_directory = resolve_workspace_path(
                    reg.workspace_root,
                    row["directory"],
                    prefer_existing=True,
                    default_to_project=True,
                    allow_external=False,
                ) if row["directory"] else None
                sessions.append({
                    "id": row["id"],
                    "title": row["title"],
                    "directory": normalized_directory,
                    "project_id": row["project_id"],
                    "created_at": row["time_created"],
                    "updated_at": row["time_updated"],
                    "message_count": msg_count,
                })

        db_size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 2)
        wal_path = db_path + "-wal"
        wal_size_mb = round(os.path.getsize(wal_path) / 1024 / 1024, 2) if os.path.exists(wal_path) else 0
        con.close()

        return {
            "user_id": target_user_id,
            "workspace_root": reg.workspace_root,
            "project_dir": reg.project_dir,
            "db_exists": True,
            "db_size_mb": db_size_mb,
            "wal_size_mb": wal_size_mb,
            "session_count": len(sessions),
            "total_messages": total_messages,
            "sessions": sessions,
        }
    except Exception as e:
        return {
            "user_id": target_user_id,
            "workspace_root": reg.workspace_root,
            "db_exists": True,
            "error": str(e),
            "sessions": [],
        }


# ─── GET /diagnostics ────────────────────────────────────────────────────────

@router.get("/diagnostics")
def dev_studio_diagnostics(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户的运行时状态全面诊断。"""
    from app.services.studio_registry import get_registration, probe_session_db

    reg = get_registration(db, user.id, "opencode")
    if not reg:
        return {"error": "未注册"}

    probe = probe_session_db(reg.workspace_root)

    inst = _user_instances.get(user.id)
    runtime_pid = None
    rss_mb = 0
    fd_count = 0
    started_at = None
    if inst and inst.get("proc") and inst["proc"].returncode is None:
        runtime_pid = inst["proc"].pid
        rss_mb = _get_proc_tree_rss_mb(runtime_pid)
        try:
            fd_count = len(os.listdir(f"/proc/{runtime_pid}/fd"))
        except Exception:
            fd_count = -1
        started_at = inst.get("started_at")

    last_reap_reason = (inst or {}).get("last_restart_reason", None)

    output_dir = os.path.join(reg.project_dir, "output")
    output_file_count = 0
    if os.path.isdir(output_dir):
        output_file_count = sum(1 for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f)))

    return {
        "workspace_root": reg.workspace_root,
        "db_health": probe.db_health,
        "db_path": probe.db_path,
        "migration_state": probe.migration_state,
        "session_total": probe.total,
        "output_health": "ok" if os.path.isdir(output_dir) else "missing",
        "output_file_count": output_file_count,
        "runtime_status": reg.runtime_status,
        "runtime_metrics": {
            "pid": runtime_pid,
            "rss_mb": rss_mb,
            "fd_count": fd_count,
            "started_at": started_at,
            "max_rss_mb": MAX_RSS_MB,
            "max_fd_count": MAX_FD_COUNT,
        },
        "last_reap_reason": last_reap_reason,
    }


# ─── GET /sessions ────────────────────────────────────────────────────────────

@router.get("/sessions")
def dev_studio_sessions(
    page: int = 1,
    page_size: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户的历史 OpenCode 会话列表（分页）。"""
    from app.services.studio_registry import get_registration, read_opencode_sessions
    from dataclasses import asdict

    reg = get_registration(db, user.id, "opencode")
    if not reg:
        return {
            "items": [], "total": 0, "page": page, "page_size": page_size,
            "db_health": "missing", "db_source": "missing",
            "db_path": None, "migration_state": "none",
        }

    sessions, total, probe = read_opencode_sessions(
        reg.workspace_root, page=page, page_size=page_size,
    )
    items = []
    for session in sessions:
        item = asdict(session)
        if item.get("directory"):
            item["directory"] = resolve_workspace_path(
                reg.workspace_root,
                item["directory"],
                prefer_existing=True,
                default_to_project=True,
                allow_external=False,
            )
        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "db_path": probe.db_path,
        "db_health": probe.db_health,
        "db_source": probe.db_source,
        "migration_state": probe.migration_state,
    }


# ─── POST /sessions/{id}/resume ──────────────────────────────────────────────

@router.post("/sessions/{session_id}/resume")
async def dev_studio_session_resume(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """恢复到指定 OpenCode session。"""
    from app.services.studio_registry import get_registration, resolve_entry, probe_session_db
    import httpx
    import sqlite3
    import subprocess as _sp

    entry = resolve_entry(db, user, "opencode")
    reg = get_registration(db, user.id, "opencode")
    workspace_root = reg.workspace_root if reg else entry.workspace_root
    expected_port = _port_for_user(user.id)

    probe = probe_session_db(workspace_root)
    session_belongs_to_user = False
    session_directory = reg.project_dir if reg else entry.project_dir
    if probe.db_path:
        try:
            con = sqlite3.connect(probe.db_path, timeout=5)
            try:
                row = con.execute("SELECT id, directory FROM session WHERE id = ? LIMIT 1", (session_id,)).fetchone()
                session_belongs_to_user = row is not None
                if row and len(row) > 1 and row[1]:
                    session_directory = resolve_workspace_path(
                        workspace_root,
                        row[1],
                        prefer_existing=True,
                        default_to_project=True,
                        allow_external=False,
                    )
            finally:
                con.close()
        except Exception:
            session_belongs_to_user = False

    if not session_belongs_to_user:
        return {
            "ok": False,
            "resumed_session_id": None,
            "route_path": None,
            "port": None,
            "runtime_status": reg.runtime_status if reg else "stopped",
            "error_code": "session_not_found",
            "error_message": "这个历史会话不属于当前用户工作区，已阻止串线恢复",
        }

    def _encode_directory_route(directory: str) -> str:
        encoded = base64.urlsafe_b64encode(directory.encode("utf-8")).decode("ascii")
        return encoded.rstrip("=")

    route_path = f"/{_encode_directory_route(session_directory)}/session/{session_id}"

    async def _session_available(port: int) -> bool:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/session/{session_id}")
            return resp.status_code < 300

    async def _restart_runtime() -> None:
        inst = _user_instances.get(user.id)
        if inst:
            proc = inst.get("proc")
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            inst["proc"] = None
        try:
            _lsof = _sp.run(["lsof", "-ti", f":{expected_port}"], capture_output=True, text=True, timeout=5)
            for _pid_str in _lsof.stdout.strip().splitlines():
                if _pid_str.strip().isdigit():
                    try:
                        os.kill(int(_pid_str.strip()), 9)
                    except Exception:
                        pass
        except Exception:
            pass
        _mark_registry_stopped(user.id)
        await asyncio.sleep(1)

    try:
        result = await prepare_and_start(user.id, user.display_name or "")
        port = result["port"]
    except HTTPException as e:
        return {
            "ok": False,
            "resumed_session_id": None,
            "route_path": None,
            "port": None,
            "runtime_status": "stopped",
            "error_code": "runtime_start_failed",
            "error_message": f"OpenCode 启动失败: {e.detail}",
        }

    session_ready = False
    runtime_error = None
    try:
        session_ready = await _session_available(port)
    except Exception as e:
        runtime_error = str(e)

    if not session_ready:
        await _restart_runtime()
        try:
            retry = await prepare_and_start(user.id, user.display_name or "")
            port = retry["port"]
            session_ready = await _session_available(port)
        except HTTPException as e:
            runtime_error = e.detail
        except Exception as e:
            runtime_error = str(e)

    if session_ready:
        return {
            "ok": True,
            "resumed_session_id": session_id,
            "route_path": route_path,
            "directory": session_directory,
            "port": port,
            "runtime_status": "running",
            "error_code": None,
            "error_message": None,
        }

    return {
        "ok": False,
        "resumed_session_id": None,
        "route_path": None,
        "port": port,
        "runtime_status": "running",
        "error_code": "session_not_found",
        "error_message": runtime_error or "历史 Session 在当前 runtime 中不可用，已自动重启仍未恢复",
    }


# ─── POST /restart ─────────────────────────────────────────────────────────

@router.post("/restart")
async def restart_instance(user: User = Depends(get_current_user)):
    """强制杀掉当前用户的 opencode 进程，等端口释放后重新启动。"""
    import signal as _sig

    inst = _user_instances.get(user.id)
    port = _port_for_user(user.id)

    if inst:
        proc = inst.get("proc")
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            inst["proc"] = None
            _mark_registry_stopped(user.id)

    try:
        import subprocess as _sp
        _lsof = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5)
        for _pid_str in _lsof.stdout.strip().splitlines():
            if _pid_str.strip().isdigit():
                try:
                    os.kill(int(_pid_str.strip()), _sig.SIGKILL)
                except Exception:
                    pass
    except Exception:
        pass

    for _ in range(10):
        if not _port_open(port):
            break
        await asyncio.sleep(0.5)

    info = await prepare_and_start(user.id, display_name=user.display_name or "")
    return {"status": "restarted", "port": info["port"]}


# ─── GET /user-port ────────────────────────────────────────────────────────

@router.get("/user-port")
async def get_user_port(user: User = Depends(get_current_user)):
    """返回当前用户对应的 opencode 端口。"""
    return {"port": _port_for_user(user.id), "user_id": user.id}


# ─── POST /sessions（兼容旧接口）─────────────────────────────────────────

@router.post("/sessions")
async def create_session(user: User = Depends(get_current_user)):
    info = await prepare_and_start(user.id, display_name=user.display_name or "")
    return {
        "session_id": f"user_{user.id}",
        "url": info["url"],
        "port": info["port"],
    }


# ─── GET /latest-output ─────────────────────────────────────────────────────

@router.get("/latest-output")
def get_latest_output(
    limit: int = 10,
    user: User = Depends(get_current_user),
):
    """读取 opencode 最近 session 写入的文件列表及内容。"""
    import sqlite3 as _sqlite3
    import json as _json

    workdir = _workspace_root_for_user(user.id, user.display_name or "")
    db_path = _user_opencode_db_path(workdir)
    if not os.path.exists(db_path):
        return []

    try:
        con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = _sqlite3.Row
        rows = con.execute("""
            SELECT p.data, s.title, s.time_updated
            FROM part p
            JOIN session s ON s.id = p.session_id
            WHERE json_extract(p.data, '$.type') = 'tool'
              AND json_extract(p.data, '$.tool') IN ('write', 'edit', 'patch')
              AND json_extract(p.data, '$.state.status') = 'completed'
            ORDER BY p.time_updated DESC
            LIMIT ?
        """, (limit * 3,)).fetchall()
        con.close()
    except Exception:
        return []

    seen_paths: set = set()
    result = []

    for row in rows:
        if len(result) >= limit:
            break
        try:
            d = _json.loads(row["data"])
        except Exception:
            continue

        state = d.get("state") or {}
        inp = state.get("input") or {}
        tool = d.get("tool", "")
        raw_file_path = inp.get("filePath") or inp.get("file_path") or ""
        if not raw_file_path:
            continue

        project_dir = _workspace_project_dir(workdir)
        resolved_file_path = resolve_workspace_path(
            workdir,
            raw_file_path,
            prefer_existing=True,
            default_to_project=False,
            allow_external=False,
        )
        display_file_path = resolve_workspace_path(
            workdir,
            raw_file_path,
            prefer_existing=False,
            default_to_project=False,
            allow_external=False,
        )
        file_path = resolved_file_path if os.path.isfile(resolved_file_path) else display_file_path
        if not file_path or file_path in seen_paths:
            continue
        seen_paths.add(file_path)

        exists_on_disk = os.path.isfile(resolved_file_path)

        content = ""
        if tool == "write":
            content = inp.get("content") or ""
        elif exists_on_disk:
            norm_path = os.path.normpath(resolved_file_path)
            if norm_path == project_dir or norm_path.startswith(project_dir + os.sep):
                try:
                    with open(resolved_file_path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    content = ""

        result.append({
            "path": file_path,
            "filename": os.path.basename(file_path),
            "content": content,
            "tool": tool,
            "session_title": row["title"] or "",
            "exists_on_disk": exists_on_disk,
            "category": "recent_output",
        })

    # 额外扫描 runtime/config/opencode/skills/ 中最近修改的 .md 文件
    runtime_skills_dir = os.path.join(
        _workspace_runtime_config_dir(workdir), "opencode", "skills"
    )
    if os.path.isdir(runtime_skills_dir):
        import glob as _glob_mod
        md_files = _glob_mod.glob(os.path.join(runtime_skills_dir, "*.md"))
        md_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for md_path in md_files[:limit]:
            if len(result) >= limit:
                break
            if md_path in seen_paths:
                continue
            seen_paths.add(md_path)
            try:
                with open(md_path, encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)
                result.append({
                    "path": md_path,
                    "filename": os.path.basename(md_path),
                    "content": content,
                    "tool": "skill_file",
                    "session_title": "",
                    "exists_on_disk": True,
                    "category": "runtime_skill",
                })
            except Exception:
                pass

    return result


# ─── GET /output-files ─────────────────────────────────────────────────────

@router.get("/output-files")
def dev_studio_output_files(
    user: User = Depends(get_current_user),
):
    """枚举用户产出文件。"""
    import datetime as _dt

    workdir = _user_workdir(user)
    project_dir = workdir

    _skip_dirs = {".git", ".opencode", "node_modules", "__pycache__", ".venv", "venv",
                  ".next", "dist", "build", ".cache", ".bin", ".local", ".config"}

    def _categorize(fname: str) -> str:
        ext = os.path.splitext(fname)[1].lower()
        return (
            "skill" if ext == ".md" else
            "code" if ext in (".py", ".ts", ".js", ".json", ".bat", ".command", ".sh") else
            "data" if ext in (".csv", ".xlsx", ".xls", ".sql") else
            "doc" if ext in (".html", ".pdf", ".docx", ".txt") else
            "other"
        )

    items = []
    seen_paths: set = set()

    if os.path.isdir(project_dir):
        for dirpath, dirnames, filenames in os.walk(project_dir):
            dirnames[:] = [d for d in dirnames if d not in _skip_dirs]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(fpath)
                except OSError:
                    continue
                seen_paths.add(fpath)
                items.append({
                    "path": fpath,
                    "rel_path": os.path.relpath(fpath, project_dir),
                    "name": fname,
                    "size": stat.st_size,
                    "updated_at": _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "category": _categorize(fname),
                    "source": "project",
                })

    ws_root = os.path.dirname(project_dir)
    ss_data_dir = os.path.join(ws_root, "skill_studio", "data")
    if os.path.isdir(ss_data_dir):
        for fname in os.listdir(ss_data_dir):
            if fname.startswith("."):
                continue
            fpath = os.path.join(ss_data_dir, fname)
            if not os.path.isfile(fpath) or fpath in seen_paths:
                continue
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            seen_paths.add(fpath)
            items.append({
                "path": fpath,
                "rel_path": f"skill_studio/data/{fname}",
                "name": fname,
                "size": stat.st_size,
                "updated_at": _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "category": _categorize(fname),
                "source": "skill_studio",
            })

    runtime_skills_dir = os.path.join(ws_root, "runtime", "config", "opencode", "skills")
    if os.path.isdir(runtime_skills_dir):
        for fname in os.listdir(runtime_skills_dir):
            if fname.startswith(".") or not fname.lower().endswith(".md"):
                continue
            fpath = os.path.join(runtime_skills_dir, fname)
            if not os.path.isfile(fpath) or fpath in seen_paths:
                continue
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            seen_paths.add(fpath)
            items.append({
                "path": fpath,
                "rel_path": f".opencode/skills/{fname}",
                "name": fname,
                "size": stat.st_size,
                "updated_at": _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "category": "runtime_skill",
                "source": "runtime_skill",
            })

    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"items": items}


# ─── Tool Task (from Skill Studio) ────────────────────────────────────────────

class ToolTaskRequest(BaseModel):
    skill_id: int
    skill_name: str
    tool_description: str
    expected_schema: dict = {}


@router.post("/tool-task")
def create_tool_task(
    req: ToolTaskRequest,
    user: User = Depends(get_current_user),
):
    """从 Skill Studio 发起工具开发任务。"""
    ss_dir = _user_skill_studio_dir(user)

    schema_text = ""
    if req.expected_schema:
        import json as _json
        schema_text = f"\n```json\n{_json.dumps(req.expected_schema, ensure_ascii=False, indent=2)}\n```"

    content = f"""# 工具开发需求

来源 Skill: {req.skill_name} (ID: {req.skill_id})

## 需求描述

{req.tool_description}

## 期望接口
{schema_text if schema_text else "（待定义）"}

## 完成后

保存为 Tool，回到 Skill Studio 绑定到源 Skill。
"""
    inbox_dir = os.path.join(ss_dir, "inbox")
    os.makedirs(inbox_dir, exist_ok=True)
    dest = os.path.join(inbox_dir, "TOOL_REQUEST.md")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)

    return {"ok": True, "skill_id": req.skill_id, "file": "TOOL_REQUEST.md"}


# ─── Save as Tool ─────────────────────────────────────────────────────────────

class SaveToolRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    tool_type: str = "http"
    input_schema: dict = {}
    output_format: str = "text"
    config: dict = {}
    bind_skill_id: int


@router.post("/save-tool")
def save_tool(
    req: SaveToolRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.query(Skill).filter(Skill.id == req.bind_skill_id).first()
    if not skill:
        raise HTTPException(404, f"绑定目标 Skill (id={req.bind_skill_id}) 不存在")

    existing = db.query(ToolRegistry).filter(ToolRegistry.name == req.name).first()
    if existing:
        raise HTTPException(409, f"工具名称 '{req.name}' 已存在")

    try:
        tool_type_enum = ToolType(req.tool_type)
    except ValueError:
        tool_type_enum = ToolType.HTTP

    tool = ToolRegistry(
        name=req.name,
        display_name=req.display_name,
        description=req.description or None,
        tool_type=tool_type_enum,
        input_schema=req.input_schema,
        output_format=req.output_format,
        config=req.config,
        created_by=user.id,
        is_active=False,
        scope="personal",
        status="draft",
    )
    db.add(tool)
    db.flush()

    binding = SkillTool(skill_id=req.bind_skill_id, tool_id=tool.id)
    db.add(binding)

    db.commit()
    db.refresh(tool)
    return {"id": tool.id, "name": tool.name, "display_name": tool.display_name, "bound_skill_id": req.bind_skill_id}


# ─── Save as Skill ────────────────────────────────────────────────────────────

class SaveSkillRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    source_files: list[str] = []
    change_note: str = "由工具开发工作台生成"


@router.post("/save-skill")
def save_skill(
    req: SaveSkillRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """整体成果保存为 Skill。"""
    workdir = _user_workdir(user)
    project_dir = _workspace_project_dir(workdir)
    output_dir = os.path.join(project_dir, "output")

    file_entries: list[dict] = []
    for rel_path in (req.source_files or []):
        stripped = os.path.basename(rel_path)
        output_path = os.path.join(output_dir, stripped)
        if os.path.isfile(output_path):
            abs_path = output_path
        else:
            abs_path = _safe_path(project_dir, rel_path)
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)
                file_entries.append({"path": rel_path, "content": content})
            except OSError:
                file_entries.append({"path": rel_path, "content": ""})

    system_prompt = req.system_prompt.strip()
    if not system_prompt:
        for entry in file_entries:
            if entry["path"].endswith(".md") and entry["content"].strip():
                system_prompt = entry["content"]
                break
    if not system_prompt:
        raise HTTPException(400, "system_prompt 不能为空，且未找到可用的 .md 文件")

    skill = Skill(
        name=req.name,
        description=req.description,
        scope="personal",
        mode="hybrid",
        created_by=user.id,
        status=SkillStatus.DRAFT,
        auto_inject=True,
        source_type="local",
        source_files=file_entries if file_entries else None,
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=system_prompt,
        change_note=req.change_note,
        created_by=user.id,
    )
    db.add(version)

    db.commit()
    db.refresh(skill)
    return {
        "id": skill.id,
        "name": skill.name,
        "status": skill.status.value,
        "approval_id": None,
    }


# ─── Sync Workspace Skills → DB ──────────────────────────────────────────────

@router.post("/sync-skills-from-workspace")
def sync_skills_from_workspace(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """扫描用户 workspace 中 opencode 生成的 .md skill 文件，自动注册未入库的为 Draft Skill。"""
    from app.models.skill import Skill, SkillStatus, SkillVersion

    workdir = _user_workdir(user)
    ws_root = os.path.dirname(workdir) if workdir.endswith("/project") else workdir

    runtime_skills_dir = os.path.join(ws_root, "runtime", "config", "opencode", "skills")
    output_dir = os.path.join(workdir, "output")

    _SUPERPOWER_NAMES = {
        "dispatching-parallel-agents", "executing-plans", "finishing-a-development-branch",
        "receiving-code-review", "requesting-code-review", "subagent-driven-development",
        "test-driven-development", "using-git-worktrees", "using-superpowers",
        "verification-before-completion", "writing-plans", "writing-skills",
    }

    candidates: dict[str, str] = {}
    for scan_dir in [runtime_skills_dir, output_dir]:
        if not os.path.isdir(scan_dir):
            continue
        for fname in os.listdir(scan_dir):
            if not fname.endswith(".md") or fname.startswith("."):
                continue
            stem = fname[:-3]
            if stem in _SUPERPOWER_NAMES:
                continue
            if fname not in candidates:
                fpath = os.path.join(scan_dir, fname)
                if os.path.isfile(fpath):
                    candidates[fname] = fpath

    if not candidates:
        return {"synced": 0, "skipped": 0}

    existing_names = {
        s.name for s in db.query(Skill.name).filter(Skill.created_by == user.id).all()
    }
    user_skills = db.query(Skill).filter(Skill.created_by == user.id).all()
    existing_filenames = set()
    for s in user_skills:
        for sf in (s.source_files or []):
            if isinstance(sf, dict):
                existing_filenames.add(sf.get("filename", ""))
                existing_filenames.add(os.path.basename(sf.get("path", "")))

    synced = 0
    skipped = 0
    for fname, fpath in candidates.items():
        skill_name = fname[:-3].replace("-", " ").replace("_", " ").strip()
        if fname in existing_filenames or skill_name in existing_names:
            skipped += 1
            continue

        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(500_000)
        except Exception:
            continue
        if not content.strip():
            continue

        skill = Skill(
            name=skill_name,
            description=f"从工作区文件 {fname} 自动导入",
            scope="personal",
            mode="hybrid",
            created_by=user.id,
            status=SkillStatus.DRAFT,
            auto_inject=False,
            source_type="local",
            source_files=[{"filename": fname, "path": fpath}],
        )
        db.add(skill)
        db.flush()

        version = SkillVersion(
            skill_id=skill.id,
            version=1,
            system_prompt=content,
            change_note="从工作区自动同步",
            created_by=user.id,
        )
        db.add(version)
        synced += 1

    if synced > 0:
        db.commit()

    return {"synced": synced, "skipped": skipped}


# ─── Save to Existing Skill ──────────────────────────────────────────────────

class SaveToSkillRequest(BaseModel):
    skill_id: int
    action: str  # "new_version" | "bind_tool"
    system_prompt: str = ""
    change_note: str = "由工作台追加"
    tool_name: str = ""
    tool_display_name: str = ""
    tool_description: str = ""
    tool_config: dict = {}


@router.post("/save-to-skill")
def save_to_skill(
    req: SaveToSkillRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将工作台产出追加到已有 Skill。"""
    skill = db.query(Skill).filter(Skill.id == req.skill_id).first()
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    if req.action == "new_version":
        if not req.system_prompt.strip():
            raise HTTPException(400, "system_prompt 不能为空")
        max_ver = (
            db.query(func.max(SkillVersion.version))
            .filter(SkillVersion.skill_id == skill.id)
            .scalar()
        ) or 0
        version = SkillVersion(
            skill_id=skill.id,
            version=max_ver + 1,
            system_prompt=req.system_prompt,
            change_note=req.change_note,
            created_by=user.id,
        )
        db.add(version)
        db.commit()
        return {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "version": max_ver + 1,
            "action": "new_version",
        }

    elif req.action == "bind_tool":
        if not req.tool_name.strip():
            raise HTTPException(400, "tool_name 不能为空")
        existing = db.query(ToolRegistry).filter(ToolRegistry.name == req.tool_name).first()
        if existing:
            raise HTTPException(409, f"工具名称 '{req.tool_name}' 已存在")
        tool = ToolRegistry(
            name=req.tool_name,
            display_name=req.tool_display_name or req.tool_name,
            description=req.tool_description or None,
            tool_type=ToolType.HTTP,
            input_schema={},
            output_format="text",
            config=req.tool_config,
            created_by=user.id,
            is_active=False,
            scope="personal",
            status="draft",
        )
        db.add(tool)
        db.flush()
        binding = SkillTool(skill_id=skill.id, tool_id=tool.id)
        db.add(binding)
        db.commit()
        return {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "tool_id": tool.id,
            "tool_name": tool.name,
            "action": "bind_tool",
        }
    else:
        raise HTTPException(400, f"不支持的 action: {req.action}")


# ─── Transfer Table to Workdir ────────────────────────────────────────────────

class TransferTableRequest(BaseModel):
    table_name: str
    format: str = "csv"
    filename: Optional[str] = None


@router.post("/transfer-table")
async def transfer_table(
    req: TransferTableRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将业务表全量数据导出并写入当前用户的 opencode workdir 根目录。"""
    import csv
    import io
    import json as _json
    import re
    from sqlalchemy import text as _text
    from app.models.business import BusinessTable
    from app.config import settings as _cfg

    fmt = req.format.lower()
    if fmt not in ("csv", "json", "sql"):
        raise HTTPException(400, "format 只支持 csv / json / sql")

    bt = db.query(BusinessTable).filter(BusinessTable.table_name == req.table_name).first()
    if not bt:
        raise HTTPException(404, f"业务表 '{req.table_name}' 未注册")

    rows_result = db.execute(_text(f"SELECT * FROM {qi(req.table_name, '表名')}"))
    columns = list(rows_result.keys())
    raw_rows = [dict(zip(columns, row)) for row in rows_result.fetchall()]

    import datetime, decimal

    def _ser(v):
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.isoformat()
        if isinstance(v, decimal.Decimal):
            return float(v)
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v

    rows = [{k: _ser(v) for k, v in r.items()} for r in raw_rows]

    table_name = req.table_name
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        content = buf.getvalue()
        ext = "csv"
    elif fmt == "json":
        content = _json.dumps(rows, ensure_ascii=False, indent=2)
        ext = "json"
    else:
        lines = [f"-- {bt.display_name or table_name} seed data\n"]
        for row in rows:
            col_str = ", ".join(f"`{c}`" for c in columns)
            val_parts = []
            for c in columns:
                v = row[c]
                if v is None:
                    val_parts.append("NULL")
                elif isinstance(v, (int, float)):
                    val_parts.append(str(v))
                else:
                    val_parts.append("'" + str(v).replace("'", "''") + "'")
            val_str = ", ".join(val_parts)
            lines.append(f"INSERT INTO {qi(table_name, '表名')} ({col_str}) VALUES ({val_str});")
        content = "\n".join(lines)
        ext = "sql"

    ss_dir = _user_skill_studio_dir(user)
    data_dir = os.path.join(ss_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    if req.filename:
        safe_filename = os.path.basename(req.filename)
    else:
        safe_filename = f"{table_name}.{ext}"

    dest = os.path.join(data_dir, safe_filename)
    with open(dest, "w", encoding="utf-8", newline="" if fmt == "csv" else "\n") as f:
        f.write(content)

    project_dir = _user_workdir(user)
    project_data_dir = os.path.join(project_dir, "data")
    indexed = False
    try:
        os.makedirs(project_data_dir, exist_ok=True)
        project_dest = os.path.join(project_data_dir, safe_filename)
        shutil.copy2(dest, project_dest)
        indexed = True
    except Exception:
        pass

    return {
        "ok": True,
        "filename": safe_filename,
        "rows": len(rows),
        "format": fmt,
        "workdir": ss_dir,
        "indexed": indexed,
    }


# ─── Upload File to Workdir ───────────────────────────────────────────────────

@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    target_path: str = Form(default=""),
    user: User = Depends(get_current_user),
):
    """将用户上传的文件写入其 opencode workdir。"""
    workdir = _user_workdir(user)
    max_bytes = WORKSPACE_MAX_GB * 1024 ** 3

    declared_size = file.size
    if declared_size is not None and declared_size > _MAX_UPLOAD_FILE_BYTES:
        raise HTTPException(400, f"单文件不能超过 {_MAX_UPLOAD_FILE_BYTES // 1024 // 1024}MB")

    ws_bytes = _dir_size_bytes(workdir)
    if declared_size is not None and ws_bytes + declared_size > max_bytes:
        raise HTTPException(
            400,
            f"Workspace 已使用 {ws_bytes / 1024**3:.2f}GB，"
            f"上传此文件将超过 {WORKSPACE_MAX_GB}GB 上限"
        )

    content = await file.read()

    if len(content) > _MAX_UPLOAD_FILE_BYTES:
        raise HTTPException(400, f"单文件不能超过 {_MAX_UPLOAD_FILE_BYTES // 1024 // 1024}MB")
    if ws_bytes + len(content) > max_bytes:
        raise HTTPException(
            400,
            f"Workspace 已使用 {ws_bytes / 1024**3:.2f}GB，"
            f"上传此文件将超过 {WORKSPACE_MAX_GB}GB 上限"
        )

    safe_filename = os.path.basename(file.filename or "upload")
    if not safe_filename:
        safe_filename = "upload"

    if target_path and target_path.strip():
        dest_dir = _safe_path(workdir, target_path.strip())
        os.makedirs(dest_dir, exist_ok=True)
    else:
        dest_dir = os.path.join(workdir, "work")
        os.makedirs(dest_dir, exist_ok=True)

    dest = os.path.join(dest_dir, safe_filename)
    with open(dest, "wb") as f:
        f.write(content)

    rel_dest = os.path.relpath(dest, workdir)
    return {
        "ok": True,
        "filename": safe_filename,
        "path": rel_dest,
        "size": len(content),
    }


# ─── POST /analyze-project ──────────────────────────────────────────────────

def _read_and_patch_html(project_path: str, skip_dirs: set, assigned_port: int, original_port) -> tuple[str, str]:
    """找 index.html 并替换 localhost 路径。"""
    import re as _re
    html_files: list[str] = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if fname.lower().endswith((".html", ".htm")):
                html_files.append(os.path.join(root, fname))
    if not html_files:
        return "", ""
    entry = next((f for f in html_files if os.path.basename(f).lower() == "index.html"), html_files[0])
    try:
        html = open(entry, encoding="utf-8", errors="replace").read()
    except Exception:
        return "", entry
    if original_port:
        html = html.replace(f"http://localhost:{original_port}", f"/api/webapp-proxy/{assigned_port}")
        html = html.replace(f"http://127.0.0.1:{original_port}", f"/api/webapp-proxy/{assigned_port}")
    html = _re.sub(
        r'http://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(/[^"\'\s]*)',
        lambda m: f"/api/webapp-proxy/{assigned_port}" + m.group(1),
        html,
    )
    html = _re.sub(
        r"""(const\s+API_BASE\s*=\s*['"])/api[^'"]*(['"])""",
        rf"\g<1>/api/webapp-proxy/{assigned_port}\g<2>",
        html,
    )
    return html, entry


class AnalyzeProjectRequest(BaseModel):
    project_path: str
    name: str = ""
    description: str = ""


@router.post("/analyze-project")
async def analyze_project(
    req: AnalyzeProjectRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """分析项目目录，按类型自动选择发布策略。"""
    import re as _re
    import json as _json
    from app.models.web_app import WebApp
    from app.routers.web_apps import _user_port
    import secrets as _secrets

    project_path = os.path.abspath(os.path.expanduser(req.project_path))
    if not os.path.isdir(project_path):
        raise HTTPException(400, f"路径不存在或不是目录：{project_path}")

    SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".trae", ".bin", ".local", ".config", ".next", "dist", "build"}
    assigned_port = _user_port(user.id)
    app_name = req.name.strip() or os.path.basename(project_path) or "未命名应用"

    found: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            found.setdefault(fname, []).append(os.path.join(root, fname))

    def _shallowest(paths: list[str]) -> list[str]:
        return sorted(paths, key=lambda p: p.count(os.sep))

    project_type = "static"
    pkg = {}
    backend_cwd: str | None = None
    backend_cmd: str | None = None
    original_port: int | None = None

    for pkg_path in _shallowest(found.get("package.json", [])):
        try:
            p = _json.loads(open(pkg_path).read())
        except Exception:
            continue
        deps = {**p.get("dependencies", {}), **p.get("devDependencies", {})}
        pkg_dir = os.path.dirname(pkg_path)

        if "next" in deps:
            project_type = "nextjs"
            pkg = p
            backend_cwd = pkg_dir
            break

        SPA_MARKERS = {"react", "vue", "vite", "@angular/core", "svelte", "solid-js"}
        if SPA_MARKERS & set(deps.keys()) and not any(
            os.path.exists(os.path.join(pkg_dir, f)) for f in ["server.js", "app.js", "index.js"]
        ):
            project_type = "spa"
            pkg = p
            backend_cwd = pkg_dir
            break

        for js_entry in ["server.js", "app.js", "index.js"]:
            candidate = os.path.join(pkg_dir, js_entry)
            if os.path.exists(candidate):
                project_type = "node"
                pkg = p
                backend_cwd = pkg_dir
                js_src = open(candidate, encoding="utf-8", errors="replace").read()
                has_env_port = bool(_re.search(r'process\.env\.PORT', js_src))
                m = _re.search(r'(?:const|let|var)\s+PORT\s*=\s*(\d{4,5})', js_src)
                if m:
                    original_port = int(m.group(1))
                    if not has_env_port:
                        js_src_fixed = js_src.replace(
                            m.group(0),
                            m.group(0).replace(m.group(1), f"process.env.PORT || {m.group(1)}")
                        )
                        with open(candidate, "w", encoding="utf-8") as _f:
                            _f.write(js_src_fixed)
                break
        if project_type != "static":
            break

    if project_type == "static":
        for req_path in _shallowest(found.get("requirements.txt", [])):
            req_dir = os.path.dirname(req_path)
            for py_entry in ["app.py", "main.py", "server.py", "run.py"]:
                candidate = os.path.join(req_dir, py_entry)
                if os.path.exists(candidate):
                    project_type = "python"
                    backend_cwd = req_dir
                    py_src = open(candidate, encoding="utf-8", errors="replace").read()
                    has_env_port = bool(_re.search(r'os\.environ|os\.getenv', py_src))
                    m = _re.search(r'(port\s*=\s*)(\d{4,5})', py_src, _re.IGNORECASE)
                    if m:
                        original_port = int(m.group(2))
                        if not has_env_port:
                            py_src_fixed = py_src.replace(
                                m.group(0),
                                f"{m.group(1)}int(os.environ.get('PORT', {m.group(2)}))"
                            )
                            if "import os" not in py_src_fixed:
                                py_src_fixed = "import os\n" + py_src_fixed
                            with open(candidate, "w", encoding="utf-8") as _f:
                                _f.write(py_src_fixed)
                    break
            if project_type != "static":
                break

    if project_type == "nextjs":
        start_script = pkg.get("scripts", {}).get("start", "npm start")
        backend_cmd = f"PORT={assigned_port} {start_script}"
        html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{app_name}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}html,body,iframe{{width:100%;height:100%;border:none}}</style>
</head>
<body>
<iframe src="/api/webapp-proxy/{assigned_port}/" allow="same-origin"></iframe>
</body>
</html>"""

    elif project_type == "spa":
        build_dir = None
        for d in ["dist", "build", "out", ".next/static"]:
            candidate = os.path.join(backend_cwd, d)
            if os.path.isdir(candidate):
                build_dir = candidate
                break
        if not build_dir:
            raise HTTPException(400, f"SPA 项目尚未构建，请先在项目目录执行 npm run build，再发布")
        html_content, _ = _read_and_patch_html(build_dir, SKIP_DIRS, assigned_port, None)
        if not html_content:
            raise HTTPException(400, "构建目录下没有找到 index.html")

    elif project_type == "node":
        for js_entry in ["server.js", "app.js", "index.js"]:
            if os.path.exists(os.path.join(backend_cwd, js_entry)):
                start_script = pkg.get("scripts", {}).get("start", "")
                backend_cmd = start_script if start_script else f"node {js_entry}"
                break
        html_content, _ = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, original_port)

    elif project_type == "python":
        for py_entry in ["app.py", "main.py", "server.py", "run.py"]:
            if os.path.exists(os.path.join(backend_cwd, py_entry)):
                backend_cmd = f"python {py_entry}"
                break
        html_content, _ = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, original_port)
        if not html_content and backend_cmd:
            html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{app_name}</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}html,body,iframe{{width:100%;height:100%;border:none}}</style>
</head>
<body>
<iframe src="/api/webapp-proxy/{assigned_port}/" allow="same-origin"></iframe>
</body>
</html>"""

    else:
        html_content, entry = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, None)
        if not html_content:
            raise HTTPException(400, "项目目录下没有找到 HTML 文件，请先用「完成开发，准备发布」Skill 生成 index.html")

    from app.routers.web_apps import _app_port
    share_token = _secrets.token_urlsafe(16)
    web_app = WebApp(
        name=app_name,
        description=req.description.strip() or f"从 {project_path} 发布（{project_type}）",
        html_content="",
        created_by=user.id,
        is_public=True,
        share_token=share_token,
        backend_cmd=backend_cmd,
        backend_cwd=backend_cwd,
        backend_port=None,
    )
    db.add(web_app)
    db.flush()

    final_port = _app_port(web_app.id) if backend_cmd else None
    if final_port and backend_cmd:
        html_content = html_content.replace(
            f"/api/webapp-proxy/{assigned_port}",
            f"/api/webapp-proxy/{final_port}",
        )
        web_app.backend_port = final_port
        if project_type == "nextjs":
            web_app.backend_cmd = backend_cmd.replace(str(assigned_port), str(final_port))

    web_app.html_content = html_content
    db.commit()
    db.refresh(web_app)

    return {
        "id": web_app.id,
        "name": web_app.name,
        "project_type": project_type,
        "share_token": share_token,
        "preview_url": f"/api/web-apps/{web_app.id}/preview",
        "share_url": f"/share/{share_token}",
        "backend_port": final_port,
        "has_backend": bool(backend_cmd),
    }


# ─── Workdir File Manager ──────────────────────────────────────────────────────

@router.get("/workdir/tree")
def workdir_tree(user: User = Depends(get_current_user)):
    """返回用户 workdir 的完整文件树。"""
    workdir = _user_workdir(user)
    tree = _tree(workdir)

    existing_names = {n["name"] for n in tree if n["type"] == "dir"}
    for d in _REQUIRED_TOP_DIRS:
        if d not in existing_names:
            tree.insert(0, {"name": d, "path": d, "type": "dir", "children": []})

    required_set = set(_REQUIRED_TOP_DIRS)
    required_nodes = [n for n in tree if n["name"] in required_set and n["type"] == "dir"]
    other_nodes = [n for n in tree if not (n["name"] in required_set and n["type"] == "dir")]
    order = {name: i for i, name in enumerate(_REQUIRED_TOP_DIRS)}
    required_nodes.sort(key=lambda n: order.get(n["name"], 99))
    tree = required_nodes + other_nodes

    return {"workdir": workdir, "tree": tree}


class MkdirRequest(BaseModel):
    path: str


@router.post("/workdir/mkdir")
def workdir_mkdir(req: MkdirRequest, user: User = Depends(get_current_user)):
    workdir = _user_workdir(user)
    target = _safe_path(workdir, req.path)
    os.makedirs(target, exist_ok=True)
    return {"ok": True, "path": req.path}


class RenameRequest(BaseModel):
    src: str
    dst: str


@router.post("/workdir/rename")
def workdir_rename(req: RenameRequest, user: User = Depends(get_current_user)):
    workdir = _user_workdir(user)
    src = _safe_path(workdir, req.src)
    dst = _safe_path(workdir, req.dst)
    if not os.path.exists(src):
        raise HTTPException(404, "源路径不存在")
    if os.path.exists(dst):
        raise HTTPException(400, "目标路径已存在")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return {"ok": True}


class DeleteRequest(BaseModel):
    path: str


@router.post("/workdir/delete")
def workdir_delete(req: DeleteRequest, user: User = Depends(get_current_user)):
    workdir = _user_workdir(user)
    target = _safe_path(workdir, req.path)
    if not os.path.exists(target):
        raise HTTPException(404, "路径不存在")
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return {"ok": True}


@router.get("/read-file")
def read_file(path: str, user: User = Depends(get_current_user)):
    workdir = _user_workdir(user)
    target = _safe_path(workdir, path)
    if not os.path.exists(target) or os.path.isdir(target):
        raise HTTPException(404, "文件不存在")
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read(64 * 1024)
    except Exception:
        raise HTTPException(400, "无法读取文件")
    return {"content": content}


@router.get("/workdir/download")
def workdir_download(path: str, user: User = Depends(get_current_user)):
    from fastapi.responses import FileResponse
    workdir = _user_workdir(user)

    ws_root = os.path.dirname(workdir)
    if os.path.isabs(path):
        target = os.path.normpath(path)
        if not (target == ws_root or target.startswith(ws_root + os.sep)):
            raise HTTPException(400, "路径不合法")
    else:
        target = _safe_path(workdir, path)
        if not os.path.exists(target):
            alt = os.path.normpath(os.path.join(ws_root, path.lstrip("/")))
            if alt.startswith(ws_root + os.sep) and os.path.exists(alt):
                target = alt

    if not os.path.exists(target):
        raise HTTPException(404, "文件不存在")
    if os.path.isdir(target):
        raise HTTPException(400, "不支持下载文件夹，请先打包")
    filename = os.path.basename(target)
    return FileResponse(
        path=target,
        media_type="application/octet-stream",
        filename=filename,
    )


# ─── 数据视图接口 ─────────────────────────────────────────────────────────────

def _risk_level_from_flags(risk_flags: list[str]) -> str:
    high_flags = {"L0_BLOCKED", "ACCESS_DENIED", "INVALID_SCHEMA", "COMPILE_FAILED"}
    medium_flags = {"AGGREGATE_ONLY", "SYNC_FAILED", "NO_FIELDS", "NO_VIEW", "CEILING_CAPPED", "DECISION_ONLY"}
    if any(f in high_flags for f in risk_flags):
        return "high"
    if any(f in medium_flags for f in risk_flags):
        return "medium"
    return "low"


def _unavailable_reason_from_avail(avail) -> str | None:
    if avail.available:
        return None
    if avail.unavailable_reason:
        return avail.unavailable_reason
    flag_map = {
        "L0_BLOCKED": "披露级别为 L0（禁止访问）",
        "ACCESS_DENIED": "无访问权限",
        "NO_FIELDS": "视图无可见字段",
        "SYNC_FAILED": "数据同步失败",
        "NO_VIEW": "未配置数据视图",
        "INVALID_SCHEMA": "视图 schema 已失效",
        "COMPILE_FAILED": "视图编译失败",
    }
    reasons = []
    for flag in avail.risk_flags:
        if flag in flag_map:
            reasons.append(flag_map[flag])
    return "；".join(reasons) if reasons else "不可用"


@router.get("/data-views")
def list_data_views(
    q: str = "",
    source_type: str = "",
    table_id: Optional[int] = None,
    only_bindable: bool = True,
    include_direct_table: bool = False,
    disclosure_mode: str = "",
    only_available: bool = False,
    include_system: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户可见的数据视图列表。"""
    from app.models.business import (
        BusinessTable, TableView, TableField,
    )
    from app.services.policy_engine import resolve_user_role_groups, resolve_effective_policy
    from app.services.data_view_runtime import assess_view_availability

    disclosure_set = set(disclosure_mode.split(",")) if disclosure_mode else set()

    tq = db.query(BusinessTable).filter(BusinessTable.is_archived == False)  # noqa: E712
    if source_type:
        tq = tq.filter(BusinessTable.source_type == source_type)
    if table_id:
        tq = tq.filter(BusinessTable.id == table_id)
    tables = tq.all()

    result_tables = []
    for bt in tables:
        vq = db.query(TableView).filter(TableView.table_id == bt.id)
        if only_bindable:
            vq = vq.filter(TableView.view_purpose.in_(["skill_runtime", "explore", "ops"]))
        if not include_system:
            vq = vq.filter(TableView.is_system == False)  # noqa: E712
        views = vq.all()

        role_groups = resolve_user_role_groups(db, bt.id, user)
        group_ids = [g.id for g in role_groups]

        view_items = []
        for v in views:
            policy = resolve_effective_policy(db, bt.id, group_ids, view_id=v.id)
            avail = assess_view_availability(v, policy, bt)

            if avail.view_state == "permission_blocked" and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
                continue
            if policy.denied and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
                continue
            if disclosure_set and (v.disclosure_ceiling or "") not in disclosure_set:
                continue
            if only_available and not avail.available:
                continue
            if q:
                haystack = f"{bt.display_name} {bt.table_name} {v.name}".lower()
                if q.lower() not in haystack:
                    continue

            field_count = len(v.visible_field_ids or [])
            risk_flags = avail.risk_flags
            available = avail.available
            risk_level = _risk_level_from_flags(risk_flags)

            view_items.append({
                "view_id": v.id,
                "view_name": v.name,
                "view_purpose": v.view_purpose,
                "view_kind": v.view_kind,
                "disclosure_ceiling": v.disclosure_ceiling,
                "is_system": v.is_system or False,
                "is_default": v.is_default or False,
                "result_mode": avail.display_mode,
                "field_count": field_count,
                "available": available,
                "risk_level": risk_level,
                "display_mode": avail.display_mode,
                "risk_flags": risk_flags,
                "view_state": avail.view_state,
                "unavailable_reason": _unavailable_reason_from_avail(avail),
            })

        view_items.sort(key=lambda x: (0 if x["available"] else 1, x["view_id"]))

        if not views and not view_items:
            if include_direct_table and user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
                view_items.append({
                    "view_id": None,
                    "view_name": None,
                    "view_purpose": None,
                    "view_kind": None,
                    "disclosure_ceiling": None,
                    "is_system": False,
                    "is_default": False,
                    "result_mode": "blocked",
                    "field_count": 0,
                    "available": False,
                    "risk_level": "high",
                    "display_mode": "blocked",
                    "risk_flags": ["NO_VIEW"],
                    "unavailable_reason": "未配置数据视图",
                })

        if q and not view_items:
            haystack = f"{bt.display_name} {bt.table_name}".lower()
            if q.lower() not in haystack:
                continue

        if view_items or not q:
            result_tables.append({
                "table_id": bt.id,
                "table_name": bt.table_name,
                "display_name": bt.display_name or bt.table_name,
                "source_type": bt.source_type,
                "sync_status": bt.sync_status,
                "record_count_cache": bt.record_count_cache,
                "views": view_items,
            })

    return {"ok": True, "tables": result_tables}


@router.get("/data-views/{view_id}")
def get_data_view_detail(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回单个数据视图的详情。"""
    from app.models.business import (
        BusinessTable, TableView, TableField,
    )
    from app.services.policy_engine import (
        resolve_user_role_groups, resolve_effective_policy,
        check_disclosure_capability,
    )
    from app.services.data_view_runtime import execute_view_read, assess_view_availability

    view = db.get(TableView, view_id)
    if not view:
        raise HTTPException(404, "视图不存在")

    view_state = getattr(view, "view_state", None) or "available"

    bt = db.get(BusinessTable, view.table_id)
    if not bt:
        raise HTTPException(404, "关联数据表不存在")
    if bt.is_archived:
        raise HTTPException(400, f"数据表 '{bt.display_name}' 已归档")

    role_groups = resolve_user_role_groups(db, bt.id, user)
    group_ids = [g.id for g in role_groups]
    policy = resolve_effective_policy(db, bt.id, group_ids, view_id=view.id)

    if policy.denied and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权访问此视图: " + "; ".join(policy.deny_reasons))

    avail = assess_view_availability(view, policy, bt)
    caps = check_disclosure_capability(policy.disclosure_level)

    all_fields = db.query(TableField).filter(TableField.table_id == bt.id).order_by(TableField.sort_order).all()
    view_field_ids = set(view.visible_field_ids or [])
    visible_fields = [f for f in all_fields if f.id in view_field_ids] if view_field_ids else all_fields

    fields_info = [
        {
            "id": f.id,
            "field_name": f.field_name,
            "display_name": f.display_name or f.field_name,
            "field_type": f.field_type,
            "is_enum": f.is_enum or False,
            "enum_values": f.enum_values or [],
            "is_sensitive": f.is_sensitive or False,
            "is_filterable": f.is_filterable or False,
            "is_groupable": f.is_groupable or False,
            "is_sortable": f.is_sortable or False,
        }
        for f in visible_fields
    ]

    permission_summary = {
        "disclosure_level": policy.disclosure_level,
        "row_access_mode": policy.row_access_mode,
        "tool_permission_mode": policy.tool_permission_mode,
        "denied": policy.denied,
        "deny_reasons": policy.deny_reasons,
        "capabilities": caps,
    }

    preview = None
    if avail.available and avail.display_mode in ("rows", "aggregate"):
        try:
            result = execute_view_read(db, view_id, user, limit=20)
            preview = result.to_dict()
        except Exception as e:
            preview = {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "table": {
            "id": bt.id,
            "table_name": bt.table_name,
            "display_name": bt.display_name or bt.table_name,
            "source_type": bt.source_type,
            "sync_status": bt.sync_status,
        },
        "view": {
            "id": view.id,
            "name": view.name,
            "view_kind": view.view_kind,
            "view_purpose": view.view_purpose,
            "disclosure_ceiling": view.disclosure_ceiling,
            "is_system": view.is_system,
            "is_default": view.is_default,
            "result_mode": avail.display_mode,
            "view_state": avail.view_state,
        },
        "fields": fields_info,
        "permission": permission_summary,
        "availability": {
            "available": avail.available,
            "risk_flags": avail.risk_flags,
            "display_mode": avail.display_mode,
            "view_state": avail.view_state,
            "unavailable_reason": _unavailable_reason_from_avail(avail),
        },
        "preview": preview,
    }


# ─── Admin: 运维诊断接口 ─────────────────────────────────────────────────────

@router.get("/admin/instances")
def admin_list_instances(
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """返回所有活跃 opencode 实例的资源快照。"""
    return list_all_instances()
