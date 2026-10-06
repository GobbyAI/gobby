"""Core startup and shutdown stay independent of retained recovery work."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from uvicorn import Server

from gobby import runner_lifecycle_shutdown as shutdown
from gobby import runner_lifecycle_subsystems as lifecycle
from gobby import runner_rollback as rollback
from gobby.agents.readiness import spawn_readiness_blocker
from gobby.hooks import inbox
from gobby.hooks.inbox_lifecycle import stop_hook_inbox_replays
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.runner import GobbyRunner
from gobby.runner_hook_replay import HookReplayBarrierOutcome
from gobby.runner_lifecycle_startup import StartupTracker
from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin import _health as health
from gobby.shutdown_intent import ShutdownIntent

pytestmark = pytest.mark.unit


def _runner(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> SimpleNamespace:
    services = SimpleNamespace(
        startup_ready=False, restart_recovery_ready=False, shutdown_in_progress=False
    )
    runner = SimpleNamespace(
        bootstrap_config=SimpleNamespace(run_mode=lambda: "hub"),
        agent_runner=object(),
        agent_lifecycle_monitor=SimpleNamespace(
            set_reconciliation_callback=Mock(), set_non_task_resume_callback=Mock()
        ),
        http_server=SimpleNamespace(app=app, services=services),
        http_bound_at_ms=1_800_000_000_000,
        wake_dispatcher=SimpleNamespace(
            reconcile_restart_active_sessions=AsyncMock(return_value=())
        ),
        wake_replay_coordinator=SimpleNamespace(open=AsyncMock()),
    )
    for name in (
        "_start_terminal_host",
        "_connect_mcp_servers",
        "_check_embedding_service",
        "_cleanup_metrics_on_startup",
        "_cleanup_stale_expansion_runs_on_startup",
        "_initialize_vector_store",
        "_start_core_services",
        "_start_agent_lifecycle_monitor",
        "_start_cron_scheduler",
        "_recover_pipelines",
        "_start_system_automation_loop",
        "_resume_dead_handoff_dispatches",
        "_run_tracked_start_async",
    ):
        monkeypatch.setattr(lifecycle, name, AsyncMock(return_value=0))
    monkeypatch.setattr(lifecycle, "_repair_code_index_bm25", AsyncMock(return_value=False))
    monkeypatch.setattr(lifecycle, "_schedule_workflow_skill_prewarm", lambda *_: None)
    monkeypatch.setattr(lifecycle, "_run_tracked_start", lambda *_: None)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hook", "session", "wake", "agent"])
async def test_recovery_phase_does_not_hold_core_startup(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    runner = _runner(monkeypatch, FastAPI())
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return (
            HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)
            if phase == "hook"
            else 0
        )

    monkeypatch.setattr(
        lifecycle,
        "_run_agent_hook_replay_barrier",
        AsyncMock(return_value=HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)),
    )
    if phase == "hook":
        monkeypatch.setattr(lifecycle, "_run_agent_hook_replay_barrier", blocked)
    elif phase == "session":
        runner.wake_dispatcher.reconcile_restart_active_sessions = blocked
    elif phase == "wake":
        runner.wake_replay_coordinator.open = blocked
    tracker = StartupTracker()
    async with asyncio.timeout(2):
        await lifecycle.init_subsystems(
            cast(GobbyRunner, runner),
            AsyncMock(),
            tracker,
            reconcile_agent_runs_after_restart=blocked
            if phase == "agent"
            else AsyncMock(return_value=0),
            reap_orphaned_srt_runners=AsyncMock(return_value=0),
            recover_agent_completion_subscribers=AsyncMock(return_value=0),
        )
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        assert tracker.to_dict()["done"] is True
        assert runner.http_server.services.startup_ready is True
        assert (
            spawn_readiness_blocker(runner.http_server.services)
            == "daemon_restart_recovery_pending"
        )
        release.set()
        await runner._startup_recovery_task
        assert spawn_readiness_blocker(runner.http_server.services) is None
    finally:
        release.set()
        runner._startup_recovery_task.cancel()
        await asyncio.gather(runner._startup_recovery_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_thousand_retained_hooks_do_not_hold_health_or_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {},
            "headers": {},
        }
    )
    for index in range(2_000):
        (tmp_path / f"{index:04}.json").write_text(payload, encoding="utf-8")
    app = FastAPI()
    runner = _runner(monkeypatch, app)
    runner.http_server.bootstrap_config = runner.bootstrap_config
    runner.http_server.get_runner = lambda: runner
    diagnostic = SimpleNamespace(is_degraded=False, to_dict=lambda: {})
    monkeypatch.setattr(health, "read_ghook_runtime_diagnostic", lambda: diagnostic)
    app.include_router(health.create_health_router(cast(HTTPServer, runner.http_server)))
    monkeypatch.setattr(inbox, "get_hook_inbox_dir", lambda: tmp_path)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    entered = asyncio.Event()

    async def blocked_post(*args: Any, **kwargs: Any) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    monkeypatch.setattr(inbox, "_post_envelope", blocked_post)
    tracker = StartupTracker()
    async with asyncio.timeout(2):
        await lifecycle.init_subsystems(
            cast(GobbyRunner, runner),
            AsyncMock(),
            tracker,
            reconcile_agent_runs_after_restart=AsyncMock(return_value=0),
            reap_orphaned_srt_runners=AsyncMock(return_value=0),
            recover_agent_completion_subscribers=AsyncMock(return_value=0),
        )
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["restart_recovery_pending"] is True
        assert tracker.to_dict()["done"] is True
        assert len(list(tmp_path.glob("*.json"))) == 2_000
    finally:
        runner._startup_recovery_task.cancel()
        await asyncio.gather(runner._startup_recovery_task, return_exceptions=True)
        await stop_hook_inbox_replays(app)
    assert not app.state.hook_inbox_replays
    assert len(list(tmp_path.glob("*.json"))) == 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [ShutdownIntent.STOP, ShutdownIntent.RESTART])
@pytest.mark.parametrize("phase", ["hook", "session", "wake", "agent"])
async def test_shutdown_during_recovery_cancels_before_storage_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, intent: ShutdownIntent, phase: str
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    runner = _runner(monkeypatch, FastAPI())
    runner._shutdown_intent = intent
    runner.wake_replay_coordinator.close = AsyncMock()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    def assert_cancelled() -> None:
        assert cancelled.is_set()
        assert runner.http_server.services.shutdown_in_progress

    runner.database = SimpleNamespace(close=Mock(side_effect=assert_cancelled))

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        lifecycle,
        "_run_agent_hook_replay_barrier",
        blocked
        if phase == "hook"
        else AsyncMock(
            return_value=HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)
        ),
    )
    if phase == "session":
        runner.wake_dispatcher.reconcile_restart_active_sessions = blocked
    elif phase == "wake":
        runner.wake_replay_coordinator.open = blocked
    for name in (
        "_run_graceful_shutdown_sequence",
        "_run_async_shutdown_cleanup",
        "_run_terminal_delivery_finalizers",
        "_shutdown_database_concurrency_under_cancellation",
        "_drain_worktree_deletes_under_cancellation",
        "force_terminate_uvicorn_http_server_under_cancellation",
    ):
        monkeypatch.setattr(shutdown, name, AsyncMock(return_value=None))
    async with asyncio.timeout(3):
        await lifecycle.init_subsystems(
            cast(GobbyRunner, runner),
            AsyncMock(),
            None,
            reconcile_agent_runs_after_restart=blocked
            if phase == "agent"
            else AsyncMock(return_value=0),
            reap_orphaned_srt_runners=AsyncMock(return_value=0),
            recover_agent_completion_subscribers=AsyncMock(return_value=0),
        )
        await entered.wait()
        server_task = asyncio.create_task(AsyncMock()())
        await shutdown.shutdown_daemon_services(
            cast(GobbyRunner, runner),
            cast(Server, MagicMock(spec=Server)),
            server_task,
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=Mock(),
            cleanup_pid_file=Mock(),
        )
        await server_task
    assert runner._startup_recovery_task.cancelled()
    assert runner.http_server.services.startup_ready is False
    assert runner.http_server.services.restart_recovery_ready is False
    runner.database.close.assert_called_once()
    cast(AsyncMock, lifecycle._start_agent_lifecycle_monitor).assert_not_awaited()
    cast(AsyncMock, lifecycle._start_cron_scheduler).assert_not_awaited()
    cast(AsyncMock, lifecycle._start_system_automation_loop).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_rollback_closes_storage_only_after_recovery_settles(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    monkeypatch.setattr(shutdown, "_expiry_exit_backstop_required", expired)
    events: list[str] = []
    entered = asyncio.Event()

    async def recovery() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("recovery-stopped")

    task = asyncio.create_task(recovery())
    runner = SimpleNamespace(
        _startup_recovery_task=task,
        database=SimpleNamespace(close=Mock(side_effect=lambda: events.append("pool-closed"))),
    )
    await entered.wait()
    await rollback.rollback_runner_resources_async(runner)
    assert task.cancelled()
    assert events == (["recovery-stopped"] if expired else ["recovery-stopped", "pool-closed"])


@pytest.mark.asyncio
async def test_terminal_lease_cleanup_precedes_database_executor_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    events: list[str] = []

    async def finalize() -> None:
        entered.set()
        await release.wait()
        events.append("leases-persisted")

    async def database_close(*args: Any) -> None:
        events.append("database-executor-closed")

    runner = SimpleNamespace(
        websocket_server=SimpleNamespace(
            lease_registry=SimpleNamespace(finalize_shutdown_attachments=finalize)
        )
    )
    monkeypatch.setattr(shutdown, "shutdown_agent_event_broadcasting", AsyncMock())
    monkeypatch.setattr(
        "gobby.sessions.compact_continuation.shutdown_compact_continuations", AsyncMock()
    )
    monkeypatch.setattr(shutdown, "_settle_terminal_delivery_barrier", AsyncMock())
    monkeypatch.setattr(shutdown, "_shutdown_database_concurrency", database_close)
    monkeypatch.setattr(shutdown, "reset_terminal_delivery_offload", Mock())
    task = asyncio.create_task(
        shutdown._run_terminal_delivery_finalizers(cast(GobbyRunner, runner))
    )
    try:
        await entered.wait()
        assert not events
    finally:
        release.set()
        await task
    assert events == ["leases-persisted", "database-executor-closed"]


@pytest.mark.asyncio
async def test_recovery_failure_keeps_lifecycle_admission_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(monkeypatch, FastAPI())
    monkeypatch.setattr(
        lifecycle,
        "_run_agent_hook_replay_barrier",
        AsyncMock(side_effect=RuntimeError("barrier failed")),
    )
    async with asyncio.timeout(2):
        await lifecycle.init_subsystems(cast(GobbyRunner, runner), AsyncMock(), None)
        with pytest.raises(RuntimeError, match="barrier failed"):
            await runner._startup_recovery_task
    assert runner.http_server.services.startup_ready is True
    assert spawn_readiness_blocker(runner.http_server.services) == "daemon_restart_recovery_pending"
    cast(AsyncMock, lifecycle._start_system_automation_loop).assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_task_cancellation_deadline_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutdown, "_expiry_exit_backstop_required", False)
    release = asyncio.Event()
    cancel_seen = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()

    task = asyncio.create_task(stubborn())
    runner = SimpleNamespace(_startup_recovery_task=task)
    try:
        await asyncio.sleep(0)
        async with asyncio.timeout(1):
            await shutdown._cancel_runner_task(
                cast(GobbyRunner, runner), "_startup_recovery_task", timeout=0.01
            )
        assert cancel_seen.is_set()
        assert not task.done()
        assert shutdown.finalizer_expiry_backstop_required()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_wake_timeout_still_cancels_inbox_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutdown, "_expiry_exit_backstop_required", False)
    app = FastAPI()
    inbox_stop = AsyncMock()
    monkeypatch.setattr("gobby.hooks.inbox_lifecycle.stop_hook_inbox_replays", inbox_stop)
    services = SimpleNamespace(shutdown_in_progress=False)
    runner = SimpleNamespace(
        http_server=SimpleNamespace(app=app, services=services),
        wake_replay_coordinator=SimpleNamespace(close=AsyncMock(side_effect=TimeoutError)),
    )
    await shutdown.stop_restart_recovery(cast(GobbyRunner, runner))
    inbox_stop.assert_awaited_once_with(app)
    assert services.shutdown_in_progress
    assert shutdown.finalizer_expiry_backstop_required()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hook", "session", "handoff", "wake", "agent", "sandbox"])
async def test_shutdown_prevents_next_recovery_phase(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    runner = _runner(monkeypatch, FastAPI())
    calls: list[str] = []

    def operation(name: str, result: Any) -> AsyncMock:
        async def run(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            if name == phase:
                runner.http_server.services.shutdown_in_progress = True
            return result

        return AsyncMock(side_effect=run)

    monkeypatch.setattr(
        lifecycle,
        "_run_agent_hook_replay_barrier",
        operation("hook", HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)),
    )
    runner.wake_dispatcher.reconcile_restart_active_sessions = operation("session", ())
    monkeypatch.setattr(lifecycle, "_resume_dead_handoff_dispatches", operation("handoff", None))
    runner.wake_replay_coordinator.open = operation("wake", None)
    await lifecycle._recover_after_restart(
        cast(GobbyRunner, runner),
        None,
        reconcile_agent_runs_after_restart=operation("agent", 0),
        reap_orphaned_srt_runners=operation("sandbox", 0),
        recover_agent_completion_subscribers=operation("subscribers", 0),
    )
    sequence = ["hook", "session", "handoff", "wake", "agent", "sandbox", "subscribers"]
    assert calls == sequence[: sequence.index(phase) + 1]
    assert runner.http_server.services.restart_recovery_ready is False
    cast(AsyncMock, lifecycle._start_core_services).assert_not_awaited()
    cast(AsyncMock, lifecycle._start_agent_lifecycle_monitor).assert_not_awaited()


@pytest.mark.asyncio
async def test_node_lifecycle_starts_only_after_local_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(monkeypatch, FastAPI())
    runner.bootstrap_config = SimpleNamespace(run_mode=lambda: "node")
    events: list[str] = []

    async def barrier(*args: Any) -> HookReplayBarrierOutcome:
        events.append("hooks-settled")
        return HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)

    async def start_local(*args: Any) -> None:
        events.append("local-lifecycle-started")

    monkeypatch.setattr(lifecycle, "_run_agent_hook_replay_barrier", barrier)
    monkeypatch.setattr(lifecycle, "_start_machine_local_lifecycle", start_local)
    await lifecycle._recover_after_restart(
        cast(GobbyRunner, runner),
        None,
        reconcile_agent_runs_after_restart=AsyncMock(return_value=0),
        reap_orphaned_srt_runners=AsyncMock(return_value=0),
        recover_agent_completion_subscribers=AsyncMock(return_value=0),
    )
    assert events == ["hooks-settled", "local-lifecycle-started"]
    assert runner.http_server.services.restart_recovery_ready
    cast(AsyncMock, lifecycle._start_core_services).assert_not_awaited()
    cast(AsyncMock, lifecycle._start_cron_scheduler).assert_not_awaited()
    cast(AsyncMock, lifecycle._recover_pipelines).assert_not_awaited()
    cast(AsyncMock, lifecycle._start_system_automation_loop).assert_not_awaited()
