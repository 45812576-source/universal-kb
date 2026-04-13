"""RuntimeProcessManager — OpenCode 进程池生命周期管理。"""
import asyncio
import glob as _glob
import os
import shutil
import socket
import time as _time
from typing import Optional

import aiohttp

from app.services.workdir_manager import (
    _workspace_project_dir,
    _workspace_root_for_user,
)

# ─── 进程池状态（进程句柄缓存，非状态真相源）──────────────────────────────
# 仅缓存运行态进程句柄和异步锁，workdir/port/status 的真相源是 StudioRegistration 注册表。
# 结构：{user_id: {"proc": Process|None, "port": int, "workdir": str|None, "lock": Lock, "last_active": float}}
_user_instances: dict = {}
_instances_lock: object = None   # 全局 asyncio.Lock，保护 _user_instances 写入

IDLE_TIMEOUT_SECONDS = 900   # 15分钟无操作自动回收（降低内存压力）
_REAPER_INTERVAL = 300        # 每5分钟检查一次空闲实例
_idle_reaper_task = None
MAX_ACTIVE_INSTANCES = 12    # 最多同时运行 12 个 opencode 进程

MAX_RSS_MB = 512  # 单实例内存硬上限（opencode 正常工作约 350~420MB，留余量）
MAX_FD_COUNT = 500   # 单实例 fd 上限，pty 泄漏时 fd 会持续累积，超过则强制重启

# 重启抖动保护：记录每用户 1h 内重启次数，超过阈值冻结该实例
_MAX_RESTARTS_PER_HOUR = 3
_restart_history: dict[int, list[float]] = {}  # {user_id: [timestamp, ...]}

OPENCODE_BASE_PORT = 17171   # user_id=1 → 17172, user_id=2 → 17173, ...


def _port_for_user(user_id: int) -> int:
    """每个用户分配固定端口，重启后不变。端口 = BASE + user_id。"""
    return OPENCODE_BASE_PORT + user_id


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


def _record_restart(user_id: int) -> bool:
    """记录一次重启事件，返回是否已超过 1h 内重启阈值（应冻结）。"""
    now = _time.time()
    history = _restart_history.setdefault(user_id, [])
    history.append(now)
    # 清理 1h 前的记录
    cutoff = now - 3600
    _restart_history[user_id] = [t for t in history if t > cutoff]
    return len(_restart_history[user_id]) > _MAX_RESTARTS_PER_HOUR


def _mark_registry_stopped(user_id: int) -> None:
    """将注册表中的 runtime_status 标记为 stopped（fire-and-forget）。"""
    try:
        from app.database import SessionLocal as _RSL
        from app.services.studio_registry import update_runtime_status as _urt
        _rdb = _RSL()
        try:
            _urt(_rdb, user_id, "opencode", "stopped")
        finally:
            _rdb.close()
    except Exception:
        pass


def _mark_registry_unhealthy(user_id: int) -> None:
    """将注册表中的 runtime_status 标记为 unhealthy。"""
    try:
        from app.database import SessionLocal as _USL
        from app.services.studio_registry import update_runtime_status as _u_urt
        _udb = _USL()
        try:
            _u_urt(_udb, user_id, "opencode", "unhealthy")
        finally:
            _udb.close()
    except Exception:
        pass


def _get_registry_project_dir(user_id: int) -> Optional[str]:
    """从注册表读 project_dir，失败返回 None。"""
    try:
        from app.database import SessionLocal as _RSL
        from app.models.opencode import StudioRegistration as _SR
        _rdb = _RSL()
        try:
            _reg = (
                _rdb.query(_SR)
                .filter(_SR.user_id == user_id, _SR.workspace_type == "opencode")
                .first()
            )
            if _reg and _reg.project_dir and os.path.isdir(_reg.project_dir):
                return _reg.project_dir
        finally:
            _rdb.close()
    except Exception:
        pass
    return None


def _get_registry_workspace_root(user_id: int) -> Optional[str]:
    """从注册表读 workspace_root，失败返回 None。"""
    try:
        from app.database import SessionLocal as _RSL
        from app.models.opencode import StudioRegistration as _SR
        _rdb = _RSL()
        try:
            _reg = (
                _rdb.query(_SR)
                .filter(_SR.user_id == user_id, _SR.workspace_type == "opencode")
                .first()
            )
            if _reg and _reg.workspace_root:
                return _reg.workspace_root
        finally:
            _rdb.close()
    except Exception:
        pass
    return None


async def _idle_reaper() -> None:
    """后台任务：每2分钟扫一遍，回收空闲实例 + 杀掉内存超限实例 + 重启抖动保护。

    注意：此 reaper 只管理 _user_instances 中的 OpenCode 进程。
    Skill Studio 的 runtime_status 为 "n/a"，不参与进程管理，不受此 reaper 影响。
    """
    import logging
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(_REAPER_INTERVAL)
        now = _time.time()
        for uid, inst in list(_user_instances.items()):
            proc = inst.get("proc")
            if proc is None or proc.returncode is not None:
                continue
            # 空闲超时回收（不计入重启抖动）
            last = inst.get("last_active", now)
            if now - last > IDLE_TIMEOUT_SECONDS:
                logger.info(f"[Reaper] user={uid} 空闲超时，终止进程")
                try:
                    proc.terminate()
                except Exception:
                    pass
                inst["proc"] = None
                _mark_registry_stopped(uid)
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
                inst["last_restart_reason"] = f"RSS={rss}MB>{MAX_RSS_MB}MB"
                # 抖动保护：超过阈值则冻结为 unhealthy
                if _record_restart(uid):
                    logger.error(
                        f"[Reaper] user={uid} 1h 内重启超 {_MAX_RESTARTS_PER_HOUR} 次，冻结为 unhealthy"
                    )
                    _mark_registry_unhealthy(uid)
                else:
                    _mark_registry_stopped(uid)
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
                inst["last_restart_reason"] = f"fd={fd_count}>{MAX_FD_COUNT}"
                if _record_restart(uid):
                    logger.error(
                        f"[Reaper] user={uid} 1h 内重启超 {_MAX_RESTARTS_PER_HOUR} 次，冻结为 unhealthy"
                    )
                    _mark_registry_unhealthy(uid)
                else:
                    _mark_registry_stopped(uid)

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
                        _orphan_port = _port_for_user(_orphan_uid)
                        _user_instances[_orphan_uid] = {
                            "proc": None,
                            "port": _orphan_port,
                            "workdir": None,
                            "lock": asyncio.Lock(),
                            "last_active": _time.time(),
                        }
                        # 同步注册表：标记 running + port
                        try:
                            from app.database import SessionLocal as _OSL
                            from app.services.studio_registry import update_runtime_status as _o_urt
                            _odb = _OSL()
                            try:
                                _o_urt(_odb, _orphan_uid, "opencode", "running", port=_orphan_port)
                            finally:
                                _odb.close()
                        except Exception:
                            pass
                        logger.info(f"[Reaper] 认领 user={_orphan_uid} 的遗留进程 pid={pid}，纳入管理+注册表")
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
    """Startup: 杀掉未托管的遗留 opencode 进程树，避免 orphan 常驻撑爆内存。"""
    import logging as _log, signal as _sig, subprocess as _sp, re as _re
    logger = _log.getLogger(__name__)

    def _kill_pid_tree(_pid: int) -> None:
        try:
            _children = _sp.run(["pgrep", "-P", str(_pid)], capture_output=True, text=True, timeout=5)
            for _child in _children.stdout.strip().splitlines():
                if _child.strip().isdigit():
                    _kill_pid_tree(int(_child.strip()))
        except Exception:
            pass
        try:
            os.kill(_pid, _sig.SIGKILL)
        except Exception:
            pass

    try:
        managed_pids = set()
        managed_user_cwd_markers = set()

        def _collect_pid_tree(_pid: int) -> None:
            managed_pids.add(_pid)
            try:
                _children = _sp.run(["pgrep", "-P", str(_pid)], capture_output=True, text=True, timeout=5)
                for _child in _children.stdout.strip().splitlines():
                    if _child.strip().isdigit():
                        _collect_pid_tree(int(_child.strip()))
            except Exception:
                pass

        for _uid, _inst in _user_instances.items():
            _p = _inst.get("proc")
            if _p and _p.returncode is None:
                _collect_pid_tree(_p.pid)
            managed_user_cwd_markers.add(f"user_{_uid}/")

        result = _sp.run(["pgrep", "-f", ".opencode web"], capture_output=True, text=True, timeout=5)
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
            if any(marker in cwd for marker in managed_user_cwd_markers):
                logger.debug(f"[Startup] pid={pid} cwd={cwd} 属于已托管用户子进程，跳过")
                continue
            _uid = None
            _m = _re.search(r"user_(\d+)", cwd)
            if _m:
                _uid = int(_m.group(1))
            logger.warning(f"[Startup] orphan pid={pid} cwd={cwd}, killing whole tree")
            _kill_pid_tree(pid)
            if _uid is not None:
                try:
                    from app.database import SessionLocal as _SSL
                    from app.services.studio_registry import update_runtime_status as _s_urt
                    _sdb = _SSL()
                    try:
                        _s_urt(_sdb, _uid, "opencode", "stopped", port=None)
                    finally:
                        _sdb.close()
                except Exception:
                    pass
                if _uid in _user_instances:
                    _user_instances[_uid]["proc"] = None
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
        _mark_registry_stopped(uid)


def get_instance_info(user_id: int) -> Optional[dict]:
    """返回用户实例信息，不存在返回 None。"""
    return _user_instances.get(user_id)


def list_all_instances() -> list[dict]:
    """返回所有实例信息，供 admin 用。"""
    now = _time.time()
    result = []
    for uid, inst in _user_instances.items():
        proc = inst.get("proc")
        pid = proc.pid if proc and proc.returncode is None else None
        rss_mb = 0
        fd_count = 0
        if pid:
            rss_mb = _get_proc_tree_rss_mb(pid)
            try:
                fd_count = len(os.listdir(f"/proc/{pid}/fd"))
            except Exception:
                fd_count = 0

        # 1h 内重启次数
        cutoff = now - 3600
        restart_count = len([t for t in _restart_history.get(uid, []) if t > cutoff])

        # 状态判定
        if pid is None:
            status = "stopped"
        elif restart_count > _MAX_RESTARTS_PER_HOUR:
            status = "unhealthy"
        else:
            status = "running"

        result.append({
            "user_id": uid,
            "pid": pid,
            "port": inst.get("port"),
            "cwd": inst.get("workdir"),
            "rss_mb": rss_mb,
            "fd_count": fd_count,
            "restart_count_1h": restart_count,
            "last_restart_reason": inst.get("last_restart_reason", ""),
            "last_active": inst.get("last_active", 0),
            "status": status,
        })
    return result
