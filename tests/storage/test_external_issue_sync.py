"""Tests for durable external issue reconciliation status."""

from datetime import UTC, datetime
from typing import Never

import pytest

from gobby.storage.external_issue_sync import (
    ExternalIssueSyncStatus,
    ExternalIssueSyncStatusStore,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_status_upsert_round_trip(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(name="sync-status", repo_path=None)
    store = ExternalIssueSyncStatusStore(temp_db)
    attempted_at = datetime(2026, 7, 21, tzinfo=UTC)

    def fail_refetch(*args: object, **kwargs: object) -> Never:
        raise AssertionError("upsert must return the row from RETURNING")

    monkeypatch.setattr(store, "get", fail_refetch)

    status = store.upsert(
        project_id=project.id,
        provider="linear",
        state="degraded",
        linked_count=4,
        pending_count=2,
        last_attempt_at=attempted_at,
        consecutive_failures=3,
        last_statistics={"created": 0},
        last_error="pending work remains",
    )

    assert status.state == "degraded"
    assert status.last_attempt_at == attempted_at
    assert status.linked_count == 4
    assert status.pending_count == 2
    assert status.consecutive_failures == 3
    assert status.last_statistics == {"created": 0}
    assert status.last_error == "pending work remains"


@pytest.mark.parametrize(
    ("raw_statistics", "expected"),
    [
        pytest.param('{"created": 2}', {"created": 2}, id="valid"),
        pytest.param("{malformed", {}, id="malformed"),
    ],
)
def test_status_from_row_handles_statistics_json(
    raw_statistics: str,
    expected: dict[str, int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    caplog.set_level("WARNING", logger="gobby.storage.external_issue_sync")

    status = ExternalIssueSyncStatus.from_row(
        {
            "project_id": "project-1",
            "provider": "linear",
            "state": "healthy",
            "last_attempt_at": now,
            "last_success_at": now,
            "linked_count": 2,
            "pending_count": 0,
            "consecutive_failures": 0,
            "retry_at": None,
            "last_statistics": raw_statistics,
            "last_error": None,
            "updated_at": now,
        }
    )

    assert status.last_statistics == expected
    if raw_statistics == "{malformed":
        assert "Ignoring malformed external issue sync statistics" in caplog.text


def test_status_upsert_requires_returned_row(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(name="sync-status-no-row", repo_path=None)
    store = ExternalIssueSyncStatusStore(temp_db)
    monkeypatch.setattr(temp_db, "fetchone", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="upsert returned no row"):
        store.upsert(
            project_id=project.id,
            provider="linear",
            state="healthy",
            linked_count=0,
            pending_count=0,
        )


def test_provider_counts_are_project_scoped(temp_db: HubDatabase) -> None:
    project_manager = LocalProjectManager(temp_db)
    first = project_manager.create(name="first-sync-counts", repo_path=None)
    second = project_manager.create(name="second-sync-counts", repo_path=None)
    tasks = LocalTaskManager(temp_db)
    validation_criteria = "External issue linkage counts are observable."
    tasks.create_task(
        project_id=first.id,
        title="Pending Linear",
        validation_criteria=validation_criteria,
    )
    tasks.create_task(
        project_id=first.id,
        title="Linked everywhere",
        linear_issue_id="linear-1",
        github_repo="owner/repo",
        github_issue_number=1,
        validation_criteria=validation_criteria,
    )
    tasks.create_task(
        project_id=second.id,
        title="Other project",
        linear_issue_id="linear-2",
        github_repo="owner/other",
        github_issue_number=2,
        validation_criteria=validation_criteria,
    )
    store = ExternalIssueSyncStatusStore(temp_db)

    assert store.counts(first.id, "linear") == (1, 1)
    assert store.counts(first.id, "github") == (1, 0)
