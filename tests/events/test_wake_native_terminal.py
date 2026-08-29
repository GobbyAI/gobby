"""Live wake reaches an interactive session through the terminal row hosting it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import Delivered, IndeterminateWrite
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

WAKE_SESSION_ID = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
# A gterm-hosted session: it has terminal_context, but $TMUX_PANE never set a
# pane in it, which is exactly what used to end the wake as `no_tmux_pane`.
NATIVE_TERMINAL_CONTEXT = {"parent_pid": 4242, "term_program": "gterm"}
WAKE_SEQUENCE = [
    ("key", "escape"),
    ("text", CONTINUE_WAKE_MESSAGE),
    ("key", "enter"),
]


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: object | None = None
    status: str = "active"
    turn_count: int = 0
    session_type: str = "terminal"


@dataclass
class ManagedChain:
    """The production wake sender bound to fake runtimes and a fake row store."""

    store: MemoryTerminalStore
    native: FakeRuntime
    tmux: FakeRuntime
    row: Terminal


def _session_manager(terminal_context: object | None) -> MagicMock:
    manager = MagicMock()
    manager.get.return_value = FakeSession(
        id=WAKE_SESSION_ID,
        agent_depth=0,
        terminal_context=terminal_context,
    )
    return manager


@pytest.fixture
def managed_chain(monkeypatch: pytest.MonkeyPatch) -> ManagedChain:
    """Bind `_send_tmux_session_wake` to a native row and a two-runtime registry."""
    row = replace(
        make_memory_terminal(backend="native"),
        session_id=WAKE_SESSION_ID,
    )
    store = MemoryTerminalStore(row)
    native = FakeRuntime(backend="native")
    tmux = FakeRuntime(backend="tmux")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(tmux, native),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "gobby.runner_init.orchestration._wake_write_services",
        lambda: (store, coordinator),
    )
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", no_sleep)
    return ManagedChain(store=store, native=native, tmux=tmux, row=row)


@pytest.mark.asyncio
async def test_native_backed_interactive_session_wakes_through_its_terminal_row(
    managed_chain: ManagedChain,
) -> None:
    """A gterm-hosted session has no tmux_pane, so the row is what makes it wakeable."""
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "terminal"
    assert result.get("error_code") is None
    assert managed_chain.native.write_log == WAKE_SEQUENCE
    # FakeRuntime ignores Terminal.backend, so the tmux runtime staying untouched
    # is the only thing separating a routed write from a bound one.
    assert managed_chain.tmux.write_log == []
    pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_session_without_a_managed_row_still_uses_the_tmux_pane() -> None:
    """A tmux session Gobby owns no row for has no backend to resolve, so the pane stays."""
    managed_sender = AsyncMock()
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({"tmux_pane": "%12", "tmux_socket_path": "/tmp/s"}),
        ism_manager=MagicMock(),
        tmux_sender=managed_sender,
        tmux_pane_sender=pane_sender,
        terminal_manager=MemoryTerminalStore(),
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "tmux_pane"
    pane_sender.assert_awaited_once_with(
        "%12",
        CONTINUE_WAKE_MESSAGE,
        "/tmp/s",
        submit=True,
        escape_before_submit=True,
    )
    managed_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_indeterminate_terminal_wake_records_no_delivery_and_skips_the_pane(
    managed_chain: ManagedChain,
) -> None:
    """Bytes may already be on screen, so no second route and no debounce record."""
    managed_chain.native.outcomes = [Delivered(), IndeterminateWrite(detail="lost")]
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["indeterminate"] is True
    assert result["method"] == "terminal"
    assert result["error_message"] == "lost"
    assert "enter" not in [payload for _kind, payload in managed_chain.native.write_log]
    pane_sender.assert_not_awaited()

    # A recorded delivery would coalesce the next wake in the same turn into
    # `skipped: debounced`; attempting the route again is what proves none was.
    retry = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert retry.get("skipped") is None
    assert retry["method"] == "terminal"


@pytest.mark.asyncio
async def test_failed_terminal_wake_is_structured_and_does_not_fall_back_to_the_pane(
    managed_chain: ManagedChain,
) -> None:
    """The pane would target the same terminal, so a retry there would double-write."""
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({**NATIVE_TERMINAL_CONTEXT, "tmux_pane": "%12"}),
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(side_effect=RuntimeError("terminal gone")),
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["method"] == "terminal"
    assert result["error_code"] == "terminal_wake_failed"
    assert result["error_message"] == "terminal gone"
    pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_row_lookup_failure_degrades_to_the_tmux_pane() -> None:
    """A lookup error must not cost a tmux-hosted session the wake it used to get."""
    failing_manager = MagicMock()
    failing_manager.get_live_for_session.side_effect = RuntimeError("hub down")
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({"tmux_pane": "%12"}),
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(),
        tmux_pane_sender=pane_sender,
        terminal_manager=failing_manager,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "tmux_pane"
    pane_sender.assert_awaited_once()
