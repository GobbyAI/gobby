"""Live wake reaches an interactive session through the terminal row hosting it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import UUID

import pytest

from gobby.agents.idle_detector import ComposerRead, IdleDetector
from gobby.events.live_wake import ActivityProbe, TerminalActivity, composer_occupied_result
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.runner_init.wake_activity import probe_terminal_activity
from gobby.storage.terminals import Terminal
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, IndeterminateWrite, UnregisteredBackendError
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.events.wake_test_support import PendingWakeLedger
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

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
    project_id: str | None = None
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
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
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
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
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


@pytest.mark.parametrize("backend", ["native", "tmux"])
async def test_wake_requires_backend_registered_by_native_only_runner(
    managed_chain: ManagedChain,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    row = replace(managed_chain.row, backend=backend)
    managed_chain.store.rows[row.id] = row
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, managed_chain.store),
        runtime_registry(managed_chain.native),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="native-only-test"),
    )
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (managed_chain.store, coordinator),
    )
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )
    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
    if backend == "native":
        assert result["delivered"] is True
        assert managed_chain.native.write_log == WAKE_SEQUENCE
    else:
        assert result["delivered"] is False
        assert result["error_code"] == "terminal_wake_failed"
        with pytest.raises(UnregisteredBackendError):
            coordinator.runtime_for(row)
        assert managed_chain.native.write_log == []


@pytest.mark.asyncio
async def test_native_context_id_wakes_through_unbound_terminal_row(
    managed_chain: ManagedChain,
) -> None:
    managed_chain.row.session_id = None
    session_manager = _session_manager({"gobby_terminal_id": managed_chain.row.id})
    session_manager.get.return_value.project_id = managed_chain.row.project_id
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "terminal"
    assert managed_chain.native.write_log == WAKE_SEQUENCE


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
    # No probe confirmed these composers empty, so each keeps its blind drain.
    assert [target.drain for target in targets] == [True, True, True]
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
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["error_code"] == "session_awaiting_input"
    assert result["delivered"] is False
    assert managed_chain.native.write_log == []
    assert managed_chain.tmux.write_log == []


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
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, coordinator),
    )
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", no_sleep)
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
        terminal_manager=store,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(REPRO_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "terminal"
    assert runtime.write_log == WAKE_SEQUENCE
    assert not [record for record in caplog.records if record.exc_info]


@pytest.mark.asyncio
async def test_interactive_session_without_a_managed_row_does_not_use_raw_tmux_pane() -> None:
    managed_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({"tmux_pane": "%12", "tmux_socket_path": "/tmp/s"}),
        ism_manager=MagicMock(),
        tmux_sender=managed_sender,
        terminal_manager=MemoryTerminalStore(),
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["error_code"] == "no_live_wake_channel"
    managed_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_indeterminate_terminal_wake_records_no_delivery_and_skips_the_pane(
    managed_chain: ManagedChain,
) -> None:
    """Bytes may already be on screen, so no second route and no debounce record."""
    managed_chain.native.outcomes = [Delivered(), IndeterminateWrite(detail="lost")]
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["indeterminate"] is True
    assert result["method"] == "terminal"
    assert result["error_message"] == "lost"
    assert "enter" not in [payload for _kind, payload in managed_chain.native.write_log]

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
    managed_chain.store.persist_unresolved_write(
        managed_chain.row.id,
        wake_key,
        "automatic",
        daemon_epoch="test-epoch",
    )
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.runner_init.orchestration"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert result["method"] == "terminal"
    assert managed_chain.native.write_log == WAKE_SEQUENCE
    assert managed_chain.store.rows[managed_chain.row.id].unresolved_writes == {}
    assert [record.getMessage() for record in caplog.records if "Settling" in record.getMessage()]


def _provider_composer_frame(source: str, text: str = "") -> str:
    if source == "claude":
        return f"done\n{'─' * 20}\n❯\xa0{text}\n{'─' * 20}\n"
    footer = "  GPT-6-Sol xhigh · ~/Projects/gobby · 0.5.0\n  ← for agents · ? for shortcuts"
    return f"done\n› {text}\n\n{footer}"


@pytest.mark.parametrize("source", ["claude", "codex"])
@pytest.mark.parametrize("state", ["empty", "draft", "unknown", "error"])
async def test_real_probe_settles_old_wake_only_after_confirmed_empty(
    managed_chain: ManagedChain,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    state: str,
) -> None:
    """An old wake latch recovers without draining; unsafe reads preserve the latch and text."""
    wake_key = f"wake:{managed_chain.row.id}"
    managed_chain.store.persist_unresolved_write(
        managed_chain.row.id, wake_key, "automatic", daemon_epoch="earlier-daemon"
    )
    registry = BundledDetectionRegistry()
    frame = (
        "unreadable provider frame"
        if state == "unknown"
        else _provider_composer_frame(source, "operator draft" if state != "empty" else "")
    )
    managed_chain.native.snapshot_text = frame
    if state == "error":
        monkeypatch.setattr(
            managed_chain.native, "snapshot", AsyncMock(side_effect=RuntimeError("probe failed"))
        )
    else:
        assert IdleDetector(registry, source).composer_read(frame).state == state
    runner = cast(
        "GobbyRunner",
        SimpleNamespace(
            detection_registry=registry,
            terminal_services=SimpleNamespace(runtime_for=lambda _terminal: managed_chain.native),
        ),
    )

    async def probe(session: Any, terminal: Any | None) -> TerminalActivity:
        return await probe_terminal_activity(runner, session, terminal)

    manager = _session_manager(NATIVE_TERMINAL_CONTEXT)
    manager.get.return_value.source = source
    dispatcher = WakeDispatcher(
        session_manager=manager,
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
        activity_probe=probe,
    )
    try:
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
        unresolved = managed_chain.store.rows[managed_chain.row.id].unresolved_writes
        if state == "empty":
            assert result["delivered"] is True
            assert unresolved == {}
            assert managed_chain.native.write_log == [
                ("text", CONTINUE_WAKE_MESSAGE),
                ("key", "enter"),
            ]
        else:
            assert result["delivered"] is False
            assert wake_key in unresolved
            assert managed_chain.native.write_log == []
            assert managed_chain.native.snapshot_text == frame
    finally:
        retries = list(dispatcher._composer_retries.values())
        for retry in retries:
            retry.cancel()
        await asyncio.gather(*retries, return_exceptions=True)


@pytest.mark.asyncio
async def test_undelivered_composer_clear_leaves_the_earlier_wake_latched(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a Delivered drain proves the composer is empty; a lost one settles nothing."""
    wake_key = f"wake:{managed_chain.row.id}"
    clear_key = f"wake-clear:{managed_chain.row.id}"
    managed_chain.store.persist_unresolved_write(
        managed_chain.row.id,
        wake_key,
        "automatic",
        daemon_epoch="test-epoch",
    )
    managed_chain.native.outcomes = [IndeterminateWrite(detail="lost")]
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
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
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["method"] == "terminal"
    assert result["error_code"] == "automatic_write_quarantined"
    assert managed_chain.native.write_log == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert not [record for record in caplog.records if "declined" in record.getMessage()]


@pytest.mark.asyncio
async def test_quarantined_agent_terminal_wake_falls_back_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A quarantined agent terminal refuses wake without a raw pane fallback."""
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
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, coordinator),
    )
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(
            {"tmux_session": f"gobby-{REPRO_TERMINAL_ID}", "tmux_pane": "%21"},
            session_id=REPRO_SESSION_ID,
            agent_depth=1,
        ),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=store,
    )

    with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(REPRO_SESSION_ID)

    assert result["delivered"] is False
    assert result["method"] == "terminal"
    assert result["error_code"] == "automatic_write_quarantined"
    assert runtime.write_log == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_failed_terminal_wake_is_structured_and_does_not_fall_back_to_the_pane(
    managed_chain: ManagedChain,
) -> None:
    """The pane would target the same terminal, so a retry there would double-write."""
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({**NATIVE_TERMINAL_CONTEXT, "tmux_pane": "%12"}),
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(side_effect=RuntimeError("terminal gone")),
        terminal_manager=managed_chain.store,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["method"] == "terminal"
    assert result["error_code"] == "terminal_wake_failed"
    assert result["error_message"] == "terminal gone"


@pytest.mark.asyncio
async def test_terminal_row_lookup_failure_does_not_use_raw_tmux_pane() -> None:
    failing_manager = MagicMock()
    failing_manager.resolve_live_for_session.side_effect = RuntimeError("hub down")
    dispatcher = WakeDispatcher(
        session_manager=_session_manager({"tmux_pane": "%12"}),
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(),
        terminal_manager=failing_manager,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["error_code"] == "no_live_wake_channel"


def _claude_composer_activity(prompt: str) -> TerminalActivity:
    snapshot = "\n".join(
        (
            "⏺ done",
            "──────────── epic-22508-feedback-triage ─",
            prompt,
            "────────────────────",
            "   Fable 5.1  12%   ⎇ main",
        )
    )
    detector = IdleDetector(BundledDetectionRegistry(), "claude")
    return TerminalActivity(detector.composer_read(snapshot))


async def _draft(_session: object, _terminal: object) -> TerminalActivity:
    return _claude_composer_activity("❯ hello draft")


async def _empty(_session: object, _terminal: object) -> TerminalActivity:
    return _claude_composer_activity("❯")


async def _broken(_session: object, _terminal: object) -> TerminalActivity:
    raise RuntimeError("no pane")


def _managed_dispatcher(probe: ActivityProbe | None, terminal_sender: AsyncMock) -> WakeDispatcher:
    terminal = replace(make_memory_terminal(backend="native"), session_id=WAKE_SESSION_ID)
    return WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=terminal_sender,
        terminal_manager=MemoryTerminalStore(terminal),
        activity_probe=probe,
    )


def _managed_terminal_id(dispatcher: WakeDispatcher) -> str:
    terminals = dispatcher._terminal_manager
    assert isinstance(terminals, MemoryTerminalStore)
    terminal = terminals.get_live_for_session(WAKE_SESSION_ID)
    assert terminal is not None
    return terminal.id


@pytest.mark.asyncio
async def test_managed_wake_is_withheld_while_the_composer_holds_a_draft() -> None:
    terminal_sender = AsyncMock()
    dispatcher = _managed_dispatcher(_draft, terminal_sender)
    ledger = PendingWakeLedger(dispatcher)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    terminal_sender.assert_not_awaited()
    # No debounce record: the next wake probes the composer again.
    assert ledger.recorded == []


@pytest.mark.asyncio
async def test_urgent_wake_is_withheld_while_the_composer_holds_a_draft() -> None:
    terminal_sender = AsyncMock()
    dispatcher = _managed_dispatcher(_draft, terminal_sender)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, priority="urgent")

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    terminal_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_urgent_wake_injects_an_empty_composer() -> None:
    terminal_sender = AsyncMock()
    dispatcher = _managed_dispatcher(_empty, terminal_sender)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, priority="urgent")

    assert result["delivered"] is True
    terminal_sender.assert_awaited_once_with(
        _managed_terminal_id(dispatcher),
        CONTINUE_WAKE_MESSAGE,
        submit=True,
        clear_before_submit=False,
        composer_confirmed_empty=True,
        cli_source=ANY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("probe", "drains"), [(_empty, False), (None, True)])
async def test_only_an_unconfirmed_composer_keeps_the_blind_drain(
    probe: ActivityProbe | None, drains: bool
) -> None:
    """A confirmed-empty wake types directly; only a missing probe drains first."""
    terminal_sender = AsyncMock()
    dispatcher = _managed_dispatcher(probe, terminal_sender)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    terminal_sender.assert_awaited_once_with(
        _managed_terminal_id(dispatcher),
        CONTINUE_WAKE_MESSAGE,
        submit=True,
        clear_before_submit=drains,
        cli_source=ANY,
        **({} if drains else {"composer_confirmed_empty": True}),
    )


@pytest.mark.asyncio
async def test_probe_error_withholds_until_a_positive_empty_read() -> None:
    terminal_sender = AsyncMock()
    dispatcher = _managed_dispatcher(_broken, terminal_sender)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["skipped"] == "composer_unconfirmed"
    assert result["delivered"] is False
    assert result["ism_persisted"] is True
    terminal_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_terminal_wake_is_withheld_for_a_draft(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
        activity_probe=_draft,
    )

    with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    assert managed_chain.native.write_log == []
    assert any(
        record.levelno >= logging.INFO and "hello draft" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_composer_occupied_log_truncates_a_long_draft(
    managed_chain: ManagedChain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_line = "word " * 50

    async def _long(_session: object, _terminal: object) -> TerminalActivity:
        return TerminalActivity(ComposerRead("draft", long_line.strip()))

    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
        activity_probe=_long,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.events.wake"):
        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    messages = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(messages) == 1
    assert long_line.strip() not in messages[0]
    assert messages[0].endswith("...")


def _batch_dispatcher(probe: ActivityProbe) -> tuple[WakeDispatcher, AsyncMock]:
    row = replace(make_memory_terminal(backend="native"), session_id=WAKE_SESSION_ID)
    store = MemoryTerminalStore(row)
    batch_sender = AsyncMock(
        return_value=[{"session_id": WAKE_SESSION_ID, "delivered": True, "method": "terminal"}]
    )
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=MagicMock(),
        tmux_sender=AsyncMock(),
        native_batch_sender=batch_sender,
        terminal_manager=store,
        activity_probe=probe,
    )
    return dispatcher, batch_sender


@pytest.mark.asyncio
async def test_batch_wake_skips_occupied_composer() -> None:
    dispatcher, batch_sender = _batch_dispatcher(_draft)
    ledger = PendingWakeLedger(dispatcher)

    results = await dispatcher.dispatch_live_wakes([WAKE_SESSION_ID])

    assert results == [composer_occupied_result(WAKE_SESSION_ID, method="terminal")]
    batch_sender.assert_not_awaited()
    assert ledger.recorded == []


@pytest.mark.asyncio
async def test_batch_wake_still_injects_an_empty_composer() -> None:
    dispatcher, batch_sender = _batch_dispatcher(_empty)

    results = await dispatcher.dispatch_live_wakes([WAKE_SESSION_ID])

    assert results[0]["delivered"] is True
    assert batch_sender.await_args is not None
    assert [t.session_id for t in batch_sender.await_args.args[0]] == [WAKE_SESSION_ID]
    # The positive empty read under the lock leaves the batch drain nothing to do.
    assert [t.drain for t in batch_sender.await_args.args[0]] == [False]


@pytest.mark.asyncio
async def test_native_spawned_agent_wakes_through_its_terminal_row(
    managed_chain: ManagedChain,
) -> None:
    """A gterm-hosted agent has no tmux keys, so its row is its only wake channel."""
    sdk_resumer = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT, agent_depth=1),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        sdk_resumer=sdk_resumer,
        terminal_manager=managed_chain.store,
        activity_probe=_empty,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result == {"session_id": WAKE_SESSION_ID, "delivered": True, "method": "terminal"}
    # The probe confirmed the composer empty, so the wake types without draining.
    assert managed_chain.native.write_log == [
        ("text", CONTINUE_WAKE_MESSAGE),
        ("key", "enter"),
    ]
    assert managed_chain.tmux.write_log == []
    sdk_resumer.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_spawned_agent_wake_is_withheld_for_a_draft(
    managed_chain: ManagedChain,
) -> None:
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT, agent_depth=1),
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=managed_chain.store,
        activity_probe=_draft,
    )
    ledger = PendingWakeLedger(dispatcher)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    assert managed_chain.native.write_log == []
    assert ledger.recorded == []


@pytest.mark.asyncio
async def test_spawned_agent_without_a_managed_row_does_not_use_raw_tmux_session() -> None:
    managed_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(
            {"tmux_session": "gobby-agent-abc", "tmux_pane": "%7"}, agent_depth=1
        ),
        ism_manager=MagicMock(),
        tmux_sender=managed_sender,
        terminal_manager=MemoryTerminalStore(),
        activity_probe=_empty,
    )

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is False
    assert result["error_code"] == "no_live_wake_channel"
    managed_sender.assert_not_awaited()
