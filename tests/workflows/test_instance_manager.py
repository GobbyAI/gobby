"""Tests for WorkflowInstanceManager CRUD operations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> Iterator[LocalDatabase]:
    database = LocalDatabase(tmp_path / "test.db")
    run_migrations(database)
    database.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        ("proj1", "test-project"),
    )
    yield database
    database.close()


def _ensure_session(db: LocalDatabase, session_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session_id, f"ext-{session_id}", "machine-1", "claude", "proj1"),
    )


def test_save_and_get_instance(db: LocalDatabase) -> None:
    """Test saving and retrieving a workflow instance."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    instance = WorkflowInstance(
        id="inst-1",
        session_id="s1",
        workflow_name="auto-task",
        priority=25,
        current_step="work",
    )
    mgr.save_instance(instance)

    result = mgr.get_instance("s1", "auto-task")
    assert result is not None
    assert result.id == "inst-1"
    assert result.session_id == "s1"
    assert result.workflow_name == "auto-task"
    assert result.priority == 25
    assert result.current_step == "work"
    assert result.enabled is True


def test_get_instance_not_found(db: LocalDatabase) -> None:
    """Test get_instance returns None for non-existent instance."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.get_instance("nonexistent", "nonexistent")
    assert result is None


def test_save_instance_upsert(db: LocalDatabase) -> None:
    """Test that save_instance updates existing row on conflict."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    # Create
    instance = WorkflowInstance(
        id="inst-1",
        session_id="s1",
        workflow_name="auto-task",
        current_step="work",
        step_action_count=0,
    )
    mgr.save_instance(instance)

    # Update
    instance.current_step = "complete"
    instance.step_action_count = 5
    mgr.save_instance(instance)

    result = mgr.get_instance("s1", "auto-task")
    assert result is not None
    assert result.current_step == "complete"
    assert result.step_action_count == 5


def test_get_active_instances(db: LocalDatabase) -> None:
    """Test get_active_instances returns enabled instances sorted by priority."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    # Create 3 instances with different priorities and enabled states
    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="session-lifecycle",
            enabled=True,
            priority=10,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-2",
            session_id="s1",
            workflow_name="developer",
            enabled=True,
            priority=20,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-3",
            session_id="s1",
            workflow_name="auto-task",
            enabled=True,
            priority=25,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-4",
            session_id="s1",
            workflow_name="disabled-wf",
            enabled=False,
            priority=5,
        )
    )

    active = mgr.get_active_instances("s1")
    assert len(active) == 3  # Disabled one excluded
    assert active[0].workflow_name == "session-lifecycle"  # priority=10
    assert active[1].workflow_name == "developer"  # priority=20
    assert active[2].workflow_name == "auto-task"  # priority=25


def test_get_active_instances_empty(db: LocalDatabase) -> None:
    """Test get_active_instances returns empty list for no instances."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.get_active_instances("nonexistent")
    assert result == []


def test_delete_instance(db: LocalDatabase) -> None:
    """Test deleting a workflow instance."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
        )
    )

    assert mgr.get_instance("s1", "auto-task") is not None

    mgr.delete_instance("s1", "auto-task")

    assert mgr.get_instance("s1", "auto-task") is None


def test_delete_instance_nonexistent(db: LocalDatabase) -> None:
    """Test that deleting a non-existent instance doesn't raise."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.delete_instance("nonexistent", "nonexistent")
    assert result is None
    assert mgr.get_instance("nonexistent", "nonexistent") is None


def test_delete_instances_for_session(db: LocalDatabase) -> None:
    """Test deleting all workflow instances for one session returns row count."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    _ensure_session(db, "s2")
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-2",
            session_id="s1",
            workflow_name="developer",
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-3",
            session_id="s2",
            workflow_name="plan-adversary-steps",
        )
    )

    deleted_count = mgr.delete_instances_for_session("s1")

    assert deleted_count == 2
    assert mgr.get_active_instances("s1") == []
    remaining = mgr.get_active_instances("s2")
    assert len(remaining) == 1
    assert remaining[0].workflow_name == "plan-adversary-steps"


def test_set_enabled(db: LocalDatabase) -> None:
    """Test toggling enabled state on an instance."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
            enabled=True,
        )
    )

    # Disable
    mgr.set_enabled("s1", "auto-task", False)
    result = mgr.get_instance("s1", "auto-task")
    assert result is not None
    assert result.enabled is False

    # Re-enable
    mgr.set_enabled("s1", "auto-task", True)
    result = mgr.get_instance("s1", "auto-task")
    assert result is not None
    assert result.enabled is True


def test_set_enabled_nonexistent(db: LocalDatabase) -> None:
    """Test set_enabled on non-existent instance doesn't raise."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.set_enabled("nonexistent", "nonexistent", True)
    assert result is None
    assert mgr.get_instance("nonexistent", "nonexistent") is None


def test_multiple_sessions_isolated(db: LocalDatabase) -> None:
    """Test that instances from different sessions are isolated."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    _ensure_session(db, "s2")
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
            variables={"key": "session1"},
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id="inst-2",
            session_id="s2",
            workflow_name="auto-task",
            variables={"key": "session2"},
        )
    )

    s1_inst = mgr.get_instance("s1", "auto-task")
    s2_inst = mgr.get_instance("s2", "auto-task")

    assert s1_inst is not None
    assert s2_inst is not None
    assert s1_inst.variables["key"] == "session1"
    assert s2_inst.variables["key"] == "session2"


def test_save_instance_preserves_variables(db: LocalDatabase) -> None:
    """Test that variables dict is correctly serialized and deserialized."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, "s1")
    mgr = WorkflowInstanceManager(db)

    variables = {
        "task_id": "task-123",
        "context_injected": True,
        "nested": {"list": [1, 2, 3], "flag": False},
    }
    mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
            variables=variables,
        )
    )

    result = mgr.get_instance("s1", "auto-task")
    assert result is not None
    assert result.variables == variables
    assert result.variables["nested"]["list"] == [1, 2, 3]
