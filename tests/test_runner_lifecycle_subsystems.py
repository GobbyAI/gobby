from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.runner_lifecycle_subsystems import _register_wiki_cron_handlers
from gobby.wiki import scheduled_jobs

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

    monkeypatch.setattr(scheduled_jobs, "register_wiki_cron_jobs", register)

    await _register_wiki_cron_handlers(_runner(), tracker=None)

    assert received["project_id"] == "project-id"
    assert received["scopes"] is None


@pytest.mark.asyncio
async def test_wiki_cron_registration_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_registration(**_kwargs: Any) -> int:
        raise RuntimeError("registration failed")

    monkeypatch.setattr(scheduled_jobs, "register_wiki_cron_jobs", fail_registration)
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
