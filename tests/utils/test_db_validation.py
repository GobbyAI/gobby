"""Tests for task validation and maintenance."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.validation import TaskValidator

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction


class _RollbackInvalidRows(Exception):
    """Roll back deliberately invalid rows after validation assertions."""


@contextmanager
def _deferred_constraints_rollback(db: "HubDatabase") -> Iterator["Transaction"]:
    try:
        with db.transaction() as conn:
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            yield conn
            raise _RollbackInvalidRows
    except _RollbackInvalidRows:
        pass


@pytest.fixture
def manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def dep_manager(temp_db):
    return TaskDependencyManager(temp_db)


@pytest.mark.integration
def test_orphan_dependencies(manager, dep_manager, sample_project):
    """Test detection and cleanup of orphan dependencies."""
    proj_id = sample_project["id"]
    t1 = manager.create_task(proj_id, "Task 1")
    t2 = manager.create_task(proj_id, "Task 2")

    # Create valid dependency
    dep_manager.add_dependency(t2.id, t1.id)

    with manager.db.transaction() as conn:
        conn.execute("SET CONSTRAINTS ALL DEFERRED")
        manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, %s, %s)",
            ("non_existent_task", t1.id, "blocks", "2023-01-01T00:00:00Z"),
        )

        validator = TaskValidator(manager)
        orphans = validator.check_orphan_dependencies()

        assert len(orphans) == 1
        # We can't assert ID equality easily since it's auto-increment, but we know it's there
        assert orphans[0]["task_id"] == "non_existent_task"

        # Clean before the deferred constraints are checked.
        count = validator.clean_orphans()
    assert count == 1

    assert len(validator.check_orphan_dependencies()) == 0
    # Valid dependency remains
    assert len(dep_manager.get_blockers(t2.id)) == 1


@pytest.mark.integration
def test_invalid_projects(manager, sample_project):
    """Test detection of tasks with invalid projects."""
    proj_id = sample_project["id"]
    manager.create_task(proj_id, "Valid Task")

    validator = TaskValidator(manager)
    with _deferred_constraints_rollback(manager.db):
        manager.db.execute(
            """
            INSERT INTO tasks (id, project_id, title, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                "task_orphan_proj",
                "invalid_proj",
                "Orphan Project Task",
                "2023-01-01",
                "2023-01-01",
            ),
        )

        invalid = validator.check_invalid_projects()

        assert len(invalid) == 1
        assert invalid[0]["id"] == "task_orphan_proj"


@pytest.mark.integration
def test_cycles_check(manager, dep_manager, sample_project):
    """Test cycle detection via validator."""
    proj_id = sample_project["id"]
    t1 = manager.create_task(proj_id, "T1")
    t2 = manager.create_task(proj_id, "T2")

    dep_manager.add_dependency(t2.id, t1.id)

    # Create cycle manually to bypass manager checks
    manager.db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, %s, %s)",
        (t1.id, t2.id, "blocks", "2023-01-01T00:00:00Z"),
    )

    validator = TaskValidator(manager)
    cycles = validator.check_cycles()

    assert len(cycles) > 0
