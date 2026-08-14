"""Tests for WorkflowInstanceManager CRUD operations."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# Session/project/instance id columns are native uuid in PostgreSQL; synthetic
# ids like S1 would fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
INST_1 = "33333333-3333-4333-8333-333333333333"
INST_2 = "44444444-4444-4444-8444-444444444444"
INST_3 = "55555555-5555-4555-8555-555555555555"
INST_4 = "66666666-6666-4666-8666-666666666666"
NONEXISTENT_SESSION_ID = "77777777-7777-4777-8777-777777777777"


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "test-project"),
    )
    yield database


def _ensure_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (
            session_id,
            f"ext-{session_id}",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            PROJECT_ID,
        ),
    )


def test_save_and_get_instance(db: HubDatabase) -> None:
    """Test saving and retrieving a workflow instance."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)

    instance = WorkflowInstance(
        id=INST_1,
        session_id=S1,
        workflow_name="auto-task",
        priority=25,
        current_step="work",
    )
    mgr.save_instance(instance)

    result = mgr.get_instance(S1, "auto-task")
    assert result is not None
    assert result.id == INST_1
    assert result.session_id == S1
    assert result.workflow_name == "auto-task"
    assert result.priority == 25
    assert result.current_step == "work"
    assert result.enabled is True


def test_get_instance_not_found(db: HubDatabase) -> None:
    """Test get_instance returns None for non-existent instance."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.get_instance(NONEXISTENT_SESSION_ID, "nonexistent")
    assert result is None


def test_save_instance_upsert(db: HubDatabase) -> None:
    """Upserts preserve creation identity and synchronize persisted timestamps."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)

    # Create
    instance = WorkflowInstance(
        id=INST_1,
        session_id=S1,
        workflow_name="auto-task",
        current_step="work",
        step_action_count=0,
    )
    mgr.save_instance(instance)
    created_at = instance.created_at
    updated_at = instance.updated_at

    replacement = WorkflowInstance(
        id=INST_2,
        session_id=S1,
        workflow_name="auto-task",
        current_step="complete",
        step_action_count=5,
    )
    mgr.save_instance(replacement)

    result = mgr.get_instance(S1, "auto-task")
    assert result is not None
    assert replacement.id == result.id == INST_1
    assert replacement.created_at == result.created_at == created_at
    assert replacement.updated_at == result.updated_at
    assert replacement.updated_at > updated_at
    assert result.current_step == "complete"
    assert result.step_action_count == 5


def test_merge_instance_variables_preserves_concurrent_step_transition(
    db: HubDatabase,
) -> None:
    """Variable merges must not overwrite execution state changed after a stale read."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)
    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="auto-task",
            current_step="work",
            step_action_count=1,
            total_action_count=2,
            variables={"existing": "value"},
        )
    )

    stale = mgr.get_instance(S1, "auto-task")
    assert stale is not None
    db.execute(
        "UPDATE workflow_instances "
        "SET current_step = %s, step_action_count = %s, total_action_count = %s "
        "WHERE session_id = %s AND workflow_name = %s",
        ("review", 7, 11, S1, "auto-task"),
    )

    assert mgr.merge_instance_variables(S1, "auto-task", {"new": "value"}) is True

    result = mgr.get_instance(S1, "auto-task")
    assert result is not None
    assert result.current_step == "review"
    assert result.step_action_count == 7
    assert result.total_action_count == 11
    assert result.variables == {"existing": "value", "new": "value"}


def test_instance_mutations_serialize_parallel_step_and_variable_writes(
    db: HubDatabase,
) -> None:
    from gobby.storage.hub.protocol import AgentStepInstanceMutation
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)
    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="auto-task",
            current_step="work",
            variables={"existing": "value"},
        )
    )
    transition_read = threading.Event()
    variable_write_started = threading.Event()
    finish_transition = threading.Event()

    def transition_step() -> None:
        lock = AgentStepInstanceMutation(session_id=S1)
        with db.transaction_immediate(lock):
            instance = mgr.get_instance(S1, "auto-task")
            assert instance is not None
            transition_read.set()
            assert finish_transition.wait(timeout=5)
            instance.current_step = "review"
            mgr.save_instance(instance)

    def merge_variables() -> bool:
        assert transition_read.wait(timeout=5)
        variable_write_started.set()
        return mgr.merge_instance_variables(S1, "auto-task", {"new": "value"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        transition_future = executor.submit(transition_step)
        variable_future = executor.submit(merge_variables)
        assert variable_write_started.wait(timeout=5)
        finish_transition.set()
        transition_future.result(timeout=5)
        assert variable_future.result(timeout=5) is True

    result = mgr.get_instance(S1, "auto-task")
    assert result is not None
    assert result.current_step == "review"
    assert result.variables == {"existing": "value", "new": "value"}


def test_merge_instance_variables_returns_false_for_missing_instance(db: HubDatabase) -> None:
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)

    assert (
        WorkflowInstanceManager(db).merge_instance_variables(
            S1,
            "missing",
            {"new": "value"},
        )
        is False
    )


def test_get_active_instances(db: HubDatabase) -> None:
    """Test get_active_instances returns enabled instances sorted by priority."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)

    # Insert equal-priority instances in reverse lexical order to exercise the tiebreaker.
    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="session-lifecycle",
            enabled=True,
            priority=10,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_2,
            session_id=S1,
            workflow_name="developer",
            enabled=True,
            priority=20,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_3,
            session_id=S1,
            workflow_name="auto-task",
            enabled=True,
            priority=20,
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_4,
            session_id=S1,
            workflow_name="disabled-wf",
            enabled=False,
            priority=5,
        )
    )

    active = mgr.get_active_instances(S1)
    assert len(active) == 3  # Disabled one excluded
    assert active[0].workflow_name == "session-lifecycle"  # priority=10
    assert active[1].workflow_name == "auto-task"  # priority=20, lexical first
    assert active[2].workflow_name == "developer"  # priority=20, lexical second


def test_get_active_instances_empty(db: HubDatabase) -> None:
    """Test get_active_instances returns empty list for no instances."""
    from gobby.workflows.state_manager import WorkflowInstanceManager

    mgr = WorkflowInstanceManager(db)
    result = mgr.get_active_instances(NONEXISTENT_SESSION_ID)
    assert result == []


def test_delete_instances_for_session(db: HubDatabase) -> None:
    """Test deleting all workflow instances for one session returns row count."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    _ensure_session(db, S2)
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="auto-task",
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_2,
            session_id=S1,
            workflow_name="developer",
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_3,
            session_id=S2,
            workflow_name="plan-adversary-steps",
        )
    )

    deleted_count = mgr.delete_instances_for_session(S1)

    assert deleted_count == 2
    assert mgr.get_active_instances(S1) == []
    remaining = mgr.get_active_instances(S2)
    assert len(remaining) == 1
    assert remaining[0].workflow_name == "plan-adversary-steps"


def test_multiple_sessions_isolated(db: HubDatabase) -> None:
    """Test that instances from different sessions are isolated."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    _ensure_session(db, S2)
    mgr = WorkflowInstanceManager(db)

    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="auto-task",
            variables={"key": "session1"},
        )
    )
    mgr.save_instance(
        WorkflowInstance(
            id=INST_2,
            session_id=S2,
            workflow_name="auto-task",
            variables={"key": "session2"},
        )
    )

    s1_inst = mgr.get_instance(S1, "auto-task")
    s2_inst = mgr.get_instance(S2, "auto-task")

    assert s1_inst is not None
    assert s2_inst is not None
    assert s1_inst.variables["key"] == "session1"
    assert s2_inst.variables["key"] == "session2"


def test_save_instance_preserves_variables(db: HubDatabase) -> None:
    """Test that variables dict is correctly serialized and deserialized."""
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    _ensure_session(db, S1)
    mgr = WorkflowInstanceManager(db)

    variables = {
        "task_id": "task-123",
        "context_injected": True,
        "nested": {"list": [1, 2, 3], "flag": False},
    }
    mgr.save_instance(
        WorkflowInstance(
            id=INST_1,
            session_id=S1,
            workflow_name="auto-task",
            variables=variables,
        )
    )

    result = mgr.get_instance(S1, "auto-task")
    assert result is not None
    assert result.variables == variables
    assert result.variables["nested"]["list"] == [1, 2, 3]
