"""Serialization of the one physical composer across wake and handoff writers.

#22915 criterion 6: an earlier empty snapshot cannot authorize a later
overlapping write. These tests race two real writers -- the daemon wake sender
and the compaction/clear ladder -- on a shared terminal and prove their writes
never interleave, so a wake cannot be typed into a staged ``/compact``.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import replace
from typing import Any, cast

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.terminals import composer_lock as composer_lock_module
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.pane_io import RuntimePaneIO
from gobby.terminals.runtime import Delivered, SnapshotResult, WriteOutcome
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.asyncio

_PHASE: contextvars.ContextVar[str] = contextvars.ContextVar("composer_phase", default="?")


def _arbitrated() -> tuple[MemoryTerminalStore, FakeRuntime, WriteCoordinator, str]:
    terminal = make_memory_terminal(session_name="gobby-agent-arbitration")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    runtime.write_log = []
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    return store, runtime, coordinator, terminal.id


class _ComposerRuntime(FakeRuntime):
    """Model the staged editor and submissions, including a held literal newline."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = ""
        self.submitted: list[str] = []
        self.phases: list[str] = []
        self.hold_phase: str | None = None
        self.staged = asyncio.Event()
        self.release_staged = asyncio.Event()
        self.unreadable = False

    async def snapshot(
        self, terminal: Any, lines: int = 50, *, mode: Any = "text"
    ) -> SnapshotResult:
        return SnapshotResult(
            text="unreadable provider frame" if self.unreadable else self.buffer,
            truncated=False,
            dropped_bytes=0,
            total_bytes=len(self.buffer.encode()),
        )

    async def _record(self, kind: str, payload: str | bytes) -> WriteOutcome:
        outcome = await super()._record(kind, payload)
        self.phases.append(_PHASE.get())
        if isinstance(outcome, Delivered):
            if kind == "text" and isinstance(payload, str):
                self.buffer += payload.rstrip("\n")
                if _PHASE.get() == self.hold_phase:
                    self.staged.set()
                    await self.release_staged.wait()
            elif kind == "key" and payload == "enter":
                if self.buffer:
                    self.submitted.append(self.buffer)
                    self.buffer = ""
            elif kind == "key" and payload in {"ctrl_u", "ctrl_k", "backspace", "delete"}:
                self.buffer = ""
        return outcome


def _read_editor(text: str | None) -> ComposerRead:
    if text is None or text == "unreadable provider frame":
        return ComposerRead("unknown")
    return ComposerRead("draft", text) if text else ComposerRead("empty")


def _editor_seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    cli_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WakeDispatcher, InterSessionMessageManager, _ComposerRuntime, str, RuntimePaneIO]:
    session = session_manager.register(
        external_id="composer-race",
        machine_id=None,
        source=cli_source,
        project_id=project_id,
    )
    temp_db.execute("UPDATE sessions SET status = 'paused' WHERE id = %s", (session.id,))
    terminal = replace(make_memory_terminal(), session_id=session.id)
    store = MemoryTerminalStore(terminal)
    runtime = _ComposerRuntime()
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="composer-race"),
    )
    monkeypatch.setattr(composer_lock_module, "_coordinator", coordinator)
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services", lambda: (store, coordinator)
    )
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    pane = RuntimePaneIO(runtime, terminal)
    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=session.id,
        to_session=session.id,
        content="durable composer-race notification",
        metadata_json='{"wake_requested": true}',
    )

    async def probe(_session: Any, _terminal: Any) -> TerminalActivity:
        return TerminalActivity(_read_editor(await pane.snapshot(mode="ansi")))

    return (
        WakeDispatcher(
            session_manager=session_manager,
            ism_manager=messages,
            tmux_sender=_send_tmux_session_wake,
            terminal_manager=store,
            activity_probe=probe,
        ),
        messages,
        runtime,
        session.id,
        pane,
    )


async def _cancel_retries(dispatcher: WakeDispatcher) -> None:
    tasks = list(dispatcher._composer_retries.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _send_command(
    pane: RuntimePaneIO, command: str, session_id: str, cli_source: str
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
        _send_terminal_compaction_command,
    )

    _PHASE.set("command")
    return await _send_terminal_compaction_command(
        pane,
        command,
        session_id,
        cli_source=cli_source,
        mark_continuation_pending=_true,
        clear_continuation_pending=_true,
        turn_settled=_true,
        composer_read=_read_editor,
        settle_seconds=0.0,
    )


async def test_real_wake_and_compaction_writers_do_not_interleave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
        _send_terminal_compaction_command,
    )

    store, runtime, coordinator, terminal_id = _arbitrated()
    terminal = store.get(terminal_id)
    assert terminal is not None
    pane = RuntimePaneIO(runtime, terminal)

    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services", lambda: (store, coordinator)
    )
    monkeypatch.setattr(composer_lock_module, "_coordinator", coordinator)
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", _no_sleep)

    # Tag every recorded write with the writer that produced it.
    phases: list[str] = []
    original_record = runtime._record

    async def tagged_record(kind: str, payload: str | bytes) -> Any:
        phases.append(_PHASE.get())
        return await original_record(kind, payload)

    monkeypatch.setattr(runtime, "_record", tagged_record)

    first_write_seen = asyncio.Event()
    release = asyncio.Event()
    runtime.gate = lambda: first_write_seen.set()
    runtime.hold = release

    async def wake_writer() -> None:
        _PHASE.set("wake")
        await _send_tmux_session_wake(
            "gobby-agent-arbitration",
            "Message from Gobby daemon: New activity available.",
            submit=True,
            clear_before_submit=True,
            cli_source="claude",
        )

    async def compact_writer() -> None:
        _PHASE.set("compact")
        await _send_terminal_compaction_command(
            pane,
            "/compact",
            "session-arbitration",
            cli_source="claude",
            mark_continuation_pending=_true,
            clear_continuation_pending=_true,
            settle_seconds=0.0,
        )

    wake = asyncio.create_task(wake_writer())
    await asyncio.wait_for(first_write_seen.wait(), timeout=5)
    compact = asyncio.create_task(compact_writer())
    # Yield to the compact task; it must block on the held composer lock, so it
    # cannot record any write while the wake is mid-sequence.
    for _ in range(10):
        await asyncio.sleep(0)
    assert set(phases) == {"wake"}
    release.set()
    await asyncio.wait_for(asyncio.gather(wake, compact), timeout=5)

    # No interleaving: each writer's writes form one contiguous block.
    blocks = [
        phase for index, phase in enumerate(phases) if index == 0 or phases[index - 1] != phase
    ]
    assert len(blocks) == len(set(blocks)), blocks
    assert len(blocks) == 2, blocks
    assert set(phases) == {"wake", "compact"}


@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
@pytest.mark.parametrize("first_writer", ["wake", "command"])
async def test_wake_and_handoff_writes_never_concatenate_across_providers(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    cli_source: str,
    command: str,
    first_writer: str,
) -> None:
    """Race at staged text, before Enter; inspect actual editor and submissions."""
    dispatcher, messages, runtime, session_id, pane = _editor_seat(
        temp_db, session_manager, sample_project["id"], cli_source, monkeypatch
    )
    runtime.hold_phase = first_writer

    async def wake_writer() -> dict[str, Any]:
        _PHASE.set("wake")
        return await dispatcher.dispatch_live_wake(session_id)

    async def command_writer() -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        return await _send_command(pane, command, session_id, cli_source)

    wake: asyncio.Task[dict[str, Any]] | None = None
    compact: asyncio.Task[tuple[bool, str | None, bool, dict[str, Any] | None]] | None = None
    try:
        if first_writer == "wake":
            wake = asyncio.create_task(wake_writer())
        else:
            compact = asyncio.create_task(command_writer())
        await asyncio.wait_for(runtime.staged.wait(), timeout=5)
        staged = CONTINUE_WAKE_MESSAGE if first_writer == "wake" else command
        assert runtime.buffer == staged
        assert runtime.submitted == []
        if wake is None:
            wake = asyncio.create_task(wake_writer())
        else:
            compact = asyncio.create_task(command_writer())
        for _ in range(10):
            await asyncio.sleep(0)
        assert set(runtime.phases) == {first_writer}
        assert runtime.buffer == staged
        assert runtime.submitted == []
        assert len(messages.get_undelivered_wake_messages(session_id)) == 1
        runtime.release_staged.set()
        assert compact is not None
        wake_result, compact_result = await asyncio.wait_for(
            asyncio.gather(wake, compact), timeout=5
        )
        assert wake_result["delivered"] is True
        assert compact_result[0] is True
        expected = [CONTINUE_WAKE_MESSAGE, command]
        assert runtime.submitted == (expected if first_writer == "wake" else expected[::-1])
        assert runtime.buffer == ""
        blocks = [
            phase
            for index, phase in enumerate(runtime.phases)
            if index == 0 or runtime.phases[index - 1] != phase
        ]
        assert blocks == [first_writer, "command" if first_writer == "wake" else "wake"]
        assert [
            message.content for message in messages.get_undelivered_wake_messages(session_id)
        ] == ["durable composer-race notification"]
    finally:
        runtime.release_staged.set()
        for task in (wake, compact):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (wake, compact) if task), return_exceptions=True)
        await _cancel_retries(dispatcher)


@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
@pytest.mark.parametrize("unreadable", [False, True], ids=["draft", "unknown"])
async def test_wake_and_handoff_preserve_operator_draft_and_durable_message(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    cli_source: str,
    command: str,
    unreadable: bool,
) -> None:
    dispatcher, messages, runtime, session_id, pane = _editor_seat(
        temp_db, session_manager, sample_project["id"], cli_source, monkeypatch
    )
    runtime.buffer = "operator's unfinished word"
    runtime.unreadable = unreadable
    try:
        wake = await dispatcher.dispatch_live_wake(session_id)
        compact = await _send_command(pane, command, session_id, cli_source)
        assert wake["delivered"] is False
        assert wake["skipped"] == ("composer_unconfirmed" if unreadable else "composer_occupied")
        assert compact[0] is False
        assert runtime.write_log == []
        assert runtime.buffer == "operator's unfinished word"
        assert runtime.submitted == []
        assert len(messages.get_undelivered_wake_messages(session_id)) == 1
    finally:
        await _cancel_retries(dispatcher)


@pytest.mark.parametrize("cli_source", ["claude", "codex"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_cancelled_staged_handoff_keeps_wake_durable_without_submitting_it(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    cli_source: str,
    command: str,
) -> None:
    dispatcher, messages, runtime, session_id, pane = _editor_seat(
        temp_db, session_manager, sample_project["id"], cli_source, monkeypatch
    )
    runtime.hold_phase = "command"
    compact = asyncio.create_task(_send_command(pane, command, session_id, cli_source))
    wake: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(runtime.staged.wait(), timeout=5)
        assert runtime.buffer == command
        wake = asyncio.create_task(dispatcher.dispatch_live_wake(session_id))
        compact.cancel()
        with pytest.raises(asyncio.CancelledError):
            await compact
        result = await asyncio.wait_for(wake, timeout=5)
        assert result["delivered"] is False
        assert result["skipped"] == "composer_occupied"
        assert runtime.buffer == command
        assert runtime.submitted == []
        assert CONTINUE_WAKE_MESSAGE not in [payload for _kind, payload in runtime.write_log]
        assert len(messages.get_undelivered_wake_messages(session_id)) == 1
    finally:
        runtime.release_staged.set()
        for task in (wake, compact):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (wake, compact) if task), return_exceptions=True)
        await _cancel_retries(dispatcher)


async def test_compaction_holds_the_lock_across_its_whole_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
        _send_terminal_compaction_command,
    )

    store, runtime, coordinator, terminal_id = _arbitrated()
    terminal = store.get(terminal_id)
    assert terminal is not None
    pane = RuntimePaneIO(runtime, terminal)

    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(composer_lock_module, "_coordinator", coordinator)

    lock_held: list[bool] = []
    runtime.gate = lambda: lock_held.append(coordinator.logical_action_lock(terminal_id).locked())

    await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-arbitration",
        cli_source="claude",
        mark_continuation_pending=_true,
        clear_continuation_pending=_true,
        settle_seconds=0.0,
    )

    assert lock_held and all(lock_held), lock_held


async def _no_sleep(_seconds: float) -> None:
    return None


def _true() -> bool:
    return True
