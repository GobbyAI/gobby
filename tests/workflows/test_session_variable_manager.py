"""Tests for SessionVariableManager CRUD operations."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
        (
            session_id,
            f"ext-{session_id}",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            PROJECT_ID,
        ),
    )


def _install_variable_default(db: HubDatabase, name: str, value: Any) -> None:
    from gobby.storage.definitions import SessionVariableDefaultManager

    SessionVariableDefaultManager(db).create(name=name, default_value=value)


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

    def fetchall(self, _query: str, _params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return []

    def fetchone(self, _query: str, _params: tuple[Any, ...] = ()) -> None:
        return None


def test_get_variables_empty(db: Any) -> None:
    """Test get_variables returns empty dict for new/unknown session."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    result = mgr.get_variables(NONEXISTENT_SESSION_ID)
    assert result == {}


def test_container_defaults_are_isolated_across_sessions_and_cache_hits(db: Any) -> None:
    """Mutating one returned default must not mutate the cached value or another session."""
    from gobby.storage.definitions import SessionVariableDefaultManager
    from gobby.workflows.state_manager import SessionVariableManager

    SessionVariableDefaultManager(db).create(name="loaded_skills", default_value=[])
    mgr = SessionVariableManager(db)

    session_a = mgr.get_variables(S1)
    session_a["loaded_skills"].append("development-discipline")
    session_b = mgr.get_variables(S2)

    assert session_b["loaded_skills"] == []
    assert mgr._defaults_cache
    cached = next(iter(mgr._defaults_cache.values()))
    assert session_a["loaded_skills"] is not cached["loaded_skills"]
    assert session_b["loaded_skills"] is not cached["loaded_skills"]


def test_get_variables_skips_disabled_variable_defaults(db: Any) -> None:
    from gobby.storage.definitions import SessionVariableDefaultManager
    from gobby.workflows.state_manager import SessionVariableManager

    SessionVariableDefaultManager(db).create(
        name="disabled-default", default_value="hidden", enabled=False
    )
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


def test_variables_persist_across_workflow_changes(db: Any) -> None:
    """Test that session variables persist when workflow instances change.

    Session variables live in their own table, independent of workflow instances.
    Creating/removing workflow instances should not affect session variables.
    """
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.step_instances import AgentStepInstanceManager
    from tests.workflows.step_instance_fixtures import make_step_instance

    _ensure_session(db, S1)
    sv_mgr = SessionVariableManager(db)
    wi_mgr = AgentStepInstanceManager(db)

    # Set session variables
    sv_mgr.set_variable(S1, "task_claimed", True)
    sv_mgr.set_variable(S1, "unlocked_tools", ["Read", "Write"])

    # Create and then delete a workflow instance
    wi_mgr.save(
        make_step_instance(
            S1,
            agent_name="auto-task",
            current_step="claim",
        )
    )
    wi_mgr.delete_for_session(S1)

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


def test_merge_variables_sanitizes_scalar_nul_and_preserves_ordinary_values(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    ordinary = {"list": [1, True, None], "nested": {"text": "unchanged"}}

    assert mgr.merge_variables(
        S1,
        {"command_output": "tracked.py\x00untracked.py", "ordinary": ordinary},
    )

    variables = mgr.get_variables(S1)
    assert variables["command_output"] == "tracked.py\ufffduntracked.py"
    assert variables["ordinary"] == ordinary


def test_bounded_list_persistence_sanitizes_nested_nul_delimited_output(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    audit_event = {
        "command": "git status --porcelain=v1 -z",
        "output": " M tracked.py\x00?? untracked.py\x00",
        "details": {
            "streams": ["ordinary", {"stderr": "warning\x00detail"}],
        },
    }

    count = mgr.append_to_bounded_list_variable(
        S1,
        "audit_events",
        audit_event,
        max_items=5,
        updates={"audit_ready": True},
    )

    assert count == 1
    variables = mgr.get_variables(S1)
    assert variables["audit_events"] == [
        {
            "command": "git status --porcelain=v1 -z",
            "output": " M tracked.py\ufffd?? untracked.py\ufffd",
            "details": {
                "streams": ["ordinary", {"stderr": "warning\ufffddetail"}],
            },
        }
    ]
    assert variables["audit_ready"] is True


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


def test_append_to_set_variable_preserves_first_seen_order_when_requested(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.append_to_set_variable(
        S1,
        "loaded_skills",
        ["plan", "brevity", "plan"],
        preserve_order=True,
    )
    mgr.append_to_set_variable(
        S1,
        "loaded_skills",
        ["tasks", "brevity"],
        preserve_order=True,
    )

    assert mgr.get_variables(S1)["loaded_skills"] == ["plan", "brevity", "tasks"]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, ["new.py"]),
        (False, ["new.py"]),
        (0, ["new.py"]),
        ("", ["new.py"]),
        ({"bad": "value"}, ["new.py"]),
        (["kept.py", 0, ["unhashable"]], ["kept.py", "new.py"]),
    ],
)
def test_append_to_set_variable_normalizes_stored_values(
    db: Any,
    stored: Any,
    expected: list[str],
) -> None:
    """Only strings from stored lists participate in set mutation."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.set_variable(S1, "files", stored)

    mgr.append_to_set_variable(S1, "files", ["new.py"])

    assert mgr.get_variables(S1)["files"] == expected


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
        barrier.wait(timeout=5)
        return mgr.claim_set_variable_values(S1, "injected", ["shared"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        barrier.wait(timeout=5)
        claims = [future.result() for future in futures]

    assert sorted(claims) == [[], ["shared"]]
    assert mgr.get_variables(S1)["injected"] == ["shared"]


def test_open_tool_error_upsert_deduplicates_and_resolves_exact_target(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    first_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    mgr.upsert_open_tool_error(
        S1,
        "gobby-tasks/close_task",
        "args:aaaaaaaa",
        "first",
        occurred_at=first_at,
    )
    mgr.upsert_open_tool_error(
        S1,
        "gobby-tasks/close_task",
        "args:aaaaaaaa",
        "second",
        occurred_at=first_at + timedelta(seconds=1),
    )
    mgr.upsert_open_tool_error(
        S1,
        "gobby-tasks/close_task",
        "args:bbbbbbbb",
        "other target",
        occurred_at=first_at + timedelta(seconds=2),
    )

    records = mgr.get_variables(S1)["open_tool_errors"]
    assert len(records) == 2
    # `error_id` is a content-derived retrieval handle (#19338). Assert its shape
    # here and leave the digest contract to tests/hooks/test_tool_error_tracker.py.
    assert records[0].pop("error_id").startswith("error-")
    assert records[0] == {
        "tool": "gobby-tasks/close_task",
        "target_key": "args:aaaaaaaa",
        "error": "second",
        "first_at": "2026-07-23T12:00:00+00:00",
        "last_at": "2026-07-23T12:00:01+00:00",
        "count": 2,
    }

    mgr.resolve_open_tool_errors(S1, "gobby-tasks/close_task", "args:aaaaaaaa")

    remaining = mgr.get_variables(S1)["open_tool_errors"]
    assert [record["target_key"] for record in remaining] == ["args:bbbbbbbb"]


def test_resolve_open_tool_errors_skips_write_without_match(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"open_tool_errors": []})
    db.execute(
        "UPDATE session_variables SET updated_at = %s WHERE session_id = %s",
        ("2000-01-01T00:00:00+00:00", S1),
    )
    before = db.fetchone(
        "SELECT updated_at FROM session_variables WHERE session_id = %s",
        (S1,),
    )

    mgr.resolve_open_tool_errors(S1, "Edit", "/repo/missing.py")

    after = db.fetchone(
        "SELECT updated_at FROM session_variables WHERE session_id = %s",
        (S1,),
    )
    assert after == before


def test_open_tool_error_upsert_caps_oldest_and_saturates_count(db: Any) -> None:
    from gobby.hooks.tool_error_tracker import MAX_OPEN_TOOL_ERRORS, MAX_TOOL_ERROR_COUNT
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    first_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    for index in range(MAX_OPEN_TOOL_ERRORS + 2):
        mgr.upsert_open_tool_error(
            S1,
            "Bash",
            f"args:{index:08x}",
            f"failure {index}",
            occurred_at=first_at + timedelta(seconds=index),
        )

    records = mgr.get_variables(S1)["open_tool_errors"]
    assert len(records) == MAX_OPEN_TOOL_ERRORS
    assert records[0]["target_key"] == "args:00000002"

    records[-1]["count"] = MAX_TOOL_ERROR_COUNT
    mgr.set_variable(S1, "open_tool_errors", records)
    mgr.upsert_open_tool_error(
        S1,
        "Bash",
        records[-1]["target_key"],
        "again",
        occurred_at=first_at + timedelta(minutes=1),
    )
    assert mgr.get_variables(S1)["open_tool_errors"][-1]["count"] == MAX_TOOL_ERROR_COUNT


def test_open_tool_error_concurrent_upserts_merge_counts(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    barrier = threading.Barrier(3)
    occurred_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)

    def upsert() -> None:
        barrier.wait(timeout=5)
        mgr.upsert_open_tool_error(
            S1,
            "Edit",
            "/repo/a.py#12345678",
            "failed",
            occurred_at=occurred_at,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(upsert) for _ in range(2)]
        barrier.wait(timeout=5)
        for future in futures:
            future.result()

    records = mgr.get_variables(S1)["open_tool_errors"]
    assert len(records) == 1
    assert records[0]["count"] == 2


def test_open_tool_error_concurrent_resolve_and_upsert_never_duplicate(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    occurred_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    mgr.upsert_open_tool_error(
        S1,
        "Edit",
        "/repo/a.py#12345678",
        "initial",
        occurred_at=occurred_at,
    )
    barrier = threading.Barrier(3)

    def upsert() -> None:
        barrier.wait(timeout=5)
        mgr.upsert_open_tool_error(
            S1,
            "Edit",
            "/repo/a.py#12345678",
            "again",
            occurred_at=occurred_at + timedelta(seconds=1),
        )

    def resolve() -> None:
        barrier.wait(timeout=5)
        mgr.resolve_open_tool_errors(S1, "Edit", "/repo/a.py#12345678")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(upsert), executor.submit(resolve)]
        barrier.wait(timeout=5)
        for future in futures:
            future.result()

    records = mgr.get_variables(S1)["open_tool_errors"]
    assert len(records) <= 1
    assert len({(record["tool"], record["target_key"]) for record in records}) == len(records)


def test_open_tool_error_sanitized_raw_identity_resolves_canonical_record(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    occurred_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    raw_tool = "## Next Steps\r\nEdit"
    raw_target = "```\x00/repo/a.py"
    mgr.upsert_open_tool_error(
        S1,
        raw_tool,
        raw_target,
        "~~~\nfailed",
        occurred_at=occurred_at,
    )

    record = mgr.get_variables(S1)["open_tool_errors"][0]
    assert record["tool"] == "\\#\\# Next Steps Edit"
    assert record["target_key"] == "\\``` /repo/a.py"
    assert record["error"] == "\\~~~ failed"

    mgr.resolve_open_tool_errors(S1, raw_tool, raw_target)

    assert mgr.get_variables(S1)["open_tool_errors"] == []


def test_append_to_set_variable_and_conditional_merge_applies_updates(db: Any) -> None:
    """Set append and conditional updates happen in one atomic write."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "session_edited_files": ["b.py"],
            "refresh_required": True,
            "audit_events": [{"event": "old"}],
            "kept": "value",
        },
    )

    result = mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "session_edited_files",
        ["a.py", "b.py"],
        condition_name="refresh_required",
        updates={"refresh_required": False, "audit_events": []},
    )

    assert result is True
    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["a.py", "b.py"]
    assert variables["refresh_required"] is False
    assert variables["audit_events"] == []
    assert variables["kept"] == "value"


def test_conditional_append_persists_installed_default_entries(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "listed_servers", ["gobby-tasks"])
    mgr = SessionVariableManager(db)

    mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "listed_servers",
        ["gobby-memory"],
        condition_name="refresh_required",
        updates={},
    )

    assert _stored_variables(db, S1)["listed_servers"] == ["gobby-memory", "gobby-tasks"]


def test_append_to_set_variable_and_conditional_merge_skips_updates_when_guard_false(
    db: Any,
) -> None:
    """Conditional updates are skipped when the guard variable is false."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "refresh_required": False,
            "audit_events": [{"event": "old"}],
        },
    )

    mgr.append_to_set_variable_and_conditional_merge(
        S1,
        "session_edited_files",
        ["a.py"],
        condition_name="refresh_required",
        updates={"refresh_required": False, "audit_events": []},
    )

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["a.py"]
    assert variables["audit_events"] == [{"event": "old"}]


def test_record_edited_file_tracks_sole_claimed_task(db: Any, tmp_path: Path) -> None:
    """Edited files are recorded in both session and task-scoped ledgers."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})

    mgr.record_edited_file(S1, "src/app.py", checkout_root=str(tmp_path))

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert variables["task_edited_files"] == {"task-1": ["src/app.py"]}
    assert variables["task_edited_file_checkouts"] == {"task-1": {str(tmp_path): ["src/app.py"]}}


def test_record_edited_files_atomically_preserves_order_and_deduplicates(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "claimed_tasks": {"task-1": "#1"},
            "session_edited_files": ["existing.py"],
            "task_edited_files": {"task-1": ["existing.py"]},
        },
    )

    mgr.record_edited_files(
        S1,
        ["src/first.py", "docs/plan.md", "src/first.py", "existing.py"],
    )

    variables = mgr.get_variables(S1)
    expected = ["existing.py", "src/first.py", "docs/plan.md"]
    assert variables["session_edited_files"] == expected
    assert variables["task_edited_files"] == {"task-1": expected}


def test_release_task_edited_files_removes_only_requested_task_paths(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "session_edited_files": ["./src/released.py", "./src/remaining.py"],
            "task_edited_files": {
                "task-1": ["./src/released.py", "./src/remaining.py"],
                "task-2": ["src/other.py"],
            },
        },
    )

    released, remaining = mgr.release_task_edited_files(
        S1,
        "task-1",
        ["src/released.py"],
    )

    assert released == ["src/released.py"]
    assert remaining == ["src/remaining.py"]
    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["./src/released.py", "./src/remaining.py"]
    assert variables["task_edited_files"] == {
        "task-1": ["src/remaining.py"],
        "task-2": ["src/other.py"],
    }


def test_release_task_edited_files_preserves_same_path_in_other_checkout(
    db: Any,
    tmp_path: Path,
) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    first_checkout = tmp_path / "first"
    second_checkout = tmp_path / "second"
    first_checkout.mkdir()
    second_checkout.mkdir()
    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})
    mgr.record_edited_file(S1, "src/shared.py", checkout_root=str(first_checkout))
    mgr.record_edited_file(S1, "src/shared.py", checkout_root=str(second_checkout))

    released, remaining = mgr.release_task_edited_files(
        S1,
        "task-1",
        ["src/shared.py"],
        checkout_root=str(first_checkout),
    )

    assert released == ["src/shared.py"]
    assert remaining == ["src/shared.py"]
    variables = mgr.get_variables(S1)
    assert variables["task_edited_file_checkouts"] == {
        "task-1": {
            str(second_checkout): ["src/shared.py"],
        }
    }


def test_release_task_edited_files_pops_last_task_path(db: Any, tmp_path: Path) -> None:
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.task_claim_state import target_task_has_edits

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})
    mgr.record_edited_file(S1, "src/only.py", checkout_root=str(checkout))

    released, remaining = mgr.release_task_edited_files(
        S1,
        "task-1",
        ["src/only.py"],
        checkout_root=str(checkout),
    )

    assert released == ["src/only.py"]
    assert remaining == []
    variables = mgr.get_variables(S1)
    assert "task-1" not in variables["task_edited_files"]
    assert "task-1" not in variables["task_edited_file_checkouts"]
    assert target_task_has_edits(variables, "task-1") is False


def test_record_edited_files_stamps_the_newest_edit_per_task_path(db: Any, tmp_path: Path) -> None:
    """release_task_paths compares each stamp with the path's last commit, so the
    ledger keeps epoch seconds of the newest edit and overwrites it on every edit."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})

    before = time.time()
    mgr.record_edited_files(S1, ["src/app.py", "src/lib.py"], checkout_root=str(tmp_path))
    between = time.time()
    mgr.record_edited_files(S1, ["src/app.py"], checkout_root=str(tmp_path))
    after = time.time()

    times = mgr.get_variables(S1)["task_edited_file_times"]
    assert set(times) == {"task-1"}
    assert set(times["task-1"]) == {"src/app.py", "src/lib.py"}
    assert before <= times["task-1"]["src/lib.py"] <= between
    assert between <= times["task-1"]["src/app.py"] <= after


def test_record_edited_files_without_claim_stamps_nothing(db: Any, tmp_path: Path) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)

    mgr.record_edited_files(S1, ["src/app.py"], checkout_root=str(tmp_path))

    assert "task_edited_file_times" not in _stored_variables(db, S1)


def test_release_task_edited_files_drops_released_stamps(db: Any, tmp_path: Path) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})
    mgr.record_edited_files(S1, ["src/app.py", "src/lib.py"], checkout_root=str(tmp_path))

    released, _remaining = mgr.release_task_edited_files(
        S1, "task-1", ["src/app.py"], checkout_root=str(tmp_path)
    )
    assert released == ["src/app.py"]
    assert set(mgr.get_variables(S1)["task_edited_file_times"]["task-1"]) == {"src/lib.py"}

    released, _remaining = mgr.release_task_edited_files(
        S1, "task-1", ["src/lib.py"], checkout_root=str(tmp_path)
    )
    assert released == ["src/lib.py"]
    assert "task-1" not in mgr.get_variables(S1)["task_edited_file_times"]


def test_record_edited_files_empty_paths_is_noop(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1"}})
    before = mgr.get_variables(S1)

    changed = mgr.record_edited_files(S1, [])

    assert changed is False
    assert mgr.get_variables(S1) == before


def test_record_edited_file_persists_installed_default_entries(db: Any) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    _install_variable_default(db, "session_edited_files", ["seed.py"])
    mgr = SessionVariableManager(db)

    mgr.record_edited_file(S1, "src/app.py")

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

    mgr.record_edited_file(S1, "src/app.py")

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

    mgr.record_edited_file(S1, "src/app.py")

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert variables["task_edited_files"] == {"task-2": ["src/app.py"]}


def test_record_edited_file_does_not_guess_with_multiple_claims(db: Any) -> None:
    """Multiple claimed tasks without active_task_id do not receive task-scoped edits."""
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(S1, {"claimed_tasks": {"task-1": "#1", "task-2": "#2"}})

    mgr.record_edited_file(S1, "src/app.py")

    variables = mgr.get_variables(S1)
    assert variables["session_edited_files"] == ["src/app.py"]
    assert "task_edited_files" not in variables


def test_upsert_bounded_list_variable_replaces_identity_and_updates_companion(
    db: Any,
) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    mgr = SessionVariableManager(db)
    mgr.merge_variables(
        S1,
        {
            "audit_events": [
                {
                    "event_type": "snapshot",
                    "task_id": "task-1",
                    "item_count": 1,
                },
                {
                    "event_type": "manual_note",
                    "task_id": "task-1",
                },
            ],
            "audit_ready": False,
        },
    )

    mgr.upsert_bounded_list_variable(
        S1,
        "audit_events",
        {
            "event_type": "snapshot",
            "task_id": "task-1",
            "item_count": 2,
        },
        identity={"event_type": "snapshot", "task_id": "task-1"},
        max_items=50,
        updates={"audit_ready": True},
    )

    variables = mgr.get_variables(S1)
    assert variables["audit_events"] == [
        {"event_type": "manual_note", "task_id": "task-1"},
        {
            "event_type": "snapshot",
            "task_id": "task-1",
            "item_count": 2,
        },
    ]
    assert variables["audit_ready"] is True
