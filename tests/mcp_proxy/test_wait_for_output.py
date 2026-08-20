"""Branch and precedence coverage for the wait_for_output MCP tool."""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit


def _run(*, status: str = "running", terminal_id: str | None = "agent-test") -> Any:
    return SimpleNamespace(
        id="run-1",
        status=status,
        terminal_id=terminal_id,
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
async def test_wait_for_output_returns_bounded_matching_excerpt() -> None:
    match_tmux = MagicMock()
    pane_output = f"{'a' * 3_000}\nREADY: port 60887\n{'b' * 3_000}"
    match_tmux.capture_pane = AsyncMock(return_value=pane_output)
    matched = await _invoke(
        _run(),
        match_tmux,
        pattern=r"READY: port \d+",
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert matched["success"] is True
    assert matched["matched"] is True
    assert len(matched["excerpt"]) <= 4_096
    assert "output omitted" in matched["excerpt"]


@pytest.mark.asyncio
async def test_wait_for_output_returns_timeout() -> None:
    timeout_tmux = MagicMock()
    timeout_tmux.capture_pane = AsyncMock(return_value="still working")
    timed_out = await _invoke(
        _run(),
        timeout_tmux,
        pattern="READY",
        timeout_seconds=0,
        poll_interval_seconds=0.1,
    )
    assert timed_out == {
        "success": True,
        "matched": False,
        "reason": "timeout",
        "status": "running",
    }


@pytest.mark.asyncio
async def test_wait_for_output_returns_terminal_status() -> None:
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "pattern", "timeout_seconds", "poll_interval_seconds", "error"),
    [
        (None, "(", float("nan"), 2.0, "invalid_run"),
        (_run(terminal_id=None), "(", float("nan"), 2.0, "no_terminal"),
        (_run(), "(", float("nan"), 2.0, "invalid_pattern"),
        (_run(), "READY", float("nan"), 2.0, "invalid_argument"),
        (_run(), "READY", 1.0, float("inf"), "invalid_argument"),
    ],
)
async def test_wait_for_output_validates_payload(
    run: Any | None,
    pattern: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    error: str,
) -> None:
    result = await _invoke(
        run,
        MagicMock(),
        pattern=pattern,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    assert result["error"] == error


@pytest.mark.asyncio
async def test_wait_for_output_returns_pane_lost() -> None:
    lost_tmux = MagicMock()
    lost_tmux.capture_pane = AsyncMock(return_value=None)
    lost_tmux.has_session = AsyncMock(return_value=False)
    pane_lost = await _invoke(
        _run(),
        lost_tmux,
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert pane_lost == {
        "success": True,
        "matched": False,
        "reason": "pane_lost",
        "status": "running",
    }


@pytest.mark.asyncio
async def test_wait_for_output_returns_capture_failed_after_three_attempts() -> None:
    failing_tmux = MagicMock()
    failing_tmux.capture_pane = AsyncMock(side_effect=RuntimeError("capture failed"))
    failing_tmux.has_session = AsyncMock(return_value=True)
    capture_failed = await _invoke(
        _run(),
        failing_tmux,
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert capture_failed["error"] == "capture_failed"
    assert failing_tmux.capture_pane.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_output_returns_pattern_timeout() -> None:
    pathological_tmux = MagicMock()
    pathological_tmux.capture_pane = AsyncMock(return_value="a" * 10_000 + "!")
    pattern_timeout = await _invoke(
        _run(),
        pathological_tmux,
        pattern=r"(a+)+$",
        timeout_seconds=1,
    )
    assert pattern_timeout["error"] == "pattern_timeout"


@pytest.mark.asyncio
async def test_wait_for_output_match_precedes_terminal_status() -> None:
    collision_tmux = MagicMock()
    collision_tmux.capture_pane = AsyncMock(return_value="READY")
    match_beats_terminal = await _invoke(
        _run(status="error"),
        collision_tmux,
        pattern="READY",
        timeout_seconds=0,
    )
    assert match_beats_terminal["matched"] is True


@pytest.mark.asyncio
async def test_wait_for_output_capture_failure_precedes_deadline() -> None:
    deadline_collision_tmux = MagicMock()
    deadline_collision_tmux.capture_pane = AsyncMock(side_effect=TimeoutError)
    deadline_collision_tmux.has_session = AsyncMock(return_value=True)
    clock = MagicMock()
    clock.monotonic.side_effect = [0.0, 0.1, 0.2]
    agents = SimpleNamespace(
        _TERMINAL_AGENT_STATUSES={"success", "error", "cancelled"},
        time=clock,
        asyncio=SimpleNamespace(sleep=AsyncMock()),
    )
    with patch("gobby.mcp_proxy.tools.agents_query_tools.facade", return_value=agents):
        capture_beats_deadline = await _invoke(
            _run(),
            deadline_collision_tmux,
            pattern="READY",
            timeout_seconds=1,
            poll_interval_seconds=0.1,
        )
    assert capture_beats_deadline["error"] == "capture_failed"
    assert deadline_collision_tmux.capture_pane.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_output_cancellation_finishes_capture_cleanup() -> None:
    capture_started = asyncio.Event()
    capture_finished = asyncio.Event()

    async def blocking_capture(*_args: Any, **_kwargs: Any) -> None:
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


def test_wait_for_agent_subscription_critical_region_contains_no_await() -> None:
    from gobby.mcp_proxy.tools import agents_query_tools

    source = Path(agents_query_tools.__file__).read_text()
    region = source.split(
        "# The region from this status re-read through conditional cleanup contains no",
        1,
    )[1].split("# ---- end of no-await critical region ----", 1)[0]
    parsed = ast.parse(textwrap.dedent(region))

    assert not any(isinstance(node, ast.Await) for node in ast.walk(parsed))
    assert "subscribe_agent_completion(" in region
    assert "remove_agent_completion_subscribers(" in region
    assert "ctx.completion_registry.cleanup(" in region
