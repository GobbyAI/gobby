"""Tests for task validation and maintenance."""

import pytest
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.validation import TaskValidator
from gobby.storage.task_dependencies import TaskDependencyManager


@pytest.fixture
def manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def dep_manager(temp_db):
    return TaskDependencyManager(temp_db)


def test_orphan_dependencies(manager, dep_manager, sample_project):
    """Test detection and cleanup of orphan dependencies."""
    proj_id = sample_project["id"]
    t1 = manager.create_task(proj_id, "Task 1")
    t2 = manager.create_task(proj_id, "Task 2")

    # Create valid dependency
    dep_manager.add_dependency(t2.id, t1.id)

    # Create orphan dependency by disabling FK checks temporarily
    manager.db.execute("PRAGMA foreign_keys = OFF")
    manager.db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
        ("non_existent_task", t1.id, "blocks", "2023-01-01T00:00:00Z"),
    )
    manager.db.execute("PRAGMA foreign_keys = ON")

    validator = TaskValidator(manager)
    orphans = validator.check_orphan_dependencies()

    assert len(orphans) == 1
    # We can't assert ID equality easily since it's auto-increment, but we know it's there
    assert orphans[0]["task_id"] == "non_existent_task"

    # Clean
    count = validator.clean_orphans()
    assert count == 1

    assert len(validator.check_orphan_dependencies()) == 0
    # Valid dependency remains
    assert len(dep_manager.get_blockers(t2.id)) == 1


def test_invalid_projects(manager, sample_project):
    """Test detection of tasks with invalid projects."""
    proj_id = sample_project["id"]
    manager.create_task(proj_id, "Valid Task")

    # Create task with invalid project manually
    manager.db.execute("PRAGMA foreign_keys = OFF")
    manager.db.execute(
        """
        INSERT INTO tasks (id, project_id, title, status, created_at, updated_at) 
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "task_orphan_proj",
            "invalid_proj",
            "Orphan Project Task",
            "open",
            "2023-01-01",
            "2023-01-01",
        ),
    )
    manager.db.execute("PRAGMA foreign_keys = ON")

    validator = TaskValidator(manager)
    invalid = validator.check_invalid_projects()

    assert len(invalid) == 1
    assert invalid[0]["id"] == "task_orphan_proj"


def test_cycles_check(manager, dep_manager, sample_project):
    """Test cycle detection via validator."""
    proj_id = sample_project["id"]
    t1 = manager.create_task(proj_id, "T1")
    t2 = manager.create_task(proj_id, "T2")

    dep_manager.add_dependency(t2.id, t1.id)

    # Create cycle manually to bypass manager checks
    manager.db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
        (t1.id, t2.id, "blocks", "2023-01-01T00:00:00Z"),
    )

    validator = TaskValidator(manager)
    cycles = validator.check_cycles()

    assert len(cycles) > 0
