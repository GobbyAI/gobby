"""Real PostgreSQL allocation and preservation checks for shared mutations."""

import json
import logging
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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


@pytest.mark.parametrize(
    "operation",
    [
        "claims",
        "seed",
        "agent-name",
        "definition",
        "rule-set",
        "delivery-count",
        "memory-ids",
        "grok-ack",
        "step-recovery",
        "terminal-stop",
        "start-context",
    ],
)
def test_small_read_consumers_do_not_decode_unrelated_variables(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    mutation_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    from gobby.hooks.event_handlers._session_start.agents import (
        _seed_parent_turn_seq,
        resolve_agent_name,
    )
    from gobby.hooks.event_handlers._session_start.claims import rehydrate_found_work_gate_arm
    from gobby.hooks.event_handlers._session_start.context import classify_session_start_context
    from gobby.hooks.grok_pending_context import (
        DELIVERY_VARIABLE,
        handle_ack_pending_inbox_envelope,
    )
    from gobby.hooks.rule_evaluator import _committed_set_values
    from gobby.hooks.session_activation import _ensure_step_instance
    from gobby.hooks.terminal_handoff_delivery import (
        _consecutive_delivery_failures,
        schedule_staged_handoff_on_stop,
    )
    from gobby.mcp_proxy.tools.apply_agent_definition import commit_definition_changes
    from gobby.mcp_proxy.tools.memory_review import _accessed_ids
    from gobby.mcp_proxy.tools.memory_session import ACCESSED_MEMORY_IDS_VARIABLE
    from gobby.sessions.handoff import HANDOFF_DELIVERY_FAILURES_VARIABLE
    from gobby.storage.session_tasks import SessionTaskManager

    manager = SessionVariableManager(temp_db)
    unrelated = "x" * (4 * 1024 * 1024)
    manager.merge_variables(
        mutation_session,
        {
            "unrelated": unrelated,
            "_agent_type": "allocation-test",
            "_agent_definition_hash": "old-pin",
            "_persona_name": "live-persona",
            "_active_skill_names": ["persona-skill"],
            "_agent_context_injected": True,
            "seen": ["one"],
            HANDOFF_DELIVERY_FAILURES_VARIABLE: 3,
            ACCESSED_MEMORY_IDS_VARIABLE: [{"task_id": "task-one", "memory_id": "memory-one"}],
            DELIVERY_VARIABLE: {
                "envelope_id": "envelope-one",
                "components": [{"id": "one", "text": "pending"}],
            },
        },
    )
    decode_sizes: list[int] = []
    decode = state_manager._decode_variables_payload

    def observe_decode(payload: Any) -> dict[str, Any]:
        decode_sizes.append(len(payload) if isinstance(payload, str) else len(json.dumps(payload)))
        return decode(payload)

    monkeypatch.setattr(state_manager, "_decode_variables_payload", observe_decode)
    handler: Any = SimpleNamespace(
        _session_manager=session_manager,
        _session_task_manager=SessionTaskManager(temp_db),
        logger=logging.getLogger(__name__),
    )
    if operation == "claims":
        rehydrate_found_work_gate_arm(handler, mutation_session)
    elif operation == "seed":
        _seed_parent_turn_seq(handler, mutation_session)
        assert manager.get_variable_subset(mutation_session, ("parent_turn_seq",)) == {
            "parent_turn_seq": 0
        }
    elif operation == "agent-name":
        assert resolve_agent_name(handler, mutation_session, None) == "allocation-test"
    elif operation == "definition":
        result = commit_definition_changes(
            temp_db,
            mutation_session,
            "allocation-test",
            {
                "_agent_type": "allocation-test",
                "_agent_definition_hash": "new-pin",
                "_agent_definition_keys": ["_agent_type", "_active_skill_names"],
                "_active_skill_names": ["replacement"],
            },
            expected_agent_type="allocation-test",
            relaunch=False,
            same_pin_noop=False,
        )
        assert result["status"] == "applied"
        assert result["variables"]["_active_skill_names"] == ["persona-skill"]
    elif operation == "rule-set":
        assert _committed_set_values(manager, mutation_session, "seen") == {"one"}
    elif operation == "delivery-count":
        assert _consecutive_delivery_failures(temp_db, mutation_session) == 3
    elif operation == "memory-ids":
        assert _accessed_ids(manager, [mutation_session], "task-one") == ["memory-one"]
    elif operation == "grok-ack":
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text("{}")
        removed: list[str] = []
        assert (
            handle_ack_pending_inbox_envelope(
                handler,
                "envelope-one",
                {"headers": {"X-Gobby-Session-Id": mutation_session}},
                envelope_path,
                remove_marker=removed.append,
            )
            is True
        )
        assert envelope_path.exists()
        assert removed == []
    elif operation == "terminal-stop":
        event = HookEvent(
            event_type=HookEventType.STOP,
            session_id="bounded-shared-mutation",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": mutation_session},
        )
        assert (
            schedule_staged_handoff_on_stop(
                event,
                session_manager=session_manager,
                agent_run_manager=MagicMock(),
                event_loop=None,
            )
            is False
        )
    elif operation == "start-context":
        decision = classify_session_start_context(
            handler,
            session_id=mutation_session,
            session=session_manager.get(mutation_session),
            session_source="resume",
            is_existing_session=True,
        )
        assert decision.mode == "live"
    else:
        assert (
            _ensure_step_instance(
                temp_db,
                mutation_session,
                {"_agent_type": "allocation-test"},
                session_manager.get(mutation_session),
            )
            is False
        )
    assert decode_sizes
    assert max(decode_sizes) < 4096
    assert manager.get_variables(mutation_session)["unrelated"] == unrelated


def test_activation_reconciliation_projects_its_invariants(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    mutation_session: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.session_activation import (
        MARKER_COMPLETED,
        MARKER_HASH,
        MARKER_VERSION,
        SESSION_ACTIVATION_CONTRACT_HASH,
        SESSION_ACTIVATION_CONTRACT_VERSION,
        reconcile_session_activation,
    )

    manager = SessionVariableManager(temp_db)
    unrelated = "x" * (4 * 1024 * 1024)
    manager.merge_variables(
        mutation_session,
        {
            "unrelated": unrelated,
            "_agent_type": "allocation-test",
            "_active_rule_names": [],
            "_active_skill_names": [],
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": False,
            "baseline_dirty_files": ["baseline.py"],
            "session_edited_files": ["edited.py"],
            "active_task_id": None,
            "task_edited_files": {},
            "step_workflow_complete": False,
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
        },
    )
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
        data={},
        metadata={"_platform_session_id": mutation_session},
    )
    result = reconcile_session_activation(event, SimpleNamespace(_session_manager=session_manager))
    assert result.reason == "current"
    assert result.changed is False
    assert decode_sizes
    assert max(decode_sizes) < 4096
    stored = manager.get_variables(mutation_session)
    assert stored["unrelated"] == unrelated
    assert stored["baseline_dirty_files"] == ["baseline.py"]
    assert stored["session_edited_files"] == ["edited.py"]
