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

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.runner_lifecycle_startup import StartupTracker

pytestmark = pytest.mark.unit


def _patch_init_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async_steps = (
        "_run_agent_hook_replay_barrier",
        "_connect_mcp_servers",
        "_check_embedding_service",
        "_cleanup_metrics_on_startup",
        "_cleanup_stale_expansion_runs_on_startup",
        "_initialize_vector_store",
        "_start_core_services",
        "_check_tmux_health",
        "_start_terminal_host",
        "_start_agent_lifecycle_monitor",
        "_start_cron_scheduler",
        "_recover_pipelines",
        "_start_system_automation_loop",
    )
    for name in async_steps:
        monkeypatch.setattr(lifecycle_subsystems, name, AsyncMock())
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
        agent_lifecycle_monitor=None,
        agent_runner=None,
        http_server=SimpleNamespace(services=services),
    )


@pytest.mark.asyncio
async def test_periodic_agent_reconciliation_includes_task_close_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_reconcile = AsyncMock(return_value=2)
    review_reconcile = AsyncMock(return_value=3)
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
    runner = SimpleNamespace()

    reconciled = await lifecycle_subsystems._reconcile_agent_lifecycle_state(runner)

    assert reconciled == 5
    agent_reconcile.assert_awaited_once_with(runner)
    review_reconcile.assert_awaited_once_with(runner)


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
