"""Tests for SessionVariableManager CRUD operations."""

from __future__ import annotations

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        ("proj1", "test-project"),
    )
    yield database


def _ensure_session(db, session_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session_id, f"ext-{session_id}", "machine-1", "claude", "proj1"),
    )


def test_get_variables_empty(db) -> None:
    """Test get_variables returns empty dict for new/unknown session."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    result = mgr.get_variables("nonexistent")
    assert result == {}


def test_set_variable(db) -> None:
    """Test set_variable writes a single variable."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "task_claimed", True)

    result = mgr.get_variables("s1")
    assert result["task_claimed"] is True


def test_set_variable_multiple(db) -> None:
    """Test set_variable can set multiple variables incrementally."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "task_claimed", True)
    mgr.set_variable("s1", "servers_listed", False)
    mgr.set_variable("s1", "stop_attempts", 0)

    result = mgr.get_variables("s1")
    assert result["task_claimed"] is True
    assert result["servers_listed"] is False
    assert result["stop_attempts"] == 0


def test_set_variable_overwrite(db) -> None:
    """Test set_variable overwrites an existing variable."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "stop_attempts", 0)
    mgr.set_variable("s1", "stop_attempts", 3)

    result = mgr.get_variables("s1")
    assert result["stop_attempts"] == 3


def test_merge_variables(db) -> None:
    """Test merge_variables atomically merges updates."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    # Set initial state
    mgr.set_variable("s1", "a", 1)
    mgr.set_variable("s1", "b", 2)

    # Merge new values (add c, update a, leave b)
    result = mgr.merge_variables("s1", {"a": 10, "c": 3})
    assert result is True

    variables = mgr.get_variables("s1")
    assert variables["a"] == 10  # Updated
    assert variables["b"] == 2  # Unchanged
    assert variables["c"] == 3  # Added


def test_merge_variables_creates_row(db) -> None:
    """Test merge_variables creates a row if one doesn't exist."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    result = mgr.merge_variables("s1", {"key": "value"})
    assert result is True

    variables = mgr.get_variables("s1")
    assert variables["key"] == "value"


def test_merge_variables_empty_updates(db) -> None:
    """Test merge_variables with empty dict is a no-op."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    result = mgr.merge_variables("s1", {})
    assert result is True


def test_delete_variables(db) -> None:
    """Test delete_variables removes all variables for a session."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "a", 1)
    mgr.set_variable("s1", "b", 2)

    mgr.delete_variables("s1")

    result = mgr.get_variables("s1")
    assert result == {}


def test_delete_variables_nonexistent(db) -> None:
    """Test delete_variables on non-existent session doesn't raise."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    result = mgr.delete_variables("nonexistent")
    assert result is None
    assert mgr.get_variables("nonexistent") == {}


def test_variables_persist_across_workflow_changes(db) -> None:
    """Test that session variables persist when workflows are enabled/disabled.

    Session variables live in their own table, independent of workflow instances.
    Enabling/disabling a workflow should not affect session variables.
    """
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import SessionVariableManager, WorkflowInstanceManager

    _ensure_session(db, "s1")
    sv_mgr = SessionVariableManager(db)
    wi_mgr = WorkflowInstanceManager(db)

    # Set session variables
    sv_mgr.set_variable("s1", "task_claimed", True)
    sv_mgr.set_variable("s1", "unlocked_tools", ["Read", "Write"])

    # Create and then delete a workflow instance
    wi_mgr.save_instance(
        WorkflowInstance(
            id="inst-1",
            session_id="s1",
            workflow_name="auto-task",
        )
    )
    wi_mgr.delete_instance("s1", "auto-task")

    # Session variables should be unaffected
    result = sv_mgr.get_variables("s1")
    assert result["task_claimed"] is True
    assert result["unlocked_tools"] == ["Read", "Write"]


def test_sessions_isolated(db) -> None:
    """Test that variables from different sessions are isolated."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "key", "session1")
    mgr.set_variable("s2", "key", "session2")

    assert mgr.get_variables("s1")["key"] == "session1"
    assert mgr.get_variables("s2")["key"] == "session2"


def test_complex_variable_types(db) -> None:
    """Test that variables support complex JSON types."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable("s1", "list_val", [1, 2, 3])
    mgr.set_variable("s1", "dict_val", {"nested": {"deep": True}})
    mgr.set_variable("s1", "null_val", None)
    mgr.set_variable("s1", "bool_val", False)

    result = mgr.get_variables("s1")
    assert result["list_val"] == [1, 2, 3]
    assert result["dict_val"] == {"nested": {"deep": True}}
    assert result["null_val"] is None
    assert result["bool_val"] is False


# --- append_to_set_variable tests ---


def test_append_to_set_variable_creates_new(db) -> None:
    """Creates row and initializes list when session has no variables."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable("new-session", "session_edited_files", ["a.py"])

    result = mgr.get_variables("new-session")
    assert result["session_edited_files"] == ["a.py"]


def test_append_to_set_variable_deduplicates(db) -> None:
    """Duplicate values are ignored, result is sorted."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable("s1", "files", ["b.py", "a.py"])
    mgr.append_to_set_variable("s1", "files", ["a.py", "c.py"])

    result = mgr.get_variables("s1")
    assert result["files"] == ["a.py", "b.py", "c.py"]


def test_append_to_set_variable_preserves_other_vars(db) -> None:
    """Appending to a list variable doesn't clobber other session variables."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.set_variable("s1", "baseline_dirty_files", ["x.py"])
    mgr.append_to_set_variable("s1", "session_edited_files", ["a.py"])

    result = mgr.get_variables("s1")
    assert result["baseline_dirty_files"] == ["x.py"]
    assert result["session_edited_files"] == ["a.py"]


def test_append_to_set_variable_noop_on_empty(db) -> None:
    """Empty values list is a no-op — doesn't create a row."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable("no-row", "files", [])

    result = mgr.get_variables("no-row")
    assert result == {}
