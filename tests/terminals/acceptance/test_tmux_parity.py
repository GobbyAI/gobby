"""Runtime parity between an external tmux pane and a native terminal (plan 7.2).

Gobby spawns no tmux: a tmux row is a pane on a server the user started, which
Gobby addresses by its recorded socket. Each case runs twice, once against such
a pane on a private tmux server the test starts itself and once against a
terminal spawned on a ``gterm host`` built from this tree, so the parity claim
is the same assertions on both sides. Every case reads the child's output back
through ``snapshot``.

Spawn, attach identity, and resize are not parity: only native spawns, each
backend hands back a different attach identity, and an external pane's
geometry belongs to its owner.

| Test | What it proves on both backends |
| --- | --- |
| `test_write_and_snapshot_parity` | text, raw input, and a named key are delivered, reach the child, and show in the snapshot |
| `test_child_exit_parity` | a child that exits stops being live, and further writes are refused |
| `test_terminate_parity` | `terminate` ends the terminal, it stops being live, and further writes are refused |
"""

from __future__ import annotations

import pytest

from gobby.terminals.runtime import Delivered, TerminalWriteError
from tests.terminals.acceptance.conftest import (
    AcceptanceHost,
    LiveTerminal,
    answering_shell,
    emit_marker,
    marker_command,
    spawn_native,
    wait_for_text,
    wait_until_dead,
    write_line,
)


@pytest.fixture(params=["tmux", "native"])
def parity_backend(request: pytest.FixtureRequest) -> AcceptanceHost | LiveTerminal:
    """The backend this parity cell covers: a live ``gterm host`` or an external pane.

    Resolved lazily, so the tmux cell never builds the vendored host.
    """
    if str(request.param) == "native":
        host = request.getfixturevalue("native_host")
        assert isinstance(host, AcceptanceHost)
        return host
    pane = request.getfixturevalue("external_tmux_pane")
    assert isinstance(pane, LiveTerminal)
    return pane


@pytest.fixture
async def parity_terminal(parity_backend: AcceptanceHost | LiveTerminal) -> LiveTerminal:
    """One live terminal whose shell has answered: a native spawn or the external pane."""
    if isinstance(parity_backend, AcceptanceHost):
        return await spawn_native(parity_backend)
    return await answering_shell(parity_backend)


async def test_write_and_snapshot_parity(parity_terminal: LiveTerminal) -> None:
    """Text, raw input, and a named key reach the child and show in the snapshot."""
    runtime = parity_terminal.runtime
    terminal = parity_terminal.terminal
    assert await runtime.is_live(terminal) is True

    text_marker = await emit_marker(parity_terminal, "PARITY-TEXT")
    await wait_for_text(parity_terminal, text_marker, description="the submitted text write")

    raw_command, raw_marker = marker_command("PARITY-RAW")
    raw = await runtime.write_input(terminal, f"{raw_command}\r".encode())
    assert isinstance(raw, Delivered)
    await wait_for_text(parity_terminal, raw_marker, description="the raw input write")

    key_command, key_marker = marker_command("PARITY-KEY")
    typed = await runtime.write_text(terminal, key_command, False)
    assert isinstance(typed, Delivered)
    key = await runtime.write_key(terminal, "enter")
    assert isinstance(key, Delivered)
    await wait_for_text(parity_terminal, key_marker, description="the named-key submission")


async def test_child_exit_parity(parity_terminal: LiveTerminal) -> None:
    """A child that exits stops being live, and later writes are refused."""
    marker = await emit_marker(parity_terminal, "PARITY-ALIVE")
    await wait_for_text(parity_terminal, marker, description="the child before it exits")

    await write_line(parity_terminal, "exit")
    await wait_until_dead(parity_terminal)

    with pytest.raises(TerminalWriteError) as refused:
        await emit_marker(parity_terminal, "PARITY-AFTER-EXIT")
    assert refused.value.stage in {"none", "partial"}


async def test_terminate_parity(parity_terminal: LiveTerminal) -> None:
    """``terminate`` ends a live terminal, and later writes are refused."""
    runtime = parity_terminal.runtime
    terminal = parity_terminal.terminal
    assert await runtime.is_live(terminal) is True

    await runtime.terminate(terminal, 5.0)
    await wait_until_dead(parity_terminal)

    with pytest.raises(TerminalWriteError) as refused:
        await emit_marker(parity_terminal, "PARITY-AFTER-TERMINATE")
    assert refused.value.stage in {"none", "partial"}
