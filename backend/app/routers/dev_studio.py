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

KIMI_DEFAULT_MODEL = "kimi-for-coding/k2p5"


def _write_opencode_config(workdir: str) -> None:
    """在 workdir 写入 opencode.json，仅指定默认 model。
    kimi-for-coding 是内置 provider，通过 KIMI_API_KEY 环境变量鉴权，不需要 provider 覆盖。
    """
    config: dict = {
        "$schema": "https://opencode.ai/config.schema.json",
        "model": KIMI_DEFAULT_MODEL,
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
            return {"port": _singleton["port"], "url": f"http://127.0.0.1:{_singleton['port']}"}

        opencode_bin = _find_opencode()
        if not opencode_bin:
            raise HTTPException(503, "opencode 未安装，请先运行: npm install -g opencode-ai")

        # 固定 workdir（重启后同一目录，保留上下文）
        workdir = os.path.join(tempfile.gettempdir(), "ledesk_dev_studio")
        os.makedirs(workdir, exist_ok=True)

        from app.config import settings as _settings
        kimi_key = getattr(_settings, "KIMI_API_KEY", "") or os.environ.get("KIMI_API_KEY", "")

        # 写入 opencode.json（含 provider endpoint 和默认 model）
        _write_opencode_config(workdir)

        # kimi-for-coding provider 读取 KIMI_API_KEY 环境变量
        proc_env = os.environ.copy()
        if kimi_key:
            proc_env["KIMI_API_KEY"] = kimi_key

        new_proc = await asyncio.create_subprocess_exec(
            opencode_bin, "web",
            "--port", str(OPENCODE_FIXED_PORT),
            "--hostname", "127.0.0.1",
            "--cors", "http://localhost:5023",
            "--cors", "http://localhost:3000",
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

        return {"port": OPENCODE_FIXED_PORT, "url": f"http://127.0.0.1:{OPENCODE_FIXED_PORT}"}


# ─── GET /instance — 获取（或启动）单例 ───────────────────────────────────────

@router.get("/instance")
async def get_instance(user: User = Depends(get_current_user)):
    info = await _ensure_singleton()
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
    skill = Skill(
        name=req.name,
        description=req.description,
        scope="personal",
        mode="hybrid",
        created_by=user.id,
        status=SkillStatus.DRAFT,
        auto_inject=True,
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
    return {"id": skill.id, "name": skill.name}
