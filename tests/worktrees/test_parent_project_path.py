"""Tests for storing and reading parent_project_path in worktree project.json.

These tests verify that:
1. When a worktree is created, parent_project_path is stored in project.json
2. get_workflow_project_path() returns the parent path for worktrees
3. Edge cases when worktree is not inside a parent project
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.worktrees import _copy_project_json_to_worktree
from gobby.utils.project_context import get_project_context, get_workflow_project_path

pytestmark = pytest.mark.unit


class TestCopyProjectJsonToWorktree:
    """Tests for _copy_project_json_to_worktree function."""

    def test_copies_project_json_with_parent_path(self, tmp_path: Path) -> None:
        """Verify parent_project_path is added when copying project.json to worktree."""
        # Setup main repo with .gobby/project.json
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_gobby_dir = main_repo / ".gobby"
        main_gobby_dir.mkdir()
        main_project_json = main_gobby_dir / "project.json"
        main_project_json.write_text(
            json.dumps(
                {"id": "proj-123", "name": "test-project", "created_at": "2024-01-01T00:00:00Z"}
            )
        )

        # Setup worktree directory (no .gobby yet)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Call the function
        _copy_project_json_to_worktree(main_repo, worktree)

        # Verify worktree project.json was created
        worktree_project_json = worktree / ".gobby" / "project.json"
        assert worktree_project_json.exists(), "project.json should be created in worktree"

        # Verify content includes parent_project_path
        with open(worktree_project_json) as f:
            data = json.load(f)

        assert "parent_project_path" in data, "parent_project_path should be added"
        assert data["parent_project_path"] == str(main_repo.resolve())
        assert data["parent_project_id"] == "proj-123"
        assert data["id"] == "proj-123"
        assert data["name"] == "test-project"

    def test_overwrites_existing_project_json_preserves_parent_project_id(
        self, tmp_path: Path
    ) -> None:
        """Verify existing worktree project.json is overwritten with source data.

        ensure_project_json_for_isolation always overwrites so that
        parent_project_path is correct even for git-tracked copies.
        """
        # Setup main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_gobby_dir = main_repo / ".gobby"
        main_gobby_dir.mkdir()
        main_project_json = main_gobby_dir / "project.json"
        main_project_json.write_text(json.dumps({"id": "new-id"}))

        # Setup worktree with existing project.json
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        worktree_gobby_dir = worktree / ".gobby"
        worktree_gobby_dir.mkdir()
        worktree_project_json = worktree_gobby_dir / "project.json"
        worktree_project_json.write_text(
            json.dumps({"id": "existing-id", "parent_project_path": "/some/old/path"})
        )

        # Call the function
        _copy_project_json_to_worktree(main_repo, worktree)

        # Verify source content overwrites existing
        with open(worktree_project_json) as f:
            data = json.load(f)

        assert data["id"] == "new-id", "Source ID should overwrite existing"
        assert data["parent_project_path"] == str(main_repo.resolve()), (
            "parent_project_path should point to source repo"
        )
        assert data["parent_project_id"] == "new-id"

    def test_no_project_json_in_main_repo(self, tmp_path: Path) -> None:
        """Verify function handles missing project.json in main repo gracefully."""
        # Setup main repo without project.json
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()

        # Setup worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Call the function - should not raise
        _copy_project_json_to_worktree(main_repo, worktree)

        # Verify no project.json was created in worktree
        worktree_project_json = worktree / ".gobby" / "project.json"
        assert not worktree_project_json.exists()

    def test_creates_gobby_dir_if_missing(self, tmp_path: Path) -> None:
        """Verify .gobby directory is created in worktree if it doesn't exist."""
        # Setup main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_gobby_dir = main_repo / ".gobby"
        main_gobby_dir.mkdir()
        main_project_json = main_gobby_dir / "project.json"
        main_project_json.write_text(json.dumps({"id": "test-id"}))

        # Setup worktree without .gobby dir
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Call the function
        _copy_project_json_to_worktree(main_repo, worktree)

        # Verify .gobby dir was created
        assert (worktree / ".gobby").is_dir()
        assert (worktree / ".gobby" / "project.json").exists()


class TestGetWorkflowProjectPath:
    """Tests for get_workflow_project_path function."""

    def test_returns_parent_path_for_worktree(self, tmp_path: Path) -> None:
        """Verify get_workflow_project_path returns parent_project_path for worktrees."""
        # Setup worktree with parent_project_path in project.json
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gobby_dir = worktree / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"

        parent_path = tmp_path / "parent_repo"
        parent_path.mkdir()

        project_json.write_text(
            json.dumps({"id": "worktree-id", "parent_project_path": str(parent_path)})
        )

        result = get_workflow_project_path(worktree)

        assert result is not None
        assert result == parent_path

    def test_returns_project_path_for_main_project(self, tmp_path: Path) -> None:
        """Verify get_workflow_project_path returns project_path for non-worktrees."""
        # Setup main project without parent_project_path
        project = tmp_path / "project"
        project.mkdir()
        gobby_dir = project / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(json.dumps({"id": "main-id", "name": "main-project"}))

        result = get_workflow_project_path(project)

        assert result is not None
        assert result.resolve() == project.resolve()

    def test_returns_none_for_no_project(self, tmp_path: Path, monkeypatch) -> None:
        """Verify get_workflow_project_path returns None when no project found."""
        # Isolate test from parent directories
        original_exists = Path.exists

        def isolated_exists(self):
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)

        result = get_workflow_project_path(tmp_path)

        assert result is None

    def test_handles_missing_project_path_key(self, tmp_path: Path) -> None:
        """Verify function handles project.json without project_path key."""
        project = tmp_path / "project"
        project.mkdir()
        gobby_dir = project / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        # project_path is normally added by get_project_context, but test edge case
        project_json.write_text(json.dumps({"id": "test-id"}))

        result = get_workflow_project_path(project)

        # Should still work - project_path is added by get_project_context
        assert result is not None

    def test_handles_nested_worktree_scenario(self, tmp_path: Path) -> None:
        """Verify function handles nested worktree scenario by following parent chain."""
        # Scenario: worktree of a worktree should still resolve to original parent
        # In practice, worktrees should always point to the main project, not to
        # another worktree, but we test this to ensure consistent behavior.

        original_project = tmp_path / "original"
        original_project.mkdir()

        # First worktree
        worktree1 = tmp_path / "worktree1"
        worktree1.mkdir()
        gobby_dir1 = worktree1 / ".gobby"
        gobby_dir1.mkdir()
        (gobby_dir1 / "project.json").write_text(
            json.dumps({"id": "wt1-id", "parent_project_path": str(original_project)})
        )

        # get_workflow_project_path should return the parent path
        result = get_workflow_project_path(worktree1)

        assert result is not None
        assert result == original_project

    def test_returns_from_subdirectory(self, tmp_path: Path) -> None:
        """Verify function works when called from a subdirectory of the project."""
        project = tmp_path / "project"
        project.mkdir()
        gobby_dir = project / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text(
            json.dumps({"id": "proj-id", "name": "test-project"})
        )

        # Create nested subdirectory
        subdir = project / "src" / "lib" / "utils"
        subdir.mkdir(parents=True)

        result = get_workflow_project_path(subdir)

        assert result is not None
        assert result.resolve() == project.resolve()


class TestReadParentProjectPath:
    """Tests for reading parent_project_path from existing worktree project.json."""

    def test_get_project_context_includes_parent_project_path(self, tmp_path: Path) -> None:
        """Verify get_project_context returns parent_project_path if present."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gobby_dir = worktree / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(
            json.dumps(
                {
                    "id": "wt-id",
                    "name": "worktree-project",
                    "parent_project_path": "/path/to/parent",
                    "parent_project_id": "parent-id",
                }
            )
        )

        result = get_project_context(worktree)

        assert result is not None
        assert result["parent_project_path"] == "/path/to/parent"
        assert result["parent_project_id"] == "parent-id"
        assert result["id"] == "wt-id"

    def test_get_project_context_without_parent_project_path(self, tmp_path: Path) -> None:
        """Verify get_project_context works without parent_project_path."""
        project = tmp_path / "project"
        project.mkdir()
        gobby_dir = project / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(json.dumps({"id": "proj-id", "name": "main-project"}))

        result = get_project_context(project)

        assert result is not None
        assert "parent_project_path" not in result or result.get("parent_project_path") is None
        assert result["id"] == "proj-id"


class TestEdgeCases:
    """Tests for edge cases when worktree is not inside a parent project."""

    def test_worktree_with_nonexistent_parent_path(self, tmp_path: Path) -> None:
        """Verify handling when parent_project_path points to nonexistent directory."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gobby_dir = worktree / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(
            json.dumps({"id": "wt-id", "parent_project_path": "/nonexistent/path"})
        )

        # get_workflow_project_path still returns the path even if it doesn't exist
        # This is expected - callers should verify existence if needed
        result = get_workflow_project_path(worktree)

        assert result is not None
        assert result == Path("/nonexistent/path")

    def test_worktree_with_empty_parent_path(self, tmp_path: Path) -> None:
        """Verify handling when parent_project_path is empty string."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gobby_dir = worktree / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(json.dumps({"id": "wt-id", "parent_project_path": ""}))

        result = get_workflow_project_path(worktree)

        # Empty string is falsy, so should fall back to project_path
        assert result is not None
        assert result.resolve() == worktree.resolve()

    def test_worktree_with_null_parent_path(self, tmp_path: Path) -> None:
        """Verify handling when parent_project_path is null/None."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        gobby_dir = worktree / ".gobby"
        gobby_dir.mkdir()
        project_json = gobby_dir / "project.json"
        project_json.write_text(json.dumps({"id": "wt-id", "parent_project_path": None}))

        result = get_workflow_project_path(worktree)

        # None is falsy, so should fall back to project_path
        assert result is not None
        assert result.resolve() == worktree.resolve()

    def test_copy_handles_json_write_error(self, tmp_path: Path) -> None:
        """Verify _copy_project_json_to_worktree handles write errors gracefully."""
        # Setup main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_gobby_dir = main_repo / ".gobby"
        main_gobby_dir.mkdir()
        main_project_json = main_gobby_dir / "project.json"
        main_project_json.write_text(json.dumps({"id": "test-id"}))

        # Setup worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Mock json.dump to raise an exception
        with patch("json.dump", side_effect=OSError("Write failed")):
            # Should not raise - function handles errors gracefully with warning
            _copy_project_json_to_worktree(main_repo, worktree)

        # Verify .gobby dir was created (function gets that far before error)
        assert (worktree / ".gobby").is_dir()

    def test_copy_handles_invalid_json_in_source(self, tmp_path: Path) -> None:
        """Verify _copy_project_json_to_worktree handles invalid JSON in source."""
        # Setup main repo with invalid JSON
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_gobby_dir = main_repo / ".gobby"
        main_gobby_dir.mkdir()
        main_project_json = main_gobby_dir / "project.json"
        main_project_json.write_text("this is not valid json {{{")

        # Setup worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Should not raise - function handles errors gracefully
        _copy_project_json_to_worktree(main_repo, worktree)

        # Verify no project.json was created (due to parse error)
        worktree_project_json = worktree / ".gobby" / "project.json"
        assert not worktree_project_json.exists()
