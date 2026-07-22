"""Branch and precedence coverage for the wait_for_output MCP tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry

pytestmark = pytest.mark.unit


def _run(*, status: str = "running", tmux_session_name: str | None = "agent-test") -> Any:
    return SimpleNamespace(
        id="run-1",
        status=status,
        tmux_session_name=tmux_session_name,
    )


def _runner(run: Any | None) -> MagicMock:
    runner = MagicMock()
    runner.run_storage = MagicMock()
    runner.get_run.return_value = run
    return runner


async def _invoke(run: Any | None, tmux: MagicMock, **kwargs: Any) -> dict[str, Any]:
    runner = _runner(run)
    with patch(
        "gobby.mcp_proxy.tools.agents_query_tools.get_tmux_session_manager",
        return_value=tmux,
    ):
        registry = create_agents_registry(runner)
        wait_for_output = registry._tools["wait_for_output"].func
        return await wait_for_output("run-1", **kwargs)


@pytest.mark.asyncio
async def test_wait_branches() -> None:
    match_tmux = MagicMock()
    match_tmux.capture_pane = AsyncMock(return_value="booting\nREADY: port 60887\n")
    matched = await _invoke(
        _run(),
        match_tmux,
        pattern=r"READY: port \d+",
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    assert matched == {
        "success": True,
        "matched": True,
        "excerpt": "booting\nREADY: port 60887\n",
    }

    timeout_tmux = MagicMock()
    timeout_tmux.capture_pane = AsyncMock(return_value="still working")
    timed_out = await _invoke(
        _run(),
        timeout_tmux,
        pattern="READY",
        timeout_seconds=0,
        poll_interval_seconds=0.01,
    )
    assert timed_out == {
        "success": True,
        "matched": False,
        "reason": "timeout",
        "status": "running",
    }

    terminal_tmux = MagicMock()
    terminal_tmux.capture_pane = AsyncMock(return_value="finished cleanly")
    terminal = await _invoke(
        _run(status="success"),
        terminal_tmux,
        pattern="READY",
        timeout_seconds=1,
    )
    assert terminal == {
        "success": True,
        "matched": False,
        "reason": "terminal",
        "status": "success",
    }

    unknown = await _invoke(
        None,
        MagicMock(),
        pattern="(",
        timeout_seconds=float("nan"),
    )
    assert unknown["error"] == "invalid_run"

    no_terminal = await _invoke(
        _run(tmux_session_name=None),
        MagicMock(),
        pattern="(",
        timeout_seconds=float("nan"),
    )
    assert no_terminal["error"] == "no_terminal"

    invalid_pattern = await _invoke(
        _run(),
        MagicMock(),
        pattern="(",
        timeout_seconds=float("nan"),
    )
    assert invalid_pattern["error"] == "invalid_pattern"

    invalid_timeout = await _invoke(
        _run(),
        MagicMock(),
        pattern="READY",
        timeout_seconds=float("nan"),
    )
    assert invalid_timeout["error"] == "invalid_argument"
    invalid_interval = await _invoke(
        _run(),
        MagicMock(),
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=float("inf"),
    )
    assert invalid_interval["error"] == "invalid_argument"

    lost_tmux = MagicMock()
    lost_tmux.capture_pane = AsyncMock(return_value=None)
    lost_tmux.has_session = AsyncMock(return_value=False)
    pane_lost = await _invoke(
        _run(),
        lost_tmux,
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    assert pane_lost == {
        "success": True,
        "matched": False,
        "reason": "pane_lost",
        "status": "running",
    }

    failing_tmux = MagicMock()
    failing_tmux.capture_pane = AsyncMock(side_effect=RuntimeError("capture failed"))
    failing_tmux.has_session = AsyncMock(return_value=True)
    capture_failed = await _invoke(
        _run(),
        failing_tmux,
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    assert capture_failed["error"] == "capture_failed"
    assert failing_tmux.capture_pane.await_count == 3

    pathological_tmux = MagicMock()
    pathological_tmux.capture_pane = AsyncMock(return_value="a" * 100_000 + "!")
    pattern_timeout = await _invoke(
        _run(),
        pathological_tmux,
        pattern=r"(a+)+$",
        timeout_seconds=1,
    )
    assert pattern_timeout["error"] == "pattern_timeout"

    collision_tmux = MagicMock()
    collision_tmux.capture_pane = AsyncMock(return_value="READY")
    match_beats_terminal = await _invoke(
        _run(status="error"),
        collision_tmux,
        pattern="READY",
        timeout_seconds=0,
    )
    assert match_beats_terminal["matched"] is True

    deadline_collision_tmux = MagicMock()
    deadline_collision_tmux.capture_pane = AsyncMock(side_effect=TimeoutError)
    deadline_collision_tmux.has_session = AsyncMock(return_value=True)
    capture_beats_deadline = await _invoke(
        _run(),
        deadline_collision_tmux,
        pattern="READY",
        timeout_seconds=0.015,
        poll_interval_seconds=0.01,
    )
    assert capture_beats_deadline["error"] == "capture_failed"
    assert deadline_collision_tmux.capture_pane.await_count == 3

    capture_started = asyncio.Event()
    capture_finished = asyncio.Event()

    async def blocking_capture(*_args: Any, **_kwargs: Any) -> str:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture_finished.set()

    cancelling_tmux = MagicMock()
    cancelling_tmux.capture_pane = AsyncMock(side_effect=blocking_capture)
    cancelling_tmux.has_session = AsyncMock(return_value=True)
    runner = _runner(_run())
    with patch(
        "gobby.mcp_proxy.tools.agents_query_tools.get_tmux_session_manager",
        return_value=cancelling_tmux,
    ):
        registry = create_agents_registry(runner)
        wait_for_output = registry._tools["wait_for_output"].func
        waiting = asyncio.create_task(wait_for_output("run-1", pattern="READY", timeout_seconds=10))
        await capture_started.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    assert waiting.done()
    assert capture_finished.is_set()
