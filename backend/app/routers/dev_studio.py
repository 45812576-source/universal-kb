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
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.skill import Skill, SkillStatus, SkillVersion
from app.models.tool import ToolRegistry, ToolType
from app.models.user import User, Role

router = APIRouter(prefix="/api/dev-studio", tags=["dev-studio"])

# ─── 按用户隔离的实例池 ────────────────────────────────────────────────────────
# 每个 user_id 对应独立的 opencode 进程 + workdir + 端口
# 结构：{user_id: {"proc": Process, "port": int, "workdir": str, "lock": Lock, "last_active": float}}
_user_instances: dict = {}
_instances_lock: object = None   # 全局 asyncio.Lock，保护 _user_instances 写入

IDLE_TIMEOUT_SECONDS = 1800  # 30分钟无操作自动回收
_REAPER_INTERVAL = 600       # 每10分钟检查一次
_idle_reaper_task = None
MAX_ACTIVE_INSTANCES = 20    # 最多同时运行 20 个 opencode 进程

# 每个用户 workspace 目录总大小上限（包含 .local 等隐藏目录，超出后删最老 session + VACUUM）
WORKSPACE_MAX_GB = 2
_db_cleaner_task = None


_DIR_SIZE_SKIP = {"node_modules", ".bun", ".cache", "__pycache__", ".venv", "venv", ".next", "dist", "build"}


def _dir_size_bytes(path: str) -> int:
    """递归计算目录下所有文件的总字节数（跳过包管理器缓存等大目录）。"""
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


def _cleanup_workspace_if_needed(workdir: str, max_bytes: int) -> None:
    """若 workdir 总大小超过 max_bytes，删最老 session 直到达标。同步函数，可在启动前直接调用。"""
    import sqlite3 as _sqlite3
    import logging
    logger = logging.getLogger(__name__)

    ws_bytes = _dir_size_bytes(workdir)
    if ws_bytes <= max_bytes:
        return
    db_path = os.path.join(workdir, ".local", "share", "opencode", "opencode.db")
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
    """每20分钟扫一遍所有用户 workspace，总大小超过 WORKSPACE_MAX_GB 则清理。"""
    import logging
    max_bytes = WORKSPACE_MAX_GB * 1024 ** 3

    while True:
        await asyncio.sleep(1200)
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


async def _idle_reaper() -> None:
    """后台任务：每10分钟扫一遍，超过1小时没活动的 opencode 进程自动杀掉。"""
    while True:
        await asyncio.sleep(_REAPER_INTERVAL)
        now = _time.time()
        for uid, inst in list(_user_instances.items()):
            proc = inst.get("proc")
            if proc is None or proc.returncode is not None:
                continue
            last = inst.get("last_active", now)
            if now - last > IDLE_TIMEOUT_SECONDS:
                try:
                    proc.terminate()
                except Exception:
                    pass
                inst["proc"] = None


def _start_idle_reaper():
    global _idle_reaper_task
    _idle_reaper_task = asyncio.create_task(_idle_reaper())

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
    # deque of (unix_timestamp, estimated_calls_delta)
    "events": _collections.deque(),
    "last_files_total": 0,
    "monitor_task": None,
    # 触发 fallback 的原因，便于查询
    "trigger_reason": "",
}

# 三窗口秒数
_WINDOW_5H  = 5  * 3600
_WINDOW_7D  = 7  * 86400
_WINDOW_30D = 30 * 86400


def _window_sum(seconds: int) -> int:
    """统计最近 seconds 内的估算调用总量。"""
    cutoff = _time.time() - seconds
    return sum(calls for ts, calls in _usage_counter["events"] if ts >= cutoff)


def _prune_events() -> None:
    """删除 30 天前的旧事件，防内存无限增长。"""
    cutoff = _time.time() - _WINDOW_30D
    while _usage_counter["events"] and _usage_counter["events"][0][0] < cutoff:
        _usage_counter["events"].popleft()


def _read_files_total() -> int:
    """从所有用户的 OpenCode SQLite 汇总 summary_files 总量。"""
    import sqlite3 as _sqlite3
    total = 0
    db_paths = []

    # 各用户独立目录
    for uid, inst in list(_user_instances.items()):
        from app.config import settings as _cfg3
        _studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg3, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
        wdir = inst.get("workdir") or os.path.join(_studio_root, f"user_{uid}")
        db_paths.append(os.path.join(wdir, ".local", "share", "opencode", "opencode.db"))

    # 兜底：全局路径（向后兼容）
    global_db = os.environ.get("OPENCODE_DB_PATH", os.path.expanduser("~/.local/share/opencode/opencode.db"))
    db_paths.append(global_db)

    for db_path in db_paths:
        if not os.path.exists(db_path):
            continue
        try:
            con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute("SELECT COALESCE(SUM(summary_files), 0) FROM session").fetchone()
            con.close()
            total += int(row[0]) if row else 0
        except Exception:
            pass
    return total


async def _bailian_usage_monitor() -> None:
    """每 5 分钟采样一次文件变更量，估算百炼调用次数，任一窗口超 90% 自动切换 ARK。"""
    import logging
    logger = logging.getLogger(__name__)

    while True:
        await asyncio.sleep(300)  # 5 分钟
        try:
            from app.config import settings as _settings
            q5h  = getattr(_settings, "BAILIAN_QUOTA_5H",  6000)
            q7d  = getattr(_settings, "BAILIAN_QUOTA_7D",  45000)
            q30d = getattr(_settings, "BAILIAN_QUOTA_30D", 90000)

            # 采样增量
            current_total = _read_files_total()
            delta = max(0, current_total - _usage_counter["last_files_total"])
            _usage_counter["last_files_total"] = current_total
            if delta > 0:
                _usage_counter["events"].append((_time.time(), delta * 2))
            _prune_events()

            # 三窗口求和
            s5h  = _window_sum(_WINDOW_5H)
            s7d  = _window_sum(_WINDOW_7D)
            s30d = _window_sum(_WINDOW_30D)

            logger.info(
                f"[BailianMonitor] delta={delta} "
                f"5h={s5h}/{q5h} 7d={s7d}/{q7d} 30d={s30d}/{q30d}"
            )

            # 任一窗口超 90% 触发 fallback
            reason = ""
            if s5h  >= q5h  * 0.9: reason = f"5h用量 {s5h}/{q5h}"
            elif s7d  >= q7d  * 0.9: reason = f"7d用量 {s7d}/{q7d}"
            elif s30d >= q30d * 0.9: reason = f"30d用量 {s30d}/{q30d}"

            if reason and not _runtime_fallback["use_ark"]:
                logger.warning(f"[BailianMonitor] {reason} 已达90%，自动切换 ARK")
                _runtime_fallback["use_ark"] = True
                _usage_counter["trigger_reason"] = reason

                bailian_key = os.environ.get("BAILIAN_API_KEY", "")
                ark_key = os.environ.get("ARK_API_KEY", "")
                # 更新所有已启动用户实例的 opencode.json，并重启进程
                for uid, inst in list(_user_instances.items()):
                    from app.config import settings as _cfg2
                    _studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg2, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
                    wdir = inst.get("workdir") or os.path.join(_studio_root, f"user_{uid}")
                    os.makedirs(wdir, exist_ok=True)
                    _write_opencode_config(wdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=True)
                    proc = inst.get("proc")
                    if proc is not None and proc.returncode is None:
                        proc.terminate()
                        inst["proc"] = None
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
    config_path = os.path.join(workdir, "opencode.json")
    new_content = json.dumps(config, ensure_ascii=False, indent=2)
    # 内容没变则跳过写入，避免 mtime 更新触发 opencode file watcher 重启
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as _f:
            if _f.read() == new_content:
                return
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    # opencode 优先读 XDG_CONFIG_HOME/opencode/config.json，同步写一份确保生效
    xdg_config_dir = os.path.join(workdir, ".config", "opencode")
    os.makedirs(xdg_config_dir, exist_ok=True)
    with open(os.path.join(xdg_config_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _sync_company_skills_to_workdir(workdir: str) -> None:
    """把公司级 published skill 的 system_prompt 写到 workdir/.opencode/skills/*.md。

    opencode 会自动发现该目录下的 .md 文件作为可加载的 skill。
    每次启动时全量刷新，确保 skill 内容与数据库同步。
    """
    from app.database import SessionLocal
    from app.models.skill import Skill, SkillStatus, SkillVersion

    # 两个路径都写：.opencode/skills/（项目级）和 .config/opencode/skills/（XDG_CONFIG_HOME级）
    skills_dir = os.path.join(workdir, ".opencode", "skills")
    skills_dir_xdg = os.path.join(workdir, ".config", "opencode", "skills")
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(skills_dir_xdg, exist_ok=True)

    db = SessionLocal()
    try:
        skills = (
            db.query(Skill)
            .filter(Skill.status == SkillStatus.PUBLISHED, Skill.scope == "company")
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
            for d in (skills_dir, skills_dir_xdg):
                with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
                    f.write(content)
            written.add(filename)

        # 清理已删除或不再是 published/company 的旧文件
        for d in (skills_dir, skills_dir_xdg):
            for existing in os.listdir(d):
                if existing.endswith(".md") and existing not in written:
                    os.remove(os.path.join(d, existing))
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


async def _wait_ready(port: int, retries: int = 20) -> bool:
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

        # 每用户独立持久化 workdir，用姓名命名（重启后保留文件和 session 历史）
        from app.config import settings as _cfg
        import re
        studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
        # 用 display_name 做目录名，去掉不安全字符，兜底用 user_{id}
        safe_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', display_name).strip('_') if display_name else ""
        folder_name = safe_name if safe_name else f"user_{user_id}"
        workdir = os.path.join(studio_root, folder_name)
        is_new = not os.path.exists(workdir)
        os.makedirs(workdir, exist_ok=True)

        # 首次创建：初始化项目目录结构
        if is_new:
            for subdir in ["src", "docs", "scripts", ".local/share", ".config"]:
                os.makedirs(os.path.join(workdir, subdir), exist_ok=True)
            readme = os.path.join(workdir, "README.md")
            with open(readme, "w", encoding="utf-8") as f:
                f.write(f"# {display_name or folder_name} 的工作台\n\n这是你的专属开发工作台，文件会持久保存。\n")

        # 启动前检查 workspace 大小，超过上限先清理再继续
        # 注意：_cleanup_workspace_if_needed 是同步 IO，用 executor 跑防止阻塞 event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _cleanup_workspace_if_needed, workdir, WORKSPACE_MAX_GB * 1024 ** 3
        )

        # 始终写入 .gitignore，避免 opencode snapshot 把子仓库的 packfile 吸进来导致磁盘爆炸
        # snapshot 仓库的 worktree 指向 workdir，会读这里的 .gitignore
        _gitignore_path = os.path.join(workdir, ".gitignore")
        _gitignore_content = (
            "# 依赖/缓存/构建目录 — 禁止 opencode snapshot 写入\n"
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
        config_path = os.path.join(workdir, "opencode.json")
        old_config = ""
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as _f:
                old_config = _f.read()

        # 内容无变化时 _write_opencode_config 会跳过写文件（不更新 mtime）
        _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=use_ark_fallback, lemondata_key=lemondata_key)

        # 将公司级 published skill 写入 .opencode/skills/，供 opencode 按需加载
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

        # 每用户独立数据目录（持久化，session db 隔离）
        user_data_dir = os.path.join(workdir, ".local", "share")
        os.makedirs(user_data_dir, exist_ok=True)
        user_config_dir = os.path.join(workdir, ".config")
        os.makedirs(user_config_dir, exist_ok=True)

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
        # opencode 在 macOS 上通过调用系统 `open` 命令打开浏览器，
        # 在 PATH 最前面注入一个假 `open` 脚本来拦截这个行为。
        fake_open_dir = os.path.join(workdir, ".bin")
        os.makedirs(fake_open_dir, exist_ok=True)
        fake_open_path = os.path.join(fake_open_dir, "open")
        if not os.path.exists(fake_open_path):
            with open(fake_open_path, "w") as _f:
                _f.write("#!/bin/sh\n# stub: suppress opencode auto-open browser\nexit 0\n")
            os.chmod(fake_open_path, 0o755)
        proc_env["PATH"] = fake_open_dir + ":" + proc_env.get("PATH", "")
        # 限制每个 opencode 进程的 Node.js 堆内存，防止单进程无限膨胀
        proc_env["NODE_OPTIONS"] = "--max-old-space-size=1024"

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
            cwd=workdir,
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
        _usage_counter["events"].clear()
        _usage_counter["last_files_total"] = _read_files_total()
        _usage_counter["trigger_reason"] = ""

    from app.config import settings as _settings
    from app.database import SessionLocal as _SL
    from app.models.opencode import UserModelGrant as _UMG
    bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
    ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
    _lemondata_raw = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")

    # 更新所有已启动用户实例的配置，并逐一重启
    for uid, inst in list(_user_instances.items()):
        wdir = inst.get("workdir") or os.path.join(tempfile.gettempdir(), f"ledesk_studio_user_{uid}")
        os.makedirs(wdir, exist_ok=True)
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
    """查询百炼三窗口估算用量及当前 provider 配置。"""
    from app.config import settings as _settings
    q5h  = getattr(_settings, "BAILIAN_QUOTA_5H",  6000)
    q7d  = getattr(_settings, "BAILIAN_QUOTA_7D",  45000)
    q30d = getattr(_settings, "BAILIAN_QUOTA_30D", 90000)

    _prune_events()
    s5h  = _window_sum(_WINDOW_5H)
    s7d  = _window_sum(_WINDOW_7D)
    s30d = _window_sum(_WINDOW_30D)

    return {
        "fallback_active": _runtime_fallback["use_ark"],
        "trigger_reason": _usage_counter["trigger_reason"],
        "active_provider": "ark" if _runtime_fallback["use_ark"] else "bailian-coding-plan",
        "default_model": _resolve_default_model(_runtime_fallback["use_ark"]),
        "windows": {
            "5h":  {"estimated": s5h,  "quota": q5h,  "pct": round(s5h  / q5h  * 100, 1)},
            "7d":  {"estimated": s7d,  "quota": q7d,  "pct": round(s7d  / q7d  * 100, 1)},
            "30d": {"estimated": s30d, "quota": q30d, "pct": round(s30d / q30d * 100, 1)},
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
    if mapping is None:
        user_workdir = os.path.join(tempfile.gettempdir(), f"ledesk_studio_user_{user.id}")
        mapping = OpenCodeWorkspaceMapping(
            user_id=user.id,
            directory=user_workdir,
            opencode_workspace_name=user.display_name,
        )
        db.add(mapping)
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

        content = ""
        if tool == "write":
            content = inp.get("content") or ""
        else:
            # edit/patch: 读磁盘当前版本
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
        })

    return result


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

    # 5. 确定用户 workdir
    import re as _re
    studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
    safe_name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', user.display_name).strip('_') if user.display_name else ""
    folder_name = safe_name if safe_name else f"user_{user.id}"
    workdir = os.path.join(studio_root, folder_name)
    os.makedirs(workdir, exist_ok=True)

    # 6. 写文件
    if req.filename:
        # 安全检查：不允许路径分隔符或 ..
        safe_filename = os.path.basename(req.filename)
    else:
        safe_filename = f"{table_name}.{ext}"

    dest = os.path.join(workdir, safe_filename)
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

@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """将用户上传的文件直接写入其 opencode workdir 根目录。"""
    import re as _re
    from app.config import settings as _cfg

    # 确定用户 workdir（与 _ensure_user_instance 保持一致）
    studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
    safe_name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', user.display_name).strip('_') if user.display_name else ""
    folder_name = safe_name if safe_name else f"user_{user.id}"
    workdir = os.path.join(studio_root, folder_name)
    os.makedirs(workdir, exist_ok=True)

    # 安全处理文件名：只取 basename，去掉路径分隔符
    safe_filename = os.path.basename(file.filename or "upload")
    if not safe_filename:
        safe_filename = "upload"

    dest = os.path.join(workdir, safe_filename)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    return {
        "ok": True,
        "filename": safe_filename,
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

def _user_workdir(user: User) -> str:
    """返回当前用户的 workdir 路径，不存在则创建。"""
    import re as _re
    from app.config import settings as _cfg
    studio_root = os.path.abspath(os.path.expanduser(getattr(_cfg, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
    safe_name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', user.display_name).strip('_') if user.display_name else ""
    folder_name = safe_name if safe_name else f"user_{user.id}"
    workdir = os.path.join(studio_root, folder_name)
    os.makedirs(workdir, exist_ok=True)
    return workdir


def _safe_path(workdir: str, rel: str) -> str:
    """将相对路径解析为绝对路径，确保不超出 workdir（防路径穿越）。"""
    abs_path = os.path.normpath(os.path.join(workdir, rel.lstrip("/")))
    if not abs_path.startswith(workdir):
        raise HTTPException(400, "路径不合法")
    return abs_path


_TREE_SKIP = {
    ".git", ".bin", ".bun", ".cache", ".config", ".local",
    "node_modules", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".trae",
}

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


@router.get("/workdir/tree")
def workdir_tree(user: User = Depends(get_current_user)):
    """返回用户 workdir 的完整文件树。"""
    workdir = _user_workdir(user)
    return {"workdir": workdir, "tree": _tree(workdir)}


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
