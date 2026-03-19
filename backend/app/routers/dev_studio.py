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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.skill import Skill, SkillStatus, SkillVersion
from app.models.tool import ToolRegistry, ToolType
from app.models.user import User

router = APIRouter(prefix="/api/dev-studio", tags=["dev-studio"])

# ─── 全局单例 ──────────────────────────────────────────────────────────────────
# 整个后端进程只启动一个 opencode web，所有用户共用
_singleton: dict = {
    "proc": None,
    "port": None,
    "workdir": None,
    "lock": None,   # asyncio.Lock，运行时初始化
}

OPENCODE_FIXED_PORT = 17171   # 固定端口，重启后不变

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
    """从 OpenCode SQLite 读取所有 session 的 summary_files 累计总量。"""
    import sqlite3 as _sqlite3
    db_path = os.environ.get(
        "OPENCODE_DB_PATH",
        os.path.expanduser("~/.local/share/opencode/opencode.db"),
    )
    if not os.path.exists(db_path):
        return 0
    try:
        con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT COALESCE(SUM(summary_files), 0) FROM session").fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


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
                workdir = _singleton.get("workdir") or os.path.join(tempfile.gettempdir(), "ledesk_dev_studio")
                os.makedirs(workdir, exist_ok=True)
                _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=True)

                proc = _singleton.get("proc")
                if proc is not None and proc.returncode is None:
                    proc.terminate()
                    _singleton["proc"] = None
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
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


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


async def _ensure_singleton() -> dict:
    """确保全局 opencode web 实例在跑，返回 {port, url}。"""
    # 延迟初始化 lock（event loop 在 startup 之后才存在）
    if _singleton["lock"] is None:
        _singleton["lock"] = asyncio.Lock()

    async with _singleton["lock"]:
        proc: Optional[asyncio.subprocess.Process] = _singleton["proc"]

        # 已有进程且还活着，直接复用
        if proc is not None and proc.returncode is None:
            return {"port": _singleton["port"], "url": "/opencode"}

        opencode_bin = _find_opencode()
        if not opencode_bin:
            raise HTTPException(503, "opencode 未安装，请先运行: npm install -g opencode-ai")

        # 固定 workdir（重启后同一目录，保留上下文）
        workdir = os.path.join(tempfile.gettempdir(), "ledesk_dev_studio")
        os.makedirs(workdir, exist_ok=True)

        from app.config import settings as _settings
        bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
        ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
        lemondata_key = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")
        # 运行时开关优先，其次读配置文件
        use_ark_fallback = _runtime_fallback["use_ark"] or getattr(_settings, "BAILIAN_FALLBACK_TO_ARK", False)

        # 写入完整 opencode.json（含 provider 配置），每次启动刷新确保配置最新
        _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=use_ark_fallback, lemondata_key=lemondata_key)

        # provider key 通过环境变量传入（{env:XXX} 语法备用）
        proc_env = os.environ.copy()
        if bailian_key:
            proc_env["BAILIAN_API_KEY"] = bailian_key
        if ark_key:
            proc_env["ARK_API_KEY"] = ark_key
        if lemondata_key:
            proc_env["LEMONDATA_API_KEY"] = lemondata_key

        # CORS 允许来源：从环境变量 FRONTEND_ORIGIN 读取（支持内网穿透域名）
        frontend_origins = [
            o.strip()
            for o in os.environ.get("FRONTEND_ORIGIN", "http://localhost:5023").split(",")
            if o.strip()
        ]
        cors_args = []
        for origin in frontend_origins:
            cors_args += ["--cors", origin]

        new_proc = await asyncio.create_subprocess_exec(
            opencode_bin, "web",
            "--port", str(OPENCODE_FIXED_PORT),
            "--hostname", "127.0.0.1",
            *cors_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workdir,
            env=proc_env,
        )

        ready = await _wait_ready(OPENCODE_FIXED_PORT)
        if not ready:
            # 进程可能已自行退出，安全 terminate
            if new_proc.returncode is None:
                new_proc.terminate()
            raise HTTPException(503, "opencode web 启动超时，请重试")

        _singleton["proc"] = new_proc
        _singleton["port"] = OPENCODE_FIXED_PORT
        _singleton["workdir"] = workdir

        # 启动百炼用量监控（全局只跑一个）
        if _usage_counter["monitor_task"] is None or _usage_counter["monitor_task"].done():
            _usage_counter["monitor_task"] = asyncio.create_task(_bailian_usage_monitor())

        return {"port": OPENCODE_FIXED_PORT, "url": "/opencode"}


# ─── 运行时 fallback 开关（不重启进程，只刷新 opencode.json + 可选重启进程）──

@router.post("/provider-fallback")
async def set_provider_fallback(
    enable: bool,
    reset_counter: bool = False,
    user: User = Depends(get_current_user),
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
    bailian_key = getattr(_settings, "BAILIAN_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")
    ark_key = getattr(_settings, "ARK_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
    lemondata_key = getattr(_settings, "LEMONDATA_API_KEY", "") or os.environ.get("LEMONDATA_API_KEY", "")

    workdir = _singleton.get("workdir") or os.path.join(tempfile.gettempdir(), "ledesk_dev_studio")
    os.makedirs(workdir, exist_ok=True)
    _write_opencode_config(workdir, bailian_key=bailian_key, ark_key=ark_key, use_ark_fallback=enable, lemondata_key=lemondata_key)

    # 终止现有进程，让下次请求触发重启（刷新配置）
    proc = _singleton.get("proc")
    if proc is not None and proc.returncode is None:
        proc.terminate()
        _singleton["proc"] = None

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


# ─── GET /instance — 获取（或启动）单例 ───────────────────────────────────────

@router.get("/instance")
async def get_instance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    info = await _ensure_singleton()

    # 自动为当前用户建立 workdir → user_id 映射（用于统计）
    user_workdir = os.path.join(tempfile.gettempdir(), f"ledesk_studio_user_{user.id}")
    os.makedirs(user_workdir, exist_ok=True)

    from app.models.opencode import OpenCodeWorkspaceMapping
    mapping = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.user_id == user.id,
        OpenCodeWorkspaceMapping.directory != None,
    ).first()
    if mapping is None:
        mapping = OpenCodeWorkspaceMapping(
            user_id=user.id,
            directory=user_workdir,
            opencode_workspace_name=user.display_name,
        )
        db.add(mapping)
        db.commit()

    return {"url": info["url"], "port": info["port"], "status": "ready"}


# ─── 兼容旧的 POST /sessions（不再创建多个，统一走单例）─────────────────────

@router.post("/sessions")
async def create_session(user: User = Depends(get_current_user)):
    info = await _ensure_singleton()
    return {
        "session_id": "singleton",
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
        is_active=True,
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
    from app.models.user import Role

    # 超管直接发布，其他人进审批流
    is_super = user.role == Role.SUPER_ADMIN
    initial_status = SkillStatus.PUBLISHED if is_super else SkillStatus.REVIEWING
    initial_scope = "company" if is_super else "personal"

    skill = Skill(
        name=req.name,
        description=req.description,
        scope=initial_scope,
        mode="hybrid",
        created_by=user.id,
        status=initial_status,
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

    approval_id = None
    if not is_super:
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        from app.models.user import Role as R
        # 部门管理员跳过部门审批，直接到超管
        stage = "super_pending" if user.role == R.DEPT_ADMIN else "dept_pending"
        approval = ApprovalRequest(
            request_type=ApprovalRequestType.skill_publish,
            target_id=skill.id,
            target_type="skill",
            requester_id=user.id,
            status=ApprovalStatus.pending,
            stage=stage,
        )
        db.add(approval)
        db.flush()
        approval_id = approval.id

    db.commit()
    db.refresh(skill)
    return {
        "id": skill.id,
        "name": skill.name,
        "status": skill.status,
        "approval_id": approval_id,
    }
