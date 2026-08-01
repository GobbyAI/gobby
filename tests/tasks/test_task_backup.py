import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.dispatch.context import reload_candidate
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.task_github_import import GitHubIssueImporter
from gobby.sync.tasks import (
    TaskBackupError,
    TaskBackupManager,
    TaskRestoreError,
    _compute_path_cache,
)
from gobby.tasks.criteria_contract import TaskCriteriaError
from gobby.tasks.state_semantics import is_task_closed

pytestmark = pytest.mark.unit


def test_cli_backup_wraps_domain_error_as_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from gobby.cli.tasks import main as task_cli

    manager = MagicMock()
    manager.backup.side_effect = TaskBackupError("backup destination is unavailable")
    monkeypatch.setattr(task_cli, "get_backup_manager", lambda _path: manager)
    monkeypatch.setattr(
        "gobby.utils.project_context.get_project_context",
        lambda **_kwargs: None,
    )

    result = CliRunner().invoke(task_cli.backup_tasks, [])

    assert result.exit_code == 1
    assert "Error: backup destination is unavailable" in result.output


def _task_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gobby-sync-test:{name}"))


def _session_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gobby-sync-test-session:{name}"))


def _github_issue_task_id(project_id: str, issue_num: int, github_repo: str = "owner/repo") -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{project_id}/github/{github_repo}/issues/{issue_num}",
        )
    )


def _legacy_normalized_github_issue_task_id(issue_num: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"owner/repo/issues/{issue_num}"))


def _legacy_github_issue_task_id(repo_url: str, issue_num: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo_url}/issues/{issue_num}"))


@pytest.fixture
def backup_manager(hub_db: HubDatabase, tmp_path: Path) -> Iterator[TaskBackupManager]:
    backup_path = tmp_path / ".gobby" / "tasks.jsonl"
    task_manager = LocalTaskManager(hub_db)
    manager = TaskBackupManager(task_manager, str(backup_path))
    yield manager


@pytest.fixture
def github_importer(hub_db: HubDatabase) -> GitHubIssueImporter:
    return GitHubIssueImporter(hub_db)


@pytest.fixture
def task_manager(hub_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(hub_db)


@pytest.fixture
def sample_project(hub_db: HubDatabase) -> dict[str, Any]:
    project = LocalProjectManager(hub_db).create(
        name="test-project",
        repo_path="/tmp/test-project",
        github_url="https://github.com/test/test-project",
    )
    return project.to_dict()


def _insert_session(db: HubDatabase, session_id: str, project_id: str) -> None:
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (session_id, session_id, "test-machine", "test", project_id),
    )


class _FailingPathCacheConnection:
    def execute(self, sql: str, params: tuple[object, ...]) -> object:
        raise AssertionError("path-cache computation should use cached parent metadata")


def test_compute_path_cache_prefers_existing_task_cache() -> None:
    existing_tasks = {
        "parent": {
            "project_id": "project-1",
            "seq_num": 12,
            "parent_task_id": None,
        }
    }

    path_cache = _compute_path_cache(
        _FailingPathCacheConnection(),
        "project-1",
        13,
        "parent",
        existing_tasks,
    )

    assert path_cache == "12.13"


class TestTaskBackupManager:
    @pytest.mark.integration
    @pytest.mark.slow
    def test_backup(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        # Create tasks
        t1 = task_manager.create_task(
            sample_project["id"],
            "Task 1",
            validation_criteria="Test task completion is observable.",
        )
        t2 = task_manager.create_task(
            sample_project["id"],
            "Task 2",
            validation_criteria="Test task completion is observable.",
        )

        # Add dependency: Task 2 depends on Task 1
        # task_id = t2.id (the one with dependency), depends_on = t1.id (the dependency)
        # Note: In schema, unique constraint includes dep_type
        now = "2023-01-01T00:00:00"
        backup_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, %s, %s)",
            (t2.id, t1.id, "blocking", now),
        )

        backup_manager.backup()

        assert backup_manager.backup_path.exists()

        lines = backup_manager.backup_path.read_text().strip().split("\n")
        assert len(lines) == 2

        data = [json.loads(line) for line in lines]

        # Verify Task 1
        task1_data = next(d for d in data if d["id"] == t1.id)
        assert task1_data["title"] == "Task 1"
        assert task1_data["deps_on"] == []
        assert "state" in task1_data

        # Verify Task 2
        task2_data = next(d for d in data if d["id"] == t2.id)
        assert task2_data["title"] == "Task 2"
        assert task2_data["deps_on"] == [t1.id]
        assert task2_data["state"]["is_closed"] is False
        assert "status" not in task2_data

    def test_backup_fetches_all_pages(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task_manager.create_task(
            sample_project["id"],
            "Task 1",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.create_task(
            sample_project["id"],
            "Task 2",
            validation_criteria="Test task completion is observable.",
        )

        list_tasks = backup_manager.task_manager.list_tasks
        with (
            patch("gobby.sync.tasks.TASK_BACKUP_PAGE_SIZE", 1),
            patch.object(backup_manager.task_manager, "list_tasks", wraps=list_tasks) as mock_list,
        ):
            backup_manager.backup()

        assert [call.kwargs["offset"] for call in mock_list.call_args_list] == [0, 1, 2]
        assert len(backup_manager.backup_path.read_text().splitlines()) == 2

    @pytest.mark.integration
    def test_jsonl_round_trip_preserves_state_packed_columns(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task = task_manager.create_task(
            sample_project["id"],
            "Routed task",
            assigned_agent="backend-developer",
            implementation_domain="backend",
            additional_skills=["test-driven-development"],
            validation_criteria="Test task completion is observable.",
        )
        task_manager.update_task(
            task.id,
            allow_automation=True,
            unattended=True,
            isolation="clone",
        )

        backup_manager.backup()
        task_manager.db.execute("DELETE FROM tasks WHERE id = %s", (task.id,))
        backup_manager.restore()

        imported = task_manager.get_task(task.id)
        assert imported.allow_automation is True
        assert imported.unattended is True
        assert imported.isolation == "clone"
        assert imported.assigned_agent == "backend-developer"
        assert imported.implementation_domain == "backend"
        assert imported.additional_skills == ["test-driven-development"]

    @pytest.mark.integration
    def test_restore(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test importing tasks from JSONL."""
        # Create JSONL file content
        now = "2023-01-02T00:00:00+00:00"
        later = "2023-01-03T00:00:00+00:00"

        tasks_data = [
            {
                "id": _task_id("task-imported-1"),
                "title": "Imported Task",
                "description": "Desc",
                "status": "todo",
                "created_at": now,
                "updated_at": now,
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": [],
                "start_date": "2023-01-05",
                "due_date": "2023-01-10",
                "validation": {"criteria": "Test task completion is observable."},
            },
            {
                "id": _task_id("task-imported-2"),
                "title": "Imported Task with Dep",
                "description": "Desc",
                "status": "todo",
                "created_at": now,
                "updated_at": later,
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": [_task_id("task-imported-1")],
                "validation": {"criteria": "Test task completion is observable."},
            },
        ]

        # Write export file
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            for task in tasks_data:
                f.write(json.dumps(task) + "\n")

        # Run import
        backup_manager.restore()

        # Verify tasks in DB
        t1 = task_manager.get_task(_task_id("task-imported-1"))
        assert t1 is not None
        assert t1.title == "Imported Task"
        assert t1.created_at == datetime(2023, 1, 2, tzinfo=UTC)
        assert t1.updated_at == datetime(2023, 1, 2, tzinfo=UTC)
        assert t1.start_date == "2023-01-05"
        assert t1.due_date == "2023-01-10"

        t2 = task_manager.get_task(_task_id("task-imported-2"))
        assert t2 is not None
        assert t2.title == "Imported Task with Dep"

        # Verify Dependency
        deps = task_manager.db.fetchall(
            "SELECT * FROM task_dependencies WHERE task_id = %s", (t2.id,)
        )
        assert len(deps) == 1
        assert deps[0]["depends_on"] == t1.id

    @pytest.mark.integration
    def test_import_skips_dependency_with_missing_endpoint(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        task_id = _task_id("task-with-dangling-dependency")
        other_task_id = _task_id("independent-imported-task")
        missing_id = _task_id("missing-dependency")
        records = [
            {
                "id": task_id,
                "title": "Task with dangling dependency",
                "created_at": "2023-01-02T00:00:00+00:00",
                "updated_at": "2023-01-02T00:00:00+00:00",
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": [missing_id],
                "validation": {"criteria": "Test task completion is observable."},
            },
            {
                "id": other_task_id,
                "title": "Independent imported task",
                "created_at": "2023-01-02T00:00:00+00:00",
                "updated_at": "2023-01-02T00:00:00+00:00",
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": [],
                "validation": {"criteria": "Test task completion is observable."},
            },
        ]
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )

        with caplog.at_level("WARNING", logger="gobby.sync.tasks"):
            backup_manager.restore()

        assert task_manager.get_task(task_id) is not None
        assert task_manager.get_task(other_task_id) is not None
        assert (
            backup_manager.db.fetchall(
                "SELECT * FROM task_dependencies WHERE task_id = %s", (task_id,)
            )
            == []
        )
        assert (
            f"Skipping dependency {task_id} -> {missing_id}: endpoint missing after import"
            in caplog.messages
        )

    @pytest.mark.integration
    def test_import_conflict_resolution(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test LWW conflict resolution during import."""
        # 1. Local Task is NEWER (should keep local)
        t1 = task_manager.create_task(
            sample_project["id"],
            "Local Newer",
            validation_criteria="Test task completion is observable.",
        )
        # Force updated_at to future
        future = "2025-01-01T00:00:00+00:00"
        task_manager.db.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (future, t1.id))

        # File has older version
        past = "2020-01-01T00:00:00+00:00"
        file_data = {
            "id": t1.id,
            "title": "File Older",
            "description": "",
            "status": "todo",
            "created_at": past,
            "updated_at": past,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }

        # Write file
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(file_data) + "\n")

        backup_manager.restore()

        # Verify DB unchanged
        t1_fresh = task_manager.get_task(t1.id)
        assert t1_fresh.title == "Local Newer"

        # 2. File is NEWER (should overwrite local)
        t2 = task_manager.create_task(
            sample_project["id"],
            "Local Older",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.db.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (past, t2.id))

        file_data_2 = {
            "id": t2.id,
            "title": "File Newer",
            "description": "",
            "status": "todo",
            "created_at": past,
            "updated_at": future,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }

        # Append to file
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(file_data_2) + "\n")

        backup_manager.restore()

        # Verify DB updated
        t2_fresh = task_manager.get_task(t2.id)
        assert t2_fresh.title == "File Newer"

    def test_restore_preserves_newer_database_row(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task = task_manager.create_task(
            sample_project["id"],
            "Newer local",
            validation_criteria="Test task completion is observable.",
        )
        local_old = "2020-01-01T00:00:00+00:00"
        file_time = "2022-01-01T00:00:00+00:00"
        database_time = "2025-01-01T00:00:00+00:00"
        task_manager.db.execute(
            "UPDATE tasks SET updated_at = %s WHERE id = %s", (database_time, task.id)
        )

        file_data = {
            "id": task.id,
            "title": "File Version",
            "description": "",
            "created_at": local_old,
            "updated_at": file_time,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(json.dumps(file_data) + "\n")

        assert backup_manager.restore() == 0

        imported = task_manager.get_task(task.id)
        assert imported.title == "Newer local"
        assert imported.updated_at == datetime.fromisoformat(database_time)

    def test_restore_preserves_equal_timestamp_database_row(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task_id = _task_id("equal-timestamp")
        file_time = "2022-01-01T00:00:00+00:00"
        file_data = {
            "id": task_id,
            "title": "File Version",
            "created_at": file_time,
            "updated_at": file_time,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(json.dumps(file_data) + "\n")

        task_manager.db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria,
                created_at, updated_at, seq_num, path_cache
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                task_id,
                sample_project["id"],
                "Equal local",
                "The restored task remains unchanged.",
                datetime.fromisoformat(file_time),
                datetime.fromisoformat(file_time),
                999_999,
                "999999",
            ),
        )

        assert backup_manager.restore() == 0

        imported = task_manager.get_task(task_id)
        assert imported.title == "Equal local"

    @pytest.mark.integration
    def test_export_always_writes_fresh_content(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that export always writes correct content, even if file was externally modified."""
        task = task_manager.create_task(
            sample_project["id"],
            "Task 1",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.backup()

        # Read correct content
        correct_content = backup_manager.backup_path.read_text()
        assert "Task 1" in correct_content

        # Externally overwrite the file (simulates git checkout/merge)
        backup_manager.backup_path.write_text(
            json.dumps(
                {
                    "id": task.id,
                    "title": "Stale data",
                    "updated_at": "2000-01-01T00:00:00Z",
                }
            )
            + "\n"
        )

        # Export again — should restore correct content
        backup_manager.backup()
        restored_content = backup_manager.backup_path.read_text()
        assert restored_content == correct_content

    def test_backup_replaces_previous_file_with_live_database_rows(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        local_task = task_manager.create_task(
            sample_project["id"],
            "Local task",
            validation_criteria="Test task completion is observable.",
        )
        remote_task_id = _task_id("remote-only")
        file_only_record = {
            "id": remote_task_id,
            "title": "Remote-only task",
            "updated_at": "2099-01-02T00:00:00Z",
        }
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(json.dumps(file_only_record) + "\n")

        backup_manager.backup()

        records = [json.loads(line) for line in backup_manager.backup_path.read_text().splitlines()]
        assert [record["id"] for record in records] == [local_task.id]
        assert records[0]["title"] == "Local task"

    def test_backup_shrinks_after_deletion_and_restore_preserves_absent_row(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task = task_manager.create_task(
            sample_project["id"],
            "Delete everywhere",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.backup()

        assert task_manager.delete_task(task.id)
        backup_manager.backup()

        assert backup_manager.backup_path.read_text() == ""

        backup_manager.db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                task.id,
                sample_project["id"],
                "Stale peer copy",
                "The stale peer copy is removed.",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            ),
        )

        assert backup_manager.restore() == 0

        assert backup_manager.db.fetchone("SELECT id FROM tasks WHERE id = %s", (task.id,))

    def test_import_replaces_removed_dependencies(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        blocker = task_manager.create_task(
            sample_project["id"],
            "Blocker",
            validation_criteria="Test task completion is observable.",
        )
        task = task_manager.create_task(
            sample_project["id"],
            "Dependent",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.db.execute(
            """
            INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
            VALUES (%s, %s, 'blocks', %s)
            """,
            (task.id, blocker.id, "2020-01-01T00:00:00+00:00"),
        )
        record = {
            "id": task.id,
            "title": task.title,
            "created_at": "2020-01-01T00:00:00+00:00",
            "updated_at": "2099-01-01T00:00:00+00:00",
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(json.dumps(record) + "\n")

        backup_manager.restore()

        dependencies = backup_manager.db.fetchall(
            "SELECT depends_on FROM task_dependencies WHERE task_id = %s", (task.id,)
        )
        assert dependencies == []

    @pytest.mark.integration
    def test_export_replace_failure_preserves_existing_file(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        original = b'{"id": "existing"}\n'
        backup_manager.backup_path.write_bytes(original)
        task_manager.create_task(
            sample_project["id"],
            "Task 1",
            validation_criteria="Test task completion is observable.",
        )

        with patch("gobby.sync.jsonl_io.os.replace", side_effect=OSError("interrupted")):
            with pytest.raises(TaskBackupError, match="interrupted") as exc_info:
                backup_manager.backup()

        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == "interrupted"
        assert backup_manager.backup_path.read_bytes() == original
        assert list(backup_manager.backup_path.parent.glob(".tasks.jsonl.*.tmp")) == []


class TestImportEdgeCases:
    """Tests for import edge cases and error handling."""

    @pytest.mark.integration
    def test_import_no_file_exists(self, backup_manager: TaskBackupManager) -> None:
        """Test import when file doesn't exist - should just return."""
        # Ensure file doesn't exist
        assert not backup_manager.backup_path.exists()

        # Should not raise
        backup_manager.restore()

    @pytest.mark.integration
    def test_import_with_empty_lines(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import handles empty lines in JSONL file."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-empty-lines"),
            "title": "Test Task",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write("\n")  # Empty line at start
            f.write(json.dumps(tasks_data) + "\n")
            f.write("\n")  # Empty line in middle
            f.write("   \n")  # Whitespace-only line

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-empty-lines"))
        assert task is not None
        assert task.title == "Test Task"

    @pytest.mark.integration
    def test_import_with_validation_data(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import handles validation object."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-validation"),
            "title": "Task with Validation",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {
                "status": "valid",  # Must be 'pending', 'valid', or 'invalid'
                "feedback": "All tests passed",
                "fail_count": 0,
                "criteria": "Must pass unit tests",
                "override_reason": None,
            },
            "validation_criteria": "Test task completion is observable.",
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-validation"))
        assert task is not None
        assert task.validation_status == "valid"
        assert task.validation_feedback == "All tests passed"
        assert task.validation_criteria == "Must pass unit tests"

    @pytest.mark.integration
    def test_import_with_commits(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import handles commits array."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-commits"),
            "title": "Task with Commits",
            "description": "Desc",
            "status": "completed",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "commits": ["abc123", "def456"],
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-commits"))
        assert task is not None
        assert task.commits == ["abc123", "def456"]

    @pytest.mark.integration
    def test_import_with_escalation_data(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import handles escalation fields."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-escalated"),
            "title": "Escalated Task",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "escalated_at": now,
            "escalation_reason": "Blocked by external dependency",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-escalated"))
        assert task is not None
        assert task.escalated_at == datetime(2023, 1, 2, tzinfo=UTC)
        assert task.escalation_reason == "Blocked by external dependency"

    @pytest.mark.integration
    def test_import_with_canonical_state_projection(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import reads canonical lifecycle and ownership from the state object."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-canonical-state"),
            "title": "Canonical Task",
            "description": "Desc",
            "status": "needs_review",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "state": {
                "owner_session_id": _session_id("session-123"),
                "lifecycle_stage": "needs_review",
                "is_claimed": True,
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
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        _insert_session(backup_manager.db, _session_id("session-123"), sample_project["id"])
        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-canonical-state"))
        assert task is not None
        assert task.claimed_by_session_id == _session_id("session-123")

    @pytest.mark.integration
    def test_import_with_null_validation(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test import handles null validation object."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": _task_id("task-null-validation"),
            "title": "Task without Validation",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": None,
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        with pytest.raises(TaskCriteriaError, match="validation_criteria"):
            backup_manager.restore()

        with pytest.raises(ValueError, match="not found"):
            task_manager.get_task(_task_id("task-null-validation"))

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "bad_line",
        ["invalid json {{{", json.dumps({"id": _task_id("deleted"), "_deleted": True})],
        ids=["malformed", "tombstone"],
    )
    def test_invalid_record_aborts_restore_before_mutation(
        self,
        bad_line: str,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        task_id = _task_id("valid-before-invalid")
        valid_record = {
            "id": task_id,
            "title": "Must not be inserted",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "project_id": sample_project["id"],
            "validation": {"criteria": "Test task completion is observable."},
        }
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.write_text(json.dumps(valid_record) + "\n" + bad_line + "\n")

        with pytest.raises(TaskRestoreError):
            backup_manager.restore()

        with pytest.raises(ValueError, match="not found"):
            task_manager.get_task(task_id)


class TestClosedStateRoundTrip:
    """Tests that closed task metadata survives export → import round-trip."""

    @pytest.mark.integration
    def test_closed_task_round_trip_preserves_all_fields(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that a closed task with full metadata survives export → import."""
        task = task_manager.create_task(
            sample_project["id"],
            "Task to close",
            validation_criteria="Test task completion is observable.",
        )

        # Simulate a fully closed task with all metadata
        backup_manager.db.execute(
            """UPDATE tasks SET
                closed_at = '2026-01-15T10:00:00+00:00',
                closed_reason = 'completed',
                closed_commit_sha = 'abc123def456',
                labels = '["bug", "p0"]',
                category = 'code',
                github_issue_number = 42,
                github_pr_number = 99,
                github_repo = 'owner/repo',
                linear_issue_id = 'LIN-123',
                linear_team_id = 'TEAM-1',
                start_date = '2026-01-10',
                due_date = '2026-01-20'
            WHERE id = %s""",
            (task.id,),
        )

        # Export
        backup_manager.backup()

        # Verify JSONL has the closed fields
        lines = backup_manager.backup_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["state"]["is_closed"] is True
        assert data["closed_at"] is not None
        assert data["closed_reason"] == "completed"
        assert data["closed_commit_sha"] == "abc123def456"
        assert data["labels"] == ["bug", "p0"]
        assert data["category"] == "code"
        assert data["github_issue_number"] == 42
        assert data["github_pr_number"] == 99
        assert data["github_repo"] == "owner/repo"
        assert data["linear_issue_id"] == "LIN-123"
        assert data["linear_team_id"] == "TEAM-1"
        assert data["start_date"] == "2026-01-10"
        assert data["due_date"] == "2026-01-20"

        # Delete task from DB to simulate fresh import
        backup_manager.db.execute("DELETE FROM tasks WHERE id = %s", (task.id,))
        row = backup_manager.db.fetchone("SELECT 1 FROM tasks WHERE id = %s", (task.id,))
        assert row is None

        # Import from JSONL
        backup_manager.restore()

        # Verify all closed state fields survived
        reimported = task_manager.get_task(task.id)
        assert reimported is not None
        assert is_task_closed(reimported)
        assert reimported.closed_at == datetime(2026, 1, 15, 10, tzinfo=UTC)
        assert reimported.closed_reason == "completed"
        assert reimported.closed_commit_sha == "abc123def456"
        assert reimported.labels == ["bug", "p0"]
        assert reimported.category == "code"
        assert reimported.github_issue_number == 42
        assert reimported.github_pr_number == 99
        assert reimported.github_repo == "owner/repo"
        assert reimported.linear_issue_id == "LIN-123"
        assert reimported.linear_team_id == "TEAM-1"
        assert reimported.start_date == "2026-01-10"
        assert reimported.due_date == "2026-01-20"

    @pytest.mark.integration
    def test_update_path_preserves_session_local_fields(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that UPDATE import path preserves session-local columns."""
        task = task_manager.create_task(
            sample_project["id"],
            "Session task",
            validation_criteria="Test task completion is observable.",
        )

        # Set session-local fields that should NOT be wiped by import
        claimed_session_id = _session_id("session-uuid-123")
        created_session_id = _session_id("session-aaa")
        closed_session_id = _session_id("session-bbb")
        _insert_session(backup_manager.db, claimed_session_id, sample_project["id"])
        _insert_session(backup_manager.db, created_session_id, sample_project["id"])
        _insert_session(backup_manager.db, closed_session_id, sample_project["id"])
        backup_manager.db.execute(
            """UPDATE tasks SET
                claimed_by_session_id = %s,
                created_in_session_id = %s,
                closed_in_session_id = %s,
                compacted_at = '2026-01-10T00:00:00+00:00',
                updated_at = '2020-01-01T00:00:00+00:00'
            WHERE id = %s""",
            (claimed_session_id, created_session_id, closed_session_id, task.id),
        )

        # Create JSONL with newer timestamp to trigger UPDATE path
        jsonl_data = {
            "id": task.id,
            "title": "Updated title from JSONL",
            "description": "Updated desc",
            "status": "closed",
            "closed_at": "2026-02-01T00:00:00+00:00",
            "closed_reason": "done",
            "created_at": task.created_at.isoformat(),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "priority": 2,
            "task_type": "task",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(jsonl_data) + "\n")

        backup_manager.restore()

        # Verify synced fields were updated
        updated = task_manager.get_task(task.id)
        assert updated.title == "Updated title from JSONL"
        assert is_task_closed(updated)
        assert updated.closed_at == datetime(2026, 2, 1, tzinfo=UTC)
        assert updated.closed_reason == "done"

        # Verify session-local fields were PRESERVED (not wiped to NULL)
        row = backup_manager.db.fetchone(
            "SELECT claimed_by_session_id, created_in_session_id, closed_in_session_id, "
            "compacted_at FROM tasks WHERE id = %s",
            (task.id,),
        )
        assert row is not None
        assert row["claimed_by_session_id"] == claimed_session_id
        assert row["created_in_session_id"] == created_session_id
        assert row["closed_in_session_id"] == closed_session_id
        assert row["compacted_at"] == datetime(2026, 1, 10, tzinfo=UTC)

    @pytest.mark.integration
    def test_export_includes_priority_and_task_type(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that export includes priority and task_type fields."""
        task = task_manager.create_task(
            sample_project["id"],
            "Typed task",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.db.execute(
            "UPDATE tasks SET priority = 1, task_type = 'bug' WHERE id = %s",
            (task.id,),
        )

        backup_manager.backup()

        lines = backup_manager.backup_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["priority"] == 1
        assert data["task_type"] == "bug"


class TestExportEdgeCases:
    """Tests for export edge cases and error handling."""

    @pytest.mark.integration
    def test_export_multiple_dependencies(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test export with task having multiple dependencies."""
        t1 = task_manager.create_task(
            sample_project["id"],
            "Dependency 1",
            validation_criteria="Test task completion is observable.",
        )
        t2 = task_manager.create_task(
            sample_project["id"],
            "Dependency 2",
            validation_criteria="Test task completion is observable.",
        )
        t3 = task_manager.create_task(
            sample_project["id"],
            "Task with multiple deps",
            validation_criteria="Test task completion is observable.",
        )

        # Add multiple dependencies to t3
        now = "2023-01-01T00:00:00"
        backup_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, %s, %s)",
            (t3.id, t1.id, "blocking", now),
        )
        backup_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, %s, %s)",
            (t3.id, t2.id, "blocking", now),
        )

        backup_manager.backup()

        lines = backup_manager.backup_path.read_text().strip().split("\n")
        data = [json.loads(line) for line in lines]

        task3_data = next(d for d in data if d["id"] == t3.id)
        # deps_on should be sorted
        assert sorted(task3_data["deps_on"]) == sorted([t1.id, t2.id])

    @pytest.mark.integration
    def test_export_with_validation_data(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test export includes validation data."""
        task = task_manager.create_task(
            sample_project["id"],
            "Task with validation",
            validation_criteria="Test task completion is observable.",
        )

        # Add validation data directly to DB (status must be 'pending', 'valid', or 'invalid')
        backup_manager.db.execute(
            """UPDATE tasks SET
                validation_status = %s,
                validation_feedback = %s,
                validation_fail_count = %s,
                validation_criteria = %s
            WHERE id = %s""",
            ("invalid", "Test failed", 2, "Must pass CI", task.id),
        )

        backup_manager.backup()

        lines = backup_manager.backup_path.read_text().strip().split("\n")
        data = json.loads(lines[0])

        assert data["validation"] is not None
        assert data["validation"]["state"] == "invalid"
        assert data["validation"]["feedback"] == "Test failed"
        assert data["validation"]["fail_count"] == 2
        assert data["validation"]["criteria"] == "Must pass CI"

    @pytest.mark.integration
    def test_export_with_commits(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test export includes commits array."""
        task = task_manager.create_task(
            sample_project["id"],
            "Task with commits",
            validation_criteria="Test task completion is observable.",
        )

        # Link commits
        commits_json = json.dumps(["commit1", "commit2"])
        backup_manager.db.execute(
            "UPDATE tasks SET commits = %s WHERE id = %s",
            (commits_json, task.id),
        )

        backup_manager.backup()

        lines = backup_manager.backup_path.read_text().strip().split("\n")
        data = json.loads(lines[0])

        assert data["commits"] == ["commit1", "commit2"]

    @pytest.mark.integration
    def test_export_error_propagates(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Test that export errors are propagated."""
        task_manager.create_task(
            sample_project["id"],
            "Task 1",
            validation_criteria="Test task completion is observable.",
        )

        # Make the export path a directory to cause write error
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_manager.backup_path.mkdir()

        with pytest.raises(TaskBackupError) as exc_info:
            backup_manager.backup()

        assert isinstance(exc_info.value.__cause__, IsADirectoryError)

    @pytest.mark.integration
    def test_export_empty_tasks(self, backup_manager: TaskBackupManager) -> None:
        """Test export with no tasks creates empty file."""
        backup_manager.backup()

        assert backup_manager.backup_path.exists()
        content = backup_manager.backup_path.read_text()
        assert content == ""


class TestImportFromGitHubIssues:
    """Tests for import_from_github_issues async method."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_invalid_github_url(self, github_importer: GitHubIssueImporter) -> None:
        """Test import with invalid GitHub URL."""
        result = await github_importer.import_from_github_issues("not-a-url")

        assert result["success"] is False
        assert "Invalid GitHub URL" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_github_url_with_git_suffix(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import handles .git suffix in URL."""
        with patch("subprocess.run") as mock_run:
            # Mock gh --version check
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout="[]"),  # gh issue list
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo.git",
                project_id=sample_project["id"],
            )

            assert result["success"] is True
            assert result["count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_gh_not_installed(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import when gh CLI is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

            assert result["success"] is False
            assert "gh CLI not found" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_gh_command_fails(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import when gh command fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=1, stderr="auth required"),  # gh issue list
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

            assert result["success"] is False
            assert "gh command failed" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_no_open_issues(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import when there are no open issues."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout="[]"),  # gh issue list
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

            assert result["success"] is True
            assert result["count"] == 0
            assert result["imported"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_without_project_context(
        self, github_importer: GitHubIssueImporter
    ) -> None:
        """Test import fails without project context."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body 1",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout=issues_json),  # gh issue list
            ]

            with patch("gobby.utils.project_context.get_project_context", return_value=None):
                result = await github_importer.import_from_github_issues(
                    "https://github.com/owner/repo"
                )

        assert result["success"] is False
        assert "Could not determine project ID" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_with_project_id(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import with explicit project_id."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body 1",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                },
                {
                    "number": 2,
                    "title": "Issue 2",
                    "body": None,
                    "labels": [{"name": "bug"}],
                    "createdAt": "2023-01-02T00:00:00Z",
                },
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout=issues_json),  # gh issue list
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 2
        assert _github_issue_task_id(sample_project["id"], 1) in result["imported"]
        assert _github_issue_task_id(sample_project["id"], 2) in result["imported"]
        task = github_importer.task_manager.get_task(_github_issue_task_id(sample_project["id"], 1))
        assert task.github_repo == "owner/repo"
        assert task.github_issue_number == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_same_issue_number_in_different_repositories_creates_distinct_tasks(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        other_project = LocalProjectManager(github_importer.db).create(
            name="other-project",
            repo_path="/tmp/other-project",
            github_url="https://github.com/other/repo",
        )
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Repository-specific body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]
            first_result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )
            second_result = await github_importer.import_from_github_issues(
                "https://github.com/other/repo",
                project_id=other_project.id,
            )

        first_id = _github_issue_task_id(sample_project["id"], 1)
        second_id = _github_issue_task_id(other_project.id, 1, "other/repo")
        assert first_result["imported"] == [first_id]
        assert second_result["imported"] == [second_id]
        assert first_id != second_id

        first_task = github_importer.task_manager.get_task(first_id)
        second_task = github_importer.task_manager.get_task(second_id)
        assert first_task.project_id == sample_project["id"]
        assert first_task.github_repo == "owner/repo"
        assert first_task.github_issue_number == 1
        assert first_task.seq_num is not None
        assert first_task.path_cache is not None
        assert second_task.project_id == other_project.id
        assert second_task.github_repo == "other/repo"
        assert second_task.github_issue_number == 1
        assert second_task.seq_num is not None
        assert second_task.path_cache is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_updates_existing(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import updates existing issues."""
        # First import
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Original body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result1 = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result1["count"] == 1

        # Second import with updated issue
        issues_json_updated = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Updated Title",
                    "body": "Updated body",
                    "labels": [{"name": "enhancement"}],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json_updated),
            ]

            result2 = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        # Should update, not import
        assert result2["count"] == 0
        assert _github_issue_task_id(sample_project["id"], 1) in result2["imported"]
        assert "updated 1 existing" in result2["message"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_updates_existing_by_github_identifiers(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import updates a task already linked by GitHub identifiers."""
        existing = github_importer.task_manager.create_task(
            project_id=sample_project["id"],
            title="Existing task",
            description="Original body",
            github_repo="owner/repo",
            github_issue_number=7,
            validation_criteria="Test task completion is observable.",
        )
        issues_json = json.dumps(
            [
                {
                    "number": 7,
                    "title": "Updated from GitHub",
                    "body": "Updated body",
                    "labels": [{"name": "bug"}],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 0
        assert result["imported"] == [existing.id]
        updated = github_importer.task_manager.get_task(existing.id)
        assert updated.title == "Updated from GitHub"
        assert updated.github_repo == "owner/repo"
        assert updated.github_issue_number == 7

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_ignores_existing_legacy_url_id(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import ignores rows that used the retired repo URL UUID seed."""
        repo_url = "https://github.com/owner/repo"
        legacy_task_id = _legacy_github_issue_task_id(repo_url, 8)
        existing = github_importer.task_manager.create_task(
            project_id=sample_project["id"],
            title="Legacy task",
            description="Original body",
            validation_criteria="Test task completion is observable.",
        )
        github_importer.db.execute(
            "UPDATE tasks SET id = %s WHERE id = %s",
            (legacy_task_id, existing.id),
        )
        issues_json = json.dumps(
            [
                {
                    "number": 8,
                    "title": "Legacy Updated",
                    "body": "Updated body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                repo_url,
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        expected_task_id = _github_issue_task_id(sample_project["id"], 8)
        assert result["count"] == 1
        assert result["imported"] == [expected_task_id]
        imported = github_importer.task_manager.get_task(expected_task_id)
        assert imported.title == "Legacy Updated"
        legacy = github_importer.task_manager.get_task(legacy_task_id)
        assert legacy.title == "Legacy task"
        assert legacy.github_repo is None
        assert legacy.github_issue_number is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_ignores_current_project_legacy_normalized_id(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import ignores rows that used the retired normalized UUID seed."""
        repo_url = "https://github.com/owner/repo"
        legacy_task_id = _legacy_normalized_github_issue_task_id(9)
        existing = github_importer.task_manager.create_task(
            project_id=sample_project["id"],
            title="Legacy normalized task",
            description="Original body",
            validation_criteria="Test task completion is observable.",
        )
        github_importer.db.execute(
            "UPDATE tasks SET id = %s WHERE id = %s",
            (legacy_task_id, existing.id),
        )
        issues_json = json.dumps(
            [
                {
                    "number": 9,
                    "title": "Legacy Normalized Updated",
                    "body": "Updated body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                repo_url,
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        expected_task_id = _github_issue_task_id(sample_project["id"], 9)
        assert result["count"] == 1
        assert result["imported"] == [expected_task_id]
        imported = github_importer.task_manager.get_task(expected_task_id)
        assert imported.title == "Legacy Normalized Updated"
        legacy = github_importer.task_manager.get_task(legacy_task_id)
        assert legacy.title == "Legacy normalized task"
        assert legacy.github_repo is None
        assert legacy.github_issue_number is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_ignores_legacy_id_in_other_project(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test legacy ID fallback is scoped to the requested project."""
        other_project = LocalProjectManager(github_importer.db).create(
            name="other-project",
            repo_path="/tmp/other-project",
            github_url="https://github.com/other/repo",
        )
        legacy_task_id = _legacy_normalized_github_issue_task_id(10)
        other_task = github_importer.task_manager.create_task(
            project_id=other_project.id,
            title="Other project legacy task",
            description="Other body",
            validation_criteria="Test task completion is observable.",
        )
        github_importer.db.execute(
            "UPDATE tasks SET id = %s WHERE id = %s",
            (legacy_task_id, other_task.id),
        )
        issues_json = json.dumps(
            [
                {
                    "number": 10,
                    "title": "Current project issue",
                    "body": "Current body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        expected_task_id = _github_issue_task_id(sample_project["id"], 10)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["imported"] == [expected_task_id]

        current_project_task = github_importer.task_manager.get_task(expected_task_id)
        assert current_project_task.title == "Current project issue"
        assert current_project_task.project_id == sample_project["id"]

        other_project_task = github_importer.task_manager.get_task(legacy_task_id)
        assert other_project_task.title == "Other project legacy task"
        assert other_project_task.project_id == other_project.id
        assert other_project_task.github_repo is None
        assert other_project_task.github_issue_number is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_skip_no_number(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import skips issues without number."""
        issues_json = json.dumps(
            [
                {
                    "title": "Issue without number",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_skip_non_integer_number(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import skips issues with non-integer numbers."""
        issues_json = json.dumps(
            [
                {
                    "number": "1",
                    "title": "Issue with string number",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 0
        assert result["imported"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_json_decode_error(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import handles invalid JSON from gh."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout="not valid json"),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is False
        assert "Failed to parse GitHub response" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_finds_project_by_url(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import finds project by matching github_url."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        # The sample_project fixture has github_url set
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await github_importer.import_from_github_issues(
                repo_url=sample_project["github_url"],
            )

        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_general_exception(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import handles general exceptions."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                Exception("Unexpected error"),
            ]

            result = await github_importer.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is False
        assert "Unexpected error" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_with_project_context(
        self, github_importer: GitHubIssueImporter, sample_project: dict[str, Any]
    ) -> None:
        """Test import uses project context when project_id not provided."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            # Mock project context to return sample project
            with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": sample_project["id"]}

                result = await github_importer.import_from_github_issues(
                    "https://github.com/different/repo"
                )

        assert result["success"] is True
        assert result["count"] == 1


class TestImportSeqNumPreservation:
    """Tests for seq_num preservation during JSONL import (#9914)."""

    @pytest.mark.integration
    def test_import_preserves_seq_num_from_jsonl(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """seq_num 42 into empty DB → gets 42."""
        now = "2023-01-02T00:00:00+00:00"

        task_data = {
            "id": _task_id("task-preserve-seq"),
            "title": "Preserved Seq Task",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 42,
            "path_cache": "42",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(task_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-preserve-seq"))
        assert task is not None
        assert task.seq_num == 42
        assert task.path_cache == "42"

    @pytest.mark.integration
    def test_import_assigns_fresh_on_collision(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """DB has seq_num 5, import different task with 5 → gets fresh seq."""
        # Create existing task with seq_num 5
        existing = task_manager.create_task(
            sample_project["id"],
            "Existing Task",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.db.execute(
            "UPDATE tasks SET seq_num = 5, path_cache = '5' WHERE id = %s",
            (existing.id,),
        )

        now = "2023-01-02T00:00:00+00:00"
        task_data = {
            "id": _task_id("task-collision"),
            "title": "Colliding Seq Task",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 5,
            "path_cache": "5",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(task_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-collision"))
        assert task is not None
        # Should NOT be 5 since that's taken
        assert task.seq_num != 5
        # Should be > 5 (fresh assignment)
        assert task.seq_num is not None
        assert task.seq_num > 5

    @pytest.mark.integration
    def test_import_update_keeps_local_sequence_metadata_on_collision(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Updating a task keeps its local sequence metadata when the JSONL seq is occupied."""
        task = task_manager.create_task(
            sample_project["id"],
            "Task to update",
            validation_criteria="Test task completion is observable.",
        )
        occupant = task_manager.create_task(
            sample_project["id"],
            "Sequence occupant",
            validation_criteria="Test task completion is observable.",
        )
        backup_manager.db.execute(
            "UPDATE tasks SET seq_num = 5, path_cache = '5', "
            "updated_at = '2020-01-01T00:00:00+00:00' WHERE id = %s",
            (task.id,),
        )
        backup_manager.db.execute(
            "UPDATE tasks SET seq_num = 100, path_cache = '100' WHERE id = %s",
            (occupant.id,),
        )

        task_data = {
            "id": task.id,
            "title": "Updated task",
            "description": "Desc",
            "status": "open",
            "created_at": task.created_at.isoformat(),
            "updated_at": "2023-01-02T00:00:00+00:00",
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 100,
            "path_cache": "100",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(task_data) + "\n")

        backup_manager.restore()

        updated = task_manager.get_task(task.id)
        assert updated is not None
        assert updated.title == "Updated task"
        assert updated.seq_num == 5
        assert updated.path_cache == "5"

    @pytest.mark.integration
    def test_import_batch_dedup(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Two JSONL tasks with same seq_num → first wins, second gets fresh."""
        now = "2023-01-02T00:00:00+00:00"

        task1 = {
            "id": _task_id("task-batch-1"),
            "title": "Batch Task 1",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 100,
            "path_cache": "100",
            "validation": {"criteria": "Test task completion is observable."},
        }
        task2 = {
            "id": _task_id("task-batch-2"),
            "title": "Batch Task 2",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 100,
            "path_cache": "100",
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(task1) + "\n")
            f.write(json.dumps(task2) + "\n")

        backup_manager.restore()

        t1 = task_manager.get_task(_task_id("task-batch-1"))
        t2 = task_manager.get_task(_task_id("task-batch-2"))
        assert t1 is not None
        assert t2 is not None
        # First one should get 100, second should get something else
        assert t1.seq_num == 100
        assert t2.seq_num != 100
        assert t2.seq_num is not None
        assert t2.seq_num > 100

    @pytest.mark.integration
    def test_import_no_seq_num_in_jsonl(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """No seq_num field in JSONL → gets fresh assignment."""
        now = "2023-01-02T00:00:00+00:00"

        task_data = {
            "id": _task_id("task-no-seq"),
            "title": "No Seq Task",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {"criteria": "Test task completion is observable."},
            # No seq_num or path_cache
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(task_data) + "\n")

        backup_manager.restore()

        task = task_manager.get_task(_task_id("task-no-seq"))
        assert task is not None
        assert task.seq_num is not None
        assert task.seq_num >= 1
        assert task.path_cache is not None

    @pytest.mark.integration
    def test_path_cache_is_order_independent(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """Child imported before its parent still gets the complete path_cache."""
        now = "2023-01-02T00:00:00+00:00"

        parent = {
            "id": _task_id("task-parent-seq"),
            "title": "Parent",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "seq_num": 50,
            "path_cache": "50",
            "validation": {"criteria": "Test task completion is observable."},
        }
        child = {
            "id": _task_id("task-child-seq"),
            "title": "Child",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": _task_id("task-parent-seq"),
            "deps_on": [],
            "seq_num": 51,
            "path_cache": "50.51",
            "validation": {"criteria": "Test task completion is observable."},
        }

        # UUID-sorted exports can place a child before its parent.
        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(child) + "\n")
            f.write(json.dumps(parent) + "\n")

        backup_manager.restore()

        p = task_manager.get_task(_task_id("task-parent-seq"))
        c = task_manager.get_task(_task_id("task-child-seq"))
        assert p is not None
        assert c is not None
        assert p.seq_num == 50
        assert c.seq_num == 51
        assert p.path_cache == "50"
        assert c.path_cache == "50.51"

        resolved = reload_candidate(
            "50.51",
            db=backup_manager.db,
            project_id=sample_project["id"],
        )
        assert resolved is not None
        assert resolved.id == c.id

    @pytest.mark.integration
    def test_import_path_cache_ignores_parent_from_other_project(
        self,
        backup_manager: TaskBackupManager,
        task_manager: LocalTaskManager,
        sample_project: dict[str, Any],
    ) -> None:
        """A foreign-project parent id must not shape imported task path_cache."""
        other_project = LocalProjectManager(backup_manager.db).create(
            name="other-project",
            repo_path="/tmp/other-project",
        )
        now = "2023-01-02T00:00:00+00:00"
        foreign_parent_id = _task_id("foreign-parent")
        backup_manager.db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria,
                created_at, updated_at, seq_num, path_cache
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                foreign_parent_id,
                other_project.id,
                "Foreign Parent",
                "The foreign parent remains isolated.",
                now,
                now,
                77,
                "77",
            ),
        )
        child = {
            "id": _task_id("foreign-parent-child"),
            "title": "Child",
            "description": "Desc",
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": foreign_parent_id,
            "deps_on": [],
            "seq_num": 78,
            "validation": {"criteria": "Test task completion is observable."},
        }

        backup_manager.backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_manager.backup_path, "w") as f:
            f.write(json.dumps(child) + "\n")

        backup_manager.restore()

        imported = task_manager.get_task(_task_id("foreign-parent-child"))
        assert imported is not None
        assert imported.seq_num == 78
        assert imported.path_cache == "78"
