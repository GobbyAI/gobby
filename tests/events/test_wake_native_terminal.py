"""Live wake reaches an interactive session through the terminal row hosting it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import cast
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import UUID

import pytest

from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.storage.terminals import Terminal
from gobby.terminals.composer import composer_clear_sequence
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
REPRO_SESSION_ID = "b3010944-ba0e-408b-a8a2-e55f43cbf41e"
REPRO_TERMINAL_ID = "4e0a798f-984f-4c92-8ae2-e6395a496c75"
# A gterm-hosted session: it has terminal_context, but $TMUX_PANE never set a
# pane in it, which is exactly what used to end the wake as `no_tmux_pane`.
NATIVE_TERMINAL_CONTEXT = {"parent_pid": 4242, "term_program": "gterm"}
WAKE_SEQUENCE = [
    *(("key", key) for key in composer_clear_sequence(None)),
    ("text", CONTINUE_WAKE_MESSAGE),
    ("key", "enter"),
]


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: object | None = None
    status: str = "paused"
    turn_count: int = 0
    session_type: str = "terminal"


@dataclass
class ManagedChain:
    """The production wake sender bound to fake runtimes and a fake row store."""

    store: MemoryTerminalStore
    native: FakeRuntime
    tmux: FakeRuntime
    row: Terminal


class UUIDValidatingTerminalStore(MemoryTerminalStore):
    """Match TerminalManager.get's UUID-only terminal ID contract."""

    def get(self, terminal_id: str) -> Terminal | None:
        return super().get(str(UUID(terminal_id)))


def _session_manager(
    terminal_context: object | None,
    *,
    session_id: str = WAKE_SESSION_ID,
    agent_depth: int = 0,
) -> MagicMock:
    manager = MagicMock()
    manager.get.return_value = FakeSession(
        id=session_id,
        agent_depth=agent_depth,
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
        "gobby.runner_init.orchestration.wake_write_services",
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
async def test_three_native_recipients_use_one_ordered_batch() -> None:
    session_ids = [
        "9264a39c-68db-5eed-917c-6f7babb8e6b1",
        "2a1b0639-bb96-56bf-9dfa-e28194dbab4b",
        "0fe83c82-cd2f-501c-b06c-dc89d5f47726",
    ]
    sessions = {
        session_id: FakeSession(
            id=session_id,
            terminal_context=NATIVE_TERMINAL_CONTEXT,
            status="paused",
        )
        for session_id in session_ids
    }
    session_manager = MagicMock()
    session_manager.get.side_effect = sessions.get
    rows = [
        replace(make_memory_terminal(backend="native"), session_id=session_id)
        for session_id in session_ids
    ]
    store = MemoryTerminalStore()
    store.rows.update({row.id: row for row in rows})
    batch_sender = AsyncMock(
        return_value=[
            {
                "session_id": session_id,
                "delivered": True,
                "method": "terminal",
            }
            for session_id in session_ids
        ]
    )
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(),
        native_batch_sender=batch_sender,
        terminal_manager=store,
    )

    results = await dispatcher.dispatch_live_wakes(session_ids)

    batch_sender.assert_awaited_once()
    batch_call = batch_sender.await_args
    assert batch_call is not None
    targets = batch_call.args[0]
    assert [target.session_id for target in targets] == session_ids
    assert [target.terminal_id for target in targets] == [row.id for row in rows]
    assert [result["session_id"] for result in results] == session_ids
    assert all(result["delivered"] is True for result in results)


@pytest.mark.asyncio
async def test_batch_keeps_active_nonurgent_recipient_out_of_terminal_injection() -> None:
    paused_id = WAKE_SESSION_ID
    active_id = REPRO_SESSION_ID
    sessions = {
        paused_id: FakeSession(paused_id, terminal_context=NATIVE_TERMINAL_CONTEXT),
        active_id: FakeSession(
            active_id,
            terminal_context=NATIVE_TERMINAL_CONTEXT,
            status="active",
        ),
    }
    session_manager = MagicMock()
    session_manager.get.side_effect = sessions.get
    rows = [
        replace(make_memory_terminal(backend="native"), session_id=session_id)
        for session_id in (paused_id, active_id)
    ]
    store = MemoryTerminalStore()
    store.rows.update({row.id: row for row in rows})
    batch_sender = AsyncMock(
        return_value=[{"session_id": paused_id, "delivered": True, "method": "terminal"}]
    )
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(),
        native_batch_sender=batch_sender,
        terminal_manager=store,
    )

    results = await dispatcher.dispatch_live_wakes([paused_id, active_id])

    batch_call = batch_sender.await_args
    assert batch_call is not None
    targets = batch_call.args[0]
    assert [target.session_id for target in targets] == [paused_id]
    assert results[0]["delivered"] is True
    assert results[1]["delivered"] is False
    assert results[1]["method"] == "next_call_context"
    assert results[1]["skipped"] == "session_active"


@pytest.mark.asyncio
async def test_final_preflight_suppresses_session_that_becomes_protected(
    managed_chain: ManagedChain,
) -> None:
    session_manager = _session_manager(NATIVE_TERMINAL_CONTEXT)
    session_manager.get.side_effect = [
        FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context=NATIVE_TERMINAL_CONTEXT,
            status="paused",
        ),
        FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context=NATIVE_TERMINAL_CONTEXT,
            status="awaiting_input",
        ),
    ]
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["error_code"] == "session_awaiting_input"
    assert result["delivered"] is False
    assert managed_chain.native.write_log == []
    assert managed_chain.tmux.write_log == []
    pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_tmux_agent_wake_resolves_name_without_uuid_lookup_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reproduce the b3010944 wake whose identity was gobby-4e0a798f."""
    row = replace(
        make_memory_terminal(terminal_id=REPRO_TERMINAL_ID),
        session_id=REPRO_SESSION_ID,
    )
    store = UUIDValidatingTerminalStore(row)
    runtime = FakeRuntime(backend="tmux")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, coordinator),
    )
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", no_sleep)
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(
            {
                "tmux_session": f"gobby-{REPRO_TERMINAL_ID}",
                "tmux_pane": "%21",
            },
            session_id=REPRO_SESSION_ID,
            agent_depth=1,
        ),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(REPRO_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "tmux"
    assert runtime.write_log == WAKE_SEQUENCE
    pane_sender.assert_not_awaited()
    assert not [record for record in caplog.records if record.exc_info]


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
        clear_before_submit=True,
        cli_source=ANY,
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
    # The lost reply latched `wake:<id>`; the drained retry settles it instead
    # of being suppressed by it (#21670).
    assert retry["delivered"] is True
    assert managed_chain.store.rows[managed_chain.row.id].unresolved_writes == {}


@pytest.mark.asyncio
async def test_latched_wake_is_settled_by_the_delivered_composer_clear(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A latch persisted by an earlier daemon life must not suppress wakes forever."""
    wake_key = f"wake:{managed_chain.row.id}"
    managed_chain.store.persist_unresolved_write(managed_chain.row.id, wake_key, "automatic")
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.runner_init.orchestration"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "terminal"
    assert managed_chain.native.write_log == WAKE_SEQUENCE
    assert managed_chain.store.rows[managed_chain.row.id].unresolved_writes == {}
    assert [record.getMessage() for record in caplog.records if "Settling" in record.getMessage()]
    pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_undelivered_composer_clear_leaves_the_earlier_wake_latched(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a Delivered drain proves the composer is empty; a lost one settles nothing."""
    wake_key = f"wake:{managed_chain.row.id}"
    clear_key = f"wake-clear:{managed_chain.row.id}"
    managed_chain.store.persist_unresolved_write(managed_chain.row.id, wake_key, "automatic")
    managed_chain.native.outcomes = [IndeterminateWrite(detail="lost")]
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.runner_init.orchestration"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["indeterminate"] is True
    assert result["method"] == "terminal"
    assert managed_chain.native.write_log == [WAKE_SEQUENCE[0]]
    unresolved = managed_chain.store.rows[managed_chain.row.id].unresolved_writes
    assert wake_key in unresolved
    # The drain is idempotent, so its lost reply latches nothing of its own.
    assert clear_key not in unresolved
    assert not [record for record in caplog.records if "Settling" in record.getMessage()]
    pane_sender.assert_not_awaited()

    # The next wake repeats the drain instead of being suppressed by the lost
    # reply, and only its delivery settles the earlier wake.
    retry = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert retry["delivered"] is True
    assert managed_chain.native.write_log == [WAKE_SEQUENCE[0], *WAKE_SEQUENCE]
    assert managed_chain.store.rows[managed_chain.row.id].unresolved_writes == {}


@pytest.mark.asyncio
async def test_quarantined_terminal_wake_is_a_structured_decline_without_traceback(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A quarantine held for another action refuses the wake; nothing reaches the screen."""
    managed_chain.store.set_automatic_write_quarantine(managed_chain.row.id, "handoff:compact")
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
        terminal_manager=managed_chain.store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["method"] == "terminal"
    assert result["error_code"] == "automatic_write_quarantined"
    assert managed_chain.native.write_log == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record for record in caplog.records if "declined" in record.getMessage()]
    pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarantined_agent_terminal_wake_falls_back_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An agent's refused tmux wake keeps its pane fallback and logs no traceback."""
    row = replace(
        make_memory_terminal(terminal_id=REPRO_TERMINAL_ID),
        session_id=REPRO_SESSION_ID,
    )
    store = UUIDValidatingTerminalStore(row)
    store.set_automatic_write_quarantine(row.id, "handoff:compact")
    runtime = FakeRuntime(backend="tmux")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
    )
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, coordinator),
    )
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(
            {"tmux_session": f"gobby-{REPRO_TERMINAL_ID}", "tmux_pane": "%21"},
            session_id=REPRO_SESSION_ID,
            agent_depth=1,
        ),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=pane_sender,
    )

    with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(REPRO_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "tmux_pane"
    assert runtime.write_log == []
    pane_sender.assert_awaited_once()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


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
