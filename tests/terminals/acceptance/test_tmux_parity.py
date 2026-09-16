"""The tmux parity list mirrored across both real backends (plan 7.2).

Every case runs twice, once against a private tmux server and once against a
`gterm host` built from this tree, so the parity claim is made of the same
assertions on both sides.

| Test | Plan item it proves |
| --- | --- |
| `test_attach_parity` | 7.2.1 attach: each backend hands back its own attach identity for a committed terminal |
| `test_resize_parity` | 7.2.1 resize: the child's own PTY reports 120x40 at spawn and the new geometry after a resize |
| `test_write_parity` | 7.2.1 write: text, raw input, and a named key are delivered and reach the child |
| `test_exit_parity` | 7.2.1 exit: a child that exits stops being live and further writes are refused |
"""

from __future__ import annotations

import pytest

from gobby.terminals.runtime import Delivered, TerminalWriteError
from tests.terminals.acceptance.conftest import (
    ACCEPTANCE_COLS,
    ACCEPTANCE_ROWS,
    AcceptanceHost,
    AcceptanceTmux,
    LiveTerminal,
    emit_marker,
    marker_command,
    observed_size,
    spawn_native,
    spawn_tmux,
    wait_for_text,
    wait_until_dead,
    write_line,
)

RESIZE_ROWS = 30
RESIZE_COLS = 100


@pytest.fixture(params=["tmux", "native"])
async def parity_terminal(
    request: pytest.FixtureRequest,
    native_host: AcceptanceHost,
    tmux_server: AcceptanceTmux,
) -> LiveTerminal:
    """One committed terminal on the backend this parity cell covers."""
    if str(request.param) == "native":
        return await spawn_native(native_host)
    return await spawn_tmux(tmux_server)


async def test_attach_parity(parity_terminal: LiveTerminal) -> None:
    """Each backend hands back its own attach identity for a live terminal."""
    runtime = parity_terminal.runtime
    terminal = parity_terminal.terminal

    locator = await runtime.attach_locator(terminal)

    assert locator.backend == parity_terminal.backend
    assert await runtime.is_live(terminal) is True
    assert await runtime.session_present(terminal) is True
    if parity_terminal.backend == "native":
        assert locator.host_terminal_id
        assert locator.frame_host_epoch == terminal.host_epoch
    else:
        assert locator.pane_id
        assert locator.socket_path
        assert isinstance(locator.server_pid, int)


async def test_resize_parity(parity_terminal: LiveTerminal) -> None:
    """The child's own PTY reports the spawn geometry, then the resized one."""
    assert await observed_size(parity_terminal) == (ACCEPTANCE_ROWS, ACCEPTANCE_COLS)

    await parity_terminal.runtime.resize(parity_terminal.terminal, RESIZE_ROWS, RESIZE_COLS)

    assert await observed_size(parity_terminal) == (RESIZE_ROWS, RESIZE_COLS)


async def test_write_parity(parity_terminal: LiveTerminal) -> None:
    """Text, raw input, and a named key all reach the child."""
    runtime = parity_terminal.runtime
    terminal = parity_terminal.terminal

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


async def test_exit_parity(parity_terminal: LiveTerminal) -> None:
    """A child that exits stops being live, and later writes are refused."""
    marker = await emit_marker(parity_terminal, "PARITY-ALIVE")
    await wait_for_text(parity_terminal, marker, description="the child before it exits")

    await write_line(parity_terminal, "exit")
    await wait_until_dead(parity_terminal)

    with pytest.raises(TerminalWriteError) as refused:
        await emit_marker(parity_terminal, "PARITY-AFTER-EXIT")
    assert refused.value.stage in {"none", "partial"}
