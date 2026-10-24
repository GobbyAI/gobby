"""gobby-agents cold_start_admin: the /gobby admin and /gobby assistant pane cold start."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager, WorkspacePane
from gobby.terminals.actor_scope import OPERATOR_ACTOR
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import PreparedSpawn, TerminalSpawnRequest
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import (
    reset_request_principal,
    session_context_for_test,
    set_request_principal,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@dataclass
class _NativeRuntime(FakeRuntime):
    """Native fake whose host epoch follows each spawn, so live locator keys stay unique."""

    backend: Literal["tmux", "native"] = "native"

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.host_epoch = str(request.terminal_id)
        return await super().prepare_spawn(request)


@dataclass
class _Stack:
    server: WebSocketServer
    db: HubDatabase
    sessions: SessionManager
    terminals: TerminalManager
    workspaces: WorkspaceManager
    native: _NativeRuntime
    agents: InternalToolRegistry
    project_id: str

    @property
    def ops(self) -> WorkspaceOps:
        assert self.server.workspace_ops is not None
        return self.server.workspace_ops


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
async def stack(temp_db: HubDatabase, sample_project: dict[str, Any]) -> AsyncIterator[_Stack]:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    sync_bundled_agents(temp_db)
    config = MagicMock(host="localhost", port=60888, ping_interval=30, ping_timeout=10)
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), MagicMock())
    sessions = SessionManager(temp_db)
    server.session_manager = sessions
    terminals = TerminalManager(temp_db)
    native = _NativeRuntime()
    registry = runtime_registry(FakeRuntime(backend="tmux"), native)
    leases = TerminalLeaseRegistry(daemon_epoch="admin-cold-start-epoch")
    workspaces = WorkspaceManager(temp_db)
    server.configure_terminals(
        terminals,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(terminals, registry, lease_registry=leases),
        workspace_manager=workspaces,
    )
    agents = create_agents_registry(
        MagicMock(),
        session_manager=sessions,
        db=temp_db,
        workspace_ops_resolver=lambda: server.workspace_ops,
    )
    yield _Stack(
        server,
        temp_db,
        sessions,
        terminals,
        workspaces,
        native,
        agents,
        str(sample_project["id"]),
    )
    await leases.shutdown_lifecycle_publication()


@contextmanager
def _principal(principal: AgentApiTokenClaims | None | Literal[False]) -> Iterator[None]:
    async def resolve() -> AgentApiTokenClaims | None | Literal[False]:
        return principal

    token = set_request_principal(resolve)
    try:
        yield
    finally:
        reset_request_principal(token)


async def _operator_pane(stack: _Stack) -> tuple[str, WorkspacePane]:
    """The pane the operator opens in gclient: a shell in the project checkout."""
    workspace = await stack.ops.workspace_create(OPERATOR_ACTOR, "admin")
    layout = await stack.ops.tab_create(OPERATOR_ACTOR, workspace.id, stack.project_id)
    return workspace.id, layout.panes[0]


def _pane(stack: _Stack, workspace_id: str, pane_id: str) -> WorkspacePane:
    return next(pane for pane in stack.workspaces.list_panes(workspace_id) if pane.id == pane_id)


def _pane_session(stack: _Stack, pane: WorkspacePane, name: str, source: str) -> Session:
    """The provider session the operator starts in ``pane``."""
    assert pane.terminal_id is not None
    return stack.sessions.register(
        external_id=name,
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id=stack.project_id,
        terminal_context={"gobby_terminal_id": pane.terminal_id},
    )


async def _cold_start(stack: _Stack, session: Session, role: str) -> dict[str, Any]:
    with _principal(None), session_context_for_test(session.id):
        result = await stack.agents.call("cold_start_admin", {"role": role})
    assert isinstance(result, dict)
    return result


def _agent_type(stack: _Stack, session: Session) -> object:
    return (
        SessionVariableManager(stack.db)
        .get_variable_subset(session.id, ("_agent_type",))
        .get("_agent_type")
    )


def _ref(stack: _Stack, session: Session) -> str:
    stored = stack.sessions.get(session.id)
    assert stored is not None
    return stored.ref


async def test_cold_start_twice_from_each_side_opens_one_counterpart_pane(stack: _Stack) -> None:
    """The Orchestrator pane opens the Assistant pane once; every re-run reuses it."""
    workspace_id, orchestrator_pane = await _operator_pane(stack)
    orchestrator = _pane_session(stack, orchestrator_pane, "orchestrator-pane", "claude")
    spawns = stack.native.create_calls
    writes = len(stack.native.write_log)

    first = await _cold_start(stack, orchestrator, "orchestrator")
    assert first["success"] is True, first
    assert (first["definition"], first["created"]) == ("applied", True)
    assert first["session"] == _ref(stack, orchestrator)
    assistant_pane_id = first["counterpart"]["pane"]
    assistant_pane = _pane(stack, workspace_id, assistant_pane_id)
    assert (assistant_pane.tab_id, assistant_pane.role) == (orchestrator_pane.tab_id, "assistant")
    assert first["counterpart"]["session"] is None
    assert stack.native.create_calls == spawns + 1
    assert stack.native.write_log[writes] == ("text", "claude '/gobby assistant'\n")
    launched = len(stack.native.write_log)

    again = await _cold_start(stack, orchestrator, "orchestrator")
    assert again["success"] is True, again
    assert (again["definition"], again["created"]) == ("unchanged", False)
    assert again["counterpart"] == {"role": "assistant", "pane": assistant_pane_id, "session": None}

    assistant = _pane_session(stack, assistant_pane, "assistant-pane", "claude")
    for run in range(2):
        reply = await _cold_start(stack, assistant, "assistant")
        assert reply["success"] is True, reply
        assert reply["created"] is False
        assert reply["definition"] == ("applied" if run == 0 else "unchanged")
        assert reply["session"] == _ref(stack, assistant)
        assert reply["counterpart"] == {
            "role": "orchestrator",
            "pane": orchestrator_pane.id,
            "session": _ref(stack, orchestrator),
        }

    last = await _cold_start(stack, orchestrator, "orchestrator")
    assert last["created"] is False
    assert last["counterpart"]["session"] == _ref(stack, assistant)
    assert stack.native.create_calls == spawns + 1
    assert len(stack.native.write_log) == launched
    assert (_agent_type(stack, orchestrator), _agent_type(stack, assistant)) == (
        "orchestrator",
        "assistant",
    )


async def test_a_dead_counterpart_pane_is_replaced(stack: _Stack) -> None:
    """A pane row whose terminal exited no longer counts; codex launches with its prefix."""
    workspace_id, assistant_pane = await _operator_pane(stack)
    assistant = _pane_session(stack, assistant_pane, "assistant-pane", "codex")
    spawns = stack.native.create_calls

    first = await _cold_start(stack, assistant, "assistant")
    assert first["success"] is True, first
    assert first["created"] is True
    dead = _pane(stack, workspace_id, first["counterpart"]["pane"])
    assert dead.role == "orchestrator" and dead.terminal_id is not None
    assert ("text", "codex '$gobby admin'\n") in stack.native.write_log
    terminal = stack.terminals.get(dead.terminal_id)
    assert terminal is not None
    assert stack.terminals.transition_for_test(terminal.id, terminal.state, "exited") is not None

    second = await _cold_start(stack, assistant, "assistant")
    assert second["created"] is True, second
    assert second["counterpart"]["pane"] != dead.id
    assert stack.native.create_calls == spawns + 2


@pytest.mark.parametrize(
    ("source", "role", "line"),
    [
        ("agy", "orchestrator", "agy -i '/gobby assistant'"),
        ("claude", "assistant", "claude '/gobby admin'"),
        ("codex", "orchestrator", "codex '$gobby assistant'"),
        ("droid", "orchestrator", "droid '/gobby assistant'"),
        ("grok", "assistant", "grok '/gobby admin'"),
    ],
)
async def test_the_counterpart_cli_starts_interactively_with_its_command(
    stack: _Stack, source: str, role: str, line: str
) -> None:
    """Each provider gets the form that keeps its initial prompt in the interactive UI."""
    _, pane = await _operator_pane(stack)
    session = _pane_session(stack, pane, f"{source}-pane", source)
    writes = len(stack.native.write_log)

    result = await _cold_start(stack, session, role)

    assert result["success"] is True, result
    assert result["launch"]["command"] == line
    assert stack.native.write_log[writes] == ("text", f"{line}\n")


async def test_refusals_open_no_pane(stack: _Stack) -> None:
    """A role change, a pane-less caller and an unknown role each refuse without a split."""
    _, pane = await _operator_pane(stack)
    orchestrator = _pane_session(stack, pane, "orchestrator-pane", "claude")
    opened = await _cold_start(stack, orchestrator, "orchestrator")
    assert opened["success"] is True, opened
    assert opened["created"] is True
    spawns = stack.native.create_calls

    switched = await _cold_start(stack, orchestrator, "assistant")
    assert (switched["success"], switched["error_code"]) == (
        False,
        "role_change_requires_relaunch",
    )
    unknown = await _cold_start(stack, orchestrator, "triage")
    assert (unknown["success"], unknown["error_code"]) == (False, "invalid_role")
    detached = stack.sessions.register(
        external_id="no-pane",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=stack.project_id,
    )
    stranded = await _cold_start(stack, detached, "assistant")
    assert stranded["success"] is True, stranded
    assert stranded["counterpart"]["session"] == _ref(stack, orchestrator)
    lone = stack.sessions.register(
        external_id="no-pane-orchestrator",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=stack.project_id,
    )
    stack.sessions.update_status(orchestrator.id, "expired")
    refused = await _cold_start(stack, lone, "orchestrator")
    assert (refused["success"], refused["error_code"]) == (False, "pane_unavailable")
    assert stack.native.create_calls == spawns
