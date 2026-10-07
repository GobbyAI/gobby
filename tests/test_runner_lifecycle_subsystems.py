from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import gobby.runner_lifecycle_subsystems as lifecycle_subsystems
from gobby.config.bootstrap import BootstrapConfig
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.runner_hook_replay import HookReplayBarrierOutcome
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests._timing import drain_asyncio_tasks

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.runner_lifecycle_startup import StartupTracker

pytestmark = pytest.mark.unit

SAFE_BARRIER = HookReplayBarrierOutcome(settled=True, session_recovery_safe=True)


def _patch_init_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async_steps = (
        "_run_agent_hook_replay_barrier",
        "_connect_mcp_servers",
        "_check_embedding_service",
        "_cleanup_metrics_on_startup",
        "_cleanup_stale_expansion_runs_on_startup",
        "_initialize_vector_store",
        "_start_core_services",
        "_start_terminal_host",
        "_start_agent_lifecycle_monitor",
        "_start_cron_scheduler",
        "_recover_pipelines",
        "_start_system_automation_loop",
    )
    for name in async_steps:
        result = SAFE_BARRIER if name == "_run_agent_hook_replay_barrier" else None
        monkeypatch.setattr(lifecycle_subsystems, name, AsyncMock(return_value=result))
    monkeypatch.setattr(
        lifecycle_subsystems,
        "_repair_code_index_bm25",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(lifecycle_subsystems, "_start_websocket_server", Mock())
    monkeypatch.setattr(lifecycle_subsystems, "_schedule_workflow_skill_prewarm", Mock())


def _minimal_init_runner() -> SimpleNamespace:
    services = SimpleNamespace(shutdown_in_progress=False, startup_ready=False)
    return SimpleNamespace(
        bootstrap_config=BootstrapConfig(),
        agent_lifecycle_monitor=None,
        agent_runner=None,
        http_bound_at_ms=1_700_000_000_000,
        http_server=SimpleNamespace(services=services),
        wake_dispatcher=SimpleNamespace(
            reconcile_restart_active_sessions=AsyncMock(return_value=())
        ),
    )


@pytest.mark.asyncio
async def test_periodic_agent_reconciliation_rotates_credentials_before_runs_and_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def rotate_due() -> list[str]:
        calls.append("credentials")
        return ["execution-a", "execution-b"]

    runner = cast(
        "GobbyRunner",
        SimpleNamespace(managed_credential_manager=SimpleNamespace(rotate_due=rotate_due)),
    )

    async def agent_reconcile(received: GobbyRunner) -> int:
        assert received is runner
        calls.append("runs")
        return 2

    async def review_reconcile(received: GobbyRunner) -> int:
        assert received is runner
        calls.append("reviews")
        return 3

    monkeypatch.setattr(
        lifecycle_subsystems,
        "_reclassify_reconciliation_pending_runs",
        agent_reconcile,
    )
    monkeypatch.setattr(
        lifecycle_subsystems,
        "_reconcile_task_close_reviews",
        review_reconcile,
    )

    reconciled = await lifecycle_subsystems._reconcile_agent_lifecycle_state(runner)

    assert reconciled == 7
    assert calls == ["credentials", "runs", "reviews"]


@pytest.mark.asyncio
async def test_failed_credential_rotation_does_not_stop_run_and_review_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def rotate_due() -> list[str]:
        raise RuntimeError("hub unavailable")

    runner = cast(
        "GobbyRunner",
        SimpleNamespace(managed_credential_manager=SimpleNamespace(rotate_due=rotate_due)),
    )
    monkeypatch.setattr(
        lifecycle_subsystems,
        "_reclassify_reconciliation_pending_runs",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        lifecycle_subsystems,
        "_reconcile_task_close_reviews",
        AsyncMock(return_value=3),
    )

    with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
        reconciled = await lifecycle_subsystems._reconcile_agent_lifecycle_state(runner)

    assert reconciled == 5
    assert "Managed credential rotation failed" in caplog.text


@pytest.mark.asyncio
async def test_ui_dev_server_start_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_init_dependencies(monkeypatch)
    launcher_returned = threading.Event()
    callback_observations: list[bool] = []

    def launch(_runner: object) -> None:
        # test-quality: allow SLEEP_IN_TEST -- criterion requires a 0.3 s blocking launcher
        time.sleep(0.3)
        launcher_returned.set()

    monkeypatch.setattr(lifecycle_subsystems, "_maybe_start_ui_dev_server", launch)
    asyncio.get_running_loop().call_soon(
        lambda: callback_observations.append(launcher_returned.is_set()),
    )

    await lifecycle_subsystems.init_subsystems(
        cast("GobbyRunner", _minimal_init_runner()),
        AsyncMock(),
        None,
        reap_orphaned_srt_runners=AsyncMock(),
        recover_agent_completion_subscribers=AsyncMock(return_value=0),
    )

    assert launcher_returned.is_set()
    assert callback_observations == [False]


@pytest.mark.asyncio
async def test_ui_dev_server_start_reports_tracker_status(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_init_dependencies(monkeypatch)

    def fail_launch(_runner: object) -> None:
        raise RuntimeError("launch failed")

    monkeypatch.setattr(lifecycle_subsystems, "_maybe_start_ui_dev_server", fail_launch)
    tracker = SimpleNamespace(error=Mock(), finish=Mock())

    with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
        await lifecycle_subsystems.init_subsystems(
            cast("GobbyRunner", _minimal_init_runner()),
            AsyncMock(),
            cast("StartupTracker", tracker),
            reap_orphaned_srt_runners=AsyncMock(),
            recover_agent_completion_subscribers=AsyncMock(return_value=0),
        )

    tracker.error.assert_called_once_with("UI development server", "launch failed")
    assert tracker.finish.call_count == 1
    assert "UI development server start failed: launch failed" in caplog.text


@pytest.mark.asyncio
async def test_startup_vector_rebuild_includes_project_id_payload() -> None:
    vector_store = SimpleNamespace(
        initialize=AsyncMock(),
        ensure_collection=AsyncMock(),
        count=AsyncMock(return_value=0),
    )
    memory = SimpleNamespace(id="memory-1", content="content", project_id="project-1")
    runner = SimpleNamespace(
        vector_store=vector_store,
        memory_manager=SimpleNamespace(
            storage=SimpleNamespace(list_memories=lambda **_kwargs: [memory]),
            embed_fn=object(),
        ),
        config=SimpleNamespace(embeddings=SimpleNamespace(dim=768)),
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(
                    active=SimpleNamespace(embeddings=SimpleNamespace(dim=768))
                )
            )
        ),
        _vector_rebuild_task=None,
    )
    captured: list[dict[str, str]] = []

    async def rebuild(
        _vector_store: object,
        memory_dicts: Any,
        _embed_fn: object,
    ) -> None:
        captured.extend(memory_dicts())

    await lifecycle_subsystems._initialize_vector_store(
        cast("GobbyRunner", runner), rebuild, tracker=None
    )
    await runner._vector_rebuild_task

    assert captured == [{"id": "memory-1", "content": "content", "project_id": "project-1"}]


@pytest.mark.asyncio
async def test_wake_replay_gate_opens_after_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    """Clients reconnect as soon as HTTP serves; the surviving gterm host must be
    adopted before the slow recovery steps so their attaches find it (#22002)."""
    _patch_init_dependencies(monkeypatch)
    order: list[str] = []
    runner = _minimal_init_runner()
    tracker = SimpleNamespace(complete=Mock(), error=Mock(), finish=Mock())

    def record(name: str, result: object = None) -> AsyncMock:
        async def step(*_args: object, **_kwargs: object) -> object:
            order.append(name)
            return result

        return AsyncMock(side_effect=step)

    steps = ("_run_agent_hook_replay_barrier", "_connect_mcp_servers", "_start_terminal_host")
    mocks = {name: record(name) for name in steps}
    for name, mock in mocks.items():
        monkeypatch.setattr(lifecycle_subsystems, name, mock)
    recipient = session_manager.register(
        external_id="startup-gate-recipient",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    sender = session_manager.register(
        external_id="startup-gate-sender",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=sender.id,
        to_session=recipient.id,
        content="startup gate",
        metadata_json='{"wake_requested": true}',
    )
    terminal_write = AsyncMock(
        return_value={"session_id": recipient.id, "delivered": True, "method": "terminal"}
    )

    async def run_barrier(*_args: object, **_kwargs: object) -> HookReplayBarrierOutcome:
        order.append("_run_agent_hook_replay_barrier")
        session_manager.update_status(recipient.id, "paused")
        await drain_asyncio_tasks()
        terminal_write.assert_not_awaited()
        return SAFE_BARRIER

    mocks["_run_agent_hook_replay_barrier"].side_effect = run_barrier
    runner.wake_dispatcher = SimpleNamespace(
        reconcile_restart_active_sessions=record("session_reconcile")
    )

    async def run_db(operation: Any, *args: Any, **kwargs: Any) -> Any:
        return operation(*args, **kwargs)

    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=SimpleNamespace(dispatch_live_wake=terminal_write),
        run_db=run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())

    async def open_gate() -> None:
        order.append("wake_replay_open")
        await coordinator.open()

    runner.wake_replay_coordinator = SimpleNamespace(open=open_gate)
    monkeypatch.setattr(lifecycle_subsystems, "_maybe_start_ui_dev_server", lambda _runner: None)

    await lifecycle_subsystems.init_subsystems(
        cast("GobbyRunner", runner),
        AsyncMock(),
        cast("StartupTracker", tracker),
        reap_orphaned_srt_runners=AsyncMock(),
        recover_agent_completion_subscribers=AsyncMock(return_value=0),
    )

    assert runner.http_server.services.startup_ready is True
    mocks["_run_agent_hook_replay_barrier"].assert_not_awaited()
    await runner._startup_recovery_task

    assert order == [
        "_start_terminal_host",
        "_connect_mcp_servers",
        "_run_agent_hook_replay_barrier",
        "session_reconcile",
        "wake_replay_open",
    ]
    mocks["_start_terminal_host"].assert_awaited_once_with(runner, tracker)
    runner.wake_dispatcher.reconcile_restart_active_sessions.assert_awaited_once_with(
        restart_horizon_ms=runner.http_bound_at_ms,
        excluded_session_ids=frozenset(),
        recovery_safe=True,
    )
    terminal_write.assert_awaited_once_with(recipient.id, priority="normal")
    assert runner.http_server.services.restart_recovery_ready is True


HUB_ONLY_PHASES = {
    "code_index_bm25": "_repair_code_index_bm25",
    "metrics_cleanup": "_cleanup_metrics_on_startup",
    "expansion_cleanup": "_cleanup_stale_expansion_runs_on_startup",
    "vector_store": "_initialize_vector_store",
    "core_services": "_start_core_services",
    "cron_scheduler": "_start_cron_scheduler",
    "pipeline_recovery": "_recover_pipelines",
    "system_automation_start": "_start_system_automation_loop",
}
EVERY_MODE_STEPS = (
    "_start_terminal_host",
    "_run_agent_hook_replay_barrier",
    "_connect_mcp_servers",
    "_check_embedding_service",
    "_start_agent_lifecycle_monitor",
    "_start_websocket_server",
)
NODE_ONLY_STEP = "_start_machine_local_lifecycle"


async def _run_init_recording_steps(
    monkeypatch: pytest.MonkeyPatch, bootstrap_config: BootstrapConfig
) -> set[str]:
    _patch_init_dependencies(monkeypatch)
    called: set[str] = set()

    def record_async(name: str, result: object = None) -> AsyncMock:
        async def step(*_args: object, **_kwargs: object) -> object:
            called.add(name)
            return result

        return AsyncMock(side_effect=step)

    def record_sync(name: str) -> Mock:
        return Mock(side_effect=lambda *_args, **_kwargs: called.add(name))

    for name in (*HUB_ONLY_PHASES.values(), *EVERY_MODE_STEPS):
        if name == "_start_websocket_server":
            monkeypatch.setattr(lifecycle_subsystems, name, record_sync(name))
        elif name == "_run_agent_hook_replay_barrier":
            monkeypatch.setattr(lifecycle_subsystems, name, record_async(name, SAFE_BARRIER))
        elif name == "_repair_code_index_bm25":
            monkeypatch.setattr(lifecycle_subsystems, name, record_async(name, True))
        else:
            monkeypatch.setattr(lifecycle_subsystems, name, record_async(name))
    monkeypatch.setattr(lifecycle_subsystems, NODE_ONLY_STEP, record_async(NODE_ONLY_STEP))
    monkeypatch.setattr(
        lifecycle_subsystems, "_start_code_index_tasks", record_sync("_start_code_index_tasks")
    )
    monkeypatch.setattr(lifecycle_subsystems, "_maybe_start_ui_dev_server", lambda _runner: None)
    runner = _minimal_init_runner()
    runner.bootstrap_config = bootstrap_config

    await lifecycle_subsystems.init_subsystems(
        cast("GobbyRunner", runner),
        AsyncMock(),
        None,
        reap_orphaned_srt_runners=AsyncMock(),
        recover_agent_completion_subscribers=AsyncMock(return_value=0),
    )
    assert runner.http_server.services.startup_ready is True
    await runner._startup_recovery_task
    assert runner.http_server.services.restart_recovery_ready is True
    return called


def _phase_skips(caplog: pytest.LogCaptureFixture) -> list[str]:
    return sorted(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("skipping hub-only ")
    )


@pytest.mark.asyncio
async def test_node_mode_skips_hub_only_phases(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"):
        called = await _run_init_recording_steps(
            monkeypatch, BootstrapConfig(datastore_mode="remote")
        )

    assert called == {*EVERY_MODE_STEPS, NODE_ONLY_STEP}
    assert _phase_skips(caplog) == sorted(
        f"skipping hub-only {phase} in node mode"
        for phase in {*HUB_ONLY_PHASES, "communications_start"}
    )


@pytest.mark.asyncio
async def test_node_lifecycle_phase_starts_the_manager_machine_local_only() -> None:
    start_kwargs: list[dict[str, object]] = []

    async def start(**kwargs: object) -> None:
        start_kwargs.append(kwargs)

    runner = SimpleNamespace(lifecycle_manager=SimpleNamespace(start=start))

    await lifecycle_subsystems._start_machine_local_lifecycle(cast("GobbyRunner", runner), None)

    assert start_kwargs == [{"machine_local_only": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("hub", [False, True], ids=["standalone", "hub"])
async def test_standalone_and_hub_run_every_phase(
    hub: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"):
        called = await _run_init_recording_steps(monkeypatch, BootstrapConfig(hub=hub))

    assert called == {
        *HUB_ONLY_PHASES.values(),
        *EVERY_MODE_STEPS,
        "_start_code_index_tasks",
    }
    assert _phase_skips(caplog) == []
