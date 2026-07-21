"""Integration tests for validation infrastructure-failure backoff (Fix #4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle
from gobby.failure_categories import FailureCategory
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_leaf_task_with_llm
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._validation_backoff import (
    MAX_CONSECUTIVE_INFRA_FAILURES,
    TaskValidationBackoffStore,
)
from gobby.tasks.validation import ValidationResult as TaskValidationResult
from gobby.tasks.validation_history import ValidationHistoryManager

pytestmark = pytest.mark.integration


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
    """Controllable stand-in for the module's ``utc_now``."""

    current = datetime(2026, 1, 1, tzinfo=UTC)


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
    first = await validate_leaf_task_with_llm(task, validator, "context", ctx, task.id, None)
    assert first.can_close is False
    assert first.error_type == "validation_infrastructure_unavailable"
    assert validator.calls == 1
    assert manager.get_task(task.id).validation_fail_count == 0
    store = TaskValidationBackoffStore(temp_db)
    state = store.get(task.id)
    assert state is not None and state.consecutive_failures == 1
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert [(item.iteration, item.status) for item in history] == [(1, "error")]

    # Second attempt while the backoff window is active: validation is skipped entirely.
    second = await validate_leaf_task_with_llm(task, validator, "context", ctx, task.id, None)
    assert second.can_close is False
    assert second.error_type == "validation_infrastructure_unavailable"
    assert validator.calls == 1  # LLM not called again
    assert len(ValidationHistoryManager(temp_db).get_iteration_history(task.id)) == 1


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
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    # Round 1: infra failure records backoff.
    await validate_leaf_task_with_llm(task, validator, "context", ctx, task.id, None)
    assert TaskValidationBackoffStore(temp_db).get(task.id) is not None

    # Advance past the backoff window; round 2 produces a real verdict and resets backoff.
    _Clock.current = _Clock.current + timedelta(hours=2)
    final = await validate_leaf_task_with_llm(task, validator, "context", ctx, task.id, None)
    assert final.can_close is True
    assert validator.calls == 2
    assert TaskValidationBackoffStore(temp_db).get(task.id) is None

    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    # The valid verdict is persisted with the guarded close transition, which
    # this helper-level test intentionally does not execute.
    assert refreshed.validation_status == "error"
    assert final.validation_status == "valid"
    assert final.reset_reason == "llm_valid"
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert [(item.iteration, item.status) for item in history] == [
        (1, "error"),
        (2, "valid"),
    ]


@pytest.mark.asyncio
async def test_environment_verdict_is_retryable_and_does_not_increment_fail_count(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _StubValidator(
        [
            TaskValidationResult(
                status="invalid",
                feedback="PostgreSQL connection refused",
                blocking_reasons=["worktree database unavailable"],
                failure_category=FailureCategory.ENVIRONMENT,
            )
        ]
    )

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "context",
        SimpleNamespace(task_manager=manager),
        task.id,
        None,
    )

    assert result.error_type == "validation_infrastructure_failure"
    assert result.extra is not None
    assert result.extra["retryable"] is True
    assert result.extra["validation_status"] == "error"
    assert result.extra["failure_category"] == "environment"
    assert manager.get_task(task.id).validation_fail_count == 0
    assert manager.get_task(task.id).validation_status == "error"
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert history[-1].status == "error"
    assert history[-1].failure_category is FailureCategory.ENVIRONMENT


@pytest.mark.asyncio
async def test_environment_verdict_escalates_at_infrastructure_failure_threshold(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _StubValidator(
        [
            TaskValidationResult(
                status="invalid",
                feedback="PostgreSQL connection refused",
                blocking_reasons=["worktree database unavailable"],
                failure_category=FailureCategory.ENVIRONMENT,
            )
        ]
    )
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    prior_message: str | None = None
    result = None
    for attempt in range(MAX_CONSECUTIVE_INFRA_FAILURES):
        result = await validate_leaf_task_with_llm(
            task,
            validator,
            "context",
            SimpleNamespace(task_manager=manager),
            task.id,
            None,
        )
        if attempt == MAX_CONSECUTIVE_INFRA_FAILURES - 2:
            prior_message = result.message
        _Clock.current += timedelta(hours=2)

    assert result is not None
    assert result.error_type == "validation_infrastructure_failure"
    assert result.message == prior_message
    assert result.failure_category is FailureCategory.ENVIRONMENT
    assert result.extra == {
        "validation_status": "error",
        "failure_category": "environment",
        "retryable": False,
        "escalated": True,
        "consecutive_failures": MAX_CONSECUTIVE_INFRA_FAILURES,
    }
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    assert refreshed.escalated_at is not None
    assert refreshed.is_escalated is True
    assert refreshed.validation_fail_count == 0
    assert refreshed.escalation_reason == (
        "validation generation unavailable after "
        f"{MAX_CONSECUTIVE_INFRA_FAILURES} consecutive infrastructure failures"
    )


@pytest.mark.asyncio
async def test_error_verdict_escalates_with_authoritative_task_transition(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _StubValidator(
        [TaskValidationResult(status="error", feedback="provider unavailable")]
    )
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    result = None
    for _attempt in range(MAX_CONSECUTIVE_INFRA_FAILURES):
        result = await validate_leaf_task_with_llm(
            task,
            validator,
            "context",
            SimpleNamespace(task_manager=manager),
            task.id,
            None,
        )
        _Clock.current += timedelta(hours=2)

    assert result is not None
    assert result.error_type == "validation_infrastructure_unavailable"
    assert result.failure_category is FailureCategory.PROVIDER
    assert result.extra is not None
    assert result.extra["retryable"] is False
    assert result.extra["escalated"] is True
    assert result.extra["consecutive_failures"] == MAX_CONSECUTIVE_INFRA_FAILURES
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    assert refreshed.is_escalated is True
