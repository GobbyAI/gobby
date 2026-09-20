"""Backoff contracts for close-review infrastructure failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    account_criteria_verdict,
    active_validation_backoff,
    record_validation_infrastructure_failure,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._validation_backoff import (
    MAX_CONSECUTIVE_INFRA_FAILURES,
    TaskValidationBackoffStore,
    compute_next_retry_at,
)
from gobby.tasks.close_verdict import CloseCriterionVerdict, CloseVerdict

pytestmark = pytest.mark.integration


def _task(manager: LocalTaskManager, project_id: str) -> Task:
    return manager.create_task(
        project_id=project_id,
        title="Close reviewer infrastructure",
        category="code",
        validation_criteria="The reviewer returns a verdict.",
    )


def _ctx(manager: LocalTaskManager) -> RegistryContext:
    return cast(RegistryContext, SimpleNamespace(task_manager=manager))


def test_backoff_caps_retry_at_120_seconds() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert compute_next_retry_at(100, now) - now == timedelta(seconds=120)


def test_infrastructure_failure_records_an_active_retry_window(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    ctx = _ctx(manager)

    result = record_validation_infrastructure_failure(
        task,
        ctx,
        resolved_id=task.id,
        message="reviewer provider unavailable",
    )
    refreshed = manager.get_task(task.id)

    assert result.error_type == "validation_infrastructure_unavailable"
    assert result.extra["retryable"] is True
    assert refreshed is not None
    blocked = active_validation_backoff(refreshed, ctx)
    assert blocked is not None
    assert blocked.extra["retry_after"] >= 1


def test_valid_review_clears_prior_infrastructure_backoff(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    ctx = _ctx(manager)
    record_validation_infrastructure_failure(
        task,
        ctx,
        resolved_id=task.id,
        message="temporary provider failure",
    )
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    verdict = CloseVerdict(
        status="valid",
        criteria=(
            CloseCriterionVerdict(
                index=1,
                criterion="The reviewer returns a verdict.",
                satisfied=True,
                gap=None,
            ),
        ),
        feedback="valid",
    )

    result = account_criteria_verdict(
        task=refreshed,
        verdict=verdict,
        ctx=ctx,
        resolved_id=task.id,
        validation_config=None,
        reset_reason="close_review_valid",
    )

    assert result.can_close is True
    assert TaskValidationBackoffStore(temp_db).get(task.id) is None


def test_repeated_infrastructure_failures_escalate(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    ctx = _ctx(manager)
    result = None
    for attempt in range(MAX_CONSECUTIVE_INFRA_FAILURES):
        current = manager.get_task(task.id)
        assert current is not None
        result = record_validation_infrastructure_failure(
            current,
            ctx,
            resolved_id=task.id,
            message=f"reviewer launch failed {attempt + 1}",
        )

    assert result is not None
    assert result.extra["retryable"] is False
    assert result.extra["escalated"] is True
    escalated = manager.get_task(task.id)
    assert escalated is not None and escalated.is_escalated is True
