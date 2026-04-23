"""Tests for gobby.sync.integrity module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.sync.integrity import (
    CONTENT_TYPE_DIRS,
    IntegrityResult,
    get_dirty_content_types,
    verify_bundled_integrity,
)

pytestmark = pytest.mark.unit


class TestIntegrityResult:
    """Tests for the IntegrityResult dataclass."""

    def test_all_clean_when_empty(self) -> None:
        result = IntegrityResult()
        assert result.all_clean is True

    def test_not_clean_with_dirty_files(self) -> None:
        result = IntegrityResult(dirty_files=["some/file.yaml"])
        assert result.all_clean is False

    def test_not_clean_with_untracked_files(self) -> None:
        result = IntegrityResult(untracked_files=["some/new.yaml"])
        assert result.all_clean is False

    def test_defaults(self) -> None:
        result = IntegrityResult()
        assert result.clean_files == []
        assert result.dirty_files == []
        assert result.untracked_files == []
        assert result.errors == []
        assert result.git_available is True


class TestVerifyBundledIntegrity:
    """Tests for verify_bundled_integrity."""

    def test_missing_shared_dir(self, tmp_path: Path) -> None:
        """Returns error when shared/ dir doesn't exist."""
        result = verify_bundled_integrity(tmp_path)
        assert result.git_available is False
        assert len(result.errors) == 1
        assert "Shared directory not found" in result.errors[0]

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        """Returns git_available=False when not in a git repo."""
        shared = tmp_path / "shared"
        shared.mkdir()

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is False
        assert result.all_clean is True

    def test_clean_repo(self, tmp_path: Path) -> None:
        """All files clean when git reports no changes."""
        shared = tmp_path / "shared"
        shared.mkdir()

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                # Tracked files
                return "src/gobby/install/shared/workflows/default.yaml"
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is True
        assert result.all_clean is True
        assert len(result.dirty_files) == 0
        assert len(result.untracked_files) == 0

    def test_dirty_files_detected(self, tmp_path: Path) -> None:
        """Detects modified tracked files."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty_file = "src/gobby/install/shared/workflows/default.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd and "--cached" not in cmd:
                return dirty_file
            if "diff" in cmd and "--cached" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                return dirty_file
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.git_available is True
        assert result.all_clean is False
        assert dirty_file in result.dirty_files

    def test_untracked_files_detected(self, tmp_path: Path) -> None:
        """Detects untracked files in shared content dirs."""
        shared = tmp_path / "shared"
        shared.mkdir()

        untracked_file = "src/gobby/install/shared/skills/evil.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return untracked_file
                return ""
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.all_clean is False
        assert untracked_file in result.untracked_files

    def test_staged_changes_detected(self, tmp_path: Path) -> None:
        """Staged (cached) changes are also detected as dirty."""
        shared = tmp_path / "shared"
        shared.mkdir()

        staged_file = "src/gobby/install/shared/prompts/system.yaml"

        def mock_git(cmd: list[str], cwd: str | Path, **kwargs: object) -> str | None:
            if "rev-parse" in cmd and "--show-toplevel" in cmd:
                return str(tmp_path)
            if "diff" in cmd and "--cached" in cmd:
                return staged_file
            if "diff" in cmd:
                return ""
            if "ls-files" in cmd:
                if "--others" in cmd:
                    return ""
                return staged_file
            return None

        with patch("gobby.sync.integrity.run_git_command", side_effect=mock_git):
            result = verify_bundled_integrity(tmp_path)

        assert result.all_clean is False
        assert staged_file in result.dirty_files


class TestGetDirtyContentTypes:
    """Tests for get_dirty_content_types."""

    def test_maps_pipeline_paths(self, tmp_path: Path) -> None:
        """Maps pipeline file paths to 'pipelines' content type."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = ["shared/workflows/pipelines/default.yaml"]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert "pipelines" in result

    def test_maps_multiple_types(self, tmp_path: Path) -> None:
        """Maps files across multiple content type directories."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = [
            "shared/workflows/pipelines/default.yaml",
            "shared/skills/evil.yaml",
            "shared/workflows/agents/rogue.yaml",
        ]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert result == {"pipelines", "skills", "agents"}

    def test_ignores_non_content_dirs(self, tmp_path: Path) -> None:
        """Files in non-content dirs (hooks, plugins) are ignored."""
        shared = tmp_path / "shared"
        shared.mkdir()

        dirty = [
            "shared/hooks/validate_settings.py",
            "shared/plugins/foo.py",
        ]

        with patch("gobby.sync.integrity.run_git_command", return_value=str(tmp_path)):
            result = get_dirty_content_types(dirty, tmp_path)

        assert result == set()

    def test_no_git_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty set when git is not available."""
        shared = tmp_path / "shared"
        shared.mkdir()

        with patch("gobby.sync.integrity.run_git_command", return_value=None):
            result = get_dirty_content_types(["shared/workflows/pipelines/x.yaml"], tmp_path)

        assert result == set()

    def test_content_type_dirs_matches_sync_targets(self) -> None:
        """CONTENT_TYPE_DIRS covers all DB-synced content types."""
        expected = {"skills", "prompts", "rules", "agents", "variables", "pipelines"}
        assert set(CONTENT_TYPE_DIRS.values()) == expected
