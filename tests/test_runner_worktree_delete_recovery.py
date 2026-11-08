"""Startup wiring for the worktree delete recovery pass (#25838)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby import runner_lifecycle_subsystems as subsystems
from gobby.runner import GobbyRunner
from gobby.worktrees.deletion import WorktreeDeletionResult

pytestmark = pytest.mark.unit

RecoveryPass = Callable[[], Awaitable[list[WorktreeDeletionResult]]]


def _runner(*, shutdown_in_progress: bool = False) -> Any:
    services = SimpleNamespace(shutdown_in_progress=shutdown_in_progress)
    return SimpleNamespace(
        database=SimpleNamespace(name="hub-db"),
        worktree_delete_executor=SimpleNamespace(name="executor"),
        http_server=SimpleNamespace(services=services),
    )


@pytest.fixture
def no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subsystems, "WORKTREE_DELETE_RECOVERY_DELAY_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_startup_pass_uses_the_runner_database_and_executor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, no_delay: None
) -> None:
    runner = _runner()
    built: list[tuple[object, object]] = []
    refusals = [WorktreeDeletionResult(success=False, error="uncommitted changes")]

    async def recover() -> list[WorktreeDeletionResult]:
        return refusals

    def factory(
        database: object, executor: object, should_stop: Callable[[], bool]
    ) -> RecoveryPass:
        built.append((database, executor))
        assert should_stop() is False
        return recover

    monkeypatch.setattr(subsystems, "deleting_worktree_recovery", factory)

    with caplog.at_level(logging.WARNING, logger="gobby.runner_lifecycle"):
        await subsystems._recover_deleting_worktrees_after_start(cast(GobbyRunner, runner))

    assert built == [(runner.database, runner.worktree_delete_executor)]
    assert "recovery refused 1 record(s)" in caplog.text


@pytest.mark.asyncio
async def test_startup_pass_does_not_start_once_shutdown_has_begun(
    monkeypatch: pytest.MonkeyPatch, no_delay: None
) -> None:
    runner = _runner(shutdown_in_progress=True)
    built: list[object] = []

    async def recover() -> list[WorktreeDeletionResult]:
        return []

    def factory(*_args: object) -> RecoveryPass:
        built.append(True)
        return recover

    monkeypatch.setattr(subsystems, "deleting_worktree_recovery", factory)

    await subsystems._recover_deleting_worktrees_after_start(cast(GobbyRunner, runner))

    assert built == []


@pytest.mark.asyncio
async def test_failed_startup_pass_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, no_delay: None
) -> None:
    runner = _runner()

    async def recover() -> list[WorktreeDeletionResult]:
        raise RuntimeError("storage unavailable")

    def factory(*_args: object) -> RecoveryPass:
        return recover

    monkeypatch.setattr(subsystems, "deleting_worktree_recovery", factory)

    with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
        await subsystems._recover_deleting_worktrees_after_start(cast(GobbyRunner, runner))

    assert "Worktree delete recovery failed" in caplog.text
