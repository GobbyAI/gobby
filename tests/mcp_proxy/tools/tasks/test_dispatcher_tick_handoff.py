"""Regression tests for immediate dispatcher ticks after MCP handoffs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._dispatcher_tick import schedule_dispatcher_tick
from gobby.storage.tasks import LocalTaskManager
from tests._timing import drain_asyncio_tasks, wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_schedule_dispatcher_tick_runs_with_matching_daemon_context(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=cast(Any, SimpleNamespace()),
    )
    services = SimpleNamespace(
        agent_runner=SimpleNamespace(),
        task_manager=task_manager,
    )
    calls: list[dict[str, object]] = []

    async def fake_kick_dispatcher_tick(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("gobby.app_context._current_container", services)
    monkeypatch.setattr(
        "gobby.build.dispatch_tick.kick_dispatcher_tick",
        fake_kick_dispatcher_tick,
    )

    schedule_dispatcher_tick(
        ctx,
        project_id=sample_project["id"],
        reason="submit_for_review",
    )

    await wait_for_async_condition(lambda: calls, description="dispatcher tick")
    assert calls[0]["db"] is temp_db
    assert calls[0]["project_id"] == sample_project["id"]
    assert calls[0]["services"] is services


@pytest.mark.asyncio
async def test_schedule_dispatcher_tick_ignores_stale_app_context(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=cast(Any, SimpleNamespace()),
    )
    stale_services = SimpleNamespace(
        agent_runner=SimpleNamespace(),
        task_manager=LocalTaskManager(temp_db),
    )
    calls: list[dict[str, object]] = []

    async def fake_kick_dispatcher_tick(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("gobby.app_context._current_container", stale_services)
    monkeypatch.setattr(
        "gobby.build.dispatch_tick.kick_dispatcher_tick",
        fake_kick_dispatcher_tick,
    )

    schedule_dispatcher_tick(
        ctx,
        project_id=sample_project["id"],
        reason="submit_for_review",
    )

    await drain_asyncio_tasks()
    assert calls == []
