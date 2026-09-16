"""Live-backend acceptance fixtures (plan 7.2).

Every test in this package drives a real backend: a ``gterm host`` built from
this tree, or a private tmux server. Spawns take a 120x40 PTY, the geometry the
plan names for acceptance.

The package is gated on ``GOBBY_RUN_VENDOR_BUILD=1`` because it builds the
vendored libghostty-vt layer. Without the opt-in the whole package skips, so
guard set G group 2 keeps running ``tests/terminals`` on a machine with no Zig
toolchain.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gobby.agents.spawn_executor import derive_spawn_key
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig
from gobby.storage.terminals import Terminal
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
from gobby.terminals.runtime import (
    Delivered,
    PreparedSpawn,
    TerminalRuntime,
    TerminalSpawnRequest,
)
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests._timing import wait_for_awaited_condition

ACCEPTANCE_ROWS = 40
ACCEPTANCE_COLS = 120
#: Bounded waits for a real child to paint a line on a real PTY.
SCREEN_TIMEOUT = 20.0
#: Bounded wait for a real backend to settle a lifecycle transition.
SETTLE_TIMEOUT = 20.0
#: A cold `cargo build` of the vendored vt engine is minutes, not seconds.
BUILD_TIMEOUT = 2400.0
#: The child every acceptance spawn runs: a POSIX shell on the PTY.
SHELL_COMMAND = ("/bin/sh",)
#: A predictable environment for that shell on both macOS and Linux.
SHELL_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "TERM": "xterm-256color",
    "PS1": "$ ",
}

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_BUILD_ENABLED = os.environ.get("GOBBY_RUN_VENDOR_BUILD") == "1"
VENDOR_BUILD_REASON = (
    "the acceptance suite builds gterm from this tree, which requires a working "
    "Zig toolchain; set GOBBY_RUN_VENDOR_BUILD=1 to run"
)


@pytest.fixture(scope="session", autouse=True)
def _requires_vendor_build() -> None:
    """Skip the whole package unless the vendored host build is opted in."""
    if not VENDOR_BUILD_ENABLED:
        pytest.skip(VENDOR_BUILD_REASON)


@pytest.fixture(scope="session")
def gterm_host_binary(_requires_vendor_build: None) -> Path:
    """Build ``gterm`` from this tree and return the artifact.

    Acceptance never runs an installed host: the plan requires the suite to run
    against a host built from the tree under test.
    """
    build = subprocess.run(
        ["cargo", "build", "-p", "gobby-terminal", "--features", "vt-engine", "--bin", "gterm"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT,
    )
    if build.returncode != 0:
        pytest.fail(f"building gterm from the tree failed:\n{build.stdout}\n{build.stderr}")
    metadata = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT,
    )
    if metadata.returncode != 0:
        pytest.fail(f"cargo metadata failed:\n{metadata.stderr}")
    target_directory = Path(str(json.loads(metadata.stdout)["target_directory"]))
    binary = target_directory / "debug" / "gterm"
    if not binary.is_file():
        pytest.fail(f"cargo build left no gterm at {binary}")
    return binary


def short_dir(prefix: str) -> Path:
    """Return a temp directory whose gterm sockets fit AF_UNIX's 104 bytes."""
    root = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix=prefix[:1], dir=root)).resolve()
    if len(os.fsencode(path / "gterm-control.sock")) >= 104:
        path.rmdir()
        pytest.fail(f"permitted temp root is too long for AF_UNIX sockets: {root}")
    return path


@dataclass
class AcceptanceHost:
    """A live ``gterm host`` and the native runtime that drives it."""

    manager: TerminalHostManager
    runtime: NativeTerminalRuntime
    socket_dir: Path
    workdir: Path


@dataclass
class AcceptanceTmux:
    """A private tmux server and the tmux runtime that drives it."""

    sessions: TmuxSessionManager
    runtime: TmuxTerminalRuntime
    socket: Path
    workdir: Path


@dataclass
class LiveTerminal:
    """One committed terminal plus the runtime that owns it."""

    backend: str
    runtime: TerminalRuntime
    terminal: Terminal


@pytest.fixture
def acceptance_workdir() -> Iterator[Path]:
    """A working directory for the spawned children."""
    workdir = short_dir("w")
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture
async def native_host(
    gterm_host_binary: Path, acceptance_workdir: Path
) -> AsyncIterator[AcceptanceHost]:
    """Start one freshly built ``gterm host`` for a single test."""
    socket_dir = short_dir("h")
    (socket_dir / "local_cli_token").write_text("acceptance-frame-token\n", encoding="utf-8")
    manager = TerminalHostManager(
        config=TerminalHostConfig(
            enabled=True,
            socket_dir=str(socket_dir),
            binary_path=str(gterm_host_binary),
            # The suite drives every restart explicitly; a health tick would
            # race the crash and drain cases against their own assertions.
            health_interval_seconds=3600.0,
        ),
        terminal_config=TerminalConfig(),
    )
    await manager.start()
    if not manager.native_available or manager.host_epoch is None:
        await manager.stop(drain_host=True)
        shutil.rmtree(socket_dir, ignore_errors=True)
        pytest.fail(f"gterm host failed to start: {manager.last_error}")
    runtime = NativeTerminalRuntime(HostManagerControl(manager))
    # The manager owns the event stream; the runtime must not subscribe on the
    # control connection it shares with it.
    runtime._subscribed = True
    try:
        yield AcceptanceHost(
            manager=manager,
            runtime=runtime,
            socket_dir=socket_dir,
            workdir=acceptance_workdir,
        )
    finally:
        await runtime.close_frame_streams()
        if not manager.host_drained:
            await manager.stop(drain_host=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
async def tmux_server(acceptance_workdir: Path) -> AsyncIterator[AcceptanceTmux]:
    """Start one private tmux server for a single test."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux binary is not available")
    socket = acceptance_workdir / "tmux.sock"
    sessions = TmuxSessionManager(TmuxConfig(socket_name="", socket_path=str(socket)))
    try:
        yield AcceptanceTmux(
            sessions=sessions,
            runtime=TmuxTerminalRuntime(sessions),
            socket=socket,
            workdir=acceptance_workdir,
        )
    finally:
        await sessions._run("kill-server")
        socket.unlink(missing_ok=True)


def terminal_from_prepared(
    *,
    backend: str,
    terminal_id: UUID,
    spawn_key: str,
    prepared: PreparedSpawn,
) -> Terminal:
    """Build the storage row a committed spawn would have persisted."""
    now = datetime.now(UTC)
    epoch = ""
    if prepared.locator is not None:
        epoch = str(prepared.locator.frame_host_epoch or "")
    return Terminal(
        id=str(terminal_id),
        backend=backend,
        ownership="gobby",
        state="live",
        machine_id=str(uuid4()),
        project_id=str(uuid4()),
        created_at=now,
        updated_at=now,
        attempt_generation=1,
        attempt_started_at=now,
        unresolved_writes={},
        spawn_key=spawn_key,
        locator=dict(prepared.stored_locator or {}),
        locator_key=prepared.locator_key,
        session_name=spawn_key if backend == "tmux" else None,
        host_epoch=epoch or None,
        rows=ACCEPTANCE_ROWS,
        cols=ACCEPTANCE_COLS,
    )


def spawn_request(backend: str, command: tuple[str, ...], cwd: Path) -> TerminalSpawnRequest:
    """Build the daemon-shaped spawn request for one acceptance child."""
    terminal_id = uuid4()
    return TerminalSpawnRequest(
        terminal_id=terminal_id,
        spawn_key=derive_spawn_key(backend, str(terminal_id)),
        command=list(command),
        cwd=str(cwd),
        env=dict(SHELL_ENV),
        rows=ACCEPTANCE_ROWS,
        cols=ACCEPTANCE_COLS,
    )


async def prepare_native(
    host: AcceptanceHost,
    *,
    command: tuple[str, ...] = SHELL_COMMAND,
) -> tuple[TerminalSpawnRequest, Mapping[str, str], PreparedSpawn]:
    """Reserve an observer, prepare the spawn, and bind the frame stream.

    This is the daemon's own order: reserve, prepare, acknowledge the persist,
    bind the observer, and only then commit.
    """
    request = spawn_request("native", command, host.workdir)
    reservation = await host.runtime.reserve_observer(request.terminal_id)
    request.reservation_id = reservation["reservation_id"]
    request.reserve_key = reservation["reserve_key"]
    prepared = await host.runtime.prepare_spawn(request)
    prepared.acknowledge_persist()
    await host.runtime.bind_observer(prepared, reservation["reservation_id"])
    return request, reservation, prepared


async def spawn_native(
    host: AcceptanceHost,
    *,
    command: tuple[str, ...] = SHELL_COMMAND,
) -> LiveTerminal:
    """Spawn and commit one native terminal on a 120x40 PTY."""
    request, _reservation, prepared = await prepare_native(host, command=command)
    await host.runtime.commit_spawn(prepared)
    terminal = terminal_from_prepared(
        backend="native",
        terminal_id=request.terminal_id,
        spawn_key=request.spawn_key,
        prepared=prepared,
    )
    return await _answering_shell(
        LiveTerminal(backend="native", runtime=host.runtime, terminal=terminal)
    )


async def spawn_tmux(
    tmux: AcceptanceTmux,
    *,
    command: tuple[str, ...] = SHELL_COMMAND,
) -> LiveTerminal:
    """Spawn and commit one tmux terminal on a 120x40 PTY."""
    request = spawn_request("tmux", command, tmux.workdir)
    prepared = await tmux.runtime.prepare_spawn(request)
    prepared.acknowledge_persist()
    await tmux.runtime.commit_spawn(prepared)
    assert (prepared.rows, prepared.cols) == (ACCEPTANCE_ROWS, ACCEPTANCE_COLS)
    terminal = terminal_from_prepared(
        backend="tmux",
        terminal_id=request.terminal_id,
        spawn_key=request.spawn_key,
        prepared=prepared,
    )
    return await _answering_shell(
        LiveTerminal(backend="tmux", runtime=tmux.runtime, terminal=terminal)
    )


async def _answering_shell(live: LiveTerminal) -> LiveTerminal:
    """Return the terminal once its shell has answered one command."""
    marker = await emit_marker(live, "SHELL-READY")
    await wait_for_text(live, marker, description="the spawned shell to answer")
    return live


async def wait_for_text(live: LiveTerminal, needle: str, *, description: str) -> str:
    """Wait until ``needle`` is on the terminal's screen and return the screen."""

    async def painted() -> str:
        snapshot = await live.runtime.snapshot(live.terminal, lines=80)
        return snapshot.text if needle in snapshot.text else ""

    return await wait_for_awaited_condition(
        painted, timeout=SCREEN_TIMEOUT, interval=0.05, description=description
    )


async def write_line(live: LiveTerminal, line: str) -> None:
    """Send one submitted command line through the runtime."""
    outcome = await live.runtime.write_text(live.terminal, line, True)
    assert isinstance(outcome, Delivered), outcome


def marker_command(prefix: str) -> tuple[str, str]:
    """Return a shell command printing a marker, and the marker itself.

    The token travels as a ``printf`` argument, so the terminal's echo of the
    command line never contains the marker the caller waits for and a match
    always means the child ran.
    """
    token = uuid4().hex[:12]
    return f"printf '{prefix}-%s\\n' {token}", f"{prefix}-{token}"


async def emit_marker(live: LiveTerminal, prefix: str) -> str:
    """Make the child print a unique marker and return it."""
    command, marker = marker_command(prefix)
    await write_line(live, command)
    return marker


async def observed_size(live: LiveTerminal) -> tuple[int, int]:
    """Ask the child's own PTY for its size and read the answer off the screen."""
    token = uuid4().hex[:12]
    await write_line(live, f"printf '%s-SIZE-{token}\\n' \"$(stty size)\"")
    pattern = re.compile(rf"(\d+) (\d+)-SIZE-{token}")

    async def painted() -> re.Match[str] | None:
        snapshot = await live.runtime.snapshot(live.terminal, lines=80)
        return pattern.search(snapshot.text)

    match = await wait_for_awaited_condition(
        painted, timeout=SCREEN_TIMEOUT, interval=0.05, description="stty size on the screen"
    )
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def wait_until_dead(live: LiveTerminal) -> None:
    """Wait until the backend stops reporting the terminal as live."""

    async def gone() -> str:
        return "" if await live.runtime.is_live(live.terminal) else "gone"

    await wait_for_awaited_condition(
        gone, timeout=SETTLE_TIMEOUT, interval=0.05, description="the child to settle as exited"
    )
