from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest

import gobby.ai.embedding_switch_service as embedding_switch_service
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


def _fake_coordinator(
    *,
    start_journal: FakeJournal | None = None,
    load_journal: FakeJournal | None = None,
) -> tuple[EmbeddingSwitchCoordinator, list[FakeRunner], list[str]]:
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
        start_journal=((lambda *_args: start_journal) if start_journal is not None else None),
        load_journal=(lambda: load_journal) if load_journal is not None else None,
    )
    return coordinator, runners, events


@pytest.mark.asyncio
async def test_start_is_single_flight_and_abort_waits_for_cooperative_cleanup() -> None:
    coordinator, runners, events = _fake_coordinator(start_journal=FakeJournal("run-1"))
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
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_is_too_late_once_flipping_has_started() -> None:
    coordinator, runners, _events = _fake_coordinator(start_journal=FakeJournal("run-1"))
    await coordinator.start("catalog", "provider")
    task = coordinator.task
    await runners[0].started.wait()
    coordinator.control.mark_flipping_started()

    result = await coordinator.abort()
    assert result.status == "too_late"
    assert not task.done()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    terminal_result = await task
    assert terminal_result == {"completed": False, "aborted": True}
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_resume_uses_the_same_single_flight_gate() -> None:
    coordinator, runners, events = _fake_coordinator(load_journal=FakeJournal("persisted-run"))
    status = await coordinator.resume()
    task = coordinator.task
    assert status.run_id == "persisted-run"
    await runners[0].started.wait()

    with pytest.raises(EmbeddingSwitchTaskActive):
        await coordinator.resume()

    coordinator.control.abort_requested.set()
    runners[0].persistent_operation_done.set()
    terminal_result = await task
    assert terminal_result == {"completed": False, "aborted": True}
    assert events[0] == "run:persisted-run"
    assert coordinator.active_run_id is None


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
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_timeout_leaves_cooperative_cleanup_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_switch_service, "ABORT_WAIT_TIMEOUT_SECONDS", 0.01)
    coordinator, runners, _events = _fake_coordinator(start_journal=FakeJournal("run-1"))
    await coordinator.start("catalog", "provider")
    task = coordinator.task
    await runners[0].started.wait()

    result = await coordinator.abort()

    assert result.status == "timeout"
    assert result.run_id == "run-1"
    assert not task.done()
    runners[0].persistent_operation_done.set()
    assert await task == {"completed": False, "aborted": True}
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_abort_translates_runner_failure_to_terminal_status() -> None:
    class FailingRunner:
        def __init__(self, control: Any) -> None:
            self.control = control

        async def run(self, _journal: FakeJournal) -> None:
            await self.control.abort_requested.wait()
            raise RuntimeError("cleanup exploded")

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda _store, _db, control, _fence: FailingRunner(control),
        start_journal=lambda *_args: FakeJournal("run-1"),
    )
    await coordinator.start("catalog", "provider")

    result = await coordinator.abort()

    assert result.status == "failed"
    assert result.run_id == "run-1"
    assert result.message == "cleanup exploded"
    assert coordinator.active_run_id is None


@pytest.mark.asyncio
async def test_background_failure_is_observed_with_run_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingRunner:
        async def run(self, _journal: FakeJournal) -> None:
            raise RuntimeError("background exploded")

    coordinator = EmbeddingSwitchCoordinator(
        config_store=None,
        db=None,
        fence=None,
        runner_factory=lambda *_args: FailingRunner(),
        start_journal=lambda *_args: FakeJournal("run-1"),
    )
    callback_observed = asyncio.Event()

    def mark_callback_observed(_task: asyncio.Task[Any]) -> None:
        callback_observed.set()

    with caplog.at_level(logging.ERROR, logger="gobby.ai.embedding_switch_service"):
        await coordinator.start("catalog", "provider")
        coordinator.task.add_done_callback(mark_callback_observed)
        await asyncio.wait_for(callback_observed.wait(), timeout=1.0)

    assert coordinator.task.done()
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Embedding switch background task failed"
    ]
    assert len(records) == 1
    assert getattr(records[0], "run_id", None) == "run-1"
