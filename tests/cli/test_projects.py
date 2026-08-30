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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

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
    project.linear_project_id = None
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
        assert "Path:" not in result.output
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
        project.linear_project_id = None
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

    @patch("gobby.cli.projects.get_project_manager")
    def test_update_linear_project_id(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        mock_manager = MagicMock()
        mock_manager.resolve_ref.return_value = mock_project
        mock_manager.update.return_value = mock_project
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["projects", "update", "test-project", "--linear-project-id", "LIN-PROJ"],
        )

        assert result.exit_code == 0
        mock_manager.update.assert_called_once_with(mock_project.id, linear_project_id="LIN-PROJ")


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


def _cli_text(result: Result) -> str:
    return f"{result.output}{result.stderr or ''}"


def _hub(db: object) -> Any:
    from gobby.storage.hub.protocol import HubDatabase

    return cast(HubDatabase, db)


def _pin_cli_machine(db: object, monkeypatch: pytest.MonkeyPatch) -> str:
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(_hub(db))
    patch_local_machine_id(monkeypatch, machine_id)
    return machine_id


def _invoke_projects(
    runner: CliRunner,
    db: object,
    args: list[str],
    *,
    cwd: Path | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> Result:
    from gobby.storage.projects import LocalProjectManager

    if cwd is not None:
        assert monkeypatch is not None
        monkeypatch.chdir(cwd)
    manager = LocalProjectManager(_hub(db))
    with patch("gobby.cli.projects.get_project_manager", lambda: manager):
        return runner.invoke(cli, ["projects", *args])


def _checkout_root(db: object, machine_id: str, project_id: str) -> str | None:
    from gobby.storage.project_checkouts import LocalProjectCheckoutManager

    row = LocalProjectCheckoutManager(_hub(db)).get(machine_id, project_id)
    return None if row is None else row.root_path


def _project_row(db: object, project_id: str) -> dict[str, Any]:
    row = _hub(db).fetchone(
        "SELECT repo_path, deleted_at, name FROM projects WHERE id = %s",
        (project_id,),
    )
    assert row is not None
    return dict(row)


def _read_marker(root: Path) -> dict[str, object]:
    loaded: object = json.loads((root / ".gobby" / "project.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _unique_name(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TestProjectRebindCli:
    """§ 2.3.1 / 2.3.2 gobby projects rebind."""

    def test_rebind_verifies_marker_and_updates_only_local_checkout(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import insert_isolated_machine, write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("rebind-move")
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(first, project_id=project.id, name=name)
        write_project_marker(second, project_id=project.id, name=name)
        checkouts = LocalProjectCheckoutManager(_hub(temp_db))
        checkouts.register(machine_id, project.id, str(first))
        foreign_machine = insert_isolated_machine(_hub(temp_db))
        checkouts.register(foreign_machine, project.id, "/foreign/root")

        result = _invoke_projects(runner, temp_db, ["rebind", name, str(second)])

        assert result.exit_code == 0
        assert _checkout_root(temp_db, machine_id, project.id) == str(second)
        assert _checkout_root(temp_db, foreign_machine, project.id) == "/foreign/root"
        assert _project_row(temp_db, project.id)["repo_path"] is None
        mismatch = tmp_path / "other"
        mismatch.mkdir()
        write_project_marker(mismatch, project_id=_unique_name("not-uuid"), name="other")
        refused = _invoke_projects(runner, temp_db, ["rebind", name, str(mismatch)])
        assert refused.exit_code == 1
        assert "does not match project" in _cli_text(refused)
        assert _checkout_root(temp_db, machine_id, project.id) == str(second)

    def test_update_rejects_removed_repo_path_option(
        self,
        runner: CliRunner,
        mock_project: MagicMock,
    ) -> None:
        with patch("gobby.cli.projects.get_project_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.resolve_ref.return_value = mock_project
            mock_get_manager.return_value = mock_manager
            result = runner.invoke(
                cli,
                ["projects", "update", "test-project", "--repo-path", "/tmp/moved"],
            )

        text = _cli_text(result).lower()
        assert result.exit_code != 0
        assert "no such option" in text
        assert "repo-path" in text
        mock_manager.update.assert_not_called()

    def test_list_and_show_print_local_checkout_separately(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("show-checkout")
        root = tmp_path / "repo"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        LocalProjectCheckoutManager(_hub(temp_db)).register(machine_id, project.id, str(root))

        listed = _invoke_projects(runner, temp_db, ["list"])
        assert listed.exit_code == 0
        list_text = _cli_text(listed)
        assert name in list_text
        assert str(root) in list_text
        listed_json = _invoke_projects(runner, temp_db, ["list", "--json"])
        assert listed_json.exit_code == 0
        payload = json.loads(listed_json.output)
        row = next(item for item in payload if item["name"] == name)
        assert "repo_path" not in row
        assert row["checkout"]["root_path"] == str(root)
        assert row["checkout"]["machine_id"] == machine_id

        shown = _invoke_projects(runner, temp_db, ["show", name])
        assert shown.exit_code == 0
        show_text = _cli_text(shown)
        assert f"Checkout: {root}" in show_text
        assert "Path:" not in show_text
        shown_json = _invoke_projects(runner, temp_db, ["show", name, "--json"])
        assert shown_json.exit_code == 0
        show_payload = json.loads(shown_json.output)
        assert "repo_path" not in show_payload
        assert show_payload["checkout"]["root_path"] == str(root)

    def test_rebind_unique_soft_deleted_preserves_deleted_at(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("deleted-rebind")
        root = tmp_path / "deleted-root"
        root.mkdir()
        manager = LocalProjectManager(_hub(temp_db))
        project = manager.create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        assert manager.soft_delete(project.id)

        by_name = _invoke_projects(runner, temp_db, ["rebind", name, str(root)])
        assert by_name.exit_code == 0
        stored = _project_row(temp_db, project.id)
        assert stored["deleted_at"] is not None
        assert stored["repo_path"] is None
        assert _checkout_root(temp_db, machine_id, project.id) == str(root)

        moved = tmp_path / "deleted-moved"
        moved.mkdir()
        write_project_marker(moved, project_id=project.id, name=name)
        by_uuid = _invoke_projects(runner, temp_db, ["rebind", project.id, str(moved)])
        assert by_uuid.exit_code == 0
        stored = _project_row(temp_db, project.id)
        assert stored["deleted_at"] is not None
        assert _checkout_root(temp_db, machine_id, project.id) == str(moved)

    def test_rebind_ambiguous_deleted_name_requires_uuid_or_path_marker(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("dup-deleted")
        manager = LocalProjectManager(_hub(temp_db))
        first = manager.create(name=name)
        assert manager.soft_delete(first.id)
        second = manager.create(name=name)
        assert manager.soft_delete(second.id)
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        empty_root = tmp_path / "empty"
        first_root.mkdir()
        second_root.mkdir()
        empty_root.mkdir()
        write_project_marker(first_root, project_id=first.id, name=name)
        write_project_marker(second_root, project_id=second.id, name=name)

        ambiguous = _invoke_projects(runner, temp_db, ["rebind", name, str(empty_root)])
        assert ambiguous.exit_code == 1
        assert "ambiguous" in _cli_text(ambiguous).lower()
        assert _checkout_root(temp_db, machine_id, first.id) is None
        assert _checkout_root(temp_db, machine_id, second.id) is None

        selected = _invoke_projects(runner, temp_db, ["rebind", name, str(second_root)])
        assert selected.exit_code == 0
        assert _checkout_root(temp_db, machine_id, second.id) == str(second_root)
        assert _checkout_root(temp_db, machine_id, first.id) is None
        assert _project_row(temp_db, second.id)["deleted_at"] is not None

        by_uuid = _invoke_projects(runner, temp_db, ["rebind", first.id, str(first_root)])
        assert by_uuid.exit_code == 0
        assert _checkout_root(temp_db, machine_id, first.id) == str(first_root)
        assert _project_row(temp_db, first.id)["deleted_at"] is not None


class TestProjectRepairCheckout:
    """§ 2.3.3 repair checkout/marker drift."""

    def test_repair_registers_missing_checkout_for_valid_same_root_marker(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-create")
        root = tmp_path / "repair-root"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)

        dry = _invoke_projects(runner, temp_db, ["repair"], cwd=root, monkeypatch=monkeypatch)
        assert dry.exit_code == 0
        assert _checkout_root(temp_db, machine_id, project.id) is None
        assert "missing" in _cli_text(dry).lower() or "register" in _cli_text(dry).lower()

        fixed = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=root, monkeypatch=monkeypatch
        )
        assert fixed.exit_code == 0
        assert "creat" in _cli_text(fixed).lower()
        assert _checkout_root(temp_db, machine_id, project.id) == str(root)
        assert _project_row(temp_db, project.id)["repo_path"] is None

    def test_repair_refuses_overlay_without_persist(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import insert_overlay, write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-overlay")
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(overlay, project_id=project.id, name=name)
        insert_overlay(
            _hub(temp_db),
            project_id=project.id,
            machine_id=machine_id,
            path=str(overlay),
            kind="worktree",
        )

        result = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=overlay, monkeypatch=monkeypatch
        )
        assert result.exit_code == 1
        assert _checkout_root(temp_db, machine_id, project.id) is None
        assert _project_row(temp_db, project.id)["repo_path"] is None

    def test_repair_refuses_sentinel_without_persist(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import ORPHANED_PROJECT_ID
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        root = tmp_path / "sentinel"
        root.mkdir()
        write_project_marker(root, project_id=ORPHANED_PROJECT_ID, name="_orphaned")

        result = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=root, monkeypatch=monkeypatch
        )
        assert result.exit_code == 1
        assert (
            "sentinel" in _cli_text(result).lower() or "checkout-free" in _cli_text(result).lower()
        )
        assert _checkout_root(temp_db, machine_id, ORPHANED_PROJECT_ID) is None

    def test_repair_refuses_marker_mismatch_without_persist(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import uuid

        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-mismatch")
        root = tmp_path / "mismatch"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        monkeypatch.setattr(
            "gobby.utils.checkout_root.get_project_context",
            lambda *_args, **_kwargs: {"id": str(uuid.uuid4()), "name": "other"},
        )

        result = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=root, monkeypatch=monkeypatch
        )
        assert result.exit_code == 1
        assert "does not match project" in _cli_text(result)
        assert _checkout_root(temp_db, machine_id, project.id) is None

    def test_repair_refuses_invalid_root_without_persist(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-invalid")
        root = tmp_path / "valid"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        monkeypatch.chdir(root)
        monkeypatch.setattr("os.getcwd", lambda: "not-abs-repair-root")

        result = _invoke_projects(runner, temp_db, ["repair", "--fix"])
        assert result.exit_code == 1
        assert "not a platform-local normalized absolute path" in _cli_text(result)
        assert _checkout_root(temp_db, machine_id, project.id) is None
        assert _project_row(temp_db, project.id)["repo_path"] is None

    def test_repair_refuses_different_root_and_tells_user_to_rebind(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-conflict")
        registered = tmp_path / "registered"
        other = tmp_path / "other"
        registered.mkdir()
        other.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(registered, project_id=project.id, name=name)
        write_project_marker(other, project_id=project.id, name=name)
        LocalProjectCheckoutManager(_hub(temp_db)).register(machine_id, project.id, str(registered))

        result = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=other, monkeypatch=monkeypatch
        )
        assert result.exit_code == 1
        assert "rebind" in _cli_text(result).lower()
        assert _checkout_root(temp_db, machine_id, project.id) == str(registered)
        assert _project_row(temp_db, project.id)["repo_path"] is None

    def test_repair_same_root_existing_reports_no_drift(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("repair-ok")
        root = tmp_path / "same"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        LocalProjectCheckoutManager(_hub(temp_db)).register(machine_id, project.id, str(root))

        result = _invoke_projects(
            runner, temp_db, ["repair", "--fix"], cwd=root, monkeypatch=monkeypatch
        )
        assert result.exit_code == 0
        assert "no drift" in _cli_text(result).lower() or "no issues" in _cli_text(result).lower()
        assert _checkout_root(temp_db, machine_id, project.id) == str(root)
        assert _project_row(temp_db, project.id)["repo_path"] is None


class TestProjectRenameCheckout:
    """§ 2.3.4 rename commits projects.name and best-effort marker refresh."""

    def test_rename_commits_name_without_local_checkout(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("rename-free")
        new_name = _unique_name("renamed-free")
        stale = tmp_path / "stale-repo-path"
        stale.mkdir()
        manager = LocalProjectManager(_hub(temp_db))
        project = manager.create(name=name)
        write_project_marker(stale, project_id=project.id, name=name)
        _hub(temp_db).execute(
            "UPDATE projects SET repo_path = %s WHERE id = %s",
            (str(stale), project.id),
        )

        result = _invoke_projects(runner, temp_db, ["rename", name, new_name])
        assert result.exit_code == 0
        stored = _project_row(temp_db, project.id)
        assert stored["name"] == new_name
        assert _read_marker(stale)["name"] == name

    def test_rename_refreshes_only_local_marker_after_commit(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("rename-local")
        new_name = _unique_name("renamed-local")
        root = tmp_path / "checkout"
        stale = tmp_path / "stale"
        root.mkdir()
        stale.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        write_project_marker(stale, project_id=project.id, name=name)
        marker = root / ".gobby" / "project.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["created_at"] = "2024-01-02T00:00:00Z"
        payload["extra_field"] = "keep-me"
        marker.write_text(json.dumps(payload), encoding="utf-8")
        LocalProjectCheckoutManager(_hub(temp_db)).register(machine_id, project.id, str(root))
        _hub(temp_db).execute(
            "UPDATE projects SET repo_path = %s WHERE id = %s",
            (str(stale), project.id),
        )

        result = _invoke_projects(runner, temp_db, ["rename", name, new_name])
        assert result.exit_code == 0
        assert _project_row(temp_db, project.id)["name"] == new_name
        refreshed = _read_marker(root)
        assert refreshed["name"] == new_name
        assert refreshed["id"] == project.id
        assert refreshed["created_at"] == "2024-01-02T00:00:00Z"
        assert refreshed["extra_field"] == "keep-me"
        assert _read_marker(stale)["name"] == name

    def test_rename_warns_on_marker_mismatch_leaving_database_name_changed(
        self,
        runner: CliRunner,
        tmp_path: Path,
        temp_db: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import uuid

        import gobby.utils.project_init as project_init
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager
        from gobby.storage.projects import LocalProjectManager
        from tests.fixtures.isolated_checkout import write_project_marker

        machine_id = _pin_cli_machine(temp_db, monkeypatch)
        name = _unique_name("rename-warn")
        new_name = _unique_name("renamed-warn")
        root = tmp_path / "checkout"
        root.mkdir()
        project = LocalProjectManager(_hub(temp_db)).create(name=name)
        write_project_marker(root, project_id=project.id, name=name)
        LocalProjectCheckoutManager(_hub(temp_db)).register(machine_id, project.id, str(root))
        replacement_id = str(uuid.uuid4())

        def replace_marker() -> None:
            write_project_marker(root, project_id=replacement_id, name="replacement")

        monkeypatch.setitem(
            project_init._INIT_FAILPOINTS, "refresh_after_temp_fsync", replace_marker
        )

        result = _invoke_projects(runner, temp_db, ["rename", name, new_name])
        assert result.exit_code == 0
        assert "warning" in _cli_text(result).lower()
        assert _project_row(temp_db, project.id)["name"] == new_name
        assert _read_marker(root)["id"] == replacement_id
        assert _read_marker(root)["name"] == "replacement"
