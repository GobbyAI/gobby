"""Focused startup coverage for workflow skill prewarming."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.runner_lifecycle_subsystems as lifecycle
from gobby.runner import GobbyRunner


@pytest.mark.unit
@pytest.mark.asyncio
async def test_workflow_skill_prewarm_is_scheduled_without_blocking_readiness() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def prewarm_skill_scripts(*, project_id: str | None) -> None:
        assert project_id == "project-id"
        started.set()
        await release.wait()
        finished.set()

    engine = SimpleNamespace(prewarm_skill_scripts=prewarm_skill_scripts)
    runner = cast(
        GobbyRunner,
        SimpleNamespace(
            http_server=SimpleNamespace(
                services=SimpleNamespace(project_id="project-id", startup_ready=False),
                _hook_manager=SimpleNamespace(
                    _workflow_handler=SimpleNamespace(rule_engine=engine)
                ),
            )
        ),
    )

    lifecycle._schedule_workflow_skill_prewarm(runner)
    services = cast(Any, runner.http_server.services)
    services.startup_ready = True
    await started.wait()

    assert services.startup_ready is True
    assert finished.is_set() is False

    release.set()
    await finished.wait()
