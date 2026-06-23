from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.memory.dream.cron import (
    MEMORY_DREAM_CRON_HANDLER,
    MEMORY_DREAM_CRON_JOB_NAME,
    register_memory_dream_cron,
)

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


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_loops_targets_and_formats_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-a", None, "proj-b"])
    calls: list[dict[str, Any]] = []

    async def fake_run_memory_dream(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        mutations = 2 if kwargs.get("global_only") else 1
        return {
            "success": True,
            "run": {"id": f"dream-{len(calls)}", "summary": {"mutations": mutations}},
        }

    monkeypatch.setattr("gobby.memory.dream.cron.run_memory_dream", fake_run_memory_dream)
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="daemon-proj",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 3 target(s), 4 mutation(s) total"
    assert len(memory_manager.cutoffs) == 1
    assert [
        {
            "project_id": call.get("project_id"),
            "global_only": call.get("global_only", False),
            "include_global": call.get("include_global"),
            "current_project_id": call.get("current_project_id"),
            "full_sweep": call.get("full_sweep"),
        }
        for call in calls
    ] == [
        {
            "project_id": "proj-a",
            "global_only": False,
            "include_global": False,
            "current_project_id": "daemon-proj",
            "full_sweep": None,
        },
        {
            "project_id": None,
            "global_only": True,
            "include_global": None,
            "current_project_id": "daemon-proj",
            "full_sweep": None,
        },
        {
            "project_id": "proj-b",
            "global_only": False,
            "include_global": False,
            "current_project_id": "daemon-proj",
            "full_sweep": None,
        },
    ]


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_coerces_string_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-1"])

    async def fake_run_memory_dream(**_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "run": {"id": "dream-1", "summary": {"mutations": "2"}}}

    monkeypatch.setattr("gobby.memory.dream.cron.run_memory_dream", fake_run_memory_dream)
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 1 target(s), 2 mutation(s) total"


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_warns_for_invalid_mutations(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-1"])

    async def fake_run_memory_dream(**_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "run": {"id": "dream-1", "summary": {"mutations": "bad"}}}

    caplog.set_level("WARNING", logger="gobby.memory.dream.cron")
    monkeypatch.setattr("gobby.memory.dream.cron.run_memory_dream", fake_run_memory_dream)
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert message == "memory dream: 1 target(s), 0 mutation(s) total"
    assert "Invalid memory dream mutation count: value='bad' type=str" in caplog.text


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_isolates_target_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-ok", "proj-bad", None])
    calls: list[str | None] = []

    async def fake_run_memory_dream(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs.get("project_id"))
        if kwargs.get("project_id") == "proj-bad":
            raise RuntimeError("boom")
        return {"success": True, "run": {"id": "dream-1", "summary": {"mutations": 2}}}

    monkeypatch.setattr("gobby.memory.dream.cron.run_memory_dream", fake_run_memory_dream)
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    message = await handler(SimpleNamespace())

    assert calls == ["proj-ok", "proj-bad", None]
    assert message == "memory dream: 2 target(s), 4 mutation(s) total, 1 failed"


@pytest.mark.asyncio
async def test_memory_dream_cron_handler_raises_when_every_target_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()
    memory_manager = _FakeDreamManager(["proj-bad", None])

    async def fake_run_memory_dream(**_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "run": {"summary": {"mutations": 2}}}

    monkeypatch.setattr("gobby.memory.dream.cron.run_memory_dream", fake_run_memory_dream)
    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=memory_manager,
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )
    handler = cron_executor.handlers[MEMORY_DREAM_CRON_HANDLER]

    with pytest.raises(RuntimeError, match="failed for all targets"):
        await handler(SimpleNamespace())
