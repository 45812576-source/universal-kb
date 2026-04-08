"""工作台产出自动沉淀服务。

每 30 分钟扫描所有用户 workdir，把新产出的文档（.md/.pdf/.docx/.xlsx）
自动创建 KnowledgeEntry 并归入用户的"开发工地"文件夹。

去重策略：用 source_file 字段存储 "{user_id}:{rel_path}:{mtime_int}"，
同一文件未修改则跳过；修改后创建新版本条目。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# 扫描的文件扩展名
_SCAN_EXTS = {".md", ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt"}

# 跳过的系统文件名（大小写不敏感）
_SKIP_NAMES = {
    "readme.md", "opencode.json", "agents.md",
    "tool_request.md", ".gitignore", ".ds_store",
}

# 只扫描 workdir 根目录和这些子目录（不递归进依赖/构建目录）
_SCAN_SUBDIRS = {"docs", "scripts", "src", "output", "产出", "文档", "报告"}

_SKIP_DIRS = {
    "node_modules", ".bun", ".cache", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".local", ".config", ".opencode", ".bin", ".git",
}


def _source_file_key(user_id: int, rel_path: str, mtime: int) -> str:
    # source_file 列限 VARCHAR(255)，rel_path 截到 200 字符内确保不超
    safe_rel = rel_path[-180:] if len(rel_path) > 180 else rel_path
    return f"workdir:{user_id}:{safe_rel}:{mtime}"


def _ext_to_mime(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(ext, "application/octet-stream")


def _collect_files(workdir: str) -> list[tuple[str, str, int]]:
    """返回 [(abs_path, rel_path, mtime_int), ...] 需要处理的文件列表。"""
    results = []

    def _scan_dir(abs_dir: str, rel_dir: str, depth: int):
        if depth > 3:
            return
        try:
            entries = os.scandir(abs_dir)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            if entry.is_dir():
                if depth == 0 or entry.name.lower() in _SCAN_SUBDIRS:
                    _scan_dir(entry.path, os.path.join(rel_dir, entry.name) if rel_dir else entry.name, depth + 1)
            elif entry.is_file():
                if entry.name.lower() in _SKIP_NAMES:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in _SCAN_EXTS:
                    continue
                try:
                    mtime = int(entry.stat().st_mtime)
                except OSError:
                    continue
                rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
                results.append((entry.path, rel, mtime))

    _scan_dir(workdir, "", 0)
    return results


def _read_md_content(abs_path: str) -> str:
    """读取 .md 文件内容，按字节截断到 60000 字节（TEXT 列限 65535 字节，留余量给中文多字节）。"""
    try:
        with open(abs_path, "rb") as f:
            raw = f.read(60000)
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def run_workdir_kb_sync(db) -> None:
    """主入口：扫描全部用户 workdir，沉淀新产出到知识库。"""
    from app.config import settings
    from app.models.opencode import OpenCodeWorkspaceMapping
    from app.models.knowledge import KnowledgeEntry, KnowledgeFolder, KnowledgeStatus, ReviewStage
    from app.services.oss_service import generate_oss_key, upload_file as oss_upload

    studio_root = os.path.abspath(os.path.expanduser(
        getattr(settings, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
    ))

    mappings = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.directory.isnot(None),
        OpenCodeWorkspaceMapping.kb_folder_id.isnot(None),
    ).all()

    total_new = 0
    for mapping in mappings:
        workdir = mapping.directory
        if not os.path.isdir(workdir):
            continue

        user_id = mapping.user_id
        folder_id = mapping.kb_folder_id

        files = _collect_files(workdir)
        for abs_path, rel_path, mtime in files:
            source_key = _source_file_key(user_id, rel_path, mtime)

            # 去重：同一文件同一 mtime 已入库则跳过
            existing = db.query(KnowledgeEntry).filter(
                KnowledgeEntry.source_file == source_key
            ).first()
            if existing:
                continue

            # 文件名作为标题
            filename = os.path.basename(rel_path)
            title = os.path.splitext(filename)[0]
            ext = os.path.splitext(filename)[1].lower()
            mime = _ext_to_mime(ext)

            # .md 文件直接读内容，其他文件上传 OSS
            content = ""
            oss_key = None
            file_size = None

            if ext == ".md":
                content = _read_md_content(abs_path)
                if not content.strip():
                    continue
            else:
                try:
                    file_size = os.path.getsize(abs_path)
                    oss_key = generate_oss_key(ext.lstrip("."), prefix="workdir_output")
                    oss_upload(abs_path, oss_key)
                    content = f"[工作台产出文件] {rel_path}"
                    mime = mime[:50]  # file_type 列限 VARCHAR(50)
                except Exception as e:
                    logger.warning(f"[WorkdirSync] 上传 OSS 失败 {abs_path}: {e}")
                    continue

            entry = KnowledgeEntry(
                title=title,
                content=content,
                category="experience",
                status=KnowledgeStatus.APPROVED,
                review_stage=ReviewStage.AUTO_APPROVED,
                review_level=0,
                folder_id=folder_id,
                created_by=user_id,
                source_type="workdir_sync",
                source_file=source_key,
                capture_mode="workdir_sync",
                oss_key=oss_key,
                file_type=mime if oss_key else None,
                file_ext=ext if oss_key else None,
                file_size=file_size,
                visibility_scope="private",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(entry)
            total_new += 1
            logger.info(f"[WorkdirSync] 新增条目: user={user_id} file={rel_path}")

        if total_new > 0:
            db.commit()

    logger.info(f"[WorkdirSync] 本轮完成，新增 {total_new} 条")
