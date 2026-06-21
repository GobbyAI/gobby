"""Tests for task validation infrastructure-failure backoff storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._validation_backoff import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_CONSECUTIVE_INFRA_FAILURES,
    TaskValidationBackoffStore,
    compute_next_retry_at,
)

pytestmark = pytest.mark.unit


def test_compute_next_retry_at_is_exponential_and_capped() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert compute_next_retry_at(1, now) == now + timedelta(seconds=BASE_BACKOFF_SECONDS)
    assert compute_next_retry_at(2, now) == now + timedelta(seconds=BASE_BACKOFF_SECONDS * 2)
    assert compute_next_retry_at(3, now) == now + timedelta(seconds=BASE_BACKOFF_SECONDS * 4)
    # Caps at MAX_BACKOFF_SECONDS for large failure counts.
    assert compute_next_retry_at(99, now) == now + timedelta(seconds=MAX_BACKOFF_SECONDS)


def test_record_failure_increments_and_schedules_retry(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Backoff task",
    )
    store = TaskValidationBackoffStore(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert store.get(task.id) is None

    first = store.record_failure(task.id, error="timeout", now=now)
    assert first.consecutive_failures == 1
    assert first.next_retry_at == now + timedelta(seconds=BASE_BACKOFF_SECONDS)
    assert first.is_in_backoff_window(now)
    assert not first.is_in_backoff_window(first.next_retry_at + timedelta(seconds=1))
    assert not first.should_escalate()

    second = store.record_failure(task.id, error="timeout again", now=now)
    assert second.consecutive_failures == 2
    assert second.last_error == "timeout again"

    persisted = store.get(task.id)
    assert persisted is not None
    assert persisted.consecutive_failures == 2


def test_get_rejects_unexpected_non_mapping_row_shape() -> None:
    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("task-1",)

    class FakeConn:
        def execute(self, _sql: str, _params: tuple[str]) -> Cursor:
            return Cursor()

    class FakeDb:
        def transaction(self) -> FakeDb:
            return self

        def __enter__(self) -> FakeConn:
            return FakeConn()

        def __exit__(self, *_exc: object) -> None:
            return None

    store = TaskValidationBackoffStore(FakeDb())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="expected Mapping"):
        store.get("task-1")


def test_should_escalate_after_threshold(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Escalate task",
    )
    store = TaskValidationBackoffStore(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    state = None
    for _ in range(MAX_CONSECUTIVE_INFRA_FAILURES):
        state = store.record_failure(task.id, error="infra", now=now)
    assert state is not None
    assert state.consecutive_failures == MAX_CONSECUTIVE_INFRA_FAILURES
    assert state.should_escalate()


def test_clear_resets_backoff(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Reset task",
    )
    store = TaskValidationBackoffStore(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    store.record_failure(task.id, error="infra", now=now)
    assert store.get(task.id) is not None

    assert store.clear(task.id) is True
    assert store.get(task.id) is None

    # A fresh failure after a reset starts the counter over at 1.
    restarted = store.record_failure(task.id, error="infra", now=now)
    assert restarted.consecutive_failures == 1
