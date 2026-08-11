"""Focused startup coverage for workflow skill prewarming."""

import asyncio
from collections.abc import Coroutine
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
    await asyncio.wait_for(started.wait(), timeout=1)

    assert services.startup_ready is True
    assert finished.is_set() is False

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_workflow_skill_prewarm_failure_logs_project_and_traceback(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_tasks: list[asyncio.Task[None]] = []

    async def prewarm_skill_scripts(*, project_id: str | None) -> None:
        raise RuntimeError(f"prewarm failed for {project_id}")

    def capture_task(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        created_tasks.append(task)
        return task

    runner = cast(
        GobbyRunner,
        SimpleNamespace(
            http_server=SimpleNamespace(
                services=SimpleNamespace(project_id="project-id"),
                _hook_manager=SimpleNamespace(
                    _workflow_handler=SimpleNamespace(
                        rule_engine=SimpleNamespace(prewarm_skill_scripts=prewarm_skill_scripts)
                    )
                ),
            )
        ),
    )

    with caplog.at_level("WARNING"):
        monkeypatch.setattr(lifecycle, "create_background_task", capture_task)
        lifecycle._schedule_workflow_skill_prewarm(runner)
        assert len(created_tasks) == 1
        with pytest.raises(RuntimeError, match="prewarm failed for project-id"):
            await asyncio.wait_for(created_tasks[0], timeout=1)

    assert "Workflow skill prewarm failed for project project-id" in caplog.text
    assert "RuntimeError: prewarm failed for project-id" in caplog.text


def test_workflow_skill_prewarm_skip_logs_missing_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = cast(
        GobbyRunner,
        SimpleNamespace(
            http_server=SimpleNamespace(
                services=None,
                _hook_manager=SimpleNamespace(_workflow_handler=None),
            )
        ),
    )

    with caplog.at_level("DEBUG"):
        lifecycle._schedule_workflow_skill_prewarm(runner)

    assert "services=False rule_engine=False" in caplog.text
