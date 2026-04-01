"""Dev Studio — opencode web 全局单例进程 + save-to-tool/skill."""
import asyncio
import glob as _glob
import json
import os
import shutil
import socket
import tempfile
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillStatus, SkillVersion
from app.models.tool import ToolRegistry, ToolType, SkillTool
from app.models.user import User, Role

router = APIRouter(prefix="/api/dev-studio", tags=["dev-studio"])

# ─── 按用户隔离的实例池 ────────────────────────────────────────────────────────
# 每个 user_id 对应独立的 opencode 进程 + workdir + 端口
# 结构：{user_id: {"proc": Process, "port": int, "workdir": str, "lock": Lock, "last_active": float}}
_user_instances: dict = {}
_instances_lock: object = None   # 全局 asyncio.Lock，保护 _user_instances 写入

IDLE_TIMEOUT_SECONDS = 900   # 15分钟无操作自动回收（降低内存压力）
_REAPER_INTERVAL = 300        # 每5分钟检查一次空闲实例
_idle_reaper_task = None
MAX_ACTIVE_INSTANCES = 12    # 最多同时运行 12 个 opencode 进程

# 每个用户 workspace 目录总大小上限（包含 .local 等隐藏目录，超出后删最老 session + VACUUM）
WORKSPACE_MAX_GB = 1
_db_cleaner_task = None


# ─── 统一 ignore 集合：所有目录跳过逻辑共用此集合 ──────────────────────────
RUNTIME_IGNORE_DIRS = {
    ".git", ".bin", ".bun", ".cache", ".config", ".local", ".opencode",
    "node_modules", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".trae", ".npm", ".pnpm-store",
    "runtime",  # 隔离后的运行时目录
}

_DIR_SIZE_SKIP = RUNTIME_IGNORE_DIRS


def _dir_size_bytes(path: str) -> int:
    """递归计算目录下所有文件的总字节数（跳过运行时/缓存/依赖目录）。"""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        # 原地修改 dirnames 跳过无关目录，避免递归进缓存目录
        dirnames[:] = [d for d in dirnames if d not in _DIR_SIZE_SKIP]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                pass
    return total


# ─── 目录布局：物理隔离 project（用户文件）和 runtime（OpenCode运行时）─────
# workspace_root/<user>/project/  — OpenCode cwd，只含用户项目文件
# workspace_root/<user>/runtime/data/    — XDG_DATA_HOME（opencode.db 等）
# workspace_root/<user>/runtime/config/  — XDG_CONFIG_HOME（opencode config/skills）
# workspace_root/<user>/runtime/cache/   — 缓存、日志
# workspace_root/<user>/runtime/bin/     — 假 open 等注入脚本

# ─── 统一路径工具函数 ────────────────────────────────────────────────────────

def _studio_root() -> str:
    """返回 studio workspace 总根目录。"""
    from app.config import settings as _cfg
    return os.path.abspath(os.path.expanduser(
        getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
    ))


def _workspace_root_for_user(user_id: int, display_name: str = "") -> str:
    """返回用户顶层工作区目录，固定为 studio_root/user_<id>。

    display_name 仅用于旧目录迁移时定位旧路径，不参与新路径拼接。
    """
    import re as _re
    studio_root = _studio_root()
    new_root = os.path.join(studio_root, f"user_{user_id}")

    # 旧目录迁移：如果旧 display_name 目录存在而新 user_<id> 目录不存在，整体迁移
    if display_name and not os.path.isdir(new_root):
        safe_name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', display_name).strip('_')
        if safe_name and safe_name != f"user_{user_id}":
            old_root = os.path.join(studio_root, safe_name)
            if os.path.isdir(old_root):
                import logging
                logger = logging.getLogger(__name__)
                try:
                    shutil.move(old_root, new_root)
                    logger.info(
                        f"[Migration] 旧目录迁移: {old_root} -> {new_root}, "
                        f"user_id={user_id}, display_name={display_name}"
                    )
                except Exception as e:
                    logger.warning(f"[Migration] 迁移失败: {old_root} -> {new_root}: {e}")

    # 新目录已存在，但旧目录也存在 — 合并迁移
    if display_name and os.path.isdir(new_root):
        import re as _re2
        safe_name = _re2.sub(r'[^\w\u4e00-\u9fff\-]', '_', display_name).strip('_')
        if safe_name and safe_name != f"user_{user_id}":
            old_root = os.path.join(studio_root, safe_name)
            if os.path.isdir(old_root):
                import logging, time as _time_mod
                logger = logging.getLogger(__name__)
                moved_count = 0
                for item in os.listdir(old_root):
                    src = os.path.join(old_root, item)
                    dst = os.path.join(new_root, item)
                    if not os.path.exists(dst):
                        try:
                            shutil.move(src, dst)
                            moved_count += 1
                        except Exception:
                            pass
                # 迁移完成后重命名旧目录
                ts = int(_time_mod.time())
                migrated_name = f".migrated_{ts}_{safe_name}"
                try:
                    os.rename(old_root, os.path.join(studio_root, migrated_name))
                except Exception:
                    shutil.rmtree(old_root, ignore_errors=True)
                logger.info(
                    f"[Migration] 合并迁移: {old_root} -> {new_root}, "
                    f"user_id={user_id}, moved_files={moved_count}"
                )

    return new_root


def _workspace_project_dir(workdir: str) -> str:
    """返回用户工作区的 project 子目录（OpenCode cwd）。"""
    return os.path.join(workdir, "project")


def _workspace_runtime_dir(workdir: str) -> str:
    """返回用户工作区的 runtime 子目录（OpenCode 运行时数据）。"""
    return os.path.join(workdir, "runtime")


def _workspace_runtime_data_dir(workdir: str) -> str:
    """返回 runtime/data（XDG_DATA_HOME）。"""
    return os.path.join(workdir, "runtime", "data")


def _workspace_runtime_config_dir(workdir: str) -> str:
    """返回 runtime/config（XDG_CONFIG_HOME）。"""
    return os.path.join(workdir, "runtime", "config")


def _user_opencode_db_path(workdir: str) -> Optional[str]:
    """返回当前用户的 opencode.db 路径，优先新布局，兼容旧布局。
    如果两个位置都不存在，返回新布局路径（供创建用）。
    """
    new_path = os.path.join(workdir, "runtime", "data", "opencode", "opencode.db")
    if os.path.exists(new_path):
        return new_path
    old_path = os.path.join(workdir, ".local", "share", "opencode", "opencode.db")
    if os.path.exists(old_path):
        return old_path
    return new_path  # 新布局路径作为默认


# ─── 旧布局残留检测 + 迁移 ──────────────────────────────────────────────────

_OLD_LAYOUT_MARKERS = {".local", ".config", ".bin", ".opencode"}


def _has_old_layout_residue(workdir: str) -> bool:
    """检查 workdir 根目录下是否还残留旧布局目录/文件。"""
    for marker in _OLD_LAYOUT_MARKERS:
        if os.path.exists(os.path.join(workdir, marker)):
            return True
    if os.path.exists(os.path.join(workdir, "opencode.json")):
        return True
    return False


def _is_layout_complete(workdir: str) -> bool:
    """检查新布局是否完整：project/ + runtime/data/ + runtime/config/ 都存在，且无旧残留。"""
    project_dir = _workspace_project_dir(workdir)
    runtime_data = _workspace_runtime_data_dir(workdir)
    runtime_config = _workspace_runtime_config_dir(workdir)
    if not (os.path.isdir(project_dir) and os.path.isdir(runtime_data) and os.path.isdir(runtime_config)):
        return False
    if _has_old_layout_residue(workdir):
        return False
    return True


def _migrate_workspace_layout(workdir: str) -> None:
    """将旧布局（所有文件混在 workdir 根）迁移到新的 project/runtime 分离布局。

    判定规则：只有布局完整（project/ + runtime/data/ + runtime/config/ 都存在）
    且根目录不存在旧布局残留（.local/.config/.bin/.opencode/opencode.json）才跳过。
    否则继续迁移。
    """
    import logging
    logger = logging.getLogger(__name__)

    # 布局已完整且无旧残留：跳过
    if _is_layout_complete(workdir):
        return

    project_dir = _workspace_project_dir(workdir)
    runtime_dir = _workspace_runtime_dir(workdir)

    # 全新工作区（无任何旧布局痕迹也无新布局）：无需迁移，ensure_workspace_layout 会创建
    if not os.path.isdir(project_dir) and not _has_old_layout_residue(workdir):
        return

    logger.info(f"[Migration] 迁移工作区布局: {workdir}")

    # 1. 确保新目录结构存在
    os.makedirs(project_dir, exist_ok=True)
    runtime_data = _workspace_runtime_data_dir(workdir)
    runtime_config = _workspace_runtime_config_dir(workdir)
    runtime_cache = os.path.join(runtime_dir, "cache")
    runtime_bin = os.path.join(runtime_dir, "bin")
    for d in (runtime_data, runtime_config, runtime_cache, runtime_bin):
        os.makedirs(d, exist_ok=True)

    # 2. 迁移运行时目录
    # .local/share/* → runtime/data/*
    old_share = os.path.join(workdir, ".local", "share")
    if os.path.isdir(old_share):
        for item in os.listdir(old_share):
            src = os.path.join(old_share, item)
            dst = os.path.join(runtime_data, item)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    # 清理整个 .local（即使 share 已空）
    old_local = os.path.join(workdir, ".local")
    if os.path.isdir(old_local):
        shutil.rmtree(old_local, ignore_errors=True)

    # .config/* → runtime/config/*
    old_config_dir = os.path.join(workdir, ".config")
    if os.path.isdir(old_config_dir):
        for item in os.listdir(old_config_dir):
            src = os.path.join(old_config_dir, item)
            dst = os.path.join(runtime_config, item)
            if not os.path.exists(dst):
                shutil.move(src, dst)
        shutil.rmtree(old_config_dir, ignore_errors=True)

    # .bin/* → runtime/bin/*
    old_bin = os.path.join(workdir, ".bin")
    if os.path.isdir(old_bin):
        for item in os.listdir(old_bin):
            src = os.path.join(old_bin, item)
            dst = os.path.join(runtime_bin, item)
            if not os.path.exists(dst):
                shutil.move(src, dst)
        shutil.rmtree(old_bin, ignore_errors=True)

    # 删除旧的 .opencode（skills 会重新同步到 runtime/config/opencode/skills）
    old_opencode = os.path.join(workdir, ".opencode")
    if os.path.isdir(old_opencode):
        shutil.rmtree(old_opencode, ignore_errors=True)

    # 删除旧的 opencode.json（会重新写到 runtime/config/opencode/config.json）
    old_config_json = os.path.join(workdir, "opencode.json")
    if os.path.exists(old_config_json):
        os.remove(old_config_json)

    # 3. 将剩余用户文件移到 project/
    _skip_move = {"project", "runtime"}
    for item in os.listdir(workdir):
        if item in _skip_move:
            continue
        src = os.path.join(workdir, item)
        dst = os.path.join(project_dir, item)
        if not os.path.exists(dst):
            shutil.move(src, dst)

    # 4. 迁移后校验：如果还有旧残留，记 warning
    if _has_old_layout_residue(workdir):
        residue = [m for m in _OLD_LAYOUT_MARKERS if os.path.exists(os.path.join(workdir, m))]
        if os.path.exists(os.path.join(workdir, "opencode.json")):
            residue.append("opencode.json")
        logger.warning(f"[Migration] 迁移后仍有旧残留: {workdir} -> {residue}")

    logger.info(f"[Migration] 工作区迁移完成: {workdir}")


# ─── 统一工作区初始化入口 ────────────────────────────────────────────────────

def ensure_workspace_layout(workdir: str, display_name: str = "") -> tuple[str, str]:
    """统一的工作区初始化入口。

    负责：
    1. 创建 workdir 根目录
    2. 执行旧布局迁移（如有需要）
    3. 创建 project/ 及首批项目目录（src/docs/scripts/README.md）
    4. 创建 runtime/ 下所有子目录（data/config/cache/bin）

    返回 (project_dir, runtime_dir)。
    所有写文件入口必须通过此函数进入，不能分叉。
    """
    os.makedirs(workdir, exist_ok=True)

    # 迁移旧布局（幂等）
    _migrate_workspace_layout(workdir)

    # 创建新布局目录结构
    project_dir = _workspace_project_dir(workdir)
    runtime_dir = _workspace_runtime_dir(workdir)
    is_new = not os.path.exists(project_dir)

    os.makedirs(project_dir, exist_ok=True)
    for rd in ("data", "config", "cache", "bin"):
        os.makedirs(os.path.join(runtime_dir, rd), exist_ok=True)

    # 确保四个一级业务目录始终存在
    for subdir in ("inbox", "work", "export", "archive"):
        os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

    # 首次创建：初始化 README
    if is_new:
        readme = os.path.join(project_dir, "README.md")
        if not os.path.exists(readme):
            folder_name = os.path.basename(workdir)
            with open(readme, "w", encoding="utf-8") as f:
                f.write(
                    f"# {display_name or folder_name} 的工作台\n\n"
                    "这是你的专属开发工作台，文件会持久保存。\n\n"
                    "## 目录说明\n\n"
                    "- **inbox/** — 系统生成的待处理文件\n"
                    "- **work/** — 用户上传、AI 生成的工作文件\n"
                    "- **export/** — 导出产物\n"
                    "- **archive/** — 归档内容\n"
                )

    return project_dir, runtime_dir


def _cleanup_workspace_if_needed(workdir: str, max_bytes: int) -> None:
    """若 workdir 总大小超过 max_bytes，删最老 session 直到达标。同步函数，可在启动前直接调用。"""
    import sqlite3 as _sqlite3
    import logging
    logger = logging.getLogger(__name__)

    ws_bytes = _dir_size_bytes(workdir)
    if ws_bytes <= max_bytes:
        return
    db_path = _user_opencode_db_path(workdir)
    if not os.path.exists(db_path):
        return
    try:
        con = _sqlite3.connect(db_path)
        deleted = 0
        while ws_bytes > max_bytes:
            row = con.execute(
                "SELECT id FROM session ORDER BY time_updated ASC LIMIT 1"
            ).fetchone()
            if not row:
                # session 已删完但仍超限，说明存在大文件，不能继续清理
                logger.warning(
                    f"[DbCleaner] {os.path.basename(workdir)}: session 已删完但 workspace 仍超限 "
                    f"({ws_bytes / 1024**3:.2f}GB > {max_bytes / 1024**3:.2f}GB)，"
                    f"可能存在用户上传的大文件"
                )
                break
            sid = row[0]
            con.execute("DELETE FROM part WHERE session_id = ?", (sid,))
            con.execute("DELETE FROM message WHERE session_id = ?", (sid,))
            con.execute("DELETE FROM session WHERE id = ?", (sid,))
            con.commit()
            deleted += 1
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            ws_bytes = _dir_size_bytes(workdir)
        con.close()
        name = os.path.basename(workdir)
        logger.info(
            f"[DbCleaner] {name}: 删除 {deleted} 个旧 session，"
            f"workspace 现在 {ws_bytes / 1024**3:.2f}GB"
        )
    except Exception as e:
        logger.warning(f"[DbCleaner] {os.path.basename(workdir)} 清理失败: {e}")


async def _db_cleaner() -> None:
    """每5分钟扫一遍所有用户 workspace，总大小超过 WORKSPACE_MAX_GB 则清理。"""
    import logging
    max_bytes = WORKSPACE_MAX_GB * 1024 ** 3

    while True:
        await asyncio.sleep(300)
        try:
            from app.config import settings as _cfg
            studio_root = os.path.abspath(os.path.expanduser(
                getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
            ))
            for user_dir in os.scandir(studio_root):
                if not user_dir.is_dir():
                    continue
                _cleanup_workspace_if_needed(user_dir.path, max_bytes)
        except Exception as e:
            logging.getLogger(__name__).warning(f"[DbCleaner] 扫描失败: {e}")


MAX_RSS_MB = 500  # 单实例内存硬上限（含 Go 主进程 + Node 启动器），超过则强制重启
MAX_FD_COUNT = 500   # 单实例 fd 上限，pty 泄漏时 fd 会持续累积，超过则强制重启


def _get_proc_tree_rss_mb(pid: int) -> int:
    """获取进程及其所有子进程的 RSS 总和（MB）。"""
    try:
        # 用 ps 取该 pid 及所有后代进程的 RSS
        import subprocess
        result = subprocess.run(
            ["ps", "-o", "rss=", "--pid", str(pid), "--ppid", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        total_kb = sum(int(line.strip()) for line in result.stdout.strip().split('\n') if line.strip().isdigit())
        return total_kb // 1024
    except Exception:
        return 0


async def _idle_reaper() -> None:
    """后台任务：每2分钟扫一遍，回收空闲实例 + 杀掉内存超限实例。"""
    import logging
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(_REAPER_INTERVAL)
        now = _time.time()
        for uid, inst in list(_user_instances.items()):
            proc = inst.get("proc")
            if proc is None or proc.returncode is not None:
                continue
            # 空闲超时回收
            last = inst.get("last_active", now)
            if now - last > IDLE_TIMEOUT_SECONDS:
                logger.info(f"[Reaper] user={uid} 空闲超时，终止进程")
                try:
                    proc.terminate()
                except Exception:
                    pass
                inst["proc"] = None
                continue
            # 内存超限强杀
            rss = _get_proc_tree_rss_mb(proc.pid)
            if rss > MAX_RSS_MB:
                logger.warning(f"[Reaper] user={uid} RSS={rss}MB 超过 {MAX_RSS_MB}MB 限制，强制终止")
                try:
                    proc.kill()
                except Exception:
                    pass
                inst["proc"] = None
                continue
            # fd 泄漏强杀（pty 不释放导致 /dev/ptmx fd 堆积）
            try:
                fd_count = len(os.listdir(f"/proc/{proc.pid}/fd"))
            except Exception:
                fd_count = 0
            if fd_count > MAX_FD_COUNT:
                logger.warning(
                    f"[Reaper] user={uid} fd={fd_count} 超过 {MAX_FD_COUNT} 限制（pty泄漏），强制终止"
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                inst["proc"] = None

        # ── 游离进程扫描（安全版）──
        # 收集所有托管进程及其子进程树的 PID
        managed_pids = set()
        managed_user_cwd_markers = set()
        for _uid, _inst in _user_instances.items():
            _p = _inst.get("proc")
            if _p and _p.returncode is None:
                managed_pids.add(_p.pid)
                try:
                    import subprocess as _sp
                    _children = _sp.run(
                        ["pgrep", "-P", str(_p.pid)],
                        capture_output=True, text=True, timeout=5,
                    )
                    for _cl in _children.stdout.strip().splitlines():
                        if _cl.strip().isdigit():
                            managed_pids.add(int(_cl.strip()))
                except Exception:
                    pass
            managed_user_cwd_markers.add(f"user_{_uid}/")

        try:
            import subprocess as _sp
            result = _sp.run(
                ["pgrep", "-f", ".opencode web"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                except ValueError:
                    continue
                if pid in managed_pids:
                    continue
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except Exception:
                    cwd = "unknown"
                # 通过 cwd 检查是否属于已托管用户的子进程
                if any(marker in cwd for marker in managed_user_cwd_markers):
                    logger.debug(f"[Reaper] pid={pid} cwd={cwd} 属于已托管用户子进程，跳过")
                    continue
                # 通过 cwd 提取 user_id，尝试认领
                import re as _re
                _m = _re.search(r"user_(\d+)", cwd)
                if _m:
                    _orphan_uid = int(_m.group(1))
                    if _orphan_uid not in _user_instances:
                        _user_instances[_orphan_uid] = {
                            "proc": None,
                            "port": _port_for_user(_orphan_uid),
                            "workdir": None,
                            "lock": asyncio.Lock(),
                            "last_active": _time.time(),
                        }
                        logger.info(f"[Reaper] 认领 user={_orphan_uid} 的遗留进程 pid={pid}，纳入管理")
                        continue
                # 真正的游离进程
                logger.warning(f"[Reaper] 游离进程 pid={pid}（cwd={cwd}），强制终止")
                try:
                    import signal as _sig
                    os.kill(pid, _sig.SIGKILL)
                except Exception:
                    pass
        except Exception as _e:
            logger.debug(f"[Reaper] 游离进程扫描失败: {_e}")


def _kill_orphan_opencode_procs():
    """Startup: 扫描遗留 opencode 进程，通过 cwd 认领而非无条件杀死。"""
    import logging as _log, signal as _sig, subprocess as _sp, re as _re
    logger = _log.getLogger(__name__)
    try:
        result = _sp.run(["pgrep", "-f", ".opencode web"],
                         capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except Exception:
                cwd = "unknown"
            # 通过 cwd 提取 user_id，尝试认领到 _user_instances
            _m = _re.search(r"user_(\d+)", cwd)
            if _m:
                _uid = int(_m.group(1))
                if _uid not in _user_instances:
                    _user_instances[_uid] = {
                        "proc": None,
                        "port": _port_for_user(_uid),
                        "workdir": None,
                        "lock": asyncio.Lock(),
                        "last_active": _time.time(),
                    }
                    logger.info(f"[Startup] 认领遗留进程 user={_uid} pid={pid} cwd={cwd}")
                else:
                    logger.debug(f"[Startup] pid={pid} 属于已托管 user={_uid}，跳过")
            else:
                # cwd 无法识别用户，真正的游离进程
                logger.warning(f"[Startup] orphan pid={pid} cwd={cwd}, killing")
                try:
                    os.kill(pid, _sig.SIGKILL)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"[Startup] orphan scan failed: {e}")


def _start_idle_reaper():
    global _idle_reaper_task
    _kill_orphan_opencode_procs()  # kill any leaked procs from previous run
    _idle_reaper_task = asyncio.create_task(_idle_reaper())


async def shutdown_all_instances() -> None:
    """uvicorn 关闭时调用：终止所有托管的 opencode 子进程，防止产生游离进程。"""
    import logging
    logger = logging.getLogger(__name__)
    for uid, inst in list(_user_instances.items()):
        proc = inst.get("proc")
        if proc is None or proc.returncode is not None:
            continue
        logger.info(f"[Shutdown] 终止 user={uid} pid={proc.pid}")
        try:
            proc.terminate()
        except Exception:
            pass
    # 等待最多 5 秒让进程优雅退出
    import asyncio as _aio
    await _aio.sleep(2)
    for uid, inst in list(_user_instances.items()):
        proc = inst.get("proc")
        if proc is not None and proc.returncode is None:
            logger.warning(f"[Shutdown] user={uid} pid={proc.pid} 未退出，强杀")
            try:
                proc.kill()
            except Exception:
                pass
        inst["proc"] = None

OPENCODE_BASE_PORT = 17171   # user_id=1 → 17172, user_id=2 → 17173, ...


def _port_for_user(user_id: int) -> int:
    """每个用户分配固定端口，重启后不变。端口 = BASE + user_id。"""
    return OPENCODE_BASE_PORT + user_id

BAILIAN_DEFAULT_MODEL = "bailian-coding-plan/glm-5"
ARK_DEFAULT_MODEL = "ark/doubao-seed-2.0-code"

# 百炼中与 ARK 重复的模型 ID（百炼优先，触发 fallback 时改用 ARK）
_BAILIAN_ARK_OVERLAP = {"glm-4.7", "kimi-k2.5", "MiniMax-M2.5"}

# 内存级 fallback 状态（优先于 settings，可由 API 动态写入）
_runtime_fallback: dict = {"use_ark": False}

# ─── 百炼用量估算计数器（三窗口滑动）──────────────────────────────────────────
# 每次采样记录 (timestamp, calls) 事件，窗口内求和对比阈值
import collections as _collections
import time as _time

_usage_counter: dict = {
    "monitor_task": None,
    # 触发 fallback 的原因，便于查询
    "trigger_reason": "",
}

# 三窗口秒数
_WINDOW_5H  = 5  * 3600
_WINDOW_7D  = 7  * 86400
_WINDOW_30D = 30 * 86400


def _all_opencode_db_paths() -> list[str]:
    """收集所有用户的 opencode.db 路径（含全局兜底路径）。
    通过 _user_opencode_db_path 自动兼容新旧布局。
    """
    from app.config import settings as _cfg3
    _studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg3, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
    paths: list[str] = []
    seen = set()

    def _add(p: str):
        if p not in seen:
            seen.add(p)
            paths.append(p)

    # 各用户独立目录（内存中已知实例）
    for uid, inst in list(_user_instances.items()):
        wdir = inst.get("workdir") or os.path.join(_studio_root, f"user_{uid}")
        _add(_user_opencode_db_path(wdir))
    # 扫描 studio_root 下所有用户目录（兼容重启后 _user_instances 为空的情况）
    if os.path.isdir(_studio_root):
        for name in os.listdir(_studio_root):
            wdir = os.path.join(_studio_root, name)
            if os.path.isdir(wdir):
                _add(_user_opencode_db_path(wdir))
    # 全局路径兜底
    global_db = os.environ.get("OPENCODE_DB_PATH", os.path.expanduser("~/.local/share/opencode/opencode.db"))
    _add(global_db)
    return paths


def _count_ai_calls(since_ms: int) -> int:
    """统计所有用户 opencode.db 中 since_ms（毫秒时间戳）之后的 LLM 调用次数。
    以 part 表中 type='step-finish' 的条数计算：
    每条 step-finish = 1 次实际 API 调用（含 tool-calls 中间轮和最终 stop 轮）。
    """
    import sqlite3 as _sqlite3
    total = 0
    for db_path in _all_opencode_db_paths():
        if not os.path.exists(db_path):
            continue
        try:
            con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute(
                "SELECT COUNT(*) FROM part "
                "WHERE json_extract(data, '$.type') = 'step-finish' "
                "  AND time_created >= ?",
                (since_ms,),
            ).fetchone()
            con.close()
            total += int(row[0]) if row else 0
        except Exception:
            pass
    return total


async def _bailian_usage_monitor() -> None:
    """每 5 分钟直接统计 opencode message 表中 assistant 消息条数，
    任一窗口超 90% 自动切换 ARK。"""
    import logging
    logger = logging.getLogger(__name__)

    while True:
        await asyncio.sleep(300)  # 5 分钟
        try:
            from app.config import settings as _settings
            q5h  = getattr(_settings, "BAILIAN_QUOTA_5H",  6000)
            q7d  = getattr(_settings, "BAILIAN_QUOTA_7D",  45000)
            q30d = getattr(_settings, "BAILIAN_QUOTA_30D", 90000)

            now_ms = int(_time.time() * 1000)
            s5h  = _count_ai_calls(now_ms - _WINDOW_5H  * 1000)
            s7d  = _count_ai_calls(now_ms - _WINDOW_7D  * 1000)
            s30d = _count_ai_calls(now_ms - _WINDOW_30D * 1000)

            logger.info(
                f"[BailianMonitor] 5h={s5h}/{q5h} 7d={s7d}/{q7d} 30d={s30d}/{q30d}"
            )

            # 任一窗口超 90% 触发 fallback
            reason = ""
            if s5h  >= q5h  * 0.9: reason = f"5h调用 {s5h}/{q5h}"
            elif s7d  >= q7d  * 0.9: reason = f"7d调用 {s7d}/{q7d}"
            elif s30d >= q30d * 0.9: reason = f"30d调用 {s30d}/{q30d}"

            if reason and not _runtime_fallback["use_ark"]:
                logger.warning(f"[BailianMonitor] {reason} 已达90%，自动切换 ARK")
                _runtime_fallback["use_ark"] = True
                _usage_counter["trigger_reason"] = reason

                bailian_key = os.environ.get("BAILIAN_API_KEY", "")
                ark_key = os.environ.get("ARK_API_KEY", "")
                # 只更新配置文件，不主动杀进程（避免中断用户当前操作）。
                # 下次用户调用 /instance 时，_ensure_user_instance 检测到配置变化会自动重启。
                for uid, inst in list(_user_instances.items()):
                    wdir = inst.get("workdir")
                    if not wdir:
                        continue
                    _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=True)
        except Exception as e:
            logging.getLogger(__name__).warning(f"[BailianMonitor] 采样失败: {e}")


def _resolve_default_model(use_ark_fallback: bool) -> str:
    """根据是否 fallback 返回默认模型。"""
    if use_ark_fallback:
        return ARK_DEFAULT_MODEL
    return BAILIAN_DEFAULT_MODEL


def _write_opencode_config(
    workdir: str,
    bailian_key: str = "",
    ark_key: str = "",
    use_ark_fallback: bool = False,
    lemondata_key: str = "",
) -> None:
    """写入 workdir 的 opencode.json，包含完整 provider 配置。

    优先级规则：
    - 百炼与 ARK 有重叠模型时，百炼优先（ARK 侧以不同 ID 暴露）
    - use_ark_fallback=True 时，默认模型切换到 ARK，百炼仍保留可手动选用
    """
    default_model = _resolve_default_model(use_ark_fallback)

    config: dict = {
        "$schema": "https://opencode.ai/config.schema.json",
        "model": default_model,
        "snapshot": False,
        "provider": {
            "bailian-coding-plan": {
                "npm": "@ai-sdk/anthropic",
                "name": "百炼 Coding Plan",
                "options": {
                    "baseURL": "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1",
                    "apiKey": bailian_key if bailian_key else "{env:BAILIAN_API_KEY}",
                },
                "models": {
                    "qwen3.5-plus": {
                        "name": "Qwen3.5 Plus",
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "options": {"thinking": {"type": "enabled", "budgetTokens": 8192}},
                        "limit": {"context": 1000000, "output": 65536},
                    },
                    "qwen3-coder-next": {
                        "name": "Qwen3 Coder Next",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 262144, "output": 65536},
                    },
                    "qwen3-coder-plus": {
                        "name": "Qwen3 Coder Plus",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 1000000, "output": 65536},
                    },
                    "glm-5": {
                        "name": "GLM-5",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "options": {"thinking": {"type": "enabled", "budgetTokens": 8192}},
                        "limit": {"context": 202752, "output": 16384},
                    },
                    # 重叠模型：百炼优先保留，ARK 侧用 ark/ 前缀区分
                    "glm-4.7": {
                        "name": "GLM-4.7 (百炼)",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    "kimi-k2.5": {
                        "name": "Kimi K2.5 (百炼)",
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "options": {"thinking": {"type": "enabled", "budgetTokens": 8192}},
                        "limit": {"context": 262144, "output": 32768},
                    },
                    "MiniMax-M2.5": {
                        "name": "MiniMax M2.5 (百炼)",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 1000000, "output": 65536},
                    },
                },
            },
            "ark": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "火山引擎 ARK",
                "options": {
                    "baseURL": "https://ark.cn-beijing.volces.com/api/coding/v3",
                    "apiKey": ark_key if ark_key else "{env:ARK_API_KEY}",
                },
                "models": {
                    "doubao-seed-2.0-code": {
                        "name": "Doubao Seed 2.0 Code",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    "doubao-seed-2.0-pro": {
                        "name": "Doubao Seed 2.0 Pro",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    "doubao-seed-2.0-lite": {
                        "name": "Doubao Seed 2.0 Lite",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    "doubao-seed-code": {
                        "name": "Doubao Seed Code",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    # 重叠模型：ARK 侧带 (ARK) 后缀区分，百炼版本优先
                    "minimax-m2.5": {
                        "name": "MiniMax M2.5 (ARK)",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 1000000, "output": 65536},
                    },
                    "glm-4.7": {
                        "name": "GLM-4.7 (ARK)",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                    "deepseek-v3.2": {
                        "name": "DeepSeek V3.2",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 65536, "output": 16384},
                    },
                    "kimi-k2.5": {
                        "name": "Kimi K2.5 (ARK)",
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 131072, "output": 16384},
                    },
                },
            },
            "lemondata": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "LemonData",
                "options": {
                    "baseURL": "https://api.lemondata.cc/v1",
                    "apiKey": lemondata_key if lemondata_key else "{env:LEMONDATA_API_KEY}",
                },
                "models": {
                    "gpt-5.4": {
                        "name": "GPT-5.4",
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "limit": {"context": 128000, "output": 16384},
                    },
                },
            },
        },
    }
    # 只写到 runtime/config/opencode/config.json（通过 XDG_CONFIG_HOME 生效）
    # 不再写 workdir/opencode.json，避免项目根目录产生额外变更触发 watcher
    oc_config_dir = os.path.join(_workspace_runtime_config_dir(workdir), "opencode")
    os.makedirs(oc_config_dir, exist_ok=True)
    config_path = os.path.join(oc_config_dir, "config.json")
    new_content = json.dumps(config, ensure_ascii=False, indent=2)
    # 内容没变则跳过写入，避免 mtime 更新触发 opencode file watcher 重启
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as _f:
            if _f.read() == new_content:
                return
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def _sync_company_skills_to_workdir(workdir: str) -> None:
    """把 superpower 全家桶 skill 写到 workdir/.opencode/skills/*.md。

    只同步开发相关的 superpower skills，不载入全部公司级 skill，
    避免 opencode 启动时加载过多无关上下文。
    """
    from app.database import SessionLocal
    from app.models.skill import Skill, SkillStatus, SkillVersion

    # superpower 全家桶 skill 名称白名单
    _SUPERPOWER_NAMES = {
        "dispatching-parallel-agents",
        "executing-plans",
        "finishing-a-development-branch",
        "receiving-code-review",
        "requesting-code-review",
        "subagent-driven-development",
        "test-driven-development",
        "using-git-worktrees",
        "using-superpowers",
        "verification-before-completion",
        "writing-plans",
        "writing-skills",
    }

    # 只写到 runtime/config/opencode/skills/（通过 XDG_CONFIG_HOME 生效）
    # 不再写 project/.opencode/skills，避免触发 OpenCode 文件 watcher
    skills_dir = os.path.join(_workspace_runtime_config_dir(workdir), "opencode", "skills")
    os.makedirs(skills_dir, exist_ok=True)

    db = SessionLocal()
    try:
        skills = (
            db.query(Skill)
            .filter(
                Skill.status == SkillStatus.PUBLISHED,
                Skill.scope == "company",
                Skill.name.in_(_SUPERPOWER_NAMES),
            )
            .all()
        )
        written = set()
        for skill in skills:
            ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            if not ver or not ver.system_prompt:
                continue
            # 文件名用 skill.name，替换不安全字符
            import re as _re
            safe = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', skill.name).strip('_')
            filename = f"{safe}.md"
            filepath = os.path.join(skills_dir, filename)
            content = f"---\nname: {skill.name}\ndescription: {skill.description or skill.name}\n---\n\n{ver.system_prompt}"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            written.add(filename)

        # 清理已删除或不再是 published/company 的旧文件
        for existing in os.listdir(skills_dir):
            if existing.endswith(".md") and existing not in written:
                os.remove(os.path.join(skills_dir, existing))
    except Exception:
        pass  # skill 同步失败不影响实例启动
    finally:
        db.close()


def _find_opencode() -> Optional[str]:
    path = shutil.which("opencode")
    if path:
        return path
    candidates = [
        "/opt/homebrew/bin/opencode",
        "/usr/local/bin/opencode",
        os.path.expanduser("~/.npm-global/bin/opencode"),
    ]
    for pattern in [os.path.expanduser("~/.nvm/versions/node/*/bin/opencode")]:
        for m in sorted(_glob.glob(pattern)):
            candidates.append(m)
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


async def _wait_ready(port: int, retries: int = 60) -> bool:
    url = f"http://127.0.0.1:{port}"
    for _ in range(retries):
        await asyncio.sleep(0.5)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=1)) as r:
                    if r.status < 500:
                        return True
        except Exception:
            pass
    return False


async def _ensure_user_instance(user_id: int, display_name: str = "") -> dict:
    """确保该用户的 opencode web 实例在跑，返回 {port, url}。每用户独立进程+workdir+端口。"""
    global _instances_lock
    # 延迟初始化全局锁
    if _instances_lock is None:
        _instances_lock = asyncio.Lock()

    # 确保该用户有独立的实例槽和锁
    async with _instances_lock:
        if user_id not in _user_instances:
            _user_instances[user_id] = {
                "proc": None,
                "port": _port_for_user(user_id),
                "workdir": None,
                "lock": asyncio.Lock(),
                "last_active": _time.time(),
            }

    inst = _user_instances[user_id]
    async with inst["lock"]:
        proc: Optional[asyncio.subprocess.Process] = inst["proc"]

        opencode_bin = _find_opencode()
        if not opencode_bin:
            raise HTTPException(503, "opencode 未安装，请先运行: npm install -g opencode-ai")

        # 后端重启后 _user_instances 内存清空，但 opencode 进程可能仍在跑。
        # 若 proc 为 None 但固定端口已有进程在监听，检查 cwd 是否正确再决定复用或重启。
        if proc is None and _port_open(inst["port"]):
            _expected_cwd = _workspace_project_dir(_workspace_root_for_user(user_id, display_name))
            # 通过端口找到占用该端口的进程，检查其 cwd
            _cwd_ok = False
            try:
                import subprocess as _sp
                _lsof = _sp.run(["lsof", "-ti", f":{inst['port']}"], capture_output=True, text=True, timeout=5)
                for _pid_str in _lsof.stdout.strip().split('\n'):
                    if _pid_str.strip().isdigit():
                        _actual_cwd = os.readlink(f"/proc/{_pid_str.strip()}/cwd")
                        if _actual_cwd == _expected_cwd or os.path.realpath(_actual_cwd) == os.path.realpath(_expected_cwd):
                            _cwd_ok = True
                            break
            except Exception:
                _cwd_ok = True  # 检查失败时保守复用，不影响已有逻辑
            if _cwd_ok:
                inst["last_active"] = _time.time()
                return {"port": inst["port"], "url": "/opencode"}
            # cwd 不对，杀掉旧进程，下方代码会重新启动
            try:
                for _pid_str in _lsof.stdout.strip().split('\n'):
                    if _pid_str.strip().isdigit():
                        os.kill(int(_pid_str.strip()), 9)
            except Exception:
                pass
            import asyncio as _aio
            await _aio.sleep(1)  # 等端口释放

        # 每用户独立持久化 workdir，用姓名命名（重启后保留文件和 session 历史）
        workdir = _workspace_root_for_user(user_id, display_name)

        # 统一初始化入口：迁移旧布局 + 创建 project/runtime 目录结构
        loop = asyncio.get_event_loop()
        project_dir, runtime_dir = await loop.run_in_executor(
            None, ensure_workspace_layout, workdir, display_name
        )

        # 启动前检查 workspace 大小，超过上限先清理再继续
        await loop.run_in_executor(
            None, _cleanup_workspace_if_needed, workdir, WORKSPACE_MAX_GB * 1024 ** 3
        )

        # .gitignore 只在内容变更时写入，避免 mtime 变化触发状态刷新
        _gitignore_path = os.path.join(project_dir, ".gitignore")
        _gitignore_content = (
            "# opencode 运行时数据 — 禁止被 git diff/snapshot 统计进代码变更\n"
            ".git/\n"
            "*.pack\n"
            "*.idx\n"
            "# 依赖/缓存/构建目录\n"
            "node_modules/\n"
            ".bun/\n"
            ".npm/\n"
            ".pnpm-store/\n"
            ".venv/\n"
            "venv/\n"
            "__pycache__/\n"
            "*.pyc\n"
            ".next/\n"
            "dist/\n"
            "build/\n"
            ".cache/\n"
            "*.log\n"
            "*.tmp\n"
        )
        _need_write_gitignore = True
        if os.path.exists(_gitignore_path):
            with open(_gitignore_path, encoding="utf-8") as _f:
                if _f.read() == _gitignore_content:
                    _need_write_gitignore = False
        if _need_write_gitignore:
            with open(_gitignore_path, "w", encoding="utf-8") as _f:
                _f.write(_gitignore_content)

        from app.config import settings as _settings
        bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
        ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
        use_ark_fallback = _runtime_fallback["use_ark"] or getattr(_settings, "BAILIAN_FALLBACK_TO_ARK", False)

        # 仅向已授权 lemondata/gpt-5.4 的用户暴露 LEMONDATA_API_KEY
        _lemondata_raw = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")
        lemondata_key = ""
        if _lemondata_raw:
            from app.database import SessionLocal as _SL
            from app.models.opencode import UserModelGrant as _UMG
            _db = _SL()
            try:
                _grant = (
                    _db.query(_UMG)
                    .filter(
                        _UMG.user_id == user_id,
                        _UMG.model_key.like("%gpt%") | _UMG.model_key.like("lemondata/%"),
                    )
                    .first()
                )
                if _grant:
                    lemondata_key = _lemondata_raw
            finally:
                _db.close()

        # 读取旧配置内容，用于检测是否需要重启
        config_path = os.path.join(_workspace_runtime_config_dir(workdir), "opencode", "config.json")
        old_config = ""
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as _f:
                old_config = _f.read()

        # 内容无变化时 _write_opencode_config 会跳过写文件（不更新 mtime）
        _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=use_ark_fallback, lemondata_key=lemondata_key)

        # 将公司级 published skill 写入 runtime/config/opencode/skills/，供 opencode 按需加载
        await loop.run_in_executor(None, _sync_company_skills_to_workdir, workdir)

        # 已有进程且还活着：若配置无变化直接复用，否则重启使新配置生效
        new_config = ""
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as _f:
                new_config = _f.read()
        if proc is not None and proc.returncode is None:
            if old_config == new_config:
                inst["last_active"] = _time.time()
                return {"port": inst["port"], "url": "/opencode"}
            # 配置有变化，终止旧进程，下方代码负责重启
            proc.terminate()
            inst["proc"] = None

        # 活跃进程数上限检查（已有进程的用户不受限，仅限新启动）
        active_count = sum(
            1 for uid, i in _user_instances.items()
            if uid != user_id and i.get("proc") is not None and i["proc"].returncode is None
        )
        if active_count >= MAX_ACTIVE_INSTANCES:
            raise HTTPException(503, f"当前并发实例已达上限（{MAX_ACTIVE_INSTANCES}），请稍后再试")

        # XDG 目录指向 runtime/，物理隔离于 project/ — OpenCode 运行时数据不在 cwd 下
        user_data_dir = _workspace_runtime_data_dir(workdir)
        user_config_dir = _workspace_runtime_config_dir(workdir)

        proc_env = os.environ.copy()
        proc_env["XDG_DATA_HOME"] = user_data_dir
        proc_env["XDG_CONFIG_HOME"] = user_config_dir
        if bailian_key:
            proc_env["BAILIAN_API_KEY"] = bailian_key
        if ark_key:
            proc_env["ARK_API_KEY"] = ark_key
        if lemondata_key:
            proc_env["LEMONDATA_API_KEY"] = lemondata_key
        else:
            # 无授权时显式清除，防止从系统 env 继承后绕过授权
            proc_env.pop("LEMONDATA_API_KEY", None)
        # 禁止 opencode web 自动在服务器本机打开浏览器标签。
        # 假 `open` 脚本放在 runtime/bin/ 而非 project 内
        fake_open_dir = os.path.join(runtime_dir, "bin")
        fake_open_path = os.path.join(fake_open_dir, "open")
        if not os.path.exists(fake_open_path):
            with open(fake_open_path, "w") as _f:
                _f.write("#!/bin/sh\n# stub: suppress opencode auto-open browser\nexit 0\n")
            os.chmod(fake_open_path, 0o755)
        proc_env["PATH"] = fake_open_dir + ":" + proc_env.get("PATH", "")
        # 限制每个 opencode 进程的 Node.js 堆内存，防止单进程无限膨胀
        proc_env["NODE_OPTIONS"] = "--max-old-space-size=384"

        frontend_origins = [
            o.strip()
            for o in os.environ.get("FRONTEND_ORIGIN", "http://localhost:5023").split(",")
            if o.strip()
        ]
        cors_args = []
        for origin in frontend_origins:
            cors_args += ["--cors", origin]

        port = inst["port"]
        new_proc = await asyncio.create_subprocess_exec(
            opencode_bin, "web",
            "--port", str(port),
            "--hostname", "127.0.0.1",
            *cors_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=project_dir,  # cwd 指向 project/，OpenCode 只看到用户项目文件
            env=proc_env,
        )

        ready = await _wait_ready(port)
        if not ready:
            if new_proc.returncode is None:
                new_proc.terminate()
            raise HTTPException(503, "opencode web 启动超时，请重试")

        inst["proc"] = new_proc
        inst["workdir"] = workdir
        inst["last_active"] = _time.time()

        # 启动百炼用量监控（全局只跑一个）
        if _usage_counter["monitor_task"] is None or _usage_counter["monitor_task"].done():
            _usage_counter["monitor_task"] = asyncio.create_task(_bailian_usage_monitor())

        # 启动空闲进程回收任务（全局只跑一个）
        if _idle_reaper_task is None or _idle_reaper_task.done():
            _start_idle_reaper()

        # 启动 db 大小清理任务（全局只跑一个）
        global _db_cleaner_task
        if _db_cleaner_task is None or _db_cleaner_task.done():
            _db_cleaner_task = asyncio.create_task(_db_cleaner())

        return {"port": port, "url": "/opencode"}


# ─── 运行时 fallback 开关（不重启进程，只刷新 opencode.json + 可选重启进程）──

@router.post("/provider-fallback")
async def set_provider_fallback(
    enable: bool,
    reset_counter: bool = False,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """手动触发/关闭百炼→ARK fallback，刷新 opencode.json 并重启 opencode 进程。
    reset_counter=true 可重置估算计数器（月初清零时用）。
    """
    _runtime_fallback["use_ark"] = enable
    if reset_counter:
        _usage_counter["trigger_reason"] = ""

    from app.config import settings as _settings
    from app.database import SessionLocal as _SL
    from app.models.opencode import UserModelGrant as _UMG
    bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
    ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
    _lemondata_raw = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")

    # 全量刷新：扫描 STUDIO_WORKSPACE_ROOT 下所有用户目录，更新磁盘配置
    from app.config import settings as _cfg_fb
    _studio_root = os.path.abspath(os.path.expanduser(
        getattr(_cfg_fb, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
    ))
    _updated_wdirs = set()
    if os.path.isdir(_studio_root):
        for _entry in os.scandir(_studio_root):
            if not _entry.is_dir():
                continue
            wdir = _entry.path
            # 确保 runtime 布局存在（幂等）
            ensure_workspace_layout(wdir)
            _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=enable)
            _updated_wdirs.add(wdir)

    # 对活跃实例：按用户授权补写 lemondata key + 终止进程使新配置生效
    for uid, inst in list(_user_instances.items()):
        wdir = inst.get("workdir")
        if not wdir:
            continue
        # 按用户授权决定是否传入 lemondata key
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
            # 有 lemondata 授权的用户需要重写含 key 的配置
            _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=enable, lemondata_key=_uid_lemondata_key)
        proc = inst.get("proc")
        if proc is not None and proc.returncode is None:
            proc.terminate()
            inst["proc"] = None

    return {
        "fallback_enabled": enable,
        "default_model": _resolve_default_model(enable),
        "message": "opencode.json 已更新，进程将在下次请求时重启",
    }


@router.get("/provider-status")
async def get_provider_status(
    user: User = Depends(get_current_user),
):
    """查询百炼三窗口实际调用次数及当前 provider 配置。"""
    from app.config import settings as _settings
    q5h  = getattr(_settings, "BAILIAN_QUOTA_5H",  6000)
    q7d  = getattr(_settings, "BAILIAN_QUOTA_7D",  45000)
    q30d = getattr(_settings, "BAILIAN_QUOTA_30D", 90000)

    now_ms = int(_time.time() * 1000)
    s5h  = _count_ai_calls(now_ms - _WINDOW_5H  * 1000)
    s7d  = _count_ai_calls(now_ms - _WINDOW_7D  * 1000)
    s30d = _count_ai_calls(now_ms - _WINDOW_30D * 1000)

    return {
        "fallback_active": _runtime_fallback["use_ark"],
        "trigger_reason": _usage_counter["trigger_reason"],
        "active_provider": "ark" if _runtime_fallback["use_ark"] else "bailian-coding-plan",
        "default_model": _resolve_default_model(_runtime_fallback["use_ark"]),
        "windows": {
            "5h":  {"calls": s5h,  "quota": q5h,  "pct": round(s5h  / q5h  * 100, 1)},
            "7d":  {"calls": s7d,  "quota": q7d,  "pct": round(s7d  / q7d  * 100, 1)},
            "30d": {"calls": s30d, "quota": q30d, "pct": round(s30d / q30d * 100, 1)},
        },
    }


# ─── GET /instance — 启动/获取当前用户的独立实例 ──────────────────────────────

@router.get("/instance")
async def get_instance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    info = await _ensure_user_instance(user.id, display_name=user.display_name or "")

    from app.models.opencode import OpenCodeWorkspaceMapping
    mapping = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.user_id == user.id,
        OpenCodeWorkspaceMapping.directory != None,
    ).first()
    # directory 保存 workspace_root（user_<id>），不是 project_dir
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
        # 旧数据可能存的是 display_name 路径，纠正为 user_<id>
        mapping.directory = workspace_root
        db.commit()

    return {"url": info["url"], "port": info["port"], "status": "ready"}


# ─── POST /restart — 强制重启当前用户的 opencode 实例 ─────────────────────────

@router.post("/restart")
async def restart_instance(user: User = Depends(get_current_user)):
    """强制杀掉当前用户的 opencode 进程，下次 /instance 请求时重新启动。"""
    inst = _user_instances.get(user.id)
    if inst:
        proc = inst.get("proc")
        if proc is not None and proc.returncode is None:
            proc.terminate()
            inst["proc"] = None
    info = await _ensure_user_instance(user.id, display_name=user.display_name or "")
    return {"status": "restarted", "port": info["port"]}


# ─── GET /user-port — Next.js 代理用：查询当前用户的 opencode 端口 ────────────

@router.get("/user-port")
async def get_user_port(user: User = Depends(get_current_user)):
    """返回当前用户对应的 opencode 端口，供 Next.js 代理层做请求路由。"""
    return {"port": _port_for_user(user.id), "user_id": user.id}


# ─── POST /sessions（兼容旧接口，改为按用户隔离）─────────────────────────────

@router.post("/sessions")
async def create_session(user: User = Depends(get_current_user)):
    info = await _ensure_user_instance(user.id, display_name=user.display_name or "")
    return {
        "session_id": f"user_{user.id}",
        "url": info["url"],
        "port": info["port"],
    }


# ─── GET /latest-output — 读取最近 session 的产出文件 ─────────────────────────

@router.get("/latest-output")
def get_latest_output(
    limit: int = 10,
    user: User = Depends(get_current_user),
):
    """读取 opencode 最近 session 写入的文件列表及内容，供前端预填保存表单。
    返回: [{path, content, tool, session_title}]
    - write tool: content 是完整文件内容
    - edit/patch tool: content 从磁盘读取当前文件内容
    """
    import sqlite3 as _sqlite3
    import json as _json

    # 优先读当前用户自己的 DB，不串用户、不读全局 DB
    workdir = _workspace_root_for_user(user.id, user.display_name or "")
    db_path = _user_opencode_db_path(workdir)
    if not os.path.exists(db_path):
        # 最后兜底：全局 DB（仅用于没有用户 workspace 的异常场景）
        db_path = os.environ.get(
            "OPENCODE_DB_PATH",
            os.path.expanduser("~/.local/share/opencode/opencode.db"),
        )
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
        file_path = inp.get("filePath") or inp.get("file_path") or ""
        if not file_path or file_path in seen_paths:
            continue
        seen_paths.add(file_path)

        # 路径安全：只返回 project_dir 内的文件
        project_dir = _workspace_project_dir(workdir)
        exists_on_disk = os.path.isfile(file_path)

        content = ""
        if tool == "write":
            content = inp.get("content") or ""
        elif exists_on_disk:
            # edit/patch: 读磁盘当前版本，限制在 project_dir 内
            norm_path = os.path.normpath(file_path)
            if norm_path == project_dir or norm_path.startswith(project_dir + os.sep):
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as f:
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

    return result


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
    """从 Skill Studio 发起工具开发任务，在用户 workdir 写入 TOOL_REQUEST.md。"""
    workdir = _user_workdir(user)

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
    inbox_dir = os.path.join(workdir, "inbox")
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


@router.post("/save-tool")
def save_tool(
    req: SaveToolRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
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
    db.commit()
    db.refresh(tool)
    return {"id": tool.id, "name": tool.name, "display_name": tool.display_name}


# ─── Save as Skill ────────────────────────────────────────────────────────────

class SaveSkillRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    change_note: str = "由工具开发工作台生成"


@router.post("/save-skill")
def save_skill(
    req: SaveSkillRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 统一创建为草稿，由用户在 Skills & Tools 页面沙盒测试通过后手动提交发布
    skill = Skill(
        name=req.name,
        description=req.description,
        scope="personal",
        mode="hybrid",
        created_by=user.id,
        status=SkillStatus.DRAFT,
        auto_inject=True,
        source_type="local",
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=req.system_prompt,
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


# ─── Save to Existing Skill ──────────────────────────────────────────────────

class SaveToSkillRequest(BaseModel):
    skill_id: int
    action: str  # "new_version" | "bind_tool"
    # new_version fields
    system_prompt: str = ""
    change_note: str = "由工作台追加"
    # bind_tool fields
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
    """将工作台产出追加到已有 Skill（新版本或绑定 Tool）。"""
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
    format: str = "csv"   # "csv" | "json" | "sql"
    filename: Optional[str] = None   # 留空则自动生成


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

    # 1. 校验格式
    fmt = req.format.lower()
    if fmt not in ("csv", "json", "sql"):
        raise HTTPException(400, "format 只支持 csv / json / sql")

    # 2. 确认表已注册
    bt = db.query(BusinessTable).filter(BusinessTable.table_name == req.table_name).first()
    if not bt:
        raise HTTPException(404, f"业务表 '{req.table_name}' 未注册")

    # 3. 拉取全量数据（不分页）
    rows_result = db.execute(_text(f"SELECT * FROM `{req.table_name}`"))
    columns = list(rows_result.keys())
    raw_rows = [dict(zip(columns, row)) for row in rows_result.fetchall()]

    # 序列化
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

    # 4. 生成文件内容
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
    else:  # sql
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
            lines.append(f"INSERT INTO `{table_name}` ({col_str}) VALUES ({val_str});")
        content = "\n".join(lines)
        ext = "sql"

    # 5. 确定用户 workdir（指向 project/ 子目录）
    workdir = _user_workdir(user)

    # 6. 系统生成文件统一落到 inbox/
    inbox_dir = os.path.join(workdir, "inbox")
    os.makedirs(inbox_dir, exist_ok=True)

    if req.filename:
        safe_filename = os.path.basename(req.filename)
    else:
        safe_filename = f"{table_name}.{ext}"

    dest = os.path.join(inbox_dir, safe_filename)
    with open(dest, "w", encoding="utf-8", newline="" if fmt == "csv" else "\n") as f:
        f.write(content)

    return {
        "ok": True,
        "filename": safe_filename,
        "rows": len(rows),
        "format": fmt,
        "workdir": workdir,
    }


# ─── Upload File to Workdir ───────────────────────────────────────────────────

_MAX_UPLOAD_FILE_BYTES = 200 * 1024 * 1024  # 单文件上限 200MB


@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    target_path: str = Form(default=""),
    user: User = Depends(get_current_user),
):
    """将用户上传的文件写入其 opencode workdir，支持指定子目录。"""
    # 确定用户 workdir（指向 project/ 子目录）
    workdir = _user_workdir(user)

    max_bytes = WORKSPACE_MAX_GB * 1024 ** 3

    # 若 Content-Length 已知，提前拒绝超大文件（避免读入内存再报错）
    declared_size = file.size  # FastAPI 从 Content-Length 头解析，可能为 None
    if declared_size is not None and declared_size > _MAX_UPLOAD_FILE_BYTES:
        raise HTTPException(400, f"单文件不能超过 {_MAX_UPLOAD_FILE_BYTES // 1024 // 1024}MB")

    # 检查上传后是否会超出用户 workspace 配额
    ws_bytes = _dir_size_bytes(workdir)
    if declared_size is not None and ws_bytes + declared_size > max_bytes:
        raise HTTPException(
            400,
            f"Workspace 已使用 {ws_bytes / 1024**3:.2f}GB，"
            f"上传此文件将超过 {WORKSPACE_MAX_GB}GB 上限"
        )

    content = await file.read()

    # 读取后二次校验（防止 declared_size 为 None 或客户端伪造）
    if len(content) > _MAX_UPLOAD_FILE_BYTES:
        raise HTTPException(400, f"单文件不能超过 {_MAX_UPLOAD_FILE_BYTES // 1024 // 1024}MB")
    if ws_bytes + len(content) > max_bytes:
        raise HTTPException(
            400,
            f"Workspace 已使用 {ws_bytes / 1024**3:.2f}GB，"
            f"上传此文件将超过 {WORKSPACE_MAX_GB}GB 上限"
        )

    # 安全处理文件名：只取 basename，去掉路径分隔符
    safe_filename = os.path.basename(file.filename or "upload")
    if not safe_filename:
        safe_filename = "upload"

    # 处理目标子目录：防路径穿越，确保在 workdir 内
    if target_path and target_path.strip():
        dest_dir = _safe_path(workdir, target_path.strip())
        os.makedirs(dest_dir, exist_ok=True)
    else:
        # 默认落到 work/ 目录
        dest_dir = os.path.join(workdir, "work")
        os.makedirs(dest_dir, exist_ok=True)

    dest = os.path.join(dest_dir, safe_filename)
    with open(dest, "wb") as f:
        f.write(content)

    # 返回相对于 workdir 的路径，便于前端显示
    rel_dest = os.path.relpath(dest, workdir)
    return {
        "ok": True,
        "filename": safe_filename,
        "path": rel_dest,
        "size": len(content),
    }


# ─── POST /analyze-project — 分析项目目录，生成可发布的 Web App HTML ──────────

def _read_and_patch_html(project_path: str, skip_dirs: set, assigned_port: int, original_port) -> tuple[str, str]:
    """找 index.html 并替换 localhost 路径，返回 (html_content, entry_path)。"""
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
    # 替换原始端口
    if original_port:
        html = html.replace(f"http://localhost:{original_port}", f"/api/webapp-proxy/{assigned_port}")
        html = html.replace(f"http://127.0.0.1:{original_port}", f"/api/webapp-proxy/{assigned_port}")
    # 兜底替换所有剩余 localhost
    html = _re.sub(
        r'http://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(/[^"\'\s]*)',
        lambda m: f"/api/webapp-proxy/{assigned_port}" + m.group(1),
        html,
    )
    # 替换 API_BASE = '/api...' 模式（匹配 '/api'、'/api/proxy'、'/api/xxx' 等任意相对路径）
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
    """
    分析项目目录，按类型自动选择发布策略：
    - Next.js 全栈：生成 iframe 包装页，backend_cmd=npm start
    - Node/Python 后端：读 index.html 替换路径，backend_cmd 指向启动文件
    - 纯静态 HTML：读 index.html 替换路径，无 backend
    """
    import re as _re
    import json as _json
    from app.models.web_app import WebApp
    from app.routers.web_apps import _user_port
    import secrets as _secrets

    # 1. 安全校验
    project_path = os.path.abspath(os.path.expanduser(req.project_path))
    if not os.path.isdir(project_path):
        raise HTTPException(400, f"路径不存在或不是目录：{project_path}")

    SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".trae", ".bin", ".local", ".config", ".next", "dist", "build"}
    assigned_port = _user_port(user.id)
    app_name = req.name.strip() or os.path.basename(project_path) or "未命名应用"

    # 2. 递归扫描目录树，收集特征文件（不限根目录）
    # 记录：{ 'package.json': [路径,...], 'server.js': [...], ... }
    found: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            found.setdefault(fname, []).append(os.path.join(root, fname))

    # 3. 按优先级判断类型
    # 关键原则：同类特征文件有多个时，优先选层级最浅的（路径最短 = 最靠近项目根）
    def _shallowest(paths: list[str]) -> list[str]:
        """按路径深度升序排序，层级最浅的排最前。"""
        return sorted(paths, key=lambda p: p.count(os.sep))

    project_type = "static"
    pkg = {}
    backend_cwd: str | None = None
    backend_cmd: str | None = None
    original_port: int | None = None

    # Node/Next.js：遍历所有 package.json，优先最浅层
    for pkg_path in _shallowest(found.get("package.json", [])):
        try:
            p = _json.loads(open(pkg_path).read())
        except Exception:
            continue
        deps = {**p.get("dependencies", {}), **p.get("devDependencies", {})}
        pkg_dir = os.path.dirname(pkg_path)

        # Next.js
        if "next" in deps:
            project_type = "nextjs"
            pkg = p
            backend_cwd = pkg_dir
            break

        # 纯前端框架（React/Vue/Vite/Angular）— 需要 build，无 Node 后端进程
        SPA_MARKERS = {"react", "vue", "vite", "@angular/core", "svelte", "solid-js"}
        if SPA_MARKERS & set(deps.keys()) and not any(
            os.path.exists(os.path.join(pkg_dir, f)) for f in ["server.js", "app.js", "index.js"]
        ):
            project_type = "spa"
            pkg = p
            backend_cwd = pkg_dir
            break

        # Node 后端：package.json 同目录有启动文件
        for js_entry in ["server.js", "app.js", "index.js"]:
            candidate = os.path.join(pkg_dir, js_entry)
            if os.path.exists(candidate):
                project_type = "node"
                pkg = p
                backend_cwd = pkg_dir
                js_src = open(candidate, encoding="utf-8", errors="replace").read()
                # 检测是否已支持 PORT 环境变量
                has_env_port = bool(_re.search(r'process\.env\.PORT', js_src))
                m = _re.search(r'(?:const|let|var)\s+PORT\s*=\s*(\d{4,5})', js_src)
                if m:
                    original_port = int(m.group(1))
                    if not has_env_port:
                        # 自动修改源文件，加上环境变量读取
                        js_src_fixed = js_src.replace(
                            m.group(0),
                            m.group(0).replace(m.group(1), f"process.env.PORT || {m.group(1)}")
                        )
                        with open(candidate, "w", encoding="utf-8") as _f:
                            _f.write(js_src_fixed)
                break
        if project_type != "static":
            break

    # Python：遍历所有 requirements.txt，优先最浅层
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
                            # 自动修改源文件，加上环境变量读取
                            py_src_fixed = py_src.replace(
                                m.group(0),
                                f"{m.group(1)}int(os.environ.get('PORT', {m.group(2)}))"
                            )
                            # 确保 import os 存在
                            if "import os" not in py_src_fixed:
                                py_src_fixed = "import os\n" + py_src_fixed
                            with open(candidate, "w", encoding="utf-8") as _f:
                                _f.write(py_src_fixed)
                    break
            if project_type != "static":
                break

    # 4A. Next.js
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

    # 4B. SPA（React/Vue/Vite）：需要先 build，从 dist/build 目录找 HTML
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
        # SPA 无需后端进程

    # 4C. Node.js
    elif project_type == "node":
        for js_entry in ["server.js", "app.js", "index.js"]:
            if os.path.exists(os.path.join(backend_cwd, js_entry)):
                start_script = pkg.get("scripts", {}).get("start", "")
                backend_cmd = start_script if start_script else f"node {js_entry}"
                break
        html_content, _ = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, original_port)

    # 4D. Python
    elif project_type == "python":
        for py_entry in ["app.py", "main.py", "server.py", "run.py"]:
            if os.path.exists(os.path.join(backend_cwd, py_entry)):
                backend_cmd = f"python {py_entry}"
                break
        html_content, _ = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, original_port)
        # Python 全栈无前端 HTML：生成 iframe 代理页
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

    # 4E. 纯静态 HTML
    else:
        html_content, entry = _read_and_patch_html(project_path, SKIP_DIRS, assigned_port, None)
        if not html_content:
            raise HTTPException(400, "项目目录下没有找到 HTML 文件，请先用「完成开发，准备发布」Skill 生成 index.html")

    # 4. 创建 WebApp 记录（先插入拿到 id，再算端口、更新 html）
    from app.routers.web_apps import _app_port
    share_token = _secrets.token_urlsafe(16)
    web_app = WebApp(
        name=app_name,
        description=req.description.strip() or f"从 {project_path} 发布（{project_type}）",
        html_content="",  # 先占位
        created_by=user.id,
        is_public=True,
        share_token=share_token,
        backend_cmd=backend_cmd,
        backend_cwd=backend_cwd,
        backend_port=None,
    )
    db.add(web_app)
    db.flush()  # 拿到 auto-increment id，还未 commit

    # 用 app_id 确定端口，替换 html 中的端口占位
    final_port = _app_port(web_app.id) if backend_cmd else None
    if final_port and backend_cmd:
        # 替换 html 里之前用 assigned_port 占位的地址
        html_content = html_content.replace(
            f"/api/webapp-proxy/{assigned_port}",
            f"/api/webapp-proxy/{final_port}",
        )
        web_app.backend_port = final_port
        # Next.js 的 backend_cmd 里也有端口
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

def _user_opencode_cwd(user_id: int) -> Optional[str]:
    """尝试从运行中的 opencode 实例获取其实际 CWD（Linux /proc）。
    用于确保文件管理器和 opencode 看到同一个目录。

    路径 1：inst 里有 proc 对象 → 直接读 /proc/<pid>/cwd
    路径 2：后端重启后 proc 为 None，但端口上有旧进程 → lsof 找 PID 再读 cwd
    """
    inst = _user_instances.get(user_id)

    # 路径 1：有 proc 对象
    if inst:
        proc = inst.get("proc")
        if proc is not None and proc.returncode is None:
            try:
                cwd = os.readlink(f"/proc/{proc.pid}/cwd")
                if os.path.isdir(cwd):
                    return cwd
            except Exception:
                pass

    # 路径 2：通过端口找进程 PID
    port = _port_for_user(user_id)
    try:
        import subprocess as _sp
        result = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=3)
        for pid_str in result.stdout.strip().split('\n'):
            if pid_str.strip().isdigit():
                cwd = os.readlink(f"/proc/{pid_str.strip()}/cwd")
                if os.path.isdir(cwd):
                    return cwd
    except Exception:
        pass
    return None


def _user_workdir(user: User) -> str:
    """返回当前用户的 project 目录路径（用户可见文件）。
    优先使用 opencode 进程的实际 CWD，确保与 opencode 文件树一致。
    回退到 project/ 目录（并确保布局完整）。
    """
    cwd = _user_opencode_cwd(user.id)
    if cwd:
        return cwd
    workdir = _workspace_root_for_user(user.id, user.display_name or "")
    project_dir, _ = ensure_workspace_layout(workdir, display_name=user.display_name or "")
    return project_dir


def _safe_path(workdir: str, rel: str) -> str:
    """将相对路径解析为绝对路径，确保不超出 workdir（防路径穿越）。"""
    abs_path = os.path.normpath(os.path.join(workdir, rel.lstrip("/")))
    # 用 trailing separator 防止 /a/project 被 /a/project_evil 绕过
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
        # 跳过隐藏目录和已知大目录
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


@router.get("/workdir/tree")
def workdir_tree(user: User = Depends(get_current_user)):
    """返回用户 workdir 的完整文件树，始终包含四个一级业务目录。"""
    workdir = _user_workdir(user)
    tree = _tree(workdir)

    # 确保四个一级目录节点始终存在（即使为空）
    existing_names = {n["name"] for n in tree if n["type"] == "dir"}
    for d in _REQUIRED_TOP_DIRS:
        if d not in existing_names:
            tree.insert(0, {"name": d, "path": d, "type": "dir", "children": []})

    # 把四个必需目录排到前面
    required_set = set(_REQUIRED_TOP_DIRS)
    required_nodes = [n for n in tree if n["name"] in required_set and n["type"] == "dir"]
    other_nodes = [n for n in tree if not (n["name"] in required_set and n["type"] == "dir")]
    # 按 _REQUIRED_TOP_DIRS 顺序排列
    order = {name: i for i, name in enumerate(_REQUIRED_TOP_DIRS)}
    required_nodes.sort(key=lambda n: order.get(n["name"], 99))
    tree = required_nodes + other_nodes

    return {"workdir": workdir, "tree": tree}


class MkdirRequest(BaseModel):
    path: str   # 相对路径，如 "seed_data/v2"


@router.post("/workdir/mkdir")
def workdir_mkdir(req: MkdirRequest, user: User = Depends(get_current_user)):
    """在 workdir 内新建文件夹（含多级）。"""
    workdir = _user_workdir(user)
    target = _safe_path(workdir, req.path)
    os.makedirs(target, exist_ok=True)
    return {"ok": True, "path": req.path}


class RenameRequest(BaseModel):
    src: str   # 相对路径
    dst: str   # 相对路径


@router.post("/workdir/rename")
def workdir_rename(req: RenameRequest, user: User = Depends(get_current_user)):
    """重命名或移动文件/文件夹（src → dst，均为相对路径）。"""
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
    path: str   # 相对路径


@router.post("/workdir/delete")
def workdir_delete(req: DeleteRequest, user: User = Depends(get_current_user)):
    """删除 workdir 内的文件或文件夹。"""
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
    """读取 workdir 内的文本文件内容（用于前端展示 TOOL_REQUEST.md 等）。"""
    workdir = _user_workdir(user)
    target = _safe_path(workdir, path)
    if not os.path.exists(target) or os.path.isdir(target):
        raise HTTPException(404, "文件不存在")
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read(64 * 1024)  # 最多读 64KB
    except Exception:
        raise HTTPException(400, "无法读取文件")
    return {"content": content}


@router.get("/workdir/download")
def workdir_download(path: str, user: User = Depends(get_current_user)):
    """下载 workdir 内的单个文件到本地。"""
    from fastapi.responses import FileResponse
    workdir = _user_workdir(user)
    target = _safe_path(workdir, path)
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

@router.get("/data-views")
def list_data_views(
    q: str = "",
    source_type: str = "",
    table_id: Optional[int] = None,
    only_bindable: bool = True,
    include_direct_table: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户可见的数据视图列表（表 + 视图二级）。"""
    from app.models.business import (
        BusinessTable, TableView, TableField,
    )
    from app.services.policy_engine import resolve_user_role_groups, resolve_effective_policy
    from app.services.data_view_runtime import assess_view_availability

    # 查所有非归档表
    tq = db.query(BusinessTable).filter(BusinessTable.is_archived == False)  # noqa: E712
    if source_type:
        tq = tq.filter(BusinessTable.source_type == source_type)
    if table_id:
        tq = tq.filter(BusinessTable.id == table_id)
    tables = tq.all()

    items = []
    for bt in tables:
        # 查该表的视图
        vq = db.query(TableView).filter(
            TableView.table_id == bt.id,
        )
        if only_bindable:
            vq = vq.filter(TableView.view_purpose.in_(["skill_runtime", "explore", "ops"]))
        views = vq.all()

        if not views:
            # 无视图的裸表 — 仅 admin 且 include_direct_table 时返回
            if include_direct_table and user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
                items.append({
                    "table_id": bt.id,
                    "table_name": bt.table_name,
                    "display_name": bt.display_name or bt.table_name,
                    "source_type": bt.source_type,
                    "sync_status": bt.sync_status,
                    "view_id": None,
                    "view_name": None,
                    "view_purpose": None,
                    "view_kind": None,
                    "disclosure_ceiling": None,
                    "field_count": 0,
                    "record_count_cache": bt.record_count_cache,
                    "risk_flags": ["NO_VIEW"],
                })
            continue

        # 获取用户对该表的权限
        role_groups = resolve_user_role_groups(db, bt.id, user)
        group_ids = [g.id for g in role_groups]

        for v in views:
            policy = resolve_effective_policy(db, bt.id, group_ids, view_id=v.id)
            avail = assess_view_availability(v, policy, bt)

            # 权限完全拒绝且非 admin → 不返回
            if policy.denied and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
                continue

            # 文本搜索过滤
            if q:
                haystack = f"{bt.display_name} {bt.table_name} {v.name}".lower()
                if q.lower() not in haystack:
                    continue

            field_count = len(v.visible_field_ids or [])

            items.append({
                "table_id": bt.id,
                "table_name": bt.table_name,
                "display_name": bt.display_name or bt.table_name,
                "source_type": bt.source_type,
                "sync_status": bt.sync_status,
                "view_id": v.id,
                "view_name": v.name,
                "view_purpose": v.view_purpose,
                "view_kind": v.view_kind,
                "disclosure_ceiling": v.disclosure_ceiling,
                "field_count": field_count,
                "record_count_cache": bt.record_count_cache,
                "risk_flags": avail.risk_flags,
                "available": avail.available,
                "display_mode": avail.display_mode,
            })

    return {"ok": True, "items": items, "total": len(items)}


@router.get("/data-views/{view_id}")
def get_data_view_detail(
    view_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回单个数据视图的详情：字段列表 + 权限摘要 + 预览数据。"""
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

    bt = db.get(BusinessTable, view.table_id)
    if not bt:
        raise HTTPException(404, "关联数据表不存在")

    if bt.is_archived:
        raise HTTPException(400, f"数据表 '{bt.display_name}' 已归档")

    # 权限检查
    role_groups = resolve_user_role_groups(db, bt.id, user)
    group_ids = [g.id for g in role_groups]
    policy = resolve_effective_policy(db, bt.id, group_ids, view_id=view.id)

    if policy.denied and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权访问此视图: " + "; ".join(policy.deny_reasons))

    avail = assess_view_availability(view, policy, bt)
    caps = check_disclosure_capability(policy.disclosure_level)

    # 字段信息
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

    # 权限摘要
    permission_summary = {
        "disclosure_level": policy.disclosure_level,
        "row_access_mode": policy.row_access_mode,
        "tool_permission_mode": policy.tool_permission_mode,
        "denied": policy.denied,
        "deny_reasons": policy.deny_reasons,
        "capabilities": caps,
    }

    # 预览数据（前 20 行）
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
        },
        "fields": fields_info,
        "permission": permission_summary,
        "availability": {
            "available": avail.available,
            "risk_flags": avail.risk_flags,
            "display_mode": avail.display_mode,
        },
        "preview": preview,
    }
