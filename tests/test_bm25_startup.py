from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby import runner_lifecycle_subsystems as lifecycle
from gobby.code_index import bm25_health
from gobby.runner import GobbyRunner


def _runner() -> SimpleNamespace:
    code_index = SimpleNamespace(
        enabled=True,
        maintenance_index_timeout_seconds=17,
    )
    return SimpleNamespace(
        startup_config=SimpleNamespace(database_url="postgres://db"),
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(active=SimpleNamespace(code_index=code_index))
            )
        ),
        degraded_services=set(),
    )


@pytest.mark.asyncio
async def test_startup_bm25_failure_marks_service_degraded(monkeypatch: Any) -> None:
    runner = _runner()
    status = bm25_health.unavailable_bm25_status("invalid chunk style tag: 254")
    monkeypatch.setattr(bm25_health, "repair_bm25_indexes", lambda *_args, **_kwargs: status)

    ready = await lifecycle._repair_code_index_bm25(cast(GobbyRunner, runner), None)

    assert ready is False
    assert runner.degraded_services == {"code_index_bm25"}


@pytest.mark.asyncio
async def test_startup_bm25_repair_allows_workers(monkeypatch: Any) -> None:
    runner = _runner()
    status = {
        "healthy": True,
        "repair_command": bm25_health.BM25_REPAIR_COMMAND,
        "indexes": [
            {
                "name": bm25_health.BM25_INDEXES[0],
                "state": "healthy",
                "repaired": True,
                "checks": [],
                "error": None,
            }
        ],
    }
    monkeypatch.setattr(bm25_health, "repair_bm25_indexes", lambda *_args, **_kwargs: status)

    ready = await lifecycle._repair_code_index_bm25(cast(GobbyRunner, runner), None)

    assert ready is True
    assert runner.degraded_services == set()


@pytest.mark.asyncio
async def test_init_subsystems_skips_code_index_workers_after_failed_repair(
    monkeypatch: Any,
) -> None:
    runner = _runner()
    runner.http_server = SimpleNamespace(
        services=SimpleNamespace(shutdown_in_progress=False, startup_ready=False)
    )
    async_steps = [
        "_connect_mcp_servers",
        "_check_embedding_service",
        "_cleanup_metrics_on_startup",
        "_cleanup_stale_expansion_runs_on_startup",
        "_initialize_vector_store",
        "_start_core_services",
        "_check_tmux_health",
        "_start_agent_lifecycle_monitor",
        "_start_cron_scheduler",
        "_recover_pipelines",
        "_start_system_automation_loop",
        "_run_agent_hook_replay_barrier",
    ]
    for name in async_steps:
        monkeypatch.setattr(lifecycle, name, AsyncMock())
    monkeypatch.setattr(lifecycle, "_repair_code_index_bm25", AsyncMock(return_value=False))
    monkeypatch.setattr(lifecycle, "_schedule_workflow_skill_prewarm", lambda *_args: None)
    tracked: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_run_tracked_start",
        lambda _operation, label, _tracker: tracked.append(label),
    )

    await lifecycle.init_subsystems(
        cast(GobbyRunner, runner),
        AsyncMock(),
        None,
        reap_orphaned_srt_runners=AsyncMock(),
        recover_agent_completion_subscribers=AsyncMock(return_value=0),
    )

    assert "Code index tasks" not in tracked
    assert runner.http_server.services.startup_ready is True
