from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from gobby.ai.embedding_switch_service import (
    EmbeddingSwitchCoordinator,
    EmbeddingSwitchTaskActive,
)


@dataclass
class FakeJournal:
    run_id: str
    phase: str = "staging"


class FakeRunner:
    def __init__(self, control: Any, events: list[str]) -> None:
        self.control = control
        self.events = events
        self.started = asyncio.Event()
        self.persistent_operation_done = asyncio.Event()

    async def run(self, journal: FakeJournal) -> dict[str, Any]:
        self.events.append(f"run:{journal.run_id}")
        self.started.set()
        await self.control.abort_requested.wait()
        await self.persistent_operation_done.wait()
        self.events.extend(["cleanup:staged", "journal:delete"])
        return {"completed": False, "aborted": True}


@pytest.mark.asyncio
async def test_start_is_single_flight_and_abort_waits_for_cooperative_cleanup() -> None:
    events: list[str] = []
    runners: list[FakeRunner] = []

    def factory(_store: Any, _db: Any, control: Any, _fence: Any) -> FakeRunner:
        runner = FakeRunner(control, events)
        runners.append(runner)
        return runner

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=factory,
        start_journal=lambda *_args: FakeJournal("run-1"),
    )
    first_status = await coordinator.start("catalog", "provider")
    first = coordinator.task
    assert first_status.run_id == "run-1"
    await runners[0].started.wait()

    with pytest.raises(EmbeddingSwitchTaskActive):
        await coordinator.start("catalog", "provider")

    abort_task = asyncio.create_task(coordinator.abort())
    await coordinator.control.abort_requested.wait()
    assert not abort_task.done()

    runners[0].persistent_operation_done.set()
    result = await abort_task
    assert result.status == "aborted"
    assert events == ["run:run-1", "cleanup:staged", "journal:delete"]
    assert (await first).get("aborted") is True


@pytest.mark.asyncio
async def test_abort_is_too_late_once_flipping_has_started() -> None:
    events: list[str] = []
    runners: list[FakeRunner] = []

    def factory(_store: Any, _db: Any, control: Any, _fence: Any) -> FakeRunner:
        runner = FakeRunner(control, events)
        runners.append(runner)
        return runner

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=factory,
        start_journal=lambda *_args: FakeJournal("run-1"),
    )
    await coordinator.start("catalog", "provider")
    task = coordinator.task
    await runners[0].started.wait()
    coordinator.control.mark_flipping_started()

    result = await coordinator.abort()
    assert result.status == "too_late"
    assert not task.done()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    await task


@pytest.mark.asyncio
async def test_resume_uses_the_same_single_flight_gate() -> None:
    events: list[str] = []
    runners: list[FakeRunner] = []

    def factory(_store: Any, _db: Any, control: Any, _fence: Any) -> FakeRunner:
        runner = FakeRunner(control, events)
        runners.append(runner)
        return runner

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=factory,
        load_journal=lambda: FakeJournal("persisted-run"),
    )
    status = await coordinator.resume()
    task = coordinator.task
    assert status.run_id == "persisted-run"
    await runners[0].started.wait()

    with pytest.raises(EmbeddingSwitchTaskActive):
        await coordinator.resume()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    await task
    assert events[0] == "run:persisted-run"


@pytest.mark.asyncio
async def test_abort_retries_persisted_cleanup_pending_journal() -> None:
    cleanup_runs: list[str] = []

    class CleanupRunner:
        async def run(self, journal: FakeJournal) -> dict[str, Any]:
            cleanup_runs.append(journal.run_id)
            return {"aborted": True}

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda *_args: CleanupRunner(),
        load_journal=lambda: FakeJournal("cleanup-run", phase="aborted"),
    )

    result = await coordinator.abort()

    assert result.status == "aborted"
    assert result.run_id == "cleanup-run"
    assert cleanup_runs == ["cleanup-run"]
