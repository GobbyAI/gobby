"""Native host lifecycle acceptance against a real gterm host (plan 7.2).

| Test | Plan item it proves |
| --- | --- |
| `test_spawn_commit_and_exit_settle` | 7.2.1 spawn, commit, and exit settlement on the 120x40 PTY the plan names |
| `test_exec_failure_is_typed` | 1.2.13 an exec failure from the gate is `exec_failed:<code>` with errno detail, settled `fail_pending` |
| `test_host_crash_respawns_and_fences_the_old_epoch` | 1.2.3 one respawn after an unexpected host death, and 3.2.5 a row from the earlier epoch refused `host_epoch_changed` |
| `test_drain_ends_the_host_and_refuses_respawn` | 3.1.2 `host_shutdown` really drains the host, and 1.2.3 a drained manager never respawns |
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from gobby.terminals.host_client import (
    HostCommandError,
    HostEpochChangedError,
    HostManagerStopped,
)
from gobby.terminals.native_runtime import classify_native_spawn_failure
from gobby.terminals.runtime import TerminalWriteError
from tests.terminals.acceptance.conftest import (
    ACCEPTANCE_COLS,
    ACCEPTANCE_ROWS,
    AcceptanceHost,
    emit_marker,
    observed_size,
    prepare_native,
    spawn_native,
    wait_for_text,
    wait_until_dead,
    write_line,
)


async def test_spawn_commit_and_exit_settle(native_host: AcceptanceHost) -> None:
    """A committed child owns a 120x40 PTY, and its exit settles the row."""
    live = await spawn_native(native_host)

    assert (live.terminal.locator or {})["host_terminal_id"]
    assert live.terminal.host_epoch == native_host.manager.host_epoch
    assert await live.runtime.is_live(live.terminal) is True

    marker = await emit_marker(live, "COMMITTED")
    await wait_for_text(live, marker, description="the committed child to answer")
    assert await observed_size(live) == (ACCEPTANCE_ROWS, ACCEPTANCE_COLS)

    await write_line(live, "exit")
    await wait_until_dead(live)

    with pytest.raises(TerminalWriteError) as refused:
        await emit_marker(live, "AFTER-EXIT")
    assert refused.value.stage in {"none", "partial"}


async def test_exec_failure_is_typed(native_host: AcceptanceHost) -> None:
    """A missing executable fails the commit as `exec_failed:<code>`."""
    missing = str(native_host.workdir / "gobby-acceptance-missing-command")
    _request, _reservation, prepared = await prepare_native(
        native_host, command=(missing, "--no-such-flag")
    )

    with pytest.raises(HostCommandError) as failure:
        await native_host.runtime.commit_spawn(prepared)

    assert failure.value.error == "exec_failed"
    assert failure.value.code == "ENOENT"
    code, detail, settlement = classify_native_spawn_failure(failure.value)
    assert code == "exec_failed:ENOENT"
    assert settlement == "fail_pending"
    assert detail is not None and detail


async def test_host_crash_respawns_and_fences_the_old_epoch(
    native_host: AcceptanceHost,
) -> None:
    """An unexpected host death respawns once and strands the old epoch's rows."""
    stranded = await spawn_native(native_host)
    manager = native_host.manager
    crashed_epoch = manager.host_epoch
    crashed_pid = manager.host_pid
    spawns_before = manager.restart_count
    assert crashed_epoch is not None
    assert crashed_pid is not None

    os.kill(crashed_pid, signal.SIGKILL)

    # Two concurrent callers share one restart: the epoch is the same for both
    # and the host is spawned once.
    epoch, joined = await asyncio.gather(manager.ensure_restart(), manager.ensure_restart())
    assert epoch == joined
    assert epoch != crashed_epoch
    assert manager.host_epoch == epoch
    assert manager.native_available is True
    assert manager.host_pid not in {None, crashed_pid}
    # One host spawn, not one per caller: `ensure_restart` is singleflight.
    assert manager.restart_count == spawns_before + 1

    with pytest.raises(HostEpochChangedError):
        await native_host.runtime.resize(stranded.terminal, 30, 100)
    assert await native_host.runtime.is_live(stranded.terminal) is False

    replacement = await spawn_native(native_host)
    assert replacement.terminal.host_epoch == epoch
    marker = await emit_marker(replacement, "RESPAWNED")
    await wait_for_text(replacement, marker, description="the child on the replacement host")


async def test_drain_ends_the_host_and_refuses_respawn(native_host: AcceptanceHost) -> None:
    """`stop(drain_host=True)` takes the host down and closes it to respawn."""
    live = await spawn_native(native_host)
    marker = await emit_marker(live, "BEFORE-DRAIN")
    await wait_for_text(live, marker, description="the child before the drain")
    manager = native_host.manager
    drained_pid = manager.host_pid
    assert drained_pid is not None

    await manager.stop(drain_host=True)

    assert manager.host_drained is True
    assert manager.running is False
    assert not _process_alive(drained_pid)
    with pytest.raises(HostManagerStopped):
        await manager.ensure_restart()


def _process_alive(pid: int) -> bool:
    """Report whether the drained or killed host process is still present."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
