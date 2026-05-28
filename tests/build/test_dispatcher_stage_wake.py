"""Dispatcher wake coverage for automated stage and close transitions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


def _capture_dispatch_schedules(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def schedule(db, *, project_id: str, reason: str, services=None) -> bool:
        calls.append((project_id, reason))
        return True

    monkeypatch.setattr("gobby.build.dispatch_tick.schedule_dispatcher_tick_for_project", schedule)
    return calls


def test_submit_for_review_schedules_direct_dispatch_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    calls = _capture_dispatch_schedules(monkeypatch)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    calls.clear()

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    assert calls == [(sample_project["id"], "task_change")]
    from gobby.storage.cron import CronJobStorage

    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_close_task_schedules_direct_dispatch_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager

    calls = _capture_dispatch_schedules(monkeypatch)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="No-review leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")

    manager.close_task(task.id, reason="completed")

    assert calls == [(sample_project["id"], "task_change")]
    from gobby.storage.cron import CronJobStorage

    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_dispatcher_wake_respects_stopped_automation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    calls = _capture_dispatch_schedules(monkeypatch)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Stopped leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=False, assigned_agent="backend-developer")
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id="worker")

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    from gobby.storage.cron import CronJobStorage

    assert calls == []
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


@pytest.mark.asyncio
async def test_final_worker_submit_for_review_dispatches_reviewer_without_manual_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.build.dispatch_tick import DispatcherTickSummary
    from gobby.config.app import DaemonConfig
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec
    from gobby.system_automation import SystemAutomationLoop

    sync_bundled_agents(temp_db)
    manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    preexisting_started = asyncio.Event()
    release_preexisting = asyncio.Event()
    spawn_kwargs: dict[str, object] = {}

    async def run_inline(
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        return func(*args, **kwargs)

    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        run_db=run_inline,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        system_automation_loop=loop,
        startup_ready=True,
        shutdown_in_progress=False,
    )
    loop.set_services(services)
    monkeypatch.setattr("gobby.app_context._current_container", services)

    real_dispatch_project_once = loop.dispatch_project_once

    async def dispatch_project_once(**kwargs: object) -> DispatcherTickSummary:
        if kwargs["reason"] == "preexisting":
            preexisting_started.set()
            await release_preexisting.wait()
            return DispatcherTickSummary()
        return await real_dispatch_project_once(**kwargs)

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id="run-reviewer",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Final worker handoff",
        category="code",
        task_type="feature",
    )
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")

    assert loop.schedule_project_dispatch(
        project_id=sample_project["id"],
        reason="preexisting",
    )
    await asyncio.wait_for(preexisting_started.wait(), timeout=1)

    updated = manager.stage_states.submit_for_review(
        task.id,
        "development",
        by_session_id="worker",
    )
    release_preexisting.set()

    await wait_for_async_condition(
        lambda: run_manager.get("run-reviewer"),
        description="reviewer dispatch",
    )

    assert updated.state == "needs_review"
    assert spawn_kwargs["agent_lookup_name"] == "qa-reviewer"
    assert spawn_kwargs["task_id"] == task.id
    assert spawn_kwargs["initial_variables"]["stage_state"] == "needs_review"
