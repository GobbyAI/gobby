"""Tests for approval-timeout runner maintenance."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.runner_maintenance import expire_approval_timeouts_loop

pytestmark = pytest.mark.unit


class _RetryingApprovalManager:
    def __init__(self) -> None:
        self.attempts: list[tuple[int, str]] = []
        self.step = SimpleNamespace(id=7, step_id="approval", execution_id="execution-1")

    def get_expired_approval_steps(self) -> list[SimpleNamespace]:
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
