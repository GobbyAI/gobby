from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.coordinator import MemoryDreamCoordinator
from gobby.memory.dream.cron import (
    MEMORY_DREAM_CRON_HANDLER,
    MEMORY_DREAM_CRON_JOB_NAME,
    register_memory_dream_cron,
)
from gobby.memory.dream.service import DreamRunOptions
from gobby.storage.cron import CronJobStorage

pytestmark = pytest.mark.unit


class _FakeCronStorage:
    def __init__(
        self,
        *,
        existing: Any | None = None,
        repaired: Any | None = None,
        update_result: Any | None = None,
    ) -> None:
        self.existing = existing
        self.repaired = repaired
        self.update_result = update_result
        self.created_jobs: list[dict[str, Any]] = []
        self.updated_jobs: list[tuple[str, dict[str, Any]]] = []
        self.reconciled_jobs: list[tuple[str, dict[str, Any]]] = []
        self.system_job_ids: list[str] = []
        self.woken_job_ids: list[str] = []

    def get_job_by_name(self, _name: str) -> Any | None:
        return self.existing

    def create_job(self, **kwargs: Any) -> Any:
        self.created_jobs.append(kwargs)
        return SimpleNamespace(id="created-job", **kwargs)

    def update_job(self, job_id: str, **kwargs: Any) -> Any | None:
        self.updated_jobs.append((job_id, kwargs))
        return self.update_result

    def mark_as_system_job(self, job_id: str) -> None:
        self.system_job_ids.append(job_id)

    def reconcile_system_job_definition(self, job_id: str, **kwargs: Any) -> Any | None:
        self.reconciled_jobs.append((job_id, kwargs))
        return self.repaired

    def wake_system_job(self, job_id: str) -> Any:
        self.woken_job_ids.append(job_id)
        return SimpleNamespace(id=job_id, enabled=True)


class _FakeCronExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def _stub_coordinator() -> MemoryDreamCoordinator:
    """Registration-only coordinator stand-in; the handler is never invoked."""
    return cast(MemoryDreamCoordinator, MagicMock())


def _fake_coordinator(
    aggregate: dict[str, Any],
    *,
    started: dict[str, Any] | None = None,
) -> tuple[MemoryDreamCoordinator, dict[str, Any]]:
    """Build a coordinator whose shared service resolves admission per
    ``started`` and returns a canned aggregate from the run executor. The
    per-target loop itself is unit-tested against the real service in
    test_dream.py; here we only verify cron orchestration."""
    captured: dict[str, Any] = {}

    class _Service:
        async def start_all_due_projects_async(self, **kwargs: Any) -> dict[str, Any]:
            captured["start"] = kwargs
            return dict(started or {"success": True, "run_id": "run-agg"})

        async def execute_all_due_projects_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
            captured["call"] = kwargs
            captured["run_id"] = run_id
            return {
                "success": bool(aggregate.get("success")),
                "run_id": run_id,
                "aggregate": aggregate,
            }

    coordinator = cast(MemoryDreamCoordinator, SimpleNamespace(service=_Service()))
    return coordinator, captured


def test_register_memory_dream_cron_creates_single_system_job() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    config = MemoryDreamConfig(enabled=True)

    registered = register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=_stub_coordinator(),
        dream_config=config,
        project_id="proj-1",
    )

    assert registered == 1
    assert set(cron_executor.handlers) == {MEMORY_DREAM_CRON_HANDLER}
    assert len(cron_storage.created_jobs) == 1
    kwargs = cron_storage.created_jobs[0]
    assert kwargs["name"] == MEMORY_DREAM_CRON_JOB_NAME
    assert kwargs["schedule_type"] == "cron"
    # The default nightly schedule is 2:00 AM.
    assert kwargs["cron_expr"] == "0 2 * * *"
    # Default admission window (14400s) + one work unit (1500s) + finalize grace.
    assert kwargs["action_config"] == {
        "handler": MEMORY_DREAM_CRON_HANDLER,
        "timeout_seconds": 16200.0,
        "restart_protected": True,
    }
    assert kwargs["is_system"] is True


def test_register_memory_dream_cron_does_not_register_pipeline_action() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    config = MemoryDreamConfig(enabled=True, schedule_cron="0 3 * * *")

    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=_stub_coordinator(),
        dream_config=config,
        project_id="proj-1",
    )

    kwargs = cron_storage.created_jobs[0]
    assert kwargs["action_type"] == "handler"
    assert kwargs["action_config"]["handler"] == MEMORY_DREAM_CRON_HANDLER


def test_register_memory_dream_cron_bounds_action_beyond_admission_window() -> None:
    """The inline sweep must outlive the executor's default 1440s action bound."""
    cron_storage = _FakeCronStorage()
    config = MemoryDreamConfig(
        enabled=True,
        max_runtime_seconds=7200,
        work_unit_timeout_seconds=900.0,
    )

    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=_FakeCronExecutor(),
        coordinator=_stub_coordinator(),
        dream_config=config,
        project_id="proj-1",
    )

    timeout = cron_storage.created_jobs[0]["action_config"]["timeout_seconds"]
    assert timeout == pytest.approx(8400.0)
    assert timeout > config.max_runtime_seconds + config.work_unit_timeout_seconds


def test_register_memory_dream_cron_repairs_existing_job_action_timeout() -> None:
    """An already-registered job predating the timeout gets it back on reconcile."""
    existing = SimpleNamespace(
        id="job-1",
        enabled=True,
        is_system=True,
        action_config={"handler": MEMORY_DREAM_CRON_HANDLER},
    )
    cron_storage = _FakeCronStorage(
        existing=existing,
        repaired=SimpleNamespace(id="job-1", enabled=True),
    )
    config = MemoryDreamConfig(enabled=True)

    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=_FakeCronExecutor(),
        coordinator=_stub_coordinator(),
        dream_config=config,
        project_id="proj-1",
    )

    assert not cron_storage.created_jobs
    job_id, kwargs = cron_storage.reconciled_jobs[0]
    assert job_id == "job-1"
    assert kwargs["action_config"] == {
        "handler": MEMORY_DREAM_CRON_HANDLER,
        "timeout_seconds": 16200.0,
        "restart_protected": True,
    }


def test_register_memory_dream_cron_tolerates_missing_job_during_disable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True),
        update_result=None,
    )

    registered = register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=MagicMock(),
        coordinator=_stub_coordinator(),
        dream_config=MemoryDreamConfig(enabled=False),
        project_id="proj-1",
    )

    assert registered == 0
    assert cron_storage.updated_jobs == [("job-1", {"enabled": False, "next_run_at": None})]
    assert "already disappeared during disable" in caplog.text


def test_register_memory_dream_cron_preserves_disabled_system_job() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=False, is_system=True),
        repaired=SimpleNamespace(id="job-1", enabled=False),
    )

    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=MagicMock(),
        coordinator=_stub_coordinator(),
        dream_config=MemoryDreamConfig(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    assert cron_storage.reconciled_jobs[0][0] == "job-1"
    assert cron_storage.woken_job_ids == []


def test_register_memory_dream_cron_restores_previously_enabled_system_job() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True, is_system=True),
        repaired=SimpleNamespace(id="job-1", enabled=False),
    )

    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=MagicMock(),
        coordinator=_stub_coordinator(),
        dream_config=MemoryDreamConfig(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    assert cron_storage.reconciled_jobs[0][0] == "job-1"
    assert cron_storage.woken_job_ids == ["job-1"]


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_delegates_and_formats_aggregate() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, captured = _fake_coordinator(
        {"success": True, "targets": 3, "completed": 3, "failed": 0, "mutations": 4, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="daemon-proj",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 3 target(s), 4 mutation(s) total"
    # Nightly runs stay cooldown-throttled (full_sweep is not forced).
    assert captured["call"].get("full_sweep", False) is False
    # Nightly mutating maintenance is the default: the cron never dry-runs.
    assert captured["call"]["dry_run"] is False
    # The executor runs the row that admission created.
    assert captured["start"]["dry_run"] is False
    assert captured["run_id"] == "run-agg"


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_coalesces_onto_active_run() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, captured = _fake_coordinator(
        {"success": True},
        started={"success": True, "run_id": "run-active", "coalesced": True},
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream coalesced onto active run run-active"
    assert "call" not in captured  # no second executor for a coalesced run


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_skips_on_conflicting_active_run() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, captured = _fake_coordinator(
        {"success": True},
        started={
            "success": False,
            "error": "a memory dream run is already active with incompatible options",
            "conflict": {"run_id": "run-busy", "scope": "project:proj-9", "phase": "sweep"},
        },
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream skipped: active run run-busy"
    assert "call" not in captured


def test_dream_run_options_default_to_plan_only() -> None:
    assert DreamRunOptions().dry_run is True


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_reports_failed_targets() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, _captured = _fake_coordinator(
        {"success": True, "targets": 3, "completed": 2, "failed": 1, "mutations": 4, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 2 target(s), 4 mutation(s) total, 1 failed"


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_reports_window_exhaustion_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, _captured = _fake_coordinator(
        {
            "success": True,
            "targets": 3,
            "completed": 2,
            "failed": 0,
            "mutations": 5,
            "runs": [],
            "stop_reason": "window_exhausted",
        },
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]
    caplog.set_level("INFO", logger="gobby.memory.dream.cron")

    message = await handler(SimpleNamespace())

    # A window-exhausted partial is a normal outcome: reported, never warned.
    assert message == "memory dream: 2 target(s), 5 mutation(s) total, stopped: window_exhausted"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == []


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_raises_when_aggregate_failed() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    coordinator, _captured = _fake_coordinator(
        {"success": False, "targets": 2, "completed": 0, "failed": 2, "mutations": 0, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        coordinator=coordinator,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    with pytest.raises(RuntimeError, match="failed for all targets"):
        await handler(SimpleNamespace())
