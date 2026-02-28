"""Tests for project CLI commands.

Tests cover:
- Listing projects (empty, with data, JSON format, --all flag)
- Showing project details (by ID, by name, not found, JSON format)
- Renaming projects (success, protected, reserved name, name conflict)
- Deleting projects (success, protected, confirmation mismatch)
- Updating projects (success, no fields)
- Repairing projects (no issues, mismatches, --fix)
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_project():
    """Create a mock project with common attributes."""
    project = MagicMock()
    project.id = "proj-abc123"
    project.name = "test-project"
    project.repo_path = "/home/user/projects/test-project"
    project.github_url = "https://github.com/user/test-project"
    project.github_repo = "user/test-project"
    project.linear_team_id = None
    project.deleted_at = None
    project.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    project.updated_at = datetime(2024, 1, 15, 14, 30, 0, tzinfo=UTC)
    project.to_dict.return_value = {
        "id": "proj-abc123",
        "name": "test-project",
        "repo_path": "/home/user/projects/test-project",
        "github_url": "https://github.com/user/test-project",
        "github_repo": "user/test-project",
        "created_at": "2024-01-01T12:00:00+00:00",
        "updated_at": "2024-01-15T14:30:00+00:00",
    }
    return project


class TestListProjects:
    """Tests for gobby projects list command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_projects_empty(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list with no projects found."""
        mock_manager = MagicMock()
        mock_manager.list.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list"])

        assert result.exit_code == 0
        assert "No projects found" in result.output
        assert "gobby init" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_projects_with_data(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test list with multiple projects."""
        project2 = MagicMock()
        project2.id = "proj-def456"
        project2.name = "another-project"
        project2.repo_path = "/home/user/projects/another"

        mock_manager = MagicMock()
        mock_manager.list.return_value = [mock_project, project2]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list"])

        assert result.exit_code == 0
        assert "Found 2 project(s)" in result.output
        assert "test-project" in result.output
        assert "another-project" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_projects_json(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test list with JSON output format."""
        mock_manager = MagicMock()
        mock_manager.list.return_value = [mock_project]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert len(output) == 1
        assert output[0]["name"] == "test-project"

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_projects_without_repo_path(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list with project that has no repo_path."""
        project = MagicMock()
        project.id = "proj-no-path"
        project.name = "no-path-project"
        project.repo_path = None

        mock_manager = MagicMock()
        mock_manager.list.return_value = [project]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list"])

        assert result.exit_code == 0
        assert "no-path-project" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_hides_system_projects_by_default(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test that system projects (prefixed with _) are hidden by default."""
        orphaned = MagicMock()
        orphaned.id = "00000000-0000-0000-0000-000000000000"
        orphaned.name = "_orphaned"
        orphaned.repo_path = None

        mock_manager = MagicMock()
        mock_manager.list.return_value = [orphaned, mock_project]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list"])

        assert result.exit_code == 0
        assert "_orphaned" not in result.output
        assert "test-project" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_list_shows_system_projects_with_all(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test that --all flag includes system projects."""
        orphaned = MagicMock()
        orphaned.id = "00000000-0000-0000-0000-000000000000"
        orphaned.name = "_orphaned"
        orphaned.repo_path = None

        mock_manager = MagicMock()
        mock_manager.list.return_value = [orphaned, mock_project]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "list", "--all"])

        assert result.exit_code == 0
        assert "_orphaned" in result.output
        assert "test-project" in result.output


class TestShowProject:
    """Tests for gobby projects show command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_show_project_by_id(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test showing project by UUID."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "show", "proj-abc123"])

        assert result.exit_code == 0
        assert "Project: test-project" in result.output
        assert "ID: proj-abc123" in result.output
        assert "Path:" in result.output
        assert "GitHub:" in result.output
        assert "Repo:" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_show_project_not_found(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing project when not found."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "show", "nonexistent"])

        assert result.exit_code == 1
        assert "Project not found: nonexistent" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_show_project_json(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test showing project with JSON output format."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "show", "proj-abc123", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["name"] == "test-project"
        assert output["id"] == "proj-abc123"

    @patch("gobby.cli.projects.get_project_manager")
    def test_show_project_minimal_info(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing project with minimal information (no github)."""
        project = MagicMock()
        project.id = "proj-minimal"
        project.name = "minimal-project"
        project.repo_path = "/home/user/minimal"
        project.github_url = None
        project.github_repo = None
        project.linear_team_id = None
        project.deleted_at = None
        project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        project.updated_at = datetime(2024, 1, 1, tzinfo=UTC)

        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "show", "proj-minimal"])

        assert result.exit_code == 0
        assert "Project: minimal-project" in result.output
        # Should not show GitHub fields
        assert "GitHub:" not in result.output
        assert "Repo:" not in result.output


class TestRenameProject:
    """Tests for gobby projects rename command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_rename_success(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test successful rename."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.is_protected.return_value = False
        mock_manager.get_by_name.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "rename", "test-project", "new-name"])

        assert result.exit_code == 0
        assert "Renamed 'test-project' -> 'new-name'" in result.output
        mock_manager.update.assert_called_once_with(mock_project.id, name="new-name")

    @patch("gobby.cli.projects.get_project_manager")
    def test_rename_protected_project(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test renaming a protected project fails."""
        project = MagicMock()
        project.name = "_orphaned"

        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = project
        mock_manager.is_protected.return_value = True
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "rename", "_orphaned", "new-name"])

        assert result.exit_code == 1
        assert "Cannot rename protected project" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_rename_to_reserved_name(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test renaming to a reserved name fails."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.is_protected.return_value = False
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "rename", "test-project", "_orphaned"])

        assert result.exit_code == 1
        assert "Cannot use reserved name" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_rename_name_conflict(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test renaming to an existing name fails."""
        existing = MagicMock()
        existing.name = "taken-name"

        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.is_protected.return_value = False
        mock_manager.get_by_name.return_value = existing
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "rename", "test-project", "taken-name"])

        assert result.exit_code == 1
        assert "already exists" in result.output


class TestDeleteProject:
    """Tests for gobby projects delete command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_delete_success(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test successful delete with correct confirmation."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.is_protected.return_value = False
        mock_manager.soft_delete.return_value = True
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli, ["projects", "delete", "test-project", "--confirm=test-project"]
        )

        assert result.exit_code == 0
        assert "Deleted project: test-project" in result.output
        mock_manager.soft_delete.assert_called_once_with(mock_project.id)

    @patch("gobby.cli.projects.get_project_manager")
    def test_delete_protected_project(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test deleting a protected project fails."""
        project = MagicMock()
        project.name = "gobby"

        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = project
        mock_manager.is_protected.return_value = True
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "delete", "gobby", "--confirm=gobby"])

        assert result.exit_code == 1
        assert "Cannot delete protected project" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_delete_confirmation_mismatch(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test delete with wrong confirmation name."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.is_protected.return_value = False
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "delete", "test-project", "--confirm=wrong-name"])

        assert result.exit_code == 1
        assert "Confirmation mismatch" in result.output


class TestUpdateProject:
    """Tests for gobby projects update command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_update_github_url(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test updating github URL."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.update.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["projects", "update", "test-project", "--github-url", "https://github.com/new/url"],
        )

        assert result.exit_code == 0
        assert "Updated project" in result.output
        mock_manager.update.assert_called_once_with(
            mock_project.id, github_url="https://github.com/new/url"
        )

    @patch("gobby.cli.projects.get_project_manager")
    def test_update_no_fields(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test update with no fields provided."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["projects", "update", "test-project"])

        assert result.exit_code == 0
        assert "No fields to update" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_update_multiple_fields(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        """Test updating multiple fields at once."""
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.update.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "projects",
                "update",
                "test-project",
                "--github-repo",
                "user/repo",
                "--linear-team-id",
                "TEAM-123",
            ],
        )

        assert result.exit_code == 0
        assert "Updated project" in result.output
        mock_manager.update.assert_called_once_with(
            mock_project.id, github_repo="user/repo", linear_team_id="TEAM-123"
        )


class TestRepairProject:
    """Tests for gobby projects repair command."""

    @patch("gobby.cli.projects.get_project_manager")
    def test_repair_no_project_json(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        """Test repair when no project.json exists."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["projects", "repair"])

        assert result.exit_code == 1
        assert "No .gobby/project.json found" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_repair_no_issues(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test repair when everything is consistent."""
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            os.makedirs(".gobby")
            cwd = os.path.realpath(td)
            with open(".gobby/project.json", "w") as f:
                json.dump({"id": "proj-123", "name": "my-project"}, f)

            db_project = MagicMock()
            db_project.name = "my-project"
            db_project.repo_path = cwd

            mock_manager = MagicMock()
            mock_manager.get.return_value = db_project
            mock_get_manager.return_value = mock_manager

            result = runner.invoke(cli, ["projects", "repair"])

        assert result.exit_code == 0
        assert "No issues found" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_repair_detects_path_mismatch(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test repair detects repo_path mismatch."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            os.makedirs(".gobby")
            with open(".gobby/project.json", "w") as f:
                json.dump({"id": "proj-123", "name": "my-project"}, f)

            db_project = MagicMock()
            db_project.name = "my-project"
            db_project.repo_path = "/some/other/path"

            mock_manager = MagicMock()
            mock_manager.get.return_value = db_project
            mock_get_manager.return_value = mock_manager

            result = runner.invoke(cli, ["projects", "repair"])

        assert result.exit_code == 0
        assert "repo_path mismatch" in result.output
        assert "Run with --fix" in result.output

    @patch("gobby.cli.projects.get_project_manager")
    def test_repair_fix_applies_corrections(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test repair --fix applies corrections."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            os.makedirs(".gobby")
            with open(".gobby/project.json", "w") as f:
                json.dump({"id": "proj-123", "name": "my-project"}, f)

            db_project = MagicMock()
            db_project.id = "proj-123"
            db_project.name = "my-project"
            db_project.repo_path = "/some/other/path"

            mock_manager = MagicMock()
            mock_manager.get.return_value = db_project
            mock_get_manager.return_value = mock_manager

            result = runner.invoke(cli, ["projects", "repair", "--fix"])

        assert result.exit_code == 0
        assert "Applied 1 fix(es)" in result.output
        mock_manager.update.assert_called_once()
