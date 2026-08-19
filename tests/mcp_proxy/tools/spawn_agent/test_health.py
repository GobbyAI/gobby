"""Spawn-agent health check tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionInfo
from gobby.config.tmux import TmuxConfig
from gobby.mcp_proxy.tools.spawn_agent._health import (
    _bounded_redacted_pane_output,
    _check_tmux_session_alive,
    _deferred_tmux_health_check,
    cancel_health_checks,
    schedule_tmux_health_check,
)
from tests.completion_delivery_helpers import record_removals

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_check_tmux_session_alive_uses_configured_manager() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=123))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive(
            "sess",
            socket_name="custom",
            socket_path="/tmp/tmux-1000/custom",
        )

    assert result == (True, None)
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "custom"
    assert config.socket_path == "/tmp/tmux-1000/custom"
    manager.get_session.assert_awaited_once_with("sess")


@pytest.mark.asyncio
async def test_check_tmux_session_alive_rejects_dead_pane() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(
        return_value=TmuxSessionInfo(name="sess", pane_pid=123, pane_dead=True)
    )
    manager.capture_pane = AsyncMock(return_value="/bin/bash: claude: command not found\n")

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result == (False, "/bin/bash: claude: command not found")
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "gobby"
    assert config.socket_path is None
    manager.is_available.assert_called_once_with()
    manager.get_session.assert_awaited_once_with("sess")
    manager.capture_pane.assert_awaited_once_with("sess", lines=50)


@pytest.mark.asyncio
async def test_check_tmux_session_alive_rejects_missing_pane_pid() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=None))
    manager.capture_pane = AsyncMock(return_value="x" * 5000)

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result == (False, "x" * 5000)
    assert result[1] is not None
    assert len(result[1]) > 4096
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "gobby"
    assert config.socket_path is None
    manager.is_available.assert_called_once_with()
    manager.get_session.assert_awaited_once_with("sess")
    manager.capture_pane.assert_awaited_once_with("sess", lines=50)


@pytest.mark.asyncio
async def test_check_tmux_session_alive_keeps_confirmed_death_when_capture_fails() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(
        return_value=TmuxSessionInfo(name="sess", pane_pid=123, pane_dead=True)
    )
    manager.capture_pane = AsyncMock(side_effect=OSError("capture failed"))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ),
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess")

    assert result[0] is False
    assert result[1] is None
    manager.capture_pane.assert_awaited_once_with("sess", lines=50)


@pytest.mark.asyncio
async def test_check_tmux_session_alive_propagates_unexpected_capture_failure() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(
        return_value=TmuxSessionInfo(name="sess", pane_pid=123, pane_dead=True)
    )
    manager.capture_pane = AsyncMock(side_effect=RuntimeError("capture failed"))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ),
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
        pytest.raises(RuntimeError, match="capture failed"),
    ):
        await _check_tmux_session_alive("sess")

    assert manager.get_session.await_args_list == [call("sess")]
    assert manager.capture_pane.await_args_list == [call("sess", lines=50)]


@pytest.mark.asyncio
async def test_check_tmux_session_alive_bounds_capture_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(
        return_value=TmuxSessionInfo(name="sess", pane_pid=123, pane_dead=True)
    )

    async def capture_forever(*_args: object, **_kwargs: object) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    manager.capture_pane = AsyncMock(side_effect=capture_forever)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._health._TMUX_HEALTH_CHECK_TIMEOUT_SECONDS",
        0.001,
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ),
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess")

    assert result == (False, None)
    assert manager.get_session.await_args_list == [call("sess")]
    assert manager.capture_pane.await_args_list == [call("sess", lines=50)]


def test_bounded_redacted_pane_output_redacts_secret_and_preserves_tail_bound() -> None:
    output = f"{'x' * 2048}\nsk-ABCDEFGHIJKLMNOPQRSTUV"

    result = _bounded_redacted_pane_output(output)

    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in result
    assert "sk-<redacted>" in result
    assert result.startswith("[truncated]\n")
    assert len(result) <= 1024


@pytest.mark.asyncio
async def test_deferred_health_check_does_not_fail_terminal_run() -> None:
    runner = MagicMock()
    terminal_run = SimpleNamespace(status="success")
    runner.run_storage.get.return_value = terminal_run
    runner.run_storage.fail.side_effect = lambda *args, **kwargs: setattr(
        terminal_run, "status", "error"
    )

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            tmux_session_name="tmux-run",
            socket_name=None,
            socket_path=None,
            delay=0,
        )

    assert terminal_run.status == "success"
    runner.run_storage.fail.assert_not_called()


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

    runner = SimpleNamespace(
        run_storage=SimpleNamespace(
            get=get_run,
            fail=fail_run,
        )
    )

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
        new_callable=AsyncMock,
        return_value=(False, pane_output),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            tmux_session_name="tmux-run",
            socket_name=None,
            socket_path=None,
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
    runner = SimpleNamespace(run_storage=run_storage)

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
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
            tmux_session_name="tmux-run",
            socket_name=None,
            socket_path=None,
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
        tmux_session_name="session-1",
        socket_name=None,
        socket_path=None,
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
            tmux_session_name="session-1",
            socket_name=None,
            socket_path=None,
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
        runner = SimpleNamespace(run_storage=run_storage)
        return SimpleNamespace(wake=wake, registry=registry, runner=runner)

    @pytest.mark.asyncio
    async def test_acknowledged_delivery_wakes_and_removes_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(ism_persisted=True)
        removals = record_removals(monkeypatch)
        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
            new_callable=AsyncMock,
            return_value=(False, None),
        ):
            await _deferred_tmux_health_check(
                harness.runner,
                "run-123",
                "tmux-run-123",
                None,
                None,
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
            "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
            new_callable=AsyncMock,
            return_value=(False, None),
        ):
            await _deferred_tmux_health_check(
                harness.runner,
                "run-123",
                "tmux-run-123",
                None,
                None,
                delay=0,
                completion_registry=harness.registry,
            )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert removals == []
        assert harness.registry.is_registered("run-123") is False
