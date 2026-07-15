"""Tests for SessionVariableManager CRUD operations."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# Session/project id columns are native uuid in PostgreSQL; synthetic ids like
# S1 would fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
NEW_SESSION_ID = "33333333-3333-4333-8333-333333333333"
NO_ROW_SESSION_ID = "44444444-4444-4444-8444-444444444444"
NONEXISTENT_SESSION_ID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def db(temp_db: HubDatabase) -> Any:
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "test-project"),
    )
    for session_id in (S1, S2, NEW_SESSION_ID):
        _ensure_session(database, session_id)
    yield database


def _ensure_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, f"ext-{session_id}", "machine-1", "claude", PROJECT_ID),
    )


def _install_variable_default(db: HubDatabase, name: str, value: Any) -> None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    LocalWorkflowDefinitionManager(db).create(
        name=name,
        definition_json=json.dumps({"variable": name, "value": value}),
        workflow_type="variable",
    )


def _stored_variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    assert row is not None
    variables = row["variables"]
    return json.loads(variables) if isinstance(variables, str) else dict(variables)


class _DictVariablesConnection:
    def __init__(self, variables: dict[str, Any]) -> None:
        self.variables = variables
        self.written_variables: dict[str, Any] | None = None

    def __enter__(self) -> _DictVariablesConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> _DictVariablesConnection:
        if "UPDATE session_variables" in query:
            self.written_variables = json.loads(params[0])
        return self

    def fetchone(self) -> dict[str, Any]:
        return {"variables": self.variables}


class _DictVariablesDB:
    def __init__(self, variables: dict[str, Any]) -> None:
        self.connection = _DictVariablesConnection(variables)

    def transaction_immediate(self, _mutation: object) -> _DictVariablesConnection:
        return self.connection

    def fetchall(self, _query: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return []


def test_get_variables_empty(db: Any) -> None:
    """Test get_variables returns empty dict for new/unknown session."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    result = mgr.get_variables(NONEXISTENT_SESSION_ID)
    assert result == {}


def test_container_defaults_are_isolated_across_sessions_and_cache_hits(db: Any) -> None:
    """Mutating one returned default must not mutate the cached value or another session."""
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.state_manager import SessionVariableManager

    LocalWorkflowDefinitionManager(db).create(
        name="loaded-skills",
        definition_json=json.dumps({"variable": "loaded_skills", "value": []}),
        workflow_type="variable",
    )
    mgr = SessionVariableManager(db)

    session_a = mgr.get_variables(S1)
    session_a["loaded_skills"].append("development-discipline")
    session_b = mgr.get_variables(S2)

    assert session_b["loaded_skills"] == []
    assert mgr._defaults_cache is not None
    assert session_a["loaded_skills"] is not mgr._defaults_cache["loaded_skills"]
    assert session_b["loaded_skills"] is not mgr._defaults_cache["loaded_skills"]


@pytest.mark.parametrize("definition_json", [None, json.dumps("scalar")])
def test_get_variables_skips_malformed_variable_defaults(
    definition_json: str | None,
) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    db = MagicMock()
    db.fetchall.return_value = [{"name": "malformed-default", "definition_json": definition_json}]
    db.fetchone.return_value = None

    assert SessionVariableManager(db).get_variables(S1) == {}


def test_append_to_set_variable_accepts_jsonb_dict_payload() -> None:
    """PostgreSQL JSONB may return a dict instead of a JSON string."""
    from gobby.workflows.state_manager import SessionVariableManager

    fake_db = _DictVariablesDB({"session_edited_files": ["b.py"]})
    mgr = SessionVariableManager(fake_db)  # type: ignore[arg-type]

    result = mgr.append_to_set_variable(S1, "session_edited_files", ["a.py"])

    assert result is True
    assert fake_db.connection.written_variables == {"session_edited_files": ["a.py", "b.py"]}


def test_claim_startup_context_accepts_jsonb_dict_payload() -> None:
    """Startup-context claims use the same JSONB session variable payload."""
    from gobby.workflows.state_manager import SessionVariableManager

    fake_db = _DictVariablesDB({"_startup_context_injected": False})
    mgr = SessionVariableManager(fake_db)  # type: ignore[arg-type]

    result = mgr.claim_startup_context(S1)

    assert result == "full"
    assert fake_db.connection.written_variables == {"_startup_context_injected": True}


def test_set_variable(db: Any) -> None:
    """Test set_variable writes a single variable."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "task_claimed", True)

    result = mgr.get_variables(S1)
    assert result["task_claimed"] is True


def test_set_variable_multiple(db: Any) -> None:
    """Test set_variable can set multiple variables incrementally."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "task_claimed", True)
    mgr.set_variable(S1, "servers_listed", False)
    mgr.set_variable(S1, "stop_attempts", 0)

    result = mgr.get_variables(S1)
    assert result["task_claimed"] is True
    assert result["servers_listed"] is False
    assert result["stop_attempts"] == 0


def test_set_variable_overwrite(db: Any) -> None:
    """Test set_variable overwrites an existing variable."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "stop_attempts", 0)
    mgr.set_variable(S1, "stop_attempts", 3)

    result = mgr.get_variables(S1)
    assert result["stop_attempts"] == 3


def test_merge_variables(db: Any) -> None:
    """Test merge_variables atomically merges updates."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    # Set initial state
    mgr.set_variable(S1, "a", 1)
    mgr.set_variable(S1, "b", 2)

    # Merge new values (add c, update a, leave b)
    result = mgr.merge_variables(S1, {"a": 10, "c": 3})
    assert result is True

    variables = mgr.get_variables(S1)
    assert variables["a"] == 10  # Updated
    assert variables["b"] == 2  # Unchanged
    assert variables["c"] == 3  # Added


def test_merge_variables_creates_row(db: Any) -> None:
    """Test merge_variables creates a row if one doesn't exist."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    result = mgr.merge_variables(S1, {"key": "value"})
    assert result is True

    variables = mgr.get_variables(S1)
    assert variables["key"] == "value"


def test_merge_variables_empty_updates(db: Any) -> None:
    """Test merge_variables with empty dict is a no-op."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    result = mgr.merge_variables(S1, {})
    assert result is True


def test_adjust_counter_and_derive_boolean_tracks_overlapping_subagents(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    assert (
        mgr.adjust_counter_and_derive_boolean(S1, "subagent_count", 1, boolean_name="is_subagent")
        == 1
    )
    assert (
        mgr.adjust_counter_and_derive_boolean(S1, "subagent_count", 1, boolean_name="is_subagent")
        == 2
    )
    assert (
        mgr.adjust_counter_and_derive_boolean(S1, "subagent_count", -1, boolean_name="is_subagent")
        == 1
    )
    variables = mgr.get_variables(S1)
    assert variables["subagent_count"] == 1
    assert variables["is_subagent"] is True


def test_adjust_counter_and_derive_boolean_clamps_at_zero(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    assert (
        mgr.adjust_counter_and_derive_boolean(S1, "subagent_count", -1, boolean_name="is_subagent")
        == 0
    )
    variables = mgr.get_variables(S1)
    assert variables["subagent_count"] == 0
    assert variables["is_subagent"] is False


def test_delete_variables(db: Any) -> None:
    """Test delete_variables removes all variables for a session."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "a", 1)
    mgr.set_variable(S1, "b", 2)

    mgr.delete_variables(S1)

    result = mgr.get_variables(S1)
    assert result == {}


def test_delete_variables_nonexistent(db: Any) -> None:
    """Test delete_variables on non-existent session doesn't raise."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.delete_variables(NONEXISTENT_SESSION_ID)
    assert mgr.get_variables(NONEXISTENT_SESSION_ID) == {}


def test_variables_persist_across_workflow_changes(db: Any) -> None:
    """Test that session variables persist when workflows are enabled/disabled.

    Session variables live in their own table, independent of workflow instances.
    Enabling/disabling a workflow should not affect session variables.
    """
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import SessionVariableManager, WorkflowInstanceManager

    _ensure_session(db, S1)
    sv_mgr = SessionVariableManager(db)
    wi_mgr = WorkflowInstanceManager(db)

    # Set session variables
    sv_mgr.set_variable(S1, "task_claimed", True)
    sv_mgr.set_variable(S1, "unlocked_tools", ["Read", "Write"])

    # Create and then delete a workflow instance
    wi_mgr.save_instance(
        WorkflowInstance(
            id=str(uuid.uuid4()),
            session_id=S1,
            workflow_name="auto-task",
        )
    )
    wi_mgr.delete_instance(S1, "auto-task")

    # Session variables should be unaffected
    result = sv_mgr.get_variables(S1)
    assert result["task_claimed"] is True
    assert result["unlocked_tools"] == ["Read", "Write"]


def test_sessions_isolated(db: Any) -> None:
    """Test that variables from different sessions are isolated."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "key", "session1")
    mgr.set_variable(S2, "key", "session2")

    assert mgr.get_variables(S1)["key"] == "session1"
    assert mgr.get_variables(S2)["key"] == "session2"


def test_complex_variable_types(db: Any) -> None:
    """Test that variables support complex JSON types."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.set_variable(S1, "list_val", [1, 2, 3])
    mgr.set_variable(S1, "dict_val", {"nested": {"deep": True}})
    mgr.set_variable(S1, "null_val", None)
    mgr.set_variable(S1, "bool_val", False)

    result = mgr.get_variables(S1)
    assert result["list_val"] == [1, 2, 3]
    assert result["dict_val"] == {"nested": {"deep": True}}
    assert result["null_val"] is None
    assert result["bool_val"] is False


# --- append_to_set_variable tests ---


def test_append_to_set_variable_creates_new(db: Any) -> None:
    """Creates row and initializes list when session has no variables."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable(NEW_SESSION_ID, "session_edited_files", ["a.py"])

    result = mgr.get_variables(NEW_SESSION_ID)
    assert result["session_edited_files"] == ["a.py"]


def test_append_to_set_variable_persists_installed_default_entries(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "listed_servers", ["gobby-tasks"])
    mgr = SessionVariableManager(db)

    mgr.append_to_set_variable(S1, "listed_servers", ["gobby-memory"])

    assert _stored_variables(db, S1)["listed_servers"] == ["gobby-memory", "gobby-tasks"]


def test_append_to_set_variable_deduplicates(db: Any) -> None:
    """Duplicate values are ignored, result is sorted."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable(S1, "files", ["b.py", "a.py"])
    mgr.append_to_set_variable(S1, "files", ["a.py", "c.py"])

    result = mgr.get_variables(S1)
    assert result["files"] == ["a.py", "b.py", "c.py"]


def test_append_to_set_variable_preserves_other_vars(db: Any) -> None:
    """Appending to a list variable doesn't clobber other session variables."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.set_variable(S1, "baseline_dirty_files", ["x.py"])
    mgr.append_to_set_variable(S1, "session_edited_files", ["a.py"])

    result = mgr.get_variables(S1)
    assert result["baseline_dirty_files"] == ["x.py"]
    assert result["session_edited_files"] == ["a.py"]


def test_append_to_set_variable_noop_on_empty(db: Any) -> None:
    """Empty values list is a no-op — doesn't create a row."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable(NO_ROW_SESSION_ID, "files", [])

    result = mgr.get_variables(NO_ROW_SESSION_ID)
    assert result == {}


def test_claim_set_variable_values_returns_only_new_values(db: Any) -> None:
    """Claims are deduplicated atomically and preserve input order."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.append_to_set_variable(S1, "injected", ["existing"])

    first = mgr.claim_set_variable_values(S1, "injected", ["new-b", "existing", "new-a"])
    second = mgr.claim_set_variable_values(S1, "injected", ["new-a", "new-b"])

    assert first == ["new-b", "new-a"]
    assert second == []
    assert mgr.get_variables(S1)["injected"] == ["existing", "new-a", "new-b"]


def test_claim_set_variable_values_serializes_concurrent_claims(db: Any) -> None:
    """Only one concurrent caller can claim a newly injected value."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    barrier = threading.Barrier(3)

    def claim() -> list[str]:
        barrier.wait()
        return mgr.claim_set_variable_values(S1, "injected", ["shared"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        barrier.wait()
        claims = [future.result() for future in futures]

    assert sorted(claims) == [[], ["shared"]]
    assert mgr.get_variables(S1)["injected"] == ["shared"]


def test_append_to_set_variable_and_conditional_merge_resets_evidence(db: Any) -> None:
    """Edited-file tracking and verification reset happen in one atomic update."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "session_edited_files": ["b.py"],
            "verification_evidence_recorded": True,
            "verification_evidence": [{"command": "uv run pytest old.py", "success": True}],
            "kept": "value",
        },
    )

    result = mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "session_edited_files",
        ["a.py", "b.py"],
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    assert result is True
    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["a.py", "b.py"]
    assert variables["verification_evidence_recorded"] is False
    assert variables["verification_evidence"] == []
    assert variables["kept"] == "value"


def test_conditional_append_persists_installed_default_entries(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "listed_servers", ["gobby-tasks"])
    mgr = SessionVariableManager(db)

    mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "listed_servers",
        ["gobby-memory"],
        condition_name="verification_evidence_recorded",
        updates={},
    )

    assert _stored_variables(db, S1)["listed_servers"] == ["gobby-memory", "gobby-tasks"]


def test_append_to_set_variable_and_conditional_merge_preserves_unrecorded_evidence(
    db: Any,
) -> None:
    """Conditional updates are skipped when the guard variable is false."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "verification_evidence_recorded": False,
            "verification_evidence": [{"command": "uv run pytest old.py", "success": True}],
        },
    )

    mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "session_edited_files",
        ["a.py"],
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["a.py"]
    assert variables["verification_evidence"] == [
        {"command": "uv run pytest old.py", "success": True}
    ]


def test_record_edited_file_tracks_sole_claimed_task(db: Any) -> None:
    """Edited files are recorded in both session and task-scoped ledgers."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})

    mgr.record_edited_file(
        S1,
        "src/app.py",
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert variables["task_edited_files"] == {"task-1": ["src/app.py"]}


def test_record_edited_file_persists_installed_default_entries(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "session_edited_files", ["seed.py"])
    mgr = SessionVariableManager(db)

    mgr.record_edited_file(
        S1,
        "src/app.py",
        condition_name="verification_evidence_recorded",
        updates={},
    )

    assert _stored_variables(db, S1)["session_edited_files"] == ["seed.py", "src/app.py"]


def test_claim_startup_context_persists_installed_defaults(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "listed_servers", ["gobby-tasks"])
    mgr = SessionVariableManager(db)

    assert mgr.claim_startup_context(S1) == "full"

    assert _stored_variables(db, S1) == {
        "_startup_context_injected": True,
        "listed_servers": ["gobby-tasks"],
    }


def test_record_edited_file_without_claim_has_no_task_scoped_entry(db: Any) -> None:
    """No claimed task means only the session-level edit ledger is updated."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.record_edited_file(
        S1,
        "src/app.py",
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert "task_edited_files" not in variables


def test_record_edited_file_uses_active_task_when_multiple_claimed(db: Any) -> None:
    """Multiple claimed tasks require active_task_id for edit attribution."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "active_task_id": "task-2",
            "claimed_tasks": {"task-1": "#1", "task-2": "#2"},
        },
    )

    mgr.record_edited_file(
        S1,
        "src/app.py",
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert variables["task_edited_files"] == {"task-2": ["src/app.py"]}


def test_record_edited_file_does_not_guess_with_multiple_claims(db: Any) -> None:
    """Multiple claimed tasks without active_task_id do not receive task-scoped edits."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1", "task-2": "#2"}})

    mgr.record_edited_file(
        S1,
        "src/app.py",
        condition_name="verification_evidence_recorded",
        updates={"verification_evidence_recorded": False, "verification_evidence": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert "task_edited_files" not in variables
