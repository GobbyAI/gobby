from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import gobby.runner_lifecycle_subsystems as lifecycle_subsystems
from gobby.runner_lifecycle_subsystems import _register_wiki_cron_handlers
from gobby.wiki import prune_job, scheduled_jobs

pytestmark = pytest.mark.unit


def _runner() -> SimpleNamespace:
    return SimpleNamespace(
        cron_storage=object(),
        cron_scheduler=SimpleNamespace(executor=object()),
        project_id="project-id",
        database=object(),
    )


@pytest.mark.asyncio
async def test_wiki_cron_registration_uses_canonical_default_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    async def register(**kwargs: Any) -> int:
        received.update(kwargs)
        return 7

    async def run_db(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, None]], list[Any]]:
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

    async def run_db(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, None]], list[Any]]:
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
async def test_global_wiki_prune_registers_before_empty_project_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[dict[str, Any]] = []

    async def run_db(*_args: Any, **_kwargs: Any) -> tuple[list[Any], list[Any]]:
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
