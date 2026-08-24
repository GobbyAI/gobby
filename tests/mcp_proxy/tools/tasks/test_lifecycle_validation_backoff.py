"""Backoff contracts for the bounded close criteria review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle
from gobby.ai.text_generation import FeatureGenerationUnavailableError
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import evaluate_criteria_review
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._validation_backoff import (
    MAX_CONSECUTIVE_INFRA_FAILURES,
    TaskValidationBackoffStore,
    compute_next_retry_at,
)
from gobby.tasks.close_verdict import (
    CloseCriterionVerdict,
    CloseVerdict,
    CloseVerdictParseError,
)
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_history import ValidationHistoryManager

pytestmark = pytest.mark.integration


def test_backoff_caps_retry_at_120_seconds() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert compute_next_retry_at(100, now) - now == timedelta(seconds=120)


class _ScriptedValidator:
    def __init__(self, outcomes: list[CloseVerdict | Exception]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def validate_task(self, **_kwargs: object) -> CloseVerdict:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Clock:
    current = datetime(2026, 1, 1, tzinfo=UTC)


def _make_leaf_task(manager: LocalTaskManager, project_id: str) -> Task:
    return manager.create_task(
        project_id=project_id,
        title="Backoff leaf",
        category="code",
        validation_criteria="Focused tests pass.",
    )


def _ctx(manager: LocalTaskManager) -> RegistryContext:
    return cast(RegistryContext, SimpleNamespace(task_manager=manager))


def _valid_verdict() -> CloseVerdict:
    return CloseVerdict(
        status="valid",
        criteria=(CloseCriterionVerdict(1, "Focused tests pass.", True, None),),
        feedback="Criteria are satisfied.",
    )


async def _evaluate(
    task: Task,
    manager: LocalTaskManager,
    validator: _ScriptedValidator,
) -> lifecycle.ValidationResult:
    return await evaluate_criteria_review(
        task=task,
        task_validator=cast(TaskValidator, validator),
        ctx=_ctx(manager),
        resolved_id=task.id,
        changes_summary="Implemented the close checklist.",
        diff_text="diff --git a/a.py b/a.py",
        checklist_facts={"validation_commands": "clean"},
        validation_config=None,
    )


@pytest.mark.asyncio
async def test_infra_failure_records_backoff_and_skips_active_window(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _ScriptedValidator([CloseVerdictParseError("invalid JSON")])
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    first = await _evaluate(task, manager, validator)
    second = await _evaluate(task, manager, validator)

    assert first.error_type == "validation_infrastructure_unavailable"
    assert first.extra["retry_after"] == 15
    assert second.error_type == "validation_infrastructure_unavailable"
    assert validator.calls == 1
    refreshed = manager.get_task(task.id)
    assert refreshed is not None and refreshed.validation_fail_count == 0
    state = TaskValidationBackoffStore(temp_db).get(task.id)
    assert state is not None and state.consecutive_failures == 1
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert [(item.iteration, item.status) for item in history] == [(1, "error")]


@pytest.mark.asyncio
async def test_success_after_backoff_clears_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _ScriptedValidator([CloseVerdictParseError("invalid JSON"), _valid_verdict()])
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    await _evaluate(task, manager, validator)
    _Clock.current += timedelta(seconds=16)
    result = await _evaluate(task, manager, validator)

    assert result.can_close is True
    assert result.validation_status == "valid"
    assert result.reset_reason == "llm_valid"
    assert validator.calls == 2
    assert TaskValidationBackoffStore(temp_db).get(task.id) is None
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert [(item.iteration, item.status) for item in history] == [
        (1, "error"),
        (2, "valid"),
    ]


@pytest.mark.asyncio
async def test_fifth_consecutive_infra_failure_escalates(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    validator = _ScriptedValidator([CloseVerdictParseError("invalid JSON")])
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    result: lifecycle.ValidationResult | None = None
    with patch.object(lifecycle, "coordinate_task_escalation", return_value="event-1") as notify:
        for _attempt in range(MAX_CONSECUTIVE_INFRA_FAILURES):
            current = manager.get_task(task.id)
            assert current is not None
            result = await _evaluate(current, manager, validator)
            _Clock.current += timedelta(seconds=121)

    assert result is not None
    assert result.extra["retryable"] is False
    assert result.extra["consecutive_failures"] == MAX_CONSECUTIVE_INFRA_FAILURES
    assert result.extra["escalated"] is True
    notify.assert_called_once()
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    assert refreshed.is_escalated is True
    assert refreshed.validation_fail_count == 0


@pytest.mark.asyncio
async def test_total_timeout_expiry_fails_closed_into_backoff(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired provider chain must back off, not count against the task.

    validate_task bounds the chain with close_review_total_timeout_seconds, and
    the service raises FeatureGenerationUnavailableError when it expires
    (#20866). That is infrastructure saying nothing, so the close records a
    backoff window and leaves validation_fail_count alone -- counting it would
    walk an unreachable provider straight into escalation.
    """
    manager = LocalTaskManager(temp_db)
    task = _make_leaf_task(manager, sample_project["id"])
    expiry = FeatureGenerationUnavailableError("JSON generation exceeded total timeout (120s)")
    validator = _ScriptedValidator([expiry])
    _Clock.current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: _Clock.current)

    result = await _evaluate(task, manager, validator)

    assert result.can_close is False
    assert result.error_type == "validation_infrastructure_unavailable"
    assert result.extra["retry_after"] == 15
    assert "exceeded total timeout" in str(result.message)
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    assert refreshed.validation_fail_count == 0, "an unreachable provider is not a failed criterion"
    assert refreshed.is_escalated is False
    state = TaskValidationBackoffStore(temp_db).get(task.id)
    assert state is not None and state.consecutive_failures == 1
    assert state.consecutive_failures < MAX_CONSECUTIVE_INFRA_FAILURES
