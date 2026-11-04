"""Seat ownership for stopping or killing an agent run: one helper, synthetic definitions.

Covers the shared helper directly and the two MCP tools that call it, so a refusal is
proven to happen before any delegate that signals or cancels is reached.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.termination_authority import (
    NOT_OWNED_ERROR,
    STOP_ANY_SEAT,
    caller_may_terminate,
    not_owned_refusal,
)
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.conftest import AGENT_TEST_MACHINE_ID

pytestmark = pytest.mark.unit

LEAD_ROLE = "fixture-definition-5a1c0e01"
SEAT_ROLE = "fixture-definition-5a1c0e02"
HOLDER_ROLE = "fixture-definition-5a1c0e03"
RUN_ID = "run-fixture-25659"


def _seat(
    sessions: SessionManager,
    project_id: str,
    external_id: str,
    role: str,
    *,
    parent: str | None = None,
    capabilities: tuple[str, ...] = (),
) -> str:
    definitions = AgentDefinitionManager(sessions.db)
    if definitions.get_by_name(role, project_id) is None:
        definitions.create(
            role,
            {
                "name": role,
                "prompts": {"agent": "Hold the assigned seat."},
                "workflows": {"rule_selectors": {"include": []}},
                "capabilities": list(capabilities),
            },
            project_id=project_id,
        )
    parent_session = sessions.get(parent) if parent else None
    depth = parent_session.agent_depth + 1 if parent_session is not None else 0
    session = sessions.register(
        external_id=external_id,
        machine_id=AGENT_TEST_MACHINE_ID,
        source="codex",
        project_id=project_id,
        title=external_id,
        agent_depth=depth,
        parent_session_id=parent,
    )
    SessionVariableManager(sessions.db).set_variable(session.id, "_agent_type", role)
    return session.id


def _operator_terminal(sessions: SessionManager, project_id: str, external_id: str) -> str:
    """An unspawned terminal: no agent definition, no depth, no parent."""
    session = sessions.register(
        external_id=external_id,
        machine_id=AGENT_TEST_MACHINE_ID,
        source="codex",
        project_id=project_id,
        title=external_id,
    )
    return session.id


@dataclass(frozen=True)
class _Tree:
    operator: str
    lead: str
    worker: str
    sibling: str
    grandchild: str
    stranger_lead: str
    stranger: str
    holder: str


@pytest.fixture
def tree(session_manager: SessionManager, sample_project: dict[str, Any]) -> _Tree:
    """Operator -> lead -> (worker -> grandchild, sibling), plus an unrelated team and a holder."""
    project = sample_project["id"]
    sessions = session_manager
    operator = _operator_terminal(sessions, project, "fixture-operator-terminal")
    lead = _seat(sessions, project, "fixture-lead", LEAD_ROLE, parent=operator)
    worker = _seat(sessions, project, "fixture-worker", SEAT_ROLE, parent=lead)
    sibling = _seat(sessions, project, "fixture-sibling", SEAT_ROLE, parent=lead)
    grandchild = _seat(sessions, project, "fixture-grandchild", SEAT_ROLE, parent=worker)
    stranger_lead = _seat(sessions, project, "fixture-stranger-lead", LEAD_ROLE, parent=operator)
    stranger = _seat(sessions, project, "fixture-stranger", SEAT_ROLE, parent=stranger_lead)
    holder = _seat(
        sessions,
        project,
        "fixture-holder",
        HOLDER_ROLE,
        parent=operator,
        capabilities=(STOP_ANY_SEAT,),
    )
    return _Tree(
        operator=operator,
        lead=lead,
        worker=worker,
        sibling=sibling,
        grandchild=grandchild,
        stranger_lead=stranger_lead,
        stranger=stranger,
        holder=holder,
    )


def test_self_may_terminate_its_own_run(temp_db: HubDatabase, tree: _Tree) -> None:
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.worker,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_direct_spawner_may_terminate_its_child(temp_db: HubDatabase, tree: _Tree) -> None:
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.lead,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_spawner_ancestor_may_terminate_a_descendant(temp_db: HubDatabase, tree: _Tree) -> None:
    """The lead spawned the worker, which spawned the grandchild: the lead is an ancestor."""
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.lead,
        spawner_session_id=tree.worker,
        run_session_id=tree.grandchild,
    )


def test_sibling_seat_is_refused(temp_db: HubDatabase, tree: _Tree) -> None:
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=tree.sibling,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_unrelated_team_seat_is_refused(temp_db: HubDatabase, tree: _Tree) -> None:
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=tree.stranger,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_child_cannot_terminate_its_parent(temp_db: HubDatabase, tree: _Tree) -> None:
    """The worker is a descendant of the lead, so it cannot stop the lead's run."""
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=tree.worker,
        spawner_session_id=tree.operator,
        run_session_id=tree.lead,
    )


def test_declared_capability_admits_an_unrelated_caller(temp_db: HubDatabase, tree: _Tree) -> None:
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.holder,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_unspawned_operator_terminal_is_admitted(temp_db: HubDatabase, tree: _Tree) -> None:
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.operator,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_request_without_a_caller_session_is_admitted(temp_db: HubDatabase, tree: _Tree) -> None:
    """Wrapper traffic without a session is refused upstream, so None is non-agent traffic."""
    assert caller_may_terminate(
        temp_db,
        caller_session_id=None,
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_unknown_caller_session_is_refused(temp_db: HubDatabase, tree: _Tree) -> None:
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=str(uuid.uuid4()),
        spawner_session_id=tree.lead,
        run_session_id=tree.worker,
    )


def test_pending_run_without_child_session_is_owned_by_its_spawner(
    temp_db: HubDatabase, tree: _Tree
) -> None:
    """A run records its spawner before the child session registers."""
    assert caller_may_terminate(
        temp_db,
        caller_session_id=tree.lead,
        spawner_session_id=tree.lead,
        run_session_id=None,
    )
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=tree.sibling,
        spawner_session_id=tree.lead,
        run_session_id=None,
    )


def test_lineage_cycle_terminates_and_refuses_an_unrelated_caller(
    temp_db: HubDatabase, tree: _Tree
) -> None:
    """A corrupt parent loop must not hang the walk or admit an unrelated seat."""
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s",
            (tree.grandchild, tree.worker),
        )
    assert not caller_may_terminate(
        temp_db,
        caller_session_id=tree.stranger,
        spawner_session_id=tree.worker,
        run_session_id=tree.grandchild,
    )


def test_refusal_names_the_run_and_the_typed_code() -> None:
    refusal = not_owned_refusal("run-123")

    assert refusal["success"] is False
    assert refusal["error_code"] == NOT_OWNED_ERROR == "agent_stop_not_owned"
    assert refusal["run_id"] == "run-123"
    assert STOP_ANY_SEAT in refusal["error"]


@dataclass
class _Delegates:
    stop_run: AsyncMock
    terminate: AsyncMock
    complete_self: AsyncMock


@pytest.fixture
def delegates() -> Iterator[_Delegates]:
    """The paths that signal or cancel a run; every refusal must leave them untouched."""
    ok = {"success": True}
    with (
        patch(
            "gobby.mcp_proxy.tools.agents_lifecycle_tools.stop_agent_run",
            new_callable=AsyncMock,
            return_value=ok,
        ) as stop_run,
        patch(
            "gobby.mcp_proxy.tools.agents_lifecycle_tools.terminate_agent_run",
            new_callable=AsyncMock,
            return_value=ok,
        ) as terminate,
        patch(
            "gobby.mcp_proxy.tools.agents._complete_self_terminated_run",
            new_callable=AsyncMock,
            return_value=ok,
        ) as complete_self,
    ):
        yield _Delegates(stop_run=stop_run, terminate=terminate, complete_self=complete_self)


def _run(child: str | None, spawner: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=RUN_ID,
        status="running",
        child_session_id=child,
        parent_session_id=spawner,
        terminal_id=None,
    )


def _registry(session_manager: SessionManager, db: HubDatabase, run: SimpleNamespace) -> Any:
    runner = MagicMock()
    runner.get_run.return_value = run
    return create_agents_registry(runner, session_manager=session_manager, db=db)


async def _call(registry: Any, tool: str, caller: str) -> dict[str, Any]:
    with session_context_for_test(caller):
        result: dict[str, Any] = await registry._tools[tool].func(run_id=RUN_ID)
    return result


def _signalling_delegate(delegates: _Delegates, tool: str) -> AsyncMock:
    return delegates.stop_run if tool == "stop_agent" else delegates.terminate


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["stop_agent", "kill_agent"])
async def test_sibling_is_refused_before_any_signal_or_cancel(
    tool: str,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tree: _Tree,
    delegates: _Delegates,
) -> None:
    registry = _registry(session_manager, temp_db, _run(child=tree.worker, spawner=tree.lead))

    result = await _call(registry, tool, tree.sibling)

    assert result["success"] is False
    assert result["error_code"] == NOT_OWNED_ERROR
    assert result["run_id"] == RUN_ID
    assert STOP_ANY_SEAT in result["error"]
    delegates.stop_run.assert_not_awaited()
    delegates.terminate.assert_not_awaited()
    delegates.complete_self.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["stop_agent", "kill_agent"])
async def test_child_cannot_stop_its_parent(
    tool: str,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tree: _Tree,
    delegates: _Delegates,
) -> None:
    registry = _registry(session_manager, temp_db, _run(child=tree.lead, spawner=tree.operator))

    result = await _call(registry, tool, tree.worker)

    assert result["error_code"] == NOT_OWNED_ERROR
    delegates.stop_run.assert_not_awaited()
    delegates.terminate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["stop_agent", "kill_agent"])
@pytest.mark.parametrize("caller_attr", ["lead", "holder", "operator"])
async def test_spawner_capability_and_operator_are_admitted(
    tool: str,
    caller_attr: str,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tree: _Tree,
    delegates: _Delegates,
) -> None:
    registry = _registry(session_manager, temp_db, _run(child=tree.worker, spawner=tree.lead))

    result = await _call(registry, tool, getattr(tree, caller_attr))

    assert result["success"] is True
    _signalling_delegate(delegates, tool).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["stop_agent", "kill_agent"])
async def test_spawner_ancestor_is_admitted_for_a_grandchild_run(
    tool: str,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tree: _Tree,
    delegates: _Delegates,
) -> None:
    registry = _registry(session_manager, temp_db, _run(child=tree.grandchild, spawner=tree.worker))

    result = await _call(registry, tool, tree.lead)

    assert result["success"] is True
    _signalling_delegate(delegates, tool).assert_awaited_once()


@pytest.mark.asyncio
async def test_self_termination_is_admitted_through_the_self_path(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tree: _Tree,
    delegates: _Delegates,
) -> None:
    registry = _registry(session_manager, temp_db, _run(child=tree.worker, spawner=tree.lead))

    result = await _call(registry, "kill_agent", tree.worker)

    assert result["success"] is True
    delegates.complete_self.assert_awaited_once()
    delegates.terminate.assert_not_awaited()
