"""MCP 服务安装器：审批通过后自动在本机安装并启动 MCP 服务，探测端口，写入 config.url。"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.models.tool import ToolRegistry

logger = logging.getLogger(__name__)

# MCP 服务解压安装根目录
_INSTALL_ROOT = Path(__file__).parent.parent.parent / "mcp_servers"
# 等待服务就绪的超时秒数
_STARTUP_TIMEOUT = 60
# 运行中的进程注册表 tool_id → Popen
_running_procs: dict[int, subprocess.Popen] = {}


# ─── zip 解析 ─────────────────────────────────────────────────────────────────

def analyze_zip(zip_path: str) -> dict:
    """
    解压 zip 到临时目录，分析项目类型，推断启动命令。
    返回：
      {
        "project_type": "python" | "node" | "unknown",
        "entry_file": "server.py",        # 推断出的入口文件
        "run_cmd": "python server.py",    # 推断出的启动命令
        "dependencies": ["requests"],      # 从依赖文件中提取
        "warnings": ["..."],
      }
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    result: dict = {"project_type": "unknown", "entry_file": "", "run_cmd": "", "dependencies": [], "warnings": []}

    # 去掉顶层目录前缀（如 my-server/server.py → server.py）
    def strip_prefix(names: list[str]) -> list[str]:
        if not names:
            return names
        parts = [n.split("/") for n in names if n]
        multi = [p for p in parts if len(p) > 1]
        # 只有当所有文件都在同一顶层目录下时才剥前缀
        if multi and all(p[0] == multi[0][0] for p in multi):
            prefix = multi[0][0]
            return ["/".join(p[1:]) for p in parts if len(p) > 1 and p[1:]]
        return names

    flat = strip_prefix(names)

    has_package_json = any(f == "package.json" or f.endswith("/package.json") for f in flat)
    has_requirements = any(f in ("requirements.txt", "pyproject.toml") for f in flat)
    py_candidates = [f for f in flat if f.endswith(".py") and f.split("/")[-1] in ("server.py", "main.py", "__main__.py", "app.py")]
    js_candidates = [f for f in flat if f.split("/")[-1] in ("server.js", "index.js", "main.js")]

    if has_package_json:
        result["project_type"] = "node"
        entry = js_candidates[0].split("/")[-1] if js_candidates else "index.js"
        result["entry_file"] = entry
        result["run_cmd"] = f"node {entry}"
    elif has_requirements or py_candidates:
        result["project_type"] = "python"
        entry = py_candidates[0].split("/")[-1] if py_candidates else "server.py"
        result["entry_file"] = entry
        result["run_cmd"] = f"python {entry}"
    else:
        result["warnings"].append("无法识别项目类型，请手动确认启动命令")

    return result


def extract_zip(zip_path: str, tool_name: str) -> Path:
    """解压 zip 到 mcp_servers/<tool_name>，返回解压目录。"""
    install_dir = _INSTALL_ROOT / tool_name
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        # 统一去除顶层目录
        names = zf.namelist()
        prefix = ""
        parts = [n.split("/") for n in names if n and not n.endswith("/")]
        multi = [p for p in parts if len(p) > 1]
        if multi and all(p[0] == multi[0][0] for p in multi):
            prefix = multi[0][0] + "/"

        for member in zf.infolist():
            if member.filename.endswith("/"):
                continue
            rel = member.filename[len(prefix):] if prefix and member.filename.startswith(prefix) else member.filename
            if not rel:
                continue
            dest = install_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member.filename))

    return install_dir


async def _install_dependencies(install_dir: Path, project_type: str) -> tuple[bool, str]:
    """安装依赖，返回 (成功, 错误信息)。用 asyncio.create_subprocess_exec 避免阻塞事件循环。"""
    try:
        if project_type == "node" and (install_dir / "package.json").exists():
            cmd = ["npm", "install"]
        elif project_type == "python":
            if (install_dir / "requirements.txt").exists():
                cmd = ["pip", "install", "-r", "requirements.txt"]
            elif (install_dir / "pyproject.toml").exists():
                cmd = ["pip", "install", "-e", "."]
            else:
                return True, ""
        else:
            return True, ""

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=install_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "依赖安装超时（120s）"

        if proc.returncode != 0:
            return False, stderr.decode(errors="replace")[:500]
    except Exception as e:
        return False, str(e)
    return True, ""


def _find_free_port() -> int:
    import socket
    for host in ("127.0.0.1", ""):
        try:
            with socket.socket() as s:
                s.bind((host, 0))
                return s.getsockname()[1]
        except PermissionError:
            continue
        except OSError:
            continue

    fallback_base = int(os.environ.get("MCP_FALLBACK_PORT_BASE", "38080"))
    fallback_spread = max(int(os.environ.get("MCP_FALLBACK_PORT_SPREAD", "1000")), 1)
    port = fallback_base + (os.getpid() % fallback_spread)
    logger.warning("[MCP] 无法探测空闲端口，回退到端口 %s", port)
    return port


async def _wait_for_http(url: str, timeout: int = _STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                await client.get(url)
                return True
            except Exception:
                await asyncio.sleep(1)
    return False


# ─── 主入口 ───────────────────────────────────────────────────────────────────

async def install_and_start(db: Session, tool: ToolRegistry) -> dict:
    """
    审批通过后调用：
    1. 解压 zip（已在 upload 阶段完成，直接用 install_dir）
    2. 安装依赖
    3. 启动进程，注入 PORT 环境变量
    4. 等待 HTTP 就绪
    5. 写入 tool.config["url"]，设 is_active=True

    返回 {"ok": True, "url": "..."} 或 {"ok": False, "error": "..."}
    """
    config = tool.config or {}
    install_dir_str = config.get("install_dir", "")
    run_cmd = config.get("run_cmd", "").strip()

    if not install_dir_str or not run_cmd:
        return {"ok": False, "error": "缺少 install_dir 或 run_cmd，请重新上传"}

    install_dir = Path(install_dir_str)
    if not install_dir.exists():
        return {"ok": False, "error": f"安装目录不存在：{install_dir_str}"}

    # 若进程已在运行且 url 有效，直接返回
    if tool.id in _running_procs:
        proc = _running_procs[tool.id]
        if proc.poll() is None and config.get("url"):
            return {"ok": True, "url": config["url"], "note": "进程已在运行"}

    # 安装依赖
    project_type = config.get("project_type", "unknown")
    ok, err = await _install_dependencies(install_dir, project_type)
    if not ok:
        return {"ok": False, "error": f"依赖安装失败：{err}"}

    # 分配端口，启动进程
    port = _find_free_port()
    url = f"http://localhost:{port}"
    cmd_str = run_cmd.replace("{port}", str(port))
    cmd = cmd_str.split()
    env = {**os.environ, "PORT": str(port), "MCP_PORT": str(port)}

    logger.info(f"[MCP] 启动 '{tool.name}': {cmd_str} port={port}")
    try:
        proc = subprocess.Popen(cmd, cwd=install_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令未找到：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"进程启动失败：{e}"}

    _running_procs[tool.id] = proc

    ready = await _wait_for_http(url)
    if not ready:
        proc.kill()
        del _running_procs[tool.id]
        stderr_out = b""
        if proc.stderr:
            try:
                stderr_out = proc.stderr.read()
            except Exception:
                pass
        return {"ok": False, "error": f"服务启动超时（{_STARTUP_TIMEOUT}s），stderr：{stderr_out.decode(errors='replace')[:500]}"}

    # 写入 DB
    tool.config = {**config, "url": url}
    tool.is_active = True
    db.commit()
    logger.info(f"[MCP] '{tool.name}' 就绪：{url}")
    return {"ok": True, "url": url}


def stop_tool(tool_id: int) -> None:
    """停止 MCP 进程（工具归档/删除时调用）。"""
    proc = _running_procs.pop(tool_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
