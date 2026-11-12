"""A /clear successor keeps its predecessor's seat, its run, and its spawn depth."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.hooks.event_handlers._session_start.agents import (
    activate_default_agent,
    build_agent_changes,
    resolve_agent_name,
)
from gobby.hooks.event_handlers._session_start.claims import MCP_PROXY_READY_VARIABLE
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.mcp_proxy.tools.apply_agent_definition import (
    apply_agent_definition_impl,
)
from gobby.sessions.clear_continuation import stage_clear_attempt
from gobby.sessions.handoff import HANDOFF_PULL_PENDING_VARIABLE
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal
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
async def test_clear_successor_discards_agent_type_and_pin(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "reset-seat")
    _define(temp_db, ["claim"])
    assert (await apply_agent_definition_impl(_SEAT, temp_db, pane.predecessor_id))["success"]
    variables = SessionVariableManager(temp_db)
    old_pin = variables.get_variables(pane.predecessor_id)["_agent_definition_hash"]
    _stage(temp_db, pane)
    successor = _start(pane, "cleared-thread")
    after = variables.get_variables(successor)
    assert after["_agent_type"] == "default"
    assert after.get("_agent_definition_hash") != old_pin
    assert after[HANDOFF_PULL_PENDING_VARIABLE] is True
    assert AgentStepInstanceManager(temp_db).get_for_session(successor) is None


@pytest.mark.parametrize("base", ["default", "configured-base"])
def test_clear_ignores_configured_previous_identity(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, f"base-{base}")
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository.read",
        lambda *args, **kwargs: SimpleNamespace(values={"default_agent": "configured-base"}),
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(pane.predecessor_id, {"_agent_type": base})
    _stage(temp_db, pane)
    successor = _start(pane, "cleared-thread", activate=False)
    assert variables.get_variables(successor)["_agent_type"] == "default"
    handler = _seat_handler(pane.sessions)
    assert resolve_agent_name(handler, successor, "configured-base") == "default"


def test_config_failure_preserves_clear_handoff_state(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "config-failure")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(pane.predecessor_id, {MCP_PROXY_READY_VARIABLE: True})
    _stage(temp_db, pane)
    with patch(
        "gobby.storage.config_repository.ConfigRepository.read", side_effect=RuntimeError("offline")
    ):
        successor = _start(pane, "cleared-thread", activate=False)
    after = variables.get_variables(successor)
    assert after[HANDOFF_PULL_PENDING_VARIABLE] is True
    assert after[MCP_PROXY_READY_VARIABLE] is True
    assert after["_agent_type"] == "default"
