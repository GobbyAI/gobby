"""Integration tests for real tmux session semantics."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.tmux.output_reader import TmuxOutputReader
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
