"""WorkdirManager — 磁盘目录生命周期管理（纯文件系统操作，无进程/网络依赖）。"""
import os
import shutil
from typing import Optional

# ─── 统一 ignore 集合：所有目录跳过逻辑共用此集合 ──────────────────────────
RUNTIME_IGNORE_DIRS = {
    ".git", ".bin", ".bun", ".cache", ".config", ".local", ".opencode",
    "node_modules", "__pycache__", ".venv", "venv",
    ".next", "dist", "build", ".trae", ".npm", ".pnpm-store",
    "runtime",  # 隔离后的运行时目录
}

# 每个用户 workspace 目录总大小上限（包含 .local 等隐藏目录，超出后删最老 session + VACUUM）
WORKSPACE_MAX_GB = 1

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


def _workspace_skill_studio_dir(workdir: str) -> str:
    """返回用户工作区的 skill_studio 子目录（Skill Studio 专用写入区，与 OpenCode cwd 隔离）。"""
    return os.path.join(workdir, "skill_studio")


def _workspace_runtime_dir(workdir: str) -> str:
    """返回用户工作区的 runtime 子目录（OpenCode 运行时数据）。"""
    return os.path.join(workdir, "runtime")


def _workspace_runtime_data_dir(workdir: str) -> str:
    """返回 runtime/data（XDG_DATA_HOME）。"""
    return os.path.join(workdir, "runtime", "data")


def _workspace_runtime_config_dir(workdir: str) -> str:
    """返回 runtime/config（XDG_CONFIG_HOME）。"""
    return os.path.join(workdir, "runtime", "config")


def _workspace_alias_roots(workdir: str, display_name: str = "") -> list[str]:
    """返回当前工作区及其可能的 legacy 根目录别名。"""
    roots: list[str] = []

    def _add(path: str) -> None:
        normalized = os.path.normpath(path)
        if normalized not in roots:
            roots.append(normalized)

    _add(workdir)

    if display_name:
        import re as _re

        safe_name = _re.sub(r"[^\w\u4e00-\u9fff\-]", "_", display_name).strip("_")
        if safe_name:
            legacy_root = os.path.join(_studio_root(), safe_name)
            _add(legacy_root)

    return roots


def _workspace_path_candidates(workdir: str, raw_path: str) -> list[str]:
    """给定历史路径，推导当前工作区内最可能的候选路径。"""
    if not raw_path:
        return []

    project_dir = _workspace_project_dir(workdir)
    runtime_config_dir = _workspace_runtime_config_dir(workdir)
    studio_root = _studio_root()
    normalized = os.path.normpath(raw_path)
    candidates: list[str] = []

    def _add(path: str) -> None:
        normalized_path = os.path.normpath(path)
        if normalized_path not in candidates:
            candidates.append(normalized_path)

    if not os.path.isabs(normalized):
        _add(os.path.join(project_dir, normalized.lstrip("/")))
        return candidates

    if normalized == workdir:
        _add(project_dir)
        _add(workdir)
        return candidates

    if normalized == project_dir or normalized.startswith(project_dir + os.sep):
        _add(normalized)
        return candidates

    if normalized.startswith(workdir + os.sep):
        rel = os.path.relpath(normalized, workdir)
        head = rel.split(os.sep, 1)[0]
        if head in {"runtime", "skill_studio", ".local", ".config", ".bin"}:
            _add(normalized)
        elif head == ".opencode":
            _add(os.path.join(project_dir, rel))
            suffix = rel[len(".opencode"):].lstrip("/\\")
            if suffix:
                _add(os.path.join(runtime_config_dir, "opencode", suffix))
        else:
            _add(normalized)
            _add(os.path.join(project_dir, rel))
        return candidates

    if normalized.startswith(studio_root + os.sep):
        rel_to_studio = os.path.relpath(normalized, studio_root)
        parts = rel_to_studio.split(os.sep, 1)
        if len(parts) == 1:
            _add(project_dir)
            _add(workdir)
            return candidates

        remainder = parts[1]
        head = remainder.split(os.sep, 1)[0]
        if head in {"runtime", "skill_studio", ".local", ".config", ".bin"}:
            _add(os.path.join(workdir, remainder))
        elif head == ".opencode":
            _add(os.path.join(project_dir, remainder))
            suffix = remainder[len(".opencode"):].lstrip("/\\")
            if suffix:
                _add(os.path.join(runtime_config_dir, "opencode", suffix))
        else:
            _add(os.path.join(project_dir, remainder))
            _add(os.path.join(workdir, remainder))
        return candidates

    _add(normalized)
    return candidates


def resolve_workspace_path(
    workdir: str,
    raw_path: str,
    *,
    prefer_existing: bool = True,
    default_to_project: bool = False,
    allow_external: bool = True,
) -> str:
    """把历史路径映射到当前用户工作区中的稳定路径。"""
    candidates = _workspace_path_candidates(workdir, raw_path)
    project_dir = _workspace_project_dir(workdir)

    if not allow_external:
        candidates = [
            path for path in candidates
            if path == workdir
            or path.startswith(workdir + os.sep)
            or path == project_dir
            or path.startswith(project_dir + os.sep)
        ]

    if prefer_existing:
        for path in candidates:
            if os.path.exists(path):
                return path

    if default_to_project:
        for path in candidates:
            if path == project_dir or path.startswith(project_dir + os.sep):
                return path
        return project_dir

    return candidates[0] if candidates else os.path.normpath(raw_path)


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

    # 确保一级业务目录始终存在（output 是平台产物唯一正式输出目录）
    for subdir in ("inbox", "work", "export", "archive", "output"):
        os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

    # Skill Studio 隔离写入区（不在 OpenCode cwd 下，避免触发 file watcher / git 索引）
    skill_studio_dir = _workspace_skill_studio_dir(workdir)
    for subdir in ("inbox", "data"):
        os.makedirs(os.path.join(skill_studio_dir, subdir), exist_ok=True)

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
                    "- **output/** — 平台正式产物（Skill/Tool 保存来源）\n"
                    "- **inbox/** — 系统生成的待处理文件\n"
                    "- **work/** — 用户上传、AI 生成的工作文件\n"
                    "- **export/** — 导出产物\n"
                    "- **archive/** — 归档内容\n"
                )

    # 确保 project/ 是 git repo — opencode 的 @ 文件引用和工具依赖 git 索引
    git_dir = os.path.join(project_dir, ".git")
    if not os.path.exists(git_dir):
        try:
            import subprocess as _sp
            _sp.run(["git", "init"], cwd=project_dir, capture_output=True, timeout=5)
        except Exception:
            pass

    # 一次性迁移：散落在多处的用户产物 → project/output/（平台唯一正式输出目录）
    _SUPERPOWER_NAMES = {
        "dispatching-parallel-agents", "executing-plans", "finishing-a-development-branch",
        "receiving-code-review", "requesting-code-review", "subagent-driven-development",
        "test-driven-development", "using-git-worktrees", "using-superpowers",
        "verification-before-completion", "writing-plans", "writing-skills",
    }
    output_dir = os.path.join(project_dir, "output")
    import shutil as _shutil
    # 迁移源1: runtime/config/opencode/skills/ 中非白名单 .md
    runtime_skills_src = os.path.join(runtime_dir, "config", "opencode", "skills")
    if os.path.isdir(runtime_skills_src):
        for fname in os.listdir(runtime_skills_src):
            if not fname.endswith(".md"):
                continue
            stem = fname[:-3]
            if stem in _SUPERPOWER_NAMES:
                continue
            src_file = os.path.join(runtime_skills_src, fname)
            dst_file = os.path.join(output_dir, fname)
            if not os.path.exists(dst_file) and os.path.isfile(src_file):
                try:
                    _shutil.copy2(src_file, dst_file)
                except Exception:
                    pass
    # 迁移源2: skill_studio/data/ 中的用户产物
    ss_data = os.path.join(skill_studio_dir, "data")
    if os.path.isdir(ss_data):
        for fname in os.listdir(ss_data):
            src_file = os.path.join(ss_data, fname)
            dst_file = os.path.join(output_dir, fname)
            if not os.path.exists(dst_file) and os.path.isfile(src_file):
                try:
                    _shutil.copy2(src_file, dst_file)
                except Exception:
                    pass

    # symlink: project/.opencode → runtime/config/opencode/
    # 让 OpenCode 在 cwd(=project/) 下的 .opencode/ 实际指向 runtime 隔离目录
    oc_link = os.path.join(project_dir, ".opencode")
    oc_target = os.path.join(runtime_dir, "config", "opencode")
    os.makedirs(oc_target, exist_ok=True)
    if not os.path.exists(oc_link):
        try:
            os.symlink(oc_target, oc_link)
        except OSError:
            pass

    return project_dir, runtime_dir


def _cleanup_workspace_if_needed(workdir: str, max_bytes: int) -> None:
    """若 workdir 总大小超过 max_bytes，按严格白名单清理可再生内容。

    【重要】绝不删除 opencode.db 中的 session/message/part 数据。
    【重要】不做全工作区递归扫描，只清理以下白名单路径：
    """
    import logging
    logger = logging.getLogger(__name__)

    ws_bytes = _dir_size_bytes(workdir)
    if ws_bytes <= max_bytes:
        return

    name = os.path.basename(workdir)
    logger.info(
        f"[DiskCleaner] {name}: workspace {ws_bytes / 1024**3:.2f}GB 超过 "
        f"{max_bytes / 1024**3:.2f}GB 上限，开始白名单清理"
    )
    freed = 0

    project_dir = _workspace_project_dir(workdir)

    # ── 白名单：每项是 (绝对路径, 清理方式) ──
    # "rmtree"  = 删除整个目录内容（保留目录本身）
    # "glob"    = 删除目录下匹配扩展名的文件（不递归）
    _CLEANABLE: list[tuple[str, str]] = [
        # 1. runtime/cache/ — OpenCode 运行时缓存，可完全重建
        (os.path.join(workdir, "runtime", "cache"), "rmtree"),
        # 2. project/__pycache__/ — Python 字节码缓存
        (os.path.join(project_dir, "__pycache__"), "rmtree"),
        # 3. project/node_modules/.cache/ — 构建工具缓存
        (os.path.join(project_dir, "node_modules", ".cache"), "rmtree"),
        # 4. project/.next/ — Next.js 构建产物
        (os.path.join(project_dir, ".next"), "rmtree"),
        # 5. project/dist/ — 通用构建产物
        (os.path.join(project_dir, "dist"), "rmtree"),
        # 6. project/build/ — 通用构建产物
        (os.path.join(project_dir, "build"), "rmtree"),
    ]

    # ── 白名单路径内的文件级清理 ──
    _FILE_CLEAN_DIRS: list[tuple[str, set[str]]] = [
        # runtime/ 目录下的日志和临时文件（不递归进 data/config/ 等子目录）
        (os.path.join(workdir, "runtime"), {".log", ".tmp"}),
        # project/ 目录下的日志和临时文件（仅顶层）
        (project_dir, {".log", ".tmp"}),
    ]

    # 执行目录级清理
    for target_path, mode in _CLEANABLE:
        if not os.path.isdir(target_path):
            continue
        if mode == "rmtree":
            try:
                before = _dir_size_bytes(target_path)
                shutil.rmtree(target_path, ignore_errors=True)
                freed += before
                logger.debug(f"[DiskCleaner] 清理目录: {target_path} ({before / 1024**2:.1f}MB)")
            except OSError:
                pass

    # 执行文件级清理（仅扫描指定目录的顶层文件，不递归）
    for dir_path, exts in _FILE_CLEAN_DIRS:
        if not os.path.isdir(dir_path):
            continue
        try:
            for entry in os.scandir(dir_path):
                if entry.is_file(follow_symlinks=False) and os.path.splitext(entry.name)[1] in exts:
                    try:
                        size = entry.stat().st_size
                        os.remove(entry.path)
                        freed += size
                    except OSError:
                        pass
        except OSError:
            pass

    # WAL checkpoint（不删数据，只合并 WAL 回主库释放磁盘）
    db_path = _user_opencode_db_path(workdir)
    if db_path and os.path.exists(db_path):
        try:
            import sqlite3 as _sqlite3
            wal_path = db_path + "-wal"
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            if wal_size > 10 * 1024 * 1024:  # WAL > 10MB 时做 checkpoint
                con = _sqlite3.connect(db_path, timeout=5)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.close()
                freed += wal_size
        except Exception:
            pass

    ws_bytes_after = _dir_size_bytes(workdir)
    logger.info(
        f"[DiskCleaner] {name}: 清理完成，释放约 {freed / 1024**2:.1f}MB，"
        f"workspace 现在 {ws_bytes_after / 1024**3:.2f}GB"
    )
    if ws_bytes_after > max_bytes:
        logger.warning(
            f"[DiskCleaner] {name}: 清理后仍超限 ({ws_bytes_after / 1024**3:.2f}GB)，"
            f"可能存在用户上传的大文件，需管理员手动检查"
        )
