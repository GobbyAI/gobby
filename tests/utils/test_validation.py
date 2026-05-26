"""Tests for task validation utilities."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.utils.validation import TaskValidator

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction
    from gobby.storage.projects import LocalProjectManager


class _RollbackInvalidRows(Exception):
    """Roll back deliberately invalid rows after validation assertions."""


@pytest.fixture
def task_manager(temp_db: "HubDatabase") -> LocalTaskManager:
    """Create a task manager with temp database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def validator(task_manager: LocalTaskManager) -> TaskValidator:
    """Create a task validator."""
    return TaskValidator(task_manager)


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(UTC).isoformat()


@contextmanager
def _deferred_constraints_rollback(db: "HubDatabase") -> Iterator["Transaction"]:
    try:
        with db.transaction() as conn:
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            yield conn
            raise _RollbackInvalidRows
    except _RollbackInvalidRows:
        pass


class TestCheckOrphanDependencies:
    """Tests for check_orphan_dependencies."""

    def test_no_orphans_when_empty(self, validator: TaskValidator) -> None:
        """Returns empty list when no dependencies exist."""
        result = validator.check_orphan_dependencies()
        assert result == []

    def test_no_orphans_with_valid_dependencies(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Returns empty list when all dependencies are valid."""
        # Create a project first
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        # Create two tasks
        task1 = task_manager.create_task(
            title="Task 1",
            task_type="task",
            project_id=project.id,
        )
        task2 = task_manager.create_task(
            title="Task 2",
            task_type="task",
            project_id=project.id,
        )

        # Create valid dependency
        task_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (task2.id, task1.id, "blocks", _now_iso()),
        )

        result = validator.check_orphan_dependencies()
        assert result == []

    def test_detects_orphan_with_missing_task(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Detects dependency where task_id references non-existent task."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task1 = task_manager.create_task(
            title="Task 1",
            task_type="task",
            project_id=project.id,
        )

        # Defer FK checks so the validator can inspect intentionally invalid rows.
        with _deferred_constraints_rollback(task_manager.db):
            task_manager.db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                ("nonexistent-task-id", task1.id, "blocks", _now_iso()),
            )

            result = validator.check_orphan_dependencies()
            assert len(result) == 1
            assert result[0]["task_id"] == "nonexistent-task-id"

    def test_detects_orphan_with_missing_depends_on(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Detects dependency where depends_on references non-existent task."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task1 = task_manager.create_task(
            title="Task 1",
            task_type="task",
            project_id=project.id,
        )

        # Defer FK checks so the validator can inspect intentionally invalid rows.
        with _deferred_constraints_rollback(task_manager.db):
            task_manager.db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                (task1.id, "nonexistent-dep-id", "blocks", _now_iso()),
            )

            result = validator.check_orphan_dependencies()
            assert len(result) == 1
            assert result[0]["depends_on"] == "nonexistent-dep-id"


class TestCheckInvalidProjects:
    """Tests for check_invalid_projects."""

    def test_no_invalid_when_empty(self, validator: TaskValidator) -> None:
        """Returns empty list when no tasks exist."""
        result = validator.check_invalid_projects()
        assert result == []

    def test_no_invalid_with_valid_project(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Returns empty list when all tasks have valid projects."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task_manager.create_task(
            title="Task 1",
            task_type="task",
            project_id=project.id,
        )

        result = validator.check_invalid_projects()
        assert result == []

    def test_detects_invalid_project(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
    ) -> None:
        """Detects task with non-existent project_id."""
        # Defer FK checks so the validator can inspect intentionally invalid rows.
        with _deferred_constraints_rollback(task_manager.db):
            task_manager.db.execute(
                """
                INSERT INTO tasks (id, title, task_type, project_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, NOW(), NOW())
                """,
                ("orphan-task", "Orphan Task", "task", "nonexistent-project"),
            )

            result = validator.check_invalid_projects()
            assert len(result) == 1
            assert result[0]["id"] == "orphan-task"
            assert result[0]["project_id"] == "nonexistent-project"


class TestCheckCycles:
    """Tests for check_cycles."""

    def test_no_cycles_when_empty(self, validator: TaskValidator) -> None:
        """Returns empty list when no dependencies exist."""
        result = validator.check_cycles()
        assert result == []

    def test_no_cycles_with_linear_deps(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Returns empty list for linear dependency chain."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task1 = task_manager.create_task(title="Task 1", task_type="task", project_id=project.id)
        task2 = task_manager.create_task(title="Task 2", task_type="task", project_id=project.id)
        task3 = task_manager.create_task(title="Task 3", task_type="task", project_id=project.id)

        # Linear chain: task1 <- task2 <- task3
        task_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (task2.id, task1.id, "blocks", _now_iso()),
        )
        task_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (task3.id, task2.id, "blocks", _now_iso()),
        )

        result = validator.check_cycles()
        assert result == []

    def test_detects_direct_cycle(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Detects A -> B -> A cycle."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task1 = task_manager.create_task(title="Task 1", task_type="task", project_id=project.id)
        task2 = task_manager.create_task(title="Task 2", task_type="task", project_id=project.id)

        # Create cycle: task1 -> task2 -> task1
        task_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (task1.id, task2.id, "blocks", _now_iso()),
        )
        task_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (task2.id, task1.id, "blocks", _now_iso()),
        )

        result = validator.check_cycles()
        assert len(result) > 0
        # Verify cycle contains both tasks
        cycle = result[0]
        assert task1.id in cycle
        assert task2.id in cycle


class TestCleanOrphans:
    """Tests for clean_orphans."""

    def test_returns_zero_when_no_orphans(self, validator: TaskValidator) -> None:
        """Returns 0 when no orphans exist."""
        result = validator.clean_orphans()
        assert result == 0

    def test_removes_orphan_dependencies(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Removes orphan dependencies and returns count."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        task1 = task_manager.create_task(title="Task 1", task_type="task", project_id=project.id)

        with task_manager.db.transaction() as conn:
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            task_manager.db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                ("nonexistent-1", task1.id, "blocks", _now_iso()),
            )
            task_manager.db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                (task1.id, "nonexistent-2", "blocks", _now_iso()),
            )

            # Verify orphans exist
            assert len(validator.check_orphan_dependencies()) == 2

            # Clean orphans before the deferred constraints are checked.
            removed = validator.clean_orphans()
        assert removed == 2

        # Verify orphans are gone
        assert len(validator.check_orphan_dependencies()) == 0


class TestValidateAll:
    """Tests for validate_all."""

    def test_returns_all_validation_results(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
        project_manager: "LocalProjectManager",
    ) -> None:
        """Returns dict with all validation check results."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/test",
        )

        # Create a valid task
        task_manager.create_task(title="Valid Task", task_type="task", project_id=project.id)

        result = validator.validate_all()

        assert "orphan_dependencies" in result
        assert "invalid_projects" in result
        assert "cycles" in result

        # All should be empty for valid state
        assert result["orphan_dependencies"] == []
        assert result["invalid_projects"] == []
        assert result["cycles"] == []

    def test_validate_all_aggregates_issues(
        self,
        validator: TaskValidator,
        task_manager: LocalTaskManager,
    ) -> None:
        """Returns all issues found across validation checks."""
        with _deferred_constraints_rollback(task_manager.db):
            # Create task with invalid project
            task_manager.db.execute(
                """
                INSERT INTO tasks (id, title, task_type, project_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, NOW(), NOW())
                """,
                ("orphan-task", "Orphan Task", "task", "nonexistent-project"),
            )

            # Create orphan dependency (depends_on doesn't exist)
            task_manager.db.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                ("orphan-task", "nonexistent-dep", "blocks", _now_iso()),
            )

            result = validator.validate_all()

            assert len(result["invalid_projects"]) == 1
            assert len(result["orphan_dependencies"]) == 1
