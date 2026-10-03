"""Integration tests for real tmux session semantics."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager

pytestmark = pytest.mark.integration


@pytest.fixture
def tmux_socket_path() -> Iterator[Path]:
    """Provide a unique tmux socket path and remove its server at teardown."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    socket_path = Path(tempfile.gettempdir()) / f"t-{uuid4().hex[:8]}"
    assert len(str(socket_path).encode()) < 104
    yield socket_path
    subprocess.run(
        ["tmux", "-S", str(socket_path), "-f", "/dev/null", "kill-server"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


@pytest.fixture
def tmux_manager(tmux_socket_path: Path) -> TmuxSessionManager:
    """A manager on the isolated server; ``tmux_socket_path`` kills it at teardown."""
    return TmuxSessionManager(str(tmux_socket_path))


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


def _start_session(manager: TmuxSessionManager, name: str, command: str) -> None:
    """Start a hand-made session on the manager's isolated server.

    ``-f /dev/null`` keeps the user's tmux.conf out of the server it starts.
    """
    subprocess.run(
        [*manager.base_args(), "-f", "/dev/null", "new-session", "-d", "-s", name, command],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


async def test_has_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    _start_session(tmux_manager, "agent-extra", "tail -f /dev/null")

    assert await tmux_manager.has_session("agent") is False
    assert await tmux_manager.has_session("agent-extra") is True


async def test_kill_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    _start_session(tmux_manager, "agent-157", "tail -f /dev/null")

    assert await tmux_manager.kill_session("agent-15", missing_ok=True, timeout=0.1) is True

    assert await tmux_manager.has_session("agent-157") is True


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

    _start_session(tmux_manager, "paste-target", command)
    assert await tmux_manager.send_keys("paste-target", "alpha\nbeta\ndone\n")

    await _wait_for(lambda: _path_contains(output_path, "alpha\nbeta\ndone\n"))
