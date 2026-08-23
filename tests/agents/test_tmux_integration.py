"""Integration tests for real tmux session semantics."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.tmux.history import capture_history
from gobby.agents.tmux.output_reader import TmuxOutputReader
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.integration


@pytest.fixture
def tmux_socket_name() -> Iterator[str]:
    """Provide a unique tmux socket name and remove its server at teardown."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    socket_name = f"gobby-test-{uuid4().hex}"
    yield socket_name
    subprocess.run(
        ["tmux", "-L", socket_name, "-f", "/dev/null", "kill-server"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


@pytest.fixture
def tmux_config(tmux_socket_name: str) -> TmuxConfig:
    return TmuxConfig(socket_name=tmux_socket_name, config_file="/dev/null")


@pytest.fixture
async def tmux_manager(tmux_config: TmuxConfig) -> AsyncIterator[TmuxSessionManager]:
    manager = TmuxSessionManager(tmux_config)
    yield manager
    await manager.shutdown()


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


def _path_contains(path: Path, expected: str) -> bool:
    return path.exists() and expected in path.read_text(encoding="utf-8")


async def test_get_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    await tmux_manager.create_session(
        name="agent-extra",
        command="tail -f /dev/null",
    )

    assert await tmux_manager.get_session("agent") is None

    exact = await tmux_manager.get_session("agent-extra")
    assert exact is not None
    assert exact.name == "agent-extra"


async def test_kill_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    await tmux_manager.create_session(
        name="agent-157",
        command="tail -f /dev/null",
    )

    assert await tmux_manager.kill_session("agent-15", missing_ok=True, timeout=0.1) is True

    exact = await tmux_manager.get_session("agent-157")
    assert exact is not None
    assert exact.name == "agent-157"


async def test_create_session_preserves_env_value_with_trailing_semicolon(
    tmux_manager: TmuxSessionManager,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "env-value.txt"

    await tmux_manager.create_session(
        name="env-semicolon",
        command='printf "%s" "$TRAILING_VALUE" > "$GOBBY_OUT"; tail -f /dev/null',
        env={
            "GOBBY_OUT": str(output_path),
            "TRAILING_VALUE": "value;",
        },
    )

    await _wait_for(lambda: output_path.exists())
    assert output_path.read_text(encoding="utf-8") == "value;"


async def test_send_keys_pastes_multiline_literal_text(
    tmux_manager: TmuxSessionManager,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "paste-output.txt"
    quoted_output = shlex.quote(str(output_path))
    command = (
        "while IFS= read -r line; do "
        f"printf '%s\\n' \"$line\" >> {quoted_output}; "
        '[ "$line" = done ] && break; '
        "done; tail -f /dev/null"
    )

    await tmux_manager.create_session(name="paste-target", command=command)
    assert await tmux_manager.send_keys("paste-target", "alpha\nbeta\ndone\n")

    await _wait_for(lambda: _path_contains(output_path, "alpha\nbeta\ndone\n"))


async def test_pty_bridge_attach_renders_truecolor(
    tmux_manager: TmuxSessionManager,
    tmux_config: TmuxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The truecolor SGR is printed before attaching: tmux repaints the screen
    # on attach, re-encoding each cell's colors with the client's advertised
    # features, so the bridge output only carries the 24-bit triplet when the
    # attach client declares RGB support (gobby-#20063). A developer shell's
    # COLORTERM=truecolor would leak into the attach client and enable RGB on
    # its own; the daemon runs without it, so strip it to prove the -T flag.
    monkeypatch.delenv("COLORTERM", raising=False)
    await tmux_manager.create_session(
        name="truecolor-target",
        command=("printf '\\033[48;2;20;80;40mTRUECOLOR-MARKER\\033[0m\\n'; tail -f /dev/null"),
    )

    bridge = TmuxPTYBridge()
    collected = bytearray()
    master_fd = await bridge.attach(
        session_name="truecolor-target",
        streaming_id="bridge-truecolor",
        config=tmux_config,
    )
    try:
        os.set_blocking(master_fd, False)

        def _pump() -> str:
            try:
                while chunk := os.read(master_fd, 65536):
                    collected.extend(chunk)
            except BlockingIOError:
                pass
            return collected.decode("utf-8", errors="replace")

        await _wait_for(lambda: "TRUECOLOR-MARKER" in _pump(), timeout=10.0)
        text = _pump()
        # tmux emits semicolon or colon (possibly double-colon) subparameters
        # depending on version; a 256-color downgrade matches neither.
        assert re.search(r"48[:;]2[:;]{1,2}20[:;]80[:;]40", text), text
    finally:
        await bridge.detach("bridge-truecolor")


async def test_output_reader_streams_multibyte_fifo_data(
    tmux_manager: TmuxSessionManager,
    tmux_config: TmuxConfig,
) -> None:
    chunks: list[str] = []

    async def collect(_run_id: str, data: str) -> None:
        chunks.append(data)

    await tmux_manager.create_session(name="fifo-target")
    reader = TmuxOutputReader(tmux_config)
    reader.set_output_callback(collect)
    started = await reader.start_reader("run-fifo", "fifo-target")
    assert started is True

    try:
        assert await tmux_manager.send_keys(
            "fifo-target", "printf 'fifo-multibyte: cafe é 漢\\n'\n"
        )
        await _wait_for(lambda: "fifo-multibyte: cafe é 漢" in "".join(chunks))
    finally:
        await reader.stop_reader("run-fifo")


async def _tmux_query(manager: TmuxSessionManager, *args: str) -> str:
    """Run a read-only tmux command against the test server."""
    proc = await asyncio.create_subprocess_exec(
        *manager.base_args(),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace").strip()


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 10.0,
) -> None:
    """Poll an awaitable predicate, so tests wait on state rather than a clock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


async def _visible_pane(manager: TmuxSessionManager, session: str) -> str:
    return await _tmux_query(manager, "capture-pane", "-t", f"={session}:", "-p")


async def test_capture_history_excludes_the_visible_pane(
    tmux_manager: TmuxSessionManager,
) -> None:
    # -E -1 ends one line above the visible pane, so the history window and the
    # repaint that follows it do not overlap. Only a real tmux server has a
    # real screen/scrollback split to prove that against.
    await tmux_manager.create_session(
        name="history-boundary",
        command="for i in $(seq 1 60); do printf 'hline-%04d\\n' \"$i\"; done; tail -f /dev/null",
    )

    async def printed() -> bool:
        return "hline-0060" in await _visible_pane(tmux_manager, "history-boundary")

    await _wait_until(printed)

    capture = await capture_history(tmux_manager, "history-boundary")

    # The first line has long since scrolled off a 24-row pane.
    assert "hline-0001" in capture.text
    # The last line printed is on screen -- the wait above just observed it
    # there -- so the history window must stop short of it.
    assert "hline-0060" not in capture.text
    assert capture.truncated is False


async def test_capture_history_probe_truncates_real_scrollback(
    tmux_manager: TmuxSessionManager,
) -> None:
    await tmux_manager.create_session(
        name="history-probe",
        command="for i in $(seq 1 60); do printf 'pline-%04d\\n' \"$i\"; done; tail -f /dev/null",
    )

    async def printed() -> bool:
        return "pline-0060" in await _visible_pane(tmux_manager, "history-probe")

    await _wait_until(printed)

    capture = await capture_history(tmux_manager, "history-probe", max_lines=5)

    assert capture.truncated is True
    assert len(capture.text.split("\r\n")) == 5
    assert "pline-0001" not in capture.text


async def test_capture_history_uses_the_attached_client_width(
    tmux_manager: TmuxSessionManager,
    tmux_config: TmuxConfig,
) -> None:
    # A scrollback row keeps the width it was stored at. If capture ran before
    # the attaching client registered its geometry, a line wider than the
    # default 80 columns would have been stored soft-wrapped, and -J is
    # deliberately absent, so it would come back split across two rows.
    wide = "W" * 100
    await tmux_manager.create_session(name="history-width", command="tail -f /dev/null")

    bridge = TmuxPTYBridge()
    await bridge.attach(
        session_name="history-width",
        streaming_id="bridge-width",
        config=tmux_config,
        rows=40,
        cols=120,
    )
    try:
        # The client must be registered and the pane resized before anything is
        # printed, or the wide line is stored at the old width.
        async def resized() -> bool:
            width = await _tmux_query(
                tmux_manager, "display-message", "-p", "-t", "=history-width:", "#{pane_width}"
            )
            return width == "120"

        await _wait_until(resized)

        await tmux_manager.send_keys(
            "history-width",
            f"printf '%s\\n' {wide}; for i in $(seq 1 60); do printf 'x-%04d\\n' \"$i\"; done",
        )

        async def scrolled_off() -> bool:
            return "W" in (await capture_history(tmux_manager, "history-width")).text

        await _wait_until(scrolled_off)

        capture = await capture_history(tmux_manager, "history-width")

        rows = [row for row in capture.text.split("\r\n") if "W" in row]
        assert rows, capture.text
        # One unbroken row, not two halves of a soft wrap.
        assert any(wide in row for row in rows), rows[:4]
    finally:
        await bridge.detach("bridge-width")
