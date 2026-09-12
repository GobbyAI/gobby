"""Spawn-agent health check tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent._health import (
    _bounded_redacted_pane_output,
    _deferred_tmux_health_check,
    _terminal_is_live,
    cancel_health_checks,
    schedule_tmux_health_check,
)
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import TerminalRuntimeRegistry
from tests.completion_delivery_helpers import record_removals

pytestmark = pytest.mark.unit


def _runner_with_terminal(run_storage: object) -> SimpleNamespace:
    terminal_manager = MagicMock()
    terminal_manager.get.return_value = cast(
        Terminal,
        SimpleNamespace(id="terminal-1", backend="tmux"),
    )
    return SimpleNamespace(
        run_storage=run_storage,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=MagicMock(spec=TerminalRuntimeRegistry),
    )


@pytest.mark.asyncio
async def test_terminal_is_live_uses_registered_runtime() -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="tmux"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=True)
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime

    result = await _terminal_is_live(row, registry)

    assert result == (True, None)
    registry.resolve.assert_called_once_with("tmux")
    runtime.is_live.assert_awaited_once_with(row)


@pytest.mark.asyncio
async def test_terminal_is_live_reports_dead_runtime_output() -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="tmux"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=False)
    runtime.snapshot = AsyncMock(
        return_value=SimpleNamespace(text="/bin/bash: claude: command not found\n")
    )
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime

    result = await _terminal_is_live(row, registry)

    assert result == (False, _bounded_redacted_pane_output("/bin/bash: claude: command not found"))
    runtime.is_live.assert_awaited_once_with(row)
    runtime.snapshot.assert_awaited_once_with(row, lines=50)


@pytest.mark.asyncio
async def test_terminal_is_live_bounds_dead_runtime_output() -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="tmux"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=False)
    runtime.snapshot = AsyncMock(return_value=SimpleNamespace(text="x" * 5000))
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime

    result = await _terminal_is_live(row, registry)

    assert result == (False, _bounded_redacted_pane_output("x" * 5000))
    assert result[1] is not None
    assert len(result[1]) <= 1024
    assert result[1].startswith("[truncated]\n")
    runtime.snapshot.assert_awaited_once_with(row, lines=50)


@pytest.mark.asyncio
async def test_terminal_is_live_keeps_confirmed_death_when_snapshot_fails() -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="native"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=False)
    runtime.snapshot = AsyncMock(side_effect=OSError("capture failed"))
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime

    result = await _terminal_is_live(row, registry)

    assert result[0] is False
    assert result[1] is None
    runtime.snapshot.assert_awaited_once_with(row, lines=50)


@pytest.mark.asyncio
async def test_terminal_is_live_propagates_unexpected_snapshot_failure() -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="native"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=False)
    runtime.snapshot = AsyncMock(side_effect=RuntimeError("capture failed"))
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime

    with pytest.raises(RuntimeError, match="capture failed"):
        await _terminal_is_live(row, registry)

    assert runtime.is_live.await_args_list == [call(row)]
    assert runtime.snapshot.await_args_list == [call(row, lines=50)]


@pytest.mark.asyncio
async def test_terminal_is_live_bounds_snapshot_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = cast(Terminal, SimpleNamespace(id="terminal-1", backend="native"))
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=False)

    async def capture_forever(*_args: object, **_kwargs: object) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime.snapshot = AsyncMock(side_effect=capture_forever)
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = runtime
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._health._TMUX_HEALTH_CHECK_TIMEOUT_SECONDS",
        0.001,
    )

    result = await _terminal_is_live(row, registry)

    assert result == (False, None)
    assert runtime.is_live.await_args_list == [call(row)]
    assert runtime.snapshot.await_args_list == [call(row, lines=50)]


def test_bounded_redacted_pane_output_redacts_secret_and_preserves_tail_bound() -> None:
    output = f"{'x' * 2048}\nsk-ABCDEFGHIJKLMNOPQRSTUV"

    result = _bounded_redacted_pane_output(output)

    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in result
    assert "sk-<redacted>" in result
    assert result.startswith("[truncated]\n")
    assert len(result) <= 1024


@pytest.mark.asyncio
async def test_deferred_health_check_does_not_fail_terminal_run() -> None:
    run_storage = MagicMock()
    runner = _runner_with_terminal(run_storage)
    terminal_run = SimpleNamespace(status="success")
    run_storage.get.return_value = terminal_run
    run_storage.fail.side_effect = lambda *args, **kwargs: setattr(terminal_run, "status", "error")

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            terminal_id="terminal-1",
            delay=0,
        )

    assert terminal_run.status == "success"
    run_storage.fail.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pane_output", "expected_error"),
    [
        (
            "/bin/bash: claude: command not found",
            "Agent process exited immediately after spawn\n"
            "Pane output:\n/bin/bash: claude: command not found",
        ),
        (None, "Agent process exited immediately after spawn"),
    ],
)
async def test_deferred_health_failure_reports_available_pane_output(
    pane_output: str | None,
    expected_error: str,
) -> None:
    recorded_errors: list[str] = []

    def get_run(_run_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="running")

    def fail_run(_run_id: str, error: str) -> None:
        recorded_errors.append(error)

    runner = _runner_with_terminal(
        SimpleNamespace(
            get=get_run,
            fail=fail_run,
        )
    )

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
        new_callable=AsyncMock,
        return_value=(False, pane_output),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            terminal_id="terminal-1",
            delay=0,
        )

    assert recorded_errors == [expected_error]


@pytest.mark.asyncio
async def test_deferred_health_delivery_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_storage = MagicMock()
    run_storage.get.return_value = SimpleNamespace(status="running")
    run_storage.fail.return_value = SimpleNamespace(status="error")
    runner = _runner_with_terminal(run_storage)

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
            new_callable=AsyncMock,
            return_value=(False, None),
        ),
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("delivery failed"),
        ),
        caplog.at_level("WARNING"),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            terminal_id="terminal-1",
            delay=0,
        )

    assert "Failed to deliver terminal agent_run run-123 after health check" in caplog.text


@pytest.mark.asyncio
async def test_scheduled_health_check_does_not_create_a_sleeping_task() -> None:
    runner = SimpleNamespace(run_storage=MagicMock())
    existing_tasks = asyncio.all_tasks()

    handle = schedule_tmux_health_check(
        runner=runner,
        run_id="run-1",
        terminal_id="session-1",
        delay=60,
    )

    assert asyncio.all_tasks() == existing_tasks
    handle.cancel()


@pytest.mark.asyncio
async def test_cancel_health_checks_cancels_pending_timer_before_callback() -> None:
    runner = SimpleNamespace(run_storage=MagicMock())
    with patch("gobby.mcp_proxy.tools.spawn_agent._health._start_tmux_health_check") as start:
        handle = schedule_tmux_health_check(
            runner=runner,
            run_id="run-1",
            terminal_id="session-1",
            delay=60,
        )
        cancel_health_checks()

    assert handle.cancelled()
    start.assert_not_called()


class _RecordingWake:
    """Wake callback recording deliveries with a configurable outcome."""

    def __init__(self, ism_persisted: bool) -> None:
        self._ism_persisted = ism_persisted
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def __call__(
        self, session_id: str, message: str, result: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((session_id, message, result))
        return {"ism_persisted": self._ism_persisted}


class TestDeferredHealthFailureWakesWaiter:
    """Plan 1.4.10: a deferred-health failure wakes the pre-registered waiter."""

    def _harness(self, *, ism_persisted: bool) -> SimpleNamespace:
        from contextlib import nullcontext

        from gobby.events import CompletionEventRegistry

        wake = _RecordingWake(ism_persisted)
        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register("run-123", ["waiter-sess"])

        run_storage = MagicMock()
        run_storage.fail.return_value = SimpleNamespace(
            id="run-123", status="error", error="Agent process exited immediately after spawn"
        )
        run_storage.db.bounded_transaction.return_value = nullcontext()
        # deliver_existing_terminal_run re-reads the run after fail(); return the
        # terminal row on that second read.
        run_storage.get.side_effect = [
            SimpleNamespace(status="running"),
            SimpleNamespace(
                id="run-123",
                status="error",
                error="Agent process exited immediately after spawn",
            ),
        ]
        runner = _runner_with_terminal(run_storage)
        return SimpleNamespace(wake=wake, registry=registry, runner=runner)

    @pytest.mark.asyncio
    async def test_acknowledged_delivery_wakes_and_removes_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(ism_persisted=True)
        removals = record_removals(monkeypatch)
        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
            new_callable=AsyncMock,
            return_value=(False, None),
        ):
            await _deferred_tmux_health_check(
                harness.runner,
                "run-123",
                "terminal-1",
                delay=0,
                completion_registry=harness.registry,
            )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert harness.wake.calls[0][2]["run_id"] == "run-123"
        assert removals == [("run-123", ["waiter-sess"])]
        assert harness.registry.is_registered("run-123") is False

    @pytest.mark.asyncio
    async def test_failed_delivery_retains_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = self._harness(ism_persisted=False)
        removals = record_removals(monkeypatch)
        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
            new_callable=AsyncMock,
            return_value=(False, None),
        ):
            await _deferred_tmux_health_check(
                harness.runner,
                "run-123",
                "terminal-1",
                delay=0,
                completion_registry=harness.registry,
            )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert removals == []
        assert harness.registry.is_registered("run-123") is False
