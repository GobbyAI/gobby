"""A /clear successor keeps its predecessor's seat, its run, and its spawn depth."""

from __future__ import annotations

import os
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.agents.session import ChildSessionManager
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.agents import (
    activate_default_agent,
    build_agent_changes,
    resolve_agent_name,
)
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_activation import _ensure_step_instance
from gobby.mcp_proxy.tools.apply_agent_definition import (
    apply_agent_definition_impl,
    definition_pin,
)
from gobby.sessions.clear_continuation import stage_clear_attempt
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.agents.terminal_fixtures import make_live_terminal
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.hooks.test_session_materialize import (
    _ACTIVATION_STUBS,
    _ATTEMPT_ID,
    _MATERIALIZE,
    _event,
    _handler,
    _StagedClear,
)

pytestmark = pytest.mark.unit

_SEAT = "seat"


def _define(db: HubDatabase, steps: list[str]) -> None:
    AgentDefinitionManager(db).upsert_with_steps(
        _SEAT,
        {
            "name": _SEAT,
            "surfaces": ["persona", "spawn"],
            "prompts": {"persona": "Seat.", "agent": "Work."},
            "workflows": {
                "variables": {"seat_only": 1},
                "rule_selectors": {"include": [], "exclude": []},
            },
        },
        {"steps": [{"name": step} for step in steps]},
        source="custom",
    )


def _pane(
    db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> _StagedClear:
    """One pane whose CLI process is this test process, with its first session registered."""
    checkout = install_isolated_checkout_project(
        db, tmp_path / name, name=name, monkeypatch=monkeypatch
    )
    pane = _StagedClear(
        sessions=SessionManager(db),
        machine_id=checkout.machine_id,
        project_id=checkout.project.id,
        root=checkout.root_path,
        term={"parent_pid": os.getpid(), "parent_create_time": psutil.Process().create_time()},
    )
    pane.predecessor_id = pane.register("pred-ext")
    return pane


def _stage(db: HubDatabase, pane: _StagedClear) -> None:
    stage_clear_attempt(
        db,
        pane.predecessor_id,
        attempt_id=_ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="Ready.", next_steps=["Continue."]),
        terminal_context=pane.term,
        chat_context=None,
    )


def _spawned_seat(db: HubDatabase, pane: _StagedClear) -> tuple[AgentRun, Terminal]:
    """Bind the predecessor to a running run in a live Gobby terminal, as spawn does."""
    root_id = pane.sessions.register_session(
        external_id="root-ext",
        machine_id=pane.machine_id,
        source="grok",
        project_id=pane.project_id,
    )
    runs = LocalAgentRunManager(db)
    created = runs.create(
        parent_session_id=root_id,
        provider="claude",
        prompt="Seat.",
        agent_name=_SEAT,
        child_session_id=pane.predecessor_id,
    )
    run = runs.start(created.id)
    assert run is not None
    db.execute(
        "UPDATE sessions SET agent_run_id = %s, agent_depth = 1, spawned_by_agent_id = %s "
        "WHERE id = %s",
        (run.id, "parent-agent", pane.predecessor_id),
    )
    return run, make_live_terminal(run, backend="native", db=db)


def _seat_handler(sessions: SessionManager) -> MagicMock:
    handler = _handler(sessions)
    handler._resolve_agent_name = lambda sid, override, existing=None: resolve_agent_name(
        handler, sid, override, existing
    )
    handler._build_agent_changes = lambda *args: build_agent_changes(handler, *args)
    handler._activate_default_agent = lambda *args, **kwargs: activate_default_agent(
        handler, *args, **kwargs
    )
    return handler


def _start(
    pane: _StagedClear,
    external_id: str,
    *,
    supersedes: str | None = None,
    activate: bool = True,
) -> str:
    """Run the successor's SessionStart activation on the pane, as the clear source."""
    successor_id = pane.register(external_id)
    session = pane.sessions.get(successor_id)
    assert session is not None
    data: dict[str, object] = {"source": "clear", "cwd": pane.root}
    if not activate:
        data["skip_default_agent_activation"] = True
    event = _event(data)
    event.task_id = None
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=pane.sessions.get(pane.predecessor_id),
        clear_attempt_id=_ATTEMPT_ID,
        clear_supersedes=supersedes,
    )
    with ExitStack() as stack:
        for name in _ACTIVATION_STUBS:
            stack.enter_context(patch(f"{_MATERIALIZE}.{name}", MagicMock()))
        activate_materialized_session(
            _seat_handler(pane.sessions),
            event,
            successor_id,
            resolution=resolution,
            session_obj=session,
            project_id=pane.project_id,
            transcript_path=None,
            terminal_context=pane.term,
        )
    return successor_id


def _session(pane: _StagedClear, session_id: str) -> Any:
    session = pane.sessions.get(session_id)
    assert session is not None
    return session


@pytest.mark.asyncio
async def test_clear_successor_inherits_agent_type_and_pin(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "inherit")
    _define(temp_db, ["claim"])
    assert (await apply_agent_definition_impl(_SEAT, temp_db, pane.predecessor_id))["success"]
    variables = SessionVariableManager(temp_db)
    pin = variables.get_variables(pane.predecessor_id)["_agent_definition_hash"]
    _stage(temp_db, pane)

    successor_id = _start(pane, "succ-ext")

    after = variables.get_variables(successor_id)
    assert after["_agent_type"] == _SEAT
    assert after["_agent_definition_hash"] == pin
    # The definition's own variable comes from the successor's activation, not the copy.
    assert after["seat_only"] == 1
    assert not after.get("_agent_definition_drift")
    assert after["is_spawned_agent"] is False


@pytest.mark.asyncio
async def test_clear_successor_gets_fresh_step_instance(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "steps")
    _define(temp_db, ["claim", "implement"])
    assert (await apply_agent_definition_impl(_SEAT, temp_db, pane.predecessor_id))["success"]
    instances = AgentStepInstanceManager(temp_db)
    running = instances.get_for_session(pane.predecessor_id)
    assert running is not None
    running.current_step = "implement"
    instances.save(running)
    variables = SessionVariableManager(temp_db)
    old_pin = variables.get_variables(pane.predecessor_id)["_agent_definition_hash"]
    _define(temp_db, ["replacement", "verify"])
    _stage(temp_db, pane)

    successor_id = _start(pane, "succ-ext")
    after = variables.get_variables(successor_id)
    _ensure_step_instance(temp_db, successor_id, after, _session(pane, successor_id))

    fresh = instances.get_for_session(successor_id)
    assert fresh is not None
    assert fresh.id != running.id
    assert fresh.current_step == "replacement"
    resolved = resolve_agent(_SEAT, temp_db, project_id=pane.project_id, cli_source="grok")
    assert resolved is not None
    assert after["_agent_definition_hash"] == definition_pin(resolved)
    assert after["_agent_definition_hash"] != old_pin
    assert "changed since" in after["_agent_definition_drift"]


@pytest.mark.parametrize("base", ["default", "configured-base"])
def test_base_agent_clear_successor_unchanged(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, f"base-{base}")
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository.read",
        lambda *args, **kwargs: SimpleNamespace(values={"default_agent": "configured-base"}),
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        pane.predecessor_id, {"_agent_type": base, "_agent_definition_hash": "base-pin"}
    )
    _stage(temp_db, pane)

    successor_id = _start(pane, "succ-ext", activate=False)

    after = variables.get_variables(successor_id)
    assert "_agent_type" not in after
    assert "_agent_definition_hash" not in after
    assert _session(pane, successor_id).agent_run_id is None


@pytest.mark.asyncio
async def test_spawned_interactive_seat_keeps_run_binding_after_clear(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_dependencies: dict[str, Any],
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "spawned")
    _define(temp_db, ["claim"])
    # A spawned session's definition is fixed, so the seat is activated before the run binds.
    assert (await apply_agent_definition_impl(_SEAT, temp_db, pane.predecessor_id))["success"]
    run, terminal = _spawned_seat(temp_db, pane)
    # The termination sweep is machine-scoped; a foreign machine id would make
    # its negative check below vacuous.
    assert run.machine_id == pane.machine_id
    runs = LocalAgentRunManager(temp_db)
    child = runs.create(parent_session_id=pane.predecessor_id, provider="claude", prompt="Kid.")
    _stage(temp_db, pane)
    terminals = TerminalManager(temp_db)

    handlers = EventHandlers(
        **{**mock_dependencies, "session_storage": pane.sessions, "session_manager": pane.sessions},
        terminal_manager=terminals,
    )
    handlers.handle_session_end(
        HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="pred-ext",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={"reason": "clear", "terminal_context": pane.term},
            machine_id=pane.machine_id,
            metadata={"_platform_session_id": pane.predecessor_id},
        )
    )

    mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
    ended = runs.get(run.id)
    assert ended is not None and ended.status == "running"
    held = terminals.get(terminal.id)
    assert held is not None and held.state == "live"

    successor_id = _start(pane, "succ-ext")

    bound = runs.get(run.id)
    assert bound is not None
    assert bound.child_session_id == successor_id
    assert _session(pane, successor_id).agent_run_id == run.id
    assert _session(pane, pane.predecessor_id).agent_run_id is None
    moved = terminals.get(terminal.id)
    assert moved is not None and (moved.session_id, moved.state) == (successor_id, "live")
    assert SessionVariableManager(temp_db).get_variables(successor_id)["is_spawned_agent"] is True
    spawned = runs.get(child.id)
    assert spawned is not None and spawned.parent_session_id == successor_id
    candidates = {row.id for row in runs.list_termination_candidates(machine_id=pane.machine_id)}
    assert run.id not in candidates
    # Control: the same sweep does pick the run up once its bound session expires.
    pane.sessions.update_status_if_non_terminal(successor_id, "expired")
    candidates = {row.id for row in runs.list_termination_candidates(machine_id=pane.machine_id)}
    assert run.id in candidates


def test_superseding_successor_takes_the_run(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "supersede")
    run, terminal = _spawned_seat(temp_db, pane)
    _stage(temp_db, pane)
    runs = LocalAgentRunManager(temp_db)
    stale_id = _start(pane, "stale-ext", activate=False)
    taken = runs.get(run.id)
    assert taken is not None and taken.child_session_id == stale_id

    newcomer_id = _start(pane, "newcomer-ext", supersedes=stale_id, activate=False)

    bound = runs.get(run.id)
    assert bound is not None and bound.child_session_id == newcomer_id
    assert _session(pane, newcomer_id).agent_run_id == run.id
    assert _session(pane, stale_id).agent_run_id is None
    moved = TerminalManager(temp_db).get(terminal.id)
    assert moved is not None and moved.session_id == newcomer_id


def test_clear_successor_keeps_spawn_depth(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "depth")
    temp_db.execute(
        "UPDATE sessions SET agent_depth = 5, spawned_by_agent_id = %s WHERE id = %s",
        ("parent-agent", pane.predecessor_id),
    )
    _stage(temp_db, pane)

    # Neither SessionStart payload carries agent_depth, so both rows register at 0.
    successor_id = _start(pane, "succ-ext", activate=False)
    newcomer_id = _start(pane, "newcomer-ext", supersedes=successor_id, activate=False)

    spawning = ChildSessionManager(pane.sessions, max_agent_depth=5)
    for session_id in (successor_id, newcomer_id):
        row = _session(pane, session_id)
        assert (row.agent_depth, row.spawned_by_agent_id) == (5, "parent-agent")
        allowed, _reason, depth = spawning.can_spawn_child(session_id)
        assert (allowed, depth) == (False, 5)
