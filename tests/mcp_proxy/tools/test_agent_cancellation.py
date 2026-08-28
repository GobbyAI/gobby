"""Tests for shared agent cancellation helpers."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools import agent_cancellation
from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminalize_cancelled_agent_run,
    terminalize_killed_agent_run,
)
from tests.completion_delivery_helpers import DeliveryRegistry, record_removals

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_srt_runner_reap(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    reaper = AsyncMock(return_value=0)
    monkeypatch.setattr(
        agent_cancellation,
        "reap_srt_runner_process_tree",
        reaper,
    )
    return reaper


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_uses_lifecycle_monitor() -> None:
    runner = MagicMock()
    lifecycle_monitor = MagicMock()
    lifecycle_monitor.terminalize_cancelled_run = AsyncMock(return_value=True)
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()

    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=lifecycle_monitor,
        completion_registry=completion_registry,
        task_manager=MagicMock(),
    )

    assert transitioned is True
    assert isinstance(transitioned, bool)
    lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
        "run-123",
        terminal_reason="user_cancelled",
    )
    runner.cancel_run.assert_not_called()
    completion_registry.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_fallback_recovers_task_claim(
    _stub_srt_runner_reap: AsyncMock,
) -> None:
    runner = MagicMock()
    runner.cancel_run.return_value = True
    runner.run_storage = MagicMock()
    cancelled_run = MagicMock()
    cancelled_run.id = "run-123"
    cancelled_run.status = "cancelled"
    cancelled_run.error = None
    runner.run_storage.db.bounded_transaction.return_value = nullcontext()
    runner.run_storage.get.return_value = cancelled_run
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock(return_value={})
    task_manager = MagicMock()

    with patch("gobby.mcp_proxy.tools.agent_cancellation.TaskRecoveryHandler") as recovery_cls:
        recovery = recovery_cls.return_value
        recovery.recover_task_from_terminal_agent = AsyncMock()

        transitioned = await terminalize_cancelled_agent_run(
            runner=runner,
            run_id="run-123",
            terminal_reason="user_cancelled",
            lifecycle_monitor=None,
            completion_registry=completion_registry,
            task_manager=task_manager,
            message="Agent run-123 cancelled",
        )

    assert transitioned is True
    assert isinstance(transitioned, bool)
    runner.cancel_run.assert_called_once_with("run-123")
    assert runner.run_storage.get.call_args_list == [
        (("run-123",), {}),
        (("run-123",), {}),
    ]
    recovery_cls.assert_called_once_with(task_manager, runner.run_storage, ANY)
    recovery.recover_task_from_terminal_agent.assert_awaited_once_with(
        cancelled_run,
        outcome="cancelled",
    )
    _stub_srt_runner_reap.assert_awaited_once_with("run-123")
    completion_registry.notify.assert_awaited_once_with(
        "run-123",
        result={
            "status": "cancelled",
            "run_id": "run-123",
            "error": None,
        },
        message="Agent run-123 cancelled",
    )


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_fallback_skips_recovery_when_not_transitioned() -> (
    None
):
    runner = MagicMock()
    runner.cancel_run.return_value = False
    runner.run_storage = MagicMock()
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()

    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=None,
        completion_registry=completion_registry,
        task_manager=MagicMock(),
    )

    assert transitioned is False
    assert isinstance(transitioned, bool)
    runner.cancel_run.assert_called_once_with("run-123")
    runner.run_storage.get.assert_not_called()
    completion_registry.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminalize_killed_agent_run_error_recovers_claim_and_notifies() -> None:
    notifications: list[tuple[str, dict[str, str], str]] = []

    class RecordingCompletionRegistry:
        async def notify(self, run_id: str, result: dict[str, str], *, message: str) -> None:
            notifications.append((run_id, result, message))

        def cleanup(self, _run_id: str) -> None:
            pass

    runner = MagicMock()
    failed_run = MagicMock()
    failed_run.id = "run-123"
    failed_run.status = "error"
    failed_run.error = "Agent self-reported error"
    runner.run_storage.db.bounded_transaction.return_value = nullcontext()
    runner.run_storage.fail.return_value = failed_run
    runner.run_storage.get.return_value = failed_run
    completion_registry = RecordingCompletionRegistry()
    task_manager = MagicMock()

    async def run_offload(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch("gobby.mcp_proxy.tools.agent_cancellation.TaskRecoveryHandler") as recovery_cls,
        patch(
            "gobby.agents.terminal_delivery.run_terminal_delivery_offload",
            new_callable=AsyncMock,
            side_effect=run_offload,
        ) as mock_offload,
    ):
        recovery = recovery_cls.return_value
        recovery.recover_task_from_terminal_agent = AsyncMock()

        result = await terminalize_killed_agent_run(
            runner=runner,
            run_id="run-123",
            effective_status="error",
            lifecycle_monitor=None,
            completion_registry=completion_registry,
            task_manager=task_manager,
        )

    assert result == {"status": "error", "workflow_stopped": True}
    mock_offload.assert_any_await(
        runner.run_storage.fail,
        "run-123",
        error="Agent self-reported error",
    )
    runner.run_storage.fail.assert_called_once_with("run-123", error="Agent self-reported error")
    recovery.recover_task_from_terminal_agent.assert_awaited_once_with(
        failed_run,
        outcome="failed",
    )
    assert notifications == [
        (
            "run-123",
            {
                "status": "error",
                "error": "Agent self-reported error",
                "run_id": "run-123",
            },
            "Agent run-123 failed",
        )
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_happy_path_call_order() -> None:
    call_order: list[str] = []
    run = MagicMock()
    run.id = "run-123"
    run.status = "pending"
    run.terminal_id = "tmux-run-123"
    run.child_session_id = "child-session-123"

    runner = MagicMock()
    runner.get_run.return_value = run
    terminal_services = object()
    runner.terminal_services = terminal_services
    agent_run_manager = MagicMock()
    agent_run_manager.db.bounded_transaction.return_value = nullcontext()

    async def kill_side_effect(*_args: object, **_kwargs: object) -> dict[str, bool]:
        call_order.append("kill")
        return {"success": True}

    async def terminalize_side_effect(**_kwargs: object) -> bool:
        call_order.append("terminalize")
        return True

    async def cleanup_side_effect(**_kwargs: object) -> None:
        call_order.append("cleanup")

    kill_agent_process = AsyncMock(side_effect=kill_side_effect)
    cleanup_terminal_artifacts = AsyncMock(side_effect=cleanup_side_effect)

    with patch(
        "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
        new_callable=AsyncMock,
        side_effect=terminalize_side_effect,
    ) as terminalize:
        result = await stop_agent_run(
            run_id="run-123",
            runner=runner,
            agent_run_manager=agent_run_manager,
            db=None,
            lifecycle_monitor=None,
            completion_registry=None,
            task_manager=None,
            session_manager=None,
            kill_agent_process=kill_agent_process,
            cleanup_terminal_artifacts=cleanup_terminal_artifacts,
        )

    assert result == {
        "success": True,
        "message": "Agent run run-123 stopped",
        "run_id": "run-123",
        "status": "cancelled",
        "terminal_reason": "user_cancelled",
        "agent_step_instances_deleted": None,
    }
    assert call_order == ["kill", "terminalize", "cleanup"]
    kill_agent_process.assert_awaited_once_with(
        run,
        agent_run_manager.db,
        signal_name="TERM",
        close_terminal=True,
        terminal_services=terminal_services,
    )
    terminalize.assert_awaited_once_with(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=None,
        completion_registry=None,
        task_manager=None,
        message="Agent run-123 cancelled",
    )
    cleanup_terminal_artifacts.assert_awaited_once_with(
        run_id="run-123",
        db=agent_run_manager.db,
        terminal_id="tmux-run-123",
        agent_session_id="child-session-123",
        debug=False,
        session_manager=None,
        result={"success": True, "terminal_reason": "user_cancelled"},
    )


class TestStopAgentRunCapturePreemptedDelivery:
    """Plan 1.4.7: capture-committed transitions reach the waiter through the helper."""

    def _harness(
        self,
        *,
        delivery: dict[str, bool] | None,
        db_status: str = "cancelled",
    ) -> SimpleNamespace:
        live_run = MagicMock()
        live_run.id = "run-123"
        live_run.status = "running"
        live_run.terminal_id = "tmux-run-123"
        live_run.child_session_id = "child-session-123"

        runner = MagicMock()
        runner.get_run.return_value = live_run
        agent_run_manager = MagicMock()
        agent_run_manager.db.bounded_transaction.return_value = nullcontext()
        agent_run_manager.get.return_value = SimpleNamespace(
            id="run-123", status=db_status, error=None
        )
        registry = DeliveryRegistry(delivery)
        return SimpleNamespace(
            run=live_run,
            runner=runner,
            agent_run_manager=agent_run_manager,
            registry=registry,
        )

    async def _stop(self, harness: SimpleNamespace, kill_agent_process: Any) -> dict[str, Any]:
        return await stop_agent_run(
            run_id="run-123",
            runner=harness.runner,
            agent_run_manager=harness.agent_run_manager,
            db=None,
            lifecycle_monitor=None,
            completion_registry=harness.registry,
            task_manager=None,
            session_manager=None,
            kill_agent_process=kill_agent_process,
            cleanup_terminal_artifacts=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_capture_preempted_stop_delivers_committed_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(delivery={"waiter-sess": True})
        removals = record_removals(monkeypatch)
        with patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await self._stop(harness, AsyncMock(return_value={"success": True}))

        assert result["success"] is True
        assert len(harness.registry.notify_calls) == 1
        _, payload, _ = harness.registry.notify_calls[0]
        assert payload is not None and payload["run_id"] == "run-123"
        assert payload["status"] == "cancelled"
        assert removals == [("run-123", ["waiter-sess"])]
        assert harness.registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    async def test_failed_delivery_retains_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = self._harness(delivery={"waiter-sess": False})
        removals = record_removals(monkeypatch)
        with patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await self._stop(harness, AsyncMock(return_value={"success": True}))

        assert result["success"] is True
        assert removals == []
        assert harness.registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    async def test_kill_failure_after_terminal_close_still_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(delivery={"waiter-sess": True})
        removals = record_removals(monkeypatch)
        kill_result = {
            "success": False,
            "error": "Terminal closed but no target PID was found to verify process death",
            "error_code": "kill_verification_failed",
        }
        result = await self._stop(harness, AsyncMock(return_value=kill_result))

        assert result == kill_result
        assert len(harness.registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert harness.registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    async def test_kill_raises_after_commit_delivers_before_propagation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(delivery={"waiter-sess": True})
        removals = record_removals(monkeypatch)

        async def _commit_then_raise(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("kill exploded after committing")

        with pytest.raises(RuntimeError, match="kill exploded"):
            await self._stop(harness, AsyncMock(side_effect=_commit_then_raise))

        assert len(harness.registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert harness.registry.cleanup_calls == ["run-123"]

    @pytest.mark.asyncio
    async def test_commits_then_cancel_settles_before_cancelled_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness(delivery={"waiter-sess": True})
        removals = record_removals(monkeypatch)
        started = asyncio.Event()
        release = asyncio.Event()

        async def _gated_kill(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            started.set()
            await release.wait()
            return {"success": True}

        with patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            return_value=False,
        ):
            task = asyncio.ensure_future(self._stop(harness, AsyncMock(side_effect=_gated_kill)))
            await started.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(harness.registry.notify_calls) == 1
        assert removals == [("run-123", ["waiter-sess"])]
        assert harness.registry.cleanup_calls == ["run-123"]


@pytest.mark.asyncio
async def test_terminalize_killed_error_shape_delivers_when_fail_reports_none() -> None:
    """Plan 1.4.7: the error request shape routes no-transition runs to delivery."""
    runner = MagicMock()
    runner.run_storage.fail.return_value = None
    runner.get_run.return_value = SimpleNamespace(status="cancelled")

    with patch(
        "gobby.agents.terminal_delivery.deliver_existing_terminal_run",
        new_callable=AsyncMock,
    ) as deliver:
        result = await terminalize_killed_agent_run(
            runner=runner,
            run_id="run-123",
            effective_status="error",
            lifecycle_monitor=None,
            completion_registry=None,
            task_manager=None,
        )

    assert result == {"status": "error", "workflow_stopped": True}
    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["run_id"] == "run-123"
