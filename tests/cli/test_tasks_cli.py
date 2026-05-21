"""Comprehensive tests for task CLI commands (ai.py and crud.py).

These tests focus on increasing coverage for the low-coverage CLI modules:
- tasks/ai.py (26% coverage)
- tasks/crud.py (35% coverage)

Tests use Click's CliRunner and mock external dependencies.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from click import ClickException
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.tasks._utils import config as task_config_utils

pytestmark = pytest.mark.unit


def _stage_row(state: str = "ready", name: str = "development") -> SimpleNamespace:
    return SimpleNamespace(stage_name=name, name=name, position=1, state=state)


def _set_task_state(task: MagicMock, state: str = "ready") -> None:
    task.current_stage = None
    task.stages = (
        _stage_row(state if state not in {"closed", "escalated", "blocked"} else "ready"),
    )
    task.closed_at = "2026-01-01T00:00:00Z" if state == "closed" else None
    task.escalated_at = "2026-01-01T00:00:00Z" if state == "escalated" else None
    task.is_escalated = state == "escalated"
    task.active_blocked_by = {"blocker"} if state == "blocked" else set()


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_task():
    """Create a mock task with common attributes."""
    task = MagicMock()
    task.id = "gt-abc123"
    task.seq_num = 1
    task.title = "Test Task"
    task.description = "A test task description"
    task.priority = 2
    task.task_type = "task"
    task.created_at = "2024-01-01T00:00:00Z"
    task.updated_at = "2024-01-01T00:00:00Z"
    task.project_id = "proj-123"
    task.parent_task_id = None
    task.assignee = None
    task.claimed_by_session_id = None
    task.current_stage = None
    task.stages = (
        SimpleNamespace(stage_name="development", name="development", position=1, state="ready"),
        SimpleNamespace(stage_name="pr", name="pr", position=2, state="ready"),
        SimpleNamespace(stage_name="merge", name="merge", position=3, state="ready"),
    )
    task.closed_at = None
    task.closed_reason = None
    task.closed_in_session_id = None
    task.closed_commit_sha = None
    task.escalated_at = None
    task.escalation_reason = None
    task.is_escalated = False
    task.blocked_by = set()
    task.active_blocked_by = set()
    task.labels = None
    task.validation_criteria = None
    task.validation_fail_count = 0
    task.category = None
    task.to_dict.return_value = {
        "id": "gt-abc123",
        "title": "Test Task",
        "description": "A test task description",
        "priority": 2,
        "task_type": "task",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "project_id": "proj-123",
        "state": {
            "owner_session_id": None,
            "current_stage": {"name": "development", "state": "ready"},
            "is_claimed": False,
            "is_closed": False,
            "is_escalated": False,
            "is_blocked": False,
            "is_merge_ready": False,
            "closed_at": None,
            "closed_reason": None,
            "closed_in_session_id": None,
            "closed_commit_sha": None,
            "escalated_at": None,
            "escalation_reason": None,
        },
    }
    return task


@pytest.fixture
def mock_manager():
    """Create a mock task manager."""
    manager = MagicMock()
    manager.db = MagicMock()
    return manager


def test_get_task_manager_uses_postgres_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task CLI storage follows bootstrap-selected PostgreSQL hub storage."""
    db = MagicMock()
    postgres_cls = MagicMock(return_value=db)
    monkeypatch.setattr(
        task_config_utils,
        "load_config",
        lambda: SimpleNamespace(
            hub_backend="postgres",
            database_url="postgresql://gobby:gobby@localhost/gobby",
        ),
    )
    monkeypatch.setattr(task_config_utils, "PostgresHubDatabase", postgres_cls)
    manager_cls = MagicMock()
    monkeypatch.setattr(task_config_utils, "LocalTaskManager", manager_cls)

    task_config_utils.get_task_manager()

    postgres_cls.assert_called_once_with("postgresql://gobby:gobby@localhost/gobby")
    db.apply_migrations.assert_called_once_with()
    manager_cls.assert_called_once_with(db)


def test_get_task_manager_rejects_sqlite_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task CLI does not fall back to removed SQLite runtime storage."""
    monkeypatch.setattr(
        task_config_utils,
        "load_config",
        lambda: SimpleNamespace(hub_backend="sqlite", database_url=None),
    )

    with pytest.raises(ClickException, match="SQLite task storage is no longer supported"):
        task_config_utils.get_task_manager()


# ==============================================================================
# Tests for crud.py - List Commands
# ==============================================================================


class TestListTasksCommand:
    """Tests for gobby tasks list command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_no_tasks(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list with no tasks found."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list"])

        assert result.exit_code == 0
        assert "No tasks found" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    @patch("gobby.cli.tasks.crud.get_claimed_task_ids")
    def test_list_with_tasks(
        self,
        mock_claimed: MagicMock,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with tasks."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_claimed.return_value = set()
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_manager.db.fetchall.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list"])

        assert result.exit_code == 0
        assert "Found 1 tasks" in result.output
        # New compact layout: stage-state letter, priority emoji, task ref,
        # and title. No more LIFECYCLE/OWNER column headers.
        assert "#1" in result.output
        assert "Test Task" in result.output
        assert "🟡" in result.output  # priority 2 → yellow

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_json_output(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with JSON output."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "gt-abc123"

    @patch("gobby.cli.tasks.crud.filter_tasks_by_stage")
    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_with_stage_and_state_filter(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_filter: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with exact stage and state filters."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager
        mock_filter.return_value = [mock_task]

        result = runner.invoke(
            cli,
            ["tasks", "list", "--stage", "development", "--state", "ready", "--json"],
        )

        assert result.exit_code == 0
        mock_manager.list_tasks.assert_called_once()
        assert mock_manager.list_tasks.call_args.kwargs["limit"] == 10000
        mock_filter.assert_called_once_with(
            mock_manager,
            [mock_task],
            stage_name="development",
            state="ready",
            project_id=ANY,
        )

    def test_list_state_requires_stage(self, runner: CliRunner) -> None:
        """Test --state is only accepted with an explicit --stage."""
        result = runner.invoke(cli, ["tasks", "list", "--state", "ready"])

        assert result.exit_code == 0
        assert "--state requires --stage" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_with_active_flag(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with --active flag."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list", "--active"])

        assert result.exit_code == 0
        mock_manager.list_tasks.assert_called_once()
        call_kwargs = mock_manager.list_tasks.call_args.kwargs
        assert call_kwargs["closed"] is False

    @patch("gobby.cli.tasks.crud.filter_tasks_by_stage")
    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_with_stage_filter(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_filter: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with canonical stage filter."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager
        mock_filter.return_value = [mock_task]

        result = runner.invoke(cli, ["tasks", "list", "--stage", "development", "--json"])

        assert result.exit_code == 0
        mock_filter.assert_called_once_with(
            mock_manager,
            [mock_task],
            stage_name="development",
            state=None,
            project_id=ANY,
        )
        assert mock_filter.call_count == 1
        assert mock_filter.call_args is not None

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_with_claimed_filter(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with canonical claimed filter."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list", "--claimed"])

        assert result.exit_code == 0
        call_kwargs = mock_manager.list_tasks.call_args.kwargs
        assert call_kwargs["claimed"] is True

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_list_with_unclaimed_filter(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with canonical unclaimed filter."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "list", "--unclaimed"])

        assert result.exit_code == 0
        call_kwargs = mock_manager.list_tasks.call_args.kwargs
        assert call_kwargs["claimed"] is False

    def test_list_ready_and_blocked_mutually_exclusive(self, runner: CliRunner) -> None:
        """Test that --ready and --blocked are mutually exclusive."""
        result = runner.invoke(cli, ["tasks", "list", "--ready", "--blocked"])

        assert result.exit_code == 0
        assert "--ready and --blocked are mutually exclusive" in result.output

    def test_list_active_and_closed_mutually_exclusive(self, runner: CliRunner) -> None:
        """Test that --active and --closed are mutually exclusive."""
        result = runner.invoke(cli, ["tasks", "list", "--active", "--closed"])

        assert result.exit_code == 0
        assert "--active, --closed, and --escalated are mutually exclusive" in result.output

    def test_list_ready_and_active_mutually_exclusive(self, runner: CliRunner) -> None:
        """Test that --ready is not combined with broader filters."""
        result = runner.invoke(cli, ["tasks", "list", "--ready", "--active"])

        assert result.exit_code == 0
        assert "--ready/--blocked cannot be combined" in result.output

    def test_list_claimed_and_unclaimed_mutually_exclusive(self, runner: CliRunner) -> None:
        """Test that --claimed and --unclaimed are mutually exclusive."""
        result = runner.invoke(cli, ["tasks", "list", "--claimed", "--unclaimed"])

        assert result.exit_code == 0
        assert "--claimed and --unclaimed are mutually exclusive" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    @patch("gobby.cli.tasks.crud.get_claimed_task_ids")
    @patch("gobby.cli.tasks.crud.collect_ancestors")
    def test_list_with_ready_flag(
        self,
        mock_ancestors: MagicMock,
        mock_claimed: MagicMock,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with --ready flag."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_claimed.return_value = set()
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager
        mock_ancestors.return_value = ([mock_task], {mock_task.id})

        result = runner.invoke(cli, ["tasks", "list", "--ready"])

        assert result.exit_code == 0
        assert "Found 1 ready tasks" in result.output
        mock_manager.list_ready_tasks.assert_called_once()

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    @patch("gobby.cli.tasks.crud.get_claimed_task_ids")
    @patch("gobby.cli.tasks.crud.collect_ancestors")
    def test_list_with_blocked_flag(
        self,
        mock_ancestors: MagicMock,
        mock_claimed: MagicMock,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test list with --blocked flag."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_claimed.return_value = set()
        mock_manager = MagicMock()
        mock_manager.list_blocked_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager
        mock_ancestors.return_value = ([mock_task], {mock_task.id})

        result = runner.invoke(cli, ["tasks", "list", "--blocked"])

        assert result.exit_code == 0
        assert "Found 1 blocked tasks" in result.output
        mock_manager.list_blocked_tasks.assert_called_once()


class TestReadyTasksCommand:
    """Tests for gobby tasks ready command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_ready_no_tasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test ready with no tasks."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "ready"])

        assert result.exit_code == 0
        assert "No ready tasks found" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_claimed_task_ids")
    @patch("gobby.cli.tasks.crud.collect_ancestors")
    def test_ready_with_tasks(
        self,
        mock_ancestors: MagicMock,
        mock_claimed: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test ready with tasks."""
        mock_claimed.return_value = set()
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager
        mock_ancestors.return_value = ([mock_task], {mock_task.id})

        result = runner.invoke(cli, ["tasks", "ready"])

        assert result.exit_code == 0
        assert "Found 1 ready tasks" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_ready_json_output(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test ready with JSON output."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "ready", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_claimed_task_ids")
    def test_ready_flat_output(
        self,
        mock_claimed: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test ready with --flat flag."""
        mock_claimed.return_value = set()
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "ready", "--flat"])

        assert result.exit_code == 0
        assert "Found 1 ready tasks" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_ready_with_filters(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test ready with priority and type filters."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli, ["tasks", "ready", "--priority", "1", "--type", "bug", "--limit", "5"]
        )

        assert result.exit_code == 0
        call_kwargs = mock_manager.list_ready_tasks.call_args.kwargs
        assert call_kwargs["priority"] == 1
        assert call_kwargs["task_type"] == "bug"
        assert call_kwargs["limit"] == 5


class TestBlockedTasksCommand:
    """Tests for gobby tasks blocked command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_blocked_no_tasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test blocked with no tasks."""
        mock_manager = MagicMock()
        mock_manager.list_blocked_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "blocked"])

        assert result.exit_code == 0
        assert "No blocked tasks found" in result.output

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_blocked_with_tasks(
        self,
        mock_get_manager: MagicMock,
        mock_dep_cls: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test blocked with tasks."""
        mock_manager = MagicMock()
        mock_manager.list_blocked_tasks.return_value = [mock_task]
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager

        mock_dep_manager = MagicMock()
        mock_dep_manager.get_dependency_tree.return_value = {"blockers": [{"id": "gt-blocker1"}]}
        mock_dep_cls.return_value = mock_dep_manager

        blocker_task = MagicMock()
        _set_task_state(blocker_task, "ready")
        blocker_task.title = "Blocker Task"
        mock_manager.get_task.return_value = blocker_task

        result = runner.invoke(cli, ["tasks", "blocked"])

        assert result.exit_code == 0
        assert "Found 1 blocked tasks" in result.output

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_blocked_json_output(
        self,
        mock_get_manager: MagicMock,
        mock_dep_cls: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test blocked with JSON output."""
        mock_manager = MagicMock()
        mock_manager.list_blocked_tasks.return_value = [mock_task]
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager

        mock_dep_manager = MagicMock()
        mock_dep_manager.get_dependency_tree.return_value = {"blockers": []}
        mock_dep_cls.return_value = mock_dep_manager

        result = runner.invoke(cli, ["tasks", "blocked", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert "task" in data[0]
        assert "blocked_by" in data[0]


class TestTaskStatsCommand:
    """Tests for gobby tasks stats command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_stats_basic(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stats command."""
        mock_task = MagicMock()
        _set_task_state(mock_task, "ready")
        mock_task.priority = 2
        mock_task.task_type = "feature"
        mock_task.claimed_by_session_id = None
        mock_task.assignee = None

        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_manager.list_blocked_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "stats"])

        assert result.exit_code == 0
        assert "Task Statistics" in result.output
        assert "Total: 1" in result.output
        assert "development:ready: 1" in result.output
        assert "Claimed: 0" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_stats_json_output(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stats with JSON output."""
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []
        mock_manager.list_ready_tasks.return_value = []
        mock_manager.list_blocked_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "stats", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "total" in data
        assert "by_stage_state" in data
        assert "by_priority" in data
        assert "closed" in data


# ==============================================================================
# Tests for crud.py - CRUD Commands
# ==============================================================================


class TestCreateTaskCommand:
    """Tests for gobby tasks create command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.get_project_context")
    def test_create_task(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test creating a task."""
        mock_project_ctx.return_value = {"id": "proj-123", "name": "Test Project"}
        mock_manager = MagicMock()
        mock_manager.create_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "create", "My new task"])

        assert result.exit_code == 0
        assert "Created task Test Project-#1: Test Task" in result.output
        mock_manager.create_task.assert_called_once()

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.utils.get_project_context")
    def test_create_task_with_options(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test creating a task with options."""
        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.create_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "tasks",
                "create",
                "My new task",
                "--description",
                "Task description",
                "--priority",
                "1",
                "--type",
                "bug",
            ],
        )

        assert result.exit_code == 0
        mock_manager.create_task.assert_called_once_with(
            project_id="proj-123",
            title="My new task",
            description="Task description",
            priority=1,
            task_type="bug",
        )

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.utils.get_project_context")
    def test_create_task_no_project_uses_personal(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test creating a task with no project context uses personal workspace."""
        from gobby.storage.projects import PERSONAL_PROJECT_ID

        mock_project_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.create_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "create", "My new task"])

        assert result.exit_code == 0
        mock_manager.create_task.assert_called_once_with(
            project_id=PERSONAL_PROJECT_ID,
            title="My new task",
            description=None,
            priority=2,
            task_type="task",
        )


class TestShowTaskCommand:
    """Tests for gobby tasks show command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_show_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test showing a task."""
        mock_resolve.return_value = mock_task
        mock_get_manager.return_value = MagicMock()

        result = runner.invoke(cli, ["tasks", "show", "gt-abc123"])

        assert result.exit_code == 0
        assert "Test Task" in result.output
        assert "gt-abc123" in result.output
        assert "Current Stage: development:ready" in result.output
        assert "Legacy Status" not in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_show_task_with_labels(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test showing a task with labels."""
        mock_task.labels = ["bug", "priority"]
        mock_task.assignee = "john"
        mock_resolve.return_value = mock_task
        mock_get_manager.return_value = MagicMock()

        result = runner.invoke(cli, ["tasks", "show", "gt-abc123"])

        assert result.exit_code == 0
        assert "bug, priority" in result.output
        assert "Compatibility Assignee: john" in result.output
        assert "john" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_show_task_not_found(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing a non-existent task."""
        mock_resolve.return_value = None
        mock_get_manager.return_value = MagicMock()

        result = runner.invoke(cli, ["tasks", "show", "gt-nonexistent"])

        assert result.exit_code == 0  # Click doesn't set exit code for None return


class TestUpdateTaskCommand:
    """Tests for gobby tasks update command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test updating a task."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--title", "Updated title"])

        assert result.exit_code == 0
        assert "Updated task" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task_multiple_fields(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test updating multiple task fields."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "tasks",
                "update",
                "gt-abc123",
                "--title",
                "New title",
                "--priority",
                "1",
            ],
        )

        assert result.exit_code == 0
        mock_manager.update_task.assert_called_once()
        call_kwargs = mock_manager.update_task.call_args.kwargs
        assert call_kwargs["title"] == "New title"
        assert call_kwargs["priority"] == 1

    def test_update_task_rejects_status_option(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--status", "in_progress"])

        assert result.exit_code != 0
        assert "No such option: --status" in result.output

    def test_update_task_rejects_assignee_option(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--assignee", "alice"])

        assert result.exit_code != 0
        assert "No such option: --assignee" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task_with_parent(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test updating task with parent re-assignment."""
        parent_task = MagicMock()
        parent_task.id = "gt-parent"

        # First call returns the task, second call returns parent
        mock_resolve.side_effect = [mock_task, parent_task]
        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--parent", "gt-parent"])

        assert result.exit_code == 0
        mock_manager.update_task.assert_called_once()
        call_kwargs = mock_manager.update_task.call_args.kwargs
        assert call_kwargs["parent_task_id"] == "gt-parent"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task_with_task_type(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """--task-type forwards the new value to LocalTaskManager.update_task."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--task-type", "epic"])

        assert result.exit_code == 0
        mock_manager.update_task.assert_called_once()
        call_kwargs = mock_manager.update_task.call_args.kwargs
        assert call_kwargs["task_type"] == "epic"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task_with_isolation(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """--isolation forwards the new automation isolation mode."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--isolation", "none"])

        assert result.exit_code == 0
        mock_manager.update_task.assert_called_once()
        call_kwargs = mock_manager.update_task.call_args.kwargs
        assert call_kwargs["isolation"] == "none"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_update_task_rejects_isolation_artifact_conflict(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.artifacts.get_artifacts.return_value = SimpleNamespace(
            worktree_path=None,
            clone_path="/tmp/gobby-clone",
        )
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "update", "gt-abc123", "--isolation", "worktree"])

        assert result.exit_code != 0
        assert (
            "task already has a clone artifact; clear existing build artifacts "
            "before switching to worktree isolation"
        ) in result.output
        mock_manager.update_task.assert_not_called()


class TestCloseTaskCommand:
    """Tests for gobby tasks close command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_close_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test closing a task."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []  # No children
        mock_manager.close_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "close", "gt-abc123"])

        assert result.exit_code == 0
        assert "Closed task" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_close_task_with_reason(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test closing a task with a reason."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []
        mock_manager.close_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "close", "gt-abc123", "--reason", "wont_fix"])

        assert result.exit_code == 0
        assert "wont_fix" in result.output
        mock_manager.close_task.assert_called_once_with(mock_task.id, reason="wont_fix")

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_close_task_with_open_children(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test closing a task with open children fails."""
        child_task = MagicMock()
        child_task.id = "gt-child1"
        child_task.title = "Child task"
        _set_task_state(child_task, "ready")

        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [child_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "close", "gt-abc123"])

        assert result.exit_code == 0
        assert "Cannot close" in result.output
        assert "child tasks still open" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_close_task_with_force(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test closing a task with --force bypasses child check."""
        child_task = MagicMock()
        child_task.id = "gt-child1"
        _set_task_state(child_task, "ready")

        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.close_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "close", "gt-abc123", "--force"])

        assert result.exit_code == 0
        assert "Closed task" in result.output
        mock_manager.close_task.assert_called_once()


class TestReopenTaskCommand:
    """Tests for gobby tasks reopen command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_reopen_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test reopening a task."""
        _set_task_state(mock_task, "closed")
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.reopen_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "reopen", "gt-abc123"])

        assert result.exit_code == 0
        assert "Reopened task" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_reopen_task_with_reason(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test reopening a task with a reason."""
        _set_task_state(mock_task, "closed")
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.reopen_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli, ["tasks", "reopen", "gt-abc123", "--reason", "bug still exists"]
        )

        assert result.exit_code == 0
        assert "bug still exists" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_reopen_non_closed_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test reopening an already active task fails."""
        _set_task_state(mock_task, "ready")
        mock_resolve.return_value = mock_task
        mock_get_manager.return_value = MagicMock()

        result = runner.invoke(cli, ["tasks", "reopen", "gt-abc123"])

        assert "already active" in result.output


class TestDeleteTaskCommand:
    """Tests for gobby tasks delete command."""

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_delete_task(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test deleting a task with confirmation."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        # Need to confirm with 'y'
        result = runner.invoke(cli, ["tasks", "delete", "gt-abc123"], input="y\n")

        assert result.exit_code == 0
        assert "Deleted task" in result.output
        mock_manager.delete_task.assert_called_once_with(mock_task.id, cascade=False, unlink=False)

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_delete_task_with_cascade(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test deleting a task with cascade."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "delete", "gt-abc123", "--cascade"], input="y\n")

        assert result.exit_code == 0
        mock_manager.delete_task.assert_called_once_with(mock_task.id, cascade=True, unlink=False)

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_delete_task_abort(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test aborting task deletion."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "delete", "gt-abc123"], input="n\n")
        assert result.exit_code == 0
        mock_manager.delete_task.assert_not_called()


# ==============================================================================
# Tests for ai.py - AI Commands
# ==============================================================================


class TestSuggestCommand:
    """Tests for gobby tasks suggest command."""

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_no_ready_tasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test suggest with no ready tasks."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest"])

        assert result.exit_code == 0
        assert "No ready tasks found" in result.output

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_with_tasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test suggest returns best task."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_manager.list_tasks.return_value = []  # No children
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest"])

        assert result.exit_code == 0
        assert "Suggested next task" in result.output
        assert "gt-abc123" in result.output

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_json_output(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test suggest with JSON output."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "suggestion" in data
        assert "score" in data
        assert "reason" in data

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_prefers_leaf_tasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test that suggest prefers leaf tasks by default."""
        leaf_task = MagicMock()
        leaf_task.id = "gt-leaf"
        leaf_task.title = "Leaf Task"
        leaf_task.priority = 2
        _set_task_state(leaf_task, "ready")
        leaf_task.complexity_score = 3
        leaf_task.category = None
        leaf_task.description = "A leaf task"
        leaf_task.to_dict.return_value = {"id": "gt-leaf", "title": "Leaf Task"}

        parent_task = MagicMock()
        parent_task.id = "gt-parent"
        parent_task.title = "Parent Task"
        parent_task.priority = 1  # Higher priority
        _set_task_state(parent_task, "ready")
        parent_task.complexity_score = 5
        parent_task.category = None
        parent_task.description = "A parent task"
        parent_task.to_dict.return_value = {"id": "gt-parent", "title": "Parent Task"}

        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [parent_task, leaf_task]

        # Parent has children, leaf doesn't
        def list_tasks_side_effect(**kwargs):
            if kwargs.get("parent_task_id") == "gt-parent":
                return [MagicMock()]  # Has child
            return []  # No children

        mock_manager.list_tasks.side_effect = list_tasks_side_effect
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        # With prefer_subtasks=True, leaf task should be suggested
        assert data["suggestion"]["id"] == "gt-leaf"

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_no_prefer_subtasks(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test suggest with --no-prefer-subtasks."""
        task = MagicMock()
        task.id = "gt-abc123"
        task.title = "High Priority Task"
        task.priority = 1
        _set_task_state(task, "ready")
        task.complexity_score = 3
        task.category = None
        task.description = "High priority"
        task.to_dict.return_value = {"id": "gt-abc123", "title": "High Priority Task"}

        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [task]
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--no-prefer-subtasks"])

        assert result.exit_code == 0
        assert "high priority" in result.output.lower()


class TestValidateCommand:
    """Tests for gobby tasks validate command - AI module."""

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_parent_task_all_children_closed(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate parent task when all children are closed."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()

        child_task = MagicMock()
        child_task.id = "gt-child1"
        _set_task_state(child_task, "closed")
        mock_manager.list_tasks.return_value = [child_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--summary", "test"])

        assert result.exit_code == 0
        assert "VALID" in result.output
        mock_manager.close_task.assert_called()

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_parent_task_with_open_children(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate parent task with open children."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()

        child_task = MagicMock()
        child_task.id = "gt-child1"
        child_task.title = "Open child"
        _set_task_state(child_task, "ready")
        mock_manager.list_tasks.return_value = [child_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--summary", "test"])

        assert result.exit_code == 0
        assert "INVALID" in result.output
        assert "still open" in result.output

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_leaf_task_empty_summary(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate leaf task with empty summary."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []  # No children
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--summary", "   "])

        assert "Changes summary is required" in result.output


# ==============================================================================
# Tests for _utils.py helpers
# ==============================================================================


class TestResolveTaskId:
    """Tests for resolve_task_id helper."""

    @patch("gobby.cli.tasks._utils.get_task_manager")
    def test_resolve_exact_match(
        self,
        mock_get_manager: MagicMock,
        mock_task: MagicMock,
    ) -> None:
        """Test resolve_task_id with exact match."""
        from gobby.cli.tasks._utils import resolve_task_id

        mock_manager = MagicMock()
        mock_manager.get_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = resolve_task_id(mock_manager, "gt-abc123")

        assert result == mock_task

    @patch("gobby.cli.tasks._utils.get_task_manager")
    def test_resolve_prefix_single_match(
        self,
        mock_get_manager: MagicMock,
        mock_task: MagicMock,
    ) -> None:
        """Test resolve_task_id with single prefix match."""
        from gobby.cli.tasks._utils import resolve_task_id

        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")
        mock_manager.find_tasks_by_prefix.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = resolve_task_id(mock_manager, "abc")

        assert result == mock_task

    @patch("gobby.cli.tasks._utils.get_task_manager")
    def test_resolve_prefix_no_match(
        self,
        mock_get_manager: MagicMock,
    ) -> None:
        """Test resolve_task_id with no match."""
        from gobby.cli.tasks._utils import resolve_task_id

        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")
        mock_manager.find_tasks_by_prefix.return_value = []
        mock_get_manager.return_value = mock_manager

        result = resolve_task_id(mock_manager, "nonexistent")

        assert result is None

    @patch("gobby.cli.tasks._utils.get_task_manager")
    def test_resolve_prefix_ambiguous(
        self,
        mock_get_manager: MagicMock,
    ) -> None:
        """Test resolve_task_id with ambiguous prefix."""
        from gobby.cli.tasks._utils import resolve_task_id

        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")

        task1 = MagicMock()
        task1.id = "gt-abc123"
        task1.title = "Task 1"
        task2 = MagicMock()
        task2.id = "gt-abc456"
        task2.title = "Task 2"

        mock_manager.find_tasks_by_prefix.return_value = [task1, task2]
        mock_get_manager.return_value = mock_manager

        result = resolve_task_id(mock_manager, "abc")

        assert result is None
        assert len(mock_manager.find_tasks_by_prefix.return_value) == 2


class TestFormatTaskRow:
    """Tests for format_task_row helper."""

    def test_format_task_row_basic(self, mock_task: MagicMock) -> None:
        """Test basic task row formatting."""
        from gobby.cli.tasks._utils import format_task_row

        result = format_task_row(mock_task)

        assert "#1" in result  # Shows seq_num instead of full task ID
        assert "Test Task" in result

    def test_format_task_row_muted(self, mock_task: MagicMock) -> None:
        """Test muted task row formatting."""
        from gobby.cli.tasks._utils import format_task_row

        result = format_task_row(mock_task, is_primary=False)

        # Should contain ANSI escape codes for dim
        assert "\033[2m" in result or mock_task.title in result

    def test_format_task_row_with_tree_prefix(self, mock_task: MagicMock) -> None:
        """Test task row with tree prefix."""
        from gobby.cli.tasks._utils import format_task_row

        result = format_task_row(mock_task, tree_prefix="├── ")

        assert "├── " in result

    def test_format_task_row_claimed(self, mock_task: MagicMock) -> None:
        """Test task row for claimed task.

        format_task_row is the single-row primitive and intentionally does
        not render the session column (that lives in format_task_list which
        batches session lookups). Being in claimed_task_ids still resolves
        the row without crashing and leaves the task ref + title intact.
        """
        from gobby.cli.tasks._utils import format_task_row

        result = format_task_row(mock_task, claimed_task_ids={"gt-abc123"})

        assert "#1" in result
        assert "Test Task" in result
        # Ready/claimed tasks keep the "O" stage letter — claim state
        # surfaces as a session column in format_task_list instead.
        assert "O" in result


# ==============================================================================
# Additional ai.py tests for improved coverage
# ==============================================================================


class TestValidateCommandExtended:
    """Extended tests for validate command covering more paths."""

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_task_not_found(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validate when task is not found."""
        mock_resolve.return_value = None
        mock_get_manager.return_value = MagicMock()

        result = runner.invoke(cli, ["tasks", "validate", "gt-nonexistent", "--summary", "test"])

        assert result.exit_code == 0

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_parent_many_open_children(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate parent task with many open children shows truncated list."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()

        # Create 10 open children
        children = []
        for i in range(10):
            child = MagicMock()
            child.id = f"gt-child{i}"
            child.title = f"Child task {i}"
            _set_task_state(child, "ready")
            children.append(child)

        mock_manager.list_tasks.return_value = children
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--summary", "test"])

        assert result.exit_code == 0
        assert "INVALID" in result.output
        assert "10 of 10" in result.output
        assert "more" in result.output  # Should show truncation

    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_with_file_summary(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        mock_config: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
        tmp_path,
    ) -> None:
        """Test validate with --file option for summary."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []  # No children
        mock_get_manager.return_value = mock_manager

        # Create temp file with summary
        summary_file = tmp_path / "summary.txt"
        summary_file.write_text("This is a test summary from file")

        mock_config.side_effect = Exception("Config not available")

        result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--file", str(summary_file)])

        # Command should attempt to validate (may fail on config but accepts the file)
        assert result.exit_code == 0
        assert summary_file.read_text() == "This is a test summary from file"

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_history(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate --history."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.tasks.validation_history.ValidationHistoryManager") as MockHistory:
            history_mock = MockHistory.return_value
            history_mock.get_iteration_history.return_value = [
                MagicMock(iteration=1, status="invalid", feedback="Bad code", issues=["bug"])
            ]

            result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--history"])

            assert result.exit_code == 0
            assert "Iteration 1: invalid" in result.output
            assert "Bad code" in result.output

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_recurring(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate --recurring."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.tasks.validation_history.ValidationHistoryManager") as MockHistory:
            history_mock = MockHistory.return_value
            history_mock.get_recurring_issue_summary.return_value = {
                "total_iterations": 3,
                "recurring_issues": [{"title": "Lint Error", "count": 2}],
            }
            history_mock.has_recurring_issues.return_value = True

            result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--recurring"])

            assert result.exit_code == 0
            assert "Lint Error (count: 2)" in result.output
            assert "Total iterations: 3" in result.output

    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_max_retries_exceeded(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        mock_config: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validation failure exceeding max retries."""
        mock_task.validation_fail_count = 2  # Already failed 2 times
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        with patch("gobby.tasks.validation.TaskValidator") as MockValidator:
            validator_mock = MockValidator.return_value
            # Mock async validation result
            future = MagicMock()
            future.status = "invalid"
            future.feedback = "Still broken"

            async def async_result(*args, **kwargs):
                return future

            validator_mock.validate_task.side_effect = async_result

            # Max retries is 3 in code. With 2 failures + 1 new failure = 3 -> Exceeded if check is < MAX
            # Code: if new_fail_count < MAX_RETRIES (3): create fix task
            # 2 + 1 = 3. 3 < 3 is False. So it should mark as failed.

            result = runner.invoke(cli, ["tasks", "validate", "gt-abc123", "--summary", "fix"])

            assert result.exit_code == 0
            assert "Exceeded max retries" in result.output
            mock_manager.update_task.assert_called()
            call_kwargs = mock_manager.update_task.call_args.kwargs
            assert "Exceeded max retries (3)" in call_kwargs["validation_feedback"]
            mock_manager.escalate_task.assert_called_once_with(
                mock_task.id,
                reason="exceeded_validation_retries (3)",
            )


class TestSuggestCommandExtended:
    """Extended tests for suggest command."""

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_no_ready_json_output(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test suggest with no ready tasks in JSON mode."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["suggestion"] is None
        assert "No ready tasks found" in data["reason"]

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_with_type_filter(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test suggest with type filter."""
        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [mock_task]
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--type", "bug"])

        assert result.exit_code == 0
        mock_manager.list_ready_tasks.assert_called_once()
        call_kwargs = mock_manager.list_ready_tasks.call_args.kwargs
        assert call_kwargs["task_type"] == "bug"

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_task_with_category(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test that task with test strategy gets bonus score."""
        task_with_strategy = MagicMock()
        task_with_strategy.id = "gt-strat"
        task_with_strategy.title = "Task with strategy"
        task_with_strategy.priority = 2
        _set_task_state(task_with_strategy, "ready")
        task_with_strategy.complexity_score = 3
        task_with_strategy.category = "Unit tests for all methods"
        task_with_strategy.description = "Has test strategy"
        task_with_strategy.to_dict.return_value = {
            "id": "gt-strat",
            "title": "Task with strategy",
        }

        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [task_with_strategy]
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "has category" in data["reason"].lower()

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_suggest_high_priority_task(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test that high priority task is mentioned in reason."""
        high_priority_task = MagicMock()
        high_priority_task.id = "gt-high"
        high_priority_task.title = "High Priority"
        high_priority_task.priority = 1
        _set_task_state(high_priority_task, "ready")
        high_priority_task.complexity_score = None
        high_priority_task.category = None
        high_priority_task.description = "Urgent task"
        high_priority_task.to_dict.return_value = {
            "id": "gt-high",
            "title": "High Priority",
        }

        mock_manager = MagicMock()
        mock_manager.list_ready_tasks.return_value = [high_priority_task]
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "suggest", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "high priority" in data["reason"].lower()


class TestUtilsHelpers:
    """Additional tests for _utils.py helpers."""

    def test_format_task_row_different_states(self, mock_task: MagicMock) -> None:
        """Test format_task_row with different task states.

        Under the compact layout each stage/flag condition maps to a
        single letter: O/P/R/A/C for stage states, B/E/M for flags.
        """
        from gobby.cli.tasks._utils import format_task_row

        # Test in_progress stage state -> "P"
        _set_task_state(mock_task, "in_progress")
        result = format_task_row(mock_task)
        assert "P" in result

        # Test closed task -> "C" (rendered dim)
        _set_task_state(mock_task, "closed")
        result = format_task_row(mock_task)
        assert "C" in result

        # Test blocked predicate -> "B" flag letter
        _set_task_state(mock_task, "blocked")
        result = format_task_row(mock_task)
        assert "B" in result

        # Test escalated predicate -> "E" flag letter
        _set_task_state(mock_task, "escalated")
        result = format_task_row(mock_task)
        assert "E" in result

    def test_format_task_row_different_priorities(self, mock_task: MagicMock) -> None:
        """Test format_task_row with different priorities."""
        from gobby.cli.tasks._utils import format_task_row

        _set_task_state(mock_task, "ready")

        # High priority
        mock_task.priority = 1
        result = format_task_row(mock_task)
        assert "🔴" in result

        # Medium priority
        mock_task.priority = 2
        result = format_task_row(mock_task)
        assert "🟡" in result

        # Low priority
        mock_task.priority = 3
        result = format_task_row(mock_task)
        assert "🔵" in result


class TestFormatTaskHeader:
    """Tests for format_task_header helper."""

    def test_format_task_header_content(self) -> None:
        """Test task header formatting."""
        from gobby.cli.tasks._utils import format_task_header

        result = format_task_header()

        assert "#" in result  # Column header changed from ID to #
        assert "TITLE" in result


class TestFormatTaskList:
    """Tests for the compact multi-row format_task_list renderer."""

    @staticmethod
    def _make_task(
        *,
        seq_num: int,
        title: str,
        state: str = "ready",
        priority: int = 2,
        owner: str | None = None,
        escalated: bool = False,
        merge_ready: bool = False,
        project_id: str = "proj-123",
    ) -> MagicMock:
        task = MagicMock()
        task.id = f"gt-{seq_num:05d}"
        task.seq_num = seq_num
        task.title = title
        task.priority = priority
        task.task_type = "task"
        task.project_id = project_id
        task.parent_task_id = None
        task.assignee = owner
        task.claimed_by_session_id = owner
        task.current_stage = None
        task.stages = (
            SimpleNamespace(
                stage_name="development",
                name="development",
                position=1,
                state="ready" if state in {"blocked", "escalated", "closed"} else state,
            ),
        )
        task.closed_at = "2026-01-01T00:00:00Z" if state == "closed" else None
        task.closed_reason = None
        task.closed_in_session_id = None
        task.closed_commit_sha = None
        task.escalated_at = "2026-01-01T00:00:00Z" if escalated or state == "escalated" else None
        task.escalation_reason = "stuck" if escalated or state == "escalated" else None
        task.is_escalated = escalated or state == "escalated"
        task.blocked_by = set()
        task.active_blocked_by = {"other"} if state == "blocked" else set()
        task.labels = None
        task.validation_criteria = None
        task.validation_fail_count = 0
        task.category = None
        task.updated_at = "2026-04-13T00:00:00Z"
        task.merge_commit_sha = "abc" if merge_ready else None
        task.is_merge_ready = merge_ready
        return task

    @staticmethod
    def _strip_ansi(text: str) -> str:
        import re

        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_empty_list_returns_empty(self) -> None:
        from gobby.cli.tasks._utils import format_task_list

        mock_db = MagicMock()
        result = format_task_list([], db=mock_db)
        assert result == ""

    def test_open_only_suppresses_session_column(self) -> None:
        """All-open list with nothing claimed → no session column at all."""
        from gobby.cli.tasks._utils import format_task_list

        tasks = [
            self._make_task(seq_num=1, title="first task"),
            self._make_task(seq_num=2, title="second task"),
        ]
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []

        rendered = format_task_list(tasks, db=mock_db, term_width=120)
        plain = self._strip_ansi(rendered)

        assert "#1" in plain
        assert "first task" in plain
        assert "#2" in plain
        assert "second task" in plain
        # Single-letter stage state: every row is ready
        assert "O" in plain
        # No session refs should appear
        assert "#100" not in plain

    def test_blocked_flag_renders_letter(self) -> None:
        from gobby.cli.tasks._utils import format_task_list

        task = self._make_task(seq_num=42, title="blocked work", state="blocked")
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []

        rendered = format_task_list([task], db=mock_db, term_width=120)
        plain = self._strip_ansi(rendered)

        assert "#42" in plain
        # Flag letter B should appear somewhere in the prefix before #42
        prefix = plain.split("#42", 1)[0]
        assert "B" in prefix

    def test_session_column_aligns_claimed_and_unclaimed_rows(self) -> None:
        """Claimed + unclaimed rows in one render: #session column aligns."""
        from gobby.cli.tasks._utils import format_task_list

        claimed = self._make_task(seq_num=100, title="claimed work", owner="sess-owner-uuid")
        unclaimed = self._make_task(seq_num=101, title="unclaimed work")
        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"id": "sess-owner-uuid", "seq_num": 2572},
        ]

        rendered = format_task_list([claimed, unclaimed], db=mock_db, term_width=120)
        plain_lines = [self._strip_ansi(line) for line in rendered.split("\n")]

        # Session ref appears only on the claimed row, and is a seq_num (#2572)
        assert "#2572" in plain_lines[0]
        assert "#2572" not in plain_lines[1]
        # Both lines are the same length — the session column is reserved on
        # both rows so #NNNN aligns vertically. rstrip() would collapse the
        # blank padding on the unclaimed row, so we don't rstrip here.
        assert len(plain_lines[0]) == len(plain_lines[1])

    def test_orphaned_session_falls_back_to_uuid_prefix(self) -> None:
        """If the session row is missing, the owner UUID prefix is shown."""
        from gobby.cli.tasks._utils import format_task_list

        task = self._make_task(seq_num=7, title="orphan work", owner="orphaned-session-uuid")
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []  # lookup returns nothing

        rendered = format_task_list([task], db=mock_db, term_width=120)
        plain = self._strip_ansi(rendered)

        # Fallback is the first 8 chars of the owner UUID
        assert "orphaned" in plain

    def test_title_truncates_to_terminal_width(self) -> None:
        from gobby.cli.tasks._utils import format_task_list

        task = self._make_task(seq_num=1, title="this title is quite long and should be truncated")
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []

        rendered = format_task_list([task], db=mock_db, term_width=40)
        plain = self._strip_ansi(rendered)

        # An ellipsis must appear when the title is clipped
        assert "…" in plain
        # And the full original title must NOT be there in one piece
        assert "truncated" not in plain

    def test_group_by_stage_emits_headers(self) -> None:
        from gobby.cli.tasks._utils import format_task_list

        open_task = self._make_task(seq_num=1, title="new work", state="ready")
        progress_task = self._make_task(seq_num=2, title="active work", state="in_progress")
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []

        rendered = format_task_list(
            [open_task, progress_task],
            db=mock_db,
            group_by="stage",
            term_width=120,
        )
        plain = self._strip_ansi(rendered)

        assert "development:ready (1)" in plain
        assert "development:in_progress (1)" in plain
        assert "#1" in plain
        assert "#2" in plain

    def test_group_by_project_resolves_names(self) -> None:
        from gobby.cli.tasks._utils import format_task_list

        task_a = self._make_task(seq_num=1, title="gobby work", project_id="proj-a")
        task_b = self._make_task(seq_num=2, title="other work", project_id="proj-b")

        mock_db = MagicMock()

        def fake_fetchall(sql: str, params: tuple) -> list:
            if "FROM sessions" in sql:
                return []
            if "FROM projects" in sql:
                return [
                    {"id": "proj-a", "name": "gobby"},
                    {"id": "proj-b", "name": "other-repo"},
                ]
            return []

        mock_db.fetchall.side_effect = fake_fetchall

        rendered = format_task_list(
            [task_a, task_b], db=mock_db, group_by="project", term_width=120
        )
        plain = self._strip_ansi(rendered)

        assert "gobby (1)" in plain
        assert "other-repo (1)" in plain


# ==============================================================================
# Tests for main.py - Task Sync Command
# ==============================================================================


class TestSyncTasksCommand:
    """Tests for gobby tasks sync command."""

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_sync_default_both(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test sync with default behavior imports only."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "sync"])

        assert result.exit_code == 0
        mock_manager.import_from_jsonl.assert_called_once()
        mock_manager.export_to_jsonl.assert_not_called()
        assert "Sync completed" in result.output

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_sync_import_only(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test sync with --import flag only."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "sync", "--import"])

        assert result.exit_code == 0
        mock_manager.import_from_jsonl.assert_called_once()
        mock_manager.export_to_jsonl.assert_not_called()

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_sync_export_only_skips_outside_remote_push_context(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test sync with --export flag does not dirty JSONL locally."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "sync", "--export"])

        assert result.exit_code == 0
        mock_manager.import_from_jsonl.assert_not_called()
        mock_manager.export_to_jsonl.assert_not_called()
        assert ".gobby/tasks.jsonl is generated only during remote push" in result.output

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_sync_export_only_runs_in_remote_push_context(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test sync with --export flag in the pre-push publication context."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "sync", "--export"],
            env={"GOBBY_JSONL_EXPORT_CONTEXT": "pre-push"},
        )

        assert result.exit_code == 0
        mock_manager.import_from_jsonl.assert_not_called()
        mock_manager.export_to_jsonl.assert_called_once()

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_sync_quiet_mode(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test sync with --quiet flag suppresses output."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        result = runner.invoke(cli, ["tasks", "sync", "--quiet"])

        assert result.exit_code == 0
        assert "Importing" not in result.output
        assert "Exporting" not in result.output


# ==============================================================================
# Tests for main.py - Compact Commands
# ==============================================================================


class TestCompactAnalyzeCommand:
    """Tests for gobby tasks compact analyze command."""

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_analyze_no_candidates(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact analyze with no candidates found."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            mock_compactor.find_candidates.return_value = []
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(cli, ["tasks", "compact", "analyze"])

            assert result.exit_code == 0
            assert "No compaction candidates found" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_analyze_with_candidates(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact analyze with candidates found."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        candidate = {
            "id": "gt-123",
            "title": "Old Task",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            mock_compactor.find_candidates.return_value = [candidate]
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(cli, ["tasks", "compact", "analyze"])

            assert result.exit_code == 0
            assert "Found 1 candidates" in result.output
            assert "gt-123" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_analyze_custom_days(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact analyze with custom days threshold."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            mock_compactor.find_candidates.return_value = []
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(cli, ["tasks", "compact", "analyze", "--days", "60"])

            assert result.exit_code == 0
            mock_compactor.find_candidates.assert_called_once_with(days_closed=60)
            assert mock_compactor.find_candidates.call_count == 1
            assert mock_compactor.find_candidates.call_args is not None


class TestCompactApplyCommand:
    """Tests for gobby tasks compact apply command."""

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_apply_with_summary(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact apply with direct summary."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(
                cli, ["tasks", "compact", "apply", "--id", "gt-123", "--summary", "Task summary"]
            )

            assert result.exit_code == 0
            mock_compactor.compact_task.assert_called_once_with("gt-123", "Task summary")
            assert "Compacted task gt-123" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_apply_with_file(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Test compact apply with summary from file."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        # Create summary file
        summary_file = tmp_path / "summary.txt"
        summary_file.write_text("Summary from file")

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(
                cli,
                ["tasks", "compact", "apply", "--id", "gt-123", "--summary", f"@{summary_file}"],
            )

            assert result.exit_code == 0
            mock_compactor.compact_task.assert_called_once_with("gt-123", "Summary from file")
            assert mock_compactor.compact_task.call_count == 1
            assert mock_compactor.compact_task.call_args is not None

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_apply_error(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact apply with error."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            mock_compactor.compact_task.side_effect = Exception("Compaction failed")
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(
                cli, ["tasks", "compact", "apply", "--id", "gt-123", "--summary", "Summary"]
            )

            assert result.exit_code == 0  # CLI handles error gracefully
            assert "Error compacting task" in result.output


class TestCompactStatsCommand:
    """Tests for gobby tasks compact stats command."""

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_compact_stats(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test compact stats display."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.storage.compaction.TaskCompactor") as MockCompactor:
            mock_compactor = MagicMock()
            mock_compactor.get_stats.return_value = {
                "total_closed": 100,
                "compacted": 25,
                "rate": 25.0,
            }
            MockCompactor.return_value = mock_compactor

            result = runner.invoke(cli, ["tasks", "compact", "stats"])

            assert result.exit_code == 0
            assert "Compaction Statistics" in result.output
            assert "Total Closed: 100" in result.output
            assert "Compacted:    25" in result.output
            assert "Rate:         25.0%" in result.output


# ==============================================================================
# Tests for main.py - Import Commands
# ==============================================================================


class TestImportGitHubCommand:
    """Tests for gobby tasks import github command."""

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_import_github_success(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test successful GitHub import."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        # Mock the async method

        async def mock_import(*args, **kwargs):
            return {
                "success": True,
                "message": "Imported 5 issues",
                "imported": ["issue-1", "issue-2", "issue-3", "issue-4", "issue-5"],
            }

        mock_manager.import_from_github_issues = mock_import

        result = runner.invoke(cli, ["tasks", "import", "github", "https://github.com/user/repo"])

        assert result.exit_code == 0
        assert "Imported 5 issues" in result.output

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_import_github_error(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test GitHub import error handling."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        async def mock_import(*args, **kwargs):
            return {
                "success": False,
                "error": "Invalid GitHub URL",
            }

        mock_manager.import_from_github_issues = mock_import

        result = runner.invoke(cli, ["tasks", "import", "github", "invalid-url"])

        assert result.exit_code == 0
        assert "Error: Invalid GitHub URL" in result.output

    @patch("gobby.cli.tasks.main.get_sync_manager")
    def test_import_github_with_limit(
        self,
        mock_get_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test GitHub import with custom limit."""
        mock_manager = MagicMock()
        mock_get_sync.return_value = mock_manager

        async def mock_import(url, limit=50):
            assert limit == 100
            return {"success": True, "message": "Done", "imported": []}

        mock_manager.import_from_github_issues = mock_import

        result = runner.invoke(
            cli, ["tasks", "import", "github", "https://github.com/user/repo", "--limit", "100"]
        )

        assert result.exit_code == 0


# ==============================================================================
# Tests for main.py - Doctor Command
# ==============================================================================


class TestDoctorCommand:
    """Tests for gobby tasks doctor command."""

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_doctor_no_issues(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test doctor with no issues found."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.validate_all.return_value = {
                "orphan_dependencies": [],
                "invalid_projects": [],
                "cycles": [],
            }
            MockValidator.return_value = mock_validator

            result = runner.invoke(cli, ["tasks", "doctor"])

            assert result.exit_code == 0
            assert "✓ No orphan dependencies" in result.output
            assert "✓ No invalid projects" in result.output
            assert "✓ No dependency cycles" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_doctor_with_orphans(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test doctor with orphan dependencies."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.validate_all.return_value = {
                "orphan_dependencies": [
                    {"id": "dep-1", "task_id": "task-1", "depends_on": "missing-task"}
                ],
                "invalid_projects": [],
                "cycles": [],
            }
            MockValidator.return_value = mock_validator

            result = runner.invoke(cli, ["tasks", "doctor"])

            assert result.exit_code == 0
            assert "Found 1 orphan dependencies" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_doctor_with_invalid_projects(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test doctor with invalid projects."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.validate_all.return_value = {
                "orphan_dependencies": [],
                "invalid_projects": [
                    {"id": "task-1", "title": "Bad Task", "project_id": "invalid-proj"}
                ],
                "cycles": [],
            }
            MockValidator.return_value = mock_validator

            result = runner.invoke(cli, ["tasks", "doctor"])

            assert result.exit_code == 0
            assert "Found 1 tasks with invalid projects" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_doctor_with_cycles(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test doctor with dependency cycles."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.validate_all.return_value = {
                "orphan_dependencies": [],
                "invalid_projects": [],
                "cycles": [["task-1", "task-2", "task-1"]],
            }
            MockValidator.return_value = mock_validator

            result = runner.invoke(cli, ["tasks", "doctor"])

            assert result.exit_code == 0
            assert "Found 1 dependency cycles" in result.output


# ==============================================================================
# Tests for main.py - Clean Command
# ==============================================================================


class TestCleanCommand:
    """Tests for gobby tasks clean command."""

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_clean_removes_orphans(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test clean removes orphan dependencies."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.clean_orphans.return_value = 5
            MockValidator.return_value = mock_validator

            # Need to confirm the operation
            result = runner.invoke(cli, ["tasks", "clean"], input="y\n")

            assert result.exit_code == 0
            assert "Removed 5 orphan dependencies" in result.output

    @patch("gobby.cli.tasks.main.get_task_manager")
    def test_clean_no_orphans(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test clean with no orphans to remove."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.utils.validation.TaskValidator") as MockValidator:
            mock_validator = MagicMock()
            mock_validator.clean_orphans.return_value = 0
            MockValidator.return_value = mock_validator

            result = runner.invoke(cli, ["tasks", "clean"], input="y\n")

            assert result.exit_code == 0
            assert "No orphan dependencies found" in result.output


# ==============================================================================
# Tests for _utils.py - Additional Coverage
# ==============================================================================


class TestTaskUtilsFunctions:
    """Additional tests for cli/tasks/_utils.py functions."""

    def test_check_tasks_enabled_disabled(self) -> None:
        """Test check_tasks_enabled when tasks are disabled."""
        from gobby.cli.tasks._utils import check_tasks_enabled

        mock_config = MagicMock()
        mock_config.gobby_tasks.enabled = False

        with patch("gobby.cli.tasks._utils.config.load_config", return_value=mock_config):
            with pytest.raises(SystemExit) as exc_info:
                check_tasks_enabled()
            assert exc_info.value.code == 1

    def test_check_tasks_enabled_config_error(self) -> None:
        """Test check_tasks_enabled handles config errors gracefully."""
        from gobby.cli.tasks._utils import check_tasks_enabled

        with patch("gobby.cli.tasks._utils.config.load_config", side_effect=FileNotFoundError):
            check_tasks_enabled()
            assert check_tasks_enabled() is None

    def test_stage_state_choices(self) -> None:
        """Test stage-state CLI choices."""
        from gobby.cli.tasks._stage_filters import STAGE_STATES

        assert "ready" in STAGE_STATES
        assert "in_progress" in STAGE_STATES
        assert "needs_review" in STAGE_STATES

    def test_pad_to_width(self) -> None:
        """Test pad_to_width function."""
        from gobby.cli.tasks._utils import pad_to_width

        result = pad_to_width("test", 10)
        assert len(result) == 10
        assert result == "test      "

    def test_pad_to_width_emoji(self) -> None:
        """Test pad_to_width with emoji."""
        from gobby.cli.tasks._utils import pad_to_width

        result = pad_to_width("🔴", 4)
        # Emoji takes 2 visual chars, so 2 spaces padding
        assert result == "🔴  "

    def test_sort_tasks_for_tree(self, mock_task: MagicMock) -> None:
        """Test sorting tasks for tree display."""
        from gobby.cli.tasks._utils import sort_tasks_for_tree

        parent = MagicMock()
        parent.id = "parent-1"
        parent.parent_task_id = None

        child1 = MagicMock()
        child1.id = "child-1"
        child1.parent_task_id = "parent-1"

        child2 = MagicMock()
        child2.id = "child-2"
        child2.parent_task_id = "parent-1"

        tasks = [child2, child1, parent]  # Out of order
        result = sort_tasks_for_tree(tasks)

        # Parent should come before children
        assert result[0].id == "parent-1"
        assert result[1].id in ["child-1", "child-2"]
        assert result[2].id in ["child-1", "child-2"]

    def test_parse_task_refs(self) -> None:
        """Test parse_task_refs function."""
        from gobby.cli.tasks._utils import parse_task_refs

        # Single reference
        result = parse_task_refs(("42",))
        assert result == ["#42"]

        # Comma-separated
        result = parse_task_refs(("#1,#2,#3",))
        assert result == ["#1", "#2", "#3"]

        # Mixed
        result = parse_task_refs(("1", "2,3"))
        assert result == ["#1", "#2", "#3"]

        # UUID passthrough
        result = parse_task_refs(("abc123-def",))
        assert result == ["abc123-def"]

    def test_get_all_descendants(self, mock_task: MagicMock) -> None:
        """Test get_all_descendants function."""
        from gobby.cli.tasks._utils import get_all_descendants

        child = MagicMock()
        child.id = "child-1"
        grandchild = MagicMock()
        grandchild.id = "grandchild-1"

        mock_manager = MagicMock()

        def list_tasks_side_effect(**kwargs):
            parent_id = kwargs.get("parent_task_id")
            if parent_id == "parent-1":
                return [child]
            elif parent_id == "child-1":
                return [grandchild]
            return []

        mock_manager.list_tasks.side_effect = list_tasks_side_effect

        result = get_all_descendants(mock_manager, "parent-1")

        assert len(result) == 2
        assert result[0].id == "child-1"
        assert result[1].id == "grandchild-1"
