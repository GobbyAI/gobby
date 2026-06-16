"""Integration tests for validation infrastructure-failure backoff (Fix #4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_leaf_task_with_llm
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._validation_backoff import TaskValidationBackoffStore
from gobby.tasks.validation import ValidationResult as TaskValidationResult

pytestmark = pytest.mark.unit


class _StubValidator:
    """TaskValidator stub returning a scripted result and counting calls."""

    def __init__(self, results: list[TaskValidationResult]) -> None:
        self._results = results
        self.calls = 0

    async def validate_task(self, **_kwargs: Any) -> TaskValidationResult:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


class _Clock:
    """Controllable stand-in for the module's ``datetime`` (only ``now`` is used)."""

    current = datetime(2026, 1, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.current


def _make_leaf_task(manager: LocalTaskManager, project_id: str) -> Any:
    return manager.create_task(
        project_id=project_id,
        title="Backoff leaf",
        category="code",
        validation_criteria="must pass",
    )


@pytest.mark.asyncio
async def test_infra_failure_records_backoff_and_skips_while_active(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    ctx = SimpleNamespace(task_manager=manager)
    validator = _StubValidator([TaskValidationResult(status="error", feedback="infra down")])

    # First attempt: infra failure → records backoff, retryable result.
    first = await validate_leaf_task_with_llm(task, validator, "context", None, ctx, task.id, None)
    assert first.can_close is False
    assert first.error_type == "validation_infrastructure_unavailable"
    assert validator.calls == 1
    store = TaskValidationBackoffStore(temp_db)
    state = store.get(task.id)
    assert state is not None and state.consecutive_failures == 1

    # Second attempt while the backoff window is active: validation is skipped entirely.
    second = await validate_leaf_task_with_llm(task, validator, "context", None, ctx, task.id, None)
    assert second.can_close is False
    assert second.error_type == "validation_infrastructure_unavailable"
    assert validator.calls == 1  # LLM not called again


@pytest.mark.asyncio
async def test_real_verdict_after_window_clears_backoff(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    ctx = SimpleNamespace(task_manager=manager)
    validator = _StubValidator(
        [
            TaskValidationResult(status="error", feedback="infra down"),
            TaskValidationResult(status="valid", feedback="looks good"),
        ]
    )

    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "datetime", _Clock)

    # Round 1: infra failure records backoff.
    await validate_leaf_task_with_llm(task, validator, "context", None, ctx, task.id, None)
    assert TaskValidationBackoffStore(temp_db).get(task.id) is not None

    # Advance past the backoff window; round 2 produces a real verdict and resets backoff.
    _Clock.current = _Clock.current + timedelta(hours=2)
    final = await validate_leaf_task_with_llm(task, validator, "context", None, ctx, task.id, None)
    assert final.can_close is True
    assert validator.calls == 2
    assert TaskValidationBackoffStore(temp_db).get(task.id) is None

    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    assert refreshed.validation_status == "valid"
