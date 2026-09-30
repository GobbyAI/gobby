"""Serialization of the one physical composer across wake and handoff writers.

#22915 criterion 6: an earlier empty snapshot cannot authorize a later
overlapping write. These tests race two real writers -- the daemon wake sender
and the compaction/clear ladder -- on a shared terminal and prove their writes
never interleave, so a wake cannot be typed into a staged ``/compact``.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, cast

import pytest

from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.terminals import composer_lock as composer_lock_module
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.pane_io import RuntimePaneIO
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
async def test_wake_and_handoff_writes_never_concatenate_across_providers(
    monkeypatch: pytest.MonkeyPatch,
    cli_source: str,
    command: str,
) -> None:
    """Criterion 6: a wake and a staged command never merge into one composer line.

    The wake text and the command text must each appear as their own contiguous
    write, and neither may be appended to the other -- the exact failure behind
    "Unknown command: /compactMessage" on a staged handoff.
    """
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

    first_write_seen = asyncio.Event()
    release = asyncio.Event()
    runtime.gate = lambda: first_write_seen.set()
    runtime.hold = release

    wake_text = "Message from Gobby daemon: New activity available."

    async def wake_writer() -> None:
        _PHASE.set("wake")
        await _send_tmux_session_wake(
            "gobby-agent-arbitration",
            wake_text,
            submit=True,
            clear_before_submit=True,
            cli_source=cli_source,
        )

    async def command_writer() -> None:
        _PHASE.set("command")
        await _send_terminal_compaction_command(
            pane,
            command,
            "session-arbitration",
            cli_source=cli_source,
            mark_continuation_pending=_true,
            clear_continuation_pending=_true,
            settle_seconds=0.0,
        )

    # Race both orderings: the command waits on the held lock while the wake is
    # mid-sequence, and vice versa, because either writer may take it first.
    for first, second in ((wake_writer, command_writer), (command_writer, wake_writer)):
        runtime.write_log.clear()
        first_write_seen.clear()
        release.clear()
        runtime.hold = release
        started = asyncio.create_task(first())
        await asyncio.wait_for(first_write_seen.wait(), timeout=5)
        racing = asyncio.create_task(second())
        for _ in range(10):
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(started, racing), timeout=5)

    texts = [
        payload
        for kind, payload in runtime.write_log
        if kind == "text" and isinstance(payload, str)
    ]
    # Each writer's own text is a standalone write; the wake text never carries
    # the command and the command never carries the wake text.
    assert wake_text in texts
    assert any(payload.rstrip("\n").endswith(command) for payload in texts)
    for payload in texts:
        if wake_text in payload:
            assert payload.strip() == wake_text
        if command in payload:
            assert payload.startswith(command)


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
