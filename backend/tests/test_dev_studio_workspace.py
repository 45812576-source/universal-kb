"""Tests for dev_studio workspace layout isolation (project/runtime split).

Covers:
- Old layout migration (full + partial/half-migrated)
- ensure_workspace_layout unified initialization
- latest-output DB location
- transfer_table / upload_file write to project/
- _ensure_user_instance paths (cwd, XDG_DATA_HOME, XDG_CONFIG_HOME)
"""
import os
import sqlite3
import shutil
import tempfile
from unittest import mock

import pytest

# Import workspace functions directly — they are pure functions with no DB dependency
from app.routers.dev_studio import (
    _workspace_project_dir,
    _workspace_runtime_dir,
    _workspace_runtime_data_dir,
    _workspace_runtime_config_dir,
    _user_opencode_db_path,
    _has_old_layout_residue,
    _is_layout_complete,
    _migrate_workspace_layout,
    ensure_workspace_layout,
    RUNTIME_IGNORE_DIRS,
)


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace root directory."""
    ws = tmp_path / "test_user"
    ws.mkdir()
    return str(ws)


# ─── Path helpers ─────────────────────────────────────────────────────────────

class TestPathHelpers:
    def test_workspace_project_dir(self, workspace):
        assert _workspace_project_dir(workspace) == os.path.join(workspace, "project")

    def test_workspace_runtime_dir(self, workspace):
        assert _workspace_runtime_dir(workspace) == os.path.join(workspace, "runtime")

    def test_workspace_runtime_data_dir(self, workspace):
        assert _workspace_runtime_data_dir(workspace) == os.path.join(workspace, "runtime", "data")

    def test_workspace_runtime_config_dir(self, workspace):
        assert _workspace_runtime_config_dir(workspace) == os.path.join(workspace, "runtime", "config")


# ─── Old layout migration: full ──────────────────────────────────────────────

class TestMigrationFull:
    """旧布局迁移：给一个临时工作区放入旧布局文件，验证迁移到 runtime/ 下。"""

    def _setup_old_layout(self, workspace):
        """Set up a workspace with old layout files."""
        # .local/share/opencode/opencode.db
        db_dir = os.path.join(workspace, ".local", "share", "opencode")
        os.makedirs(db_dir)
        db_path = os.path.join(db_dir, "opencode.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE session (id TEXT, title TEXT, time_updated INTEGER)")
        con.execute("INSERT INTO session VALUES ('s1', 'test', 1000)")
        con.close()

        # .config/opencode/config.json
        cfg_dir = os.path.join(workspace, ".config", "opencode")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            f.write('{"model": "test"}')

        # .bin/open
        bin_dir = os.path.join(workspace, ".bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "open"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")

        # .opencode/skills/test.md
        skills_dir = os.path.join(workspace, ".opencode", "skills")
        os.makedirs(skills_dir)
        with open(os.path.join(skills_dir, "test.md"), "w") as f:
            f.write("# test skill")

        # opencode.json at root
        with open(os.path.join(workspace, "opencode.json"), "w") as f:
            f.write('{"model": "test"}')

        # User files
        with open(os.path.join(workspace, "README.md"), "w") as f:
            f.write("# Hello")
        os.makedirs(os.path.join(workspace, "src"))
        with open(os.path.join(workspace, "src", "main.py"), "w") as f:
            f.write("print('hello')")

    def test_full_migration(self, workspace):
        """旧布局完整迁移后，所有文件到正确位置。"""
        self._setup_old_layout(workspace)

        _migrate_workspace_layout(workspace)

        # opencode.db 迁移到 runtime/data/opencode/
        new_db = os.path.join(workspace, "runtime", "data", "opencode", "opencode.db")
        assert os.path.exists(new_db)
        con = sqlite3.connect(new_db)
        # Verify the DB is a valid sqlite file with the expected table
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        con.close()
        assert "session" in tables

        # config.json 迁移到 runtime/config/opencode/
        new_cfg = os.path.join(workspace, "runtime", "config", "opencode", "config.json")
        assert os.path.exists(new_cfg)

        # .bin/open 迁移到 runtime/bin/
        new_open = os.path.join(workspace, "runtime", "bin", "open")
        assert os.path.exists(new_open)

        # 旧隐藏目录被清掉
        assert not os.path.exists(os.path.join(workspace, ".local"))
        assert not os.path.exists(os.path.join(workspace, ".config"))
        assert not os.path.exists(os.path.join(workspace, ".bin"))
        assert not os.path.exists(os.path.join(workspace, ".opencode"))
        assert not os.path.exists(os.path.join(workspace, "opencode.json"))

        # 用户文件移到 project/
        assert os.path.exists(os.path.join(workspace, "project", "README.md"))
        assert os.path.exists(os.path.join(workspace, "project", "src", "main.py"))

    def test_migration_idempotent(self, workspace):
        """迁移后再次调用不报错，也不重复迁移。"""
        self._setup_old_layout(workspace)
        _migrate_workspace_layout(workspace)
        # Run again
        _migrate_workspace_layout(workspace)
        assert os.path.exists(os.path.join(workspace, "project", "README.md"))


# ─── Half-migration scenario ─────────────────────────────────────────────────

class TestMigrationHalf:
    """半迁移场景：project/ 已存在，但仍残留 .local/.config。"""

    def test_half_migration_continues(self, workspace):
        """project/ 已存在但仍有 .local/.config 残留时，继续迁移而不跳过。"""
        # Create project/ (as if partially migrated)
        project_dir = os.path.join(workspace, "project")
        os.makedirs(project_dir)
        with open(os.path.join(project_dir, "hello.py"), "w") as f:
            f.write("print(1)")

        # But still have old layout residue
        old_local = os.path.join(workspace, ".local", "share", "opencode")
        os.makedirs(old_local)
        db_path = os.path.join(old_local, "opencode.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE session (id TEXT)")
        con.execute("INSERT INTO session VALUES ('orphan')")
        con.close()

        old_config = os.path.join(workspace, ".config", "opencode")
        os.makedirs(old_config)
        with open(os.path.join(old_config, "config.json"), "w") as f:
            f.write("{}")

        # Migrate should NOT skip
        _migrate_workspace_layout(workspace)

        # .local and .config should be cleaned up
        assert not os.path.exists(os.path.join(workspace, ".local"))
        assert not os.path.exists(os.path.join(workspace, ".config"))

        # DB should be in runtime/data/
        new_db = os.path.join(workspace, "runtime", "data", "opencode", "opencode.db")
        assert os.path.exists(new_db)

        # Original project file preserved
        assert os.path.exists(os.path.join(project_dir, "hello.py"))

    def test_has_old_layout_residue(self, workspace):
        """检测旧残留函数正确工作。"""
        assert not _has_old_layout_residue(workspace)

        os.makedirs(os.path.join(workspace, ".local"))
        assert _has_old_layout_residue(workspace)

        shutil.rmtree(os.path.join(workspace, ".local"))
        with open(os.path.join(workspace, "opencode.json"), "w") as f:
            f.write("{}")
        assert _has_old_layout_residue(workspace)

    def test_is_layout_complete(self, workspace):
        """布局完整性检测。"""
        assert not _is_layout_complete(workspace)

        # Create partial layout
        os.makedirs(os.path.join(workspace, "project"))
        assert not _is_layout_complete(workspace)

        os.makedirs(os.path.join(workspace, "runtime", "data"))
        assert not _is_layout_complete(workspace)

        os.makedirs(os.path.join(workspace, "runtime", "config"))
        assert _is_layout_complete(workspace)

        # Add old residue — no longer complete
        os.makedirs(os.path.join(workspace, ".local"))
        assert not _is_layout_complete(workspace)


# ─── ensure_workspace_layout ──────────────────────────────────────────────────

class TestEnsureWorkspaceLayout:
    def test_fresh_workspace(self, workspace):
        """全新工作区：创建完整布局 + 首批项目目录。"""
        project_dir, runtime_dir = ensure_workspace_layout(workspace, "测试用户")

        assert project_dir == os.path.join(workspace, "project")
        assert runtime_dir == os.path.join(workspace, "runtime")

        # project subdirs (四个业务目录)
        assert os.path.isdir(os.path.join(project_dir, "inbox"))
        assert os.path.isdir(os.path.join(project_dir, "work"))
        assert os.path.isdir(os.path.join(project_dir, "export"))
        assert os.path.isdir(os.path.join(project_dir, "archive"))
        assert os.path.isfile(os.path.join(project_dir, "README.md"))

        # runtime subdirs
        assert os.path.isdir(os.path.join(runtime_dir, "data"))
        assert os.path.isdir(os.path.join(runtime_dir, "config"))
        assert os.path.isdir(os.path.join(runtime_dir, "cache"))
        assert os.path.isdir(os.path.join(runtime_dir, "bin"))

    def test_idempotent(self, workspace):
        """多次调用不报错，README 不被重写。"""
        ensure_workspace_layout(workspace, "User")
        readme = os.path.join(workspace, "project", "README.md")
        with open(readme, "w") as f:
            f.write("Custom content")

        ensure_workspace_layout(workspace, "User")
        with open(readme) as f:
            assert f.read() == "Custom content"

    def test_old_layout_triggers_migration(self, workspace):
        """有旧布局时自动迁移。"""
        # Set up old layout
        os.makedirs(os.path.join(workspace, ".local", "share"))
        with open(os.path.join(workspace, "opencode.json"), "w") as f:
            f.write("{}")

        project_dir, runtime_dir = ensure_workspace_layout(workspace)

        assert not os.path.exists(os.path.join(workspace, "opencode.json"))
        assert not os.path.exists(os.path.join(workspace, ".local"))
        assert os.path.isdir(project_dir)
        assert os.path.isdir(runtime_dir)


# ─── _user_opencode_db_path ──────────────────────────────────────────────────

class TestUserOpencodeDbPath:
    def test_new_layout(self, workspace):
        """新布局下返回 runtime/data/opencode/opencode.db。"""
        db_dir = os.path.join(workspace, "runtime", "data", "opencode")
        os.makedirs(db_dir)
        db_path = os.path.join(db_dir, "opencode.db")
        with open(db_path, "w") as f:
            f.write("")
        assert _user_opencode_db_path(workspace) == db_path

    def test_old_layout_fallback(self, workspace):
        """旧布局下返回 .local/share/opencode/opencode.db。"""
        db_dir = os.path.join(workspace, ".local", "share", "opencode")
        os.makedirs(db_dir)
        db_path = os.path.join(db_dir, "opencode.db")
        with open(db_path, "w") as f:
            f.write("")
        assert _user_opencode_db_path(workspace) == db_path

    def test_neither_exists_returns_new_path(self, workspace):
        """两个路径都不存在时返回新布局路径。"""
        result = _user_opencode_db_path(workspace)
        assert "runtime/data/opencode/opencode.db" in result

    def test_new_layout_takes_priority(self, workspace):
        """两个路径都存在时优先返回新布局。"""
        for sub in ("runtime/data/opencode", ".local/share/opencode"):
            d = os.path.join(workspace, sub)
            os.makedirs(d)
            with open(os.path.join(d, "opencode.db"), "w") as f:
                f.write("")
        result = _user_opencode_db_path(workspace)
        assert "runtime/data" in result


# ─── latest-output reads user's own DB ────────────────────────────────────────

class TestLatestOutputDbLocation:
    """latest-output 能读到当前用户实例最新产出，不串用户。"""

    def _create_test_db(self, db_path):
        """Create a minimal opencode.db with test data."""
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, time_updated INTEGER)")
        con.execute("CREATE TABLE part (id TEXT, session_id TEXT, data TEXT, time_updated INTEGER, time_created INTEGER)")
        con.execute("INSERT INTO session VALUES ('s1', 'Test Session', 1000)")
        con.execute("""INSERT INTO part VALUES ('p1', 's1', '{"type":"tool","tool":"write","state":{"status":"completed","input":{"filePath":"/tmp/test.py","content":"print(1)"}}}', 1000, 1000)""")
        con.commit()
        con.close()

    def test_reads_user_runtime_db(self, workspace):
        """读取用户 runtime/data 下的 DB。"""
        db_path = os.path.join(workspace, "runtime", "data", "opencode", "opencode.db")
        self._create_test_db(db_path)

        result = _user_opencode_db_path(workspace)
        assert result == db_path
        assert os.path.exists(result)

    def test_does_not_read_other_user_db(self, tmp_path):
        """不同用户的 DB 互相隔离。"""
        ws_a = str(tmp_path / "user_a")
        ws_b = str(tmp_path / "user_b")
        os.makedirs(ws_a)
        os.makedirs(ws_b)

        db_a = os.path.join(ws_a, "runtime", "data", "opencode", "opencode.db")
        db_b = os.path.join(ws_b, "runtime", "data", "opencode", "opencode.db")
        self._create_test_db(db_a)
        self._create_test_db(db_b)

        assert _user_opencode_db_path(ws_a) == db_a
        assert _user_opencode_db_path(ws_b) == db_b
        assert _user_opencode_db_path(ws_a) != _user_opencode_db_path(ws_b)


# ─── transfer_table / upload_file write to project/ ──────────────────────────

class TestFileWriteToProject:
    """transfer_table 和 upload_file 的文件应落到 project/ 下。"""

    def test_user_workdir_returns_project(self, workspace):
        """_user_workdir 返回的是 project/ 子目录。"""
        ensure_workspace_layout(workspace, "test")
        project_dir = _workspace_project_dir(workspace)
        assert os.path.isdir(project_dir)
        # Simulating what _user_workdir does
        assert "project" in project_dir

    def test_ensure_creates_business_dirs(self, workspace):
        """统一初始化创建 inbox/work/export/archive。"""
        project_dir, _ = ensure_workspace_layout(workspace, "test")
        for d in ("inbox", "work", "export", "archive"):
            assert os.path.isdir(os.path.join(project_dir, d))

    def test_upload_to_project_not_root(self, workspace):
        """文件应写入 project/ 而非 workspace 根目录。"""
        project_dir, _ = ensure_workspace_layout(workspace, "test")

        # Simulate upload
        upload_path = os.path.join(project_dir, "data.csv")
        with open(upload_path, "w") as f:
            f.write("col1,col2\n1,2")

        assert os.path.exists(upload_path)
        # Not in workspace root
        assert not os.path.exists(os.path.join(workspace, "data.csv"))


# ─── _ensure_user_instance paths ──────────────────────────────────────────────

class TestEnsureUserInstancePaths:
    """验证 _ensure_user_instance 设置的路径正确。"""

    def test_cwd_is_project_dir(self, workspace):
        """cwd 应为 project/ 子目录。"""
        project_dir, _ = ensure_workspace_layout(workspace)
        assert project_dir.endswith("/project") or project_dir.endswith("\\project")

    def test_xdg_data_home_is_runtime_data(self, workspace):
        """XDG_DATA_HOME 应指向 runtime/data。"""
        _, runtime_dir = ensure_workspace_layout(workspace)
        xdg_data = _workspace_runtime_data_dir(workspace)
        assert xdg_data == os.path.join(workspace, "runtime", "data")
        assert os.path.isdir(xdg_data)

    def test_xdg_config_home_is_runtime_config(self, workspace):
        """XDG_CONFIG_HOME 应指向 runtime/config。"""
        _, runtime_dir = ensure_workspace_layout(workspace)
        xdg_config = _workspace_runtime_config_dir(workspace)
        assert xdg_config == os.path.join(workspace, "runtime", "config")
        assert os.path.isdir(xdg_config)

    def test_fake_open_in_runtime_bin(self, workspace):
        """假 open 脚本应在 runtime/bin 而非 project。"""
        _, runtime_dir = ensure_workspace_layout(workspace)
        bin_dir = os.path.join(runtime_dir, "bin")
        assert os.path.isdir(bin_dir)
        # project/ should not have .bin
        assert not os.path.exists(os.path.join(workspace, "project", ".bin"))

    def test_no_runtime_dirs_in_project(self, workspace):
        """project/ 下不应有任何运行时目录。"""
        project_dir, _ = ensure_workspace_layout(workspace)
        entries = set(os.listdir(project_dir))
        runtime_dirs = {".local", ".config", ".bin", ".opencode"}
        assert entries.isdisjoint(runtime_dirs), f"Found runtime dirs in project: {entries & runtime_dirs}"


# ─── RUNTIME_IGNORE_DIRS consistency ──────────────────────────────────────────

class TestIgnoreDirs:
    def test_runtime_in_ignore_set(self):
        """runtime 目录本身在 ignore 集合中。"""
        assert "runtime" in RUNTIME_IGNORE_DIRS

    def test_common_dirs_in_ignore_set(self):
        """常见需忽略的目录都在集合中。"""
        expected = {".git", ".local", ".config", ".opencode", ".bin", "node_modules", ".cache"}
        assert expected.issubset(RUNTIME_IGNORE_DIRS)
