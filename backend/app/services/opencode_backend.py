"""OpenCodeBackend — OpenCode 配置写入、skill 同步、sanitize、用量监控、进程启动编排。

协调 WorkdirManager + RuntimeProcessManager + 自身的 config/sanitize 逻辑，
对外提供 prepare_and_start() 作为 _ensure_user_instance 的重构后宿主。
"""
import asyncio
import collections as _collections
import json
import logging
import os
import time as _time
from typing import Optional

from fastapi import HTTPException

from app.services.workdir_manager import (
    WORKSPACE_MAX_GB,
    _cleanup_workspace_if_needed,
    _studio_root,
    _user_opencode_db_path,
    _workspace_project_dir,
    _workspace_root_for_user,
    _workspace_runtime_config_dir,
    _workspace_runtime_data_dir,
    _workspace_skill_studio_dir,
    ensure_workspace_layout,
)
from app.services.runtime_process_manager import (
    MAX_ACTIVE_INSTANCES,
    _find_opencode,
    _get_registry_project_dir,
    _get_registry_workspace_root,
    _instances_lock,
    _mark_registry_stopped,
    _port_for_user,
    _port_open,
    _start_idle_reaper,
    _idle_reaper_task,
    _user_instances,
    _wait_ready,
)

logger = logging.getLogger(__name__)

# ─── 百炼 / ARK 常量 ────────────────────────────────────────────────────────

BAILIAN_DEFAULT_MODEL = "bailian-coding-plan/glm-5"
ARK_DEFAULT_MODEL = "ark/doubao-seed-2.0-code"

# 百炼中与 ARK 重复的模型 ID（百炼优先，触发 fallback 时改用 ARK）
_BAILIAN_ARK_OVERLAP = {"glm-4.7", "kimi-k2.5", "MiniMax-M2.5"}

# 内存级 fallback 状态（优先于 settings，可由 API 动态写入）
_runtime_fallback: dict = {"use_ark": False}

# ─── 百炼用量估算计数器（三窗口滑动）──────────────────────────────────────────
# 每次采样记录 (timestamp, calls) 事件，窗口内求和对比阈值

_usage_counter: dict = {
    "monitor_task": None,
    # 触发 fallback 的原因，便于查询
    "trigger_reason": "",
}

# 三窗口秒数
_WINDOW_5H  = 5  * 3600
_WINDOW_7D  = 7  * 86400
_WINDOW_30D = 30 * 86400


def _resolve_default_model(use_ark_fallback: bool) -> str:
    """根据是否 fallback 返回默认模型。"""
    if use_ark_fallback:
        return ARK_DEFAULT_MODEL
    return BAILIAN_DEFAULT_MODEL


# ─── sanitize ────────────────────────────────────────────────────────────────

def _sanitize_opencode_db(workdir: str, project_dir: str) -> None:
    """启动前修正 opencode.db 中 global project 的 worktree 指向。

    【重要】不修改已有 session 的 directory 和 project_id。
    历史 session 保留原始 directory 上下文，OpenCode UI 依赖这些值区分不同 session。
    只做以下最小修正：
    - 确保 global project 的 worktree 指向当前正确的 project_dir
    - 将 project_id 为空的 session 归入 global（防止 orphan session 不可见）
    """
    db_path = _user_opencode_db_path(workdir)
    if not db_path or not os.path.exists(db_path):
        return
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=5)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            changed = False
            if "project" in tables:
                # 确保 global project 存在且 worktree 正确
                row = con.execute("SELECT worktree FROM project WHERE id='global'").fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO project (id, worktree) VALUES ('global', ?)",
                        (project_dir,),
                    )
                    logger.info(f"[SanitizeDB] 创建 global project: {project_dir}")
                    changed = True
                elif row[0] != project_dir:
                    con.execute("UPDATE project SET worktree=? WHERE id='global'", (project_dir,))
                    logger.info(f"[SanitizeDB] 更新 global worktree: {row[0]} → {project_dir}")
                    changed = True
            if "session" in tables:
                # 只修复空 project_id，保留已有 project/session 上下文，避免多项目历史会话串线。
                cur = con.execute(
                    "UPDATE session SET project_id='global' WHERE project_id IS NULL OR project_id=''",
                )
                if cur.rowcount > 0:
                    logger.info(f"[SanitizeDB] 修复 {cur.rowcount} 条空 project_id session 到 global project")
                    changed = True
            if changed:
                con.commit()
                try:
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    logger.debug("[SanitizeDB] wal checkpoint skipped", exc_info=True)
            else:
                con.commit()
        finally:
            con.close()
    except Exception as e:
        logger.debug(f"[SanitizeDB] 修正失败（非致命）: {workdir}: {e}")


# ─── 配置写入 ────────────────────────────────────────────────────────────────

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


# ─── Skill 同步 ──────────────────────────────────────────────────────────────

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


# ─── 用量监控 ────────────────────────────────────────────────────────────────

def _all_opencode_db_paths() -> list[str]:
    """收集所有用户的 opencode.db 路径（含全局兜底路径）。"""
    from app.config import settings as _cfg3
    _sr = os.path.abspath(os.path.expanduser(getattr(_cfg3, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")))
    paths: list[str] = []
    seen = set()

    def _add(p: str):
        if p not in seen:
            seen.add(p)
            paths.append(p)

    # 从注册表读所有 opencode workspace_root
    try:
        from app.database import SessionLocal as _DbPSL
        from app.models.opencode import StudioRegistration as _DbPSR
        _dbp = _DbPSL()
        try:
            for _reg in _dbp.query(_DbPSR).filter(_DbPSR.workspace_type == "opencode").all():
                if _reg.workspace_root:
                    _add(_user_opencode_db_path(_reg.workspace_root))
        finally:
            _dbp.close()
    except Exception:
        pass
    # 扫描 studio_root 下所有用户目录（兜底，覆盖未注册的旧目录）
    if os.path.isdir(_sr):
        for name in os.listdir(_sr):
            wdir = os.path.join(_sr, name)
            if os.path.isdir(wdir):
                _add(_user_opencode_db_path(wdir))
    # 全局路径兜底
    global_db = os.environ.get("OPENCODE_DB_PATH", os.path.expanduser("~/.local/share/opencode/opencode.db"))
    _add(global_db)
    return paths


def _count_ai_calls(since_ms: int) -> int:
    """统计所有用户 opencode.db 中 since_ms（毫秒时间戳）之后的 LLM 调用次数。"""
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
                try:
                    from app.database import SessionLocal as _MonSL
                    from app.models.opencode import StudioRegistration as _MonSR
                    _mon_db = _MonSL()
                    try:
                        for _mreg in _mon_db.query(_MonSR).filter(
                            _MonSR.workspace_type == "opencode",
                            _MonSR.workspace_root != "",
                        ).all():
                            _write_opencode_config(
                                _mreg.workspace_root,
                                bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=True,
                            )
                    finally:
                        _mon_db.close()
                except Exception as _me:
                    logger.warning(f"[BailianMonitor] 写配置失败: {_me}")
        except Exception as e:
            logger.warning(f"[BailianMonitor] 采样失败: {e}")


# ─── db cleaner 后台任务 ─────────────────────────────────────────────────────

_db_cleaner_task = None


async def _db_cleaner() -> None:
    """每5分钟扫一遍所有用户 workspace，总大小超过 WORKSPACE_MAX_GB 则清理。"""
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
            logger.warning(f"[DbCleaner] 扫描失败: {e}")


# ─── 核心编排：prepare_and_start ─────────────────────────────────────────────

async def prepare_and_start(user_id: int, display_name: str = "") -> dict:
    """确保该用户的 opencode web 实例在跑，返回 {port, url}。

    组合 workdir + config + sanitize + process start。
    """
    import app.services.runtime_process_manager as rpm
    global _db_cleaner_task

    try:
        from app.database import SessionLocal as _EntrySL
        from app.models.user import User as _EntryUser
        from app.services.studio_registry import resolve_entry as _resolve_entry
        _entry_db = _EntrySL()
        try:
            _entry_user = _entry_db.get(_EntryUser, user_id)
            if _entry_user is not None:
                _resolve_entry(_entry_db, _entry_user, "opencode")
        finally:
            _entry_db.close()
    except Exception:
        logger.warning(
            f"[prepare_and_start] user={user_id} 注册表预热失败",
            exc_info=True,
        )

    # 延迟初始化全局锁
    if rpm._instances_lock is None:
        rpm._instances_lock = asyncio.Lock()

    # 确保该用户有独立的实例槽和锁
    async with rpm._instances_lock:
        if user_id not in rpm._user_instances:
            rpm._user_instances[user_id] = {
                "proc": None,
                "port": _port_for_user(user_id),
                "workdir": None,
                "lock": asyncio.Lock(),
                "last_active": _time.time(),
            }

    inst = rpm._user_instances[user_id]
    async with inst["lock"]:
        proc: Optional[asyncio.subprocess.Process] = inst["proc"]

        opencode_bin = _find_opencode()
        if not opencode_bin:
            raise HTTPException(503, "opencode 未安装，请先运行: npm install -g opencode-ai")

        # 后端重启后 _user_instances 内存清空，但 opencode 进程可能仍在跑。
        if proc is None and _port_open(inst["port"]):
            _expected_cwd = _get_registry_project_dir(user_id)
            if not _expected_cwd:
                _expected_cwd = _workspace_project_dir(_workspace_root_for_user(user_id, display_name))
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
                _cwd_ok = True
            if _cwd_ok:
                inst["last_active"] = _time.time()
                return {"port": inst["port"], "url": "/opencode"}
            logger.warning(
                f"[prepare_and_start] user={user_id} cwd 不匹配注册表，杀掉旧进程并重启"
            )
            _mark_registry_stopped(user_id)
            try:
                for _pid_str in _lsof.stdout.strip().split('\n'):
                    if _pid_str.strip().isdigit():
                        os.kill(int(_pid_str.strip()), 9)
            except Exception:
                pass
            await asyncio.sleep(1)

        # 优先从注册表读 workdir
        workdir = _get_registry_workspace_root(user_id)
        if not workdir:
            workdir = _workspace_root_for_user(user_id, display_name)

        _reg_project_dir = _get_registry_project_dir(user_id)
        if _reg_project_dir and not os.path.isdir(_reg_project_dir):
            logger.warning(
                f"[prepare_and_start] user={user_id} 注册表 project_dir 不存在于磁盘，将重建: {_reg_project_dir}"
            )

        loop = asyncio.get_event_loop()
        project_dir, runtime_dir = await loop.run_in_executor(
            None, ensure_workspace_layout, workdir, display_name
        )

        await loop.run_in_executor(
            None, _cleanup_workspace_if_needed, workdir, WORKSPACE_MAX_GB * 1024 ** 3
        )

        await loop.run_in_executor(None, _sanitize_opencode_db, workdir, project_dir)

        # .gitignore
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

        _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=use_ark_fallback, lemondata_key=lemondata_key)

        await loop.run_in_executor(None, _sync_company_skills_to_workdir, workdir)

        # 已有进程且还活着：若配置无变化直接复用，否则重启
        new_config = ""
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as _f:
                new_config = _f.read()
        if proc is not None and proc.returncode is None:
            if old_config == new_config:
                inst["last_active"] = _time.time()
                return {"port": inst["port"], "url": "/opencode"}
            proc.terminate()
            inst["proc"] = None
            _mark_registry_stopped(user_id)

        # 活跃进程数上限检查
        active_count = sum(
            1 for uid, i in rpm._user_instances.items()
            if uid != user_id and i.get("proc") is not None and i["proc"].returncode is None
        )
        if active_count >= MAX_ACTIVE_INSTANCES:
            raise HTTPException(503, f"当前并发实例已达上限（{MAX_ACTIVE_INSTANCES}），请稍后再试")

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
            proc_env.pop("LEMONDATA_API_KEY", None)

        fake_open_dir = os.path.join(runtime_dir, "bin")
        fake_open_path = os.path.join(fake_open_dir, "open")
        if not os.path.exists(fake_open_path):
            with open(fake_open_path, "w") as _f:
                _f.write("#!/bin/sh\n# stub: suppress opencode auto-open browser\nexit 0\n")
            os.chmod(fake_open_path, 0o755)
        proc_env["PATH"] = fake_open_dir + ":" + proc_env.get("PATH", "")
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

        # 标记注册表：starting
        try:
            from app.database import SessionLocal as _StartSL
            from app.services.studio_registry import update_runtime_status as _start_urt
            _start_db = _StartSL()
            try:
                _start_urt(_start_db, user_id, "opencode", "starting", port=port)
            finally:
                _start_db.close()
        except Exception:
            pass

        new_proc = await asyncio.create_subprocess_exec(
            opencode_bin, "web",
            "--port", str(port),
            "--hostname", "127.0.0.1",
            *cors_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=project_dir,
            env=proc_env,
        )

        ready = await _wait_ready(port)
        if not ready:
            if new_proc.returncode is None:
                new_proc.terminate()
            _mark_registry_stopped(user_id)
            raise HTTPException(503, "opencode web 启动超时，请重试")

        inst["proc"] = new_proc
        inst["workdir"] = workdir
        inst["last_active"] = _time.time()

        # 更新注册表：running + port + generation+1
        try:
            from app.database import SessionLocal as _RegSL
            from app.services.studio_registry import update_runtime_status as _update_rt
            _reg_db = _RegSL()
            try:
                _update_rt(_reg_db, user_id, "opencode", "running", port=port, bump_generation=True)
            finally:
                _reg_db.close()
        except Exception:
            pass

        # 启动百炼用量监控（全局只跑一个）
        if _usage_counter["monitor_task"] is None or _usage_counter["monitor_task"].done():
            _usage_counter["monitor_task"] = asyncio.create_task(_bailian_usage_monitor())

        # 启动空闲进程回收任务（全局只跑一个）
        if rpm._idle_reaper_task is None or rpm._idle_reaper_task.done():
            _start_idle_reaper()

        # 启动 db 大小清理任务（全局只跑一个）
        if _db_cleaner_task is None or _db_cleaner_task.done():
            _db_cleaner_task = asyncio.create_task(_db_cleaner())

        return {"port": port, "url": "/opencode"}


# ─── Provider fallback 对外接口 ──────────────────────────────────────────────

def set_provider_fallback(enable: bool, reset_counter: bool = False) -> dict:
    """手动触发/关闭百炼→ARK fallback。返回状态摘要。"""
    _runtime_fallback["use_ark"] = enable
    if reset_counter:
        _usage_counter["trigger_reason"] = ""
    return {
        "fallback_enabled": enable,
        "default_model": _resolve_default_model(enable),
    }


def get_provider_status() -> dict:
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
