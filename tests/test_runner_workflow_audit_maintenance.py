from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.runner_lifecycle_periodic import _default_loops, start_periodic_tasks


@pytest.mark.asyncio
async def test_periodic_start_schedules_configured_workflow_audit_retention() -> None:
    calls: list[dict[str, Any]] = []

    async def complete_loop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture_audit_loop(*_args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    loops = dict.fromkeys(_default_loops(), complete_loop)
    loops["workflow_audit_cleanup_loop"] = capture_audit_loop
    runner = SimpleNamespace(
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(
                    active=DaemonConfig(session_lifecycle={"workflow_audit_retention_days": 21})
                )
            )
        ),
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        db_executor=None,
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        session_manager=None,
        _shutdown_requested=False,
    )

    start_periodic_tasks(runner, tracker=None, **loops)
    await asyncio.gather(
        *(task for task in vars(runner).values() if isinstance(task, asyncio.Task))
    )

    assert len(calls) == 1
    snapshot = calls[0]["capture_bundle"]()
    assert snapshot.snapshot.active.session_lifecycle.workflow_audit_retention_days == 21
