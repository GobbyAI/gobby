"""Tests for the Cursor installer module.

Exercises the real install_cursor() and uninstall_cursor() functions
with real filesystem operations. Only get_install_dir() is mocked.

Targets 90%+ statement coverage of src/gobby/cli/installers/cursor.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.cli.installers.cursor import install_cursor, uninstall_cursor

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_install_dir(base: Path) -> Path:
    """Create a realistic install directory with cursor hook sources."""
    install_dir = base / "install"
    cursor_dir = install_dir / "cursor"
    hooks_dir = cursor_dir / "hooks"
    hooks_dir.mkdir(parents=True)

    (hooks_dir / "hook_dispatcher.py").write_text("#!/usr/bin/env python3\n# dispatcher\n")

    template = {
        "hooks": {
            "file_saved": [
                {
                    "command": "python $HOOKS_DIR/hook_dispatcher.py file_saved",
                    "events": ["file_saved"],
                }
            ],
            "context_loading": [
                {
                    "command": "python $HOOKS_DIR/hook_dispatcher.py context_loading",
                    "events": ["context_loading"],
                }
            ],
        }
    }
    (cursor_dir / "hooks-template.json").write_text(json.dumps(template))

    return install_dir


# ---------------------------------------------------------------------------
# install_cursor
# ---------------------------------------------------------------------------


class TestInstallCursor:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        p = tmp_path / "project"
        p.mkdir()
        return p

    @pytest.fixture
    def install_dir(self, tmp_path: Path) -> Path:
        return _create_install_dir(tmp_path)

    def test_fresh_install_success(self, project: Path, install_dir: Path) -> None:
        """Full install on a clean project directory."""
        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": ["lifecycle.yaml"],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True
        assert result["error"] is None
        assert "file_saved" in result["hooks_installed"]
        assert "context_loading" in result["hooks_installed"]
        assert result["workflows_installed"] == ["lifecycle.yaml"]

        # Verify directory structure
        assert (project / ".cursor").is_dir()

        # Verify hooks.json content
        hooks_file = project / ".cursor" / "hooks.json"
        assert hooks_file.exists()
        with open(hooks_file) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert "file_saved" in data["hooks"]
        assert "context_loading" in data["hooks"]

        # Verify $HOOKS_DIR was replaced
        cmd = data["hooks"]["file_saved"][0]["command"]
        assert "$HOOKS_DIR" not in cmd
        assert "hook_dispatcher.py" in cmd

    def test_missing_hook_dispatcher(self, project: Path, tmp_path: Path) -> None:
        """Succeeds but dispatcher is not installed when source is missing."""
        install_dir = tmp_path / "install"
        cursor_dir = install_dir / "cursor"
        (cursor_dir / "hooks").mkdir(parents=True)
        (cursor_dir / "hooks-template.json").write_text(json.dumps({"hooks": {}}))
        # Create shared/hooks/ dir but do NOT add hook_dispatcher.py
        (install_dir / "shared" / "hooks").mkdir(parents=True)

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch("gobby.cli.installers.shared.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        # install_shared_hooks logs a warning but doesn't fail
        assert result["success"] is True
        # Dispatcher was not installed because source was missing
        assert not (project / ".cursor" / "hooks" / "hook_dispatcher.py").exists()

    def test_missing_hooks_template(self, project: Path, tmp_path: Path) -> None:
        """Fails when hooks-template.json is missing."""
        install_dir = tmp_path / "install"
        cursor_dir = install_dir / "cursor"
        hooks_dir = cursor_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook_dispatcher.py").write_text("# dispatcher")
        # No hooks-template.json

        with patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Missing source files" in result["error"]
        assert "hooks-template.json" in result["error"]

    def test_missing_both_source_files(self, project: Path, tmp_path: Path) -> None:
        """Fails when both source files are missing."""
        install_dir = tmp_path / "install"
        (install_dir / "cursor" / "hooks").mkdir(parents=True)

        with patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Missing source files" in result["error"]

    def test_install_file_oserror(self, project: Path, install_dir: Path) -> None:
        """Fails when install_global_hooks raises OSError."""
        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_global_hooks",
                side_effect=OSError("Permission denied"),
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to install hook files" in result["error"]

    def test_shared_content_failure_is_non_fatal(self, project: Path, install_dir: Path) -> None:
        """install_shared_content failure does not block hooks installation."""
        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                side_effect=RuntimeError("Shared content kaboom"),
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True
        assert result["error"] is None
        assert (project / ".cursor" / "hooks.json").exists()

    def test_shared_content_results_populated(self, project: Path, install_dir: Path) -> None:
        """All shared content result keys are populated."""
        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": ["w.yaml"],
                    "agents": ["a.yaml"],
                    "plugins": ["p.py"],
                    "prompts": ["pr.md"],
                    "docs": ["d.md"],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True
        assert result["workflows_installed"] == ["w.yaml"]
        assert result["agents_installed"] == ["a.yaml"]
        assert result["plugins_installed"] == ["p.py"]
        assert result["prompts_installed"] == ["pr.md"]
        assert result["docs_installed"] == ["d.md"]

    def test_existing_hooks_json_backup_and_merge(self, project: Path, install_dir: Path) -> None:
        """Existing hooks.json is backed up and merged with Gobby hooks."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        existing = {
            "version": 1,
            "hooks": {
                "custom_hook": [{"command": "echo custom", "events": ["custom"]}],
            },
        }
        (cursor_path / "hooks.json").write_text(json.dumps(existing))

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True

        # Verify backup
        backups = list(cursor_path.glob("hooks.json.*.backup"))
        assert len(backups) == 1
        with open(backups[0]) as f:
            assert json.load(f) == existing

        # Verify merged content
        with open(cursor_path / "hooks.json") as f:
            merged = json.load(f)
        assert "custom_hook" in merged["hooks"]
        assert "file_saved" in merged["hooks"]
        assert "context_loading" in merged["hooks"]

    def test_corrupt_hooks_json_starts_fresh(self, project: Path, install_dir: Path) -> None:
        """Corrupt hooks.json causes a fresh start, not a failure."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text("{ invalid json !!!")

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True
        with open(cursor_path / "hooks.json") as f:
            data = json.load(f)
        assert "version" in data
        assert "file_saved" in data["hooks"]

    def test_hooks_json_read_oserror(self, project: Path, install_dir: Path) -> None:
        """OSError reading existing hooks.json returns error."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        hooks_file = cursor_path / "hooks.json"
        hooks_file.write_text(json.dumps({"version": 1, "hooks": {}}))
        hooks_file.chmod(0o000)

        try:
            with (
                patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
                patch(
                    "gobby.cli.installers.cursor.install_shared_content",
                    return_value={
                        "workflows": [],
                        "agents": [],
                        "plugins": [],
                        "prompts": [],
                        "docs": [],
                    },
                ),
            ):
                result = install_cursor(project, mode="project")

            assert result["success"] is False
            assert "Failed" in result["error"]
        finally:
            hooks_file.chmod(0o644)

    def test_template_read_oserror(self, project: Path, tmp_path: Path) -> None:
        """OSError reading template returns error."""
        install_dir = tmp_path / "install2"
        cursor_dir = install_dir / "cursor"
        hooks_dir = cursor_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook_dispatcher.py").write_text("# dispatcher")
        template = cursor_dir / "hooks-template.json"
        template.write_text(json.dumps({"hooks": {}}))
        template.chmod(0o000)

        try:
            with (
                patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
                patch(
                    "gobby.cli.installers.cursor.install_shared_content",
                    return_value={
                        "workflows": [],
                        "agents": [],
                        "plugins": [],
                        "prompts": [],
                        "docs": [],
                    },
                ),
            ):
                result = install_cursor(project, mode="project")

            assert result["success"] is False
            assert "Failed to read hooks template" in result["error"]
        finally:
            template.chmod(0o644)

    def test_template_parse_error(self, project: Path, tmp_path: Path) -> None:
        """Invalid JSON in template returns error."""
        install_dir = tmp_path / "install3"
        cursor_dir = install_dir / "cursor"
        hooks_dir = cursor_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook_dispatcher.py").write_text("# dispatcher")
        (cursor_dir / "hooks-template.json").write_text("{ not json !!!")

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to parse hooks template" in result["error"]

    def test_backup_failure_returns_error(self, project: Path, install_dir: Path) -> None:
        """Backup failure (copy2 error) returns error."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {}}))

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
            patch("gobby.cli.installers.cursor.copy2", side_effect=OSError("Permission denied")),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to create backup" in result["error"]

    def test_atomic_write_failure_restores_backup(self, project: Path, install_dir: Path) -> None:
        """Atomic write failure restores from backup."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        original_hooks = {"version": 1, "hooks": {"custom": [{"command": "echo hi"}]}}
        hooks_file = cursor_path / "hooks.json"
        hooks_file.write_text(json.dumps(original_hooks))

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
            patch(
                "gobby.cli.installers.cursor.tempfile.mkstemp",
                side_effect=OSError("Disk full"),
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to write hooks.json" in result["error"]

        # Original should be restored from backup
        with open(hooks_file) as f:
            restored = json.load(f)
        assert restored == original_hooks

    def test_atomic_write_failure_no_backup(self, project: Path, install_dir: Path) -> None:
        """Atomic write failure with no backup (new install) just returns error."""
        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
            patch(
                "gobby.cli.installers.cursor.tempfile.mkstemp",
                side_effect=OSError("Disk full"),
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to write hooks.json" in result["error"]

    def test_ensures_version_and_hooks_keys(self, project: Path, install_dir: Path) -> None:
        """Existing hooks.json missing version/hooks keys gets them added."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text(json.dumps({"other_key": "value"}))

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is True
        with open(cursor_path / "hooks.json") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert "hooks" in data

    def test_hooks_json_open_oserror_after_backup(self, project: Path, install_dir: Path) -> None:
        """OSError on open() reading hooks.json (after backup succeeds) returns error.

        We need copy2 (backup) to succeed, but the subsequent open() for reading
        to fail with OSError. We achieve this by making the file unreadable AFTER
        the backup copy is made, using a patched copy2 that does the real copy
        then changes permissions.
        """
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        hooks_file = cursor_path / "hooks.json"
        hooks_file.write_text(json.dumps({"version": 1, "hooks": {}}))

        from shutil import copy2 as real_copy2

        def copy2_then_lock(src, dst, *args, **kwargs):
            """Do the real backup copy, then make the source unreadable."""
            result = real_copy2(src, dst, *args, **kwargs)
            # Make hooks_file unreadable so the next open() fails
            hooks_file.chmod(0o000)
            return result

        try:
            with (
                patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
                patch(
                    "gobby.cli.installers.cursor.install_shared_content",
                    return_value={
                        "workflows": [],
                        "agents": [],
                        "plugins": [],
                        "prompts": [],
                        "docs": [],
                    },
                ),
                patch("gobby.cli.installers.cursor.copy2", side_effect=copy2_then_lock),
            ):
                result = install_cursor(project, mode="project")

            assert result["success"] is False
            assert "Failed to read hooks.json" in result["error"]
        finally:
            hooks_file.chmod(0o644)

    def test_atomic_write_failure_backup_restore_also_fails(
        self, project: Path, install_dir: Path
    ) -> None:
        """When atomic write fails AND backup restore also fails."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        original_hooks = {"version": 1, "hooks": {"custom": [{"command": "echo hi"}]}}
        hooks_file = cursor_path / "hooks.json"
        hooks_file.write_text(json.dumps(original_hooks))

        # copy2 is used both for backup (should succeed) and restore (should fail).
        # We need backup to succeed but restore to fail.
        copy2_call_count = 0
        from shutil import copy2 as real_copy2

        def patched_copy2(src, dst, *args, **kwargs):
            nonlocal copy2_call_count
            copy2_call_count += 1
            if copy2_call_count == 1:
                # First call: backup - let it succeed
                return real_copy2(src, dst, *args, **kwargs)
            else:
                # Second call: restore - make it fail
                raise OSError("Restore failed")

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
            patch(
                "gobby.cli.installers.cursor.tempfile.mkstemp",
                side_effect=OSError("Disk full"),
            ),
            patch("gobby.cli.installers.cursor.copy2", side_effect=patched_copy2),
        ):
            result = install_cursor(project, mode="project")

        assert result["success"] is False
        assert "Failed to write hooks.json" in result["error"]

    def test_atomic_write_inner_exception_cleans_temp(
        self, project: Path, install_dir: Path
    ) -> None:
        """Inner exception during atomic write cleans up temp file."""
        import tempfile as real_tempfile

        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)

        with (
            patch("gobby.cli.installers.cursor.get_install_dir", return_value=install_dir),
            patch(
                "gobby.cli.installers.cursor.install_shared_content",
                return_value={
                    "workflows": [],
                    "agents": [],
                    "plugins": [],
                    "prompts": [],
                    "docs": [],
                },
            ),
        ):
            # Create a real temp file then make os.fdopen fail
            fd, temp_path = real_tempfile.mkstemp(
                dir=str(cursor_path), suffix=".tmp", prefix="hooks_"
            )
            os.close(fd)

            with (
                patch(
                    "gobby.cli.installers.cursor.tempfile.mkstemp",
                    return_value=(999, temp_path),
                ),
                patch("gobby.cli.installers.cursor.os.fdopen", side_effect=OSError("Bad fd")),
            ):
                result = install_cursor(project, mode="project")

        assert result["success"] is False
        # Temp file should be cleaned up
        assert not os.path.exists(temp_path)


# ---------------------------------------------------------------------------
# uninstall_cursor
# ---------------------------------------------------------------------------


class TestUninstallCursor:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        p = tmp_path / "project"
        p.mkdir()
        return p

    def test_uninstall_removes_dispatcher_and_hooks(self, project: Path) -> None:
        """Full uninstall removes dispatcher and Gobby hooks."""
        cursor_path = project / ".cursor"
        hooks_dir = cursor_path / "hooks"
        hooks_dir.mkdir(parents=True)

        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("# dispatcher")

        hooks_config = {
            "version": 1,
            "hooks": {
                "file_saved": [{"command": "python .cursor/hooks/hook_dispatcher.py file_saved"}],
                "context_loading": [
                    {"command": "python .cursor/hooks/hook_dispatcher.py context_loading"}
                ],
                "custom_hook": [{"command": "echo custom", "events": ["custom"]}],
            },
        }
        (cursor_path / "hooks.json").write_text(json.dumps(hooks_config))

        result = uninstall_cursor(project)

        assert result["success"] is True
        assert result["error"] is None
        assert not dispatcher.exists()
        assert str(dispatcher) in result["files_removed"]
        assert "file_saved" in result["hooks_removed"]
        assert "context_loading" in result["hooks_removed"]
        assert "custom_hook" not in result["hooks_removed"]

        # Verify hooks.json was updated
        with open(cursor_path / "hooks.json") as f:
            data = json.load(f)
        assert "file_saved" not in data["hooks"]
        assert "custom_hook" in data["hooks"]

    def test_uninstall_no_cursor_dir(self, project: Path) -> None:
        """Uninstall succeeds when .cursor doesn't exist."""
        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["files_removed"] == []
        assert result["hooks_removed"] == []

    def test_uninstall_no_hooks_json(self, project: Path) -> None:
        """Uninstall succeeds when hooks.json doesn't exist."""
        hooks_dir = project / ".cursor" / "hooks"
        hooks_dir.mkdir(parents=True)

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["hooks_removed"] == []

    def test_uninstall_dispatcher_does_not_exist(self, project: Path) -> None:
        """No error when dispatcher doesn't exist."""
        hooks_dir = project / ".cursor" / "hooks"
        hooks_dir.mkdir(parents=True)

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["files_removed"] == []

    def test_uninstall_corrupt_hooks_json(self, project: Path) -> None:
        """Corrupt hooks.json is handled gracefully."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text("{ broken json !!!")

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["hooks_removed"] == []

    def test_uninstall_hooks_json_oserror(self, project: Path) -> None:
        """OSError reading hooks.json is handled gracefully."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        hooks_file = cursor_path / "hooks.json"
        hooks_file.write_text(json.dumps({"version": 1, "hooks": {}}))
        hooks_file.chmod(0o000)

        try:
            result = uninstall_cursor(project)
            assert result["success"] is True
        finally:
            hooks_file.chmod(0o644)

    def test_uninstall_preserves_non_gobby_hooks(self, project: Path) -> None:
        """Hooks not referencing hook_dispatcher.py are preserved."""
        cursor_path = project / ".cursor"
        hooks_dir = cursor_path / "hooks"
        hooks_dir.mkdir(parents=True)

        hooks_config = {
            "version": 1,
            "hooks": {
                "user_hook_a": [{"command": "echo a", "events": ["a"]}],
                "user_hook_b": [{"command": "echo b", "events": ["b"]}],
            },
        }
        (cursor_path / "hooks.json").write_text(json.dumps(hooks_config))

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["hooks_removed"] == []

        with open(cursor_path / "hooks.json") as f:
            data = json.load(f)
        assert "user_hook_a" in data["hooks"]
        assert "user_hook_b" in data["hooks"]

    def test_uninstall_hooks_not_list_skipped(self, project: Path) -> None:
        """Hook entries that are not lists are skipped during uninstall."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)

        hooks_config = {
            "version": 1,
            "hooks": {
                "dict_hook": {"command": "python hook_dispatcher.py"},
                "list_hook": [{"command": "python hook_dispatcher.py list_hook"}],
            },
        }
        (cursor_path / "hooks.json").write_text(json.dumps(hooks_config))

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert "list_hook" in result["hooks_removed"]
        assert "dict_hook" not in result["hooks_removed"]

    def test_uninstall_dispatcher_removal_failure(self, project: Path) -> None:
        """Continues when dispatcher file removal fails."""
        cursor_path = project / ".cursor"
        hooks_dir = cursor_path / "hooks"
        hooks_dir.mkdir(parents=True)
        dispatcher = hooks_dir / "hook_dispatcher.py"
        dispatcher.write_text("# dispatcher")

        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            result = uninstall_cursor(project)

        assert result["success"] is True

    def test_uninstall_result_structure(self, project: Path) -> None:
        """Result dictionary has expected structure."""
        result = uninstall_cursor(project)
        expected_keys = {"success", "hooks_removed", "files_removed", "error"}
        assert set(result.keys()) == expected_keys
        assert isinstance(result["success"], bool)
        assert isinstance(result["hooks_removed"], list)
        assert isinstance(result["files_removed"], list)

    def test_uninstall_empty_hooks_section(self, project: Path) -> None:
        """Uninstall with empty hooks section."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {}}))

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["hooks_removed"] == []

    def test_uninstall_no_hooks_key_in_json(self, project: Path) -> None:
        """Uninstall when hooks.json has no 'hooks' key."""
        cursor_path = project / ".cursor"
        cursor_path.mkdir(parents=True)
        (cursor_path / "hooks.json").write_text(json.dumps({"version": 1}))

        result = uninstall_cursor(project)
        assert result["success"] is True
        assert result["hooks_removed"] == []
