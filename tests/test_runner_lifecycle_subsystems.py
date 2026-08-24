from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import gobby.runner_lifecycle_subsystems as lifecycle_subsystems
from gobby.runner_lifecycle_subsystems import _register_wiki_cron_handlers
from gobby.wiki import prune_job, scheduled_jobs

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit


def _config_runtime(wiki_enabled: bool = True) -> SimpleNamespace:
    active = SimpleNamespace(wiki=SimpleNamespace(enabled=wiki_enabled))
    bundle = SimpleNamespace(snapshot=SimpleNamespace(active=active))
    return SimpleNamespace(capture=lambda: bundle)


def _runner(wiki_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        cron_storage=object(),
        cron_scheduler=SimpleNamespace(executor=object()),
        project_id="project-id",
        database=object(),
        config_runtime=_config_runtime(wiki_enabled),
    )


@pytest.mark.asyncio
async def test_wiki_cron_registration_uses_canonical_default_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    async def register(**kwargs: Any) -> int:
        received.update(kwargs)
        return 7

    async def run_db(
        _runner: object,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if operation is prune_job.register_wiki_prune_cron:
            return operation(*args, **kwargs)
        return ([("project-id", None)], [])

    monkeypatch.setattr(lifecycle_subsystems, "_run_db", run_db)
    monkeypatch.setattr(prune_job, "register_wiki_prune_cron", lambda **_kwargs: None)
    monkeypatch.setattr(scheduled_jobs, "register_wiki_cron_jobs_for_projects", register)

    await _register_wiki_cron_handlers(_runner(), tracker=None)

    assert received["project_scopes"] == [("project-id", None)]


@pytest.mark.asyncio
async def test_wiki_cron_registration_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_registration(**_kwargs: Any) -> int:
        raise RuntimeError("registration failed")

    async def run_db(
        _runner: object,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if operation is prune_job.register_wiki_prune_cron:
            return operation(*args, **kwargs)
        return ([("project-id", None)], [])

    monkeypatch.setattr(lifecycle_subsystems, "_run_db", run_db)
    monkeypatch.setattr(prune_job, "register_wiki_prune_cron", lambda **_kwargs: None)
    monkeypatch.setattr(
        scheduled_jobs,
        "register_wiki_cron_jobs_for_projects",
        fail_registration,
    )
    tracker_error = Mock()
    tracker = SimpleNamespace(error=tracker_error)

    with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
        await _register_wiki_cron_handlers(_runner(), tracker)

    record = next(
        record
        for record in caplog.records
        if "Failed to register wiki cron handlers" in record.message
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    tracker_error.assert_called_once_with("Wiki cron handlers", "registration failed")


@pytest.mark.asyncio
async def test_wiki_cron_registration_skipped_when_wiki_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[str] = []

    async def register(**_kwargs: Any) -> int:
        registrations.append("family")
        return 0

    async def run_db(
        _runner: object,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return operation(*args, **kwargs)

    monkeypatch.setattr(lifecycle_subsystems, "_run_db", run_db)
    monkeypatch.setattr(
        prune_job,
        "register_wiki_prune_cron",
        lambda **_kwargs: registrations.append("prune"),
    )
    monkeypatch.setattr(scheduled_jobs, "register_wiki_cron_jobs_for_projects", register)

    scheduled_row = SimpleNamespace(id="job-scheduled", enabled=True, next_run_at=object())
    disabled_row = SimpleNamespace(id="job-disabled", enabled=False, next_run_at=None)
    parked: list[str] = []
    cron_storage = SimpleNamespace(
        list_system_jobs_by_name_prefix=lambda prefix: [scheduled_row, disabled_row],
        park_system_job=lambda job_id: parked.append(job_id),
    )
    runner = _runner(wiki_enabled=False)
    runner.cron_storage = cron_storage
    tracker_complete = Mock()
    tracker = SimpleNamespace(complete=tracker_complete, error=Mock())

    await _register_wiki_cron_handlers(cast("GobbyRunner", runner), tracker)

    assert registrations == []
    assert parked == ["job-scheduled"]
    assert disabled_row.enabled is False
    tracker_complete.assert_called_once_with("Wiki cron handlers")


@pytest.mark.asyncio
async def test_global_wiki_prune_registers_before_empty_project_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[dict[str, Any]] = []

    async def run_db(
        _runner: object,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if operation is prune_job.register_wiki_prune_cron:
            return operation(*args, **kwargs)
        return ([], [])

    monkeypatch.setattr(lifecycle_subsystems, "_run_db", run_db)
    monkeypatch.setattr(
        prune_job,
        "register_wiki_prune_cron",
        lambda **kwargs: registrations.append(kwargs),
    )

    runner = _runner()
    await _register_wiki_cron_handlers(runner, tracker=None)

    assert len(registrations) == 1
    assert registrations[0]["cron_storage"] is runner.cron_storage
    assert registrations[0]["cron_executor"] is runner.cron_scheduler.executor
    assert registrations[0]["project_id"] == "project-id"


@pytest.mark.asyncio
async def test_startup_vector_rebuild_includes_project_id_payload() -> None:
    vector_store = SimpleNamespace(
        initialize=AsyncMock(),
        ensure_collection=AsyncMock(),
        count=AsyncMock(return_value=0),
    )
    memory = SimpleNamespace(id="memory-1", content="content", project_id="project-1")
    runner = SimpleNamespace(
        vector_store=vector_store,
        memory_manager=SimpleNamespace(
            storage=SimpleNamespace(list_memories=lambda **_kwargs: [memory]),
            embed_fn=object(),
        ),
        config=SimpleNamespace(embeddings=SimpleNamespace(dim=768)),
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(
                    active=SimpleNamespace(embeddings=SimpleNamespace(dim=768))
                )
            )
        ),
        _vector_rebuild_task=None,
    )
    captured: list[dict[str, str]] = []

    async def rebuild(
        _vector_store: object,
        memory_dicts: Any,
        _embed_fn: object,
    ) -> None:
        captured.extend(memory_dicts())

    await lifecycle_subsystems._initialize_vector_store(
        cast("GobbyRunner", runner), rebuild, tracker=None
    )
    await runner._vector_rebuild_task

    assert captured == [{"id": "memory-1", "content": "content", "project_id": "project-1"}]
