from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.cron import (
    MEMORY_DREAM_CRON_HANDLER,
    MEMORY_DREAM_CRON_JOB_NAME,
    register_memory_dream_cron,
)
from gobby.memory.dream.service import DreamRunOptions

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
        self.toggled_job_ids: list[str] = []

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

    def toggle_job(self, job_id: str) -> Any:
        self.toggled_job_ids.append(job_id)
        return SimpleNamespace(id=job_id, enabled=True)


class _FakeCronExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


class _FakeDreamManager:
    def __init__(self, targets: list[str | None]) -> None:
        self.targets = targets
        self.cutoffs: list[str] = []
        self.db = MagicMock()

    def list_dream_project_ids(self, *, redream_cutoff: str) -> list[str | None]:
        self.cutoffs.append(redream_cutoff)
        return list(self.targets)


def test_register_memory_dream_cron_creates_single_system_job() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    config = SimpleNamespace(enabled=True, schedule_cron="0 3 * * *")

    registered = register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=MagicMock(),
        dream_config=config,
        project_id="proj-1",
    )

    assert registered == 1
    assert set(cron_executor.handlers) == {MEMORY_DREAM_CRON_HANDLER}
    assert len(cron_storage.created_jobs) == 1
    kwargs = cron_storage.created_jobs[0]
    assert kwargs["name"] == MEMORY_DREAM_CRON_JOB_NAME
    assert kwargs["schedule_type"] == "cron"
    assert kwargs["cron_expr"] == "0 3 * * *"
    assert kwargs["action_config"] == {"handler": MEMORY_DREAM_CRON_HANDLER}
    assert kwargs["is_system"] is True


def test_register_memory_dream_cron_does_not_register_pipeline_action() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()

    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=MagicMock(),
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    kwargs = cron_storage.created_jobs[0]
    assert kwargs["action_type"] == "handler"
    assert kwargs["action_config"] == {"handler": MEMORY_DREAM_CRON_HANDLER}


def test_register_memory_dream_cron_tolerates_missing_job_during_disable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True),
        update_result=None,
    )

    registered = register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=MagicMock(),
        memory_manager=MagicMock(),
        dream_config=SimpleNamespace(enabled=False),
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
        cron_storage=cron_storage,
        cron_executor=MagicMock(),
        memory_manager=MagicMock(),
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    assert cron_storage.reconciled_jobs[0][0] == "job-1"
    assert cron_storage.toggled_job_ids == []


def test_register_memory_dream_cron_restores_previously_enabled_system_job() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True, is_system=True),
        repaired=SimpleNamespace(id="job-1", enabled=False),
    )

    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=MagicMock(),
        memory_manager=MagicMock(),
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    assert cron_storage.reconciled_jobs[0][0] == "job-1"
    assert cron_storage.toggled_job_ids == ["job-1"]


def _patch_dream_service(
    monkeypatch: pytest.MonkeyPatch, aggregate: dict[str, Any]
) -> dict[str, Any]:
    """Replace cron's MemoryDreamService with a fake that records construction and
    returns a canned aggregate. The per-target loop itself is unit-tested against
    the real service in test_dream.py; here we only verify cron orchestration."""
    captured: dict[str, Any] = {}

    class _Service:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs

        async def run_all_due_projects(self, **kwargs: Any) -> dict[str, Any]:
            captured["call"] = kwargs
            return aggregate

    monkeypatch.setattr("gobby.memory.dream.cron.MemoryDreamService", _Service)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_unattended_mutations", "expected_dry_run"),
    [(False, True), (True, False)],
)
async def test_memory_dream_cron_handler_delegates_and_formats_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    allow_unattended_mutations: bool,
    expected_dry_run: bool,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-a", None, "proj-b"])
    captured = _patch_dream_service(
        monkeypatch,
        {"success": True, "targets": 3, "completed": 3, "failed": 0, "mutations": 4, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=MemoryDreamConfig(
            allow_unattended_mutations=allow_unattended_mutations,
        ),
        project_id="daemon-proj",
        daemon_config=SimpleNamespace(),
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 3 target(s), 4 mutation(s) total"
    # The daemon's own project identity is threaded into the service so the
    # per-target loop can route the daemon's own memories to platform truth.
    assert captured["init"]["current_project_id"] == "daemon-proj"
    assert captured["init"]["memory_manager"] is memory_manager
    # Nightly runs stay cooldown-throttled (full_sweep is not forced).
    assert captured["call"].get("full_sweep", False) is False
    assert captured["call"]["dry_run"] is expected_dry_run


def test_dream_run_options_default_to_plan_only() -> None:
    assert DreamRunOptions().dry_run is True


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_reports_failed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-ok", "proj-bad", None])
    _patch_dream_service(
        monkeypatch,
        {"success": True, "targets": 3, "completed": 2, "failed": 1, "mutations": 4, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 2 target(s), 4 mutation(s) total, 1 failed"


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_raises_when_aggregate_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-bad", None])
    _patch_dream_service(
        monkeypatch,
        {"success": False, "targets": 2, "completed": 0, "failed": 2, "mutations": 0, "runs": []},
    )
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=MemoryDreamConfig(),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    with pytest.raises(RuntimeError, match="failed for all targets"):
        await handler(SimpleNamespace())
