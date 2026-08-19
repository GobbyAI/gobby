"""Tests for approval-timeout runner maintenance."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_maintenance import expire_approval_timeouts_loop
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


class _RetryingApprovalManager:
    def __init__(self) -> None:
        self.attempts: list[tuple[int, str]] = []
        self.step = SimpleNamespace(id=7, step_id="approval", execution_id="execution-1")

    def get_expired_approval_steps(self, *, limit: int) -> list[SimpleNamespace]:
        assert limit > 0
        return [self.step]

    def expire_approval_timeout(self, *, step_execution_id: int, execution_id: str) -> None:
        self.attempts.append((step_execution_id, execution_id))
        if len(self.attempts) == 1:
            raise RuntimeError("injected expiry failure")


@pytest.mark.asyncio
async def test_expiry_loop_retries_failed_atomic_transition_on_next_tick() -> None:
    """A failed storage transition remains eligible for the following tick."""
    manager = _RetryingApprovalManager()
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 2

    await expire_approval_timeouts_loop(manager, is_shutdown_requested, interval_seconds=0)

    assert manager.attempts == [(7, "execution-1"), (7, "execution-1")]


def test_periodic_approval_expiry_uses_global_manager_without_startup_project() -> None:
    """Approval expiry starts globally when the daemon has no project context."""
    runner: Any = SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=MagicMock(),
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        _shutdown_requested=False,
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(
                    active=DaemonConfig(
                        telemetry={"trace_retention_days": 7},
                        bin_freshness=BinFreshnessConfig(enabled=False),
                    )
                )
            )
        ),
        degraded_services=set(),
    )
    approval_managers: list[LocalPipelineExecutionManager] = []

    async def noop() -> None:
        return None

    def approval_loop(
        manager: LocalPipelineExecutionManager,
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        approval_managers.append(manager)
        return noop()

    def fake_create_task(coro: Any, *, name: str | None = None) -> MagicMock:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        task = MagicMock()
        task.name = name
        return task

    with patch("gobby.runner_lifecycle_periodic.asyncio.create_task", side_effect=fake_create_task):
        start_periodic_tasks(
            runner,
            tracker=None,
            expire_approval_timeouts_loop=approval_loop,
        )

    assert runner._approval_timeout_task is not None
    assert len(approval_managers) == 1
    assert approval_managers[0].project_id is None
