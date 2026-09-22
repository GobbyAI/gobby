"""Daemon-tracked terminal termination service tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from gobby.terminals.termination import (
    TerminalTerminationError,
    kill_terminal,
    terminate_terminal,
)
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_kill_terminal_marks_external_row_exited_synchronously() -> None:
    terminal = make_memory_terminal(session_name="external-shell")
    terminal.ownership = "external"
    terminals = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()

    exited = await kill_terminal(terminals, runtime_registry(runtime), terminal)

    assert exited is terminal
    assert terminal.state == "exited"
    assert runtime.killed == ["external-shell"]


@pytest.mark.asyncio
async def test_terminate_terminal_resolves_root_session_reference() -> None:
    root_session_id = "root-session"
    terminal = make_memory_terminal(session_name="root-shell")
    terminal.ownership = "external"
    terminal.project_id = "project-1"
    terminal.session_id = root_session_id
    terminals = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    caller = SimpleNamespace(id="caller", project_id="project-1", agent_run_id=None)
    sessions = MagicMock()
    sessions.resolve_session_reference.side_effect = lambda reference, _project_id=None: reference
    sessions.get.return_value = caller

    exited = await terminate_terminal(
        terminals,
        runtime_registry(runtime),
        sessions,
        actor="session:caller",
        reference=root_session_id,
    )

    assert exited is terminal
    assert terminal.state == "exited"
    assert runtime.killed == ["root-shell"]
    assert sessions.resolve_session_reference.call_args_list == [
        call(caller.id),
        call(root_session_id, caller.project_id),
    ]


@pytest.mark.asyncio
async def test_terminate_terminal_refuses_target_outside_actor_scope() -> None:
    root_session_id = "root-session"
    terminal = make_memory_terminal(session_name="other-project-shell")
    terminal.ownership = "external"
    terminal.project_id = "project-2"
    terminal.session_id = root_session_id
    terminals = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    caller = SimpleNamespace(id="caller", project_id="project-1", agent_run_id=None)
    sessions = MagicMock()
    sessions.resolve_session_reference.side_effect = lambda reference, _project_id=None: reference
    sessions.get.return_value = caller
    sessions.is_ancestor.return_value = False

    with pytest.raises(TerminalTerminationError, match="outside the actor") as raised:
        await terminate_terminal(
            terminals,
            runtime_registry(runtime),
            sessions,
            actor="session:caller",
            reference=root_session_id,
        )

    assert raised.value.code == "forbidden"
    assert terminal.state == "live"
    assert runtime.killed == []
