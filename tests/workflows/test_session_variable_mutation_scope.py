"""Real PostgreSQL allocation and preservation checks for shared mutations."""

import json
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows import state_manager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_tool_bindings import TaskToolBindings, cleanup_task_tool_bindings

pytestmark = pytest.mark.integration


@pytest.fixture
def mutation_session(session_manager: SessionManager, sample_project: dict[str, Any]) -> str:
    return session_manager.register(
        external_id="bounded-shared-mutation",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    ).id


@pytest.mark.parametrize(
    "operation", ["set", "merge", "reconcile", "set-list", "binding", "binding-cleanup"]
)
def test_shared_mutations_do_not_decode_unrelated_variables(
    temp_db: HubDatabase,
    mutation_session: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    manager = SessionVariableManager(temp_db)
    unrelated = "x" * (4 * 1024 * 1024)
    manager.merge_variables(mutation_session, {"unrelated": unrelated, "preserved": {"x": 1}})
    manager.get_variables(mutation_session)  # Warm defaults and the connection before tracing.
    decode_sizes: list[int] = []
    decode = state_manager._decode_variables_payload

    def observe_decode(payload: Any) -> dict[str, Any]:
        decode_sizes.append(len(payload) if isinstance(payload, str) else len(json.dumps(payload)))
        return decode(payload)

    monkeypatch.setattr(state_manager, "_decode_variables_payload", observe_decode)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="bounded-shared-mutation",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "exec_command"},
        request_id="bounded-call",
    )
    bindings = TaskToolBindings(manager, mutation_session)
    if operation == "binding-cleanup":
        bindings.start(event)
    tracemalloc.start()
    try:
        for index in range(4):
            if operation == "set":
                manager.set_variable(mutation_session, "code_index_available", True)
            elif operation == "merge":
                manager.merge_variables(mutation_session, {"small": index})
            elif operation == "reconcile":
                manager.merge_variables(mutation_session, {"small": index}, reconcile_claims=True)
            elif operation == "set-list":
                manager.append_to_set_variable(mutation_session, "seen", [str(index)])
            elif operation == "binding-cleanup":
                cleanup_task_tool_bindings(
                    replace(event, event_type=HookEventType.STOP), manager, mutation_session, {}
                )
            else:
                bindings.start(event)
                assert bindings.started_at(event) == event.timestamp.timestamp()
                bindings.complete(event)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert decode_sizes
    assert max(decode_sizes) < 4096
    assert peak < 1024 * 1024
    stored = manager.get_variables(mutation_session)
    assert stored["unrelated"] == unrelated
    assert stored["preserved"] == {"x": 1}
    if operation == "set":
        assert stored["code_index_available"] is True
    elif operation in {"merge", "reconcile"}:
        assert stored["small"] == 3
        if operation == "reconcile":
            assert stored["claimed_tasks"] == {}
            assert stored["task_claimed"] is False
    elif operation == "set-list":
        assert stored["seen"] == ["0", "1", "2", "3"]
    elif operation == "binding-cleanup":
        assert stored["task_tool_bindings"] == {}
    else:
        assert len(stored["task_tool_bindings"]) == 1
        assert next(iter(stored["task_tool_bindings"].values()))["pending"] is False


def test_scoped_mutation_preserves_defaults_overrides_history_and_deletions(
    temp_db: HubDatabase, mutation_session: str
) -> None:
    defaults = SessionVariableDefaultManager(temp_db)
    defaults.create(name="installed_only", default_value=["default"])
    defaults.create(name="stored_override", default_value=["default"])
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(
        mutation_session,
        {"stored_override": None, "delete_me": True, "active_task_id": "task-one"},
    )
    history = manager.get_variables(mutation_session)["task_selection_history"]

    def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
        variables.pop("delete_me")
        variables["task_selection_history"] = [{"task_id": "injected"}]
        return None, True

    manager._mutate_variables(mutation_session, mutate, keys=("delete_me",), apply_defaults=True)
    row = temp_db.fetchone(
        "SELECT variables::text AS variables FROM session_variables WHERE session_id = %s",
        (mutation_session,),
    )
    assert row is not None
    stored = json.loads(row["variables"])
    assert stored["installed_only"] == ["default"]  # Persisted, not just layered on read.
    assert stored["stored_override"] is None
    assert "delete_me" not in stored
    assert stored["task_selection_history"] == history


def test_noop_callback_and_existing_only_merge_preserve_stored_state(
    temp_db: HubDatabase, mutation_session: str
) -> None:
    manager = SessionVariableManager(temp_db)
    assert manager.merge_existing_variables(mutation_session, {"small": 1}) is False
    manager.merge_variables(mutation_session, {"small": 1, "unrelated": [1, 2]})
    query = "SELECT variables::text AS variables, updated_at FROM session_variables WHERE session_id = %s"
    before = temp_db.fetchone(query, (mutation_session,))

    def noop(variables: dict[str, Any]) -> tuple[str, bool]:
        variables.pop("small")
        return "unchanged", False

    assert manager._mutate_variables(mutation_session, noop, keys=("small",)) == "unchanged"
    assert manager.merge_existing_variables(mutation_session, {"small": 1}) is False
    assert temp_db.fetchone(query, (mutation_session,)) == before
    assert manager.merge_existing_variables(mutation_session, {"small": 2}) is True
    assert manager.get_variables(mutation_session)["unrelated"] == [1, 2]


def test_concurrent_first_mutations_keep_every_disjoint_key(
    temp_db: HubDatabase, mutation_session: str
) -> None:
    manager = SessionVariableManager(temp_db)

    def write(index: int) -> None:
        manager.set_variable(mutation_session, f"worker_{index}", index)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(8)))
    stored = manager.get_variables(mutation_session)
    assert {key: stored[key] for key in (f"worker_{index}" for index in range(8))} == {
        f"worker_{index}": index for index in range(8)
    }


@pytest.mark.parametrize("payload", [None, [1], "invalid-root"])
def test_mutation_replaces_non_object_stored_roots(
    temp_db: HubDatabase, mutation_session: str, payload: Any
) -> None:
    temp_db.execute(
        "INSERT INTO session_variables (session_id, variables, updated_at) "
        "VALUES (%s, %s::jsonb, CURRENT_TIMESTAMP)",
        (mutation_session, json.dumps(payload)),
    )
    manager = SessionVariableManager(temp_db)
    manager.set_variable(mutation_session, "small", True)
    assert manager.get_variables(mutation_session) == {"small": True}


@pytest.mark.parametrize("operation", ["set", "existing", "set-list"])
def test_projected_mutations_sanitize_nul_variable_names(
    temp_db: HubDatabase, mutation_session: str, operation: str
) -> None:
    manager = SessionVariableManager(temp_db)
    manager.set_variable(mutation_session, "preserved", True)
    name = "unusual\x00name"
    if operation == "set":
        manager.set_variable(mutation_session, name, True)
    elif operation == "existing":
        assert manager.merge_existing_variables(mutation_session, {name: True}) is True
    else:
        manager.append_to_set_variable(mutation_session, name, ["value"])
    stored = manager.get_variables(mutation_session)
    assert stored["preserved"] is True
    assert stored["unusual\ufffdname"] == (["value"] if operation == "set-list" else True)
