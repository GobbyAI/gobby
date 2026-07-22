"""Tests for durable external issue reconciliation status."""

from datetime import UTC, datetime

from gobby.storage.external_issue_sync import ExternalIssueSyncStatusStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager


def test_status_upsert_round_trip(temp_db: HubDatabase) -> None:
    project = LocalProjectManager(temp_db).create(name="sync-status", repo_path=None)
    store = ExternalIssueSyncStatusStore(temp_db)
    attempted_at = datetime(2026, 7, 21, tzinfo=UTC)

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


def test_provider_counts_are_project_scoped(temp_db: HubDatabase) -> None:
    project_manager = LocalProjectManager(temp_db)
    first = project_manager.create(name="first-sync-counts", repo_path=None)
    second = project_manager.create(name="second-sync-counts", repo_path=None)
    tasks = LocalTaskManager(temp_db)
    tasks.create_task(project_id=first.id, title="Pending Linear")
    tasks.create_task(
        project_id=first.id,
        title="Linked everywhere",
        linear_issue_id="linear-1",
        github_repo="owner/repo",
        github_issue_number=1,
    )
    tasks.create_task(
        project_id=second.id,
        title="Other project",
        linear_issue_id="linear-2",
        github_repo="owner/other",
        github_issue_number=2,
    )
    store = ExternalIssueSyncStatusStore(temp_db)

    assert store.counts(first.id, "linear") == (1, 1)
    assert store.counts(first.id, "github") == (1, 0)
