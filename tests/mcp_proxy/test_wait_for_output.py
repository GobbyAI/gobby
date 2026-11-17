"""Branch and precedence coverage for the wait_for_output MCP tool."""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.terminals import Terminal
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.runtime import SnapshotMode, SnapshotResult, TerminalRuntime
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

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


class _CaptureRuntime(FakeRuntime):
    """FakeRuntime that delegates snapshot/liveness to a capture adapter."""

    def __init__(self, capture: MagicMock) -> None:
        super().__init__()
        self._capture = capture

    async def snapshot(
        self, terminal: Terminal, lines: int = 50, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        del terminal, lines, mode
        text = await self._capture.capture_pane("agent-test")
        if text is None:
            raise RuntimeError("capture returned none")
        return SnapshotResult(
            text=text,
            truncated=False,
            dropped_bytes=0,
            total_bytes=len(text.encode("utf-8")),
        )

    async def is_live(self, terminal: Terminal) -> bool:
        del terminal
        has_session = getattr(self._capture, "has_session", None)
        if has_session is None:
            return True
        return bool(await has_session("agent-test"))


async def _invoke(run: Any | None, capture: MagicMock, **kwargs: Any) -> dict[str, Any]:
    runner = _runner(run)
    store = MemoryTerminalStore()
    runtime = _CaptureRuntime(capture)
    if run is not None and run.terminal_id:
        terminal = make_memory_terminal(terminal_id=run.terminal_id)
        store.rows[terminal.id] = terminal
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    runner.terminal_manager = store
    runner.terminal_runtime_registry = registry
    tools = create_agents_registry(runner)
    wait_for_output = tools._tools["wait_for_output"].func
    return await wait_for_output("run-1", **kwargs)


@pytest.mark.asyncio
async def test_wait_for_output_requires_managed_runtime_registry() -> None:
    run = SimpleNamespace(id="run-1", terminal_id="terminal-1", status="running")
    runner = _runner(run)
    runner.terminal_runtime_registry = None
    wait_for_output = create_agents_registry(runner)._tools["wait_for_output"].func

    result = await wait_for_output("run-1", pattern="READY")

    assert result["success"] is False
    assert result["error"] == "runtime_unavailable"


@pytest.mark.asyncio
async def test_wait_for_output_returns_bounded_matching_excerpt() -> None:
    match_capture = MagicMock()
    pane_output = f"{'a' * 3_000}\nREADY: port 60887\n{'b' * 3_000}"
    match_capture.capture_pane = AsyncMock(return_value=pane_output)
    matched = await _invoke(
        _run(),
        match_capture,
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
    timeout_capture = MagicMock()
    timeout_capture.capture_pane = AsyncMock(return_value="still working")
    timed_out = await _invoke(
        _run(),
        timeout_capture,
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
    terminal_capture = MagicMock()
    terminal_capture.capture_pane = AsyncMock(return_value="finished cleanly")
    terminal = await _invoke(
        _run(status="success"),
        terminal_capture,
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
    lost_capture = MagicMock()
    lost_capture.capture_pane = AsyncMock(return_value=None)
    lost_capture.has_session = AsyncMock(return_value=False)
    pane_lost = await _invoke(
        _run(),
        lost_capture,
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
    failing_capture = MagicMock()
    failing_capture.capture_pane = AsyncMock(side_effect=RuntimeError("capture failed"))
    failing_capture.has_session = AsyncMock(return_value=True)
    capture_failed = await _invoke(
        _run(),
        failing_capture,
        pattern="READY",
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert capture_failed["error"] == "capture_failed"
    assert failing_capture.capture_pane.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_output_returns_pattern_timeout() -> None:
    pathological_capture = MagicMock()
    pathological_capture.capture_pane = AsyncMock(return_value="a" * 10_000 + "!")
    pattern_timeout = await _invoke(
        _run(),
        pathological_capture,
        pattern=r"(a+)+$",
        timeout_seconds=1,
    )
    assert pattern_timeout["error"] == "pattern_timeout"


@pytest.mark.asyncio
async def test_wait_for_output_match_precedes_terminal_status() -> None:
    collision_capture = MagicMock()
    collision_capture.capture_pane = AsyncMock(return_value="READY")
    match_beats_terminal = await _invoke(
        _run(status="error"),
        collision_capture,
        pattern="READY",
        timeout_seconds=0,
    )
    assert match_beats_terminal["matched"] is True


@pytest.mark.asyncio
async def test_wait_for_output_capture_failure_precedes_deadline() -> None:
    deadline_collision_capture = MagicMock()
    deadline_collision_capture.capture_pane = AsyncMock(side_effect=TimeoutError)
    deadline_collision_capture.has_session = AsyncMock(return_value=True)
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
            deadline_collision_capture,
            pattern="READY",
            timeout_seconds=1,
            poll_interval_seconds=0.1,
        )
    assert capture_beats_deadline["error"] == "capture_failed"
    assert deadline_collision_capture.capture_pane.await_count == 3


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

    cancelling_capture = MagicMock()
    cancelling_capture.capture_pane = AsyncMock(side_effect=blocking_capture)
    cancelling_capture.has_session = AsyncMock(return_value=True)
    runner = _runner(_run())
    store = MemoryTerminalStore()
    terminal = make_memory_terminal(terminal_id="agent-test")
    store.rows[terminal.id] = terminal
    runtime = _CaptureRuntime(cancelling_capture)
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    runner.terminal_manager = store
    runner.terminal_runtime_registry = registry
    tools = create_agents_registry(runner)
    wait_for_output = tools._tools["wait_for_output"].func
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
