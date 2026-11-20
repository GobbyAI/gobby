"""A seat whose terminal is named only by its terminal context has one input terminal.

The terminal row is live with no session bound; the session's terminal context names
it. The wake already resolves that row, so every other site that reads or writes the
seat's composer (the submit hook, the release valve, the send_keys gate) must resolve
the same row, and the same guards must refuse a row the seat does not own.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.live_wake import ActivityProbe
from gobby.events.wake import WakeDispatcher
from gobby.hooks.event_handlers import EventHandlers
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate
from gobby.runner_init.wake_activity import probe_terminal_activity
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.composer_ledger import (
    ComposerLedger,
    ComposerReleaseError,
    LedgerRead,
    release_composer,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000126"
HOST_EPOCH = "context-terminal-epoch"
# Recorded by the gate stand-in once the gate reached the seat's terminal.
REACHED = "send_keys_target_bare_shell"

Foreign = Literal["other_session", "other_project", "agent_run"]


@dataclass
class Scene:
    terminals: TerminalManager
    session: Session
    row: Terminal


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, MACHINE_ID)


def _register(sessions: SessionManager, project_id: str, context: dict[str, object]) -> Session:
    return sessions.register(
        external_id=f"context-seat-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
        terminal_context=context,
        session_type="terminal",
    )


def _live_row(
    terminals: TerminalManager, project_id: str, *, agent_run_id: str | None = None
) -> Terminal:
    terminal_id = str(uuid.uuid4())
    terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=MACHINE_ID,
        agent_run_id=agent_run_id,
    )
    host_terminal_id = f"host-{terminal_id}"
    live = terminals.promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(HOST_EPOCH, host_terminal_id),
        host_epoch=HOST_EPOCH,
    )
    assert live is not None
    return live


@pytest.fixture
def scene(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> Scene:
    terminals = TerminalManager(temp_db)
    row = _live_row(terminals, sample_project["id"])
    session = _register(session_manager, sample_project["id"], {"gobby_terminal_id": row.id})
    assert row.session_id is None
    resolved = terminals.resolve_live_for_session(session)
    assert resolved is not None and resolved.id == row.id
    return Scene(terminals, session, row)


def _handlers(
    scene: Scene, ledger: ComposerLedger, monkeypatch: pytest.MonkeyPatch
) -> EventHandlers:
    handlers = EventHandlers(terminal_manager=scene.terminals)
    monkeypatch.setattr(handlers, "_composer_ledger", lambda: ledger)
    return handlers


def _typed_then_submitted(
    scene: Scene, ledger: ComposerLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's keys reach the pane, then the provider's prompt-submit hook runs."""
    ledger.record_spawn(scene.row.id, HOST_EPOCH)
    ledger.observe_write(scene.row.id, origin="operator", kind="input", payload="prompt\r")
    assert ledger.read(scene.row.id) == LedgerRead("draft")
    _handlers(scene, ledger, monkeypatch)._record_composer_submit(scene.session.id)


def _release(terminals: TerminalManager, session_id: str) -> str:
    """The released terminal id, or the valve's refusal code."""
    try:
        return release_composer(terminals, session_id, caller_session_id=None)
    except ComposerReleaseError as refused:
        return refused.code


def _wake_dispatcher(scene: Scene, pane_sender: AsyncMock) -> WakeDispatcher:
    """A dispatcher over the seat's real session row and the real terminal manager.

    The seat is idle between turns, so only the terminal it resolves decides the wake.
    """
    sessions = MagicMock()
    sessions.get.return_value = replace(scene.session, status="paused")
    return WakeDispatcher(
        session_manager=sessions,
        ism_manager=MagicMock(),
        terminal_sender=pane_sender,
        terminal_manager=scene.terminals,
        activity_probe=cast(ActivityProbe, probe_terminal_activity),
    )


def _agent_cli_in_foreground(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Process inspection reports an agent CLI; returns the terminal ids it inspected."""
    inspected: list[str] = []

    async def foreground(terminal: Any, _context: object) -> str:
        inspected.append(terminal.id)
        return "codex"

    monkeypatch.setattr(gate, "_foreground_agent_cli", foreground)
    return inspected


async def _gate_reached(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> tuple[str | None, list[str]]:
    """The gate's refusal code and the terminal ids it went on to inspect."""
    inspected: list[str] = []

    async def foreground(terminal: Any, _context: object) -> gate.SendKeysRefusal:
        inspected.append(terminal.id)
        return gate._refuse(REACHED, "the stand-in stops the gate at the foreground check")

    monkeypatch.setattr(gate, "_foreground_agent_cli", foreground)
    refusal = await gate.send_keys_target_refusal(
        scene.session.id,
        scene.terminals,
        object(),
        terminal_context=scene.session.terminal_context,
    )
    code = refusal.code if isinstance(refusal, gate.SendKeysRefusal) else None
    return code, inspected


def test_submit_hook_clears_the_draft_of_a_context_named_terminal(
    scene: Scene, composer_ledger: ComposerLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _typed_then_submitted(scene, composer_ledger, monkeypatch)

    assert composer_ledger.read(scene.row.id) == LedgerRead("empty")


@pytest.mark.asyncio
async def test_wake_delivers_after_the_submit_hook_clears_a_context_named_terminal(
    scene: Scene, composer_ledger: ComposerLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook worker runs off the event loop, as the daemon runs it.
    await asyncio.to_thread(_typed_then_submitted, scene, composer_ledger, monkeypatch)
    pane_sender = AsyncMock()
    dispatcher = _wake_dispatcher(scene, pane_sender)
    inspected = _agent_cli_in_foreground(monkeypatch)

    result = await dispatcher.dispatch_live_wake(scene.session.id)

    assert result.get("decline_reason") != "composer_occupied"
    assert result["delivered"] is True
    assert inspected == [scene.row.id]
    pane_sender.assert_awaited_once()
    assert pane_sender.await_args is not None
    assert pane_sender.await_args.args[0] == scene.row.id


def test_release_composer_releases_a_context_named_terminal(
    scene: Scene, composer_ledger: ComposerLedger
) -> None:
    composer_ledger.record_spawn(scene.row.id, HOST_EPOCH)
    composer_ledger.observe_write(scene.row.id, origin="operator", kind="input", payload="x")
    assert composer_ledger.read(scene.row.id) == LedgerRead("draft")

    assert _release(scene.terminals, scene.session.id) == scene.row.id
    assert composer_ledger.read(scene.row.id) == LedgerRead("empty")


@pytest.mark.asyncio
async def test_send_keys_gate_reaches_a_context_named_terminal(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, inspected = await _gate_reached(scene, monkeypatch)

    assert code == REACHED
    assert inspected == [scene.row.id]


def _foreign_row(
    kind: Foreign,
    temp_db: HubDatabase,
    sessions: SessionManager,
    projects: LocalProjectManager,
    project_id: str,
) -> Terminal:
    terminals = TerminalManager(temp_db)
    if kind == "other_project":
        return _live_row(terminals, projects.create(name=f"foreign-{uuid.uuid4()}").id)
    if kind == "agent_run":
        parent = _register(sessions, project_id, {})
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=parent.id, provider="codex", prompt="an agent's own pane"
        )
        return _live_row(terminals, project_id, agent_run_id=run.id)
    owner = _register(sessions, project_id, {})
    row = _live_row(terminals, project_id)
    bound = terminals.bind_session(row.id, owner.id, project_id)
    assert bound is not None
    return bound


@pytest.fixture(params=["other_session", "other_project", "agent_run"])
def foreign_scene(
    request: pytest.FixtureRequest,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_manager: LocalProjectManager,
    sample_project: dict[str, Any],
) -> Iterator[Scene]:
    project_id = sample_project["id"]
    row = _foreign_row(request.param, temp_db, session_manager, project_manager, project_id)
    session = _register(session_manager, project_id, {"gobby_terminal_id": row.id})
    yield Scene(TerminalManager(temp_db), session, row)


@pytest.mark.asyncio
async def test_a_context_naming_a_row_the_seat_does_not_own_resolves_nothing_anywhere(
    foreign_scene: Scene, composer_ledger: ComposerLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = foreign_scene
    composer_ledger.record_spawn(scene.row.id, HOST_EPOCH)
    composer_ledger.observe_write(scene.row.id, origin="operator", kind="input", payload="x")
    pane_sender = AsyncMock()
    record_submit: Callable[[str], None] = _handlers(
        scene, composer_ledger, monkeypatch
    )._record_composer_submit

    await asyncio.to_thread(record_submit, scene.session.id)
    woke = await _wake_dispatcher(scene, pane_sender).dispatch_live_wake(scene.session.id)
    released = _release(scene.terminals, scene.session.id)
    code, inspected = await _gate_reached(scene, monkeypatch)

    assert composer_ledger.read(scene.row.id) == LedgerRead("draft")
    assert woke["delivered"] is False
    pane_sender.assert_not_awaited()
    assert released == "no_live_terminal"
    assert (code, inspected) == ("send_keys_no_live_terminal", [])
